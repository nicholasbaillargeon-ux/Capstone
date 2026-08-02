"""Turn raw XBRL facts into a comparable series.

This is the part that is actually hard, and it is where most of the time
went. The problems are not visible from the companyfacts JSON until you try:

  1. DUPLICATES. The same period appears in many filings (original, then
     again as a comparative in later ones). Latest `filed` wins, which is
     also how restatements are handled.
  2. MIXED DURATIONS. The same concept holds 3-month, 6-month, 9-month and
     annual facts, distinguished only by start/end. Filtering by `fp` does
     not work: `fp` describes the FILING's period, not the FACT's.
  3. NO Q4. Companies file 10-Qs for Q1-Q3 and a 10-K for the year. Q4 does
     not exist as a filed fact and must be derived: FY - (Q1+Q2+Q3).
  4. INSTANT vs DURATION. Balance-sheet concepts have no `start` at all —
     they are a snapshot on one date, not a slice of time. They need their
     own path; the duration path drops them entirely.
  5. SEGMENTS. A consolidated total and a per-segment breakout share a
     concept tag and differ only by XBRL dimensions, which the companyfacts
     API does not expose. Same-period duplicates filed together are resolved
     by taking the largest, which is the consolidated figure.
"""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import asdict, dataclass

import duckdb

from . import config, db


def _log(*a):
    """stdout is the JSON-RPC wire in an MCP stdio server. Log to stderr."""
    print(*a, file=sys.stderr, flush=True)


@dataclass
class Fact:
    id: int = 0
    concept: str = ""
    value: float = 0.0
    unit: str = "USD"
    start: str = ""
    end: str = ""
    form: str = ""
    accn: str = ""
    filed: str = ""
    derived: bool = False
    derivation: str = ""
    restated: bool = False
    label: str = ""

    def dict(self) -> dict:
        return asdict(self)


def _days(a: str, b: str) -> int:
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


def _bucket(start: str, end: str) -> str | None:
    d = _days(start, end)
    if 80 <= d <= 100:
        return "Q"
    if 340 <= d <= 380:
        return "FY"
    return None  # H1, 9-month cumulative, odd stubs — dropped


def _resolve_concept(con, cik: int, logical: str) -> str | None:
    """The single best tag for a concept — the alias with the most recent data.

    Kept for callers that need one name to show. The series builders merge
    across aliases instead; see `_alias_rows`.
    """
    best, best_end = None, ""
    for tag in config.CONCEPT_ALIASES.get(logical, [logical]):
        row = con.execute(
            'SELECT count(*), max("end") FROM facts WHERE cik=? AND concept=?',
            [cik, tag]).fetchone()
        if row and row[0] and str(row[1]) > best_end:
            best, best_end = tag, str(row[1])
    return best


def has_any(cik: int) -> bool:
    try:
        with db.reading() as con:
            return bool(con.execute(
                "SELECT count(*) FROM facts WHERE cik = ?", [cik]).fetchone()[0])
    except duckdb.Error:  # no database yet, or another process is writing
        return False


def available_concepts(cik: int) -> list[str]:
    """Which logical concepts actually have data for this company."""
    try:
        with db.reading() as con:
            rows = con.execute(
                "SELECT DISTINCT concept FROM facts WHERE cik = ?",
                [cik]).fetchall()
    except duckdb.Error:
        return []
    tags = {r[0] for r in rows}
    return [logical for logical, aliases in config.CONCEPT_ALIASES.items()
            if tags & set(aliases)]


def concept_catalog(cik: int) -> list[dict]:
    """available_concepts, annotated for the UI's concept picker."""
    got = set(available_concepts(cik))
    return [{"key": k, "label": v["label"], "group": v["group"],
             "kind": v["kind"], "unit": v.get("unit", "USD")}
            for k, v in config.CONCEPTS.items() if k in got]


@dataclass(frozen=True)
class Pick:
    """One period's winning row, plus which alias and filing it came from."""
    rank: int
    tag: str
    start: str
    end: str
    value: float
    form: str
    accn: str
    filed: str


