"""The first question about a company nobody has looked at yet.

This is a regression test for a bug that only appeared in the deployed shape.
DuckDB allows many concurrent readers across processes but one exclusive
writer, and the server keeps a read-only handle open to serve the dashboard.
The MCP tools run in a subprocess, so their attempt to fetch an uncached
company hit "Conflicting lock is held" — and the request came back refused,
with no figures, for a company the dashboard could load on demand.

Standalone it never reproduced: one process, one lock, no conflict. The whole
test suite passed while the deployed answer path could not load a new company.

The fix is to fetch in the process that owns the lock, before dispatching the
tool that needs the data. These tests pin that ordering and the failure
handling around it, not the DuckDB behaviour underneath — that is DuckDB's to
keep.
"""
import asyncio

import pytest

from filingdesk import agent


@pytest.fixture
def ensured(monkeypatch):
    """Record every seed.ensure the agent performs."""
    calls = []

    def fake_ensure(ticker, force=False):
        calls.append(ticker)
        return {"ok": True, "cached": False, "n_facts": 25116}

    monkeypatch.setattr(agent.seed, "ensure", fake_ensure)
    return calls


def test_the_company_is_loaded_before_the_tool_needs_it(ensured):
    asyncio.run(agent.warm_cache("AAPL"))
    assert ensured == ["AAPL"]


def test_nothing_is_fetched_for_a_tool_with_no_ticker(ensured):
    """fd_resolve_company takes a query, not a ticker. There is nothing to
    warm and no reason to touch the network."""
    asyncio.run(agent.warm_cache(None))
    asyncio.run(agent.warm_cache(""))
    assert ensured == []


def test_a_failed_fetch_does_not_lose_the_request(monkeypatch):
    """EDGAR being down, or the ticker being nonsense, is the tool's answer to
    give in its own words. Raising here would replace a specific refusal with
    a stack trace."""
    def boom(ticker, force=False):
        raise RuntimeError("EDGAR unreachable")

    monkeypatch.setattr(agent.seed, "ensure", boom)
    asyncio.run(agent.warm_cache("NVDA"))          # returns, does not raise


def test_an_unknown_ticker_is_left_for_the_tool_to_report(monkeypatch):
    def unknown(ticker, force=False):
        return {"ok": False, "error_kind": "unknown_ticker",
                "error": "ZZZZ is not an SEC-registered ticker."}

    monkeypatch.setattr(agent.seed, "ensure", unknown)
    asyncio.run(agent.warm_cache("ZZZZ"))


def test_the_fetch_happens_before_the_tool_call_not_after(monkeypatch):
    """Ordering is the whole fix. Warming after dispatch would leave the first
    question about a company answered from an empty cache."""
    order = []

    monkeypatch.setattr(agent.seed, "ensure",
                        lambda ticker, force=False: (
                            order.append(f"warm:{ticker}"),
                            {"ok": True, "cached": True})[1])

    class FakeResult:
        structured_content = {"ticker": "AAPL", "concept": "NetIncome",
                              "facts": []}

    class FakeTool:
        name = "fd_get_concept"
        description = "Time series for one financial concept."
        inputSchema = {  # noqa: N815 — the MCP SDK's own spelling
            "type": "object",
            "properties": {"ticker": {"type": "string"},
                           "concept": {"type": "string"}},
            "required": ["ticker", "concept"],
        }

    class FakeListing:
        tools = [FakeTool()]

    class FakeSession:
        async def list_tools(self):
            return FakeListing()

        async def call_tool(self, name, args):
            order.append(f"call:{name}:{args.get('ticker')}")
            return FakeResult()

    # Three replies, because this fake tool returns no facts: the planner
    # calls once, declines, gets nudged (see below), and declines again.
    replies = iter([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {
             "name": "fd_get_concept",
             "arguments": '{"ticker": "AAPL", "concept": "NetIncome"}'}}]},
        {"role": "assistant", "content": "done"},
        {"role": "assistant", "content": "done"},
    ])
    monkeypatch.setattr(agent.llm, "chat", lambda *a, **k: next(replies))

    asyncio.run(agent.gather_facts(FakeSession(), "net income?", "AAPL"))
    assert order == ["warm:AAPL", "call:fd_get_concept:AAPL"]


# ---- the planner declining the turn -------------------------------------
# Separate failure from an empty cache, and the same symptom: a request that
# refuses with "no filed figures could be retrieved" while the figures sit in
# the database. Here the planner simply never asked for them.

class _Tool:
    name = "fd_get_concept"
    description = "Time series for one financial concept."
    inputSchema = {"type": "object",  # noqa: N815 — the MCP SDK's own spelling
                   "properties": {"ticker": {"type": "string"},
                                  "concept": {"type": "string"}},
                   "required": ["ticker", "concept"]}


class _Listing:
    tools = [_Tool()]


class _Session:
    def __init__(self):
        self.called = []

    async def list_tools(self):
        return _Listing()

    async def call_tool(self, name, args):
        self.called.append((name, args.get("ticker")))

        class R:
            structured_content = {"ticker": "NVDA", "concept": "NetIncome",
                                  "facts": []}
        return R()


def _replies(monkeypatch, *msgs):
    """Script the planner, and record what it was asked each time."""
    seen = []
    it = iter(msgs)

    def chat(messages, tools=None, effort=None, model=None):
        seen.append(list(messages))
        return next(it)

    monkeypatch.setattr(agent.llm, "chat", chat)
    return seen


NO_CALLS = {"role": "assistant", "content": "I can't help with that."}
ONE_CALL = {"role": "assistant", "content": "", "tool_calls": [
    {"id": "c1", "function": {"name": "fd_get_concept",
                              "arguments": '{"ticker": "NVDA", '
                                           '"concept": "NetIncome"}'}}]}


def test_a_planner_that_calls_nothing_is_asked_once_more(monkeypatch, ensured):
    seen = _replies(monkeypatch, NO_CALLS, ONE_CALL, NO_CALLS)
    sess = _Session()
    asyncio.run(agent.gather_facts(sess, "How's the margin?", "NVDA"))

    assert len(seen) == 3                      # ask, nudge, done
    assert agent.prompts.NO_TOOLS_YET in seen[1][-1]["content"]
    assert sess.called == [("fd_get_concept", "NVDA")]


def test_the_nudge_does_not_repeat(monkeypatch, ensured):
    """A planner that declines twice is declining. Re-asking in a loop turns a
    fast refusal into a slow one."""
    seen = _replies(monkeypatch, NO_CALLS, NO_CALLS)
    facts, _ = asyncio.run(agent.gather_facts(_Session(), "q", "NVDA"))

    assert len(seen) == 2
    assert facts == []


def test_a_finished_plan_is_not_nudged(monkeypatch, ensured):
    """No tool calls WITH facts in hand is the normal exit. Nudging there
    would buy a round trip of latency on every well-behaved request."""
    seen = _replies(monkeypatch, ONE_CALL, NO_CALLS)

    class Loaded(_Session):
        async def call_tool(self, name, args):
            self.called.append((name, args.get("ticker")))

            class R:
                structured_content = {
                    "ticker": "NVDA", "concept": "NetIncome",
                    "facts": [{"concept": "NetIncome", "value": 1.0,
                               "end": "2025-01-26", "accn": "0001-25-1"}]}
            return R()

    facts, _ = asyncio.run(agent.gather_facts(Loaded(), "q", "NVDA"))
    assert len(seen) == 2
    assert len(facts) == 1
    assert all(agent.prompts.NO_TOOLS_YET not in m.get("content", "")
               for msgs in seen for m in msgs)
