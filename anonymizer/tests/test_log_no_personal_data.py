"""Регрессия: персональные данные документа не попадают ни в stderr
(journald на проде), ни в JSONL-журнал расхода.

Контекст: сервис маскирования ПДн заявил юристу, что журнал содержит только
метаданные. Раньше это было неправдой для системного журнала: диагностика
review.py печатала значения кандидатов (фамилии из person-gate, короткие
числа, примеры recall, сырой ответ recall-модели), а тела HTTP-ответов шлюза
(``llm.py``, ``gliner_remote.py``, ``review.py``) попадали в текст исключения
и оттуда в ``error=`` JSONL-журнала.

Тесты ниже гоняют документ с выдуманными ПДн через настоящий
``Anonymizer.anonymize`` (с review и recall), подменив только
``http_pool.post_json`` — ни одного живого вызова. Любой возврат старых
print'ов (или тела ответа в исключении) делает их красными.

Отладочный переключатель ``usage_log.UNSAFE_LOG_PERSONAL_DATA`` проверяется
отдельным тестом: включённый, он обязан вернуть подробный вывод — иначе он
декоративный.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from anonymizer import gliner_remote, http_pool, usage_log  # noqa: E402
from anonymizer import review as review_mod  # noqa: E402
from anonymizer.engine import Anonymizer  # noqa: E402
from anonymizer.gliner_remote import RemoteGLiNERConfig, RemoteGLiNERDetector  # noqa: E402
from anonymizer.llm import LLMConfig, LLMDetector  # noqa: E402
from anonymizer.review import ReviewConfig  # noqa: E402
from anonymizer.spans import Span  # noqa: E402

# Выдуманные значения: словарь их знать не может, в stderr/журнале им делать
# нечего. Каждое — ровно то, что в старых print'ах уходило в journald.
_SURNAME_GATE = "Зюзенко"          # person-gate: модель просит снять, шлюз отказывает
_NAME_FULL = "Крапивенский Артемий"
_SURNAME_RECALL = "Лукашевич"      # вернёт recall
_SURNAME_UNKNOWN_TYPE = "Мигунов"  # recall вернёт с неизвестным типом
_SHORT_NUMBER = "913"              # короткое число, которое модель оставит замаскированным
_PASSPORT = "4512 345678"
_SNILS = "112-233-445 95"
_PHONE = "+7 912 345-67-89"
_FILENAME = "Приказ_Ларионов_И_И.docx"

_FORBIDDEN = [
    _SURNAME_GATE, _NAME_FULL, "Крапивенский", "Артемий", _SURNAME_RECALL,
    _SURNAME_UNKNOWN_TYPE, _SHORT_NUMBER, _PASSPORT, "345678", _SNILS,
    "233-445", _PHONE, "345-67-89", "Ларионов",
]

_TEXT = (
    f"Заявитель {_NAME_FULL}, {_SURNAME_GATE} подал заявление. "
    f"Свидетель {_SURNAME_RECALL} подтвердил. Паспорт {_PASSPORT}, "
    f"СНИЛС {_SNILS}, телефон {_PHONE}. Пункт {_SHORT_NUMBER} учтён."
)


class _FakeDetector:
    def __init__(self, label: str, value: str, source: str = "llm") -> None:
        self.label, self.value, self.source = label, value, source

    def find(self, text: str) -> list[Span]:
        i = text.index(self.value)
        return [Span(i, i + len(self.value), self.label, self.value, source=self.source)]


def _anonymizer() -> Anonymizer:
    return Anonymizer(
        [
            _FakeDetector("PERSON", _NAME_FULL),
            _FakeDetector("PERSON", _SURNAME_GATE),
            _FakeDetector("PASSPORT", _PASSPORT, "regex"),
            _FakeDetector("SNILS", _SNILS, "regex"),
            _FakeDetector("PHONE", _PHONE, "regex"),
            _FakeDetector("ADMIN_CODE", _SHORT_NUMBER),
        ],
        review_config=ReviewConfig(model="test-model", recall=True),
    )


def _chat(content: str) -> tuple[int, bytes]:
    body = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    return 200, json.dumps(body).encode("utf-8")


def _quoted_texts(user: str) -> list[str]:
    """Значения из строк вида ``N. [LABEL] "значение" — контекст…`` /
    ``N. "значение" — контекст…`` — фейк-модель отвечает по тому, что ей реально
    прислали, а не по хардкод-ids."""
    out = []
    for line in user.splitlines():
        a = line.find('"')
        b = line.find('" —', a + 1)
        out.append(line[a + 1:b] if a >= 0 and b > a else "")
    return out


def _upstream(url, payload_bytes, headers, timeout, *, pool="chat"):
    """Фейк шлюза: отвечает на все четыре слоя review/recall, называя в
    ответах выдуманные значения."""
    payload = json.loads(payload_bytes.decode("utf-8"))
    system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
    if system == review_mod._RECALL_SYSTEM_PROMPT:
        return _chat(
            "Нашёл пропущенное: "
            + json.dumps(
                [
                    {"text": _SURNAME_RECALL, "type": "PERSON"},
                    {"text": _SURNAME_UNKNOWN_TYPE, "type": "GADGET"},
                ],
                ensure_ascii=False,
            )
        )
    if system == review_mod._SHORT_NUMBER_SYSTEM_PROMPT:
        verdicts = [
            {"id": i, "text": t, "mask": True}
            for i, t in enumerate(_quoted_texts(user)) if t
        ]
        return _chat(json.dumps(verdicts, ensure_ascii=False))
    if system == review_mod._ADJACENT_SYSTEM_PROMPT:
        verdicts = [
            {"id": i, "text": t, "mask": True}
            for i, t in enumerate(_quoted_texts(user)) if t
        ]
        return _chat(json.dumps(verdicts, ensure_ascii=False))
    # Основной список: просим снять фамилию (шлюз морфологии должен отказать)
    # и часть полного имени (trim).
    verdicts = []
    for i, t in enumerate(_quoted_texts(user)):
        if t == _SURNAME_GATE:
            verdicts.append({"id": i, "text": t, "keep": False})
        elif t == _NAME_FULL:
            verdicts.append({"id": i, "text": t, "keep": True, "trim": "Артемий"})
    return _chat(json.dumps(verdicts, ensure_ascii=False))


@contextmanager
def _patched_post_json(fn):
    orig = http_pool.post_json
    http_pool.post_json = fn
    try:
        yield
    finally:
        http_pool.post_json = orig


@contextmanager
def _captured_stderr():
    orig = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = orig


@contextmanager
def _temp_usage_log():
    orig_path, orig_mode = usage_log.LOG_PATH, usage_log.USAGE_LOG_CALLS
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "usage.jsonl"
        usage_log.LOG_PATH = path
        usage_log.USAGE_LOG_CALLS = "all"  # строка на КАЖДЫЙ вызов — максимум поверхности
        try:
            yield path
        finally:
            usage_log.LOG_PATH, usage_log.USAGE_LOG_CALLS = orig_path, orig_mode


@contextmanager
def _no_sleep():
    orig = gliner_remote._sleep
    gliner_remote._sleep = lambda _seconds: None
    try:
        yield
    finally:
        gliner_remote._sleep = orig


@contextmanager
def _unsafe_switch(value: bool):
    orig = usage_log.UNSAFE_LOG_PERSONAL_DATA
    usage_log.UNSAFE_LOG_PERSONAL_DATA = value
    try:
        yield
    finally:
        usage_log.UNSAFE_LOG_PERSONAL_DATA = orig


def _run_pipeline() -> tuple[str, str, list[dict]]:
    """Прогон документа; возвращает (stderr, текст JSONL-журнала, warnings)."""
    with _temp_usage_log() as log_path, _patched_post_json(_upstream), _captured_stderr() as buf:
        with usage_log.request_context(filename=_FILENAME, chars=len(_TEXT)):
            result = _anonymizer().anonymize(_TEXT)
        journal = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return buf.getvalue(), journal, list(result.warnings)


# --- 1. Главная регрессия: по умолчанию ни одно значение не утекает ---------

def test_no_document_value_reaches_stderr_or_journal_by_default():
    assert usage_log.UNSAFE_LOG_PERSONAL_DATA is False  # дефолт — выключено
    with _unsafe_switch(False):
        stderr, journal, _warnings = _run_pipeline()

    # Слои действительно сработали (иначе тест ничего не доказывает).
    assert "[short-num]" in stderr
    assert "person-gate" in stderr
    assert "[recall] ответ LLM" in stderr
    assert "неизвестным типом" in stderr
    assert '"kind": "request_total"' in journal or '"request_total"' in journal

    for value in _FORBIDDEN:
        assert value not in stderr, f"{value!r} утёк в stderr:\n{stderr}"
        assert value not in journal, f"{value!r} утёк в журнал:\n{journal}"


def test_gate_refusal_is_still_diagnosable_without_values():
    with _unsafe_switch(False):
        stderr, _journal, _warnings = _run_pipeline()
    # «Кого и почему» заменено на «сколько и почему».
    assert "refused model's keep=false for 1 candidate(s)" in stderr
    assert "×1" in stderr  # причина со счётчиком
    assert "модель оставила замаскированными: 1" in stderr
    assert "кандидатов разобрано: 2" in stderr
    assert "отброшено кандидатов с неизвестным типом: 1" in stderr


# --- 2. Переключатель настоящий: включённый, возвращает подробности ---------

def test_unsafe_switch_restores_verbose_output():
    with _unsafe_switch(True):
        stderr, _journal, _warnings = _run_pipeline()

    assert _SURNAME_GATE in stderr          # person-gate: значение кандидата
    assert _SHORT_NUMBER in stderr          # short-num: оставленные значения
    assert "сырой ответ LLM" in stderr      # recall: сырой ответ…
    assert _SURNAME_RECALL in stderr        # …с настоящими значениями
    assert _SURNAME_UNKNOWN_TYPE in stderr  # recall: примеры отброшенных


def test_switch_is_off_unless_explicitly_enabled():
    import os
    import subprocess

    root = str(Path(__file__).resolve().parents[2])
    code = "from anonymizer import usage_log; print(usage_log.UNSAFE_LOG_PERSONAL_DATA)"
    for raw, expected in [(None, "False"), ("", "False"), ("0", "False"), ("no", "False"),
                          ("1", "True"), ("true", "True"), ("ON", "True")]:
        env = {k: v for k, v in os.environ.items() if k != "ANONYMIZER_UNSAFE_LOG_PERSONAL_DATA"}
        if raw is not None:
            env["ANONYMIZER_UNSAFE_LOG_PERSONAL_DATA"] = raw
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=root, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert out == expected, (raw, out)


# --- 3. Тело HTTP-ответа шлюза не попадает в исключение/журнал --------------

_ECHO = f"upstream says: cannot parse {_SURNAME_GATE} {_PASSPORT}"
_ECHO_BYTES = _ECHO.encode("utf-8")


def _echoing_502(url, payload_bytes, headers, timeout, *, pool="chat"):
    return 502, _ECHO_BYTES


def test_review_http_error_has_status_and_length_but_no_body():
    cfg = ReviewConfig(model="test-model")
    with _unsafe_switch(False), _patched_post_json(_echoing_502):
        with pytest.raises(OSError) as info:
            review_mod._chat_completion(cfg, {"model": "m", "messages": []})
    msg = str(info.value)
    assert "502" in msg
    assert str(len(_ECHO_BYTES)) in msg
    assert _SURNAME_GATE not in msg and "4512" not in msg


def test_gliner_http_error_has_status_and_length_but_no_body():
    det = RemoteGLiNERDetector(RemoteGLiNERConfig())
    with _unsafe_switch(False), _temp_usage_log() as log_path, _patched_post_json(_echoing_502), _no_sleep():
        with pytest.raises(RuntimeError) as info:
            det._extract("some chunk")
        # 5xx ретраится — к журналу пишутся все попытки; дожидаемся без пауз
        journal = log_path.read_text(encoding="utf-8")
    msg = str(info.value)
    assert "502" in msg and str(len(_ECHO_BYTES)) in msg
    assert _SURNAME_GATE not in msg and "4512" not in msg
    assert "502" in journal and str(len(_ECHO_BYTES)) in journal
    assert _SURNAME_GATE not in journal and "4512" not in journal


def test_llm_http_error_has_status_and_length_but_no_body():
    det = LLMDetector(LLMConfig(max_chars=50, concurrency=1))
    with _unsafe_switch(False), _temp_usage_log() as log_path, _patched_post_json(_echoing_502), \
            _captured_stderr() as buf:
        det.find("Меня зовут Иван Петров, я живу в Москве.")
        journal = log_path.read_text(encoding="utf-8")
    assert "502" in journal and str(len(_ECHO_BYTES)) in journal
    for blob in (journal, buf.getvalue(), json.dumps(det.warnings, ensure_ascii=False)):
        assert _SURNAME_GATE not in blob and "4512" not in blob


def test_http_error_body_is_restored_only_with_unsafe_switch():
    with _unsafe_switch(True):
        msg = usage_log.describe_http_error(502, _ECHO_BYTES)
    assert _SURNAME_GATE in msg
    with _unsafe_switch(False):
        msg = usage_log.describe_http_error(502, _ECHO_BYTES)
    assert _SURNAME_GATE not in msg
    assert msg == f"HTTP 502, тело ответа {len(_ECHO_BYTES)} байт"
