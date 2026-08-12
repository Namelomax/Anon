"""Pooled, keep-alive HTTP client for upstream calls (LLM chat endpoint,
remote GLiNER).

Why this exists (measured on the live endpoints, see the deploy notes and
``RemoteGLiNERConfig``'s docstring): a GLiNER call costs ~990 ms on a fresh
connection and ~68 ms on a warm, pooled one — almost all of the difference is
the TLS handshake, not the service itself. Worse, the upstream gateway
tolerates many *simultaneous* requests fine but falls over when the rate of
*new* connections spikes: a handful of sequential requests that each open a
fresh connection can knock it out for minutes. Reusing connections removes
that failure mode entirely; it isn't just a speedup.

Design, standard library only:

* One ``http.client.HTTPConnection``/``HTTPSConnection`` per
  ``(scheme, host, port)`` is kept in a ``threading.local()`` and reused
  across calls FROM THE SAME THREAD — this sidesteps ``http.client``'s lack
  of thread safety without needing a lock per connection.
* A pooled connection that has sat idle can be closed by the server between
  calls; the next request on it then raises (``RemoteDisconnected`` and
  friends — see ``_RETRYABLE``). ``post_json`` treats that as transient: it
  closes the dead socket, opens a fresh connection to the same
  ``(scheme, host, port)`` and retries the SAME request exactly once. Only if
  the retry also fails does the caller see an exception. This is not
  hypothetical: a bare persistent connection reproducibly died with
  ``ConnectionAbortedError`` after a 60 s idle pause.
* TWO independent module-level ``threading.BoundedSemaphore``s cap how many
  upstream calls may be in flight AT ONCE across the whole process (not per
  document/thread pool) — one per call PROFILE, not one shared cap. This
  split exists because the two profiles this module serves have wildly
  different hold times: a chat-completion call holds its slot for ~12
  SECONDS, a GLiNER ``/extract`` call for ~70 MILLISECONDS. A single shared
  semaphore would let a burst of long chat calls occupy every slot and force
  short GLiNER calls to queue up behind them — turning a ~8 s GLiNER stage
  into minutes whenever it happens to run alongside another document's LLM
  stage. That is exactly the multi-document scenario this cap exists to
  protect (within one document the stages already run sequentially, so nothing
  contends there); sharing one semaphore across both profiles would make
  concurrent use SLOWER, the opposite of the goal. Call sites therefore pick
  a pool EXPLICITLY via ``post_json``'s ``pool`` argument — never guessed from
  the URL, since the GLiNER service and the chat endpoint currently share a
  host and that could change:

  - ``"chat"`` (default) — sized from ``ANONYMIZER_MAX_INFLIGHT`` (default
    24). Several documents processed concurrently each run their own LLM
    thread pool (e.g. 4 documents x 8 workers = 32 simultaneous calls), and
    it's the process-wide total the upstream gateway cares about, not any
    single document's concurrency setting.
  - ``"gliner"`` — sized from ``ANONYMIZER_MAX_INFLIGHT_GLINER`` (default 16,
    matching ``RemoteGLiNERConfig.concurrency``).

  Worst case, both pools are saturated at once: 24 + 16 = 40 simultaneous
  upstream requests. The chat endpoint was measured to hold flat latency up
  to 32 concurrent callers (12.8 s at 1 concurrent vs 14.5 s at 32) — 40 is
  ABOVE that measured range, so treat it as untested headroom, not a
  validated number; only the chat-pool half (up to 32) is backed by a
  measurement.
"""

from __future__ import annotations

import http.client
import os
import threading
from urllib.parse import urlsplit

# Exceptions that mean "this pooled connection is dead", not "the request is
# fundamentally broken" — see module docstring. ``http.client.BadStatusLine``
# also covers its subclass ``RemoteDisconnected`` (raised when the peer
# closes the socket between our request and its response). ``OSError`` covers
# ``ConnectionError`` and friends (a plain ``ConnectionError`` is itself an
# ``OSError`` subclass, listed here for clarity since the spec calls it out
# by name).
_RETRYABLE: tuple[type[BaseException], ...] = (
    http.client.BadStatusLine,
    ConnectionError,
    OSError,
)

DEFAULT_MAX_INFLIGHT = 24
DEFAULT_MAX_INFLIGHT_GLINER = 16


def _int_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _max_inflight_from_env() -> int:
    return _int_from_env("ANONYMIZER_MAX_INFLIGHT", DEFAULT_MAX_INFLIGHT)


