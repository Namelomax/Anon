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
    POST /anonymize  {text, regex?, corporate?, ner?, llm?}
         -> {anonymized_text, mapping, summary, spans:[{start,end,label,text}], stages}

Each pipeline stage (regex / corporate / ner / llm) can be toggled per request
via optional booleans in the POST body; omitted flags fall back to the server's
start-up defaults. This lets the UI try e.g. "GLiNER only, no regex" without a
redeploy.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer.engine import Anonymizer  # noqa: E402

_DETECTORS: dict = {}    # stage name -> list of detector objects (built once)
_DEFAULTS: dict = {}     # stage name -> bool (start-up default on/off)
_INFO: dict = {}
_LOCK = threading.Lock()  # serialize model calls: torch/GLiNER is not thread-safe


def _compose(stages: dict) -> Anonymizer:
    """Build an Anonymizer from the selected stages (detectors are reused)."""
    dets: list = []
    for name in ("regex", "corporate", "ner", "llm"):
        on = stages.get(name)
        if on is None:
            on = _DEFAULTS.get(name, False)
        if on and _DETECTORS.get(name):
            dets.extend(_DETECTORS[name])
    return Anonymizer(dets)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("health") or self.path in ("/", ""):
            self._send(200, {"status": "ok", **_INFO})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("anonymize"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            text = data.get("text", "")
            stages = {k: data[k] for k in ("regex", "corporate", "ner", "llm") if k in data}
            with _LOCK:  # one model call at a time (torch is not thread-safe)
                anon = _compose(stages)
                res = anon.anonymize(text)
            used = {k: stages.get(k, _DEFAULTS.get(k, False)) for k in ("regex", "corporate", "ner", "llm")}
            self._send(200, {
                "anonymized_text": res.anonymized_text,
                "mapping": res.mapping,
                "summary": res.summary,
                "spans": [
                    {"start": s.start, "end": s.end, "label": s.label, "text": s.text}
                    for s in res.spans
                ],
                "stages": used,
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
    args = ap.parse_args()

    global _INFO
    print("Загружаю модели…", flush=True)

    from anonymizer.detectors import CORPORATE_DETECTORS, DEFAULT_DETECTORS

    _DETECTORS["regex"] = list(DEFAULT_DETECTORS)
    _DETECTORS["corporate"] = list(CORPORATE_DETECTORS)

    if args.ner != "none":
        if args.ner == "gliner":
            from anonymizer.gliner_ner import GLiNERConfig, GLiNERDetector

            _DETECTORS["ner"] = [GLiNERDetector(GLiNERConfig(device=args.device))]
        else:
            from anonymizer.ner import NatashaDetector

            _DETECTORS["ner"] = [NatashaDetector()]

    if args.llm:
        from anonymizer.llm import LLMConfig, LLMDetector

        extra = {"reasoning_effort": "none"} if args.llm_no_think else {}
        lconf = LLMConfig(base_url=args.llm_base_url, model=args.llm_model, extra_body=extra)
        if args.corporate:  # let the LLM also return organization names
            from dataclasses import replace

            lconf = replace(lconf, allowed_labels=lconf.allowed_labels | {"ORG"})
        _DETECTORS["llm"] = [LLMDetector(lconf)]

    # Start-up defaults: a stage is ON if it was loaded / requested.
    _DEFAULTS.update(
        regex=True,
        corporate=args.corporate,
        ner=args.ner != "none",
        llm=args.llm,
    )

    _compose(_DEFAULTS).anonymize("Иван Иванов из Москвы, ИНН 7707083893.")  # warm up
    _INFO = {
        "ner": args.ner, "device": args.device,
        "corporate": args.corporate, "llm": args.llm,
        "llm_model": args.llm_model if args.llm else None,
        "stages": dict(_DEFAULTS), "toggleable": True,
    }
    print(f"Сервер готов: http://{args.host}:{args.port}  {_INFO}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
