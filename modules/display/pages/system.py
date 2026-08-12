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
    cpu_temp_c: float | None = None


def render(caps: DisplayCapabilities, data: SystemScreenData, locale: str = DEFAULT_LOCALE):
    image, _draw = new_canvas(caps)
    w, h = image.size

    title_font = load_font(16, bold=True)
    label_font = load_font(12)
    value_font = load_font(14, bold=True)

    draw_text(image, (4, 2), t("system", locale), "black", title_font)

    def fmt(value, unit):
        return f"{value:.0f}{unit}" if value is not None else "--"

    rows = [
        (t("cpu", locale), fmt(data.cpu_percent, "%")),
        (t("ram", locale), fmt(data.ram_percent, "%")),
        (t("temp", locale), fmt(data.cpu_temp_c, "C")),
    ]
    value_x = 4 + max(text_width(f"{label}:", label_font) for label, _ in rows) + 6
    y = 30
    for label, value in rows:
        draw_text(image, (4, y), f"{label}:", "black", label_font)
        draw_text(image, (value_x, y), value, "black", value_font)
        y += 22

    return image
