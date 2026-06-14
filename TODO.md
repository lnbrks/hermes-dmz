# TODO — review & follow-ups

Captured from a review pass on the v2 agent-first build. Nothing here blocks the
working pipeline; these are correctness/clarity/security improvements to make
before relying on it heavily.

## Flow & efficiency

- [ ] **Audit the cursor / loading / webhook flow as it actually runs.** Trace a
      real trigger end to end (`host/fetch-and-trigger` → webhook → agent →
      `apply-labels` → `commit-cursor`). Are there redundant agent invocations
      (e.g. the agent re-classifying, multiple model round-trips that could be
      collapsed)? Is anything double-processed?
- [ ] **Consider batching.** Right now each trigger is one batch of N candidates.
      Should we batch differently (size caps, multiple smaller batches, coalescing
      rapid triggers) for cost/latency? Decide on a sensible batch-size policy.

## Tools & skills

- [ ] **Confirm the agent has exactly the right toolset.** Currently
      `platform_toolsets.{webhook,telegram} = [terminal, file, skills, memory,
      session_search, todo]`. Is anything missing (or extra)? E.g. does it need
      `clarify`? Should `web`/MCP `web_search` be explicitly removed (it's offered
      via an MCP plugin, firewall-blocked, wastes a turn)?
- [ ] **Give the agent a "post to Telegram" helper script** so it doesn't have to
      carry large content (esp. unsubscribe links) in its context window. A small
      script that sends a message/file via the (gateway-fronted) Telegram API,
      callable from the skill, keeps bulky output out of the model context.

## Unsubscribe-link automation

- [ ] **Build the unsubscribe-link flow into the skill + scripts.** The host
      pre-filter already writes `unsubscribe-urls.json` per batch (id → URL). Wire
      a script that renders the canonical unsubscribe list (grouped by sender, as
      a clickable message/file) and have the skill invoke it — instead of the
      agent hand-assembling links in context. (Port the intent of v1's
      `finalize-notification`.)

## Security audits

- [ ] **Audit the mitmproxy policy filters closely** (`gateway/policy/*`):
      egress dial-target keying + authority-agreement, the Google `ai-cleanup/*`
      body validation (batchModify/modify/labels), the public-endpoint bypasses
      (discovery, `/api/v1/models`), token/key/bot-token injection + shared-secret
      gates, Telegram method allowlist + chat_id extraction (json/form/multipart),
      and the audit-log redaction. Look for bypasses, parser gaps, and
      fail-open paths.
- [ ] **Audit the Docker DMZ setup closely** (`deploy/docker-compose.yml`): the
      `internal: true` jail (no route to the internet except via the proxy), the
      socat ingress path, SELinux `:z` relabels, uid/ownership, the named CA
      volume sharing, and that no real secret reaches the Hermes container.
      Verify the network isolation holds (e.g. Hermes truly cannot reach the
      internet directly, DNS included).

## Naming

- [ ] **Rename "gateway" → "dmz" where it's ours**, to avoid collision with
      Hermes' own `gateway` (it runs `gateway run`, writes `gateway.log`, has
      `gateway.platforms.*`). Scope: the `gateway/` dir, the `inbox-gateway`
      container + compose service name, the `gateway` network alias used in
      `HTTPS_PROXY`/socat target, `GATEWAY_*` env vars, and doc/comment
      references. Mechanical but touches several files — do it in one sweep.
