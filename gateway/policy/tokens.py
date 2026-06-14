"""Real Google access-token provider — lives ONLY on the trusted gateway side.

The gateway is the one place the real credential exists. It holds a long-lived
`authorized_user` credential (client_id / client_secret / refresh_token — the
exact JSON `gws auth export --unmasked` produces) mounted as a read-only
secret, and mints short-lived access tokens on demand via the standard OAuth
refresh grant. The Hermes sandbox never sees any of this.

The refresh POST goes straight to Google from the gateway container — it does
NOT traverse the proxy listener — so it never collides with the synthetic
`/token` response we serve to the sandbox.

A static-token override (GOOGLE_TOKEN_FILE) is supported for the alternative
"host timer writes the token" deployment, but self-refresh is the default and
needs no external moving parts.
"""

import json
import os
import threading
import time
from pathlib import Path

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
REFRESH_SKEW_SEC = 120  # refresh this many seconds before actual expiry

_CREDS_FILE = os.environ.get(
    "GOOGLE_REFRESH_CREDENTIALS_FILE", "/run/secrets/gws-creds.json"
)
_STATIC_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "")


class TokenStore:
    def __init__(self, creds_file=None, static_file=None):
        self.creds_file = Path(creds_file or _CREDS_FILE)
        _static = static_file or _STATIC_FILE
        self.static_file = Path(_static) if _static else None
        self._lock = threading.Lock()
        self._access = None
        self._expires_at = 0.0

    def access_token(self):
        # Optional override: a host process drops a ready access token on disk.
        if self.static_file and self.static_file.exists():
            try:
                data = json.loads(self.static_file.read_text())
                if data.get("access_token"):
                    return data["access_token"]
            except Exception:
                pass  # fall through to self-refresh

        with self._lock:
            if self._access and time.time() < (self._expires_at - REFRESH_SKEW_SEC):
                return self._access
            tok = self._refresh()
            return tok

    def _refresh(self):
        try:
            creds = json.loads(self.creds_file.read_text())
        except Exception:
            return self._access  # keep last-known-good if the file is missing
        try:
            r = httpx.post(
                TOKEN_URL,
                data={
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "refresh_token": creds["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=15,
            )
            r.raise_for_status()
            body = r.json()
        except Exception:
            return self._access  # transient failure → reuse current token
        self._access = body.get("access_token", self._access)
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._access


TOKENS = TokenStore()
