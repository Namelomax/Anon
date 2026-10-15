"""Личный словарь аккаунта (поле ``custom_terms`` запроса): разбор/валидация,
маскирование как CUSTOM_TERM и изоляция между запросами. Модели не грузятся —
общие детекторы подменяются пустыми."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import server  # noqa: E402


@pytest.fixture(autouse=True)
def _no_models(monkeypatch):
    monkeypatch.setattr(server, "_DETECTORS", {})
    monkeypatch.setattr(server, "_DEFAULTS", {})
    monkeypatch.setattr(server, "_REVIEW_CFG", None)


def _entries(raw):
    return server._parse_custom_terms(raw)


def test_absent_or_empty_is_no_terms():
    assert _entries(None) == ()
    assert _entries([]) == ()


def test_parse_ok_defaults_canonical_to_first_alias():
    (e,) = _entries([{"aliases": [" Ямбург ", "Ямбург-2"]}])
    assert e.aliases == ("Ямбург", "Ямбург-2")
    assert e.canonical == "Ямбург"
    (e,) = _entries([{"aliases": ["ШФ-7"], "canonical": "Шифр проекта"}])
    assert e.canonical == "Шифр проекта"


@pytest.mark.parametrize("raw", [
    "строка",
    {"aliases": ["ab"]},
    ["x"],
    [{"aliases": []}],
    [{"aliases": "ab"}],
    [{"aliases": ["a"]}],
    [{"aliases": ["a" * 101]}],
    [{"aliases": [5]}],
    [{"aliases": ["abc"] * 21}],
    [{"aliases": ["abc"], "canonical": 3}],
    [{"aliases": ["abc"]}] * 501,
])
def test_malformed_rejected(raw):
    with pytest.raises(server._BadRequest):
        _entries(raw)


def test_error_message_does_not_contain_term_value():
    with pytest.raises(server._BadRequest) as ei:
        _entries([{"aliases": ["СекретнаяЦель" * 20]}])
    assert "СекретнаяЦель" not in str(ei.value)


def test_per_request_term_masked_as_custom_term():
    terms = _entries([{"aliases": ["Ямбург"], "canonical": "Месторождение А"}])
    res = server._compose({}, account_terms=terms).anonymize("Добыча в Ямбурге выросла.")
    assert "Ямбург" not in res.anonymized_text
    assert "[CUSTOM_TERM_1]" in res.anonymized_text
    assert "Месторождение А" in res.mapping.values()


def test_terms_do_not_leak_between_requests():
    terms = _entries([{"aliases": ["Ямбург"]}])
    text = "Добыча в Ямбурге выросла."
    with_terms = server._compose({}, account_terms=terms).anonymize(text)
    other = server._compose({}).anonymize(text)
    after = server._compose({}, account_terms=_entries([{"aliases": ["Другое"]}])).anonymize(text)
    assert "CUSTOM_TERM" in with_terms.anonymized_text
    assert other.anonymized_text == text
    assert after.anonymized_text == text
    assert "glossary" not in server._DETECTORS


def test_run_anonymize_text_rejects_bad_terms_and_applies_good():
    with pytest.raises(server._BadRequest):
        server._run_anonymize_text({"text": "x", "custom_terms": "oops"})
    out = server._run_anonymize_text(
        {"text": "Проект Заря-9 закрыт.", "custom_terms": [{"aliases": ["Заря-9"]}]}
    )
    assert "Заря-9" not in out["anonymized_text"]
