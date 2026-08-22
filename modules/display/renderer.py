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


# WeAct 200x200 UI Design doc, section 2 - the common frame all five pages
# (plus alert.py, per this session's decision) build on: an inverse node-
# name header, a page-title/clock row, a 1px separator, then whatever
# content the page wants. Exported so time_helper.py's draw_epaper_clock()
# can share PAGE_HEADER_Y instead of hardcoding a second copy of it - the
# clock is drawn by a separate call, after content-hash time (see
# service.py's _mark_dirty_with_clock()), but on the same row as the page
# title drawn by draw_page_header() below.
NODE_HEADER_HEIGHT = 30
PAGE_HEADER_Y = 32
PAGE_HEADER_CLOCK_FONT_SIZE = 12
SEPARATOR_Y = 54
CONTENT_Y = 60

_NODE_HEADER_FONT_SIZES = (22, 20, 18, 16, 14)


def fit_font(text: str, max_width: int, sizes: tuple[int, ...], bold: bool = True) -> ImageFont.FreeTypeFont:
    """The largest font in `sizes` (tried largest-first) whose rendered
    `text` fits within `max_width` - the exact loop alert.py's
    _TITLE_FONT_SIZES used to duplicate on its own, now shared so the node
    header/alert title/any future HERO-value autosizing all go through one
    implementation instead of three copies of the same measure-and-compare
    loop. Falls back to the smallest size if nothing fits (never raises,
    matches this module's existing never-fail-a-render convention)."""
    fallback = load_font(sizes[-1], bold=bold)
    for size in sizes:
        font = load_font(size, bold=bold)
        if text_width(text, font) <= max_width:
            return font
    return fallback


