"""Runtime environment / capability detection (Docker support, DK1).

One explicit switch: the environment variable ``MESHCENTER_RUNTIME``. Only the
exact value ``container`` selects container mode - there are deliberately no
heuristics (``/.dockerenv``, cgroup sniffing, ...). With the variable absent
(every native Raspberry Pi / Linux install) every capability keeps its
historical value and every call site behaves exactly as before.

Everything is evaluated on each call (never cached at import) so tests and
the entrypoint can flip the variable without re-importing anything.

Stdlib only; safe to import from any Core module.
"""
from __future__ import annotations

import os
import signal
from typing import Any, Dict

RUNTIME_ENV = "MESHCENTER_RUNTIME"
TRUST_HOST_CLOCK_ENV = "MESHCENTER_TRUST_HOST_CLOCK"
VERSION_ENV = "MESHCENTER_VERSION"
CONFIG_DIR_ENV = "MESHCENTER_CONFIG_DIR"

CONTAINER = "container"
NATIVE = "native"

_TRUTHY = {"1", "true", "yes", "on"}


def is_container() -> bool:
    return os.environ.get(RUNTIME_ENV, "").strip().lower() == CONTAINER


def mode() -> str:
    return CONTAINER if is_container() else NATIVE


def trust_host_clock() -> bool:
    """Container only: treat the host's clock as NTP-synchronized.

    A container has no ``timedatectl`` so ``time_service`` can never confirm
    NTP sync itself; the operator vouches for the host (a NAS syncs via DSM)
    with ``MESHCENTER_TRUST_HOST_CLOCK=1``. Ignored natively. Code default: off.
    """
    if not is_container():
        return False
    return os.environ.get(TRUST_HOST_CLOCK_ENV, "").strip().lower() in _TRUTHY


def capabilities() -> Dict[str, Any]:
    """Capability flags. Natively every flag has its historical value."""
    container = is_container()
    return {
        "mode": mode(),
        "restart_method": "signal" if container else "systemctl",
        "can_self_restart": True,
        "can_reboot_host": not container,
        "can_manage_wifi": not container,
        "can_self_update": not container,
        "can_edit_hardware_config": not container,
        "has_host_metrics": not container,
        # The host's thermal zone is not "this machine's" temperature: hidden
        # everywhere (System card via /api/system/info, and the footer) together.
        "has_cpu_temp": not container,
        "hardware_io": not container,
        # network (RTSP) cameras need no host hardware; they do need ffmpeg, which the Docker image does
        # not include yet (+625 MB with Debian's package), so there they show "ffmpeg not available"
        "network_camera": True,
        "trust_host_clock": trust_host_clock(),
    }


def refusal(capability: str) -> Dict[str, Any]:
    """JSON body for an operation refused because the capability is off."""
    return {
        "ok": False,
        "error": "This action is not available when MeshCenter runs in a container.",
        "error_code": "not_available_in_container",
        "capability": capability,
    }


def request_self_restart() -> None:
    """Container restart: SIGTERM to this (gunicorn worker) process.

    The gunicorn master respawns a fresh worker, which re-imports ``wsgi``
    and re-runs ``start_runtime()``; the old worker's death releases the
    runtime ``flock`` and (PR_SET_PDEATHSIG) the adapter subprocess. Works
    independent of any Docker restart policy.
    """
    os.kill(os.getpid(), signal.SIGTERM)
