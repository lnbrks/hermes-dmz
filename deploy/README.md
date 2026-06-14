# Agent-first inbox cleanup — deployment

The model-driven version of the inbox sweep. A [Hermes](https://hermes-agent.nousresearch.com)
agent runs in a Docker container with **no direct internet access**; its only
route out is a mitmproxy **gateway** that allowlists hosts and restricts all
Gmail mutations to the `ai-cleanup/*` label namespace. The real Google
credential lives only in the gateway and is never exposed to the agent.

```
 host (trusted)                     docker
 ─────────────                      ──────────────────────────────────────────
 fetch-and-trigger ─webhook─▶ [hermes]  (internal net, NO internet)
   (gws, real creds)              │  HTTPS_PROXY ──▶ [gateway] ──▶ internet
   reads inbox, writes batch      │                    │  egress net
   to deploy/state/work/          │                    ├─ openrouter.ai   (model)
                                  │                    ├─ api.telegram.org (delivery)
   systemd timer drives it        │                    └─ *.googleapis.com (gws:
                                  ▼                        ai-cleanup/* only,
 deploy/state/secrets/gws-creds.json ──(mounted RO)──▶    real token injected)
   (real refresh token, gateway-only)
```

What's where:

| Path | Role |
|------|------|
| `gateway/` | mitmproxy image + policy addons (`egress`, `google_gmail`, `audit`). Self-contained; `gateway/test/smoke.sh` tests it with no creds. |
| `hermes/config.yaml` | Hermes config (model, telegram, webhook route, toolsets). |
| `hermes/skills/inbox-cleanup/` | The skill: `SKILL.md` (ported prompt) + `scripts/{apply-labels,commit-cursor}`. |
| `host/fetch-and-trigger` | Trusted host fetch + pre-filter + webhook fire (reuses `agent/bin/{gws-fetch,pre-filter}`). |
| `host/seed-creds` | Exports the gws refresh token to the gateway-only secret. |
| `deploy/docker-compose.yml` | The two services + the network jail. |
| `deploy/systemd/` | Timer that runs `fetch-and-trigger`. |

## Prerequisites

- Docker + docker compose, and `openssl`, `curl`, `python3` on the host.
- `gws` installed on the host. For this agent, mint a **dedicated, Gmail-only**
  credential — do NOT reuse your everyday gws token (it's scoped to many APIs;
  a gateway leak of that would expose your whole Workspace). Ideally use an
  OAuth client in **its own GCP project with only the Gmail API enabled**, then
  log in into a separate config dir (e.g. `host/gws_secrets`, gitignored) so you
  don't clobber your daily token:
  ```bash
  GOOGLE_WORKSPACE_CLI_CONFIG_DIR=host/gws_secrets \
    gws auth login --services gmail \
      --scopes https://www.googleapis.com/auth/gmail.modify
  ```
  `gmail.modify` is the minimum that allows label changes — and notably cannot
  permanently delete mail, a backstop beneath the gateway's label-only rule.

## Setup

```bash
cd deploy
cp .env.example .env          # fill in OpenRouter key, Telegram bot/chat, webhook secret
#   openssl rand -hex 32  →  WEBHOOK_SECRET

# 1. Real credential → gateway-only secret (host side; never enters hermes).
#    Export the dedicated Gmail-only authorized_user creds (see Prerequisites).
mkdir -p state/secrets
GOOGLE_WORKSPACE_CLI_CONFIG_DIR=../host/gws_secrets \
  gws auth export --unmasked > state/secrets/gws-creds.json
chmod 600 state/secrets/gws-creds.json

# 2. (Telegram) Message your bot once — bots can't DM you first. Your DM
#    chat id == your numeric user id; put it in TELEGRAM_CHAT_ID (and
#    TELEGRAM_ALLOWED_USERS). Delivery uses TELEGRAM_HOME_CHANNEL, so no
#    chat id needs to go in hermes/config.yaml.

# 3. Build + start. The gateway generates its CA on first boot; the healthcheck
#    blocks hermes until the CA exists, so hermes always trusts the proxy.
docker compose up -d --build
docker compose logs -f hermes     # watch it come up
```

## Test the gateway alone (no creds, no hermes)

```bash
../gateway/test/smoke.sh
# checks: deny non-allowlisted host, stub OAuth /token, fail-closed with no
# token, and that batchModify of a system label (TRASH) is rejected by policy.
```

## Test the full loop manually

```bash
# Fetch a few real messages and wake the agent.
cd ..
host/fetch-and-trigger --max-emails 10
# Watch the agent classify + apply in Telegram (and docker compose logs hermes).
# Inspect what the agent tried at the Google wire:
docker exec inbox-gateway cat /home/mitmproxy/.mitmproxy/audit.jsonl
```

A run should: read `deploy/state/work/batches/<ts>/candidates.ndjson`, apply
`ai-cleanup/*` labels via gws (visible as `mode:google decision:allow` for
`batchModify` in the audit log), write `applied.json`, advance
`deploy/state/work/cursor/last-run.json`, and post a summary to Telegram.

## Schedule it

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/inbox-cleanup-fetch.{service,timer} ~/.config/systemd/user/
# edit the WorkingDirectory/ExecStart paths if the repo isn't at ~/src/ai-inbox-cleanup
systemctl --user daemon-reload
systemctl --user enable --now inbox-cleanup-fetch.timer
systemctl --user list-timers | grep inbox-cleanup
```

(For a headless VM, `loginctl enable-linger $USER` so the timer runs without an
active session.)

## How the safety properties hold

The threat model: a prompt-injected agent trying to exfiltrate the inbox or
mutate Gmail. Every external dependency is a potential exfil channel — not just
Google, but the *model route* and the *delivery chat* too. So all three get
dummy-credential-in / real-credential-out, and all egress is deny-by-default.

- **No real creds in the sandbox — for anything.** Hermes holds only dummies:
  `GOOGLE_WORKSPACE_CLI_TOKEN`, `OPENROUTER_API_KEY`, and a dummy Telegram bot
  token. The gateway swaps each for the real secret upstream. The real Google
  refresh token, OpenRouter key, and bot token live ONLY in the gateway
  container's env / mounted secret.
- **The dummy is a shared secret.** The gateway *verifies* each request carries
  the expected dummy credential (the Google bearer, the OpenRouter key, the
  Telegram path token) and denies (`bad-sandbox-credential`) otherwise — it
  never injects a real secret into a request that didn't present the dummy. Set
  the `SANDBOX_*` values in `.env` to random strings for a real secret; compose
  keeps both sides in sync.
- **No exfiltration path.** Hermes is on an `internal: true` network with no
  internet route. Its only egress is the proxy, which:
  - **Denies any host** not in `gateway/config/allowlist.yaml`, keyed on the
    actual **dial target** (`server_conn.address`), not the spoofable Host
    header — and requires the request's claimed authority to match the dial
    target per-request (closes Host spoofing + HTTP/2 coalescing).
  - **Google:** reads pass; the only writes are label add/remove with
    `ai-cleanup/*` IDs and create/delete within the `ai-cleanup/` prefix.
    `INBOX`/`TRASH`/`SPAM`/etc. can never be touched.
  - **OpenRouter:** only the chat/completions + models endpoints are allowed;
    an attacker can't point the model at an arbitrary openrouter.ai path.
  - **Telegram:** deny-by-default on methods; every *send* must target a chat in
    `TELEGRAM_ALLOWED_CHATS` (defaults to your `TELEGRAM_CHAT_ID`). The bot
    cannot message an attacker's chat or `setWebhook` to redirect itself.
- **Safe retries.** The cursor only advances via `commit-cursor`, run after
  labels are applied. A crashed run re-processes the same window next time.
- **Audit trail.** Every request — allowed, denied, spoof-attempt — is one JSON
  line in the gateway's `audit.jsonl`, with the bot token redacted and both the
  dial target and the (possibly spoofed) claimed host recorded.

**Residual gaps (known, not yet closed):** the allowlist is name-level, so the
egress resolver's DNS is in the trust base (no IP-pinning). And the model
provider necessarily sees the email content it's asked to classify — the
gateway bounds *where* data can go, not the fact that the chosen model reads it.

## Troubleshooting

- **gws TLS errors in the container** (`zero valid certificates` / cert
  verify): gws (rustls) honors `SSL_CERT_FILE`, which compose points at the
  gateway's CA. Confirm the CA is present: `docker exec inbox-hermes cat
  $SSL_CERT_FILE | head -1`. If empty, the gateway didn't finish first boot —
  `docker compose restart hermes`.
- **Webhook 401 / signature rejected:** Hermes's generic webhook expects an
  HMAC-SHA256 in `X-Webhook-Signature`. `fetch-and-trigger` sends `sha256=<hex>`.
  If the installed Hermes wants bare hex (no prefix), drop `sha256=` in the
  script. Check `docker compose logs hermes` for the exact expectation.
- **Host can't reach the webhook port:** the hermes container publishes
  `127.0.0.1:8644` even though it's on an internal network (published ports use
  host DNAT, independent of the network's internet access). If your Docker
  refuses, put hermes on a second non-internal bridge for ingress only.
- **Gateway returns 503 on Gmail calls:** it has no upstream token —
  `gws-creds.json` is missing/invalid or the refresh grant failed. Re-run
  `host/seed-creds` and `docker compose restart gateway`.
- **Config schema mismatch:** `hermes/config.yaml` targets the documented keys;
  if the pinned image renamed one, diff against a `hermes setup`-generated
  config and adjust.
