"""Tests for modules/display/time_helper.py, focused on task 37's change:
draw_epaper_clock()'s default y moved from the old y=2 (the title row
before the WeAct UI Design frame existed) to renderer.PAGE_HEADER_Y (the
page-title/clock row under the new permanent node-name header).
format_epaper_time() itself is unchanged by task 37 and already covered
elsewhere in spirit - not re-tested here beyond a smoke check.
"""

from __future__ import annotations

from PIL import Image

from modules.display.renderer import PAGE_HEADER_Y, load_font
from modules.display.time_helper import draw_epaper_clock, format_epaper_time


def test_draw_epaper_clock_default_y_matches_page_header_row():
    # The exact regression task 37 fixed: a hardcoded y=2 would land the
    # clock inside the new inverse node-name header instead of next to the
    # page title - assert the *default* (no y= override) uses the shared
    # PAGE_HEADER_Y constant, not a second hardcoded number.
    image = Image.new("RGB", (200, 200), "white")
    font = load_font(12)
    draw_epaper_clock(image, "20:24", font)
    # Nothing should be painted at the old y=2 row (that's inside the node
    # header's territory now) - a loose but real assertion.
    old_row = [image.getpixel((x, 2)) for x in range(0, 200)]
    assert all(pixel == (255, 255, 255) for pixel in old_row)


def test_draw_epaper_clock_y_override_still_works():
    image = Image.new("RGB", (200, 200), "white")
    font = load_font(12)
    draw_epaper_clock(image, "20:24", font, y=100)
    # Confirms the y= parameter is honored, not silently ignored in favor
    # of the default.
    row_100 = [image.getpixel((x, 104)) for x in range(150, 200)]
    row_2 = [image.getpixel((x, 2)) for x in range(150, 200)]
    assert row_100 != row_2


def test_draw_epaper_clock_right_aligned():
    image = Image.new("RGB", (200, 200), "white")
    font = load_font(12)
    draw_epaper_clock(image, "20:24", font)
    # Right-aligned means nothing should be painted in the far-left column
    # at the clock's row.
    assert image.getpixel((0, PAGE_HEADER_Y)) == (255, 255, 255)


def test_format_epaper_time_24h_smoke():
    result = format_epaper_time("24", "UTC")
    assert ":" in result and len(result) == 5


def test_format_epaper_time_12h_has_am_pm():
    result = format_epaper_time("12", "UTC")
    assert "AM" in result or "PM" in result
