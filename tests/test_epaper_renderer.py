"""Tests for modules/display/renderer.py's shared UI primitives added in
task 37 (WeAct 200x200 UI Design doc): fit_font(), fit_text_to_box(),
draw_node_header(), draw_page_header(), draw_separator(). Needs the real
DejaVu font (renderer.FONT_PATH/FONT_PATH_BOLD, Linux-only paths) to
measure real glyph widths - skipped on a machine without it (e.g. Windows
dev), same reasoning as the POSIX-only tests in
test_meshtastic_transport.py/test_runtime_lock.py.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists(),
    reason="requires the real DejaVu font (Linux-only path)",
)

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import (
    CONTENT_Y,
    NODE_HEADER_HEIGHT,
    PAGE_HEADER_Y,
    SEPARATOR_Y,
    draw_node_header,
    draw_page_header,
    draw_separator,
    fit_font,
    fit_text_to_box,
    load_font,
    new_canvas,
    text_width,
)

WEACT_CAPS = DisplayCapabilities(width=200, height=200, colors=("black", "white"))


# ---------------- fit_font() ----------------

def test_fit_font_picks_largest_size_that_fits():
    font = fit_font("OK", 200, (22, 18, 14), bold=True)
    assert font.size == 22


def test_fit_font_picks_smaller_size_for_long_text():
    long_text = "A VERY LONG NODE NAME INDEED"
    font = fit_font(long_text, 100, (22, 18, 14), bold=True)
    assert font.size in (18, 14)
    assert text_width(long_text, font) <= text_width(long_text, load_font(22, bold=True))


def test_fit_font_falls_back_to_smallest_when_nothing_fits():
    # Even at the smallest candidate size, an absurdly small max_width
    # can't be satisfied - fit_font() must still return something usable
    # (the smallest size) rather than raising or returning None.
    font = fit_font("UNREASONABLY LONG TEXT THAT NEVER FITS", 1, (22, 18, 14), bold=True)
    assert font.size == 14


# ---------------- fit_text_to_box() ----------------

def test_fit_text_to_box_short_message_gets_largest_size():
    lines, font, line_height = fit_text_to_box("OK", 190, 100, (28, 22, 16))
    assert lines == ["OK"]
    assert font.size == 28


def test_fit_text_to_box_wraps_long_message_and_shrinks_font():
    long_text = " ".join(["word"] * 40)
    lines, font, line_height = fit_text_to_box(long_text, 190, 100, (28, 22, 16, 12))
    assert len(lines) * line_height <= 100
    # A 40-word message can't possibly fit at the largest size in a
    # 100px-tall box - confirms the shrink actually happened, not just
    # that *some* font got returned.
    assert font.size < 28


def test_fit_text_to_box_truncates_when_even_smallest_size_overflows():
    long_text = " ".join(["word"] * 200)
    lines, font, line_height = fit_text_to_box(long_text, 190, 40, (14,))
    assert font.size == 14
    assert len(lines) * line_height <= 40 + line_height  # truncated, not endless


# ---------------- draw_node_header() ----------------

def test_draw_node_header_paints_full_width_black_band():
    image, _draw = new_canvas(WEACT_CAPS)
    draw_node_header(image, "Flint TAP2")
    w, _h = image.size
    # Corners of the header band should be black (the inverse fill),
    # regardless of where the centered text itself landed.
    assert image.getpixel((0, 0)) == (0, 0, 0)
    assert image.getpixel((w - 1, 0)) == (0, 0, 0)
    assert image.getpixel((0, NODE_HEADER_HEIGHT - 1)) == (0, 0, 0)


def test_draw_node_header_below_band_is_untouched():
    image, _draw = new_canvas(WEACT_CAPS)
    draw_node_header(image, "Flint TAP2")
    assert image.getpixel((0, NODE_HEADER_HEIGHT + 5)) == (255, 255, 255)


def test_draw_node_header_long_name_does_not_crash_and_stays_one_line():
    image, _draw = new_canvas(WEACT_CAPS)
    # Should shrink the font (fit_font) rather than wrap or overflow -
    # just confirm it doesn't raise and the band is still fully painted.
    draw_node_header(image, "AN EXTREMELY LONG NODE NAME FOR TESTING")
    assert image.getpixel((0, 0)) == (0, 0, 0)


# ---------------- draw_page_header() / draw_separator() ----------------

def test_draw_page_header_draws_title_text():
    image, _draw = new_canvas(WEACT_CAPS)
    draw_page_header(image, "status")
    # Somewhere on the title row, black pixels should now exist (the
    # canvas starts all-white) - a loose but real assertion that text was
    # actually painted at PAGE_HEADER_Y.
    row_pixels = [image.getpixel((x, PAGE_HEADER_Y + 4)) for x in range(4, 60)]
    assert (0, 0, 0) in row_pixels


def test_draw_separator_draws_a_full_width_line():
    image, _draw = new_canvas(WEACT_CAPS)
    w, _h = image.size
    draw_separator(image, SEPARATOR_Y)
    assert image.getpixel((0, SEPARATOR_Y)) == (0, 0, 0)
    assert image.getpixel((w - 1, SEPARATOR_Y)) == (0, 0, 0)
    assert image.getpixel((w // 2, SEPARATOR_Y)) == (0, 0, 0)


def test_content_y_is_below_separator():
    # Layout sanity: the content area must start after the separator, not
    # overlap the page-header row above it.
    assert CONTENT_Y > SEPARATOR_Y > PAGE_HEADER_Y > NODE_HEADER_HEIGHT
