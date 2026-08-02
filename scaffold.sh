#!/usr/bin/env bash
#
# scaffold.sh — initialise the capstone repo, push to Gitea, tag spec-locked.
#
# Run on the homelab (MobaXterm SSH session), from INSIDE the directory that
# already contains README.md, spec.md, and docs/.
#
#   chmod +x scaffold.sh
#   ./scaffold.sh
#
# Gitea repo creation is optional. Either create `capstone` in the Gitea web UI
# first, or export a token with repo:write scope:
#
#   read -rs GITEA_TOKEN && export GITEA_TOKEN     # -s so it stays out of ~/.bash_history
#   ./scaffold.sh
#
set -euo pipefail

GITEA_HOST="${GITEA_HOST:-gitea.baillargeon.casa}"
GITEA_USER="${GITEA_USER:-$(whoami)}"
REPO_NAME="${REPO_NAME:-capstone}"
TAG="${TAG:-spec-locked}"

log()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. preflight -----------------------------------------------------------
command -v git >/dev/null || die "git not found"
[[ -f spec.md   ]] || die "spec.md not found. Run this from the repo directory."
[[ -f README.md ]] || die "README.md not found. Run this from the repo directory."
[[ -d .git ]] && die ".git already exists — this script is for first init only."

# --- 1. structure -----------------------------------------------------------
log "Creating structure"
mkdir -p docs/adr docs/diagrams src
touch src/.gitkeep    # src/ is intentionally empty at spec-locked

for f in docs/adr/0001-two-lane-grounding.md \
         docs/adr/0002-duckdb-filings-cache.md \
         docs/diagrams/component.mmd \
         docs/diagrams/sequence.mmd \
         docs/spec-review.md \
         docs/candidates.md; do
  [[ -f "$f" ]] || warn "missing: $f"
done

# --- 2. git init ------------------------------------------------------------
log "Initialising repository"
git init -q -b main
git add -A

# Fail loudly if anything but the intended files is staged.
log "Staged files:"
git diff --cached --name-only | sed 's/^/    /'

git commit -q -m "docs: lock capstone spec, ADRs, and diagrams

Filing Desk: grounded analysis of SEC filings on local CPU inference.
Figures come from MCP tool calls against a normalized XBRL cache,
framing from vault RAG, ratios computed in Python, with a
deterministic guard at the seam.

src/ is intentionally empty. Implementation begins after this tag.

Refs: ADR-0001, ADR-0002"

log "Commit created: $(git rev-parse --short HEAD)"

# --- 3. create the Gitea repo (optional) ------------------------------------
if [[ -n "${GITEA_TOKEN:-}" ]]; then
  log "Creating ${REPO_NAME} on ${GITEA_HOST} via API"
  http_code=$(curl -sS -o /tmp/gitea_create.json -w '%{http_code}' \
    -X POST "https://${GITEA_HOST}/api/v1/user/repos" \
    -H "Authorization: token ${GITEA_TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"${REPO_NAME}\",\"private\":false,\"auto_init\":false,\"default_branch\":\"main\"}")

  case "$http_code" in
    201) log "Repository created" ;;
    409) warn "Repository already exists — continuing" ;;
    *)   die "Gitea API returned ${http_code}: $(cat /tmp/gitea_create.json)" ;;
  esac
  rm -f /tmp/gitea_create.json
else
  warn "GITEA_TOKEN not set — assuming ${REPO_NAME} already exists on ${GITEA_HOST}"
fi

# --- 4. push ----------------------------------------------------------------
REMOTE="git@${GITEA_HOST}:${GITEA_USER}/${REPO_NAME}.git"
log "Adding remote: ${REMOTE}"
git remote add origin "$REMOTE"

log "Pushing main"
git push -u origin main

# --- 5. tag -----------------------------------------------------------------
log "Tagging ${TAG}"
git tag -a "$TAG" -m "Spec locked.

Scope, interfaces, and success criteria are fixed as of this tag.
Changes after this point are amendments with a recorded reason,
not silent edits.

Open questions 1-5 in spec.md remain open by design. Two require
spikes before the orchestrator is written: OQ1 (citation syntax vs
numeral extraction for the grounding guard) and OQ2 (XBRL tag
heterogeneity across filers)."

git push origin "$TAG"

# --- 6. verify --------------------------------------------------------------
log "Verifying"
git --no-pager log --oneline -1
git --no-pager tag -l "$TAG" -n1
echo
log "Tree at ${TAG}:"
git ls-tree -r --name-only "$TAG" | sed 's/^/    /'
echo
log "Done — https://${GITEA_HOST}/${GITEA_USER}/${REPO_NAME}/src/tag/${TAG}"
