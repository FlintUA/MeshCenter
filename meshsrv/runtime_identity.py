#!/usr/bin/env python3
"""Runtime discovery helpers for the local MeshCenter instance."""

from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path


def resolve_meshtastic_cli(configured_path: str = "", project_dir: str | os.PathLike | None = None) -> str:
    """Return an absolute executable path to the Meshtastic CLI.

    Resolution order:
    1. Explicit path from config.py
    2. CLI next to the active Python interpreter (virtual environment)
    3. <project>/venv/bin/meshtastic
    4. PATH lookup via shutil.which()

    Raises RuntimeError instead of allowing subprocess to receive an empty path.
    """
    project = Path(project_dir or Path(__file__).resolve().parents[1]).resolve()
    configured = str(configured_path or "").strip()

    candidates: list[Path] = []
    if configured:
        candidate = Path(os.path.expanduser(configured))
        if not candidate.is_absolute():
            candidate = project / candidate
        candidates.append(candidate)

    candidates.extend([
        Path(sys.executable).resolve().parent / "meshtastic",
        project / "venv" / "bin" / "meshtastic",
        Path.home() / ".local" / "bin" / "meshtastic",
    ])

    path_match = shutil.which("meshtastic")
    if path_match:
        candidates.append(Path(path_match))

    checked: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.expanduser().resolve()
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        checked.append(text)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return text

    raise RuntimeError(
        "Meshtastic CLI was not found or is not executable. Checked: "
        + ", ".join(checked)
    )


def discover_serial_ports() -> list[str]:
    """Return likely Meshtastic serial ports in a stable, deduplicated order.

    Persistent /dev/serial/by-id links are preferred because ttyACM numbers can
    change when a different radio is connected.
    """
    patterns = (
        "/dev/serial/by-id/*",
        "/dev/ttyACM*",
        "/dev/ttyUSB*",
    )
    result: list[str] = []
    seen_real: set[str] = set()

    for pattern in patterns:
        for candidate in sorted(glob.glob(pattern)):
            if not os.path.exists(candidate):
                continue
            real = os.path.realpath(candidate)
            if real in seen_real:
                continue
            seen_real.add(real)
            result.append(candidate)

    return result


def resolve_serial_port(configured_port: str = "") -> str:
    """Resolve a usable serial port without silently choosing among many radios.

    The configured port is preferred. If it disappeared after a physical radio
    replacement, a single discovered serial device is accepted. Multiple
    candidates remain an error because MeshCenter currently controls one active
    radio at a time.
    """
    port = str(configured_port or "").strip()
    if port and os.path.exists(port):
        return port

    candidates = discover_serial_ports()
    if len(candidates) == 1:
        return candidates[0]

    if not port:
        if not candidates:
            raise RuntimeError("MESHTASTIC_PORT is empty and no serial radio was found")
        raise RuntimeError(
            "MESHTASTIC_PORT is empty and multiple serial devices were found: "
            + ", ".join(candidates)
        )

    if not candidates:
        raise RuntimeError(
            f"Configured Meshtastic serial port does not exist: {port}; "
            "no replacement serial radio was found"
        )

    raise RuntimeError(
        f"Configured Meshtastic serial port does not exist: {port}; "
        "multiple replacement serial devices were found: "
        + ", ".join(candidates)
    )


def meshtastic_command(cli_path: str, serial_port: str, *arguments: str) -> list[str]:
    """Build a Meshtastic CLI command pinned to one serial radio."""
    cli = str(cli_path or "").strip()
    port = str(serial_port or "").strip()
    if not cli:
        raise RuntimeError("Meshtastic CLI path is empty")
    if not port:
        raise RuntimeError("Meshtastic serial port is empty")
    return [cli, "--port", port, *map(str, arguments)]
