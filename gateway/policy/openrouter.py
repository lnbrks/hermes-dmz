"""OpenRouter policy: dummy key in the sandbox, real key injected here.

Same shape as the Google policy, for the same reason: a prompt-injected agent
could otherwise point Hermes at an attacker-controlled model route and exfil
the inbox through the completion request. So:

  - The sandbox holds only a dummy `OPENROUTER_API_KEY`. The real key lives in
    THIS container's env and is swapped in on the way upstream.
  - Only the handful of endpoints Hermes actually needs are allowed; everything
    else on openrouter.ai is denied.

Acts only on flows egress tagged `mode: openrouter`.
"""

import os

from mitmproxy import http

REAL_KEY = os.environ.get("OPENROUTER_API_KEY", "")
# Shared-secret dummy the sandbox is configured to send. A request without it
# isn't from our sandbox → deny, don't inject the real key.
SANDBOX_KEY = os.environ.get("SANDBOX_OPENROUTER_KEY", "dummy-openrouter-key")

# (method, path) pairs Hermes needs. Add here if a real call gets denied.
ALLOWED = {
    ("POST", "/api/v1/chat/completions"),
    ("POST", "/api/v1/completions"),
    ("GET", "/api/v1/models"),
}


class OpenRouter:
    def request(self, flow: http.HTTPFlow) -> None:
        if flow.response:
            return
        if flow.metadata.get("gateway_mode") != "openrouter":
            return

        req = flow.request
        path = req.path.split("?", 1)[0]
        method = req.method.upper()

        if (method, path) not in ALLOWED:
            return self._deny(flow, f"endpoint-not-allowed:{method}:{path}")

        # /models is public — Hermes fetches model metadata unauthenticated.
        # No secret to gate, nothing to inject; let it pass as-is.
        if path == "/api/v1/models":
            return

        # chat/completions: require the dummy key, then inject the real one.
        if req.headers.get("Authorization") != f"Bearer {SANDBOX_KEY}":
            return self._deny(flow, "bad-sandbox-credential")

        if not REAL_KEY:
            flow.metadata["gateway_decision"] = "deny:no-openrouter-key"
            flow.response = http.Response.make(
                503,
                b'{"error":"gateway has no OpenRouter key"}',
                {"Content-Type": "application/json"},
            )
            return

        req.headers["Authorization"] = f"Bearer {REAL_KEY}"

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        # Stream SSE completions through instead of buffering, so token-by-token
        # model output isn't stalled until the response completes.
        if flow.metadata.get("gateway_mode") == "openrouter" and flow.response:
            if "text/event-stream" in flow.response.headers.get("content-type", ""):
                flow.response.stream = True

    def _deny(self, flow, why):
        flow.metadata["gateway_decision"] = f"deny:{why}"
        flow.response = http.Response.make(
            403,
            f'{{"error":"gateway policy","reason":"{why}"}}'.encode(),
            {"Content-Type": "application/json"},
        )
