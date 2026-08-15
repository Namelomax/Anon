"""Нагрузочный тест бэкенда анонимизации: сколько клиентов сервер держит
одновременно, какой у него потолок пропускной способности и на каком числе
клиентов всё начинает разваливаться.

Два РЕЖИМА (--mode), два разных протокола за одним и тем же понятием
«клиент отправил документ и дождался результата»:

  * backend (по умолчанию) — напрямую в anonymizer/server.py: JSON-тело,
    POST /jobs/anonymize-file -> {"job_id": ...}, поллинг GET /jobs/<id> ->
    {"status", "result", "error"}. Работает ТОЛЬКО если бэкенд слушает
    адрес, до которого харнесс реально достаёт (в проде — только
    127.0.0.1, т.е. с внешней машины backend-режим недостижим).
  * site — через Next.js-сайт (anonymizer/web/app/api/anonymize/route.ts):
    multipart/form-data с одним полем "file", POST /api/anonymize ->
    202 {"jobId": ..., "done": false} (camelCase!), поллинг
    GET /api/anonymize?jobId=... ВСЕГДА 200, тело {"done": false} пока не
    готово, {"done": true, ...поля результата ПРЯМО В КОРНЕ объекта, БЕЗ
    вложенности под "result"} по готовности, {"done": false, "cancelled":
    true} при отмене; ошибка задания — HTTP 500 {"error": ...}; HTTP 404
    значит «сервер забыл задачу» (TTL/рестарт) — считается ошибкой, а не
    поводом для повтора. Это единственный путь, реально достижимый снаружи
    (см. https://anon.cpcore.ru).

Оба режима гоняют ТОЛЬКО асинхронный job-API, а не синхронный
/anonymize-file: синхронный запрос на реальном документе занимает минуты, а
шлюз перед сервером обрывает любой ОТДЕЛЬНЫЙ HTTP-запрос примерно через 100с
(см. docstring anonymizer/server.py про /jobs/*) — синхронный вызов в такой
сценарий просто не проходит. Документ читается один раз, тело запроса (JSON
с base64 для backend, multipart для site) собирается РОВНО ОДИН раз и
переиспользуется для всех клиентов на всех уровнях: иначе тест мерил бы
скорость диска/кодирования, а не сервера.

Уровни («сколько клиентов одновременно») идут ПОСЛЕДОВАТЕЛЬНО, и каждый
полностью стекает (все клиенты дошли до done/error/cancelled), прежде чем
начинается следующий. Внутри уровня все клиенты стартуют максимально
одновременно — через threading.Barrier, чтобы разброс запуска потоков сам
по себе не размазывал всплеск нагрузки по времени. После каждого уровня
рампа ОБРЫВАЕТСЯ (если не передан --no-abort) и репортится предыдущий
уровень как последний работоспособный, если: error rate > 20%, у кого-то из
клиентов HTTP 5xx, или кто-то упёрся в --timeout — это и есть способ найти
точку излома, а не просто померить пару точек «на глаз».

Запуск (из корня репозитория):

    # напрямую в бэкенд (доступен только с самого сервера / из локальной сети)
    python anonymizer/loadtest.py --url http://127.0.0.1:8000 \\
        --file anonymizer/test_samples/sample2_meeting_protocol.txt

    # через сайт (единственный путь, реально достижимый снаружи)
    python anonymizer/loadtest.py --mode site --url https://anon.cpcore.ru \\
        --file mydoc.docx --levels 1,5,10,20 --repeat 3 --out results.json

    # проверить сам харнесс (сабмит/поллинг/агрегация/логика обрыва обоих
    # протоколов) без сети, без ключа и без реального сервера
    python anonymizer/loadtest.py --self-test

Ctrl-C ДО выхода отменяет все ещё не завершившиеся задания через DELETE —
иначе брошенный на середине тест продолжает жрать платные апстрим-вызовы на
сервере в фоне уже без наблюдателя.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import http.server
import json
import mimetypes
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

# --- HTTP-слой: стандартная библиотека, свои соединения (НЕ http_pool.py —
# семафоры http_pool ограничивают СЕРВЕР, а не клиента; если харнесс встанет
# на них же, он будет душить сам себя и испортит замер). ------------------

# Таймаут ОДНОГО HTTP-вызова (сокет), отдельно от --timeout (общего бюджета
# ожидания одного клиента до терминального статуса задания). Реальный вызов
# ограничивается ещё и оставшимся временем до дедлайна клиента — см.
# _client_worker — так что --timeout меньше этого значения тоже работает
# как ожидается, а не блокируется на все 60с.
_HTTP_CALL_TIMEOUT = 60.0

_TERMINAL_STATUSES = ("done", "error", "cancelled")


def _request(
    method: str, url: str, body: bytes | None, key: str | None, timeout: float,
    headers: dict | None = None,
) -> tuple[int, bytes]:
    """Один HTTP-вызов через urllib. Возвращает (код, тело) ДЛЯ ЛЮБОГО кода
    ответа, включая 4xx/5xx (как http.client, в отличие от голого
    urlopen — HTTPError перехватывается и разворачивается здесь же). Если
    запрос вообще не смог дойти до сервера (отказ в соединении, DNS, наш
    собственный таймаут сокета), исключение (OSError) пробрасывается
    вызывающему — это принципиально другой случай, чем «сервер ответил
    ошибкой», и вызывающий код их различает.

    ``headers`` по умолчанию — только Content-Type: application/json (нужно
    и для GET/DELETE без тела, и это же значение раньше было единственным);
    submit-запрос site-режима передаёт свой multipart Content-Type явно и
    полностью его переопределяет."""
    hdrs = dict(headers) if headers is not None else {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --- Кодирование запроса постановки задачи: JSON (backend) / multipart (site)

def _guess_content_type(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"


def _encode_multipart(filename: str, content: bytes) -> tuple[bytes, str]:
    """Ручная сборка multipart/form-data тела (только stdlib, без cgi —
    в 3.13 его и нет — и без requests): один part с именем "file", ровно то,
    что браузер шлёт на /api/anonymize (см.
    anonymizer/web/app/api/anonymize/route.ts: ``form.get("file")``).
    Возвращает (тело, boundary); boundary кладётся вызывающим в заголовок
    Content-Type. Собирается ОДИН раз при старте и переиспользуется для
    каждого клиента — как и JSON-тело в backend-режиме."""
    boundary = uuid.uuid4().hex  # достаточно уникально, чтобы не встретиться в теле файла
    content_type = _guess_content_type(filename)
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + content + tail, boundary


@dataclass
class SubmitRequest:
    """Заранее собранный (один раз, при старте) POST-запрос постановки
    задачи — путь, готовое тело и заголовки. Одинаковый для всех клиентов
    всех уровней; см. модульный docstring про то, зачем это важно."""

    path: str
    body: bytes
    headers: dict


@dataclass
class PollOutcome:
    """Результат разбора ОДНОГО ответа на поллинг в терминах, общих для
    обоих протоколов, — то немногое, что реально нужно ``_client_worker``,
    чтобы не знать, backend перед ним или site."""

    kind: str  # running | done | cancelled | error
    result: dict | None = None
    error: str | None = None
    http_status: int | None = None


# --- Протоколы -----------------------------------------------------------

class BackendProtocol:
    """Протокол anonymizer/server.py напрямую: JSON, ``job_id`` (snake_case),
    результат вложен под ``result``. См. модульный docstring и server.py."""

    name = "backend"

    def build_submit(self, filename: str, raw_bytes: bytes) -> SubmitRequest:
        body = json.dumps({
            "filename": filename,
            "file_base64": base64.b64encode(raw_bytes).decode("ascii"),
        }).encode("utf-8")
        return SubmitRequest(
            path="/jobs/anonymize-file", body=body,
            headers={"Content-Type": "application/json"},
        )

    def parse_submit_response(self, body: bytes) -> str:
        return json.loads(body)["job_id"]

    def poll_path(self, job_id: str) -> str:
        return f"/jobs/{job_id}"

    def parse_poll(self, status: int, body: bytes) -> PollOutcome:
        if status != 200:
            return PollOutcome(
                kind="error", http_status=status,
                error=f"HTTP {status}: {body[:200]!r}",
            )
        payload = json.loads(body)  # ValueError бросается наверх -> клиент повторит опрос
        st = payload.get("status")
        if st == "done":
            return PollOutcome(kind="done", result=payload.get("result"))
        if st == "error":
            return PollOutcome(kind="error", error=payload.get("error"))
        if st == "cancelled":
            return PollOutcome(kind="cancelled")
        return PollOutcome(kind="running")

    def cancel_path(self, job_id: str) -> str:
        return f"/jobs/{job_id}"


class SiteProtocol:
    """Протокол сайта (anonymizer/web/app/api/anonymize/route.ts): multipart
    на submit, camelCase ``jobId``, поллинг ВСЕГДА отвечает HTTP 200, пока не
    готово (``{"done": false}``), итог — плоский объект (``{"done": true,
    ...поля результата на верхнем уровне}``), отмена — ``{"done": false,
    "cancelled": true}``, ошибка задания — HTTP 500 ``{"error": ...}``,
    забытая задача (TTL/рестарт бэкенда за сайтом) — HTTP 404, считается
    ошибкой, НЕ поводом для повтора опроса."""

    name = "site"

    def build_submit(self, filename: str, raw_bytes: bytes) -> SubmitRequest:
        body, boundary = _encode_multipart(filename, raw_bytes)
        return SubmitRequest(
            path="/api/anonymize", body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def parse_submit_response(self, body: bytes) -> str:
        return json.loads(body)["jobId"]

    def poll_path(self, job_id: str) -> str:
        return f"/api/anonymize?jobId={job_id}"

    def parse_poll(self, status: int, body: bytes) -> PollOutcome:
        if status == 404:
            return PollOutcome(
                kind="error", http_status=404,
                error=_best_effort_error(body) or "задача забыта сервером (TTL/рестарт)",
            )
        if status == 500:
            return PollOutcome(
                kind="error", http_status=500,
                error=_best_effort_error(body) or "HTTP 500",
            )
        if status != 200:
            return PollOutcome(
                kind="error", http_status=status,
                error=f"HTTP {status}: {body[:200]!r}",
            )
        payload = json.loads(body)  # ValueError бросается наверх -> клиент повторит опрос
        if payload.get("cancelled"):
            return PollOutcome(kind="cancelled")
        if payload.get("done"):
            result = dict(payload)
            result.pop("done", None)  # остальное — поля результата, на верхнем уровне
            return PollOutcome(kind="done", result=result)
        return PollOutcome(kind="running")

    def cancel_path(self, job_id: str) -> str:
        return f"/api/anonymize?jobId={job_id}"


def _best_effort_error(body: bytes) -> str | None:
    """Пытается вытащить поле "error" из тела ответа; используется только
    для НЕ-200 ответов site-режима, где тело уже классифицировано по коду —
    невалидный JSON здесь не повод повторять опрос, просто нет текста ошибки."""
    try:
        return json.loads(body).get("error")
    except ValueError:
        return None


PROTOCOLS: dict[str, object] = {"backend": BackendProtocol(), "site": SiteProtocol()}


# --- Данные по каждому клиенту / уровню -------------------------------------

@dataclass
class ClientRecord:
    """Итог одного клиента одного прогона: сколько ждал до 202, сколько ждал
    до терминального статуса, чем всё кончилось."""

    job_id: str | None
    submit_latency: float  # секунды до кода 202 на submit
    wait_time: float  # секунды от старта до терминального статуса (или таймаута)
    status: str  # done | error | cancelled | timeout | http_error | connection_error
    http_status: int | None = None  # код ответа, если сам HTTP-запрос вернул ошибку
    server_elapsed: float | None = None  # elapsed_seconds сервера (только done)
    warning_kinds: list[str] = field(default_factory=list)  # w["kind"] для каждого warning
    error: str | None = None  # текст ошибки (job.error, тело неудачного ответа, ...)


@dataclass
class LevelResult:
    """Сырые данные одного уровня рампы (все повторы --repeat вместе)."""

    level: int  # число одновременных клиентов
    repeats: int
    doc_chars: int
    duration_seconds: float  # суммарное настенное время всех повторов уровня
    records: list[ClientRecord] = field(default_factory=list)


@dataclass
class LevelSummary:
    """Агрегат по уровню — то, что идёт в таблицу и в отчёт."""

    level: int
    client_count: int
    ok: int
    error: int
    submit_p50: float
    wait_p50: float
    wait_p95: float
    wait_max: float
    docs_per_min: float
    pages_per_min: float
    warning_histogram: dict[str, int]


# --- Чистая арифметика агрегации — без сети, легко тестируется юнит-тестами -

def _percentile(values: list[float], pct: float) -> float:
    """Перцентиль с линейной интерполяцией между соседними отсчётами (как у
    numpy по умолчанию). Пустой список -> 0.0 (а не деление на ноль/исключение),
    чтобы уровень с одними ошибками не ронял отчёт."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] * (c - k) + s[c] * (k - f)


