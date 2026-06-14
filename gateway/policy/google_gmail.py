"""Gmail policy: read freely, mutate only the ai-cleanup/* label namespace.

Enforces the same safety property as the v1 trusted apply-labels step, but at
the HTTP wire instead of in argv. Acts ONLY on flows that egress tagged with
mode `google` or `google_oauth`.

Three jobs:
  1. OAuth stub — answer oauth2.googleapis.com/token with a synthetic access
     token so the sandbox's gws believes it is authenticated. The sandbox
     never holds a real credential.
  2. Token injection — on every real Google API call, replace whatever bearer
     the sandbox sent with the real host-side access token before forwarding.
  3. Mutation policy — GETs pass; the only allowed writes are label adds/removes
     whose label IDs are all ai-cleanup/*, and label create/delete inside the
     ai-cleanup/ name prefix. Everything else is 403. Structural traps (batch,
     watch, resumable upload) are denied regardless of method.
"""

import json
import os
import re
import threading
import time

import httpx
from mitmproxy import http

from .tokens import TOKENS

DUMMY_TOKEN = "dummy-sandbox-token"
# The dummy bearer the sandbox is configured to send. Treated as a shared
# secret: a Google request that doesn't present it isn't from our sandbox and
# is denied, rather than handed a real-token-backed call.
SANDBOX_TOKEN = os.environ.get("SANDBOX_GOOGLE_TOKEN", DUMMY_TOKEN)
CLEANUP_PREFIX = "ai-cleanup/"
CACHE_TTL_SEC = 300

_OAUTH_STUB = json.dumps(
    {
        "access_token": DUMMY_TOKEN,
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": "https://www.googleapis.com/auth/gmail.modify",
    }
).encode()

RE_BATCHMODIFY = re.compile(r"^/gmail/v1/users/[^/]+/messages/batchModify$")
RE_MODIFY = re.compile(r"^/gmail/v1/users/[^/]+/messages/[^/]+/modify$")
RE_LABELS = re.compile(r"^/gmail/v1/users/[^/]+/labels$")
RE_LABEL_ID = re.compile(r"^/gmail/v1/users/[^/]+/labels/([^/]+)$")


