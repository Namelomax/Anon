"""Confirms the new review/recall failure warnings actually survive the trip
through server.py's response assembly (`_run_anonymize_text`) — the same
place `gliner_chunk_failed` / `llm_chunk_failed` entries already appear (see
server.py: ``"warnings": list(res.warnings)``, used verbatim by both
/anonymize and /anonymize-file and the async job result).

No live upstream calls: http_pool.post_json is monkeypatched. No real
detectors/models are loaded — server._DETECTORS/_DEFAULTS/_REVIEW_CFG are
swapped for the duration of the test (save/restore, matching
test_server_llm_max_chars.py's convention of touching server module state
directly rather than running server.main()).
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool, server, usage_log  # noqa: E402
from anonymizer.review import ReviewConfig  # noqa: E402


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
def _patched_post_json(fn):
    orig = http_pool.post_json
    http_pool.post_json = fn
    try:
        yield
    finally:
        http_pool.post_json = orig


@contextmanager
def _server_state(review_cfg: ReviewConfig):
    """Swap server.py's module-level pipeline config for a minimal one: no
    detectors at all (every detection stage off), only the review layer (with
    recall) wired up — the smallest setup that still exercises
    _compose -> Anonymizer -> AnonymizationResult.warnings -> response dict."""
    orig_detectors = server._DETECTORS
    orig_defaults = server._DEFAULTS
    orig_review_cfg = server._REVIEW_CFG
    orig_ner_backend = server._NER_BACKEND
    orig_needs_lock = server._NEEDS_MODEL_LOCK
    server._DETECTORS = {}
    server._DEFAULTS = {name: False for name in server._STAGE_NAMES}
    server._REVIEW_CFG = review_cfg
    server._NER_BACKEND = "none"
    server._NEEDS_MODEL_LOCK = False
    try:
        yield
    finally:
        server._DETECTORS = orig_detectors
        server._DEFAULTS = orig_defaults
        server._REVIEW_CFG = orig_review_cfg
        server._NER_BACKEND = orig_ner_backend
        server._NEEDS_MODEL_LOCK = orig_needs_lock


def _connection_boom(url, payload_bytes, headers, timeout, *, pool="chat"):
    raise http_pool.PoolConnectionError("dns lookup failed")


def test_recall_failed_warning_reaches_run_anonymize_text_response():
    review_cfg = ReviewConfig(model="test-model", recall=True)

    with _server_state(review_cfg), _temp_usage_log(), _patched_post_json(_connection_boom):
        result = server._run_anonymize_text(
            {"text": "Просто обычный текст без вложенных персональных данных.", "review": True}
        )

    assert "warnings" in result
    kinds = [w["kind"] for w in result["warnings"]]
    assert kinds == ["recall_failed"]
    warning = result["warnings"][0]
    assert "kind" in warning and "message" in warning
    # Product decision: end-user warnings must never carry technical detail
    # (API URLs, HTTP codes, Python exception text) — only stderr does.
    assert "dns lookup failed" not in warning["message"]
    # Same field, same shape a gliner_chunk_failed / llm_chunk_failed entry
    # would show up in — see verify.scan_residual_pii's warnings and
    # LLMDetector.warnings / RemoteGLiNERDetector.warnings for the sibling
    # entries this list also carries.
    assert isinstance(result["warnings"], list)


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