def _alias_rows(cik: int, logical: str, unit: str) -> list[tuple]:
    """Every alias for a concept in one query, each row tagged with the
    alias's rank in the preference order.

    Merging matters because tag usage changes over a company's life. NVDA
    reports revenue under `Revenues` from 2008 to today but also used
    `RevenueFromContractWithCustomerExcludingAssessedTax` between 2017 and
    2022; picking one alias and stopping gives either a series that ends in
    2022 or one that starts in 2017. Capex switches tag in 2020 the same way.
    Merging by rank keeps the series continuous and still resolves overlaps
    deterministically.
    """
    aliases = config.CONCEPT_ALIASES.get(logical, [logical])
    placeholders = ", ".join("?" * len(aliases))
    try:
        with db.reading() as con:
            rows = con.execute(
                f"""
                SELECT concept, start, "end", val, form, accn, filed
                FROM facts
                WHERE cik = ? AND unit = ? AND concept IN ({placeholders})
                ORDER BY "end", filed
                """, [cik, unit, *aliases]).fetchall()
    except duckdb.Error:
        return []
    rank = {tag: i for i, tag in enumerate(aliases)}
    return [(rank[c], c, s, e, float(v), form, accn, filed)
            for c, s, e, v, form, accn, filed in rows]


def _dedupe(rows) -> tuple[dict, dict]:
    """Collapse each period to one fact.

    Preference order, in order: the higher-ranked alias, then the later
    `filed` (which is how restatements are handled), then the larger absolute
    value — the last of which picks the consolidated figure over a segment
    breakout sharing the same tag in the same filing.

    Restatement is judged within a single tag. Two aliases disagreeing about
    a period is a tagging difference, not a restatement, and flagging it as
    one would be a lie in the UI.
    """
    best: dict[tuple[str, str], Pick] = {}
    seen: dict[tuple[str, str, str], set] = {}
    for rank, tag, start, end, val, form, accn, filed in rows:
        k = (str(start), str(end))
        seen.setdefault((tag, *k), set()).add(round(val, 2))
        cand = Pick(rank, tag, str(start), str(end), val, form or "",
                    accn or "", str(filed))
        cur = best.get(k)
        # -rank because rank 0 is the most preferred alias, and max() wins.
        if cur is None or ((-cand.rank, cand.filed, abs(cand.value))
                           > (-cur.rank, cur.filed, abs(cur.value))):
            best[k] = cand

    restated = {k: len(seen[(p.tag, *k)]) > 1 for k, p in best.items()}
    return best, restated


def instant(cik: int, logical_concept: str, limit: int = 12) -> list[Fact]:
    """Balance-sheet series: one snapshot per reporting date.

    Nothing is ever derived here — a balance sheet is filed with the 10-K
    too, so unlike the income statement there is no missing Q4 to rebuild.
    """
    unit = config.concept_unit(logical_concept)
    # An instant fact has no start date at all — that is what makes it one.
    rows = [r for r in _alias_rows(cik, logical_concept, unit) if r[2] is None]
    if not rows:
        return []
    best, restated = _dedupe(rows)

    out = [Fact(concept=p.tag, value=p.value, unit=unit, start="", end=p.end,
                form=p.form, accn=p.accn, filed=p.filed,
                restated=restated[k], label=p.end)
           for k, p in best.items()]
    out.sort(key=lambda f: f.end)
    _log(f"[series] {logical_concept}: {len(out)} instants")
    return out[-limit:]


