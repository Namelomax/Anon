"""Форматное покрытие детерминированного слоя (regex + corporate + glossary).

Проверяем, что значение РАЗНЫХ форматов не остаётся видимым в обезличенном
тексте. Это тесты на recall (утечку), а не на точные границы спана: главное —
чтобы данные «не проскакивали». NER/LLM здесь не участвуют.

Закрытые здесь дыры (см. историю): дата с полным словом «года» утекала целиком
(детектор съедал «г» из «года») — но маскирование дат ОТКЛЮЧЕНО намеренно (см.
ниже), поэтому этот случай теперь проверяется как «слово „года" не изуродовано
рядом с видимой датой», а не как утечка; суммы без слова-валюты («Цена: 1 200
000»); ОКВЭД/ОКТМО/ОКАТО; VIN/госномер/кадастровый номер.

Даты (ISO/слэш/ММ.ГГГГ/квартал) СПЕЦИАЛЬНО не масштрируются: см. рационале у
``CORPORATE_DETECTORS`` в ``detectors.py`` — маскирование дат отключено по
договорённости с заказчиком, детекторы DATE/DATE_RANGE существуют, но не
зарегистрированы. Тесты на даты ниже проверяют ОБРАТНОЕ: что даты остаются
видимыми в тексте, а не что они маскируются — это фиксирует решение и не даёт
случайно вернуть DATE/DATE_RANGE в реестр без осознанного решения.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer.detectors import CORPORATE_DETECTORS, DEFAULT_DETECTORS  # noqa: E402
from anonymizer.engine import Anonymizer  # noqa: E402
from anonymizer.glossary import (  # noqa: E402
    DEFAULT_GLOSSARY_PATH,
    GlossaryDetector,
    load_glossary,
)


def _anonymizer() -> Anonymizer:
    dets = list(DEFAULT_DETECTORS) + list(CORPORATE_DETECTORS)
    entries = load_glossary(DEFAULT_GLOSSARY_PATH)
    if entries:
        dets.append(GlossaryDetector(entries))
    return Anonymizer(dets)


_A = _anonymizer()


def _masked(text: str, *must_disappear: str) -> None:
    """Утверждает, что каждое значение исчезло из обезличенного текста."""
    out = _A.anonymize(text).anonymized_text
    leaked = [v for v in must_disappear if v in out]
    assert not leaked, f"утекло {leaked} в: {out!r}"


def _visible(text: str, *must_remain: str) -> None:
    """Утверждает, что каждое значение ОСТАЛОСЬ видимым в обезличенном тексте.

    Обратное `_masked`: используется для дат — их маскирование отключено
    намеренно (см. рационале у ``CORPORATE_DETECTORS`` в ``detectors.py``), и
    для этого решения нужен именно guard «не замаскировано», а не «замаскировано».
    """
    out = _A.anonymize(text).anonymized_text
    missing = [v for v in must_remain if v not in out]
    assert not missing, f"дата пропала (замаскирована?) {missing} в: {out!r}"


# --- Даты -----------------------------------------------------------------
# Маскирование дат ОТКЛЮЧЕНО ПО ДОГОВОРЁННОСТИ С ЗАКАЗЧИКОМ — см. рационале у
# ``CORPORATE_DETECTORS`` в ``detectors.py`` (детекторы DATE/DATE_RANGE там
# определены, но намеренно не зарегистрированы в DEFAULT_DETECTORS). Тесты
# ниже фиксируют именно это решение: дата должна остаться ВИДИМОЙ в тексте.
# Вернуть можно, добавив DATE/DATE_RANGE обратно в реестр — но не раньше, чем
# кто-то заново прочитает рационале и согласует это с заказчиком.

def test_date_with_full_word_goda_not_leaking():
    """Даты не маскируются намеренно (detectors.py, рационале у CORPORATE_DETECTORS).

    Раньше здесь проверялась утечка («12.03.2025 г» ломался словом «года» и
    оставалась видна только часть даты). Теперь дата вообще не маскируется —
    проверяем, что она осталась видна ЦЕЛИКОМ, а слово «года»/«году» рядом с
    ней не изуродовано.
    """
    _visible("Договор от 12.03.2025 года действует", "12.03.2025 года")
    _visible("подписан 05.11.2025 году", "05.11.2025 году")


def test_date_iso_and_slash():
    """Даты не маскируются намеренно (detectors.py, рационале у CORPORATE_DETECTORS)."""
    _visible("дата 2025-03-12", "2025-03-12")
    _visible("дата 12/03/2025", "12/03/2025")


def test_date_month_year_and_quarter():
    """Даты не маскируются намеренно (detectors.py, рационале у CORPORATE_DETECTORS)."""
    _visible("за период 01.2025", "01.2025")
    _visible("за 1 квартал 2025 г.", "1 квартал 2025")


def test_date_ddmm_range():
    """Даты не маскируются намеренно (detectors.py, рационале у CORPORATE_DETECTORS)."""
    _visible("отпуск с 01.09 по 30.09", "01.09", "30.09")


# --- Суммы ----------------------------------------------------------------

def test_amount_without_currency_word():
    _masked("Цена: 1 200 000", "1 200 000")
    _masked("Стоимость доставки 250000,00", "250000,00")
    _masked("аванс 5000 за партию", "5000")


def test_amount_currency_symbol_first():
    _masked("Аванс: $1000", "1000")
    _masked("бюджет €500", "500")


def test_amount_with_currency_still_masked():
    _masked("стоимость 1 200 000 рублей", "1 200 000")
    _masked("4,7 млрд руб.", "4,7")


# --- Реквизиты ------------------------------------------------------------

def test_okved_oktmo_okato():
    _masked("ОКВЭД 62.01", "62.01")
    _masked("ОКТМО 45382000", "45382000")
    _masked("ОКАТО 45286560000", "45286560000")


def test_existing_requisites_still_masked():
    _masked("ИНН 7707083893, ОГРН 1027700132195, КПП 770701001", "7707083893",
            "1027700132195", "770701001")
    _masked("БИК 044525225, р/с 40702810900000012345", "044525225",
            "40702810900000012345")


# --- Идентификаторы объектов ----------------------------------------------

def test_vin_plate_cadastre():
    _masked("VIN XTA210990Y1234567", "XTA210990Y1234567")
    _masked("госномер А123ВС77", "А123ВС77")
    _masked("кадастровый номер 77:01:0001001:1234", "77:01:0001001:1234")


def test_order_number_with_inner_quote():
    # Реальный пропуск из договора ГПХ: номер приказа с кавычкой-буквой внутри.
    _masked("Приказом №21«А»-О от 20.01.2026", "№21«А»-О")


def test_appendix_and_section_numbers_not_overmasked():
    # Не должны попадать под номер договора/приказа.
    out = _A.anonymize("Приложение № 1; п. № 5 настоящего раздела").anonymized_text
    assert "№ 1" in out and "№ 5" in out


# --- Глоссарий ------------------------------------------------------------

def test_glossary_terms_all_cases():
    _masked("письмо в Минфин", "Минфин")
    _masked("согласовано с Мингосом", "Мингосом")
    _masked("решение Правительства", "Правительства")


# --- Аудит по «Реестру типов данных» чек-листа: ранее пропускавшиеся типы ----

def test_zagranpassport():
    _masked("загранпаспорт 71 1234567", "1234567")


def test_dms_policy():
    _masked("полис ДМС 7712345678901234", "7712345678901234")


def test_internal_phone_extension():
    _masked("тел. 100, доб. 1234", "доб. 1234")


def test_postal_index():
    _masked("индекс 664003, г. Иркутск", "664003")


def test_telegram_handle_but_not_email():
    _masked("пишите @ivan_petrov в телеграм", "@ivan_petrov")
    # e-mail НЕ должен распадаться на @-хэндл — остаётся одним EMAIL
    out = _A.anonymize("почта user@domain.ru").anonymized_text
    assert "user@domain.ru" not in out and "@domain" not in out


def test_card_solid_16_digits():
    _masked("карта 4276380012345678", "4276380012345678")
    # 20-значный счёт при этом не задет (маскируется как счёт, а не карта)
    _masked("р/с 40702810900000012345", "40702810900000012345")


def test_medical_icd_and_record():
    _masked("основной диагноз J06.9", "J06.9")
    _masked("история болезни № 12345", "12345")


def test_surname_after_patronymic_and_status_word():
    # Порядок «Имя Отчество Фамилия» + статусное слово: фамилия не должна утекать,
    # а «Самозанятый» не должно попасть в имя.
    out = _A.anonymize(
        "и Самозанятый Андрей Петрович Смирнов, именуемый «Исполнитель»"
    ).anonymized_text
    assert "Смирнов" not in out and "Андрей" not in out
    assert "Самозанятый" in out  # статусное слово остаётся в тексте


def test_section_number_not_treated_as_amount():
    # «п. 3.1» — ссылка на пункт, не сумма (регрессия PRICE_KW).
    out = _A.anonymize("Сумму, указанную в п. 3.1 настоящего Договора").anonymized_text
    assert "3.1" in out
    # но реальная цена рядом с тем же словом — маскируется
    _masked("Цена договора: 175 000 рублей", "175 000")
