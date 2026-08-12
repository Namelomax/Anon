"""Offline tests for RemoteGLiNERDetector's usage_log instrumentation (see
anonymizer/usage_log.py). No live GLiNER endpoint: urllib.request.urlopen is
monkeypatched to return a canned {"entities": [...]} response.
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
from anonymizer.gliner_remote import RemoteGLiNERConfig, RemoteGLiNERDetector  # noqa: E402


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


def test_extract_records_gliner_call_with_chars_and_no_tokens():
    # Success case: force mode "all" — under the new default ("errors") a
    # successful call writes no per-call line at all (see test_usage_log.py
    # and the module docstring; this matters a lot for gliner specifically,
    # which is the layer making the most calls per document).
    payload = {"entities": [{"text": "Иван", "label": "person", "start": 0, "end": 4, "score": 0.9}]}
    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_urlopen(lambda req, timeout=None: _FakeResponse(payload)):
        det._extract("Иван пошёл домой")
        lines = _read_jsonl(log_path)

    calls = [l for l in lines if l["kind"] == "gliner"]
    assert len(calls) == 1
    assert calls[0]["chars"] == len("Иван пошёл домой")
    assert calls[0]["prompt_tokens"] == 0
    assert calls[0]["completion_tokens"] == 0
    assert calls[0]["ok"] is True


def test_extract_records_failure_on_http_error():
    import io

    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "server error", {}, io.BytesIO(b"internal error"),
        )

    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_urlopen(_boom):
        try:
            det._extract("some chunk")
        except RuntimeError:
            pass  # existing error handling untouched — see gliner_remote.py
        lines = _read_jsonl(log_path)

    calls = [l for l in lines if l["kind"] == "gliner"]
    assert len(calls) == 1
    assert calls[0]["ok"] is False
    assert "500" in calls[0]["error"]


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
