"""SIG4: a per-key repeat block (the same traceroute target at most once per N seconds).

A traceroute costs airtime on a shared mesh, so asking for the same node again within a minute is refused.
Pure and stdlib only: the clock is injected, the object holds nothing but the last start time per key.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict


class RepeatBlock:
    def __init__(self, window_s: float, clock: Callable[[], float] = time.monotonic, max_keys: int = 256):
        self.window_s = float(window_s)
        self._clock = clock
        self._max_keys = int(max_keys)
        self._started: Dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        """Whole seconds still to wait for `key` (0 = free to run)."""
        with self._lock:
            started = self._started.get(key)
            if started is None:
                return 0
            left = self.window_s - (self._clock() - started)
            return int(left) + (1 if left > int(left) else 0) if left > 0 else 0

    def clear(self, key: str) -> None:
        """Forget a run that never reached the air (the radio was busy), so the retry is not blocked."""
        with self._lock:
            self._started.pop(key, None)

    def mark(self, key: str) -> None:
        """Record a run that starts now."""
        with self._lock:
            now = self._clock()
            self._started[key] = now
            if len(self._started) > self._max_keys:
                for old in [k for k, t in self._started.items() if now - t >= self.window_s]:
                    del self._started[old]
