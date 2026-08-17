"""Tests for recall's unknown-type fallback (see review.py's ``_RECALL_LABELS``,
``_RECALL_TYPE_ALIASES``, ``_normalize_recall_type`` and
``ReviewConfig.recall_allow_unknown_types``).

Background (the measured evidence this closes): ``recall_spans`` used to
relabel any candidate whose ``type`` wasn't an exact member of
``_RECALL_LABELS`` to the generic ``SENSITIVE`` and mask it anyway. Measured
across 3 real documents, ALL 43 values that ended up under SENSITIVE were NOT
personal data (``Excel``, ``FoxPro``, ``Microsoft SQL Server``, ``2012``,
``Dell PowerEdge R740``, ``10:00-12:30``, ``22 года``, ``GPT``, ...) — masking
them destroyed document readability, and SENSITIVE was also the single most
unstable label across repeated runs (9->35 masks on the same document). The
fix: normalize near-miss type spellings (case/whitespace/hyphen variants and a
small alias map for common synonyms like PHONE_NUMBER -> PHONE) BEFORE
deciding, then DROP whatever is still unknown instead of relabelling it —
unless ``recall_allow_unknown_types=True`` restores the old behaviour.

No live upstream calls: ``http_pool.post_json`` is monkeypatched, matching the
convention in test_review_recall_chunking.py / test_review_warnings.py.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool  # noqa: E402
from anonymizer.review import (  # noqa: E402
    ReviewConfig,
    _normalize_recall_type,
    recall_spans,
)


@contextmanager
def _patched_post_json(fn):
    orig = http_pool.post_json
    http_pool.post_json = fn
    try:
        yield
    finally:
        http_pool.post_json = orig


@contextmanager
def _captured_stderr():
    orig = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = orig


def _chat_body(content) -> bytes:
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return json.dumps(payload).encode("utf-8")


def _found_fake(found: list[dict]):
    body = _chat_body(json.dumps(found))

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        return 200, body

    return _fake


_TEXT = "В работе используется Excel и звонить нужно Иванову Петру."


# --- 1. Unknown type produces no span, value stays in the text --------------

def test_unknown_type_produces_no_span_and_value_stays_visible():
    cfg = ReviewConfig(model="test-model")
    fake = _found_fake([{"text": "Excel", "type": "TECHNOLOGY"}])

    with _patched_post_json(fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    assert out == []
    assert "Excel" in _TEXT  # sanity: value untouched in the source text


# --- 2. Variant type names are kept and land on the canonical label ---------

def test_variant_type_names_land_on_canonical_labels():
    cfg = ReviewConfig(model="test-model")
    variants = [
        ("PHONE_NUMBER", "PHONE"),
        ("COMPANY", "ORG"),
        ("FULL_NAME", "PERSON"),
    ]
    for raw_type, canonical in variants:
        fake = _found_fake([{"text": "Иванову", "type": raw_type}])
        with _patched_post_json(fake), _captured_stderr():
            out = recall_spans(_TEXT, [], cfg)
        assert len(out) == 1, (raw_type, out)
        assert out[0].label == canonical, (raw_type, out[0].label)
        assert out[0].text == "Иванову"


# --- 3. Case/format-insensitive normalization --------------------------------

def test_case_and_format_variants_all_normalize_to_phone():
    for raw in ("phone_number", "Phone-Number", "PHONE NUMBER"):
        assert _normalize_recall_type(raw) == "PHONE"


# --- 4. Known types are unaffected -------------------------------------------

def test_known_type_person_is_unaffected():
    cfg = ReviewConfig(model="test-model")
    fake = _found_fake([{"text": "Иванову", "type": "PERSON"}])

    with _patched_post_json(fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    assert len(out) == 1
    assert out[0].label == "PERSON"
    assert out[0].text == "Иванову"


# --- 5. recall_allow_unknown_types=True restores the old SENSITIVE fallback -

def test_allow_unknown_types_restores_sensitive_masking():
    cfg = ReviewConfig(model="test-model", recall_allow_unknown_types=True)
    fake = _found_fake([{"text": "Excel", "type": "TECHNOLOGY"}])

    with _patched_post_json(fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    assert len(out) == 1
    assert out[0].label == "SENSITIVE"
    assert out[0].text == "Excel"


# --- 6. Dropped candidates are summarized to stderr, not in warnings --------

def test_dropped_candidates_are_printed_to_stderr_not_in_warnings():
    cfg = ReviewConfig(model="test-model")
    fake = _found_fake([{"text": "Excel", "type": "TECHNOLOGY"}])

    warnings: list[dict] = []
    with _patched_post_json(fake), _captured_stderr() as buf:
        out = recall_spans(_TEXT, [], cfg, warnings)

    assert out == []
    stderr_text = buf.getvalue()
    assert "Excel" in stderr_text
    assert "неизвестным типом" in stderr_text
    assert warnings == []
    for w in warnings:
        assert "Excel" not in json.dumps(w, ensure_ascii=False)


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else str(failures) + ' FAILURE(S)'}")
    sys.exit(1 if failures else 0)
