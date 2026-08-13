"""e-Paper clock formatting/drawing helper - Stage 3 of the Time System
feature (see meshsrv/time_service.py for the Time Service this consumes).

A small, focused utility module under modules/display/, matching this
package's existing convention of splitting non-page-specific concerns into
their own module (i18n.py for translation, renderer.py for drawing
primitives) rather than growing service.py or duplicating logic across the
five page modules.

Deliberately does NOT take the full raw `settings` dict or call
meshsrv.time_service.get_status() itself - server.py's existing
_epaper_get_temperature_unit()/_epaper_get_display_language() convention is
to resolve a setting down to its normalized primitive value (behind a
get_<field>() callable) before it ever reaches modules/display/*, and
format_epaper_time() follows that same shape: it takes the already-
normalized time_format string ("12"/"24") and the already-resolved IANA
timezone name, not a raw settings blob.
"""

from __future__ import annotations

from datetime import datetime

from modules.display.renderer import draw_text, text_width


def format_epaper_time(time_format: str, timezone_name: str) -> str:
    """Format "now" for e-paper display, respecting the 12/24h setting
    (settings.units.time_format, see api/api_settings.py's
    normalize_settings()) and the Time Service's resolved timezone
    (meshsrv/time_service.py get_status()["timezone"]).

    Never raises: an unresolvable timezone name (or no zoneinfo/pytz
    available) falls back to naive local time rather than blocking the
    e-paper worker's poll loop.
    """
    tz = None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone_name)
    except Exception:
        try:
            import pytz
            tz = pytz.timezone(timezone_name)
        except Exception:
            tz = None

    now = datetime.now(tz) if tz else datetime.now()

    if str(time_format).strip().lower() != "12":
        return now.strftime("%H:%M")

    hour = now.strftime("%I").lstrip("0") or "12"
    minute = now.strftime("%M")
    ampm = now.strftime("%p")
    return f"{hour}:{minute} {ampm}"


def draw_epaper_clock(image, time_str: str, font) -> None:
    """Draw the live clock in the top-right corner, in-place on `image`.

    Top-right at the title row's y (2px, same as every page's title/status
    line) is the one screen location free on all five pages: status.py and
    radio.py already draw a colored status pill starting at x=4 on that
    row, but none of the five pages' titles are wide enough to reach the
    right edge at the canvas widths in play (200px WeAct / 250px
    Waveshare), and status.py's only other occupant of screen space near
    an edge is its *bottom*-left "Last update HH:MM" stamp - a different
    corner entirely, see status.py's render(). Right-aligned (not a fixed
    x) so it works at both panels' differing canvas widths without
    per-driver-model tuning.

    Callers must draw this only *after* computing whatever hash they will
    pass to DisplayManager.mark_dirty()'s `content_hash=` - the clock
    changes every minute by design, and folding it into the image-byte
    hash that mark_dirty() uses for physical-refresh dedup would force a
    real panel refresh once a minute, defeating that dedup (see
    modules/display/service.py's _mark_dirty_with_clock()).
    """
    w, _h = image.size
    x = max(4, w - text_width(time_str, font) - 4)
    draw_text(image, (x, 2), time_str, "black", font)
