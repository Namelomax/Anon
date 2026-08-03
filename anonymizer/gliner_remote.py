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
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .chunking import chunk_text
from .gliner_ner import _DEFAULT_LABEL_MAP
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
        label_map: Отображение меток сервиса в метки анонимизатора.
        concurrency: Число одновременных запросов к ``/extract``. Эндпоинт
            упирается в задержку сети, а не в вычисления: сама модель отвечает
            за 10-20 мс, но один вызов целиком занимает ~1.15-1.27 с — это
            накладные расходы round-trip, постоянные независимо от размера
            текста. Замер показал почти линейное ускорение без единой ошибки:
            1 воркер — 40.7 с на 32 вызова, 8 — 5.5 с (7.4x), 16 — 2.8 с
            (14.6x), 32 — 1.9 с (21.9x). На реальном документе ~73 000
            символов слой GLiNER делает ~356 запросов по одному на строку:
            ~460 с последовательно против ~31 с при 16 воркерах. 16 — разумный
            запас: заметно быстрее единицы, но не настолько агрессивно, чтобы
            перегрузить общий шлюз.
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
                future_to_index = {
                    pool.submit(self._extract, chunk): i
                    for i, (_offset, chunk) in enumerate(chunks)
                }
                for future in future_to_index:
                    index = future_to_index[future]
                    try:
                        results[index] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        results[index] = exc
        else:
            for i, (_offset, chunk) in enumerate(chunks):
                try:
                    results[i] = self._extract(chunk)
                except Exception as exc:  # noqa: BLE001
                    results[i] = exc

        spans: list[Span] = []
        for (offset, chunk), result in zip(chunks, results):
            if isinstance(result, Exception):
                message = (
                    "Фрагмент текста не удалось проверить через GLiNER API "
                    f"({result}). Персональные данные в этом фрагменте могли "
                    "остаться незамаскированными."
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
    def _extract(self, chunk: str) -> list[dict]:
        """Один вызов /extract. Бросает RuntimeError при ошибке HTTP —
        find() ловит её по-кускам и продолжает с остальными, а не роняет
        весь документ из-за одного неудачного запроса."""
        cfg = self.config
        payload = {
            "text": chunk,
            "labels": list(cfg.labels),
            "threshold": cfg.threshold,
        }
        req = urllib.request.Request(
            cfg.base_url + "/extract",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read()[:200].decode("utf-8", "replace")
            raise RuntimeError(
                f"GLiNER API {cfg.base_url} вернул HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"GLiNER API {cfg.base_url} недоступен: {exc}"
            ) from exc

        # Сервис отдаёт {"entities": [...]}; на всякий случай принимаем и голый
        # список — контракт у подобных обёрток любит меняться.
        if isinstance(data, dict):
            data = data.get("entities", [])
        return [e for e in data if isinstance(e, dict)]
