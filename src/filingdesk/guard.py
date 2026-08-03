"""The grounding guard — spec Open Question 1, resolved by building it.

Decision: require the model to emit [[fact:N]] markers AND verify the
numbers independently. Citations alone are not enough (the model will cite
a real fact next to a wrong number); number-matching alone is not enough
(no way to tell which fact a figure came from). Both, or neither works.
"""
import re

# Two things this pattern gets wrong if written loosely, both silent:
#
# `(?:\.\d+)?` rather than `\.?\d*` — the loose form swallowed the full stop
# ending a sentence, so "$706,413,000,000." reached interpretations() as an
# unparseable token, produced no candidates, and was skipped. A figure the
# guard could not parse was treated exactly like one it had cleared, which
# left the last number in a sentence — usually the headline one — unverified.
#
# Suffixes longest-first — unanchored alternation takes the first branch that
# matches, so "bn|B|billion" clipped "$12.5 billion" to "$12.5 b". The scale
# still resolved to 1e9, but the UNVERIFIED list quoted a figure the answer
# never contained. (interpretations() is anchored with $, so it backtracks
# into the right branch and was never affected.)
#
# The word suffixes need a trailing \b for the same reason, one word further
# on: "the prior quarter's 0.7500 but above" matched "0.7500 b", read it as
# 0.75e9, and reported a fabricated figure the report never contained — which
# fails an eval case for a claim nobody wrote. Any word beginning b/m/k does
# it: "but", "more", "known". The boundary is on the word branches only; there
# is no word boundary after "%", so a shared \b would break every percentage.
# The leading sign is not optional decoration: a company with a loss files a
# negative figure, and without this the guard read "‑0.0238" as 0.0238, failed
# to match it against a fact worth -0.0238, and struck a number the model had
# quoted exactly right. Every loss-making period in every company was affected
# — operating margin, net margin, net income, free cash flow, EPS — and the
# eval suite never caught it because all 15 cases ask about one profitable
# company.
#
# The lookbehind is what keeps the sign from being stolen out of a range or a
# span of years: in "2024-2025" and "quarters 8-12" the hyphen follows a word
# character and is not a minus. `)` is excluded too, so ")-3" does not read as
# negative three, while "(-0.25)" still does.
NUM = re.compile(
    r"(?<![\w)])-?\$?\d[\d,]*(?:\.\d+)?\s*(?:%|(?:billion|bn|b|million|m|k)\b)?",
    re.I)
CITE = re.compile(r"\[\[fact:(\d+)\]\]")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Prose dates, stripped for the same reason as ISO ones: the day-of-month is
# not a claim about a filing. Years and single digits are already ignored
# below, but a two-digit day is not — so "period ending January 31 2026"
# reported `31` as a figure tracing to no fact, and every answer from a model
# that writes dates in words came back dirty. Which models do is not something
# the prompt reliably controls, so the guard has to.
_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
          r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
          r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_YEAR = r"(?:19|20)\d{2}"
# Splitting on ".!?" alone cut "ended Jan. 31, 2026" in half, which put the
# day in a sentence of its own where no date pattern could reach it. The
# lookbehinds keep abbreviated months attached to what follows. Every branch
# is four characters, which is what Python's fixed-width lookbehind requires;
# "Sept." needs its own.
SENTENCE = re.compile(
    r"(?<=[.!?])"
    r"(?<!(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.)(?<!Sept\.)"
    r"\s+")

PROSE_DATE = re.compile("|".join([
    rf"\b{_MONTH}\s+{_DAY},?\s+{_YEAR}",              # January 31, 2026
    rf"\b{_DAY}\s+{_MONTH},?\s+{_YEAR}",              # 31 January 2026
    rf"\b{_MONTH}\s+{_YEAR}",                         # January 2026
    # The bare "Month DD" form carries a risk the others do not: "may 5%" is
    # English, not a date, and swallowing it would hide a fabricated figure —
    # a false negative here is far worse than a false positive. Hence the
    # lookahead. Its `\b` applies to the word-suffixes only: there is no word
    # boundary between "%" and the space after it, so a trailing \b would let
    # the lookahead pass on "may 5%" and strip the figure it exists to keep.
    rf"\b{_MONTH}\s+{_DAY}\b(?!\s*(?:%|(?:bn|b|billion|m|million|k)\b))",
]), re.I)

