"""Детерминированные детекторы реквизитов: даты с днём в кавычках, банк/филиал."""

from anonymizer.detectors import DATE, BANK


def _mask(det, text):
    return sorted(s.text for s in det.find(text))


def test_date_quoted_day_masked_fully():
    # Утечка из реального договора: день «12» оставался, маскировался только месяц/год.
    got = DATE.find('заключён «12» июня 2026 года между')
    assert got and got[0].text == '«12» июня 2026 года'


def test_date_plain_and_numeric():
    assert DATE.find('12 апреля 2026 г.')[0].text == '12 апреля 2026 г.'
    assert DATE.find('от 04.06.2026 г.')[0].text.startswith('04.06.2026')
    assert DATE.find('июнь 2026 года')[0].text == 'июнь 2026 года'


def test_date_no_false_positive_inside_number():
    # «312 июня» — «12» не должно выделяться отдельной датой.
    assert DATE.find('сумма 312 июня нет') == [] or all(
        s.text != '12 июня' for s in DATE.find('сумма 312 июня нет')
    )


def test_bank_branch_and_name_one_span():
    # Утекало целиком: и номер филиала, и название банка.
    got = BANK.find('Р/Счёт 123, Филиал №5440 Банка ВТБ (ПАО) БИК')
    assert any(s.text == 'Филиал №5440 Банка ВТБ (ПАО)' for s in got)


def test_bank_name_standalone_and_declension():
    assert any(s.text.startswith('Сбербанк') for s in BANK.find('оплата через Сбербанк'))
    assert any(s.text.startswith('Сбербанк') for s in BANK.find('в Сбербанке открыт счёт'))
    assert any(s.text == 'Альфа-Банк (АО)' for s in BANK.find('банк: Альфа-Банк (АО)'))


def test_bank_branch_unknown_name():
    assert any(s.text == 'Филиал №123' for s in BANK.find('Филиал №123 некоего банка'))


def test_bank_no_false_positive():
    assert BANK.find('обычный банковский день прошёл') == []


def test_contract_numeric_number_masked():
    from anonymizer.detectors import CONTRACT_NUM
    assert any(s.text == '№ 77/2026' for s in CONTRACT_NUM.find('ДОГОВОР № 77/2026 от'))
    assert any(s.text == '№42' for s in CONTRACT_NUM.find('Договор №42 от 01.07.2026'))
    # разделы/приложения/пункты НЕ трогаем
    assert CONTRACT_NUM.find('Приложение № 1 к настоящему Договору') == []
    assert CONTRACT_NUM.find('п. № 5 договора') == []
