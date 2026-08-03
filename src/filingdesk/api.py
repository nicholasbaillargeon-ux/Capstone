"""HTTP service.

Three surfaces over one data layer:

  /            the landing page — what this is, over live counts read from the
               same place /api/health reads them.
  /app         an interactive dashboard — search any listed company, chart any
               concept it reports, read every figure back to its filing.
  /ask         the grounded-answer path, where a language model narrates the
               same facts and a deterministic guard strikes anything it made up.

The split is the same one its two sibling apps in the combined showcase use —
landing at the mount point, dashboard one level under it — so three projects
behind one proxy answer the same URL shape.

The healthcheck is real: it reports data freshness and whether the model is
actually reachable, not just 200.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from html import escape as html_escape
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (
    agent,
    companies,
    config,
    dashboard,
    db,
    history,
    llm,
    logging_setup,
    metrics,
    seed,
    web,
)

logging_setup.configure()
log = logging_setup.log

# Offline demo mode: fabricated facts, faked model. Opt-in and loud about it —
# every page rendered in this mode carries a banner saying so.
STUB = os.environ.get("FD_STUB") == "1"
if STUB:
    from . import stub
    stub.install()

app = FastAPI(title="Filing Desk", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(web.STATIC)), name="static")
STARTED = dt.datetime.now(dt.UTC)

_EXAMPLES = [
    ("NVDA", "How has gross margin moved over the last 8 quarters?"),
    ("AAPL", "What was net income in the most recent quarter?"),
    ("MSFT", "How has operating cash flow moved across the series?"),
]

# hx-vals carries the question straight to /ui/run, so an example is one click
# rather than fill-then-submit.
EXAMPLES = [{"ticker": t, "question": q,
             "vals": json.dumps({"question": q, "ticker": t})}
            for t, q in _EXAMPLES]


class ReportRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    ticker: str = Field(default="NVDA", max_length=8)


def _mtime(path) -> str | None:
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC).isoformat()
    except OSError:
        return None


def _model_online() -> tuple[bool, str]:
    """Whether the configured model actually answers, not whether it is set.

    The dashboard works without a model; only the narrated answer needs one.
    Reporting the difference is the point — a "degraded" that means "no chat"
    is more useful than a green light that lies.
    """
    if STUB:
        return True, "stub"
    return llm.ping()


def _health() -> dict:
    facts = companies_n = None
    try:
        with db.reading() as con:
            facts = con.execute("SELECT count(*) FROM facts").fetchone()[0]
            companies_n = con.execute(
                "SELECT count(*) FROM loaded").fetchone()[0]
    except Exception:  # noqa: BLE001 — health must never raise
        pass
    model_ok, why = _model_online()
    return {
        "status": "ok" if facts else "degraded",
        "ready": bool(facts),
        "started": STARTED.isoformat(),
        "facts_loaded": facts,
        "companies_loaded": companies_n,
        "universe": companies.count(),
        "filings_refreshed": _mtime(config.DUCK),
        "vault_indexed": _mtime(config.VAULT_DB),
        "model": config.CHAT_MODEL,
        "model_label": llm.describe(),
        "endpoint": config.LLM_BASE_URL,
        "model_enabled": config.model_enabled() or STUB,
        "model_online": model_ok,
        "model_detail": why,
        "stub": STUB,
    }


@app.get("/api/health")
def health() -> dict:
    return _health()


# ---- data API ------------------------------------------------------------

@app.get("/api/companies/search")
def company_search(q: str = Query("", max_length=64),
                   limit: int = Query(12, ge=1, le=50)) -> dict:
    """Type-ahead over every SEC registrant, by ticker or company name."""
    return {"query": q, "results": companies.search(q, limit),
            "universe": companies.count()}


@app.get("/api/company/{ticker}")
async def company_view(ticker: str,
                       period: str = "quarterly",
                       limit: int = Query(12, ge=2, le=40),
                       concept: str = "Revenues") -> JSONResponse:
    """Everything one company view needs, in one read.

    Off the event loop: a cold company triggers a live SEC fetch, and blocking
    here would stall every other request on the server.
    """
    payload = await asyncio.to_thread(dashboard.build, ticker, period,
                                      limit, concept)
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 404)


@app.post("/api/company/{ticker}/refresh")
async def company_refresh(ticker: str) -> JSONResponse:
    """Re-pull this company from SEC EDGAR now, ignoring the cache TTL."""
    res = await asyncio.to_thread(seed.ensure, ticker, True)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.get("/api/company/{ticker}/series")
async def company_series(ticker: str, concept: str = "Revenues",
                         period: str = "quarterly",
                         limit: int = Query(12, ge=2, le=40)) -> JSONResponse:
    from . import series as series_mod
    cik = companies.resolve(ticker)
    if cik is None:
        return JSONResponse({"error": f"Unknown ticker {ticker!r}."},
                            status_code=404)
    await asyncio.to_thread(seed.ensure, ticker)
    facts = await asyncio.to_thread(series_mod.get, cik, concept, period, limit)
    return JSONResponse({
        "ticker": ticker.upper(), "concept": concept, "period": period,
        "label": config.concept_label(concept),
        "facts": [f.dict() for f in facts]})


@app.get("/api/company/{ticker}/metric")
async def company_metric(ticker: str, metric: str = "gross_margin",
                         period: str = "quarterly",
                         limit: int = Query(12, ge=2, le=40)) -> JSONResponse:
    cik = companies.resolve(ticker)
    if cik is None:
        return JSONResponse({"error": f"Unknown ticker {ticker!r}."},
                            status_code=404)
    await asyncio.to_thread(seed.ensure, ticker)
    res = await asyncio.to_thread(metrics.compute, cik, metric, period, limit)
    return JSONResponse(res, status_code=200 if "error" not in res else 400)


@app.get("/api/loaded")
def loaded() -> dict:
    return {"companies": seed.loaded_companies()}


@app.post("/api/report")
async def report(req: ReportRequest) -> dict:
    return await agent.run(req.question, req.ticker.upper())


@app.get("/api/history")
def recent(n: int = 20) -> dict:
    return {"reports": history.recent(n)}


# ---- UI ------------------------------------------------------------------

@app.get("/landing", include_in_schema=False)
def landing_moved() -> RedirectResponse:
    """The landing page briefly lived here before it took the root."""
    return RedirectResponse(f"{config.BASE_PATH}/", status_code=301)


@app.get("/", response_class=HTMLResponse)
def landing() -> HTMLResponse:
    """The front door.

    Its numbers are read from the same place /api/health reads them, not
    written into the page. A landing page that advertises a fact count the
    instance behind it doesn't have would be the first unchecked claim in a
    project whose whole argument is that every figure is checkable.
    """
    h = _health()
    facts, universe = h["facts_loaded"] or 0, h["universe"]
    loaded_n = h["companies_loaded"] or 0
    stats = [
        {"label": "XBRL FACTS", "value": f"{facts:,}",
         "note": "indexed and queryable"},
        {"label": "SEC FILERS", "value": f"{universe:,}",
         "note": "searchable by ticker or name"},
        {"label": "COMPANIES CACHED", "value": f"{loaded_n:,}",
         "note": "pulled on first view, kept fresh"},
        {"label": "PERIODS", "value": "8–Max", "note": "quarterly or annual"},
    ]
    stack = ["Python", "XBRL company facts", "SEC EDGAR"]
    if h["model_enabled"]:
        endpoint = ("LiteLLM" if config.LLM_BASE_URL in config.KNOWN_PROXIES
                    else config.LLM_BASE_URL.split("//")[-1].split("/")[0])
        stack += [config.CHAT_MODEL, endpoint]
    return HTMLResponse(web.render(
        "landing.html", stub=STUB, base=config.BASE_PATH,
        showcase_url=config.SHOWCASE_URL,
        app_url=f"{config.BASE_PATH}/app", featured=config.FEATURED,
        ready=h["ready"], facts=facts, universe=universe,
        model_enabled=h["model_enabled"], stats=stats, stack=stack))


@app.get("/app", response_class=HTMLResponse)
def index(ticker: str = "NVDA") -> HTMLResponse:
    """The dashboard. All rendering below the shell is client-side, against
    /api/company/{ticker} — the charts are interactive, so the data has to
    reach the browser regardless."""
    return HTMLResponse(web.render(
        "dashboard.html", stub=STUB, base=config.BASE_PATH, ticker=ticker.upper(),
        model_enabled=config.model_enabled() or STUB,
        featured=config.FEATURED, universe=companies.count()))


def as_ticker(raw: str) -> str:
    """What the company field submitted -> a ticker.

    The field is a type-ahead now, and picking a suggestion writes the symbol
    back into it. Nothing forces a pick, though: "berkshire" and Enter is a
    reasonable thing to do, and so is typing a name with JavaScript off. Both
    used to reach the agent verbatim and come back "that symbol is not in the
    SEC's registrant list", which is true and useless.

    A symbol that resolves is taken as-is. Anything else goes through the same
    ranked search the dropdown uses and takes its top hit — the row the user
    would have clicked. If that finds nothing, the input is returned unchanged
    so the refusal still names what was actually asked for.
    """
    t = (raw or "").strip().upper()
    if not t or companies.resolve(t):
        return t
    hits = companies.search(t, 1)
    return hits[0]["ticker"] if hits else t


def _page(question: str = "", ticker: str = "NVDA",
          result: dict | None = None) -> str:
    return web.render(
        "index.html",
        stub=STUB,
        base=config.BASE_PATH,
        model_enabled=config.model_enabled() or STUB,
        universe=companies.count(),
        examples=EXAMPLES,
        question=question,
        ticker=ticker,
        result=result,
        report_fragment=web.Markup(web.report_html(result)) if result else "",
    )


@app.get("/ask", response_class=HTMLResponse)
def ask(ticker: str = "NVDA") -> HTMLResponse:
    return HTMLResponse(_page(ticker=ticker.upper()))


@app.get("/api/config")
def ui_config() -> dict:
    """What the browser needs to know about how this instance is set up."""
    return {"model_enabled": config.model_enabled() or STUB,
            "endpoint": config.LLM_BASE_URL,
            "universe": companies.count()}


@app.post("/report", response_class=HTMLResponse)
async def report_form(question: str = Form(...), ticker: str = Form("NVDA")):
    """No-JavaScript path: run it, render the finished page. No streaming."""
    t = as_ticker(ticker)
    result = await agent.run(question.strip(), t)
    return HTMLResponse(_page(question=question, ticker=t, result=result))


@app.get("/ui/run", response_class=HTMLResponse)
def ui_run(question: str, ticker: str = "NVDA") -> HTMLResponse:
    """Hand htmx the SSE shell. The stream itself does the work."""
    q, t = question.strip(), as_ticker(ticker)
    url = (f"{config.BASE_PATH}/api/report/stream?"
           + urlencode({"question": q, "ticker": t}))
    return HTMLResponse(web.render(
        "_run.html",
        stream_url=url,
        rail=web.Markup(web.render_rail(web.new_state(), 0.0)),
    ))


@app.get("/api/report/stream")
async def report_stream(question: str, ticker: str = "NVDA"):
    """Same request as POST /api/report, with each stage pushed as it lands."""
    return StreamingResponse(
        web.stream(question.strip(), ticker.strip().upper()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Caddy and nginx both buffer proxied responses by default, which
            # would hold every stage event until the request finished and
            # defeat the point of streaming them.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health/html", response_class=HTMLResponse)
def health_html() -> HTMLResponse:
    h = _health()
    cls = "chip-ok" if h["ready"] else "chip-warn"
    state = "ready" if h["ready"] else "no data"
    bits = [f'<span class="chip {cls}">{state}</span>',
            f'<span class="chip">{h["facts_loaded"] or 0:,} facts</span>',
            f'<span class="chip">{h["universe"]:,} tickers</span>']
    # A working model gets no chip. It named itself in green on every page,
    # which read as a status worth watching when it is only the answer to a
    # question nobody asked — the two states worth surfacing are "no model,
    # by choice" and "a model that should be there is not".
    label = html_escape(h["model_label"])
    if not h["model_enabled"]:
        # Deliberately no model. Styling this as a warning would report a
        # decision as a fault — the dashboard is complete without one.
        bits.append('<span class="chip" title="Every figure here is retrieved '
                    'and every ratio computed in Python. No model is involved '
                    'in this view.">dashboard only · no model</span>')
    elif not h["model_online"]:
        bits.append(f'<span class="chip chip-warn" title="Charts and figures '
                    f'work without it; only the written answer needs a model. '
                    f'({html_escape(h["model_detail"])})">'
                    f'model offline — {label}</span>')
    return HTMLResponse("".join(bits))


@app.get("/api/history/html", response_class=HTMLResponse)
def history_html() -> HTMLResponse:
    return HTMLResponse(web.history_html())
