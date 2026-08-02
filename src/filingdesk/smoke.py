"""Smoke test. Runs the questions in docs/questions.md against a live service
and writes results to the eval volume. Failures are the point of the exercise.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
from pathlib import Path

from . import agent, config, logging_setup

EVAL_DIR = config.ROOT / "eval"
QUESTIONS = Path(__file__).resolve().parents[2] / "docs" / "questions.md"


def load_questions(path: Path = QUESTIONS) -> list[dict]:
    """Parse `- [TICKER] question text` bullets."""
    if not path.exists():
        sys.exit(f"questions file not found: {path}")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*-\s*\[([A-Z]{1,5})\]\s*(.+?)\s*$", line)
        if m:
            out.append({"ticker": m.group(1), "question": m.group(2)})
    return out


def verdict(res: dict) -> tuple[str, str]:
    if res.get("refused"):
        return "REFUSED", res["refused"]
    if res.get("unsupported_claims"):
        claims = ", ".join(p["claim"] for p in res["unsupported_claims"])
        return "FLAGGED", f"ungrounded: {claims}"
    if not res.get("facts"):
        return "FAIL", "no facts"
    return "PASS", f"{len(res['facts'])} facts"


async def main() -> None:
    logging_setup.configure()
    if "--stub" in sys.argv:
        from . import stub
        stub.install()

    qs = load_questions()
    print(f"[smoke] {len(qs)} questions\n")
    rows, results = [], []
    for i, q in enumerate(qs, 1):
        try:
            res = await agent.run(q["question"], q["ticker"])
        except Exception as exc:  # noqa: BLE001 — a crash is a result
            res = {"error": repr(exc), "trace_id": None}
            rows.append((i, q["ticker"], "CRASH", repr(exc)[:60], None))
            results.append({**q, "verdict": "CRASH", "error": repr(exc)})
            print(f"  {i}. {q['ticker']:<5} CRASH   {repr(exc)[:60]}")
            continue
        v, note = verdict(res)
        rows.append((i, q["ticker"], v, note, res.get("trace_id")))
        results.append({**q, "verdict": v, "note": note,
                        "trace_id": res.get("trace_id"),
                        "latency_ms": res.get("latency_ms")})
        print(f"  {i}. {q['ticker']:<5} {v:<8}{note}")

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = EVAL_DIR / f"smoke-{stamp}.json"
    out.write_text(json.dumps(results, indent=2, default=str))

    counts: dict[str, int] = {}
    for _, _, v, _, _ in rows:
        counts[v] = counts.get(v, 0) + 1
    print(f"\n[smoke] {counts}")
    print(f"[smoke] written to {out}")
    if counts.get("PASS", 0) != len(qs):
        print("[smoke] NOT CLEAN — pick the worst one and fix it tomorrow")


if __name__ == "__main__":
    asyncio.run(main())
