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
from modules.display.i18n import DEFAULT_LOCALE, t
from modules.display.renderer import (
    NODE_HEADER_HEIGHT,
    draw_node_header,
    draw_text,
    fit_font,
    has_color,
    load_font,
    new_canvas,
)

# Tried largest-first until the title fits the panel width - a fixed 18pt
# clipped even the original English "LOW BATTERY (10%)" on WeAct's 200px
# canvas (found via tools/test_epaper_i18n.py, pre-dates any translation:
# same failure with locale="en"), and translated titles are rarely shorter.
# Shared with the node-name header via renderer.fit_font() (task 37) -
# this is just this screen's own choice of candidate sizes, not a second
# copy of the fitting loop itself.
_TITLE_FONT_SIZES = (18, 16, 14, 12)


@dataclass
class AlertScreenData:
    title: str  # already-translated, e.g. t("radio_offline_title", locale)
    reason: str = ""  # already-translated, e.g. t("connection_lost", locale)
    node_name: str = ""
    device_path: str = ""  # e.g. "/dev/ttyACM0"
    last_seen: str = ""  # already-formatted, e.g. "15:42"


def render(caps: DisplayCapabilities, data: AlertScreenData, locale: str = DEFAULT_LOCALE):
    image, draw = new_canvas(caps)
    w, h = image.size
    background = "red" if has_color(caps) else "black"
    draw.rectangle([0, 0, w, h], fill=background)

    # Node header on top, per this session's decision (task 37) - not in
    # the WeAct UI Design doc, which doesn't cover ALERT at all. Draws
    # fine over an already-solid-black canvas (has_color(caps) is False
    # for WeAct - the header's own black rect is a harmless no-op there;
    # on a color panel it stays legible white-on-black same as the rest
    # of this screen's text, since draw_node_header() always paints its
    # own black block first regardless of what's already behind it).
    draw_node_header(image, data.node_name)

    available_width = w - 16
    title_font = fit_font(data.title, available_width, _TITLE_FONT_SIZES, bold=True)
    label_font = load_font(11)

    y = NODE_HEADER_HEIGHT + 8
    draw_text(image, (8, y), data.title, "white", title_font)
    y += 24

    lines = []
    if data.reason:
        lines.append(data.reason)
    if data.node_name:
        lines.append(data.node_name)
    if data.device_path:
        lines.append(t("device_prefix", locale, path=data.device_path))
    if data.last_seen:
        lines.append(t("last_seen_prefix", locale, time=data.last_seen))

    for line in lines:
        draw_text(image, (8, y), line, "white", label_font)
        y += 16

    return image
