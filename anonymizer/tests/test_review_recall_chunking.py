"""Tests for recall's input chunking, per-value dedup, and per-chunk fail-safe
(see review.py's ``ReviewConfig.recall_max_chars`` / ``.recall_max_tokens``
and ``recall_spans``).

Background (the incident this closes): ``_ask_recall`` used to send the ENTIRE
masked document as one user message, sharing its output budget with the main
review pass's 16000-token ``max_tokens`` — on a real document this produced
``prompt_tokens + max_tokens > context_length`` (HTTP 400) on EVERY run, so
recall — the only layer that ADDS masks for PII every earlier layer missed —
never actually ran outside of small toy inputs. This file pins: (1) the
document is now chunked at ``recall_max_chars``, (2) values found in
different chunks are all placed, (3) a value reported by more than one chunk
is placed once, (4)/(5) one chunk failing no longer discards every other
chunk's results (only ALL chunks failing does), and (6)/(7) recall's own,
smaller ``recall_max_tokens`` budget and cooperative cancellation.

No live upstream calls: ``http_pool.post_json`` is monkeypatched, matching
the convention in test_review_warnings.py / test_review_usage.py.

Below that, a second block of tests pins ``ReviewConfig.recall_concurrency``
(chunks dispatched over a ``ThreadPoolExecutor`` instead of sequentially):
same coverage as above (dedup, one-chunk-failure, all-chunks-failure,
``Cancelled``) run again under concurrency, PLUS an explicit determinism
test — completion order is scrambled on purpose (one chunk answers slower
than the others), yet the merged result must come out byte-for-byte
identical to the strictly sequential path, because merging happens by
CHUNK INDEX, never by whichever request finished first.
"""

from __future__ import annotations

import io
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool  # noqa: E402
from anonymizer.llm import Cancelled  # noqa: E402
from anonymizer.review import (  # noqa: E402
    _RECALL_FAILED_MESSAGE,
    _RECALL_PARTIAL_MESSAGE,
    _ask_recall,
    ReviewConfig,
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


def _ok(found: list[dict]):
    body = _chat_body(json.dumps(found))

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        return 200, body

    return _fake


def _user_content(payload_bytes: bytes) -> str:
    return json.loads(payload_bytes)["messages"][1]["content"]


# Шесть строк-заполнителей, среди которых спрятаны две "пропущенные" ПДн:
# фамилия "Смирнова" (строка 3) и организация "Ромашка" (строка 5). При
# recall_max_chars=200 они попадают в РАЗНЫЕ куски (см. проверку ниже).
_LINES = [
    "Первая строка текста для заполнения объёма документа номер один тут.",
    "Вторая строка текста для заполнения объёма документа номер два тут же.",
    "Третья строка текста, где встречается фамилия Смирнова где-то тут рядом.",
    "Четвёртая строка текста для заполнения объёма документа номер четыре ещё.",
    "Пятая строка текста, где встречается организация Ромашка тут же рядышком.",
    "Шестая строка текста для заполнения объёма документа номер шесть в конце.",
]
_TEXT = "\n".join(_LINES)


# --- 1. Chunking itself: multiple calls, each within recall_max_chars -------

def test_large_document_produces_multiple_recall_calls_each_within_max_chars():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200)
    calls: list[str] = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        calls.append(_user_content(payload_bytes))
        return 200, _chat_body(json.dumps([]))

    with _patched_post_json(_fake), _captured_stderr():
        recall_spans(_TEXT, [], cfg)

    assert len(calls) > 1, "document should have been split into several chunks"
    assert all(len(c) <= cfg.recall_max_chars for c in calls)


# --- 2. Values from different chunks are all placed --------------------------

def test_values_found_in_different_chunks_are_all_placed():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        user = _user_content(payload_bytes)
        if "Смирнова" in user:
            return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))
        if "Ромашка" in user:
            return 200, _chat_body(json.dumps([{"text": "Ромашка", "type": "ORG"}]))
        return 200, _chat_body(json.dumps([]))

    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    texts = {s.text for s in out}
    assert "Смирнова" in texts
    assert "Ромашка" in texts


# --- 3. A value reported by two different chunks is placed once -------------

def test_value_returned_by_two_chunks_is_placed_once_not_twice():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        # Каждый кусок "находит" одно и то же значение — оно, тем не менее,
        # встречается в исходном тексте ровно один раз.
        return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))

    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    matches = [s for s in out if s.text == "Смирнова"]
    assert len(matches) == 1


# --- 4. One chunk failing does not discard the others' results --------------

