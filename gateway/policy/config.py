"""Host allowlist loader.

Backs the generic `egress` addon. Reads `allowlist.yaml` and exposes a
host -> config lookup. Hot-reloads on file mtime change so the allowlist can
be edited without restarting the proxy.
"""

import os
import threading
from pathlib import Path

import yaml

_DEFAULT_PATH = os.environ.get("GATEWAY_ALLOWLIST", "/app/config/allowlist.yaml")


class Allowlist:
    def __init__(self, path=None):
        self.path = Path(path or _DEFAULT_PATH)
        self._lock = threading.Lock()
        self._mtime = None
        self._hosts = {}
        self.reload()

    def reload(self):
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            with self._lock:
                self._hosts = {}
                self._mtime = None
            return
        if mtime == self._mtime:
            return
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
        except Exception:
            return  # keep last-known-good on a malformed edit
        hosts = {}
        for host, cfg in (data.get("hosts") or {}).items():
            hosts[host.strip().lower()] = cfg or {}
        with self._lock:
            self._hosts = hosts
            self._mtime = mtime

    def host_cfg(self, host):
        self.reload()
        with self._lock:
            return self._hosts.get((host or "").strip().lower())


ALLOWLIST = Allowlist()
