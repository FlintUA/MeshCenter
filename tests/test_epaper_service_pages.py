"""Tests for modules/display/service.py::_render_page()'s task-37 data
plumbing: the new get_battery_percent/get_radio_identity/get_uptime_seconds
callbacks actually reach the right dataclass fields, and a message's
kind/chat_type/chat_name (already present on the message dict, just not
previously passed through) map to MessageScreenData.direction/chat_type/
chat_name correctly.

Only _render_page() itself is exercised here (not the full epaper_worker
poll loop, not DisplayManager/DisplayDriver) - a minimal stub stands in
for `manager`, since _render_page() only ever reads manager.capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.service import _render_page


@dataclass
class _FakeManager:
    capabilities: DisplayCapabilities


WEACT_CAPS = DisplayCapabilities(width=200, height=200, colors=("black", "white"))


def _manager():
    return _FakeManager(capabilities=WEACT_CAPS)


# ---------------- radio page: node_id/serial_port/hardware ----------------

def test_render_page_radio_wires_identity_and_serial_port():
    calls = {}

    def render_stub(caps, data, locale):
        calls["data"] = data
        from modules.display.pages import status as status_page
        return status_page.render(caps, status_page.StatusScreenData(node_name="x"), locale)

    import modules.display.pages.radio as radio_page
    original_render = radio_page.render
    radio_page.render = render_stub
    try:
        _render_page(
            "radio", _manager(), "Flint TAP2",
            get_radio_status=lambda: {"mode": "connected", "serial_port": "/dev/ttyACM0"},
            get_listener_alive=lambda: True,
            get_last_error=lambda: "",
            get_power_readings=lambda: {},
            get_cpu_percent=lambda: None,
            get_ram_percent=lambda: None,
            get_cpu_temp=lambda: None,
            get_latest_message=lambda: None,
            get_radio_identity=lambda: {"node_id": "!756f9960", "hardware": "RAK3312"},
        )
    finally:
        radio_page.render = original_render

    data = calls["data"]
    assert data.node_id == "!756f9960"
    assert data.hardware == "RAK3312"
    assert data.serial_port == "/dev/ttyACM0"
    assert data.status == "online"


# ---------------- power page: battery_percent ----------------

def test_render_page_power_wires_battery_percent():
    calls = {}

    def render_stub(caps, data, locale):
        calls["data"] = data
        from modules.display.pages import status as status_page
        return status_page.render(caps, status_page.StatusScreenData(node_name="x"), locale)

    import modules.display.pages.power as power_page
    original_render = power_page.render
    power_page.render = render_stub
    try:
        _render_page(
            "power", _manager(), "Flint TAP2",
            get_radio_status=lambda: {},
            get_listener_alive=lambda: True,
            get_last_error=lambda: "",
            get_power_readings=lambda: {"voltage": 4.1, "current": 100.0, "power": 400.0},
            get_cpu_percent=lambda: None,
            get_ram_percent=lambda: None,
            get_cpu_temp=lambda: None,
            get_latest_message=lambda: None,
            get_battery_percent=lambda: 96,
        )
    finally:
        power_page.render = original_render

    data = calls["data"]
    assert data.battery_percent == 96
    assert data.node_name == "Flint TAP2"
    assert data.voltage == 4.1


# ---------------- system page: uptime_seconds ----------------

def test_render_page_system_wires_uptime_seconds():
    calls = {}

    def render_stub(caps, data, locale):
        calls["data"] = data
        from modules.display.pages import status as status_page
        return status_page.render(caps, status_page.StatusScreenData(node_name="x"), locale)

    import modules.display.pages.system as system_page
    original_render = system_page.render
    system_page.render = render_stub
    try:
        _render_page(
            "system", _manager(), "Flint TAP2",
            get_radio_status=lambda: {},
            get_listener_alive=lambda: True,
            get_last_error=lambda: "",
            get_power_readings=lambda: {},
            get_cpu_percent=lambda: 25.0,
            get_ram_percent=lambda: 47.0,
            get_cpu_temp=lambda: 51.0,
            get_latest_message=lambda: None,
            get_uptime_seconds=lambda: 123456.0,
        )
    finally:
        system_page.render = original_render

    data = calls["data"]
    assert data.uptime_seconds == 123456.0
    assert data.node_name == "Flint TAP2"


# ---------------- message page: direction/chat_type/chat_name ----------------

def test_render_page_message_rx_maps_kind_to_direction():
    calls = {}

    def render_stub(caps, data, locale):
        calls["data"] = data
        from modules.display.pages import status as status_page
        return status_page.render(caps, status_page.StatusScreenData(node_name="x"), locale)

    import modules.display.pages.message as message_page
    original_render = message_page.render
    message_page.render = render_stub
    try:
        _render_page(
            "message", _manager(), "Flint TAP2",
            get_radio_status=lambda: {},
            get_listener_alive=lambda: True,
            get_last_error=lambda: "",
            get_power_readings=lambda: {},
            get_cpu_percent=lambda: None,
            get_ram_percent=lambda: None,
            get_cpu_temp=lambda: None,
            get_latest_message=lambda: {
                "kind": "rx", "sender": "Flint Base", "text": "hi", "time": "20:18",
                "chat_type": "channel", "chat_name": "LongFast",
            },
        )
    finally:
        message_page.render = original_render

    data = calls["data"]
    assert data.direction == "rx"
    assert data.chat_type == "channel"
    assert data.chat_name == "LongFast"


def test_render_page_message_outgoing_kind_me_maps_to_tx():
    calls = {}

    def render_stub(caps, data, locale):
        calls["data"] = data
        from modules.display.pages import status as status_page
        return status_page.render(caps, status_page.StatusScreenData(node_name="x"), locale)

    import modules.display.pages.message as message_page
    original_render = message_page.render
    message_page.render = render_stub
    try:
        _render_page(
            "message", _manager(), "Flint TAP2",
            get_radio_status=lambda: {},
            get_listener_alive=lambda: True,
            get_last_error=lambda: "",
            get_power_readings=lambda: {},
            get_cpu_percent=lambda: None,
            get_ram_percent=lambda: None,
            get_cpu_temp=lambda: None,
            get_latest_message=lambda: {
                "kind": "me", "sender": "Flint TAP2 → Flint Base", "text": "on my way", "time": "20:18",
                "chat_type": "dm", "chat_name": "Flint Base",
            },
        )
    finally:
        message_page.render = original_render

    data = calls["data"]
    assert data.direction == "tx"
    assert data.chat_type == "dm"
    assert data.chat_name == "Flint Base"
