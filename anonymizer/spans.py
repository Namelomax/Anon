"""Core span type and overlap-resolution utilities."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Span:
    """A detected piece of sensitive text.

    Attributes:
        start: Character offset of the span start in the original text.
        end: Character offset just past the span end (exclusive).
        label: Entity type, e.g. ``"PASSPORT"`` or ``"FIRST_NAME"``.
        text: The exact substring covered by the span.
        source: Which detector produced the span (``"regex"``, ``"ner"``...).
        merge_key: Optional override for placeholder grouping. Normally two
            spans share a placeholder only if they have the same label and
            identical (case-folded) text. The review layer (``review.py``)
            sets this when it decides two *differently worded* mentions are
            the same real-world entity (e.g. "Капитан Яков" and "Вайгус") —
            both get the same ``merge_key`` so ``assign_placeholders`` groups
            them under one placeholder despite the differing surface text.
        canonical_text: Optional override for the value recorded in the
            output mapping when ``merge_key`` is set (all merged spans keep
            their own original ``text`` for in-document masking, but the
            mapping needs one canonical spelling to restore).
    """

    start: int
    end: int
    label: str
    text: str
    source: str = "regex"
    merge_key: str | None = None
    canonical_text: str | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"Invalid span bounds: {self.start}..{self.end}")

    @property
    def length(self) -> int:
        return self.end - self.start


def resolve_overlaps(
    spans: list[Span],
    *,
    priority: dict[str, int] | None = None,
) -> list[Span]:
    """Drop overlapping spans, keeping the strongest at each position.

    Two spans overlap if their character ranges intersect. When they do, the
    survivor is chosen by, in order: higher label priority, then longer span,
    then earlier start. The result is sorted by start offset and contains no
    overlaps, which is what the placeholder substitution step requires.

    Args:
        spans: Candidate spans from one or more detectors.
        priority: Optional ``label -> weight`` map; higher wins. Labels absent
            from the map get weight 0.

    Returns:
        A non-overlapping list of spans sorted by ``start``.
    """
    priority = priority or {}

    def rank(span: Span) -> tuple[int, int, int]:
        # Higher is better: priority, then length, then earlier start.
        return (priority.get(span.label, 0), span.length, -span.start)

    # Strongest first so the greedy sweep keeps the best and discards conflicts.
    ordered = sorted(spans, key=rank, reverse=True)
    kept: list[Span] = []
    for span in ordered:
        if any(_overlaps(span, k) for k in kept):
            continue
        kept.append(span)
    kept.sort(key=lambda s: s.start)
    return kept


def _overlaps(a: Span, b: Span) -> bool:
    return a.start < b.end and b.start < a.end


def rebalance_quotes(text: str, span: Span) -> Span:
    """Выравнивает непарные «ёлочки» на границах спана.

    Детекторы/трим режут кавычки посимвольно и не знают о парности: DATE-регэксп
    захватывал «12» сентября…, а `_trim` срезал ведущую «, оставляя внутри спана
    непарную » (в тексте оставалась сирота-«). Аналогично NER возвращал
    «Технопарка «Сколково» без закрывающей ».

    Правило: если внутри спана есть непарная кавычка, а её пара стоит ВПЛОТНУЮ
    к границе снаружи — расширяем спан на неё; если пары рядом нет, а непарная
    кавычка стоит на краю спана — выталкиваем её из спана. Непарную кавычку в
    СЕРЕДИНЕ спана без пары рядом не трогаем (не наша ошибка — так в тексте).
    Прямые кавычки (") не обрабатываются: открывающая и закрывающая неотличимы.
    """
    start, end = rebalance_bounds(text, span.start, span.end)
    if (start, end) == (span.start, span.end) or end <= start:
        return span
    return replace(span, start=start, end=end, text=text[start:end])


# Пары, чью парность выравниваем. Прямые кавычки (") не входят: открывающая
# и закрывающая неотличимы.
_BALANCED_PAIRS = (("«", "»"), ("(", ")"))


def rebalance_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Границы спана после выравнивания непарных «ёлочек» и скобок.

    Используется и движком (NER/LLM-спаны), и RegexDetector после `_trim`
    (трим срезал ведущую « у «12» сентября… и хвостовую ) у «Альфа-Банк (АО)»).
    """
    for opener, closer in _BALANCED_PAIRS:
        while True:
            opens = text.count(opener, start, end)
            closes = text.count(closer, start, end)
            # Пара стоит снаружи вплотную к границе — забираем её в спан.
            if closes > opens and start > 0 and text[start - 1] == opener:
                start -= 1
                continue
            if opens > closes and end < len(text) and text[end] == closer:
                end += 1
                continue
            # Пары рядом нет — выталкиваем непарный КРАЕВОЙ символ из спана.
            if closes > opens and end > start and text[end - 1] == closer:
                end -= 1
                continue
            if opens > closes and start < end and text[start] == opener:
                start += 1
                continue
            break
    return start, end
