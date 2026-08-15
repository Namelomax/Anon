"""Локальный учёт использования вышестоящих моделей (токены, время, стоимость).

Обработка одного документа оборачивается в :func:`request_context`, который
на выходе из блока пишет ОДНУ строку — агрегат ``kind="request_total"`` с
суммой токенов/времени/стоимости по всем вызовам вышестоящих моделей внутри
блока (LLM-детекция ``llm.py``, четыре слоя ревью ``review.py``, удалённый
GLiNER ``gliner_remote.py``). Именно эта строка — биллинговая запись по
документу; её форму (набор полей) менять нельзя.

``request_total`` также несёт ``seconds_by_kind`` — суммарное время вызовов
вышестоящих моделей, накопленное отдельно по каждому ``kind`` (``gliner``,
``llm_detect``, ``review_list`` и т.д.). КРИТИЧЕСКИ ВАЖНО: это НЕ wall-clock
время — это СУММА времени по всем вызовам данного вида, а вызовы уходят
параллельно через ``ThreadPoolExecutor`` (см. ловушку с потоками ниже).
Поэтому сумма ``seconds_by_kind`` по всем видам РУТИННО ПРЕВЫШАЕТ ``seconds``
самого документа — это не баг и не ошибка учёта, а прямое следствие
параллелизма. Именно в этом и есть польза поля: разделив накопленные
``seconds_by_kind[kind]`` на wall-clock ``seconds`` документа, получаем
реально достигнутый параллелизм для этого вида вызовов (сколько вызовов в
среднем выполнялось одновременно) — читатель, который примет
``seconds_by_kind`` за wall-clock время, придёт к выводу, ПРОТИВОПОЛОЖНОМУ
истине.

Каждый отдельный HTTP-вызов вышестоящей модели ПРОХОДИТ через
:func:`record_call`, который ВСЕГДА обновляет накопитель, питающий
``request_total`` (см. :func:`_accumulate`) — это происходит независимо от
того, пишется ли для вызова отдельная строка в лог. Пишется ли отдельная
строка per-call — определяется ``ANONYMIZER_USAGE_LOG_CALLS``:

* ``errors`` (ДЕФОЛТ) — отдельная строка пишется только для неудачных
  вызовов (``ok=False``); успешные вызовы не оставляют по себе строки,
  но всё равно учитываются в агрегате.
* ``all`` — как раньше: КАЖДЫЙ вызов (успех и неудача) пишет свою строку.
  Полезно для отладки, но взрывоопасно по объёму: например, GLiNER-слой
  делает десятки/сотни вызовов на один документ (до ~550 на 30-страничный
  документ в наблюдавшемся случае), и они не биллятся токенами — их
  агрегат уже полностью виден в ``request_total`` (``calls`` /
  ``tokens_by_kind``).
* ``off`` — отдельные строки не пишутся вообще, даже для неудачных
  вызовов. Диагностика конкретных ошибок при этом теряется — используйте
  только если лог не нужен даже для расследования сбоев.

Формат — JSON Lines (одна JSON-запись на строку), чтобы читать лог построчно
без парсинга целого файла и не бояться незаконченной последней строки при
падении процесса посередине записи (см. :func:`read_request_totals`).

Путь к логу — ``ANONYMIZER_USAGE_LOG``, по умолчанию ``<корень репо>/logs/
usage.jsonl``. Цены — ``ANONYMIZER_PRICE_IN_PER_MTOK`` / ``_OUT_`` (₽ за 1M
токенов), по умолчанию тариф Selectel Gemma 4 31B.

``cost_rub`` в ``request_total`` — ПОЛНАЯ стоимость документа: LLM
(``llm_detect`` + все слои ревью) плюс удалённый GLiNER, который биллится тем
же провайдером по токенам, но не присылает ``usage``-блок в HTTP-ответе.
Токены LLM — ИЗМЕРЕНЫ (``prompt_tokens``/``completion_tokens`` из ответа);
токены GLiNER — ОЦЕНЕНЫ из ``chars``, которые ``record_call`` уже получает на
каждый вызов: ``chars / ANONYMIZER_CHARS_PER_TOKEN +
ANONYMIZER_GLINER_TOKENS_PER_CALL`` (см. константы ``CHARS_PER_TOKEN`` /
``GLINER_TOKENS_PER_CALL`` / ``PRICE_GLINER_PER_MTOK`` ниже). Оценка попадает
ТОЛЬКО в отдельное поле ``gliner_tokens_est`` и в ``cost_rub_gliner`` —
``prompt_tokens``/``tokens_by_kind`` остаются чисто измеренными, GLiNER там
всегда 0. ``cost_rub_llm``/``cost_rub_gliner`` — та же ``cost_rub``,
разложенная на измеренную и оценённую часть, чтобы разбивка не терялась.

ГЛАВНОЕ ПРАВИЛО МОДУЛЯ: сбой логирования НИКОГДА не должен ронять запрос
анонимизации. Каждая публичная функция обёрнута в максимально широкий
``try/except`` и в худшем случае печатает одну строку предупреждения в
stderr — раз, и работа продолжается как будто лога не было.

ЛОВУШКА С ПОТОКАМИ (contextvars). ``llm.py`` и ``gliner_remote.py`` шлют
запросы через ``ThreadPoolExecutor``. Обычный ``pool.submit(fn, ...)`` НЕ
копирует ``contextvars``-контекст текущего потока в поток-воркер — то есть
``request_id``, выставленный ``request_context`` в основном потоке, внутри
воркера читался бы как ``None``, и группировка вызовов по документу
(``request_total``) тихо развалилась бы. Поэтому все точки постановки задачи
в пул детекторов ОБЯЗАНЫ идти через :func:`run_in_context`, а не через
``pool.submit`` напрямую (см. тест ``test_calls_from_threadpool_get_correct_
request_id`` и его пару ``test_bare_submit_without_context_loses_request_id``
в ``tests/test_usage_log.py``, которая проверяет именно эту ловушку).
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _default_log_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "logs" / "usage.jsonl"


# Читаются один раз при импорте; тесты монkeypatch'ят сами атрибуты модуля
# (``monkeypatch.setattr(usage_log, "LOG_PATH", ...)``), а не переменные
# окружения, поэтому перезагрузка модуля не нужна.
LOG_PATH: Path = Path(os.getenv("ANONYMIZER_USAGE_LOG") or _default_log_path())

# ₽ за 1M токенов. Дефолт — тариф Selectel Gemma 4 31B (см.
# deploy/anonymizer.env.example).
PRICE_IN_PER_MTOK: float = float(os.getenv("ANONYMIZER_PRICE_IN_PER_MTOK", "14.79"))
PRICE_OUT_PER_MTOK: float = float(os.getenv("ANONYMIZER_PRICE_OUT_PER_MTOK", "43.15"))

# ₽ за 1M токенов удалённого GLiNER (gliner_remote.py) — тот же провайдер, тот
# же биллинг по токенам, но HTTP-ответ GLiNER не несёт usage-блока (см. ниже
# про CHARS_PER_TOKEN/GLINER_TOKENS_PER_CALL), поэтому цена нужна отдельно от
# цены LLM. Дефолт — цена ВХОДНЫХ токенов LLM: консервативный выбор, малая
# модель классификации вряд ли стоит дороже входа большой.
PRICE_GLINER_PER_MTOK: float = float(
    os.getenv("ANONYMIZER_PRICE_GLINER_PER_MTOK", str(PRICE_IN_PER_MTOK))
)

# Число символов на один токен GLiNER — калибровочный коэффициент, а НЕ
# измеренная величина: GLiNER-эндпоинт не возвращает usage/prompt_tokens,
# поэтому его токены оцениваются из chars, которые record_call уже получает
# на каждый вызов. Дефолт 2.27 измерен на этом корпусе: собственные
# prompt_tokens LLM минус системные промпты, делённые на отправленные
# символы, на документе 73 321 символ. Перекалибровать по первому реальному
# счёту провайдера — единственная причина существования этой переменной.
CHARS_PER_TOKEN: float = float(os.getenv("ANONYMIZER_CHARS_PER_TOKEN", "2.27"))

# Токены накладных расходов НА КАЖДЫЙ вызов GLiNER — список меток
# (person/first name/last name/nickname/location/address/organization) плюс
# JSON-обвязка запроса едут в КАЖДОМ запросе независимо от длины текста. Это
# не мелочь для округления: на 30-страничном документе это 7120 токенов из
# 39381 токена GLiNER, то есть 18% всех токенов GLiNER. Дефолт 20.
GLINER_TOKENS_PER_CALL: float = float(os.getenv("ANONYMIZER_GLINER_TOKENS_PER_CALL", "20"))

# Режим построчного логирования отдельных вызовов (см. докстринг модуля):
# "errors" (дефолт) — строка только для ok=False; "all" — строка для каждого
# вызова (старое поведение, для отладки); "off" — строка не пишется никогда,
# даже для ошибок. НЕ влияет на агрегат request_total — тот заполняется
# всегда, см. _accumulate.
USAGE_LOG_CALLS: str = (os.getenv("ANONYMIZER_USAGE_LOG_CALLS") or "errors").strip().lower()

# Сериализует дозапись строк в файл лога.
_LOCK = threading.Lock()

# request_id текущего документа. НЕ читать напрямую из чужого потока без
# copy_context()/run_in_context() — см. докстринг модуля.
_current_request: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_request", default=None
)


class _Accumulator:
    """Итоги ОДНОГО активного ``request_context`` — накапливается со всех
    потоков, поэтому изменяется только под ``_ACC_LOCK``.

    ``prompt_tokens``/``completion_tokens``/``tokens_by_kind`` — ИЗМЕРЕННЫЕ
    значения, взятые как есть из ``usage``-блока ответа провайдера (LLM
    детекция и ревью присылают его в HTTP-ответе). ``gliner_tokens_est`` —
    ОЦЕНКА: GLiNER не присылает usage-блок вообще, поэтому его токены
    оцениваются из ``chars`` каждого вызова (см. ``CHARS_PER_TOKEN`` /
    ``GLINER_TOKENS_PER_CALL`` выше). Оценка копится ОТДЕЛЬНО и никогда не
    попадает в ``prompt_tokens``/``tokens_by_kind`` — те обязаны оставаться
    чисто измеренными числами."""

    __slots__ = (
        "prompt_tokens", "completion_tokens", "calls", "tokens_by_kind",
        "seconds_by_kind", "gliner_tokens_est",
    )

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls: dict[str, int] = {}
        self.tokens_by_kind: dict[str, dict[str, int]] = {}
        # kind -> сумма seconds по всем вызовам этого kind. НЕ wall-clock —
        # см. докстринг модуля про параллелизм ThreadPoolExecutor.
        self.seconds_by_kind: dict[str, float] = {}
        # ОЦЕНКА (не измерение) суммарных токенов GLiNER по всем вызовам
        # kind=="gliner" — см. докстринг класса выше.
        self.gliner_tokens_est: float = 0.0


# request_id -> _Accumulator, только для документов, чья обработка идёт
# ПРЯМО СЕЙЧАС (внутри активного request_context). Несколько документов
# одновременно (разные запросы к серверу) держат разные ключи, не мешая
# друг другу.
_ACTIVE: dict[str, _Accumulator] = {}
_ACC_LOCK = threading.Lock()


def current_request_id() -> str | None:
    """``request_id`` текущего документа, если он выставлен ``request_context``
    (см. ниже), иначе ``None`` (например, вызов вне обработки документа)."""
    return _current_request.get()


def run_in_context(pool, fn, *args, **kwargs):
    """Поставить ``fn(*args, **kwargs)`` в ``ThreadPoolExecutor``-пул
    ``pool``, СОХРАНИВ текущий contextvars-контекст (в т.ч. ``request_id``).

    Обычный ``pool.submit(fn, ...)`` этого не делает — см. предупреждение о
    ловушке в докстринге модуля. Использовать на КАЖДОЙ точке
    ``pool.submit`` в ``llm.py`` / ``gliner_remote.py``.
    """
    ctx = contextvars.copy_context()
    return pool.submit(ctx.run, fn, *args, **kwargs)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _append(record: dict) -> None:
    """Дописать одну JSON-строку в лог. Никогда не бросает исключение — см.
    правило модуля."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[usage_log] запись в лог не удалась: {exc}", file=sys.stderr)


