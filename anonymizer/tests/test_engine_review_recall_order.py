"""Tests for the SEQUENTIAL review -> recall pass in engine.py (Anonymizer.anonymize).

Commit 4bbe086 used to parallelise review_spans/recall_spans by feeding
recall the PRE-review span list (so the two calls had no data dependency and
could overlap). That was reverted: a production A/B on a real document
showed PERSON coverage drop from 33 to 29 masked entities the moment recall
stopped seeing review's output. The canonical case is 'Вайгус' — a rare
surname the review model repeatedly judges as non-PII and unmasks; recall
running on review's POST-review text used to see that plaintext surname and
re-flag it, restoring the mask. See engine.py's ``anonymize`` docstring
comment for the full account, including why the speed argument for
parallelising is void (the upstream endpoint is throughput-limited, so
concurrent calls slow each other down instead of overlapping for free).

This file covers: recall receives review's RETURNED span list, not the
pre-review one (the regression guard for the above); review runs strictly
before recall, both in call order and in wall-clock time; each of
review/recall failing leaves a safe, well-defined result without taking the
other down (fail-safes unchanged by 7fa2fe2's warnings plumbing); and
recall's spans still land at the correct offsets in the ORIGINAL text.

No live upstream calls: anonymizer.review.review_spans / recall_spans are
monkeypatched directly (module attribute save/restore, matching the
convention in test_review_exceptions.py / test_usage_log.py), except in the
offset test, which exercises the REAL recall_spans through a monkeypatched
http_pool.post_json to prove the sequential wiring doesn't break its
contract.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
from contextlib import contextmanager
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

    engine.anonymize does ``from .review import ...`` INSIDE the method, so
    it always resolves the module attribute at call time — patching the
    module attribute here reaches it, same as monkeypatching
    http_pool.post_json reaches review.py's HTTP calls in the other test
    files.
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
_ORG = "Ромашка"


def _anonymizer(recall: bool = True) -> Anonymizer:
    cfg = ReviewConfig(model="test-model", recall=recall)
    return Anonymizer([_FakeDetector("PERSON", _PERSON)], review_config=cfg)


def _anonymizer_with_org(recall: bool = True) -> tuple[Anonymizer, str]:
    text = _TEXT + " Представитель " + _ORG + "."
    anon = Anonymizer(
        [_FakeDetector("PERSON", _PERSON), _FakeDetector("ORG", _ORG)],
        review_config=ReviewConfig(model="test-model", recall=recall),
    )
    return anon, text


# --- 1. THE regression guard: recall gets review's OUTPUT, not the detected -
#        list, and is called strictly after it.

def test_recall_called_after_review_with_reviews_returned_spans():
    """Pins the data flow and the order. Must fail if someone reintroduces
    the parallel version (4bbe086), which fed recall the PRE-review span
    list instead of review's result."""
    calls: list[tuple[str, tuple[str, ...]]] = []

    def _review(text, spans, cfg, warnings=None):
        kept = [s for s in spans if s.label == "PERSON"]  # drops the ORG span
        calls.append(("review", tuple(sorted(s.text for s in spans))))
        return kept

    def _recall(text, spans, cfg, warnings=None):
        calls.append(("recall", tuple(sorted(s.text for s in spans))))
        return []

    anon, text = _anonymizer_with_org()
    with _patched_review_recall(_review, _recall):
        anon.anonymize(text)

    assert [kind for kind, _ in calls] == ["review", "recall"], (
        "review must run before recall"
    )
    review_call, recall_call = calls
    # review was handed BOTH detected spans...
    assert review_call[1] == tuple(sorted((_ORG, _PERSON)))
    # ...but recall must be handed review's RETURNED list (ORG dropped), not
    # the original detected list review was given.
    assert recall_call[1] == (_PERSON,)


def test_recall_recatches_value_review_just_unmasked():
    """End-to-end version of the same guard, mirroring the real 'Вайгус'
    interaction: review wrongly drops a span (unmasking it), and because
    recall runs on review's OUTPUT it can see that now-plaintext value and
    re-flag it, restoring the mask. Under the reverted parallel wiring,
    recall would still see the ORG span in its (pre-review) input and this
    assertion inside the stub would fail, causing the recall_failed
    fail-safe to fire and the mask to stay lost — so this test fails loudly
    if the parallel version comes back."""

    def _review_wrongly_drops_org(text, spans, cfg, warnings=None):
        # Simulates the real review model incorrectly deciding a rare/
        # unfamiliar value isn't PII (the 'Вайгус' failure mode).
        return [s for s in spans if s.label != "ORG"]

    anon, text = _anonymizer_with_org()
    org_start = text.index(_ORG)

    def _recall_re_flags_org(text, spans, cfg, warnings=None):
        assert not any(s.label == "ORG" for s in spans), (
            "recall received a pre-review span list — regression!"
        )
        return [Span(org_start, org_start + len(_ORG), "ORG", _ORG, source="llm-recall")]

    with _patched_review_recall(_review_wrongly_drops_org, _recall_re_flags_org):
        res = anon.anonymize(text)

    # Recall's re-flag restored the mask review had incorrectly removed.
    assert _ORG not in res.anonymized_text
    assert _ORG in {s.text for s in res.spans}


# --- 2. Strictly sequential in wall-clock time (not concurrent) -------------

def test_review_and_recall_run_sequentially_not_concurrently():
    delay = 0.2

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

    # Sequential takes ~2*delay. If the two calls overlapped (the reverted
    # 4bbe086 behaviour), elapsed would be close to 1*delay instead.
    assert elapsed > delay * 1.8, (
        f"elapsed={elapsed:.3f}s — looks concurrent, expected sequential (~{2*delay:.3f}s)"
    )


# --- 3. recall raising leaves review's result intact -------------------------

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


# --- 4. review raising leaves detection's spans intact, recall still applies -

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
    # Recall's addition still lands even though review blew up (recall is
    # handed the fallback "spans stayed as detected" list, so it still runs).
    assert phone in texts
    assert "review_spans упал" in buf.getvalue()


# --- 5. Recall's spans land at correct offsets in the ORIGINAL text ---------

def _chat_body(content) -> bytes:
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return json.dumps(payload).encode("utf-8")


def test_recall_spans_land_at_correct_offsets_in_original_text():
    """Exercises the REAL review.recall_spans (not a stub) through engine's
    sequential wiring: it must find the LLM-reported value in the ORIGINAL
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


# --- 6. usage_log grouping: the per-document request_id still covers both ---
#        calls (no thread pool remains, so no run_in_context is needed —
#        both calls happen in the same thread as request_context).

def test_review_and_recall_calls_share_the_same_request_id():
    """Both stages must be visible under the SAME request_total line. Now
    that both calls run sequentially in the caller's own thread (no
    ThreadPoolExecutor), this holds automatically via the contextvar — but
    it's still worth pinning explicitly as a regression guard."""
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
