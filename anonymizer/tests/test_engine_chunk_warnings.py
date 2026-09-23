"""Предупреждения о необработанных кусках: чей проход, тот и вид.

Один и тот же экземпляр LLM-детектора работает и в основном проходе, и в
leak-скане по уже замаскированному тексту (см. build_anonymizer и
server._build_pipeline), а ``find()`` очищает ``detector.warnings`` в начале
каждого вызова. Отсюда два требования к engine.anonymize:

* предупреждения ПЕРВОГО прохода не должны теряться под вторым — иначе
  реально непроверенный кусок исчезает из отчёта;
* предупреждения ВТОРОГО прохода не должны выдаваться за первые — основной
  проход этот кусок разобрал, не завершилась лишь перепроверка, и смещения в
  них посчитаны по промежуточному тексту, а не по оригиналу.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from anonymizer.engine import Anonymizer  # noqa: E402
from anonymizer.spans import Span  # noqa: E402

TEXT = "Иван Петров и отчёт о встрече по вопросу поставки товара."
# Насколько оригинал длиннее промежуточного текста: «Иван Петров» (11) против
# «[PERSON_1]» (10). Ровно на столько же смещения второго прохода отстают от
# оригинальных.
SHIFT = len("Иван Петров") - len("[PERSON_1]")


class _FakeLLMDetector:
    """Повторяет контракт LLMDetector в части warnings.

    ``find()`` очищает список в начале вызова — ровно как настоящий детектор
    (см. llm.LLMDetector.find); сценарий задаёт, что вернуть и на что
    пожаловаться в каждом из проходов.
    """

    def __init__(self, script: list[tuple[list[Span], list[dict]]]) -> None:
        self.warnings: list[dict] = []
        self._script = list(script)
        self.calls: list[str] = []

    def find(self, text: str) -> list[Span]:
        self.warnings = []
        self.calls.append(text)
        spans, warnings = self._script.pop(0)
        self.warnings.extend(warnings)
        return list(spans)


def _anonymize_with(script):
    detector = _FakeLLMDetector(script)
    # Тот же экземпляр в обоих списках — как его ставит build_anonymizer.
    anon = Anonymizer([detector], second_pass_detectors=[detector])
    return detector, anon.anonymize(TEXT)


def test_first_pass_warning_survives_the_leak_scan():
    first = {"kind": "llm_chunk_failed", "offset": 0, "chars": 30, "message": "не разобран"}
    detector, res = _anonymize_with(
        [
            ([Span(0, 11, "PERSON", "Иван Петров", source="llm")], [first]),
            ([], []),  # leak-скан отработал чисто и очистил detector.warnings
        ]
    )
    assert detector.calls, "детектор должен быть вызван обоими проходами"
    assert [w["kind"] for w in res.warnings] == ["llm_chunk_failed"]
    assert res.warnings[0]["offset"] == 0
    assert res.warnings[0]["chars"] == 30


def test_leak_scan_failure_is_reported_as_a_recheck_not_as_unchecked():
    _, res = _anonymize_with(
        [
            ([Span(0, 11, "PERSON", "Иван Петров", source="llm")], []),
            ([], [{"kind": "llm_chunk_failed", "offset": 20, "chars": 10, "message": "не разобран"}]),
        ]
    )
    assert [w["kind"] for w in res.warnings] == ["recheck_chunk_failed"]
    # Смещения второго прохода посчитаны по промежуточному тексту — в отчёте
    # они должны указывать на оригинал.
    assert res.warnings[0]["offset"] == 20 + SHIFT
    assert res.warnings[0]["chars"] == 10
    assert "перепроверка" in res.warnings[0]["message"].lower()


def test_both_passes_fail_and_both_are_reported_separately():
    _, res = _anonymize_with(
        [
            (
                [Span(0, 11, "PERSON", "Иван Петров", source="llm")],
                [{"kind": "llm_chunk_failed", "offset": 0, "chars": 30, "message": "не разобран"}],
            ),
            ([], [{"kind": "llm_chunk_failed", "offset": 0, "chars": 30, "message": "не разобран"}]),
        ]
    )
    assert [w["kind"] for w in res.warnings] == ["llm_chunk_failed", "recheck_chunk_failed"]


def test_warnings_are_copies_not_the_detector_list():
    """Отчёт не должен зависеть от того, что детектор сделает со своим списком
    дальше — иначе следующий запрос (тот же экземпляр) затрёт выданное."""
    detector, res = _anonymize_with(
        [
            (
                [],
                [{"kind": "llm_chunk_failed", "offset": 0, "chars": 5, "message": "не разобран"}],
            ),
            ([], []),
        ]
    )
    detector.warnings.clear()
    assert [w["kind"] for w in res.warnings] == ["llm_chunk_failed"]
