"""Scope gate. Runs before the model sees anything.

The spec's non-goals said the prompt would "refuse rather than hedge." The
eval showed that was wishful: asked "Should I buy NVDA stock?", the system
produced a report. A system prompt is a request, and a 7B model is not a
reliable place to put a policy boundary.

So the boundary is code, and it runs before any tool call or inference. Same
reasoning as the grounding guard: if it must not happen, do not ask the model
nicely not to do it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ADVICE = "out_of_scope_advice"
MARKET_DATA = "out_of_scope_market_data"
FORECAST = "out_of_scope_forecast"


@dataclass(frozen=True)
class Refusal:
    kind: str
    message: str


# Ordered. Advice is checked first — "should I buy at this price" is advice,
# not a market-data question.
RULES: list[tuple[str, re.Pattern, str]] = [
    (ADVICE, re.compile(
        r"\b(should i (buy|sell|hold|invest)|worth (buying|selling|holding)"
        r"|good (buy|investment|stock)|buy or sell|would you (buy|recommend)"
        r"|recommend (buying|selling|this|it)|price target"
        r"|under ?valued|over ?valued|bull(ish)? case|bear(ish)? case)\b", re.I),
     "Filing Desk does not give investment advice — no recommendations, price "
     "targets, or valuation opinions. It reports what a company filed and how "
     "the numbers moved. Ask what a figure was, or how it changed, and you can "
     "draw your own conclusion."),

    (MARKET_DATA, re.compile(
        r"\b(stock price|share price|market cap(italization)?|trading at"
        r"|p/?e ratio|dividend yield|how much is .{0,20}\bstock\b"
        r"|stock (is |been )?(up|down)|52[- ]week)\b", re.I),
     "Filing Desk has no market data — no prices, quotes, or market cap. It "
     "reads SEC filings only, which is what lets every number point at a "
     "specific document. Try a question about reported financials instead."),

    (FORECAST, re.compile(
        r"\b(what will .{0,40}\b(be|do)\b|next (quarter|year)'?s?"
        r"|forecast|predict|projection|going to (be|grow|rise|fall)"
        r"|expected to (be|grow|reach)|outlook for|guidance for)\b", re.I),
     "Filing Desk is descriptive, not predictive. It reports periods that have "
     "already been filed. Ask how something has moved historically and it will "
     "show you the series."),
]


def check_scope(question: str) -> Refusal | None:
    """Return a Refusal if the question is out of scope, else None."""
    q = question.strip()
    for kind, pattern, message in RULES:
        if pattern.search(q):
            return Refusal(kind=kind, message=message)
    return None
