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
* JSON is parsed from ``content`` or, for reasoning models that exhaust the
  token budget mid-think, from ``reasoning_content`` as a fallback.

Works with LM Studio (``http://127.0.0.1:1234/v1``) and Ollama
(``http://127.0.0.1:11434/v1``); both expose the same chat-completions API.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .chunking import chunk_text
from .spans import Span

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
}

def _build_system_prompt(allowed_labels: frozenset) -> str:
    """System prompt listing exactly the types the model is allowed to emit."""
    types = ", ".join(sorted(allowed_labels))
    org_on = "ORG" in allowed_labels
    amount_on = "AMOUNT" in allowed_labels
    # When ORG is requested we DO want organization names; otherwise we tell the
    # model to skip them (keeps PII-only mode precise).
    org_rule = (
        "Тип ORG — названия организаций и компаний (в т.ч. в кавычках, в любом "
        "падеже).\n"
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
    return (
        "Ты — детектор персональных данных (PII) в русском тексте. "
        "Найди ВСЕ персональные данные. Особое внимание удели тому, что трудно "
        "поймать регулярными выражениями: числа, записанные словами (например "
        "«семь восемь девять»), номера документов без подсказывающих слов, имена "
        "и фамилии латиницей или в необычном порядке (в любом падеже), улицы и "
        "номера домов.\n"
        "Верни ТОЛЬКО JSON-массив объектов вида "
        '{"text": "<точная подстрока из текста>", "type": "<ТИП>"}.\n'
        f"Допустимые типы (используй ТОЛЬКО их): {types}.\n"
        "Поле text должно ДОСЛОВНО совпадать с фрагментом исходного текста "
        "(те же символы, регистр и пробелы). Не перефразируй, не нормализуй числа.\n"
        + org_rule + amount_rule +
        "НЕ помечай: слова-категории сами по себе (паспорт, СНИЛС, ИНН, полис, "
        "серия, номер, телефон, почта, адрес); должности и роли (директор, "
        "фрилансер, бухгалтер); даты; обычные слова и глаголы. Помечай только "
        "конкретные ЗНАЧЕНИЯ.\n"
        "Если документ относится к водительскому удостоверению (рядом есть слова "
        "«водитель», «права», «ВУ», «вождение»), используй тип DRIVER_LICENSE, а "
        "не PASSPORT.\n"
        "Если ничего не найдено — верни []. Никаких пояснений, только JSON."
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
        max_tokens: Generous budget — reasoning models spend most of it thinking
            before emitting the JSON. Too small => empty ``content``.
        temperature: 0 for deterministic extraction.
        timeout: Per-request seconds. Reasoning models can be slow on CPU/GPU.
        api_key: Sent as Bearer; ignored by LM Studio/Ollama but required shape.
        extra_body: Merged into the request JSON. Use it to disable thinking,
            e.g. ``{"chat_template_kwargs": {"enable_thinking": False}}`` or, on
            Ollama's native path, ``{"think": False}``.
        allowed_labels: Whitelist of labels to keep; others are dropped. Defaults
            to the canonical PII set (no ORGANIZATION/DATE).
        max_chars: Documents are split into chunks of at most this many chars
            before being sent to the model. Smaller chunks raise recall (the
            model sees less at once, so declined word-forms aren't missed) at the
            cost of more API calls.
    """

    base_url: str = "http://127.0.0.1:1234/v1"
    model: str = "qwen/qwen3.5-9b"
    max_tokens: int = 8000
    temperature: float = 0.0
    timeout: float = 300.0
    api_key: str = "not-needed"
    extra_body: dict = field(default_factory=dict)
    allowed_labels: frozenset = _DEFAULT_ALLOWED
    max_chars: int = 3000


class LLMDetector:
    """Detector that asks a local LLM for PII substrings and locates them."""

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig()
        self._system = _build_system_prompt(self.config.allowed_labels)

    def find(self, text: str) -> list[Span]:
        if not text.strip():
            return []
        spans: list[Span] = []
        # Chunk long documents: smaller inputs keep the model's recall high
        # (declined word-forms aren't missed) and avoid output truncation.
        for offset, chunk in chunk_text(text, self.config.max_chars):
            content = self._complete(chunk)
            items = _parse_items(content)
            spans.extend(self._locate(chunk, offset, items))
        return spans

    # -- HTTP -----------------------------------------------------------
    def _complete(self, text: str) -> str:
        cfg = self.config
        payload = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": text},
            ],
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
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
        msg = data["choices"][0]["message"]
        # Prefer the answer; fall back to reasoning text if content is empty
        # (reasoning model ran out of budget before emitting content).
        return msg.get("content") or msg.get("reasoning_content") or ""

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


# --- JSON parsing ----------------------------------------------------------

def _parse_items(content: str) -> list[tuple[str, str]]:
    """Extract ``(text, type)`` pairs from a model reply (tolerant of prose)."""
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
