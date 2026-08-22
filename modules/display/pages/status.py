"""Status Screen - MeshCenter's default e-paper page. e-Paper Stage 1 plan,
Phase 4 (section 14), rebuilt on the WeAct 200x200 UI Design doc's common
five-page frame (task 37): radio status, node count, last RX as the main
"is everything working?" glance, CPU/RAM as a small footer line.

Data comes from a plain StatusScreenData the caller builds - this module
has no knowledge of server.py's live state (that wiring is Phase 5,
section 40: only MeshCenter's already-collected internal state, never a
fresh `meshtastic --info` call triggered from here).
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.renderer import (
    CONTENT_Y,
    SEPARATOR_Y,
    draw_inverted_status,
    draw_label_value,
    draw_node_header,
    draw_page_header,
    draw_separator,
    draw_text,
    load_font,
    new_canvas,
)

_HERO_SIZES = (24, 22, 20)
_PRIMARY_SIZES = (21, 19, 17)


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
    right = w - 4

    draw_node_header(image, data.node_name)
    draw_page_header(image, t("status", locale))
    draw_separator(image, SEPARATOR_Y)

    # 12px, not 14 - "LETZTER EMPFANG" (de "Last RX") needs the extra room
    # to leave any space at all for its value on a 200px row at any
    # _PRIMARY_SIZES size (found via live i18n PNG review, task 37).
    label_font = load_font(12, bold=True)
    y = CONTENT_Y

    radio_label = t(data.radio_status, locale).upper()
    if data.radio_status == "online":
        draw_label_value(
            image, y, t("radio", locale).upper(), radio_label, label_font, None,
            right=right, value_sizes=_PRIMARY_SIZES,
        )
    else:
        draw_text(image, (4, y), t("radio", locale).upper(), "black", label_font)
        draw_inverted_status(image, y + 20, radio_label, load_font(16, bold=True), right=right)
        y += 24
    y += 30

    draw_label_value(
        image, y, t("nodes", locale).upper(), str(data.node_count), label_font, None,
        right=right, value_sizes=_HERO_SIZES,
    )
    y += 34

    draw_label_value(
        image, y, t("last_rx", locale).upper(), data.last_rx, label_font, None,
        right=right, value_sizes=_PRIMARY_SIZES,
    )

    footer_font = load_font(15, bold=True)
    footer_y = h - 22
    draw_separator(image, footer_y - 6)
    if data.cpu_percent is not None and data.ram_percent is not None:
        draw_text(
            image, (4, footer_y),
            f"{t('cpu', locale)} {data.cpu_percent:.0f}%   {t('ram', locale)} {data.ram_percent:.0f}%",
            "black", footer_font,
        )

    return image
