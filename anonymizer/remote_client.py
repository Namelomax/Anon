"""Thin client for the remote anonymizer backend (`server.py`).

Lets local tools (the Streamlit UI, the benchmark) run the heavy pipeline on the
GPU host. ``RemoteAnonymizer`` mirrors the local ``Anonymizer.anonymize`` API
(returns an object with ``.anonymized_text``, ``.mapping``, ``.summary`` and
``.spans``) so it is a drop-in replacement.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .spans import Span


@dataclass(frozen=True)
class RemoteResult:
    text: str
    anonymized_text: str
    mapping: dict
    summary: dict
    spans: tuple


def anonymize_remote(
    text: str,
    base_url: str,
    api_key: str = "",
    timeout: float = 300.0,
    retries: int = 3,
    stages: dict | None = None,
) -> dict:
    """POST text to the backend's /anonymize and return the parsed JSON.

    ``stages`` optionally toggles pipeline stages per request, e.g.
    ``{"regex": False, "ner": True, "llm": False}`` for GLiNER-only. Omitted
    stages use the server's defaults.

    Retries transient network/proxy failures (e.g. HTTP 599 from a dropped proxy
    connection during long batch runs) with a short backoff.
    """
    url = base_url.rstrip("/") + "/anonymize"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = {"text": text, **(stages or {})}
    body = json.dumps(payload).encode("utf-8")
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            # The server reports the real cause in the JSON body; surface it.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            last_exc = RuntimeError(f"Сервер вернул HTTP {exc.code}: {detail or exc.reason}")
            if exc.code < 500:  # client error won't fix itself on retry
                raise last_exc
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise last_exc  # exhausted retries


class RemoteAnonymizer:
    """Drop-in replacement for ``Anonymizer`` that calls the remote backend."""

    def __init__(
        self, base_url: str, api_key: str = "", timeout: float = 300.0,
        stages: dict | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.stages = stages
        self.failures = 0

    def anonymize(self, text: str) -> RemoteResult:
        try:
            d = anonymize_remote(
                text, self.base_url, self.api_key, self.timeout, stages=self.stages
            )
        except Exception as exc:  # don't let one bad row kill a long batch
            self.failures += 1
            print(f"[remote] строка пропущена ({exc})", file=sys.stderr)
            return RemoteResult(text, text, {}, {}, ())
        spans = tuple(
            Span(s["start"], s["end"], s["label"], s.get("text", ""), source="remote")
            for s in d.get("spans", [])
        )
        return RemoteResult(
            text=text,
            anonymized_text=d["anonymized_text"],
            mapping=d.get("mapping", {}),
            summary=d.get("summary", {}),
            spans=spans,
        )
