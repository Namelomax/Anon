"""Тесты пользовательского глоссария и склонение-пропагации.

Покрывают два ранее не тестированных механизма:
  * GlossaryDetector — всегда-маскируемые термины (аббревиатуры/жаргон/обычные
    слова, которые NER не распознаёт как ПДн);
  * propagate_declensions — добор косвенных падежей для сущностей, пойманных
    NER/LLM в произвольной форме.
"""

from anonymizer.glossary import (
    GlossaryDetector,
    GlossaryEntry,
    parse_glossary_text,
)
from anonymizer.detectors import propagate_declensions
from anonymizer.spans import Span


def _texts(detector, text):
    return sorted(s.text for s in detector.find(text))


def test_parse_basic_and_malformed():
    entries = parse_glossary_text(
        "# коммент\n"
        "Мингос, МГУ = Министерство государственного управления\n"
        "Минфин = Министерство финансов\n"
        "\n"
        "сломанная строка без равно\n"
        " = только правая часть\n"
    )
    assert len(entries) == 2
    assert entries[0].aliases == ("Мингос", "МГУ")
    assert entries[0].canonical == "Министерство государственного управления"
    assert entries[1].canonical == "Министерство финансов"


def test_masc_consonant_all_cases():
    det = GlossaryDetector([GlossaryEntry("Министерство финансов", ("Минфин",))])
    text = "Минфин против. На стороне Минфина. С Минфином согласовали. Дать Минфину. О Минфине."
    hits = _texts(det, text)
    for form in ("Минфин", "Минфина", "Минфином", "Минфину", "Минфине"):
        assert form in hits, f"пропущено: {form}"


def test_neuter_oblique_cases_the_regression():
    # Главный кейс: «Правительство» в косвенных падежах, что старая реализация теряла.
    det = GlossaryDetector([GlossaryEntry("Правительство", ("Правительство",))])
    text = (
        "поменять на правительство, чтобы правительство обменивалось, "
        "могли обмениваться с правительством, у правительства нет, дать правительству"
    )
    found = {s.text.lower() for s in det.find(text)}
    for form in ("правительство", "правительством", "правительства", "правительству"):
        assert form in found, f"косвенная форма пропущена (утечка): {form}"


def test_feminine_oblique_cases():
    det = GlossaryDetector([GlossaryEntry("Лента", ("Лента",))])
    text = "Договор с Лентой. У Ленты филиалы. Ленте передали. Про Ленту знают."
    found = {s.text for s in det.find(text)}
    for form in ("Лентой", "Ленты", "Ленте", "Ленту"):
        assert form in found, f"пропущено: {form}"


def test_no_false_match_inside_other_words():
    det = GlossaryDetector([
        GlossaryEntry("Правительство", ("Правительство",)),
        GlossaryEntry("Лента", ("Лента",)),
    ])
    text = "правительственный указ, ленточный конвейер, лентяй ушёл"
    assert det.find(text) == []


def test_aliases_share_one_merge_key():
    det = GlossaryDetector([
        GlossaryEntry("Министерство государственного управления", ("Мингос", "МГУ")),
    ])
    spans = det.find("Мингос и МГУ — это одно и то же")
    keys = {s.merge_key for s in spans}
    assert len(spans) == 2
    assert len(keys) == 1  # оба алиаса → один плейсхолдер
    assert all(s.canonical_text == "Министерство государственного управления" for s in spans)


# --- propagate_declensions ---

def _person(text_full, surface):
    start = text_full.find(surface)
    return Span(start, start + len(surface), "ORG", surface, source="ner")


def test_propagate_from_oblique_first_catch():
    # NER поймал «Мингосом» (творительный) первым — добор должен найти «Мингоса».
    text = "со стороны с Мингосом спрашивали, что у Мингоса, что у нас"
    span = _person(text, "Мингосом")
    extra = propagate_declensions(text, [span])
    forms = {e.text for e in extra}
    assert "Мингоса" in forms


def test_propagate_does_not_shrink_surname():
    # Фамилия «Иванов» не должна ужиматься до «Иван» (окончания -ов/-ев исключены).
    text = "Иванов пришёл. Иванову передали. Об Иванове говорили."
    span = _person(text, "Иванов")
    extra = propagate_declensions(text, [span])
    assert all(e.text != "Иван" for e in extra)
    assert all(not e.text.startswith("Иван ") for e in extra)
