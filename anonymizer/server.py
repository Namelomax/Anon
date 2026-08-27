"""HTTP backend for the anonymizer — run it on the GPU host (JupyterHub).

The whole pipeline (regex + GLiNER on CUDA + LLM via local Ollama) runs here;
local clients (the Streamlit UI, the benchmark) just POST text and get back the
anonymized text, mapping and spans. This is the "thin client + remote GPU
backend" setup for demos.

Run on the hub (полный конвейер включён по умолчанию — флаги НЕ нужны):
    python anonymizer/server.py
    # или через обёртку с CUDA_VISIBLE_DEVICES=0:  bash anonymizer/run.sh

Флаги нужны только чтобы что-то ОТКЛЮЧИТЬ, например:
    python anonymizer/server.py --no-review          # без слоя проверки
    python anonymizer/server.py --no-llm --ner none   # только regex-детекторы
    python anonymizer/server.py --think               # включить reasoning LLM

Expose it through JupyterHub's proxy (like Ollama): the URL becomes
    https://<hub>/user/<id>/proxy/8000/
and clients authenticate with the JupyterHub Bearer token.

Все маршруты, кроме ``OPTIONS`` (CORS preflight) и ``GET /health`` (последний
без ключа отдаёт только ``{"status": "ok"}``, полную информацию — лишь
аутентифицированному вызову), требуют заголовок ``Authorization: Bearer
<секрет>``; см. ``ANONYMIZER_API_KEYS``/``ANONYMIZER_API_KEY`` и
``_authenticate`` ниже.

API:
    GET  /health           -> {"status": "ok", ...}
    POST /anonymize  {text, regex?, corporate?, ner?, llm?, review?, subject?}
         -> {anonymized_text, mapping, summary, spans:[{start,end,label,text}], stages}

Each pipeline stage (regex / corporate / ner / llm / review / subject) can be
toggled per request via optional booleans in the POST body; omitted flags
fall back to the server's start-up defaults. This lets the UI try e.g.
"GLiNER only, no regex" without a redeploy. ``review`` is the 4th, last
layer: it re-checks the spans produced by the other layers against their
context and un-masks obvious false positives (see ``review.py``); it only
has any effect if the server was started with ``--review``. ``subject``
adds the SUBJECT label (предмет договора) to the existing LLM detection
call — no extra pass, no extra time — and only has any effect if ``llm`` is
also on. It also switches ``review`` into subject mode: otherwise the two
layers work against each other, since the reviewer's default rules let it
unmask "product names" — which is exactly what this stage masks.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer import depersonalization_log  # noqa: E402
from anonymizer import usage_log  # noqa: E402
from anonymizer.engine import Anonymizer  # noqa: E402
from anonymizer.llm import Cancelled  # noqa: E402

_DETECTORS: dict = {}    # stage name -> list of detector objects (built once)
_DEFAULTS: dict = {}     # stage name -> bool (start-up default on/off)
_GLINER_CFG = None       # base GLiNERConfig, for per-request threshold overrides
_REVIEW_CFG = None       # ReviewConfig for the LLM review layer, or None if disabled
_INFO: dict = {}
_NER_BACKEND = "none"    # args.ner, set once in main() ("gliner"/"natasha"/"remote"/"none")

# _LOCK serializes model calls, but is only actually taken when the process
# holds a LOCAL model in memory (--ner gliner or --ner natasha): both are not
# thread-safe (GLiNER wraps torch; concurrent calls corrupt each other's
# state). With --ner remote or --ner none there is NO local model in the
# process at all — anonymize() just fans out HTTP requests — so this lock
# bought nothing but serialization. Measured on the live site with --ner
# remote: three simultaneous document uploads took 31.7 / 64.9 / 91.9s, a
# perfect staircase (91.9s ~= 3 * the ~28.9s single-document time), purely
# from queuing on this lock while every request waited for the previous one's
# _compose(...) + anon.anonymize(text) to finish. _NEEDS_MODEL_LOCK (set once
# in main() from the chosen --ner backend) controls whether _model_lock()
# below actually takes it; see _model_lock's docstring.
_LOCK = threading.Lock()
_NEEDS_MODEL_LOCK = False

_STAGE_NAMES = ("regex", "corporate", "glossary", "ner", "llm", "review", "second_pass", "subject")

# --- Async job store (for /jobs/anonymize-file) --------------------------
# The devtunnel relay in front of this server 504s any single request after
# ~100s, but the anonymization pipeline routinely takes longer than that on
# real documents. /jobs/anonymize-file returns immediately with a job id, and
# the caller polls /jobs/<id> — every individual HTTP request stays fast.
# Guarded by its own lock, separate from _LOCK (which serializes model calls):
# never hold both at once, and GET /jobs/<id> must never block on _LOCK, or
# polling would queue up behind a running job and defeat the whole point.
_JOBS: dict = {}          # job_id -> {"status", "result", "error", "created", "cancel"}
_JOBS_LOCK = threading.Lock()
_JOB_TTL_SECONDS = 10 * 60  # evict finished jobs after 10 minutes

_TERMINAL_STATUSES = ("done", "error", "cancelled")

# Источники, которым разрешён кросс-доменный доступ из браузера. По умолчанию
# пусто: текущая архитектура кросс-доменных браузерных запросов не делает
# (подробное обоснование — в Handler._cors). "*" намеренно не поддерживается.
_CORS_ALLOWED_ORIGINS = frozenset(
    o.strip()
    for o in (os.getenv("ANONYMIZER_CORS_ORIGINS") or "").split(",")
    if o.strip() and o.strip() != "*"
)

# Возвращать ли исходный текст найденных фрагментов в поле spans[].text.
# По умолчанию ВЫКЛЮЧЕНО: spans с исходными подстроками и смещениями являются
# самостоятельным ключом деанонимизации — по ним документ восстанавливается
# посимвольно даже без mapping. Хранить и передавать их вместе с результатом
# означает свести эффект маскирования к нулю (приказ РКН № 140, п. 1.6 и
# приложение № 2 п. 3). Клиент, которому текст спанов действительно нужен
# (anonymizer/remote_client.py), запрашивает его явным флагом.
_SPAN_TEXT_DEFAULT = (os.getenv("ANONYMIZER_SPAN_TEXT") or "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Удалять задание сразу после первой выдачи терминального результата.
# Выключено по умолчанию: клиенты с ретраями должны иметь возможность забрать
# результат повторно. См. комментарий в _handle_job_status.
_JOB_ONESHOT = (os.getenv("ANONYMIZER_JOB_ONESHOT") or "").strip().lower() in (
    "1", "true", "yes", "on",
)

# --- Аутентификация входящих запросов -------------------------------------
# Секрет -> имя субъекта (principal). Заполняется ОДИН РАЗ при старте
# (``_configure_auth``, вызывается из ``main()``); тесты подменяют напрямую.
# Пустой словарь по умолчанию — значит НИЧЕГО не аутентифицируется, что и
# требуется до явной настройки (см. ``_configure_auth`` про отказ стартовать
# без ключей).
_API_KEYS: dict = {}
# --allow-anonymous — единственный сознательный обход проверки, только для
# локальной разработки (см. _configure_auth).
_ALLOW_ANONYMOUS = False


def _load_api_keys() -> dict:
    """Прочитать секреты доступа из окружения: секрет -> имя субъекта (principal).

    ``ANONYMIZER_API_KEYS`` — список пар ``имя:секрет`` через запятую,
    например ``web:s3cr3t,cli:0th3r``; имя — принципал, попадающий в поле
    ``actor`` журнала обезличивания (см. ``depersonalization_log.py``).
    ``ANONYMIZER_API_KEY`` — устаревший одиночный секрет без имени, принципал
    для него всегда ``"default"``. Если заданы обе переменные, ключи из обеих
    объединяются в один словарь.

    Значения секретов НИКОГДА не печатаются и не логируются: при некорректной
    записи в ``ANONYMIZER_API_KEYS`` в stderr идёт только факт ошибки, без
    содержимого записи.
    """
    keys: dict = {}
    legacy = (os.getenv("ANONYMIZER_API_KEY") or "").strip()
    if legacy:
        keys[legacy] = "default"
    raw = os.getenv("ANONYMIZER_API_KEYS") or ""
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, secret = item.partition(":")
        name = name.strip()
        secret = secret.strip()
        if not sep or not name or not secret:
            print(
                "[server] игнорирую некорректную запись в ANONYMIZER_API_KEYS "
                "(ожидался формат имя:секрет)",
                file=sys.stderr,
            )
            continue
        keys[secret] = name
    return keys


def _authenticate(headers) -> str | None:
    """Проверить заголовок ``Authorization: Bearer <секрет>`` и вернуть имя
    аутентифицированного субъекта (principal), либо ``None``, если проверка
    не пройдена (заголовок отсутствует, неверный формат или секрет не
    совпал ни с одним из ``_API_KEYS``).

    Сравнение — ``hmac.compare_digest`` (постоянное время выполнения):
    обычное ``==`` прерывается на первом несовпадающем байте, что
    теоретически позволяет подобрать секрет по времени ответа
    (тайминг-атака). ``==`` для сравнения с секретом использовать нельзя
    нигде в этой функции.

    ``_ALLOW_ANONYMOUS`` (флаг ``--allow-anonymous``, только для локальной
    разработки — см. ``_configure_auth``) отключает проверку целиком и
    возвращает фиксированное имя ``"anonymous"``, не читая заголовок.
    """
    if _ALLOW_ANONYMOUS:
        return "anonymous"
    auth = headers.get("Authorization") or ""
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return None
    supplied = auth[len(prefix):].strip()
    if not supplied:
        return None
    for secret, name in _API_KEYS.items():
        if hmac.compare_digest(supplied, secret):
            return name
    return None


def _configure_auth(args) -> None:
    """Загрузить ключи доступа и, если ни один не задан, ОТКАЗАТЬСЯ СТАРТОВАТЬ.

    Дефолт «безопасность выключена, пока её явно не включат» здесь
    неприемлем: один забытый ``ANONYMIZER_API_KEY``/``ANONYMIZER_API_KEYS``
    означает бэкенд, открытый любому с сетевым доступом — тот слепо тратит
    бюджет владельца на вышестоящие модели и отдаёт ``/usage`` (токены,
    стоимость) кому угодно. Ошибка конфигурации ДОЛЖНА быть громкой и
    происходить при запуске процесса, а не тихо оставлять дверь открытой до
    первого инцидента — отсюда ``sys.exit(1)`` вместо запуска с пустым
    ``_API_KEYS`` (при котором ``_authenticate`` и так отвергла бы любой
    запрос, но молча, без объяснения при старте, что именно не так).

    Единственный сознательный обход — флаг ``--allow-anonymous`` для
    локальной разработки: он печатает громкое предупреждение в stderr при
    каждом запуске и полностью отключает проверку (см. ``_authenticate``).
    """
    global _API_KEYS, _ALLOW_ANONYMOUS
    _API_KEYS = _load_api_keys()
    _ALLOW_ANONYMOUS = bool(args.allow_anonymous)

    if _ALLOW_ANONYMOUS:
        print(
            "[server] ВНИМАНИЕ: сервер запущен с --allow-anonymous — проверка "
            "Authorization ПОЛНОСТЬЮ ОТКЛЮЧЕНА. Любой, у кого есть сетевой "
            "доступ к этому адресу, может отправлять документы на "
            "анонимизацию (за счёт бюджета владельца) и читать статистику "
            "/usage. Используйте только для локальной разработки, никогда — "
            "в проде.",
            file=sys.stderr,
        )
        return
    if not _API_KEYS:
        print(
            "[server] ОШИБКА: не задан ни один ключ доступа — сервер не "
            "будет запущен. Задайте ANONYMIZER_API_KEYS в формате "
            '"имя:секрет,имя2:секрет2" (по паре на клиента) либо, для '
            "одного секрета без разделения по клиентам, "
            "ANONYMIZER_API_KEY=<секрет> (субъект будет называться "
            "'default'). Тот же секрет должен быть прописан как "
            "ANONYMIZER_BACKEND_KEY на стороне Next.js-прокси и в настройках "
            "Streamlit-клиента. Для локальной разработки без ключа запустите "
            "с флагом --allow-anonymous.",
            file=sys.stderr,
        )
        sys.exit(1)


def _harden_process() -> None:
    """Запретить сброс core dump процесса.

    В дампе памяти оказываются и исходный текст документа, и таблица
    соответствий — то есть дамп является полноценной утечкой ПДн, происходящей
    в обход всех прикладных мер. Заявление «мы ничего не пишем на диск» без
    этого вызова недостоверно: при аварийном завершении ядро запишет всё.

    Ограничение: в CPython строки неизменяемы и копируются сборщиком, поэтому
    гарантированное затирание значений в памяти средствами языка недостижимо.
    Остаточный риск (чтение памяти процесса, попадание страниц в swap)
    устраняется не здесь, а мерами уровня ОС — отдельный пользователь,
    отключённый swap либо шифрование раздела подкачки, запрет ptrace — и
    подлежит фиксации в модели угроз как принятый.
    """
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception as exc:  # noqa: BLE001 — платформа без resource (Windows)
        print(f"[server] не удалось отключить core dump: {exc}", file=sys.stderr)


def _sweep_jobs() -> None:
    """Drop finished jobs older than the TTL. Called lazily on each submit —
    no background timer thread. Must be called with _JOBS_LOCK held."""
    now = time.time()
    stale = [
        jid for jid, job in _JOBS.items()
        if job["status"] in _TERMINAL_STATUSES and now - job["created"] > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        del _JOBS[jid]
        # Уничтожение результата и ключа — тоже «операция, совершаемая с
        # персональными данными, полученными в результате обезличивания»
        # (п. 1.7 приказа РКН № 140), поэтому фиксируется наравне с самим
        # обезличиванием. Без этой записи подтвердить факт уничтожения нечем.
        depersonalization_log.record_event(
            event="job_destroyed",
            request_id=jid,
            detail={"reason": "ttl", "ttl_seconds": _JOB_TTL_SECONDS},
        )


def _serialize_spans(spans, include_text: bool) -> list:
    """Serialise spans for an HTTP response.

    ``include_text`` controls the ``text`` field only. Offsets and labels are
    always returned: the UI needs them to highlight, and on their own they do
    not disclose the masked values.

    Why the default is off — see ``_SPAN_TEXT_DEFAULT``: ``text`` plus
    ``start``/``end`` reconstructs the source document character-for-character
    without ever touching ``mapping``, so it is a second, more complete
    deanonymisation key. Shipping it next to the masked result defeats the
    masking entirely.
    """
    if include_text:
        return [
            {"start": s.start, "end": s.end, "label": s.label, "text": s.text}
            for s in spans
        ]
    return [{"start": s.start, "end": s.end, "label": s.label} for s in spans]


def _response_flags(data: dict) -> tuple[bool, bool]:
    """Read the two disclosure flags from a request body.

    Returns ``(include_span_text, irreversible)``.

    ``irreversible=true`` suppresses the deanonymisation key entirely: no
    ``mapping``, no span texts. In that mode the result satisfies п. 9 ст. 3
    152-ФЗ literally — the additional information required to re-identify the
    subject is never produced, so it cannot leak, be requested, or be stored
    alongside the result. The masked document itself is unaffected.
    """
    irreversible = bool(data.get("irreversible", False))
    if irreversible:
        return False, True
    return bool(data.get("include_span_text", _SPAN_TEXT_DEFAULT)), False


def _model_lock():
    """Context manager to hold around a model call: ``_LOCK`` if a local model
    needs it, a no-op otherwise (see ``_LOCK``'s docstring above for why).
    """
    return _LOCK if _NEEDS_MODEL_LOCK else contextlib.nullcontext()


def _compose(
    stages: dict,
    ner_threshold=None,
    cancel_event: threading.Event | None = None,
) -> Anonymizer:
    """Build an Anonymizer from the selected stages.

    ``ner_threshold`` optionally overrides GLiNER's confidence threshold for this
    request (lower => higher recall / more catches). The model itself is cached,
    so a per-request detector with a different threshold is cheap.

    ``cancel_event``, if given, is wired onto the per-request ``LLMDetector``
    and ``RemoteGLiNERDetector`` instances built below (see their
    ``cancel_event`` attribute) so a job worker can stop a running pipeline
    between chunks once the client has gone away. ``None`` (the default) means
    "never cancel" — every synchronous caller (sync endpoints, the CLI tools)
    keeps today's behaviour untouched.

    Requests now run concurrently (see ``_model_lock``), so any detector with
    per-request mutable state must NOT be one of the shared ``_DETECTORS``
    instances — sharing it would let one request's ``find()`` clear another's
    in-flight state. ``LLMDetector.warnings`` and (with ``--ner remote``)
    ``RemoteGLiNERDetector.warnings`` are exactly that: a list cleared at the
    start of every ``find()`` call, used to report "this chunk was never
    analysed, PII inside it may be unmasked" up through engine.py. So both are
    always built fresh per request below, from the shared (immutable) config
    object — cheap, since neither holds model weights, just an HTTP client.
    The regex/corporate/glossary detectors are stateless and stay shared; the
    local GLiNER (--ner gliner) and Natasha (--ner natasha) detectors hold no
    such per-request state either (only model weights, expensive to
    duplicate), so they also stay shared.
    """
    subject_on = stages.get("subject")
    if subject_on is None:
        subject_on = _DEFAULTS.get("subject", False)

    # SUBJECT (предмет договора) — просто расширяем allowed_labels существующего
    # LLM-детектора; отдельного прохода не добавляем. Строим ОДИН экземпляр и
    # переиспользуем его и в детекции, и во втором проходе: иначе leak-скан
    # (second_pass) шёл базовым детектором и предмет договора не видел вовсе.
    subject_detector = None
    if subject_on and _DETECTORS.get("llm"):
        from dataclasses import replace

        from anonymizer.llm import LLMDetector

        base_cfg = _DETECTORS["llm"][0].config
        subject_detector = LLMDetector(
            replace(base_cfg, allowed_labels=base_cfg.allowed_labels | {"SUBJECT"})
        )
        subject_detector.cancel_event = cancel_event

    # Fresh per-request LLM detector for the non-subject case too (replacing
    # what used to be the shared _DETECTORS["llm"][0] instance — see the
    # warnings-isolation note in the docstring above). Built unconditionally
    # whenever an LLM detector is configured at all, independent of whether
    # this particular request has the "llm" stage on: second_pass below can
    # still want it even when "llm" itself is off.
    base_llm_detector = None
    if _DETECTORS.get("llm"):
        from anonymizer.llm import LLMDetector

        base_llm_detector = LLMDetector(_DETECTORS["llm"][0].config)
        base_llm_detector.cancel_event = cancel_event

    dets: list = []
    for name in ("regex", "corporate", "glossary", "ner", "llm"):
        on = stages.get(name)
        if on is None:
            on = _DEFAULTS.get(name, False)
        if not (on and _DETECTORS.get(name)):
            continue
        if name == "ner" and _NER_BACKEND == "remote":
            # RemoteGLiNERDetector.warnings is per-request mutable state too
            # (see docstring above) — always build a fresh one from the
            # shared config, same shape as the local-GLiNER threshold-override
            # branch below.
            from dataclasses import replace

            from anonymizer.gliner_remote import RemoteGLiNERDetector

            base_ner_cfg = _DETECTORS["ner"][0].config
            if ner_threshold is not None:
                base_ner_cfg = replace(base_ner_cfg, threshold=float(ner_threshold))
            remote_ner_detector = RemoteGLiNERDetector(base_ner_cfg)
            remote_ner_detector.cancel_event = cancel_event
            dets.append(remote_ner_detector)
        elif name == "ner" and ner_threshold is not None and _GLINER_CFG is not None:
            from dataclasses import replace

            from anonymizer.gliner_ner import GLiNERDetector

            dets.append(GLiNERDetector(replace(_GLINER_CFG, threshold=float(ner_threshold))))
        elif name == "llm" and subject_detector is not None:
            # Если llm выключена, до этой ветки не доходим — subject молча
            # игнорируется.
            dets.append(subject_detector)
        elif name == "llm":
            dets.append(base_llm_detector)
        else:
            dets.extend(_DETECTORS[name])

    review_on = stages.get("review")
    if review_on is None:
        review_on = _DEFAULTS.get("review", False)
    review_cfg = _REVIEW_CFG if (review_on and _REVIEW_CFG is not None) else None
    if review_cfg is not None and subject_on:
        # Иначе слои воюют: детекция маскирует номенклатуру, а ревью снимает её
        # обратно по правилу «название продукта — не ПДн» (см. review.py).
        from dataclasses import replace

        review_cfg = replace(review_cfg, subject=True)

    # Leak check: re-scan the interim-anonymized text with the LLM detector and
    # mask whatever it still finds (bare first names, standalone surnames...).
    # Model-driven recall; costs roughly one extra LLM sweep of the document.
    sp_on = stages.get("second_pass")
    if sp_on is None:
        sp_on = _DEFAULTS.get("second_pass", False)
    if not sp_on:
        second_pass = []
    elif subject_detector is not None:
        second_pass = [subject_detector]
    elif base_llm_detector is not None:
        second_pass = [base_llm_detector]
    else:
        second_pass = []

    return Anonymizer(dets, review_config=review_cfg, second_pass_detectors=second_pass)


class _BadRequest(Exception):
    """Raised by _run_anonymize_file for a 400-worthy input error."""


def _run_anonymize_text(data: dict, cancel_event: threading.Event | None = None) -> dict:
    """Run the text-anonymization pipeline for a parsed /anonymize body.

    Body: {text, regex?, corporate?, ner?, llm?, ner_threshold?}
    Returns the same shape the synchronous /anonymize handler replies with.
    Shared by the synchronous handler and the async job worker so the two can
    never drift apart (same contract as ``_run_anonymize_file``).

    ``cancel_event`` is only ever passed by the async job worker (see
    ``_submit_job``); the synchronous handler leaves it at the default
    ``None``, so ``anon.anonymize(text)`` can raise ``Cancelled`` only for
    jobs, never for a direct /anonymize call.
    """
    text = data.get("text", "")
    stages = {k: data[k] for k in _STAGE_NAMES if k in data}
    used = {k: stages.get(k, _DEFAULTS.get(k, False)) for k in _STAGE_NAMES}

    t0 = time.time()
    with usage_log.request_context(chars=len(text), stages=used) as usage_totals:
        # Снимаем request_id ВНУТРИ контекста: на выходе из request_context
        # contextvar сбрасывается, и снаружи вернулся бы None.
        req_id = usage_log.current_request_id()
        with _model_lock():  # only held for local (in-process) models; see _LOCK
            anon = _compose(stages, data.get("ner_threshold"), cancel_event=cancel_event)
            res = anon.anonymize(text)
    elapsed = time.time() - t0

    include_span_text, irreversible = _response_flags(data)
    # Учёт действий по обезличиванию — форма учёта по п. 1.7 требований,
    # утв. приказом Роскомнадзора от 19.06.2025 № 140. В журнал идут только
    # количества по категориям, без значений; см. depersonalization_log.
    depersonalization_log.record_operation(
        request_id=req_id,
        labels=res.summary,
        chars=len(text),
        seconds=elapsed,
        irreversible=irreversible,
    )
    depersonalization_log.record_event(
        event="key_withheld" if irreversible else "key_issued",
        request_id=req_id,
    )
    return {
        "anonymized_text": res.anonymized_text,
        "mapping": {} if irreversible else res.mapping,
        "irreversible": irreversible,
        "summary": res.summary,
        "spans": _serialize_spans(res.spans, include_span_text),
        "stages": used,
        "elapsed_seconds": round(elapsed, 2),
        "preexisting_placeholders": res.preexisting_placeholders,
        "warnings": list(res.warnings),
        "usage": usage_totals.as_response_dict(),
    }


def _run_anonymize_file(data: dict, cancel_event: threading.Event | None = None) -> dict:
    """Run the file-anonymization pipeline for a parsed /anonymize-file body.

    Body: {filename, file_base64, regex?, corporate?, ner?, llm?}
    Returns: {filename, is_docx, anonymized_text, mapping, summary, spans,
              stages, document_base64, document_name, document_mime}
    Raises _BadRequest for a malformed request (missing file_base64), or lets
    any other exception propagate. Shared by the synchronous /anonymize-file
    handler and the async job worker, so the two paths can never drift apart.

    ``cancel_event`` — see ``_run_anonymize_text``'s docstring; only the async
    job worker ever passes one.
    """
    import base64
    from pathlib import PurePosixPath

    from anonymizer.documents import anonymized_docx_bytes, read_text_from_bytes

    filename = (data.get("filename") or "document.txt").strip()
    b64 = data.get("file_base64") or ""
    if not b64:
        raise _BadRequest("file_base64 is required")
    raw = base64.b64decode(b64)
    stages = {k: data[k] for k in _STAGE_NAMES if k in data}
    used = {k: stages.get(k, _DEFAULTS.get(k, False)) for k in _STAGE_NAMES}

    is_docx = filename.lower().endswith(".docx")
    text = read_text_from_bytes(filename, raw)

    t0 = time.time()
    with usage_log.request_context(filename=filename, chars=len(text), stages=used) as usage_totals:
        # см. комментарий в _run_anonymize_text — request_id снимается внутри
        req_id = usage_log.current_request_id()
        with _model_lock():  # only held for local (in-process) models; see _LOCK
            anon = _compose(stages, data.get("ner_threshold"), cancel_event=cancel_event)
            res = anon.anonymize(text)
    elapsed = time.time() - t0

    stem = PurePosixPath(filename).stem or "document"
    if is_docx:
        doc_bytes = anonymized_docx_bytes(raw, res.mapping)
        doc_name = f"{stem}.anon.docx"
        doc_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        doc_bytes = res.anonymized_text.encode("utf-8")
        doc_name = f"{stem}.anon.txt"
        doc_mime = "text/plain"

    include_span_text, irreversible = _response_flags(data)
    # Учёт по п. 1.7 приказа РКН № 140. Имя файла в журнал не пишется —
    # сохраняется только его хеш (source_ref), см. depersonalization_log.
    depersonalization_log.record_operation(
        request_id=req_id,
        labels=res.summary,
        chars=len(text),
        seconds=elapsed,
        irreversible=irreversible,
        filename=filename,
    )
    depersonalization_log.record_event(
        event="key_withheld" if irreversible else "key_issued",
        request_id=req_id,
    )
    return {
        "filename": filename,
        "is_docx": is_docx,
        "anonymized_text": res.anonymized_text,
        "mapping": {} if irreversible else res.mapping,
        "irreversible": irreversible,
        "summary": res.summary,
        "spans": _serialize_spans(res.spans, include_span_text),
        "stages": used,
        "elapsed_seconds": round(elapsed, 2),
        "preexisting_placeholders": res.preexisting_placeholders,
        "warnings": list(res.warnings),
        "document_base64": base64.b64encode(doc_bytes).decode("ascii"),
        "document_name": doc_name,
        "document_mime": doc_mime,
        "usage": usage_totals.as_response_dict(),
    }


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        # CORS-заголовок по умолчанию НЕ отправляется.
        #
        # Обоснование (проверено по коду 16.08.2026): ни один компонент
        # архитектуры не обращается к этому серверу кросс-доменно из браузера.
        # web/app/page.tsx ходит только на собственный origin (`/api/...`);
        # Next.js-роуты — серверный прокси (`runtime = "nodejs"`), то есть
        # запрос к бэкенду идёт с сервера, а не из браузера; Streamlit-клиент
        # (app.py) — обычный HTTP-клиент, к которому CORS неприменим.
        # Ранее здесь отдавалось "*", что в связке с отсутствием проверки
        # входящего Authorization давало возможность отправить документ на
        # бэкенд с любого стороннего сайта при наличии сетевой доступности.
        # См. ЮРИДИЧЕСКИЙ_АНАЛИЗ.md, п. Б.13 (R9).
        #
        # Если появится клиент, которому CORS действительно нужен, перечислите
        # источники через ANONYMIZER_CORS_ORIGINS (запятая-разделитель).
        # Значение "*" намеренно не поддерживается.
        origin = self.headers.get("Origin")
        if origin and origin in _CORS_ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # Клиент (фронтенд с лимитом ~300 с) может отвалиться, пока мы ещё пишем ответ —
        # тогда send_response/end_headers/write кидают BrokenPipeError, а
        # обработчик исключения в _handle_* пытается отправить 500 и падает
        # ВТОРОЙ раз с того же места, удваивая трейсбек в логах. Ловим здесь и
        # тихо выходим — отправлять уже некому.
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            print(
                f"[server] клиент отключился, не дождавшись ответа ({self.path}) — "
                "ответ не доставлен",
                file=sys.stderr,
            )

    def do_OPTIONS(self):  # CORS preflight — НИКОГДА не требует Authorization,
        # иначе браузер не смог бы даже дойти до реального запроса.
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authenticate_or_401(self) -> str | None:
        """Проверить ``Authorization`` и, если проверка не пройдена, сразу
        отправить 401 и вернуть ``None`` — вызывающий код обязан прекратить
        обработку маршрута в этом случае (см. использования ниже).

        Тело ответа минимально (``{"error": "unauthorized"}``) и не намекает,
        что именно не так (нет заголовка / неверный формат / неверный
        секрет) и не отражает присланное значение — иначе ответ сам стал бы
        оракулом для подбора секрета.
        """
        principal = _authenticate(self.headers)
        if principal is None:
            self._send(401, {"error": "unauthorized"})
        return principal

    def do_GET(self):
        path = self.path.rstrip("/")
        if path.endswith("health") or self.path in ("/", ""):
            # /health обязан отвечать БЕЗ авторизации (мониторинг), но полный
            # _INFO (конфигурация бэкенда, модель, стадии) — только
            # аутентифицированному вызову; см. докстринг задачи в верхнем
            # комментарии файла.
            principal = _authenticate(self.headers)
            payload = {"status": "ok", **_INFO} if principal is not None else {"status": "ok"}
            self._send(200, payload)
            return
        principal = self._authenticate_or_401()
        if principal is None:
            return
        with depersonalization_log.actor_context(principal):
            if path.endswith("usage"):
                # Только чтение JSONL-лога — не трогает _LOCK/_JOBS_LOCK,
                # поэтому никогда не стоит в очереди за обрабатывающимся
                # документом.
                self._send(200, usage_log.usage_summary())
                return
            if "/jobs/" in path:
                job_id = path.rsplit("/jobs/", 1)[1]
                self._handle_job_status(job_id)
                return
            self._send(404, {"error": "not found"})

    def do_DELETE(self):
        # Only route here is /jobs/<job_id> — cooperative job cancellation.
        # There is no "jobs/anonymize-file"-style ambiguity to worry about
        # (unlike do_POST) since DELETE has no other endpoints at all.
        principal = self._authenticate_or_401()
        if principal is None:
            return
        with depersonalization_log.actor_context(principal):
            path = self.path.rstrip("/")
            if "/jobs/" in path:
                job_id = path.rsplit("/jobs/", 1)[1]
                self._handle_job_cancel(job_id)
                return
            self._send(404, {"error": "not found"})

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        principal = self._authenticate_or_401()
        if principal is None:
            return
        with depersonalization_log.actor_context(principal):
            path = self.path.rstrip("/")
            # NB: check the jobs routes first — "jobs/anonymize-file" also ends
            # with "anonymize-file", so it would otherwise be swallowed below.
            if path.endswith("jobs/anonymize-file"):
                self._submit_job(_run_anonymize_file)
                return
            if path.endswith("jobs/anonymize"):
                self._submit_job(_run_anonymize_text)
                return
            # NB: check "deanonymize-file" first — it also ends with "anonymize-file".
            if path.endswith("deanonymize-file"):
                self._handle_deanon_file()
                return
            if path.endswith("anonymize-file"):
                self._handle_file()
                return
            if path.endswith("anonymize"):
                self._handle_text()
                return
            self._send(404, {"error": "not found"})

    def _handle_text(self):
        try:
            self._send(200, _run_anonymize_text(self._read_json()))
        except _BadRequest as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def _submit_job(self, runner):
        """Parse the body, start ``runner(data)`` on a worker thread, reply 202.

        Shared by the text and file job routes. The HTTP request itself lasts
        milliseconds — which is the whole point: any gateway between us and the
        client (a serverless frontend host, a VS Code dev tunnel) enforces a fixed per-request
        timeout that a multi-minute pipeline can never satisfy, no matter how
        the budget is configured on either end. Polling GET /jobs/<id> keeps
        every request short, so the gateway limit stops mattering.

        The worker runs in its own thread, started via
        ``depersonalization_log.run_in_context`` rather than a bare
        ``threading.Thread`` — that carries the ``actor`` contextvar
        (set by ``do_POST`` just above) into the worker, so
        ``_run_anonymize_text``/``_run_anonymize_file`` still see the
        authenticated principal when they call
        ``depersonalization_log.record_operation``/``record_event`` from
        inside this new thread (see that function's docstring for the
        propagation trap a bare ``Thread`` would fall into).
        """
        try:
            data = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": f"invalid json: {exc}"})
            return

        job_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        with _JOBS_LOCK:
            _sweep_jobs()
            _JOBS[job_id] = {
                "status": "pending", "result": None, "error": None, "created": time.time(),
                "cancel": cancel_event,
            }

        def _worker():
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                if job is not None:
                    job["status"] = "running"
            try:
                result = runner(data, cancel_event)
            except Cancelled:
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                    if job is not None:
                        job["status"] = "cancelled"
                        job["error"] = None
                return
            except Exception as exc:  # noqa: BLE001
                with _JOBS_LOCK:
                    job = _JOBS.get(job_id)
                    if job is not None and job["status"] != "cancelled":
                        job["status"] = "error"
                        job["error"] = str(exc)
                return
            with _JOBS_LOCK:
                job = _JOBS.get(job_id)
                # Don't resurrect a job DELETE already marked "cancelled" —
                # e.g. the cancel event was set right after the last
                # cancellation check, so the pipeline ran to completion anyway.
                if job is not None and job["status"] != "cancelled":
                    job["status"] = "done"
                    job["result"] = result

        depersonalization_log.run_in_context(_worker).start()
        self._send(202, {"job_id": job_id})

    def _handle_file(self):
        """Accept a base64-encoded .docx/.txt, return the anonymized document.

        Body: {filename, file_base64, regex?, corporate?, ner?, llm?}
        Reply: {filename, is_docx, anonymized_text, mapping, summary, spans,
                stages, document_base64, document_name, document_mime}
        The whole document is anonymized in one pass, so each entity keeps the
        same placeholder everywhere; for .docx we rebuild a copy preserving the
        paragraph/table structure.
        """
        try:
            data = self._read_json()
            self._send(200, _run_anonymize_file(data))
        except _BadRequest as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def _handle_job_status(self, job_id: str):
        """GET /jobs/<job_id> -> {status, result, error}. Never touches _LOCK,
        so polling is served concurrently with a running job (the server is
        ThreadingHTTPServer)."""
        # Snapshot under the lock, then release it before writing the response:
        # a finished result carries the document as base64 (megabytes), and a
        # slow client would otherwise hold _JOBS_LOCK for the whole socket
        # write, stalling job submits and worker status updates.
        with _JOBS_LOCK:
            # Подметаем на КАЖДОМ обращении, а не только при submit. Раньше
            # очистка вызывалась лениво из submit, поэтому при отсутствии новых
            # заданий завершённая задача с ключом деанонимизации могла лежать в
            # памяти неограниченно долго — то есть фактический срок хранения не
            # совпадал с декларируемым. Приказ РКН № 140, п. 1.6.
            _sweep_jobs()
            job = _JOBS.get(job_id)
            payload = None if job is None else {
                "status": job["status"],
                "result": job["result"],
                "error": job["error"],
            }
            # Одноразовая выдача: после отдачи терминального результата задание
            # удаляется, ключ в памяти не остаётся. По умолчанию ВЫКЛЮЧЕНО —
            # клиент с ретраями (web/app/api/_shared.ts повторяет запрос при
            # обрыве соединения) должен иметь возможность забрать результат
            # повторно. Включать вместе с переходом на раздельную выдачу
            # документа и ключа.
            if (
                _JOB_ONESHOT
                and job is not None
                and job["status"] in _TERMINAL_STATUSES
            ):
                del _JOBS[job_id]
                depersonalization_log.record_event(
                    event="job_destroyed",
                    request_id=job_id,
                    detail={"reason": "oneshot"},
                )
        if payload is not None and payload["status"] in _TERMINAL_STATUSES:
            depersonalization_log.record_event(
                event="result_issued", request_id=job_id
            )
        if payload is None:
            self._send(404, {"error": "unknown job"})
            return
        self._send(200, payload)

    def _handle_job_cancel(self, job_id: str):
        """DELETE /jobs/<job_id> -> {"status": ...}. Cooperative cancellation:
        sets the job's cancel event so the worker's detectors stop between
        chunks (see Cancelled in anonymizer/llm.py) rather than killing the
        thread outright.

        Unknown id -> 404, matching GET's behaviour. A job already in a
        terminal state (done / error / cancelled) is left untouched and its
        existing status is returned — DELETE never resurrects or overwrites
        a finished job's result.
        """
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                self._send(404, {"error": "unknown job"})
                return
            if job["status"] not in _TERMINAL_STATUSES:
                job["cancel"].set()
                job["status"] = "cancelled"
            status = job["status"]
        self._send(200, {"status": status})

    def _handle_deanon_file(self):
        """Restore originals in an anonymized .docx/.txt using a mapping (no AI).

        Body: {filename, file_base64, mapping}
        Reply: {filename, is_docx, restored_text, leftover, document_base64,
                document_name, document_mime}
        Deanonymization is a deterministic placeholder->value substitution; for
        .docx we restore into a copy preserving the paragraph/table structure.
        """
        import base64
        from pathlib import PurePosixPath

        from anonymizer.deanonymize import deanonymize, find_unknown_placeholders
        from anonymizer.documents import deanonymized_docx_bytes, read_text_from_bytes

        try:
            data = self._read_json()
            filename = (data.get("filename") or "document.txt").strip()
            b64 = data.get("file_base64") or ""
            mapping = data.get("mapping") or {}
            if not b64:
                self._send(400, {"error": "file_base64 is required"})
                return
            if not isinstance(mapping, dict) or not mapping:
                self._send(400, {"error": "mapping is required"})
                return
            raw = base64.b64decode(b64)

            is_docx = filename.lower().endswith(".docx")
            anon_text = read_text_from_bytes(filename, raw)
            restored_text = deanonymize(anon_text, mapping)
            leftover = sorted(set(find_unknown_placeholders(anon_text, mapping)))

            stem = PurePosixPath(filename).stem or "document"
            if stem.endswith(".anon"):
                stem = stem[: -len(".anon")]
            if is_docx:
                doc_bytes = deanonymized_docx_bytes(raw, mapping)
                doc_name = f"{stem}.restored.docx"
                doc_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            else:
                doc_bytes = restored_text.encode("utf-8")
                doc_name = f"{stem}.restored.txt"
                doc_mime = "text/plain"

            self._send(200, {
                "filename": filename,
                "is_docx": is_docx,
                "restored_text": restored_text,
                "leftover": leftover,
                "document_base64": base64.b64encode(doc_bytes).decode("ascii"),
                "document_name": doc_name,
                "document_mime": doc_mime,
            })
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def log_message(self, *a):  # silence default logging
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--ner", default="gliner", choices=["gliner", "natasha", "remote", "none"]
    )
    ap.add_argument("--device", default="cuda", help="GLiNER device: cpu | cuda | dml")
    # Весь конвейер включён ПО УМОЛЧАНИЮ — просто `python server.py` поднимает
    # полный корпоративный режим (regex + GLiNER + LLM + review + second-pass +
    # recall). Флаги нужны ТОЛЬКО чтобы что-то ОТКЛЮЧИТЬ: --no-llm, --no-review,
    # --no-second-pass, --no-recall, --no-corporate, --ner none.
    _Bool = argparse.BooleanOptionalAction
    ap.add_argument(
        "--corporate", action=_Bool, default=True,
        help="Корпоративные детекторы (суммы, договоры, реквизиты, VIN/госномер). "
             "По умолчанию ВКЛ. Отключить: --no-corporate.",
    )
    ap.add_argument(
        "--llm", action=_Bool, default=True,
        help="LLM-слой добора сложных ПДн. По умолчанию ВКЛ. Отключить: --no-llm.",
    )
    # Дефолты берутся из окружения, чтобы модель переключалась ОДНОЙ переменной
    # и одинаково для детекции и для review — иначе легко забыть --review-model
    # и держать в VRAM две модели сразу, что на 10-гигабайтной карте кончается
    # частичной выгрузкой слоёв на CPU (`offloaded 34/49 layers`) и падением
    # скорости втрое.
    ap.add_argument(
        "--llm-base-url",
        default=os.getenv("ANONYMIZER_LLM_BASE_URL", "http://127.0.0.1:11433/v1"),
        help="OpenAI-совместимый эндпоинт. Дефолт — локальный Ollama на хабе "
             "(:11433, см. JUPYTERHUB_GPU.md); обычная установка Ollama — :11434, "
             "LM Studio — :1234. Переопределяется ANONYMIZER_LLM_BASE_URL.",
    )
    ap.add_argument(
        "--llm-model",
        default=os.getenv("ANONYMIZER_LLM_MODEL", "qwen3.5:9b"),
        help="Идентификатор модели РОВНО как его отдаёт сервер (`ollama list` "
             "или GET /v1/models). Переопределяется ANONYMIZER_LLM_MODEL.",
    )
    ap.add_argument(
        "--llm-api-key",
        default=os.getenv("ANONYMIZER_LLM_API_KEY", "not-needed"),
        help="Bearer-токен для LLM-эндпоинта. Локальные Ollama и LM Studio его "
             "игнорируют, но внешний шлюз без него ответит 401. "
             "Переопределяется ANONYMIZER_LLM_API_KEY.",
    )
    ap.add_argument(
        "--gliner-url",
        default=os.getenv("ANONYMIZER_GLINER_URL", "https://oui.interfonica.cloud/gliner"),
        help="База удалённого GLiNER (--ner remote), БЕЗ /extract на конце. "
             "Переопределяется ANONYMIZER_GLINER_URL.",
    )
    ap.add_argument(
        "--gliner-api-key",
        default=os.getenv("ANONYMIZER_GLINER_API_KEY", ""),
        help="Bearer-токен для GLiNER-эндпоинта (--ner remote). Пусто по "
             "умолчанию — тогда используется --llm-api-key, один ключ "
             "обслуживает оба эндпоинта. Переопределяется "
             "ANONYMIZER_GLINER_API_KEY.",
    )
    ap.add_argument(
        "--gliner-concurrency", type=int, default=16,
        help="Число параллельных запросов к удалённому GLiNER (--ner remote). "
             "Эндпоинт упирается в задержку сети, а не в вычисления, поэтому "
             "параллельность почти линейно ускоряет обработку. По умолчанию 16.",
    )
    ap.add_argument(
        "--llm-max-chars",
        type=int,
        default=int(os.getenv("ANONYMIZER_LLM_MAX_CHARS", "3000")),
        help="Размер куска текста (в символах), отправляемого модели за один "
             "запрос детекции (см. LLMConfig.max_chars). Больше — меньше "
             "запросов и меньше повторов системного промпта (дешевле и "
             "быстрее), но ниже recall: модель хуже ловит редкие формы на "
             "большом куске текста. Дефолт 3000 — измеренное компромиссное "
             "значение, не меняйте его без сравнения. Переопределяется "
             "ANONYMIZER_LLM_MAX_CHARS.",
    )
    ap.add_argument(
        "--llm-concurrency", type=int, default=24,
        help="Число параллельных запросов к LLM-детектору. По умолчанию 24: "
             "запросы идут через пул постоянных соединений (см. http_pool.py), "
             "который снимает накладные расходы TLS-хендшейка на каждый "
             "вызов, поэтому параллельность больше не упирается в цену "
             "установки соединения. Все вызовы процесса (по всем документам и "
             "детекторам сразу) дополнительно ограничены общим пределом "
             "ANONYMIZER_MAX_INFLIGHT (см. http_pool.py), так что поднимать "
             "этот параметр безопасно.",
    )
    ap.add_argument(
        "--review", action=_Bool, default=True,
        help="4-й слой: LLM перепроверяет итоговый список и снимает очевидные "
             "ложные срабатывания. По умолчанию ВКЛ. Отключить: --no-review.",
    )
    ap.add_argument(
        "--second-pass", action=_Bool, default=True,
        help="Повторная проверка на утечки: после маскирования ещё раз сканирует "
             "текст LLM-детектором. По умолчанию ВКЛ. Отключить: --no-second-pass.",
    )
    ap.add_argument(
        "--recall", action=_Bool, default=True,
        help="Recall-проход: добор пропущенных ПДн по уже замаскированному тексту "
             "(требует review). По умолчанию ВКЛ. Отключить: --no-recall.",
    )
    ap.add_argument(
        "--think", action=_Bool, default=False,
        help="Размышления (reasoning) LLM в детекции и проверке. По умолчанию "
             "ВЫКЛ (быстрее). Включить: --think.",
    )
    ap.add_argument(
        "--subject", action=_Bool, default=True,
        help="Метка SUBJECT — предмет договора (наименования товаров/работ/услуг), "
             "добавляется в тот же LLM-вызов детекции без доп. времени обработки. "
             "Дополнительно переводит слой --review в режим предмета договора, "
             "чтобы он не раскрывал номенклатуру как «название продукта». "
             "По умолчанию ВКЛ, работает только вместе с --llm. Отключить: "
             "--no-subject.",
    )
    ap.add_argument("--review-base-url", default=None, help="Defaults to --llm-base-url")
    ap.add_argument("--review-model", default=None, help="Defaults to --llm-model")
    ap.add_argument(
        "--custom-terms", default=None,
        help="Path to a glossary file of always-mask terms (see glossary.py). "
             "Defaults to anonymizer/custom_terms.txt if it exists.",
    )
    ap.add_argument(
        "--allow-anonymous", action="store_true", default=False,
        help="Отключить проверку Authorization целиком. ТОЛЬКО для локальной "
             "разработки — печатает предупреждение в stderr при каждом "
             "запуске и никогда не используется в проде. Без этого флага и "
             "без ANONYMIZER_API_KEYS/ANONYMIZER_API_KEY сервер откажется "
             "стартовать, см. _configure_auth.",
    )
    args = ap.parse_args()

    # Проверка ключей доступа — ДО загрузки моделей: ошибка конфигурации
    # должна остановить процесс сразу, а не после многосекундного прогрева
    # (см. докстринг _configure_auth).
    _configure_auth(args)

    global _INFO, _GLINER_CFG, _REVIEW_CFG, _NER_BACKEND, _NEEDS_MODEL_LOCK
    print("Загружаю модели…", flush=True)

    _NER_BACKEND = args.ner
    # Local models (gliner/natasha) are not thread-safe; remote/none carry no
    # in-process model at all, so no serialization is needed — see _LOCK.
    _NEEDS_MODEL_LOCK = args.ner in ("gliner", "natasha")

    from anonymizer.detectors import CORPORATE_DETECTORS, DEFAULT_DETECTORS
    from anonymizer.glossary import DEFAULT_GLOSSARY_PATH, GlossaryDetector, load_glossary

    _DETECTORS["regex"] = list(DEFAULT_DETECTORS)
    _DETECTORS["corporate"] = list(CORPORATE_DETECTORS)

    glossary_entries = load_glossary(args.custom_terms or DEFAULT_GLOSSARY_PATH)
    if glossary_entries:
        _DETECTORS["glossary"] = [GlossaryDetector(glossary_entries)]

    if args.ner != "none":
        if args.ner == "gliner":
            from anonymizer.gliner_ner import GLiNERConfig, GLiNERDetector

            _GLINER_CFG = GLiNERConfig(device=args.device)
            _DETECTORS["ner"] = [GLiNERDetector(_GLINER_CFG)]
        elif args.ner == "remote":
            # Imported lazily, same as the "gliner" branch above: the entire
            # point of the remote path is to run without torch installed.
            from anonymizer.gliner_remote import RemoteGLiNERConfig, RemoteGLiNERDetector

            _DETECTORS["ner"] = [
                RemoteGLiNERDetector(
                    RemoteGLiNERConfig(
                        base_url=args.gliner_url,
                        api_key=args.gliner_api_key or args.llm_api_key,
                        concurrency=args.gliner_concurrency,
                    )
                )
            ]
        else:
            from anonymizer.ner import NatashaDetector

            _DETECTORS["ner"] = [NatashaDetector()]

    if args.llm:
        from anonymizer.llm import NO_THINKING_EXTRA_BODY, LLMConfig, LLMDetector

        extra = {} if args.think else NO_THINKING_EXTRA_BODY
        lconf = LLMConfig(
            base_url=args.llm_base_url,
            model=args.llm_model,
            api_key=args.llm_api_key,
            extra_body=extra,
            concurrency=args.llm_concurrency,
            max_chars=args.llm_max_chars,
        )
        if args.corporate:  # the LLM (not regex) handles organizations and money sums
            from dataclasses import replace

            lconf = replace(lconf, allowed_labels=lconf.allowed_labels | {"ORG", "AMOUNT"})
        _DETECTORS["llm"] = [LLMDetector(lconf)]

    if args.review:
        from anonymizer.llm import NO_THINKING_EXTRA_BODY
        from anonymizer.review import ReviewConfig

        review_extra = {} if args.think else NO_THINKING_EXTRA_BODY
        review_kwargs = dict(
            base_url=args.review_base_url or args.llm_base_url,
            model=args.review_model or args.llm_model,
            api_key=args.llm_api_key,
            extra_body=review_extra,
            recall=args.recall,  # recall ВКЛ по умолчанию; отключить: --no-recall
        )
        _REVIEW_CFG = ReviewConfig(**review_kwargs)

    # Start-up defaults: a stage is ON if it was loaded / requested.
    _DEFAULTS.update(
        regex=True,
        corporate=args.corporate,
        glossary=bool(_DETECTORS.get("glossary")),
        ner=args.ner != "none",
        llm=args.llm,
        review=args.review,
        second_pass=args.second_pass and args.llm,
        subject=args.subject and args.llm,
    )

    # Warm up the pipeline. A transient LLM outage must NOT prevent the server
    # from starting — regex/GLiNER still work, and the LLM can come back later.
    try:
        _compose(_DEFAULTS).anonymize("Иван Иванов из Москвы, ИНН 7707083893.")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] прогрев не удался (сервер всё равно поднят): {exc}", flush=True)
    # Фактическое устройство GLiNER, а не запрошенное: при откате на CPU
    # (несовместимый cuDNN в LD_LIBRARY_PATH, занятая карта) /health раньше
    # продолжал показывать "cuda", и трёхкратное замедление выглядело
    # необъяснимым. Отдаём оба поля, чтобы расхождение было видно сразу.
    effective_device = args.device
    if args.ner == "gliner":
        from anonymizer.gliner_ner import effective_device as _eff

        effective_device = _eff() or args.device

    _INFO = {
        "ner": args.ner,
        "device": effective_device,
        "device_requested": args.device,
        "corporate": args.corporate, "llm": args.llm,
        "llm_model": args.llm_model if args.llm else None,
        "llm_max_chars": args.llm_max_chars if args.llm else None,
        "glossary_terms": len(glossary_entries),
        "review": args.review,
        "review_model": _REVIEW_CFG.model if _REVIEW_CFG else None,
        "llm_recall": bool(_REVIEW_CFG and _REVIEW_CFG.recall),
        "second_pass": _DEFAULTS.get("second_pass", False),
        "subject": _DEFAULTS.get("subject", False),
        "stages": dict(_DEFAULTS), "toggleable": True,
        "ner_threshold": _GLINER_CFG.threshold if _GLINER_CFG else None,
    }
    _harden_process()
    print(f"Сервер готов: http://{args.host}:{args.port}  {_INFO}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
