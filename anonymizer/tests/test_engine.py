"""Tests for the reversible anonymize/deanonymize core."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import anonymize, deanonymize  # noqa: E402
from anonymizer.deanonymize import find_unknown_placeholders  # noqa: E402


def _labels(result) -> set[str]:
    return {s.label for s in result.spans}


def test_roundtrip_restores_original():
    text = (
        "Сервер на IP-адресе 166.98.65.23 и почта ivan@example.ru, "
        "тел. +7 (812) 987 6543."
    )
    res = anonymize(text)
    assert res.anonymized_text != text
    assert "166.98.65.23" not in res.anonymized_text
    assert "ivan@example.ru" not in res.anonymized_text
    restored = deanonymize(res.anonymized_text, res.mapping)
    assert restored == text


def test_same_value_shares_one_placeholder():
    text = "Почта a@b.ru важна. Пишите снова на a@b.ru."
    res = anonymize(text)
    # one distinct email -> one placeholder, used twice
    assert res.anonymized_text.count("[EMAIL_1]") == 2
    assert list(res.mapping) == ["[EMAIL_1]"]
    assert deanonymize(res.anonymized_text, res.mapping) == text


def test_distinct_values_get_distinct_placeholders():
    text = "Карты 6271 8855 1122 3344 и 1234 5678 9012 3456 разные."
    res = anonymize(text)
    assert "[CREDIT_CARD_1]" in res.anonymized_text
    assert "[CREDIT_CARD_2]" in res.anonymized_text
    assert deanonymize(res.anonymized_text, res.mapping) == text


def test_detects_russian_documents():
    cases = {
        "В выписке написали ИНН 7711222333": "INN",
        "СНИЛС 112-233-445 95 указан верно": "SNILS",
        "Полис ОМС 1234567890123456 действует": "OMS",
        "Паспорт серия 7518, номер 492137 готов": "PASSPORT",
        "Свидетельство о рождении II - АВ 123456 предъявлено": "BIRTH_CERTIFICATE",
    }
    for text, label in cases.items():
        res = anonymize(text)
        assert label in _labels(res), f"{label} not found in: {text} -> {res.spans}"
        assert deanonymize(res.anonymized_text, res.mapping) == text


def test_keyword_stays_only_number_redacted():
    res = anonymize("В выписке написали ИНН 7711222333.")
    assert "ИНН" in res.anonymized_text  # trigger word preserved
    assert "7711222333" not in res.anonymized_text


def test_placeholder_10_vs_1_no_collision():
    mapping = {"[PERSON_1]": "Иванов", "[PERSON_10]": "Петров"}
    text = "[PERSON_10] и [PERSON_1] встретились."
    assert deanonymize(text, mapping) == "Петров и Иванов встретились."


def test_unknown_placeholder_left_untouched_by_default():
    text = "[CITY_1] неизвестен."
    assert deanonymize(text, {}) == text
    assert find_unknown_placeholders(text, {}) == ["[CITY_1]"]


def test_no_pii_returns_text_unchanged():
    text = "Сегодня хорошая погода и ничего секретного."
    res = anonymize(text)
    assert res.anonymized_text == text
    assert res.mapping == {}


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
