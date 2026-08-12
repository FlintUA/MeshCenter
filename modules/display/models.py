"""Shared value types for DisplayManager (manager.py). e-Paper Stage 1
plan, sections 11 (status), 25/68 (event priority), 29 (stats),
66-69 (refresh modes)."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class DisplayStatus(str, Enum):
    DISABLED = "disabled"
    CONFIGURED = "configured"
    INITIALIZING = "initializing"
    ONLINE = "online"
    REFRESHING = "refreshing"
    SLEEPING = "sleeping"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


class EventPriority(str, Enum):
    NORMAL = "normal"
    # Bypasses debounce/conservative delay entirely - see plan sections 25/68
    # (radio offline, critical power, internal fault, hardware display error).
    CRITICAL = "critical"


class RefreshMode(str, Enum):
    RESPONSIVE = "responsive"
    DEBOUNCE = "debounce"
    CONSERVATIVE = "conservative"


# Default debounce window per mode, in seconds - plan section 66-69.
# Overridable via DisplayManager(debounce_seconds=...).
DEFAULT_DEBOUNCE_SECONDS: dict[RefreshMode, float] = {
    RefreshMode.RESPONSIVE: 2.0,
    RefreshMode.DEBOUNCE: 30.0,
    RefreshMode.CONSERVATIVE: 120.0,
}


@dataclass
class RefreshStats:
    """Runtime-only (plan section 6's default assumption - not persisted
    across restarts)."""

    refresh_count: int = 0
    error_count: int = 0
    last_duration: float | None = None
    last_successful_refresh: float | None = None  # epoch seconds
    _recent_durations: deque[float] = field(default_factory=lambda: deque(maxlen=10))

    @property
    def average_duration(self) -> float | None:
        if not self._recent_durations:
            return None
        return sum(self._recent_durations) / len(self._recent_durations)

    def record_success(self, duration: float) -> None:
        self.refresh_count += 1
        self.last_duration = duration
        self.last_successful_refresh = time.time()
        self._recent_durations.append(duration)

    def record_error(self) -> None:
        self.error_count += 1

    def as_dict(self) -> dict:
        return {
            "refresh_count": self.refresh_count,
            "error_count": self.error_count,
            "last_duration": self.last_duration,
            "average_duration": self.average_duration,
            "last_successful_refresh": self.last_successful_refresh,
        }
