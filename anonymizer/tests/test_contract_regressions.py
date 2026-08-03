"""Регрессионные тесты по косякам из реального договора ГПХ (сентябрь 2025):

* [DATE_1] = «12» сентября…» с оторванной кавычкой (сирота-« в тексте);
* [ORG_7] = «Технопарка «Сколково» без закрывающей »;
* [PERSON_4] = «Исполнителя» / [PERSON_2] = «Заказчик» — роли сторон
  маскировались как ФИО (косвенные падежи проскакивали exact-фильтр,
  morph-спаны шли в обход фильтров);
* [LOCATION_3] = «КПП», [LOCATION_4] = «ОГРН» — слова-метки реквизитов
  маскировались как локации;
* «КиберКубок 2025» и КиберКубок 2025 — два плейсхолдера на одну сущность.
"""

from anonymizer.canonicalize import group_key
from anonymizer.detectors import (
    DATE,
    is_contract_role,
    is_noise_span,
    is_short_number,
    is_stopword_entity,
)
from anonymizer.engine import Anonymizer
from anonymizer.spans import Span, rebalance_quotes


# --- rebalance_quotes -------------------------------------------------------

def test_rebalance_extends_left_to_opening_quote():
    text = "г. Москва «12» сентября 2025 года"
    # спан как после _trim: без ведущей «, но с внутренней »
    s = Span(11, len(text), "DATE", text[11:])
    fixed = rebalance_quotes(text, s)
    assert fixed.text == "«12» сентября 2025 года"
    assert text[fixed.start - 1] == " "


def test_rebalance_extends_right_to_closing_quote():
    text = "на базе Технопарка «Сколково»."
    s = Span(8, 28, "ORG", text[8:28])  # ...«Сколково без »
    fixed = rebalance_quotes(text, s)
    assert fixed.text == "Технопарка «Сколково»"


def test_rebalance_shrinks_edge_quote_without_pair():
    text = "слово Сколково» дальше"
    s = Span(6, 15, "ORG", text[6:15])  # Сколково» — пары « рядом нет
    fixed = rebalance_quotes(text, s)
    assert fixed.text == "Сколково"


def test_rebalance_keeps_balanced_and_midspan():
    text = "ООО «Ромашка» и партнёры"
    s = Span(0, 13, "ORG", text[0:13])
    assert rebalance_quotes(text, s) is s
    # непарная кавычка в СЕРЕДИНЕ без пары рядом — не трогаем
    text2 = "Технопарк «Сколково и партнёры"
    s2 = Span(0, len(text2), "ORG", text2)
    assert rebalance_quotes(text2, s2).text == text2


def test_date_detector_plus_rebalance_no_orphan_quote():
    text = "г. Москва «12» сентября 2025 года"
    spans = [rebalance_quotes(text, s) for s in DATE.find(text)]
    assert any(s.text == "«12» сентября 2025 года" for s in spans)


# --- роли сторон договора ----------------------------------------------------

def test_contract_roles_dropped_in_any_case():
    for v in ("Исполнителя", "Исполнителю", "Заказчик", "Заказчика",
              "Подрядчиком", "Сторонами", "Арендодателя"):
        assert is_contract_role(v), v
        assert is_noise_span(v, "PERSON") is True, v
        assert is_noise_span(v, "ORG") is True, v


def test_real_names_survive_role_filter():
    for v in ("Андрей Петрович Смирнов", "С.В. Кузнецов", "Командиров"):
        assert is_contract_role(v) is False, v
        assert is_noise_span(v, "PERSON") is False, v


def test_role_seed_does_not_spread_via_declensions():
    """Плохой seed от NER («Заказчика» как PERSON) не должен размножаться
    morph-проходом: раньше morph-спаны шли в обход passes_filters."""

    class FakeNER:
        def find(self, text):
            i = text.index("Заказчика")
            return [Span(i, i + len("Заказчика"), "PERSON",
                         "Заказчика", source="gliner")]

    text = "Исполнитель обязуется по заданию Заказчика. Заказчик обязан оплатить."
    res = Anonymizer([FakeNER()]).anonymize(text)
    assert res.anonymized_text == text  # ничего не замаскировано
    assert res.mapping == {}


# --- слова-метки реквизитов ---------------------------------------------------

def test_requisite_labels_not_entities():
    for v in ("КПП", "ОГРН", "БИК", "ОКПО", "Реквизиты банка"):
        assert is_stopword_entity(v, "LOCATION") is True, v
        assert is_stopword_entity(v, "ORG") is True, v
    # сами ЗНАЧЕНИЯ реквизитов (жёсткие метки) фильтр не трогает
    assert is_stopword_entity("770801001", "KPP") is False


# --- канонизация: кавычки не дробят сущность ----------------------------------

