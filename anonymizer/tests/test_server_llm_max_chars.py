"""Runtime-configurable LLM chunk size (LLMConfig.max_chars) on server.py.

Exercises anonymizer.server.main()'s argparse/env wiring end-to-end (rather
than re-implementing the "env then flag" precedence logic here), while
avoiding any real network I/O: --ner none skips loading GLiNER/torch, the LLM
base URL points at an unused local port so the mandatory warm-up call fails
fast with "connection refused" (caught internally by main()), and
ThreadingHTTPServer is stubbed so main() returns instead of serving forever.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import server  # noqa: E402

# Definitely-unused local port: guarantees the mandatory warm-up call inside
# main() fails fast with "connection refused" instead of accidentally hitting
# a real local Ollama/LM Studio instance a developer happens to have running
# on the usual ports (11433/11434/1234).
_DEAD_LLM_URL = "http://127.0.0.1:59237/v1"

_ENV_VAR = "ANONYMIZER_LLM_MAX_CHARS"


class _FakeHTTPServer:
    """Stands in for ThreadingHTTPServer so main() returns instead of
    blocking on serve_forever()."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def serve_forever(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Stub the HTTP server and clear the env var before/after each test."""
    monkeypatch.setattr(server, "ThreadingHTTPServer", _FakeHTTPServer)
    monkeypatch.delenv(_ENV_VAR, raising=False)
    yield


def _run_main(argv_extra: list[str]) -> None:
    argv = [
        "server.py",
        "--ner", "none",
        "--no-review", "--no-second-pass", "--no-recall", "--no-corporate",
        "--llm-base-url", _DEAD_LLM_URL,
        "--host", "127.0.0.1", "--port", "0",
        # Иначе main() отказывается стартовать без ключа доступа — см.
        # server._configure_auth. Эти тесты не проверяют аутентификацию, им
        # достаточно локального обхода.
        "--allow-anonymous",
    ] + argv_extra
    old_argv = sys.argv
    sys.argv = argv
    try:
        server.main()
    finally:
        sys.argv = old_argv


def test_default_is_3000_when_unset(monkeypatch):
    _run_main([])
    assert server._DETECTORS["llm"][0].config.max_chars == 3000
    assert server._INFO["llm_max_chars"] == 3000


def test_env_var_sets_value(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "6000")
    _run_main([])
    assert server._DETECTORS["llm"][0].config.max_chars == 6000
    assert server._INFO["llm_max_chars"] == 6000


def test_flag_overrides_env_var(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "6000")
    _run_main(["--llm-max-chars", "4500"])
    assert server._DETECTORS["llm"][0].config.max_chars == 4500
    assert server._INFO["llm_max_chars"] == 4500


def test_every_composed_llm_detector_carries_configured_value(monkeypatch):
    """A second detector silently keeping the old default is exactly the bug
    this must rule out — so assert on the composed Anonymizer's detectors,
    not just on the parsed args or the shared base config."""
    _run_main(["--llm-max-chars", "5000"])

    stages_off = {name: False for name in server._STAGE_NAMES}

    # Base detection detector (subject stage off).
    anon_base = server._compose({**stages_off, "llm": True, "subject": False})
    llm_dets = [d for d in anon_base._detectors if hasattr(d, "config")]
    assert llm_dets, "expected an LLM detector among the composed detectors"
    for det in llm_dets:
        assert det.config.max_chars == 5000

    # Contract-subject detector, built separately via dataclasses.replace
    # around server.py's subject-stage branch.
    anon_subject = server._compose({**stages_off, "llm": True, "subject": True})
    subject_dets = [d for d in anon_subject._detectors if hasattr(d, "config")]
    assert subject_dets, "expected a subject LLM detector among the composed detectors"
    for det in subject_dets:
        assert det.config.max_chars == 5000
        assert "SUBJECT" in det.config.allowed_labels
