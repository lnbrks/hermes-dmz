"""Host-resolution helpers for the policy addons.

The security-critical rule (see egress.Egress): never key policy off
`pretty_host` — it returns the client-supplied Host header, which the sandbox
controls. Key off the actual dial target, `server_conn.address`, and require
the request's claimed authority to agree with it on the request hook (so the
check runs per-request, closing Host-header spoofing AND HTTP/2 connection
coalescing in one shot).

Caveat this does NOT cover: `server_conn.address` is the dial target only in
regular/transparent/reverse modes (we run regular). It is a NAME, not an IP —
the egress resolver still maps name→IP, so upstream DNS remains in the trust
base unless we IP-pin (not done here; documented residual).
"""


def host_only(value):
    """Lowercase host with any :port stripped. Handles [ipv6] literals."""
    if not value:
        return None
    h = value.strip().lower()
    if h.startswith("["):                      # [::1]:443  ->  ::1
        return h[1:].split("]", 1)[0]
    if h.count(":") == 1:                       # host:port  ->  host
        host, _, port = h.rpartition(":")
        if port.isdigit():
            return host
    return h


def dial_host(flow):
    """The host the socket will actually be opened to (regular-mode truth)."""
    addr = getattr(flow.server_conn, "address", None)
    if not addr or not isinstance(addr, (list, tuple)) or not addr[0]:
        return None
    return host_only(addr[0])


def authority_host(flow):
    """The host the request claims to target (spoofable; must match dial)."""
    r = flow.request
    return host_only(r.host or r.host_header)