def test_one_failed_chunk_leaves_survivors_results_and_warns_partial():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200)
    # "FAILMARK" сидит в первой строке — это гарантирует, что именно ПЕРВЫЙ
    # кусок (в него попадает строка 0) провалится, а остальные — нет.
    lines = list(_LINES)
    lines[0] = lines[0] + " FAILMARK"
    text = "\n".join(lines)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        user = _user_content(payload_bytes)
        if "FAILMARK" in user:
            raise http_pool.PoolConnectionError("dns lookup failed")
        if "Смирнова" in user:
            return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))
        if "Ромашка" in user:
            return 200, _chat_body(json.dumps([{"text": "Ромашка", "type": "ORG"}]))
        return 200, _chat_body(json.dumps([]))

    warnings: list[dict] = []
    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(text, [], cfg, warnings)

    texts = {s.text for s in out}
    assert "Смирнова" in texts
    assert "Ромашка" in texts
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "recall_partial"
    assert warnings[0]["message"] == _RECALL_PARTIAL_MESSAGE
    assert "dns lookup failed" not in warnings[0]["message"]


# --- 5. All chunks failing produces exactly one recall_failed warning -------

def test_all_chunks_failing_produces_no_spans_and_one_recall_failed_warning():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        raise http_pool.PoolConnectionError("dns lookup failed")

    warnings: list[dict] = []
    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg, warnings)

    assert out == []
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "recall_failed"
    assert warnings[0]["message"] == _RECALL_FAILED_MESSAGE


# --- 6. _ask_recall uses recall_max_tokens, not the shared max_tokens -------

def test_ask_recall_sends_recall_max_tokens_not_shared_max_tokens():
    cfg = ReviewConfig(model="test-model")
    assert cfg.recall_max_tokens == 2000
    assert cfg.max_tokens == 16000
    captured: dict = {}

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        captured["max_tokens"] = json.loads(payload_bytes)["max_tokens"]
        return 200, _chat_body(json.dumps([]))

    with _patched_post_json(_fake), _captured_stderr():
        _ask_recall("some interim text", cfg)

    assert captured["max_tokens"] == cfg.recall_max_tokens
    assert captured["max_tokens"] != cfg.max_tokens


# --- 7. Cancelled propagates out of recall_spans, never becomes a warning ---

def test_cancelled_propagates_out_of_recall_spans_and_is_not_a_warning():
    cfg = ReviewConfig(model="test-model")  # recall_max_chars default: one chunk

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        raise Cancelled()

    warnings: list[dict] = []
    with _patched_post_json(_fake), _captured_stderr():
        try:
            recall_spans(_TEXT, [], cfg, warnings)
        except Cancelled:
            pass
        else:
            assert False, "expected Cancelled to propagate out of recall_spans"

    assert warnings == []


# =============================================================================
# recall_concurrency: same fail-safes, now dispatched over a thread pool.
# =============================================================================

# Три строки-пары, каждая пара — отдельный кусок при recall_max_chars=200
# (проверено: chunk_text группирует их именно так). У каждого куска — своё
# "пропущенное" значение, порядок которых в документе: Кузнецова -> Смирнова
# -> Ромашка. Используется для теста детерминизма слияния (см. ниже).
_DET_LINES = [
    "Первая строка текста, где встречается фамилия Кузнецова где-то тут же.",
    "Вторая строка текста для заполнения объёма документа номер два тут же.",
    "Третья строка текста, где встречается фамилия Смирнова где-то тут рядом.",
    "Четвёртая строка текста для заполнения объёма документа номер четыре ещё.",
    "Пятая строка текста, где встречается организация Ромашка тут же рядышком.",
    "Шестая строка текста для заполнения объёма документа номер шесть в конце.",
]
_DET_TEXT = "\n".join(_DET_LINES)


def _det_fake(url, payload_bytes, headers, timeout, *, pool="chat"):
    user = _user_content(payload_bytes)
    if "Кузнецова" in user:
        # Первый по индексу кусок нарочно отвечает МЕДЛЕННЕЕ остальных — под
        # конкурентным диспетчером он завершится ПОСЛЕДНИМ, хотя ушёл первым.
        time.sleep(0.05)
        return 200, _chat_body(json.dumps([{"text": "Кузнецова", "type": "PERSON"}]))
    if "Смирнова" in user:
        return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))
    if "Ромашка" in user:
        return 200, _chat_body(json.dumps([{"text": "Ромашка", "type": "ORG"}]))
    return 200, _chat_body(json.dumps([]))


# --- 8. Every chunk still requested exactly once; all found values placed ---

def test_concurrent_every_chunk_requested_once_and_all_values_placed():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=4)
    calls: list[str] = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        user = _user_content(payload_bytes)
        calls.append(user)
        if "Смирнова" in user:
            return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))
        if "Ромашка" in user:
            return 200, _chat_body(json.dumps([{"text": "Ромашка", "type": "ORG"}]))
        return 200, _chat_body(json.dumps([]))

    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    assert len(calls) == 3, "each of the three chunks should be requested exactly once"
    texts = {s.text for s in out}
    assert "Смирнова" in texts
    assert "Ромашка" in texts


# --- 9. Order determinism: scrambled completion order, same merged result ---

