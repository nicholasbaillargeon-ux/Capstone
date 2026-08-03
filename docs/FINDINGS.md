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

---

# Latency findings — the five things worth trying

Five ways to make the answer path faster, each implemented, each measured
against the eval suite on its own, each kept or thrown away on the number it
produced. Two worked, one was already true, one is somebody else's flag, and
one made the app worse in a way the eval suite could not see.

Every run below is 15 cases against gpt-oss-120b on the homelab proxy, at
`FD_REASONING_EFFORT=medium`, changing one thing at a time. Medians and worst
cases are over the 12 cases that produce an answer; the 3 refusals never reach
a model. Reproduce with `python -m evals.compare`.

| | pass | median | worst | planning | draft | blank screen |
|---|---|---|---|---|---|---|
| baseline | 15/15 | 29.3s | 108.2s | 14.2s | 17.1s | 17.1s |
| + streamed draft | 15/15 | 29.4s | 108.3s | 14.5s | 17.2s | **14.5s** |
| + stop on first facts | 15/15 | **20.0s** | **60.4s** | **4.9s** | 17.2s | 14.5s |
| + trimmed fact table | **14/15** | 23.4s | 42.1s | 5.0s | 15.1s | 13.6s |
| shipped, confirmed | 15/15 | 20.0s | 38.2s | 4.9s | 17.2s | 12.9s |

The fourth row is not cumulative. It was measured, rejected, and left off; the
last row is the suite re-run against what actually ships. **A third off the
median question, two thirds off the worst, a quarter off the blank screen, and
the same 15 cases passing.** The draft call is untouched, because nothing here
could touch it — see below.

## The measurement that reframed the other four

`evals/probe_llm.py` asks the endpoint two questions the eval suite cannot.

**Prefill is not a cost here.** A 1400-word preamble reaches its first chunk
41 ms later than a four-word one. Getting any request in and out at all costs
518 ms. So the prompt could be ten times longer and it would not matter, and a
prefix cache — however perfect — cannot buy back time that is not being spent.

**Generation is the entire cost.** 44.7 tokens/s across the whole generation,
reasoning included. Every second in the table above is a second of tokens
coming out one at a time.

Which settles two arguments before they start. Anything that shortens the
prompt is worthless. Anything that removes a model call, or makes tokens come
out faster, is worth exactly what it removes.

## 1. Streaming the draft — kept, and honest about what it does

The draft call is last and longest, and until it returned the page had nothing
on it. It now arrives as it is written.

It changes no total, and it was never going to: the same tokens are generated
either way. What it changes is the blank screen, and by less than it would
against most models — **17.1s to 14.5s, a 15% cut** — because gpt-oss reasons
before it writes. First chunk of *any* kind arrives at 0.5s; first chunk of
*answer* at 14.5s of a 17.2s call. The model spends 85% of the call thinking.

The tail is where it pays: the worst case emits its first word 32 seconds
before it finishes (86.5s draft, first word at 54.6s).

Two properties this had to keep, both tested:

- **What is shown unchecked is labelled unchecked.** The partial draft has not
  been through the guard, so it renders with an "unchecked — no figure traced
  yet" banner, in a held-back colour, with citation markers left as raw
  `[[fact:N]]` rather than as traceable chips. A chip is a link to a verified
  fact and nothing there is verified yet.
- **The finished report replaces it in the same slot.** Both SSE events swap
  into one element, so the guarded answer looks like the draft settling rather
  than a second answer appearing below the first.

One bug found while building it, worth recording because it would have been
miserable to find later: the draft runs in a worker thread so the SSE heartbeat
keeps beating, which means `on_stage` is now called from two threads.
`asyncio.Queue.put_nowait` is not safe from the second one. Hopping every event
onto the loop with `call_soon_threadsafe` is not the fix either — it reorders
the finished report ahead of the stage frames that preceded it. The fix is to
be direct on the loop and scheduled off it, and the absence of a running loop
is the test for which you are.

## 2. Stopping the planning loop on first facts — kept, and it is the win

