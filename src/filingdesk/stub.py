"""Offline fixtures. Lets the whole thread run with no Ollama and no SEC,
so the wiring can be proved before the real stack is attached.

ALL NUMBERS HERE ARE SYNTHETIC. Not NVDA's actual results.

Synthetic mode gets its OWN database, which matters for two reasons. It used
to write straight into the real cache — `DELETE FROM facts WHERE cik=1045810`
then insert fabricated NVDA numbers — so running the eval harness silently
replaced real filings with invented ones, and every later chart showed them.
It also means the harness no longer fights the running server for DuckDB's
single-writer lock.
"""
import datetime as dt
import os
import re
import tempfile
from pathlib import Path

from . import config, db, llm, seed, vault

M = 1_000_000
CIK = 1045810
REV_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"

# (start, end, revenue_musd, gross_profit_musd)
Q = [
    ("2024-01-29", "2024-04-28", 26044, 16720),
    ("2024-04-29", "2024-07-28", 30040, 20667),
    ("2024-07-29", "2024-10-27", 35082, 24978),
    ("2025-01-27", "2025-04-27", 44062, 32209),
    ("2025-04-28", "2025-07-27", 46743, 34356),
    ("2025-07-28", "2025-10-26", 57006, 42070),
]
FY = [
    ("2024-01-29", "2025-01-26", 130497, 90683),   # implies a Q4
    ("2025-01-27", "2026-01-25", 208811, 153714),  # implies a Q4
]


def seed_duckdb() -> None:
    with db.writing() as con:
        _seed(con)


