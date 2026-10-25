"""AUTH2: per-session revocation and the re-check of long-lived responses.

The login is a signed cookie with no server-side session. Two things reach beyond it:

* every login gets a random **session id** (`sid`, kept in the cookie). A normal logout adds only that id to a small
  **revoked ids** file (`data/revoked_sessions.json`, instance-scoped, written atomically, kept in memory too); the
  entry lives as long as the cookie can (the cookie's own lifetime counted from the revocation: a revoked cookie is
  refused, so it is never refreshed). The global `auth_state["auth_version"]` (bumped by a password change, by
  re-enabling protection and by "sign out everywhere") already ends every other session; it is the global epoch.
* a **long-lived response** (a camera stream, MJPEG) is authorised once when it starts. `guard_stream()` wraps its
  body iterator and asks the guard again every few seconds, so a logout or a password change ends an open stream
  instead of leaving it to the stream's own maximum age (10 minutes, kept as the last resort).

A cookie without a `sid` (made before AUTH2) stays valid and simply gets an id on its next request. Stdlib plus the
project's atomic JSON store; no Flask import, so it is unit-testable on its own.
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Callable, Iterable

from storage.json_store import safe_read_json, safe_write_json

MAX_REVOKED = 5000  # a bound, not an expected size: a logout is a human action
DEFAULT_RECHECK_S = 3.0


def new_session_id() -> str:
    return secrets.token_urlsafe(16)


class RevokedSessions:
    """Revoked session ids with their expiry (epoch seconds), in memory and in one small JSON file."""

    def __init__(self, path: str | None = None, lifetime_s: float = 7 * 86400.0, clock: Callable[[], float] = time.time):
        self._path = path
        self._lifetime = float(lifetime_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, float] = {}
        if path:
            data = safe_read_json(path, default=None)
            entries = data.get("revoked") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                for sid, expires in entries.items():
                    if isinstance(sid, str) and isinstance(expires, (int, float)):
                        self._entries[sid] = float(expires)
            self._prune_locked()

    def _prune_locked(self) -> None:
        now = self._clock()
        for sid in [sid for sid, expires in self._entries.items() if expires <= now]:
            del self._entries[sid]
        if len(self._entries) > MAX_REVOKED:
            for sid, _expires in sorted(self._entries.items(), key=lambda item: item[1])[: len(self._entries) - MAX_REVOKED]:
                del self._entries[sid]

    def add(self, sid: str) -> bool:
        """Revoke `sid`. The in-memory entry is set first and always stands; the return value says whether the file
        was written (a failed write must not stop a logout, it only means a restart forgets the entry)."""
        if not isinstance(sid, str) or not sid:
            return False
        with self._lock:
            self._entries[sid] = self._clock() + self._lifetime
            self._prune_locked()
            snapshot = dict(self._entries)
        if not self._path:
            return True
        return bool(safe_write_json(self._path, {"revoked": snapshot}))

    def is_revoked(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            expires = self._entries.get(sid)
            if expires is None:
                return False
            if expires <= self._clock():
                del self._entries[sid]
                return False
            return True

    def count(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._entries)


class _GuardedStream:
    """A response body that asks `check()` between chunks. A class (not a generator) on purpose: the WSGI server
    calls close() on a body that was never iterated (a client that left before the first byte), and the wrapped
    stream's close() - which frees its viewer slot - must run then too."""

    def __init__(self, body, check, interval_s, clock):
        self._body = body
        self._iterator = iter(body)
        self._check = check
        self._interval = interval_s
        self._clock = clock
        self._last = clock()

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self._iterator)  # StopIteration (the stream ended) passes through
        now = self._clock()
        if now - self._last >= self._interval:
            self._last = now
            if not self._check():
                self.close()
                raise StopIteration
        return chunk

    def close(self) -> None:
        close = getattr(self._body, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def guard_stream(body: Iterable[bytes], check: Callable[[], bool] | None, interval_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
    """Wrap `body` so `check()` is asked at most every `interval_s` seconds between chunks; when it says the session
    is no longer valid the body is closed and the response ends. `check` None (nothing to guard) returns `body`
    itself. A body that is waiting for data (a camera that stopped sending) is not interrupted from here: the
    stream's own no-data limit ends it."""
    if check is None:
        return body
    return _GuardedStream(body, check, DEFAULT_RECHECK_S if interval_s is None else interval_s, clock)