def _max_inflight_gliner_from_env() -> int:
    return _int_from_env("ANONYMIZER_MAX_INFLIGHT_GLINER", DEFAULT_MAX_INFLIGHT_GLINER)


# Read once at import (same convention as usage_log.py's module-level
# constants) — tests that need a different cap patch ``MAX_INFLIGHT``/
# ``_INFLIGHT`` (or their ``_GLINER`` counterparts) directly rather than the
# environment variable + reload. Two independent pools — see module
# docstring for why one shared cap doesn't work.
MAX_INFLIGHT: int = _max_inflight_from_env()
_INFLIGHT = threading.BoundedSemaphore(MAX_INFLIGHT)

MAX_INFLIGHT_GLINER: int = _max_inflight_gliner_from_env()
_INFLIGHT_GLINER = threading.BoundedSemaphore(MAX_INFLIGHT_GLINER)

# pool name -> semaphore. Looked up at call time in post_json() so that tests
# patching _INFLIGHT/_INFLIGHT_GLINER (module attributes, see above) are
# picked up rather than a copy captured at import time.
_POOL_NAMES = ("chat", "gliner")

# One connection per (scheme, host, port) PER THREAD.
_local = threading.local()


class PoolConnectionError(OSError):
    """The pooled request could not be completed even after one retry on a
    fresh connection (see ``post_json``). Subclasses ``OSError`` so existing
    call sites that used to catch ``urllib.error.URLError`` (itself commonly
    wrapping a plain ``OSError``/socket error) can catch this the same way,
    with an ``except OSError`` clause."""


def _thread_conns() -> dict:
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    return conns


def _split_url(url: str) -> tuple[str, str, int, str]:
    parts = urlsplit(url)
    scheme = parts.scheme or "http"
    host = parts.hostname
    if not host:
        raise ValueError(f"URL without a host: {url!r}")
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return scheme, host, port, path


def _new_connection(scheme: str, host: str, port: int) -> http.client.HTTPConnection:
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, port)


def _get_connection(scheme: str, host: str, port: int) -> http.client.HTTPConnection:
    conns = _thread_conns()
    key = (scheme, host, port)
    conn = conns.get(key)
    if conn is None:
        conn = _new_connection(scheme, host, port)
        conns[key] = conn
    return conn


def _drop_connection(scheme: str, host: str, port: int) -> None:
    conns = _thread_conns()
    conn = conns.pop((scheme, host, port), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup only
            pass


def _inflight_semaphore(pool: str) -> threading.BoundedSemaphore:
    if pool == "chat":
        return _INFLIGHT
    if pool == "gliner":
        return _INFLIGHT_GLINER
    raise ValueError(f"unknown pool {pool!r}; expected one of {_POOL_NAMES}")


def post_json(
    url: str,
    payload_bytes: bytes,
    headers: dict,
    timeout: float,
    *,
    pool: str = "chat",
) -> tuple[int, bytes]:
    """POST ``payload_bytes`` to ``url`` over a pooled, keep-alive connection.

    ``pool`` selects which of the two independent in-flight caps this call
    counts against — ``"chat"`` (default, LLM chat-completion calls: llm.py,
    review.py) or ``"gliner"`` (remote GLiNER ``/extract`` calls:
    gliner_remote.py). This is an explicit choice at the call site, not
    inferred from the URL — see module docstring for why the two must not
    share a cap.

    Returns ``(status_code, body_bytes)`` for ANY HTTP response, including
    non-2xx statuses (mirroring ``http.client``'s own behaviour, unlike
    ``urllib.request.urlopen`` which raises ``HTTPError`` on those) — the
    caller decides whether the status is acceptable.

    Raises ``PoolConnectionError`` if the request could not be completed at
    all (connection refused/reset, dead pooled socket, etc.), even after one
    retry on a fresh connection — see module docstring. The retry is
    transparent to the caller: this function is called at most twice
    internally per invocation, so callers that log/count "one call" (e.g.
    ``usage_log.record_call``) around this function never double-count it.

    Blocks on the selected pool's semaphore for the duration of the call
    (including any retry) — see module docstring.
    """
    scheme, host, port, path = _split_url(url)
    semaphore = _inflight_semaphore(pool)

    with semaphore:
        last_exc: BaseException | None = None
        for attempt in range(2):
            conn = _get_connection(scheme, host, port)
            try:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request("POST", path, body=payload_bytes, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                return resp.status, body
            except _RETRYABLE as exc:
                _drop_connection(scheme, host, port)
                last_exc = exc
                continue

        raise PoolConnectionError(
            f"request to {url} failed after retry on a fresh connection: {last_exc}"
        ) from last_exc
