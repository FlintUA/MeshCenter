"""System Screen - CPU/RAM/CPU temperature. e-Paper Stage 1 plan, Phase
10. Reuses the same /proc-based readers as the Status Screen (server.py's
_read_cpu_temperature() etc.) - no new telemetry collection.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.renderer import draw_text, load_font, new_canvas, text_width


@dataclass
class SystemScreenData:
    cpu_percent: float | None = None
    ram_percent: float | None = None
    cpu_temp_c: float | None = None  # always Celsius - see _format_temp() for unit conversion
    temperature_unit: str = "c"  # "c" | "f" | "both" - settings.units.temperature


def _celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _format_temp(celsius: float | None, unit: str) -> str:
    """Mirrors static/chat.js's formatTemperature() (same 3 unit modes,
    same 1-decimal precision) so the e-paper screen and the web UI footer
    agree on more than just the underlying sensor value - found missing
    after the System Screen's temperature bug was fixed: the screen kept
    drawing Celsius regardless of this setting, so switching units in
    Settings made the footer and the screen disagree even though both
    were already reading the same source."""
    if celsius is None:
        return "--"
    if unit == "f":
        return f"{_celsius_to_fahrenheit(celsius):.1f}°F"
    if unit == "both":
        return f"{celsius:.1f}°C/{_celsius_to_fahrenheit(celsius):.1f}°F"
    return f"{celsius:.1f}°C"


def render(caps: DisplayCapabilities, data: SystemScreenData, locale: str = DEFAULT_LOCALE):
    image, _draw = new_canvas(caps)
    w, h = image.size

    title_font = load_font(16, bold=True)
    label_font = load_font(12)
    value_font = load_font(14, bold=True)

    draw_text(image, (4, 2), t("system", locale), "black", title_font)

    # 1 decimal place - matches the web UI footer's precision convention
    # for these same three metrics (static/chat.js's formatDockPercent()/
    # formatTemperature()); this is the only Python-side formatter for
    # them, so there's nowhere else a second, differently-precisioned one
    # could creep in.
    def fmt(value, unit):
        return f"{value:.1f}{unit}" if value is not None else "--"

    rows = [
        (t("cpu", locale), fmt(data.cpu_percent, "%")),
        (t("ram", locale), fmt(data.ram_percent, "%")),
        (t("temp", locale), _format_temp(data.cpu_temp_c, data.temperature_unit)),
    ]
    value_x = 4 + max(text_width(f"{label}:", label_font) for label, _ in rows) + 6
    y = 30
    for label, value in rows:
        draw_text(image, (4, y), f"{label}:", "black", label_font)
        draw_text(image, (value_x, y), value, "black", value_font)
        y += 22

    return image
