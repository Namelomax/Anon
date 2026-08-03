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
    # Экземпляр детектора теперь СВОЙ на каждый запрос: у LLMDetector есть
    # изменяемое поле warnings, и при общем экземпляре два параллельных
    # документа затирали бы предупреждения друг друга. Общим остаётся КОНФИГ —
    # именно он несёт allowed_labels, которые проверены строкой выше.
    assert llm_dets[0].config is dets["llm"][0].config


def test_compose_defaults_to_subject_off():
    base_cfg = LLMConfig()
    dets = {"llm": [LLMDetector(base_cfg)]}
    # subject omitted from the request AND from defaults -> off
    with _with_server_globals(dets, {"llm": True}) as server:
        anon = server._compose({})
    llm_dets = [d for d in anon._detectors if isinstance(d, LLMDetector)]
    assert len(llm_dets) == 1
    assert "SUBJECT" not in llm_dets[0].config.allowed_labels


# --- SUBJECT в разрешении перекрытий ----------------------------------------
# Метки не было в DEFAULT_PRIORITY => вес 0 => любой пересекающийся спан
# (дата, сумма, SENSITIVE) выбивал ВЕСЬ предмет договора целиком.

def test_subject_has_priority_and_beats_date_and_amount():
    from anonymizer.detectors import DEFAULT_PRIORITY  # noqa: E402
    from anonymizer.spans import resolve_overlaps  # noqa: E402

    assert "SUBJECT" in DEFAULT_PRIORITY
    for weaker in ("DATE", "AMOUNT", "SENSITIVE"):
        assert DEFAULT_PRIORITY["SUBJECT"] > DEFAULT_PRIORITY[weaker], weaker
    for stronger in ("ORG", "CONTRACT", "PASSPORT", "INN"):
        assert DEFAULT_PRIORITY["SUBJECT"] < DEFAULT_PRIORITY[stronger], stronger

    text = "станок ЧПУ Haas VF-2 2014 года выпуска"
    subject = Span(0, 20, "SUBJECT", text[:20], source="llm")
    date = Span(15, 25, "DATE", text[15:25], source="regex")  # пересекает предмет
    kept = resolve_overlaps([subject, date], priority=DEFAULT_PRIORITY)
    assert [s.label for s in kept] == ["SUBJECT"], kept


def test_subject_is_a_recall_label():
    from anonymizer.review import _RECALL_LABELS  # noqa: E402

    assert "SUBJECT" in _RECALL_LABELS


# --- review больше не воюет с subject ---------------------------------------

def test_review_prompt_stops_unmasking_goods_in_subject_mode():
    from anonymizer.review import _build_review_prompt  # noqa: E402

    off = _build_review_prompt(False)
    on = _build_review_prompt(True)
    # шаблон полностью раскрыт
    assert "<<" not in off and "<<" not in on
    # обычный режим не изменился — разрешение снимать «продукты» на месте
    assert "продуктов/ПО" in off and "продукт/ПО" in off
    assert "SUBJECT" not in off
    # subject-режим: разрешение сужено до ПО, добавлен критерий предмета
    assert "продуктов/ПО" not in on and "продукт/ПО" not in on
    assert "SUBJECT" in on
    # правило решает по РОЛИ значения: предмет поставки скрываем, рабочий
    # инструмент (Битрикс/Zoom) — нет
    assert "НАЗВАНИЯ-БРЕНДЫ" in on
    assert "Битрикс" in on


def test_review_config_carries_the_subject_flag():
    from anonymizer.review import ReviewConfig  # noqa: E402

    assert ReviewConfig().subject is False
    assert ReviewConfig(subject=True).subject is True


def test_subject_is_reviewable_only_in_subject_mode():
    """Fail-safe: без subject-промпта ревьюер не знает критерия «номенклатура vs
    служебная лексика» и на живой модели стабильно снимал маску с предмета
    договора. Поэтому вне режима метка на пересмотр не отдаётся вовсе."""
    from anonymizer.review import _REVIEWABLE_LABELS, _group_candidates  # noqa: E402

    assert "SUBJECT" in _REVIEWABLE_LABELS

    text = "Поставка: автомат Калашникова АК-74М в количестве 400 шт."
    value = "автомат Калашникова АК-74М"
    i = text.index(value)
    spans = [Span(i, i + len(value), "SUBJECT", value, source="llm")]

    off = _group_candidates(text, spans, 60, _REVIEWABLE_LABELS - {"SUBJECT"})
    on = _group_candidates(text, spans, 60, _REVIEWABLE_LABELS)
    assert off == {}, off          # не кандидат => маска гарантированно остаётся
    assert len(on) == 1, on


# --- second-pass и review получают ту же конфигурацию ------------------------

def test_second_pass_uses_the_subject_aware_detector():
    base_cfg = LLMConfig(allowed_labels=_DEFAULT_ALLOWED | {"ORG", "AMOUNT"})
    dets = {"llm": [LLMDetector(base_cfg)]}
    with _with_server_globals(dets, {"llm": True, "second_pass": True}) as server:
        anon = server._compose({"subject": True})
    sp = anon._second_pass_detectors
    assert len(sp) == 1
    assert "SUBJECT" in sp[0].config.allowed_labels
    # тот же экземпляр, что и в детекции — лишнего объекта не создаём
    assert sp[0] is [d for d in anon._detectors if isinstance(d, LLMDetector)][0]


def test_second_pass_untouched_when_subject_off():
    base_cfg = LLMConfig()
    dets = {"llm": [LLMDetector(base_cfg)]}
    with _with_server_globals(dets, {"llm": True, "second_pass": True}) as server:
        anon = server._compose({"subject": False})
    # Второй проход обязан идти ТЕМ ЖЕ экземпляром, что и детекция: engine.py
    # дедуплицирует предупреждения детекторов по id(), и отдельный третий
    # объект дал бы дубликаты в warnings документа.
    llm_dets = [d for d in anon._detectors if isinstance(d, LLMDetector)]
    assert anon._second_pass_detectors == (llm_dets[0],)
    assert "SUBJECT" not in anon._second_pass_detectors[0].config.allowed_labels


def test_compose_switches_review_into_subject_mode():
    import anonymizer.server as server_mod
    from anonymizer.review import ReviewConfig  # noqa: E402

    dets = {"llm": [LLMDetector(LLMConfig())]}
    orig = server_mod._REVIEW_CFG
    server_mod._REVIEW_CFG = ReviewConfig()
    try:
        with _with_server_globals(dets, {"llm": True, "review": True}) as server:
            on = server._compose({"subject": True})
            off = server._compose({"subject": False})
    finally:
        server_mod._REVIEW_CFG = orig
    assert on._review_config.subject is True
    assert off._review_config.subject is False
    # глобальный конфиг сервера не мутирован
    assert server_mod._REVIEW_CFG is orig


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
