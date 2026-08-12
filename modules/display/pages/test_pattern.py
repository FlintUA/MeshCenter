"""Test pattern page: all supported colors + a readable text banner.
Used by both tools/test_epaper_driver.py (Phase 2 CLI test) and
POST /api/hardware/display/test (Phase 7 UI "Test Display" button) so
there's one definition of what "the test pattern" actually looks like.
"""

from __future__ import annotations

from modules.display.drivers.base import DisplayCapabilities
from modules.display.renderer import draw_text, load_font, new_canvas

TEST_LINES = ["MeshCenter", "EPAPER TEST", "PASS"]


def render(caps: DisplayCapabilities):
    image, draw = new_canvas(caps)
    w, h = image.size

    band_w = w // len(caps.colors)
    for i, color in enumerate(caps.colors):
        draw.rectangle([i * band_w, 0, (i + 1) * band_w, h // 2], fill=color)

    font = load_font(16, bold=True)
    draw_text(image, (4, h // 2 + 4), " / ".join(TEST_LINES), "black", font)

    return image
