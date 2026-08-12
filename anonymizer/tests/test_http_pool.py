"""Tests for anonymizer/http_pool.py: connection reuse, stale-connection
retry, and the process-wide in-flight cap — all against a local
``http.server.ThreadingHTTPServer`` started in this test process (per the
task spec's constraint against calling the live upstream endpoint).

Also covers the two places the rest of the pipeline leans on for its
contract with ``http_pool``: ``RemoteGLiNERDetector._extract``'s exception
mapping (HTTP status -> ``RuntimeError`` vs. unreachable host -> a
``RuntimeError`` with the "недоступен" wording — see ``gliner_remote.py``)
and ``usage_log`` instrumentation surviving a real (non-monkeypatched) round
trip through the pool.
"""

from __future__ import annotations

import concurrent.futures
import http.server
import json
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool, usage_log  # noqa: E402
from anonymizer.gliner_remote import RemoteGLiNERConfig, RemoteGLiNERDetector  # noqa: E402


@contextmanager
def _run_server(handler_cls):
    """Start ``handler_cls`` on an OS-assigned localhost port in a
    background thread; yields ``(host, port)``. Shuts the server down (and
    joins its thread) on exit."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _free_port_with_nothing_listening() -> tuple[str, int]:
    """Reserve a port, then release it WITHOUT ever listening on it —
    connecting there fails fast with "connection refused", simulating an
    unreachable host without touching the network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()
    return host, port


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


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- 1. Connection reuse -----------------------------------------------------

class _CountingHandler(http.server.BaseHTTPRequestHandler):
    """Counts ACCEPTED CONNECTIONS: ``setup()`` runs once per TCP connection,
    even across several keep-alive requests sent over it — unlike
    ``do_POST``, which runs once per request."""

    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    connections = 0
    requests = 0

    def setup(self):
        super().setup()
        with type(self).lock:
            type(self).connections += 1

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        with type(self).lock:
            type(self).requests += 1
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — stdlib signature
        pass  # keep test output quiet


def test_sequential_requests_from_one_thread_reuse_one_connection():
    _CountingHandler.connections = 0
    _CountingHandler.requests = 0
    with _run_server(_CountingHandler) as (host, port):
        url = f"http://{host}:{port}/extract"
        headers = {"Content-Type": "application/json"}
        for _ in range(5):
            status, body = http_pool.post_json(url, b"{}", headers, 5.0)
            assert status == 200

    assert _CountingHandler.requests == 5
    assert _CountingHandler.connections == 1  # all 5 requests reused ONE TCP connection


# --- 2. Stale-connection retry -----------------------------------------------

class _CloseAfterFirstHandler(http.server.BaseHTTPRequestHandler):
    """Serves the first request normally, then actively closes the TCP
    connection right after replying — simulating a server that dropped an
    idle pooled connection. The second logical request must still succeed,
    transparently, via ``post_json``'s one-shot retry."""

    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    connections = 0
    requests = 0

    def setup(self):
        super().setup()
        with type(self).lock:
            type(self).connections += 1

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        with type(self).lock:
            type(self).requests += 1
            is_first = type(self).requests == 1
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if is_first:
            self.close_connection = True  # force-close right after replying

    def log_message(self, format, *args):
        pass


def test_stale_pooled_connection_is_retried_transparently():
    _CloseAfterFirstHandler.connections = 0
    _CloseAfterFirstHandler.requests = 0
    with _run_server(_CloseAfterFirstHandler) as (host, port):
        url = f"http://{host}:{port}/extract"
        headers = {"Content-Type": "application/json"}

        status1, _ = http_pool.post_json(url, b"{}", headers, 5.0)
        assert status1 == 200

        # Give the server a moment to actually close its end before we try
        # to reuse the (now-stale) pooled connection for the second call.
        time.sleep(0.1)

        status2, _ = http_pool.post_json(url, b"{}", headers, 5.0)  # must NOT raise
        assert status2 == 200

    # The second call needed exactly one retry: one extra accepted
    # connection (the reconnect), and the server actually processed both
    # logical requests (the failed write, if any, never reached do_POST).
    assert _CloseAfterFirstHandler.requests == 2
    assert _CloseAfterFirstHandler.connections == 2


# --- 3. Global in-flight cap --------------------------------------------------

class _ConcurrencyTrackingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    current = 0
    peak = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        with type(self).lock:
            type(self).current += 1
            type(self).peak = max(type(self).peak, type(self).current)
        time.sleep(0.05)  # widen the window so overlapping calls actually overlap
        with type(self).lock:
            type(self).current -= 1
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextmanager
def _patched_max_inflight(n: int):
    """Same module-attribute-patch convention as usage_log's tests (see
    test_usage_log.py's ``_patched_calls_mode``): swap the live CHAT-pool
    semaphore for a smaller one, restore on exit."""
    orig_max, orig_sem = http_pool.MAX_INFLIGHT, http_pool._INFLIGHT
    http_pool.MAX_INFLIGHT = n
    http_pool._INFLIGHT = threading.BoundedSemaphore(n)
    try:
        yield
    finally:
        http_pool.MAX_INFLIGHT = orig_max
        http_pool._INFLIGHT = orig_sem


@contextmanager
def _patched_max_inflight_gliner(n: int):
    """Same as ``_patched_max_inflight`` above, but for the independent
    GLiNER pool (``MAX_INFLIGHT_GLINER``/``_INFLIGHT_GLINER`` — see
    http_pool.py's module docstring for why there are two)."""
    orig_max, orig_sem = http_pool.MAX_INFLIGHT_GLINER, http_pool._INFLIGHT_GLINER
    http_pool.MAX_INFLIGHT_GLINER = n
    http_pool._INFLIGHT_GLINER = threading.BoundedSemaphore(n)
    try:
        yield
    finally:
        http_pool.MAX_INFLIGHT_GLINER = orig_max
        http_pool._INFLIGHT_GLINER = orig_sem


