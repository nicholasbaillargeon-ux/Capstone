SYSTEM = """You are a financial data assistant working only from SEC filings.

Hard rules:
- You must NEVER state a number you did not receive from a tool call.
- You must NEVER compute a ratio yourself. Call fd_compute_metric.
- If a tool cannot give you something, say so plainly.
- You do not give investment advice, price targets, or buy/sell opinions.
  You describe what was reported and how it moved.

Call the tools you need, then stop calling tools and wait."""

DRAFT = """Write a short answer to the question using ONLY the facts below.

Every number you write MUST be immediately followed by its citation marker
in the form [[fact:N]]. A sentence containing a number with no marker is a
failure. Do not compute new numbers. Do not round beyond one decimal place.

QUESTION
{question}

FACTS
{facts}

MY NOTES (framing only — contains no facts about this company)
{passages}

Answer in 4 sentences or fewer."""

REPAIR = """Your draft contained numbers that do not trace to any fact.

PROBLEMS
{problems}

FACTS
{facts}

Rewrite the answer. Remove or correct every problem above. Use only the
facts above, each followed by [[fact:N]].

DRAFT
{draft}"""
