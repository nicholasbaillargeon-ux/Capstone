"""The "Try" row on /ask: which questions get offered, against which company.

Three of these are shown at a time and they change on every page load, because
three fixed examples teach exactly three things and then become furniture. The
pool is twenty questions over ten companies, walked in order rather than
sampled — a shuffle repeats, and a visitor who reloads twice and sees the same
button learns nothing the second time.

The part that took measuring is the pairing. An example that comes back
"this company reports no data for that figure" is worse than no example at all:
it is the first thing a visitor clicks and the app's answer is a refusal. And
the mismatches are not exotic — JPMorgan files no gross profit, because a bank
does not have one, so `JPM · How has gross margin moved?` is a broken button
sitting on the front of a tool whose whole argument is that it does not make
things up. Alphabet and Meta do not tag it either.

So every question declares what it needs, and a question is only ever offered
against a company known to report it. "Known" is the operative word: capability
is read from the filings already on disk, and a company that has never been
loaded reports nothing rather than reporting that it has nothing. Unknown is
therefore treated as allowed — the agent fetches on first use and the pairing
almost always works — while a company that IS loaded and demonstrably lacks the
figure is skipped. The examples get better as the cache fills, which is the
right direction for them to move in.
"""
from __future__ import annotations

import itertools
import json

from . import companies, config, metrics, series

# Twenty questions, in rough order of how often a person actually wants each
# one. `needs` is either a logical concept from config.CONCEPTS or a metric key
# from metrics.REGISTRY; it is what decides whether a company can be asked this.
#
# All twenty are inside the scope gate on purpose, and a test asserts it. It
# would be a poor joke to put a question on the front page that policy.py
# refuses — and the phrasings that trip it are closer than they look: "next
# quarter", "outlook for", "how much is <x> stock", "dividend yield".
QUESTIONS: list[dict] = [
    {"q": "How has gross margin moved over the last 8 quarters?",
     "needs": "gross_margin"},
    {"q": "What was revenue in the most recent quarter?",
     "needs": "Revenues"},
    {"q": "What was net income in the most recent quarter?",
     "needs": "NetIncome"},
    {"q": "How has revenue moved across the series?",
     "needs": "Revenues"},
    {"q": "How has operating cash flow moved across the series?",
     "needs": "OperatingCashFlow"},
    {"q": "Which quarter had the highest gross margin?",
     "needs": "gross_margin"},
    {"q": "How has net income moved over the last 8 quarters?",
     "needs": "NetIncome"},
    {"q": "What was free cash flow in the most recent quarter?",
     "needs": "free_cash_flow"},
    {"q": "How has operating margin moved across the series?",
     "needs": "operating_margin"},
    {"q": "What was diluted EPS in the most recent quarter?",
     "needs": "EarningsPerShareDiluted"},
    {"q": "How has net margin moved over the last 8 quarters?",
     "needs": "net_margin"},
    {"q": "How much of revenue goes to research and development?",
     "needs": "rnd_intensity"},
    {"q": "How has research and development spending moved across the series?",
     "needs": "ResearchAndDevelopment"},
    {"q": "Has any quarter in the series been restated?",
     "needs": "Revenues"},
    {"q": "What were total assets at the most recent period end?",
     "needs": "Assets"},
    {"q": "How has the current ratio moved across the series?",
     "needs": "current_ratio"},
    {"q": "How has debt to equity moved over the last 8 quarters?",
     "needs": "debt_to_equity"},
    {"q": "How much cash and equivalents were on the balance sheet?",
     "needs": "CashAndEquivalents"},
    {"q": "How much has been spent buying back stock across the series?",
     "needs": "StockRepurchased"},
    {"q": "How has the effective tax rate moved across the series?",
     "needs": "effective_tax_rate"},
]

# Which companies the row rotates through. config.FEATURED is already ordered
# by how often people look one up, so this is its head rather than a second
# list to drift out of sync with it.
TICKERS: list[str] = config.FEATURED[:10]

# Advanced once per render. itertools.count is C-level, so `next` on it is
# atomic — which matters because FastAPI serves sync endpoints from a thread
# pool and `n += 1` from two of them at once is not.
_turn = itertools.count()

# {ticker: (concepts, metrics)}, thrown away whenever the filings cache is
# written. Keyed on the database's mtime rather than a clock: the thing that
# can change the answer is a company being loaded or refreshed, and that is
# exactly what moves the file.
_caps: dict[str, tuple[set[str], set[str]] | None] = {}
_caps_stamp: float | None = None


def _capabilities(ticker: str) -> tuple[set[str], set[str]] | None:
    """What this company reports, or None if that is not known.

    None is cached for a symbol that is not a registrant, because that answer
    cannot change. It is NOT cached for a registrant with nothing loaded — that
    one changes the moment somebody views the company.
    """
    global _caps_stamp

    try:
        stamp = config.DUCK.stat().st_mtime
    except OSError:
        return None
    if stamp != _caps_stamp:
        _caps.clear()
        _caps_stamp = stamp

    if ticker in _caps:
        return _caps[ticker]

    cik = companies.resolve(ticker)
    if cik is None:
        _caps[ticker] = None
        return None
    concepts = set(series.available_concepts(cik))
    if not concepts:
        # Never loaded. Deliberately not cached as "no": the next page load
        # after somebody views this company should see the real answer.
        return None
    met = {m.key for m in metrics.REGISTRY.values()
           if set(m.inputs) <= concepts}
    _caps[ticker] = (concepts, met)
    return _caps[ticker]


def can_answer(ticker: str, need: str) -> bool:
    """Is this company known NOT to report what the question needs?

    Phrased as a permission rather than a fact because unknown has to mean yes.
    A company nothing has been loaded for reports nothing, and reading that as
    "cannot answer" would empty the Try row on a fresh instance — where it is
    the only thing telling a visitor what to type.
    """
    caps = _capabilities(ticker)
    if caps is None:
        return True
    concepts, met = caps
    return need in met or need in concepts


def pick(n: int = 3) -> list[dict]:
    """`n` examples, different ones each call, none of them known to fail.

    One counter drives both wheels. Questions walk it directly, so consecutive
    renders share none and a lap of the pool takes seven loads. Tickers walk it
    plus the lap number, which is the whole trick: without the lap term, ten
    tickers under twenty questions means question 3 draws ticker 3 forever, and
    the row rotates its text while showing the same company against the same
    question every time round. Adding one per lap shifts the whole pairing, so
    a question meets all ten companies over ten laps instead of one.
    """
    turn = next(_turn)
    out: list[dict] = []
    used: set[str] = set()

    for i in range(min(n, len(QUESTIONS))):
        seq = turn * n + i
        item = QUESTIONS[seq % len(QUESTIONS)]
        ticker = _ticker_for(item["needs"], seq + seq // len(QUESTIONS), used)
        used.add(ticker)
        out.append({
            "ticker": ticker,
            "question": item["q"],
            # hx-vals carries the pair straight to /ui/run, so an example is
            # one click rather than fill-then-submit.
            "vals": json.dumps({"question": item["q"], "ticker": ticker}),
        })
    return out


def _ticker_for(need: str, offset: int, used: set[str]) -> str:
    """A company that can answer this, preferring one not already on the row.

    Two passes rather than one: a row of three that names the same company
    three times reads as a bug, but a repeat is much better than an example
    that refuses, so "can answer" wins over "not already used".
    """
    order = [TICKERS[(offset + k) % len(TICKERS)] for k in range(len(TICKERS))]
    able = [t for t in order if can_answer(t, need)]
    for t in able:
        if t not in used:
            return t
    return able[0] if able else order[0]
