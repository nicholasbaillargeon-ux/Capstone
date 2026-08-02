"""Filing Desk — grounded answers from SEC filings.

Every figure is retrieved from a filing; none are generated. Numbers come from
MCP tool calls, framing from vault RAG, ratios from Python, and a deterministic
guard at the seam rejects anything that traces to no fact.
"""

__version__ = "0.2.0"
