"""Reversible placeholder mapping: assignment, normalization, and JSON I/O.

A mapping is a plain ``placeholder -> original`` dict, e.g.
``{"[PASSPORT_1]": "серия 7518, номер 492137"}``. It is the *key* that lets the
deanonymize step restore the text, so it is written separately from the
redacted document and should be stored as securely as the source data.

Reversibility rule: identical sensitive values of the same label collapse to one
placeholder (so the same passport number appearing twice maps back consistently),
while distinct values get distinct numbered placeholders.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict

from .spans import Span

Mapping = Dict[str, str]

_PLACEHOLDER_RE = re.compile(r"\[([A-Z_]+)_(\d+)\]")


def placeholder_for(label: str, index: int) -> str:
    """Build a placeholder token, e.g. ``("PASSPORT", 1) -> "[PASSPORT_1]"``."""
    return f"[{label}_{index}]"


def _normalize(span: Span) -> str:
    """Group key for deciding whether two spans are the *same* entity.

    Whitespace is collapsed so that "812 987" and "812  987" share a placeholder.
    Case is folded for names/locations but kept for structured ids where it is
    not meaningful anyway.

    A span can override this via ``merge_key`` (set by the LLM review layer,
    see ``review.py``) to force grouping with a *differently worded* mention
    of the same real-world entity — e.g. "Капитан Яков" and "Вайгус" turning
    out to be the same person by context.
    """
    if span.merge_key is not None:
        return span.merge_key
    collapsed = " ".join(span.text.split())
    return f"{span.label}\x00{collapsed.casefold()}"


def assign_placeholders(spans: list[Span]) -> tuple[Mapping, dict[int, str]]:
    """Assign placeholders to spans, reusing one per distinct entity.

    Args:
        spans: Non-overlapping spans sorted by start offset.

    Returns:
        A ``(mapping, span_placeholders)`` pair where ``mapping`` is the
        ``placeholder -> original`` dict and ``span_placeholders`` maps each
        span's identity (``id(span)``) to its placeholder token. Placeholders are
        numbered per label in order of first appearance in the text.
    """
    counters: dict[str, int] = {}
    by_key: dict[str, str] = {}
    mapping: Mapping = {}
    span_placeholders: dict[int, str] = {}

    for span in spans:
        key = _normalize(span)
        placeholder = by_key.get(key)
        if placeholder is None:
            counters[span.label] = counters.get(span.label, 0) + 1
            placeholder = placeholder_for(span.label, counters[span.label])
            by_key[key] = placeholder
            mapping[placeholder] = span.canonical_text if span.canonical_text is not None else span.text
        span_placeholders[id(span)] = placeholder

    return mapping, span_placeholders


def is_placeholder(token: str) -> bool:
    """Return True if ``token`` looks like a generated placeholder."""
    return bool(_PLACEHOLDER_RE.fullmatch(token))


def find_placeholder_spans(text: str) -> list[tuple[int, int]]:
    """Locate every ``[LABEL_123]``-shaped substring already present in ``text``.

    Used to make anonymization idempotent: if a document was anonymized once
    already (e.g. someone re-uploads the ``.anon.docx`` output by mistake),
    detectors must never touch these regions. Without this guard, GLiNER/regex
    happily treat placeholder tokens as brand-new "entities" (they look like
    capitalized identifiers) and re-wrap them, producing garbage like
    ``[[PERSON_1]]`` or a mapping whose values are themselves broken
    placeholders (``"[ORG_2]": "[ORG_1"``) instead of real data.
    """
    return [m.span() for m in _PLACEHOLDER_RE.finditer(text)]


def save_mapping(mapping: Mapping, path: str | Path) -> None:
    """Write a mapping to a UTF-8 JSON file (human-readable, sorted-ish)."""
    Path(path).write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_mapping(path: str | Path) -> Mapping:
    """Load a mapping from a JSON file, validating its shape."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ValueError("Mapping file must be a JSON object of string->string")
    return data