def _throughput(
    client_count: int, duration_seconds: float, doc_chars: int, chars_per_page: int
) -> tuple[float, float]:
    """(документов/мин, страниц/мин). Страница — это --chars-per-page символов
    исходного документа; страниц/мин = документов/мин * страниц-в-документе."""
    if duration_seconds <= 0 or client_count == 0:
        return 0.0, 0.0
    docs_per_min = client_count / (duration_seconds / 60.0)
    pages_per_doc = (doc_chars / chars_per_page) if chars_per_page > 0 else 0.0
    return docs_per_min, docs_per_min * pages_per_doc


def _warning_histogram(records: list[ClientRecord]) -> dict[str, int]:
    """Сколько раз каждый kind предупреждения встретился суммарно по всем
    клиентам уровня (у одного клиента warnings может быть несколько)."""
    hist: dict[str, int] = {}
    for r in records:
        for kind in r.warning_kinds:
            hist[kind] = hist.get(kind, 0) + 1
    return hist


def summarize_level(
    records: list[ClientRecord], duration_seconds: float, doc_chars: int, chars_per_page: int
) -> LevelSummary:
    client_count = len(records)
    ok = sum(1 for r in records if r.status == "done")
    docs_per_min, pages_per_min = _throughput(
        client_count, duration_seconds, doc_chars, chars_per_page
    )
    waits = [r.wait_time for r in records]
    return LevelSummary(
        level=client_count,
        client_count=client_count,
        ok=ok,
        error=client_count - ok,
        submit_p50=_percentile([r.submit_latency for r in records], 50),
        wait_p50=_percentile(waits, 50),
        wait_p95=_percentile(waits, 95),
        wait_max=max(waits) if waits else 0.0,
        docs_per_min=docs_per_min,
        pages_per_min=pages_per_min,
        warning_histogram=_warning_histogram(records),
    )


