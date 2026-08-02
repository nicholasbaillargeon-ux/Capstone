"""The web UI: server-rendered fragments pushed over SSE.

Three things here are deliberate.

**Rendering happens on the server, including during the request.** The stream
emits HTML, not JSON, because htmx's SSE extension swaps an event's data
straight into the DOM. The browser holds no state about which stage is running
— it is told, each time.

**The report is escaped before anything else touches it.** It is markdown
produced by a language model; treating it as markup would mean shipping model
output as HTML and hoping.

**The stream closes on its own event.** `done` carries the report and `close`
is a separate sentinel, because the browser reconnects to an SSE endpoint that
simply ends — which would silently re-run the whole request. Keeping the two
apart also avoids racing the swap against the close on one event name.

**The stream heartbeats.** Criterion 6 allows 90 seconds on CPU but requires
the UI not be silent, and a single draft call can hold the pipeline for most of
that. A re-render every two seconds keeps the elapsed clock honest and keeps
idle proxies from dropping the connection mid-generation.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import re
import time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from . import agent, history

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

CITE = re.compile(r"\[\[fact:(\d+)\]\]")
HEARTBEAT = 2.0

# Ordered: the rail renders in pipeline order, not arrival order.
STAGES = [
    ("tools", "Retrieving facts", "MCP tool calls against the filings cache"),
    ("retrieve", "Reading notes", "vector search over the vault"),
    ("draft", "Drafting", "one generation pass on CPU"),
    ("guard", "Checking every figure", "each numeral traced back to a fact"),
]

_CLASSES = {"pending": "", "active": "is-active",
            "done": "is-done", "error": "is-error"}


def render(template: str, **ctx) -> str:
    return env.get_template(template).render(**ctx)


# ---- report rendering ----------------------------------------------------

def fmt_value(v: float) -> str:
    """Mirrors agent.fact_table so the UI and the model see the same string."""
    return f"{v:.4f}" if abs(v) < 100 else f"{v:,.0f}"


def fact_rows(facts: list[dict]) -> list[dict]:
    rows = []
    for i, f in enumerate(facts, 1):
        flags = [n for n, on in (("derived", f.get("derived")),
                                 ("restated", f.get("restated"))) if on]
        rows.append({
            "id": i,
            "concept": f.get("concept", ""),
            "value": fmt_value(f["value"]),
            "end": f.get("end", ""),
            "form": f.get("form") or "—",
            "accn": f.get("accn", ""),
            "computed": f.get("accn") == "computed",
            "formula": f.get("formula") or "",
            "flags": flags,
        })
    return rows


def _strike(fragment: str, claims: list[str]) -> str:
    """Mark unsupported figures in place. Spec: struck and flagged, not dropped."""
    for claim in claims:
        esc = html.escape(claim)
        if esc and esc in fragment:
            fragment = fragment.replace(
                esc, f'<del class="struck" title="unsupported figure">{esc}</del>')
    return fragment


def render_report(report_md: str, problems: list[dict] | None = None) -> Markup:
    """Model markdown -> safe HTML, with citation markers as traceable chips."""
    claims = sorted({p["claim"] for p in (problems or [])}, key=len, reverse=True)
    out = []
    for para in re.split(r"\n\s*\n", (report_md or "").strip()):
        if not para.strip():
            continue
        # Split on the markers first so striking never rewrites anchor markup.
        pieces = CITE.split(html.escape(para))
        buf = []
        for idx, piece in enumerate(pieces):
            if idx % 2:  # capture group: the fact id
                buf.append(
                    f'<a class="cite" href="#fact-{piece}" data-fact="{piece}">'
                    f'fact {piece}</a>')
            else:
                buf.append(_strike(piece, claims))
        out.append("<p>" + "".join(buf).replace("\n", "<br>") + "</p>")
    return Markup("".join(out))


def report_html(result: dict) -> str:
    return render(
        "_report.html",
        r=result,
        rows=fact_rows(result.get("facts") or []),
        report=render_report(result.get("report_md") or "",
                             result.get("unsupported_claims")),
    )


# ---- history -------------------------------------------------------------

def history_items(n: int = 8) -> list[dict]:
    items = []
    for h in reversed(history.recent(n)):
        if h.get("refused"):
            verdict, cls = "refused", "refused"
        elif h.get("unsupported_claims"):
            verdict, cls = "flagged", "flagged"
        else:
            verdict, cls = "grounded", "ok"
        items.append({
            "verdict": verdict,
            "cls": cls,
            "question": h.get("question") or "(question not recorded)",
            "facts": len(h.get("facts") or []),
            "ms": (h.get("latency_ms") or {}).get("total", 0),
            "trace_id": h.get("trace_id") or "",
        })
    return items


def history_html() -> str:
    return render("_history.html", items=history_items())


def history_oob() -> str:
    """Rides along with the finished report so the panel refreshes itself."""
    return (f'<div id="history" hx-swap-oob="innerHTML">{history_html()}</div>')


# ---- progress rail -------------------------------------------------------

def new_state() -> dict[str, dict]:
    return {key: {"status": "pending", "detail": ""} for key, _, _ in STAGES}


def apply_stage(state: dict[str, dict], ev: dict) -> None:
    stage, status = ev.get("stage", ""), ev.get("status", "")

    # A repair is a second pass through the guard, not a stage of its own.
    if stage == "repair":
        guard = state["guard"]
        if status == "start":
            n = len(ev.get("claims") or [])
            guard["status"] = "active"
            guard["detail"] = f"{n} to repair" if n else "repairing"
        return

    if stage not in state:
        return

    step = state[stage]
    if status == "start":
        step["status"] = "active"
    elif status == "done":
        step["status"] = "done"
        bits = []
        if isinstance(ev.get("ms"), int):
            bits.append(f"{ev['ms']} ms")
        if isinstance(ev.get("facts"), int):
            bits.append(f"{ev['facts']} facts")
        if isinstance(ev.get("k"), int):
            bits.append(f"{ev['k']} passages")
        if stage == "guard":
            unsupported = ev.get("unsupported", 0)
            bits.append("all figures traced" if not unsupported
                        else f"{unsupported} struck")
        step["detail"] = " · ".join(bits)
    elif status == "error":
        step["status"] = "error"
        step["detail"] = "failed"


def render_rail(state: dict[str, dict], elapsed: float,
                heading: str = "Working") -> str:
    steps = [{"title": title, "note": note,
              "detail": state[key]["detail"],
              "cls": _CLASSES[state[key]["status"]]}
             for key, title, note in STAGES]
    return render("_rail.html", steps=steps, heading=heading,
                  elapsed=f"{elapsed:.1f}s")


# ---- the stream ----------------------------------------------------------

def sse(event: str, payload: str) -> str:
    """One SSE frame. HTML is multi-line, so every line needs its own field."""
    body = "".join(f"data: {line}\n" for line in payload.split("\n"))
    return f"event: {event}\n{body}\n"


async def stream(question: str, ticker: str):
    """Run one request, re-rendering the rail as each stage lands."""
    queue: asyncio.Queue = asyncio.Queue()
    state = new_state()
    t0 = time.monotonic()

    def on_stage(stage: str, status: str, **extra) -> None:
        queue.put_nowait({"stage": stage, "status": status, **extra})

    async def runner() -> None:
        try:
            res = await agent.run(question, ticker, on_stage=on_stage)
            await queue.put({"_done": res})
        except Exception as exc:  # noqa: BLE001 — a crash must reach the page
            await queue.put({"_error": repr(exc)})

    task = asyncio.create_task(runner())
    yield sse("stage", render_rail(state, 0.0))

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT)
            except TimeoutError:
                yield sse("stage", render_rail(state, time.monotonic() - t0))
                continue

            if "_done" in item:
                for step in state.values():
                    if step["status"] == "active":
                        step["status"] = "done"
                yield sse("stage", render_rail(state, time.monotonic() - t0,
                                               heading="Done"))
                yield sse("done", report_html(item["_done"]) + history_oob())
                yield sse("close", "")
                return

            if "_error" in item:
                yield sse("done", render("_error.html",
                                         message=item["_error"]))
                yield sse("close", "")
                return

            apply_stage(state, item)
            yield sse("stage", render_rail(state, time.monotonic() - t0))
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
