"""Tests for the Natasha NER detector. Skipped cleanly if natasha is absent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _has_natasha() -> bool:
    try:
        import natasha  # noqa: F401
    except Exception:
        return False
    return True


def test_ner_finds_person_and_location():
    if not _has_natasha():
        print("SKIP test_ner_finds_person_and_location (natasha not installed)")
        return
    from anonymizer.ner import NatashaDetector

    spans = NatashaDetector().find("Ефимов Данил Маратович живёт в Москве.")
    labels = {s.label for s in spans}
    assert "PERSON" in labels
    assert "LOCATION" in labels


def test_hybrid_roundtrip_with_names():
    if not _has_natasha():
        print("SKIP test_hybrid_roundtrip_with_names (natasha not installed)")
        return
    from anonymizer import build_anonymizer, deanonymize

    a = build_anonymizer(use_ner=True)
    text = "Иванов Пётр из Казани, ИНН 7711222333."
    res = a.anonymize(text)
    assert "Иванов" not in res.anonymized_text
    assert "7711222333" not in res.anonymized_text
    assert deanonymize(res.anonymized_text, res.mapping) == text


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
