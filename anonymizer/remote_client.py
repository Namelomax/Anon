"""Thin client for the remote anonymizer backend (`server.py`).

Lets local tools (the Streamlit UI, the benchmark) run the heavy pipeline on the
GPU host. ``RemoteAnonymizer`` mirrors the local ``Anonymizer.anonymize`` API
(returns an object with ``.anonymized_text``, ``.mapping``, ``.summary`` and
``.spans``) so it is a drop-in replacement.
"""

from __future__ import annotations

import json
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


def anonymize_remote(text: str, base_url: str, api_key: str = "", timeout: float = 300.0) -> dict:
    """POST text to the backend's /anonymize and return the parsed JSON."""
    url = base_url.rstrip("/") + "/anonymize"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(
        url, data=json.dumps({"text": text}).encode("utf-8"), headers=headers
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class RemoteAnonymizer:
    """Drop-in replacement for ``Anonymizer`` that calls the remote backend."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 300.0) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def anonymize(self, text: str) -> RemoteResult:
        d = anonymize_remote(text, self.base_url, self.api_key, self.timeout)
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
