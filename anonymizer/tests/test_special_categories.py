"""Специальные категории ПДн (ст. 10, 11 152-ФЗ): детекторы, шлюз, маскирование.

Якорь — поле анкеты с двоеточием или однозначный оборот, а не голое слово:
«православный храм» и «русский язык» в договоре/вакансии отказа не вызывают.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer import server, usage_log  # noqa: E402
from anonymizer.detectors import (  # noqa: E402
    DEFAULT_DETECTORS,
    SPECIAL_CATEGORY_DETECTORS,
    run_detectors,
)

# (метка, текст, значение, которое не должно утечь)
POSITIVE = [
    ("MEDICAL", "Группа инвалидности: вторая", "вторая"),
    ("MEDICAL", "Является инвалидом II группы с 2019 года", "II группы"),
    ("MEDICAL", "Листок нетрудоспособности № 910 123 456 789", "910 123 456 789"),
    ("MEDICAL", "Справка МСЭ серия МСЭ-2012 № 0123456", "0123456"),
    ("MEDICAL", "Сотрудник состоит на учёте у нарколога", "нарколога"),
    ("MEDICAL", "ВИЧ-статус: положительный", "положительный"),
    ("MEDICAL", "Состояние здоровья: гипертония", "гипертония"),
    ("ETHNICITY", "Национальность: татарин", "татарин"),
    ("POLITICAL", "Партийность: член КПРФ", "член КПРФ"),
    ("POLITICAL", "Политические взгляды: либеральные", "либеральные"),
    ("RELIGION", "Вероисповедание: православное", "православное"),
    ("RELIGION", "Религиозные убеждения: буддизм", "буддизм"),
    ("INTIMATE", "Сексуальная ориентация: гетеросексуальная", "гетеросексуальная"),
    ("CRIMINAL_RECORD", "Судимость: имеется, ст. 158 УК РФ", "имеется"),
    ("CRIMINAL_RECORD", "Ранее не судим, на иждивении двое детей", "не судим"),
    ("CRIMINAL_RECORD", "Справка о наличии (отсутствии) судимости от 01.02.2024", "судимости"),
    ("BIOMETRIC", "Дактилоскопические данные: хэш a1b2c3d4", "a1b2c3d4"),
    ("BIOMETRIC", "Отпечатки пальцев: приложены в файле", "приложены в файле"),
    ("BIOMETRIC", "Фото для идентификации: img_0042.jpg", "img_0042.jpg"),
]

NEAR_MISS = [
    "Подрядчик выполняет реставрацию фасада православного храма по адресу заказчика.",
    "Требования к кандидату: русский язык, опыт работы от трёх лет.",
    "Работник обязан знать русский язык и соблюдать нормы делового этикета.",
    "Поставка оборудования для контроля доступа по отпечаткам пальцев на объект.",
    "Парковочные места для инвалидов 1 группы выделяются на первом этаже.",
    "Организация не вправе требовать сведения о политических взглядах и религиозных убеждениях.",
    "Национальный банк установил ключевую ставку; партия товара принята.",
    "Срок давности привлечения к ответственности истёк, судимость погашена по закону.",
    "Выплата по листкам нетрудоспособности производится в установленном порядке.",
    "Заказчик строит мечеть, церковь и синагогу; русская национальная кухня в меню.",
    "Диагностика оборудования проведена; состояние дел удовлетворительное.",
]


@pytest.mark.parametrize("label,text,value", POSITIVE)
def test_detected(label, text, value):
    spans = run_detectors(text, SPECIAL_CATEGORY_DETECTORS)
    assert label in {s.label for s in spans}
    assert any(value in s.text or s.text in value for s in spans if s.label == label)


@pytest.mark.parametrize("text", NEAR_MISS)
def test_near_miss_not_detected(text):
    assert run_detectors(text, SPECIAL_CATEGORY_DETECTORS) == []


# --- Шлюз: отказ без значения и контекста -----------------------------------

@pytest.fixture(autouse=True)
def _state():
    orig = server._ALLOW_SPECIAL_CATEGORIES
    server._ALLOW_SPECIAL_CATEGORIES = False
    try:
        yield
    finally:
        server._ALLOW_SPECIAL_CATEGORIES = orig


@pytest.mark.parametrize("label,text,value", POSITIVE)
def test_gate_refuses_without_value_or_context(label, text, value, capsys):
    doc = "Секретный контекст Зубаткин. " + text + " Конец документа Мельхиор."
    with tempfile.TemporaryDirectory() as tmp:
        orig = usage_log.LOG_PATH
        usage_log.LOG_PATH = Path(tmp) / "usage.jsonl"
        try:
            with pytest.raises(server._SpecialCategoryRefused) as exc:
                server._check_special_categories(doc)
            journal = (
                usage_log.LOG_PATH.read_text(encoding="utf-8")
                if usage_log.LOG_PATH.exists() else ""
            )
        finally:
            usage_log.LOG_PATH = orig
    err = capsys.readouterr().err
    visible = str(exc.value) + err + journal
    assert value not in visible
    for ctx in ("Зубаткин", "Мельхиор"):
        assert ctx not in visible
    assert label in err  # метка категории и счётчик — можно
    assert "совпадений:" in err
    assert journal == ""


@pytest.mark.parametrize("text", NEAR_MISS)
def test_gate_passes_near_miss(text):
    server._check_special_categories(text)


# --- Разрешено: маскируем ---------------------------------------------------

@pytest.mark.parametrize("label,text,value", POSITIVE)
def test_allowed_mode_masks_value(label, text, value):
    server._ALLOW_SPECIAL_CATEGORIES = True
    orig = (server._DETECTORS, server._DEFAULTS, server._REVIEW_CFG,
            server._NER_BACKEND, server._NEEDS_MODEL_LOCK)
    server._DETECTORS = {"regex": list(DEFAULT_DETECTORS)}
    server._DEFAULTS = {n: False for n in server._STAGE_NAMES}
    server._REVIEW_CFG = None
    server._NER_BACKEND = "none"
    server._NEEDS_MODEL_LOCK = False
    try:
        res = server._compose({}).anonymize(text)
    finally:
        (server._DETECTORS, server._DEFAULTS, server._REVIEW_CFG,
         server._NER_BACKEND, server._NEEDS_MODEL_LOCK) = orig
    masked = [s for s in res.spans if s.label == label]
    assert masked
    hidden = masked[0].text
    assert hidden not in res.anonymized_text
    assert hidden in res.mapping.values()


def test_default_mode_does_not_add_special_detectors():
    assert server._special_category_masking_detectors() == ()
    server._ALLOW_SPECIAL_CATEGORIES = True
    assert server._special_category_masking_detectors() == tuple(SPECIAL_CATEGORY_DETECTORS)
