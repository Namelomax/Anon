"""Привязка фоновых задач к владельцу: GET/DELETE /jobs/<id> принимают личность
вызывающего query-параметрами ``account_id``/``user_id`` (их подставляет только
доверенный Next.js под ключом бэкенда). Чужая задача неотличима от
несуществующей — тот же 404 с тем же телом. Модели не грузятся."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import server  # noqa: E402


class _Headers:
    def __init__(self, items: dict | None = None) -> None:
        self._items = {k.lower(): v for k, v in (items or {}).items()}

    def get(self, key: str, default=None):
        return self._items.get(key.lower(), default)


def _invoke(method: str, path: str, body: bytes = b""):
    h = server.Handler.__new__(server.Handler)
    h.command = method
    h.path = path
    h.request_version = "HTTP/1.1"
    h.protocol_version = "HTTP/1.1"
    h.requestline = f"{method} {path} {h.request_version}"
    h.client_address = ("127.0.0.1", 55555)
    h.headers = _Headers({"Authorization": "Bearer secret1", "Content-Length": str(len(body))})
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.close_connection = True
    getattr(h, f"do_{method}")()
    raw = h.wfile.getvalue()
    head, _, resp_body = raw.partition(b"\r\n\r\n")
    code = int(head.split(b"\r\n", 1)[0].split()[1])
    return code, resp_body


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    monkeypatch.setattr(server, "_API_KEYS", {"secret1": "web"})
    monkeypatch.setattr(server, "_ALLOW_ANONYMOUS", False)
    monkeypatch.setattr(server, "_JOB_ONESHOT", False)
    server._JOBS.clear()
    yield
    server._JOBS.clear()


def _put_job(job_id: str, owner, status: str = "running", result=None) -> None:
    server._JOBS[job_id] = {
        "status": status, "result": result, "error": None, "created": time.time(),
        "cancel": threading.Event(), "owner": owner,
    }


def _unknown_response():
    return _invoke("GET", "/jobs/does-not-exist?account_id=1&user_id=7")


def test_owner_can_poll():
    _put_job("j1", (1, 7), status="done", result={"x": 1})
    code, raw = _invoke("GET", "/jobs/j1?account_id=1&user_id=7")
    assert code == 200
    assert json.loads(raw)["result"] == {"x": 1}


def test_other_user_gets_404_identical_to_unknown_job():
    _put_job("j1", (1, 7), status="done", result={"secret": "mapping"})
    unknown = _unknown_response()
    for qs in ("?account_id=1&user_id=8", "?account_id=2&user_id=8", "", "?user_id=abc"):
        code, raw = _invoke("GET", "/jobs/j1" + qs)
        assert (code, raw) == unknown
        assert b"secret" not in raw
    assert unknown[0] == 404


def test_job_without_owner_behaves_as_before():
    _put_job("j2", None, status="done", result={"x": 2})
    for qs in ("", "?account_id=1&user_id=7"):
        code, raw = _invoke("GET", "/jobs/j2" + qs)
        assert code == 200
        assert json.loads(raw)["result"] == {"x": 2}


def test_cancel_follows_same_rule():
    _put_job("j3", (1, 7))
    unknown = _invoke("DELETE", "/jobs/does-not-exist?account_id=1&user_id=7")
    assert unknown[0] == 404
    # чужой: тот же 404 и задача НЕ отменена
    assert _invoke("DELETE", "/jobs/j3?account_id=1&user_id=8") == unknown
    assert _invoke("DELETE", "/jobs/j3") == unknown
    assert server._JOBS["j3"]["status"] == "running"
    assert not server._JOBS["j3"]["cancel"].is_set()
    # владелец отменяет
    code, raw = _invoke("DELETE", "/jobs/j3?account_id=1&user_id=7")
    assert code == 200 and json.loads(raw) == {"status": "cancelled"}
    assert server._JOBS["j3"]["cancel"].is_set()


def test_cancel_job_without_owner_as_before():
    _put_job("j4", None)
    code, raw = _invoke("DELETE", "/jobs/j4")
    assert code == 200 and json.loads(raw) == {"status": "cancelled"}


def test_submit_records_owner(monkeypatch):
    monkeypatch.setattr(server, "_run_anonymize_text", lambda data, cancel=None: {"ok": True})
    body = json.dumps({"text": "x", "account_id": 1, "user_id": "7"}).encode()
    code, raw = _invoke("POST", "/jobs/anonymize", body)
    assert code == 202
    job_id = json.loads(raw)["job_id"]
    assert server._JOBS[job_id]["owner"] == (1, 7)
    assert _invoke("GET", f"/jobs/{job_id}?account_id=1&user_id=8") == _unknown_response()

    code, raw = _invoke("POST", "/jobs/anonymize", json.dumps({"text": "x"}).encode())
    assert code == 202
    assert server._JOBS[json.loads(raw)["job_id"]]["owner"] is None


# --- Heartbeat: задача владельца, которую не опрашивают, отменяется ---------

def _hb_job(job_id: str, owner, age: float) -> dict:
    """Задача со своим _JobCancelEvent; last_seen = «age секунд назад»."""
    ev = server._JobCancelEvent()
    job = {
        "status": "running", "result": None, "error": None, "created": time.time(),
        "cancel": ev, "owner": owner, "last_seen": time.monotonic() - age,
    }
    ev.job = job
    server._JOBS[job_id] = job
    return job


def test_heartbeat_stale_owner_job_is_cancelled(monkeypatch):
    monkeypatch.setattr(server, "_JOB_HEARTBEAT_SECONDS", 1.0)
    job = _hb_job("h1", (1, 7), age=5)
    # детекторы спрашивают is_set() между чанками — это и есть проверка
    assert job["cancel"].is_set() is True
    assert job["cancel"].is_set() is True  # и дальше остаётся поставленным


def test_heartbeat_fresh_job_not_cancelled(monkeypatch):
    monkeypatch.setattr(server, "_JOB_HEARTBEAT_SECONDS", 100.0)
    job = _hb_job("h2", (1, 7), age=1)
    assert job["cancel"].is_set() is False


def test_owner_poll_keeps_job_alive(monkeypatch):
    monkeypatch.setattr(server, "_JOB_HEARTBEAT_SECONDS", 10.0)
    job = _hb_job("h3", (1, 7), age=9)
    code, _ = _invoke("GET", "/jobs/h3?account_id=1&user_id=7")
    assert code == 200
    assert time.monotonic() - job["last_seen"] < 1
    # без опроса прошло бы ещё 2 с -> протухло бы; после опроса свежо
    job["last_seen"] -= 2
    assert job["cancel"].is_set() is False


def test_foreign_poll_does_not_refresh_last_seen(monkeypatch):
    monkeypatch.setattr(server, "_JOB_HEARTBEAT_SECONDS", 10.0)
    job = _hb_job("h4", (1, 7), age=9)
    before = job["last_seen"]
    assert _invoke("GET", "/jobs/h4?account_id=1&user_id=8")[0] == 404
    assert _invoke("GET", "/jobs/h4")[0] == 404
    assert job["last_seen"] == before
    job["last_seen"] -= 2
    assert job["cancel"].is_set() is True


def test_ownerless_job_never_heartbeat_cancelled(monkeypatch):
    monkeypatch.setattr(server, "_JOB_HEARTBEAT_SECONDS", 1.0)
    job = _hb_job("h5", None, age=10_000)
    assert job["cancel"].is_set() is False


def test_submit_sets_heartbeat_event_and_last_seen(monkeypatch):
    monkeypatch.setattr(server, "_run_anonymize_text", lambda data, cancel=None: {"ok": True})
    body = json.dumps({"text": "x", "account_id": 1, "user_id": 7}).encode()
    code, raw = _invoke("POST", "/jobs/anonymize", body)
    job = server._JOBS[json.loads(raw)["job_id"]]
    assert isinstance(job["cancel"], server._JobCancelEvent)
    assert time.monotonic() - job["last_seen"] < 5
