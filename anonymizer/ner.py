"""NER detector for names and locations, backed by Natasha (Slovnet).

Natasha runs on CPU with Numpy and emits coarse spans — ``PER`` (people),
``LOC`` (geo/political places), ``ORG`` (organizations) — with character
offsets. We map those to the anonymizer's labels:

    PER -> PERSON      LOC -> LOCATION      ORG -> ORG (optional)

These coarse labels match the placeholder scheme in ``anonymizer.skill``
(``[PERSON_N]``, ``[LOCATION_N]``). Fine-grained splits from the benchmark
(FIRST_NAME / CITY / STREET ...) collapse onto PERSON / LOCATION, which is
exactly what reversible redaction needs and what the benchmark's coarse,
overlap-based scoring rewards.

The Natasha models are heavy to construct (~1-2 s, a few hundred MB RAM), so
they are loaded once, lazily, and shared across detector instances.
"""

from __future__ import annotations

import functools
from typing import Mapping

from .spans import Span

# Natasha span type -> anonymizer label.
_DEFAULT_TYPE_MAP: dict[str, str] = {
    "PER": "PERSON",
    "LOC": "LOCATION",
}


@functools.lru_cache(maxsize=1)
def _load_pipeline():
    """Build and cache the Natasha segmenter + NER tagger (lazy, once)."""
    from natasha import NewsEmbedding, NewsNERTagger, Segmenter

    segmenter = Segmenter()
    emb = NewsEmbedding()
    ner_tagger = NewsNERTagger(emb)
    return segmenter, ner_tagger


class NatashaDetector:
    """Detector that finds people/locations (and optionally orgs) via Natasha.

    Args:
        include_org: If True, also emit ``ORG`` spans. Off by default — an
            organization name is not personal data, and including it raises
            over-redaction. Turn on for the corporate-document use case from
            ``anonymizer.skill``.
        type_map: Optional override of the ``natasha_type -> label`` mapping.
    """

    def __init__(
        self,
        *,
        include_org: bool = False,
        type_map: Mapping[str, str] | None = None,
    ) -> None:
        mapping = dict(type_map) if type_map is not None else dict(_DEFAULT_TYPE_MAP)
        if include_org:
            mapping.setdefault("ORG", "ORG")
        self._type_map = mapping

    def find(self, text: str) -> list[Span]:
        if not text.strip():
            return []
        from natasha import Doc

        segmenter, ner_tagger = _load_pipeline()
        doc = Doc(text)
        doc.segment(segmenter)
        doc.tag_ner(ner_tagger)

        spans: list[Span] = []
        for s in doc.spans:
            label = self._type_map.get(s.type)
            if label is None:
                continue
            surface = text[s.start : s.stop]
            spans.append(Span(s.start, s.stop, label, surface, source="ner"))
        return spans

    def warmup(self) -> None:
        """Eagerly load the models (e.g. at app startup) to avoid first-call lag."""
        _load_pipeline()
