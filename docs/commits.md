# Commit plan — skeleton day

Feature branch, small commits, rebase later. Seven commits, each one a working
or at least coherent state.

```bash
git checkout -b skeleton/walking-path
```

| # | Files | Message |
|---|---|---|
| 1 | `src/filingdesk/{__init__,config,seed}.py` | `feat(seed): pull one company's XBRL facts from SEC into DuckDB` |
| 2 | `src/filingdesk/series.py` | `feat(series): quarterly reconstruction — dedupe, drop cumulatives, derive Q4` |
| 3 | `src/filingdesk/mcp_server.py` | `feat(mcp): filingdesk-mcp over stdio with fd_get_concept and fd_compute_metric` |
| 4 | `src/filingdesk/{llm,vault}.py` | `feat(vault): chunk, embed via ollama, cosine retrieve` |
| 5 | `src/filingdesk/{guard,prompts}.py` | `feat(guard): citation markers plus independent numeric verification` |
| 6 | `src/filingdesk/{agent,stub}.py` | `feat(agent): end-to-end loop, plus offline stub so the thread is provable` |
| 7 | `src/filingdesk/series.py`, `src/filingdesk/agent.py` | `fix(mcp): log to stderr — stdout is the JSON-RPC wire` |
| 8 | `spec.md`, `docs/FINDINGS.md` | `docs: amend spec to Rev 4 with what the skeleton disproved` |

Commit 7 is worth keeping separate rather than squashing into 3. It is the
non-obvious bug of the day and the commit message is the note to future-you.

## Sticky notes — cleanup deferred, not forgotten

- `harvest()` in `agent.py` dedupes facts with an O(n²) scan and a tuple key. Fine
  at 24 facts, not fine later.
- `config.TICKERS` is a hardcoded dict of three. Becomes `fd_resolve_company`.
- `vault.py` loads every embedding into memory and does a full numpy scan. That is
  the fallback path; wire `sqlite-vec` properly once the corpus is real.
- No streaming. The draft call blocks for the full generation.
- `guard.py` splits sentences on `[.!?]` — will mis-split on `$1.5M` mid-sentence.
- Fact ids are positional. If harvest order changes, every citation in a cached
  report silently means something else.

---

# Commit plan — hardening day

```bash
git checkout -b harden/deploy
```

| # | Files | Message |
|---|---|---|
| 1 | `requirements.txt`, `Dockerfile`, `.dockerignore` | `feat(docker): multi-stage image, non-root uid 10001, data-aware healthcheck` |
| 2 | `compose.yaml`, `.env.example`, `.gitignore` | `feat(compose): named volume for filings, index, history and eval; journald driver` |
| 3 | `src/filingdesk/logging_setup.py`, `history.py` | `feat(obs): structlog JSON with per-request trace_id; persist reports to the volume` |
| 4 | `src/filingdesk/agent.py` | `refactor(agent): return a result instead of printing; log every stage` |
| 5 | `src/filingdesk/api.py` | `feat(api): /api/report, /api/health reporting data freshness, /api/history` |
| 6 | `src/filingdesk/toolcall.py`, `tests/test_toolcall.py` | `feat(seam): three-layer tool-call validation incl. semantic entity check` |
| 7 | `src/filingdesk/agent.py` | `fix(mcp): pass FD_ROOT explicitly — SDK does not inherit parent env` |
| 8 | `src/filingdesk/agent.py`, `toolcall.py`, `tests/` | `fix(seam): accept ticker from the request field, not just the question text` |
| 9 | `deploy/`, `docs/DEPLOY.md`, `docs/questions.md`, `src/filingdesk/smoke.py` | `feat(deploy): Caddy snippet, systemd unit, reboot verification, smoke runner` |
| 10 | `spec.md`, `docs/FINDINGS.md` | `docs: amend spec to Rev 5 with deployment reality` |

Commits 7 and 8 stay separate and stay in the history. Both were found by the
smoke test rather than by a unit test, and the messages are the record of why the
tests that now cover them exist.

---

# Commit plan — eval and demo day

```bash
git checkout -b evals/harness
```

| # | Files | Message |
|---|---|---|
| 1 | `evals/cases.py`, `evals/run_evals.py`, `evals/__init__.py` | `feat(evals): 15-case harness — 6 happy, 5 edge, 4 must-refuse` |
| 2 | `evals/results/before-fix-*` | `test(evals): baseline 9/15` |
| 3 | `src/filingdesk/agent.py` | `fix(mcp): derive subprocess PYTHONPATH from package location` |
| 4 | `src/filingdesk/policy.py`, `tests/test_policy_and_provenance.py` | `feat(policy): refuse advice, market data and forecasts before inference` |
| 5 | `src/filingdesk/provenance.py`, `agent.py` | `feat(provenance): disclose derived and restated figures in code` |
| 6 | `src/filingdesk/mcp_server.py`, `series.py`, `agent.py` | `feat(mcp): typed tool errors and fd_list_concepts; typed refusals` |
| 7 | `src/filingdesk/stub.py`, `evals/run_evals.py` | `fix(evals): reject reports that cite no facts — the check passed an empty draft` |
| 8 | `evals/results/after-fix-*` | `test(evals): 15/15 after scope gate and provenance` |
| 9 | `src/filingdesk/agent.py` | `feat(cli): readable reports and refusals, --ticker, --json, --help` |
| 10 | `README.md`, `docs/DEMO.md` | `docs: rewrite README with limitations; add 20-minute demo runbook` |

Commit 7 stays in the history on its own. It is the day's most useful finding and
the message is why the check now looks the way it does.
