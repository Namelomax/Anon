"""Regression tests for the deterministic PERSON-unmask morphology gate (see
detectors.py's "PERSON un-mask morphological gate" section and review.py's
module docstring). No live upstream calls — ``http_pool.post_json`` is
monkeypatched, matching the convention in test_review_exceptions.py.

The motivating production failure: on a real transcript the LLM reviewer
dropped BOTH "Капитан Яков" and "Вайгус" (adjacent PERSON candidates) as
non-PII in the same batch. review.py's existing adjacency escalation
(``_adjacent_dropped_persons``) explicitly does NOT rescue this case — its own
docstring says a pair dropped on BOTH sides doesn't save each other, since
either could really be junk ("Так-так, Ну-ну"). Only a check independent of
the model's own verdict can close this leak — this file proves the gate does,
calling ``review_spans`` directly so recall (a separate function, only wired
up by engine.py) is never even in the picture.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import detectors, http_pool  # noqa: E402
from anonymizer.review import ReviewConfig, review_spans  # noqa: E402
from anonymizer.spans import Span  # noqa: E402


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


def _ok(content):
    body = _chat_body(content if isinstance(content, str) else json.dumps(content))

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        return 200, body

    return _fake


def _mk_spans(text: str, items: list[tuple[str, str]]) -> list[Span]:
    spans = []
    for label, value in items:
        start = text.index(value)
        spans.append(Span(start, start + len(value), label, value, source="llm"))
    return spans


# --- The regression: model drops BOTH adjacent PERSON candidates ------------

def test_captain_yakov_and_vaigus_both_kept_masked_despite_model_dropping_both():
    """Without the gate this test fails: the model's two keep=false verdicts
    would be applied verbatim and both spans would leak into the output."""
    text = "Капитан Яков позвонил. Вайгус ответил. Дело закрыто."
    spans = _mk_spans(text, [("PERSON", "Капитан Яков"), ("PERSON", "Вайгус")])
    cfg = ReviewConfig(model="test-model")
    response = [
        {"id": 0, "text": "Капитан Яков", "keep": False},
        {"id": 1, "text": "Вайгус", "keep": False},
    ]
    with _patched_post_json(_ok(response)), _captured_stderr() as buf:
        out = review_spans(text, spans, cfg)

    kept_texts = {s.text for s in out}
    assert kept_texts == {"Капитан Яков", "Вайгус"}
    assert len(out) == 2
    logged = buf.getvalue()
    assert "person-gate" in logged
    assert "Яков" in logged  # the blocking word is named in the refusal reason


def test_ordinary_word_person_drop_still_goes_through():
    """Control case: a genuine false positive (a common word tagged PERSON)
    must still be dropped — the gate must not turn into a blanket refusal."""
    text = "Добрый День, коллеги."
    spans = _mk_spans(text, [("PERSON", "День")])
    cfg = ReviewConfig(model="test-model")
    response = [{"id": 0, "text": "День", "keep": False}]
    with _patched_post_json(_ok(response)):
        out = review_spans(text, spans, cfg)

    assert out == []  # dropped, as the model (correctly) requested


# --- Fail-safe: morphology unavailable --------------------------------------

def test_morphology_unavailable_keeps_person_masked_and_appends_warning(monkeypatch):
    monkeypatch.setattr(detectors, "_morph_vocab", lambda: None)
    text = "Добрый День, коллеги."
    spans = _mk_spans(text, [("PERSON", "День")])
    cfg = ReviewConfig(model="test-model")
    # Even an ORDINARY word the model wants to drop must stay masked once the
    # gate itself can't run — refuse-all-PERSON-drops is the safe direction.
    response = [{"id": 0, "text": "День", "keep": False}]
    warnings: list = []
    with _patched_post_json(_ok(response)), _captured_stderr():
        out = review_spans(text, spans, cfg, warnings=warnings)

    assert {s.text for s in out} == {"День"}
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "review_person_gate_unavailable"


def test_morphology_unavailable_does_not_crash_and_does_not_affect_org():
    """ORG candidates are outside this gate's scope entirely — an unrelated
    keep=false for ORG must be applied normally regardless of the PERSON
    gate's availability."""
    text = "Отдел Восход закрыт."
    spans = _mk_spans(text, [("ORG", "Восход")])
    cfg = ReviewConfig(model="test-model")
    response = [{"id": 0, "text": "Восход", "keep": False}]
    with _patched_post_json(_ok(response)):
        out = review_spans(text, spans, cfg)

    assert out == []


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
