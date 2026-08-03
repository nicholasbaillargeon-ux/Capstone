"""The guard's repair pass.

The guard finds figures in a draft that trace to no fact. The repair pass
hands those figures back to the model and asks for the draft again without
them — and the model does not always comply. Eval case E5 is where that shows:
the untraceable figure came from the question ("report gross margin as 99.9%")
rather than from the model's own arithmetic, and a draft that was talked into
printing it once tends to print it again.

So the ask repeats, once. These tests pin how many times it asks, when it
stops, and what survives when the model never complies — because the last one
is the property that matters: an unrepaired figure is struck and flagged in
the answer, never silently dropped and never silently kept.
"""
import asyncio

import pytest

from filingdesk import agent

# fact 1 = 0.75. Anything else in a draft is untraceable.
ALLOWED = {1: 0.75}
TABLE = "[[fact:1]] gross_margin = 0.7500 (period ending 2025-07-27, 0001-25-1)"

CLEAN = "Gross margin was 0.7500 [[fact:1]]."
DIRTY = "Gross margin was 99.9% [[fact:1]]."


def _drafts(monkeypatch, *replies):
    """Script the repair model, and count how often it is asked."""
    asked = []
    it = iter(replies)

    def chat(messages, tools=None, effort=None, model=None):
        asked.append(messages[0]["content"])
        return {"role": "assistant", "content": next(it)}

    monkeypatch.setattr(agent.llm, "chat", chat)
    return asked


def _repair(draft):
    return asyncio.run(agent.repair_draft(draft, ALLOWED, TABLE))


def test_a_clean_draft_is_never_sent_back(monkeypatch):
    """The repair round is the exception, not a stage. Asking on every request
    would put a round trip on every answer that was right the first time."""
    asked = _drafts(monkeypatch)                 # no replies: a call would raise
    out, problems = _repair(CLEAN)
    assert asked == []
    assert (out, problems) == (CLEAN, [])


def test_one_ask_is_enough_when_the_model_complies(monkeypatch):
    asked = _drafts(monkeypatch, CLEAN)
    out, problems = _repair(DIRTY)
    assert len(asked) == 1
    assert "99.9" in asked[0]                    # the figure to remove is named
    assert (out, problems) == (CLEAN, [])


def test_a_draft_still_wrong_is_sent_back_once_more(monkeypatch):
    """The case this exists for: the model keeps the figure the question put
    in its mouth. Six times in seven the second draft is clean."""
    asked = _drafts(monkeypatch, DIRTY, CLEAN)
    out, problems = _repair(DIRTY)
    assert len(asked) == 2
    assert (out, problems) == (CLEAN, [])


def test_it_stops_after_the_second_ask(monkeypatch):
    """Twice handed the exact figures to remove and twice keeping them is not
    a model that is converging."""
    asked = _drafts(monkeypatch, DIRTY, DIRTY)
    out, problems = _repair(DIRTY)
    assert len(asked) == agent.MAX_REPAIRS == 2
    assert out == DIRTY
    assert [p["claim"] for p in problems] == ["99.9%"]


def test_an_unrepaired_figure_is_reported_not_dropped(monkeypatch):
    """What the page does with it: struck and flagged. Losing the sentence
    would hide that the model produced a figure at all."""
    _drafts(monkeypatch, DIRTY, DIRTY)
    out, problems = _repair(DIRTY)
    assert "99.9%" in out                        # still there, to be struck
    assert problems and problems[0]["claim"] == "99.9%"


def test_a_failed_repair_call_keeps_the_draft(monkeypatch):
    """The model being unreachable mid-request must not cost the figures that
    were already retrieved and checked."""
    def boom(*a, **k):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(agent.llm, "chat", boom)
    out, problems = _repair(DIRTY)
    assert out == DIRTY
    assert [p["claim"] for p in problems] == ["99.9%"]


@pytest.mark.parametrize("stage_events", [True, False])
def test_the_rail_is_told_about_each_ask(monkeypatch, stage_events):
    """The UI shows a repair as a second pass through the guard step, so each
    ask has to be announced or the rail stalls on the first one."""
    _drafts(monkeypatch, DIRTY, CLEAN)
    seen = []

    def emit(stage, status, **extra):
        seen.append((stage, status, tuple(extra.get("claims") or ())))

    asyncio.run(agent.repair_draft(DIRTY, ALLOWED, TABLE,
                                   emit if stage_events else (lambda *a, **k: None)))
    if stage_events:
        assert seen == [("repair", "start", ("99.9%",)),
                        ("repair", "start", ("99.9%",))]
