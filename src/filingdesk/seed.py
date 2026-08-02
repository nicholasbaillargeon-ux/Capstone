"""Pull a company's XBRL facts from SEC EDGAR into DuckDB.

Uses the per-company companyfacts API rather than the 1GB bulk zip: one
request per company, and it lets a company be loaded on demand the first time
somebody asks about it instead of maintaining a watchlist.

Concurrency note: DuckDB takes an exclusive write lock on the database file.
Readers open read-only connections, so seeding is serialised behind one
in-process lock and every writer opens, writes, and closes immediately.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import os
import sys
import tempfile
import threading
import time

import duckdb
import requests

from . import companies, config, db

RATE_LOCK = threading.Lock()
WRITE_LOCK = threading.RLock()
_last_request = 0.0

SCHEMA = db.SCHEMA


def _throttle(min_interval: float = 0.12) -> None:
    """SEC allows 10 req/s. Stay under it globally, not per caller."""
    global _last_request
    with RATE_LOCK:
        wait = min_interval - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def ensure_schema() -> None:
    """Create the tables if this is a cold start, so callers do not have to
    special-case a database that does not exist yet."""
    db.ensure_schema()


def fetch_companyfacts(cik: int) -> dict:
    if not config.SEC_UA:
        raise RuntimeError(
            "SEC_USER_AGENT not set. Example:\n"
            "  export SEC_USER_AGENT='FilingDesk/0.2 (you@example.com)'")
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    _throttle()
    r = requests.get(url, headers={"User-Agent": config.SEC_UA}, timeout=60)
    if r.status_code == 404:
        raise LookupError(f"SEC publishes no XBRL company facts for CIK {cik}")
    r.raise_for_status()
    return r.json()


def flatten(facts: dict) -> list[tuple]:
    """companyfacts JSON -> flat rows.

    Both us-gaap and dei taxonomies, and every unit in config.UNITS rather
    than USD alone — per-share figures live in "USD/shares" and the skeleton
    was dropping all of them.
    """
    rows: list[tuple] = []
    cik = int(facts["cik"])
    entity = facts.get("entityName", "")
    taxonomies = facts.get("facts", {}) or {}
    for taxonomy in ("us-gaap", "dei"):
        for concept, body in (taxonomies.get(taxonomy) or {}).items():
            for unit, items in (body.get("units") or {}).items():
                if unit not in config.UNITS:
                    continue
                for it in items:
                    val, end = it.get("val"), it.get("end")
                    if val is None or end is None:
                        continue
                    rows.append((
                        cik, entity, concept, unit,
                        it.get("start"), end, float(val),
                        it.get("fy"), it.get("fp"), it.get("form"),
                        it.get("accn"), it.get("filed"), it.get("frame"),
                    ))
    return rows


COLUMNS = ("cik", "entity", "concept", "unit", "start", "end", "val",
           "fy", "fp", "form", "accn", "filed", "frame")

CSV_TYPES = ("{'cik':'BIGINT','entity':'VARCHAR','concept':'VARCHAR',"
             "'unit':'VARCHAR','start':'DATE','end':'DATE','val':'DOUBLE',"
             "'fy':'INTEGER','fp':'VARCHAR','form':'VARCHAR',"
             "'accn':'VARCHAR','filed':'DATE','frame':'VARCHAR'}")

# `start` and `end` are reserved words in SQL, so every column is quoted.
SELECT_LIST = ", ".join(f'"{c}"' for c in COLUMNS)


def load(rows: list[tuple], ticker: str = "") -> int:
    """Replace this company's facts.

    Via a temp CSV and DuckDB's native reader rather than `executemany`. A
    big filer is ~150k rows; parameter-binding them one at a time took minutes
    and made first-load of a new company unusable, while the CSV round-trip
    does the same work in about a second.
    """
    if not rows:
        return 0
    cik, entity = rows[0][0], rows[0][1]
    with WRITE_LOCK, db.writing() as con:
        tmp = None
        try:
            con.execute("DELETE FROM facts WHERE cik = ?", [cik])
            with tempfile.NamedTemporaryFile(
                    "w", suffix=".csv", delete=False, newline="",
                    encoding="utf-8") as fh:
                tmp = fh.name
                w = csv.writer(fh)
                w.writerow(COLUMNS)
                w.writerows(rows)
            con.execute(
                f"INSERT INTO facts SELECT {SELECT_LIST} "
                f"FROM read_csv(?, header=true, columns={CSV_TYPES}, "
                "nullstr='', dateformat='%Y-%m-%d')", [tmp])
            n = con.execute("SELECT count(*) FROM facts WHERE cik = ?",
                            [cik]).fetchone()[0]
            con.execute("DELETE FROM loaded WHERE cik = ?", [cik])
            con.execute("INSERT INTO loaded VALUES (?,?,?,?,?)",
                        [cik, ticker.upper(), entity, n,
                         dt.datetime.now(dt.UTC).replace(tzinfo=None)])
        finally:
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
    return int(n)


def status(cik: int) -> dict | None:
    """What is on disk for this company, if anything."""
    try:
        with db.reading() as con:
            row = con.execute(
                "SELECT ticker, entity, n_facts, refreshed FROM loaded "
                "WHERE cik = ?", [cik]).fetchone()
    except duckdb.Error:
        return None
    if not row:
        return None
    ticker, entity, n, refreshed = row
    age_h = None
    if refreshed:
        age_h = ((dt.datetime.now(dt.UTC).replace(tzinfo=None) - refreshed)
                 .total_seconds() / 3600)
    return {"ticker": ticker, "entity": entity, "n_facts": int(n or 0),
            "refreshed": refreshed.isoformat() if refreshed else None,
            "age_hours": round(age_h, 2) if age_h is not None else None,
            "stale": age_h is None or age_h > config.FILINGS_TTL_HOURS}


def ensure(ticker: str, force: bool = False) -> dict:
    """Make sure this company's filings are cached, fetching them if not.

    This is what lets the app cover all ~10,400 registrants without a
    watchlist: the first question about a company loads it.
    """
    ticker = ticker.upper().strip()
    cik = companies.resolve(ticker)
    if cik is None:
        return {"ok": False, "error_kind": "unknown_ticker",
                "error": f"{ticker} is not an SEC-registered ticker."}

    have = status(cik)
    if have and have["n_facts"] and not force and not have["stale"]:
        return {"ok": True, "cached": True, "cik": cik, **have}

    try:
        payload = fetch_companyfacts(cik)
        rows = flatten(payload)
    except LookupError as e:
        # Real and common: trusts, funds and some foreign filers file no XBRL.
        if have and have["n_facts"]:
            return {"ok": True, "cached": True, "stale_ok": True,
                    "cik": cik, **have}
        return {"ok": False, "error": str(e), "error_kind": "no_xbrl_data"}
    except Exception as e:  # noqa: BLE001 — being offline must not lose cache
        if have and have["n_facts"]:
            return {"ok": True, "cached": True, "stale_ok": True,
                    "cik": cik, **have}
        return {"ok": False, "error": f"SEC fetch failed: {e}",
                "error_kind": "fetch_failed"}

    if not rows:
        # Reached with real, current tickers. After a holding-company
        # reorganisation the SEC's ticker file points at the NEW registrant,
        # which has filed nothing yet, while the operating history stays
        # under the predecessor's CIK — and that CIK usually carries no
        # ticker, so it is only reachable by number. Say so, rather than
        # implying the company does not report.
        entity = str(payload.get("entityName", "")).strip()
        return {"ok": False, "error_kind": "no_xbrl_data", "cik": cik,
                "error": (
                    f"SEC lists {ticker} against CIK {cik:010d}"
                    f"{' (' + entity + ')' if entity else ''}, which publishes "
                    "no us-gaap XBRL facts. That is usually a newly formed "
                    "holding company or successor registrant — the operating "
                    "company's history stays under a different CIK, which you "
                    "can load by typing that number into the search box.")}

    n = load(rows, ticker)
    return {"ok": True, "cached": False, "cik": cik, "ticker": ticker,
            "n_facts": n, "entity": rows[0][1],
            "refreshed": dt.datetime.now(dt.UTC).isoformat(), "stale": False}


def loaded_companies() -> list[dict]:
    try:
        with db.reading() as con:
            rows = con.execute(
                "SELECT ticker, entity, cik, n_facts, refreshed FROM loaded "
                "ORDER BY refreshed DESC").fetchall()
    except duckdb.Error:
        return []
    return [{"ticker": t, "entity": e, "cik": int(c), "n_facts": int(n or 0),
             "refreshed": r.isoformat() if r else None}
            for t, e, c, n, r in rows]


def main(*tickers: str) -> None:
    for t in tickers or ("NVDA",):
        res = ensure(t, force=True)
        if res.get("ok"):
            print(f"[seed] {t.upper()}: {res['n_facts']:,} facts "
                  f"· {res.get('entity', '')}")
        else:
            print(f"[seed] {t.upper()}: {res['error']}", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