# Models do not restrict themselves to ASCII punctuation. gpt-oss-120b writes
# ISO dates with U+2011 non-breaking hyphens ("2026‑01‑31"), which the DATE
# pattern above does not match — so the date survived stripping and its parts
# were reported as figures tracing to no fact. Normalising the dashes is
# cheaper and less brittle than teaching every pattern about seven code
# points, and it only affects what the guard reads, never what the user sees.
DASHES = dict.fromkeys(
    map(ord, "‐‑‒–—―−"), "-")

SCALES = {"b": 1e9, "bn": 1e9, "billion": 1e9, "m": 1e6, "million": 1e6, "k": 1e3}


def interpretations(tok: str) -> list[float]:
    """A token can mean several things. Accept if ANY reading matches.

    The sign is read where the model wrote one and left open where it did not,
    which is not symmetry for its own sake:

    A figure written "-0.0238" is a claim that the period was a loss, and it
    should match a fact worth -0.0238 and *not* one worth 0.0238. Writing the
    wrong sign turns a loss into a profit, which is exactly the class of error
    this module exists to catch.

    A figure written "0.0238" carries no such claim — "a loss of 2.4%" is how
    the number is normally written in prose, with the sign in the word. So an
    unsigned token is allowed to match either, on the same reasoning that lets
    it match 0.739 or 73.9% or $45,079M: the token is ambiguous and any
    reading of it may be the intended one.
    """
    t = tok.strip().replace("$", "").replace(",", "")
    m = re.match(r"^(-?)(\d*\.?\d+)\s*(%|bn|b|billion|m|million|k)?$", t, re.I)
    if not m:
        return []
    signed = m.group(1) == "-"
    sign = -1.0 if signed else 1.0
    v, suf = float(m.group(2)), (m.group(3) or "").lower()

    if suf == "%":
        base = [v / 100, v]
    elif suf in SCALES:
        base = [v * SCALES[suf]]
    else:
        base = [v, v / 100, v * 1e6, v * 1e9]

    out = [sign * c for c in base]
    if not signed:
        out += [-c for c in base]
    return out


def check(report: str, allowed: dict[int, float]) -> list[dict]:
    """Return unsupported claims. Empty list means clean."""
    values = list(allowed.values())
    problems = []
    report = report.translate(DASHES)

    for sent in SENTENCE.split(report):
        cited = [int(i) for i in CITE.findall(sent)]
        stripped = PROSE_DATE.sub("", DATE.sub("", CITE.sub("", sent)))
        for tok in NUM.findall(stripped):
            raw = tok.strip()
            if not raw or raw in {".", "$"}:
                continue
            # ignore years and bare quarter counts
            # The sign comes off before these two: "-2025" is still a year and
            # "-3" is still a bare count, and leaving the minus on would send
            # both down the matching path they are excluded from.
            bare = raw.replace("$", "").replace(",", "").lstrip("-")
            if re.fullmatch(r"(19|20)\d{2}", bare) or re.fullmatch(r"[1-9]", bare):
                continue
            cands = interpretations(raw)
            if not cands:
                continue
            ok = any(
                abs(c - v) <= max(abs(v) * 0.005, 1e-9)
                for c in cands for v in values
            )
            if not ok:
                problems.append({"claim": raw, "sentence": sent.strip()[:120],
                                 "reason": "no matching fact"})
            elif not cited:
                problems.append({"claim": raw, "sentence": sent.strip()[:120],
                                 "reason": "uncited"})
    return problems
