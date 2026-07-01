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
from .mapping import Mapping, assign_placeholders
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
    ) -> None:
        self._detectors = tuple(detectors) if detectors is not None else DEFAULT_DETECTORS
        self._priority = priority if priority is not None else DEFAULT_PRIORITY
        self._mask_all = mask_all_occurrences

    def anonymize(self, text: str) -> AnonymizationResult:
        """Detect sensitive spans and replace them with reversible placeholders.

        Once a value is identified as sensitive it is masked at *every* exact
        occurrence (when ``mask_all_occurrences``), so a repeat the detectors
        missed (common in multi-paragraph documents) can't leak.
        """
        raw = run_detectors(text, self._detectors)
        raw = [
            s for s in raw
            if _has_alnum(s.text)
            and not (s.label in _TITLE_FILTER_LABELS and is_non_pii(s.text))
            and not is_stopword_entity(s.text, s.label)
            and not is_noise_span(s.text, s.label)
            and not is_generic_entity(s.text, s.label)
            and not (s.label == "AMOUNT" and not is_money_amount(s.text))
        ]
        spans = resolve_overlaps(raw, priority=self._priority)
        # Mask declined case-forms of detected entities (e.g. "Лентой" given "Лента").
        extra = propagate_declensions(text, spans)
        if extra:
            spans = resolve_overlaps(spans + extra, priority=self._priority)
        mapping, span_placeholders = assign_placeholders(spans)

        if self._mask_all and mapping:
            anonymized, spans = _apply_all_occurrences(text, mapping)
        else:
            anonymized = _apply(text, spans, span_placeholders)

        return AnonymizationResult(
            text=text,
            anonymized_text=anonymized,
            mapping=mapping,
            spans=tuple(spans),
        )


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


def _apply_all_occurrences(text: str, mapping: Mapping) -> tuple[str, tuple[Span, ...]]:
    """Replace every exact occurrence of each mapped value with its placeholder.

    Originals are matched longest-first so a value containing a shorter one wins.
    Matching is whole-token (word-boundary aware) so a placeholder is never
    spliced into the middle of an unrelated word. Returns the anonymized text and
    the spans of all replaced occurrences.
    """
    items = sorted(mapping.items(), key=lambda kv: len(kv[1]), reverse=True)
    inverse = {orig: ph for ph, orig in items}
    pattern = re.compile("|".join(_bounded(orig) for _, orig in items))

    spans: list[Span] = []
    for m in pattern.finditer(text):
        ph = inverse[m.group(0)]
        spans.append(Span(m.start(), m.end(), _label_of(ph), m.group(0)))
    anonymized = pattern.sub(lambda m: inverse[m.group(0)], text)
    spans.sort(key=lambda s: s.start)
    return anonymized, tuple(spans)


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
) -> Anonymizer:
    """Construct an anonymizer from the layered detectors.

    Layers (cheap to expensive): regex -> NER -> local LLM. Each layer is
    independently switchable; each only adds spans, overlap resolution merges.

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
    return Anonymizer(detectors)


_default = Anonymizer()


def anonymize(text: str) -> AnonymizationResult:
    """Anonymize text with the default regex-only engine (no NER, no model load)."""
    return _default.anonymize(text)
