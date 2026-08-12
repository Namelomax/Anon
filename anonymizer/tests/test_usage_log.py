"""Tests for anonymizer.usage_log: JSONL logging, per-document aggregation,
and the ThreadPoolExecutor/contextvars request_id trap (see module docstring).

No live upstream calls here — everything is either pure usage_log plumbing
or a monkeypatched urllib.request.urlopen (see test_llm_review_gliner
instrumentation tests in their respective modules' test files).

Patches usage_log's module globals directly (save/restore), rather than
pytest's monkeypatch fixture, so this file stays runnable standalone via the
__main__ runner below, matching the rest of this test suite's convention
(see test_subject.py).
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import sys
import tempfile
import time
from contextlib import contextmanager, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import usage_log  # noqa: E402


@contextmanager
def _temp_log_path():
    """Point usage_log.LOG_PATH at a fresh temp file for the duration of the
    block, restoring the original value on exit (even on error)."""
    orig = usage_log.LOG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "usage.jsonl"
        usage_log.LOG_PATH = path
        try:
            yield path
        finally:
            usage_log.LOG_PATH = orig


@contextmanager
def _patched_prices(price_in: float, price_out: float):
    orig_in, orig_out = usage_log.PRICE_IN_PER_MTOK, usage_log.PRICE_OUT_PER_MTOK
    usage_log.PRICE_IN_PER_MTOK = price_in
    usage_log.PRICE_OUT_PER_MTOK = price_out
    try:
        yield
    finally:
        usage_log.PRICE_IN_PER_MTOK = orig_in
        usage_log.PRICE_OUT_PER_MTOK = orig_out


@contextmanager
def _patched_calls_mode(mode: str):
    """Force ANONYMIZER_USAGE_LOG_CALLS's resolved value (errors/all/off) for
    the duration of the block, restoring the original module attribute on
    exit — same monkeypatch-the-module-constant convention as LOG_PATH/
    PRICE_*_PER_MTOK above (see module docstring: read once at import,
    patched directly rather than via env + reload)."""
    orig = usage_log.USAGE_LOG_CALLS
    usage_log.USAGE_LOG_CALLS = mode
    try:
        yield
    finally:
        usage_log.USAGE_LOG_CALLS = orig


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_call_writes_parseable_line():
    # Raw per-call line shape is only guaranteed in mode "all" now — under
    # the new default ("errors") a successful call writes no line at all
    # (see test_record_call_success_writes_no_line_under_default_mode below).
    with _temp_log_path() as log_path, _patched_calls_mode("all"):
        usage_log.record_call(
            "llm_detect", model="gemma-4-31b", prompt_tokens=100,
            completion_tokens=50, seconds=1.23456, chars=300, ok=True,
        )
        lines = _read_lines(log_path)

    assert len(lines) == 1
    rec = lines[0]
    assert rec["kind"] == "llm_detect"
    assert rec["model"] == "gemma-4-31b"
    assert rec["prompt_tokens"] == 100
    assert rec["completion_tokens"] == 50
    assert rec["seconds"] == 1.235  # rounded to 3 decimals
    assert rec["chars"] == 300
    assert rec["ok"] is True
    assert rec["error"] is None
    assert isinstance(rec["ts"], str) and rec["ts"]
    assert "request_id" in rec  # None here: no active request_context


def test_record_call_records_failure_without_raising():
    # Default mode is "errors": failed calls still get a line even without
    # forcing "all" — this is the one case the default keeps.
    with _temp_log_path() as log_path:
        usage_log.record_call("gliner", seconds=0.5, chars=120, ok=False, error="boom")
        rec = _read_lines(log_path)[0]

    assert rec["ok"] is False
    assert rec["error"] == "boom"
    assert rec["prompt_tokens"] == 0
    assert rec["completion_tokens"] == 0


def test_record_call_success_writes_no_line_under_default_mode():
    """Default ANONYMIZER_USAGE_LOG_CALLS is "errors": a successful call
    must not produce a per-call line, but must still reach the accumulator
    (request_total totals must not silently go to zero)."""
    with _temp_log_path() as log_path:
        assert usage_log.USAGE_LOG_CALLS == "errors"  # sanity: default unchanged
        with usage_log.request_context(chars=100) as totals:
            usage_log.record_call("llm_detect", prompt_tokens=10, completion_tokens=5, seconds=0.1, ok=True)
        lines = _read_lines(log_path)

    # Exactly one line (the request_total) — no per-call line for the
    # successful call.
    assert len(lines) == 1
    assert lines[0]["kind"] == "request_total"
    # ... yet the call's tokens made it into the aggregate.
    assert lines[0]["prompt_tokens_total"] == 10
    assert lines[0]["completion_tokens_total"] == 5
    assert lines[0]["calls"] == {"llm_detect": 1}
    assert totals.prompt_tokens == 10
    assert totals.completion_tokens == 5


def test_record_call_failure_writes_line_under_default_mode():
    with _temp_log_path() as log_path:
        with usage_log.request_context(chars=100):
            usage_log.record_call("llm_detect", seconds=0.1, ok=False, error="boom")
        lines = _read_lines(log_path)

    call_lines = [l for l in lines if l["kind"] == "llm_detect"]
    assert len(call_lines) == 1
    assert call_lines[0]["ok"] is False
    totals_line = next(l for l in lines if l["kind"] == "request_total")
    assert totals_line["calls"] == {"llm_detect": 1}


def test_record_call_off_mode_suppresses_even_failures():
    with _temp_log_path() as log_path, _patched_calls_mode("off"):
        with usage_log.request_context(chars=100):
            usage_log.record_call("llm_detect", seconds=0.1, ok=False, error="boom")
        lines = _read_lines(log_path)

    # Only the request_total line — no per-call line even for the failure.
    assert len(lines) == 1
    assert lines[0]["kind"] == "request_total"
    assert lines[0]["calls"] == {"llm_detect": 1}  # accumulator still ran


def test_record_call_all_mode_restores_line_per_call():
    with _temp_log_path() as log_path, _patched_calls_mode("all"):
        with usage_log.request_context(chars=100):
            usage_log.record_call("llm_detect", prompt_tokens=1, completion_tokens=1, seconds=0.1, ok=True)
            usage_log.record_call("gliner", seconds=0.1, ok=False, error="boom")
        lines = _read_lines(log_path)

    call_lines = sorted((l["kind"], l["ok"]) for l in lines if l["kind"] != "request_total")
    assert call_lines == [("gliner", False), ("llm_detect", True)]


def test_record_call_never_raises_on_unwritable_path():
    # A path whose parent can't possibly be created must not break the
    # caller — logging failures are never allowed to propagate into the
    # request path (see the module's broad try/except contract). Uses
    # ok=False so a per-call line is attempted (and fails) under every mode,
    # including the new default ("errors").
    orig = usage_log.LOG_PATH
    usage_log.LOG_PATH = Path("Z:\\this\\drive\\does\\not\\exist\\usage.jsonl")
    try:
        buf = io.StringIO()
        with redirect_stderr(buf):
            usage_log.record_call("llm_detect", seconds=0.1, ok=False, error="boom")  # must not raise
        assert "usage_log" in buf.getvalue()
    finally:
        usage_log.LOG_PATH = orig


def test_request_context_writes_single_summary_with_matching_totals():
    """Simulates one document's processing: several successful upstream
    calls inside one request_context. Under the new default mode
    ("errors") this must produce EXACTLY ONE line in the log — the
    request_total — and that line's totals must match the calls made,
    proving _accumulate still ran for every successful call even though no
    per-call line was written for any of them."""
    with _temp_log_path() as log_path:
        with usage_log.request_context(filename="doc.txt", chars=1800, stages={"llm": True}) as totals:
            usage_log.record_call("llm_detect", prompt_tokens=10, completion_tokens=5, seconds=0.5)
            usage_log.record_call("llm_detect", prompt_tokens=20, completion_tokens=8, seconds=0.7)
            usage_log.record_call("gliner", seconds=0.2, chars=400)
        lines = _read_lines(log_path)

    # Exactly one line total: no per-call lines for the successful calls
    # above, only the request_total summary.
    assert len(lines) == 1
    summary = lines[0]
    assert summary["kind"] == "request_total"

    assert summary["request_id"] == totals.request_id
    assert summary["filename"] == "doc.txt"
    assert summary["chars"] == 1800
    assert summary["pages"] == 1.0  # 1800 / 1800
    assert summary["calls"] == {"llm_detect": 2, "gliner": 1}
    assert summary["prompt_tokens_total"] == 30
    assert summary["completion_tokens_total"] == 13
    assert summary["tokens_by_kind"] == {
        "llm_detect": {"prompt_tokens": 30, "completion_tokens": 13},
        "gliner": {"prompt_tokens": 0, "completion_tokens": 0},
    }
    assert summary["stages"] == {"llm": True}
    # seconds_by_kind: cumulative (NOT wall-clock) seconds per kind — sits
    # next to tokens_by_kind, same shape/rounding contract.
    assert summary["seconds_by_kind"] == {"llm_detect": 1.2, "gliner": 0.2}

    # The object yielded by the context manager reflects the same totals
    # AFTER the block exits (server.py reads it this way for the HTTP reply).
    assert totals.prompt_tokens == 30
    assert totals.completion_tokens == 13
    assert totals.calls == {"llm_detect": 2, "gliner": 1}
    assert totals.seconds_by_kind == {"llm_detect": 1.2, "gliner": 0.2}

    # server.py surfaces usage via as_response_dict() — must carry the same
    # seconds_by_kind map (task spec point 3).
    response_usage = totals.as_response_dict()
    assert response_usage["seconds_by_kind"] == {"llm_detect": 1.2, "gliner": 0.2}


def test_seconds_by_kind_sums_correctly_including_failed_calls():
    """seconds_by_kind must accumulate for BOTH successful and failed calls
    (same invariant as tokens/calls — see record_call's docstring point 1),
    and sum correctly across multiple calls of the same kind."""
    with _temp_log_path() as log_path:
        with usage_log.request_context(chars=100) as totals:
            usage_log.record_call("llm_detect", seconds=0.5, ok=True)
            usage_log.record_call("llm_detect", seconds=0.7, ok=False, error="boom")
            usage_log.record_call("gliner", seconds=0.2, ok=True)
        lines = _read_lines(log_path)

    totals_line = next(l for l in lines if l["kind"] == "request_total")
    assert totals_line["seconds_by_kind"] == {"llm_detect": 1.2, "gliner": 0.2}
    assert totals.seconds_by_kind == {"llm_detect": 1.2, "gliner": 0.2}


def test_cumulative_seconds_can_legitimately_exceed_wall_clock_seconds():
    """Pins the semantics called out in the module docstring: seconds_by_kind
    is a SUM across calls of a kind, not wall-clock time. Calls of the same
    kind run in parallel via a ThreadPoolExecutor (through run_in_context,
    exactly like llm.py/gliner_remote.py do), each sleeping for real — so the
    document's wall-clock ``seconds`` genuinely reflects overlapped
    execution, while seconds_by_kind sums each call's own duration
    independently and therefore ends up LARGER than wall-clock. A reader who
    mistook seconds_by_kind for wall-clock time would conclude the opposite
    of the truth — this test pins that the cumulative figure legitimately
    exceeds wall-clock, not just that it's an unrelated number."""

    def _slow_call():
        t0 = time.time()
        time.sleep(0.05)
        usage_log.record_call("gliner", seconds=time.time() - t0)

    with _temp_log_path() as log_path:
        with usage_log.request_context(chars=100) as totals:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [usage_log.run_in_context(pool, _slow_call) for _ in range(8)]
                for f in futures:
                    f.result()
        lines = _read_lines(log_path)

    totals_line = next(l for l in lines if l["kind"] == "request_total")
    cumulative = totals_line["seconds_by_kind"]["gliner"]
    wall = totals_line["seconds"]
    # 8 calls x ~0.05s each run 4-wide -> cumulative ~0.4s, wall ~0.1s.
    assert cumulative > wall
    assert totals.seconds_by_kind["gliner"] == cumulative


def test_request_context_all_mode_keeps_per_call_lines_alongside_summary():
    """Mode "all" restores the old behaviour: per-call lines coexist with
    the request_total summary."""
    with _temp_log_path() as log_path, _patched_calls_mode("all"):
        with usage_log.request_context(filename="doc.txt", chars=1800) as totals:
            usage_log.record_call("llm_detect", prompt_tokens=10, completion_tokens=5, seconds=0.5)
            usage_log.record_call("llm_detect", prompt_tokens=20, completion_tokens=8, seconds=0.7)
            usage_log.record_call("gliner", seconds=0.2, chars=400)
        lines = _read_lines(log_path)

    totals_lines = [l for l in lines if l["kind"] == "request_total"]
    assert len(totals_lines) == 1
    assert totals_lines[0]["request_id"] == totals.request_id

    call_kinds = sorted(l["kind"] for l in lines if l["kind"] != "request_total")
    assert call_kinds == ["gliner", "llm_detect", "llm_detect"]


def test_calls_from_threadpool_get_correct_request_id():
    """Proves run_in_context actually propagates request_id into worker
    threads — the exact trap called out in the task spec."""

    def _log_one(i):
        usage_log.record_call("llm_detect", prompt_tokens=1, completion_tokens=1, seconds=0.01)
        return i

    # Mode "all": this test is specifically about per-call request_id
    # propagation through run_in_context, so it needs the raw per-call
    # lines (all calls here succeed, which the new default would suppress).
    with _temp_log_path() as log_path, _patched_calls_mode("all"):
        with usage_log.request_context(chars=100) as totals:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [usage_log.run_in_context(pool, _log_one, i) for i in range(8)]
                for f in futures:
                    f.result()
        lines = _read_lines(log_path)

    call_lines = [l for l in lines if l["kind"] == "llm_detect"]
    assert len(call_lines) == 8
    assert all(l["request_id"] == totals.request_id for l in call_lines)

    totals_line = next(l for l in lines if l["kind"] == "request_total")
    assert totals_line["prompt_tokens_total"] == 8
    assert totals_line["completion_tokens_total"] == 8
    assert totals_line["calls"] == {"llm_detect": 8}
    assert totals.prompt_tokens == 8
    assert totals.completion_tokens == 8


def test_bare_pool_submit_loses_request_id():
    """Documents the trap the spec warns about: a plain pool.submit() (no
    context copy) does NOT propagate request_id. This is exactly why every
    real call site (llm.py/gliner_remote.py) must go through run_in_context
    instead of pool.submit directly."""

    def _log_one():
        usage_log.record_call("llm_detect", seconds=0.01)

    # Mode "all": needs the raw per-call line to check its request_id (the
    # call succeeds, which the new default would otherwise suppress).
    with _temp_log_path() as log_path, _patched_calls_mode("all"):
        with usage_log.request_context(chars=100):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                pool.submit(_log_one).result()  # bare submit, no ctx.run
        lines = _read_lines(log_path)

    call_line = next(l for l in lines if l["kind"] == "llm_detect")
    assert call_line["request_id"] is None


def test_cost_rub_matches_configured_rates():
    with _temp_log_path() as log_path, _patched_prices(10.0, 20.0):
        with usage_log.request_context(chars=1) as totals:
            usage_log.record_call(
                "llm_detect", prompt_tokens=1_000_000, completion_tokens=500_000, seconds=1,
            )
        summary_line = next(l for l in _read_lines(log_path) if l["kind"] == "request_total")

    # 1M in @ 10/Mtok = 10.0; 0.5M out @ 20/Mtok = 10.0 -> 20.0 total.
    assert totals.cost_rub == 20.0
    assert summary_line["cost_rub"] == 20.0


def test_usage_summary_handles_missing_and_malformed_lines():
    good = {
        "ts": "2026-08-12T10:00:00.000+00:00", "kind": "request_total",
        "request_id": "abc", "pages": 2.0, "prompt_tokens_total": 100,
        "completion_tokens_total": 50, "cost_rub": 1.5, "seconds": 4.0,
    }
    with _temp_log_path() as log_path:
        log_path.write_text(
            json.dumps(good) + "\n"
            + "this is not json at all {{{\n"  # malformed line -> skipped
            + json.dumps({"kind": "llm_detect", "prompt_tokens": 1}) + "\n",  # not request_total
            encoding="utf-8",
        )
        summary = usage_log.usage_summary(days=30)

    assert summary["all_time"]["requests"] == 1
    assert summary["all_time"]["pages"] == 2.0
    assert summary["all_time"]["prompt_tokens"] == 100
    assert summary["all_time"]["completion_tokens"] == 50
    assert summary["all_time"]["cost_rub"] == 1.5
    assert summary["all_time"]["avg_seconds_per_page"] == 2.0  # 4.0s / 2.0 pages
    assert summary["all_time"]["avg_tokens_per_page"] == 75.0  # 150 tok / 2.0 pages
    assert "last_30_days" in summary
    assert "today" in summary


def test_usage_summary_aggregates_by_stage_across_documents_tolerating_malformed():
    """by_stage (usage_report.py table, GET /usage) must aggregate calls/
    seconds/tokens per kind across MULTIPLE request_total lines — and stay
    tolerant of lines missing seconds_by_kind/tokens_by_kind entirely (old
    log lines predating this feature) or carrying a malformed shape for
    those fields, matching usage_summary's existing malformed-line
    tolerance (see test_usage_summary_handles_missing_and_malformed_lines
    above)."""
    rec1 = {
        "ts": "2026-08-12T10:00:00.000+00:00", "kind": "request_total",
        "request_id": "a", "pages": 1.0, "prompt_tokens_total": 10,
        "completion_tokens_total": 5, "cost_rub": 0.1, "seconds": 2.0,
        "calls": {"gliner": 3, "llm_detect": 1},
        "seconds_by_kind": {"gliner": 1.5, "llm_detect": 0.4},
        "tokens_by_kind": {
            "llm_detect": {"prompt_tokens": 10, "completion_tokens": 5},
            "gliner": {"prompt_tokens": 0, "completion_tokens": 0},
        },
    }
    rec2 = {
        "ts": "2026-08-12T11:00:00.000+00:00", "kind": "request_total",
        "request_id": "b", "pages": 2.0, "prompt_tokens_total": 20,
        "completion_tokens_total": 8, "cost_rub": 0.2, "seconds": 3.0,
        "calls": {"gliner": 5},
        "seconds_by_kind": {"gliner": 2.5},
        "tokens_by_kind": {"gliner": {"prompt_tokens": 0, "completion_tokens": 0}},
    }
    # Old-shaped/malformed line: no seconds_by_kind/tokens_by_kind at all,
    # and "calls" isn't even a dict — must be skipped for by_stage without
    # blowing up the whole summary.
    malformed = {
        "ts": "2026-08-12T12:00:00.000+00:00", "kind": "request_total",
        "request_id": "c", "pages": 0.5, "calls": "not-a-dict", "seconds_by_kind": None,
    }
    with _temp_log_path() as log_path:
        log_path.write_text(
            "\n".join(json.dumps(r) for r in (rec1, rec2, malformed))
            + "\n" + "not json at all {{{\n",
            encoding="utf-8",
        )
        summary = usage_log.usage_summary(days=30)

    by_stage = summary["all_time"]["by_stage"]
    assert by_stage["gliner"]["calls"] == 8  # 3 (rec1) + 5 (rec2)
    assert by_stage["gliner"]["seconds"] == 4.0  # 1.5 + 2.5, cumulative not wall-clock
    assert by_stage["gliner"]["avg_seconds"] == 0.5  # 4.0 / 8
    assert by_stage["gliner"]["tokens"] == 0
    assert by_stage["llm_detect"] == {
        "calls": 1, "seconds": 0.4, "avg_seconds": 0.4, "tokens": 15,
    }


def test_usage_summary_missing_log_file_returns_zeros():
    with _temp_log_path() as log_path:
        assert not log_path.exists()  # _temp_log_path only reserves a path, doesn't create it
        summary = usage_log.usage_summary()

    assert summary["all_time"]["requests"] == 0
    assert summary["all_time"]["pages"] == 0.0
    assert summary["today"]["requests"] == 0
    assert summary["all_time"]["by_stage"] == {}


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
