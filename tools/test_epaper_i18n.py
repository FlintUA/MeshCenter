"""Renders every e-paper page in every supported locale (en/de/ru/uk) and
saves each as a PNG for visual inspection - proves the modules/display/i18n.py
wiring end-to-end (catalog lookup, {placeholder} formatting, glyph
rendering) without touching GPIO/SPI, so it's safe to run without stopping
meshcenter.service.

Run directly on the dev node over SSH (needs the real DejaVu font this repo
renders with - modules/display/renderer.py's FONT_PATH is a Linux path, so
this can't run on a plain dev machine without that font installed):

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_epaper_i18n.py

Output goes to /tmp/epaper_i18n/<locale>/<page>_<model>.png - fetch with
scp for the actual visual check (glyph correctness, no antialiasing
dropout, no obvious truncation) that this script alone can't verify.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT_DIR = Path("/tmp/epaper_i18n")


def main() -> int:
    from modules.display.drivers.base import DisplayCapabilities
    from modules.display.i18n import SUPPORTED_LOCALES
    from modules.display.pages import alert as alert_page
    from modules.display.pages import message as message_page
    from modules.display.pages import power as power_page
    from modules.display.pages import radio as radio_page
    from modules.display.pages import status as status_page
    from modules.display.pages import system as system_page

    caps_by_model = {
        "waveshare_213g": DisplayCapabilities(
            width=122, height=250, colors=("black", "white", "yellow", "red"),
            supports_fast_refresh=True,
        ),
        "weact_154": DisplayCapabilities(
            width=200, height=200, colors=("black", "white"), supports_fast_refresh=False,
        ),
    }

    ok = True
    for locale in SUPPORTED_LOCALES:
        for model, caps in caps_by_model.items():
            out_dir = OUT_DIR / locale
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                status_page.render(caps, status_page.StatusScreenData(
                    meshcenter_status="online", radio_status="warning", node_name="Flint TAP2",
                    node_count=273, last_rx="12:34", cpu_percent=45, ram_percent=60, last_update="12:35",
                ), locale=locale).save(out_dir / f"status_{model}.png")

                radio_page.render(caps, radio_page.RadioScreenData(
                    status="offline", mode="reconnecting", node_name="Flint TAP2",
                    listener_running=False, last_error="serial timeout",
                    node_id="!756f9960", serial_port="/dev/ttyACM0", hardware="RAK3312",
                ), locale=locale).save(out_dir / f"radio_{model}.png")

                power_page.render(caps, power_page.PowerScreenData(
                    node_name="Flint TAP2", voltage=4.01, current=123.4, power=494.9, battery_percent=96,
                ), locale=locale).save(out_dir / f"power_{model}.png")

                system_page.render(caps, system_page.SystemScreenData(
                    node_name="Flint TAP2", cpu_percent=45, ram_percent=60, cpu_temp_c=52,
                    uptime_seconds=3 * 86400 + 14 * 3600,
                ), locale=locale).save(out_dir / f"system_{model}.png")

                message_page.render(caps, message_page.MessageScreenData(
                    node_name="Flint TAP2", sender="Elektroniker.help",
                    text="Test-Nachricht für die Sichtprüfung", time="12:34",
                    direction="rx", chat_type="channel", chat_name="LongFast",
                ), locale=locale).save(out_dir / f"message_{model}.png")

                from modules.display.i18n import t
                alert_page.render(caps, alert_page.AlertScreenData(
                    title=t("radio_offline_title", locale), reason=t("connection_lost", locale),
                    node_name="Flint TAP2", device_path="/dev/ttyACM0", last_seen="12:34",
                ), locale=locale).save(out_dir / f"alert_radio_{model}.png")

                # Realistic worst case: service.py's CRITICAL_LOW_BATTERY_PERCENT
                # is 10, so battery_percent here is always <=10 (2 digits max) -
                # not 100, which this title never actually renders in practice.
                alert_page.render(caps, alert_page.AlertScreenData(
                    title=t("low_battery_title", locale, percent=10), reason=t("critically_low_power", locale),
                    node_name="Flint TAP2", device_path="/dev/ttyACM0", last_seen="12:34",
                ), locale=locale).save(out_dir / f"alert_battery_{model}.png")

                print(f"OK   {locale}/{model}")
            except Exception:
                print(f"FAIL {locale}/{model}")
                import traceback
                traceback.print_exc()
                ok = False

    print(f"\nSaved to {OUT_DIR} - fetch with scp for visual review.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
