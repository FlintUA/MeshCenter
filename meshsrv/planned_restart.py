"""Planned self-restarts of MeshCenter (UPD1).

Every self-restart - the Updates card, "Restart MeshCenter", a radio profile switch,
a host reboot/shutdown - runs `sudo systemctl ...` from a process that systemd is
about to kill. Two things followed from that, and both were noise, not faults:

* the helper itself dies with SIGTERM (`returncode -15`) before it can report
  success, which used to be logged as an ERROR ("Restart after update failed ...
  died with SIGTERM") on two of the three paths;
* the `--listen` child dies with the process, and the health worker / listener
  monitor of the still-alive old process saw "listener down" and "unexpected exit".

`begin()` marks a short window (`WINDOW_S`, 60 s) in which that is expected:
`active()` is consulted by the listener supervisor (the exit is intentional, not
counted) and by the radio-health decision (PAUSED/INFO "MeshCenter is restarting",
never LISTENER_DOWN). The window is process-local on purpose: the new process starts
with none, so a genuine failure after the restart is still reported normally.

Stdlib only, no I/O of its own.
"""
from __future__ import annotations

import signal
import subprocess
import threading
import time
from typing import Callable, Optional, Sequence

WINDOW_S = 60.0

_lock = threading.Lock()
_until = float("-inf")
_reason = ""


def begin(reason: str, window_s: float = WINDOW_S) -> None:
    """Open (or extend) the planned-restart window."""
    global _until, _reason
    with _lock:
        _until = max(_until, time.monotonic() + float(window_s))
        _reason = str(reason or "")


def active() -> bool:
    with _lock:
        return time.monotonic() < _until


def reason() -> str:
    with _lock:
        return _reason if time.monotonic() < _until else ""


def clear() -> None:
    """Close the window (used by tests and when a restart request itself failed)."""
    global _until, _reason
    with _lock:
        _until = float("-inf")
        _reason = ""


def helper_killed_by_the_restart(error: BaseException) -> bool:
    """True when the restart helper ended because of SIGTERM - the restart it asked
    for is already tearing this process group down (returncode -15, or 128+15 if a
    shell/sudo reported it that way)."""
    return getattr(error, "returncode", None) in (-signal.SIGTERM, 128 + signal.SIGTERM)


def run_restart_command(
    command: Sequence[str],
    *,
    label: str,
    log: Callable[..., None],
    source: str = "system",
    info_detail: str = "MeshCenter is restarting",
    runner: Optional[Callable[..., object]] = None,
) -> bool:
    """Run a restart/reboot/shutdown helper. Returns True when it is (or must be assumed
    to be) under way. SIGTERM of the helper is the expected outcome and is logged as INFO;
    any other failure clears the window and is logged as ERROR (`<label> failed`).

    `log(title, level, details, source=...)` is `system_log.log_system_event`."""
    run = runner or subprocess.run
    try:
        run(list(command), check=True, capture_output=True, text=True, timeout=20)
        return True
    except subprocess.CalledProcessError as error:
        if helper_killed_by_the_restart(error):
            log(label, "INFO", info_detail, source=source)
            return True
        clear()
        log(f"{label} failed", "ERROR", str(error), source=source)
    except Exception as error:  # noqa: BLE001 - the helper must never take the thread down
        clear()
        log(f"{label} failed", "ERROR", str(error), source=source)
    return False
