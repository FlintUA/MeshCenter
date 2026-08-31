#!/usr/bin/env python3
"""One-shot-until-resolved assignment of an installation's confirmed time.

Stage C of the installation-ID rollout. installation.assigned_at/time_source
(meshsrv/instance_manager.py, schema v2) start as null/"pending" (Stage B's
default) because a fresh or migrated install has no way yet to know whether
the system clock can be trusted - meshsrv.time_service's NTP-sync status
isn't even initialized until meshsrv.time_service.start_background_thread()
runs, which happens later in server.py's start_runtime() than
instance_manager.load_or_create() does.

This module owns the wait: poll meshsrv.installation_identity's
get_confirmed_utc_time() (itself backed by time_service's already-running
cache - no new timedatectl polling here) every `poll_interval` seconds,
for up to `timeout` seconds total, since a cold-boot device may need real
wall-clock time for NTP to actually sync before this can succeed. On first
success, save assigned_at/time_source once and stop. On giving up,
time_source stays "pending" - nothing is written, so a later restart's own
call to this same function gets a clean second chance.

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

_DEFAULT_POLL_INTERVAL = 15
_DEFAULT_TIMEOUT = 300


def assign_installation_time_when_confirmed(
    instance_manager: InstanceManager,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: float = _DEFAULT_TIMEOUT,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll until NTP sync is confirmed or `timeout` seconds pass.

    Returns True if assigned_at/time_source were set, False on give-up.
    `sleep_fn`/`monotonic_fn` are injectable so tests can exercise the
    retry/give-up behavior without any real sleeping.
    """
    deadline = monotonic_fn() + timeout
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

        if monotonic_fn() >= deadline:
            return False
        sleep_fn(poll_interval)


def start_background_assignment(instance_manager: InstanceManager) -> threading.Thread:
    """Run assign_installation_time_when_confirmed() on a daemon thread.
    Call once from server.py's start_runtime(), after
    meshsrv.time_service.start_background_thread() so the NTP cache it
    reads is already initialized."""
    thread = threading.Thread(
        target=assign_installation_time_when_confirmed,
        args=(instance_manager,),
        daemon=True,
        name="installation-time-assignment",
    )
    thread.start()
    return thread
