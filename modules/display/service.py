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

from modules.display.config_store import DEFAULT_EPAPER_CONFIG, DEFAULT_MODEL
from modules.display.drivers.base import DisplayDriver
from modules.display.drivers.waveshare_213g import Waveshare213gDriver
from modules.display.drivers.weact_154 import Weact154Driver
from modules.display.gpio_registry import GpioRegistry
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.manager import DisplayManager
from modules.display.models import EventPriority, RefreshMode
from modules.display.pages import alert as alert_page
from modules.display.pages import message as message_page
from modules.display.pages import power as power_page
from modules.display.pages import radio as radio_page
from modules.display.pages import system as system_page
from modules.display.pages.status import StatusScreenData, render

# One entry per supported panel - e-Paper Stage 2 plan (WeAct 1.54"),
# Phase 4. Both driver classes share the same (pins, spi, gpio_registry)
# constructor shape by convention (see DisplayDriver), so selecting a
# model is just picking which class to instantiate.
DRIVER_CLASSES: dict[str, type[DisplayDriver]] = {
    "waveshare_213g": Waveshare213gDriver,
    "weact_154": Weact154Driver,
}

# Pages selectable via POST /api/hardware/display/show/<page> (plan
# section 34). "status" is the default/fallback - an unknown or not-yet-
# implemented page name (radio/power/environment/message pages beyond
# what Phase 10 built) falls back to it rather than erroring the worker.
KNOWN_PAGES = ("status", "radio", "power", "system", "message")

logger = logging.getLogger("epaper_service")

POLL_INTERVAL_SECONDS = 5.0

# Plan section 68 lists 5 critical-event triggers for the Alert Screen.
# Final disposition (not a TODO - each of the other 3 was a deliberate
# decision, made once, not revisited absent a real reason to):
#   1. Radio offline - wired below.
#   2. Critically low power - wired below (CRITICAL_LOW_BATTERY_PERCENT).
#   3. "Hardware display error" - deliberately NOT wired, permanently.
#      Alerting *on the display* about the display itself being broken is
#      incoherent - DisplayManager.status == ERROR already means the
#      display can't reliably show anything anyway (see the Hardware card
#      for that signal instead).
#   4. "Internal fault" - deliberately NOT wired. No single well-defined
#      signal for this exists in server.py today, and inventing one just
#      for this alert would be speculative. Revisit only if a concrete
#      "internal fault" concept is added to server.py for other reasons.
#   5. General "critical hardware errors" (across all peripherals) - NOT
#      wired, and intentionally not folded into #4. There is no cross-
#      device-type health registry to poll (DeviceDriver/CameraManager is
#      camera-only; power/environment sensors and the e-paper driver
#      itself aren't registered anywhere in common). Building one just for
#      this would be speculative infrastructure. Tracked as its own
#      backlog item with an explicit trigger condition (not "someday") -
#      see epaper_stage2_weact.md / MEMORY.md's e-paper backlog entry.
CRITICAL_LOW_BATTERY_PERCENT = 10

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


def build_driver(config: dict, gpio_registry: GpioRegistry) -> DisplayDriver:
    model = config.get("model", DEFAULT_MODEL)
    driver_cls = DRIVER_CLASSES.get(model)
    if driver_cls is None:
        raise ValueError(f"Unknown e-paper model: {model!r}")
    return driver_cls(
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
    get_battery_percent: Callable[[], float | None] = lambda: None,
    get_active_page: Callable[[], str] = lambda: "status",
    get_last_error: Callable[[], str] = lambda: "",
    get_power_readings: Callable[[], dict[str, float | None]] = lambda: {},
    get_cpu_temp: Callable[[], float | None] = lambda: None,
    get_latest_message: Callable[[], dict[str, Any] | None] = lambda: None,
    get_display_language: Callable[[], str] = lambda: DEFAULT_LOCALE,
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
                    get_battery_percent, get_active_page, get_last_error,
                    get_power_readings, get_cpu_temp, get_latest_message, get_display_language,
                )
        except Exception:
            logger.exception("[EPAPER] epaper_worker: poll failed")
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


