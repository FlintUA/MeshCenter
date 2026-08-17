"""
MeshCenter Timer Service
In-memory countdown/stopwatch timers. Session-scoped by design - do not
survive a service restart (same lifetime class as
meshsrv/notification_service.py's queue). The countdown itself ticks on
the frontend (client-side setInterval reading TimeFormatter.now()); this
module only tracks state/elapsed-time bookkeeping so the backend can run
the finish notify-pipeline (push_notification + optional mesh send) when
the frontend tells it a timer reached zero via POST /api/timers/<id>/finish.

Elapsed time is derived, never accumulated tick-by-tick, to avoid drift:
    elapsed(t) = t['accumulated_s'] + (now - t['segment_started_at']
                                        if t['state'] == 'running' else 0)
'accumulated_s' only changes at a state transition (pause/stop/finish),
banking the just-completed running segment exactly once.
"""
import time
import uuid
import threading

_lock   = threading.Lock()
_timers = {}


def _make_timer(label: str, duration_s, notify_cfg) -> dict:
    return {
        'id':                 str(uuid.uuid4()),
        'label':               label,
        'duration_s':          duration_s,
        'state':               'running',
        'accumulated_s':       0,
        'segment_started_at':  int(time.time()),
        'notify':              notify_cfg or {},
    }


def _bank_running_segment(t: dict, now: int):
    """Fold the current running segment into accumulated_s and clear the
    segment anchor. No-op unless t is actually running - callers use this
    from every transition that leaves the running state, so it's always
    safe to call unconditionally."""
    if t['state'] == 'running' and t['segment_started_at'] is not None:
        t['accumulated_s'] += now - t['segment_started_at']
        t['segment_started_at'] = None


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


def pause_timer(timer_id: str):
    """Freeze a running timer, resumable later from the same elapsed point."""
    with _lock:
        t = _timers.get(timer_id)
        if t and t['state'] == 'running':
            _bank_running_segment(t, int(time.time()))
            t['state'] = 'paused'
        return t


def resume_timer(timer_id: str):
    """Continue a paused timer without losing the time already banked."""
    with _lock:
        t = _timers.get(timer_id)
        if t and t['state'] == 'paused':
            t['segment_started_at'] = int(time.time())
            t['state'] = 'running'
        return t


def stop_timer(timer_id: str):
    """Terminal freeze (only reset_timer can bring it back). Valid from
    either running or paused - banks the live segment first if running,
    so pause->stop and stop-while-running both land on the same elapsed
    value regardless of which raced in first (both paths go through the
    same lock, and _bank_running_segment() is a no-op once already
    paused/stopped)."""
    with _lock:
        t = _timers.get(timer_id)
        if t and t['state'] in ('running', 'paused'):
            _bank_running_segment(t, int(time.time()))
            t['state'] = 'stopped'
        return t


def reset_timer(timer_id: str):
    with _lock:
        t = _timers.get(timer_id)
        if t:
            t['accumulated_s'] = 0
            t['segment_started_at'] = int(time.time())
            t['state'] = 'running'
        return t


def delete_timer(timer_id: str) -> bool:
    with _lock:
        return _timers.pop(timer_id, None) is not None


def mark_finished(timer_id: str):
    with _lock:
        t = _timers.get(timer_id)
        if t:
            _bank_running_segment(t, int(time.time()))
            t['state'] = 'finished'
        return t


def get_elapsed(t: dict) -> int:
    if t['state'] == 'running' and t['segment_started_at'] is not None:
        return t['accumulated_s'] + (int(time.time()) - t['segment_started_at'])
    return t['accumulated_s']
