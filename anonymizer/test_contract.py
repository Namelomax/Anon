"""Локальный прогон анонимизатора на договоре + отчёт об утечках.

Запуск (LM Studio на 127.0.0.1:1234, GLiNER на CPU/CUDA):

    python anonymizer/test_contract.py путь/к/договору.docx
    python anonymizer/test_contract.py договор.docx --second-pass
    python anonymizer/test_contract.py договор.docx --no-llm          # только regex+GLiNER
    python anonymizer/test_contract.py договор.docx --threshold 0.35  # recall повыше

Печатает mapping по меткам и «сниффер утечек»: что в анонимизированном тексте
похоже на непойманные ПДн (слова с заглавной подряд, длинные цифровые серии,
@, ключевые слова реквизитов рядом с цифрами). Сниффер — эвристика для ГЛАЗ:
решение «утечка/не утечка» за человеком.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer.documents import read_text  # noqa: E402
from anonymizer.engine import build_anonymizer  # noqa: E402
from anonymizer.gliner_ner import GLiNERConfig  # noqa: E402
from anonymizer.llm import LLMConfig  # noqa: E402
from anonymizer.review import ReviewConfig  # noqa: E402

PLACEHOLDER_RX = re.compile(r"\[[A-Z_]+_\d+\]")


def sniff_leaks(anon_text: str) -> list[tuple[str, list[str]]]:
    """Эвристический поиск подозрительных остатков в анонимизированном тексте."""
    t = PLACEHOLDER_RX.sub(" ", anon_text)
    report: list[tuple[str, list[str]]] = []

    caps_pairs = re.findall(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}(?:\s+[А-ЯЁ][а-яё]{2,})?\b", t)
    if caps_pairs:
        report.append(("Подряд слова с заглавной (возможные ФИО/организации)", sorted(set(caps_pairs))))

    digit_runs = re.findall(r"\b\d[\d \-/]{5,}\d\b", t)
    if digit_runs:
        report.append(("Цифровые серии 6+ (номера/счета/телефоны?)", sorted(set(digit_runs))))

    emails = re.findall(r"\S+@\S+", t)
    if emails:
        report.append(("Похоже на email", sorted(set(emails))))

    req = re.findall(
        r"(?:ИНН|КПП|ОГРН|БИК|ОКПО|СНИЛС|сч[её]т\w*|паспорт\w*)[^\n]{0,40}\d[^\n]{0,20}",
        t, re.IGNORECASE,
    )
    if req:
        report.append(("Реквизит-слово рядом с цифрами", sorted(set(req))))

    money = re.findall(r"\d[\d\s.,]*\s*(?:руб|₽|коп)[а-я.]*", t, re.IGNORECASE)
    if money:
        report.append(("Похоже на суммы", sorted(set(money))))
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", help="Файл .docx или .txt")
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--llm-model", default="gemma4:12b")
    ap.add_argument("--no-llm", action="store_true", help="Без LLM-слоя (только regex+GLiNER)")
    ap.add_argument("--no-ner", action="store_true", help="Без GLiNER (только regex)")
    ap.add_argument("--no-review", action="store_true")
    ap.add_argument("--second-pass", action="store_true", help="Повторный LLM-скан утечек")
    ap.add_argument("--threshold", type=float, default=0.45, help="Порог GLiNER (ниже = выше recall)")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--save", help="Сохранить анонимизированный текст в файл")
    args = ap.parse_args()

    text = read_text(args.path)
    print(f"Файл: {args.path} — {len(text)} символов")

    use_llm = not args.no_llm
    llm_cfg = LLMConfig(base_url=args.llm_base_url, model=args.llm_model)
    anon = build_anonymizer(
        use_regex=True,
        corporate=True,
        use_ner=not args.no_ner,
        ner_backend="gliner",
        gliner_config=GLiNERConfig(device=args.device, threshold=args.threshold),
        use_llm=use_llm,
        llm_config=llm_cfg,
        use_review=use_llm and not args.no_review,
        review_config=ReviewConfig(base_url=args.llm_base_url, model=args.llm_model),
        use_second_pass=use_llm and args.second_pass,
    )

    import time
    t0 = time.time()
    res = anon.anonymize(text)
    print(f"Готово за {time.time() - t0:.1f} с; спанов: {len(res.spans)}; меток: {Counter(s.label for s in res.spans)}\n")

    print("=== MAPPING ===")
    for ph, orig in sorted(res.mapping.items()):
        print(f"{ph:22s} {orig!r}")

    print("\n=== СНИФФЕР УТЕЧЕК (проверить глазами) ===")
    report = sniff_leaks(res.anonymized_text)
    if not report:
        print("Подозрительных остатков не найдено.")
    for title, items in report:
        print(f"\n-- {title}:")
        for it in items[:40]:
            print(f"   {it}")

    if args.save:
        Path(args.save).write_text(res.anonymized_text, encoding="utf-8")
        print(f"\nАнонимизированный текст: {args.save}")


if __name__ == "__main__":
    main()
