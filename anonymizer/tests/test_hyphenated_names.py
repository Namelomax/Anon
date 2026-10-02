"""Регрессионные тесты по пропускам двойных фамилий (тест «Блокнот», окт. 2026):

* «Максимилиан Игнатьевич Фролов-Заречный» → «[PERSON_2]-Заречный»: шумовой
  фильтр считал дефис границей слова (4 «слова» → «фраза», спан выброшен), а
  регэксп по отчеству обрывался на «Фролов»;
* «Фамилия Заречный», «семьи Заречных» — часть двойной фамилии и её падежи
  не распространялись по документу;
* «Таракановой-Лунной» при пойманной «Тараканова-Лунная» — склонялась только
  последняя часть, основа «Тараканова-Лунна» с косвенным падежом не совпадала;
* «Вельяминов» → «Вельяминова/Вельяминовой» не маскировались;
* «…@ravenmail.ru. Под её именем» — email «съедал» следующее слово.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer.detectors import (  # noqa: E402
    DEFAULT_DETECTORS,
    EMAIL,
    PERSON_PATRONYMIC,
    is_noise_span,
)
from anonymizer.engine import Anonymizer  # noqa: E402
from anonymizer.spans import Span  # noqa: E402


class _FixedNER:
    """Детектор-заглушка: помечает PERSON ровно указанные значения."""

    def __init__(self, *values: str) -> None:
        self.values = values

    def find(self, text: str) -> list[Span]:
        out = []
        for v in self.values:
            for m in re.finditer(r"(?<!\w)" + re.escape(v) + r"(?!\w)", text):
                out.append(Span(m.start(), m.end(), "PERSON", v, source="gliner"))
        return out


def _anon(text: str, *names: str) -> str:
    return Anonymizer([*DEFAULT_DETECTORS, _FixedNER(*names)]).anonymize(text).anonymized_text


# --- шумовой фильтр и регэксп по отчеству ---------------------------------

def test_hyphenated_full_name_is_not_noise():
    assert not is_noise_span("Максимилиан Игнатьевич Фролов-Заречный", "PERSON")


def test_four_real_words_still_noise():
    assert is_noise_span("Это было очень давно", "PERSON")


def test_patronymic_regex_keeps_hyphenated_surname_after():
    text = "Полное имя: Максимилиан Игнатьевич Фролов-Заречный"
    assert [s.text for s in PERSON_PATRONYMIC.find(text)] == [
        "Максимилиан Игнатьевич Фролов-Заречный"
    ]


def test_patronymic_regex_keeps_hyphenated_surname_before():
    text = "Ответственный: Римская-Корсакова Анна Петровна."
    assert [s.text for s in PERSON_PATRONYMIC.find(text)] == [
        "Римская-Корсакова Анна Петровна"
    ]


def test_full_name_with_patronymic_leaves_no_tail():
    out = _anon("Полное имя: Максимилиан Игнатьевич Фролов-Заречный")
    assert "Заречный" not in out
    assert "Фролов" not in out


# --- распространение частей и падежей -------------------------------------

def test_part_of_double_surname_and_its_cases_are_masked():
    text = (
        "Владелец — Максимилиан Фролов-Заречный. Фамилия Заречный — не его. "
        "Место работы: архив семьи Заречных."
    )
    out = _anon(text, "Максимилиан Фролов-Заречный")
    assert "Заречн" not in out
    assert "Фролов" not in out


def test_double_surname_declined_in_both_parts():
    text = "Пришла Ясмина Тараканова-Лунная. Фамилия Таракановой-Лунной знакома."
    out = _anon(text, "Ясмина Тараканова-Лунная")
    assert "Тараканов" not in out
    assert "Лунн" not in out


def test_ov_surname_declensions_are_masked():
    out = _anon("Вельяминов пришёл. Брат Вельяминова и Вельяминовой.", "Вельяминов")
    assert "Вельямин" not in out


def test_consonant_first_name_declensions_are_masked():
    out = _anon("Максимилиан нашёл блокнот. У Максимилиана похолодели пальцы.", "Максимилиан")
    assert "Максимилиан" not in out


def test_declension_does_not_swallow_unrelated_lowercase_words():
    # Склонения ищутся только с заглавной: обычное прилагательное в тексте
    # не должно маскироваться из-за фамилии-прилагательного.
    out = _anon("Гордей Кривоколенный пришёл. Переулок кривоколенный узкий.", "Гордей Кривоколенный")
    assert "кривоколенный узкий" in out


def test_mapping_restores_original_text():
    from anonymizer.deanonymize import deanonymize

    text = (
        "Ясмина Тараканова-Лунная и Максимилиан Фролов-Заречный. "
        "Фамилия Таракановой-Лунной, семья Заречных."
    )
    res = Anonymizer(
        [*DEFAULT_DETECTORS, _FixedNER("Ясмина Тараканова-Лунная", "Максимилиан Фролов-Заречный")]
    ).anonymize(text)
    assert deanonymize(res.anonymized_text, res.mapping) == text


# --- email не съедает следующее слово --------------------------------------

def test_email_stops_before_next_sentence():
    text = "почта: aglaya.veliaminova@ravenmail.ru. Под её именем стояла пометка"
    assert [s.text for s in EMAIL.find(text)] == ["aglaya.veliaminova@ravenmail.ru"]


def test_email_stops_before_lowercase_cyrillic_word():
    text = "почта i.dubrovin@nocturn.net. и приписка"
    assert [s.text for s in EMAIL.find(text)] == ["i.dubrovin@nocturn.net"]


def test_email_with_spaced_dots_still_matched():
    assert [s.text for s in EMAIL.find("пишите n . makarov@aol . com срочно")] == [
        "n . makarov@aol . com"
    ]


def test_email_in_rf_zone_with_spaced_dot_still_matched():
    assert [s.text for s in EMAIL.find("адрес: info@пример . рф")] == ["info@пример . рф"]
