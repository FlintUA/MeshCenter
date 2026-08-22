"""Power Screen - node telemetry (voltage/current/power/battery). e-Paper
Stage 1 plan, Phase 10, rebuilt on the WeAct 200x200 UI Design doc's common
five-page frame (task 37): voltage as the HERO value, current/power as a
paired row, battery% as its own row. Never fabricates a value for a field
the sensor didn't report - shows "--" instead (plan section 16), same
convention as the Status Screen's last_rx/CPU/RAM fields.

No SOURCE footer (task 37, user decision): the readings here come from the
Meshtastic node's own mesh telemetry (server.py's
_epaper_get_power_readings(), sensor_data), not from a local host I2C
power sensor (that's the unrelated I2C/RTC/BME280 subsystem, tasks 23-27/
35) - there is no sensor model to report, so showing one would be
fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.renderer import (
    CONTENT_Y,
    SEPARATOR_Y,
    draw_label_value,
    draw_node_header,
    draw_page_header,
    draw_separator,
    draw_text,
    load_font,
    new_canvas,
    text_width,
)

_HERO_FONT_SIZE = 36
_HERO_FALLBACK_FONT_SIZE = 30  # used when voltage is the only thing left to show big


@dataclass
class PowerScreenData:
    node_name: str = ""
    voltage: float | None = None
    current: float | None = None
    power: float | None = None  # milliwatts (voltage_V * current_mA, see server.py's apply_telemetry_values())
    battery_percent: float | None = None


def _fmt_voltage(value: float | None) -> str:
    return f"{value:.2f} V" if value is not None else "--"


def _fmt_current(value: float | None) -> str:
    return f"{value:.0f} mA" if value is not None else "--"


def _fmt_power_watts(milliwatts: float | None) -> str:
    # Same mW -> W conversion as static/chat.js's formatPowerWattsFromMilliwatts()
    # (node panel's Power row) - the doc's "0.51 W" example, not raw mW.
    return f"{milliwatts / 1000.0:.2f} W" if milliwatts is not None else "--"


def _fmt_percent(value: float | None) -> str:
    return f"{value:.0f}%" if value is not None else "--"


def render(caps: DisplayCapabilities, data: PowerScreenData, locale: str = DEFAULT_LOCALE):
    image, _draw = new_canvas(caps)
    w, h = image.size
    right = w - 4

    draw_node_header(image, data.node_name)
    # Distinct key from "power" below (the CURRENT/POWER wattage pair's own
    # label, correctly "Мощность"/"Leistung" - actual wattage) - the page
    # title means "power supply status" as a whole, not the wattage value
    # specifically, and RU/UK's "Мощность"/"Потужність" read wrong there
    # (found live - user wanted "Питание", not "Мощность", for the title).
    draw_page_header(image, t("power_page_title", locale))
    draw_separator(image, SEPARATOR_Y)

    has_current_power = data.current is not None or data.power is not None
    y = CONTENT_Y

    # HERO voltage, centered - bigger still when current/power aren't
    # available (doc section 8's fallback: "автоматически перестраивать
    # layout под два показателя, делая их ещё крупнее").
    hero_font = load_font(_HERO_FONT_SIZE if has_current_power else _HERO_FONT_SIZE + 4, bold=True)
    voltage_text = _fmt_voltage(data.voltage)
    tw = text_width(voltage_text, hero_font)
    draw_text(image, ((w - tw) // 2, y), voltage_text, "black", hero_font)
    y += _HERO_FONT_SIZE + 14

    label_font = load_font(12)
    value_font = load_font(19, bold=True)

    if has_current_power:
        col_left = 4
        col_right_label_x = w // 2 + 6
        draw_text(image, (col_left, y), t("current", locale).upper(), "black", label_font)
        draw_text(image, (col_right_label_x, y), t("power", locale).upper(), "black", label_font)
        y += 18
        draw_text(image, (col_left, y), _fmt_current(data.current), "black", value_font)
        draw_text(image, (col_right_label_x, y), _fmt_power_watts(data.power), "black", value_font)
        y += 30

    battery_font = load_font(24, bold=True)
    draw_label_value(image, y, t("battery", locale).upper(), _fmt_percent(data.battery_percent), label_font, battery_font, right=right)

    return image