Already built as `FD_STOP_ON_FIRST_FACTS`, off pending evidence. The evidence:
**a third off the median request and nearly half off the worst, no case lost.**
Planning drops from 14.2s to 4.9s.

What it removes is a round trip whose entire content is the model declining to
call anything else. At 44.7 tokens/s, "I have what I need" is not free — it is
several seconds of reasoning tokens, once per question, with somebody watching.

The cost is real and this suite does not price it: a plan that genuinely needs
a second round of tool calls, decided after seeing the first round's results,
now gets one round. No case here is multi-hop. Revisit if one is ever added.
The failure mode is a thin answer rather than a wrong one — the guard still
checks every figure, and a fact that was never retrieved cannot be cited.

## 3. Prefix caching on the serving backend — nothing to ask for

The reasoning was that every request sends the same system prompt and the same
four tool schemas, and the planning loop resends its history each step, so most
of what is sent has been sent before.

All true, and all irrelevant: prefill is 41 ms. Withdrawn.

## 4. Trimming the fact table — rejected, and it is the interesting one

A plan asking for twenty quarters of a ratio returns sixty facts — the ratio
plus both filed inputs behind each — for a question naming two periods. Cap
what the DRAFT prompt carries; keep everything in Sources and everything in the
guard.

`agent.draft_table` does this about as carefully as it can be done. Row numbers
never move, so `[[fact:41]]` means the same fact trimmed or not. Each concept
keeps its earliest row and its most recent ones, because "compare the first and
last quarter" is an eval case and a `[-N:]` slice answers it with the wrong
quarter. The prompt says outright that the list was truncated and that no
maximum should be claimed over it.

At a cap of 12 it took two seconds off the draft, nothing off the median
request, and **H1 from pass to fail** — handed a shorter table the model started
restating the figures as percentages and wrote two that trace to nothing. The
guard struck them, which is the system working. The answer was still worse.

**And then the case that matters more passed.**

H5 asks which quarter had the highest gross margin. Untrimmed, the answer is
0.7835 and it is right. Trimmed, the answer is 0.7500 — cited, grounded,
checkable, and wrong, because the quarter that was actually highest had been
cut from the table before the model was asked. Every figure traced to a filing,
so the grounding check had nothing to say and the case passed.

That is this project's stated failure mode arriving through a latency knob, and
the harness waved it through. Two things follow.

**A note in a prompt is not a constraint.** The trimmed table says in plain
words that it is truncated and that a maximum must not be claimed over it. The
model claimed one anyway, in the same measured tone it uses when it is right.

**The check was too weak, again.** `auto_check` verified that every figure was
grounded, which is not the same as verifying that the right fact was cited.
Cases now take an optional `max_of`, and H5 uses it: the answer has to name the
largest retrieved value of that concept, not merely a real one. It fails with
the cap on and passes with it off, so the default cannot be turned on by
accident without the suite saying so.

This is the second time here that a check proving the absence of a specific
failure passed on rubble. It will not be the last.

The knob stays, defaulted off. It is the right lever for an endpoint with a
small context window, where the trade has to be made. It is not a lever here.

## 5. Speculative decoding — asked for, then measured, and withdrawn

**This section first said "roughly 6 seconds a question, more than anything
left in application code". That was a quoted industry range, not a
measurement, and measuring it turned it over.**

44.7 tokens/s is the binding constraint on every number in the table. A draft
model against a 120B typically returns 1.5–2.5x, and unlike anything else tried
here it would speed up the reasoning tokens too, which are 85% of the draft
call. So: ask the homelab to load a draft model. The proxy already serves
gpt-oss-20b, same family, same tokenizer — the obvious candidate, sitting right
there.

Then `probe_llm.py` grew a measurement of it, and gpt-oss-20b decodes at
**44.7 tokens/s. The same rate as the 120b, to within the noise, repeatedly.**

Speculative decoding rests on exactly one assumption: the draft is several
times cheaper per token than the target, so guessing k tokens and checking them
in one target pass beats generating k with the target. Every quoted speedup
assumes it. Priced against each other as served here, the draft costs 1.00
target-steps per token, and the arithmetic inverts:

