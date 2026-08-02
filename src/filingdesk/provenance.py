"""Deterministic disclosure of derived and restated figures.

The eval showed the system knew a quarter was computed rather than filed, and
did not say so. Telling the model to mention it would have been a prompt fix,
and prompt fixes are not guarantees. This appends the disclosure in code, so
it happens whether or not the model cooperates.
"""
from __future__ import annotations


def note(facts: list[dict]) -> str:
    derived = [f for f in facts if f.get("derived") and f.get("accn") != "computed"]
    computed = [f for f in facts if f.get("accn") == "computed"]
    restated = [f for f in facts if f.get("restated")]
    if not (derived or restated or computed):
        return ""

    lines = ["", "---", ""]
    if derived:
        ends = sorted({f["end"] for f in derived})
        lines.append(
            f"**Derived, not filed.** {len(ends)} quarter"
            f"{'s' if len(ends) > 1 else ''} in this answer "
            f"({', '.join(ends)}) appear in no filing. No company files a 10-Q "
            "for Q4, so it is computed as the fiscal year minus Q1–Q3.")
    if restated:
        ends = sorted({f["end"] for f in restated})
        lines.append(
            f"**Restated.** The period{'s' if len(ends) > 1 else ''} ending "
            f"{', '.join(ends)} {'were' if len(ends) > 1 else 'was'} reported "
            "more than once with different values. The most recently filed "
            "figure is used here.")
    if computed:
        lines.append(
            "**Ratios computed, not reported.** Margin figures are calculated "
            "in code from the cited inputs, not taken from a filing.")
    return "\n\n".join(lines)
