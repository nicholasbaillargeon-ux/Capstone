# ADR-0001 — Two-lane grounding

**Status:** Accepted · **Date:** 2026-07-29 · **Relates to:** spec criteria 1, 4; Open Question 1

> Decision taken at `spec-locked`; written up after the walking skeleton, so the
> "What building it changed" section is evidence rather than prediction.

## Context

Ask a general-purpose LLM for a company's gross margin and it returns a plausible,
confidently-wrong figure. In financial work that is worse than no answer: a fabricated
number survives review because it looks exactly like a real one, and it contaminates
everything computed from it downstream.

Prompting does not fix this. "Never state a number you did not retrieve" is an
instruction the model follows most of the time, and *most of the time* is the failure
mode — an error rate low enough to stop being checked is more dangerous than one high
enough to be obvious.

The project therefore needs a structural property, not a behavioural one: it must be
**impossible** for a figure to reach the report without a filing behind it, whatever the
model does.

## Decision

**Quantities and judgement travel in separate lanes, and the model is only trusted with
one of them.**

**Lane 1 — quantities.** Figures come from typed MCP tool calls against a normalized
local cache of SEC XBRL data. Every fact carries its accession number, form, period, and
a `restated` flag. Ratios are computed **in Python** and return their formula and input
fact ids. The model *selects* a metric; it never *computes* one. (Per spec-review
Finding 3: near-miss arithmetic is worse than obvious error, because it passes casual
review.)

**Lane 2 — judgement.** Analytical framing — which margin definition, what counts as a
one-off, how to treat a restatement — comes from vector search over my own notes. The
vault contributes prose only. It is not a source of figures and is prompted as such.

**The seam.** A deterministic guard sits between the draft and the response. It verifies
that every numeral in the report traces to a retrieved fact. Anything that doesn't is
struck and flagged rather than shipped. The repair loop runs **at most once** — looping
until the guard passes would optimize for satisfying the check rather than being
correct, which is the exact failure the check exists to prevent.

### The model selection follows from this

Because the model never produces a figure and never does arithmetic, what it must
actually do is narrow:

1. pick the right tool with the right arguments, and
2. write four sentences of prose that cite fact ids.

That is a tool-calling and instruction-following job, not a reasoning or numeracy job —
and that is what makes CPU-only inference viable. A larger model would buy better
reasoning about numbers, which is precisely the capability this design refuses to
depend on.

Hence `qwen2.5:7b-instruct-q4_K_M` via Ollama: Apache 2.0, tool-calling reliable at a
size CPU inference can serve, quantized to fit the homelab's binding constraint.
Embeddings: `nomic-embed-text`, same runtime.

Open Question 5 (is 3B enough to halve latency?) remains open and is now cheap to test,
because criterion 4 measures exactly the capability that matters.

## Alternatives considered

**Prompt-only grounding.** Instruct the model not to fabricate and trust it. Rejected:
this is the industry default and it is why the problem exists. No structural guarantee,
and the failure is silent.

**A second LLM as fact-checker.** Have a model verify the first model's numbers.
Rejected: it replaces a deterministic check with a stochastic one and doubles CPU
latency. A checker that is wrong 5% of the time cannot certify a claim that must be
right 100% of the time.

**RAG over filing text.** Retrieve 10-Q prose and let the model read figures out of it.
Rejected: it reintroduces transcription as a model task, and loses the typed provenance
that makes a claim checkable in one click. XBRL already has the numbers as data — using
the narrative instead throws that away.

**One lane, notes only.** Drop the tool layer and treat filings as another corpus.
Rejected: it collapses the distinction the whole project rests on. My notes are opinion;
filings are record. Retrieving them the same way makes them indistinguishable at the
point of use.

## Consequences

**Accepted, positive.**
- Every figure is checkable against a source document by accession number. The `facts`
  and `derived` blocks in the response are the audit trail, not decoration.
- Ratios are reproducible: same inputs, same formula, same answer, no temperature.
- The design is honest about not knowing. When no tool covers a question, the correct
  output is a refusal — and the smoke set includes two questions that must refuse.

**Accepted, negative.**
- **Tool-calling becomes the only stochastic link left**, and a wrong-but-valid call is
  invisible to the guard because the numbers it returns are real.
  `fd_compute_metric(ticker="AMD")` on an NVDA question is schema-perfect and produces a
  confidently wrong report. This is now hardened in three layers — structural, schema,
  semantic — in `toolcall.py`.
- Guard false positives are correct reports rejected. Each one is a real cost, and there
  will be more of them than the first version suggests.
- The model cannot do anything clever with a number it wasn't handed. That is the point,
  but it does mean capability is bounded by tool coverage rather than by the model.

## What building it changed

**Open Question 1 is resolved: both, not either.** Citation markers alone fail — the
model will place a valid `[[fact:N]]` next to a wrong number. Numeric matching alone
fails — there is no way to know which fact a figure was meant to come from. The guard
now requires a marker in the sentence *and* independently verifies the numeral against
the fact table, accepting several readings of each token (`$45,079M`, `73.9%`, `0.739`)
with 0.5% tolerance for rounding.

**The first false positive was dates.** `2024-04-28` tokenizes into `2024`, `04`, `28`.
Years were already excluded; `04` was not, and a correct report was flagged. ISO dates
are masked before numeral scanning.

**Derived quarters have borrowed provenance — unresolved.** Q4 does not exist as a filed
fact and must be derived as `FY - (Q1+Q2+Q3)`. The derived fact currently inherits the
10-K's accession number, which is misleading: the number appears in no filing. It needs
its own provenance shape — the inputs it was computed from, not a document reference it
doesn't have. This is a gap in *this* ADR's model of provenance and should be amended
before v1.

**The untested half.** Everything above was proved against `stub.py`. Whether the real
7B model emits the markers reliably is unmeasured — see `docs/FINDINGS.md`.
