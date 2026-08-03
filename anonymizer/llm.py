"""LLM gap-filler detector via an OpenAI-compatible local server.

This is the third detection layer, on top of regex and NER. It catches what
deterministic rules and Natasha miss: digits written as words ("СНИЛС семь
восемь девять…"), context-less identifiers, Latin-script or out-of-order names,
streets and house numbers. Everything stays local (LM Studio / Ollama) so no
data leaves the machine.

Design contract that keeps anonymization reversible and safe:

* The model only *detects* — it returns exact substrings, never offsets or
  rewrites. We locate each substring in the original text ourselves and build
  ``Span`` objects, so placeholder substitution and the mapping remain
  deterministic.
* Anything the model returns that cannot be located verbatim (after whitespace
  normalization) is dropped — we never mask text we can't pin down.
* The reply is parsed from ``content`` or, for reasoning models that exhaust
  the token budget mid-think, from ``reasoning_content`` as a fallback. The
  model is asked for a compact ``ТИП|текст`` line format; the old JSON-array
  format is still accepted as a fallback.

Works with LM Studio (``http://127.0.0.1:1234/v1``) and Ollama
(``http://127.0.0.1:11434/v1``); both expose the same chat-completions API.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .chunking import chunk_text
from .spans import Span

# Disables model "thinking"/reasoning tokens before the reply. Both keys are
# sent together because different servers honor different ones:
# llama.cpp/LM Studio (the gemma family) only listens to
# ``chat_template_kwargs.enable_thinking``; Ollama/Qwen looks at
# ``reasoning_effort``. Servers ignore extra keys they don't recognize, so
# sending both is harmless and covers both backends with one constant.
NO_THINKING_EXTRA_BODY: dict = {
    "reasoning_effort": "none",
    "chat_template_kwargs": {"enable_thinking": False},
}

# LLM output type (upper-cased) -> anonymizer label.
_TYPE_MAP: dict[str, str] = {
    "FIRST_NAME": "PERSON",
    "LAST_NAME": "PERSON",
    "MIDDLE_NAME": "PERSON",
    "NAME": "PERSON",
    "PER": "PERSON",
    "COUNTRY": "LOCATION",
    "REGION": "LOCATION",
    "DISTRICT": "LOCATION",
    "CITY": "LOCATION",
    "STREET": "LOCATION",
    "HOUSE": "LOCATION",
    "ADDRESS": "LOCATION",
    "LOC": "LOCATION",
    "ORGANIZATION": "ORG",
    "COMPANY": "ORG",
    "ORG": "ORG",
    "AMOUNT": "AMOUNT",
    "MONEY": "AMOUNT",
    "SUM": "AMOUNT",
    "PRICE": "AMOUNT",
    "SUBJECT": "SUBJECT",
    "GOODS": "SUBJECT",
    "PRODUCT": "SUBJECT",
    "ITEM": "SUBJECT",
    "SERVICE": "SUBJECT",
    "PRODUCT_NAME": "SUBJECT",
}

def _build_system_prompt(allowed_labels: frozenset) -> str:
    """System prompt listing exactly the types the model is allowed to emit."""
    types = ", ".join(sorted(allowed_labels))
    org_on = "ORG" in allowed_labels
    amount_on = "AMOUNT" in allowed_labels
    subject_on = "SUBJECT" in allowed_labels
    # When ORG is requested we DO want organization names; otherwise we tell the
    # model to skip them (keeps PII-only mode precise).
    org_rule = (
        "Тип ORG — названия организаций, компаний, команд и групп (в т.ч. в "
        "кавычках, в любом падеже): аббревиатуры («КФК»), латиницей "
        "(«Dream Team»), без кавычек и организационно-правовой формы («Форус», "
        "«группа компаний Форус» — пометь «Форус»), в том числе внутри названий "
        "файлов и заголовков.\n"
        if org_on else
        "НЕ помечай названия организаций и компаний.\n"
    )
    # In corporate mode the LLM (not regex) is responsible for monetary sums.
    amount_rule = (
        "Тип AMOUNT — ТОЛЬКО денежные суммы и цены в валюте (рубли, руб., ₽, $, "
        "€), например «4,7 млрд руб.», «500 000 рублей», «18% годовых», «$1000», "
        "в том числе записанные словами. "
        "НЕ помечай как AMOUNT: даты и сроки, время (10:00–12:30), количества "
        "(штук, человек, заказов, ТС, Мбит), номера и голые числа без валюты, "
        "проценты без денежного смысла.\n"
        if amount_on else ""
    )
    # Contract subject matter: only requested for corporate documents where the
    # nomenclature itself (not just parties/amounts) can leak the industry.
    subject_rule = (
        "Тип SUBJECT — предмет договора: конкретные наименования товаров, работ "
        "и услуг — модели, марки, типы изделий, номенклатурные обозначения "
        "(например «автомат Калашникова АК-74», «станок ЧПУ Haas VF-2»), а также "
        "родовые слова с явной отраслевой спецификой («вооружение», "
        "«боеприпасы», «медицинское оборудование»). НЕ помечай служебную лексику "
        "договора: «товар», «продукция», «услуги», «работы», «поставка», а также "
        "«оборудование» без уточняющего определения. Предметов может быть "
        "несколько — каждый отдельным объектом; если предмета в тексте нет, "
        "не возвращай ничего по этому типу и не выдумывай.\n"
        if subject_on else ""
    )
    return (
        "Ты — детектор персональных данных (PII) в русском тексте. "
        "Найди ВСЕ персональные данные. Особое внимание удели тому, что трудно "
        "поймать регулярными выражениями: числа, записанные словами (например "
        "«семь восемь девять»), номера документов без подсказывающих слов, имена "
        "и фамилии латиницей или в необычном порядке (в любом падеже), улицы и "
        "номера домов.\n"
        "Тип PERSON — КАЖДОЕ упоминание человека, отдельным объектом на каждую "
        "формулировку: полное ФИО, имя без фамилии («Никита»), фамилия без имени, "
        "уменьшительные и краткие формы («Катя», «Рома»), позывные и ники, "
        "обращения по имени внутри реплики («Никита, добрый день»), имена в "
        "заголовках, названиях файлов и записей (например внутри "
        "«Запись_Встреча_с_Ивановым.mp4» пометь «Ивановым»). Если один и тот же "
        "человек упомянут по-разному («Иванов Никита», «Никита», «Иванов») — "
        "верни КАЖДЫЙ вариант написания отдельным объектом. Текст может быть "
        "расшифровкой речи: имена бывают искажены, разорваны лишней запятой "
        "(«Капитан Яков, Вайгус» — имя и фамилия одного человека) или написаны "
        "с ошибкой — помечай их как PERSON в том виде, как они записаны, даже "
        "если написание выглядит странно. Звание/должность перед именем "
        "(«Капитан», «директор») в text не включай — только само имя.\n"
        "Текст может быть СЛИПШИМСЯ (без пробелов) или с ошибками извлечения из "
        "файла: «ИвановИ.И.», «Москва101000», «годаг.». Всё равно находи ПДн и "
        "возвращай точную подстроку, как она записана (даже слипшуюся).\n"
        "Верни результат построчно, БЕЗ JSON, БЕЗ markdown-разметки и code fences, "
        "БЕЗ пояснений: одна сущность на строку в формате «ТИП|текст», где «|» "
        "отделяет тип от текста, например:\n"
        "PERSON|Иванов Иван Петрович\n"
        "PHONE|+7 916 123-45-67\n"
        f"Допустимые типы (используй ТОЛЬКО их): {types}.\n"
        "Текст после «|» должен ДОСЛОВНО совпадать с фрагментом исходного текста "
        "(те же символы, регистр и пробелы). Не перефразируй, не нормализуй числа.\n"
        + org_rule + amount_rule + subject_rule +
        "НЕ помечай: слова-категории сами по себе (паспорт, СНИЛС, ИНН, полис, "
        "серия, номер, телефон, почта, адрес); должности и роли (директор, "
        "фрилансер, бухгалтер); даты; обычные слова и глаголы. Помечай только "
        "конкретные ЗНАЧЕНИЯ.\n"
        "ВАЖНО: если помечаешь значение-идентификатор (номер счёта, документа, "
        "кода, филиала, реквизит), выделяй его ЦЕЛИКОМ, со всеми цифрами и без "
        "пропусков — не оставляй часть цифр незамаскированной. Реквизиты банка "
        "(название банка, номер филиала/отделения) — это конфиденциальные "
        "значения, помечай их.\n"
        "Если документ относится к водительскому удостоверению (рядом есть слова "
        "«водитель», «права», «ВУ», «вождение»), используй тип DRIVER_LICENSE, а "
        "не PASSPORT.\n"
        "Если ничего не найдено — верни ТОЛЬКО слово NONE (без кавычек) отдельной "
        "строкой и больше ничего. Никаких пояснений, только строки «ТИП|текст» "
        "либо, если ничего не найдено, одно слово NONE."
    )

# Default label whitelist; anything else the model emits (ORGANIZATION, DATE...)
# is dropped. Override via LLMConfig.allowed_labels (e.g. add ORG for the
# corporate-document use case).
_DEFAULT_ALLOWED = frozenset({
    "PERSON", "LOCATION", "PHONE", "EMAIL", "URL", "IP_ADDRESS", "CREDIT_CARD",
    "INN", "SNILS", "OMS", "PASSPORT", "DRIVER_LICENSE", "MILITARY_ID",
    "BIRTH_CERTIFICATE",
})

# Bare trigger/label words the model sometimes returns as if they were values.
_STOP_WORDS = frozenset({
    "паспорт", "паспорта", "снилс", "инн", "омс", "полис", "серия", "серии",
    "номер", "номера", "телефон", "телефона", "почта", "почты", "адрес",
    "email", "e-mail", "имя", "фамилия", "отчество", "дата",
})


@dataclass
class LLMConfig:
    """Connection and decoding settings for the local LLM server.

    Attributes:
        base_url: OpenAI-compatible base, e.g. ``http://127.0.0.1:1234/v1``
            (LM Studio) or ``http://127.0.0.1:11434/v1`` (Ollama).
        model: Model id as the server reports it (``/v1/models``).
        max_tokens: Output budget for the ``ТИП|текст`` reply. With thinking
            genuinely disabled (see ``NO_THINKING_EXTRA_BODY``), real replies on
            this pipeline run 117-600 completion tokens, so 1500 is ample
            headroom. Kept deliberately small: if the model still degenerates
            into a reasoning loop despite the "no thinking" flags, a small
            budget bounds the wasted time (~45s instead of ~245s) instead of
            silently burning the whole request on empty output.
        temperature: 0 for deterministic extraction.
        timeout: Per-request seconds. Reasoning models can be slow on CPU/GPU.
        api_key: Sent as Bearer; ignored by LM Studio/Ollama but required shape.
        extra_body: Merged into the request JSON. Use it to disable thinking,
            e.g. ``NO_THINKING_EXTRA_BODY`` (module-level constant, covers both
            LM Studio/llama.cpp's ``chat_template_kwargs.enable_thinking`` and
            Ollama/Qwen's ``reasoning_effort``) or, on Ollama's native path,
            ``{"think": False}``.
        allowed_labels: Whitelist of labels to keep; others are dropped. Defaults
            to the canonical PII set (no ORGANIZATION/DATE).
        max_chars: Documents are split into chunks of at most this many chars
            before being sent to the model. Smaller chunks raise recall (the
            model sees less at once, so declined word-forms aren't missed) at the
            cost of more API calls.
        stop: Stop sequences sent to the server. The model's known failure mode
            is degenerating into a reasoning block of endless blank lines until
            the token budget runs out (~47s wasted on a single call, measured);
            four consecutive newlines never occur in a valid ``ТИП|текст`` reply
            (nor in the ``NONE`` sentinel), so this cuts a degenerate call off
            after a few tokens instead of burning the full ``max_tokens``
            budget.
        concurrency: Number of chunks sent to the server concurrently. Default
            is 1 (sequential) DELIBERATELY: the local LM Studio/Ollama path
            typically serves a single contended GPU, where concurrent requests
            just queue up behind each other and add nothing but request
            overhead. Only raise this for a remote endpoint that itself scales
            under concurrency (measured on the chat endpoint used elsewhere in
            this pipeline: ~1.23 s fixed overhead per call, throughput per
            call roughly unchanged from 1 to 8 concurrent callers — the
            backend batches).
    """

    base_url: str = "http://127.0.0.1:11433/v1"
    model: str = "qwen3.5:9b"
    max_tokens: int = 1500
    temperature: float = 0.0
    timeout: float = 300.0
    api_key: str = "not-needed"
    extra_body: dict = field(default_factory=dict)
    allowed_labels: frozenset = _DEFAULT_ALLOWED
    max_chars: int = 3000
    stop: tuple[str, ...] = ("\n\n\n\n",)
    concurrency: int = 1


class _DegenerateReply(Exception):
    """Raised by ``_complete`` when the model exhausted its token budget on a
    reasoning block and returned no usable content, even after one retry."""


class LLMDetector:
    """Detector that asks a local LLM for PII substrings and locates them."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        self._system = _build_system_prompt(self.config.allowed_labels)
        # Populated by find() when a chunk could not be analyzed at all (see
        # _DegenerateReply) — surfaced to callers so the data-loss isn't silent.
        self.warnings: list[dict] = []

    def find(self, text: str) -> list[Span]:
        self.warnings = []
        if not text.strip():
            return []
        # Chunk long documents: smaller inputs keep the model's recall high
        # (declined word-forms aren't missed) and avoid output truncation.
        chunks = chunk_text(text, self.config.max_chars)
        if not chunks:
            return []

        # Completed sequentially by default (see LLMConfig.concurrency — the
        # local GPU path gains nothing from concurrent requests) or via a
        # thread pool for remote endpoints that scale under concurrency.
        # Either way results are collected by chunk index, not completion
        # order, so the output is deterministic regardless of which request
        # answers first.
        results: list[str | _DegenerateReply] = [None] * len(chunks)  # type: ignore[list-item]
        if self.config.concurrency > 1:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.config.concurrency
            ) as pool:
                future_to_index = {
                    pool.submit(self._complete, chunk): i
                    for i, (_offset, chunk) in enumerate(chunks)
                }
                for future in future_to_index:
                    index = future_to_index[future]
                    try:
                        results[index] = future.result()
                    except _DegenerateReply as exc:
                        results[index] = exc
        else:
            for i, (_offset, chunk) in enumerate(chunks):
                try:
                    results[i] = self._complete(chunk)
                except _DegenerateReply as exc:
                    results[i] = exc

        spans: list[Span] = []
        for (offset, chunk), result in zip(chunks, results):
            if isinstance(result, _DegenerateReply):
                message = (
                    "Фрагмент текста не удалось проанализировать с помощью LLM "
                    "(модель дважды вернула пустой ответ, исчерпав лимит "
                    "токенов на «размышления»). Персональные данные в этом "
                    "фрагменте могли остаться незамаскированными."
                )
                self.warnings.append(
                    {
                        "kind": "llm_chunk_failed",
                        "offset": offset,
                        "chars": len(chunk),
                        "message": message,
                    }
                )
                print(
                    f"[warn] LLM: фрагмент {offset}-{offset + len(chunk)} "
                    "не обработан (пустой ответ после повторной попытки) — "
                    "возможна утечка ПДн",
                    file=sys.stderr,
                )
                continue
            content = result
            if _is_none_reply(content):
                # Explicit "nothing found" sentinel (see _build_system_prompt)
                # — legitimately zero entities, not a failure to record.
                continue
            items = _parse_items(content)
            spans.extend(self._locate(chunk, offset, items))
        return spans

    # -- HTTP -----------------------------------------------------------
    def _complete(self, text: str) -> str:
        """Ask the model, retrying once if it degenerates (see ``_is_degenerate``).

        Raises ``_DegenerateReply`` if the retry also degenerates — the caller
        (``find``) is responsible for recording the loss and moving on.
        """
        content, _finish_reason = self._request_once(text)
        if _is_degenerate(content):
            content, _finish_reason = self._request_once(text)
            if _is_degenerate(content):
                raise _DegenerateReply()
        return content

    def _request_once(self, text: str) -> tuple[str, str | None]:
        cfg = self.config
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": text},
            ],
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            # см. комментарий в review._ask
            "presence_penalty": 0,
            "frequency_penalty": 0,
            **({"stop": list(cfg.stop)} if cfg.stop else {}),
            **cfg.extra_body,
        }
        req = urllib.request.Request(
            cfg.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.load(resp)
        except urllib.error.URLError as exc:  # connection refused, timeout, ...
            raise RuntimeError(
                f"LLM request to {cfg.base_url} failed: {exc}. "
                "Is LM Studio / Ollama running and the model loaded?"
            ) from exc
        choice = data["choices"][0]
        msg = choice["message"]
        finish_reason = choice.get("finish_reason")
        # Prefer the answer; fall back to reasoning text if content is empty
        # (reasoning model ran out of budget before emitting content).
        content = msg.get("content") or msg.get("reasoning_content") or ""
        return content, finish_reason

    # -- Locate substrings ---------------------------------------------
    def _locate(self, chunk: str, offset: int, items: list[tuple[str, str]]) -> list[Span]:
        """Find each returned substring within ``chunk`` and shift by ``offset``."""
        allowed = self.config.allowed_labels
        spans: list[Span] = []
        for raw_text, raw_type in items:
            label = _normalize_type(raw_type)
            if allowed and label not in allowed:
                continue  # out-of-scope type (ORGANIZATION, DATE, ...)
            if raw_text.strip().casefold() in _STOP_WORDS:
                continue  # bare category/trigger word, not a value
            for start, end in _find_all(chunk, raw_text):
                spans.append(
                    Span(offset + start, offset + end, label, chunk[start:end], source="llm")
                )
        return spans


def _is_degenerate(content: str) -> bool:
    """True if the model's reply carries no usable content, whatever the
    finish reason.

    Before the ``NONE`` sentinel (see ``_build_system_prompt``) existed, an
    empty ``content`` was ambiguous between "no PII found" and "the model
    failed" — and that ambiguity was measured to hide real failures: a
    degenerate call (``finish_reason: "length"``, reasoning collapsed into
    thousands of blank lines) could be immediately followed by a retry that
    came back in 33ms with ``finish_reason: "stop"`` and empty ``content``,
    which the old ``finish_reason == "length"`` gate let through as "nothing
    found". Now that ``NONE`` is the explicit success sentinel, a genuinely
    clean chunk is never empty, so *any* empty/whitespace-only reply is
    unambiguously a failure — independent of ``finish_reason``.

    ``content`` here is already the ``content`` / ``reasoning_content``
    fallback (see ``_request_once``), so a reasoning block that is nothing but
    newlines must still count as empty — hence stripping before the check.
    """
    return not content.strip()


def _is_none_reply(content: str) -> bool:
    """True if ``content``'s only meaningful line is the ``NONE`` sentinel
    (see ``_build_system_prompt``), meaning the model looked and legitimately
    found zero entities — as opposed to an empty/failed reply (``_is_degenerate``).

    Tolerant of surrounding whitespace, case, and ``` code fences, since the
    model sometimes wraps even a single-token reply in a fence despite being
    told not to.
    """
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        lines.append(line.strip("`").strip())
    return len(lines) == 1 and lines[0].casefold() == "none"


# --- Line-format / JSON parsing ---------------------------------------------

# A type label looks like "PERSON" or "DRIVER_LICENSE" — uppercase ASCII and
# underscores only. Used to reject lines where "|" appears inside the text
# itself before the real type (defensive; the split is on the first "|").
_LABEL_RE = re.compile(r"^[A-Za-z_]+$")


def _parse_items(content: str) -> list[tuple[str, str]]:
    """Extract ``(text, type)`` pairs from a model reply.

    Prefers the compact ``ТИП|текст`` line format (one entity per line, no
    JSON scaffolding — see ``_build_system_prompt``). Falls back to the old
    JSON-array format for models that ignore the instruction.
    """
    items = _parse_lines(content)
    if items:
        return items
    return _parse_json(content)


def _parse_lines(content: str) -> list[tuple[str, str]]:
    """Parse ``ТИП|текст`` lines, tolerant of ``` code fences around the reply."""
    items: list[tuple[str, str]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("```"):
            continue  # opening/closing code fence, possibly with a language tag
        if "|" not in line:
            continue
        raw_type, _, raw_text = line.partition("|")
        raw_type = raw_type.strip()
        raw_text = raw_text.strip()
        if not raw_type or not raw_text:
            continue
        if not _LABEL_RE.match(raw_type):
            continue
        items.append((raw_text, raw_type))
    return items


def _parse_json(content: str) -> list[tuple[str, str]]:
    """Extract ``(text, type)`` pairs from a JSON-array reply (tolerant of prose)."""
    blob = _extract_json_array(content)
    if blob is None:
        return []
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    items: list[tuple[str, str]] = []
    for obj in data if isinstance(data, list) else []:
        if not isinstance(obj, dict):
            continue
        t = obj.get("text")
        ty = obj.get("type")
        if isinstance(t, str) and t and isinstance(ty, str):
            items.append((t, ty))
    return items


def _extract_json_array(s: str) -> str | None:
    """Return the last top-level ``[...]`` array substring, or None.

    Reasoning models often write several candidate arrays; the final one is the
    answer. We scan for balanced brackets while respecting JSON string quoting.
    """
    candidates: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "[":
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                c = s[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == "[":
                        depth += 1
                    elif c == "]":
                        depth -= 1
                        if depth == 0:
                            candidates.append(s[i : j + 1])
                            i = j
                            break
                j += 1
        i += 1
    return candidates[-1] if candidates else None


# --- Substring location ----------------------------------------------------

def _find_all(text: str, needle: str) -> list[tuple[int, int]]:
    """Locate every non-overlapping occurrence of ``needle`` in ``text``.

    Tries exact matches first; if none, falls back to a whitespace-insensitive
    match (the model may collapse or add spaces). Returns [] if not found, so
    un-locatable detections are silently skipped — never masked blindly.
    """
    needle = needle.strip()
    if not needle:
        return []

    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            break
        spans.append((idx, idx + len(needle)))
        start = idx + len(needle)
    if spans:
        return spans

    # Whitespace-insensitive fallback.
    pattern = r"\s+".join(re.escape(tok) for tok in needle.split())
    if not pattern:
        return []
    return [(m.start(), m.end()) for m in re.finditer(pattern, text)]


def _normalize_type(raw_type: str) -> str:
    key = raw_type.strip().upper().replace("-", "_").replace(" ", "_")
    return _TYPE_MAP.get(key, key or "PERSON")