class GoogleGmail:
    def __init__(self):
        self._lock = threading.Lock()
        self._cleanup_ids = None        # set[str] of ai-cleanup/* label IDs
        self._names = None              # dict[id -> name]
        self._cache_ts = 0.0

    # ── helpers ──────────────────────────────────────────────────────────
    def _deny(self, flow, why):
        flow.metadata["gateway_decision"] = f"deny:{why}"
        flow.response = http.Response.make(
            403,
            json.dumps({"error": "gateway policy", "reason": why}).encode(),
            {"Content-Type": "application/json"},
        )

    def _inject_token(self, flow):
        tok = TOKENS.access_token()
        if not tok:
            flow.metadata["gateway_decision"] = "deny:no-upstream-token"
            flow.response = http.Response.make(
                503,
                b'{"error":"gateway has no upstream Google token"}',
                {"Content-Type": "application/json"},
            )
            return False
        flow.request.headers["Authorization"] = f"Bearer {tok}"
        return True

    def _labels(self, force=False):
        """Return (cleanup_id_set, id->name map), cached with a short TTL."""
        with self._lock:
            fresh = (
                self._cleanup_ids is not None
                and not force
                and (time.time() - self._cache_ts) < CACHE_TTL_SEC
            )
            if fresh:
                return self._cleanup_ids, self._names
        tok = TOKENS.access_token()
        if not tok:
            return (self._cleanup_ids or set()), (self._names or {})
        try:
            r = httpx.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/labels",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=10,
            )
            r.raise_for_status()
            labels = r.json().get("labels", [])
        except Exception:
            # On failure, keep whatever we had; never widen the allow set.
            return (self._cleanup_ids or set()), (self._names or {})
        names = {l["id"]: l["name"] for l in labels}
        ids = {
            l["id"]
            for l in labels
            if l["name"].lower().startswith(CLEANUP_PREFIX)
        }
        with self._lock:
            self._cleanup_ids, self._names, self._cache_ts = ids, names, time.time()
        return ids, names

    def _bad_label_ids(self, ids):
        cleanup, _ = self._labels()
        bad = [i for i in ids if i not in cleanup]
        if bad:  # maybe the agent just created a new ai-cleanup label; refresh once
            cleanup, _ = self._labels(force=True)
            bad = [i for i in ids if i not in cleanup]
        return bad

    # ── main hook ────────────────────────────────────────────────────────
    def request(self, flow: http.HTTPFlow) -> None:
        if flow.response:
            return
        mode = flow.metadata.get("gateway_mode")
        if mode not in ("google", "google_oauth"):
            return  # not ours (passthrough hosts, etc.)

        req = flow.request
        path = req.path.split("?", 1)[0]

        # 1. OAuth: answer /token ourselves; deny anything else on the host.
        if mode == "google_oauth":
            if path.endswith("/token"):
                flow.metadata["gateway_decision"] = "stub:oauth-token"
                flow.response = http.Response.make(
                    200, _OAUTH_STUB, {"Content-Type": "application/json"}
                )
            else:
                self._deny(flow, "oauth-endpoint-not-allowed")
            return

        # 1a. Public discovery documents. gws builds its API client by fetching
        # these (e.g. /discovery/v1/apis/gmail/v1/rest, /$discovery/rest) — they
        # are unauthenticated and read-only, so they arrive without the dummy
        # bearer. No secret to gate, nothing to inject; allow as-is.
        if req.method in ("GET", "HEAD") and ("/discovery/" in path or "$discovery" in path):
            return

        # 1b. Shared-secret gate: the sandbox must present the dummy bearer.
        if req.headers.get("Authorization") != f"Bearer {SANDBOX_TOKEN}":
            return self._deny(flow, "bad-sandbox-credential")

        # 2. Structural traps — hard deny regardless of method.
        #    Match the JSON-RPC batch endpoint as a real path segment
        #    (/batch or /batch/<api>), NOT as a substring — otherwise the
        #    legitimate messages/batchModify endpoint gets caught too.
        if path == "/batch" or path.startswith("/batch/"):
            return self._deny(flow, "batch-endpoint")
        if path.endswith("/watch") or ":watch" in path:
            return self._deny(flow, "watch-endpoint")
        if "uploadType=resumable" in (req.url or ""):
            return self._deny(flow, "resumable-upload")

        method = req.method.upper()

        # 3. Reads: always allowed (with the real token swapped in).
        if method in ("GET", "HEAD"):
            self._inject_token(flow)
            return

        # 4a. Label add/remove on messages — every ID must be ai-cleanup/*.
        if method == "POST" and (RE_BATCHMODIFY.match(path) or RE_MODIFY.match(path)):
            try:
                body = json.loads(req.get_text() or "{}")
            except Exception:
                return self._deny(flow, "unparseable-body")
            ids = list(body.get("addLabelIds") or []) + list(
                body.get("removeLabelIds") or []
            )
            if not ids:
                return self._deny(flow, "modify-without-label-ids")
            bad = self._bad_label_ids(ids)
            if bad:
                return self._deny(flow, f"label-ids-outside-ai-cleanup:{bad}")
            self._inject_token(flow)
            return

        # 4b. Label creation — name must start with ai-cleanup/.
        if method == "POST" and RE_LABELS.match(path):
            try:
                body = json.loads(req.get_text() or "{}")
            except Exception:
                return self._deny(flow, "unparseable-body")
            name = (body.get("name") or "").lower()
            if not name.startswith(CLEANUP_PREFIX):
                return self._deny(flow, "label-create-outside-ai-cleanup")
            if self._inject_token(flow):
                flow.metadata["gateway_invalidate_labels"] = True
            return

        # 4c. Label delete/rename — resolve the ID; must be ai-cleanup/*.
        m = RE_LABEL_ID.match(path)
        if m and method in ("DELETE", "PUT", "PATCH"):
            label_id = m.group(1)
            _, names = self._labels()
            name = names.get(label_id, "")
            if not name.lower().startswith(CLEANUP_PREFIX):
                _, names = self._labels(force=True)
                name = names.get(label_id, "")
            if not name.lower().startswith(CLEANUP_PREFIX):
                return self._deny(flow, "label-modify-outside-ai-cleanup")
            if self._inject_token(flow):
                flow.metadata["gateway_invalidate_labels"] = True
            return

        # 5. Anything else on a Google host: deny.
        return self._deny(flow, f"method-not-allowed:{method}:{path}")

    def response(self, flow: http.HTTPFlow) -> None:
        # After a successful label create/delete, drop the cache so the next
        # mutation sees the new ai-cleanup/* set.
        if (
            flow.metadata.get("gateway_invalidate_labels")
            and flow.response
            and flow.response.status_code < 300
        ):
            self._labels(force=True)
