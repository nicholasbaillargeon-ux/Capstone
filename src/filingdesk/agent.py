"""The agent loop. Retrieval + generation + hardened tool-calling."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import textwrap
import time
from pathlib import Path

import requests
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import (
    companies,
    config,
    guard,
    history,
    llm,
    logging_setup,
    policy,
    prompts,
    provenance,
    toolcall,
    vault,
)

log = logging_setup.log

MAX_TOOL_STEPS = 4
MAX_REJECTIONS = 3

# The MCP SDK does NOT hand the parent environment to the server subprocess —
# it starts from a minimal safe set. Found by the smoke test: in the container
# FD_ROOT=/data never arrived, so the server opened an empty database at a
# default path and every question came back with zero facts, quietly.
# Anything the server needs must be passed explicitly.
# FD_STUB rides along because the tools — not the parent — are what would
# reach SEC EDGAR. Without it, a "no network" synthetic run still fetches
# live the moment a question names a company the fixtures do not cover.
PASS_ENV = ("PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL",
            "FD_ROOT", "FD_VAULT_DIR", "SEC_USER_AGENT", "TMPDIR", "FD_STUB")


class AgentError(RuntimeError):
    """Something structural failed — not a bad answer, a broken pipeline."""


class ModelUnavailable(AgentError):
    """The chat model could not be reached. Distinct from a data failure:
    the figures are fine, only the narration is impossible."""


NO_MODEL_MSG = (
    "The language model could not be reached, so no written answer was "
    "produced. This does not affect the figures — the dashboard charts every "
    "filed number without a model. Start Ollama to enable narrated answers.")


def mcp_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in PASS_ENV}
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Derive the import path from where this package actually lives rather than
    # hoping the caller exported PYTHONPATH. Without this the server subprocess
    # dies with ModuleNotFoundError depending on how the parent was launched,
    # which is a miserable thing to debug from a "Connection closed" message.
    pkg_parent = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH", "")
    parts = [pkg_parent] + [p for p in existing.split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return env


REFUSALS: dict[str, tuple[str, str]] = {
    "no_company_data": (
        "no_company_data",
        "No filings are loaded for that company. Load them with "
        "`filing seed <TICKER>` and try again."),
    "no_concept_data": (
        "no_concept_data",
        "That company has filings loaded, but nothing for the figure you asked "
        "about. Ask what is available and try one of those."),
    "no_such_metric": (
        "no_such_metric",
        "That metric is not supported yet. Gross margin is the only derived "
        "metric currently implemented."),
    "tool_calls_rejected": (
        "tool_calls_rejected",
        "The model could not produce a valid data request for that question. "
        "Rephrasing it more directly usually works."),
    "unknown_ticker": (
        "unknown_ticker",
        "That symbol is not in the SEC's registrant list, so there are no "
        "filings to read. Check the ticker, or search by company name."),
    "no_xbrl_data": (
        "no_xbrl_data",
        "That company is SEC-registered but files no XBRL company facts — "
        "common for trusts, funds and some foreign private issuers. There is "
        "nothing structured to retrieve."),
    "fetch_failed": (
        "fetch_failed",
        "SEC EDGAR could not be reached to load that company, and nothing is "
        "cached locally for it. No answer was attempted."),
    "no_facts": (
        "no_facts",
        "No filed figures could be retrieved for that question, so no answer "
        "was attempted rather than one being guessed."),
}


def _refuse(trace_id: str, kind: str, message: str, timing: dict,
            question: str = "", ticker: str = "",
            facts: list[dict] | None = None) -> dict:
    """A refusal is a result. It carries the question so the page and the
    history panel can show what was asked, and any facts already retrieved —
    losing them because the narration failed would throw away good data."""
    facts = facts or []
    result = {"trace_id": trace_id, "report_md": None,
              "question": question, "ticker": ticker.upper(),
              "facts": facts, "fact_table": fact_table(facts) if facts else "",
              "passages": [], "unsupported_claims": [],
              "refused": message, "refusal_kind": kind, "latency_ms": timing}
    history.append(result)
    logging_setup.clear()
    return result


def _flatten(exc: BaseException) -> list[BaseException]:
    """anyio task groups nest exceptions. Surface the real cause."""
    if isinstance(exc, BaseExceptionGroup):
        return [x for sub in exc.exceptions for x in _flatten(sub)]
    return [exc]


def harvest(payload: dict, facts: list[dict]) -> int:
    before = len(facts)
    for key in ("facts", "input_facts"):
        for f in payload.get(key, []):
            if not any(x["accn"] == f["accn"] and x["end"] == f["end"]
                       and x["concept"] == f["concept"] for x in facts):
                facts.append(dict(f))
    for s in payload.get("series", []):
        facts.append({"concept": payload.get("metric", "metric"),
                      "value": s["value"], "end": s["period_end"],
                      "accn": "computed", "formula": s.get("formula", ""),
                      "derived": True})
    return len(facts) - before


def fact_table(facts: list[dict]) -> str:
    lines = []
    for i, f in enumerate(facts, 1):
        v = f["value"]
        shown = f"{v:.4f}" if abs(v) < 100 else f"{v:,.0f}"
        tags = [t for t, on in (("derived", f.get("derived")),
                                ("restated", f.get("restated"))) if on]
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        lines.append(f"[[fact:{i}]] {f['concept']} = {shown} "
                     f"(period ending {f['end']}, {f.get('accn')}){suffix}")
    return "\n".join(lines)


TICKER_TOKEN = re.compile(r"\b[A-Z][A-Z0-9.\-]{1,5}\b")


def expected_tickers(question: str, ticker: str) -> set[str]:
    """Which companies this request is legitimately about.

    Scanning the whole universe for substrings worked at three tickers and is
    actively wrong at ten thousand: ALL, ON, IT, NOW and BE are real tickers,
    so any lowercase prose question would "mention" a dozen companies. Match
    on capitalised word-boundary tokens in the question as written instead —
    a ticker in a question is written in caps.
    """
    universe = config.TICKERS
    found = {t for t in TICKER_TOKEN.findall(question or "") if t in universe}
    return {ticker.upper()} | found


async def gather_facts(sess, question: str, ticker: str
                       ) -> tuple[list[dict], str | None]:
    listed = await sess.list_tools()
    schemas = {t.name: (getattr(t, "input_schema", None)
                        or getattr(t, "inputSchema", {}) or {})
               for t in listed.tools}
    tools = [{"type": "function",
              "function": {"name": t.name,
                           "description": (t.description or "").strip(),
                           "parameters": schemas[t.name]}}
             for t in listed.tools]
    log.info("mcp.connected", tools=sorted(schemas))

    expected = expected_tickers(question, ticker)
    messages = [{"role": "system", "content": prompts.SYSTEM},
                {"role": "user", "content": f"Ticker: {ticker}. {question}"}]
    facts: list[dict] = []
    rejections = 0
    last_error_kind: str | None = None

    for step in range(MAX_TOOL_STEPS):
        try:
            msg = llm.chat(messages, tools,
                           effort=config.PLAN_REASONING_EFFORT)
        except (requests.RequestException, llm.LLMError) as exc:
            # The planning call runs inside the MCP session, so without this
            # an unreachable model surfaces as "the filings database could
            # not be reached" — a confident, wrong diagnosis.
            raise ModelUnavailable(str(exc)) from exc
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            log.info("plan.done", step=step, facts=len(facts))
            break

        for call in calls:
            name = call.get("function", {}).get("name", "")
            raw = call.get("function", {}).get("arguments")

            try:
                v, dropped = toolcall.validate(
                    name, raw, schemas, expected, config.TICKERS)
            except toolcall.ToolCallError as e:
                rejections += 1
                log.warning("tool.rejected", tool=name, kind=e.kind,
                            reason=e.message, attempt=rejections, args=repr(raw))
                messages.append({"role": "tool", "name": name or "unknown",
                                 "content": e.for_model()})
                if rejections >= MAX_REJECTIONS:
                    log.error("tool.giving_up", rejections=rejections)
                    return facts, "tool_calls_rejected"
                continue

            if dropped:
                log.info("tool.args_dropped", tool=v.name, dropped=dropped)

            t0 = time.time()
            try:
                res = await sess.call_tool(v.name, v.args)
                payload = (getattr(res, "structured_content", None)
                           or getattr(res, "structuredContent", None)
                           or json.loads(res.content[0].text))
            except Exception as exc:  # noqa: BLE001 — tool crash is not fatal
                log.error("tool.crashed", tool=v.name, args=v.args,
                          error=str(exc), ms=int((time.time() - t0) * 1000))
                messages.append({"role": "tool", "name": v.name,
                                 "content": json.dumps({
                                     "error": f"Tool failed: {exc}",
                                     "instruction": "Try a different tool."})})
                continue

            ms = int((time.time() - t0) * 1000)
            if "error" in payload:
                last_error_kind = payload.get("error_kind") or "tool_error"
                log.warning("tool.error", tool=v.name, args=v.args,
                            error=payload["error"],
                            error_kind=last_error_kind, ms=ms)
            else:
                added = harvest(payload, facts)
                log.info("tool.call", tool=v.name, args=v.args,
                         ms=ms, facts_added=added, facts_total=len(facts))
                if added == 0:
                    # Succeeded and returned nothing. Distinct from an error,
                    # and the reason a request ends up refused.
                    log.warning("tool.empty", tool=v.name, args=v.args)
            messages.append({"role": "tool", "name": v.name,
                             "content": json.dumps(payload)[:4000]})

    return facts, last_error_kind


def _noop_stage(stage: str, status: str, **extra) -> None:
    """Default progress sink. The CLI and the JSON API have nowhere to draw."""


async def run(question: str, ticker: str = "NVDA", on_stage=None) -> dict:
    """Answer one question.

    `on_stage(stage, status, **extra)` is called as each pipeline stage starts
    and finishes, so the SSE endpoint can re-render its rail without polling.
    It is a plain callback rather than a queue because the caller decides what
    to do with an event; the agent only decides when one happened.
    """
    emit = on_stage or _noop_stage
    trace_id = logging_setup.new_trace()
    t0 = time.time()
    timing: dict[str, int] = {}
    log.info("request.start", question=question, ticker=ticker)

    # Checked first, and before any work: with no model there is no narrated
    # answer to produce, and spinning up an MCP subprocess to discover that
    # would report a configuration choice as a runtime failure.
    if not config.model_enabled():
        timing["total"] = int((time.time() - t0) * 1000)
        log.info("request.refused", kind="model_unavailable", stage="config")
        return _refuse(trace_id, "model_unavailable", llm.NO_MODEL, timing,
                       question=question, ticker=ticker)

    scope = policy.check_scope(question)
    if scope:
        log.info("request.refused", kind=scope.kind, stage="scope")
        timing["total"] = int((time.time() - t0) * 1000)
        return _refuse(trace_id, scope.kind, scope.message, timing,
                       question=question, ticker=ticker)

    # Precondition, not a discovery. A symbol that is not an SEC registrant
    # has no filings by definition, and letting the model find that out by
    # having three tool calls rejected reports it as "could not build a data
    # request" — true, but not the reason. Same argument as the scope gate:
    # decide it in code, before inference.
    if companies.resolve(ticker) is None:
        near = [h["ticker"] for h in companies.search(ticker, 4)]
        msg = REFUSALS["unknown_ticker"][1]
        if near:
            msg += f" Closest matches: {', '.join(near)}."
        log.info("request.refused", kind="unknown_ticker", stage="resolve")
        timing["total"] = int((time.time() - t0) * 1000)
        return _refuse(trace_id, "unknown_ticker", msg, timing,
                       question=question, ticker=ticker)

    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "filingdesk.mcp_server"],
                                   env=mcp_env())
    tp = time.time()
    emit("tools", "start")
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                facts, why = await gather_facts(sess, question, ticker)
    except Exception as eg:  # noqa: BLE001 — anyio raises groups
        flat = _flatten(eg)
        timing["tools"] = int((time.time() - tp) * 1000)
        emit("tools", "error")
        # anyio nests the real cause inside a task group, so the model failure
        # has to be dug out rather than inferred from the outermost type.
        if any(isinstance(e, ModelUnavailable) for e in flat):
            timing["total"] = int((time.time() - t0) * 1000)
            log.error("model.unavailable", host=config.OLLAMA)
            return _refuse(trace_id, "model_unavailable", NO_MODEL_MSG, timing,
                           question=question, ticker=ticker)
        causes = "; ".join(f"{type(e).__name__}: {e}" for e in flat)
        log.error("mcp.unavailable", error=causes)
        return _refuse(trace_id, "data_layer_unavailable",
                       "The filings database could not be reached, so no answer "
                       "was attempted. Check that the service has its data volume "
                       f"mounted. ({causes})", timing,
                       question=question, ticker=ticker)
    timing["tools"] = int((time.time() - tp) * 1000)
    emit("tools", "done", ms=timing["tools"], facts=len(facts))

    if not facts:
        kind, msg = REFUSALS.get(why or "no_facts", REFUSALS["no_facts"])
        log.info("request.refused", kind=kind, stage="retrieval", why=why)
        timing["total"] = int((time.time() - t0) * 1000)
        return _refuse(trace_id, kind, msg, timing,
                       question=question, ticker=ticker)

    tr = time.time()
    emit("retrieve", "start")
    # vault.retrieve and llm.chat are blocking `requests` calls. Awaiting them
    # on the event loop would stall the SSE heartbeat for the whole draft —
    # the UI would go silent for exactly the 40s it most needs to not be.
    passages = await asyncio.to_thread(vault.retrieve, question, 3)
    timing["retrieve"] = int((time.time() - tr) * 1000)
    emit("retrieve", "done", ms=timing["retrieve"], k=len(passages))
    log.info("retrieve", k=len(passages),
             top_score=round(passages[0]["score"], 3) if passages else None)

    table = fact_table(facts)
    allowed = {i: f["value"] for i, f in enumerate(facts, 1)}

    td = time.time()
    emit("draft", "start")
    try:
        msg = await asyncio.to_thread(
            llm.chat, [{"role": "user", "content": prompts.DRAFT.format(
                question=question, facts=table,
                passages="\n---\n".join(p["text"][:400] for p in passages)
                         or "(none)")}])
    except Exception as exc:  # noqa: BLE001 — no model is a refusal, not a 500
        timing["draft"] = int((time.time() - td) * 1000)
        timing["total"] = int((time.time() - t0) * 1000)
        emit("draft", "error")
        log.error("draft.unavailable", error=repr(exc))
        return _refuse(trace_id, "model_unavailable",
                       NO_MODEL_MSG + " The figures retrieved for this question "
                       "are kept below.", timing,
                       question=question, ticker=ticker, facts=facts)
    draft = msg["content"]
    timing["draft"] = int((time.time() - td) * 1000)
    emit("draft", "done", ms=timing["draft"])

    tg = time.time()
    emit("guard", "start")
    problems = guard.check(draft, allowed)
    log.info("guard", unsupported=len(problems), repaired=False,
             claims=[p["claim"] for p in problems])
    if problems:
        emit("repair", "start", claims=[p["claim"] for p in problems])
        try:
            draft = (await asyncio.to_thread(
                llm.chat, [{"role": "user", "content": prompts.REPAIR.format(
                    problems=json.dumps(problems, indent=2),
                    facts=table, draft=draft)}]))["content"]
        except Exception as exc:  # noqa: BLE001 — losing the repair is not
            # fatal: the unrepaired draft is still shown WITH its unsupported
            # figures struck, which is the safe direction to fail in.
            log.error("repair.failed", error=repr(exc))
        else:
            problems = guard.check(draft, allowed)
            log.info("guard", unsupported=len(problems), repaired=True,
                     claims=[p["claim"] for p in problems])
    timing["guard"] = int((time.time() - tg) * 1000)
    emit("guard", "done", ms=timing["guard"], unsupported=len(problems))

    draft = draft.strip() + provenance.note(facts)

    timing["total"] = int((time.time() - t0) * 1000)
    result = {"trace_id": trace_id, "report_md": draft.strip(),
              "question": question, "ticker": ticker.upper(),
              "facts": facts, "fact_table": table,
              "passages": [{"path": p["path"], "score": round(p["score"], 3)}
                           for p in passages],
              "unsupported_claims": problems, "latency_ms": timing}
    log.info("request.end", ok=not problems, ms=timing["total"],
             facts=len(facts))
    history.append(result)
    logging_setup.clear()
    return result


RULE = "\u2500" * 72

TITLES = {
    "out_of_scope_advice": "Out of scope \u2014 investment advice",
    "out_of_scope_market_data": "Out of scope \u2014 market data",
    "out_of_scope_forecast": "Out of scope \u2014 forecasting",
    "no_company_data": "No data for that company",
    "no_concept_data": "No data for that figure",
    "no_such_metric": "Metric not supported",
    "tool_calls_rejected": "Could not build a data request",
    "data_layer_unavailable": "Data layer unavailable",
    "model_unavailable": "No model — figures only",
    "unknown_ticker": "No such ticker",
    "no_xbrl_data": "Company files no XBRL",
    "fetch_failed": "Could not reach SEC EDGAR",
    "no_facts": "Nothing retrievable",
}


def _wrap(text: str, width: int = 72) -> str:
    return "\n".join(textwrap.fill(par, width) for par in text.split("\n\n"))


def pretty(res: dict) -> str:
    if res.get("refused"):
        kind = res.get("refusal_kind", "no_facts")
        return "\n".join([
            "", RULE, TITLES.get(kind, "Refused"), RULE, "",
            _wrap(res["refused"]), "",
            f"  no answer produced \u00b7 {kind} \u00b7 {res['trace_id']}", ""])

    out = ["", RULE, "", _wrap(res["report_md"]), "", RULE, "SOURCES", "",
           res["fact_table"]]
    if res["unsupported_claims"]:
        out += ["", "UNVERIFIED \u2014 these figures trace to no fact:"]
        out += [f"  \u00b7 {p['claim']} ({p['reason']})"
                for p in res["unsupported_claims"]]
    t = res["latency_ms"]
    out += [RULE,
            f"  {len(res['facts'])} facts \u00b7 "
            f"{len(res['unsupported_claims'])} unverified \u00b7 "
            f"{t.get('total', 0) / 1000:.1f}s \u00b7 {res['trace_id']}", ""]
    return "\n".join(out)


def main() -> None:
    logging_setup.configure()
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(textwrap.dedent("""
            filing — grounded answers from SEC filings

              filing report "<question>" [--ticker NVDA] [--json] [--stub]

            Every figure in the answer is retrieved from a filing. Nothing is
            generated. Out-of-scope questions are refused rather than guessed.
        """).strip())
        return
    if "--stub" in argv:
        from . import stub
        stub.install()
    ticker = "NVDA"
    if "--ticker" in argv:
        i = argv.index("--ticker")
        if i + 1 >= len(argv):
            sys.exit("--ticker needs a value, e.g. --ticker NVDA")
        ticker = argv[i + 1].upper()
        del argv[i:i + 2]
    words = [a for a in argv if not a.startswith("--")]
    if words and words[0] == "report":
        words = words[1:]
    q = words[0] if words else "How has gross margin moved over the last 8 quarters?"
    res = asyncio.run(run(q, ticker))
    print(json.dumps(res, indent=2, default=str) if "--json" in argv
          else pretty(res))


if __name__ == "__main__":
    main()
