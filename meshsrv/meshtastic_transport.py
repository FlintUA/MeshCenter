"""Consolidated `meshtastic --info` invocation.

Extracted 1:1 from the old meshsrv/meshsrv.py (get_info/run_command) - the
only behavior this preserves on purpose is that serial_port is optional:
when empty/None, --port is simply omitted from the command line, unlike
meshsrv.runtime_identity.meshtastic_command() which requires a non-empty
port and raises RuntimeError otherwise. Do not switch callers to that
helper here - it would be a silent behavior change.

send_message no longer goes through the CLI at all (api/api_chat.py,
server.py, meshsrv/schedule_actions.py call the Meshtastic SDK's
SerialInterface directly), and --listen is a separate long-lived
subprocess.Popen managed elsewhere - neither belongs in this module.
"""

import subprocess


def run_command(cmd, timeout=30):
    """
    Run Meshtastic CLI command and return subprocess result.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout
    )


def get_info(meshtastic_cmd, serial_port=None, timeout=30):
    """
    Run: meshtastic --info
    """
    cmd = [meshtastic_cmd]
    if serial_port:
        cmd.extend(["--port", serial_port])
    cmd.append("--info")
    return run_command(cmd, timeout=timeout)
