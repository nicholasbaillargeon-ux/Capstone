#!/usr/bin/env bash
# Start Filing Desk locally.
#
#   ./run.sh              # http://localhost:8088
#   ./run.sh 9000         # a different port
#   FD_STUB=1 ./run.sh    # synthetic fixtures, no network, no model
#
# Companies are fetched from EDGAR on first use, so nothing needs seeding first.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8088}"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# SEC requires a descriptive User-Agent carrying a real contact email, and
# refuses requests without one. .env is the intended home for it.
#
# Read rather than `source`: the User-Agent contains parentheses, which bash
# treats as syntax, and docker compose wants the value unquoted — so the file
# stays compose-shaped and this script parses it.
if [ -z "${SEC_USER_AGENT:-}" ] && [ -f .env ]; then
  while IFS= read -r line; do
    case "$line" in
      \#*|"") continue ;;
      *=*)
        key=${line%%=*}
        val=${line#*=}
        val=${val%\"}; val=${val#\"}
        val=${val%\'}; val=${val#\'}
        [ -n "${!key:-}" ] || export "$key=$val"
        ;;
    esac
  done < .env
fi
if [ -z "${SEC_USER_AGENT:-}" ]; then
  echo "SEC_USER_AGENT is not set." >&2
  echo "  cp .env.example .env && \$EDITOR .env" >&2
  echo "  (or: export SEC_USER_AGENT='FilingDesk/0.2 (you@example.com)')" >&2
  exit 1
fi

export SEC_USER_AGENT PYTHONPATH="${PYTHONPATH:-}${PYTHONPATH:+:}$PWD/src"

echo "Filing Desk  ->  http://localhost:${PORT}/"
exec "$PY" -m uvicorn filingdesk.api:app --host 0.0.0.0 --port "$PORT"
