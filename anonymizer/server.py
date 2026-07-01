"""HTTP backend for the anonymizer — run it on the GPU host (JupyterHub).

The whole pipeline (regex + GLiNER on CUDA + LLM via local Ollama) runs here;
local clients (the Streamlit UI, the benchmark) just POST text and get back the
anonymized text, mapping and spans. This is the "thin client + remote GPU
backend" setup for demos.

Run on the hub:
    python anonymizer/server.py --port 8000 --device cuda --corporate \
        --llm --llm-base-url http://127.0.0.1:11433/v1 --llm-model qwen3.5:9b --llm-no-think

Expose it through JupyterHub's proxy (like Ollama): the URL becomes
    https://<hub>/user/<id>/proxy/8000/
and clients authenticate with the JupyterHub Bearer token.

API:
    GET  /health           -> {"status": "ok", ...}
    POST /anonymize  {text, regex?, corporate?, ner?, llm?, review?}
         -> {anonymized_text, mapping, summary, spans:[{start,end,label,text}], stages}

Each pipeline stage (regex / corporate / ner / llm / review) can be toggled
per request via optional booleans in the POST body; omitted flags fall back
to the server's start-up defaults. This lets the UI try e.g. "GLiNER only, no
regex" without a redeploy. ``review`` is the 4th, last layer: it re-checks the
spans produced by the other layers against their context and un-masks obvious
false positives (see ``review.py``); it only has any effect if the server was
started with ``--review``.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer.engine import Anonymizer  # noqa: E402

_DETECTORS: dict = {}    # stage name -> list of detector objects (built once)
_DEFAULTS: dict = {}     # stage name -> bool (start-up default on/off)
_GLINER_CFG = None       # base GLiNERConfig, for per-request threshold overrides
_REVIEW_CFG = None       # ReviewConfig for the LLM review layer, or None if disabled
_INFO: dict = {}
_LOCK = threading.Lock()  # serialize model calls: torch/GLiNER is not thread-safe

_STAGE_NAMES = ("regex", "corporate", "ner", "llm", "review")


def _compose(stages: dict, ner_threshold=None) -> Anonymizer:
    """Build an Anonymizer from the selected stages (detectors are reused).

    ``ner_threshold`` optionally overrides GLiNER's confidence threshold for this
    request (lower => higher recall / more catches). The model itself is cached,
    so a per-request detector with a different threshold is cheap.
    """
    dets: list = []
    for name in ("regex", "corporate", "ner", "llm"):
        on = stages.get(name)
        if on is None:
            on = _DEFAULTS.get(name, False)
        if not (on and _DETECTORS.get(name)):
            continue
        if name == "ner" and ner_threshold is not None and _GLINER_CFG is not None:
            from dataclasses import replace

            from anonymizer.gliner_ner import GLiNERDetector

            dets.append(GLiNERDetector(replace(_GLINER_CFG, threshold=float(ner_threshold))))
        else:
            dets.extend(_DETECTORS[name])

    review_on = stages.get("review")
    if review_on is None:
        review_on = _DEFAULTS.get("review", False)
    review_cfg = _REVIEW_CFG if (review_on and _REVIEW_CFG is not None) else None
    return Anonymizer(dets, review_config=review_cfg)


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        # Allow the Next.js UI (Vercel / localhost) to call us from the browser.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # CORS preflight
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/").endswith("health") or self.path in ("/", ""):
            self._send(200, {"status": "ok", **_INFO})
        else:
            self._send(404, {"error": "not found"})

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        path = self.path.rstrip("/")
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
            data = self._read_json()
            text = data.get("text", "")
            stages = {k: data[k] for k in _STAGE_NAMES if k in data}
            t0 = time.time()
            with _LOCK:  # one model call at a time (torch is not thread-safe)
                anon = _compose(stages, data.get("ner_threshold"))
                res = anon.anonymize(text)
            elapsed = time.time() - t0
            used = {k: stages.get(k, _DEFAULTS.get(k, False)) for k in _STAGE_NAMES}
            self._send(200, {
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
            })
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def _handle_file(self):
        """Accept a base64-encoded .docx/.txt, return the anonymized document.

        Body: {filename, file_base64, regex?, corporate?, ner?, llm?}
        Reply: {filename, is_docx, anonymized_text, mapping, summary, spans,
                stages, document_base64, document_name, document_mime}
        The whole document is anonymized in one pass, so each entity keeps the
        same placeholder everywhere; for .docx we rebuild a copy preserving the
        paragraph/table structure.
        """
        import base64
        from pathlib import PurePosixPath

        from anonymizer.documents import anonymized_docx_bytes, read_text_from_bytes

        try:
            data = self._read_json()
            filename = (data.get("filename") or "document.txt").strip()
            b64 = data.get("file_base64") or ""
            if not b64:
                self._send(400, {"error": "file_base64 is required"})
                return
            raw = base64.b64decode(b64)
            stages = {k: data[k] for k in _STAGE_NAMES if k in data}

            is_docx = filename.lower().endswith(".docx")
            text = read_text_from_bytes(filename, raw)

            t0 = time.time()
            with _LOCK:  # torch is not thread-safe
                anon = _compose(stages, data.get("ner_threshold"))
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
            self._send(200, {
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
                "document_base64": base64.b64encode(doc_bytes).decode("ascii"),
                "document_name": doc_name,
                "document_mime": doc_mime,
            })
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

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
    ap.add_argument("--ner", default="gliner", choices=["gliner", "natasha", "none"])
    ap.add_argument("--device", default="cuda", help="GLiNER device: cpu | cuda | dml")
    ap.add_argument("--corporate", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:11433/v1")
    ap.add_argument("--llm-model", default="qwen3.5:9b")
    ap.add_argument("--llm-no-think", action="store_true")
    ap.add_argument(
        "--review", action="store_true",
        help="4th layer: LLM double-checks the final mapping and reverts obvious "
             "false positives (e.g. common words mislabeled as PERSON/ORG).",
    )
    ap.add_argument("--review-base-url", default=None, help="Defaults to --llm-base-url")
    ap.add_argument("--review-model", default=None, help="Defaults to --llm-model")
    ap.add_argument("--review-no-think", action="store_true")
    args = ap.parse_args()

    global _INFO, _GLINER_CFG, _REVIEW_CFG
    print("Загружаю модели…", flush=True)

    from anonymizer.detectors import CORPORATE_DETECTORS, DEFAULT_DETECTORS

    _DETECTORS["regex"] = list(DEFAULT_DETECTORS)
    _DETECTORS["corporate"] = list(CORPORATE_DETECTORS)

    if args.ner != "none":
        if args.ner == "gliner":
            from anonymizer.gliner_ner import GLiNERConfig, GLiNERDetector

            _GLINER_CFG = GLiNERConfig(device=args.device)
            _DETECTORS["ner"] = [GLiNERDetector(_GLINER_CFG)]
        else:
            from anonymizer.ner import NatashaDetector

            _DETECTORS["ner"] = [NatashaDetector()]

    if args.llm:
        from anonymizer.llm import LLMConfig, LLMDetector

        extra = {"reasoning_effort": "none"} if args.llm_no_think else {}
        lconf = LLMConfig(base_url=args.llm_base_url, model=args.llm_model, extra_body=extra)
        if args.corporate:  # the LLM (not regex) handles organizations and money sums
            from dataclasses import replace

            lconf = replace(lconf, allowed_labels=lconf.allowed_labels | {"ORG", "AMOUNT"})
        _DETECTORS["llm"] = [LLMDetector(lconf)]

    if args.review:
        from anonymizer.review import ReviewConfig

        review_extra = {"reasoning_effort": "none"} if args.review_no_think else {}
        _REVIEW_CFG = ReviewConfig(
            base_url=args.review_base_url or args.llm_base_url,
            model=args.review_model or args.llm_model,
            extra_body=review_extra,
        )

    # Start-up defaults: a stage is ON if it was loaded / requested.
    _DEFAULTS.update(
        regex=True,
        corporate=args.corporate,
        ner=args.ner != "none",
        llm=args.llm,
        review=args.review,
    )

    # Warm up the pipeline. A transient LLM outage must NOT prevent the server
    # from starting — regex/GLiNER still work, and the LLM can come back later.
    try:
        _compose(_DEFAULTS).anonymize("Иван Иванов из Москвы, ИНН 7707083893.")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] прогрев не удался (сервер всё равно поднят): {exc}", flush=True)
    _INFO = {
        "ner": args.ner, "device": args.device,
        "corporate": args.corporate, "llm": args.llm,
        "llm_model": args.llm_model if args.llm else None,
        "review": args.review,
        "review_model": _REVIEW_CFG.model if _REVIEW_CFG else None,
        "stages": dict(_DEFAULTS), "toggleable": True,
        "ner_threshold": _GLINER_CFG.threshold if _GLINER_CFG else None,
    }
    print(f"Сервер готов: http://{args.host}:{args.port}  {_INFO}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