def _abort_reason(records: list[ClientRecord], timeout: float) -> str | None:
    """Есть ли причина остановить рампу после этого уровня: HTTP 5xx у
    кого-то из клиентов, кто-то упёрся в --timeout, или error rate > 20%.
    Проверяется именно в этом порядке — от самой «жёсткой» причины к самой
    мягкой, — так что при нескольких одновременных проблемах в сообщении
    называется наиболее конкретная; порядок зафиксирован юнит-тестами.
    None, если уровень здоровый и рампу можно продолжать."""
    client_count = len(records)
    if client_count == 0:
        return None

    five_xx = [r for r in records if r.http_status is not None and r.http_status >= 500]
    if five_xx:
        return f"{len(five_xx)} из {client_count} клиент(ов) получили HTTP 5xx"

    timed_out = [r for r in records if r.status == "timeout"]
    if timed_out:
        return (
            f"{len(timed_out)} из {client_count} клиент(ов) не дождались "
            f"терминального статуса за --timeout={timeout:.0f}с"
        )

    errors = client_count - sum(1 for r in records if r.status == "done")
    error_rate = errors / client_count
    if error_rate > 0.2:
        return f"error rate {error_rate:.0%} > 20% ({errors}/{client_count})"

    return None


# --- Один клиент: submit -> поллинг -> терминальный статус ------------------

