"""Reverse substitution: put original values back using the mapping. No AI.

This is a deterministic string operation — the inverse of the anonymize step.
All placeholders are replaced in a single left-to-right pass driven by one
combined regex, so ``[PERSON_1]`` and ``[PERSON_10]`` never collide and a value
that itself contains a placeholder-like substring can't trigger a second
replacement.
"""

from __future__ import annotations

import re

from .mapping import Mapping


def deanonymize(text: str, mapping: Mapping, *, strict: bool = False) -> str:
    """Restore original values in anonymized text.

    Args:
        text: Anonymized text containing ``[LABEL_N]`` placeholders.
        mapping: ``placeholder -> original`` dict produced at anonymize time.
        strict: If True, raise ``KeyError`` when the text contains a placeholder
            absent from the mapping. If False (default), unknown placeholders are
            left untouched.

    Returns:
        The text with every known placeholder replaced by its original value.
    """
    if not mapping:
        return text

    # Longest keys first so a literal-overlap never shadows another key; the
    # regex alternation is anchored to the exact placeholder shape regardless.
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(k) for k in keys))

    def repl(m: re.Match[str]) -> str:
        token = m.group(0)
        if token not in mapping:  # pragma: no cover - alternation guarantees hit
            if strict:
                raise KeyError(token)
            return token
        return mapping[token]

    result = pattern.sub(repl, text)

    if strict:
        leftover = find_unknown_placeholders(result, mapping)
        if leftover:
            raise KeyError(f"Unresolved placeholders: {sorted(set(leftover))}")
    return result


_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]+_\d+\]")


def find_unknown_placeholders(text: str, mapping: Mapping) -> list[str]:
    """Return placeholder tokens present in text but missing from the mapping."""
    return [tok for tok in _PLACEHOLDER_RE.findall(text) if tok not in mapping]
