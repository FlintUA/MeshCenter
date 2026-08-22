"""Radio Screen - detailed radio connection status. e-Paper Stage 1 plan,
Phase 10, rebuilt on the WeAct 200x200 UI Design doc's common five-page
frame (task 37): a big ONLINE/OFFLINE state, Node ID/Port/Hardware model
rows, and a Listener state footer. Shown via the manual "Show on Display"
command (section 34).
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
    fit_font,
    load_font,
    new_canvas,
    text_width,
)

_STATUS_SIZES = (28, 24, 20)
_ROW_VALUE_SIZES = (15, 13, 12, 11)


def _strip_dev_prefix(path: str) -> str:
    """WeAct UI Design doc, section 7: "/dev/" doesn't earn its screen
    space on a 200px panel - "ttyACM0", not "/dev/ttyACM0"."""
    path = path or ""
    return path[len("/dev/"):] if path.startswith("/dev/") else path


@dataclass
class RadioScreenData:
    status: str  # "online" | "warning" | "offline"
    mode: str  # connected/reconnecting/releasing/released/error
    node_name: str
    listener_running: bool
    last_error: str = ""
    node_id: str = ""
    serial_port: str = ""  # e.g. "/dev/ttyACM0" - display strips the prefix
    hardware: str = ""  # e.g. "RAK3312" - from INSTANCE_IDENTITY.radio.hardware


def render(caps: DisplayCapabilities, data: RadioScreenData, locale: str = DEFAULT_LOCALE):
    image, _draw = new_canvas(caps)
    w, h = image.size
    right = w - 4

    draw_node_header(image, data.node_name)
    draw_page_header(image, t("radio", locale))
    draw_separator(image, SEPARATOR_Y)

    status_label = t(data.status, locale).upper()
    y = CONTENT_Y
    if data.status == "online":
        status_font = fit_font(status_label, w - 16, _STATUS_SIZES, bold=True)
        tw = text_width(status_label, status_font)
        draw_text(image, ((w - tw) // 2, y), status_label, "black", status_font)
        y += 40
    else:
        draw_inverted_status(image, y, status_label, load_font(20, bold=True), right=right)
        y += 34

    label_font = load_font(12)

    rows = []
    if data.status == "online":
        rows.append((t("node", locale).upper(), data.node_id or "--"))
    rows.append((t("port", locale).upper(), _strip_dev_prefix(data.serial_port) or "--"))
    if data.status == "online":
        rows.append((t("hardware", locale).upper(), data.hardware or "--"))

    for label, value in rows:
        draw_label_value(image, y, label, value, label_font, None, right=right, value_sizes=_ROW_VALUE_SIZES)
        y += 20

    listener_label = t("listener_running", locale) if data.listener_running else t("listener_stopped", locale)
    listener_label = listener_label.upper()

    if data.status == "online":
        draw_separator(image, y + 4)
        y += 14
        draw_label_value(image, y, t("listener", locale).upper(), listener_label, label_font, None, right=right, value_sizes=_ROW_VALUE_SIZES)
    else:
        y += 6
        draw_label_value(image, y, t("listener", locale).upper(), listener_label, label_font, None, right=right, value_sizes=_ROW_VALUE_SIZES)
        if data.last_error:
            y += 22
            draw_text(image, (4, y), t("error", locale, detail=data.last_error[:28]), "black", load_font(11))

    return image
