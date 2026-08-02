# Spec review log

Two adversarial passes were run against the spec before any code. Both were run by
Claude in a fresh context, prompted to attack the spec rather than improve it.

> **Caveat, stated plainly:** this is not a substitute for a human peer review, and it
> is not independent — the same model wrote the spec. It catches structural weakness
> and vagueness. It will not catch "you've misunderstood how that line item is
> constructed." Get a human on Rev 3 before writing the orchestrator. The prompt at the
> bottom of this file is the one to hand them, or to run in a fresh Claude Code session.

---

## Pass 1 — Rev 1 → Rev 2

**Finding 1 (blocking): success criteria were unfalsifiable.**
Rev 1 said reports should be "accurate and useful." That cannot fail, so it cannot pass.
Replaced with eight numbered criteria, each with a threshold and a measurement method,
and criterion 1 designated project-fatal.

**Finding 2 (blocking): the spec had no criterion for the actual hard part.**
Rev 1 treated XBRL as a data source rather than a problem. Tag heterogeneity across
filers is the thing most likely to make this project quietly wrong, and nothing measured
it. Added criterion 2 (concept resolution ≥ 90% on a deliberately adversarial set) and
criterion 3 (restatement handling, 100% on 10 cases).

**Finding 3: the model was being asked to compute ratios.**
Rev 1 handed raw facts to the model and asked for margins. Near-miss arithmetic is worse
than obvious error because it passes casual review. Ratios moved into Python; the model
now selects a metric rather than computing one. Propagated into ADR-0001 and the
`derived` block of the response schema.

**Finding 4: only one user acknowledged.**
The repo is also read by people who will never run it, and that reader shapes what gets
written. Added as secondary user.

**Finding 5: "not investment advice" was missing entirely.**
Not a legal reflex — a scope decision. Without it, the natural next feature is a
valuation opinion, which would break the provenance model that justifies the whole
design. Added as the first non-goal, with the reason.

## Pass 2 — Rev 2 → Rev 3

**Finding 6 (blocking): the repair loop was unbounded.**
Rev 2 said failed grounding checks "trigger a repair." Retrying until the guard passes
optimizes for satisfying the check rather than being correct — the exact failure the
check exists to prevent. Capped at exactly one repair; a second failure surfaces with
claims struck and flagged.

**Finding 7 (blocking): claim matching was hand-waved.**
Rev 2 assumed "check every figure against `facts`" was a solved implementation detail.
It isn't — `$26.7B` in prose against `26670000000` in facts, plus dates, CIKs, and
percentages. This is criterion 1, the project-fatal one. Promoted to Open Question 1
with two candidate designs and a **spike required before the orchestrator is written**.

**Finding 8: dimensional axes weren't mentioned at all.**
Facts reported both as consolidated totals and as segment breakdowns share a concept tag
and are distinguished only by XBRL dimensions. Querying by concept alone silently
double-counts. This would have been discovered at the worst possible time. Added as
Open Question 3.

**Finding 9: the SEC rate limit was noted but not designed around.**
Rev 2 mentioned 10 req/s in passing while describing per-company API calls. Rewrote the
data constraint around the bulk archive with incremental refresh, and made "no network
call during a request" explicit — which also makes results reproducible.

**Finding 10: latency criterion measured the wrong thing.**
p95 end-to-end alone permits a UI that shows nothing for 80 seconds. Added a first-token
target and a requirement that the UI stream per-stage status. Slow is fine on CPU;
silent is not.

**Finding 11 (candidates.md): C5 was dismissed without a reason.**
Macro Desk isn't a bad idea, it's C1 with the hard part removed. Recorded explicitly so
the option stays recoverable rather than lost.

---

## Prompt for an independent review

Run in a fresh session, or hand to a human:

```
Read spec.md and the two ADRs. Do not suggest improvements yet.

First, answer these:
1. What is the single most likely reason this project ships late or not at all?
2. Which success criterion is most likely to be quietly dropped when it gets hard?
3. Where does the spec assume something is easy that isn't?
4. Which non-goal will I break first, and what will the excuse be?

Then, and only then, tell me what you'd change.
```
