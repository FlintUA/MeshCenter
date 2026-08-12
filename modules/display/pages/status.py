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
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.renderer import draw_state_text, draw_text, load_font, new_canvas, text_width


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


def render(caps: DisplayCapabilities, data: StatusScreenData, locale: str = DEFAULT_LOCALE):
    image, _draw = new_canvas(caps)
    w, h = image.size

    title_font = load_font(16, bold=True)
    label_font = load_font(12)
    value_font = load_font(12, bold=True)

    # data.meshcenter_status/radio_status stay the raw "online"/"warning"/
    # "offline" state codes for draw_state_text()'s color mapping - only the
    # rendered text itself is translated, via t().
    draw_state_text(image, caps, (4, 2), "MeshCenter", data.meshcenter_status, title_font)
    draw_text(image, (4, 20), data.node_name or "-", "black", label_font)

    rows = [
        (t("radio", locale), t(data.radio_status, locale), data.radio_status),
        (t("nodes", locale), str(data.node_count), None),
        (t("last_rx", locale), data.last_rx, None),
    ]
    # Value column starts after the widest translated label in this locale,
    # not a fixed pixel offset sized for English - a longer label (e.g. de
    # "Letzter Empfang:") would otherwise overlap the value that follows it.
    value_x = 4 + max(text_width(f"{label}:", label_font) for label, _, _ in rows) + 6
    y = 40
    for label, value, state in rows:
        draw_text(image, (4, y), f"{label}:", "black", label_font)
        if state:
            draw_state_text(image, caps, (value_x, y), value, state, value_font)
        else:
            draw_text(image, (value_x, y), value, "black", value_font)
        y += 16

    if data.cpu_percent is not None and data.ram_percent is not None:
        draw_text(
            image, (4, y),
            f"{t('cpu', locale)} {data.cpu_percent:.0f}%  {t('ram', locale)} {data.ram_percent:.0f}%",
            "black", label_font,
        )
        y += 16

    draw_text(image, (4, h - 16), t("last_update", locale, time=data.last_update), "black", label_font)

    return image
