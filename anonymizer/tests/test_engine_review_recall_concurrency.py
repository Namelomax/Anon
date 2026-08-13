"""Tests for the concurrent review/recall pass in engine.py (Anonymizer._review_and_recall).

Covers the task spec's acceptance criteria: the two LLM calls actually
overlap in wall-clock time; each of review/recall failing leaves a safe,
well-defined result without taking the other down; the merged span list is
identical regardless of which call finishes first; and recall's spans still
land at the correct offsets in the ORIGINAL text.

No live upstream calls: anonymizer.review.review_spans / recall_spans are
monkeypatched directly (module attribute save/restore, matching the
convention in test_review_exceptions.py / test_usage_log.py), except in the
offset test, which exercises the REAL recall_spans through a monkeypatched
http_pool.post_json to prove the concurrent wiring doesn't break its
contract.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool, usage_log  # noqa: E402
from anonymizer import review as review_mod  # noqa: E402
from anonymizer.engine import Anonymizer  # noqa: E402
from anonymizer.review import ReviewConfig  # noqa: E402
from anonymizer.spans import Span  # noqa: E402


@contextmanager
def _temp_usage_log():
    """Point usage_log.LOG_PATH at a scratch file so these tests never write
    to the real logs/usage.jsonl (see test_review_usage.py's helper of the
    same name/contract)."""
    orig = usage_log.LOG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "usage.jsonl"
        usage_log.LOG_PATH = path
        try:
            yield path
        finally:
            usage_log.LOG_PATH = orig


@contextmanager
def _patched_review_recall(review_fn, recall_fn):
    """Swap anonymizer.review.review_spans / recall_spans for test doubles.

    engine._review_and_recall does ``from .review import recall_spans,
    review_spans`` INSIDE the method, so it always resolves the module
    attribute at call time — patching the module attribute here reaches it,
    same as monkeypatching http_pool.post_json reaches review.py's HTTP
    calls in the other test files.
    """
    orig_review, orig_recall = review_mod.review_spans, review_mod.recall_spans
    review_mod.review_spans = review_fn
    review_mod.recall_spans = recall_fn
    try:
        yield
    finally:
        review_mod.review_spans = orig_review
        review_mod.recall_spans = orig_recall


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


class _FakeDetector:
    """Minimal Detector: reports one fixed span wherever ``value`` occurs."""

    def __init__(self, label: str, value: str) -> None:
        self.label = label
        self.value = value

    def find(self, text: str) -> list[Span]:
        idx = text.index(self.value)
        return [Span(idx, idx + len(self.value), self.label, self.value, source="llm")]


_TEXT = "Встречу назначил Иван Иванов, документы у него."
_PERSON = "Иван Иванов"


def _anonymizer(recall: bool = True) -> Anonymizer:
    cfg = ReviewConfig(model="test-model", recall=recall)
    return Anonymizer([_FakeDetector("PERSON", _PERSON)], review_config=cfg)


# --- 1. The two calls actually overlap in wall-clock time -------------------

def test_review_and_recall_overlap_in_wall_clock_time():
    delay = 0.25

    def _slow_review(text, spans, cfg, warnings=None):
        time.sleep(delay)
        return list(spans)

    def _slow_recall(text, spans, cfg, warnings=None):
        time.sleep(delay)
        return []

    anon = _anonymizer()
    with _patched_review_recall(_slow_review, _slow_recall):
        t0 = time.time()
        anon.anonymize(_TEXT)
        elapsed = time.time() - t0

    # Sequential would take ~2*delay; concurrent should be close to 1*delay.
    # Comfortable margin for CI/scheduler jitter — the whole point is
    # "closer to the slower one than to their sum".
    assert elapsed < delay * 1.6, f"elapsed={elapsed:.3f}s, expected close to {delay:.3f}s"
    assert elapsed > delay * 0.9  # sanity: didn't skip the sleep entirely


# --- 2. recall raising leaves review's result intact -------------------------

def test_recall_exception_leaves_review_result_intact():
    def _review_drops_nothing(text, spans, cfg, warnings=None):
        return list(spans)

    def _recall_boom(text, spans, cfg, warnings=None):
        raise RuntimeError("upstream exploded")

    anon = _anonymizer()
    with _patched_review_recall(_review_drops_nothing, _recall_boom), _captured_stderr() as buf:
        res = anon.anonymize(_TEXT)

    assert {s.text for s in res.spans} == {_PERSON}
    assert "[PERSON_1]" in res.anonymized_text
    assert "упал" in buf.getvalue()  # failure was logged, not silently eaten


def test_recall_returning_nothing_leaves_review_result_intact():
    def _review_drops_nothing(text, spans, cfg, warnings=None):
        return list(spans)

    def _recall_empty(text, spans, cfg, warnings=None):
        return []

    anon = _anonymizer()
    with _patched_review_recall(_review_drops_nothing, _recall_empty):
        res = anon.anonymize(_TEXT)

    assert {s.text for s in res.spans} == {_PERSON}


# --- 3. review raising leaves detection's spans intact, recall still applies -

def test_review_exception_leaves_detected_spans_and_still_applies_recall():
    phone = "+7 999 123-45-67"
    text = _TEXT + " Звоните " + phone + "."
    idx = text.index(phone)
    recall_span = Span(idx, idx + len(phone), "PHONE", phone, source="llm-recall")

    def _review_boom(text, spans, cfg, warnings=None):
        raise RuntimeError("review upstream exploded")

    def _recall_finds_phone(text, spans, cfg, warnings=None):
        return [recall_span]

    anon = Anonymizer(
        [_FakeDetector("PERSON", _PERSON)],
        review_config=ReviewConfig(model="test-model", recall=True),
    )
    with _patched_review_recall(_review_boom, _recall_finds_phone), _captured_stderr() as buf:
        res = anon.anonymize(text)

    texts = {s.text for s in res.spans}
    # Detected PERSON span survives (review failed -> spans stay as detected).
    assert _PERSON in texts
    # Recall's addition still lands even though review blew up.
    assert phone in texts
    assert "review_spans упал" in buf.getvalue()


# --- 4. Merge is deterministic regardless of completion order ---------------

def test_merge_identical_regardless_of_which_call_finishes_first():
    phone = "+7 999 123-45-67"
    text = _TEXT + " Звоните " + phone + "."
    idx = text.index(phone)

    def _review_drops_nothing(text, spans, cfg, warnings=None):
        return list(spans)

    def _recall_finds_phone(text, spans, cfg, warnings=None):
        recall_span = Span(idx, idx + len(phone), "PHONE", phone, source="llm-recall")
        return [recall_span]

    def _run(review_delay: float, recall_delay: float):
        def _review(text, spans, cfg, warnings=None):
            time.sleep(review_delay)
            return _review_drops_nothing(text, spans, cfg, warnings)

        def _recall(text, spans, cfg, warnings=None):
            time.sleep(recall_delay)
            return _recall_finds_phone(text, spans, cfg, warnings)

        anon = Anonymizer(
            [_FakeDetector("PERSON", _PERSON)],
            review_config=ReviewConfig(model="test-model", recall=True),
        )
        with _patched_review_recall(_review, _recall):
            return anon.anonymize(text)

    # Force review to finish first, then force recall to finish first.
    res_review_first = _run(review_delay=0.01, recall_delay=0.15)
    res_recall_first = _run(review_delay=0.15, recall_delay=0.01)

    def _shape(res):
        return [(s.start, s.end, s.label, s.text) for s in res.spans]

    assert _shape(res_review_first) == _shape(res_recall_first)
    assert res_review_first.mapping == res_recall_first.mapping
    assert res_review_first.anonymized_text == res_recall_first.anonymized_text


# --- 5. Recall's spans land at correct offsets in the ORIGINAL text ---------

def _chat_body(content) -> bytes:
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return json.dumps(payload).encode("utf-8")


def test_recall_spans_land_at_correct_offsets_in_original_text():
    """Exercises the REAL review.recall_spans (not a stub) through engine's
    concurrent wiring: it must find the LLM-reported value in the ORIGINAL
    text at the correct offsets, not in the interim masked text engine.py
    builds for it."""
    phone = "+7 999 123-45-67"
    text = _TEXT + " Звоните " + phone + "."
    idx = text.index(phone)

    def _review_identity(text, spans, cfg, warnings=None):
        return list(spans)  # keep the detected PERSON, don't touch anything

    def _fake_post_json(url, payload_bytes, headers, timeout, *, pool="chat"):
        # recall_spans's system prompt asks for {"text": ..., "type": ...}.
        body = _chat_body(json.dumps([{"text": phone, "type": "PHONE"}]))
        return 200, body

    anon = Anonymizer(
        [_FakeDetector("PERSON", _PERSON)],
        review_config=ReviewConfig(model="test-model", recall=True),
    )
    with _temp_usage_log(), \
            _patched_review_recall(_review_identity, review_mod.recall_spans), \
            _patched_post_json(_fake_post_json):
        res = anon.anonymize(text)

    phone_spans = [s for s in res.spans if s.label == "PHONE"]
    assert len(phone_spans) == 1
    assert phone_spans[0].start == idx
    assert phone_spans[0].end == idx + len(phone)
    assert phone_spans[0].text == phone
    assert res.mapping.get("[PHONE_1]") == phone
    assert phone not in res.anonymized_text


# --- usage_log grouping: run_in_context propagates request_id to both calls -

def test_review_and_recall_calls_share_the_same_request_id():
    """Proves the concurrent calls actually go through usage_log.run_in_context
    (see usage_log.py's threading-trap docstring): both stubs record a call
    from inside the worker thread and must see the SAME request_id as the
    surrounding usage_log.request_context — a bare pool.submit would leave it
    None in the worker thread."""
    seen_request_ids: list[str | None] = []

    def _review(text, spans, cfg, warnings=None):
        seen_request_ids.append(usage_log.current_request_id())
        return list(spans)

    def _recall(text, spans, cfg, warnings=None):
        seen_request_ids.append(usage_log.current_request_id())
        return []

    anon = _anonymizer()
    with _patched_review_recall(_review, _recall):
        with usage_log.request_context(chars=len(_TEXT)) as totals:
            anon.anonymize(_TEXT)

    assert len(seen_request_ids) == 2
    assert all(rid == totals.request_id for rid in seen_request_ids)
    assert totals.request_id is not None


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
