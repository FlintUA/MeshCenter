#!/usr/bin/env python3
"""Two-phase, resolve-once assignment of an installation's confirmed time.

Stage C of the installation-ID rollout. installation.assigned_at/time_source
(meshsrv/instance_manager.py, schema v2) start as null/"pending" (Stage B's
default) because a fresh or migrated install has no way yet to know whether
the system clock can be trusted - meshsrv.time_service's NTP-sync status
isn't even initialized until meshsrv.time_service.start_background_thread()
runs, which happens later in server.py's start_runtime() than
instance_manager.load_or_create() does.

This module owns the wait: poll meshsrv.installation_identity's
get_confirmed_utc_time() (itself backed by time_service's already-running
cache - no new timedatectl polling here) every `fast_poll_interval` seconds
for up to `fast_phase_timeout` seconds, since a cold-boot device may need
real wall-clock time for NTP to actually sync before this can succeed.

If that fast window elapses without confirmation, the same thread does not
stop - it drops to a much slower `slow_poll_interval` and keeps checking
for the rest of the process's lifetime. This matters for a real scenario
already observed on this project's own fleet: a device with no network at
boot (waiting on first-time Wi-Fi setup through the web UI, as seen live on
Droidian) would otherwise stay time_source: "pending" forever unless
someone manually restarts the service - a small long-lived background
thread self-heals that without intervention.

On first confirmation, at any point in either phase, assigned_at/time_source
are saved exactly once and the thread exits - this never re-confirms or
re-saves after it has resolved. If the whole process restarts before the
slow phase ever succeeds, that's fine: start_runtime() calls this function
again from scratch, fast phase first, same as any other startup.

Deliberately not part of InstanceManager itself: that class is synchronous
and side-effect-free (no thread, no sleep) by design; this module is the
one place that turns its polling into a background thread. Deliberately not
part of installation_identity.py either: that module is meant to stay pure
time-reading/generation logic, not startup-sequence orchestration that
mutates instance state.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from meshsrv.installation_identity import get_confirmed_utc_time
from meshsrv.instance_manager import InstanceManager

_FAST_POLL_INTERVAL = 15
_FAST_PHASE_TIMEOUT = 300

# 20 minutes: frequent enough that a device which only needed a few extra
# minutes to get network/NTP up (e.g. finishing first-time Wi-Fi setup)
# self-heals in a reasonable time without a manual restart, but infrequent
# enough that a device which may never sync doesn't spend meaningful CPU/
# wake time on a background correction that stopped being time-critical
# once the fast window already had its shot - this runs on a Pi Zero 2W,
# potentially for a very long uptime.
_SLOW_POLL_INTERVAL = 1200


def assign_installation_time_when_confirmed(
    instance_manager: InstanceManager,
    poll_interval: float = _FAST_POLL_INTERVAL,
    timeout: float = _FAST_PHASE_TIMEOUT,
    slow_poll_interval: float = _SLOW_POLL_INTERVAL,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll every `poll_interval` seconds for up to `timeout` seconds
    (the fast phase); if still unconfirmed, keep polling every
    `slow_poll_interval` seconds indefinitely (the slow phase) until NTP
    sync is confirmed. Always eventually returns True once confirmed -
    there is no permanent give-up short of the process itself exiting.

    `sleep_fn`/`monotonic_fn` are injectable so tests can exercise the
    fast-to-slow transition and the loop's eventual termination without
    any real sleeping.
    """
    fast_deadline = monotonic_fn() + timeout
    in_fast_phase = True
    while True:
        confirmed = get_confirmed_utc_time()
        if confirmed:
            identity = instance_manager.get()
            updated = dict(identity)
            updated["installation"] = dict(identity["installation"])
            updated["installation"]["assigned_at"] = confirmed
            updated["installation"]["time_source"] = "system_ntp"
            instance_manager.save(updated)
            return True

        if in_fast_phase and monotonic_fn() >= fast_deadline:
            in_fast_phase = False
        sleep_fn(poll_interval if in_fast_phase else slow_poll_interval)


def start_background_assignment(instance_manager: InstanceManager) -> threading.Thread:
    """Run assign_installation_time_when_confirmed() on a daemon thread.
    Call once from server.py's start_runtime(), after
    meshsrv.time_service.start_background_thread() so the NTP cache it
    reads is already initialized. Same thread carries the fast phase
    through into the slow phase - no second thread is started."""
    thread = threading.Thread(
        target=assign_installation_time_when_confirmed,
        args=(instance_manager,),
        daemon=True,
        name="installation-time-assignment",
    )
    thread.start()
    return thread
