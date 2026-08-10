"""Status Screen - MeshCenter's default e-paper page. e-Paper Stage 1 plan,
Phase 4 (section 14): MeshCenter status, radio status, node name, node
count, last RX, CPU/RAM, "Last update HH:MM".

Data comes from a plain StatusScreenData the caller builds - this module
has no knowledge of server.py's live state (that wiring is Phase 5,
section 40: only MeshCenter's already-collected internal state, never a
fresh `meshtastic --info` call triggered from here).
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import color_for_state, load_font, new_canvas


@dataclass
class StatusScreenData:
    meshcenter_status: str = "online"  # "online" | "warning" | "offline"
    radio_status: str = "online"  # "online" | "warning" | "offline"
    node_name: str = ""
    node_count: int = 0
    last_rx: str = "--"  # already-formatted, e.g. "12:34" or "--"
    cpu_percent: float | None = None
    ram_percent: float | None = None
    # Plan section 61: updates only on an actual event, never a live clock.
    last_update: str = "--:--"


def render(caps: DisplayCapabilities, data: StatusScreenData):
    image, draw = new_canvas(caps)
    w, h = image.size

    title_font = load_font(16, bold=True)
    label_font = load_font(12)
    value_font = load_font(12, bold=True)

    draw.text((4, 2), "MeshCenter", fill=color_for_state(data.meshcenter_status), font=title_font)
    draw.text((4, 20), data.node_name or "-", fill="black", font=label_font)

    rows = [
        ("Radio", data.radio_status, color_for_state(data.radio_status)),
        ("Nodes", str(data.node_count), "black"),
        ("Last RX", data.last_rx, "black"),
    ]
    y = 40
    for label, value, color in rows:
        draw.text((4, y), f"{label}:", fill="black", font=label_font)
        draw.text((70, y), value, fill=color, font=value_font)
        y += 16

    if data.cpu_percent is not None and data.ram_percent is not None:
        draw.text(
            (4, y), f"CPU {data.cpu_percent:.0f}%  RAM {data.ram_percent:.0f}%",
            fill="black", font=label_font,
        )
        y += 16

    draw.text((4, h - 16), f"Last update {data.last_update}", fill="black", font=label_font)

    return image
