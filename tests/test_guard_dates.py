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


# A hyphen is deliberately absent from this list: "12 million-dollar deals"
# reads the suffix as a scale, and English does not settle whether it is one.
# \b fires on the hyphen, so the figure is flagged rather than waved through —
# the same direction the month-word lookahead above chose, and for the reason
# stated there: a false negative hides a fabricated figure, a false positive
# only costs a correct one.
@pytest.mark.parametrize("word", ["but", "before", "by", "broadly",
                                  "more", "meanwhile", "known", "keeping"])
def test_a_word_after_a_figure_is_not_read_as_a_scale_suffix(word):
    """Regression, found in eval case E4: NUM had no boundary after its word
    suffixes, so "0.7500 but above" tokenised as "0.7500 b", resolved to
    0.75e9, and was reported as a figure tracing to no fact. The report never
    contained it — the guard invented the claim it then rejected."""
    sent = f"The margin held at 0.7500[[fact:1]] {word} the trend was flat."
    assert guard.check(sent, {1: 0.75}) == []


@pytest.mark.parametrize("sent,claim", [
    ("Revenue reached $999 billion but then fell[[fact:1]].", "$999 billion"),
    ("Revenue reached $999b but then fell[[fact:1]].", "$999b"),
    ("Revenue reached $999m, more than planned[[fact:1]].", "$999m"),
])
def test_a_real_suffix_still_scales_when_a_word_follows(sent, claim):
    """The boundary must not cost the guard its scale handling: a fabricated
    figure written with a suffix is still caught, and still quoted as written."""
    problems = guard.check(sent, ALLOWED)
    assert [p["claim"] for p in problems] == [claim]


def test_a_day_that_is_also_a_real_figure_still_needs_its_citation():
    """31 as a standalone figure is unrelated to "January 31" and is judged
    on its own merits."""
    problems = guard.check("The ratio was 31.", {1: 12.0})
    assert [p["claim"] for p in problems] == ["31"]


# ---- losses ---------------------------------------------------------------
# A company that loses money files a negative figure, and the guard read the
# sign off every one of them. "‑0.0238" was extracted as 0.0238, compared
# against a fact worth -0.0238, and struck — a number the model had quoted
# exactly right, crossed out in front of the reader.
#
# Found by running the /ask page's own example questions rather than the eval
# suite, which asks about one company that has been profitable throughout. It
# fired on Intel's operating margin: three struck figures in a four-sentence
# answer, all three correct.

LOSSES = {1: -0.0238, 2: -0.2470, 3: 0.0289, 4: -2.5e9}


@pytest.mark.parametrize("sent", [
    "Operating margin fell to -0.0238[[fact:1]] in the period.",
    "Operating margin fell to ‑0.0238[[fact:1]] in the period.",   # U+2011
    "Operating margin fell to −0.0238[[fact:1]] in the period.",   # U+2212
    "It went from -0.0238[[fact:1]] to -0.2470[[fact:2]].",
    "Free cash flow was -2.5 billion[[fact:4]] for the quarter.",
])
def test_a_correctly_quoted_loss_is_not_struck(sent):
    assert guard.check(sent, LOSSES) == []


def test_a_loss_written_in_words_still_passes():
    """"a loss of 2.38%" puts the sign in the word and not on the number. That
    is ordinary prose and the figure is right, so an unsigned token is allowed
    to match a negative fact."""
    assert guard.check(
        "The period showed a loss of 0.0238[[fact:1]].", LOSSES) == []


def test_writing_the_wrong_sign_is_still_caught():
    """The reason the sign is read rather than discarded. Quoting a loss as a
    profit is the exact class of error this module exists to catch, and it is
    worse than a fabricated figure because it reconciles."""
    problems = guard.check("Margin was -0.0238[[fact:1]].", {1: 0.0238})
    assert [p["claim"] for p in problems] == ["-0.0238"]


def test_a_fabricated_negative_is_caught():
    problems = guard.check("Margin was -0.9999[[fact:1]].", LOSSES)
    assert [p["claim"] for p in problems] == ["-0.9999"]


@pytest.mark.parametrize("sent", [
    "Across 2024-2025 the margin held at 0.0289[[fact:3]].",
    "Between 2019-2024 it stayed at 0.0289[[fact:3]].",
])
def test_a_span_of_years_is_not_a_negative_figure(sent):
    """The hyphen in a range is not a minus. Reading it as one would turn the
    second year into a figure tracing to nothing, and every answer that writes
    a date range would come back dirty."""
    assert guard.check(sent, LOSSES) == []
