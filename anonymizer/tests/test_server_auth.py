"""Проверка аутентификации входящих запросов (server.py: Authorization:
Bearer <секрет>).

Не поднимает реальный сокет — как test_server_review_warnings.py, строит
объект Handler вручную (минуя socket-driven __init__ BaseHTTPRequestHandler)
и вызывает do_GET/do_POST/do_DELETE/do_OPTIONS напрямую, читая ответ из
io.BytesIO, подставленного вместо wfile. Модели/детекторы не грузятся —
server._DETECTORS/_DEFAULTS/_REVIEW_CFG подменяются на пустые (см.
_minimal_pipeline, тот же приём, что в test_server_review_warnings.py).
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import depersonalization_log, server, usage_log  # noqa: E402


# --- Мини-каркас HTTP-обработчика без сокета -------------------------------

class _Headers:
    """Минимальная регистронезависимая замена http.client.HTTPMessage —
    только .get(), которым пользуются Handler/_authenticate."""

    def __init__(self, items: dict | None = None) -> None:
        self._items = {k.lower(): v for k, v in (items or {}).items()}

    def get(self, key: str, default=None):
        return self._items.get(key.lower(), default)


def _invoke(method: str, path: str, headers: dict | None = None, body: bytes = b""):
    """Построить Handler, вызвать do_<METHOD>(), вернуть (код, тело_bytes).

    Instance создаётся через __new__, минуя BaseHTTPRequestHandler.__init__
    (который иначе сразу же попытался бы читать/парсить запрос из реального
    сокета) — атрибуты, которые обычно выставляет парсер запроса, задаются
    вручную.
    """
    h = server.Handler.__new__(server.Handler)
    h.command = method
    h.path = path
    h.request_version = "HTTP/1.1"
    h.protocol_version = "HTTP/1.1"
    h.requestline = f"{method} {path} {h.request_version}"
    h.client_address = ("127.0.0.1", 55555)
    hdrs = dict(headers or {})
    hdrs.setdefault("Content-Length", str(len(body)))
    h.headers = _Headers(hdrs)
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.close_connection = True
    getattr(h, f"do_{method}")()
    raw = h.wfile.getvalue()
    header_part, _, resp_body = raw.partition(b"\r\n\r\n")
    status_line = header_part.split(b"\r\n", 1)[0]
    code = int(status_line.split()[1])
    return code, resp_body


def _invoke_json(method: str, path: str, headers: dict | None = None, body_obj=None):
    body = json.dumps(body_obj).encode("utf-8") if body_obj is not None else b""
    code, raw = _invoke(method, path, headers, body)
    parsed = json.loads(raw) if raw else None
    return code, parsed


# --- Изоляция глобального состояния server.py ------------------------------

@pytest.fixture(autouse=True)
def _isolate_auth_state():
    orig_keys = server._API_KEYS
    orig_allow = server._ALLOW_ANONYMOUS
    server._API_KEYS = {}
    server._ALLOW_ANONYMOUS = False
    try:
        yield
    finally:
        server._API_KEYS = orig_keys
        server._ALLOW_ANONYMOUS = orig_allow


@contextmanager
def _minimal_pipeline():
    """Тот же приём, что _server_state в test_server_review_warnings.py: все
    стадии детекции выключены, review отсутствует — минимальная конфигурация,
    достаточная, чтобы _run_anonymize_text прошёл end-to-end без сети и без
    моделей."""
    orig_detectors = server._DETECTORS
    orig_defaults = server._DEFAULTS
    orig_review_cfg = server._REVIEW_CFG
    orig_ner_backend = server._NER_BACKEND
    orig_needs_lock = server._NEEDS_MODEL_LOCK
    server._DETECTORS = {}
    server._DEFAULTS = {name: False for name in server._STAGE_NAMES}
    server._REVIEW_CFG = None
    server._NER_BACKEND = "none"
    server._NEEDS_MODEL_LOCK = False
    try:
        yield
    finally:
        server._DETECTORS = orig_detectors
        server._DEFAULTS = orig_defaults
        server._REVIEW_CFG = orig_review_cfg
        server._NER_BACKEND = orig_ner_backend
        server._NEEDS_MODEL_LOCK = orig_needs_lock


@contextmanager
def _temp_usage_log():
    orig = usage_log.LOG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        usage_log.LOG_PATH = Path(tmp) / "usage.jsonl"
        try:
            yield
        finally:
            usage_log.LOG_PATH = orig


@contextmanager
def _temp_dep_log():
    orig = depersonalization_log.LOG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "depersonalization.jsonl"
        depersonalization_log.LOG_PATH = path
        try:
            yield path
        finally:
            depersonalization_log.LOG_PATH = orig


def _read_actors(path: Path) -> list:
    """Значения ``actor`` всех записей ``event=="depersonalize"`` в журнале,
    в порядке появления."""
    actors = []
    if not path.exists():
        return actors
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("event") == "depersonalize":
                actors.append(rec.get("actor"))
    return actors


# --- 1. Нет заголовка -> 401, пайплайн не вызывается -----------------------

def test_missing_authorization_returns_401_and_pipeline_not_invoked(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server, "_run_anonymize_text",
        lambda *a, **k: (calls.append((a, k)), {})[1],
    )
    server._API_KEYS = {"secret1": "web"}

    code, body = _invoke_json("POST", "/anonymize", headers=None, body_obj={"text": "hi"})

    assert code == 401
    assert body == {"error": "unauthorized"}
    assert calls == []


# --- 2. Неверный секрет -> 401 ---------------------------------------------

def test_wrong_secret_returns_401():
    server._API_KEYS = {"secret1": "web"}

    code, body = _invoke_json(
        "POST", "/anonymize",
        headers={"Authorization": "Bearer wrong-secret"},
        body_obj={"text": "hi"},
    )

    assert code == 401
    assert body == {"error": "unauthorized"}


# --- 3. Верный секрет -> запрос проходит нормально -------------------------

def test_correct_secret_proceeds_normally():
    server._API_KEYS = {"secret1": "web"}

    with _minimal_pipeline(), _temp_usage_log(), _temp_dep_log():
        code, body = _invoke_json(
            "POST", "/anonymize",
            headers={"Authorization": "Bearer secret1"},
            body_obj={"text": "Hello world"},
        )

    assert code == 200
    assert "anonymized_text" in body


# --- 4. OPTIONS никогда не требует аутентификации --------------------------

def test_options_never_requires_auth():
    server._API_KEYS = {"secret1": "web"}

    code, _body = _invoke("OPTIONS", "/anonymize")

    assert code == 204


# --- 5. GET /health: усечённый ответ без ключа, полный — с ключом ----------

def test_health_minimal_without_key_full_with_key():
    orig_info = server._INFO
    server._INFO = {"ner": "none", "llm_model": "test-model"}
    server._API_KEYS = {"secret1": "web"}
    try:
        code, body = _invoke_json("GET", "/health")
        assert code == 200
        assert body == {"status": "ok"}

        code2, body2 = _invoke_json(
            "GET", "/health", headers={"Authorization": "Bearer secret1"}
        )
        assert code2 == 200
        assert body2 == {"status": "ok", "ner": "none", "llm_model": "test-model"}
    finally:
        server._INFO = orig_info


# --- 6. GET /usage без ключа -> 401 -----------------------------------------

def test_usage_requires_auth(monkeypatch):
    calls = []
    monkeypatch.setattr(
        usage_log, "usage_summary", lambda *a, **k: (calls.append(1), {})[1]
    )
    server._API_KEYS = {"secret1": "web"}

    code, body = _invoke_json("GET", "/usage")
    assert code == 401
    assert body == {"error": "unauthorized"}
    assert calls == []

    code2, _body2 = _invoke_json(
        "GET", "/usage", headers={"Authorization": "Bearer secret1"}
    )
    assert code2 == 200
    assert calls == [1]


# --- 7. Маршруты /jobs/* без ключа -> 401 -----------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/jobs/anonymize"),
        ("POST", "/jobs/anonymize-file"),
        ("GET", "/jobs/deadbeef"),
        ("DELETE", "/jobs/deadbeef"),
    ],
)
def test_job_routes_require_auth(method, path):
    server._API_KEYS = {"secret1": "web"}

    body_obj = {} if method == "POST" else None
    code, body = _invoke_json(method, path, headers=None, body_obj=body_obj)

    assert code == 401
    assert body == {"error": "unauthorized"}


# --- 8. Именованные ключи: разные секреты -> разные principal --------------

def test_named_keys_authenticate_with_correct_actor():
    server._API_KEYS = {"aaa": "web", "bbb": "cli"}

    with _minimal_pipeline(), _temp_usage_log(), _temp_dep_log() as dep_path:
        code1, _ = _invoke_json(
            "POST", "/anonymize",
            headers={"Authorization": "Bearer aaa"},
            body_obj={"text": "Hello"},
        )
        code2, _ = _invoke_json(
            "POST", "/anonymize",
            headers={"Authorization": "Bearer bbb"},
            body_obj={"text": "Hello again"},
        )
        actors = _read_actors(dep_path)

    assert code1 == 200
    assert code2 == 200
    assert actors == ["web", "cli"]


# --- 9. Устаревший одиночный ключ -> principal "default" -------------------

def test_legacy_single_key_authenticates_as_default(monkeypatch):
    monkeypatch.setenv("ANONYMIZER_API_KEY", "legacysecret")
    monkeypatch.delenv("ANONYMIZER_API_KEYS", raising=False)
    server._API_KEYS = server._load_api_keys()
    assert server._API_KEYS == {"legacysecret": "default"}

    with _minimal_pipeline(), _temp_usage_log(), _temp_dep_log() as dep_path:
        code, _ = _invoke_json(
            "POST", "/anonymize",
            headers={"Authorization": "Bearer legacysecret"},
            body_obj={"text": "Hello"},
        )
        actors = _read_actors(dep_path)

    assert code == 200
    assert actors == ["default"]


# --- 10. Отказ стартовать без ключей; --allow-anonymous снимает проверку ---

class _FakeArgs:
    def __init__(self, allow_anonymous: bool) -> None:
        self.allow_anonymous = allow_anonymous


def test_configure_auth_refuses_to_start_without_any_key(monkeypatch):
    monkeypatch.delenv("ANONYMIZER_API_KEY", raising=False)
    monkeypatch.delenv("ANONYMIZER_API_KEYS", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        server._configure_auth(_FakeArgs(allow_anonymous=False))

    assert exc_info.value.code == 1


def test_configure_auth_allow_anonymous_bypasses_check_and_requests_pass(monkeypatch, capsys):
    monkeypatch.delenv("ANONYMIZER_API_KEY", raising=False)
    monkeypatch.delenv("ANONYMIZER_API_KEYS", raising=False)

    server._configure_auth(_FakeArgs(allow_anonymous=True))
    assert server._ALLOW_ANONYMOUS is True

    # Предупреждение обязано попасть в stderr и не содержать секретов (их и
    # не задано в этом сценарии, но сам факт запуска должен быть громким).
    captured = capsys.readouterr()
    assert "allow-anonymous" in captured.err

    with _minimal_pipeline(), _temp_usage_log(), _temp_dep_log():
        code, body = _invoke_json(
            "POST", "/anonymize", headers=None, body_obj={"text": "Hi"}
        )

    assert code == 200
    assert "anonymized_text" in body


# --- 11. actor НИКОГДА не берётся из клиентских полей -----------------------

def test_actor_is_never_taken_from_client_supplied_field():
    server._API_KEYS = {"secret1": "web"}

    with _minimal_pipeline(), _temp_usage_log(), _temp_dep_log() as dep_path:
        code, _ = _invoke_json(
            "POST", "/anonymize",
            headers={"Authorization": "Bearer secret1", "X-Actor": "attacker"},
            body_obj={"text": "Hi", "actor": "attacker2"},
        )
        actors = _read_actors(dep_path)

    assert code == 200
    assert actors == ["web"]


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
