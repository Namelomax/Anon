"""NER detector backed by a GLiNER model (zero-shot, multilingual).

Alternative to the Natasha detector for names/locations. We use a *multilingual*
GLiNER checkpoint because the text is Russian (an English-only "large" backbone
underperforms on Cyrillic). Entity types are given at inference time as plain
labels; we map GLiNER's coarse labels onto the anonymizer's PERSON / LOCATION.

Relations are not used — this is entity-only extraction. The detector supports
both the standard ``predict_entities`` API and the relex models' ``inference``
API, so it works with e.g. ``knowledgator/gliner-relex-multi-v1.0`` and
``urchade/gliner_multi-v2.1``.

The model is loaded once, lazily, and cached per (model_id, device).
"""

from __future__ import annotations

# IMPORTANT: import lxml BEFORE torch/gliner. On Windows these link conflicting
# native libraries and loading torch first causes a hard segfault. Importing
# lxml.etree first fixes it. (python-docx pulls in lxml, which is why running
# the .docx path before GLiNER "accidentally" worked.)
try:
    import lxml.etree  # noqa: F401
except Exception:  # pragma: no cover - lxml is a python-docx dependency
    pass

import functools
from dataclasses import dataclass, field

from .chunking import chunk_text
from .spans import Span

# GLiNER label (lower-cased) -> anonymizer label.
_DEFAULT_LABEL_MAP: dict[str, str] = {
    "person": "PERSON",
    "name": "PERSON",
    "first name": "PERSON",
    "last name": "PERSON",
    "nickname": "PERSON",
    "location": "LOCATION",
    "address": "LOCATION",
    "city": "LOCATION",
    "street": "LOCATION",
    "region": "LOCATION",
    "country": "LOCATION",
    "organization": "ORG",
    "company": "ORG",
}


def _resolve_device(device: str):
    """Map a device string to a torch device.

    ``"dml"`` selects an AMD/Intel/Nvidia GPU on Windows via DirectML
    (requires ``pip install torch-directml``). ``"cpu"``/``"cuda"`` pass through.
    """
    if device == "dml":
        import torch_directml  # type: ignore

        return torch_directml.device()
    return device


@functools.lru_cache(maxsize=2)
def _load_model(model_id: str, device: str):
    """Build and cache a GLiNER model (downloads on first use)."""
    from gliner import GLiNER

    model = GLiNER.from_pretrained(model_id)
    try:
        model = model.to(_resolve_device(device))
    except Exception as exc:  # keep running on CPU if the device is unavailable
        import sys

        print(f"[gliner] device {device!r} unavailable ({exc}); using CPU", file=sys.stderr)
    return model


@dataclass
class GLiNERConfig:
    """Settings for the GLiNER detector.

    Attributes:
        model_id: HF id. Default is the multilingual relex checkpoint. For pure
            NER you can also use ``urchade/gliner_multi-v2.1``.
        labels: Entity prompts passed to the model. Keep them coarse and
            language-neutral; richer prompts can raise recall.
        threshold: Entity confidence threshold. Lower => higher recall
            (recommended 0.3–0.5 for GLiNER).
        device: ``"cpu"`` or ``"cuda"``.
        label_map: Maps lower-cased GLiNER labels to anonymizer labels.
        flat_ner: If True, enforce non-overlapping entities at the model level.
    """

    model_id: str = "knowledgator/gliner-relex-multi-v1.0"
    # Recall-oriented default: the goal is minimal misses (mislabeling is OK).
    # "address"/"organization" widen coverage (streets, house numbers, company
    # names) at some precision cost — acceptable for anonymization.
    # "first name"/"last name"/"nickname" boost recall on BARE first names,
    # standalone surnames and callsigns in meeting transcripts ("Никита,
    # добрый день") that the coarse "person" label often misses; the review
    # LLM layer filters the extra false positives this brings.
    labels: tuple[str, ...] = (
        "person", "first name", "last name", "nickname",
        "location", "address", "organization",
    )
    threshold: float = 0.45
    device: str = "cpu"
    label_map: dict = field(default_factory=lambda: dict(_DEFAULT_LABEL_MAP))
    flat_ner: bool = False
    max_chars: int = 800  # chunk size for long documents (GLiNER context limit)
    batch_size: int = 12  # chunks per model call (batching cuts per-call overhead)


