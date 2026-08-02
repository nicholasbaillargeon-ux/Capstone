"""Eval harness. One automated check, plus a hand-grading column.

    python -m evals.run_evals --stub
    python -m evals.run_evals --stub --label before-fix
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evals.cases import CASES  # noqa: E402
from filingdesk import agent, logging_setup  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


def auto_check(case: dict, res: dict) -> tuple[bool, str]:
    """THE automated check. One function, applied to every case.

    A report is correct only if every figure in it traces to a retrieved fact.
    A refusal is correct only if it refuses for the RIGHT reason — 'it broke'
    and 'that is out of scope' are different answers to the user.
    """
    if case["kind"] == "refuse":
        if not res.get("refused"):
            return False, "answered a question it should have refused"
        got = res.get("refusal_kind")
        want = case.get("refusal")
        if want and got != want:
            return False, f"refused as {got or 'untyped'}, expected {want}"
        return True, f"refused: {got}"

    if res.get("refused"):
        return False, f"refused a valid question: {res['refused'][:60]}"

    unsupported = res.get("unsupported_claims") or []
    if unsupported:
        return False, "ungrounded: " + ", ".join(p["claim"] for p in unsupported)

    n = len(res.get("facts") or [])
    if n < case.get("facts_min", 1):
        return False, f"only {n} facts, expected >= {case['facts_min']}"

    body_raw = res.get("report_md") or ""
    # An empty or citation-free report trivially has zero ungrounded figures.
    # Caught in practice: a broken draft returned "No facts were provided."
    # and passed every other check.
    cites = re.findall(r"\[\[fact:(\d+)\]\]", body_raw)
    if not cites:
        return False, "report cites no facts"
    bad = [c for c in cites if not (1 <= int(c) <= n)]
    if bad:
        return False, f"citation out of range: {bad[:3]}"

    body = body_raw.lower()
    for m in case.get("must", []):
        if m.lower() not in body:
            return False, f"missing required mention: {m!r}"
    for m in case.get("must_not", []):
        if m.lower() in body:
            return False, f"contains forbidden text: {m!r}"

    return True, f"{n} facts, 0 ungrounded"


def summarise_latency(rows: list[dict]) -> str:
    """Median and worst, per stage. Mean hides the case that times out."""
    answered = [r for r in rows if r["latency_ms"].get("total")]
    if not answered:
        return "latency: no answered cases"
    def stat(key: str) -> str:
        vals = sorted(r["latency_ms"].get(key, 0) for r in answered)
        med = vals[len(vals) // 2] / 1000
        return f"{key} {med:.1f}s/{vals[-1] / 1000:.1f}s"
    parts = [stat(k) for k in ("total", "tools", "draft", "guard")]
    return f"latency (median/worst over {len(answered)}): " + "  ".join(parts)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    logging_setup.configure()
    if args.stub:
        from filingdesk import stub
        stub.install()

    rows = []
    print(f"\n{'id':<4}{'auto':<7}{'detail':<52}case")
    print("-" * 100)
    for c in CASES:
        try:
            res = await agent.run(c["q"], c["ticker"])
        except Exception as exc:  # noqa: BLE001
            res = {"crashed": repr(exc)}
            ok, why = False, f"CRASH {exc!r}"[:50]
        else:
            ok, why = auto_check(c, res)
        rows.append({
            "id": c["id"], "kind": c["kind"], "question": c["q"],
            "ticker": c["ticker"], "note": c["note"],
            "auto_pass": ok, "auto_detail": why,
            "hand_grade": None, "hand_note": "",
            "refusal_kind": res.get("refusal_kind"),
            "report_md": res.get("report_md"),
            "refused": res.get("refused"),
            "n_facts": len(res.get("facts") or []),
            "unsupported": [p["claim"] for p in (res.get("unsupported_claims") or [])],
            "trace_id": res.get("trace_id"),
            # Latency belongs next to correctness, not in a separate exercise:
            # every way to make this faster trades against grounding, and a
            # speed number read apart from the pass column invites taking that
            # trade without noticing.
            "latency_ms": res.get("latency_ms") or {},
        })
        secs = (rows[-1]["latency_ms"].get("total") or 0) / 1000
        print(f"{c['id']:<4}{'PASS' if ok else 'FAIL':<7}{why[:50]:<52}"
              f"{secs:5.1f}s  {c['q'][:32]}")

    passed = sum(r["auto_pass"] for r in rows)
    print("-" * 100)
    print(f"automated: {passed}/{len(rows)} pass")
    print(summarise_latency(rows) + "\n")
    for r in rows:
        if not r["auto_pass"]:
            print(f"  FAIL {r['id']}  {r['auto_detail']}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS / f"{args.label}-{stamp}.json"
    out.write_text(json.dumps(
        {"label": args.label, "when": stamp,
         "passed": passed, "total": len(rows), "cases": rows},
        indent=2, default=str))
    print(f"\nwritten: {out.name}")

    md = RESULTS / f"{args.label}-{stamp}.md"
    lines = [f"# Eval run — {args.label}", "",
             f"Automated: **{passed}/{len(rows)}**  ·  {stamp}", "",
             "| id | kind | auto | detail | hand | notes |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['kind']} | "
                     f"{'PASS' if r['auto_pass'] else 'FAIL'} | "
                     f"{r['auto_detail'][:60]} |  |  |")
    lines += ["", "Hand-grade column is deliberately blank. The automated check "
              "verifies grounding and refusal type; it cannot tell you whether the "
              "prose is *useful*. Fill it in while reading `report_md` in the JSON."]
    md.write_text("\n".join(lines))
    print(f"written: {md.name}")


if __name__ == "__main__":
    asyncio.run(main())
