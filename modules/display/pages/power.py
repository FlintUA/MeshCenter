"""Power Screen - INA226 telemetry (voltage/current/power). e-Paper Stage
1 plan, Phase 10. Never fabricates a value for a field the sensor didn't
report - shows "--" instead (plan section 16), same convention as the
Status Screen's last_rx/CPU/RAM fields.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import draw_text, load_font, new_canvas


@dataclass
class PowerScreenData:
    voltage: float | None = None
    current: float | None = None
    power: float | None = None


def _fmt(value: float | None, unit: str) -> str:
    return f"{value:.2f}{unit}" if value is not None else "--"


def render(caps: DisplayCapabilities, data: PowerScreenData):
    image, _draw = new_canvas(caps)
    w, h = image.size

    title_font = load_font(16, bold=True)
    label_font = load_font(12)
    value_font = load_font(14, bold=True)

    draw_text(image, (4, 2), "Power", "black", title_font)

    rows = [
        ("Voltage", _fmt(data.voltage, "V")),
        ("Current", _fmt(data.current, "mA")),
        ("Power", _fmt(data.power, "mW")),
    ]
    y = 30
    for label, value in rows:
        draw_text(image, (4, y), f"{label}:", "black", label_font)
        draw_text(image, (90, y), value, "black", value_font)
        y += 22

    return image
