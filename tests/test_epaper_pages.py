"""Tests for the five e-paper pages' render() functions + alert.py, after
task 37's WeAct 200x200 UI Design rebuild: new dataclass fields (RadioScreenData
.node_id/.serial_port/.hardware, PowerScreenData.node_name/.battery_percent,
SystemScreenData.node_name/.uptime_seconds, MessageScreenData.node_name/
.direction/.chat_type/.chat_name), the new common node-name header on every
page including alert.py, and the new uptime/port formatting helpers.

render() itself works fine with PIL's built-in fallback font (no real
DejaVu needed - modules/display/renderer.py's load_font() already degrades
gracefully, see its OSError catch), so unlike test_epaper_renderer.py this
file runs unconditionally, on any platform - these tests check structure
(image size, no exceptions, specific field-driven text) rather than real
glyph pixel positions.
"""

from __future__ import annotations

from modules.display.drivers.base import DisplayCapabilities
from modules.display.pages import alert as alert_page
from modules.display.pages import message as message_page
from modules.display.pages import power as power_page
from modules.display.pages import radio as radio_page
from modules.display.pages import status as status_page
from modules.display.pages import system as system_page

WEACT_CAPS = DisplayCapabilities(width=200, height=200, colors=("black", "white"))


# ---------------- status.py ----------------

def test_status_render_returns_correctly_sized_image():
    data = status_page.StatusScreenData(
        meshcenter_status="online", radio_status="online", node_name="Flint TAP2",
        node_count=14, last_rx="20:18", cpu_percent=25, ram_percent=47,
    )
    image = status_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_status_render_offline_radio_does_not_raise():
    data = status_page.StatusScreenData(radio_status="offline", node_name="X")
    image = status_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_status_render_handles_missing_cpu_ram():
    data = status_page.StatusScreenData(node_name="X", cpu_percent=None, ram_percent=None)
    image = status_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


# ---------------- radio.py ----------------

def test_radio_strip_dev_prefix():
    assert radio_page._strip_dev_prefix("/dev/ttyACM0") == "ttyACM0"
    assert radio_page._strip_dev_prefix("ttyACM0") == "ttyACM0"
    assert radio_page._strip_dev_prefix("") == ""


def test_radio_render_online_with_node_id_and_hardware():
    data = radio_page.RadioScreenData(
        status="online", mode="connected", node_name="Flint TAP2", listener_running=True,
        node_id="!756f9960", serial_port="/dev/ttyACM0", hardware="RAK3312",
    )
    image = radio_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_radio_render_offline_with_error_does_not_raise():
    data = radio_page.RadioScreenData(
        status="offline", mode="error", node_name="Flint TAP2", listener_running=False,
        last_error="serial timeout", serial_port="/dev/ttyACM0",
    )
    image = radio_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_radio_render_missing_identity_fields_shows_placeholders():
    # INSTANCE_IDENTITY.radio.hardware can be genuinely empty (identity
    # check never ran) - render() must not crash on empty node_id/hardware.
    data = radio_page.RadioScreenData(
        status="online", mode="connected", node_name="X", listener_running=True,
        node_id="", serial_port="", hardware="",
    )
    image = radio_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


# ---------------- power.py ----------------

def test_power_fmt_power_watts_converts_milliwatts():
    assert power_page._fmt_power_watts(510.0) == "0.51 W"
    assert power_page._fmt_power_watts(None) == "--"


def test_power_render_full_data():
    data = power_page.PowerScreenData(
        node_name="Flint TAP2", voltage=4.11, current=125.0, power=510.0, battery_percent=96,
    )
    image = power_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_power_render_no_sensor_data_shows_dashes_not_fabricated():
    # No SOURCE line exists at all (task 37 decision) - just confirm an
    # entirely-empty reading still renders "--" throughout, same
    # never-fabricate convention as before this rewrite.
    data = power_page.PowerScreenData(node_name="X")
    image = power_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_power_render_battery_only_fallback_layout():
    # voltage/current/power all present-but-None except battery - the
    # doc's "fallback to a two-metric layout" case.
    data = power_page.PowerScreenData(node_name="X", voltage=4.0, battery_percent=50)
    image = power_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


# ---------------- system.py ----------------

def test_system_format_uptime_days():
    assert system_page._format_uptime(3 * 86400 + 14 * 3600) == "3d 14h"


def test_system_format_uptime_hours_minutes():
    assert system_page._format_uptime(14 * 3600 + 32 * 60) == "14h 32m"


def test_system_format_uptime_minutes_only():
    assert system_page._format_uptime(5 * 60) == "5m"


def test_system_format_uptime_none():
    assert system_page._format_uptime(None) == "--"


def test_system_format_uptime_never_shows_seconds():
    # WeAct UI Design doc section 9: never HH:MM:SS / never seconds
    # precision - a value with a nonzero seconds remainder must still
    # format to a whole-minute-or-coarser string.
    text = system_page._format_uptime(3661)  # 1h 1m 1s
    assert ":" not in text
    assert text == "1h 1m"


def test_system_render_full_data():
    data = system_page.SystemScreenData(
        node_name="Flint TAP2", cpu_percent=25, ram_percent=47, cpu_temp_c=51.0,
        temperature_unit="c", uptime_seconds=3 * 86400 + 14 * 3600,
    )
    image = system_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_system_render_both_temperature_unit_does_not_raise():
    # The widest possible temp string ("51.0°C/123.8°F") - the case
    # fit_font() in the pair row exists to protect against overflow.
    data = system_page.SystemScreenData(
        node_name="X", cpu_temp_c=51.0, temperature_unit="both", uptime_seconds=100,
    )
    image = system_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_system_render_missing_uptime_shows_dashes():
    data = system_page.SystemScreenData(node_name="X", uptime_seconds=None)
    image = system_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


# ---------------- message.py ----------------

def test_message_render_rx():
    data = message_page.MessageScreenData(
        node_name="Flint TAP2", sender="Flint Base", text="Hello there", time="20:18",
        direction="rx", chat_type="channel", chat_name="LongFast",
    )
    image = message_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_message_render_tx_dm():
    data = message_page.MessageScreenData(
        node_name="Flint TAP2", sender="Flint Base", text="On my way", time="20:18",
        direction="tx", chat_type="dm", chat_name="Flint Base",
    )
    image = message_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_message_render_long_text_wraps_without_raising():
    long_text = " ".join(["word"] * 60)
    data = message_page.MessageScreenData(node_name="X", sender="Y", text=long_text)
    image = message_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_message_render_no_message_shows_placeholder():
    data = message_page.MessageScreenData(node_name="X", text="")
    image = message_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


# ---------------- alert.py ----------------

def test_alert_render_with_node_header_does_not_raise():
    data = alert_page.AlertScreenData(
        title="RADIO OFFLINE", reason="Connection lost", node_name="Flint TAP2",
        device_path="/dev/ttyACM0", last_seen="15:42",
    )
    image = alert_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)


def test_alert_render_long_node_name_does_not_raise():
    data = alert_page.AlertScreenData(
        title="LOW BATTERY (10%)", reason="Critically low power",
        node_name="AN EXTREMELY LONG NODE NAME FOR TESTING",
    )
    image = alert_page.render(WEACT_CAPS, data)
    assert image.size == (200, 200)
