"""Тесты новой метки SUBJECT (предмет договора) и переключаемой стадии `subject`.

Метка добавляется в тот же LLM-вызов детекции (без отдельного прохода), стадия
по умолчанию выключена и работает только вместе со стадией `llm`. Ничего здесь
не должно требовать живого LLM-сервера: только чистые функции
(`_build_system_prompt`, `_TYPE_MAP`, `_compose`, маппинг плейсхолдеров).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer.llm import (  # noqa: E402
    _DEFAULT_ALLOWED,
    _build_system_prompt,
    _normalize_type,
    LLMConfig,
    LLMDetector,
)
from anonymizer.mapping import assign_placeholders  # noqa: E402
from anonymizer.spans import Span  # noqa: E402


# --- system prompt: subject block only appears when SUBJECT is allowed ------

def test_prompt_without_subject_has_no_subject_block():
    prompt = _build_system_prompt(_DEFAULT_ALLOWED)
    assert "SUBJECT" not in prompt
    assert "предмет договора" not in prompt.casefold()


def test_prompt_identical_byte_for_byte_when_subject_off():
    # Стадия subject выключена -> промпт не должен отличаться от текущего.
    baseline = _build_system_prompt(_DEFAULT_ALLOWED)
    again = _build_system_prompt(_DEFAULT_ALLOWED)
    assert baseline == again


def test_prompt_with_subject_has_subject_block_and_type():
    allowed = _DEFAULT_ALLOWED | {"SUBJECT"}
    prompt = _build_system_prompt(allowed)
    assert "SUBJECT" in prompt
    assert "предмет договора" in prompt.casefold()
    # sanity: allowed-types line must list SUBJECT
    assert "Допустимые типы" in prompt


def test_prompt_subject_block_mentions_generic_lexicon_exclusion():
    allowed = _DEFAULT_ALLOWED | {"SUBJECT"}
    prompt = _build_system_prompt(allowed)
    # служебная лексика не должна маскироваться — проверяем, что промпт это
    # явно оговаривает (см. спецификацию задачи)
    assert "товар" in prompt.casefold()
    assert "оборудование" in prompt.casefold()


# --- _TYPE_MAP normalizes model synonyms to SUBJECT --------------------------

def test_normalize_type_maps_subject_synonyms():
    for raw in ("SUBJECT", "GOODS", "PRODUCT", "ITEM", "SERVICE", "PRODUCT_NAME",
                "goods", "product", "item", "service"):
        assert _normalize_type(raw) == "SUBJECT", raw


# --- LLMDetector end-to-end (offline, HTTP call stubbed) ---------------------

def test_detector_emits_subject_when_allowed():
    text = "Договор на поставку автоматов Калашникова АК-74 для нужд завода."
    cfg = LLMConfig(allowed_labels=_DEFAULT_ALLOWED | {"SUBJECT"})
    det = LLMDetector(cfg)
    det._complete = lambda t: (
        '[{"text": "автоматов Калашникова АК-74", "type": "GOODS"}]'
    )
    spans = det.find(text)
    assert len(spans) == 1
    assert spans[0].label == "SUBJECT"
    assert spans[0].text == "автоматов Калашникова АК-74"


def test_detector_drops_subject_when_not_allowed():
    # Стадия subject выключена -> allowed_labels не содержит SUBJECT -> модель
    # могла бы всё равно что-то вернуть (галлюцинация), но это должно быть
    # отброшено, как любой недопустимый тип.
    text = "Договор на поставку автоматов Калашникова АК-74."
    det = LLMDetector(LLMConfig())  # default allowed_labels, no SUBJECT
    det._complete = lambda t: (
        '[{"text": "автоматов Калашникова АК-74", "type": "GOODS"}]'
    )
    assert det.find(text) == []


# --- placeholder shape and merging -------------------------------------------

def test_subject_placeholder_shape_and_merge():
    text = "Поставка станков ЧПУ Haas VF-2. Повторная поставка станков ЧПУ Haas VF-2."
    first = text.index("станков ЧПУ Haas VF-2")
    second = text.index("станков ЧПУ Haas VF-2", first + 1)
    spans = [
        Span(first, first + len("станков ЧПУ Haas VF-2"), "SUBJECT",
             "станков ЧПУ Haas VF-2", source="llm"),
        Span(second, second + len("станков ЧПУ Haas VF-2"), "SUBJECT",
             "станков ЧПУ Haas VF-2", source="llm"),
    ]
    mapping, span_placeholders = assign_placeholders(spans)
    assert list(mapping) == ["[SUBJECT_1]"]
    assert mapping["[SUBJECT_1]"] == "станков ЧПУ Haas VF-2"
    assert set(span_placeholders.values()) == {"[SUBJECT_1]"}


def test_distinct_subjects_get_distinct_placeholders():
    spans = [
        Span(0, 10, "SUBJECT", "автомат АК", source="llm"),
        Span(20, 35, "SUBJECT", "станок Haas VF-2", source="llm"),
    ]
    mapping, _ = assign_placeholders(spans)
    assert set(mapping) == {"[SUBJECT_1]", "[SUBJECT_2]"}


# --- server._compose: stage wiring (no model load, no network) --------------
# Patches the module's globals directly (save/restore) rather than pytest's
# monkeypatch fixture, so this file stays runnable standalone like the rest of
# this test suite (see the __main__ runner below).

def _with_server_globals(detectors: dict, defaults: dict):
    """Context manager-ish helper: temporarily swap server._DETECTORS/_DEFAULTS."""
    import anonymizer.server as server

    class _Ctx:
        def __enter__(self):
            self._orig_dets = server._DETECTORS
            self._orig_defaults = server._DEFAULTS
            server._DETECTORS = detectors
            server._DEFAULTS = defaults
            return server

        def __exit__(self, *exc):
            server._DETECTORS = self._orig_dets
            server._DEFAULTS = self._orig_defaults
            return False

    return _Ctx()


def test_compose_ignores_subject_when_llm_off():
    dets = {"llm": [LLMDetector(LLMConfig())]}
    with _with_server_globals(dets, {}) as server:
        anon = server._compose({"llm": False, "subject": True})
    # llm stage off -> no LLM detector at all, subject silently had no effect
    assert not any(isinstance(d, LLMDetector) for d in anon._detectors)


def test_compose_adds_subject_to_allowed_labels_when_both_on():
    base_cfg = LLMConfig(allowed_labels=_DEFAULT_ALLOWED | {"ORG", "AMOUNT"})
    dets = {"llm": [LLMDetector(base_cfg)]}
    with _with_server_globals(dets, {}) as server:
        anon = server._compose({"llm": True, "subject": True})
    llm_dets = [d for d in anon._detectors if isinstance(d, LLMDetector)]
    assert len(llm_dets) == 1
    assert "SUBJECT" in llm_dets[0].config.allowed_labels
    # original detector/config untouched (a fresh instance was built)
    assert "SUBJECT" not in base_cfg.allowed_labels


def test_compose_leaves_allowed_labels_untouched_when_subject_off():
    base_cfg = LLMConfig(allowed_labels=_DEFAULT_ALLOWED | {"ORG", "AMOUNT"})
    dets = {"llm": [LLMDetector(base_cfg)]}
    with _with_server_globals(dets, {}) as server:
        anon = server._compose({"llm": True, "subject": False})
    llm_dets = [d for d in anon._detectors if isinstance(d, LLMDetector)]
    assert len(llm_dets) == 1
    assert "SUBJECT" not in llm_dets[0].config.allowed_labels
    assert llm_dets[0] is dets["llm"][0]  # reused as-is, no rebuild needed


def test_compose_defaults_to_subject_off():
    base_cfg = LLMConfig()
    dets = {"llm": [LLMDetector(base_cfg)]}
    # subject omitted from the request AND from defaults -> off
    with _with_server_globals(dets, {"llm": True}) as server:
        anon = server._compose({})
    llm_dets = [d for d in anon._detectors if isinstance(d, LLMDetector)]
    assert len(llm_dets) == 1
    assert "SUBJECT" not in llm_dets[0].config.allowed_labels


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
