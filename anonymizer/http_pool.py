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

* Connections are kept in a PROCESS-WIDE pool (``_ConnectionPool`` below),
  one bounded free-list of IDLE connections per ``(scheme, host, port)``,
  guarded by a lock — NOT a ``threading.local()``. A caller CHECKS OUT a
  connection for the duration of exactly one request and CHECKS IT IN (or,
  if it turned out to be dead, DISCARDS it) immediately after. This explicit
  ownership handoff is what guarantees a connection is never used by two
  threads at once (``http.client`` is not thread-safe) — the checkout is
  removed from the free-list under the lock, so only the thread that
  checked it out can ever touch it, and it can't be handed out again until
  it's checked back in. Unlike the old per-thread design, connections now
  survive across THREADS as well as calls: the detection layers spawn a
  fresh ``ThreadPoolExecutor`` per ``find()`` (16 GLiNER workers, 8-24 LLM
  workers), so a per-thread pool meant a fresh connection — and a fresh DNS
  lookup — for every stage of every document. A process-wide pool means the
  resolver is hit once per host per process lifetime in the steady state,
  not dozens of times per document. This is not hypothetical: a burst of
  those simultaneous lookups (several documents at once, each spawning its
  own worker pools) has reproducibly exhausted the server's DNS resolver in
  production — see ``_DNS_RETRYABLE`` below.
* The free-list for each ``(scheme, host, port)`` is bounded so a burst
  can't leave hundreds of idle sockets parked forever. It's sized from the
  two in-flight semaphores below (``MAX_INFLIGHT + MAX_INFLIGHT_GLINER``):
  that sum is already the most connections that could ever be open at once
  across both pools (see their docstring), so it can never actually turn
  away a connection a burst legitimately needs to keep idle.
* A pooled connection that has sat idle can be closed by the server between
  calls; the next request on it then raises (``RemoteDisconnected`` and
  friends — see ``_RETRYABLE``). ``post_json`` treats that as transient: it
  discards the dead socket (never returns it to the free-list), checks out
  a fresh connection to the same ``(scheme, host, port)`` and retries the
  SAME request exactly once. Only if the retry also fails does the caller
  see an exception. This is not hypothetical: a bare persistent connection
  reproducibly died with ``ConnectionAbortedError`` after a 60 s idle pause.
* DNS resolution failures (``socket.gaierror``, e.g. ``EAI_AGAIN``) get
  their OWN retry class, separate from the dead-connection retry above —
  see ``_DNS_RETRYABLE`` for why: retrying instantly into an overloaded
  resolver just fails again, so this waits a short backoff first and allows
  a couple of attempts.
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
import socket
import threading
import time
from urllib.parse import urlsplit

# Exceptions that mean "this pooled connection is dead", not "the request is
# fundamentally broken" — see module docstring. ``http.client.BadStatusLine``
# also covers its subclass ``RemoteDisconnected`` (raised when the peer
# closes the socket between our request and its response). ``OSError`` covers
# ``ConnectionError`` and friends (a plain ``ConnectionError`` is itself an
# ``OSError`` subclass, listed here for clarity since the spec calls it out
# by name). NOTE: ``socket.gaierror`` is ALSO an ``OSError``, so it would
# match here too — ``post_json`` checks ``_DNS_RETRYABLE`` first (see below)
# so DNS failures take the separate, backed-off retry path instead of this
# immediate-reopen one.
_RETRYABLE: tuple[type[BaseException], ...] = (
    http.client.BadStatusLine,
    ConnectionError,
    OSError,
)

# DNS-resolution failures get their own retry class, handled BEFORE (in
# post_json's except order) the broader _RETRYABLE above even though
# socket.gaierror is itself an OSError. Reason (production incident): two
# documents processing concurrently both died with
#
#     [Errno -3] Temporary failure in name resolution
#
# i.e. socket.gaierror(socket.EAI_AGAIN, ...) — the server's resolver
# transiently gave up under a burst of parallel getaddrinfo() calls (up to
# 16 GLiNER + 24 chat workers, times however many documents are processing
# at once, each spawning its own worker pools). The failure has also been
# seen killing a single document's recall pass on its own, so this is
# resolver flakiness that concurrency amplifies, not a purely concurrent
# bug — process-wide pooling (see module docstring) cuts the NUMBER of
# lookups, but a residual one can still transiently fail. _RETRYABLE's
# immediate reopen-and-retry-once walks straight back into the same
# overloaded resolver, which is why the retry ALSO failed in production. A
# short backoff before retrying rides out the hiccup instead of hammering
# the resolver again.
_DNS_RETRYABLE: tuple[type[BaseException], ...] = (socket.gaierror,)

# A couple of attempts, not just one, at a short backoff: enough to ride out
# a momentary resolver hiccup. Keep both small — this is meant to smooth
# over a millisecond-scale blip, not to hang a request for seconds.
_DNS_RETRY_ATTEMPTS = 3  # initial attempt + up to 2 retries
_DNS_RETRY_BACKOFF_SECONDS = 0.15  # pause before each DNS-failure retry

