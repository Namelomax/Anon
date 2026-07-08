"""Канонизация одинаковых сущностей (падежи + метки) в один плейсхолдер."""

from anonymizer.canonicalize import canonicalize_entities, group_key
from anonymizer.mapping import assign_placeholders
from anonymizer.spans import Span


def _mk(items):
    spans, pos = [], 0
    for it in items:
        label, text = it[0], it[1]
        mk = it[2] if len(it) > 2 else None
        spans.append(Span(pos, pos + len(text), label, text, source="ner", merge_key=mk))
        pos += len(text) + 1
    return spans


def test_group_key_declensions():
    assert group_key("Форус") == group_key("Форуса")
    assert group_key("Телеграм") == group_key("Телеграме")


def test_collapse_org_declensions():
    out = canonicalize_entities(_mk([("ORG", "Форус"), ("ORG", "Форуса"), ("ORG", "Форус")]))
    keys = {s.merge_key for s in out}
    assert len(keys) == 1 and None not in keys
    assert all(s.canonical_text == "Форус" for s in out)
    mapping, _ = assign_placeholders(out)
    assert len(mapping) == 1
    assert list(mapping.values()) == ["Форус"]


def test_cross_label_org_location():
    out = canonicalize_entities(_mk([("ORG", "Телеграме"), ("LOCATION", "Телеграме")]))
    assert out[0].merge_key == out[1].merge_key
    mapping, _ = assign_placeholders(out)
    assert len(mapping) == 1


def test_person_not_grouped():
    out = canonicalize_entities(_mk([("PERSON", "Иванов"), ("PERSON", "Иванова")]))
    assert all(s.merge_key is None for s in out)
    mapping, _ = assign_placeholders(out)
    assert len(mapping) == 2


def test_existing_merge_key_preserved():
    out = canonicalize_entities(_mk([("ORG", "Вайгус", "REVIEW\x00cap"), ("ORG", "Форус")]))
    assert out[0].merge_key == "REVIEW\x00cap"


def test_distinct_orgs_stay_separate():
    out = canonicalize_entities(_mk([("ORG", "Форус"), ("ORG", "Оптима")]))
    mapping, _ = assign_placeholders(out)
    assert len(mapping) == 2
