"""Журнал учёта действий по обезличиванию персональных данных.

Ведётся во исполнение пункта 1.7 требований, утверждённых приказом
Роскомнадзора от 19.06.2025 № 140 (действует с 01.09.2025):

    «учет осуществляемых действий по обезличиванию персональных данных,
    действий (операций), совершаемых с персональными данными, полученными в
    результате обезличивания, в определенной оператором форме, позволяющей
    обеспечить подтверждение осуществленных оператором действий...»

Форма учёта — построчный JSONL, одна запись на одно действие.

ГЛАВНОЕ ПРАВИЛО МОДУЛЯ
----------------------
**В журнал не попадают персональные данные — ни в каком виде.**

Ни исходный текст, ни значения найденных сущностей, ни таблица соответствий,
ни имя исходного файла. Имена документов в корпоративном обороте регулярно
содержат ФИО («Приказ_об_увольнении_Иванова_И_И.docx»), поэтому вместо имени
пишется его усечённый SHA-256: он позволяет соотнести записи между собой и с
обращением заказчика, но сам по себе персональных данных не образует.

Причина правила прямая: журнал, содержащий персональные данные, сам является
информационной системой персональных данных со всеми вытекающими
обязанностями — он попадает в перечень обрабатываемых ПДн, в модель угроз, под
меры приказа ФСТЭК России № 21 и под сроки уничтожения. Смысл учёта — доказать
факт совершённых действий, а не сохранить их содержание.

Отличие от ``usage_log``
------------------------
``usage_log`` — учёт ПОТРЕБЛЕНИЯ: токены, стоимость, длительность. Он нужен для
биллинга и оптимизации, требованиям приказа № 140 не отвечает и отвечать не
должен.

Настоящий модуль — учёт ДЕЙСТВИЙ: что именно было сделано с персональными
данными, когда, кем, каким методом и что стало с ключом деанонимизации. Это
разные журналы с разным назначением, разным сроком хранения и разным режимом
доступа, поэтому они намеренно разведены по разным файлам.

Путь к журналу — ``ANONYMIZER_DEPERSONALIZATION_LOG``, по умолчанию
``<корень репо>/logs/depersonalization.jsonl``. Файл создаётся с правами 0600.

Как и ``usage_log``, модуль НИКОГДА не бросает исключение наружу: сбой
журналирования не должен ронять обработку документа. Ошибки печатаются в
stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# Обозначение применяемого метода обезличивания. Соответствует пункту 2
# приложения № 2 к приказу Роскомнадзора от 19.06.2025 № 140 — «метод введения
# идентификаторов»: замена персональных данных на идентификаторы в виде кода,
# номера или иного обозначения, присваиваемые на основании заданных оператором
# алгоритмов. Реализация — anonymizer/mapping.py.
METHOD_IDENTIFIERS = "identifiers"

#: Типы регистрируемых событий.
#: ``depersonalize``   — выполнено обезличивание документа;
#: ``key_issued``      — ключ (таблица соответствий) передан получателю;
#: ``key_withheld``    — ключ не формировался (необратимый режим);
#: ``result_issued``   — результат обезличивания выдан получателю;
#: ``job_destroyed``   — задание вместе с результатом и ключом удалено из памяти.
EVENTS = (
    "depersonalize",
    "key_issued",
    "key_withheld",
    "result_issued",
    "job_destroyed",
)


def _default_log_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "logs" / "depersonalization.jsonl"


# Читается один раз при импорте; тесты подменяют сам атрибут модуля.
LOG_PATH: Path = Path(
    os.getenv("ANONYMIZER_DEPERSONALIZATION_LOG") or _default_log_path()
)

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def source_ref(filename: str | None) -> str | None:
    """Ссылка на исходный документ, не раскрывающая его имени.

    Усечённый SHA-256 от имени файла. Одинаковые имена дают одинаковую ссылку,
    что позволяет соотнести записи журнала между собой и с обращением
    заказчика, но восстановить имя по ссылке нельзя.
    """
    if not filename:
        return None
    try:
        digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()
        return digest[:16]
    except Exception:  # noqa: BLE001
        return None


def _append(record: dict) -> None:
    """Дописать одну строку. Никогда не бросает — см. докстринг модуля."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            # Создаём файл сразу с правами 0600: журнал учёта не предназначен
            # для чтения другими пользователями системы.
            if not LOG_PATH.exists():
                fd = os.open(str(LOG_PATH), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
                os.close(fd)
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[depersonalization_log] запись не удалась: {exc}", file=sys.stderr)


def record_operation(
    *,
    request_id: str | None,
    labels: dict | None = None,
    chars: int = 0,
    seconds: float = 0.0,
    irreversible: bool = False,
    method: str = METHOD_IDENTIFIERS,
    actor: str | None = None,
    filename: str | None = None,
    ok: bool = True,
    error: object = None,
) -> None:
    """Зарегистрировать факт обезличивания одного документа.

    Args:
        request_id: Идентификатор обращения, связывает записи между собой.
        labels: Счётчики замен по категориям — ``{"PERSON": 12, "INN": 3}``.
            Это ``Result.summary``: КОЛИЧЕСТВА, не значения. Значения сюда
            передавать нельзя ни при каких обстоятельствах.
        chars: Объём исходного текста в символах.
        seconds: Длительность операции.
        irreversible: Формировался ли ключ деанонимизации. ``True`` означает,
            что ключ не создавался вовсе.
        method: Применённый метод обезличивания, см. ``METHOD_IDENTIFIERS``.
        actor: Субъект доступа, инициировавший операцию. Заполняется после
            ввода аутентификации входящих запросов; до этого — ``None``,
            что честно фиксирует отсутствие идентификации инициатора.
        filename: Имя исходного файла. В журнал НЕ пишется — сохраняется
            только его хеш, см. ``source_ref``.
        ok: Успешность операции.
        error: Класс ошибки. Текст исключения не пишется: в него может попасть
            фрагмент документа.
    """
    counts = dict(labels or {})
    _append(
        {
            "ts": _now_iso(),
            "event": "depersonalize",
            "request_id": request_id,
            "actor": actor,
            "source_ref": source_ref(filename),
            "method": method,
            "irreversible": bool(irreversible),
            "labels": counts,
            "replacements_total": sum(counts.values()),
            "chars": int(chars or 0),
            "seconds": round(float(seconds or 0.0), 3),
            "ok": bool(ok),
            "error_type": type(error).__name__ if error is not None else None,
        }
    )


def record_event(
    *,
    event: str,
    request_id: str | None,
    actor: str | None = None,
    detail: dict | None = None,
) -> None:
    """Зарегистрировать операцию с результатом обезличивания или с ключом.

    Отдельно от ``record_operation``, потому что пункт 1.7 требует учёта не
    только самого обезличивания, но и «действий (операций), совершаемых с
    персональными данными, полученными в результате обезличивания» — то есть
    выдачи результата, выдачи ключа и их уничтожения.

    ``detail`` — только технические сведения (например, причина удаления
    задания). Персональные данные в него не помещаются.
    """
    if event not in EVENTS:
        print(f"[depersonalization_log] неизвестное событие: {event}", file=sys.stderr)
        return
    _append(
        {
            "ts": _now_iso(),
            "event": event,
            "request_id": request_id,
            "actor": actor,
            "detail": dict(detail or {}),
        }
    )
