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
import random
import sys
import threading
import time
from dataclasses import dataclass, field

from . import http_pool, usage_log
from .chunking import chunk_text
from .gliner_ner import _DEFAULT_LABEL_MAP
from .llm import Cancelled
from .spans import Span


def _retry_jitter() -> float:
    """Случайный множитель бэкоффа в [0.5, 1.5) — без него 16 воркеров,
    упавших в один и тот же момент, просыпаются синхронно (все через 0.4 с,
    потом все через 1.2 с) и бьют по API одинаковым повторным всплеском.
    Вынесено в отдельную функцию модуля, а не inline random.uniform, чтобы
    тесты могли подменить именно её (gliner_remote._retry_jitter) и сделать
    бэкофф детерминированным."""
    return random.uniform(0.5, 1.5)


def _sleep(seconds: float) -> None:
    """Обёртка над time.sleep для паузы между повторами. Отдельная функция
    модуля, а не прямой вызов time.sleep — тесты подменяют
    gliner_remote._sleep, а не сам time.sleep: патч последнего мутировал бы
    стандартный модуль time целиком на весь процесс (не только для этого
    модуля), что раньше и происходило в тестах (см. test_gliner_remote.py /
    test_http_pool.py)."""
    time.sleep(seconds)


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
        retries: Число повторов ПОСЛЕ первой попытки — но ТОЛЬКО при HTTP
            5xx — итого до ``retries + 1`` попыток на кусок. Прочие статусы
            (4xx, включая 429) НЕ повторяются: они детерминированы (неверный
            ключ, битый payload) или означают «притормози», а не «повтори
            то же самое» (429), так что повтор либо ничего не чинит, либо
            прямо вредит.

            ОБРЫВ СОЕДИНЕНИЯ/DNS (``OSError``, включая
            `http_pool.PoolConnectionError`) ЗДЕСЬ НЕ ПОВТОРЯЕТСЯ ВООБЩЕ —
            это осознанно, а не упущение. `http_pool.post_json` уже сам
            повторяет ровно эти классы сбоев (см. докстринг модуля и
            `_DNS_RETRYABLE` в http_pool.py) и по контракту представляет
            ОДИН логический вызов независимо от числа внутренних попыток.
            До этой правки здесь был второй слой ретраев поверх первого:
            `PoolConnectionError` — подкласс `OSError`, и `except OSError`
            в `_extract` перехватывал уже полностью исчерпанные попытки
            `post_json` и начинал ретраить их заново. В проде это утроило
            число обращений к резолверу (3 DNS-попытки `post_json` x 3
            попытки `_extract` = 9 `getaddrinfo()` на кусок) и превратило
            771 обращение к уже перегруженному резолверу в 2313 — все 257
            кусков документа провалились. Единственно верная реакция на
            `OSError` из `post_json` — считать его окончательным и сразу
            бросить `RuntimeError`.

            Бэкофф между попытками — 0.4 с, затем 1.2 с, домноженные на
            случайный джиттер (см. `_extract`/`_wait_before_retry`), чтобы
            воркеры, упавшие одновременно, не просыпались и не били по API
            синхронной пачкой.
        retry_circuit_breaker: Предохранитель на весь вызов `find()`. Как
            только суммарное число полностью провалившихся кусков (по всем
            потокам-воркерам вместе) достигает этого значения, retries для
            ОСТАЛЬНЫХ кусков этого `find()` отключаются — им достаётся
            ровно одна попытка каждому. Смысл: если весь апстрим лежит
            (см. инцидент выше), ретраить каждый из оставшихся кусков
            ещё 2 раза — чистый вред, а не подстраховка. 0 или меньше —
            предохранитель выключен (обычная политика ретраев для всех
            кусков).
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
    retry_circuit_breaker: int = 5
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
        # Состояние предохранителя (см. RemoteGLiNERConfig.retry_circuit_breaker)
        # — сбрасывается заново в начале КАЖДОГО find(), см. там же.
        # Инициализируется и здесь на случай прямого вызова _extract() в
        # обход find() (как делают некоторые тесты) — тогда предохранитель
        # просто всегда выключен, что совпадает с обычной политикой ретраев.
        self._circuit_breaker_lock = threading.Lock()
        self._circuit_breaker_failures = 0

    def find(self, text: str) -> list[Span]:
        self.warnings = []
        # Предохранитель общий на все воркер-потоки этого find() — свежий
        # на каждый вызов, как и self.warnings выше (см.
        # RemoteGLiNERConfig.retry_circuit_breaker и _bump_circuit_breaker).
        self._circuit_breaker_lock = threading.Lock()
        self._circuit_breaker_failures = 0
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
    # Итоговая пауза домножается на _retry_jitter() (см. ниже).
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
        backoff *= _retry_jitter()
        _sleep(backoff)

    def _circuit_breaker_tripped(self) -> bool:
        """True, если предохранитель на этот find() уже сработал (см.
        RemoteGLiNERConfig.retry_circuit_breaker) — тогда текущему куску
        достаётся ровно одна попытка, без ретраев."""
        cfg = self.config
        if cfg.retry_circuit_breaker <= 0:
            return False
        with self._circuit_breaker_lock:
            return self._circuit_breaker_failures >= cfg.retry_circuit_breaker

    def _bump_circuit_breaker(self) -> None:
        """Учитывает один окончательно провалившийся кусок в общем на весь
        find() счётчике. Куски обрабатываются параллельно (см. find(),
        ThreadPoolExecutor), поэтому здесь считается СУММАРНОЕ число отказов
        за вызов, а не число подряд идущих — «подряд» не имеет смысла, когда
        16 воркеров падают одновременно, а не по очереди."""
        with self._circuit_breaker_lock:
            self._circuit_breaker_failures += 1

    def _extract(self, chunk: str) -> list[dict]:
        """До ``retries + 1`` попыток одного вызова /extract (или ровно 1,
        если сработал предохранитель — см.
        RemoteGLiNERConfig.retry_circuit_breaker). Бросает RuntimeError,
        если ВСЕ попытки исчерпаны — find() ловит её по-кускам и продолжает
        с остальными, а не роняет весь документ из-за одного неудачного
        запроса.

        Повторяется ТОЛЬКО HTTP 5xx. HTTP 429 и прочие 4xx (неверный ключ,
        битый payload) НЕ повторяются: 4xx детерминированы — повтор их не
        исправит, только зря сожжёт квоту; 429 значит «ты шлёшь слишком
        часто» — правильный ответ на это «помедленнее», а не «повтори то же
        самое три раза».

        Обрыв соединения (`OSError`, включая `http_pool.PoolConnectionError`
        — он наследует `OSError`) здесь НЕ ПОВТОРЯЕТСЯ вообще, и это
        осознанно, а не пробел. `http_pool.post_json` уже сам ретраит эти
        классы сбоев (мёртвое пуловое соединение — один раз без паузы, DNS
        `gaierror` — до нескольких раз с бэкоффом, см. докстринг
        http_pool.py) и по контракту отдаёт наружу ровно ОДНУ логическую
        попытку независимо от того, сколько внутренних попыток потребовалось.
        Ретраить здесь ещё раз — значит удваивать (а с учётом DNS-ретраев
        внутри post_json — перемножать) число обращений к резолверу поверх
        уже отданных http_pool; именно так в проде 771 обращение к
        перегруженному резолверу превратилось в 2313, и все 257 кусков
        документа провалились разом (см. RemoteGLiNERConfig.retries).

        usage_log.record_call пишется на КАЖДУЮ попытку — это реальные
        оплачиваемые вызовы вышестоящего сервиса (каждый — завершённый
        HTTP round-trip, который post_json вернул нормально), и отчёт по
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
        attempts = 1 if self._circuit_breaker_tripped() else cfg.retries + 1
        for attempt in range(attempts):
            t0 = time.time()
            try:
                status, resp_body = http_pool.post_json(
                    url, body_bytes, headers, cfg.timeout, pool="gliner"
                )
            except OSError as exc:  # post_json уже сам ретраил внутри — это ОДНА исчерпанная попытка, повтора здесь нет (см. докстринг выше)
                usage_log.record_call(
                    "gliner", seconds=time.time() - t0, chars=len(chunk),
                    ok=False, error=str(exc),
                )
                self._bump_circuit_breaker()
                raise RuntimeError(
                    f"GLiNER API {cfg.base_url} недоступен: {exc}"
                ) from exc
            if status != 200:
                body = resp_body[:200].decode("utf-8", "replace")
                usage_log.record_call(
                    "gliner", seconds=time.time() - t0, chars=len(chunk),
                    ok=False, error=f"HTTP {status}: {body}",
                )
                if status >= 500 and attempt + 1 < attempts:
                    self._wait_before_retry(attempt)
                    continue
                self._bump_circuit_breaker()
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
