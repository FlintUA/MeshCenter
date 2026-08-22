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

from modules.display.config_store import DEFAULT_EPAPER_CONFIG, DEFAULT_MODEL, ROTATION_ALLOWED_PAGES
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
from modules.display.renderer import load_font
from modules.display.time_helper import draw_epaper_clock, format_epaper_time

# meshsrv.time_service (Stage 2 of the Time System feature) is the single
# source of truth for "what timezone is the system actually in" - imported
# here rather than duplicating a timedatectl probe, same
# not-reinventing-what-already-exists rule Stage 1/2 followed for CPU/RAM
# readers. Read-only: this module never calls anything that would let the
# e-paper worker mutate time/timezone state.
from meshsrv.time_service import get_status as get_time_status

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

# CPU temperature is a different unit/scale than a CPU/RAM percentage - a
# 5-degree bucket would be far too coarse (real thermal drift is usually
# under 1C between polls), and reusing _SIGNIFICANCE_BUCKET's step for it
# would be a unit mismatch, not a shared constant. This exists for the
# same reason _SIGNIFICANCE_BUCKET does: the System Screen shows 1-decimal
# values (matching the web UI footer's precision, see system.py's fmt()),
# and unbucketed raw values jitter on nearly every poll - showing that
# raw jitter as text would reintroduce the exact refresh-per-tick problem
# _SIGNIFICANCE_BUCKET was added to fix (e-Paper Stage 1 plan, Phase 5).
_TEMP_SIGNIFICANCE_BUCKET = 1.0


def _bucket(value: float | None, step: float = _SIGNIFICANCE_BUCKET) -> float | None:
    if value is None:
        return None
    return round(value / step) * step


def advance_rotation(
    index: int, page_count: int, last_advance_ts: float | None, now: float, interval_seconds: float,
) -> tuple[int, float, bool]:
    """Pure - task 40's auto-rotation timing decision, kept separate from
    _poll_once() so it's testable without a DisplayManager/mocked poll
    pipeline. Returns (new_index, new_last_advance_ts, advanced).

    `advanced` is True exactly when the caller should overwrite
    ui_state["active_page"] with the page at `new_index` - False means
    "not time yet, leave whatever's currently pinned (manual or rotation)
    alone". This is what lets a manual "Show X" click stay visible between
    rotation ticks instead of being stomped on every ~5s poll (see
    _poll_once()'s own comment where this is called).

    - page_count <= 0 (nothing checked for rotation): never advances.
    - last_advance_ts is None (rotation just turned on, or a fresh
      ui_state after a restart): advances immediately at the *current*
      index rather than waiting out a full interval first - the user
      enabling rotation expects to see it start, not stare at whatever
      was already pinned for another `interval_seconds`.
    - otherwise: advances (index+1, wrapping) once `now - last_advance_ts`
      has reached `interval_seconds`; unchanged before that.

    index wraps via modulo unconditionally, so a stale index left over
    from a since-shrunk page list (pages unchecked while rotation was
    running) can never point past the end of the current list.
    """
    if page_count <= 0:
        return 0, last_advance_ts if last_advance_ts is not None else now, False

    safe_index = index % page_count
    if last_advance_ts is None:
        return safe_index, now, True
    if now - last_advance_ts >= interval_seconds:
        return (safe_index + 1) % page_count, now, True
    return safe_index, last_advance_ts, False


# Same small font every page already uses for its own labels (status.py/
# radio.py/power.py/system.py/message.py each call load_font(12) as
# "label_font") - not a new size introduced just for the clock.
_CLOCK_FONT_SIZE = 12


def _mark_dirty_with_clock(
    manager: DisplayManager, image, time_str: str, priority: EventPriority = EventPriority.NORMAL,
) -> None:
    """Overlay the live clock onto an already-fully-rendered page image,
    then hand it to DisplayManager.mark_dirty() - hashing the *pre-overlay*
    image for dedup instead of the final (clock-included) one.

    This is the fix for e-Paper Stage 3's core risk (see manager.py's
    mark_dirty() docstring for the mechanism): DisplayManager._refresh()
    hashes raw image bytes to decide whether a physical panel write is
    actually needed. `time_str` changes every minute by construction, so
    if it were baked into `image` before hashing, every single poll where
    the minute ticked over would look like "new content" and force a real
    refresh - directly the behavior the task's hard constraints forbid.
    Hashing the image as it looked *before* the clock was drawn keeps
    dedup keyed to the content that actually matters (radio/CPU/node
    counts/etc.), while the physical write - on the polls where one does
    happen for a real content reason - still shows the current clock,
    since drawing happens in-place on the same object passed to
    mark_dirty()."""
    content_hash = hashlib.sha256(image.tobytes()).hexdigest()
    draw_epaper_clock(image, time_str, load_font(_CLOCK_FONT_SIZE))
    manager.mark_dirty(image, priority=priority, content_hash=content_hash)