def test_group_key_ignores_quotes():
    assert group_key("«КиберКубок 2025»") == group_key("КиберКубок 2025")
    assert group_key("«Форуса»") == group_key("Форус")


def test_quoted_and_bare_org_share_placeholder_and_mapping_is_clean():
    class FakeORG:
        def find(self, text):
            out = []
            for needle in ("«КиберКубок 2025»", "КиберКубок 2025"):
                start = 0
                while (i := text.find(needle, start)) >= 0:
                    if needle == "КиберКубок 2025" and i > 0 and text[i - 1] == "«":
                        start = i + 1  # вложено в кавычечную форму — пропускаем
                        continue
                    out.append(Span(i, i + len(needle), "ORG", needle, source="gliner"))
                    start = i + len(needle)
            return out

    text = "соревнование «КиберКубок 2025» прошло; итоги КиберКубок 2025 подведены"
    res = Anonymizer([FakeORG()]).anonymize(text)
    org_placeholders = {ph for ph in res.mapping if ph.startswith("[ORG_")}
    assert len(org_placeholders) == 1, res.mapping
    # мёртвых строк нет: каждый плейсхолдер из маппинга есть в тексте
    for ph in res.mapping:
        assert ph in res.anonymized_text, ph


# --- Голые цифры под форматными метками (договор купли-продажи участка) -----
# LLM пометила номера пунктов «1»/«2»/«3»/«4» как PASSPORT/EMAIL. Защита от
# односимвольных значений жила в is_stopword_entity и работала только для
# _SOFT_LABELS, поэтому форматные метки её не получали, а mask_all_occurrences
# размножил цифры по 127 местам: «[PASSPORT_1].[PASSPORT_2]. Цена…».

def test_single_character_is_noise_under_every_label():
    """Одиночный символ не бывает спорным — режется детерминированно."""
    for label in ("PASSPORT", "EMAIL", "SUBJECT", "PERSON", "ORG", "SENSITIVE",
                  "ADMIN_CODE"):
        assert is_noise_span("1", label), label


def test_short_numbers_are_routed_to_the_model_not_a_hard_threshold():
    """2-3-значные числа спорные: их судит LLM по контексту, а не длина."""
    for value in ("12", "000", "777"):
        assert is_short_number(value), value
        assert not is_noise_span(value, "PASSPORT"), value
    # длинные значения и значения с разделителями моделью не разбираются
    for value in ("1", "2014", "000000", "044525225", "50:20:123456:21", "777-003"):
        assert not is_short_number(value), value


def test_clause_numbering_survives_a_bogus_passport_digit():
    """Номер пункта «1» под меткой PASSPORT давал 127 подстановок и превращал
    нумерацию договора в «[PASSPORT_1].[PASSPORT_2]. Цена…»."""

    class FakeLLM:
        def find(self, text):
            out, start = [], 0
            while (i := text.find("1", start)) >= 0:
                out.append(Span(i, i + 1, "PASSPORT", "1", source="llm"))
                start = i + 1
            return out

    text = "1. Предмет Договора\n1.1. Продавец передаёт участок в п. 1.2 Договора."
    res = Anonymizer([FakeLLM()]).anonymize(text)
    assert res.anonymized_text == text, res.anonymized_text
    assert res.mapping == {}, res.mapping


def test_short_number_dropped_when_no_review_layer_can_judge_it():
    """Без ревью спросить некого: «000» снимается, иначе оно разорвёт госномер
    («А [ADMIN_CODE_1] АА 00») и пробег («22 [ADMIN_CODE_1] км»)."""

    class FakeCode:
        def find(self, text):
            out, start = [], 0
            while (i := text.find("000", start)) >= 0:
                out.append(Span(i, i + 3, "ADMIN_CODE", "000", source="llm"))
                start = i + 3
            return out

    text = "Государственный регистрационный номер: А 000 АА 00. Пробег: 22 000 км."
    res = Anonymizer([FakeCode()]).anonymize(text)  # review_config=None
    assert res.anonymized_text == text, res.anonymized_text
    assert res.mapping == {}, res.mapping


def test_short_number_verdict_defaults_to_unmasking_when_llm_fails():
    """Умолчание для этого класса ОБРАТНОЕ обычному «сомневаешься — маскируй»:
    недоступная модель не должна возвращать баг, ломающий документ."""
    from anonymizer.review import ReviewConfig, _judge_short_numbers

    text = "Приложение N 12 к Договору"
    span = Span(13, 15, "ADMIN_CODE", "12", source="llm")
    # base_url заведомо нерабочий => исключение => умолчание
    cfg = ReviewConfig(base_url="http://127.0.0.1:1/v1", timeout=1.0)
    assert _judge_short_numbers(text, [span], cfg) == {id(span)}


