"""Post-anonymization leak scan: flag PII-looking patterns still in the OUTPUT.

Every detection layer can miss something (a party name without a legal form, a
requisite in a spot the parser didn't reach, a number in an unusual format).
Instead of the miss being silent — the customer finding it — this pass scans the
*already anonymized* text for high-signal residual PII and returns warnings the
UI can show as "⚠️ Проверьте, возможно не скрыто". It only flags, never masks,
so it can't break anonymization; it turns silent leaks into a visible checklist.

Kept deliberately high-precision (few, near-certain patterns) so warnings stay
trustworthy: long digit runs (bank accounts / ОГРН / ИНН / phones) and emails.
Placeholders like ``[PERSON_1]`` are blanked first so they are never flagged.
"""

from __future__ import annotations

import re

_PLACEHOLDER = re.compile(r"\[[A-ZА-ЯЁ_]+_\d+\]")

# 10+ digits, tolerating space/dot/dash grouping — real ids only (ИНН 10-12,
# phone 11, ОГРН 13, СНИЛС 11, bank account 20). Years/codes (<=7) don't trip.
_LONG_DIGITS = re.compile(r"(?<![\d\w])\d(?:[ \t.\-]?\d){9,}(?![\d\w])")
_EMAILISH = re.compile(
    r"[A-Za-zА-Яа-яЁё0-9._%+\-]+\s?@\s?[A-Za-zА-Яа-яЁё0-9.\-]+\.[A-Za-zА-Яа-яЁё]{2,}"
)


def _snippet(text: str, start: int, end: int, pad: int = 30) -> str:
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    return " ".join(("…" + text[a:b] + "…").split())


def scan_residual_pii(text: str) -> list[dict]:
    """Return warnings about PII-looking fragments left in ``text``.

    Each item: ``{"kind": "DIGITS"|"EMAIL", "value": str, "context": str}``.
    Runs on the anonymized output; placeholders are ignored.
    """
    masked = _PLACEHOLDER.sub(lambda m: " " * len(m.group()), text)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for kind, rx in (("DIGITS", _LONG_DIGITS), ("EMAIL", _EMAILISH)):
        for m in rx.finditer(masked):
            value = m.group().strip()
            key = (kind, value)
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "value": value, "context": _snippet(text, m.start(), m.end())})
    return out
