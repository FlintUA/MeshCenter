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


def draw_text(image: Image.Image, xy: tuple[int, int], text: str, fill: str, font) -> None:
    """Draw text with hard (non-antialiased) glyph edges, then flat-fill
    with `fill`.

    Plain ImageDraw.text() antialiases glyph edges to intermediate gray
    levels. Against this panel's fixed 4-color palette (see the vendor
    driver's getbuffer() quantization), those gray edge pixels round to
    white or black essentially at random instead of forming clean strokes -
    confirmed live: small/Cyrillic text came out visibly speckled/dropped-
    out on the real panel versus a crisp bitmap-font (no-AA) renderer.
    Thresholding a 1-bit mask before compositing removes the gray levels
    entirely, matching what a non-antialiased embedded renderer would
    produce."""
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).text(xy, text, fill=255, font=font)
    mask = mask.point(lambda p: 255 if p > 128 else 0)
    solid = Image.new("RGB", image.size, fill)
    image.paste(solid, (0, 0), mask)
