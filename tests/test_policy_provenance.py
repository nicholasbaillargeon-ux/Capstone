"""The scope gate is a policy boundary in code. A gate that blocks real
questions is worse than no gate, so most of these test for false positives."""
import pytest
from evals.cases import CASES

from filingdesk import policy, provenance


@pytest.mark.parametrize("q,kind", [
    ("Should I buy NVDA stock?", policy.ADVICE),
    ("Is NVDA a good investment right now?", policy.ADVICE),
    ("Would you recommend buying at this level?", policy.ADVICE),
    ("Is the stock undervalued?", policy.ADVICE),
    ("What's your price target?", policy.ADVICE),
    ("What is NVDA's stock price today?", policy.MARKET_DATA),
    ("What's their market cap?", policy.MARKET_DATA),
    ("What P/E ratio is it trading at?", policy.MARKET_DATA),
    ("What will gross margin be next quarter?", policy.FORECAST),
    ("Forecast revenue for next year.", policy.FORECAST),
    ("Is revenue expected to grow?", policy.FORECAST),
])
def test_out_of_scope_is_refused(q, kind):
    r = policy.check_scope(q)
    assert r is not None, f"not caught: {q}"
    assert r.kind == kind


@pytest.mark.parametrize("q", [
    "How has gross margin moved over the last 8 quarters?",
    "What was gross profit in the most recent quarter?",
    "Compare the first and last quarter of the series by gross margin.",
    "Which quarter had the highest gross margin?",
    "Has any quarter been restated?",
    "Show me gross profit for every quarter including Q4.",
    "How did revenue change from the first quarter to the last?",
    "What did they report for revenue in the quarter ending 2025-10-26?",
])
def test_legitimate_questions_pass(q):
    """False positives are the expensive failure. A blocked real question is
    invisible to the user — they just see a refusal and assume it's broken."""
    assert policy.check_scope(q) is None, f"wrongly blocked: {q}"


def test_every_report_case_survives_the_gate():
    for c in CASES:
        if c["kind"] == "report":
            assert policy.check_scope(c["q"]) is None, c["id"]


def test_refusal_messages_say_what_to_do_instead():
    for _, _, msg in policy.RULES:
        assert len(msg) > 80
        assert any(w in msg.lower() for w in ("ask", "try", "instead"))


def test_provenance_silent_when_nothing_to_disclose():
    assert provenance.note([{"end": "2025-10-26", "accn": "0001-25-1"}]) == ""


def test_provenance_discloses_derived_quarter():
    n = provenance.note([{"end": "2026-01-25", "accn": "0001-26-9", "derived": True}])
    assert "no filing" in n.lower() and "2026-01-25" in n


def test_provenance_discloses_restatement():
    n = provenance.note([{"end": "2024-10-27", "accn": "0001-25-9", "restated": True}])
    assert "restated" in n.lower() and "most recently filed" in n.lower()
