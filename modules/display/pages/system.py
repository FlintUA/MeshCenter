"""System Screen - CPU/RAM/CPU temperature/host uptime. e-Paper Stage 1
plan, Phase 10, rebuilt on the WeAct 200x200 UI Design doc's common
five-page frame (task 37): CPU/RAM as labeled bar indicators, temperature
and uptime as a bottom pair. Reuses the same /proc-based readers as the
rest of the app (system/cpu_history.py) - no new telemetry collection
beyond the new read_uptime_seconds() this task adds there.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.renderer import (
    draw_node_header,
    draw_page_header,
    draw_progress_bar,
    draw_separator,
    draw_text,
    fit_font,
    load_font,
    new_canvas,
)

_PAIR_VALUE_SIZES = (21, 18, 15, 12)


@dataclass
class SystemScreenData:
    node_name: str = ""
    cpu_percent: float | None = None
    ram_percent: float | None = None
    cpu_temp_c: float | None = None  # always Celsius - see _format_temp() for unit conversion
    temperature_unit: str = "c"  # "c" | "f" | "both" - settings.units.temperature
    uptime_seconds: float | None = None


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _format_temp(celsius: float | None, unit: str) -> str:
    """Mirrors static/chat.js's formatTemperature() (same 3 unit modes,
    same 1-decimal precision) so the e-paper screen and the web UI footer
    agree on more than just the underlying sensor value."""
    if celsius is None:
        return "--"
    if unit == "f":
        return f"{_celsius_to_fahrenheit(celsius):.1f}°F"
    if unit == "both":
        return f"{celsius:.1f}°C/{_celsius_to_fahrenheit(celsius):.1f}°F"
    return f"{celsius:.1f}°C"


def _format_uptime(seconds: float | None) -> str:
    """WeAct UI Design doc, section 9's "Формат uptime": a rough format
    ("3d 14h" / "14h 32m" / "42m"), never HH:MM:SS - both because seconds
    precision is meaningless for a host uptime and because the format
    itself is what keeps this row's rendered text (and therefore the
    page's content hash) from changing on every single poll."""
    if seconds is None:
        return "--"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def render(caps: DisplayCapabilities, data: SystemScreenData, locale: str = DEFAULT_LOCALE):
    image, _draw = new_canvas(caps)
    w, h = image.size

    # Task 42: header height (and everything below it) is dynamic - see
    # status.py's render() for the shared rationale.
    header_height = draw_node_header(image, data.node_name)
    page_header_y = header_height + 2
    separator_y = header_height + 24
    content_y = header_height + 30

    draw_page_header(image, t("system", locale), y=page_header_y)
    draw_separator(image, separator_y)

    label_font = load_font(13, bold=True)
    value_font = load_font(23, bold=True)
    bar_x, bar_width, bar_height = 4, w - 8, 9

    y = content_y

    def metric_row(label: str, percent: float | None, y: int) -> int:
        text = f"{percent:.0f}%" if percent is not None else "--"
        draw_text(image, (4, y), label, "black", label_font)
        tw = value_font.getlength(text)
        draw_text(image, (w - 4 - int(tw), y - 4), text, "black", value_font)
        y += 22
        draw_progress_bar(image, (bar_x, y), bar_width, bar_height, percent)
        return y + bar_height + 10

    # Explicit, tight budget rather than open-ended increments - the
    # bottom TEMP/UPTIME pair + its own small labels must still fit above
    # y=200 after two metric rows and a separator (found overflowing off
    # the bottom of the canvas entirely at looser spacing, live PNG
    # review, task 37).
    y = metric_row(t("cpu", locale).upper(), data.cpu_percent, y)
    y = metric_row(t("ram", locale).upper(), data.ram_percent, y)

    draw_separator(image, y)
    y += 8

    small_label_font = load_font(11)
    half = w // 2
    col_width = half - 8  # a "both" temperature unit ("51.0°C/123.8°F") is
    # by far the widest string either column ever has to fit - fit_font()
    # here (rather than a fixed size) is what keeps it from overflowing
    # into the other column instead of just looking nice.

    temp_text = _format_temp(data.cpu_temp_c, data.temperature_unit)
    uptime_text = _format_uptime(data.uptime_seconds)
    temp_font = fit_font(temp_text, col_width, _PAIR_VALUE_SIZES, bold=True)
    uptime_font = fit_font(uptime_text, col_width, _PAIR_VALUE_SIZES, bold=True)
    draw_text(image, (4, y), temp_text, "black", temp_font)
    draw_text(image, (half, y), uptime_text, "black", uptime_font)
    y += 22
    draw_text(image, (4, y), t("temp", locale).upper(), "black", small_label_font)
    draw_text(image, (half, y), t("uptime", locale).upper(), "black", small_label_font)

    return image
