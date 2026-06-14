# mitmproxy entry point: load the policy addons in order.
#
#   egress       — deny-by-default host allowlist + dial/authority agreement
#   google_gmail — Gmail read/mutate policy + token injection (Google-only)
#   openrouter   — model endpoint allowlist + API-key injection
#   telegram     — bot-token injection + method/chat_id allowlist
#   audit        — one JSON line per request (token-redacted)
#
# Order matters: egress runs first and can short-circuit with a 403; later
# addons see flow.response already set and bail, and only act on the `mode`
# egress stamped. audit runs on the response hook so it captures the final
# status of every flow.
#
#   mitmdump -s addons.py

from policy.audit import Audit
from policy.egress import Egress
from policy.google_gmail import GoogleGmail
from policy.openrouter import OpenRouter
from policy.telegram import Telegram

addons = [Egress(), GoogleGmail(), OpenRouter(), Telegram(), Audit()]
