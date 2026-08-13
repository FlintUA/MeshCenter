"""
MeshCenter Timer Service
In-memory countdown/stopwatch timers. Session-scoped by design - do not
survive a service restart (same lifetime class as
meshsrv/notification_service.py's queue). The countdown itself ticks on
the frontend (client-side setInterval reading TimeFormatter.now()); this
module only tracks start/stop/finish bookkeeping so the backend can run
the finish notify-pipeline (push_notification + optional mesh send) when
the frontend tells it a timer reached zero via POST /api/timers/<id>/finish.
"""
import time
import uuid
import threading

_lock   = threading.Lock()
_timers = {}


def _make_timer(label: str, duration_s, notify_cfg) -> dict:
    return {
        'id':          str(uuid.uuid4()),
        'label':       label,
        'duration_s':  duration_s,
        'started_at':  int(time.time()),
        'stopped_at':  None,
        'finished':    False,
        'notify':      notify_cfg or {},
    }


def create_timer(label: str, duration_s=None, notify_cfg=None) -> dict:
    t = _make_timer(label, duration_s, notify_cfg)
    with _lock:
        _timers[t['id']] = t
    return t


def get_all() -> list:
    with _lock:
        return list(_timers.values())


def get_timer(timer_id: str):
    with _lock:
        return _timers.get(timer_id)


def stop_timer(timer_id: str):
    with _lock:
        t = _timers.get(timer_id)
        if t and t['stopped_at'] is None:
            t['stopped_at'] = int(time.time())
        return t


def reset_timer(timer_id: str):
    with _lock:
        t = _timers.get(timer_id)
        if t:
            t['started_at'] = int(time.time())
            t['stopped_at'] = None
            t['finished'] = False
        return t


def delete_timer(timer_id: str) -> bool:
    with _lock:
        return _timers.pop(timer_id, None) is not None


def mark_finished(timer_id: str):
    with _lock:
        t = _timers.get(timer_id)
        if t:
            t['finished'] = True
            t['stopped_at'] = int(time.time())
        return t


def get_elapsed(t: dict) -> int:
    end = t['stopped_at'] or int(time.time())
    return end - t['started_at']