def test_chat_pool_cap_limits_concurrent_inflight_requests():
    _ConcurrencyTrackingHandler.current = 0
    _ConcurrencyTrackingHandler.peak = 0
    with _run_server(_ConcurrencyTrackingHandler) as (host, port), _patched_max_inflight(4):
        url = f"http://{host}:{port}/extract"
        headers = {"Content-Type": "application/json"}

        def _call(_i):
            return http_pool.post_json(url, b"{}", headers, 5.0, pool="chat")

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(_call, i) for i in range(20)]
            for f in futures:
                status, _ = f.result()
                assert status == 200

    assert _ConcurrencyTrackingHandler.peak <= 4


# --- 3b. The two pools are independent: a saturated chat pool must not delay
# GLiNER-pool calls ------------------------------------------------------

class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the chat endpoint: every call takes ~0.4 s (vs. the real
    ~12 s chat calls / ~70 ms GLiNER calls this test is modelling)."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(0.4)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class _FastHandler(http.server.BaseHTTPRequestHandler):
    """Stands in for the GLiNER endpoint: responds immediately."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def test_gliner_pool_does_not_queue_behind_saturated_chat_pool():
    """The whole point of splitting the cap in two (see http_pool.py's module
    docstring): a chat pool saturated with slow calls must not delay
    unrelated GLiNER-pool calls, even though both count against process-wide
    caps. Uses two separate local servers (slow "chat", fast "gliner") so the
    two profiles genuinely have nothing in common but the process — proving
    ``pool`` isn't merely nominal."""
    with _run_server(_SlowHandler) as (chat_host, chat_port), \
            _run_server(_FastHandler) as (gliner_host, gliner_port), \
            _patched_max_inflight(2), _patched_max_inflight_gliner(4):
        chat_url = f"http://{chat_host}:{chat_port}/chat/completions"
        gliner_url = f"http://{gliner_host}:{gliner_port}/extract"
        headers = {"Content-Type": "application/json"}

        # Saturate the (size-2) chat pool with 4 slow calls: 2 run at once,
        # the other 2 queue behind them — either way the chat pool stays
        # continuously busy for ~0.8 s, well past the GLiNER calls below.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as chat_workers:
            chat_futures = [
                chat_workers.submit(
                    http_pool.post_json, chat_url, b"{}", headers, 5.0, pool="chat"
                )
                for _ in range(4)
            ]
            time.sleep(0.1)  # let the chat calls actually acquire their slots first

            t0 = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as gliner_workers:
                gliner_futures = [
                    gliner_workers.submit(
                        http_pool.post_json, gliner_url, b"{}", headers, 5.0, pool="gliner"
                    )
                    for _ in range(4)
                ]
                for f in gliner_futures:
                    status, _ = f.result()
                    assert status == 200
            elapsed = time.time() - t0

            for f in chat_futures:
                status, _ = f.result()
                assert status == 200

    # The fast, GLiNER-pool calls must finish quickly regardless of the
    # saturated, slow chat pool — well under the ~0.8 s the chat pool needs
    # to drain 4 calls two at a time. A shared semaphore would have forced
    # these to queue behind the chat calls, pushing elapsed close to 0.8 s+.
    assert elapsed < 0.3


# --- 4. Error mapping (via RemoteGLiNERDetector, the real caller of post_json) --

class _FiveHundredHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b"internal error"
        self.send_response(500)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def test_extract_maps_http_500_to_runtime_error_with_status():
    with _run_server(_FiveHundredHandler) as (host, port):
        cfg = RemoteGLiNERConfig(base_url=f"http://{host}:{port}", api_key="x")
        det = RemoteGLiNERDetector(cfg)
        try:
            det._extract("some chunk")
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "500" in str(exc)


def test_extract_maps_unreachable_host_to_nedostupen_runtime_error():
    host, port = _free_port_with_nothing_listening()
    cfg = RemoteGLiNERConfig(base_url=f"http://{host}:{port}", api_key="x")
    det = RemoteGLiNERDetector(cfg)
    try:
        det._extract("some chunk")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "недоступен" in str(exc)


# --- 5. Instrumentation survives a real (non-monkeypatched) round trip -------

class _EntitiesHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        payload = {
            "entities": [
                {"text": "Иван", "label": "person", "start": 0, "end": 4, "score": 0.9}
            ]
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def test_successful_call_reaches_usage_log_accumulator_over_real_pool():
    with _run_server(_EntitiesHandler) as (host, port), _temp_usage_log() as log_path:
        cfg = RemoteGLiNERConfig(base_url=f"http://{host}:{port}", api_key="x")
        det = RemoteGLiNERDetector(cfg)
        with usage_log.request_context(chars=10) as totals:
            det._extract("Иван пошёл домой")
        lines = _read_jsonl(log_path)

    totals_line = next(l for l in lines if l["kind"] == "request_total")
    assert totals_line["calls"] == {"gliner": 1}
    assert totals.calls == {"gliner": 1}


def test_failed_call_over_real_pool_is_recorded_ok_false():
    host, port = _free_port_with_nothing_listening()
    with _temp_usage_log() as log_path:
        cfg = RemoteGLiNERConfig(base_url=f"http://{host}:{port}", api_key="x")
        det = RemoteGLiNERDetector(cfg)
        try:
            det._extract("some chunk")
        except RuntimeError:
            pass
        lines = _read_jsonl(log_path)

    calls = [l for l in lines if l["kind"] == "gliner"]
    assert len(calls) == 1
    assert calls[0]["ok"] is False


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