def _accumulate(
    request_id: str | None, kind: str, prompt_tokens: int, completion_tokens: int, seconds: float,
    chars: int = 0,
) -> None:
    if not request_id:
        return
    try:
        with _ACC_LOCK:
            acc = _ACTIVE.get(request_id)
            if acc is None:
                return  # вызов пришёл уже после выхода из request_context
            acc.calls[kind] = acc.calls.get(kind, 0) + 1
            acc.prompt_tokens += prompt_tokens
            acc.completion_tokens += completion_tokens
            tk = acc.tokens_by_kind.setdefault(kind, {"prompt_tokens": 0, "completion_tokens": 0})
            tk["prompt_tokens"] += prompt_tokens
            tk["completion_tokens"] += completion_tokens
            # Сумма seconds по kind — НЕ wall-clock, см. докстринг модуля.
            acc.seconds_by_kind[kind] = acc.seconds_by_kind.get(kind, 0.0) + seconds
            # GLiNER не присылает usage-блок — токены ОЦЕНИВАЮТСЯ из chars
            # этого конкретного вызова, см. CHARS_PER_TOKEN/
            # GLINER_TOKENS_PER_CALL и докстринг _Accumulator. Оценка идёт в
            # отдельное поле, НЕ в prompt_tokens/tokens_by_kind.
            if kind == "gliner":
                acc.gliner_tokens_est += chars / CHARS_PER_TOKEN + GLINER_TOKENS_PER_CALL
    except Exception as exc:  # noqa: BLE001
        print(f"[usage_log] учёт итогов запроса не удался: {exc}", file=sys.stderr)


