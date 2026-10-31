"""Which swap is active (ADR-0013 Decision 6, "Memory and swap hygiene").

Raspberry Pi OS Trixie uses zram (compressed swap in RAM): swapped pages never reach the SD card. Older releases and some
images use a swap file on the card, where a swapped page can persist. The vault reports this; it never changes the system.
"""
from typing import Dict, List

_PROC_SWAPS = "/proc/swaps"


def inspect_swaps(path: str = _PROC_SWAPS) -> Dict[str, object]:
    """``{"devices": [{"name", "type", "zram"}], "disk_swap": bool, "known": bool}``.

    ``known`` is False when the file cannot be read (not Linux, a restricted container): then nothing is claimed."""
    devices: List[Dict[str, object]] = []
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            lines = handle.read().splitlines()[1:]          # the first line is the column header
    except OSError:
        return {"devices": [], "disk_swap": False, "known": False}
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        devices.append({"name": name, "type": parts[1], "zram": name.startswith("/dev/zram")})
    return {"devices": devices, "disk_swap": any(not d["zram"] for d in devices), "known": True}