def _client_worker(
    base_url: str,
    key: str | None,
    protocol,
    submit_request: SubmitRequest,
    poll_interval: float,
    timeout: float,
    barrier: threading.Barrier,
    registry: set,
    registry_lock: threading.Lock,
) -> ClientRecord:
    """Единственная реализация клиента для ОБОИХ протоколов: всё, что
    отличает backend от site, спрятано за ``protocol`` (см. BackendProtocol/
    SiteProtocol выше) и заранее собранным ``submit_request``."""
    barrier.wait()  # все клиенты уровня стартуют настолько одновременно, насколько может ОС
    t0 = time.perf_counter()
    deadline = t0 + timeout

    call_timeout = min(_HTTP_CALL_TIMEOUT, max(timeout, 1.0))
    try:
        status, body = _request(
            "POST", f"{base_url}{submit_request.path}", submit_request.body, key,
            call_timeout, headers=submit_request.headers,
        )
    except OSError as exc:
        # Соединение вообще не открылось (отказ, DNS, наш же таймаут) —
        # принципиально другой случай, чем ответ сервера с кодом ошибки.
        elapsed = time.perf_counter() - t0
        return ClientRecord(
            job_id=None, submit_latency=elapsed, wait_time=elapsed,
            status="connection_error", error=str(exc),
        )
    submit_latency = time.perf_counter() - t0

    if status != 202:
        return ClientRecord(
            job_id=None, submit_latency=submit_latency, wait_time=submit_latency,
            status="http_error", http_status=status,
            error=body.decode("utf-8", "replace")[:200],
        )

    try:
        job_id = protocol.parse_submit_response(body)
    except (ValueError, KeyError) as exc:
        return ClientRecord(
            job_id=None, submit_latency=submit_latency, wait_time=submit_latency,
            status="error", error=f"ответ на submit без id задания: {exc}",
        )

    with registry_lock:
        registry.add(job_id)

    result: dict | None = None
    error: str | None = None
    final_status = "timeout"  # если цикл ниже выйдет по дедлайну, так и останется
    http_status: int | None = None

    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        call_timeout = min(_HTTP_CALL_TIMEOUT, max(remaining, 1.0))
        try:
            code, body = _request(
                "GET", f"{base_url}{protocol.poll_path(job_id)}", None, key, call_timeout
            )
        except OSError:
            # Транзиентная сетевая проблема на поллинге — не считаем это
            # HTTP 5xx (сервер вообще не ответил) и не роняем клиента,
            # просто пробуем ещё раз до истечения --timeout.
            time.sleep(min(poll_interval, max(deadline - time.perf_counter(), 0)))
            continue

        try:
            outcome = protocol.parse_poll(code, body)
        except ValueError:
            # Невалидный JSON в теле статус-200 ответа — тоже транзиентно,
            # тот же выбор, что и раньше для backend-режима.
            time.sleep(min(poll_interval, max(deadline - time.perf_counter(), 0)))
            continue

        if outcome.kind == "running":
            time.sleep(min(poll_interval, max(deadline - time.perf_counter(), 0)))
            continue

        final_status = outcome.kind
        result = outcome.result
        error = outcome.error
        http_status = outcome.http_status
        break

    with registry_lock:
        registry.discard(job_id)

    wait_time = time.perf_counter() - t0
    warnings = (result or {}).get("warnings") or []
    warning_kinds = [w.get("kind") for w in warnings if isinstance(w, dict) and w.get("kind")]
    server_elapsed = (result or {}).get("elapsed_seconds") if result else None

    return ClientRecord(
        job_id=job_id, submit_latency=submit_latency, wait_time=wait_time,
        status=final_status, http_status=http_status, server_elapsed=server_elapsed,
        warning_kinds=warning_kinds, error=error,
    )


def _cancel_job(base_url: str, key: str | None, protocol, job_id: str) -> None:
    try:
        _request("DELETE", f"{base_url}{protocol.cancel_path(job_id)}", None, key, 10.0)
    except OSError:
        pass  # best-effort — сервер и так скоро останется без наблюдателя


def _cancel_all_inflight(
    base_url: str, key: str | None, protocol, registry: set, registry_lock: threading.Lock
) -> list[str]:
    with registry_lock:
        ids = sorted(registry)
        registry.clear()
    for job_id in ids:
        _cancel_job(base_url, key, protocol, job_id)
    print(f"\n[Ctrl-C] Отменено незавершённых заданий: {len(ids)} -> {ids}", flush=True)
    return ids


# --- Один уровень рампы: N клиентов, --repeat повторов -----------------------