def record_call(
    kind: str,
    *,
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    seconds: float,
    chars: int = 0,
    ok: bool = True,
    error: str | None = None,
) -> None:
    """Учесть один вызов вышестоящей модели.

    Вызывается из точек HTTP-вызова в ``llm.py`` / ``review.py`` /
    ``gliner_remote.py`` — как при успехе, так и при ошибке (``ok=False,
    error=...``), не подменяя и не подавляя исходное исключение вызывающего
    кода. Сама НИКОГДА не бросает исключение (см. докстринг модуля).

    ДВЕ РАЗНЫЕ ВЕЩИ, которые эта функция делает НЕЗАВИСИМО друг от друга:

    1. Обновляет накопитель активного ``request_context`` — ``_accumulate``
       вызывается ВСЕГДА, для КАЖДОГО вызова (успешного и неуспешного).
       Именно отсюда берутся суммы в итоговой строке ``request_total``; если
       это перестанет выполняться безусловно, суммы по документу тихо
       станут нулём — это тот самый инвариант, который менять нельзя.
    2. Возможно, дописывает отдельную строку в JSONL — но это чисто
       побочная запись для диагностики, управляемая ``USAGE_LOG_CALLS``
       (см. докстринг модуля), и на (1) не влияет.
    """
    try:
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        chars = int(chars or 0)
        seconds = round(float(seconds), 3)
    except Exception as exc:  # noqa: BLE001
        print(f"[usage_log] некорректные параметры вызова ({kind}): {exc}", file=sys.stderr)
        return

    request_id = current_request_id()
    ok = bool(ok)

    # (1) Агрегат request_total — безусловно, для любого исхода вызова
    # (включая неудачные — их время тоже накапливается в seconds_by_kind).
    _accumulate(request_id, kind, prompt_tokens, completion_tokens, seconds, chars)

    # (2) Отдельная per-call строка — только если режим её требует.
    if USAGE_LOG_CALLS == "off":
        return
    if USAGE_LOG_CALLS == "errors" and ok:
        return
    record = {
        "ts": _now_iso(),
        "kind": kind,
        "request_id": request_id,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "seconds": seconds,
        "chars": chars,
        "ok": ok,
        "error": str(error) if error else None,
    }
    _append(record)