def _seed(con) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS facts (
        cik BIGINT, entity VARCHAR, concept VARCHAR, unit VARCHAR,
        start DATE, "end" DATE, val DOUBLE, fy INTEGER, fp VARCHAR,
        form VARCHAR, accn VARCHAR, filed VARCHAR, frame VARCHAR)""")
    con.execute("DELETE FROM facts WHERE cik = ?", [CIK])
    rows = []

    def add(concept, s, e, v, form, accn, filed):
        rows.append((CIK, "NVIDIA CORP (SYNTHETIC)", concept, "USD",
                     s, e, v * M, None, None, form, accn, filed, None))

    def filed_after(end, days):
        return (dt.date.fromisoformat(end) + dt.timedelta(days=days)).isoformat()

    for i, (s, e, rev, gp) in enumerate(Q):
        acc = f"0001045810-{e[2:4]}-{i:06d}"
        add(REV_TAG, s, e, rev, "10-Q", acc, filed_after(e, 24))
        add("GrossProfit", s, e, gp, "10-Q", acc, filed_after(e, 24))
    for i, (s, e, rev, gp) in enumerate(FY):
        acc = f"0001045810-{e[2:4]}-9{i:05d}"
        add(REV_TAG, s, e, rev, "10-K", acc, filed_after(e, 35))
        add("GrossProfit", s, e, gp, "10-K", acc, filed_after(e, 35))

    # noise the real data has and the skeleton must survive:
    # a 9-month cumulative that must be dropped
    add(REV_TAG, "2024-01-29", "2024-10-27", 91166, "10-Q",
        "0001045810-24-000003", "2024-11-20")
    # Q3 FY25 restated downward in a LATER filing. Latest filed must win.
    add("GrossProfit", "2024-07-29", "2024-10-27", 24901, "10-K",
        "0001045810-25-900000", "2025-03-02")

    con.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"[stub] seeded {len(rows)} synthetic facts")


def fake_embed(texts):
    out = []
    for t in texts:
        h = abs(hash(t.lower()))
        out.append([((h >> (i % 30)) % 97) / 97.0 for i in range(64)])
    return out


_last_facts: list = []


def _last_user(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "")
    return ""


def fake_chat(messages, tools=None):
    """Behaviour depends on the question, so the 5 smoke questions exercise
    5 different paths rather than replaying one canned answer."""
    if tools:
        # Already have tool output? Stop calling tools.
        if any(m.get("role") == "tool" for m in messages):
            return {"role": "assistant", "content": "I have what I need."}
        q = _last_user(messages).lower()
        # Route to the ticker actually asked about. Hardcoding NVDA here made
        # every non-NVDA case exercise the entity-mismatch rejection path
        # instead of the one it was written to test.
        # The prompt reads "Ticker: NVDA. <question>" — rstrip the sentence
        # period without eating the one in a ticker like BRK.A.
        m = re.search(r"ticker:\s*([a-z0-9.\-]+)", q)
        tick = (m.group(1) if m else "nvda").upper().rstrip(".")
        if "operating expense" in q:
            args = {"ticker": tick, "concept": "OperatingExpenses"}
            name = "fd_get_concept"
        elif "gross profit" in q:
            args = {"ticker": tick, "concept": "GrossProfit", "quarters": 8}
            name = "fd_get_concept"
        else:
            args = {"ticker": tick, "metric": "gross_margin", "quarters": 8}
            name = "fd_compute_metric"
        return {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": name, "arguments": args}}]}

    prompt = messages[-1]["content"]
    facts = re.findall(r"\[\[fact:(\d+)\]\] (\S+) = ([\d,.-]+)", prompt)
    if facts:
        _last_facts[:] = facts          # repair prompt carries no FACTS block
    else:
        facts = list(_last_facts)
    if not facts:
        return {"role": "assistant", "content": "No facts were provided."}

    def fmt(raw):
        v = float(raw.replace(",", ""))
        return f"{v * 100:.1f}%" if abs(v) < 100 else f"${v:,.0f}"

    fid, concept, val = facts[-1]
    first_id, _, first_val = facts[0]
    body = (f"{concept} was {fmt(val)} [[fact:{fid}]] in the most recent "
            f"period, against {fmt(first_val)} [[fact:{first_id}]] at the "
            f"start of the series.")
    if "PROBLEMS" not in prompt:
        # Deliberate fabrication so the guard has something to catch.
        body += " It peaked at 81.2% [[fact:1]] mid-period."
    return {"role": "assistant", "content": body}


NOTES = {
    "reading-margins.md": (
        "# Reading margin moves\n\n"
        "A margin move is only interesting if it survives the mix question. "
        "Ask first whether the mix of what was sold changed, then whether "
        "pricing changed. Most reported expansion is mix.\n\n"
        "Treat any single quarter as noise. Two consecutive moves in the same "
        "direction is the minimum before I call it a trend.\n"
    ),
    "restatements.md": (
        "# Restatements\n\n"
        "When a prior period is restated, always quote the restated figure and "
        "say that it was restated. Quoting the original silently is the worst "
        "case: it looks right and reconciles to nothing.\n"
    ),
}


def no_fetch(ticker: str, force: bool = False) -> dict:
    """Stand in for seed.ensure so stub mode never reaches SEC EDGAR.

    On-demand loading means an unseeded company is normally fetched live. In
    synthetic mode that would quietly turn a no-network run into a networked
    one — so a company that is not in the fixtures is reported as having no
    data, which is what the offline fixtures actually represent.
    """
    cik = _companies().resolve(ticker)
    if cik is None:
        return {"ok": False, "error_kind": "unknown_ticker",
                "error": f"{ticker.upper()} is not an SEC-registered ticker."}
    if not _series().has_any(cik):
        return {"ok": False, "error_kind": "no_company_data",
                "error": f"No synthetic fixtures are loaded for "
                         f"{ticker.upper()}."}
    return {"ok": True, "cached": True, "cik": cik, "ticker": ticker.upper(),
            "n_facts": 0, "entity": "", "stale": False, "refreshed": None}


def _companies():
    from . import companies
    return companies


def _series():
    from . import series
    return series


def isolate() -> Path:
    """Point every store at a synthetic-only directory.

    Must run before anything opens the database, which is why install() calls
    it first. db resolves config.DUCK on every call, so redirecting config
    and dropping cached handles is enough to send readers and writers
    somewhere else.
    """
    root = config.ROOT / "stub"
    root.mkdir(parents=True, exist_ok=True)
    db.close_all()
    config.DUCK = root / "filings.duckdb"
    config.VAULT_DB = root / "vault.db"
    return root


def install() -> None:
    print("[stub] SYNTHETIC MODE — numbers below are fabricated fixtures")
    # Mark the environment so the MCP tool subprocess isolates itself too.
    # The CLI and the eval harness call install() directly rather than via
    # FD_STUB, and without this the child reads the REAL filings database —
    # which is how an eval "in synthetic mode" ended up quoting genuine NVDA
    # figures and failing the restatement case for want of a fixture.
    os.environ["FD_STUB"] = "1"
    isolate()
    seed_duckdb()
    llm.chat = fake_chat
    llm.embed = fake_embed
    seed.ensure = no_fetch
    d = Path(tempfile.mkdtemp(prefix="fd-vault-"))
    for name, body in NOTES.items():
        (d / name).write_text(body)
    vault.index(str(d), embed_fn=fake_embed)
