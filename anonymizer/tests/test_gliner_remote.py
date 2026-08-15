"""Offline tests for RemoteGLiNERDetector's usage_log instrumentation (see
anonymizer/usage_log.py). No live GLiNER endpoint: http_pool.post_json is
monkeypatched to return a canned {"entities": [...]} response.
"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import gliner_remote, http_pool, usage_log  # noqa: E402
from anonymizer.gliner_remote import RemoteGLiNERConfig, RemoteGLiNERDetector  # noqa: E402


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


@contextmanager
def _no_sleep():
    """Backoff between retries (see gliner_remote._wait_before_retry) is
    real time.sleep — tests exercising retries patch it out so they don't
    actually wait 0.4/1.2 s per attempt. Patches gliner_remote._sleep, NOT
    time.sleep itself: the latter is the shared stdlib time module, and
    patching it there would mutate it process-wide for every other test."""
    orig = gliner_remote._sleep
    gliner_remote._sleep = lambda _seconds: None
    try:
        yield
    finally:
        gliner_remote._sleep = orig


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
    body = json.dumps(payload).encode("utf-8")
    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    seen_pools = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        seen_pools.append(pool)
        return 200, body

    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_post_json(_fake):
        det._extract("Иван пошёл домой")
        lines = _read_jsonl(log_path)

    calls = [l for l in lines if l["kind"] == "gliner"]
    assert len(calls) == 1
    assert calls[0]["chars"] == len("Иван пошёл домой")
    assert calls[0]["prompt_tokens"] == 0
    assert calls[0]["completion_tokens"] == 0
    assert calls[0]["ok"] is True
    # The whole point of splitting the pools (see http_pool.py's module
    # docstring): a GLiNER call must count against the GLiNER pool, not the
    # chat pool, so a burst of long chat calls can never make it queue.
    assert seen_pools == ["gliner"]


def test_extract_records_failure_on_http_error():
    # 400, not 500: HTTP 5xx is now retried (see the "Retry" section below),
    # so it no longer produces exactly one call — a deterministic 4xx is
    # the case that stays a single, immediate failure.
    seen_pools = []

    def _boom(url, payload_bytes, headers, timeout, *, pool="chat"):
        seen_pools.append(pool)
        return 400, b"bad request"

    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_post_json(_boom):
        try:
            det._extract("some chunk")
        except RuntimeError:
            pass  # existing error handling untouched — see gliner_remote.py
        lines = _read_jsonl(log_path)

    calls = [l for l in lines if l["kind"] == "gliner"]
    assert len(calls) == 1
    assert calls[0]["ok"] is False
    assert "400" in calls[0]["error"]
    assert seen_pools == ["gliner"]


# --- Retry (see RemoteGLiNERConfig.retries / gliner_remote._extract) --------

def test_extract_does_not_retry_on_oserror():
    """OSError из post_json (в т.ч. http_pool.PoolConnectionError) НЕ
    повторяется вообще: http_pool уже сам ретраит эти классы сбоев внутри
    себя и по контракту отдаёт наружу ровно ОДНУ логическую попытку (см.
    докстринг RemoteGLiNERConfig.retries) — второй слой ретраев здесь
    только удваивал бы (а с учётом DNS-ретраев post_json — перемножал)
    число обращений к резолверу."""
    calls = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        calls.append(1)
        raise http_pool.PoolConnectionError("connection reset")

    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_post_json(_fake), _no_sleep():
        try:
            det._extract("Иван пошёл домой")
            raised = False
        except RuntimeError:
            raised = True
        lines = _read_jsonl(log_path)

    assert raised
    assert len(calls) == 1  # ни одного повтора на OSError
    gliner_lines = [l for l in lines if l["kind"] == "gliner"]
    assert len(gliner_lines) == 1
    assert gliner_lines[0]["ok"] is False


def test_extract_retries_on_http_500_and_succeeds():
    payload = {"entities": []}
    body = json.dumps(payload).encode("utf-8")
    calls = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        calls.append(1)
        if len(calls) == 1:
            return 500, b"internal error"
        return 200, body

    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_calls_mode("all"), \
            _patched_post_json(_fake), _no_sleep():
        entities = det._extract("some chunk")
        lines = _read_jsonl(log_path)

    assert len(calls) == 2
    assert entities == []
    gliner_lines = [l for l in lines if l["kind"] == "gliner"]
    assert len(gliner_lines) == 2
    assert [l["ok"] for l in gliner_lines] == [False, True]


def test_extract_does_not_retry_on_http_401():
    """A deterministic 4xx (bad key, bad payload) must fail immediately —
    retrying just burns quota for an error a retry cannot fix."""
    calls = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        calls.append(1)
        return 401, b"unauthorized"

    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_post_json(_fake), _no_sleep():
        try:
            det._extract("some chunk")
            raised = False
        except RuntimeError:
            raised = True
        lines = _read_jsonl(log_path)

    assert raised
    assert len(calls) == 1  # exactly one call — no retry on a non-retryable status
    gliner_lines = [l for l in lines if l["kind"] == "gliner"]
    assert len(gliner_lines) == 1
    assert gliner_lines[0]["ok"] is False


def test_extract_does_not_retry_on_http_429():
    """429 — тоже 4xx, но не «неверный запрос», а «ты шлёшь слишком
    часто»: правильная реакция — притормозить, а не бить по API ещё раз
    тем же самым запросом (см. докстринг RemoteGLiNERConfig.retries)."""
    calls = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        calls.append(1)
        return 429, b"too many requests"

    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_post_json(_fake), _no_sleep():
        try:
            det._extract("some chunk")
            raised = False
        except RuntimeError:
            raised = True
        lines = _read_jsonl(log_path)

    assert raised
    assert len(calls) == 1  # exactly one call — 429 is not retried either
    gliner_lines = [l for l in lines if l["kind"] == "gliner"]
    assert len(gliner_lines) == 1
    assert gliner_lines[0]["ok"] is False


def test_extract_exhausts_all_attempts_and_raises():
    """retries=2 (default) => up to 3 total attempts; if every one fails
    with the one status that IS retried (HTTP 5xx — see the module
    docstring; OSError isn't retried at all, see the test above), _extract
    must still raise RuntimeError (find()'s per-chunk warning path is
    unchanged) and log every attempt."""
    calls = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        calls.append(1)
        return 500, b"internal error"

    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _temp_usage_log() as log_path, _patched_post_json(_fake), _no_sleep():
        try:
            det._extract("some chunk")
            raised = False
        except RuntimeError:
            raised = True
        lines = _read_jsonl(log_path)

    assert raised
    assert len(calls) == 3  # 1 initial attempt + 2 retries (default retries=2)
    gliner_lines = [l for l in lines if l["kind"] == "gliner"]
    assert len(gliner_lines) == 3
    assert all(l["ok"] is False for l in gliner_lines)


# --- Circuit breaker (see RemoteGLiNERConfig.retry_circuit_breaker) --------

def _counting_500(calls_per_chunk: dict):
    """Апстрим, который всегда отвечает HTTP 500 и считает попытки на
    кусок по тексту куска в payload — так подсчёт привязан к конкретному
    куску, а не просто к общему числу вызовов."""

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        chunk = json.loads(payload_bytes)["text"]
        calls_per_chunk[chunk] = calls_per_chunk.get(chunk, 0) + 1
        return 500, b"internal error"

    return _fake


def test_extract_circuit_breaker_reduces_retries_for_later_chunks():
    """С retry_circuit_breaker=2: как только 2 куска провалились
    ОКОНЧАТЕЛЬНО (после исчерпания их собственных ретраев), у всех
    оставшихся кусков этого find() остаётся только одна попытка. Гоняем
    именно find() (не _extract), т.к. предохранитель — это состояние
    вызова find(), а не детектора в целом; concurrency=1 — чтобы куски
    обрабатывались строго по порядку и не было гонки за тем, какой
    воркер первым добьёт счётчик до порога."""
    calls_per_chunk: dict = {}
    text = "\n".join(f"строка {i}" for i in range(5))
    cfg = RemoteGLiNERConfig(retries=2, retry_circuit_breaker=2, concurrency=1)
    det = RemoteGLiNERDetector(cfg)

    with _temp_usage_log(), _patched_post_json(_counting_500(calls_per_chunk)), _no_sleep():
        spans = det.find(text)

    assert spans == []
    assert len(det.warnings) == 5  # все 5 кусков в итоге провалились
    chunks_in_order = [f"строка {i}" for i in range(5)]
    counts = [calls_per_chunk[c] for c in chunks_in_order]
    # первые 2 куска (пока предохранитель ещё не сработал) получили полный
    # набор попыток — retries + 1 = 3; все следующие — ровно одну попытку
    assert counts == [3, 3, 1, 1, 1]


def test_extract_circuit_breaker_disabled_keeps_full_retries():
    """retry_circuit_breaker=0 — предохранитель выключен: даже после
    многих провалов подряд каждый кусок получает полный набор попыток
    (retries + 1), см. докстринг RemoteGLiNERConfig.retry_circuit_breaker
    (``<= 0`` отключает предохранитель)."""
    calls_per_chunk: dict = {}
    text = "\n".join(f"строка {i}" for i in range(5))
    cfg = RemoteGLiNERConfig(retries=2, retry_circuit_breaker=0, concurrency=1)
    det = RemoteGLiNERDetector(cfg)

    with _temp_usage_log(), _patched_post_json(_counting_500(calls_per_chunk)), _no_sleep():
        spans = det.find(text)

    assert spans == []
    assert len(det.warnings) == 5
    chunks_in_order = [f"строка {i}" for i in range(5)]
    assert [calls_per_chunk[c] for c in chunks_in_order] == [3, 3, 3, 3, 3]


def test_extract_circuit_breaker_resets_between_find_calls():
    """Счётчик предохранителя — состояние ОДНОГО вызова find(), а не
    детектора в целом (см. find(): _circuit_breaker_failures сбрасывается
    в начале каждого вызова). Второй find() на свежем документе снова
    начинает с полными ретраями для первых кусков, даже если первый
    find() пробил предохранитель до конца."""
    calls_per_chunk: dict = {}
    cfg = RemoteGLiNERConfig(retries=2, retry_circuit_breaker=2, concurrency=1)
    det = RemoteGLiNERDetector(cfg)
    text1 = "\n".join(f"первый {i}" for i in range(3))
    text2 = "\n".join(f"второй {i}" for i in range(3))

    with _temp_usage_log(), _patched_post_json(_counting_500(calls_per_chunk)), _no_sleep():
        det.find(text1)  # пробивает предохранитель к концу вызова
        calls_per_chunk.clear()
        det.find(text2)  # новый find() — новый, чистый счётчик

    chunks_in_order = [f"второй {i}" for i in range(3)]
    # первые 2 куска ВТОРОГО find() снова получили полный набор попыток —
    # значит предохранитель сбросился, а не унаследовался от первого вызова
    assert [calls_per_chunk[c] for c in chunks_in_order] == [3, 3, 1]


def test_wait_before_retry_applies_jitter_to_backoff():
    """Пауза перед повтором домножается на _retry_jitter() (см.
    _wait_before_retry) — подменяем и _retry_jitter, и _sleep фиксированным
    значением/рекордером, чтобы пауза стала детерминированной и
    проверяемой."""
    recorded: list[float] = []
    orig_jitter = gliner_remote._retry_jitter
    orig_sleep = gliner_remote._sleep
    gliner_remote._retry_jitter = lambda: 0.5
    gliner_remote._sleep = lambda seconds: recorded.append(seconds)
    try:
        det = RemoteGLiNERDetector(RemoteGLiNERConfig())
        for attempt in range(len(RemoteGLiNERDetector._RETRY_BACKOFF_SECONDS)):
            det._wait_before_retry(attempt)
    finally:
        gliner_remote._retry_jitter = orig_jitter
        gliner_remote._sleep = orig_sleep

    expected = [b * 0.5 for b in RemoteGLiNERDetector._RETRY_BACKOFF_SECONDS]
    assert recorded == expected


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