def _cost_rub_llm(prompt_tokens: int, completion_tokens: int) -> float:
    """Стоимость ИЗМЕРЕННЫХ токенов LLM (детекция + все слои ревью) — прямо
    из ``usage``-блока ответа провайдера, без оценок."""
    cost = prompt_tokens / 1e6 * PRICE_IN_PER_MTOK + completion_tokens / 1e6 * PRICE_OUT_PER_MTOK
    return round(cost, 4)


def _cost_rub_gliner(gliner_tokens_est: float) -> float:
    """Стоимость ОЦЕНЁННЫХ токенов GLiNER (см. ``CHARS_PER_TOKEN`` /
    ``GLINER_TOKENS_PER_CALL``) — не измерена, но нужна, чтобы полная
    стоимость документа не занижалась примерно на 19% входного объёма."""
    return round(gliner_tokens_est / 1e6 * PRICE_GLINER_PER_MTOK, 4)


@dataclass
class RequestTotals:
    """Итоги одного документа. Возвращается ``request_context`` как объект
    ``with``-блока; поля заполняются на ВЫХОДЕ из блока (см. ``as_response_dict``
    — используется server.py для поля ``usage`` в HTTP-ответе).

    ``cost_rub`` — ПОЛНАЯ стоимость документа (LLM + GLiNER), сверяемая со
    счётом провайдера. ``cost_rub_llm``/``cost_rub_gliner`` — та же сумма,
    разложенная на измеренную (LLM) и оценённую (GLiNER, см.
    ``gliner_tokens_est``) части, чтобы разбивка оставалась видимой."""

    request_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    cost_rub: float = 0.0
    cost_rub_llm: float = 0.0
    cost_rub_gliner: float = 0.0
    # ОЦЕНКА (не измерение) суммарных токенов GLiNER по всем вызовам
    # kind=="gliner" за документ — см. докстринг _Accumulator/модуля.
    gliner_tokens_est: int = 0
    calls: dict = field(default_factory=dict)  # kind -> количество вызовов
    # kind -> суммарные seconds по всем вызовам этого kind. НЕ wall-clock —
    # вызовы идут параллельно (ThreadPoolExecutor), поэтому сумма по всем
    # kind обычно ПРЕВЫШАЕТ ``seconds`` документа. См. докстринг модуля.
    seconds_by_kind: dict = field(default_factory=dict)

    def as_response_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "seconds": self.seconds,
            "cost_rub": self.cost_rub,
            "cost_rub_llm": self.cost_rub_llm,
            "cost_rub_gliner": self.cost_rub_gliner,
            "gliner_tokens_est": self.gliner_tokens_est,
            "calls": dict(self.calls),
            # НЕ wall-clock — см. докстринг модуля / поле RequestTotals.seconds_by_kind.
            "seconds_by_kind": dict(self.seconds_by_kind),
        }