# The pre-existing "dead pooled connection" retry (see module docstring):
# exactly one retry on a freshly checked-out connection, no backoff — a
# socket the peer already closed is expected to be fixed immediately by
# opening a new one, unlike a resolver hiccup.
_MAX_DEAD_CONN_RETRIES = 1

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


class PoolConnectionError(OSError):
    """The pooled request could not be completed even after retrying (dead
    pooled connection: once; DNS resolution failure: up to
    ``_DNS_RETRY_ATTEMPTS`` times — see ``post_json``). Subclasses
    ``OSError`` so existing call sites that used to catch
    ``urllib.error.URLError`` (itself commonly wrapping a plain
    ``OSError``/socket error) can catch this the same way, with an
    ``except OSError`` clause."""


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


def _close_quietly(conn: http.client.HTTPConnection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 — best-effort cleanup only
        pass


def _free_list_limit() -> int:
    # Sized from the two in-flight caps: their sum is already the most
    # connections that could ever be open at once across both pools (see
    # module docstring's "worst case ... 40 simultaneous upstream
    # requests"), so a burst can never need to park more idle sockets than
    # that for a given (scheme, host, port). Read live (not cached) so tests
    # that patch MAX_INFLIGHT/MAX_INFLIGHT_GLINER take effect immediately.
    return MAX_INFLIGHT + MAX_INFLIGHT_GLINER


class _ConnectionPool:
    """Process-wide keep-alive connection pool: a bounded free-list of IDLE
    connections per ``(scheme, host, port)``, guarded by a single lock.

    Ownership model — the whole point of this class:

    * ``checkout`` removes (and returns) a connection from the free-list, or
      opens a fresh one if the list is empty. Removal happens under the
      lock, so a given connection object can be checked out by at most one
      caller at a time — the free-list is the ONLY place a connection can be
      obtained from, so once it's out, it's out.
    * ``checkin`` puts a connection that finished a request successfully
      back on the free-list for reuse (subject to the bound above).
    * ``discard`` closes a connection that raised during use and must NOT be
      reused — it is deliberately never added back to the free-list.

    A caller must check a connection back in (or discard it) before any
    other thread can obtain it again; this is what keeps ``http.client``
    connections (not thread-safe) from ever being touched by two threads at
    once.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}

    def checkout(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        key = (scheme, host, port)
        with self._lock:
            idle = self._idle.get(key)
            if idle:
                return idle.pop()
        return _new_connection(scheme, host, port)

    def checkin(
        self, scheme: str, host: str, port: int, conn: http.client.HTTPConnection
    ) -> None:
        key = (scheme, host, port)
        limit = _free_list_limit()
        close_it = False
        with self._lock:
            idle = self._idle.setdefault(key, [])
            if len(idle) < limit:
                idle.append(conn)
            else:
                close_it = True
        if close_it:
            # Free-list for this key is already at its bound — close outside
            # the lock so a slow close() can't stall other threads.
            _close_quietly(conn)

    def discard(self, conn: http.client.HTTPConnection) -> None:
        """A connection that raised during use is dead — close it, and never
        return it to the free-list."""
        _close_quietly(conn)


# The pool: shared by every thread, every stage, every document in this
# process — see module docstring for why that's the fix, not just a
# convenience.
_pool = _ConnectionPool()


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
    all, even after retrying — see module docstring for the two distinct
    retry classes (dead pooled connection: one retry, no backoff; DNS
    resolution failure: up to ``_DNS_RETRY_ATTEMPTS`` attempts with a short
    backoff between them). Both retries are transparent to the caller: this
    function still represents exactly ONE logical call, so callers that
    log/count "one call" (e.g. ``usage_log.record_call``) around this
    function never double-count it, regardless of how many internal
    attempts it took.

    Blocks on the selected pool's semaphore for the duration of the call
    (including any retries) — see module docstring.
    """
    scheme, host, port, path = _split_url(url)
    semaphore = _inflight_semaphore(pool)

    with semaphore:
        last_exc: BaseException | None = None
        dead_conn_retries = 0
        dns_attempts = 0
        while True:
            conn = _pool.checkout(scheme, host, port)
            try:
                conn.timeout = timeout
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                conn.request("POST", path, body=payload_bytes, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
            except _DNS_RETRYABLE as exc:
                _pool.discard(conn)
                last_exc = exc
                dns_attempts += 1
                if dns_attempts >= _DNS_RETRY_ATTEMPTS:
                    break
                time.sleep(_DNS_RETRY_BACKOFF_SECONDS)
                continue
            except _RETRYABLE as exc:
                _pool.discard(conn)
                last_exc = exc
                dead_conn_retries += 1
                if dead_conn_retries > _MAX_DEAD_CONN_RETRIES:
                    break
                continue
            else:
                _pool.checkin(scheme, host, port, conn)
                return resp.status, body

        raise PoolConnectionError(
            f"request to {url} failed after retry on a fresh connection: {last_exc}"
        ) from last_exc