| accept | k=3 | k=5 | k=8 |
|---|---|---|---|
| 50% | 0.47x | 0.33x | 0.22x |
| 70% | 0.63x | 0.49x | 0.36x |
| 90% | 0.86x | 0.78x | 0.68x |

Net negative everywhere in that range. Turning it on would make the app slower.

Three checks before believing that, because "two different models are exactly
the same speed" is the shape of a broken measurement:

- **It is not the transport.** mistral-small-3.2-vision on the same endpoint,
  same code path, decodes at 7.8 tokens/s with 124 ms between chunks against
  the gpt-oss pair's 23 ms. Nothing is pacing the stream.
- **It is not one model wearing two names.** The proxy echoes back whichever
  name was requested, so that proves nothing — but the completion ids come back
  `chatcmpl-2` and `chatcmpl-256`, two independent sequential counters, so
  there are two server processes.
- **It reproduces.** Three samples per model per run, several runs, 44.6–44.9
  every time.

The likely reading is that both are MoE with small active parameter counts
(~5.1B against ~3.6B) and that per-token time on this hardware is dominated by
something that does not scale with the model — in which case the 120b is
already generating about as fast as anything of its family will on that box.

What this cannot see: the 20b is measured as a *separately served endpoint*,
sharing whatever the target shares. Loaded as a draft inside the target's own
process it might be cheaper than it looks from out here. That is a measurement
to take on that host, not from this one — and it is the measurement to take
before changing any flag, rather than after.

So the recommendation is not "enable speculative decoding". It is: **measure
the draft model in isolation on the serving host first**, because from the
client side its central assumption is already failing.

## What is left

Everything cheap is done, and the one expensive thing left turned out not to be
worth doing on this hardware. The pipeline makes two model calls where it made
three and shows the second as it arrives; what remains is 44.7 tokens/s and
roughly 1,500 tokens of thinking and writing per question.

That is an inference problem, and the levers on it are all on the other machine
and all bigger than a flag: different hardware, a smaller model for the
planning call, or fewer reasoning tokens.

## 6. A smaller model for the planning call — no change, either way

`FD_PLAN_CHAT_MODEL` has existed since the split-effort experiment and had
never been measured. The case for it: choosing one tool from four is a smaller
job than transcribing figures without mangling them, and gpt-oss-20b is served
by the same proxy.

There is an earlier run of this — `plan-20b-20260802T215857Z`, 13/15 — but it
predates stop-on-first-facts, the second repair attempt, and the prompt change
that fixed E2's uncited negative, so it says nothing about the current code.
Re-run on HEAD, changing only the planning model:

| | pass | median | worst | planning |
|---|---|---|---|---|
| gpt-oss-120b plans | 15/15 | 19.7s | 37.8s | 4.9s |
| gpt-oss-20b plans | 15/15 | 19.8s | 37.7s | 4.8s |

Nothing. Not a tenth of a second, not a case.

Which is the same finding as §5 wearing different clothes, and it is the second
independent confirmation of it: the 20b is not faster than the 120b on this
hardware, so putting it anywhere in the pipeline changes nothing. If the two
models had the speed relationship their parameter counts imply, this table
would show it and §5's draft-model arithmetic would work. Neither does.

The result worth keeping is the negative one on **accuracy**: planning on a 20b
costs nothing either. All 15 cases still pass, the tool calls are still
well-formed, the entity check still catches nothing. So if the serving side
ever changes such that the 20b *is* cheaper, this switch is already known to be
safe — which is most of what an experiment like this is for.

Left at its default of "same model as the draft", because a knob that changes
neither speed nor accuracy should not be set.

## Where that leaves it

Six things tried, measured, and settled. Two worked, and both were about making
fewer or better-timed model calls. Four came to nothing, and three of those
four came to nothing for the same underlying reason: **on this hardware,
generation speed does not vary the way model size says it should.** Prefill is
free, the 20b is not faster than the 120b, and a draft model would cost as much
as the thing it is drafting for.

Everything the application can do about latency has now been done. What is left
is 44.7 tokens/s and roughly 1,500 tokens of thinking and writing per question,
and the only lever on that is the hardware the models run on.
