"""Входной шлюз спецкатегорий ПДн (медицинские данные) — server.py.

Проверяет: (1) документ с признаками медицинских данных отклоняется с 422
раньше, чем вызывается ``_compose``/пайплайн анонимизации; (2) то же для
файлового маршрута; (3) отказ не создаёт запись биллинга
(``usage_log`` ``request_total``); (4) ни само найденное значение, ни его
контекст не попадают ни в ответ клиенту, ни в stderr; (5) документ без
медицинских маркеров обрабатывается как обычно (регресс); (6)
``--allow-special-categories`` (``server._ALLOW_SPECIAL_CATEGORIES``)
отключает шлюз; (7) после удаления MED_ICD/MED_RECORD из
``DEFAULT_DETECTORS`` метка ``MEDICAL`` больше не появляется даже при
включённом escape hatch и рабочих regex-детекторах.

Не поднимает реальный сокет — тот же приём, что в ``test_server_auth.py``:
Handler строится вручную (``__new__``, минуя socket-driven ``__init__``), а
запрос/ответ читаются через ``io.BytesIO``.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import server, usage_log  # noqa: E402
from anonymizer.detectors import DEFAULT_DETECTORS  # noqa: E402

# Текст с явным медицинским маркером: ключевое слово "Диагноз" + код МКБ-10.
# "J06.9" — то самое значение, которое НИКОГДА не должно попасть в ответ/лог.
_MEDICAL_TEXT = (
    "Пациент Иванов И.И. Диагноз: J06.9, острая респираторная инфекция. "
    "Рекомендовано амбулаторное наблюдение."
)
_MEDICAL_MARKER = "J06.9"

_PLAIN_TEXT = "Обычный текст без персональных и медицинских данных вообще."


# --- Мини-каркас HTTP-обработчика без сокета (как в test_server_auth.py) ---

class _Headers:
    def __init__(self, items: dict | None = None) -> None:
        self._items = {k.lower(): v for k, v in (items or {}).items()}

    def get(self, key: str, default=None):
        return self._items.get(key.lower(), default)


def _invoke(method: str, path: str, headers: dict | None = None, body: bytes = b""):
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
def _isolate_state():
    orig_keys = server._API_KEYS
    orig_allow_anon = server._ALLOW_ANONYMOUS
    orig_allow_special = server._ALLOW_SPECIAL_CATEGORIES
    server._API_KEYS = {"secret1": "web"}
    server._ALLOW_ANONYMOUS = False
    server._ALLOW_SPECIAL_CATEGORIES = False
    try:
        yield
    finally:
        server._API_KEYS = orig_keys
        server._ALLOW_ANONYMOUS = orig_allow_anon
        server._ALLOW_SPECIAL_CATEGORIES = orig_allow_special


@contextmanager
def _minimal_pipeline():
    """Все стадии детекции выключены — минимальная конфигурация, достаточная,
    чтобы пайплайн отработал end-to-end без сети и без моделей (см.
    test_server_auth.py)."""
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
def _regex_pipeline():
    """Реальные regex-детекторы (DEFAULT_DETECTORS) включены, всё остальное —
    выключено. Нужно тесту 7: проверить, что MEDICAL больше не может
    появиться из-под DEFAULT_DETECTORS, даже когда детекция реально
    работает."""
    orig_detectors = server._DETECTORS
    orig_defaults = server._DEFAULTS
    orig_review_cfg = server._REVIEW_CFG
    orig_ner_backend = server._NER_BACKEND
    orig_needs_lock = server._NEEDS_MODEL_LOCK
    server._DETECTORS = {"regex": list(DEFAULT_DETECTORS)}
    server._DEFAULTS = {name: False for name in server._STAGE_NAMES}
    server._DEFAULTS["regex"] = True
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


def _auth_headers() -> dict:
    return {"Authorization": "Bearer secret1"}


def _file_body(text: str, filename: str = "note.txt") -> dict:
    return {
        "filename": filename,
        "file_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


# --- 1. Текст с МКБ-кодом рядом с триггером -> 422, _compose не вызван -----

def test_medical_text_refused_with_422_and_pipeline_not_invoked(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_compose", lambda *a, **k: calls.append((a, k)) or None)

    with _minimal_pipeline(), _temp_usage_log():
        code, body = _invoke_json(
            "POST", "/anonymize", headers=_auth_headers(),
            body_obj={"text": _MEDICAL_TEXT},
        )

    assert code == 422
    assert "error" in body
    assert calls == []


# --- 2. То же через файловый маршрут ----------------------------------------

def test_medical_file_refused_with_422_and_pipeline_not_invoked(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_compose", lambda *a, **k: calls.append((a, k)) or None)

    with _minimal_pipeline(), _temp_usage_log():
        code, body = _invoke_json(
            "POST", "/anonymize-file", headers=_auth_headers(),
            body_obj=_file_body(_MEDICAL_TEXT),
        )

    assert code == 422
    assert "error" in body
    assert calls == []


# --- 3. Отказ не пишет запись биллинга --------------------------------------

def test_refused_document_writes_no_billing_record():
    with _minimal_pipeline(), _temp_usage_log():
        code, _body = _invoke_json(
            "POST", "/anonymize", headers=_auth_headers(),
            body_obj={"text": _MEDICAL_TEXT},
        )
        totals = usage_log.read_request_totals()

    assert code == 422
    assert totals == []


# --- 4. Ни ответ, ни stderr не содержат найденное значение/контекст --------

def test_refusal_does_not_leak_matched_value_or_context(capsys):
    with _minimal_pipeline(), _temp_usage_log():
        code, body = _invoke_json(
            "POST", "/anonymize", headers=_auth_headers(),
            body_obj={"text": _MEDICAL_TEXT},
        )
    captured = capsys.readouterr()

    assert code == 422
    response_text = json.dumps(body, ensure_ascii=False)
    assert _MEDICAL_MARKER not in response_text
    assert "Иванов" not in response_text
    assert _MEDICAL_MARKER not in captured.err
    assert "Иванов" not in captured.err
    assert _MEDICAL_MARKER not in captured.out
    assert "Иванов" not in captured.out


# --- 5. Документ без медицинских маркеров обрабатывается как обычно --------

def test_document_without_medical_markers_processed_normally():
    with _minimal_pipeline(), _temp_usage_log():
        code, body = _invoke_json(
            "POST", "/anonymize", headers=_auth_headers(),
            body_obj={"text": _PLAIN_TEXT},
        )

    assert code == 200
    assert "anonymized_text" in body


# --- 6. --allow-special-categories -> документ с маркером обрабатывается ---

def test_allow_special_categories_flag_processes_document():
    server._ALLOW_SPECIAL_CATEGORIES = True

    with _minimal_pipeline(), _temp_usage_log():
        code, body = _invoke_json(
            "POST", "/anonymize", headers=_auth_headers(),
            body_obj={"text": _MEDICAL_TEXT},
        )

    assert code == 200
    assert "anonymized_text" in body


# --- 7. MEDICAL больше не появляется среди меток даже под escape hatch -----

def test_medical_label_absent_from_default_detectors_under_escape_hatch():
    server._ALLOW_SPECIAL_CATEGORIES = True

    with _regex_pipeline(), _temp_usage_log():
        result = server._run_anonymize_text({"text": _MEDICAL_TEXT, "regex": True})

    assert "MEDICAL" not in result["summary"]
    labels = {span["label"] for span in result["spans"]}
    assert "MEDICAL" not in labels
