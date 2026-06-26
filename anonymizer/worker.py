"""Subprocess worker: does the heavy anonymization in its own process.

PyTorch (GLiNER/Natasha) is unstable when driven from Streamlit's script-runner
thread on Windows (the process can die without a traceback). Running the model
in a short-lived subprocess that exits cleanly sidesteps that entirely — the UI
process never imports torch.

IPC is file-based to avoid any stdout/encoding issues:
    python -m anonymizer.worker --in in.txt --out out.json --ner gliner [--corporate] [--llm ...]
Reads UTF-8 text from ``--in``; writes ``{anonymized_text, mapping, summary}``
JSON to ``--out``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer.engine import build_anonymizer  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--ner", default="gliner", choices=["gliner", "natasha", "none"])
    ap.add_argument("--no-regex", action="store_true", help="Disable regex detectors")
    ap.add_argument("--device", default="cpu", help="GLiNER device: cpu | cuda | dml")
    ap.add_argument("--corporate", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--llm-base-url", default="http://127.0.0.1:1234/v1")
    ap.add_argument("--llm-model", default="qwen/qwen3.5-9b")
    ap.add_argument("--llm-api-key", default="not-needed")
    ap.add_argument("--llm-no-think", action="store_true",
                    help="Disable reasoning (sends reasoning_effort=none — works for Ollama)")
    args = ap.parse_args()

    text = Path(args.inp).read_text(encoding="utf-8")

    gliner_config = None
    if args.ner == "gliner":
        from anonymizer.gliner_ner import GLiNERConfig

        gliner_config = GLiNERConfig(device=args.device)

    llm_config = None
    if args.llm:
        from anonymizer.llm import LLMConfig

        extra = {"reasoning_effort": "none"} if args.llm_no_think else {}
        llm_config = LLMConfig(
            base_url=args.llm_base_url, model=args.llm_model,
            api_key=args.llm_api_key, extra_body=extra,
        )

    anon = build_anonymizer(
        use_regex=not args.no_regex,
        use_ner=args.ner != "none",
        ner_backend="gliner" if args.ner == "gliner" else "natasha",
        corporate=args.corporate,
        gliner_config=gliner_config,
        use_llm=args.llm,
        llm_config=llm_config,
    )
    res = anon.anonymize(text)
    Path(args.out).write_text(
        json.dumps(
            {
                "anonymized_text": res.anonymized_text,
                "mapping": res.mapping,
                "summary": res.summary,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
