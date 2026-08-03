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
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

    t0 = time.time()
    with _model_lock():  # only held for local (in-process) models; see _LOCK
        anon = _compose(stages, data.get("ner_threshold"), cancel_event=cancel_event)
        res = anon.anonymize(text)
    elapsed = time.time() - t0

    used = {k: stages.get(k, _DEFAULTS.get(k, False)) for k in _STAGE_NAMES}
    return {
        "anonymized_text": res.anonymized_text,
        "mapping": res.mapping,
        "summary": res.summary,
        "spans": [
            {"start": s.start, "end": s.end, "label": s.label, "text": s.text}
            for s in res.spans
        ],
        "stages": used,
        "elapsed_seconds": round(elapsed, 2),
        "preexisting_placeholders": res.preexisting_placeholders,
        "warnings": list(res.warnings),
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

    is_docx = filename.lower().endswith(".docx")
    text = read_text_from_bytes(filename, raw)

    t0 = time.time()
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

    used = {k: stages.get(k, _DEFAULTS.get(k, False)) for k in _STAGE_NAMES}
    return {
        "filename": filename,
        "is_docx": is_docx,
        "anonymized_text": res.anonymized_text,
        "mapping": res.mapping,
        "summary": res.summary,
        "spans": [
            {"start": s.start, "end": s.end, "label": s.label, "text": s.text}
            for s in res.spans
        ],
        "stages": used,
        "elapsed_seconds": round(elapsed, 2),
        "preexisting_placeholders": res.preexisting_placeholders,
        "warnings": list(res.warnings),
        "document_base64": base64.b64encode(doc_bytes).decode("ascii"),
        "document_name": doc_name,
        "document_mime": doc_mime,
    }


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        # Allow the Next.js UI (Vercel / localhost) to call us from the browser.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # Клиент (Vercel, лимит 300с) может отвалиться, пока мы ещё пишем ответ —
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

    def do_OPTIONS(self):  # CORS preflight
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.rstrip("/")
        if path.endswith("health") or self.path in ("/", ""):
            self._send(200, {"status": "ok", **_INFO})
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
        client (Vercel, a VS Code dev tunnel) enforces a fixed per-request
        timeout that a multi-minute pipeline can never satisfy, no matter how
        the budget is configured on either end. Polling GET /jobs/<id> keeps
        every request short, so the gateway limit stops mattering.
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

        threading.Thread(target=_worker, daemon=True).start()
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
            job = _JOBS.get(job_id)
            payload = None if job is None else {
                "status": job["status"],
                "result": job["result"],
                "error": job["error"],
            }
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
        "--llm-concurrency", type=int, default=1,
        help="Число параллельных запросов к LLM-детектору. По умолчанию 1 "
             "(последовательно) — локальная модель обычно занимает одну "
             "видеокарту, где параллельные запросы просто встают в очередь. "
             "Повышать имеет смысл только для удалённого эндпоинта, который "
             "сам масштабируется под параллельной нагрузкой.",
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
    args = ap.parse_args()

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
        "glossary_terms": len(glossary_entries),
        "review": args.review,
        "review_model": _REVIEW_CFG.model if _REVIEW_CFG else None,
        "llm_recall": bool(_REVIEW_CFG and _REVIEW_CFG.recall),
        "second_pass": _DEFAULTS.get("second_pass", False),
        "subject": _DEFAULTS.get("subject", False),
        "stages": dict(_DEFAULTS), "toggleable": True,
        "ner_threshold": _GLINER_CFG.threshold if _GLINER_CFG else None,
    }
    print(f"Сервер готов: http://{args.host}:{args.port}  {_INFO}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
