"""Tests for the pure aggregation/decision logic in anonymizer/loadtest.py:
percentiles, throughput, the warning histogram, the abort-criteria decision,
and the two-protocol layer (BackendProtocol/SiteProtocol: multipart encoder,
submit-response parsing, poll-response classification). All driven by
synthetic data — no network, no threads, no real or stub server (the
end-to-end wiring of both protocols is exercised by
``python anonymizer/loadtest.py --self-test`` instead, per the task spec).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer.loadtest import (  # noqa: E402
    BackendProtocol,
    ClientRecord,
    SiteProtocol,
    _abort_reason,
    _encode_multipart,
    _percentile,
    _throughput,
    _warning_histogram,
    summarize_level,
)


def _rec(status: str = "done", **kw) -> ClientRecord:
    kw.setdefault("job_id", "job")
    kw.setdefault("submit_latency", 0.1)
    kw.setdefault("wait_time", 1.0)
    return ClientRecord(status=status, **kw)


# --- _percentile -------------------------------------------------------------

def test_percentile_empty_list_is_zero():
    assert _percentile([], 50) == 0.0


def test_percentile_single_value():
    assert _percentile([42.0], 50) == 42.0
    assert _percentile([42.0], 95) == 42.0


def test_percentile_p50_even_count_interpolates():
    # sorted [1,2,3,4], p50 index = 1.5 -> interpolate between 2 and 3
    assert abs(_percentile([4, 1, 3, 2], 50) - 2.5) < 1e-9


def test_percentile_p0_and_p100_are_min_and_max():
    values = [5, 1, 9, 3]
    assert _percentile(values, 0) == 1.0
    assert _percentile(values, 100) == 9.0


def test_percentile_p95_of_many_values():
    values = list(range(1, 101))  # 1..100
    # index = (100-1) * 0.95 = 94.05 -> interpolate between values[94]=95 and values[95]=96
    p95 = _percentile(values, 95)
    assert abs(p95 - 95.05) < 1e-9


# --- _throughput ---------------------------------------------------------

def test_throughput_docs_and_pages_per_minute():
    docs_per_min, pages_per_min = _throughput(
        client_count=10, duration_seconds=60.0, doc_chars=3600, chars_per_page=1800
    )
    assert abs(docs_per_min - 10.0) < 1e-9
    # 3600 chars / 1800 chars-per-page = 2 pages per doc -> 20 pages/min
    assert abs(pages_per_min - 20.0) < 1e-9


def test_throughput_zero_duration_is_zero_not_a_crash():
    docs_per_min, pages_per_min = _throughput(
        client_count=5, duration_seconds=0.0, doc_chars=1000, chars_per_page=1800
    )
    assert docs_per_min == 0.0
    assert pages_per_min == 0.0


def test_throughput_zero_clients_is_zero():
    docs_per_min, pages_per_min = _throughput(
        client_count=0, duration_seconds=60.0, doc_chars=1000, chars_per_page=1800
    )
    assert docs_per_min == 0.0
    assert pages_per_min == 0.0


def test_throughput_zero_chars_per_page_does_not_divide_by_zero():
    docs_per_min, pages_per_min = _throughput(
        client_count=5, duration_seconds=60.0, doc_chars=1000, chars_per_page=0
    )
    assert docs_per_min == 5.0
    assert pages_per_min == 0.0


# --- _warning_histogram ----------------------------------------------------

def test_warning_histogram_sums_kinds_across_clients():
    records = [
        _rec(warning_kinds=["review_dropped"]),
        _rec(warning_kinds=["review_dropped", "chunk_failed"]),
        _rec(warning_kinds=[]),
    ]
    assert _warning_histogram(records) == {"review_dropped": 2, "chunk_failed": 1}


def test_warning_histogram_empty_records_is_empty_dict():
    assert _warning_histogram([]) == {}


# --- _abort_reason -----------------------------------------------------------

def test_abort_reason_none_when_all_healthy():
    records = [_rec(status="done") for _ in range(10)]
    assert _abort_reason(records, timeout=900) is None


def test_abort_reason_none_when_records_empty():
    assert _abort_reason([], timeout=900) is None


def test_abort_reason_error_rate_over_20_percent():
    # 3 errors out of 10 = 30% > 20%
    records = [_rec(status="done") for _ in range(7)] + [
        _rec(status="error") for _ in range(3)
    ]
    reason = _abort_reason(records, timeout=900)
    assert reason is not None
    assert "error rate" in reason


def test_abort_reason_error_rate_exactly_20_percent_does_not_abort():
    # 2 errors out of 10 = exactly 20%, spec says "> 20%" (strictly greater)
    records = [_rec(status="done") for _ in range(8)] + [
        _rec(status="error") for _ in range(2)
    ]
    assert _abort_reason(records, timeout=900) is None


def test_abort_reason_any_http_5xx_aborts_even_with_low_error_rate():
    records = [_rec(status="done") for _ in range(9)] + [
        _rec(status="http_error", http_status=502)
    ]
    reason = _abort_reason(records, timeout=900)
    assert reason is not None
    assert "5xx" in reason


def test_abort_reason_4xx_alone_does_not_trigger_5xx_branch():
    records = [_rec(status="done") for _ in range(9)] + [
        _rec(status="http_error", http_status=404)
    ]
    # 1/10 = 10% error rate, no 5xx, no timeout -> healthy
    assert _abort_reason(records, timeout=900) is None


def test_abort_reason_any_client_timeout_aborts():
    records = [_rec(status="done") for _ in range(9)] + [
        _rec(status="timeout", wait_time=900.0)
    ]
    reason = _abort_reason(records, timeout=900)
    assert reason is not None
    assert "timeout" in reason.lower() or "900" in reason


def test_abort_reason_prioritizes_5xx_over_timeout_and_error_rate():
    # Both a 5xx AND high error rate present -> 5xx wins (checked first).
    records = (
        [_rec(status="done") for _ in range(5)]
        + [_rec(status="error") for _ in range(4)]
        + [_rec(status="http_error", http_status=503)]
    )
    reason = _abort_reason(records, timeout=900)
    assert reason is not None
    assert "5xx" in reason


# --- summarize_level (integration of the pieces above) -----------------------

def test_summarize_level_combines_all_metrics():
    records = [
        _rec(status="done", submit_latency=0.1, wait_time=1.0, warning_kinds=["w1"]),
        _rec(status="done", submit_latency=0.2, wait_time=2.0, warning_kinds=["w1"]),
        _rec(status="error", submit_latency=0.3, wait_time=3.0, warning_kinds=[]),
    ]
    summary = summarize_level(records, duration_seconds=30.0, doc_chars=1800, chars_per_page=1800)

    assert summary.client_count == 3
    assert summary.ok == 2
    assert summary.error == 1
    assert summary.wait_max == 3.0
    assert summary.warning_histogram == {"w1": 2}
    # 3 clients over 30s -> 6 docs/min; 1 page/doc -> 6 pages/min
    assert abs(summary.docs_per_min - 6.0) < 1e-9
    assert abs(summary.pages_per_min - 6.0) < 1e-9


# --- _encode_multipart (site-mode submit body) -------------------------------

def test_encode_multipart_boundary_appears_in_body_delimiters():
    body, boundary = _encode_multipart("doc.txt", b"hello world")
    opening = f"--{boundary}\r\n".encode("ascii")
    closing = f"--{boundary}--\r\n".encode("ascii")
    assert body.startswith(opening)
    assert body.endswith(closing)
    # Exactly one opening delimiter and it's immediately followed by the
    # closing one later in the body (a single-part message).
    assert body.count(opening) == 1
    assert body.count(closing) == 1


def test_encode_multipart_contains_filename_and_raw_bytes_unencoded():
    body, _boundary = _encode_multipart("report.docx", b"\x00binary\x01payload")
    assert b'filename="report.docx"' in body
    assert b'name="file"' in body
    # Raw bytes appear verbatim — no base64/URL-encoding for multipart.
    assert b"\x00binary\x01payload" in body


def test_encode_multipart_has_content_disposition_and_content_type_headers():
    body, _boundary = _encode_multipart("doc.txt", b"hi")
    assert b"Content-Disposition: form-data;" in body
    assert b"Content-Type: text/plain" in body


def test_encode_multipart_crlf_framing_between_headers_and_body():
    body, boundary = _encode_multipart("doc.txt", b"PAYLOAD")
    # Headers block ends with a blank line (\r\n\r\n) right before the content.
    assert b"\r\n\r\nPAYLOAD\r\n--" in body
    # No bare \n without a preceding \r anywhere added by the encoder itself
    # (the delimiter lines and header lines must all be CRLF-terminated).
    header_region = body.split(b"\r\n\r\n", 1)[0]
    assert b"\n" not in header_region.replace(b"\r\n", b"")


def test_encode_multipart_returns_distinct_boundaries_each_call():
    _body1, boundary1 = _encode_multipart("a.txt", b"x")
    _body2, boundary2 = _encode_multipart("a.txt", b"x")
    assert boundary1 != boundary2


def test_encode_multipart_content_type_header_matches_returned_boundary():
    submit = SiteProtocol().build_submit("doc.txt", b"payload")
    content_type = submit.headers["Content-Type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1]
    assert f"--{boundary}".encode("ascii") in submit.body
    assert b"payload" in submit.body


# --- BackendProtocol ----------------------------------------------------------

def test_backend_build_submit_is_json_with_base64_body():
    submit = BackendProtocol().build_submit("doc.txt", b"hello")
    assert submit.path == "/jobs/anonymize-file"
    assert submit.headers["Content-Type"] == "application/json"
    payload = json.loads(submit.body)
    assert payload["filename"] == "doc.txt"
    assert payload["file_base64"] == "aGVsbG8="  # base64("hello")


def test_backend_parse_submit_response_reads_job_id_snake_case():
    job_id = BackendProtocol().parse_submit_response(json.dumps({"job_id": "abc123"}).encode())
    assert job_id == "abc123"


def test_backend_poll_path_and_cancel_path():
    proto = BackendProtocol()
    assert proto.poll_path("abc123") == "/jobs/abc123"
    assert proto.cancel_path("abc123") == "/jobs/abc123"


def test_backend_parse_poll_running_done_error_cancelled():
    proto = BackendProtocol()

    running = proto.parse_poll(200, json.dumps({"status": "pending"}).encode())
    assert running.kind == "running"

    done = proto.parse_poll(200, json.dumps({"status": "done", "result": {"elapsed_seconds": 1.5}}).encode())
    assert done.kind == "done"
    assert done.result == {"elapsed_seconds": 1.5}

    errored = proto.parse_poll(200, json.dumps({"status": "error", "error": "boom"}).encode())
    assert errored.kind == "error"
    assert errored.error == "boom"

    cancelled = proto.parse_poll(200, json.dumps({"status": "cancelled"}).encode())
    assert cancelled.kind == "cancelled"


def test_backend_parse_poll_non_200_is_error_with_http_status():
    outcome = BackendProtocol().parse_poll(502, b"Bad Gateway")
    assert outcome.kind == "error"
    assert outcome.http_status == 502


# --- SiteProtocol --------------------------------------------------------------

def test_site_build_submit_is_multipart_with_raw_bytes():
    submit = SiteProtocol().build_submit("doc.txt", b"hello")
    assert submit.path == "/api/anonymize"
    assert "multipart/form-data" in submit.headers["Content-Type"]
    assert b"hello" in submit.body


def test_site_parse_submit_response_reads_job_id_camel_case():
    job_id = SiteProtocol().parse_submit_response(json.dumps({"jobId": "abc123", "done": False}).encode())
    assert job_id == "abc123"


def test_site_poll_path_and_cancel_path_use_query_string():
    proto = SiteProtocol()
    assert proto.poll_path("abc123") == "/api/anonymize?jobId=abc123"
    assert proto.cancel_path("abc123") == "/api/anonymize?jobId=abc123"


def test_site_parse_poll_still_running():
    outcome = SiteProtocol().parse_poll(200, json.dumps({"done": False}).encode())
    assert outcome.kind == "running"


def test_site_parse_poll_done_spreads_result_fields_at_top_level():
    body = json.dumps({
        "done": True,
        "elapsed_seconds": 12.3,
        "warnings": [{"kind": "test_warning"}],
        "anonymized_text": "***",
    }).encode()
    outcome = SiteProtocol().parse_poll(200, body)
    assert outcome.kind == "done"
    # "done" itself must NOT leak into the result dict; everything else must.
    assert "done" not in outcome.result
    assert outcome.result["elapsed_seconds"] == 12.3
    assert outcome.result["warnings"] == [{"kind": "test_warning"}]
    assert outcome.result["anonymized_text"] == "***"


def test_site_parse_poll_cancelled():
    body = json.dumps({"done": False, "cancelled": True}).encode()
    outcome = SiteProtocol().parse_poll(200, body)
    assert outcome.kind == "cancelled"


def test_site_parse_poll_http_500_is_error():
    body = json.dumps({"error": "pipeline exploded"}).encode()
    outcome = SiteProtocol().parse_poll(500, body)
    assert outcome.kind == "error"
    assert outcome.http_status == 500
    assert outcome.error == "pipeline exploded"


def test_site_parse_poll_http_404_is_error_not_retry():
    body = json.dumps({"error": "job forgotten"}).encode()
    outcome = SiteProtocol().parse_poll(404, body)
    assert outcome.kind == "error"
    assert outcome.http_status == 404
    assert outcome.error == "job forgotten"


def test_site_parse_poll_http_404_without_body_still_classified_as_error():
    outcome = SiteProtocol().parse_poll(404, b"not json at all")
    assert outcome.kind == "error"
    assert outcome.http_status == 404
    assert outcome.error  # falls back to a generic "forgotten" message


def test_site_parse_poll_other_non_200_is_error_with_http_status():
    outcome = SiteProtocol().parse_poll(502, b"Bad Gateway")
    assert outcome.kind == "error"
    assert outcome.http_status == 502


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
