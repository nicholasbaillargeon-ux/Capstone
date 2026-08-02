# ADR-0002 — DuckDB for filings, sqlite-vec for notes

**Status:** Accepted · **Date:** 2026-07-29 · **Relates to:** spec criteria 2, 3, 6, 8

> Decision taken at `spec-locked`; written up after the walking skeleton, so the
> "What building it changed" section is evidence rather than prediction.

## Context

Two storage engines in a single-user homelab project looks like overengineering, and the
burden of proof is on the second one. It is here because ADR-0001's two lanes are not
two flavours of the same retrieval problem — they are different problems that happen to
sit in the same service.

**The facts lane** is analytical SQL over a columnar fact table. Filter ~10⁴–10⁶ rows by
`cik` and `concept`, order by period, dedupe by filed date, classify durations, and
aggregate. Answers must be exact and reproducible.

**The notes lane** is k-NN over 768-dimensional vectors across a corpus of a few hundred
chunks. Answers are approximate by nature; "close enough" is the whole contract.

The binding constraints are the same as everywhere else in this project: CPU-only, no
server process to babysit, and cold start reproducible on a clean machine (criterion 8).

## Decision

**`filings.duckdb` over Parquet for the facts cache. `vault.db` with `sqlite-vec` for
the note index. Both embedded, both single files on one volume, no database server
anywhere in the stack.**

DuckDB for facts, because the work is genuinely analytical. The quarterly reconstruction
in `series.py` — dedupe by exact period keeping the latest `filed`, drop cumulative
durations, derive Q4 as `FY - (Q1+Q2+Q3)` — is columnar scan-and-group work, which is
what DuckDB is built for. It is embedded, has no server, reads Parquet natively, and
handles the bulk `companyfacts.zip` seed without a loading pipeline.

sqlite-vec for notes, because the vault index is small, embedded, and needs to travel
with the same volume. SQLite is already the right shape for a few hundred rows of text
plus a blob.

**Both caches are disposable.** Neither is committed (`.gitignore`: "rebuildable from
EDGAR and Gitea, never committed"), and rebuilding is `seed` + `index`, both idempotent.
Rollback does not touch the volume — that is what makes the deploy runbook's rollback
step a two-line operation rather than a restore.

### The latency claim

**Storage is not the latency budget.** Criterion 6 (p95 ≤ 90s end to end) is dominated
by one CPU generation pass; the data layer should be a rounding error against it. This
is the claim to falsify from production logs, not to assume — hence the p95 one-liner in
`docs/deploy.md`, which reads `request.end` events straight out of journald.

Stub-mode measurements bound the data layer only: an MCP round trip — subprocess spawn,
handshake, one tool call, teardown — ran ~450–500 ms, of which the tool call itself was
26 ms; vault retrieval was under 1 ms. Both figures are against synthetic fixtures with
a two-chunk corpus and a faked model, so they say the storage choice is not the problem
and say nothing yet about the real request. Unverified against generation.

## Alternatives considered

**One engine: DuckDB for both.** Store embeddings in DuckDB and do cosine similarity in
SQL. Rejected: no mature ANN index, so every query is a full scan — which is what the
numpy fallback already does, without pretending to be an index. Nothing gained but a
column.

**One engine: SQLite for both.** Rejected on the facts side. SQLite is a row store; the
series reconstruction is exactly the scan-and-aggregate workload row stores are worst
at, and the bulk seed would need a real loading path.

**Postgres + pgvector.** The honest general-purpose answer, and it would work. Rejected
because it adds a server to run, back up, secure, and upgrade for a single-user
application whose deployment principle is that infrastructure is a solved problem and
the effort goes into the hard parts. It also breaks criterion 8: cold start stops being
`git clone` → documented commands.

**A dedicated vector DB (Qdrant, Chroma).** Rejected: another service and another
failure mode to buy ANN performance on a corpus small enough to scan exhaustively in
under a millisecond.

## Consequences

**Accepted, positive.**
- No database server in the stack. The container is the application plus one volume.
- Each lane is queried the way it wants to be queried, and neither compromises for the
  other.
- Provenance stays typed all the way down: facts keep `accn`, `form`, `filed`, and the
  exact concept tag as columns, not as JSON in a text blob.

**Accepted, negative.**
- **Two freshness stories.** `/api/health` reports `filings_refreshed` and
  `vault_indexed` separately, and `ready` requires both — a direct consequence of this
  decision, and the reason the healthcheck reports data freshness rather than returning
  200.
- Two seed paths, two idempotency arguments, two things that can be stale independently.
- `sqlite-vec` is a loadable extension, and not every Python build can load one.

## What building it changed

**The sqlite-vec fallback was needed immediately.** Some stock `python3` builds ship
without loadable-extension support. `vault.py` falls back to a full numpy scan and says
so on stderr. At this corpus size the fallback is not a compromise — it is under a
millisecond — but it means the `sqlite-vec` path is, as of the skeleton, the *untested*
one. Wiring it properly is deferred until the corpus is real.

**Storage was never the hard part; interpretation was.** The three problems that
consumed the day — the same period appearing in many filings, mixed durations under one
concept, and Q4 not existing — are all normalization problems, and none of them are
solved by choosing a better engine. `fp` describes the filing's period, not the fact's,
so quarters must be classified from `start`/`end` day counts. Two of any eight quarters
are computed rather than filed.

That is the argument for this ADR being short: the engines were the easy call, and the
effort went where the spec said it would.
