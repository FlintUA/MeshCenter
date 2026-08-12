"""Radio Screen - detailed radio connection status. e-Paper Stage 1 plan,
Phase 10. Shown via the manual "Show on Display" command (section 34).
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import color_for_state, draw_text, load_font, new_canvas


@dataclass
class RadioScreenData:
    status: str  # "online" | "warning" | "offline"
    mode: str  # connected/reconnecting/releasing/released/error
    node_name: str
    listener_running: bool
    last_error: str = ""


def render(caps: DisplayCapabilities, data: RadioScreenData):
    image, _draw = new_canvas(caps)
    w, h = image.size

    title_font = load_font(16, bold=True)
    label_font = load_font(12)
    value_font = load_font(12, bold=True)

    draw_text(image, (4, 2), "Radio", color_for_state(data.status), title_font)
    draw_text(image, (4, 22), data.node_name or "-", "black", label_font)

    listener_label = "yes" if data.listener_running else "no"
    rows = [
        ("Mode", data.mode, color_for_state(data.status)),
        ("Listener", listener_label, "black" if data.listener_running else "red"),
    ]
    y = 42
    for label, value, color in rows:
        draw_text(image, (4, y), f"{label}:", "black", label_font)
        draw_text(image, (70, y), value, color, value_font)
        y += 16

    if data.last_error:
        draw_text(image, (4, y), f"Error: {data.last_error[:32]}", "red", label_font)

    return image
