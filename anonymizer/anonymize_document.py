"""CLI: anonymize a .docx/.txt file and write the redacted copy + mapping.

Examples:
    python anonymizer/anonymize_document.py mydoc.docx
    python anonymizer/anonymize_document.py mydoc.docx --gliner --llm
    python anonymizer/anonymize_document.py notes.txt --no-ner
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer.documents import anonymize_to_files, read_text  # noqa: E402
from anonymizer.engine import build_anonymizer  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="Path to a .docx or .txt document")
    parser.add_argument("--out-dir", default="", help="Output directory (default: alongside input)")
    parser.add_argument("--no-ner", action="store_true", help="Regex detectors only")
    parser.add_argument("--no-regex", action="store_true",
                        help="Disable regex detectors (e.g. GLiNER-only with --gliner)")
    parser.add_argument("--gliner", action="store_true", help="Use GLiNER NER (else Natasha)")
    parser.add_argument(
        "--corporate",
        action="store_true",
        help="Also mask business data: amounts, contract numbers, dates",
    )
    parser.add_argument("--device", default="cpu", help="GLiNER device: cpu | cuda | dml")
    parser.add_argument("--llm", action="store_true", help="Add the local LLM gap-filler layer")
    parser.add_argument("--llm-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--llm-model", default="qwen/qwen3.5-9b")
    parser.add_argument("--llm-api-key", default="not-needed")
    parser.add_argument("--llm-no-think", action="store_true",
                        help="Disable reasoning (reasoning_effort=none — for Ollama Qwen)")
    parser.add_argument(
        "--review", action="store_true",
        help="Add the LLM review layer: double-checks the final mapping and "
             "reverts obvious false positives (e.g. common words mislabeled "
             "as PERSON/ORG)",
    )
    parser.add_argument("--review-base-url", default=None, help="Defaults to --llm-base-url")
    parser.add_argument("--review-model", default=None, help="Defaults to --llm-model")
    parser.add_argument("--review-no-think", action="store_true")
    parser.add_argument("--preview", type=int, default=1500, help="Chars of anonymized preview")
    args = parser.parse_args()

    src = Path(args.file)
    if not src.exists():
        parser.error(f"File not found: {src}")

    print(f"Читаю {src.name} ...", file=sys.stderr)
    ner_backend = "gliner" if args.gliner else "natasha"
    gliner_config = None
    if args.gliner:
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
    review_config = None
    if args.review:
        from anonymizer.review import ReviewConfig

        review_extra = {"reasoning_effort": "none"} if args.review_no_think else {}
        review_config = ReviewConfig(
            base_url=args.review_base_url or args.llm_base_url,
            model=args.review_model or args.llm_model,
            api_key=args.llm_api_key,
            extra_body=review_extra,
        )
    anon = build_anonymizer(
        use_regex=not args.no_regex,
        use_ner=not args.no_ner,
        ner_backend=ner_backend,
        corporate=args.corporate,
        gliner_config=gliner_config,
        use_llm=args.llm,
        llm_config=llm_config,
        use_review=args.review,
        review_config=review_config,
    )
    import time

    ner_desc = "выкл" if args.no_ner else f"{ner_backend} (device={args.device})"
    llm_desc = f"вкл — {args.llm_model} @ {args.llm_base_url}" if args.llm else "выкл"
    review_desc = (
        f"вкл — {review_config.model} @ {review_config.base_url}" if args.review else "выкл"
    )
    print(
        f"Конфигурация: NER={ner_desc} | corporate={'да' if args.corporate else 'нет'} "
        f"| LLM={llm_desc} | review={review_desc}",
        file=sys.stderr,
    )

    t0 = time.time()
    written = anonymize_to_files(src, anon, args.out_dir or None)
    elapsed = time.time() - t0

    # Re-read for a quick on-screen summary.
    text = read_text(src)
    anon_text = written["text"].read_text(encoding="utf-8")
    import json

    mapping = json.loads(written["mapping"].read_text(encoding="utf-8"))

    by_type: dict[str, int] = {}
    for ph in mapping:
        label = ph.strip("[]").rsplit("_", 1)[0]
        by_type[label] = by_type.get(label, 0) + 1

    print("\n" + "=" * 72)
    print("КОНФИГУРАЦИЯ:")
    print(f"  NER:       {ner_desc}")
    print(f"  corporate: {'да' if args.corporate else 'нет'}")
    print(f"  LLM:       {llm_desc}")
    print(f"  review:    {review_desc}")
    print("СКОРОСТЬ:")
    print(f"  время обработки: {elapsed:.1f} c")
    print(f"  символов:        {len(text)}  ({len(text)/elapsed:.0f} симв/с)")
    print(f"  сущностей:       {len(mapping)}")
    print(f"По типам: {by_type}")
    print("\n--- ПРЕВЬЮ ОБЕЗЛИЧЕННОГО ТЕКСТА ---")
    print(anon_text[: args.preview])
    print("\n--- МАППИНГ (первые 40) ---")
    for ph, original in list(mapping.items())[:40]:
        print(f"  {ph} -> {original}")
    print("\nФайлы:")
    for kind, p in written.items():
        print(f"  {kind}: {p}")


if __name__ == "__main__":
    main()
