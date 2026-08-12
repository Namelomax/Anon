"""CLI: печатает агрегаты использования (токены, страницы, стоимость) без HTTP.

Читает тот же JSONL-лог, что и ``usage_log.record_call`` / ``GET /usage``
(``ANONYMIZER_USAGE_LOG``, по умолчанию ``<корень репо>/logs/usage.jsonl``) —
удобно посмотреть цифры прямо по SSH, без похода в API.

Использование:
    python anonymizer/usage_report.py [--days N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer import usage_log  # noqa: E402

_COLUMNS = (
    "requests", "pages", "prompt_tokens", "completion_tokens",
    "cost_rub", "avg_seconds_per_page", "avg_tokens_per_page",
)


def _print_table(summary: dict) -> None:
    header = ["период"] + list(_COLUMNS)
    rows = [
        [period] + [str(row.get(col, "")) for col in _COLUMNS]
        for period, row in summary.items()
    ]
    widths = [
        max(len(str(r[i])) for r in ([header] + rows))
        for i in range(len(header))
    ]

    def fmt(row: list[str]) -> str:
        return "  ".join(str(v).ljust(w) for v, w in zip(row, widths))

    print(fmt(header))
    print(fmt(["-" * w for w in widths]))
    for row in rows:
        print(fmt(row))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--days", type=int, default=30,
        help="Окно «последние N дней» в дополнение к сегодня/всё время (по умолчанию 30).",
    )
    args = ap.parse_args()

    print(f"Лог: {usage_log.LOG_PATH}")
    summary = usage_log.usage_summary(days=args.days)
    _print_table(summary)


if __name__ == "__main__":
    main()