def _current_clock_text(get_time_format) -> str:
    time_status = get_time_status() or {}
    return format_epaper_time(get_time_format(), time_status.get("timezone", "UTC"))


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
    get_temperature_unit: Callable[[], str] = lambda: "c",
    get_time_format: Callable[[], str] = lambda: "24",
    get_radio_identity: Callable[[], dict[str, str]] = lambda: {},
    get_uptime_seconds: Callable[[], float | None] = lambda: None,
    get_rotation_config: Callable[[], dict] = lambda: {},
    ui_state: dict | None = None,
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
                    get_temperature_unit, get_time_format, get_radio_identity, get_uptime_seconds,
                    get_rotation_config, ui_state,
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
                  get_cpu_temp, get_latest_message, locale: str = DEFAULT_LOCALE,
                  temperature_unit: str = "c",
                  get_battery_percent: Callable[[], float | None] = lambda: None,
                  get_radio_identity: Callable[[], dict[str, str]] = lambda: {},
                  get_uptime_seconds: Callable[[], float | None] = lambda: None):
    """Build+render whichever page is currently selected (plan section
    34's "Show on Display"), using only already-collected state - same
    invariant as the Status Screen (section 40)."""
    caps = manager.capabilities

    if page == "radio":
        radio_info = get_radio_status() or {}
        mode = radio_info.get("mode", "error")
        status = "online" if mode == "connected" else ("warning" if mode in ("reconnecting", "releasing") else "offline")
        identity = get_radio_identity() or {}
        return radio_page.render(caps, radio_page.RadioScreenData(
            status=status, mode=mode, node_name=local_node_name,
            listener_running=get_listener_alive(), last_error=get_last_error(),
            node_id=identity.get("node_id", ""), serial_port=radio_info.get("serial_port", ""),
            hardware=identity.get("hardware", ""),
        ), locale=locale)

    if page == "power":
        readings = get_power_readings() or {}
        return power_page.render(caps, power_page.PowerScreenData(
            node_name=local_node_name,
            voltage=readings.get("voltage"), current=readings.get("current"), power=readings.get("power"),
            battery_percent=get_battery_percent(),
        ), locale=locale)

    if page == "system":
        return system_page.render(caps, system_page.SystemScreenData(
            node_name=local_node_name,
            cpu_percent=_bucket(get_cpu_percent()), ram_percent=_bucket(get_ram_percent()),
            # Bucketed in Celsius (the sensor's native unit) regardless of
            # display unit - converting first would need a differently-
            # scaled bucket step per unit for no benefit, since the bucket
            # only exists to stabilize the rendered image, not to be
            # user-facing itself.
            cpu_temp_c=_bucket(get_cpu_temp(), step=_TEMP_SIGNIFICANCE_BUCKET),
            temperature_unit=temperature_unit,
            # Not bucketed separately - _format_uptime()'s own "Xd Yh" /
            # "Yh Zm" rounding is already the stabilization (see that
            # function's docstring in system.py); a second numeric bucket
            # on top would be redundant.
            uptime_seconds=get_uptime_seconds(),
        ), locale=locale)

    if page == "message":
        latest = get_latest_message() or {}
        kind = latest.get("kind", "rx")
        return message_page.render(caps, message_page.MessageScreenData(
            node_name=local_node_name,
            sender=latest.get("sender", ""), text=latest.get("text", ""), time=latest.get("time", ""),
            direction="tx" if kind == "me" else "rx",
            chat_type=latest.get("chat_type", ""), chat_name=latest.get("chat_name", ""),
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
    get_temperature_unit=lambda: "c",
    get_time_format=lambda: "24",
    get_radio_identity=lambda: {},
    get_uptime_seconds=lambda: None,
    get_rotation_config=lambda: {},
    ui_state=None,
) -> None:
    data, content_key = _build_status_fields(
        state_lock, nodes, get_radio_status, get_cpu_percent, get_ram_percent,
        get_listener_alive, local_node_name,
    )
    locale = get_display_language()
    clock_text = _current_clock_text(get_time_format)

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

    # Task 40: auto-rotation. Deliberately placed after the critical-alert
    # return above (not before it) - during an active alert, _poll_once()
    # never reaches this point at all, so rotation's own clock (last
    # advance timestamp) simply stops advancing along with everything
    # else, for free, with no explicit pause/resume needed. When the alert
    # clears, advance_rotation() sees a large now-last_advance_ts gap and
    # advances exactly one step from wherever it left off - continuing the
    # same cycle position, never resetting to the start.
    rotation_advanced = False
    if ui_state is not None:
        rotation_config = get_rotation_config() or {}
        if rotation_config.get("enabled"):
            checked = set(rotation_config.get("pages") or [])
            rotation_pages = [p for p in ROTATION_ALLOWED_PAGES if p in checked]
            if rotation_pages:
                interval = rotation_config.get("interval_seconds") or DEFAULT_EPAPER_CONFIG["rotation_interval_seconds"]
                new_index, new_last_ts, advanced = advance_rotation(
                    ui_state.get("rotation_index", 0), len(rotation_pages),
                    ui_state.get("rotation_last_advance_ts"), time.time(), interval,
                )
                ui_state["rotation_index"] = new_index
                ui_state["rotation_last_advance_ts"] = new_last_ts
                if advanced:
                    # Only on an actual advance - not every ~5s poll tick -
                    # so a manual "Show X" click (which writes active_page
                    # directly, see api/api_hardware_display.py's show
                    # route) stays visible until rotation's own next real
                    # tick, instead of being stomped within seconds.
                    ui_state["active_page"] = rotation_pages[new_index]
                    rotation_advanced = True

    # A rotation-driven page change bypasses debounce (CRITICAL), same as
    # an alert - found live (task 40 follow-up): with a rotation_interval
    # shorter than (or close to) debounce_seconds, a NORMAL-priority
    # mark_dirty() sits in DisplayManager._debounce()'s wait and gets
    # silently *replaced* by the next tick's newer frame before the
    # debounce window ever elapses (see that method's own "merging in any
    # newer frame that arrives during the wait" docstring) - the page
    # that arrived first is never shown at all, not delayed. Over several
    # cycles this visibly drops pages from the rotation ("only Radio and
    # System show" with 4 pages checked, user's own live report) rather
    # than just slowing it down. debounce_seconds still fully applies to
    # everything else (routine Status Screen churn, non-rotation content).
    priority = EventPriority.CRITICAL if rotation_advanced else EventPriority.NORMAL

    active_page = get_active_page()
    if active_page and active_page != "status":
        image = _render_page(
            active_page, manager, local_node_name, get_radio_status, get_listener_alive,
            get_last_error, get_power_readings, get_cpu_percent, get_ram_percent,
            get_cpu_temp, get_latest_message, locale, get_temperature_unit(),
            get_battery_percent, get_radio_identity, get_uptime_seconds,
        )
        if image is not None:
            _mark_dirty_with_clock(manager, image, clock_text, priority=priority)
            return

    data.last_update = content_state.stamp(content_key)
    image = render(manager.capabilities, data, locale=locale)
    _mark_dirty_with_clock(manager, image, clock_text, priority=priority)


def build_status_image_now(
    manager, state_lock, nodes, get_radio_status, get_cpu_percent,
    get_ram_percent, get_listener_alive, local_node_name,
    locale: str = DEFAULT_LOCALE,
    time_format: str = "24",
):
    """Same status-screen content as the poller, but stamps "last update"
    with the current time unconditionally - for an explicit manual action
    (POST /api/hardware/display/refresh), where "now" genuinely is the
    event, unlike a routine 5s poll tick.

    Also draws the live clock (same corner/font as the poller's
    _mark_dirty_with_clock()) directly into the returned image - unlike
    the poller, this is a one-shot manual action that the caller already
    pushes with EventPriority.CRITICAL and no content_hash (see
    api/api_hardware_display.py), so there's no recurring-refresh risk
    here to guard against with a pre-overlay hash split."""
    data, _content_key = _build_status_fields(
        state_lock, nodes, get_radio_status, get_cpu_percent, get_ram_percent,
        get_listener_alive, local_node_name,
    )
    data.last_update = time.strftime("%H:%M")
    image = render(manager.capabilities, data, locale=locale)
    time_status = get_time_status() or {}
    clock_text = format_epaper_time(time_format, time_status.get("timezone", "UTC"))
    draw_epaper_clock(image, clock_text, load_font(_CLOCK_FONT_SIZE))
    return image


def build_page_image_now(
    page, manager, local_node_name, get_radio_status, get_listener_alive,
    get_last_error, get_power_readings, get_cpu_percent, get_ram_percent,
    get_cpu_temp, get_latest_message,
    locale: str = DEFAULT_LOCALE,
    temperature_unit: str = "c",
    time_format: str = "24",
    get_battery_percent: Callable[[], float | None] = lambda: None,
    get_radio_identity: Callable[[], dict[str, str]] = lambda: {},
    get_uptime_seconds: Callable[[], float | None] = lambda: None,
):
    """For POST /api/hardware/display/show/<page> - builds whichever page
    was requested right now, for an immediate (CRITICAL) push. Returns
    None for "status" or an unknown page name; the route falls back to
    build_status_image_now() for "status" and to a 404 for a page this
    phase didn't build (radio/power/system/message only - see
    KNOWN_PAGES).

    Draws the live clock into the returned image for the same reason
    build_status_image_now() does - see its docstring."""
    image = _render_page(
        page, manager, local_node_name, get_radio_status, get_listener_alive,
        get_last_error, get_power_readings, get_cpu_percent, get_ram_percent,
        get_cpu_temp, get_latest_message, locale, temperature_unit,
        get_battery_percent, get_radio_identity, get_uptime_seconds,
    )
    if image is None:
        return None
    time_status = get_time_status() or {}
    clock_text = format_epaper_time(time_format, time_status.get("timezone", "UTC"))
    draw_epaper_clock(image, clock_text, load_font(_CLOCK_FONT_SIZE))
    return image
