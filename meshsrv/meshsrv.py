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


def send_text(meshtastic_cmd, text, channel=0, dest=None, timeout=45, serial_port=None):
    """
    Send text through Meshtastic CLI.
    If dest is provided, sends a direct message.
    Otherwise sends to the selected channel.
    """
    cmd = [meshtastic_cmd]
    if serial_port:
        cmd.extend(["--port", serial_port])
    cmd.extend(["--ch-index", str(channel)])

    if dest:
        cmd.extend(["--dest", dest])

    cmd.extend(["--sendtext", text])

    return run_command(cmd, timeout=timeout)
