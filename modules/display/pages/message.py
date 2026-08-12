"""Message Screen - the latest chat/channel message. e-Paper Stage 1
plan, Phase 10 (section 17): shown via the manual "Show on Display"
command, no immediate/critical refresh by default - a plain mark_dirty()
with NORMAL priority, same as the Status Screen, so it respects debounce
like everything else that isn't an alert.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import draw_text, load_font, new_canvas


@dataclass
class MessageScreenData:
    sender: str = ""
    text: str = ""
    time: str = ""


def render(caps: DisplayCapabilities, data: MessageScreenData):
    image, _draw = new_canvas(caps)
    w, h = image.size

    title_font = load_font(16, bold=True)
    meta_font = load_font(12)
    body_font = load_font(13)

    draw_text(image, (4, 2), "Message", "black", title_font)
    draw_text(image, (4, 22), f"{data.sender or '-'}  {data.time or ''}", "black", meta_font)

    body = data.text or "(no messages)"
    lines = textwrap.wrap(body, width=34)[:4]
    y = 44
    for line in lines:
        draw_text(image, (4, y), line, "black", body_font)
        y += 18

    return image
