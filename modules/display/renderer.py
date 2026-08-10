"""Rendering helpers shared by all page modules (modules/display/pages/).

Deliberately ignorant of SPI/GPIO/DisplayDriver - only ever produces a PIL
Image sized to a DisplayCapabilities. e-Paper Stage 1 plan, Phase 4.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

from modules.display.drivers.base import DisplayCapabilities

# DejaVu Sans: covers Cyrillic + Latin Extended (ä/ö/ü/ß) in one family,
# permissively licensed for bundling, already present on Raspberry Pi OS -
# see the e-Paper Stage 1 plan's font decision (section 3).
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_PATH_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

StateColor = Literal["black", "red", "yellow"]


@lru_cache(maxsize=None)
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_PATH_BOLD if bold else FONT_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def new_canvas(caps: DisplayCapabilities) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Landscape canvas (caps.height x caps.width) - matches the vendor
    driver's rotate-on-mismatch convention, see
    drivers/waveshare_213g.py / vendor epd2in13g_v2.getbuffer()."""
    image = Image.new("RGB", (caps.height, caps.width), "white")
    return image, ImageDraw.Draw(image)


def color_for_state(state: str) -> StateColor:
    """Plan section 15: red = offline/critical, yellow = warning/degraded,
    always driven by actual state - never used decoratively."""
    state = (state or "").lower()
    if state in ("offline", "critical", "error"):
        return "red"
    if state in ("warning", "degraded"):
        return "yellow"
    return "black"
