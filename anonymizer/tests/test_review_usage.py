"""Offline tests for review.py's four usage_log call sites (see
anonymizer/usage_log.py's kind values: review_short_numbers, review_adjacent,
review_recall, review_list). No live LLM server: urllib.request.urlopen is
monkeypatched to return a canned response carrying a "usage" object.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import usage_log  # noqa: E402
from anonymizer.review import (  # noqa: E402
    ReviewConfig,
    _Candidate,
    _ask,
    _ask_adjacent,
    _ask_recall,
    _ask_short_numbers,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


@contextmanager
def _temp_usage_log():
    orig = usage_log.LOG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "usage.jsonl"
        usage_log.LOG_PATH = path
        try:
            yield path
        finally:
            usage_log.LOG_PATH = orig


@contextmanager
def _patched_urlopen(fn):
    orig = urllib.request.urlopen
    urllib.request.urlopen = fn
    try:
        yield
    finally:
        urllib.request.urlopen = orig


@contextmanager
def _patched_calls_mode(mode: str):
    """Force usage_log.USAGE_LOG_CALLS for the duration of the block (see
    anonymizer/tests/test_usage_log.py's helper of the same name)."""
    orig = usage_log.USAGE_LOG_CALLS
    usage_log.USAGE_LOG_CALLS = mode
    try:
        yield
    finally:
        usage_log.USAGE_LOG_CALLS = orig


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _canned(content: str, prompt_tokens: int = 11, completion_tokens: int = 22) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_ask_short_numbers_records_review_short_numbers():
    # Success case: force mode "all" since the point of this test is the
    # raw per-call line shape — under the new default ("errors") a
    # successful call writes no line at all (see test_usage_log.py).
    cfg = ReviewConfig(model="test-model")
    items = [("123", "ctx")]
    lines = ['0. "123" — контекст: «ctx»']
    payload = _canned(json.dumps([{"id": 0, "mask": True, "text": "123"}]))

    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_urlopen(lambda req, timeout=None: _FakeResponse(payload)):
        _ask_short_numbers(lines, items, cfg)
        lines_out = _read_jsonl(log_path)

    calls = [l for l in lines_out if l["kind"] == "review_short_numbers"]
    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"
    assert calls[0]["prompt_tokens"] == 11
    assert calls[0]["completion_tokens"] == 22
    assert calls[0]["ok"] is True


def test_ask_adjacent_records_review_adjacent():
    cfg = ReviewConfig(model="test-model")
    suspects = [("k1", "Вайгус", "context [Вайгус] context")]
    payload = _canned(json.dumps([{"id": 0, "mask": True, "text": "Вайгус"}]))

    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_urlopen(lambda req, timeout=None: _FakeResponse(payload)):
        _ask_adjacent(suspects, cfg)
        lines_out = _read_jsonl(log_path)

    calls = [l for l in lines_out if l["kind"] == "review_adjacent"]
    assert len(calls) == 1
    assert calls[0]["prompt_tokens"] == 11
    assert calls[0]["completion_tokens"] == 22
    assert calls[0]["ok"] is True


def test_ask_recall_records_review_recall():
    cfg = ReviewConfig(model="test-model")
    payload = _canned(json.dumps([{"text": "Иван", "type": "PERSON"}]))

    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_urlopen(lambda req, timeout=None: _FakeResponse(payload)):
        _ask_recall("interim text with [PERSON_1] and Иван", cfg)
        lines_out = _read_jsonl(log_path)

    calls = [l for l in lines_out if l["kind"] == "review_recall"]
    assert len(calls) == 1
    assert calls[0]["prompt_tokens"] == 11
    assert calls[0]["completion_tokens"] == 22
    assert calls[0]["ok"] is True


def test_ask_records_review_list():
    cfg = ReviewConfig(model="test-model")
    batch = [("k1", _Candidate(label="PERSON", text="Иван", context="[Иван]"))]
    payload = _canned(json.dumps([{"id": 0, "keep": True, "text": "Иван"}]))

    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_urlopen(lambda req, timeout=None: _FakeResponse(payload)):
        _ask(batch, cfg)
        lines_out = _read_jsonl(log_path)

    calls = [l for l in lines_out if l["kind"] == "review_list"]
    assert len(calls) == 1
    assert calls[0]["prompt_tokens"] == 11
    assert calls[0]["completion_tokens"] == 22
    assert calls[0]["ok"] is True


def test_ask_records_failure_on_url_error():
    cfg = ReviewConfig(model="test-model")
    batch = [("k1", _Candidate(label="PERSON", text="Иван", context="[Иван]"))]

    def _boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    with _temp_usage_log() as log_path, _patched_urlopen(_boom):
        try:
            _ask(batch, cfg)
        except RuntimeError:
            pass  # existing error handling untouched — see review.py
        lines_out = _read_jsonl(log_path)

    calls = [l for l in lines_out if l["kind"] == "review_list"]
    assert len(calls) == 1
    assert calls[0]["ok"] is False
    assert "connection refused" in calls[0]["error"]


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