def run_level(
    base_url: str,
    key: str | None,
    protocol,
    submit_request: SubmitRequest,
    level: int,
    repeat: int,
    poll_interval: float,
    timeout: float,
    registry: set,
    registry_lock: threading.Lock,
) -> tuple[list[ClientRecord], float]:
    """Прогоняет уровень (level клиентов) repeat раз ПОСЛЕДОВАТЕЛЬНО,
    возвращает (все записи всех повторов, суммарное настенное время всех
    повторов). Каждый повтор уровня полностью стекает, прежде чем
    начинается следующий — точно так же, как уровни друг за другом."""
    all_records: list[ClientRecord] = []
    total_duration = 0.0

    for _ in range(repeat):
        barrier = threading.Barrier(level)
        records: list[ClientRecord | None] = [None] * level

        def _make_target(i: int):
            def _run():
                records[i] = _client_worker(
                    base_url, key, protocol, submit_request, poll_interval, timeout,
                    barrier, registry, registry_lock,
                )

            return _run

        threads = [
            threading.Thread(target=_make_target(i), daemon=True) for i in range(level)
        ]
        t_start = time.perf_counter()
        for th in threads:
            th.start()
        try:
            for th in threads:
                # join() с таймаутом в цикле, а НЕ голый join() без таймаута:
                # блокирующий join() без таймаута на Windows держит GIL так,
                # что Ctrl-C не долетает до основного потока, пока join не
                # завершится сам — то есть отмена заданий работать не будет.
                # Короткий таймаут возвращает управление интерпретатору
                # достаточно часто, чтобы KeyboardInterrupt поймался быстро.
                while th.is_alive():
                    th.join(timeout=0.2)
        except KeyboardInterrupt:
            _cancel_all_inflight(base_url, key, protocol, registry, registry_lock)
            raise
        total_duration += time.perf_counter() - t_start
        all_records.extend(records)  # type: ignore[arg-type]

    return all_records, total_duration


# --- Вся рампа: уровни один за другим, с охлаждением и логикой обрыва -------

def run_ramp(
    *,
    base_url: str,
    key: str | None,
    protocol,
    submit_request: SubmitRequest,
    doc_chars: int,
    levels: list[int],
    repeat: int,
    poll_interval: float,
    timeout: float,
    cooldown: float,
    chars_per_page: int,
    abort_enabled: bool,
) -> tuple[list[LevelSummary], list[LevelResult]]:
    """Прогоняет levels ПОСЛЕДОВАТЕЛЬНО (каждый уровень стекает целиком,
    прежде чем стартует следующий), с паузой --cooldown между ними (кроме
    как после последнего). После каждого уровня проверяет критерии обрыва
    (см. _abort_reason); при найденной причине печатает её всегда, а
    останавливает рампу, только если abort_enabled (т.е. НЕ передан
    --no-abort). ``protocol``/``submit_request`` — см. BackendProtocol/
    SiteProtocol: один и тот же прогон обслуживает оба режима."""
    registry: set = set()
    registry_lock = threading.Lock()
    summaries: list[LevelSummary] = []
    raw_results: list[LevelResult] = []

    for i, level in enumerate(levels):
        print(f"\n=== Уровень: {level} клиент(ов), повторов: {repeat} ===", flush=True)
        records, duration = run_level(
            base_url, key, protocol, submit_request, level, repeat, poll_interval, timeout,
            registry, registry_lock,
        )
        summary = summarize_level(records, duration, doc_chars, chars_per_page)
        summaries.append(summary)
        raw_results.append(
            LevelResult(
                level=level, repeats=repeat, doc_chars=doc_chars,
                duration_seconds=duration, records=records,
            )
        )
        print(
            f"    ok={summary.ok} error={summary.error}  "
            f"wait p50={summary.wait_p50:.1f}с p95={summary.wait_p95:.1f}с max={summary.wait_max:.1f}с  "
            f"{summary.docs_per_min:.2f} докум/мин  {summary.pages_per_min:.1f} стр/мин",
            flush=True,
        )

        reason = _abort_reason(records, timeout)
        if reason:
            print(f"    [!] Причина остановки рампы: {reason}", flush=True)
            if abort_enabled:
                prev = levels[i - 1] if i > 0 else None
                print(
                    "[ABORT] Останавливаю рампу. Последний работоспособный уровень: "
                    + (str(prev) if prev is not None else "ни один — сломался уже первый уровень"),
                    flush=True,
                )
                break
            print("    --no-abort: продолжаю рампу несмотря на это.", flush=True)

        is_last = i == len(levels) - 1
        if not is_last and cooldown > 0:
            print(f"    Пауза {cooldown:.0f}с перед следующим уровнем…", flush=True)
            time.sleep(cooldown)

    return summaries, raw_results


# --- Вывод -------------------------------------------------------------------

def print_table(summaries: list[LevelSummary]) -> None:
    header = (
        f"{'клиентов':>9} {'ok':>5} {'err':>5} {'submit p50':>11} "
        f"{'wait p50':>9} {'wait p95':>9} {'wait max':>9} "
        f"{'докум/мин':>10} {'стр/мин':>8}  предупреждения"
    )
    print("\n" + "=" * len(header))
    print("РЕЗУЛЬТАТЫ НАГРУЗОЧНОГО ТЕСТА")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for s in summaries:
        warn = ", ".join(f"{k}={v}" for k, v in sorted(s.warning_histogram.items())) or "—"
        print(
            f"{s.client_count:>9} {s.ok:>5} {s.error:>5} {s.submit_p50:>10.2f}с "
            f"{s.wait_p50:>8.1f}с {s.wait_p95:>8.1f}с {s.wait_max:>8.1f}с "
            f"{s.docs_per_min:>10.2f} {s.pages_per_min:>8.1f}  {warn}"
        )


