#!/usr/bin/env bash
# Task 04. Run AFTER `sudo reboot`, once the host is back.
# Proves three separate things: the container came back, it is healthy, and
# the data on the volume survived. All three, or it did not work.
set -uo pipefail

FAIL=0
ok()   { printf '\033[0;32m  PASS\033[0m %s\n' "$*"; }
bad()  { printf '\033[0;31m  FAIL\033[0m %s\n' "$*"; FAIL=1; }

echo "== 1. container autostarted =="
state=$(docker inspect -f '{{.State.Status}}' filingdesk 2>/dev/null || echo missing)
[[ "$state" == "running" ]] && ok "container running" || bad "container state: $state"

echo "== 2. docker healthcheck =="
for i in $(seq 1 12); do
  h=$(docker inspect -f '{{.State.Health.Status}}' filingdesk 2>/dev/null || echo none)
  [[ "$h" == "healthy" ]] && break
  sleep 5
done
[[ "$h" == "healthy" ]] && ok "healthcheck healthy" || bad "healthcheck: $h"

echo "== 3. data survived the reboot =="
body=$(curl -fsS http://127.0.0.1:8088/api/health) || bad "health endpoint unreachable"
facts=$(echo "$body" | jq -r '.facts_loaded // 0')
[[ "$facts" -gt 0 ]] && ok "$facts facts still loaded" || bad "facts_loaded=$facts (volume empty?)"
echo "$body" | jq -e '.vault_indexed != null' >/dev/null \
  && ok "vault index present" || bad "vault index missing"

echo "== 4. history persisted =="
n=$(docker exec filingdesk sh -c 'wc -l < /data/history/reports.jsonl 2>/dev/null || echo 0')
[[ "${n:-0}" -gt 0 ]] && ok "$n prior reports on the volume" || bad "no history (ran any reports yet?)"

echo "== 5. reachable through Caddy =="
curl -fsSk https://filing.baillargeon.casa/api/health >/dev/null \
  && ok "TLS endpoint responding" || bad "not reachable via Caddy"

echo "== 6. logs are structured and queryable =="
journalctl CONTAINER_TAG=filingdesk -n 200 -o cat 2>/dev/null \
  | jq -e -s 'length > 0' >/dev/null \
  && ok "journald has parseable JSON" || bad "no JSON log lines in journald"

echo
[[ $FAIL -eq 0 ]] && echo "ALL CHECKS PASSED" || { echo "FAILURES ABOVE — fix today, not Friday"; exit 1; }