@contextmanager
def request_context(filename: str | None = None, chars: int = 0, stages: dict | None = None):
    """Контекст обработки ОДНОГО документа.

    Назначает свежий ``request_id`` (виден всем вызовам ``record_call``
    внутри блока — в т.ч. из ``ThreadPoolExecutor``-воркеров, отправленных
    через :func:`run_in_context`, см. докстринг модуля). На выходе из блока
    пишет ОДНУ сводную строку ``kind="request_total"`` и возвращает объект
    ``RequestTotals``, который вызывающий код (``server.py``) может прочитать
    ПОСЛЕ выхода из ``with``.
    """
    request_id = uuid.uuid4().hex[:12]
    totals = RequestTotals(request_id=request_id)
    with _ACC_LOCK:
        _ACTIVE[request_id] = _Accumulator()
    token = _current_request.set(request_id)
    t0 = time.time()
    try:
        yield totals
    finally:
        _current_request.reset(token)
        elapsed = round(time.time() - t0, 3)
        with _ACC_LOCK:
            acc = _ACTIVE.pop(request_id, None) or _Accumulator()

        try:
            # ИЗМЕРЕНО: prompt_tokens/completion_tokens — прямо из usage LLM.
            cost_rub_llm = _cost_rub_llm(acc.prompt_tokens, acc.completion_tokens)
            # ОЦЕНЕНО: GLiNER не присылает usage, gliner_tokens_est накоплен
            # из chars каждого вызова (см. _accumulate/CHARS_PER_TOKEN).
            gliner_tokens_est = round(acc.gliner_tokens_est)
            cost_rub_gliner = _cost_rub_gliner(acc.gliner_tokens_est)
            # ПОЛНАЯ стоимость документа — то, что сверяется со счётом
            # провайдера (см. докстринг RequestTotals): LLM (измерено) +
            # GLiNER (оценка).
            cost_rub = round(cost_rub_llm + cost_rub_gliner, 4)
            pages = round(chars / 1800, 1) if chars else 0.0

            seconds_by_kind = {k: round(v, 3) for k, v in acc.seconds_by_kind.items()}

            totals.prompt_tokens = acc.prompt_tokens
            totals.completion_tokens = acc.completion_tokens
            totals.seconds = elapsed
            totals.cost_rub = cost_rub
            totals.cost_rub_llm = cost_rub_llm
            totals.cost_rub_gliner = cost_rub_gliner
            totals.gliner_tokens_est = gliner_tokens_est
            totals.calls = dict(acc.calls)
            totals.seconds_by_kind = seconds_by_kind

            _append({
                "ts": _now_iso(),
                "kind": "request_total",
                "request_id": request_id,
                "filename": filename,
                "chars": int(chars or 0),
                "pages": pages,
                "seconds": elapsed,
                "calls": dict(acc.calls),
                # ИЗМЕРЕННЫЕ токены по kind (usage LLM) — GLiNER здесь ВСЕГДА
                # 0/0, оценка в tokens_by_kind никогда не попадает, см.
                # gliner_tokens_est ниже.
                "tokens_by_kind": {k: dict(v) for k, v in acc.tokens_by_kind.items()},
                # Суммарные (не wall-clock!) seconds по kind — см. докстринг
                # модуля: сумма по всем kind обычно ПРЕВЫШАЕТ elapsed выше,
                # т.к. вызовы идут параллельно через ThreadPoolExecutor.
                "seconds_by_kind": seconds_by_kind,
                "prompt_tokens_total": acc.prompt_tokens,
                "completion_tokens_total": acc.completion_tokens,
                # ОЦЕНКА (не измерение) суммарных токенов GLiNER за документ —
                # см. CHARS_PER_TOKEN/GLINER_TOKENS_PER_CALL выше.
                "gliner_tokens_est": gliner_tokens_est,
                # ПОЛНАЯ стоимость документа (LLM измерено + GLiNER оценка) —
                # сверяется со счётом провайдера. cost_rub_llm/cost_rub_gliner
                # — та же сумма, разложенная на составляющие.
                "cost_rub": cost_rub,
                "cost_rub_llm": cost_rub_llm,
                "cost_rub_gliner": cost_rub_gliner,
                "stages": dict(stages or {}),
            })
        except Exception as exc:  # noqa: BLE001
            print(f"[usage_log] сводная запись документа не удалась: {exc}", file=sys.stderr)


