"""Offline tests for the LLM detector's parsing/locating logic (no server)."""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool, usage_log  # noqa: E402
from anonymizer.llm import (  # noqa: E402
    LLMConfig,
    LLMDetector,
    _extract_json_array,
    _find_all,
    _normalize_type,
    _parse_items,
)


def test_extract_json_array_from_fenced_block():
    s = 'Sure, here:\n```json\n[{"text": "Иван", "type": "PERSON"}]\n```'
    assert _extract_json_array(s) == '[{"text": "Иван", "type": "PERSON"}]'


def test_extract_json_array_returns_last_candidate():
    s = '[{"text": "draft"}] then final [{"text": "real", "type": "INN"}]'
    assert '"real"' in _extract_json_array(s)


def test_extract_json_array_respects_brackets_in_strings():
    s = '[{"text": "a[b]c", "type": "PERSON"}]'
    assert _extract_json_array(s) == s


def test_extract_json_array_none_when_absent():
    assert _extract_json_array("no json here") is None


def test_parse_items_filters_malformed():
    content = '[{"text":"Иван","type":"PERSON"},{"type":"INN"},{"text":"","type":"X"},42]'
    assert _parse_items(content) == [("Иван", "PERSON")]


def test_find_all_exact_multiple():
    text = "a@b.ru важна. снова a@b.ru."
    assert _find_all(text, "a@b.ru") == [(0, 6), (20, 26)]


def test_find_all_whitespace_insensitive_fallback():
    text = "почта n . makarov @ aol . com тут"
    spans = _find_all(text, "n . makarov @ aol .  com")  # extra spaces
    assert len(spans) == 1
    s, e = spans[0]
    assert text[s:e] == "n . makarov @ aol . com"


def test_find_all_missing_returns_empty():
    assert _find_all("hello world", "nonexistent") == []


def test_normalize_type_maps_families():
    assert _normalize_type("first_name") == "PERSON"
    assert _normalize_type("CITY") == "LOCATION"
    assert _normalize_type("SNILS") == "SNILS"


def test_detector_locate_builds_spans_without_server():
    text = "Меня зовут Иван, ИНН 500100732259."
    det = LLMDetector()
    # bypass HTTP: feed a canned model reply through the real parse/locate path
    det._complete = lambda t: '[{"text":"Иван","type":"PERSON"},{"text":"500100732259","type":"INN"}]'
    spans = det.find(text)
    by_label = {s.label: text[s.start : s.end] for s in spans}
    assert by_label == {"PERSON": "Иван", "INN": "500100732259"}
    assert all(s.source == "llm" for s in spans)


def test_detector_drops_unlocatable_hallucination():
    text = "Меня зовут Иван."
    det = LLMDetector()
    det._complete = lambda t: '[{"text":"Пётр Сидоров","type":"PERSON"}]'  # not in text
    assert det.find(text) == []


# --- usage_log instrumentation (see anonymizer/usage_log.py) ----------------
# No live server here: http_pool.post_json is monkeypatched to return a
# canned vLLM-style response carrying a "usage" object, per the task's
# constraint against calling the real upstream endpoint.

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
    """``fn(url, payload_bytes, headers, timeout, *, pool="chat") ->
    (status, body_bytes)``, same signature as ``http_pool.post_json`` — see
    that module."""
    orig = http_pool.post_json
    http_pool.post_json = fn
    try:
        yield
    finally:
        http_pool.post_json = orig


def _ok(payload: dict, seen_pools: list | None = None):
    body = json.dumps(payload).encode("utf-8")

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        if seen_pools is not None:
            seen_pools.append(pool)
        return 200, body

    return _fake


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


def test_request_once_records_llm_detect_usage():
    # Success case: force mode "all" — under the new default ("errors") a
    # successful call writes no per-call line at all (see test_usage_log.py).
    payload = {
        "choices": [{"message": {"content": "NONE"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 7},
    }
    det = LLMDetector(LLMConfig(model="test-model"))
    seen_pools: list = []
    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_post_json(_ok(payload, seen_pools)):
        det._request_once("some chunk of text")
        lines = _read_jsonl(log_path)

    calls = [l for l in lines if l["kind"] == "llm_detect"]
    assert len(calls) == 1
    assert calls[0]["model"] == "test-model"
    assert calls[0]["prompt_tokens"] == 42
    assert calls[0]["completion_tokens"] == 7
    assert calls[0]["ok"] is True
    # LLM detect calls are chat-completion calls (~seconds, not the ~70 ms
    # GLiNER profile) — they must count against the chat pool, not gliner's.
    assert seen_pools == ["chat"]


def test_request_once_records_failure_on_url_error():
    det = LLMDetector(LLMConfig(model="test-model"))
    seen_pools: list = []

    def _boom(url, payload_bytes, headers, timeout, *, pool="chat"):
        seen_pools.append(pool)
        raise http_pool.PoolConnectionError("connection refused")

    with _temp_usage_log() as log_path, _patched_post_json(_boom):
        try:
            det._request_once("some text")
        except RuntimeError:
            pass  # existing error handling untouched — see llm.py
        lines = _read_jsonl(log_path)

    calls = [l for l in lines if l["kind"] == "llm_detect"]
    assert len(calls) == 1
    assert calls[0]["ok"] is False
    assert "connection refused" in calls[0]["error"]
    assert seen_pools == ["chat"]


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
