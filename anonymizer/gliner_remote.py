"""GLiNER через HTTP-API вместо локальной модели.

Тот же контракт, что у :class:`anonymizer.gliner_ner.GLiNERDetector` — метод
``find(text) -> list[Span]`` — но веса не загружаются, torch не нужен, GPU не
нужен. Ради этого и затевалось: с внешним API серверу анонимизатора остаются
только regex-детекторы и HTTP-клиент, то есть пара сотен мегабайт вместо
нескольких гигабайт.

Эндпоинт (проверен вживую 03.08.2026):

    POST {base_url}/extract
    Authorization: Bearer <ключ>
    {"text": "...", "labels": ["person", ...], "threshold": 0.5}
    -> {"entities": [{"text","label","start","end","score"}, ...]}

ВАЖНОЕ ОГРАНИЧЕНИЕ, найденное замером. Сервер принимает до 50 000 символов, но
САМА МОДЕЛЬ видит только первые ~1800 и молча игнорирует остальное. Отвечает при
этом HTTP 200 без единого предупреждения. Замер на тексте, где в каждом
повторении ровно одна персона:

    повторов  20 (1800 симв.) -> person 20   ожидалось  20
    повторов  50 (4500 симв.) -> person 20   ожидалось  50
    повторов 100 (9000 симв.) -> person 20   ожидалось 100

То есть наивная отправка документа целиком потеряла бы ~96% ПДн, и никакой
ошибки бы не возникло. Поэтому текст режется на куски ``max_chars`` (по строкам,
как и для локального GLiNER) — размер по умолчанию взят с запасом от найденной
границы, а не «под лимит API».
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from . import http_pool, usage_log
from .chunking import chunk_text
from .gliner_ner import _DEFAULT_LABEL_MAP
from .llm import Cancelled
from .spans import Span


@dataclass
class RemoteGLiNERConfig:
    """Настройки HTTP-клиента GLiNER.

    Attributes:
        base_url: База сервиса, БЕЗ ``/extract`` на конце.
        api_key: Bearer-токен. Без него шлюз отвечает 401 даже на схему.
        labels: Те же промпты меток, что и у локального детектора.
        threshold: Порог уверенности. У API по умолчанию 0.5, у нас 0.45 —
            как в локальном конфиге: цель в том, чтобы не пропустить, а лишнее
            снимет слой review.
        max_chars: Размер куска. НЕ поднимать до лимита API (50 000): модель
            перестаёт видеть текст после ~1800 символов (см. докстринг модуля).
        timeout: Таймаут одного запроса.
        retries: Число повторов ПОСЛЕ первой попытки при транзиентных сбоях
            (обрыв соединения/`http_pool.PoolConnectionError`, HTTP 5xx, HTTP
            429) — итого до ``retries + 1`` попыток на кусок. В проде из ~70
            кусков документа 3 стабильно валятся на разовых сбоях шлюза/TLS
            под ``concurrency=16`` — при retries они просто пропадают вместо
            того, чтобы терять фрагмент текста насовсем. Прочие 4xx (неверный
            ключ, битый payload) НЕ повторяются — они детерминированы, повтор
            только жжёт квоту впустую. Бэкофф между попытками — 0.4 с, затем
            1.2 с (см. `_extract`).
        label_map: Отображение меток сервиса в метки анонимизатора.
        concurrency: Число одновременных запросов к ``/extract``. Раньше (без
            пула соединений) один вызов целиком занимал ~1.15-1.27 с, хотя
            сама модель отвечает за 10-20 мс, — остальное было накладными
            расходами TLS-хендшейка НА КАЖДЫЙ запрос. Замер это подтвердил
            напрямую: на прогретом переиспользуемом соединении тот же вызов
            занимает ~68 мс — то есть round-trip был не сетевой задержкой как
            таковой, а стоимостью установки НОВОГО соединения. Теперь запросы
            идут через ``http_pool`` (постоянные, переиспользуемые соединения
            — см. докстринг модуля), и это накладные расходы снимает, а не
            просто ускоряет: шлюз, который стабильно держит много ОДНОВРЕМЕННЫХ
            запросов, ложится от резкого всплеска НОВЫХ соединений — именно
            от этого чувствителен последовательный обход без пула. На
            реальном документе ~73 000 символов слой GLiNER делает ~356
            запросов по одному на строку. Все вызовы, независимо от этого
            параметра, дополнительно ограничены общим для процесса пределом
            ``http_pool._INFLIGHT`` (``ANONYMIZER_MAX_INFLIGHT``). 16 остаётся
            без изменений — разумный запас, не поднимаем.
    """

    base_url: str = field(
        default_factory=lambda: os.getenv(
            "ANONYMIZER_GLINER_URL", "https://oui.interfonica.cloud/gliner"
        ).rstrip("/")
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("ANONYMIZER_GLINER_API_KEY")
        or os.getenv("ANONYMIZER_LLM_API_KEY", "")
    )
    labels: tuple[str, ...] = (
        "person", "first name", "last name", "nickname",
        "location", "address", "organization",
    )
    threshold: float = 0.45
    max_chars: int = 800
    timeout: float = 60.0
    retries: int = 2
    label_map: dict = field(default_factory=lambda: dict(_DEFAULT_LABEL_MAP))
    concurrency: int = 16


class RemoteGLiNERDetector:
    """Детектор GLiNER, работающий по HTTP. Интерфейс — как у локального."""

    def __init__(self, config: RemoteGLiNERConfig | None = None) -> None:
        self.config = config or RemoteGLiNERConfig()
        # Заполняется find(), когда кусок текста не удалось обработать (см.
        # _extract) — тот же контракт, что у anonymizer.llm.LLMDetector,
        # чтобы engine.py мог собрать предупреждения с обоих детекторов
        # одинаково (getattr(detector, "warnings", None)).
        self.warnings: list[dict] = []
        # Опциональный крючок кооперативной отмены: None (по умолчанию) —
        # find() ведёт себя ровно как раньше. server.py выставляет его на
        # свежем экземпляре per-request, чтобы воркер job'а мог остановиться
        # между кусками, когда клиент отвалился (см. anonymizer.llm.Cancelled).
        self.cancel_event: threading.Event | None = None

    def find(self, text: str) -> list[Span]:
        self.warnings = []
        if not text.strip():
            return []
        cfg = self.config

        # group=False — по одной строке на кусок, как для локального GLiNER:
        # маленькие сфокусированные входы дают span-модели более высокую
        # полноту, а заодно гарантированно укладываются в её окно.
        chunks = chunk_text(text, cfg.max_chars, group=False)
        if not chunks:
            return []

        # Каждый запрос — это ~1.2 с сетевых накладных расходов и почти
        # ничего вычислений (см. докстринг RemoteGLiNERConfig.concurrency),
        # поэтому куски отправляются параллельно. Результаты собираются по
        # индексу куска, а не по порядку завершения — вывод детерминирован
        # от запуска к запуску независимо от того, какой запрос ответил
        # первым.
        results: list[list[dict] | Exception] = [None] * len(chunks)  # type: ignore[list-item]
        if cfg.concurrency > 1:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=cfg.concurrency
            ) as pool:
                future_to_index = {}
                for i, (_offset, chunk) in enumerate(chunks):
                    if self.cancel_event is not None and self.cancel_event.is_set():
                        raise Cancelled()
                    # run_in_context, НЕ pool.submit напрямую — обычный submit
                    # не копирует contextvars в поток-воркер, и usage_log
                    # потеряет request_id (см. usage_log.run_in_context).
                    future_to_index[usage_log.run_in_context(pool, self._extract, chunk)] = i
                for future in future_to_index:
                    index = future_to_index[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        results[index] = exc
                # Запросы выше уже отработали до конца (in-flight HTTP не
                # прерываем — кусок стоит максимум пару секунд), это лишь не
                # даёт запустить новую партию на job'е, от которого клиент
                # уже отвалился.
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise Cancelled()
        else:
            for i, (_offset, chunk) in enumerate(chunks):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise Cancelled()
                try:
                    results[i] = self._extract(chunk)
                except Exception as exc:  # noqa: BLE001
                    results[i] = exc

        spans: list[Span] = []
        for (offset, chunk), result in zip(chunks, results):
            if isinstance(result, Exception):
                # Текст сообщения намеренно НЕ содержит {result} (адрес API,
                # HTTP-код, текст исключения) — это техническая информация не
                # для конечного пользователя, она остаётся только в stderr
                # ниже (см. задачу «warnings без технических деталей»).
                message = (
                    "Фрагмент текста не удалось проверить. Персональные "
                    "данные в этом фрагменте могли остаться "
                    "незамаскированными."
                )
                self.warnings.append(
                    {
                        "kind": "gliner_chunk_failed",
                        "offset": offset,
                        "chars": len(chunk),
                        "message": message,
                    }
                )
                print(
                    f"[warn] GLiNER: фрагмент {offset}-{offset + len(chunk)} "
                    f"не обработан ({result}) — возможна утечка ПДн",
                    file=sys.stderr,
                )
                continue
            for ent in result:
                label = cfg.label_map.get(str(ent.get("label", "")).lower())
                if label is None:
                    continue  # метка вне области интереса
                try:
                    start = offset + int(ent["start"])
                    end = offset + int(ent["end"])
                except (KeyError, TypeError, ValueError):
                    continue
                if end > start and end <= len(text):
                    spans.append(
                        Span(start, end, label, text[start:end], source="gliner")
                    )
        return spans

    # -- HTTP -----------------------------------------------------------
    # Бэкофф между повторами (см. RemoteGLiNERConfig.retries): 0.4 с перед
    # вторым вызовом, 1.2 с перед третьим. Индекс — номер УЖЕ провалившейся
    # попытки (0 для первой), поэтому этих двух значений хватает на дефолтные
    # retries=2; при большем retries последняя пауза просто повторяется.
    _RETRY_BACKOFF_SECONDS = (0.4, 1.2)

    def _wait_before_retry(self, attempt: int) -> None:
        """Пауза перед повтором. Проверяет cancel_event ДО sleep, чтобы
        отменённая задача не отсиживала бэкофф впустую — тот же контракт
        отмены, что и в find() (см. Cancelled)."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled()
        backoff = self._RETRY_BACKOFF_SECONDS[
            min(attempt, len(self._RETRY_BACKOFF_SECONDS) - 1)
        ]
        time.sleep(backoff)

    def _extract(self, chunk: str) -> list[dict]:
        """До ``retries + 1`` попыток одного вызова /extract. Бросает
        RuntimeError, если ВСЕ попытки исчерпаны — find() ловит её по-кускам
        и продолжает с остальными, а не роняет весь документ из-за одного
        неудачного запроса.

        Повторяются ТОЛЬКО транзиентные сбои: обрыв соединения (OSError, в
        т.ч. http_pool.PoolConnectionError — он наследует OSError), HTTP 5xx,
        HTTP 429. Прочие 4xx (неверный ключ, битый payload) детерминированы —
        повтор их не исправит, только зря сожжёт квоту, поэтому они бросаются
        немедленно. usage_log.record_call пишется на КАЖДУЮ попытку — это
        реальные оплачиваемые вызовы вышестоящего сервиса, и отчёт по
        стоимости должен видеть все, а не только последний.
        """
        cfg = self.config
        payload = {
            "text": chunk,
            "labels": list(cfg.labels),
            "threshold": cfg.threshold,
        }
        url = cfg.base_url + "/extract"
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        }
        attempts = cfg.retries + 1
        for attempt in range(attempts):
            t0 = time.time()
            try:
                status, resp_body = http_pool.post_json(
                    url, body_bytes, headers, cfg.timeout, pool="gliner"
                )
            except OSError as exc:  # connection refused/reset/timeout, dead pooled socket, ...
                usage_log.record_call(
                    "gliner", seconds=time.time() - t0, chars=len(chunk),
                    ok=False, error=str(exc),
                )
                if attempt + 1 < attempts:
                    self._wait_before_retry(attempt)
                    continue
                raise RuntimeError(
                    f"GLiNER API {cfg.base_url} недоступен: {exc}"
                ) from exc
            if status != 200:
                body = resp_body[:200].decode("utf-8", "replace")
                usage_log.record_call(
                    "gliner", seconds=time.time() - t0, chars=len(chunk),
                    ok=False, error=f"HTTP {status}: {body}",
                )
                if (status >= 500 or status == 429) and attempt + 1 < attempts:
                    self._wait_before_retry(attempt)
                    continue
                raise RuntimeError(
                    f"GLiNER API {cfg.base_url} вернул HTTP {status}: {body}"
                )
            data = json.loads(resp_body)
            usage_log.record_call("gliner", seconds=time.time() - t0, chars=len(chunk), ok=True)

            # Сервис отдаёт {"entities": [...]}; на всякий случай принимаем и
            # голый список — контракт у подобных обёрток любит меняться.
            if isinstance(data, dict):
                data = data.get("entities", [])
            return [e for e in data if isinstance(e, dict)]
        # Недостижимо: цикл либо возвращает результат, либо бросает исключение
        # на последней попытке внутри тела выше.
        raise AssertionError("unreachable")
