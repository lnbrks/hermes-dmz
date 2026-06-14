# GWS Gateway — Design Sketch

A design for the agent-first version of the inbox cleanup pipeline. Where
the current build sandwiches the LLM between trusted non-AI fetch and apply
steps, the agent-first version gives the LLM direct (but policy-restricted)
access to Google Workspace APIs through an HTTPS proxy.

Status: **implemented.** The proxy addons live beside this file
(`addons.py`, `policy/`), the deployment is in `deploy/` and `hermes/`, and
`gateway/test/smoke.sh` exercises the policy with no credentials. See
`deploy/README.md` for how to run it. Two divergences from the original sketch
below: (a) the gateway **self-refreshes** its access token from a mounted
`authorized_user` credential rather than receiving an out-of-band token, and
(b) the sandbox's gws is fed a dummy token via `GOOGLE_WORKSPACE_CLI_TOKEN`
(env), so no OAuth `/token` round-trip or forged credential store is needed —
the `/token` stub remains only as defense-in-depth. The rest of this doc is the
original reasoning, kept for context.

## Why

The current pipeline (`agent/bin/cleanup-run` and friends) works, but adding
new capabilities to it means writing new Python in the trusted layer for
every kind of access. If we want the agent to optionally fetch a full body,
query a calendar to decide whether an event is upcoming, or list older
messages from a sender to evaluate engagement, each one is a new trusted
script. That doesn't scale, and it pushes complexity into the trusted layer
that we'd rather keep small.

The agent-first version lets the LLM use `gws` directly, while a proxy in
front of it enforces the **same safety properties** as the current trusted
apply layer: read whatever you want, but mutations are limited to the
`ai-cleanup/*` label namespace.

## Architecture

```
┌──────────── bwrap sandbox ─────────────┐      ┌─── host ───────────────┐
│                                        │      │                        │
│  claude -p   (--allowedTools Bash...)  │      │  gws-gateway           │
│       │                                │      │  (mitmproxy + policy)  │
│       │ shell                          │      │                        │
│       ▼                                │      │  ┌── policy addon ──┐  │
│  gws gmail users messages list  ───────┼──┐   │  │ host allowlist   │  │
│       │                                │  │   │  │ method allowlist │  │
│       │ HTTPS_PROXY=http://host:8080   │  ├──▶│  │ batch/watch deny │  │
│       ▼                                │  │   │  │ body validation  │  │
│  (TLS w/ proxy's CA → terminated       │  │   │  │ token injection  │  │
│   at proxy; re-encrypted upstream)     │  │   │  └──────────────────┘  │
│                                        │  │   │           │            │
│  ANTHROPIC_API_KEY in env              │  │   │           ▼            │
│  No real OAuth token reachable.        │  │   │   *.googleapis.com     │
│                                        │  │   │                        │
└────────────────────────────────────────┘  │   │  OAuth tokens (real)   │
                                            │   │  live ONLY here.       │
                                            │   └────────────────────────┘
                                            │
                       proxied HTTPS over localhost:8080
                       (CONNECT + TLS w/ our CA)
```

Three components:

1. **Sandbox**: bwrap-isolated `claude -p` invocation. It has `gws`, the
   proxy's CA cert installed in its trust store, and `HTTPS_PROXY=...`. It
   does **not** have real OAuth tokens — the OS keyring/gws creds dir is
   NOT bind-mounted in.
2. **Proxy**: mitmproxy on the host with a custom Python policy addon. It
   terminates the TLS connection from gws, validates the request, optionally
   rewrites it (token injection), and forwards to the real Google APIs.
3. **Host credentials**: gws OAuth tokens live in `~/.gws/` on the host.
   The proxy loads them, manages refresh out-of-band, and injects them
   into outgoing requests. The sandbox never has them.

## Why an HTTP-level proxy (vs a CLI wrapper)

The earlier design sketch (kept as discussion notes in this folder, see
`alternative-cli-wrapper.md` if added) wraps the `gws` CLI binary directly.
It's simpler but fragile — depends on gws's argv grammar staying stable
and on us correctly enumerating every command flag.

A proxy enforces policy at the **actual security surface** (the HTTPS wire
to Google), which has the following advantages:

| | CLI wrapper | API proxy |
|---|---|---|
| Layer of enforcement | argv | HTTP |
| Surface to keep correct | gws's argv grammar (proprietary) | Google's REST URLs (public, stable) |
| Independent of client | No (gws-only) | Yes (any HTTP client) |
| TLS ceremony | None | Custom CA in sandbox |
| Audit log | Build your own | Free (mitmproxy logs every request) |
| OAuth token isolation | Hard | Free (injected at proxy) |

