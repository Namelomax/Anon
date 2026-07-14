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


if __name__ == "__main__":
    import sys

    mod = sys.modules[__name__]
    for name in sorted(dir(mod)):
        if name.startswith("test_"):
            getattr(mod, name)()
            print(f"ok {name}")
    print("all tests passed")
