"""Alert Screen - shown for conditions that must bypass debounce entirely
(radio offline, critically low power). e-Paper Stage 1 plan, Phase 9
(sections 21, 68).

Red background is deliberate, not decorative on panels that have it -
matches renderer.py's color_for_state() semantics (red = offline/
critical) applied to the whole page instead of a single field, since the
point of this screen is "stop and look at this now". On a B/W panel
(e-Paper Stage 2 plan, section 1 item 2) there's no red to fall back to,
so the whole screen inverts to black-with-white-text instead - the same
"stop and look" emphasis, achieved with the only two colors available
instead of trying to dither toward some fake gray-ish red.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import draw_text, has_color, load_font, new_canvas


@dataclass
class AlertScreenData:
    title: str  # e.g. "RADIO OFFLINE"
    reason: str = ""  # e.g. "Connection lost"
    node_name: str = ""
    device_path: str = ""  # e.g. "/dev/ttyACM0"
    last_seen: str = ""  # already-formatted, e.g. "15:42"


def render(caps: DisplayCapabilities, data: AlertScreenData):
    image, draw = new_canvas(caps)
    w, h = image.size
    background = "red" if has_color(caps) else "black"
    draw.rectangle([0, 0, w, h], fill=background)

    title_font = load_font(18, bold=True)
    label_font = load_font(11)

    y = 6
    draw_text(image, (8, y), data.title, "white", title_font)
    y += 24

    lines = []
    if data.reason:
        lines.append(data.reason)
    if data.node_name:
        lines.append(data.node_name)
    if data.device_path:
        lines.append(f"Device: {data.device_path}")
    if data.last_seen:
        lines.append(f"Last seen: {data.last_seen}")

    for line in lines:
        draw_text(image, (8, y), line, "white", label_font)
        y += 16

    return image
