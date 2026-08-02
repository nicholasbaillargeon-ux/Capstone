# Walking skeleton

Ugly on purpose. Print statements on purpose. No tests on purpose.

## Prove the wiring with no Ollama and no network

```bash
python -m venv .venv && . .venv/bin/activate
pip install duckdb mcp sqlite-vec numpy requests
cd src
python -m filingdesk.agent --stub "how has gross margin moved over 8 quarters?"
```

Stub mode seeds synthetic facts, fakes the model, and runs the real MCP server
over real stdio. Everything except the LLM and the SEC is genuine.

## Real run

```bash
export SEC_USER_AGENT='FilingDesk/0.1 (you@example.com)'   # SEC requires this
ollama pull qwen2.5:7b-instruct-q4_K_M
ollama pull nomic-embed-text

cd src
python -m filingdesk.seed NVDA
python -c "from filingdesk import vault; vault.index('$HOME/brain')"
python -m filingdesk.agent "how has gross margin moved over 8 quarters?"
```

## Files

| | |
|---|---|
| `seed.py` | SEC companyfacts → DuckDB |
| `series.py` | dedupe, drop non-quarters, derive Q4. The hard part. |
| `mcp_server.py` | filingdesk-mcp, stdio, 2 tools |
| `vault.py` | chunk, embed, retrieve |
| `guard.py` | citation + numeric verification |
| `agent.py` | the loop |
| `stub.py` | offline fixtures |
