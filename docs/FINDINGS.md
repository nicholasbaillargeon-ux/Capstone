# Skeleton findings — day 2

The thread runs end to end. Everything below was learned by building it, not
by planning it.

## What works

```
[agent] MCP tools: ['fd_get_concept', 'fd_compute_metric']
[agent] -> fd_compute_metric({'ticker': 'NVDA', 'metric': 'gross_margin', 'quarters': 8})
[series] Revenues -> RevenueFromContractWithCustomerExcludingAssessedTax (9 facts)
[series] derived Q4 ending 2026-01-25: 45,079,000,000
[series] GrossProfit: 8 quarters (2 derived, 1 restated)
[vault] top-3 scores: [0.656, 0.638]
[guard] 1 problem(s) in first draft
[guard]   '81.2%': no matching fact
[guard] 0 problem(s) after one repair
```

Retrieval, generation, tool-calling, and the guard are one connected thread.
The guard caught a fabricated figure planted in the draft and the single repair
round removed it. That is the core thesis of the project, demonstrated.

## Four things the spec did not know

**1. stdout is the wire.** An MCP stdio server speaks JSON-RPC on stdout. Every
`print()` inside the server corrupts the protocol — this showed up as a pydantic
`json_invalid` on the client side, mid-stream, with the actual log line visible in
the error. All server-side logging now goes to stderr. This would have been a
miserable bug to find later, and it is a hard constraint on every module the
server imports, not just the server file.

**2. `fp` describes the filing, not the fact.** Filtering quarterly data by
`fp == "Q3"` does not work. The field describes the period of the *filing* that
carried the fact, and a single 10-Q carries 3-month, 9-month, and prior-year
comparative facts all tagged the same way. Periods must be classified from
`start`/`end` day-counts. The 9-month cumulative in the fixture is silently
dropped by exactly this rule — without it, revenue would have been double-counted.

**3. Q4 does not exist.** Companies file three 10-Qs and a 10-K. There is no Q4
fact anywhere in the data; it must be derived as `FY - (Q1+Q2+Q3)`. In an
eight-quarter series, **two of the eight quarters are computed, not filed.** The
spec asked for eight quarters as though they were all retrievable. They are not.

**4. The guard's first false positive was dates.** `2024-04-28` tokenizes into
`2024`, `04`, `28`. Years were already excluded; `04` was not, and it was flagged
as an unsupported claim. ISO dates are now masked before numeral scanning. This
is the confirmation that Open Question 1 was correctly identified as the risky
part — the fix was five minutes, but there will be more of these, and each one is
a correct report rejected.

## Open Question 1: resolved

**Both, not either.** Citation markers alone fail because the model will place a
valid `[[fact:N]]` next to a wrong number. Numeric matching alone fails because
there is no way to know which fact a figure was meant to come from. The guard now
requires a citation in the sentence *and* independently verifies the numeral
against the fact table, accepting several readings of each token (`$45,079M`,
`73.9%`, `0.739`) with 0.5% tolerance for rounding.

Caveat: the model's willingness to emit the markers was faked by the stub. That
is the untested half.

## New problem the spec should own

**Derived quarters have borrowed provenance.** A derived Q4 currently inherits the
10-K's accession number. That is misleading — the number appears in no filing. It
needs its own provenance shape: the inputs it was computed from, not a document
reference it doesn't have.

## Most likely to break under real conditions

**The tool-call step.** Ranked, with reasoning:

1. **Tool calling from the real 7B model** — the only stochastic link left. The
   stub hard-coded a correct call with correct arguments. Everything downstream
   of it is deterministic and now proven; everything upstream is one model
   deciding whether to call `fd_compute_metric` or invent a number. It is also
   the piece with the least recovery path: a wrong tool call produces a confident
   answer from the wrong data, and the guard cannot catch that, because the
   numbers are real.
2. **Concept aliasing beyond NVDA** — one company was exercised. The alias list
   has three entries and no evidence behind it.
3. **Latency** — untested. Draft is a single ~400-token generation on CPU.

Wednesday's harness: run 20 questions against the real model, log every tool call
with its arguments, and count how many are schema-valid, name a real tool, *and*
carry the right ticker and metric. That last part is the one that matters and the
one a validity check alone would miss.

---

# Hardening findings — day 3

## The seam is hardened in three layers

Tuesday's conclusion was that tool-calling is the only stochastic link and that a
*wrong-but-valid* call is invisible to the grounding guard, because the numbers it
returns are real. `toolcall.py` now checks:

