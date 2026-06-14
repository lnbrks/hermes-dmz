# Gateway policy package. Each module is an independent mitmproxy addon so the
# concerns stay separable: egress (generic host allowlist) knows nothing about
# Google; google_gmail (Gmail-specific policy) knows nothing about the
# allowlist beyond the mode tag egress stamps onto the flow.
