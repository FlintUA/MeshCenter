"""Short-lived server-side cache of the last successful TCP radio detection,
so Discovery -> Accept (two separate HTTP requests) costs ONE probe, not two
(TCP lifecycle P0, PR-B).

Trust model: entries are written only by the server, after a real probe, and
never from anything the client sends. `pop_matching` is single-use (no
replay), TTL-bounded, and requires the client-supplied node_id (if any) to
equal the cached one - anything else is a miss, which callers must answer by
running a fresh probe, never by failing. Pure and dependency-free (no I/O, no
meshtastic import); the clock is injectable for tests.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

DEFAULT_TTL_S = 60.0
DEFAULT_MAX_ENTRIES = 8


def _norm_node_id(value: Any) -> str:
    return str(value or "").strip().lower()


class DetectionCache:
    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_TTL_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = float(ttl_s)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, int], dict] = {}

    @staticmethod
    def _key(host: str, port: int) -> tuple[str, int]:
        return (str(host or "").strip().lower(), int(port or 0))

    def _prune_locked(self, now: float) -> None:
        for key in [k for k, e in self._entries.items() if now - e["stored_at"] > self._ttl_s]:
            del self._entries[key]
        while len(self._entries) > self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k]["stored_at"])
            del self._entries[oldest]

    def put(self, host: str, port: int, detected: dict, checked_at: Optional[str]) -> None:
        """Only ever called with a `detected` dict from a real, successful
        probe (it must carry a node_id - an entry without one is ignored)."""
        if not _norm_node_id((detected or {}).get("node_id")):
            return
        now = self._clock()
        with self._lock:
            self._entries[self._key(host, port)] = {
                "detected": dict(detected),
                "checked_at": checked_at,
                "stored_at": now,
            }
            self._prune_locked(now)

    def pop_matching(self, host: str, port: int, requested_node_id: str = "") -> Optional[dict]:
        """Returns {"detected": ..., "checked_at": ...} and removes the
        entry, or None (miss) when absent, expired, or when a non-empty
        requested_node_id doesn't equal the cached node_id. A mismatching
        request also drops the entry: it no longer describes what the client
        thinks it is confirming."""
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.pop(self._key(host, port), None)
        if entry is None:
            return None
        wanted = _norm_node_id(requested_node_id)
        if wanted and wanted != _norm_node_id(entry["detected"].get("node_id")):
            return None
        return {"detected": dict(entry["detected"]), "checked_at": entry["checked_at"]}
