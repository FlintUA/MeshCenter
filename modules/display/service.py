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

from modules.display.drivers.waveshare_213g import Waveshare213gDriver
from modules.display.gpio_registry import GpioRegistry
from modules.display.manager import DisplayManager
from modules.display.pages.status import StatusScreenData, render

logger = logging.getLogger("epaper_service")

POLL_INTERVAL_SECONDS = 5.0


def build_display_manager() -> DisplayManager:
    driver = Waveshare213gDriver(gpio_registry=GpioRegistry())
    return DisplayManager(driver)


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
    stop_event: threading.Event | None = None,
) -> None:
    stop_event = stop_event or threading.Event()
    content_state = _ContentState()

    while not stop_event.is_set():
        try:
            _poll_once(
                manager, state_lock, nodes, get_radio_status, get_cpu_percent,
                get_ram_percent, get_listener_alive, local_node_name, content_state,
            )
        except Exception:
            logger.exception("epaper_worker: poll failed")
        stop_event.wait(POLL_INTERVAL_SECONDS)


def _poll_once(
    manager, state_lock, nodes, get_radio_status, get_cpu_percent,
    get_ram_percent, get_listener_alive, local_node_name, content_state,
) -> None:
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
    cpu_percent = get_cpu_percent()
    ram_percent = get_ram_percent()

    content_key = "|".join(str(v) for v in (
        meshcenter_status, radio_status, local_node_name, node_count,
        last_rx, cpu_percent, ram_percent,
    ))
    last_update = content_state.stamp(content_key)

    data = StatusScreenData(
        meshcenter_status=meshcenter_status,
        radio_status=radio_status,
        node_name=local_node_name,
        node_count=node_count,
        last_rx=last_rx,
        cpu_percent=cpu_percent,
        ram_percent=ram_percent,
        last_update=last_update,
    )
    image = render(manager.capabilities, data)
    manager.mark_dirty(image)
