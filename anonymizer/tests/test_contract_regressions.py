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

def test_bare_digits_are_noise_under_every_label():
    for label in ("PASSPORT", "EMAIL", "SUBJECT", "PERSON", "ORG", "SENSITIVE",
                  "ADMIN_CODE"):
        for value in ("1", "12", "000"):
            assert is_noise_span(value, label), (label, value)


def test_meaningful_numbers_survive_the_digit_filter():
    # порог не должен задевать настоящие значения
    for value in ("2014", "000000", "044525225", "50:20:123456:21", "777-003"):
        assert not is_noise_span(value, "PASSPORT"), value


def test_short_number_does_not_fragment_a_larger_identifier():
    """«000» под ADMIN_CODE маскировалось во всех вхождениях и разрывало
    госномер («А [ADMIN_CODE_1] АА 00») и пробег («22 [ADMIN_CODE_1] км»)."""

    class FakeCode:
        def find(self, text):
            out, start = [], 0
            while (i := text.find("000", start)) >= 0:
                out.append(Span(i, i + 3, "ADMIN_CODE", "000", source="llm"))
                start = i + 3
            return out

    text = "Государственный регистрационный номер: А 000 АА 00. Пробег: 22 000 км."
    res = Anonymizer([FakeCode()]).anonymize(text)
    assert res.anonymized_text == text, res.anonymized_text
    assert res.mapping == {}, res.mapping


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