def test_plate_detected_when_written_spaced_out():
    """«А 000 АА 00» в договорах пишут вразрядку — детектор не срабатывал, и
    номер уходил на откуп LLM, а в части прогонов утекал целиком."""
    from anonymizer.detectors import PLATE

    text = "Государственный регистрационный номер: А 000 АА 00."
    assert [s.text for s in PLATE.find(text)] == ["А 000 АА 00"]
    # слитная форма и три цифры региона тоже работают
    assert [s.text for s in PLATE.find("Госномер А123ВС77")] == ["А123ВС77"]
    assert [s.text for s in PLATE.find("номер Т001ТТ199,")] == ["Т001ТТ199"]
    # буквы вне разрешённого набора и разрозненные обрывки не ловятся
    assert PLATE.find("счёт К 123 АБ 45") == []
    assert PLATE.find("в п. 1 указано 000 и АА 00") == []


def test_short_number_kept_when_the_model_confirms_it():
    from anonymizer.review import ReviewConfig, _judge_short_numbers
    import anonymizer.review as review_mod

    text = "Код подразделения 777 указан в заявлении"
    span = Span(18, 21, "ADMIN_CODE", "777", source="llm")
    orig = review_mod._ask_short_numbers
    review_mod._ask_short_numbers = lambda lines, items, cfg: {"777": True}
    try:
        assert _judge_short_numbers(text, [span], ReviewConfig()) == set()
    finally:
        review_mod._ask_short_numbers = orig


def test_clause_numbering_survives_a_bogus_passport_digit():
    class FakeLLM:
        """Имитация детектора, пометившего номер пункта как паспорт."""

        def find(self, text):
            out = []
            start = 0
            while (i := text.find("1", start)) >= 0:
                out.append(Span(i, i + 1, "PASSPORT", "1", source="llm"))
                start = i + 1
            return out

    text = "1. Предмет Договора\n1.1. Продавец передаёт участок в п. 1.2 Договора."
    res = Anonymizer([FakeLLM()]).anonymize(text)
    assert res.anonymized_text == text, res.anonymized_text
    assert res.mapping == {}, res.mapping


# --- Падежные формы, осиротевшие после ревью (договор купли-продажи авто) ---
# Ревью корректно сняло маску с «Автомобиль», но порождённые им morph-спаны
# «Автомобиля»/«Автомобилю» на пересмотр не отдаются и оставались
# замаскированными — обычное слово превращалось в [PERSON_8] в 18 местах.

def test_orphaned_declensions_dropped_with_their_reviewed_parent():
    from anonymizer.review import _is_orphaned_derivative, _stems_of_dropped
    from anonymizer.review import _Candidate

    candidates = {"PERSON\x00автомобиль": _Candidate("PERSON", "Автомобиль", "…")}
    stems = _stems_of_dropped(candidates, {"PERSON\x00автомобиль": False})

    for form in ("Автомобиля", "Автомобилю", "Автомобилем"):
        span = Span(0, len(form), "PERSON", form, source="morph")
        assert _is_orphaned_derivative(span, stems), form


def test_kept_parent_leaves_its_declensions_masked():
    from anonymizer.review import _is_orphaned_derivative, _stems_of_dropped
    from anonymizer.review import _Candidate

    candidates = {"PERSON\x00иванов": _Candidate("PERSON", "Иванов", "…")}
    stems = _stems_of_dropped(candidates, {"PERSON\x00иванов": True})
    span = Span(0, 8, "PERSON", "Иванову", source="morph")
    assert not _is_orphaned_derivative(span, stems)


def test_unrelated_and_non_derived_spans_are_never_dropped():
    from anonymizer.review import _is_orphaned_derivative, _stems_of_dropped
    from anonymizer.review import _Candidate

    candidates = {"PERSON\x00автомобиль": _Candidate("PERSON", "Автомобиль", "…")}
    stems = _stems_of_dropped(candidates, {"PERSON\x00автомобиль": False})
    # чужое значение
    assert not _is_orphaned_derivative(
        Span(0, 6, "PERSON", "Иванов", source="morph"), stems)
    # та же основа, но метка другая
    assert not _is_orphaned_derivative(
        Span(0, 10, "ORG", "Автомобиля", source="morph"), stems)
    # детерминированный источник — не производная, трогать нельзя
    assert not _is_orphaned_derivative(
        Span(0, 10, "PERSON", "Автомобиля", source="regex"), stems)


if __name__ == "__main__":
    import sys

    mod = sys.modules[__name__]
    for name in sorted(dir(mod)):
        if name.startswith("test_"):
            getattr(mod, name)()
            print(f"ok {name}")
    print("all tests passed")
