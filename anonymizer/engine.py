"""Anonymization engine: text + detectors -> redacted text + reversible mapping."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .detectors import (
    CORPORATE_DETECTORS,
    DEFAULT_DETECTORS,
    DEFAULT_PRIORITY,
    Detector,
    is_generic_entity,
    is_money_amount,
    is_non_pii,
    is_noise_span,
    is_stopword_entity,
    propagate_declensions,
    run_detectors,
)

# Labels for which a job-title surface form should be left unmasked.
_TITLE_FILTER_LABELS = frozenset({"PERSON", "ORG"})
from .mapping import Mapping, assign_placeholders, find_placeholder_spans
from .spans import Span, resolve_overlaps


@dataclass(frozen=True)
class AnonymizationResult:
    """Result of anonymizing one text.

    Attributes:
        text: The original, unmodified input.
        anonymized_text: Text with sensitive spans replaced by placeholders.
        mapping: ``placeholder -> original`` dict needed to restore the text.
        spans: The non-overlapping spans that were redacted, in order.
    """

    text: str
    anonymized_text: str
    mapping: Mapping
    spans: tuple[Span, ...] = field(default_factory=tuple)
    # How many ``[LABEL_123]``-shaped tokens were already present in the input
    # (before any detection ran). Non-zero almost always means the document was
    # anonymized before — re-uploading an already-anonymized file. Surfaced so
    # a caller/UI can warn instead of the mapping silently filling with junk.
    preexisting_placeholders: int = 0

    @property
    def summary(self) -> dict[str, int]:
        """Count of redacted spans per label."""
        counts: dict[str, int] = {}
        for span in self.spans:
            counts[span.label] = counts.get(span.label, 0) + 1
        return counts


class Anonymizer:
    """Configurable, reusable anonymizer.

    Args:
        detectors: Detector objects to run. Defaults to the regex set for
            structured Russian PII. Add an NER detector here for names/addresses.
        priority: ``label -> weight`` map used to resolve overlapping spans.
    """

    def __init__(
        self,
        detectors: Iterable[Detector] | None = None,
        *,
        priority: dict[str, int] | None = None,
        mask_all_occurrences: bool = True,
        review_config=None,
    ) -> None:
        self._detectors = tuple(detectors) if detectors is not None else DEFAULT_DETECTORS
        self._priority = priority if priority is not None else DEFAULT_PRIORITY
        self._mask_all = mask_all_occurrences
        # Optional 4th layer: an LLM double-checks the surviving spans and
        # reverts obvious false positives (see review.py). None = skip it.
        self._review_config = review_config

    def anonymize(self, text: str) -> AnonymizationResult:
        """Detect sensitive spans and replace them with reversible placeholders.

        Once a value is identified as sensitive it is masked at *every* exact
        occurrence (when ``mask_all_occurrences``), so a repeat the detectors
        missed (common in multi-paragraph documents) can't leak.
        """
        # Guard against re-anonymizing an already-anonymized document: existing
        # "[LABEL_123]" tokens must never be treated as detectable entities —
        # otherwise GLiNER/regex "re-discover" them (they look like capitalized
        # identifiers) and wrap them again, producing garbage like
        # "[[PERSON_1]]" and a mapping full of broken placeholder fragments.
        protected = find_placeholder_spans(text)

        raw = run_detectors(text, self._detectors)
        raw = [
            s for s in raw
            if _has_alnum(s.text)
            and not (s.label in _TITLE_FILTER_LABELS and is_non_pii(s.text))
            and not is_stopword_entity(s.text, s.label)
            and not is_noise_span(s.text, s.label)
            and not is_generic_entity(s.text, s.label)
            and not (s.label == "AMOUNT" and not is_money_amount(s.text))
            and not _overlaps_any(s, protected)
        ]
        spans = resolve_overlaps(raw, priority=self._priority)
        # Mask declined case-forms of detected entities (e.g. "Лентой" given "Лента").
        extra = propagate_declensions(text, spans)
        if extra:
            extra = [e for e in extra if not _overlaps_any(e, protected)]
            spans = resolve_overlaps(spans + extra, priority=self._priority)
        # 4th layer: LLM double-checks the surviving spans against their
        # context and reverts obvious false positives (see review.py). Runs
        # last, after all detectors, so it judges the final candidate set.
        if self._review_config is not None:
            from .review import review_spans

            spans = review_spans(text, spans, self._review_config)
        mapping, span_placeholders = assign_placeholders(spans)

        if self._mask_all and mapping:
            anonymized, spans = _apply_all_occurrences(text, spans, span_placeholders)
        else:
            anonymized = _apply(text, spans, span_placeholders)

        return AnonymizationResult(
            text=text,
            anonymized_text=anonymized,
            mapping=mapping,
            spans=tuple(spans),
            preexisting_placeholders=len(protected),
        )


def _overlaps_any(span: Span, ranges: list[tuple[int, int]]) -> bool:
    return any(span.start < e and st < span.end for st, e in ranges)


def _has_alnum(text: str) -> bool:
    """True if the span contains at least one letter or digit (not pure symbols)."""
    return any(ch.isalnum() for ch in text)


def _label_of(placeholder: str) -> str:
    """`[PERSON_1]` -> `PERSON`."""
    return placeholder.strip("[]").rsplit("_", 1)[0]


_WORD_CH = "0-9A-Za-zА-Яа-яЁё"


def _bounded(orig: str) -> str:
    """Escape ``orig`` and add word boundaries so it only matches as a whole token.

    Without this, a short value (e.g. a name "Ян" or a mis-tagged pronoun) would
    be substituted *inside* other words ("сегодн[Я]", "п[ОН]имаю"). Boundaries
    are added only on the alnum side(s), so values bordered by punctuation
    (emails, "+7…", "№ ЧЕБ-…") still match.
    """
    esc = re.escape(orig)
    pre = rf"(?<![{_WORD_CH}])" if orig[:1].isalnum() else ""
    post = rf"(?![{_WORD_CH}])" if orig[-1:].isalnum() else ""
    return pre + esc + post


def _apply_all_occurrences(
    text: str, spans: list[Span], span_placeholders: dict[int, str]
) -> tuple[str, tuple[Span, ...]]:
    """Replace every exact occurrence of each span's own surface text with its placeholder.

    Built from ``spans`` (not the ``mapping`` dict) so a merged entity — one
    that the review layer decided is the same real-world person/org under
    *different* wordings (e.g. "Капитан Яков" and "Вайгус", see review.py) —
    is masked correctly: each surface form is matched by its own exact text,
    but both point at the same placeholder (``span_placeholders`` already
    reflects the merge via ``Span.merge_key``). This also still catches
    repeats of any surface form the detectors missed elsewhere in the document.

    Originals are matched longest-first so a value containing a shorter one wins.
    Matching is whole-token (word-boundary aware) so a placeholder is never
    spliced into the middle of an unrelated word. Returns the anonymized text and
    the spans of all replaced occurrences.
    """
    surface_to_ph: dict[str, str] = {}
    for span in spans:
        surface_to_ph.setdefault(span.text, span_placeholders[id(span)])

    items = sorted(surface_to_ph.items(), key=lambda kv: len(kv[0]), reverse=True)
    pattern = re.compile("|".join(_bounded(orig) for orig, _ in items))

    result: list[Span] = []
    for m in pattern.finditer(text):
        ph = surface_to_ph[m.group(0)]
        result.append(Span(m.start(), m.end(), _label_of(ph), m.group(0)))
    anonymized = pattern.sub(lambda m: surface_to_ph[m.group(0)], text)
    result.sort(key=lambda s: s.start)
    return anonymized, tuple(result)


def _apply(text: str, spans: list[Span], span_placeholders: dict[int, str]) -> str:
    """Splice placeholders into the text at each (sorted, disjoint) span."""
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(text[cursor : span.start])
        pieces.append(span_placeholders[id(span)])
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def build_anonymizer(
    *,
    use_regex: bool = True,
    use_ner: bool = True,
    ner_backend: str = "natasha",
    include_org: bool = False,
    gliner_config=None,
    corporate: bool = False,
    use_llm: bool = False,
    llm_config=None,
    use_review: bool = False,
    review_config=None,
) -> Anonymizer:
    """Construct an anonymizer from the layered detectors.

    Layers (cheap to expensive): regex -> NER -> local LLM -> LLM review. Each
    detection layer is independently switchable; each only adds spans, overlap
    resolution merges. The review layer runs last and can only remove spans.

    Args:
        use_regex: Include the deterministic regex detectors (contacts + RU
            document numbers). Turn off to test NER/LLM in isolation.
        use_ner: Append an NER detector (names/locations). Loads heavy models.
        ner_backend: ``"natasha"`` (fast, CPU) or ``"gliner"`` (multilingual,
            higher recall on Cyrillic / Latin names / streets).
        include_org: Natasha-only — also redact organizations.
        gliner_config: Optional :class:`anonymizer.gliner_ner.GLiNERConfig`.
        corporate: Add business detectors (AMOUNT/CONTRACT/DATE) for business
            documents. Off by default (not part of the PII taxonomy). These are
            regex-based but controlled separately from ``use_regex``.
        use_llm: Append the local-LLM gap-filler. Requires LM Studio / Ollama.
        llm_config: Optional :class:`anonymizer.llm.LLMConfig`.
        use_review: Add the LLM review layer (see ``review.py``): double-checks
            the final span list against context and reverts obvious detector
            mistakes (common words, product names, legal abbreviations...)
            before placeholders are assigned. Requires LM Studio / Ollama.
        review_config: Optional :class:`anonymizer.review.ReviewConfig`.

    Returns:
        A configured :class:`Anonymizer`.
    """
    detectors: list[Detector] = list(DEFAULT_DETECTORS) if use_regex else []
    if corporate:
        detectors.extend(CORPORATE_DETECTORS)
    if use_ner:
        if ner_backend == "gliner":
            from .gliner_ner import GLiNERDetector

            detectors.append(GLiNERDetector(gliner_config))
        elif ner_backend == "natasha":
            from .ner import NatashaDetector

            detectors.append(NatashaDetector(include_org=include_org))
        else:
            raise ValueError(f"Unknown ner_backend: {ner_backend!r}")
    if use_llm:
        from dataclasses import replace

        from .llm import LLMConfig, LLMDetector

        cfg = llm_config or LLMConfig()
        if corporate:  # the LLM (not regex) handles organizations and money sums
            cfg = replace(cfg, allowed_labels=cfg.allowed_labels | {"ORG", "AMOUNT"})
        detectors.append(LLMDetector(cfg))

    review_cfg = None
    if use_review:
        from .review import ReviewConfig

        review_cfg = review_config or ReviewConfig()
    return Anonymizer(detectors, review_config=review_cfg)


_default = Anonymizer()


def anonymize(text: str) -> AnonymizationResult:
    """Anonymize text with the default regex-only engine (no NER, no model load)."""
    return _default.anonymize(text)
