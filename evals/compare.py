"""Compare eval runs. One row per run, one column per thing that can regress.

    python -m evals.compare                       # every run, oldest first
    python -m evals.compare baseline-perf rec2-*  # named runs, in that order

Written because a latency change is only ever half a result. Every knob in this
app trades speed against grounding, so a run that got faster and a run that got
faster *without dropping a case* are different outcomes, and reading them from
two separate summaries is how you talk yourself into the first one. The pass
column sits next to the seconds, and the delta column is measured against the
run above rather than against the best run, so a regression shows up as a
regression instead of being hidden by an earlier win.

Medians, not means: one 107-second case moves a mean by seven seconds and tells
you nothing about the run.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

# Every stage a case can spend time in, plus the two derived columns. `ttft` is
# absent from runs made before the draft was streamed, and prints as "—" there
# rather than as zero — a run that did not measure something did not measure
# zero of it.
COLUMNS = [
    ("pass", "pass"),
    ("total", "total"),
    ("tools", "tools"),
    ("draft", "draft"),
    ("draft_ttft", "ttft"),
]


def load(pattern: str) -> list[Path]:
    hits = sorted(RESULTS.glob(f"{pattern}.json" if pattern.endswith("*")
                               else f"{pattern}-*.json"))
    return hits or sorted(RESULTS.glob(f"{pattern}*.json"))


def summarise(path: Path) -> dict:
    d = json.loads(path.read_text())
    answered = [c for c in d["cases"] if (c.get("latency_ms") or {}).get("total")]
    row = {"label": d["label"], "when": d["when"], "file": path.name,
           "pass": f"{d['passed']}/{d['total']}",
           "passed": d["passed"], "n": len(answered),
           "failed": [c["id"] for c in d["cases"] if not c["auto_pass"]]}
    for key, _ in COLUMNS[1:]:
        vals = [(c["latency_ms"] or {}).get(key) for c in answered]
        vals = [v for v in vals if isinstance(v, int) and v > 0]
        row[key] = st.median(vals) / 1000 if vals else None
        row[key + "_max"] = max(vals) / 1000 if vals else None
        row[key + "_sum"] = sum(vals) / 1000 if vals else None
    return row


def cell(value: float | None, prev: float | None, width: int = 15) -> str:
    if value is None:
        return "—".rjust(width)
    if prev is None or prev == 0:
        return f"{value:.1f}s".rjust(width)
    delta = (value - prev) / prev * 100
    sign = "+" if delta >= 0 else ""
    return f"{value:.1f}s ({sign}{delta:.0f}%)".rjust(width)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("labels", nargs="*",
                    help="run labels in the order they were made; "
                         "default is every run on disk, oldest first")
    ap.add_argument("--stat", choices=("median", "max", "sum"),
                    default="median")
    args = ap.parse_args()

    paths: list[Path] = []
    if args.labels:
        for lab in args.labels:
            found = load(lab)
            if not found:
                raise SystemExit(f"no run matching {lab!r} in {RESULTS}")
            paths.extend(found)
    else:
        paths = sorted(RESULTS.glob("*.json"), key=lambda p: p.stem.split("-")[-1])

    rows = [summarise(p) for p in paths]
    suffix = {"median": "", "max": "_max", "sum": "_sum"}[args.stat]

    head = f"{'run':<34}{'pass':>7}" + "".join(
        f"{label:>15}" for _, label in COLUMNS[1:])
    print(f"\n{args.stat} per answered case\n")
    print(head)
    print("-" * len(head))
    prev: dict | None = None
    for r in rows:
        line = f"{r['label'][:33]:<34}{r['pass']:>7}"
        for key, _ in COLUMNS[1:]:
            line += cell(r.get(key + suffix),
                         prev.get(key + suffix) if prev else None)
        print(line)
        prev = r
    print()
    for r in rows:
        if r["failed"]:
            print(f"  {r['label']}: failed {', '.join(r['failed'])}")


if __name__ == "__main__":
    main()
