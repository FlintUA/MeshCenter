"""Message Screen - the latest chat/channel message. e-Paper Stage 1
plan, Phase 10, rebuilt on the WeAct 200x200 UI Design doc's common
five-page frame (task 37): RX/TX + sender/recipient in the content header,
the message body at a dynamically-fitted font size (fit_text_to_box(),
renderer.py), and the channel name / "DM" as a footer. Shown via the
manual "Show on Display" command, no immediate/critical refresh by
default - a plain mark_dirty() with NORMAL priority, same as the Status
Screen, so it respects debounce like everything else that isn't an alert.
"""

from __future__ import annotations

from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.renderer import (
    CONTENT_Y,
    SEPARATOR_Y,
    draw_node_header,
    draw_page_header,
    draw_separator,
    draw_text,
    fit_text_to_box,
    load_font,
    new_canvas,
)

_BODY_SIZES = (28, 24, 21, 18, 16, 14)


@dataclass
class MessageScreenData:
    node_name: str = ""
    sender: str = ""
    text: str = ""
    time: str = ""
    direction: str = "rx"  # "rx" | "tx" - msg["kind"]: "me" -> tx, else rx
    chat_type: str = ""  # "dm" | "channel"
    chat_name: str = ""


def render(caps: DisplayCapabilities, data: MessageScreenData, locale: str = DEFAULT_LOCALE):
    image, _draw = new_canvas(caps)
    w, h = image.size

    draw_node_header(image, data.node_name)
    draw_page_header(image, t("message", locale))
    draw_separator(image, SEPARATOR_Y)

    meta_font = load_font(13, bold=True)
    y = CONTENT_Y

    direction_label = "TX" if data.direction == "tx" else "RX"
    if data.direction == "tx":
        who = t("message_to", locale, name=data.chat_name or data.sender or "-")
    else:
        who = data.sender or "-"
    draw_text(image, (4, y), f"{direction_label}  {who}", "black", meta_font)
    y += 22

    footer_y = h - 20
    body_top = y + 6
    body_height = footer_y - 10 - body_top

    body = data.text or t("no_messages", locale)
    lines, body_font, line_height = fit_text_to_box(body, w - 8, body_height, _BODY_SIZES, bold=False)

    # Vertically center a short message in the available box (doc section
    # 11: a one-line message shouldn't look stranded at the top of a mostly
    # empty screen).
    used_height = len(lines) * line_height
    y = body_top + max(0, (body_height - used_height) // 2)
    for line in lines:
        draw_text(image, (4, y), line, "black", body_font)
        y += line_height

    draw_separator(image, footer_y - 8)
    footer_text = data.chat_name if data.chat_type == "channel" and data.chat_name else t("dm", locale)
    draw_text(image, (4, footer_y), footer_text, "black", load_font(12))

    return image