# --- Чтение агрегатов (GET /usage, usage_report.py) -------------------------

def read_request_totals() -> list[dict]:
    """Все строки ``kind=="request_total"`` из лога.

    Не использует ``_LOCK`` записи — чтение не должно стоять в очереди за
    пишущими вызовами (см. server.py: GET /usage не блокируется на модельный
    лок). Устойчиво к отсутствующему файлу и к отдельным неразборчивым
    строкам (в т.ч. незаконченной последней строке при падении процесса
    посередине дозаписи) — такие строки просто пропускаются.
    """
    records: list[dict] = []
    try:
        if not LOG_PATH.exists():
            return records
        with LOG_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue  # повреждённая/незаконченная строка — пропуск
                if isinstance(rec, dict) and rec.get("kind") == "request_total":
                    records.append(rec)
    except Exception as exc:  # noqa: BLE001
        print(f"[usage_log] чтение лога не удалось: {exc}", file=sys.stderr)
    return records


def _parse_ts(rec: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(rec["ts"])
    except Exception:  # noqa: BLE001
        return None


def _by_stage(records: list[dict]) -> dict:
    """Разбивка по ``kind`` (``gliner``, ``llm_detect``, ``review_*``, ...),
    агрегированная по переданным ``request_total`` записям: число вызовов,
    суммарные (НЕ wall-clock — см. докстринг модуля) seconds, среднее
    seconds на вызов и суммарные токены. Устойчиво к записям без
    ``seconds_by_kind``/``tokens_by_kind`` (старые строки лога до появления
    этих полей) и к записям с "кривой" формой этих полей."""
    acc: dict[str, dict[str, float]] = {}
    for rec in records:
        calls = rec.get("calls")
        calls = calls if isinstance(calls, dict) else {}
        seconds_by_kind = rec.get("seconds_by_kind")
        seconds_by_kind = seconds_by_kind if isinstance(seconds_by_kind, dict) else {}
        tokens_by_kind = rec.get("tokens_by_kind")
        tokens_by_kind = tokens_by_kind if isinstance(tokens_by_kind, dict) else {}

        for kind in set(calls) | set(seconds_by_kind) | set(tokens_by_kind):
            entry = acc.setdefault(kind, {"calls": 0, "seconds": 0.0, "tokens": 0})
            try:
                entry["calls"] += int(calls.get(kind) or 0)
            except Exception:  # noqa: BLE001
                pass
            try:
                entry["seconds"] += float(seconds_by_kind.get(kind) or 0)
            except Exception:  # noqa: BLE001
                pass
            tk = tokens_by_kind.get(kind)
            tk = tk if isinstance(tk, dict) else {}
            try:
                entry["tokens"] += int(tk.get("prompt_tokens") or 0) + int(tk.get("completion_tokens") or 0)
            except Exception:  # noqa: BLE001
                pass

    result: dict[str, dict] = {}
    for kind, entry in acc.items():
        calls_n = int(entry["calls"])
        seconds_sum = round(entry["seconds"], 3)
        result[kind] = {
            "calls": calls_n,
            "seconds": seconds_sum,
            "avg_seconds": round(seconds_sum / calls_n, 3) if calls_n else 0.0,
            "tokens": int(entry["tokens"]),
        }
    return result


def _summarize(records: list[dict]) -> dict:
    requests = len(records)
    pages = sum(float(r.get("pages") or 0) for r in records)
    prompt_tokens = sum(int(r.get("prompt_tokens_total") or 0) for r in records)
    completion_tokens = sum(int(r.get("completion_tokens_total") or 0) for r in records)
    gliner_tokens_est = sum(int(r.get("gliner_tokens_est") or 0) for r in records)
    cost_rub = sum(float(r.get("cost_rub") or 0) for r in records)
    # cost_rub_llm/cost_rub_gliner отсутствуют в старых строках лога
    # (до появления этой фичи) — фолбэк на 0, как и остальные поля здесь.
    cost_rub_llm = sum(float(r.get("cost_rub_llm") or 0) for r in records)
    cost_rub_gliner = sum(float(r.get("cost_rub_gliner") or 0) for r in records)
    seconds = sum(float(r.get("seconds") or 0) for r in records)
    total_tokens = prompt_tokens + completion_tokens
    return {
        "requests": requests,
        "pages": round(pages, 1),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "gliner_tokens_est": gliner_tokens_est,
        # ПОЛНАЯ стоимость (LLM измерено + GLiNER оценка) — см. докстринг
        # RequestTotals/request_context. cost_rub_llm/cost_rub_gliner — та же
        # сумма, разложенная на составляющие.
        "cost_rub": round(cost_rub, 4),
        "cost_rub_llm": round(cost_rub_llm, 4),
        "cost_rub_gliner": round(cost_rub_gliner, 4),
        "avg_seconds_per_page": round(seconds / pages, 3) if pages else 0.0,
        "avg_tokens_per_page": round(total_tokens / pages, 1) if pages else 0.0,
        # Разбивка по kind (gliner/llm_detect/review_*): calls/seconds/
        # avg_seconds/tokens. seconds здесь — тоже НЕ wall-clock, а сумма
        # по вызовам этого kind (см. докстринг модуля и _by_stage выше).
        "by_stage": _by_stage(records),
    }


def usage_summary(days: int = 30) -> dict:
    """Агрегаты за сегодня / последние ``days`` дней / всё время.

    Используется и ``GET /usage`` (server.py, без блокировок), и
    ``usage_report.py`` (CLI поверх того же файла). Устойчиво к пустому/
    отсутствующему логу и к строкам с неразборчивым ``ts`` (такие попадают в
    "всё время", но не в дневные/недельные срезы — см. цикл ниже).
    """
    records = read_request_totals()
    now = datetime.now(timezone.utc)
    today = now.date()
    cutoff = now.timestamp() - days * 86400

    today_records: list[dict] = []
    recent_records: list[dict] = []
    for rec in records:
        ts = _parse_ts(rec)
        if ts is None:
            continue
        if ts.date() == today:
            today_records.append(rec)
        if ts.timestamp() >= cutoff:
            recent_records.append(rec)

    return {
        "today": _summarize(today_records),
        f"last_{days}_days": _summarize(recent_records),
        "all_time": _summarize(records),
    }
