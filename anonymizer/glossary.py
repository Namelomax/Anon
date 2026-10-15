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


# Окончания полных прилагательных (муж/жен/ср/мн, все падежи). Прилагательное
# распознаём по номинативному окончанию и снимаем его (2 буквы) до основы:
# «Ярактинское» -> «Ярактинск» + любое из этих окончаний.
_ADJ_NOMINATIVE = ("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие")
_ADJ_ENDINGS = (
    "ыми", "ими", "ого", "его", "ому", "ему", "ый", "ий", "ой", "ая", "яя", "ое",
    "ее", "ые", "ие", "ым", "им", "ом", "ем", "ей", "ую", "юю", "ых", "их",
)
# Добавка к общим падежным окончаниям для существительных: множественное
# число и мягкие основы («месторождений», «месторождениям», «площадью»).
_NOUN_EXTRA = ("ям", "ов", "ев", "ей", "ью", "й")


def _alt(endings) -> str:
    return "|".join(sorted((re.escape(e) for e in set(endings) if e), key=len, reverse=True))


def _looks_adjective(low: str) -> bool:
    """Отличить прилагательное от существительного на -ие/-ий («месторождение»,
    «Дмитрий»): настоящее прилагательное на -ие/-ий стоит после заднеязычной,
    шипящей или «н» (орфография: «-ские», «-кий», «-ний»); существительные на
    «-ение/-ание/-тие» и «-рий/-лий» — нет."""
    if low.endswith(("ие", "ий")):
        return len(low) > 2 and low[-3] in "кгхжшчщн" and not low.endswith(("ение", "ание"))
    return True


def _word_regex(word: str) -> str:
    """Фрагмент регэкспа для ОДНОГО слова алиаса (без границ слова).

    * аббревиатуры (ВЧНГКМ), слова до 3 букв и всё с цифрами («ЦПС-3») —
      только точное совпадение: склонять их значит ловить лишнее;
    * полное прилагательное («Ярактинское») — основа + любое окончание
      прилагательного;
    * иначе существительное: снимаем номинативное окончание (а/я/о/е/ё/ь),
      далее основа + любое падежное окончание (в т.ч. пустое).

    Всё — конечные альтернации фиксированных строк, без вложенных
    квантификаторов: катастрофического бэктрекинга быть не может.
    """
    letters = [c for c in word if c.isalpha()]
    if (
        len(letters) <= 3
        or any(c.isdigit() for c in word)
        or (len(letters) > 1 and word.isupper())
    ):
        return re.escape(word)
    low = word.casefold()
    if len(word) >= 5 and low.endswith(_ADJ_NOMINATIVE) and _looks_adjective(low):
        return re.escape(word[:-2]) + rf"(?:{_alt(_ADJ_ENDINGS)})"

    last = low[-1]
    if last in _NOMINATIVE_VOWELS + ("ь",) and len(word) >= 4:
        stem, nom_ending = word[:-1], word[-1]
    else:
        stem, nom_ending = word, ""
    # Основа слишком короткая после отсечения — берём слово целиком (без риска
    # слишком широких совпадений вроде 2-буквенной основы).
    if len(stem) < 3:
        stem, nom_ending = word, ""
    endings = set(_DECL_SUFFIXES) | set(_NOUN_EXTRA)
    if nom_ending:
        endings.add(nom_ending)
    return re.escape(stem) + rf"(?:{_alt(endings)})?"


def _alias_regex(alias: str) -> re.Pattern:
    """Регэксп, ловящий алиас во всех падежах.

    Алиас режется по пробелам, КАЖДОЕ слово склоняется отдельно (см.
    ``_word_regex``: существительные, прилагательные, аббревиатуры остаются
    точными), слова склеиваются гибким пробелом (регэксп ``s+``). Так «Ярактинское
    месторождение» ловит «на Ярактинском месторождении», а «Минфин» — «Минфина/
    Минфином…». Совпадение — по целым словам: «Марково» не цепляет
    «Марковский» (после основы «Марков» идёт «ский», не падежное окончание).
    Морфологический словарь не нужен: детерминированные окончания.
    """
    words = alias.split()
    if not words:
        words = [alias]
    body = r"\s+".join(_word_regex(w) for w in words)
    return re.compile(
        rf"(?<![{_WORD_CH}])" + body + rf"(?![{_WORD_CH}])",
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
