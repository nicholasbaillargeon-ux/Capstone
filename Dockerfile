# ---- build ------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- runtime ----------------------------------------------------------
FROM python:3.12-slim

# Non-root. Fixed uid so the volume's ownership is stable across rebuilds.
RUN groupadd --system --gid 10001 filing \
 && useradd --system --uid 10001 --gid filing --no-create-home filing \
 && install -d -o filing -g filing /data

COPY --from=builder /opt/venv /opt/venv

# PYTHONPATH is not optional. `--app-dir src` only fixes sys.path for the
# uvicorn process; `docker compose exec ... python -m filingdesk.seed` and the
# MCP server subprocess the agent spawns are fresh interpreters that would
# otherwise fail with ModuleNotFoundError.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FD_ROOT=/data \
    FD_LOG_FORMAT=json

WORKDIR /app
COPY --chown=filing:filing src/ /app/src/
COPY --chown=filing:filing docs/questions.md /app/docs/questions.md

USER filing
EXPOSE 8000

# No curl in slim, and we are not installing one just for this. The check is
# real: it fails while the data volume is empty, so a container with no facts
# never goes healthy and never gets traffic from Caddy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,sys,urllib.request; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4)); \
sys.exit(0 if d.get('ready') else 1)"

CMD ["uvicorn", "filingdesk.api:app", "--app-dir", "src", \
     "--host", "0.0.0.0", "--port", "8000"]