def _render_page(page: str, manager, local_node_name, get_radio_status, get_listener_alive,
                  get_last_error, get_power_readings, get_cpu_percent, get_ram_percent,
                  get_cpu_temp, get_latest_message, locale: str = DEFAULT_LOCALE):
    """Build+render whichever page is currently selected (plan section
    34's "Show on Display"), using only already-collected state - same
    invariant as the Status Screen (section 40)."""
    caps = manager.capabilities

    if page == "radio":
        radio_info = get_radio_status() or {}
        mode = radio_info.get("mode", "error")
        status = "online" if mode == "connected" else ("warning" if mode in ("reconnecting", "releasing") else "offline")
        return radio_page.render(caps, radio_page.RadioScreenData(
            status=status, mode=mode, node_name=local_node_name,
            listener_running=get_listener_alive(), last_error=get_last_error(),
        ), locale=locale)

    if page == "power":
        readings = get_power_readings() or {}
        return power_page.render(caps, power_page.PowerScreenData(
            voltage=readings.get("voltage"), current=readings.get("current"), power=readings.get("power"),
        ), locale=locale)

    if page == "system":
        return system_page.render(caps, system_page.SystemScreenData(
            cpu_percent=_bucket(get_cpu_percent()), ram_percent=_bucket(get_ram_percent()),
            cpu_temp_c=get_cpu_temp(),
        ), locale=locale)

    if page == "message":
        latest = get_latest_message() or {}
        return message_page.render(caps, message_page.MessageScreenData(
            sender=latest.get("sender", ""), text=latest.get("text", ""), time=latest.get("time", ""),
        ), locale=locale)

    return None  # caller falls back to the Status Screen


def _poll_once(
    manager, state_lock, nodes, get_radio_status, get_cpu_percent,
    get_ram_percent, get_listener_alive, local_node_name, content_state,
    get_battery_percent=lambda: None,
    get_active_page=lambda: "status",
    get_last_error=lambda: "",
    get_power_readings=lambda: {},
    get_cpu_temp=lambda: None,
    get_latest_message=lambda: None,
    get_display_language=lambda: DEFAULT_LOCALE,
) -> None:
    data, content_key = _build_status_fields(
        state_lock, nodes, get_radio_status, get_cpu_percent, get_ram_percent,
        get_listener_alive, local_node_name,
    )
    locale = get_display_language()

    battery_percent = get_battery_percent()
    critical_title = None
    critical_reason = ""
    if data.radio_status == "offline":
        critical_title = t("radio_offline_title", locale)
        critical_reason = t("connection_lost", locale)
    elif battery_percent is not None and battery_percent <= CRITICAL_LOW_BATTERY_PERCENT:
        critical_title = t("low_battery_title", locale, percent=battery_percent)
        critical_reason = t("critically_low_power", locale)

    if critical_title:
        # Bypasses debounce entirely (plan section 68), and overrides
        # whatever page is manually pinned - deliberately doesn't touch
        # content_state/_last content hash bookkeeping, since recovery is
        # meant to fall back to the normal Status Screen at the next
        # allowed (debounced) refresh, not stay pinned to whatever content
        # hash the alert interrupted.
        #
        # device_path/last_seen reuse data already gathered above/nearby
        # (radio_info's serial_port, the same last_rx the Status Screen
        # would have shown) rather than collecting anything new - same
        # "already-collected state only" invariant as section 40.
        radio_info = get_radio_status() or {}
        image = alert_page.render(
            manager.capabilities,
            alert_page.AlertScreenData(
                title=critical_title,
                reason=critical_reason,
                node_name=local_node_name,
                device_path=radio_info.get("serial_port", ""),
                last_seen=data.last_rx,
            ),
            locale=locale,
        )
        manager.mark_dirty(image, priority=EventPriority.CRITICAL)
        return

    active_page = get_active_page()
    if active_page and active_page != "status":
        image = _render_page(
            active_page, manager, local_node_name, get_radio_status, get_listener_alive,
            get_last_error, get_power_readings, get_cpu_percent, get_ram_percent,
            get_cpu_temp, get_latest_message, locale,
        )
        if image is not None:
            manager.mark_dirty(image)
            return

    data.last_update = content_state.stamp(content_key)
    image = render(manager.capabilities, data, locale=locale)
    manager.mark_dirty(image)


def build_status_image_now(
    manager, state_lock, nodes, get_radio_status, get_cpu_percent,
    get_ram_percent, get_listener_alive, local_node_name,
    locale: str = DEFAULT_LOCALE,
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
    return render(manager.capabilities, data, locale=locale)


def build_page_image_now(
    page, manager, local_node_name, get_radio_status, get_listener_alive,
    get_last_error, get_power_readings, get_cpu_percent, get_ram_percent,
    get_cpu_temp, get_latest_message,
    locale: str = DEFAULT_LOCALE,
):
    """For POST /api/hardware/display/show/<page> - builds whichever page
    was requested right now, for an immediate (CRITICAL) push. Returns
    None for "status" or an unknown page name; the route falls back to
    build_status_image_now() for "status" and to a 404 for a page this
    phase didn't build (radio/power/system/message only - see
    KNOWN_PAGES)."""
    return _render_page(
        page, manager, local_node_name, get_radio_status, get_listener_alive,
        get_last_error, get_power_readings, get_cpu_percent, get_ram_percent,
        get_cpu_temp, get_latest_message, locale,
    )
