"""Детерминированные детекторы реквизитов: даты с днём в кавычках, банк/филиал."""

from anonymizer.detectors import DATE, BANK, HOUSE, HOUSE_STANDALONE, DEFAULT_DETECTORS, run_detectors


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


# --- HOUSE (номерной хвост адреса) -----------------------------------------
# Реальная утечка из ГПХ-договора: GLiNER маскировал город+улицу как LOCATION,
# а «д. 15, офис 301» оставался в чистом тексте целиком — самая идентифицирующая
# часть адреса.

def test_house_tail_with_office_one_span():
    got = HOUSE.find('г. Москва, ул. Тверская, д. 15, офис 301')
    assert any(s.text == 'д. 15, офис 301' for s in got)


def test_house_tail_with_flat_one_span():
    got = HOUSE.find('г. Москва, ул. Арбат, д. 10, кв. 45')
    assert any(s.text == 'д. 10, кв. 45' for s in got)


def test_house_number_variants():
    assert any(s.text == 'д. 15А' for s in HOUSE.find('проживает по адресу д. 15А'))
    assert any(s.text == 'д. 12/3' for s in HOUSE.find('корпус дома д. 12/3'))
    assert any(
        s.text == 'д. 5, корп. 2, кв. 7'
        for s in HOUSE.find('зарегистрирован: д. 5, корп. 2, кв. 7')
    )


def test_house_standalone_office_and_flat():
    assert any(s.text == 'офис 301' for s in HOUSE_STANDALONE.find('приём ведётся в офис 301'))
    assert any(s.text == 'кв. 45' for s in HOUSE_STANDALONE.find('прописан в кв. 45'))


def test_house_no_false_positive_i_tak_dalee():
    # «и т.д.» — «так далее», не «дом»; цифра рядом не должна давать матч.
    assert HOUSE.find('перечень товаров и т.д. 5 лет действует') == []


def test_house_no_false_positive_str_as_page():
    # «стр.» одиночное — «страница», не «строение»: без дома перед ним не матчим.
    assert HOUSE.find('см. стр. 12') == []
    assert HOUSE_STANDALONE.find('см. стр. 12') == []


def test_house_no_false_positive_kvartal():
    # «IV кв. 2025 г.» — квартал года, не квартира; существующее поведение
    # DATE-детектора (квартал) не должно ломаться.
    assert HOUSE_STANDALONE.find('отчёт за IV кв. 2025 г.') == []
    assert DATE.find('отчёт за IV кв. 2025 г.') and DATE.find('IV кв. 2025 г.')[0].text == 'IV кв. 2025 г.'


def test_house_no_false_positive_square_meters():
    # «кв. м» — квадратные метры, маркер требует цифру сразу после себя.
    assert HOUSE_STANDALONE.find('площадью 45 кв. м') == []


def test_house_no_false_positive_village_no_digits():
    # «д. Ивановка» — деревня без номера дома, цифры нет вовсе.
    assert HOUSE.find('д. Ивановка') == []


def test_house_end_to_end_with_default_detectors():
    # Полный прогон через DEFAULT_DETECTORS: хвост адреса должен быть среди спанов.
    text1 = 'г. Москва, ул. Тверская, д. 15, офис 301'
    spans1 = run_detectors(text1, DEFAULT_DETECTORS)
    assert any(s.label == 'HOUSE' and s.text == 'д. 15, офис 301' for s in spans1)

    text2 = 'г. Москва, ул. Арбат, д. 10, кв. 45'
    spans2 = run_detectors(text2, DEFAULT_DETECTORS)
    assert any(s.label == 'HOUSE' and s.text == 'д. 10, кв. 45' for s in spans2)
