"""Word-level tests for the deterministic PERSON un-mask gate (detectors.py:
``is_person_word_droppable`` / ``can_unmask_person`` / ``morph_gate_available``).

See review.py's module docstring ("PERSON keep=false is additionally vetoed
by a DETERMINISTIC morphology check") for why this exists: the LLM review
layer reliably drops rare/unfamiliar surnames as if they were ordinary words
("Вайгус" is this project's canonical case), even though its own prompt tells
it not to. This gate is a second, code-level opinion the model cannot argue
its way around.

Two independent paths make a word droppable:
  (a) dictionary path — known word, no parse tagged Name/Surn/Patr.
  (b) structural-garbage path — unknown to the dictionary AND structurally
      impossible as a Russian name (no vowel, repeated-letter filler, a
      digit/stray symbol, too short/long, or a <=3-char ALL-CAPS token).
Neither path needs a word list; both fall to natasha's ``MorphVocab``
(pymorphy2/OpenCorpora dictionaries) or plain string shape.

No live upstream calls anywhere in this file — everything here is a pure
function over natasha's local, offline dictionary.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402

from anonymizer import detectors  # noqa: E402
from anonymizer.detectors import (  # noqa: E402
    can_unmask_person,
    is_person_word_droppable,
    morph_gate_available,
)


def test_dictionary_gate_is_available_in_this_environment():
    # Sanity check: every other test in this file assumes natasha's
    # dictionaries actually loaded. If this fails, the whole suite's premise
    # (a real dictionary distinguishes names from words) can't be exercised.
    assert morph_gate_available() is True


# --- (a) real surnames/names — MUST stay refused, in any casing --------------

@pytest.mark.parametrize(
    "word",
    ["Вайгус", "Яков", "Дронова", "Смирнов", "Иванов", "ИВАНОВ", "СМИРНОВ"],
)
def test_real_names_and_surnames_refused(word):
    allowed, why = is_person_word_droppable(word)
    assert allowed is False, f"{word!r} should refuse the drop, got why={why!r}"


def test_hyphenated_double_barrelled_surname_refused():
    """«Римский-Корсаков»: the hyphen must NOT be read as a garbage signal —
    both fragments are individually name-plausible, so this must stay
    refused rather than be waved through as noise."""
    allowed, why = is_person_word_droppable("Римский-Корсаков")
    assert allowed is False, f"should refuse, got why={why!r}"


def test_short_but_vowel_bearing_unknown_word_refused():
    """«Ивэ»: unknown to the dictionary, but NOT structurally impossible (it
    has a vowel and isn't unreasonably short/long/garbled) — over-masking a
    rare/unknown short name is acceptable, unmasking a real one is not."""
    allowed, why = is_person_word_droppable("Ивэ")
    assert allowed is False, f"should refuse, got why={why!r}"


# --- (a) ordinary Russian words mis-tagged as PERSON — MUST be allowed -------

@pytest.mark.parametrize("word", ["День", "Спикер", "Решение", "Команда"])
def test_ordinary_dictionary_words_allowed_via_dict_path(word):
    allowed, why = is_person_word_droppable(word)
    assert allowed is True, f"{word!r} should be droppable, got why={why!r}"
    assert why == "dict"


# --- (b) structural garbage — MUST be allowed even though unknown/abbrev ----

@pytest.mark.parametrize("word", ["Ааа", "Ммм", "Эээ", "Тк", "Ннн"])
def test_transcription_garbage_allowed(word):
    allowed, why = is_person_word_droppable(word)
    assert allowed is True, f"{word!r} should be droppable, got why={why!r}"


@pytest.mark.parametrize("word", ["ТЗ", "МП"])
def test_short_uppercase_abbreviations_allowed(word):
    allowed, why = is_person_word_droppable(word)
    assert allowed is True, f"{word!r} should be droppable, got why={why!r}"


def test_glued_digit_hyphen_fragment_allowed_as_garbage():
    """«Ив-2»: contains a digit — one fragment ("2") is definitionally
    garbage and the other ("Ив") is too short (len<=2) to be name-plausible,
    so the whole hyphenated token is garbage."""
    allowed, why = is_person_word_droppable("Ив-2")
    assert allowed is True, f"should be droppable, got why={why!r}"
    assert why == "garbage"


def test_all_caps_is_only_a_garbage_signal_at_length_le_3():
    """The trap explicitly called out: a real ALL-CAPS surname (document
    headers write names this way) must NOT be treated as an abbreviation
    just because it's uppercase — only length <=3 counts."""
    allowed_short, why_short = is_person_word_droppable("АКТ")  # len 3, ordinary word too
    assert allowed_short is True
    allowed_long, why_long = is_person_word_droppable("ИВАНОВ")  # len 6, real surname
    assert allowed_long is False, f"should refuse, got why={why_long!r}"


# --- Latin script: out of scope, vacuously allowed (today's behaviour) ------

@pytest.mark.parametrize("value", ["Telegram", "Skype", "ERP"])
def test_latin_script_candidates_are_out_of_scope(value):
    allowed, verdicts = can_unmask_person(value)
    assert allowed is True
    assert verdicts == []  # nothing was examined — no Cyrillic-capitalised word


# --- Multi-word candidates: one blocking word refuses the whole thing -------

def test_mixed_candidate_refused_because_one_word_is_a_name():
    allowed, verdicts = can_unmask_person("Капитан Яков")
    assert allowed is False
    by_word = {w: (ok, why) for w, ok, why in verdicts}
    assert by_word["Капитан"][0] is True   # ordinary word, would be fine alone
    assert by_word["Яков"][0] is False     # but "Яков" blocks the whole candidate


def test_all_ordinary_multiword_candidate_allowed():
    allowed, verdicts = can_unmask_person("Добрый День")
    assert allowed is True
    assert {w for w, _ok, _why in verdicts} == {"Добрый", "День"}


# --- Fail-safe: morphology unavailable -> refuse everything ------------------

def test_morphology_unavailable_refuses_single_word(monkeypatch):
    monkeypatch.setattr(detectors, "_morph_vocab", lambda: None)
    assert morph_gate_available() is False
    allowed, why = is_person_word_droppable("День")  # an otherwise-ordinary word
    assert allowed is False
    assert "unavailable" in why


def test_morphology_unavailable_refuses_whole_candidate_with_empty_verdicts(monkeypatch):
    monkeypatch.setattr(detectors, "_morph_vocab", lambda: None)
    allowed, verdicts = can_unmask_person("День")
    assert allowed is False
    assert verdicts == []  # nothing WAS checked — distinguishes this from a real refusal


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else str(failures) + ' FAILURE(S)'}")
    sys.exit(1 if failures else 0)
