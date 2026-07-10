"""Форматное покрытие детерминированного слоя (regex + corporate + glossary).

Проверяем, что значение РАЗНЫХ форматов не остаётся видимым в обезличенном
тексте. Это тесты на recall (утечку), а не на точные границы спана: главное —
чтобы данные «не проскакивали». NER/LLM здесь не участвуют.

Закрытые здесь дыры (см. историю): дата с полным словом «года» утекала целиком
(детектор съедал «г» из «года»); суммы без слова-валюты («Цена: 1 200 000»);
ISO/слэш/ММ.ГГГГ/квартал даты; ОКВЭД/ОКТМО/ОКАТО; VIN/госномер/кадастровый номер.
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


# --- Даты -----------------------------------------------------------------

def test_date_with_full_word_goda_not_leaking():
    # Ключевой баг: «12.03.2025 года» утекал целиком (спан «12.03.2025 г» стоял
    # посреди слова «года» и не маскировался).
    _masked("Договор от 12.03.2025 года действует", "12.03.2025")
    _masked("подписан 05.11.2025 году", "05.11.2025")


def test_date_iso_and_slash():
    _masked("дата 2025-03-12", "2025-03-12")
    _masked("дата 12/03/2025", "12/03/2025")


def test_date_month_year_and_quarter():
    _masked("за период 01.2025", "01.2025")
    _masked("за 1 квартал 2025 г.", "1 квартал 2025")


def test_date_ddmm_range():
    _masked("отпуск с 01.09 по 30.09", "01.09", "30.09")


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


# --- Глоссарий ------------------------------------------------------------

def test_glossary_terms_all_cases():
    _masked("письмо в Минфин", "Минфин")
    _masked("согласовано с Мингосом", "Мингосом")
    _masked("решение Правительства", "Правительства")
