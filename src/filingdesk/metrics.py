"""Derived metrics, computed in Python — never by the model.

The rule from the spec: the model may quote a filed figure, but any ratio it
reports must have been calculated here. That is what lets the grounding guard
work at all. A ratio the model computed itself is indistinguishable from one
it invented.

Every metric declares its inputs as logical concepts, so a metric is available
for a company exactly when its inputs are, and the UI can say which are
missing rather than returning an empty chart.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import series


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    inputs: tuple[str, ...]
    formula: str
    unit: str                      # "ratio" renders as %, "USD" as currency
    fn: Callable[..., float | None]
    note: str = ""


def _safe_div(a: float, b: float) -> float | None:
    return None if not b else a / b


REGISTRY: dict[str, Metric] = {}


def _register(m: Metric) -> Metric:
    REGISTRY[m.key] = m
    return m


_register(Metric(
    "gross_margin", "Gross margin", ("GrossProfit", "Revenues"),
    "GrossProfit / Revenues", "ratio",
    lambda gp, rev: _safe_div(gp, rev)))

_register(Metric(
    "operating_margin", "Operating margin", ("OperatingIncome", "Revenues"),
    "OperatingIncome / Revenues", "ratio",
    lambda oi, rev: _safe_div(oi, rev)))

_register(Metric(
    "net_margin", "Net margin", ("NetIncome", "Revenues"),
    "NetIncome / Revenues", "ratio",
    lambda ni, rev: _safe_div(ni, rev)))

_register(Metric(
    "rnd_intensity", "R&D intensity", ("ResearchAndDevelopment", "Revenues"),
    "ResearchAndDevelopment / Revenues", "ratio",
    lambda rd, rev: _safe_div(rd, rev)))

_register(Metric(
    "sga_intensity", "SG&A intensity",
    ("SellingGeneralAndAdministrative", "Revenues"),
    "SellingGeneralAndAdministrative / Revenues", "ratio",
    lambda sga, rev: _safe_div(sga, rev)))

_register(Metric(
    "free_cash_flow", "Free cash flow",
    ("OperatingCashFlow", "CapitalExpenditures"),
    "OperatingCashFlow - CapitalExpenditures", "USD",
    lambda ocf, capex: ocf - abs(capex),
    note="Capex is filed as a positive outflow, so it is subtracted by "
         "absolute value rather than added."))

_register(Metric(
    "fcf_margin", "FCF margin",
    ("OperatingCashFlow", "CapitalExpenditures", "Revenues"),
    "(OperatingCashFlow - CapitalExpenditures) / Revenues", "ratio",
    lambda ocf, capex, rev: _safe_div(ocf - abs(capex), rev)))

_register(Metric(
    "current_ratio", "Current ratio", ("CurrentAssets", "CurrentLiabilities"),
    "CurrentAssets / CurrentLiabilities", "x",
    lambda ca, cl: _safe_div(ca, cl)))

_register(Metric(
    "debt_to_equity", "Debt to equity", ("Liabilities", "StockholdersEquity"),
    "Liabilities / StockholdersEquity", "x",
    lambda li, eq: _safe_div(li, eq)))

_register(Metric(
    "return_on_equity", "Return on equity", ("NetIncome", "StockholdersEquity"),
    "NetIncome / StockholdersEquity", "ratio",
    lambda ni, eq: _safe_div(ni, eq),
    note="Period net income over period-end equity, not average equity."))

_register(Metric(
    "effective_tax_rate", "Effective tax rate",
    ("IncomeTaxExpense", "OperatingIncome"),
    "IncomeTaxExpense / OperatingIncome", "ratio",
    lambda tax, oi: _safe_div(tax, oi)))


def known() -> list[str]:
    return list(REGISTRY)


def compute(cik: int, key: str, period: str = "quarterly",
            limit: int = 8) -> dict:
    """One metric as a time series, with the input facts that produced it.

    Returns the inputs as well as the outputs because provenance is the whole
    point: a computed ratio is only trustworthy if you can see the two filed
    figures underneath it.
    """
    m = REGISTRY.get(key)
    if m is None:
        return {"error": f"No metric named {key!r}.",
                "error_kind": "no_such_metric", "known": known()}

    # Instant inputs (balance sheet) are pulled with a longer window so a
    # quarterly ratio mixing the two still lines up on period end dates.
    by_concept: dict[str, dict[str, series.Fact]] = {}
    missing = []
    for concept in m.inputs:
        facts = series.get(cik, concept, period, limit + 4)
        if not facts:
            missing.append(concept)
        by_concept[concept] = {f.end: f for f in facts}

    if missing:
        return {"error": f"{m.label} needs {', '.join(missing)}, which this "
                         "company does not report under a recognised tag.",
                "error_kind": "no_concept_data", "missing": missing}

    common = set.intersection(*(set(d) for d in by_concept.values()))
    out, inputs, seen_accn = [], [], set()
    for end in sorted(common)[-limit:]:
        picked = [by_concept[c][end] for c in m.inputs]
        value = m.fn(*[f.value for f in picked])
        if value is None:
            continue
        for f in picked:
            k = (f.concept, f.end, f.accn)
            if k not in seen_accn:
                seen_accn.add(k)
                inputs.append(f.dict())
        out.append({
            "period_end": end,
            "value": value,
            "formula": m.formula,
            "derived_inputs": any(f.derived for f in picked),
            "restated_inputs": any(f.restated for f in picked),
        })

    if not out:
        return {"error": f"No period has all of {', '.join(m.inputs)} filed.",
                "error_kind": "no_concept_data"}

    return {"metric": m.key, "label": m.label, "unit": m.unit,
            "formula": m.formula, "note": m.note,
            "series": out, "input_facts": inputs}


def available(cik: int) -> list[dict]:
    """Which metrics can actually be computed for this company."""
    got = set(series.available_concepts(cik))
    return [{"key": m.key, "label": m.label, "unit": m.unit,
             "formula": m.formula}
            for m in REGISTRY.values() if set(m.inputs) <= got]