def quarterly(cik: int, logical_concept: str, limit: int = 8) -> list[Fact]:
    """Duration series as comparable quarters, with Q4 reconstructed."""
    if config.concept_kind(logical_concept) == "instant":
        return instant(cik, logical_concept, limit)

    unit = config.concept_unit(logical_concept)
    rows = [r for r in _alias_rows(cik, logical_concept, unit)
            if r[2] is not None]
    if not rows:
        return []
    best, restated = _dedupe(rows)

    quarters: dict[str, Fact] = {}
    annuals: dict[tuple[str, str], Fact] = {}

    for k, p in best.items():
        b = _bucket(p.start, p.end)
        if b is None:
            continue
        f = Fact(concept=p.tag, value=p.value, unit=unit, start=p.start,
                 end=p.end, form=p.form, accn=p.accn, filed=p.filed,
                 restated=restated[k], label=p.end)
        if b == "Q":
            quarters[p.end] = f
        else:
            annuals[(p.start, p.end)] = f

    # Cash-flow and some income-statement items are filed YEAR TO DATE, not
    # per quarter: 3, 6, 9 and 12 month figures that all share one start date.
    # Only the first of those is a discrete quarter, which is why an
    # unprocessed cash-flow series shows one point a year. Differencing
    # consecutive cumulative facts recovers the quarters in between.
    by_start: dict[str, list[Pick]] = {}
    for p in best.values():
        by_start.setdefault(p.start, []).append(p)

    for start, group in by_start.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda p: p.end)
        for prev, cur in zip(group, group[1:], strict=False):
            if cur.end in quarters:
                continue  # a directly filed quarter always wins
            if not 80 <= _days(prev.end, cur.end) <= 100:
                continue  # not a one-quarter step; 6mo->12mo tells us nothing
            quarters[cur.end] = Fact(
                concept=cur.tag, value=cur.value - prev.value, unit=unit,
                start=(dt.date.fromisoformat(prev.end)
                       + dt.timedelta(days=1)).isoformat(),
                end=cur.end, form=cur.form, accn=cur.accn, filed=cur.filed,
                derived=True,
                derivation=(f"cumulative {start}..{cur.end} minus "
                            f"{start}..{prev.end}"),
                restated=restated[(cur.start, cur.end)], label=cur.end)

    # derive Q4 = FY - (the three quarters inside that FY)
    for (fs, fe), fy_fact in annuals.items():
        inside = [q for q in quarters.values() if fs <= q.start and q.end <= fe]
        if len(inside) == 3 and fe not in quarters:
            q4 = fy_fact.value - sum(q.value for q in inside)
            ends = sorted(q.end for q in inside)
            quarters[fe] = Fact(
                concept=fy_fact.concept, value=q4, unit=unit,
                start=(dt.date.fromisoformat(ends[-1])
                       + dt.timedelta(days=1)).isoformat(),
                end=fe, form=fy_fact.form, accn=fy_fact.accn,
                filed=fy_fact.filed, derived=True,
                derivation=f"FY({fs}..{fe}) minus Q1+Q2+Q3",
                restated=fy_fact.restated, label=fe)
            _log(f"[series] derived Q4 ending {fe}: {q4:,.0f}")

    out = sorted(quarters.values(), key=lambda f: f.end)[-limit:]
    _log(f"[series] {logical_concept}: {len(out)} quarters "
         f"({sum(f.derived for f in out)} derived, "
         f"{sum(f.restated for f in out)} restated)")
    return out


def annual(cik: int, logical_concept: str, limit: int = 10) -> list[Fact]:
    """Fiscal-year series.

    For instant concepts this is the latest snapshot in each fiscal year — a
    balance sheet has no annual total to sum.
    """
    if config.concept_kind(logical_concept) == "instant":
        facts = instant(cik, logical_concept, limit * 5)
        by_year: dict[int, Fact] = {}
        for f in facts:
            y = int(f.end[:4])
            if y not in by_year or f.end > by_year[y].end:
                by_year[y] = f
        return [by_year[y] for y in sorted(by_year)][-limit:]

    unit = config.concept_unit(logical_concept)
    rows = [r for r in _alias_rows(cik, logical_concept, unit)
            if r[2] is not None]
    if not rows:
        return []
    best, restated = _dedupe(rows)

    out = [Fact(concept=p.tag, value=p.value, unit=unit, start=p.start,
                end=p.end, form=p.form, accn=p.accn, filed=p.filed,
                restated=restated[k], label=p.end)
           for k, p in best.items() if _bucket(p.start, p.end) == "FY"]
    out.sort(key=lambda f: f.end)
    _log(f"[series] {logical_concept}: {len(out)} fiscal years")
    return out[-limit:]


def get(cik: int, logical_concept: str, period: str = "quarterly",
        limit: int = 8) -> list[Fact]:
    """One entry point for the two period modes the UI offers."""
    if period == "annual":
        return annual(cik, logical_concept, limit)
    return quarterly(cik, logical_concept, limit)
