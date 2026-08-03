"""Профилировщик конвейера анонимизации: куда уходит время и сколько вызовов LLM.

Перехватывает КАЖДЫЙ HTTP-запрос к LM Studio/Ollama (все четыре точки вызова
идут через ``urllib.request.urlopen``) и приписывает его к стадии конвейера.
На выходе — таблица «стадия / вызовов / секунд / доля» плюс разбивка по
токенам из поля ``usage`` ответа, если сервер его отдаёт.

Запуск (из корня репозитория):

    python anonymizer/profile_run.py anonymizer/test_samples/sample2_meeting_protocol.txt

    # свой документ, тот же конвейер, что и у сервера по умолчанию
    python anonymizer/profile_run.py mydoc.docx --device cuda \
        --llm-base-url http://127.0.0.1:1234/v1 --llm-model google/gemma-4-12b-qat

    # сравнить три конфигурации подряд на одном документе
    python anonymizer/profile_run.py mydoc.docx --compare

Ничего не отправляет наружу и не пишет файлов — только считает время.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --- Сбор статистики -------------------------------------------------------

_CURRENT_STAGE = "прочее"


@dataclass
class _Call:
    stage: str
    seconds: float
    prompt_chars: int
    reply_chars: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass
class _Stats:
    calls: list[_Call] = field(default_factory=list)
    stage_wall: dict[str, float] = field(default_factory=dict)  # инклюзивное время (со вложенными стадиями)
    stage_self: dict[str, float] = field(default_factory=dict)  # эксклюзивное время (без вложенных стадий)
    total: float = 0.0

    def add_wall(self, stage: str, seconds: float, exclusive: float) -> None:
        self.stage_wall[stage] = self.stage_wall.get(stage, 0.0) + seconds
        self.stage_self[stage] = self.stage_self.get(stage, 0.0) + exclusive


_STATS = _Stats()

# Стек «сколько времени уже потрачено на вложенные стадии» для текущей цепочки
# активных стадий — нужен, чтобы не засчитывать одни и те же секунды дважды
# (например, second-pass внутри себя вызывает llm-детекция).
_STAGE_CHILDREN_STACK: list[float] = []


class _ReplayResponse:
    """Обёртка: тело уже прочитано, отдаём его повторно вызывающему коду."""

    def __init__(self, body: bytes, real):
        self._buf = io.BytesIO(body)
        self._real = real

    def read(self, *a):
        return self._buf.read(*a)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


_REAL_URLOPEN = urllib.request.urlopen


def _timing_urlopen(req, *args, **kwargs):
    # Не-LLM запросы (если вдруг появятся) пропускаем без обёртки.
    body = getattr(req, "data", None)
    if body is None:
        return _REAL_URLOPEN(req, *args, **kwargs)

    prompt_chars = 0
    try:
        payload = json.loads(body.decode("utf-8"))
        prompt_chars = sum(len(m.get("content") or "") for m in payload.get("messages", []))
    except Exception:  # noqa: BLE001
        pass

    t0 = time.perf_counter()
    resp = _REAL_URLOPEN(req, *args, **kwargs)
    raw = resp.read()
    elapsed = time.perf_counter() - t0

    reply_chars, ptok, ctok = 0, None, None
    try:
        data = json.loads(raw.decode("utf-8"))
        msg = data["choices"][0]["message"]
        reply_chars = len(msg.get("content") or msg.get("reasoning_content") or "")
        usage = data.get("usage") or {}
        ptok = usage.get("prompt_tokens")
        ctok = usage.get("completion_tokens")
    except Exception:  # noqa: BLE001
        pass

    _STATS.calls.append(
        _Call(_CURRENT_STAGE, elapsed, prompt_chars, reply_chars, ptok, ctok)
    )
    print(
        f"    [{_CURRENT_STAGE}] {elapsed:6.2f}s  "
        f"промпт {prompt_chars:>6} симв."
        + (f" / {ptok} ток." if ptok else "")
        + f"  ответ {reply_chars:>5} симв."
        + (f" / {ctok} ток." if ctok else ""),
        file=sys.stderr,
        flush=True,
    )
    return _ReplayResponse(raw, resp)


def _stage(name: str):
    """Декоратор: помечает стадию и меряет её настенное время (инклюзивное и
    эксклюзивное — без учёта времени, потраченного на вложенные стадии)."""

    def wrap(fn):
        def inner(*a, **kw):
            global _CURRENT_STAGE
            prev = _CURRENT_STAGE
            _CURRENT_STAGE = name
            _STAGE_CHILDREN_STACK.append(0.0)
            t0 = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                elapsed = time.perf_counter() - t0
                children = _STAGE_CHILDREN_STACK.pop()
                exclusive = elapsed - children
                if _STAGE_CHILDREN_STACK:
                    _STAGE_CHILDREN_STACK[-1] += elapsed
                _STATS.add_wall(name, elapsed, exclusive)
                _CURRENT_STAGE = prev

        return inner

    return wrap


def _install_probes() -> None:
    urllib.request.urlopen = _timing_urlopen

    from anonymizer import engine, llm, review

    llm.LLMDetector.find = _stage("llm-детекция")(llm.LLMDetector.find)
    review.review_spans = _stage("review")(review.review_spans)
    review.recall_spans = _stage("recall")(review.recall_spans)
    review._ask_adjacent = _stage("adjacency")(review._ask_adjacent)
    engine.Anonymizer._find_leaked_spans = _stage("second-pass")(
        engine.Anonymizer._find_leaked_spans
    )

    try:
        from anonymizer import gliner_ner

        gliner_ner.GLiNERDetector.find = _stage("GLiNER")(gliner_ner.GLiNERDetector.find)
    except Exception:  # noqa: BLE001
        pass


# --- Отчёт -----------------------------------------------------------------

def _report(total: float, text_len: int) -> None:
    print("\n" + "=" * 72)
    print(f"ИТОГО: {total:.1f} с на {text_len} символов "
          f"(~{text_len / 2.34:.0f} токенов исходного текста)")
    print("=" * 72)

    by_stage: dict[str, list[_Call]] = {}
    for c in _STATS.calls:
        by_stage.setdefault(c.stage, []).append(c)

    # Сортируем и считаем доли по эксклюзивному времени (без вложенных стадий),
    # иначе вложенные вызовы (например, llm-детекция внутри second-pass)
    # засчитываются дважды и доли в сумме превышают 100%.
    order = sorted(_STATS.stage_self.items(), key=lambda kv: -kv[1])
    print(f"\n{'стадия':<16}{'вызовов':>9}{'секунд':>10}{'доля':>8}{'инкл.с':>9}"
          f"{'ток. промпт':>13}{'ток. ответ':>12}")
    print("-" * 77)
    for stage, self_wall in order:
        calls = by_stage.get(stage, [])
        ptok = sum(c.prompt_tokens or 0 for c in calls)
        ctok = sum(c.completion_tokens or 0 for c in calls)
        incl = _STATS.stage_wall.get(stage, 0.0)
        print(f"{stage:<16}{len(calls):>9}{self_wall:>10.1f}"
              f"{self_wall / total * 100:>7.0f}%{incl:>9.1f}"
              f"{ptok:>13}{ctok:>12}")

    llm_wall = sum(w for s, w in _STATS.stage_self.items() if s != "GLiNER")
    print("-" * 77)
    print(f"{'сумма LLM':<16}{len(_STATS.calls):>9}{llm_wall:>10.1f}"
          f"{llm_wall / total * 100:>7.0f}%")
    other = total - sum(_STATS.stage_self.values())
    print(f"{'детекторы/код':<16}{'':>9}{other:>10.1f}{other / total * 100:>7.0f}%")

    if not _STATS.calls:
        return

    print("\nСамые дорогие отдельные вызовы:")
    for c in sorted(_STATS.calls, key=lambda c: -c.seconds)[:5]:
        speed = ""
        if c.completion_tokens and c.seconds:
            speed = f", {c.completion_tokens / c.seconds:.1f} ток/с генерации"
        print(f"  {c.seconds:6.2f}s  [{c.stage}]  промпт {c.prompt_chars} симв."
              f"  ответ {c.reply_chars} симв.{speed}")

    # Предупреждение про обрезку контекста.
    big = [c for c in _STATS.calls if (c.prompt_tokens or 0) >= 15500]
    if big:
        print(f"\n[!] {len(big)} вызов(ов) с промптом ≥15.5k токенов — проверьте "
              "лимит контекста в LM Studio, иначе они молча режутся.")
    empty = [c for c in _STATS.calls if c.reply_chars == 0]
    if empty:
        print(f"[!] {len(empty)} вызов(ов) вернули ПУСТОЙ ответ — потраченное "
              "на них время ушло впустую.")


# --- Прогон ----------------------------------------------------------------

def _build(args, *, second_pass: bool, recall: bool, review_on: bool):
    from dataclasses import replace as dc_replace

    from anonymizer.detectors import CORPORATE_DETECTORS, DEFAULT_DETECTORS
    from anonymizer.engine import Anonymizer
    from anonymizer.glossary import DEFAULT_GLOSSARY_PATH, GlossaryDetector, load_glossary
    from anonymizer.llm import LLMConfig, LLMDetector
    from anonymizer.review import ReviewConfig

    dets: list = list(DEFAULT_DETECTORS)
    if args.corporate:
        dets += list(CORPORATE_DETECTORS)

    entries = load_glossary(args.custom_terms or DEFAULT_GLOSSARY_PATH)
    if entries:
        dets.append(GlossaryDetector(entries))

    if args.ner == "gliner":
        from anonymizer.gliner_ner import GLiNERConfig, GLiNERDetector

        dets.append(GLiNERDetector(GLiNERConfig(device=args.device)))
    elif args.ner == "natasha":
        from anonymizer.ner import NatashaDetector

        dets.append(NatashaDetector())

    extra = {} if args.think else {"reasoning_effort": "none"}
    lconf = LLMConfig(
        base_url=args.llm_base_url, model=args.llm_model, extra_body=extra
    )
    if args.corporate:
        lconf = dc_replace(lconf, allowed_labels=lconf.allowed_labels | {"ORG", "AMOUNT"})
    if args.max_chars:
        lconf = dc_replace(lconf, max_chars=args.max_chars)

    llm_det = LLMDetector(lconf)
    dets.append(llm_det)

    rconf = None
    if review_on:
        rconf = ReviewConfig(
            base_url=args.llm_base_url, model=args.llm_model,
            extra_body=extra, recall=recall,
        )

    return Anonymizer(
        dets,
        review_config=rconf,
        second_pass_detectors=[llm_det] if second_pass else [],
    )


def _run_once(anon, text: str, title: str) -> float:
    global _STATS
    _STATS = _Stats()
    print(f"\n### {title}", file=sys.stderr, flush=True)
    t0 = time.perf_counter()
    res = anon.anonymize(text)
    total = time.perf_counter() - t0
    _report(total, len(text))
    print(f"\nНайдено сущностей: {len(res.mapping)}  |  спанов: {len(res.spans)}"
          f"  |  предупреждений: {len(res.warnings)}")
    return total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="Документ .txt или .docx")
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--llm-model", default="google/gemma-4-12b-qat")
    ap.add_argument("--ner", default="gliner", choices=["gliner", "natasha", "none"])
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--corporate", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--think", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--custom-terms", default=None)
    ap.add_argument("--max-chars", type=int, default=None,
                    help="Переопределить LLMConfig.max_chars (по умолчанию 3000)")
    ap.add_argument("--second-pass", action=argparse.BooleanOptionalAction, default=True,
                    help="Включить второй проход LLM-детектора (second-pass); "
                         "не действует с --compare")
    ap.add_argument("--recall", action=argparse.BooleanOptionalAction, default=True,
                    help="Включить recall-проверку в review; не действует с --compare")
    ap.add_argument("--review", action=argparse.BooleanOptionalAction, default=True,
                    help="Включить review найденных спанов; не действует с --compare")
    ap.add_argument("--compare", action="store_true",
                    help="Прогнать три конфигурации подряд и сравнить время")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        ap.error(f"Файл не найден: {src}")

    from anonymizer.documents import read_text

    text = read_text(src)
    print(f"Документ: {src.name}, {len(text)} символов", file=sys.stderr)

    _install_probes()

    if not args.compare:
        if args.second_pass and args.recall and args.review:
            title = "полный конвейер (как на сервере по умолчанию)"
        else:
            title = (
                "конвейер: "
                f"second-pass={'вкл' if args.second_pass else 'выкл'}, "
                f"recall={'вкл' if args.recall else 'выкл'}, "
                f"review={'вкл' if args.review else 'выкл'}"
            )
        _run_once(
            _build(args, second_pass=args.second_pass, recall=args.recall,
                   review_on=args.review),
            text, title,
        )
        return

    configs = [
        ("полный конвейер", dict(second_pass=True, recall=True, review_on=True)),
        ("без second-pass", dict(second_pass=False, recall=True, review_on=True)),
        ("без second-pass и recall", dict(second_pass=False, recall=False, review_on=True)),
    ]
    results: list[tuple[str, float]] = []
    for title, kw in configs:
        results.append((title, _run_once(_build(args, **kw), text, title)))

    print("\n" + "=" * 72)
    print("СРАВНЕНИЕ")
    print("=" * 72)
    base = results[0][1]
    for title, sec in results:
        print(f"  {title:<28}{sec:>8.1f} с   ({sec / base * 100:>3.0f}% от полного)")


if __name__ == "__main__":
    main()
