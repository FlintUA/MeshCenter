"""The only module allowed to invoke the privileged
scripts/meshcenter-network-helper (via `sudo -n`) for Wi-Fi connection
management. Nothing else in the codebase should shell out to nmcli/iw as
root - api/api_system.py's Wi-Fi routes call the functions here instead.

Mirrors hardware/hardware_config.py's shape (_run_helper() wrapping
`sudo -n <helper> <args>`, never raising, always returning
{"ok": True/False, ...}) - see that module's own docstring for the
reasoning behind the pattern (a missing/misconfigured sudoers rule must
fail immediately and distinctly, `sudo -n`, rather than hang the request
on a password prompt nobody can answer).

The one addition here: connect() passes the Wi-Fi password to the
helper's stdin (`subprocess.run(..., input=password)`), never as an argv
element - `ps aux` / `/proc/<pid>/cmdline` must never expose it for this
process, the intermediate `sudo` process, or the helper itself (P1 #8;
the previous direct `nmcli ... password <pw>` call put it straight into
argv).

Unprivileged reads (`iw dev wlan0 link`, `iwgetid -r`) do NOT go through
this module or the helper - they need no root on the target systems, so
routing them through a sudo-gated helper would only add attack surface
for nothing. api/api_system.py still calls those directly.
"""

from __future__ import annotations

import subprocess

HELPER_PATH = "/usr/local/sbin/meshcenter-network-helper"
HELPER_TIMEOUT = 15
# nmcli association + DHCP can legitimately take longer than a plain
# list/scan query.
CONNECT_TIMEOUT = 45


def _run_helper(args: list[str], input_text: str | None = None, timeout: int = HELPER_TIMEOUT) -> dict:
    """Never raises. `sudo -n` (no interactive prompt) means a missing/
    misconfigured sudoers rule fails immediately and distinctly rather than
    hanging the request waiting on a password prompt nobody can answer."""
    try:
        result = subprocess.run(
            ["sudo", "-n", HELPER_PATH, *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"ok": False, "reason": "sudo is not available on this system"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"meshcenter-network-helper timed out after {timeout}s"}
    except Exception as exc:  # noqa: BLE001 - never raise out of this layer
        return {"ok": False, "reason": f"meshcenter-network-helper failed: {exc}"}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "password is required" in stderr.lower():
            reason = (
                "sudo is not configured for meshcenter-network-helper - install "
                "deploy/meshcenter-wifi.sudoers (the installer does this automatically)"
            )
        else:
            reason = stderr or f"meshcenter-network-helper exited with code {result.returncode}"
        return {"ok": False, "reason": reason}

    return {"ok": True, "stdout": (result.stdout or "").strip()}


def list_wifi_connections() -> dict:
    """Saved Wi-Fi connection names (nmcli's NAME column, filtered to the
    802-11-wireless TYPE). {"ok": True, "ssids": set(...)} on success,
    {"ok": False, "reason": ...} otherwise - never raises."""
    result = _run_helper(["list-connections"])
    if not result.get("ok"):
        return result

    ssids = set()
    for line in result.get("stdout", "").splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "802-11-wireless":
            ssids.add(parts[0])
    return {"ok": True, "ssids": ssids}


def scan() -> dict:
    """Raw `iw dev wlan0 scan` output, `{"ok": True, "stdout": "..."}` or
    `{"ok": False, "reason": ...}`. api/api_system.py's own parser turns
    this into structured network entries - kept there rather than
    duplicated in bash, see this module's own docstring."""
    return _run_helper(["scan"])


def connect(ssid: str, password: str = "") -> dict:
    """Connect to `ssid`. `password` is sent to the helper's stdin, never
    as an argv element (P1 #8 - see module docstring). Empty password
    means an open network."""
    return _run_helper(["connect", ssid], input_text=password, timeout=CONNECT_TIMEOUT)


def forget(ssid: str) -> dict:
    return _run_helper(["forget", ssid])
