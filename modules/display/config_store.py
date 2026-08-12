"""Persisted e-paper settings (data/epaper_config.json). e-Paper Stage 1
plan, Phase 7.

Instance-scoped (data/, not a per-profile directory) since the physical
e-paper HAT is attached to this Pi, not to a specific radio profile -
switching radio profiles doesn't change which display is wired up.
Matches camera_config.json's placement for the same reason.

enabled/refresh_mode/debounce_seconds are meant to be applied live to a
running DisplayManager (see api/api_hardware_display.py's settings
route). pins/spi/refresh_timeout are persisted here too but are only ever
applied through the separate, explicit re-init action - a bad pin value
can reproduce the BUSY-hang debugging from Phases 1-2, this time live
instead of at wiring time, so those fields deliberately don't autosave or
apply on their own.
"""

from __future__ import annotations

from storage.json_store import safe_read_json, safe_write_json

DEFAULT_EPAPER_CONFIG: dict = {
    "enabled": True,
    "refresh_mode": "debounce",
    "debounce_seconds": 30.0,
    "pins": {"rst": 17, "dc": 25, "cs": 8, "busy": 24, "pwr": 18},
    "spi": {"bus": 0, "device": 0},
    "refresh_timeout": 75.0,
}


def load_epaper_config(path: str) -> dict:
    data = safe_read_json(path, dict(DEFAULT_EPAPER_CONFIG))
    merged = dict(DEFAULT_EPAPER_CONFIG)
    if isinstance(data, dict):
        merged.update(data)
        # Shallow update above would let a partial "pins"/"spi" dict from
        # disk silently drop keys not present in it - merge those one
        # level deeper instead.
        for key in ("pins", "spi"):
            if isinstance(data.get(key), dict):
                merged[key] = {**DEFAULT_EPAPER_CONFIG[key], **data[key]}
    return merged


def save_epaper_config(path: str, config: dict) -> None:
    safe_write_json(path, config)
