"""Append-only request audit log.

Every flow — allowed, denied, or stubbed — lands here as one JSON line. This
is the free audit trail the proxy design buys us: a tamper-evident record of
exactly what the agent tried to do at the Google wire.
"""

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from mitmproxy import http

# Telegram puts the bot token in the URL path; after our swap it's the REAL
# token. Never write it to the audit log.
_BOT_TOKEN_RE = re.compile(r"/bot[^/]+/")


def _safe_path(path):
    return _BOT_TOKEN_RE.sub("/bot<REDACTED>/", path.split("?", 1)[0])


class Audit:
    def __init__(self):
        path = os.environ.get(
            "GATEWAY_AUDIT_LOG", "/home/mitmproxy/.mitmproxy/audit.jsonl"
        )
        self._lock = threading.Lock()
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._f = p.open("a")
        except Exception:
            self._f = sys.stdout

    def response(self, flow: http.HTTPFlow) -> None:
        self._emit(flow)

    def _emit(self, flow: http.HTTPFlow) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            # `dial` is the authoritative host (server_conn.address); `claimed`
            # is the spoofable Host header, logged so mismatches are visible.
            "dial": flow.metadata.get("gateway_dial"),
            "claimed": flow.request.pretty_host,
            "authority": flow.metadata.get("gateway_authority"),
            "method": flow.request.method,
            "path": _safe_path(flow.request.path),
            "mode": flow.metadata.get("gateway_mode"),
            "decision": flow.metadata.get("gateway_decision", "allow"),
            "status": flow.response.status_code if flow.response else None,
        }
        line = json.dumps(rec)
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()
