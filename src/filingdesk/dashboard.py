"""Assembles everything one company view needs, in a single payload.

The dashboard is deliberately one request rather than a dozen. Every panel is
built from the same DuckDB read, so the KPI tiles, the charts and the table
view can never disagree about what a quarter was — which they would, given
that a refresh can land between two requests.
"""
from __future__ import annotations

from . import companies, config, metrics, seed, series

# Charts are capped at three series: the validated categorical palette clears
# every colour-blindness gate at three slots, and past that the honest fix is
# another chart rather than another hue.
MARGIN_KEYS = ("gross_margin", "operating_margin", "net_margin")
CASH_CONCEPTS = ("OperatingCashFlow", "CapitalExpenditures")


def _points(facts) -> list[dict]:
    return [{"end": f.end, "value": f.value, "derived": f.derived,
             "restated": f.restated, "accn": f.accn, "form": f.form,
             "filed": f.filed, "derivation": f.derivation,
             "concept": f.concept}
            for f in facts]


def _delta(points: list[dict], lag: int) -> dict | None:
    """Change against `lag` periods back — 4 for a year-on-year quarter.

    Year-on-year rather than sequential because most filers are seasonal and
    a quarter-on-quarter delta mostly measures the calendar.
    """
    if len(points) <= lag:
        return None
    now, then = points[-1]["value"], points[-lag - 1]["value"]
    if not then:
        return None
    return {"pct": (now - then) / abs(then), "from": points[-lag - 1]["end"],
            "lag": lag}


def _series_block(cik: int, concept: str, period: str, limit: int) -> dict | None:
    facts = series.get(cik, concept, period, limit)
    if not facts:
        return None
    pts = _points(facts)
    return {"key": concept, "label": config.concept_label(concept),
            "unit": config.concept_unit(concept),
            "kind": config.concept_kind(concept),
            "points": pts,
            "delta": _delta(pts, 4 if period == "quarterly" else 1)}


def _metric_block(cik: int, key: str, period: str, limit: int) -> dict | None:
    res = metrics.compute(cik, key, period, limit)
    if "error" in res:
        return None
    pts = [{"end": s["period_end"], "value": s["value"],
            "derived": s["derived_inputs"], "restated": s["restated_inputs"],
            "accn": "computed", "form": "", "filed": "",
            "derivation": s["formula"], "concept": key}
           for s in res["series"]]
    return {"key": key, "label": res["label"], "unit": res["unit"],
            "kind": "ratio", "formula": res["formula"], "points": pts,
            "delta": _delta(pts, 4 if period == "quarterly" else 1)}


def build(ticker: str, period: str = "quarterly", limit: int = 12,
          concept: str = "Revenues") -> dict:
    """The whole company view. Loads the company from SEC if it is new."""
    ticker = (ticker or "").upper().strip()
    cik = companies.resolve(ticker)
    if cik is None:
        return {"ok": False, "error_kind": "unknown_ticker",
                "error": f"{ticker or '(blank)'} is not an SEC-registered ticker.",
                "did_you_mean": companies.search(ticker, 6)}

    load = seed.ensure(ticker)
    if not load.get("ok"):
        return {"ok": False, "error": load["error"],
                "error_kind": load.get("error_kind", "no_company_data"),
                "ticker": ticker, "name": companies.name_of(ticker)}

    limit = max(2, min(int(limit), 40))
    period = "annual" if period == "annual" else "quarterly"

    catalog = series.concept_catalog(cik)
    available = {c["key"] for c in catalog}
    if concept not in available:
        concept = "Revenues" if "Revenues" in available else (
            catalog[0]["key"] if catalog else "")

    primary = _series_block(cik, concept, period, limit) if concept else None
    margins = [b for b in (_metric_block(cik, k, period, limit)
                           for k in MARGIN_KEYS) if b]
    cash = [b for b in (_series_block(cik, c, period, limit)
                        for c in CASH_CONCEPTS) if b]
    fcf = _metric_block(cik, "free_cash_flow", period, limit)
    if fcf:
        cash = [c for c in cash if c["key"] == "OperatingCashFlow"] + [fcf]

    return {
        "ok": True,
        "ticker": ticker,
        # The name on the filings beats the one in the ticker file: a CIK
        # reached by number has no ticker-file title at all.
        "name": load.get("entity") or companies.name_of(ticker),
        "cik": cik,
        "period": period,
        "limit": limit,
        "concept": concept,
        # `cached: False` means this very request pulled from EDGAR. The UI
        # reports that rather than leaving the reader to wonder whether the
        # figures are current — which is what the manual sync button was
        # really compensating for.
        "loaded": {k: load.get(k) for k in
                   ("n_facts", "refreshed", "cached", "stale", "age_hours")},
        "synced_now": load.get("cached") is False,
        "offline": bool(load.get("stale_ok")),
        "concepts": catalog,
        "metrics": metrics.available(cik),
        "kpis": kpis(cik, period),
        "primary": primary,
        "margins": margins,
        "cash": cash,
    }


KPI_SPEC = [
    ("Revenues", "series"),
    ("gross_margin", "metric"),
    ("NetIncome", "series"),
    ("free_cash_flow", "metric"),
]


def kpis(cik: int, period: str = "quarterly") -> list[dict]:
    """Four headline tiles: the latest filed value plus its year-on-year move.

    Each carries a 12-point sparkline, so a tile shows level and direction
    without the reader opening a chart.
    """
    out = []
    for key, kind in KPI_SPEC:
        block = (_series_block(cik, key, period, 12) if kind == "series"
                 else _metric_block(cik, key, period, 12))
        if not block or not block["points"]:
            continue
        last = block["points"][-1]
        out.append({
            "key": key, "label": block["label"], "unit": block["unit"],
            "value": last["value"], "end": last["end"],
            "derived": last["derived"], "delta": block["delta"],
            "spark": [p["value"] for p in block["points"]],
        })
    return out
