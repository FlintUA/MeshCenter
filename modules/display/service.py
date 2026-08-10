"""Wires MeshCenter's already-collected in-memory state into the Status
Screen and DisplayManager.mark_dirty() calls. e-Paper Stage 1 plan, Phase 5
(section 40: only already-collected internal state - nodes/radio
status/CPU/RAM - never a fresh `meshtastic --info` call triggered from
here).

epaper_worker() polls every POLL_INTERVAL_SECONDS instead of hooking every
place in server.py where nodes/messages/radio status change. That is
deliberate, not a shortcut: DisplayManager already debounces and
hash-dedupes physical refreshes (modules/display/manager.py), so a poll
that finds nothing changed costs one skipped comparison, not a refresh -
functionally equivalent to precise event hooks for a screen whose own DoD
is "updates within the debounce window", while touching zero of
server.py's existing state-mutation code paths (server.py's own
register_*_routes / background-worker functions are passed the specific
globals they need as parameters, and epaper_worker follows that same
dependency-injection-by-parameter-list convention rather than importing
server.py's globals directly).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Callable

from modules.display.config_store import DEFAULT_EPAPER_CONFIG
from modules.display.drivers.waveshare_213g import Waveshare213gDriver
from modules.display.gpio_registry import GpioRegistry
from modules.display.manager import DisplayManager
from modules.display.models import RefreshMode
from modules.display.pages.status import StatusScreenData, render

logger = logging.getLogger("epaper_service")

POLL_INTERVAL_SECONDS = 5.0

# Plan section 20: don't refresh the screen just because of trivial
# telemetry churn (a couple of CPU/RAM percentage points). This has to
# bucket the *value that actually gets rendered*, not just gate the
# "last update" timestamp - DisplayManager.mark_dirty() is called every
# poll regardless, and its own image-hash dedup (manager.py's _refresh())
# only skips a physical refresh when the rendered image is byte-identical
# to the last one. Rounding to raw-percent precision would still change
# the image (and trigger a real ~20s refresh) on every 0.1% fluctuation;
# bucketing to a coarser step means the image - and therefore its hash -
# stays identical across insignificant drift.
_SIGNIFICANCE_BUCKET = 5


def _bucket(value: float | None, step: int = _SIGNIFICANCE_BUCKET) -> float | None:
    if value is None:
        return None
    return round(value / step) * step


def build_driver(config: dict, gpio_registry: GpioRegistry) -> Waveshare213gDriver:
    return Waveshare213gDriver(
        pins=config.get("pins"), spi=config.get("spi"), gpio_registry=gpio_registry,
    )


def build_display_manager(
    config: dict | None = None, gpio_registry: GpioRegistry | None = None,
) -> DisplayManager:
    config = config or dict(DEFAULT_EPAPER_CONFIG)
    registry = gpio_registry or GpioRegistry()
    driver = build_driver(config, registry)
    return DisplayManager(
        driver,
        refresh_mode=RefreshMode(config.get("refresh_mode", "debounce")),
        debounce_seconds=config.get("debounce_seconds"),
        refresh_timeout=config.get("refresh_timeout", 75.0),
    )


class _ContentState:
    """Tracks whether the *content* (everything except the "last update"
    timestamp itself) actually changed since the previous poll.

    Stamping "last update" with wall-clock time on every poll would defeat
    both plan section 61 (it must reflect an actual event, not a live
    clock) and DisplayManager's hash-based dedup (section 22) - the
    timestamp would differ every poll even when nothing else did, forcing
    a physical refresh roughly every minute for no reason. So "last
    update" only advances when the rest of the content's hash changes.
    """

    def __init__(self):
        self._last_content_hash: str | None = None
        self.last_update_str = time.strftime("%H:%M")

    def stamp(self, content_key: str) -> str:
        content_hash = hashlib.sha256(content_key.encode("utf-8")).hexdigest()
        if content_hash != self._last_content_hash:
            self._last_content_hash = content_hash
            self.last_update_str = time.strftime("%H:%M")
        return self.last_update_str


def epaper_worker(
    manager: DisplayManager,
    state_lock,
    nodes: dict,
    get_radio_status: Callable[[], dict[str, Any]],
    get_cpu_percent: Callable[[], float | None],
    get_ram_percent: Callable[[], float | None],
    get_listener_alive: Callable[[], bool],
    local_node_name: str,
    get_enabled: Callable[[], bool] = lambda: True,
    stop_event: threading.Event | None = None,
) -> None:
    stop_event = stop_event or threading.Event()
    content_state = _ContentState()

    while not stop_event.is_set():
        try:
            if get_enabled():
                _poll_once(
                    manager, state_lock, nodes, get_radio_status, get_cpu_percent,
                    get_ram_percent, get_listener_alive, local_node_name, content_state,
                )
        except Exception:
            logger.exception("epaper_worker: poll failed")
        stop_event.wait(POLL_INTERVAL_SECONDS)


def _build_status_fields(
    state_lock, nodes, get_radio_status, get_cpu_percent, get_ram_percent,
    get_listener_alive, local_node_name,
) -> tuple[StatusScreenData, str]:
    """Everything StatusScreenData needs except `last_update` - returned
    alongside a content_key string so callers can decide how "last update"
    should be stamped (poll-driven callers debounce it through
    _ContentState; an explicit manual action can just use "now")."""
    with state_lock:
        node_count = len(nodes)
        last_seen_values = [n.get("last_seen") for n in nodes.values() if n.get("last_seen")]

    last_rx = "--"
    if last_seen_values:
        last_rx = time.strftime("%H:%M", time.localtime(max(last_seen_values)))

    radio_info = get_radio_status() or {}
    mode = radio_info.get("mode", "error")
    if mode == "connected":
        radio_status = "online"
    elif mode in ("reconnecting", "releasing"):
        radio_status = "warning"
    else:
        radio_status = "offline"

    meshcenter_status = "online" if get_listener_alive() else "critical"
    cpu_percent = _bucket(get_cpu_percent())
    ram_percent = _bucket(get_ram_percent())

    content_key = "|".join(str(v) for v in (
        meshcenter_status, radio_status, local_node_name, node_count,
        last_rx, cpu_percent, ram_percent,
    ))
    data = StatusScreenData(
        meshcenter_status=meshcenter_status,
        radio_status=radio_status,
        node_name=local_node_name,
        node_count=node_count,
        last_rx=last_rx,
        cpu_percent=cpu_percent,
        ram_percent=ram_percent,
    )
    return data, content_key


def _poll_once(
    manager, state_lock, nodes, get_radio_status, get_cpu_percent,
    get_ram_percent, get_listener_alive, local_node_name, content_state,
) -> None:
    data, content_key = _build_status_fields(
        state_lock, nodes, get_radio_status, get_cpu_percent, get_ram_percent,
        get_listener_alive, local_node_name,
    )
    data.last_update = content_state.stamp(content_key)
    image = render(manager.capabilities, data)
    manager.mark_dirty(image)


def build_status_image_now(
    manager, state_lock, nodes, get_radio_status, get_cpu_percent,
    get_ram_percent, get_listener_alive, local_node_name,
):
    """Same status-screen content as the poller, but stamps "last update"
    with the current time unconditionally - for an explicit manual action
    (POST /api/hardware/display/refresh), where "now" genuinely is the
    event, unlike a routine 5s poll tick."""
    data, _content_key = _build_status_fields(
        state_lock, nodes, get_radio_status, get_cpu_percent, get_ram_percent,
        get_listener_alive, local_node_name,
    )
    data.last_update = time.strftime("%H:%M")
    return render(manager.capabilities, data)
