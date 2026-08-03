"""filingdesk-mcp — the MCP server the agent calls for every figure.

Four tools. The company universe is every SEC registrant, and a company the
cache has never seen is fetched on the first call rather than refused — which
closes the skeleton's largest stated limitation.
"""
try:  # MCP SDK 2.x
    # Deliberately typed as unresolvable: this name does not exist in the 1.x
    # SDK that is installed, which is the whole reason for the fallback below.
    from mcp.server import MCPServer as _Server  # type: ignore[attr-defined]
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

import os

from . import companies, config, metrics, seed, series

# The tools are the layer that reaches SEC EDGAR and the layer that reads the
# filings cache, and they run in THIS subprocess rather than in the caller.
# Synthetic mode therefore has to be re-applied here on both counts: without
# `no_fetch` a "no network" run still calls out, and without `isolate` the
# tools read the real database while the parent seeded a synthetic one, so
# every question comes back with the wrong numbers or none.
if os.environ.get("FD_STUB") == "1":
    from . import stub
    stub.isolate()
    seed.ensure = stub.no_fetch

mcp = _Server("filingdesk")


class CompanyUnavailable(Exception):
    """No CIK for this ticker, or no filings that could be loaded for it.

    It carries the payload the tool should hand back, because the two cases
    call for different next moves from the model: a ticker that is not
    registered gets suggestions, a company whose filings would not load gets
    the reason they would not.

    Raised rather than returned. The old shape was (cik, error) with "exactly
    one of the two is not None" as a comment — a contract the type system
    cannot check and every caller had to remember, which is why every caller
    passed an `int | None` into something that wanted an `int`.
    """

    def __init__(self, payload: dict) -> None:
        super().__init__(payload.get("error", ""))
        self.payload = payload


def _company(ticker: str) -> int:
    """Resolve a ticker to a CIK with its filings on disk, loading if needed.

    Raises CompanyUnavailable when it cannot.
    """
    t = (ticker or "").upper().strip()
    cik = companies.resolve(t)
    if cik is None:
        hits = companies.search(t, 5)
        raise CompanyUnavailable({
            "error": f"{t!r} is not an SEC-registered ticker.",
            "error_kind": "unknown_ticker",
            "did_you_mean": [h["ticker"] for h in hits]})

    if not series.has_any(cik):
        res = seed.ensure(t)
        if not res.get("ok"):
            raise CompanyUnavailable({
                "error": res["error"],
                "error_kind": res.get("error_kind", "no_company_data")})
    return cik


@mcp.tool()
def fd_resolve_company(query: str) -> dict:
    """Find a company by ticker or name. Call this when a question names a
    company you do not have a ticker for.

    Returns candidate tickers with their CIK and registered name.
    """
    hits = companies.search(query, 8)
    if not hits:
        return {"error": f"Nothing matches {query!r}.",
                "error_kind": "unknown_ticker", "matches": []}
    return {"query": query, "matches": hits,
            "universe_size": companies.count()}


@mcp.tool()
def fd_get_concept(ticker: str, concept: str, quarters: int = 8,
                   period: str = "quarterly") -> dict:
    """Time series for one financial concept, with provenance.

    concept: a logical name such as "Revenues", "GrossProfit", "NetIncome",
    "OperatingCashFlow" or "Assets". Call fd_list_concepts to see what this
    company actually reports.
    period: "quarterly" or "annual".

    Every fact carries its accession number, form and filing date, and is
    flagged when derived (a reconstructed Q4) or restated.
    """
    try:
        cik = _company(ticker)
    except CompanyUnavailable as e:
        return e.payload
    facts = series.get(cik, concept, period, quarters)
    if not facts:
        return {"error": f"{ticker.upper()} reports no data for {concept}.",
                "error_kind": "no_concept_data",
                "available": series.available_concepts(cik)}
    return {"ticker": ticker.upper(), "company": companies.name_of(ticker),
            "concept": concept, "label": config.concept_label(concept),
            "period": period, "facts": [f.dict() for f in facts]}


@mcp.tool()
def fd_compute_metric(ticker: str, metric: str, quarters: int = 8,
                      period: str = "quarterly") -> dict:
    """Compute a derived ratio in Python. The model must not do this itself.

    metric: one of gross_margin, operating_margin, net_margin, rnd_intensity,
    sga_intensity, free_cash_flow, fcf_margin, current_ratio, debt_to_equity,
    return_on_equity, effective_tax_rate.

    Returns the value per period plus the formula and every input fact, so
    each ratio can be traced back to the filed figures underneath it.
    """
    try:
        cik = _company(ticker)
    except CompanyUnavailable as e:
        return e.payload
    res = metrics.compute(cik, metric, period, quarters)
    if "error" in res:
        return res
    return {"ticker": ticker.upper(), "company": companies.name_of(ticker),
            "period": period, **res}


@mcp.tool()
def fd_list_concepts(ticker: str) -> dict:
    """List what this company actually reports, and which metrics that allows.

    Call this before giving up. "No tool covers that" and "this company never
    reported that" are different answers and the user deserves to know which.
    """
    try:
        cik = _company(ticker)
    except CompanyUnavailable as e:
        return e.payload
    return {"ticker": ticker.upper(), "company": companies.name_of(ticker),
            "concepts": series.available_concepts(cik),
            "supported_metrics": [m["key"] for m in metrics.available(cik)]}


if __name__ == "__main__":
    mcp.run()
