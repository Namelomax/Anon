"""User-maintained glossary of always-mask terms.

Some sensitive references never look like PII to NER/regex at all: informal
abbreviations ("Мингос", "Минфин"), or an ordinary Russian word used in one
specific document as the name of a real, confidential entity ("Правительство").
No general-purpose detector can tell these apart from harmless everyday use of
the same word — that requires knowing your organization's own vocabulary.

This module lets a user declare that vocabulary once, in a plain text file,
and have it always masked — skipping the NER/LLM guesswork and the review
layer entirely (see ``CUSTOM_TERM`` exclusion in ``review.py`` and the
generic-noun filters in ``detectors.py``, both of which only apply to
NER-derived labels). A glossary entry is trusted because a human chose it, not
because a model scored it above a threshold.

File format (one entry per line, ``#`` starts a comment, blank lines ignored)::

    # алиасы через запятую = каноничное имя для итоговой карты сопоставления
    Мингос = Министерство государственного управления
    Минфин = Министерство финансов
    Правительство = Правительство

Matching is case-insensitive and tolerant of Russian noun case endings: the
alias's own nominative vowel ending is stripped to a stem, then any case ending
is matched, so a single "Минфин" entry catches "Минфина/Минфином…" and
"Правительство" also catches "правительством/правительства/правительству". All
aliases of one entry share ONE placeholder in the output mapping (via
``Span.merge_key``), so "Мингос" and "Министерство государственного
управления" collapse to the same ``[CUSTOM_TERM_N]`` wherever either appears.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .detectors import _DECL_SUFFIXES
from .spans import Span

_WORD_CH = "0-9A-Za-zА-Яа-яЁё"

# Номинативные окончания, которые в косвенных падежах ЗАМЕНЯЮТСЯ (ср.р. «-о/-е»,
# ж.р. «-а/-я»). Их нужно снять с алиаса, иначе к полной форме нельзя приписать
# другое окончание: «Правительство» + «-ом» ≠ «Правительством» (там «-ств» + «-ом»).
_NOMINATIVE_VOWELS = ("а", "я", "о", "е", "ё")


def _alias_regex(alias: str) -> re.Pattern:
    """Регэксп, ловящий алиас во всех падежах.

    Снимаем номинативное гласное окончание (если есть), получая основу, и
    матчим ``основа + любое падежное окончание (в т.ч. пустое)``. Так одна
    запись «Минфин» ловит «Минфина/Минфином…», а «Правительство» — ещё и
    «правительством/правительства/правительству», чего простое приписывание
    окончания к полному алиасу не давало (баг исходной реализации).
    """
    last = alias[-1:].casefold()
    if last in _NOMINATIVE_VOWELS and len(alias) >= 4:
        stem = alias[:-1]
        nom_ending = alias[-1]
    else:
        stem = alias
        nom_ending = ""

    # Основа слишком короткая после отсечения — берём алиас целиком (без риска
    # слишком широких совпадений вроде 2-буквенной основы).
    if len(stem) < 3:
        stem = alias
        nom_ending = ""

    endings = set(_DECL_SUFFIXES)
    if nom_ending:
        endings.add(nom_ending)
    alt = "|".join(sorted((re.escape(e) for e in endings if e), key=len, reverse=True))
    return re.compile(
        rf"(?<![{_WORD_CH}])" + re.escape(stem) + rf"(?:{alt})?(?![{_WORD_CH}])",
        re.IGNORECASE | re.UNICODE,
    )


@dataclass(frozen=True)
class GlossaryEntry:
    """One glossary concept: a canonical name plus every surface form for it."""

    canonical: str
    aliases: tuple[str, ...]
    label: str = "CUSTOM_TERM"


def parse_glossary_text(text: str) -> tuple[GlossaryEntry, ...]:
    """Parse the ``alias1, alias2 = canonical`` text format into entries.

    Malformed lines (no ``=``, or empty on both sides) are silently skipped —
    a typo in one line shouldn't crash the whole pipeline; better to mask less
    than to fail the upload entirely.
    """
    entries: list[GlossaryEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        left, _, right = line.partition("=")
        aliases = tuple(a.strip() for a in left.split(",") if a.strip())
        canonical = right.strip() or (aliases[0] if aliases else "")
        if not aliases or not canonical:
            continue
        entries.append(GlossaryEntry(canonical=canonical, aliases=aliases))
    return tuple(entries)


def load_glossary(path: str | Path) -> tuple[GlossaryEntry, ...]:
    """Load a glossary file. Returns ``()`` if the file doesn't exist —
    the feature is opt-in by the file's presence, not a hard requirement."""
    p = Path(path)
    if not p.exists():
        return ()
    return parse_glossary_text(p.read_text(encoding="utf-8"))


# Default location: a repo-tracked file the user edits directly and commits.
DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parent / "custom_terms.txt"


class GlossaryDetector:
    """Detector for a fixed list of user-declared always-mask terms.

    Each alias is matched whole-word, case-insensitively, with an optional
    trailing Russian case ending derived from its stem (see ``_alias_regex``),
    so declined forms are caught without a separate propagation pass.
    """

    def __init__(self, entries: Iterable[GlossaryEntry]) -> None:
        self._compiled: list[tuple[re.Pattern, GlossaryEntry, str]] = []
        for entry in entries:
            merge_key = f"CUSTOM_TERM\x00{entry.canonical.casefold()}"
            for alias in entry.aliases:
                self._compiled.append((_alias_regex(alias), entry, merge_key))

    def find(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for pattern, entry, merge_key in self._compiled:
            for m in pattern.finditer(text):
                spans.append(
                    Span(
                        m.start(), m.end(), entry.label, m.group(0),
                        source="glossary", merge_key=merge_key,
                        canonical_text=entry.canonical,
                    )
                )
        return spans
