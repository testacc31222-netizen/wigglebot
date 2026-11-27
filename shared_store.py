"""Shared, server-side persistence for Wigglesworth bot settings.

The bot must never depend on browser/client state. All settings and creator
data live in a JSON file on the server, and — when BOT_STATE_URL is configured —
are also mirrored to a shared HTTP store so every user and every bot instance
sees the same saved configuration.

Behaviour:
* ``load()`` re-reads the local file whenever its modification time changes,
  so a second process sharing the same volume sees new saves immediately.
* ``load()`` also reads the shared store (if configured) so instances on
  different machines agree on the latest saved data.
* ``save()`` writes atomically to disk and pushes to the shared store, and
  returns whether every configured target succeeded — never silent failure.
* ``status()`` exposes the last save/load error for diagnostics.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

# Optional shared store. GET /state -> JSON object, POST /state <- JSON object.
# BOT_STATE_TOKEN is sent as a bearer token when provided.
STATE_URL = os.getenv("BOT_STATE_URL", "").strip().rstrip("/")
STATE_TOKEN = os.getenv("BOT_STATE_TOKEN", "").strip()
TIMEOUT = 8


class SharedStore:
    """A JSON document persisted locally and, optionally, in a shared store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._mtime = None
        self.last_error = ""
        self.last_saved = 0.0
        self.last_loaded = 0.0
        self.remote_ok = False
        self._remote_checked = 0.0
        self._remote_interval = 30.0  # avoid a network call on every read
        self._data = None  # last known document, reused when nothing changed

    def _headers(self) -> dict:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if STATE_TOKEN:
            headers["Authorization"] = "Bearer " + STATE_TOKEN
        return headers

    def load(self, default: dict | None = None) -> dict:
        """Return the freshest data from disk / shared store."""
        data = self._data if self._data is not None else (default if default is not None else {})
        self.last_error = ""

        # Local file. Reload when another process wrote it.
        try:
            if self.path.exists():
                mtime = self.path.stat().st_mtime_ns
                if mtime != self._mtime:
                    raw = self.path.read_text(encoding="utf-8")
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        data = parsed
                    self._mtime = mtime
                    self.last_loaded = time.time()
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = f"local read failed: {type(exc).__name__}: {exc}"

        # Shared store, when configured. It wins so all instances agree.
        # Throttled: checking on every read would hammer the store.
        if STATE_URL and (time.time() - self._remote_checked) >= self._remote_interval:
            self._remote_checked = time.time()
            try:
                req = urllib.request.Request(STATE_URL + "/state", headers=self._headers())
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    parsed = json.loads(resp.read().decode("utf-8"))
                if isinstance(parsed, dict) and parsed:
                    data = parsed
                    self.remote_ok = True
                    self.last_loaded = time.time()
                    self.last_error = ""
            except (urllib.error.URLError, ValueError, OSError) as exc:
                self.remote_ok = False
                self.last_error = f"shared store read failed: {type(exc).__name__}: {exc}"
        self._data = data
        return data

    def has_changed(self) -> bool:
        """True if the file or shared store may hold newer data than we have."""
        try:
            if self.path.exists() and self.path.stat().st_mtime_ns != self._mtime:
                return True
        except OSError:
            pass
        return bool(STATE_URL and (time.time() - self._remote_checked) >= self._remote_interval)

    def save(self, data: dict) -> bool:
        """Persist everywhere configured. Returns True only if all succeeded."""
        ok = True
        self.last_error = ""

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
            self._mtime = self.path.stat().st_mtime_ns
        except (OSError, TypeError, ValueError) as exc:
            ok = False
            self.last_error = f"local write failed: {type(exc).__name__}: {exc}"

        if STATE_URL:
            try:
                body = json.dumps(data).encode("utf-8")
                req = urllib.request.Request(
                    STATE_URL + "/state", data=body, headers=self._headers(), method="POST")
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    resp.read()
                self.remote_ok = True
            except (urllib.error.URLError, OSError, TypeError, ValueError) as exc:
                ok = False
                self.remote_ok = False
                self.last_error = f"shared store write failed: {type(exc).__name__}: {exc}"

        if ok:
            self._data = data
        self.last_saved = time.time() if ok else self.last_saved
        return ok

    def status(self) -> dict:
        return {
            "file": str(self.path),
            "shared_store": STATE_URL or "not configured",
            "shared_store_ok": self.remote_ok,
            "last_saved": self.last_saved,
            "last_loaded": self.last_loaded,
            "error": self.last_error or "none",
        }
