"""Radio Screen - detailed radio connection status. e-Paper Stage 1 plan,
Phase 10. Shown via the manual "Show on Display" command (section 34).
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import draw_state_text, draw_text, load_font, new_canvas


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

    draw_state_text(image, caps, (4, 2), "Radio", data.status, title_font)
    draw_text(image, (4, 22), data.node_name or "-", "black", label_font)

    listener_label = "yes" if data.listener_running else "no"
    listener_state = "online" if data.listener_running else "offline"
    rows = [
        ("Mode", data.mode, data.status),
        ("Listener", listener_label, listener_state),
    ]
    y = 42
    for label, value, state in rows:
        draw_text(image, (4, y), f"{label}:", "black", label_font)
        draw_state_text(image, caps, (70, y), value, state, value_font)
        y += 16

    if data.last_error:
        draw_state_text(image, caps, (4, y), f"Error: {data.last_error[:32]}", "critical", label_font)

    return image
