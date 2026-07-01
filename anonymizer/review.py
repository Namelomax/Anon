"""LLM review layer: a final sanity pass over the detected spans.

This is the FOURTH and last layer, run after regex + NER + LLM detection have
produced a candidate span list (and after declension propagation), right
before placeholders are assigned. Every earlier layer is recall-oriented
("when in doubt, mask it"), which is exactly why obvious false positives slip
through — a common word capitalized at the start of a sentence ("День"), a
product/technology name (GPT, Telegram, Битрикс), a legal abbreviation (ФЗ,
НДА), or a speech-to-text artifact all get masked just like real PII.

This layer asks an LLM to look at each *distinct* detected value together with
a snippet of its surrounding context and answer a simple question: is this
really sensitive data, or is it an obvious detector mistake that should stay
in the text as plain text?

Design contract, mirroring ``llm.py``:

* The reviewer can only DROP spans (revert them to plain text) — it never
  adds masking, invents entities, or rewrites text.
* Only "soft" labels prone to false positives are reviewed (PERSON, ORG,
  LOCATION and friends). Format-driven identifiers (EMAIL, PHONE, INN,
  SNILS, PASSPORT, CREDIT_CARD...) are never sent to the model and always
  stay masked — a regex match on those is essentially never wrong, and an
  LLM should not be given a chance to talk itself into unmasking real PII.
* Fail-safe: if the LLM is unreachable, times out, or returns something that
  cannot be parsed, every affected span is kept masked (unchanged behaviour).
* Repeat occurrences of the same value (same label, whitespace-collapsed,
  case-folded) are judged once, as a group, using the first occurrence's
  context — mirrors how ``mapping.assign_placeholders`` groups identical
  entities under one placeholder.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .llm import _extract_json_array  # reuse the same tolerant JSON-array scanner
from .spans import Span

# Only these labels are second-guessed. Structured/format-driven identifiers
# are deliberately excluded — see module docstring.
_REVIEWABLE_LABELS = frozenset({
    "PERSON", "ORG", "LOCATION", "CITY", "REGION", "COUNTRY",
    "DISTRICT", "STREET", "HOUSE", "ADDRESS",
    "FIRST_NAME", "LAST_NAME", "MIDDLE_NAME",
})

_REVIEW_SYSTEM_PROMPT = (
    "Ты — контролёр качества анонимизации персональных данных (ПДн) в русском "
    "тексте. Тебе присылают пронумерованный список кандидатов, которые детектор "
    "пометил как ПДн (ФИО, организация, локация и т.п.). Для каждого дано: тип, "
    "само значение и фрагмент окружающего текста, где значение выделено "
    "квадратными скобками: [вот_так].\n"
    "Для КАЖДОГО кандидата реши: это ДЕЙСТВИТЕЛЬНО персональные или чувствительные "
    "данные (настоящее имя человека, реальное название компании, адрес и т.п.), "
    "которые нужно скрыть — тогда keep=true; или это ОШИБКА детектора — "
    "обычное слово, местоимение, день недели, должность, название программы/"
    "технологии/продукта (например GPT, ChatGPT, GigaChat, DeepSeek, Telegram, "
    "Zoom, Битрикс, 1С), юридический термин или аббревиатура (ФЗ, НДА, ТЗ), "
    "либо обрывок слова / артефакт распознавания речи — тогда keep=false, "
    "значение НЕ является ПДн и должно остаться в тексте как есть.\n"
    "Если сомневаешься — ставь keep=true: лучше скрыть лишнее, чем случайно "
    "раскрыть настоящие ПДн.\n"
    "Ответь ТОЛЬКО JSON-массивом вида "
    '[{"id": <номер>, "keep": true|false}, ...] — для ВСЕХ кандидатов по '
    "порядку, без каких-либо пояснений."
)


@dataclass
class ReviewConfig:
    """Connection and batching settings for the LLM review layer.

    Attributes:
        base_url: OpenAI-compatible base (LM Studio / Ollama). Typically the
            same server used for the detection LLM layer.
        model: Model id as the server reports it.
        max_tokens: Output budget for the verdict JSON (small — just a list
            of ``{id, keep}`` objects).
        temperature: 0 for deterministic verdicts.
        timeout: Per-request seconds.
        api_key: Sent as Bearer; ignored by LM Studio/Ollama.
        extra_body: Merged into the request JSON (e.g. disable "thinking").
        context_chars: Characters of surrounding text kept on each side of a
            candidate value, to give the model enough to judge from.
        batch_size: How many distinct candidates are sent per LLM call.
    """

    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3.5:9b"
    max_tokens: int = 4000
    temperature: float = 0.0
    timeout: float = 300.0
    api_key: str = "not-needed"
    extra_body: dict = field(default_factory=dict)
    context_chars: int = 60
    batch_size: int = 25


@dataclass
class _Candidate:
    label: str
    text: str
    context: str


def review_spans(text: str, spans: list[Span], config: "ReviewConfig | None" = None) -> list[Span]:
    """Ask an LLM to double-check spans and drop obvious false positives.

    Groups spans by (label, whitespace-collapsed, case-folded value) so a
    value repeated many times (``mask_all_occurrences``) is judged once and
    dropped/kept as a whole. Only labels in ``_REVIEWABLE_LABELS`` are ever
    sent to the model; everything else passes through untouched.

    Returns the filtered span list. On any error talking to the LLM (or if it
    returns unparsable output) the affected spans are left masked — this
    layer can only make anonymization *more* permissive when it is confident,
    never less safe by default.
    """
    if not spans:
        return spans
    cfg = config or ReviewConfig()

    candidates = _group_candidates(text, spans, cfg.context_chars)
    if not candidates:
        return spans

    drop_keys: set[str] = set()
    items = list(candidates.items())
    for i in range(0, len(items), cfg.batch_size):
        batch = items[i : i + cfg.batch_size]
        try:
            verdicts = _ask(batch, cfg)
        except Exception:
            continue  # fail safe: keep this batch masked
        for idx, keep in verdicts.items():
            if not keep and 0 <= idx < len(batch):
                drop_keys.add(batch[idx][0])

    if not drop_keys:
        return spans
    return [s for s in spans if _key_of(s) not in drop_keys]


def _key_of(span: Span) -> str:
    return f"{span.label}\x00{' '.join(span.text.split()).casefold()}"


def _group_candidates(text: str, spans: list[Span], context_chars: int) -> dict[str, _Candidate]:
    groups: dict[str, _Candidate] = {}
    for span in spans:
        if span.label not in _REVIEWABLE_LABELS:
            continue
        key = _key_of(span)
        if key in groups:
            continue  # first occurrence's context is enough
        start = max(0, span.start - context_chars)
        end = min(len(text), span.end + context_chars)
        ctx = f"{text[start:span.start]}[{span.text}]{text[span.end:end]}"
        groups[key] = _Candidate(label=span.label, text=span.text, context=" ".join(ctx.split()))
    return groups


def _ask(batch: list[tuple[str, _Candidate]], cfg: ReviewConfig) -> dict[int, bool]:
    lines = [
        f'{idx}. [{cand.label}] "{cand.text}" — контекст: «{cand.context}»'
        for idx, (_, cand) in enumerate(batch)
    ]
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
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
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Review LLM request to {cfg.base_url} failed: {exc}. "
            "Is LM Studio / Ollama running and the model loaded?"
        ) from exc
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _parse_verdicts(content)


def _parse_verdicts(content: str) -> dict[int, bool]:
    blob = _extract_json_array(content)
    if blob is None:
        return {}
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return {}
    out: dict[int, bool] = {}
    for obj in data if isinstance(data, list) else []:
        if not isinstance(obj, dict):
            continue
        i, k = obj.get("id"), obj.get("keep")
        if isinstance(i, int) and isinstance(k, bool):
            out[i] = k
    return out
