"""Generic deny-by-default host allowlist + per-request routing integrity.

Two jobs, both generic (no per-service knowledge):

1. Allowlist the DIAL TARGET. The decision is keyed on `server_conn.address`
   (where the socket actually goes), never on `pretty_host` (the client-
   supplied, spoofable Host header).

2. Require the request's claimed authority to AGREE with the dial target, on
   the request hook so it runs per-request. This closes two holes at once:
   a Host-header spoof (CONNECT to an allowed host, then send requests claiming
   another) and HTTP/2 connection coalescing (a connection opened for an
   allowed host carrying a later request for a different :authority).

If the dial target is allowlisted and the authority agrees, stamp the
configured `mode` so the per-service addons can act.
"""

from mitmproxy import http

from .config import ALLOWLIST
from .util import authority_host, dial_host


class Egress:
    def request(self, flow: http.HTTPFlow) -> None:
        if flow.response:  # already short-circuited
            return

        dial = dial_host(flow)
        auth = authority_host(flow)
        flow.metadata["gateway_dial"] = dial
        flow.metadata["gateway_authority"] = auth

        if dial is None:
            return self._deny(flow, "no-dial-target")

        cfg = ALLOWLIST.host_cfg(dial)
        if cfg is None:
            return self._deny(flow, "host-not-allowlisted")

        # The request must claim the exact host we're dialing. A mismatch means
        # a spoofed Host or a coalesced/smuggled request — refuse it.
        if auth is None or auth != dial:
            return self._deny(flow, f"authority-mismatch:{auth}!={dial}")

        flow.metadata["gateway_mode"] = cfg.get("mode", "passthrough")

    def _deny(self, flow, why):
        flow.metadata["gateway_decision"] = f"deny:{why}"
        flow.response = http.Response.make(
            403,
            b'{"error":"gateway: egress denied"}',
            {"Content-Type": "application/json"},
        )
