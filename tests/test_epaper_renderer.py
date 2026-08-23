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
    NODE_HEADER_HEIGHT_DOUBLE,
    NODE_HEADER_HEIGHT_SINGLE,
    PAGE_HEADER_Y,
    SEPARATOR_Y,
    draw_node_header,
    draw_page_header,
    draw_separator,
    fit_font,
    fit_text_to_box,
    load_font,
    new_canvas,
    node_header_layout,
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
    assert image.getpixel((0, NODE_HEADER_HEIGHT_SINGLE - 1)) == (0, 0, 0)


def test_draw_node_header_short_name_stays_single_line():
    image, _draw = new_canvas(WEACT_CAPS)
    height = draw_node_header(image, "Flint TAP2")
    assert height == NODE_HEADER_HEIGHT_SINGLE


def test_draw_node_header_below_band_is_untouched():
    image, _draw = new_canvas(WEACT_CAPS)
    draw_node_header(image, "Flint TAP2")
    assert image.getpixel((0, NODE_HEADER_HEIGHT_SINGLE + 5)) == (255, 255, 255)


def test_draw_node_header_long_name_grows_to_two_lines_and_returns_height():
    # Task 42: a name too long even at the smallest single-line size now
    # grows the header to a genuine second line (rather than the old
    # always-one-line, silently-overflowing behavior) - and the caller
    # gets the actual height back so it can lay out everything below it.
    image, _draw = new_canvas(WEACT_CAPS)
    height = draw_node_header(image, "AN EXTREMELY LONG NODE NAME FOR TESTING")
    assert height == NODE_HEADER_HEIGHT_DOUBLE
    assert image.getpixel((0, 0)) == (0, 0, 0)
    assert image.getpixel((0, height - 1)) == (0, 0, 0)
    # Band's bottom edge is exactly at `height` - nothing black leaks
    # past it into what should still be blank canvas.
    assert image.getpixel((0, height + 4)) == (255, 255, 255)


# ---------------- node_header_layout() ----------------

def test_node_header_layout_short_name_is_single_line():
    height, lines, _font = node_header_layout("Flint TAP2", 200)
    assert height == NODE_HEADER_HEIGHT_SINGLE
    assert lines == ["Flint TAP2"]


def test_node_header_layout_real_long_name_wraps_to_two_lines():
    # Real Meshtastic naming convention from the user's own mesh: a
    # region/route prefix in brackets + human name + short ID - not a
    # rare edge case, this is what triggered task 42.
    name = "[de.nby.n.RiBeNet.cb] SolisCultor c87b"
    height, lines, font = node_header_layout(name, 200)
    assert height == NODE_HEADER_HEIGHT_DOUBLE
    assert len(lines) == 2
    available_width = 200 - 16
    for line in lines:
        assert text_width(line, font) <= available_width


def test_node_header_layout_extreme_name_truncates_last_line_with_ellipsis():
    # Even 2 lines at the smallest candidate size can't hold this - the
    # hard ceiling of 2 lines must still hold, with the last line
    # ellipsis-truncated rather than a 3rd line or an overflowed edge.
    # Deliberately multi-word (not one unbreakable token) so
    # _wrap_to_width() actually produces more than 2 candidate lines and
    # the fallback path's kept=lines[:2] has 2 elements to truncate.
    name = "Word " * 40
    height, lines, font = node_header_layout(name, 200)
    assert height == NODE_HEADER_HEIGHT_DOUBLE
    assert len(lines) == 2
    assert lines[-1].endswith("…")
    available_width = 200 - 16
    for line in lines:
        assert text_width(line, font) <= available_width


# ---------------- draw_page_header() / draw_separator() ----------------

def test_draw_page_header_draws_title_text():
    image, _draw = new_canvas(WEACT_CAPS)
    draw_page_header(image, "status")
    # Somewhere on the title row, black pixels should now exist (the
    # canvas starts all-white) - a loose but real assertion that text was
    # actually painted at PAGE_HEADER_Y.
    row_pixels = [image.getpixel((x, PAGE_HEADER_Y + 4)) for x in range(4, 60)]
    assert (0, 0, 0) in row_pixels


def test_draw_page_header_honors_explicit_y():
    # Task 42: real page render()s pass a dynamic y (header_height + 2)
    # instead of always relying on the single-line PAGE_HEADER_Y default -
    # confirm draw_page_header() actually draws at the y it's given, not
    # always at the default.
    image, _draw = new_canvas(WEACT_CAPS)
    custom_y = PAGE_HEADER_Y + 16
    draw_page_header(image, "status", y=custom_y)
    row_pixels = [image.getpixel((x, custom_y + 4)) for x in range(4, 60)]
    assert (0, 0, 0) in row_pixels
    # And nothing was painted at the old default row instead.
    default_row_pixels = [image.getpixel((x, PAGE_HEADER_Y + 4)) for x in range(4, 60)]
    assert (0, 0, 0) not in default_row_pixels


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
    assert CONTENT_Y > SEPARATOR_Y > PAGE_HEADER_Y > NODE_HEADER_HEIGHT_SINGLE
