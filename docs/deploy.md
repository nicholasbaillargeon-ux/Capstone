# Deploy runbook

Everything below runs on the homelab over MobaXterm.

## 0. Running it as a service (current setup)

The app runs straight from a checkout under systemd — no image rebuild between a
code change and a restart. This is what is installed on `nickserver` today.

```bash
sudo cp deploy/filingdesk-native.service /etc/systemd/system/filingdesk.service
sudo systemctl daemon-reload
sudo systemctl enable --now filingdesk
```

Day to day:

```bash
systemctl status filingdesk          # is it up
sudo systemctl restart filingdesk    # after a code change
journalctl -u filingdesk -f          # follow the structured logs
journalctl -u filingdesk -o cat | jq # query them
```

`Restart=always` with `RestartSec=3` means a crash comes back on its own —
verified by `kill -9` on the main PID, which recovered in 5 seconds — and
`enable` means it returns after a reboot. `StartLimitBurst=5` stops a genuine
crash-loop from hiding behind a green status.

The Docker path in `filingdesk-docker.service` still exists for a packaged
deploy; the two are alternatives, not layers, and only one should be enabled.

## 1. First deploy (Docker alternative)

```bash
sudo mkdir -p /opt/filingdesk && sudo chown "$USER" /opt/filingdesk
cd /opt/filingdesk
git clone https://gitea.baillargeon.casa/nick/capstone.git .

cp .env.example .env && $EDITOR .env      # SEC_USER_AGENT, FD_VAULT_HOST
docker compose build
docker compose up -d
docker compose ps                          # expect: running (health: starting)
```

The container starts **unhealthy on purpose** — `/api/health` reports `ready:false`
until the volume has data. Caddy will not send it traffic in that state.

## 2. Seed the volume

```bash
docker compose exec filingdesk python -m filingdesk.seed NVDA
docker compose exec filingdesk python -c \
  "from filingdesk import vault, config; vault.index(config.VAULT_DIR)"

curl -s localhost:8088/api/health | jq
# ready:true, facts_loaded > 0, both timestamps present
docker inspect -f '{{.State.Health.Status}}' filingdesk   # healthy
```

Then open the UI at `https://filing.baillargeon.casa/`. Ask a question and the
progress rail fills in as each stage lands — tool calls, retrieval, drafting,
guard — rather than showing nothing until the generation finishes.

## 2a. Demo mode, no Ollama

`FD_STUB=1` seeds fabricated fixtures and fakes the model, so the whole UI is
exercisable on a machine with no Ollama and no SEC access. Every page rendered in
this mode carries a banner saying the figures are synthetic.

```bash
docker run --rm -e FD_STUB=1 -p 127.0.0.1:8088:8000 filingdesk:0.1.0
```

Useful for working on the interface without waiting on CPU inference. Never set it
on the real deployment.

## 3. Reverse proxy

```bash
sudo tee -a /etc/caddy/Caddyfile < deploy/Caddyfile.snippet
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -sk https://filing.baillargeon.casa/api/health | jq .status
```

## 4. Autostart

`restart: unless-stopped` already survives reboot. The unit adds ordering after
`docker.service`, one `systemctl status` for the stack, and a home for the env file.

```bash
sudo cp deploy/filingdesk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now filingdesk
systemctl status filingdesk --no-pager
```

## 5. Reboot test — do this now, not Friday

```bash
sudo reboot
# wait, reconnect
cd /opt/filingdesk && ./deploy/verify-reboot.sh
```

Six checks: container autostarted, healthcheck healthy, **facts survived on the
volume**, history file survived, TLS endpoint responding, journald has parseable
JSON. Any failure is today's problem.

## 6. Logs

```bash
journalctl CONTAINER_TAG=filingdesk -f -o cat | jq -c

# one request end to end
journalctl CONTAINER_TAG=filingdesk -o cat | jq -c 'select(.trace_id=="01J...")'

# every tool call with arguments and timing
journalctl CONTAINER_TAG=filingdesk -o cat \
  | jq -c 'select(.event=="tool.call") | {tool, args, ms, facts_added}'

# rejected calls, grouped by why
journalctl CONTAINER_TAG=filingdesk -o cat \
  | jq -r 'select(.event=="tool.rejected") | .kind' | sort | uniq -c

# p95 latency, straight from production logs (ADR-0002's claim)
journalctl CONTAINER_TAG=filingdesk -o cat \
  | jq -s '[.[] | select(.event=="request.end") | .ms] | sort | .[(length*0.95|floor)]'
```

## 7. Smoke test

```bash
docker compose exec filingdesk python -m filingdesk.smoke
```

Runs `docs/questions.md`, prints a verdict per question, writes JSON to
`/data/eval/smoke-<timestamp>.json`. Results persist on the volume so runs are
comparable across deploys.

## Rollback

```bash
docker compose down
git checkout <previous-tag>
docker compose up -d --build
```

The volume is untouched by rollback. Rebuilding the data is
`seed` + `index`, both idempotent.
