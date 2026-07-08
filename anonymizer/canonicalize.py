"""Canonical grouping of same-entity spans into ONE placeholder.

The detectors emit a separate span for every surface form they see, and
``assign_placeholders`` gives a distinct placeholder to each distinct
``(label, exact text)``. For names this is fine, but for organizations and
locations it fragments a single real-world entity across placeholders whenever
it appears in different cases or gets a different label:

    Форус -> [ORG_2],  Форуса -> [ORG_7]          (same org, two cases)
    Телеграме -> [ORG_11],  Телеграме -> [LOCATION_2]   (same app, two labels)

That is not a leak (both are masked), but it is noisy: the mapping has three
rows for one thing and the restored text mixes forms. This module collapses
such spans into a single placeholder BEFORE placeholder assignment by:

  * deriving a case-insensitive declension *stem* for each word (so «Форус» and
    «Форуса» share a key), and
  * ignoring the label difference for ORG/LOCATION (so an app tagged both ways
    collapses to one placeholder).

Grouping is applied ONLY to ``_CANON_LABELS`` and ONLY to spans that don't
already carry a ``merge_key`` — the LLM review layer's coreference decision
(«Капитан Яков» == «Вайгус») is stronger and always wins. PERSON is excluded on
purpose: two people can share a stem («Иванов»/«Иванова») and must stay
distinct. Everything stays reversible — the chosen canonical form (the shortest
surface seen) is what the mapping records.
"""

from __future__ import annotations

from dataclasses import replace

from .detectors import _DECL_SUFFIXES
from .spans import Span

# Метки, чьи падежные/меточные варианты схлопываем в один плейсхолдер.
# PERSON НАМЕРЕННО исключён: разные люди могут делить основу.
_CANON_LABELS = frozenset({"ORG", "LOCATION"})

# Длинные окончания первыми — чтобы снять максимальное, а не «а» из «ами».
_SUFFIXES_DESC = tuple(sorted(_DECL_SUFFIXES, key=len, reverse=True))

# Минимальная длина ключа-основы, ниже — не группируем (защита от слипания
# слишком коротких основ).
_MIN_STEM = 3


def _word_stem(word: str) -> str:
    """Основа слова: снимаем известное падежное окончание, если после него
    остаётся >= 3 символа; иначе оставляем слово как есть. Так «форуса» -> «форус»
    и «форус» -> «форус» (основа на согласную не режется), «телеграме» -> «телеграм»."""
    w = word.casefold()
    for suf in _SUFFIXES_DESC:
        if w.endswith(suf) and len(w) - len(suf) >= _MIN_STEM:
            return w[: -len(suf)]
    return w


def group_key(text: str) -> str:
    """Ключ группировки: основы всех слов через пробел (регистр не важен)."""
    return " ".join(_word_stem(w) for w in text.split() if w)


def canonicalize_entities(spans: list[Span]) -> list[Span]:
    """Схлопнуть падежные/меточные варианты одной ORG/LOCATION-сущности в один
    плейсхолдер: проставляем общий ``merge_key`` и каноничную (самую короткую)
    форму. Спаны с уже заданным ``merge_key`` (решение review-слоя) не трогаем.
    Возвращает НОВЫЙ список спанов (Span заморожен)."""
    # 1-й проход: для каждого ключа-основы выбираем каноничную форму — самую
    # короткую поверхность (обычно именительный падеж), с детерминированным
    # добором по алфавиту при равной длине.
    reps: dict[str, str] = {}
    for s in spans:
        if s.label not in _CANON_LABELS or s.merge_key is not None:
            continue
        key = group_key(s.text)
        if len(key) < _MIN_STEM:
            continue
        surface = s.text.strip()
        cur = reps.get(key)
        if cur is None or (len(surface), surface) < (len(cur), cur):
            reps[key] = surface

    if not reps:
        return spans

    # 2-й проход: проставляем merge_key + каноничную форму.
    out: list[Span] = []
    for s in spans:
        if s.label in _CANON_LABELS and s.merge_key is None:
            key = group_key(s.text)
            rep = reps.get(key)
            if rep is not None and len(key) >= _MIN_STEM:
                out.append(replace(s, merge_key=f"CANON_ENT\x00{key}", canonical_text=rep))
                continue
        out.append(s)
    return out
