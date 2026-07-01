"""Evaluate the anonymizer against the redmadrobot pii_benchmark.

Methodology mirrors the benchmark card: span-level matching with adjacent
same-category spans merged and *overlap* counting (a prediction scores if it
overlaps a gold span of the same coarse category). We report per-category
precision / recall / F1 plus the headline PERSON+LOCATION "common scope".

Gold char spans are reconstructed by aligning the dataset's whitespace tokens
back onto the original text (case-insensitive, left-to-right). Fine gold labels
are folded to the coarse space the detectors produce.

Usage:
    python anonymizer/eval_benchmark.py [--csv PATH] [--limit N] [--no-ner]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anonymizer.engine import Anonymizer, build_anonymizer  # noqa: E402
from anonymizer.spans import Span  # noqa: E402

csv.field_size_limit(10_000_000)

# Fine benchmark label -> coarse category used for scoring.
_COARSE: dict[str, str] = {
    "FIRST_NAME": "PERSON",
    "LAST_NAME": "PERSON",
    "MIDDLE_NAME": "PERSON",
    "COUNTRY": "LOCATION",
    "REGION": "LOCATION",
    "DISTRICT": "LOCATION",
    "CITY": "LOCATION",
    "STREET": "LOCATION",
    "HOUSE": "LOCATION",
}


def coarse(label: str) -> str:
    return _COARSE.get(label, label)


def align_tokens(text: str, tokens: list[str]) -> list[tuple[int, int] | None]:
    """Map each whitespace token to a (start, end) char span in ``text``."""
    spans: list[tuple[int, int] | None] = []
    low = text.lower()
    cursor = 0
    for tok in tokens:
        t = tok.lower()
        idx = low.find(t, cursor)
        if idx < 0:
            idx = low.find(t)  # restart search; tokens may be reordered/cased
        if idx < 0:
            spans.append(None)
            continue
        spans.append((idx, idx + len(tok)))
        cursor = idx + len(tok)
    return spans


def gold_spans(text: str, tokens: list[str], tags: list[str]) -> list[tuple[int, int, str]]:
    """Reconstruct coarse gold spans (start, end, category) from BIO tags."""
    offs = align_tokens(text, tokens)
    spans: list[tuple[int, int, str]] = []
    cur_label: str | None = None
    cur_start = cur_end = 0
    for off, tag in zip(offs, tags):
        if tag == "O" or off is None:
            base = None
        else:
            base = coarse(tag[2:])  # strip "B-"/"I-"
        boundary = tag.startswith("B-")
        if base is not None and base == cur_label and not boundary:
            cur_end = off[1]
            continue
        if cur_label is not None:
            spans.append((cur_start, cur_end, cur_label))
        if base is not None:
            cur_label, cur_start, cur_end = base, off[0], off[1]
        else:
            cur_label = None
    if cur_label is not None:
        spans.append((cur_start, cur_end, cur_label))
    return spans


def merge_adjacent(spans: list[Span], gap: int = 2) -> list[tuple[int, int, str]]:
    """Merge same-category predicted spans separated by only a tiny gap."""
    items = sorted(((s.start, s.end, coarse(s.label)) for s in spans))
    merged: list[tuple[int, int, str]] = []
    for start, end, cat in items:
        if merged and merged[-1][2] == cat and start - merged[-1][1] <= gap:
            ps, pe, pc = merged[-1]
            merged[-1] = (ps, max(pe, end), pc)
        else:
            merged.append((start, end, cat))
    return merged


def _overlap(a: tuple[int, int, str], b: tuple[int, int, str]) -> bool:
    return a[2] == b[2] and a[0] < b[1] and b[0] < a[1]


def _overlap_any(a: tuple[int, int, str], b: tuple[int, int, str]) -> bool:
    """Positional overlap, ignoring category (did we mask this region at all?)."""
    return a[0] < b[1] and b[0] < a[1]


def _snippet(text: str, start: int, end: int, pad: int = 25) -> str:
    """Context snippet with the span marked by »...«."""
    left = text[max(0, start - pad) : start]
    mid = text[start:end]
    right = text[end : end + pad]
    return f"...{left}»{mid}«{right}...".replace("\n", " ")


def evaluate(
    anon: Anonymizer,
    rows: list[dict],
    *,
    progress: bool = True,
    errors: dict[str, dict[str, list]] | None = None,
) -> dict:
    """Run the anonymizer over rows and accumulate overlap-match counts.

    If ``errors`` is given, false negatives (missed gold) and false positives
    (spurious predictions) are collected per category as ``(snippet, span_text)``.
    """
    # per category: gold_total, gold_hit_same, pred_total, pred_hit, gold_covered_any
    acc: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    total = len(rows)
    for i, row in enumerate(rows):
        if progress and i % 200 == 0:
            print(f"  ...{i}/{total}", file=sys.stderr)
        text = row["text"]
        tokens = json.loads(row["tokens"])
        tags = json.loads(row["ner_tags"])
        if len(tokens) != len(tags):
            continue
        golds = gold_spans(text, tokens, tags)
        preds = merge_adjacent(anon.anonymize(text).spans)

        for g in golds:
            a = acc[g[2]]
            a[0] += 1
            same = any(_overlap(g, p) for p in preds)
            others = sorted({p[2] for p in preds if _overlap_any(g, p)} - {g[2]})
            if same:
                a[1] += 1
                a[4] += 1
            elif others:  # masked, but under a different category
                a[4] += 1
                if errors is not None:
                    errors[g[2]]["MISMATCH"].append(
                        (_snippet(text, g[0], g[1]), text[g[0] : g[1]], "/".join(others))
                    )
            elif errors is not None:  # not masked at all => real leak
                errors[g[2]]["TRUEMISS"].append((_snippet(text, g[0], g[1]), text[g[0] : g[1]]))
        for p in preds:
            acc[p[2]][2] += 1
            if any(_overlap(p, g) for g in golds):
                acc[p[2]][3] += 1
            elif errors is not None:
                errors[p[2]]["FP"].append((_snippet(text, p[0], p[1]), text[p[0] : p[1]]))
    return acc


def evaluate_ab(
    base: Anonymizer,
    full: Anonymizer,
    rows: list[dict],
    *,
    progress: bool = True,
) -> tuple[dict, dict]:
    """Compare two anonymizers on the same rows (e.g. with/without the LLM layer).

    Returns ``(acc_base, acc_full)``. Only ``full`` is expected to be slow
    (one LLM call per row); ``base`` shares the regex+NER work.
    """
    import time

    acc_base: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    acc_full: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    total = len(rows)
    t0 = time.time()
    for i, row in enumerate(rows):
        if progress:
            elapsed = time.time() - t0
            print(f"  ...{i}/{total}  ({elapsed:.0f}s elapsed)", file=sys.stderr)
        text = row["text"]
        tokens = json.loads(row["tokens"])
        tags = json.loads(row["ner_tags"])
        if len(tokens) != len(tags):
            continue
        golds = gold_spans(text, tokens, tags)
        for anon, acc in ((base, acc_base), (full, acc_full)):
            preds = merge_adjacent(anon.anonymize(text).spans)
            for g in golds:
                acc[g[2]][0] += 1
                if any(_overlap(g, p) for p in preds):
                    acc[g[2]][1] += 1
                    acc[g[2]][4] += 1
                elif any(_overlap_any(g, p) for p in preds):
                    acc[g[2]][4] += 1
            for p in preds:
                acc[p[2]][2] += 1
                if any(_overlap(p, g) for g in golds):
                    acc[p[2]][3] += 1
    return acc_base, acc_full


def dump_errors(errors: dict[str, dict[str, list]], categories: list[str], show: int) -> None:
    """Print, per category: TRUE MISSES, CATEGORY MISMATCHES, and SPURIOUS."""
    for cat in categories:
        miss = errors.get(cat, {}).get("TRUEMISS", [])
        mism = errors.get(cat, {}).get("MISMATCH", [])
        fp = errors.get(cat, {}).get("FP", [])
        print(
            f"\n{'=' * 72}\n{cat}: {len(miss)} ПРОПУЩЕНО СОВСЕМ, "
            f"{len(mism)} не та категория, {len(fp)} лишних\n{'=' * 72}"
        )
        print(f"\n-- ПРОПУЩЕНО СОВСЕМ (не замаскировано — утечка) [{min(show, len(miss))} из {len(miss)}] --")
        for snippet, span_text in miss[:show]:
            print(f"  [{span_text}]  {snippet}")
        print(f"\n-- ОПРЕДЕЛЕНО, НО НЕ В ТУ КАТЕГОРИЮ (данные скрыты) [{min(show, len(mism))} из {len(mism)}] --")
        for snippet, span_text, other in mism[:show]:
            print(f"  [{span_text}] -> помечено как {other}  {snippet}")
        print(f"\n-- ЛИШНЕЕ (отметили, нет в эталоне) [{min(show, len(fp))} из {len(fp)}] --")
        for snippet, span_text in fp[:show]:
            print(f"  [{span_text}]  {snippet}")


def _prf(gold_total: int, gold_hit: int, pred_total: int, pred_hit: int):
    recall = gold_hit / gold_total if gold_total else 0.0
    precision = pred_hit / pred_total if pred_total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def report(acc: dict[str, list[int]]) -> None:
    cats = sorted(acc)
    # cover% = masked under ANY category (privacy recall); miss = not masked at all.
    print(
        f"\n{'category':<18}{'P':>7}{'R':>7}{'F1':>7}"
        f"{'gold':>6}{'cover%':>8}{'miss':>6}"
    )
    print("-" * 59)
    for cat in cats:
        gt, gh, pt, ph, cov = acc[cat]
        p, r, f = _prf(gt, gh, pt, ph)
        cover = cov / gt if gt else 0.0
        miss = gt - cov
        print(f"{cat:<18}{p:>7.3f}{r:>7.3f}{f:>7.3f}{gt:>6}{cover:>8.3f}{miss:>6}")

    def micro(keys: list[str]) -> tuple[float, float, float, int, int]:
        gt = sum(acc[k][0] for k in keys if k in acc)
        gh = sum(acc[k][1] for k in keys if k in acc)
        pt = sum(acc[k][2] for k in keys if k in acc)
        ph = sum(acc[k][3] for k in keys if k in acc)
        cov = sum(acc[k][4] for k in keys if k in acc)
        p, r, f = _prf(gt, gh, pt, ph)
        return p, r, f, gt, cov

    print("-" * 59)
    for name, keys in (("PERSON+LOCATION", ["PERSON", "LOCATION"]), ("ALL (micro)", cats)):
        p, r, f, gt, cov = micro(keys)
        cover = cov / gt if gt else 0.0
        print(f"{name:<18}{p:>7.3f}{r:>7.3f}{f:>7.3f}{gt:>6}{cover:>8.3f}{gt - cov:>6}")
    _, _, _, gt, cov = micro(cats)
    print(
        f"\nКЛЮЧЕВОЕ: замаскировано (любой меткой) {cov}/{gt} = {cov / gt:.1%}; "
        f"ПРОПУЩЕНО СОВСЕМ (утечки): {gt - cov}"
    )


def report_ab(acc_base: dict, acc_full: dict) -> None:
    """Side-by-side F1 for base vs full, with the delta."""
    cats = sorted(set(acc_base) | set(acc_full))

    def f1(acc: dict, cat: str) -> float:
        if cat not in acc:
            return 0.0
        return _prf(*acc[cat][:4])[2]

    print(f"\n{'category':<18}{'base F1':>10}{'+LLM F1':>10}{'delta':>10}")
    print("-" * 48)
    for cat in cats:
        b, f = f1(acc_base, cat), f1(acc_full, cat)
        print(f"{cat:<18}{b:>10.3f}{f:>10.3f}{f - b:>+10.3f}")

    def micro(acc: dict, keys: list[str]) -> tuple[float, float, float]:
        gt = sum(acc[k][0] for k in keys if k in acc)
        gh = sum(acc[k][1] for k in keys if k in acc)
        pt = sum(acc[k][2] for k in keys if k in acc)
        ph = sum(acc[k][3] for k in keys if k in acc)
        return _prf(gt, gh, pt, ph)

    print("-" * 48)
    for name, keys in (("PERSON+LOCATION", ["PERSON", "LOCATION"]), ("ALL (micro)", cats)):
        pb, rb, fb = micro(acc_base, keys)
        pf, rf, ff = micro(acc_full, keys)
        print(f"{name:<18}{fb:>10.3f}{ff:>10.3f}{ff - fb:>+10.3f}")
        print(f"  {'(P/R base)':<16}{pb:>10.3f}{rb:>10.3f}")
        print(f"  {'(P/R +LLM)':<16}{pf:>10.3f}{rf:>10.3f}")


def _backend(args) -> str:
    return "gliner" if getattr(args, "gliner", False) else "natasha"


def _gliner_cfg(args):
    if not getattr(args, "gliner", False):
        return None
    from anonymizer.gliner_ner import GLiNERConfig

    return GLiNERConfig(threshold=args.gliner_threshold, device=getattr(args, "device", "cpu"))


def _make_anon(args):
    """Build a local anonymizer, or a remote client if --remote-url is set."""
    if getattr(args, "remote_url", ""):
        from anonymizer.remote_client import RemoteAnonymizer

        stages: dict = {}
        if getattr(args, "no_regex", False):
            stages["regex"] = False
        if getattr(args, "gliner_threshold", None) is not None:
            stages["ner_threshold"] = args.gliner_threshold
        return RemoteAnonymizer(args.remote_url, args.remote_key, stages=stages or None)
    return build_anonymizer(
        use_regex=not getattr(args, "no_regex", False),
        use_ner=not args.no_ner, ner_backend=_backend(args),
        gliner_config=_gliner_cfg(args), use_llm=args.llm, llm_config=_llm_cfg(args),
    )


def _llm_cfg(args):
    if not getattr(args, "llm", False):
        return None
    from anonymizer.llm import LLMConfig

    extra = {"reasoning_effort": "none"} if getattr(args, "llm_no_think", False) else {}
    return LLMConfig(
        base_url=args.llm_base_url, model=args.llm_model,
        api_key=args.llm_api_key, extra_body=extra,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        default="benchmark/pii_benchmark/test.csv",
        help="Path to the benchmark test.csv",
    )
    parser.add_argument("--limit", type=int, default=0, help="Evaluate first N rows only")
    parser.add_argument("--no-ner", action="store_true", help="Regex detectors only")
    parser.add_argument("--no-regex", action="store_true",
                        help="Disable regex detectors (e.g. test GLiNER-only with --gliner)")
    parser.add_argument("--llm", action="store_true", help="Also run the local LLM layer")
    parser.add_argument(
        "--gliner",
        action="store_true",
        help="Use GLiNER (multilingual) for NER instead of Natasha",
    )
    parser.add_argument(
        "--gliner-threshold",
        type=float,
        default=0.3,
        help="GLiNER entity confidence threshold (higher = more precise)",
    )
    parser.add_argument("--device", default="cpu", help="GLiNER device: cpu | cuda | dml")
    parser.add_argument("--llm-base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--llm-model", default="qwen/qwen3.5-9b")
    parser.add_argument("--llm-api-key", default="not-needed")
    parser.add_argument("--llm-no-think", action="store_true",
                        help="Disable reasoning (reasoning_effort=none — for Ollama)")
    parser.add_argument("--remote-url", default="",
                        help="Use a remote backend (server.py) instead of running locally")
    parser.add_argument("--remote-key", default="", help="Bearer token for the remote backend")
    parser.add_argument(
        "--errors",
        default="",
        help="Comma-separated categories to dump FN/FP examples for (e.g. SNILS,EMAIL)",
    )
    parser.add_argument("--show", type=int, default=15, help="Examples per category in --errors")
    parser.add_argument("--sample", type=int, default=0, help="Random sample of N rows")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed")
    parser.add_argument(
        "--ab",
        action="store_true",
        help="A/B: compare regex+NER vs regex+NER+LLM on the same rows",
    )
    parser.add_argument(
        "--demo",
        type=int,
        default=0,
        help="Print anonymized text + mapping for the first N rows (no scoring)",
    )
    args = parser.parse_args()

    path = Path(args.csv)
    if not path.exists():
        parser.error(f"CSV not found: {path}")

    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if args.sample and args.sample < len(rows):
        import random

        rows = random.Random(args.seed).sample(rows, args.sample)
    elif args.limit:
        rows = rows[: args.limit]

    print(f"Evaluating {len(rows)} rows from {path}")

    if args.demo:
        anon = _make_anon(args)
        for row in rows[: args.demo]:
            res = anon.anonymize(row["text"])
            print("\n" + "=" * 72)
            print("ИСХОДНЫЙ:    ", res.text)
            print("ОБЕЗЛИЧЕННЫЙ:", res.anonymized_text)
            print("МАППИНГ:")
            for ph, original in res.mapping.items():
                print(f"    {ph} -> {original}")
        return

    if args.ab:
        print("Loading Natasha; LLM A/B (one LLM call per row, this is slow)...", file=sys.stderr)
        base = build_anonymizer(use_ner=not args.no_ner, ner_backend=_backend(args), gliner_config=_gliner_cfg(args))
        full = build_anonymizer(use_ner=not args.no_ner, ner_backend=_backend(args), gliner_config=_gliner_cfg(args), use_llm=True, llm_config=_llm_cfg(args))
        acc_base, acc_full = evaluate_ab(base, full, rows)
        report_ab(acc_base, acc_full)
        return

    anon = _make_anon(args)
    if not args.no_ner:
        print("Loading Natasha NER model...", file=sys.stderr)
    if args.llm:
        print("LLM layer ON (one call per row, slower)...", file=sys.stderr)

    want_errors = [c.strip().upper() for c in args.errors.split(",") if c.strip()]
    errors: dict[str, dict[str, list]] | None = None
    if want_errors:
        errors = defaultdict(lambda: {"TRUEMISS": [], "MISMATCH": [], "FP": []})

    acc = evaluate(anon, rows, errors=errors)
    report(acc)
    if errors is not None:
        dump_errors(errors, want_errors, args.show)


if __name__ == "__main__":
    main()
