"""Dates are not claims, but figures that look like dates still are.

The guard strips dates before checking numbers. Getting that wrong in either
direction is costly: strip too little and every answer that writes "January 31
2026" reports a phantom unverified figure; strip too much and a fabricated
number hides behind a word that merely looks like a month.
"""
import pytest

from filingdesk import guard

# One real fact to check against: revenue, as WMT actually reports it.
ALLOWED = {1: 706_413_000_000.0}


@pytest.mark.parametrize("sent", [
    "Revenue for the period ending January 31 2026 was $706,413,000,000[[fact:1]].",
    "Revenue for the period ending January 31, 2026 was $706,413,000,000[[fact:1]].",
    "Revenue for the year ended Jan. 31, 2026 was $706,413,000,000[[fact:1]].",
    "Revenue for the year ended 31 January 2026 was $706,413,000,000[[fact:1]].",
    "Revenue for the year ended January 31st, 2026 was $706,413,000,000[[fact:1]].",
    "In January 2026 revenue was $706,413,000,000[[fact:1]].",
    "Revenue through September 30 was $706,413,000,000[[fact:1]].",
    "Revenue for the period ending 2026-01-31 was $706,413,000,000[[fact:1]].",
])
def test_dates_are_not_unsupported_figures(sent):
    assert guard.check(sent, ALLOWED) == []


@pytest.mark.parametrize("sent", [
    # "may 5%" is English, not a date — the 5% must still be caught.
    "Margins may 5% exceed last year[[fact:1]].",
    "Growth may 12 million above plan[[fact:1]].",
])
def test_a_month_word_does_not_hide_a_figure(sent):
    problems = guard.check(sent, ALLOWED)
    assert problems, f"figure hidden behind a month word: {sent}"


def test_a_fabricated_figure_next_to_a_date_is_still_caught():
    """The date is stripped; the bogus number beside it is not."""
    sent = ("Revenue for the period ending January 31 2026 was "
            "$999,000,000,000[[fact:1]].")
    problems = guard.check(sent, ALLOWED)
    assert [p["claim"] for p in problems] == ["$999,000,000,000"]


@pytest.mark.parametrize("dash", ["-", "‐", "‑", "‒", "–",
                                  "—", "―", "−"])
def test_iso_dates_survive_whatever_dash_the_model_picked(dash):
    """gpt-oss-120b writes 2026‑01‑31 with non-breaking hyphens; the day and
    month then read as uncited figures."""
    sent = (f"The fiscal year ended on 2026{dash}01{dash}31 with revenue of "
            f"$706,413,000,000[[fact:1]].")
    assert guard.check(sent, ALLOWED) == []


@pytest.mark.parametrize("sent,claim", [
    ("Revenue was $999,000,000,000[[fact:1]].", "$999,000,000,000"),
    ("The margin was 47%.", "47%"),
    ("Free cash flow reached $12.5 billion.", "$12.5 billion"),
])
def test_a_figure_at_the_end_of_a_sentence_is_verified(sent, claim):
    """Regression: NUM used to absorb the full stop, so the token failed to
    parse and was skipped as if cleared. The last figure in a sentence is
    usually the headline one, so this silently unguarded the main claim."""
    problems = guard.check(sent, ALLOWED)
    assert [p["claim"] for p in problems] == [claim]


def test_a_day_that_is_also_a_real_figure_still_needs_its_citation():
    """31 as a standalone figure is unrelated to "January 31" and is judged
    on its own merits."""
    problems = guard.check("The ratio was 31.", {1: 12.0})
    assert [p["claim"] for p in problems] == ["31"]
