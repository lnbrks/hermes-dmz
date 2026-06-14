"""Telegram policy: dummy bot token in the sandbox, real token injected here,
and every outbound message pinned to an allowlisted chat.

The exfil vector: a prompt-injected agent makes the bot `sendMessage` the
inbox to an attacker-chosen chat. Defenses:

  - Token swap. The sandbox holds only a dummy bot token (Telegram puts the
    token in the URL path: `/bot<TOKEN>/<method>`). The real token lives in
    THIS container's env and replaces the dummy on the way upstream.
  - Method allowlist. Deny-by-default. We only allow methods we understand —
    reads (no outbound content) and "send" methods whose chat target we can
    locate. Anything else (notably `setWebhook`, which could redirect the bot)
    is denied.
  - chat_id allowlist. Every send method must target a chat in
    TELEGRAM_ALLOWED_CHATS. A send to any other chat is refused.

Acts only on flows egress tagged `mode: telegram`.
"""

import json
import os
import re

from mitmproxy import http

REAL_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Shared-secret dummy token the sandbox puts in the bot path. A request whose
# path token isn't this isn't from our sandbox → deny.
SANDBOX_TOKEN = os.environ.get(
    "SANDBOX_TELEGRAM_TOKEN", "111111:DUMMYtelegramTOKEN0000000000000000"
)
ALLOWED_CHATS = {
    c.strip()
    for c in os.environ.get(
        "TELEGRAM_ALLOWED_CHATS", os.environ.get("TELEGRAM_CHAT_ID", "")
    ).split(",")
    if c.strip()
}

# /bot<token>/<method>  or  /file/bot<token>/<path>
PATH_RE = re.compile(r"^/(file/)?bot([^/]+)/([A-Za-z0-9_]+)$")

# Send methods — must target an allowlisted chat (lowercased method names).
SEND_METHODS = {
    "sendmessage", "sendphoto", "senddocument", "sendvideo", "sendaudio",
    "sendvoice", "sendmediagroup", "sendchataction", "sendlocation",
    "sendsticker", "sendpoll", "senddice", "copymessage", "forwardmessage",
    "editmessagetext", "editmessagecaption", "editmessagereplymarkup",
    "deletemessage", "setmessagereaction", "pinchatmessage",
    # Bot API 10.1 rich messages (Hermes' default send path; carries chat_id).
    "sendrichmessage", "sendrichmessagedraft",
}
# Reads / control with no outbound-to-chat content. Allowed, no chat check.
READ_METHODS = {
    "getme", "getupdates", "getfile", "getchat", "getchatmember",
    "getmycommands", "setmycommands", "answercallbackquery",
    "getchatmenubutton", "deletewebhook",  # deletewebhook = ensure polling (benign)
}


class Telegram:
    def request(self, flow: http.HTTPFlow) -> None:
        if flow.response:
            return
        if flow.metadata.get("gateway_mode") != "telegram":
            return

        req = flow.request
        raw = req.path
        qpos = raw.find("?")
        path = raw[:qpos] if qpos >= 0 else raw
        query = raw[qpos:] if qpos >= 0 else ""

        m = PATH_RE.match(path)
        if not m:
            return self._deny(flow, "unrecognized-telegram-path")
        is_file, method = m.group(1), m.group(3).lower()

        # Shared-secret gate: the path token must be our dummy.
        if m.group(2) != SANDBOX_TOKEN:
            return self._deny(flow, "bad-sandbox-credential")

        if is_file or method in READ_METHODS:
            pass  # allowed, no chat target
        elif method in SEND_METHODS:
            chat = self._chat_id(req)
            if chat is None:
                return self._deny(flow, f"{method}-missing-chat_id")
            if str(chat) not in ALLOWED_CHATS:
                return self._deny(flow, f"chat-not-allowlisted:{chat}")
        else:
            return self._deny(flow, f"method-not-allowed:{method}")

        # Allowed → swap the dummy token in the path for the real one.
        if not REAL_TOKEN:
            flow.metadata["gateway_decision"] = "deny:no-telegram-token"
            flow.response = http.Response.make(
                503,
                b'{"error":"gateway has no Telegram token"}',
                {"Content-Type": "application/json"},
            )
            return
        new_path = f"/{'file/' if is_file else ''}bot{REAL_TOKEN}/{m.group(3)}"
        req.path = new_path + query

    def _chat_id(self, req):
        """Locate chat_id in query, JSON body, form body, or multipart body."""
        if "chat_id" in req.query:
            return req.query["chat_id"]
        body = req.content.decode("utf-8", "ignore") if req.content else ""
        ct = req.headers.get("Content-Type", "")
        try:
            if "application/json" in ct:
                return json.loads(body).get("chat_id")
            if "x-www-form-urlencoded" in ct:
                from urllib.parse import parse_qs
                vals = parse_qs(body).get("chat_id")
                return vals[0] if vals else None
            if "multipart/form-data" in ct:
                mm = re.search(
                    r'name="chat_id"\r?\n\r?\n\s*([^\r\n]+)', body
                )
                return mm.group(1).strip() if mm else None
            # Unknown content type: best-effort JSON.
            return json.loads(body).get("chat_id")
        except Exception:
            return None

    def _deny(self, flow, why):
        flow.metadata["gateway_decision"] = f"deny:{why}"
        flow.response = http.Response.make(
            403,
            f'{{"error":"gateway policy","reason":"{why}"}}'.encode(),
            {"Content-Type": "application/json"},
        )
