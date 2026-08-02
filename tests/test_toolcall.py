"""Tests for the seam. Tuesday flagged tool-calling as the thing most likely to
break; these are the failure modes worth having a red test for."""
import pytest

from filingdesk import toolcall

SCHEMAS = {
    "fd_compute_metric": {
        "type": "object",
        "properties": {
            "ticker": {"type": "string"},
            "metric": {"type": "string", "enum": ["gross_margin"]},
            "quarters": {"type": "integer"},
        },
        "required": ["ticker", "metric"],
    },
    "fd_get_concept": {
        "type": "object",
        "properties": {"ticker": {"type": "string"},
                       "concept": {"type": "string"}},
        "required": ["ticker", "concept"],
    },
}
UNIVERSE = {"NVDA": 1045810, "AMD": 2488}
Q = "How has NVDA gross margin moved over the last 8 quarters?"


def call(name, args, question=Q, ticker="NVDA"):
    expected = {ticker} | {t for t in UNIVERSE if t in question.upper()}
    return toolcall.validate(name, args, SCHEMAS, expected, UNIVERSE)


def test_happy_path():
    v, dropped = call("fd_compute_metric",
                      {"ticker": "NVDA", "metric": "gross_margin", "quarters": 8})
    assert v.name == "fd_compute_metric"
    assert v.args == {"ticker": "NVDA", "metric": "gross_margin", "quarters": 8}
    assert dropped == []


def test_string_args_are_parsed():
    v, _ = call("fd_compute_metric",
                '{"ticker": "NVDA", "metric": "gross_margin"}')
    assert v.args["ticker"] == "NVDA"


def test_numeric_string_is_coerced():
    """Small models emit "8" instead of 8 constantly. Coerce, don't reject."""
    v, _ = call("fd_compute_metric",
                {"ticker": "NVDA", "metric": "gross_margin", "quarters": "8"})
    assert v.args["quarters"] == 8


def test_lowercase_ticker_is_normalised():
    v, _ = call("fd_compute_metric", {"ticker": "nvda", "metric": "gross_margin"})
    assert v.args["ticker"] == "NVDA"


def test_unknown_tool_lists_alternatives():
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_get_revenue", {"ticker": "NVDA"})
    assert e.value.kind == "unknown_tool"
    assert "fd_compute_metric" in e.value.hint


def test_malformed_json():
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_compute_metric", "{ticker: NVDA")
    assert e.value.kind == "bad_json"


def test_missing_required_arg():
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_compute_metric", {"ticker": "NVDA"})
    assert e.value.kind == "missing_args"
    assert "metric" in e.value.message


def test_bad_enum():
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_compute_metric", {"ticker": "NVDA", "metric": "ebitda_margin"})
    assert e.value.kind == "bad_enum"


def test_uncoercible_type():
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_compute_metric",
             {"ticker": "NVDA", "metric": "gross_margin", "quarters": "eight"})
    assert e.value.kind == "bad_type"


def test_unknown_ticker():
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_compute_metric", {"ticker": "TSLA", "metric": "gross_margin"},
             question="How has TSLA gross margin moved?", ticker="TSLA")
    assert e.value.kind == "unknown_entity"


def test_entity_mismatch_is_caught():
    """THE test. Schema-valid, tools work, numbers are real, answer is wrong.
    The grounding guard cannot catch this because nothing is fabricated."""
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_compute_metric", {"ticker": "AMD", "metric": "gross_margin"})
    assert e.value.kind == "entity_mismatch"


def test_unknown_args_are_dropped_not_fatal():
    v, dropped = call("fd_compute_metric",
                      {"ticker": "NVDA", "metric": "gross_margin",
                       "currency": "USD"})
    assert dropped == ["currency"]
    assert "currency" not in v.args


def test_error_message_is_aimed_at_the_model():
    with pytest.raises(toolcall.ToolCallError) as e:
        call("fd_compute_metric", {"ticker": "NVDA"})
    payload = e.value.for_model()
    assert "instruction" in payload and "hint" in payload


def test_ticker_from_request_field_is_accepted():
    """Regression: the smoke test asked about AMD with the ticker in the
    request field, not the question text, and every call was rejected."""
    v, _ = call("fd_compute_metric", {"ticker": "AMD", "metric": "gross_margin"},
                question="How has gross margin moved?", ticker="AMD")
    assert v.args["ticker"] == "AMD"
