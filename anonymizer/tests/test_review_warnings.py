"""Tests for the visibility of failed LLM stages (review, recall, and
review's short-numbers/adjacent sub-stages) through ``AnonymizationResult.warnings``.

Every LLM-stage fail-safe used to only print to stderr and continue,
producing a "successful" document with ``warnings: []`` even when a whole
stage (most dangerously: recall, the layer that catches PII every earlier
layer missed) never ran at all — see engine.py's ``_review_and_recall`` and
review.py's ``review_spans``/``recall_spans`` for where each warning is now
appended. No live upstream calls: ``http_pool.post_json`` is monkeypatched,
matching the convention in test_review_exceptions.py /
test_engine_review_recall_concurrency.py.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool, usage_log  # noqa: E402
from anonymizer.engine import Anonymizer  # noqa: E402
from anonymizer.review import ReviewConfig  # noqa: E402
from anonymizer.spans import Span  # noqa: E402


@contextmanager
def _temp_usage_log():
    """Point usage_log.LOG_PATH at a scratch file — see
    test_review_usage.py's helper of the same name/contract. Every ``_ask*``
    call site in review.py logs via ``usage_log.record_call`` regardless of
    success/failure, so any test exercising them for real needs this."""
    orig = usage_log.LOG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "usage.jsonl"
        usage_log.LOG_PATH = path
        try:
            yield path
        finally:
            usage_log.LOG_PATH = orig


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


def _empty_ok(url, payload_bytes, headers, timeout, *, pool="chat"):
    """Every request succeeds with an empty JSON array — 'no exceptions /
    nothing found', the normal/no-op reply for every one of review.py's four
    call sites."""
    return 200, _chat_body(json.dumps([]))


def _connection_boom(url, payload_bytes, headers, timeout, *, pool="chat"):
    raise http_pool.PoolConnectionError("dns lookup failed")


class _FakeDetector:
    """Minimal Detector: reports one fixed span wherever ``value`` occurs."""

    def __init__(self, label: str, value: str) -> None:
        self.label = label
        self.value = value

    def find(self, text: str) -> list[Span]:
        idx = text.index(self.value)
        return [Span(idx, idx + len(self.value), self.label, self.value, source="llm")]


class _SpanListDetector:
    """Minimal Detector: reports exactly the given, pre-built spans."""

    def __init__(self, spans: list[Span]) -> None:
        self._spans = spans

    def find(self, text: str) -> list[Span]:
        return list(self._spans)


_TEXT = "Встречу назначил Иван Иванов, документы у него."
_PERSON = "Иван Иванов"


def _dispatch_by_system_prompt(**by_marker):
    """Build a fake ``http_pool.post_json`` that routes on a substring of the
    request's system prompt (review.py's four system prompts are textually
    distinct — see review.py's _REVIEW_PROMPT_TEMPLATE / _ADJACENT_SYSTEM_PROMPT
    / _SHORT_NUMBER_SYSTEM_PROMPT / _RECALL_SYSTEM_PROMPT). ``by_marker`` maps
    a marker substring to a handler ``(payload_bytes) -> (status, body)``;
    the first matching marker wins. No match => empty-array success (no-op)."""

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        body = json.loads(payload_bytes)
        system = body["messages"][0]["content"]
        for marker, handler in by_marker.items():
            if marker in system:
                return handler(payload_bytes)
        return 200, _chat_body(json.dumps([]))

    return _fake


# --- 1. review_failed --------------------------------------------------------

def test_review_failure_produces_review_failed_warning_and_keeps_spans_masked():
    """Review's main LLM call failing must not un-mask anything: the detected
    span stays exactly as masked as it was before review existed, but the
    caller now finds out via a warning instead of only a stderr line."""
    anon = Anonymizer(
        [_FakeDetector("PERSON", _PERSON)],
        review_config=ReviewConfig(model="test-model", recall=False),
    )

    with _temp_usage_log(), _patched_post_json(_connection_boom), _captured_stderr():
        res = anon.anonymize(_TEXT)

    assert len(res.warnings) == 1
    warning = res.warnings[0]
    assert warning["kind"] == "review_failed"
    # Product decision: end-user warnings must never carry technical detail
    # (API URLs, HTTP codes, Python exception text) — only stderr does.
    assert "dns lookup failed" not in warning["message"]
    assert "проверку" in warning["message"]
    # Fail-safe unchanged: the detected span is still masked.
    assert _PERSON not in res.anonymized_text
    assert "[PERSON_1]" in res.anonymized_text


# --- 2. recall_failed --------------------------------------------------------

def test_recall_failure_produces_recall_failed_warning_stating_pii_may_remain_unmasked():
    """Recall is the one stage whose failure is a genuine coverage gap (it's
    the layer that ADDS masking for PII no earlier layer caught) — the
    warning message must read as a coverage warning, not a reassurance."""
    anon = Anonymizer(
        [_FakeDetector("PERSON", _PERSON)],
        review_config=ReviewConfig(model="test-model", recall=True),
    )
    fake = _dispatch_by_system_prompt(
        # _RECALL_SYSTEM_PROMPT's opening line; see review.py.
        **{"ПОЛНОТЫ": lambda _b: (_ for _ in ()).throw(
            http_pool.PoolConnectionError("dns lookup failed")
        )}
    )

    with _temp_usage_log(), _patched_post_json(fake), _captured_stderr():
        res = anon.anonymize(_TEXT)

    assert len(res.warnings) == 1
    warning = res.warnings[0]
    assert warning["kind"] == "recall_failed"
    assert "dns lookup failed" not in warning["message"]
    assert "могла остаться незамаскированной" in warning["message"]
    # Review itself succeeded (empty-array reply) so the detected span is
    # still masked; recall simply never got to look for anything ELSE.
    assert _PERSON not in res.anonymized_text
    assert "[PERSON_1]" in res.anonymized_text


# --- 3. review_short_numbers_failed ------------------------------------------

def test_short_numbers_failure_produces_own_warning_and_leaves_number_unmasked():
    """Same fail-safe as today (a short number the model couldn't be asked
    about is left UNMASKED, not masked — see review._judge_short_numbers'
    docstring), now additionally reported."""
    text = "Код подразделения 42 указан в реестре."
    start = text.index("42")
    num_span = Span(start, start + 2, "ADMIN_CODE", "42", source="llm")
    anon = Anonymizer(
        [_SpanListDetector([num_span])],
        review_config=ReviewConfig(model="test-model", recall=False),
    )

    with _temp_usage_log(), _patched_post_json(_connection_boom), _captured_stderr():
        res = anon.anonymize(text)

    assert len(res.warnings) == 1
    warning = res.warnings[0]
    assert warning["kind"] == "review_short_numbers_failed"
    assert "dns lookup failed" not in warning["message"]
    # Existing fail-safe unchanged: the bare short number stays visible.
    assert "42" in res.anonymized_text


# --- 4. review_adjacent_failed ------------------------------------------------

def test_adjacent_recheck_failure_produces_own_warning_and_keeps_both_spans_masked():
    """A PERSON candidate review dropped, sitting directly next to one it
    kept, is escalated to the adjacency re-check (see
    review._adjacent_dropped_persons / _ask_adjacent). If THAT call fails,
    the fail-safe default is "keep masked" — unchanged here, just reported."""
    text = "Капитан Яков позвонил. Дело закрыто."
    cap_start = text.index("Капитан")
    cap_span = Span(cap_start, cap_start + len("Капитан"), "PERSON", "Капитан", source="llm")
    yak_start = text.index("Яков")
    yak_span = Span(yak_start, yak_start + len("Яков"), "PERSON", "Яков", source="llm")
    anon = Anonymizer(
        [_SpanListDetector([cap_span, yak_span])],
        review_config=ReviewConfig(model="test-model", recall=False),
    )

    def _main_review_drops_captain(_body):
        return 200, _chat_body(json.dumps([{"id": 0, "text": "Капитан", "keep": False}]))

    def _adjacent_boom(_body):
        raise http_pool.PoolConnectionError("dns lookup failed")

    fake = _dispatch_by_system_prompt(
        # _ADJACENT_SYSTEM_PROMPT is distinctive text; check it BEFORE the
        # main review marker since both mention "контролёр анонимизации".
        **{
            "ВПЛОТНУЮ": _adjacent_boom,
            "контролёр качества анонимизации": _main_review_drops_captain,
        }
    )

    with _temp_usage_log(), _patched_post_json(fake), _captured_stderr():
        res = anon.anonymize(text)

    assert len(res.warnings) == 1
    warning = res.warnings[0]
    assert warning["kind"] == "review_adjacent_failed"
    assert "dns lookup failed" not in warning["message"]
    # Fail-safe unchanged: both stay masked (main review's drop of "Капитан"
    # is overridden back to "keep masked" because the re-check couldn't run).
    assert "Капитан" not in res.anonymized_text
    assert "Яков" not in res.anonymized_text


# --- 5. No false alarms on a fully successful run ----------------------------

def test_fully_successful_review_and_recall_run_produces_no_new_warnings():
    anon = Anonymizer(
        [_FakeDetector("PERSON", _PERSON)],
        review_config=ReviewConfig(model="test-model", recall=True),
    )

    with _temp_usage_log(), _patched_post_json(_empty_ok):
        res = anon.anonymize(_TEXT)

    assert res.warnings == ()
    assert _PERSON not in res.anonymized_text
    assert "[PERSON_1]" in res.anonymized_text


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
