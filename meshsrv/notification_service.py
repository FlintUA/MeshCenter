#!/usr/bin/env python3
"""MeshCenter Notification Service

Backend notification queue for the user. All sources (schedule engine,
timer, system) push through push_notification(). Intentionally in-memory
only - the queue resets on process restart, same lifetime as the other
background caches in meshsrv/ (e.g. time_service's status cache).
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

_lock: threading.Lock = threading.Lock()
_queue: list[dict[str, Any]] = []
MAX_QUEUE = 50


def push_notification(level: str, source: str, title: str, body: str = "") -> dict[str, Any]:
    """Add a notification to the queue.

    level: 'info' | 'warning' | 'error'.
    source: 'schedule_engine' | 'timer' | 'system' | 'radio' | 'telemetry'.
    Returns the created event.
    """
    event: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "timestamp": int(time.time()),
        "level": level,
        "source": source,
        "title": title,
        "body": body,
        "read": False,
    }
    with _lock:
        _queue.append(event)
        if len(_queue) > MAX_QUEUE:
            _queue.pop(0)
    return event


def get_all() -> list[dict[str, Any]]:
    """Return all queued notifications, newest first."""
    with _lock:
        return list(reversed(_queue))


def get_unread_count() -> int:
    with _lock:
        return sum(1 for e in _queue if not e["read"])


def mark_read(notification_id: str) -> bool:
    with _lock:
        for e in _queue:
            if e["id"] == notification_id:
                e["read"] = True
                return True
    return False


def mark_all_read() -> int:
    count = 0
    with _lock:
        for e in _queue:
            if not e["read"]:
                e["read"] = True
                count += 1
    return count


def delete_one(notification_id: str) -> bool:
    with _lock:
        for i, e in enumerate(_queue):
            if e["id"] == notification_id:
                _queue.pop(i)
                return True
    return False


def clear_all() -> int:
    with _lock:
        count = len(_queue)
        _queue.clear()
        return count