1. **Structural** — real tool name, parseable arguments.
2. **Schema** — required params present, types coerced (small models emit `"8"`
   for an integer constantly — coerce, don't reject), enums respected, unknown
   arguments dropped rather than fatal.
3. **Semantic** — does the call match the request that was actually made?
   `fd_compute_metric(ticker="AMD")` on an NVDA question is schema-perfect and
   produces a confidently wrong report. This is the layer that matters.

Rejections go back to the model as a JSON message with a `hint` and an
`instruction`, bounded at 3 per request. 21 tests cover it; `test_entity_mismatch_is_caught`
is the one worth reading.

## Two bugs the smoke test found that no unit test would have

**1. The MCP SDK does not pass the parent environment to the server subprocess.**
It starts from a minimal safe set, so `FD_ROOT` never arrived. Locally that meant
`ModuleNotFoundError`. **In the container it would have been far worse:** the
server would have silently opened an empty database at a default path, every
question would have returned zero facts, and the healthcheck — which only checks
the *API* process's view of the volume — would have reported `ready:true` while
every answer was a refusal. Environment is now passed explicitly via `PASS_ENV`.

**2. The semantic entity check rejected legitimate calls.** It compared the
ticker against the *question text*, but the smoke test asks about AMD with the
ticker in the request field and no mention in the question. Every call was
rejected. Expected tickers now come from the request field *and* the question.
Regression test: `test_ticker_from_request_field_is_accepted`.

Both were found by running five real questions rather than one. That is the
entire argument for the smoke test existing.

## Smoke results

```
  1. NVDA  PASS    24 facts
  2. NVDA  PASS    8 facts
  3. NVDA  PASS    24 facts
  4. NVDA  REFUSED No facts could be retrieved for this question.
  5. AMD   REFUSED No facts could be retrieved for this question.
  {'PASS': 3, 'REFUSED': 2}
```

**The two refusals are the correct answers.** Question 4 asks about operating
expenses, for which no tool and no data exist; question 5 asks about a company
that was never seeded. Both refused rather than answering from nothing. The
guard also fired on all three passing questions — a fabricated figure was planted
in each draft, caught, and removed by the single repair round:

```
{"event":"guard","unsupported":1,"repaired":false,"claims":["81.2%"]}
{"event":"guard","unsupported":0,"repaired":true,"claims":[]}
```

## Worst remaining failure — fix tomorrow

**Question 4 refuses for the wrong reason.** The tool returned
`no data for concept OperatingExpenses`, and the model accepted that and stopped.
But "no tool covers this" and "this company never reported that" are different
situations with different correct responses, and the user sees the same flat
refusal for both. Right now a question about a concept outside the hardcoded
alias map is indistinguishable from a question about a company with no filings.

That is tomorrow's job: `fd_list_concepts` so the model can discover what is
actually available, and a refusal message that says which of the two happened.

## Still not tested

Everything above ran against `stub.py`. **The real model has still never been in
the loop.** The seam is now hardened against failures I predicted; whether
`qwen2.5:7b` produces those specific failures, or different ones, is unknown until
`docker compose exec filingdesk python -m filingdesk.smoke` runs without `--stub`
on the homelab. The image itself has also never been built — there is no Docker
daemon where this was written.

---

# Eval findings — demo day

## Baseline: 9/15

Fifteen cases — six happy paths, five edge cases, four the system must refuse.
The automated check is a single function: a report passes only if every figure
traces to a retrieved fact **and** the report actually cites facts; a refusal
passes only if it refuses for the *right reason*. "It broke" and "that's out of
scope" are different answers to the user.

Six failures, and they collapsed into two root causes rather than six bugs.

## The worst failure: it answered "Should I buy NVDA stock?"

R1, R2 and R3 all produced reports. Investment advice, market data, and
forecasting are explicit non-goals in the spec, and the spec said the *prompt*
would refuse them. That was wishful. A system prompt is a request, and a 4-bit 7B
model is not where a policy boundary belongs.

**Fix: `policy.py`, a deterministic scope gate that runs before any inference.**
Same reasoning as the grounding guard — if something must not happen, don't ask
the model nicely. Refusals are typed and carry a message that says what to ask
instead.

The tests for it are mostly about **false positives**, not catches. A gate that
blocks a legitimate question is worse than no gate, because the user just sees a
refusal and assumes the tool is broken. Eight real questions are asserted to pass
through, plus every report case in the eval set.

## The second cause: it knew things it didn't say

E1, E2 and R4 were one problem wearing three hats — the system had the
information and didn't surface it. A quarter was computed rather than filed; a
period had been restated; a company had no data loaded rather than some generic
failure.

**Fix: `provenance.py` and typed refusals.** The disclosure is generated in code
from the fact flags, not requested in a prompt, for the same reason as above. And
`fd_list_concepts` was added so "no tool covers this" and "this company never
reported that" can finally be distinguished — the open failure carried over from
hardening day.

## Delta: 9/15 → 15/15

`evals/results/before-fix-*.json` and `after-fix-*.json` are both committed.

## The eval caught my eval

Worth recording because it is the most useful thing that happened today.

After the fix, E1 and E2 passed — and the report body said **"No facts were
provided."** The draft was empty. It passed because an empty report has zero
ungrounded figures, and the words the case required ("derived", "restated") were
being supplied by the deterministic provenance footer rather than by the answer.

Two bugs, one visible:

1. The stub lost its fact list on the repair pass, because the repair prompt
   carries the draft but not the FACTS block.
2. **The automated check was too weak.** It verified that nothing was wrong
   rather than that anything was right.

The check now requires a report to cite at least one fact and rejects citation
indices outside the fact table. A test that only proves the absence of a specific
failure will eventually pass on rubble.

## Still not tested

Everything here ran against `stub.py`. **The real model has still never been in
the loop.** The scope gate, the provenance note, the typed refusals and the
grounding guard are all deterministic and will behave identically with a real
model behind them — but tool-call reliability, citation emission, and latency are
all unmeasured until `python -m evals.run_evals` runs without `--stub` on the
homelab.

That run is the last thing standing between this and an honest demo.
