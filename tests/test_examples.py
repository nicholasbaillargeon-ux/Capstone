"""The "Try" row.

These buttons are the first thing anybody clicks, so the bar is higher than it
looks: an example that refuses is the app failing in the one place a visitor
has no context to interpret it. Three things have to hold, and only the first
is about rotation.

1. The row changes on every page load and works through the whole pool.
2. Every question is inside the scope gate, so none of them is refused before
   the model is reached.
3. Every question asks for something the app can actually retrieve, and is only
   ever paired with a company known to report it.
"""
import json

import pytest

from filingdesk import config, examples, metrics, policy

# ---- the pool is answerable ---------------------------------------------

def test_every_question_is_in_scope():
    """policy.py refuses advice, market data and forecasts before inference.
    A question on the front page that trips it would be the app refusing its
    own example, and the phrasings are closer than they look — "next quarter",
    "outlook for", "dividend yield"."""
    for item in examples.QUESTIONS:
        assert policy.check_scope(item["q"]) is None, item["q"]


def test_every_question_needs_something_that_exists():
    """`needs` is what pairs a question with a company. A typo in it silently
    disables the pairing check for that question — can_answer would compare
    against a name nothing reports and the question would be offered to
    everyone, including the companies it is broken for."""
    known = set(config.CONCEPTS) | set(metrics.REGISTRY)
    for item in examples.QUESTIONS:
        assert item["needs"] in known, item


def test_the_pool_is_twenty_distinct_questions():
    qs = [item["q"] for item in examples.QUESTIONS]
    assert len(qs) == 20
    assert len(set(qs)) == 20


def test_ten_tickers():
    assert len(examples.TICKERS) == 10
    assert len(set(examples.TICKERS)) == 10


# ---- rotation ------------------------------------------------------------

def test_consecutive_loads_share_no_question():
    """The whole point. Three fixed examples teach three things and then become
    furniture."""
    first = {e["question"] for e in examples.pick(3)}
    second = {e["question"] for e in examples.pick(3)}
    assert first and not (first & second)


def test_the_whole_pool_is_reachable():
    """Rotating without covering would be a slower way of showing the same
    few. Twenty questions, three at a time, is seven loads."""
    seen = set()
    for _ in range(len(examples.QUESTIONS)):
        seen.update(e["question"] for e in examples.pick(3))
    assert seen == {item["q"] for item in examples.QUESTIONS}


def test_a_question_returns_against_a_different_company_each_lap(monkeypatch):
    """Ten tickers under twenty questions is the trap: without the lap term in
    pick(), question 3 draws ticker 3 forever and the row rotates its text
    while showing the identical button every time round.

    Capability is stubbed out here because this is about the arithmetic — with
    real data a question is legitimately unavailable to some of the ten, and
    that would hide the bug this is watching for."""
    monkeypatch.setattr(examples, "_capabilities", lambda t: None)
    by_q: dict[str, set[str]] = {}
    for _ in range(len(examples.QUESTIONS) * len(examples.TICKERS)):
        for e in examples.pick(3):
            by_q.setdefault(e["question"], set()).add(e["ticker"])
    assert all(v == set(examples.TICKERS) for v in by_q.values())


def test_a_row_does_not_repeat_a_company(monkeypatch):
    """Capability stubbed to unknown so this measures the arithmetic and not
    whichever companies happen to be in the cache on the machine running it.
    Real data can legitimately force a repeat — see the test below."""
    monkeypatch.setattr(examples, "_capabilities", lambda t: None)
    row = examples.pick(3)
    assert len({e["ticker"] for e in row}) == 3


def test_each_example_carries_the_pair_it_displays():
    """hx-vals is what actually gets submitted. If it drifted from the label,
    the button would run a different question than the one it advertises."""
    for e in examples.pick(3):
        vals = json.loads(e["vals"])
        assert vals["question"] == e["question"]
        assert vals["ticker"] == e["ticker"]


# ---- pairing -------------------------------------------------------------

@pytest.fixture
def caps(monkeypatch):
    """Pin what each company is known to report."""
    table = {
        "JPM": (set(), {"net_margin", "return_on_equity"}),
        "NVDA": ({"Revenues", "GrossProfit"},
                 {"gross_margin", "net_margin", "return_on_equity"}),
    }

    def fake(ticker):
        return table.get(ticker)

    monkeypatch.setattr(examples, "_capabilities", fake)
    return table


def test_a_company_that_files_no_gross_profit_is_not_asked_about_it(caps):
    """A bank has no gross profit, so `JPM · How has gross margin moved?` is a
    broken button on the front of a tool whose argument is that it does not
    make things up."""
    assert examples.can_answer("NVDA", "gross_margin")
    assert not examples.can_answer("JPM", "gross_margin")


def test_capability_is_stable_across_repeated_lookups(monkeypatch, tmp_path):
    """Second call has to agree with the first. The cache stored an empty
    tuple for a symbol that resolves to nothing, and an empty tuple is truthy —
    so the first lookup said "unknown" and every one after it said "cannot",
    for the same ticker on the same data."""
    db = tmp_path / "f.duckdb"
    db.write_bytes(b"")
    monkeypatch.setattr(config, "DUCK", db)
    monkeypatch.setattr(examples.companies, "resolve", lambda t: None)
    examples._caps.clear()
    first = examples._capabilities("ZZZZ")
    assert first is examples._capabilities("ZZZZ") is None


def test_an_unloaded_company_is_allowed_rather_than_ruled_out(caps):
    """Nothing loaded reports nothing, and reading that as "cannot answer"
    would empty the row on a fresh instance — where it is the only thing
    telling a visitor what to type. The agent fetches on first use."""
    assert examples.can_answer("MSFT", "gross_margin")


def test_pairing_beats_variety_when_they_conflict(monkeypatch):
    """A row naming one company three times reads as a bug; an example that
    refuses IS one. If only one company can answer, it is used twice."""
    monkeypatch.setattr(examples, "TICKERS", ["NVDA", "JPM"])
    monkeypatch.setattr(
        examples, "_capabilities",
        lambda t: (set(), {"gross_margin"}) if t == "NVDA" else (set(), set()))
    monkeypatch.setattr(examples, "QUESTIONS", [
        {"q": "a", "needs": "gross_margin"},
        {"q": "b", "needs": "gross_margin"}])
    assert {e["ticker"] for e in examples.pick(2)} == {"NVDA"}


def test_no_offered_pair_is_known_to_fail(caps):
    """The property that matters, over a full lap of the pool."""
    for _ in range(len(examples.QUESTIONS)):
        for e in examples.pick(3):
            need = next(i["needs"] for i in examples.QUESTIONS
                        if i["q"] == e["question"])
            assert examples.can_answer(e["ticker"], need), (e, need)
