"""Persisted e-paper settings (data/epaper_config.json). e-Paper Stage 1
plan, Phase 7; model selection added in Stage 2 (WeAct 1.54"), Phase 4.

Instance-scoped (data/, not a per-profile directory) since the physical
e-paper HAT is attached to this Pi, not to a specific radio profile -
switching radio profiles doesn't change which display is wired up.
Matches camera_config.json's placement for the same reason.

enabled/refresh_mode/debounce_seconds are meant to be applied live to a
running DisplayManager (see api/api_hardware_display.py's settings
route). model/pins/spi/refresh_timeout are persisted here too but are
only ever applied through the separate, explicit re-init action - a bad
pin value (or a model switch, which changes DisplayCapabilities - size,
colors - out from under whatever's currently rendered) can reproduce the
BUSY-hang debugging from Phases 1-2, this time live instead of at wiring
time, so those fields deliberately don't autosave or apply on their own.
"""

from __future__ import annotations

import math

from storage.json_store import safe_read_json, safe_write_json

# One entry per supported panel. Each model's own pin defaults - not a
# single shared default - since different panels use different pins (or,
# for WeAct, no PWR pin at all - seemodules/display/drivers/weact_154.py).
MODEL_DEFAULT_PINS: dict[str, dict] = {
    "waveshare_213g": {"rst": 17, "dc": 25, "cs": 8, "busy": 24, "pwr": 18},
    "weact_154": {"rst": 17, "dc": 25, "cs": 8, "busy": 24},
}

MODEL_DISPLAY_NAMES: dict[str, str] = {
    "waveshare_213g": 'Waveshare 2.13" e-Paper HAT (G)',
    "weact_154": 'WeAct Studio 1.54" e-Paper Module (SSD1681)',
}

DEFAULT_MODEL = "waveshare_213g"  # backward-compat default for configs saved before Stage 2

# task 40: pages the auto-rotation feature is allowed to cycle through -
# deliberately excludes "message" (KNOWN_SHOW_PAGES in
# api/api_hardware_display.py has that one too, for manual "Show on
# Display" only). Order here is the canonical rotation order the UI/
# service.py both follow, regardless of what order a saved config's
# rotation_pages list happens to be in.
ROTATION_ALLOWED_PAGES: tuple[str, ...] = ("status", "radio", "power", "system")

DEFAULT_EPAPER_CONFIG: dict = {
    "enabled": True,
    "model": DEFAULT_MODEL,
    "refresh_mode": "debounce",
    "debounce_seconds": 30.0,
    "pins": dict(MODEL_DEFAULT_PINS[DEFAULT_MODEL]),
    "spi": {"bus": 0, "device": 0},
    "refresh_timeout": 75.0,
    # Off by default - an empty rotation_pages list is already inert (see
    # modules/display/service.py's _poll_once()), enabled defaulting to
    # False is belt-and-suspenders for a config file that has
    # rotation_pages populated but was never explicitly turned on.
    "rotation_enabled": False,
    "rotation_pages": [],
    "rotation_interval_seconds": 30.0,
}


def _sanitize_seconds(merged: dict, key: str) -> None:
    """Defense-in-depth against a hand-edited config file, or a value saved
    before api/api_hardware_display.py's own NaN/Infinity range validation
    existed (audit review 2026-09-29, F2b): DisplayManager reads these
    straight off this dict (modules/display/service.py's build_driver()),
    so a bad number here breaks debounce/rotation/refresh timing silently
    (e.g. `time.monotonic() + NaN` makes every later "is it time yet"
    comparison False forever - the panel would simply stop refreshing).
    Falls back to the default rather than a hard failure, same spirit as
    rotation_pages filtering below. Logs a WARNING naming the key, the bad
    value and the replacement - a silent reset here would hide exactly the
    kind of corruption this function exists to catch."""
    value = merged.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        default = DEFAULT_EPAPER_CONFIG[key]
        print(
            f"[EPAPER] WARNING: {key}={value!r} in epaper_config.json is not a finite number - "
            f"resetting to default {default!r} on load",
            flush=True,
        )
        merged[key] = default


def load_epaper_config(path: str) -> dict:
    data = safe_read_json(path, dict(DEFAULT_EPAPER_CONFIG))
    merged = dict(DEFAULT_EPAPER_CONFIG)
    if isinstance(data, dict):
        merged.update(data)
        # Shallow update above would let a partial "pins"/"spi" dict from
        # disk silently drop keys not present in it - merge those one
        # level deeper instead. Merge against the *configured model's*
        # defaults, not always DEFAULT_MODEL's - a saved WeAct config
        # missing e.g. "cs" should fall back to WeAct's own default, not
        # silently pick up a Waveshare pin number.
        model = merged.get("model", DEFAULT_MODEL)
        pin_defaults = MODEL_DEFAULT_PINS.get(model, MODEL_DEFAULT_PINS[DEFAULT_MODEL])
        if isinstance(data.get("pins"), dict):
            merged["pins"] = {**pin_defaults, **data["pins"]}
        else:
            merged["pins"] = dict(pin_defaults)
        if isinstance(data.get("spi"), dict):
            merged["spi"] = {**DEFAULT_EPAPER_CONFIG["spi"], **data["spi"]}
        # Defense-in-depth against a hand-edited config file - the API
        # layer (api/api_hardware_display.py) already validates
        # rotation_pages against ROTATION_ALLOWED_PAGES before it's ever
        # saved, so this only matters for a file edited outside that path.
        if isinstance(data.get("rotation_pages"), list):
            merged["rotation_pages"] = [
                p for p in data["rotation_pages"] if p in ROTATION_ALLOWED_PAGES
            ]
        else:
            merged["rotation_pages"] = []
        for key in ("debounce_seconds", "refresh_timeout", "rotation_interval_seconds"):
            _sanitize_seconds(merged, key)
    return merged


def save_epaper_config(path: str, config: dict) -> bool:
    return safe_write_json(path, config)
