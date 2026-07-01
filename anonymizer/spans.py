"""Core span type and overlap-resolution utilities."""

from __future__ import annotations

from dataclasses import dataclass


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