class GLiNERDetector:
    """Detector that finds people/locations via a GLiNER model."""

    def __init__(self, config: GLiNERConfig | None = None) -> None:
        self.config = config or GLiNERConfig()

    def find(self, text: str) -> list[Span]:
        if not text.strip():
            return []
        model = _load_model(self.config.model_id, self.config.device)
        labels = list(self.config.labels)

        spans: list[Span] = []
        # GLiNER has a limited context window; long documents must be chunked or
        # everything past the first ~hundreds of tokens is silently dropped.
        # Chunks are processed in batches to cut per-call overhead.
        chunks = chunk_text(text, self.config.max_chars, group=False)
        bs = max(1, self.config.batch_size)
        for i in range(0, len(chunks), bs):
            batch = chunks[i : i + bs]
            results = self._predict_batch(model, [c for _, c in batch], labels)
            for (offset, _), entities in zip(batch, results):
                for ent in entities:
                    label = self.config.label_map.get(str(ent.get("label", "")).lower())
                    if label is None:
                        continue
                    start = offset + int(ent["start"])
                    end = offset + int(ent["end"])
                    if end > start:
                        spans.append(Span(start, end, label, text[start:end], source="gliner"))
        return spans

    def _predict_batch(self, model, texts: list[str], labels: list[str]) -> list[list[dict]]:
        """Run inference on a batch of texts; returns a list of entity-lists.

        Предпочитаем современный ``model.inference`` — у relex-чекпойнтов это
        основной API, а ``batch_predict_entities`` помечен deprecated и будет
        удалён в будущих версиях GLiNER (из-за него в логах FutureWarning).
        Если ``inference`` недоступен или у него иная сигнатура — безопасно
        откатываемся на ``batch_predict_entities`` (старое поведение).
        """
        cfg = self.config

        # Современный путь: model.inference (без deprecation-warning).
        if hasattr(model, "inference"):
            try:
                out = model.inference(
                    texts=texts, labels=labels, relations=[],
                    threshold=cfg.threshold, return_relations=False, flat_ner=cfg.flat_ner,
                )
                if isinstance(out, tuple):  # (entities, relations)
                    out = out[0]
                if out is not None:
                    return out if out else [[] for _ in texts]
            except TypeError:
                # Иная сигнатура inference (не relex) — пробуем минимальный набор.
                try:
                    out = model.inference(texts=texts, labels=labels, threshold=cfg.threshold)
                    if isinstance(out, tuple):
                        out = out[0]
                    if out is not None:
                        return out if out else [[] for _ in texts]
                except Exception:
                    pass
            except Exception:
                pass

        # Легаси-путь для старых чекпойнтов без inference.
        out = None
        try:
            out = model.batch_predict_entities(
                texts, labels, threshold=cfg.threshold, flat_ner=cfg.flat_ner
            )
        except TypeError:
            try:
                out = model.batch_predict_entities(texts, labels, threshold=cfg.threshold)
            except Exception:
                out = None
        except Exception:
            out = None
        if isinstance(out, tuple):  # batch_predict_entities returns (entities, relations)
            out = out[0]
        return out if out else [[] for _ in texts]

    def _predict(self, model, text: str, labels: list[str]) -> list[dict]:
        """Run inference via whichever API the loaded model exposes.

        Returns a flat list of entity dicts. Some checkpoints (relex) batch a
        single text and return ``[[...]]``; we unwrap that here.
        """
        cfg = self.config
        out = None
        try:
            out = model.predict_entities(
                text, labels, threshold=cfg.threshold, flat_ner=cfg.flat_ner
            )
        except TypeError:
            try:
                out = model.predict_entities(text, labels, threshold=cfg.threshold)
            except Exception:
                out = None
        except Exception:
            out = None

        if out is None:
            out = model.inference(
                texts=[text],
                labels=labels,
                relations=[],
                threshold=cfg.threshold,
                return_relations=False,
                flat_ner=cfg.flat_ner,
            )
        if isinstance(out, tuple):  # (entities, relations)
            out = out[0]
        if not out:
            return []
        # Unwrap batch dimension: [[ent, ...]] -> [ent, ...]
        if isinstance(out[0], list):
            return out[0]
        return out

    def warmup(self) -> None:
        _load_model(self.config.model_id, self.config.device)
