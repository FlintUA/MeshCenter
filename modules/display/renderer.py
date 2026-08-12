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


def has_color(caps: DisplayCapabilities) -> bool:
    return any(c in caps.colors for c in ("red", "yellow"))


def draw_state_text(image: Image.Image, caps: DisplayCapabilities, xy, text: str, state: str, font) -> None:
    """Text whose visual emphasis reflects `state` (see color_for_state),
    adapted to whether this panel can actually render red/yellow.

    e-Paper Stage 2 plan (WeAct 1.54"), section 1 item 2 / Phase 3: a B/W
    panel physically cannot show red/yellow - rather than dither toward
    some arbitrary gray approximation (which plan section 1 explicitly
    rejects), critical/warning states get bold white-on-black inverted
    text instead of a color fill. Panels that do have color (Stage 1's
    Waveshare) are unaffected - this only changes behavior when
    color_for_state() would have returned non-black and this panel's
    DisplayCapabilities says it can't render that color."""
    color = color_for_state(state)
    if color == "black" or has_color(caps):
        draw_text(image, xy, text, color, font)
        return
    draw_inverted_text(image, xy, text, font)


def draw_inverted_text(image: Image.Image, xy, text: str, font) -> None:
    """White text on a solid black block - the no-color substitute for a
    red/yellow fill (see draw_state_text)."""
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox(xy, text, font=font)
    pad = 2
    draw.rectangle([bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad], fill="black")
    draw_text(image, xy, text, "white", font)


def text_width(text: str, font) -> int:
    """Rendered pixel width of `text` in `font` - used to position a value
    column after a translated label instead of a fixed pixel offset sized
    for the (shortest) English text. See e.g. status.py's row loop: "Last
    RX:" and "Letzter Empfang:" don't fit the same hardcoded column without
    overlapping the value that follows it."""
    return int(font.getlength(text))


def draw_text(image: Image.Image, xy: tuple[int, int], text: str, fill: str, font) -> None:
    """Draw text with hard (non-antialiased) glyph edges, then flat-fill
    with `fill`.

    Plain ImageDraw.text() on an "L"/"RGB" image antialiases glyph edges to
    intermediate gray levels. Against this panel's fixed 4-color palette
    (see the vendor driver's getbuffer() quantization), those gray edge
    pixels round to white/black essentially at random instead of forming
    clean strokes.

    Thresholding that antialiased render at 50% (an earlier version of this
    function) was a partial fix but still visibly rough on the real
    panel: hairline strokes that only partially cover a pixel fall under
    the threshold and vanish, so thin letterforms still show gaps.
    Drawing directly onto a mode="1" mask instead makes FreeType use its
    own monochrome hinting path (FT_LOAD_TARGET_MONO) - grid-fitted for a
    1-bit target rather than naive-thresholded from a grayscale render -
    which is much closer to what a non-antialiased embedded/bitmap-font
    renderer (e.g. this panel's previous ESP32 firmware) produces."""
    mask = Image.new("1", image.size, 0)
    ImageDraw.Draw(mask).text(xy, text, fill=1, font=font)
    solid = Image.new("RGB", image.size, fill)
    image.paste(solid, (0, 0), mask)
