"""Reversible PII anonymizer for Russian text.

Public API:
    anonymize(text)            -> AnonymizationResult
    deanonymize(text, mapping) -> str

The package is detector-driven: any object implementing the ``Detector``
protocol (a ``find(text) -> list[Span]`` method) can contribute spans. The
default engine uses regex detectors for structured identifiers (documents,
contacts); an NER detector for names/addresses can be plugged in later without
changing the engine or the reversible-mapping logic.
"""

from .spans import Span
from .engine import (
    AnonymizationResult,
    Anonymizer,
    anonymize,
    build_anonymizer,
)
from .deanonymize import deanonymize
from .mapping import Mapping, load_mapping, save_mapping


def __getattr__(name: str):
    # Lazy access to LLM symbols so importing the package never imports the LLM
    # layer eagerly (keeps the core dependency-free).
    if name in ("LLMConfig", "LLMDetector"):
        from . import llm

        return getattr(llm, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Span",
    "AnonymizationResult",
    "Anonymizer",
    "anonymize",
    "build_anonymizer",
    "deanonymize",
    "Mapping",
    "load_mapping",
    "save_mapping",
    "LLMConfig",
    "LLMDetector",
]