def _wrap_to_width(text: str, font, max_width: int) -> list[str]:
    """Greedy word-wrap measured by actual rendered pixel width
    (font.getlength() via text_width()), not character count - a fixed
    "wrap at N chars" (this module's previous textwrap.wrap() approach in
    message.py) is wrong the moment the font size changes, which
    fit_text_to_box() below does on purpose."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_text_to_box(
    text: str, box_width: int, box_height: int, sizes: tuple[int, ...], bold: bool = False, line_spacing: int = 4,
) -> tuple[list[str], ImageFont.FreeTypeFont, int]:
    """WeAct UI Design doc, section 11: the largest font (tried
    largest-first) at which `text`, word-wrapped to `box_width`, still
    fits entirely within `box_height`. Returns (lines, font, line_height)
    - line_height already includes line_spacing, so callers can just
    `y += line_height` per line.

    If even the smallest size overflows box_height, returns that size's
    wrapped lines truncated to whatever fits - message.py's previous
    behavior (silently dropping overflow lines via textwrap.wrap()[:4])
    rather than a new failure mode."""
    for size in sizes:
        font = load_font(size, bold=bold)
        lines = _wrap_to_width(text, font, box_width)
        line_height = size + line_spacing
        if len(lines) * line_height <= box_height:
            return lines, font, line_height

    size = sizes[-1]
    font = load_font(size, bold=bold)
    lines = _wrap_to_width(text, font, box_width)
    line_height = size + line_spacing
    max_lines = max(1, box_height // line_height)
    return lines[:max_lines], font, line_height


def draw_node_header(image: Image.Image, node_name: str) -> None:
    """WeAct UI Design doc, section 3: the permanent inverse header - solid
    black across the full width, the node's name in white bold, centered,
    always one line (never wrapped - a long name gets a smaller font
    instead, via fit_font(), down to a 14px floor)."""
    w, _h = image.size
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, w, NODE_HEADER_HEIGHT - 1], fill="black")

    name = (node_name or "-").upper()
    available_width = w - 16
    font = fit_font(name, available_width, _NODE_HEADER_FONT_SIZES, bold=True)

    bbox = draw.textbbox((0, 0), name, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = max(4, (w - text_w) // 2)
    y = max(2, (NODE_HEADER_HEIGHT - text_h) // 2 - bbox[1])
    draw_text(image, (x, y), name, "white", font)


def draw_page_header(image: Image.Image, page_title: str, clock_text: str | None = None) -> None:
    """WeAct UI Design doc, section 4: page title (upper-cased for display
    only - t() still returns the normal-case translated string, see this
    module's callers) at the left, clock at the right, on the row directly
    under draw_node_header()'s block.

    clock_text is optional and deliberately NOT how the live poll path
    draws the clock - service.py's _mark_dirty_with_clock() overlays it
    separately, after the pre-clock image has already been hashed for
    dedup (see time_helper.draw_epaper_clock()'s docstring). Passing
    clock_text here is only for standalone renders (PNG previews, tests)
    that don't go through that hash-then-overlay pipeline."""
    w, _h = image.size
    title_font = load_font(15, bold=True)
    draw_text(image, (4, PAGE_HEADER_Y), page_title.upper(), "black", title_font)

    if clock_text:
        # Same y draw_epaper_clock() uses by default - kept in sync so a
        # standalone render (clock_text passed here) and the live overlay
        # path (time_helper.draw_epaper_clock(), drawn separately after
        # content-hash time) land on the identical row.
        clock_font = load_font(PAGE_HEADER_CLOCK_FONT_SIZE)
        x = max(4, w - text_width(clock_text, clock_font) - 4)
        draw_text(image, (x, PAGE_HEADER_Y), clock_text, "black", clock_font)


def draw_separator(image: Image.Image, y: int) -> None:
    """A single 1px horizontal rule - WeAct UI Design doc, section 13:
    "минимум рамок" - lines instead of boxes."""
    w, _h = image.size
    ImageDraw.Draw(image).line([(0, y), (w, y)], fill="black", width=1)


def draw_label_value(
    image: Image.Image, y: int, label: str, value: str, label_font, value_font,
    left: int = 4, right: int | None = None, fill: str = "black",
    value_sizes: tuple[int, ...] | None = None,
) -> None:
    """One "LABEL    value" row. If `right` is given, value is right-
    aligned to that x (the doc's "RADIO          ONLINE" / "NODES
    14" column layout); otherwise it's drawn immediately after the label
    (the doc's "NODE      !756f9960" left-grouped layout).

    If `value_sizes` is given, `value_font` is ignored and the value's
    font is instead fit_font()-picked from `value_sizes` against the
    width actually left over after the label (right - left - label_width
    - gap), not a size the caller guessed in isolation - a longer
    translated label (e.g. German "LETZTER EMPFANG" vs English "LAST RX")
    genuinely leaves less room for the value on the same 200px row, and a
    value_font sized without accounting for that can overlap the label
    outright (found via live i18n PNG review, task 37)."""
    draw_text(image, (left, y), label, fill, label_font)
    label_end = left + text_width(label, label_font)

    if value_sizes is not None:
        available = max(20, (right if right is not None else label_end + 100) - label_end - 8)
        value_font = fit_font(value, available, value_sizes, bold=True)

    if right is not None:
        value_x = max(label_end + 8, right - text_width(value, value_font))
    else:
        value_x = label_end + 8
    draw_text(image, (value_x, y), value, fill, value_font)


def draw_progress_bar(image: Image.Image, xy: tuple[int, int], width: int, height: int, percent: float | None) -> None:
    """WeAct UI Design doc, section 9/13: a plain black bar indicator, no
    grayscale gradation (this panel has none to give) - an outlined track
    with a solid black fill proportional to `percent`."""
    x, y = xy
    draw = ImageDraw.Draw(image)
    draw.rectangle([x, y, x + width, y + height], outline="black", width=1)
    pct = max(0.0, min(100.0, percent or 0.0))
    fill_w = int((width - 2) * pct / 100.0)
    if fill_w > 0:
        draw.rectangle([x + 1, y + 1, x + fill_w, y + height - 1], fill="black")


def draw_inverted_status(image: Image.Image, y: int, text: str, font, left: int = 4, right: int | None = None) -> None:
    """A full-width inverted status block - the doc's "████ OFFLINE ████"
    warning/error treatment (section 14), built on the same
    black-rectangle-then-white-text technique as draw_inverted_text(), but
    spanning the given width instead of being sized tightly to the text."""
    w, _h = image.size
    right = w if right is None else right
    draw = ImageDraw.Draw(image)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = bbox[3] - bbox[1]
    pad = 4
    top = y - pad
    bottom = y + text_h + pad
    draw.rectangle([left, top, right, bottom], fill="black")
    text_w = text_width(text, font)
    x = left + max(0, ((right - left) - text_w) // 2)
    draw_text(image, (x, y), text, "white", font)


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