def _write_json(path: str, summaries: list[LevelSummary], raw_results: list[LevelResult]) -> None:
    data = {
        "summaries": [dataclasses.asdict(s) for s in summaries],
        "levels": [
            {
                "level": lr.level,
                "repeats": lr.repeats,
                "doc_chars": lr.doc_chars,
                "duration_seconds": lr.duration_seconds,
                "records": [dataclasses.asdict(r) for r in lr.records],
            }
            for lr in raw_results
        ],
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Заглушечный сервер для --self-test --------------------------------------

class _StubState:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()


def _stub_multipart_filename(body: bytes) -> str | None:
    """Грубый разбор multipart ТОЛЬКО для нужд заглушки self-test — достаёт
    filename= из первого Content-Disposition. Не претендует на общность,
    достаточно ровно для того, что шлёт _encode_multipart выше."""
    marker = b'filename="'
    i = body.find(marker)
    if i == -1:
        return None
    j = body.find(b'"', i + len(marker))
    if j == -1:
        return None
    return body[i + len(marker):j].decode("utf-8", "replace")


def _make_stub_handler(state: _StubState, *, delay: float):
    """Реализует ОБА протокола (см. BackendProtocol/SiteProtocol): маршруты
    backend (POST /jobs/anonymize-file, GET/DELETE /jobs/<id>) и site
    (POST/GET/DELETE /api/anonymize[?jobId=...]) — на одном общем хранилище
    заданий, чтобы --self-test мог прогнать рампу в обоих режимах против
    одной и той же заглушки. Каждое задание «обрабатывается» delay секунд в
    фоновом потоке и всегда завершается success с одним тестовым warning —
    этого достаточно, чтобы прогнать сабмит/поллинг/агрегацию харнесса
    целиком в каждом режиме, не трогая сеть."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _finish_job_later(self, job_id: str, filename: str) -> None:
            def _worker() -> None:
                time.sleep(delay)
                with state.lock:
                    job = state.jobs.get(job_id)
                    if job is None or job["status"] == "cancelled":
                        return
                    job["status"] = "done"
                    job["result"] = {
                        "anonymized_text": "***",
                        "mapping": {},
                        "summary": {},
                        "elapsed_seconds": delay,
                        "warnings": [{"kind": "test_warning"}],
                        "document_base64": base64.b64encode(b"stub").decode("ascii"),
                        "document_name": filename,
                    }

            threading.Thread(target=_worker, daemon=True).start()

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/")
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n)

            if path.endswith("jobs/anonymize-file"):
                data = json.loads(body or b"{}")
                job_id = uuid.uuid4().hex
                with state.lock:
                    state.jobs[job_id] = {"status": "pending", "result": None, "error": None}
                self._finish_job_later(job_id, data.get("filename", "stub.txt"))
                self._send(202, {"job_id": job_id})
                return

            if path.endswith("/api/anonymize"):
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type or b'filename="' not in body:
                    # Ровно то, чем встретит регрессию сломанный _encode_multipart —
                    # self-test должен упасть громко, а не тихо проглотить.
                    self._send(400, {"error": "expected multipart/form-data with a file part"})
                    return
                filename = _stub_multipart_filename(body) or "stub.txt"
                job_id = uuid.uuid4().hex
                with state.lock:
                    state.jobs[job_id] = {"status": "pending", "result": None, "error": None}
                self._finish_job_later(job_id, filename)
                self._send(202, {"jobId": job_id, "done": False})
                return

            self._send(404, {"error": "not found"})

        def do_GET(self) -> None:
            raw_path, _, query = self.path.partition("?")
            path = raw_path.rstrip("/")

            if "/jobs/" in path:
                job_id = path.rsplit("/jobs/", 1)[1]
                with state.lock:
                    job = state.jobs.get(job_id)
                    payload = None if job is None else dict(job)
                if payload is None:
                    self._send(404, {"error": "unknown job"})
                    return
                self._send(200, payload)
                return

            if path.endswith("/api/anonymize"):
                job_id = parse_qs(query).get("jobId", [None])[0]
                with state.lock:
                    job = state.jobs.get(job_id) if job_id else None
                    snapshot = None if job is None else dict(job)
                if snapshot is None:
                    self._send(404, {"error": f"забытая задача {job_id}"})
                    return
                if snapshot["status"] == "done":
                    self._send(200, {"done": True, **(snapshot["result"] or {})})
                elif snapshot["status"] == "cancelled":
                    self._send(200, {"done": False, "cancelled": True})
                elif snapshot["status"] == "error":
                    self._send(500, {"error": snapshot.get("error") or "unknown error"})
                else:
                    self._send(200, {"done": False})
                return

            self._send(404, {"error": "not found"})

        def do_DELETE(self) -> None:
            raw_path, _, query = self.path.partition("?")
            path = raw_path.rstrip("/")

            if "/jobs/" in path:
                job_id = path.rsplit("/jobs/", 1)[1]
            elif path.endswith("/api/anonymize"):
                job_id = parse_qs(query).get("jobId", [None])[0]
            else:
                self._send(404, {"error": "not found"})
                return

            with state.lock:
                job = state.jobs.get(job_id) if job_id else None
                if job is None:
                    self._send(404, {"error": "unknown job"})
                    return
                if job["status"] not in _TERMINAL_STATUSES:
                    job["status"] = "cancelled"
                status = job["status"]
            self._send(200, {"status": status})

        def log_message(self, *_a) -> None:  # тихо, как в server.py
            pass

    return _Handler


def _self_test() -> bool:
    """Проверяет харнесс без сети, ключа и реального сервера: чистую
    арифметику агрегации, логику обрыва рампы на синтетических записях, а
    затем полный прогон рампы (сабмит, поллинг, агрегация) в ОБОИХ режимах
    (backend и site) против одного встроенного заглушечного HTTP-сервера,
    реализующего оба протокола. Возвращает True, если все проверки прошли."""
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if cond:
            print(f"[SELF-TEST OK]    {msg}")
        else:
            ok = False
            print(f"[SELF-TEST FAIL]  {msg}")

    # 1) Чистая арифметика агрегации.
    check(_percentile([], 50) == 0.0, "_percentile на пустом списке -> 0.0")
    check(_percentile([10.0], 50) == 10.0, "_percentile на списке из одного значения")
    check(abs(_percentile([1, 2, 3, 4], 50) - 2.5) < 1e-9, "_percentile p50 на [1,2,3,4] == 2.5")
    check(abs(_percentile([1, 2, 3, 4], 100) - 4.0) < 1e-9, "_percentile p100 == максимум")

    docs_per_min, pages_per_min = _throughput(
        client_count=10, duration_seconds=60.0, doc_chars=3600, chars_per_page=1800
    )
    check(abs(docs_per_min - 10.0) < 1e-9, "_throughput: 10 клиентов за 60с = 10 докум/мин")
    check(abs(pages_per_min - 20.0) < 1e-9, "_throughput: 2 страницы/документ => 20 стр/мин")

    hist = _warning_histogram(
        [
            ClientRecord(job_id="a", submit_latency=0.1, wait_time=1.0, status="done",
                         warning_kinds=["review_dropped"]),
            ClientRecord(job_id="b", submit_latency=0.1, wait_time=1.0, status="done",
                         warning_kinds=["review_dropped", "chunk_failed"]),
        ]
    )
    check(hist == {"review_dropped": 2, "chunk_failed": 1}, "_warning_histogram суммирует kind по клиентам")

    # 2) Логика обрыва рампы — на синтетических записях, без сети.
    healthy = [
        ClientRecord(job_id=str(i), submit_latency=0.1, wait_time=1.0, status="done")
        for i in range(10)
    ]
    check(_abort_reason(healthy, timeout=900) is None, "_abort_reason: всё ok -> причины нет")

    high_errors = healthy[:7] + [
        ClientRecord(job_id=f"e{i}", submit_latency=0.1, wait_time=1.0, status="error")
        for i in range(3)
    ]
    reason = _abort_reason(high_errors, timeout=900)
    check(bool(reason) and "error rate" in reason, "_abort_reason: error rate 30% > 20% -> есть причина")

    has_5xx = healthy[:9] + [
        ClientRecord(job_id="x", submit_latency=0.1, wait_time=1.0, status="http_error", http_status=502)
    ]
    reason = _abort_reason(has_5xx, timeout=900)
    check(bool(reason) and "5xx" in reason, "_abort_reason: HTTP 5xx -> есть причина даже при error rate < 20%")

    has_timeout = healthy[:9] + [
        ClientRecord(job_id="y", submit_latency=0.1, wait_time=900.0, status="timeout")
    ]
    check(
        _abort_reason(has_timeout, timeout=900) is not None,
        "_abort_reason: клиент упёрся в --timeout -> есть причина",
    )

    # 2b) Протокольный слой — по одному быстрому дымовому чеку на каждый
    # протокол (подробные разборы — в тестах test_loadtest.py).
    backend_submit = BackendProtocol().build_submit("doc.txt", b"hello")
    check(backend_submit.path == "/jobs/anonymize-file", "BackendProtocol: путь submit")
    check(b"hello" not in backend_submit.body and b"aGVsbG8=" in backend_submit.body,
          "BackendProtocol: тело содержит base64, а не сырые байты")

    site_submit = SiteProtocol().build_submit("doc.txt", b"hello")
    check(b"hello" in site_submit.body, "SiteProtocol: тело содержит сырые байты (multipart, не base64)")
    check("multipart/form-data" in site_submit.headers.get("Content-Type", ""),
          "SiteProtocol: Content-Type — multipart/form-data")

    if not ok:
        print("\nЮнит-проверки провалились — прогон против заглушечного сервера пропущен.")
        return False

    # 3) Полный прогон рампы против встроенного заглушечного сервера — в
    # ОБОИХ режимах, чтобы регрессия любого из двух протоколов провалила
    # self-test громко, а не тихо.
    state = _StubState()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_stub_handler(state, delay=0.1))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    base_url = f"http://{host}:{port}"

    try:
        for mode_name, protocol in PROTOCOLS.items():
            print(f"\n--- self-test: режим {mode_name} ---", flush=True)
            submit_request = protocol.build_submit(
                "stub.txt", f"dummy content for self-test ({mode_name})".encode("utf-8")
            )
            levels = [1, 3, 5]
            summaries, _raw = run_ramp(
                base_url=base_url, key=None, protocol=protocol, submit_request=submit_request,
                doc_chars=1800 * 2, levels=levels, repeat=1,
                poll_interval=0.05, timeout=10.0, cooldown=0.1,
                chars_per_page=1800, abort_enabled=True,
            )

            check(
                len(summaries) == len(levels),
                f"[{mode_name}] рампа прошла все уровни без ложного обрыва",
            )
            for level, summary in zip(levels, summaries):
                check(summary.client_count == level, f"[{mode_name}] уровень {level}: client_count совпадает")
                check(summary.ok == level and summary.error == 0,
                      f"[{mode_name}] уровень {level}: все клиенты done")
                check(
                    summary.warning_histogram.get("test_warning") == level,
                    f"[{mode_name}] уровень {level}: гистограмма собрала test_warning от каждого клиента "
                    "(доказывает, что поля результата разобраны правильно — вложенные для backend, "
                    "плоские для site)",
                )
                check(summary.docs_per_min > 0, f"[{mode_name}] уровень {level}: докум/мин > 0")
                check(summary.pages_per_min > 0, f"[{mode_name}] уровень {level}: стр/мин > 0")

            print_table(summaries)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    print(f"\n{'SELF-TEST OK' if ok else 'SELF-TEST FAILED'}")
    return ok


# --- CLI -----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=None, help="Базовый URL сервера, напр. http://127.0.0.1:8000")
    ap.add_argument("--file", default=None, help="Документ .txt или .docx для нагрузочного теста")
    ap.add_argument("--key", default=None, help="Bearer-токен, если сервер за шлюзом с авторизацией")
    ap.add_argument(
        "--mode", choices=["backend", "site"], default="backend",
        help="backend (по умолчанию) — напрямую в anonymizer/server.py (JSON, job_id, "
             "результат под result; обычно достижим только с самого сервера/из локальной "
             "сети). site — через сайт anonymizer/web (POST/GET/DELETE /api/anonymize, "
             "multipart, camelCase jobId, результат на верхнем уровне ответа; единственный "
             "путь, реально достижимый снаружи).",
    )
    ap.add_argument(
        "--levels", default="1,3,5,10,15",
        help="Уровни рампы: число одновременных клиентов через запятую (по умолчанию 1,3,5,10,15)",
    )
    ap.add_argument("--repeat", type=int, default=1, help="Сколько раз повторить каждый уровень (по умолчанию 1)")
    ap.add_argument("--poll", type=float, default=2.0, help="Интервал опроса статуса задания, секунды (по умолчанию 2.0)")
    ap.add_argument("--timeout", type=float, default=900.0, help="Таймаут ожидания ОДНОГО клиента, секунды (по умолчанию 900)")
    ap.add_argument("--cooldown", type=float, default=30.0, help="Пауза между уровнями, секунды (по умолчанию 30)")
    ap.add_argument("--chars-per-page", type=int, default=1800, help="Символов на «страницу» для стр/мин (по умолчанию 1800)")
    ap.add_argument("--out", default=None, help="Путь для сохранения полных данных по клиентам в JSON")
    ap.add_argument("--no-abort", action="store_true", help="Не останавливать рампу при обнаружении критерия обрыва — пройти все уровни")
    ap.add_argument("--self-test", action="store_true", help="Прогнать харнесс (оба режима) против встроенного заглушечного сервера и выйти")
    args = ap.parse_args()

    if args.self_test:
        return 0 if _self_test() else 1

    if not args.url:
        ap.error("--url обязателен (кроме --self-test)")
    if not args.file:
        ap.error("--file обязателен (кроме --self-test)")

    src = Path(args.file)
    if not src.exists():
        ap.error(f"Файл не найден: {src}")

    raw_bytes = src.read_bytes()

    # Число символов документа — только для стр/мин, не для самого запроса.
    # Для .txt считаем точно; для .docx и прочих бинарных форматов точный
    # подсчёт потребовал бы той же экстракции текста, что и на сервере
    # (anonymizer.documents, python-docx), а харнесс намеренно не тянет такие
    # зависимости — берём число байт как грубую оценку размера документа.
    if src.suffix.lower() == ".txt":
        doc_chars = len(raw_bytes.decode("utf-8", errors="replace"))
    else:
        doc_chars = len(raw_bytes)

    try:
        levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]
    except ValueError:
        ap.error(f"--levels: не список целых чисел через запятую: {args.levels!r}")
        return 2
    if not levels:
        ap.error("--levels: пустой список уровней")

    base_url = args.url.rstrip("/")
    protocol = PROTOCOLS[args.mode]
    submit_request = protocol.build_submit(src.name, raw_bytes)  # собирается ОДИН раз, см. docstring

    print(
        f"Файл: {src.name}, {len(raw_bytes)} байт (~{doc_chars} симв.)  |  режим: {args.mode}  |  "
        f"уровни: {levels}  |  повторов: {args.repeat}  |  сервер: {base_url}"
    )

    try:
        summaries, raw_results = run_ramp(
            base_url=base_url, key=args.key, protocol=protocol, submit_request=submit_request,
            doc_chars=doc_chars, levels=levels, repeat=args.repeat,
            poll_interval=args.poll, timeout=args.timeout, cooldown=args.cooldown,
            chars_per_page=args.chars_per_page, abort_enabled=not args.no_abort,
        )
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl-C) — незавершённые задания отменены выше.", flush=True)
        return 130

    print_table(summaries)

    if args.out:
        _write_json(args.out, summaries, raw_results)
        print(f"\nПолные данные по клиентам сохранены: {args.out}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl-C).", flush=True)
        sys.exit(130)
