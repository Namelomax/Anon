"""Offline tests for the LLM detector's parsing/locating logic (no server)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer.llm import (  # noqa: E402
    LLMDetector,
    _extract_json_array,
    _find_all,
    _normalize_type,
    _parse_items,
)


def test_extract_json_array_from_fenced_block():
    s = 'Sure, here:\n```json\n[{"text": "Иван", "type": "PERSON"}]\n```'
    assert _extract_json_array(s) == '[{"text": "Иван", "type": "PERSON"}]'


def test_extract_json_array_returns_last_candidate():
    s = '[{"text": "draft"}] then final [{"text": "real", "type": "INN"}]'
    assert '"real"' in _extract_json_array(s)


def test_extract_json_array_respects_brackets_in_strings():
    s = '[{"text": "a[b]c", "type": "PERSON"}]'
    assert _extract_json_array(s) == s


def test_extract_json_array_none_when_absent():
    assert _extract_json_array("no json here") is None


def test_parse_items_filters_malformed():
    content = '[{"text":"Иван","type":"PERSON"},{"type":"INN"},{"text":"","type":"X"},42]'
    assert _parse_items(content) == [("Иван", "PERSON")]


def test_find_all_exact_multiple():
    text = "a@b.ru важна. снова a@b.ru."
    assert _find_all(text, "a@b.ru") == [(0, 6), (20, 26)]


def test_find_all_whitespace_insensitive_fallback():
    text = "почта n . makarov @ aol . com тут"
    spans = _find_all(text, "n . makarov @ aol .  com")  # extra spaces
    assert len(spans) == 1
    s, e = spans[0]
    assert text[s:e] == "n . makarov @ aol . com"


def test_find_all_missing_returns_empty():
    assert _find_all("hello world", "nonexistent") == []


def test_normalize_type_maps_families():
    assert _normalize_type("first_name") == "PERSON"
    assert _normalize_type("CITY") == "LOCATION"
    assert _normalize_type("SNILS") == "SNILS"


def test_detector_locate_builds_spans_without_server():
    text = "Меня зовут Иван, ИНН 500100732259."
    det = LLMDetector()
    # bypass HTTP: feed a canned model reply through the real parse/locate path
    det._complete = lambda t: '[{"text":"Иван","type":"PERSON"},{"text":"500100732259","type":"INN"}]'
    spans = det.find(text)
    by_label = {s.label: text[s.start : s.end] for s in spans}
    assert by_label == {"PERSON": "Иван", "INN": "500100732259"}
    assert all(s.source == "llm" for s in spans)


def test_detector_drops_unlocatable_hallucination():
    text = "Меня зовут Иван."
    det = LLMDetector()
    det._complete = lambda t: '[{"text":"Пётр Сидоров","type":"PERSON"}]'  # not in text
    assert det.find(text) == []


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
