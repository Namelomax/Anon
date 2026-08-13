"""Тесты протокола ревью «только исключения» (см. review.py: _build_review_prompt,
review_spans, _ask/_parse_verdicts).

С этим протоколом модель перечисляет ТОЛЬКО кандидатов, требующих действия
(keep=false, trim, merge_with) — молчание по кандидату означает «оставить
замаскированным». Каждая запись обязана дословно эхом повторить текст
кандидата (поле "text"); при несовпадении запись игнорируется и спан остаётся
замаскированным. Никакой живой LLM не требуется — http_pool.post_json везде
подменён на кансервированный ответ.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import http_pool  # noqa: E402
from anonymizer.review import (  # noqa: E402
    ReviewConfig,
    _build_review_prompt,
    review_spans,
)
from anonymizer.spans import Span  # noqa: E402


@contextmanager
def _patched_post_json(fn):
    """См. anonymizer/tests/test_review_usage.py — тот же контракт подмены
    http_pool.post_json(url, payload_bytes, headers, timeout, *, pool) ->
    (status, body_bytes)."""
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


def _chat_body(content) -> bytes:
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    return json.dumps(payload).encode("utf-8")


def _ok(content):
    """content — либо готовая строка ответа модели, либо объект, который
    сериализуется в JSON-массив вердиктов."""
    body = _chat_body(content if isinstance(content, str) else json.dumps(content))

    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        return 200, body

    return _fake


def _http_error(status: int = 500, body: bytes = b"boom"):
    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        return status, body

    return _fake


def _connection_boom():
    def _fake(url, payload_bytes, headers, timeout, *, pool="chat"):
        raise http_pool.PoolConnectionError("connection refused")

    return _fake


def _mk_spans(text: str, items: list[tuple[str, str]]) -> list[Span]:
    """items: [(label, value), ...] — каждое value должно встречаться в text
    ровно один раз (ищется через text.index)."""
    spans = []
    for label, value in items:
        start = text.index(value)
        spans.append(Span(start, start + len(value), label, value, source="llm"))
    return spans


_FIVE_CANDIDATES_TEXT = (
    "Объект Ромашка. Отдел Восход. Город Тверь указан. Компания Заря. "
    "Организация Полюс."
)
_FIVE_CANDIDATES = [
    ("ORG", "Ромашка"),
    ("ORG", "Восход"),
    ("LOCATION", "Тверь"),
    ("ORG", "Заря"),
    ("ORG", "Полюс"),
]


def _five_spans() -> list[Span]:
    return _mk_spans(_FIVE_CANDIDATES_TEXT, _FIVE_CANDIDATES)


# --- основной сценарий: только исключения -----------------------------------

def test_only_two_dropped_candidates_are_removed_rest_stay_masked():
    spans = _five_spans()
    cfg = ReviewConfig(model="test-model")
    response = [
        {"id": 1, "text": "Восход", "keep": False},
        {"id": 3, "text": "Заря", "keep": False},
    ]
    with _patched_post_json(_ok(response)):
        out = review_spans(_FIVE_CANDIDATES_TEXT, spans, cfg)

    kept_texts = {s.text for s in out}
    assert kept_texts == {"Ромашка", "Тверь", "Полюс"}
    assert len(out) == 3


def test_empty_list_keeps_everything_masked():
    spans = _five_spans()
    cfg = ReviewConfig(model="test-model")
    with _patched_post_json(_ok([])):
        out = review_spans(_FIVE_CANDIDATES_TEXT, spans, cfg)

    assert {s.text for s in out} == {v for _, v in _FIVE_CANDIDATES}
    assert len(out) == len(_FIVE_CANDIDATES)


def test_malformed_json_keeps_everything_masked():
    spans = _five_spans()
    cfg = ReviewConfig(model="test-model")
    with _patched_post_json(_ok("это не JSON вообще {{{")):
        out = review_spans(_FIVE_CANDIDATES_TEXT, spans, cfg)

    assert {s.text for s in out} == {v for _, v in _FIVE_CANDIDATES}
    assert len(out) == len(_FIVE_CANDIDATES)


def test_http_failure_keeps_everything_masked():
    spans = _five_spans()
    cfg = ReviewConfig(model="test-model")
    with _patched_post_json(_http_error(500)):
        out = review_spans(_FIVE_CANDIDATES_TEXT, spans, cfg)

    assert {s.text for s in out} == {v for _, v in _FIVE_CANDIDATES}
    assert len(out) == len(_FIVE_CANDIDATES)


def test_connection_failure_keeps_everything_masked():
    spans = _five_spans()
    cfg = ReviewConfig(model="test-model")
    with _patched_post_json(_connection_boom()):
        out = review_spans(_FIVE_CANDIDATES_TEXT, spans, cfg)

    assert {s.text for s in out} == {v for _, v in _FIVE_CANDIDATES}
    assert len(out) == len(_FIVE_CANDIDATES)


def test_echo_mismatch_entry_is_ignored_and_logged():
    spans = _five_spans()
    cfg = ReviewConfig(model="test-model")
    # id 1 указывает на "Восход", но модель эхом вернула другой текст —
    # нумерация "съехала", запись должна быть проигнорирована целиком.
    response = [{"id": 1, "text": "СОВСЕМ ДРУГОЕ СЛОВО", "keep": False}]
    with _patched_post_json(_ok(response)), _captured_stderr() as buf:
        out = review_spans(_FIVE_CANDIDATES_TEXT, spans, cfg)

    # Ничего не снято — несовпавшая запись отброшена, спан остался маской.
    assert {s.text for s in out} == {v for _, v in _FIVE_CANDIDATES}
    assert len(out) == len(_FIVE_CANDIDATES)
    logged = buf.getvalue()
    assert "discarded" in logged
    assert "mismatch" in logged


def test_old_verbose_format_still_interpreted_correctly():
    """Модель может по старой привычке вернуть вердикт на КАЖДОГО кандидата
    (keep=true как no-op для большинства, keep=false для одного) — новый
    парсер обязан понять и такой ответ."""
    spans = _five_spans()
    cfg = ReviewConfig(model="test-model")
    response = [
        {"id": 0, "text": "Ромашка", "keep": True},
        {"id": 1, "text": "Восход", "keep": False},
        {"id": 2, "text": "Тверь", "keep": True},
        {"id": 3, "text": "Заря", "keep": True},
        {"id": 4, "text": "Полюс", "keep": True},
    ]
    with _patched_post_json(_ok(response)):
        out = review_spans(_FIVE_CANDIDATES_TEXT, spans, cfg)

    assert {s.text for s in out} == {"Ромашка", "Тверь", "Заря", "Полюс"}
    assert len(out) == 4


def test_trim_and_merge_with_work_as_exceptions():
    text = "Капитан Яков позвонил. Вайгус ответил. Дело закрыто."
    spans = _mk_spans(text, [("PERSON", "Капитан Яков"), ("PERSON", "Вайгус")])
    cfg = ReviewConfig(model="test-model")
    # Ни trim-, ни merge_with-запись не несёт keep вовсе — это ключевой случай
    # для нового протокола: keep остаётся неявным True (маска не снимается).
    response = [
        {"id": 0, "text": "Капитан Яков", "trim": "Яков"},
        {"id": 1, "text": "Вайгус", "merge_with": 0},
    ]
    with _patched_post_json(_ok(response)):
        out = review_spans(text, spans, cfg)

    assert len(out) == 2
    by_text = {s.text: s for s in out}
    assert "Яков" in by_text  # обрезано до ПДн-части
    assert "Вайгус" in by_text
    trimmed = by_text["Яков"]
    nickname = by_text["Вайгус"]
    assert trimmed.merge_key is not None
    assert trimmed.merge_key == nickname.merge_key
    assert trimmed.canonical_text == "Яков"
    assert nickname.canonical_text == "Яков"


# --- промпт: правило про пропуск (омиссию) должно быть в обоих вариантах ----

def test_omission_rule_present_in_both_prompt_variants():
    off = _build_review_prompt(False)
    on = _build_review_prompt(True)
    for prompt in (off, on):
        assert "ТОЛЬКО ИСКЛЮЧЕНИЯ" in prompt
        assert "оставить замаскированным как есть" in prompt
        assert "не включай" in prompt.lower() or "не нужно" in prompt.lower()


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