def test_concurrent_merge_order_matches_sequential_despite_scrambled_completion():
    cfg_seq = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=1)
    cfg_conc = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=4)

    with _patched_post_json(_det_fake), _captured_stderr():
        out_seq = recall_spans(_DET_TEXT, [], cfg_seq)
    with _patched_post_json(_det_fake), _captured_stderr():
        out_conc = recall_spans(_DET_TEXT, [], cfg_conc)

    seq_order = [s.text for s in out_seq]
    conc_order = [s.text for s in out_conc]
    # Документный порядок: Кузнецова (кусок 0) -> Смирнова (кусок 1) ->
    # Ромашка (кусок 2) — несмотря на то, что под конкурентным диспетчером
    # кусок 0 физически отвечает ПОСЛЕДНИМ (см. _det_fake).
    assert seq_order == ["Кузнецова", "Смирнова", "Ромашка"]
    assert conc_order == seq_order


# --- 10. Dedup across chunks still collapses under concurrency --------------

def test_concurrent_value_returned_by_two_chunks_is_placed_once_not_twice():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=4)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        # Каждый кусок "находит" одно и то же значение — оно, тем не менее,
        # встречается в исходном тексте ровно один раз.
        return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))

    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    matches = [s for s in out if s.text == "Смирнова"]
    assert len(matches) == 1


# --- 11. One chunk failing under concurrency: survivors placed, one partial -

def test_concurrent_one_failed_chunk_leaves_survivors_and_warns_partial():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=4)
    lines = list(_LINES)
    lines[0] = lines[0] + " FAILMARK"
    text = "\n".join(lines)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        user = _user_content(payload_bytes)
        if "FAILMARK" in user:
            raise http_pool.PoolConnectionError("dns lookup failed")
        if "Смирнова" in user:
            return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))
        if "Ромашка" in user:
            return 200, _chat_body(json.dumps([{"text": "Ромашка", "type": "ORG"}]))
        return 200, _chat_body(json.dumps([]))

    warnings: list[dict] = []
    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(text, [], cfg, warnings)

    texts = {s.text for s in out}
    assert "Смирнова" in texts
    assert "Ромашка" in texts
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "recall_partial"
    assert warnings[0]["message"] == _RECALL_PARTIAL_MESSAGE


# --- 12. All chunks failing under concurrency: exactly one recall_failed ----

def test_concurrent_all_chunks_failing_produces_one_recall_failed_warning():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=4)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        raise http_pool.PoolConnectionError("dns lookup failed")

    warnings: list[dict] = []
    with _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg, warnings)

    assert out == []
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "recall_failed"
    assert warnings[0]["message"] == _RECALL_FAILED_MESSAGE


# --- 13. Cancelled inside a worker propagates out of recall_spans, no warning

def test_concurrent_cancelled_in_worker_propagates_and_is_not_a_warning():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=4)

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        raise Cancelled()

    warnings: list[dict] = []
    with _patched_post_json(_fake), _captured_stderr():
        try:
            recall_spans(_TEXT, [], cfg, warnings)
        except Cancelled:
            pass
        else:
            assert False, "expected Cancelled to propagate out of recall_spans"

    assert warnings == []


# --- 14. recall_concurrency<=1 stays on the strictly-sequential path --------

@contextmanager
def _boom_if_thread_pool_used():
    """Патчит ``ThreadPoolExecutor`` так, чтобы его создание падало — чтобы
    убедиться, что ``recall_concurrency<=1`` вообще не пытается им
    воспользоваться (см. ``recall_spans``: ветка сохраняется verbatim)."""
    import concurrent.futures as cf

    orig = cf.ThreadPoolExecutor

    class _Boom(cf.ThreadPoolExecutor):
        def __init__(self, *a, **k):
            raise AssertionError(
                "recall_concurrency<=1 must not use ThreadPoolExecutor"
            )

    cf.ThreadPoolExecutor = _Boom
    try:
        yield
    finally:
        cf.ThreadPoolExecutor = orig


def test_recall_concurrency_one_stays_sequential_and_behaves_as_before():
    cfg = ReviewConfig(model="test-model", recall_max_chars=200, recall_concurrency=1)
    calls: list[str] = []

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        user = _user_content(payload_bytes)
        calls.append(user)
        if "Смирнова" in user:
            return 200, _chat_body(json.dumps([{"text": "Смирнова", "type": "PERSON"}]))
        if "Ромашка" in user:
            return 200, _chat_body(json.dumps([{"text": "Ромашка", "type": "ORG"}]))
        return 200, _chat_body(json.dumps([]))

    with _boom_if_thread_pool_used(), _patched_post_json(_fake), _captured_stderr():
        out = recall_spans(_TEXT, [], cfg)

    assert len(calls) == 3
    texts = {s.text for s in out}
    assert "Смирнова" in texts
    assert "Ромашка" in texts


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