We accept the TLS-interception ceremony in exchange for these properties.

## Policy: what's allowed

### Hosts

Allow only these hostnames; deny all others (kill connection):

- `gmail.googleapis.com`
- `www.googleapis.com`
- `oauth2.googleapis.com` — token refresh (handled specially; see below)

If we add Calendar / Drive / Sheets, add their hosts here.

### Methods (general rules)

| Bucket | When | Action |
|---|---|---|
| Reads (`GET`) | Always | Allow |
| Label mutations | `POST` on `/gmail/v1/users/me/messages/{modify,batchModify}` | Validate body: every label ID in `addLabelIds` / `removeLabelIds` must be an `ai-cleanup/*` label |
| Label creation | `POST` on `/gmail/v1/users/me/labels` | Validate body: `name` must start with `ai-cleanup/` |
| Label deletion | `DELETE` on `/gmail/v1/users/me/labels/{id}` | Resolve `{id}` via labels.list; allow only if name starts with `ai-cleanup/` |
| Everything else | * | Deny with 403 |

### Structural traps (hard deny regardless of method)

Three Google-specific patterns that defeat naive method-based policy:

1. **Batch endpoints** — `POST /batch/...` wraps arbitrary sub-requests.
   Block entirely. (gws doesn't appear to use these for inbox operations.)
2. **Watch / push subscriptions** — `:watch` on any resource creates a
   server-to-URL notification subscription. An exfil channel even if the
   resource itself is "read-only". Block.
3. **Resumable uploads** — `?uploadType=resumable` returns a session URI
   that subsequent `PUT`s use for chunked upload. Validation requires
   session-state tracking. Block.

```python
if "/batch" in req.path:        deny("batch endpoint")
if ":watch" in req.path:        deny("watch endpoint")
if "uploadType=resumable" in req.url: deny("resumable upload")
if req.host == "upload.googleapis.com": deny("upload host")
```

## Token strategy: dummy in, real out

The sandbox doesn't have credentials. gws starts up, can't find a real
token, and hits `oauth2.googleapis.com/token` to refresh. The proxy
**intercepts that request and returns a synthetic OAuth response**:

```json
{
  "access_token": "dummy-sandbox-token",
  "expires_in": 3600,
  "token_type": "Bearer"
}
```

gws now believes it has a valid token and stamps it into every subsequent
request:

```
Authorization: Bearer dummy-sandbox-token
```

The proxy recognizes the dummy value, **strips it, and inserts the real
token** loaded from `~/.gws/` on the host before forwarding upstream:

```python
if req.headers.get("Authorization") == "Bearer dummy-sandbox-token":
    req.headers["Authorization"] = f"Bearer {real_access_token}"
```

Properties:
- Real OAuth refresh token never enters the sandbox.
- Real access token is only present in the request that goes upstream from
  the proxy; never appears in gws's memory or the sandbox.
- The proxy is responsible for keeping `real_access_token` fresh — it
  performs token refresh out-of-band using the host's refresh token.
- If the proxy crashes or refresh fails, the sandbox sees a 401 — same
  failure mode as a normal expired token.

This is the **most important property** of the proxy design and the main
reason we're not just doing a CLI wrapper.

## Body validation: `batchModify`

The complexity is small but specific. Proxy logic:

1. Parse JSON body.
2. Extract `addLabelIds` and `removeLabelIds` (each a list of strings).
3. Get the current set of `ai-cleanup/*` label IDs (cache them in the proxy
   process; refresh on cache miss by hitting `labels.list` upstream using
   the real token).
4. For each ID in either list: if it's not in the `ai-cleanup/*` set, 403.
5. Otherwise pass through.

```python
ALLOWED_PREFIX = "ai-cleanup/"
_cleanup_ids_cache = None  # set[str]

def cleanup_ids():
    global _cleanup_ids_cache
    if _cleanup_ids_cache is None:
        # Make an upstream labels.list using the host's real token.
        # ... call gmail.googleapis.com/gmail/v1/users/me/labels with Bearer <real>
        _cleanup_ids_cache = {l["id"] for l in labels if l["name"].startswith(ALLOWED_PREFIX)}
    return _cleanup_ids_cache
```

**Critical:** never allow `INBOX`, `TRASH`, `SPAM`, `STARRED`, `IMPORTANT`
in any modification. These are the actual state mutations dressed as
labels. The prefix check catches this automatically (none start with
`ai-cleanup/`) but a paranoid second check doesn't hurt.

## Cache invalidation

The cleanup-label-ID cache is per-proxy-process and lives forever. If the
user creates a new `ai-cleanup/*` label out-of-band, the cache misses it.
Two options:
- Time-based expiry (e.g., 5 minutes).
- Invalidate on every successful `POST /gmail/v1/users/me/labels` (we just
  created or modified a label).

Probably (b) is enough; the only way new `ai-cleanup/*` labels enter the
system is the agent creating them via the proxy.

## Sandbox integration

Changes to `agent/bin/agent-run`:

```bash
# In addition to the existing binds, mount the proxy's CA cert and set
# HTTPS_PROXY. Also bind in the real gws binary.
--ro-bind "$GATEWAY_CA_BUNDLE" /etc/pki/ca-trust/source/anchors/gateway.crt
--ro-bind "$(which gws)" /opt/gws
--setenv HTTPS_PROXY "http://${GATEWAY_HOST}:${GATEWAY_PORT}"
--setenv HTTP_PROXY  "http://${GATEWAY_HOST}:${GATEWAY_PORT}"
--setenv SSL_CERT_FILE /etc/pki/ca-trust/source/anchors/gateway.crt
```

The agent's allowed tool set expands from Read/Write to Read/Write/Bash
(since it needs to shell out to gws). The system prompt changes
significantly — instead of "classify these candidates," it becomes
"investigate the inbox and produce label proposals," with the agent
deciding what to fetch.

The trusted layer shrinks dramatically: `gws-fetch`, `pre-filter`,
`apply-labels` mostly go away. What's left is `cleanup-run` (orchestration)
and `notify` (still trusted, since notifications go over public ntfy).

## Directory layout (proposed)

```
gateway/
├── DESIGN.md          (this file)
├── proxy_policy.py    (mitmproxy addon implementing the policy)
├── ca-init.sh         (generate the proxy's CA on first run; cache in agent/state/)
├── run-gateway.sh     (start mitmproxy with our addon; called by cleanup-run)
└── README.md          (install + invocation instructions)
```

## Cloud-VM evolution

This design transfers to a cloud VM with minimal change:

- The proxy runs as a systemd service on the VM (or as a sidecar container).
- The agent's sandbox is a container or a VM-local user with restricted
  filesystem access.
- Host credentials (`~/.gws/`) move to a secret-manager backend; the proxy
  fetches them at startup.
- Sandbox-to-proxy traffic stays on localhost (or a private network); the
  proxy speaks to Google over the public internet.

The policy code is unchanged. This is the main reason to invest in the
proxy now rather than the CLI wrapper: the architecture is cloud-ready.

## Open questions

1. **What does the agent's system prompt look like in this mode?** The
   current prompt is heavily focused on "classify what you're given." The
   agent-first prompt has to give it strategy: how to discover work, when to
   stop, how to bound cost.
2. **Token budget.** With Bash + gws access, the agent can make many API
   calls and the cost ceiling becomes important. Need to enforce via
   prompt and a token-count guard in the proxy (deny after N requests per
   run?).
3. **Notification scope.** With richer access (read whole bodies, query
   calendars), the agent's notification can leak more sensitive content.
   Re-evaluate the "ntfy as public push" assumption — maybe need a
   notification-content sanitizer.
4. **OAuth token lifetime.** Host-side token refresh adds the responsibility
   of keeping the proxy's tokens fresh between runs. Probably easiest: do
   it lazily on first use per run.
5. **Read-blast-radius limits.** Should we cap how many emails the agent
   can `messages.get`? Otherwise a hallucinated loop could fetch the whole
   inbox.
6. **Should batch endpoints be allowed with recursive validation?** Right
   now we deny outright. If a future use case wants batch, we'd parse the
   multipart body and apply policy per sub-request. Defer.

## When to build this

Not yet. The current pipeline works and the user is iterating on prompt
quality. Building this is a meaningful effort (proxy code, CA management,
sandbox integration, prompt rewrite) and changes the operational footprint.

Build when one of these triggers:
- The trusted layer is becoming a bottleneck for new capabilities.
- The agent needs to fetch bodies/related-messages/calendar to do its job
  well.
- We're moving to a cloud VM and need a cleaner credential boundary.
