"""REST routes for the e-paper display's Hardware card and Settings
panel. e-Paper Stage 1 plan, Phases 6-7 (sections 31, 36).

Scope for Phase 7 (per project decision): Enable/refresh-mode/debounce
settings apply live and autosave; GPIO/SPI/timeout ("Advanced") settings
never apply automatically - they require the explicit /reinit action,
validated against the GPIO registry and only persisted if the new pins
actually start successfully. A bad pin typo here risks reproducing the
BUSY-hang debugging from Phases 1-2, this time live instead of at wiring
time, hence the extra ceremony. Orientation is deferred (panel is
physically landscape-only, nothing to select). Environment page is
deferred too - no BME280 detected on dev, and the plan explicitly says
not to build a page with no real sensor behind it (section 16); Radio/
Power/System/Message are built in Phase 10 (see show/<page> below).

test/clear/refresh are all asynchronous - they return before the physical
refresh completes (plan section 36), same as the rest of this feature's
"never block the caller" design (DisplayManager.mark_dirty()). /reinit is
the one exception: it synchronously (bounded by REINIT_CHECK_TIMEOUT)
confirms the new pins actually work before responding, so the caller
finds out immediately rather than only on the next debounced refresh.
"""

from __future__ import annotations

from flask import jsonify, request

from modules.display.config_store import (
    DEFAULT_MODEL,
    MODEL_DEFAULT_PINS,
    MODEL_DISPLAY_NAMES,
    ROTATION_ALLOWED_PAGES,
    save_epaper_config,
)
from modules.display.gpio_registry import GpioConflictError
from modules.display.models import EventPriority, RefreshMode
from modules.display.pages import test_pattern
from modules.display.renderer import new_canvas
from modules.display.service import build_driver

REINIT_CHECK_TIMEOUT = 20.0


KNOWN_SHOW_PAGES = ("status", "radio", "power", "system", "message")

# task 40: auto-rotation's own interval, deliberately separate from
# debounce_seconds (that's the panel's physical-refresh antidebounce, a
# different concept - see modules/display/service.py's rotation docstring).
ROTATION_INTERVAL_MIN_SECONDS = 5.0
ROTATION_INTERVAL_MAX_SECONDS = 3600.0


def register_hardware_display_routes(
    app, display_manager, epaper_enabled: bool, handle_errors,
    config: dict, config_path: str, gpio_registry, build_status_image_now,
    build_page_image_now=None, ui_state: dict | None = None,
):
    def _disabled_response():
        return jsonify({"ok": False, "error": "e-paper display not enabled on this install"}), 400

    @app.route("/api/hardware/display")
    @handle_errors
    def api_hardware_display():
        if not epaper_enabled or display_manager is None:
            return jsonify({"ok": True, "enabled": False})

        status = display_manager.get_status_dict()
        caps = display_manager.capabilities
        return jsonify({
            "ok": True,
            "enabled": True,
            "model": display_manager.display_name,
            "model_id": config.get("model", DEFAULT_MODEL),
            "width": caps.width,
            "height": caps.height,
            "colors": list(caps.colors),
            **status,
        })

    @app.route("/api/hardware/display/settings")
    @handle_errors
    def api_hardware_display_settings_get():
        if not epaper_enabled or display_manager is None:
            return _disabled_response()
        return jsonify({
            "ok": True,
            "config": config,
            "available_models": [
                {"id": model_id, "display_name": name, "default_pins": MODEL_DEFAULT_PINS[model_id]}
                for model_id, name in MODEL_DISPLAY_NAMES.items()
            ],
        })

    @app.route("/api/hardware/display/settings", methods=["POST"])
    @handle_errors
    def api_hardware_display_settings_post():
        """Enable/refresh_mode/debounce_seconds/rotation_* only - applied
        live and autosaved. pins/spi/refresh_timeout are rejected here;
        use /reinit for those (see module docstring). rotation_* (task 40)
        needs no live-apply call to display_manager the way refresh_mode/
        debounce_seconds do - epaper_worker's poller just reads the saved
        config directly on its next tick."""
        if not epaper_enabled or display_manager is None:
            return _disabled_response()

        body = request.get_json(force=True) or {}
        if any(key in body for key in ("pins", "spi", "refresh_timeout")):
            return jsonify({
                "ok": False,
                "error": "pins/spi/refresh_timeout must go through POST /api/hardware/display/reinit",
            }), 400

        if "enabled" in body:
            config["enabled"] = bool(body["enabled"])
            if config["enabled"]:
                display_manager.start()
            else:
                display_manager.stop()

        mode_changed = "refresh_mode" in body
        if mode_changed:
            try:
                mode = RefreshMode(body["refresh_mode"])
            except ValueError:
                return jsonify({"ok": False, "error": f"Unknown refresh_mode: {body['refresh_mode']!r}"}), 400
            config["refresh_mode"] = mode.value

        if "debounce_seconds" in body:
            config["debounce_seconds"] = float(body["debounce_seconds"])

        if mode_changed or "debounce_seconds" in body:
            display_manager.set_refresh_mode(RefreshMode(config["refresh_mode"]), config["debounce_seconds"])

        if "rotation_enabled" in body:
            config["rotation_enabled"] = bool(body["rotation_enabled"])

        if "rotation_pages" in body:
            pages = body["rotation_pages"]
            if not isinstance(pages, list) or not all(isinstance(p, str) for p in pages):
                return jsonify({"ok": False, "error": "rotation_pages must be a list of strings"}), 400
            unknown = [p for p in pages if p not in ROTATION_ALLOWED_PAGES]
            if unknown:
                return jsonify({
                    "ok": False,
                    "error": f"rotation_pages contains unsupported page(s): {unknown!r} "
                             f"(allowed: {list(ROTATION_ALLOWED_PAGES)!r} - 'message' can only be shown manually, not rotated)",
                }), 400
            config["rotation_pages"] = pages

        if "rotation_interval_seconds" in body:
            try:
                interval = float(body["rotation_interval_seconds"])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "rotation_interval_seconds must be a number"}), 400
            if not (ROTATION_INTERVAL_MIN_SECONDS <= interval <= ROTATION_INTERVAL_MAX_SECONDS):
                return jsonify({
                    "ok": False,
                    "error": f"rotation_interval_seconds must be between {ROTATION_INTERVAL_MIN_SECONDS:.0f} "
                             f"and {ROTATION_INTERVAL_MAX_SECONDS:.0f}",
                }), 400
            config["rotation_interval_seconds"] = interval

        save_epaper_config(config_path, config)
        return jsonify({"ok": True, "config": config})

    @app.route("/api/hardware/display/reinit", methods=["POST"])
    @handle_errors
    def api_hardware_display_reinit():
        """GPIO/SPI/timeout AND model changes all go through here (plan
        section 1 item 2 / Phase 4) - a model switch changes
        DisplayCapabilities (size, colors), not just pins, so it gets the
        same explicit-confirm-and-validate treatment as a bad pin value,
        not a live-apply autosave."""
        if not epaper_enabled or display_manager is None:
            return _disabled_response()

        body = request.get_json(force=True) or {}
        new_config = dict(config)

        model_changed = "model" in body and body["model"] != config.get("model")
        if "model" in body:
            if body["model"] not in MODEL_DISPLAY_NAMES:
                return jsonify({"ok": False, "error": f"Unknown model: {body['model']!r}"}), 400
            new_config["model"] = body["model"]
            if model_changed and "pins" not in body:
                # Reset to the *new* model's own defaults rather than
                # keeping the old model's pins - e.g. Waveshare's PWR pin
                # has no meaning on a WeAct panel.
                new_config["pins"] = dict(MODEL_DEFAULT_PINS[body["model"]])

        if "pins" in body:
            new_config["pins"] = {**new_config["pins"], **body["pins"]}
        if "spi" in body:
            new_config["spi"] = {**config["spi"], **body["spi"]}
        if "refresh_timeout" in body:
            new_config["refresh_timeout"] = float(body["refresh_timeout"])

        # Release whatever the *current* model claimed before checking the
        # new pins - GpioRegistry.check() only exempts a pin already
        # claimed by the *same* owner name, and a model switch changes
        # that name (e.g. "waveshare_213g" -> "weact_154"), even when the
        # actual physical pins are identical (both panels use the same
        # HAT header). Without this, reconfiguring onto the very pins the
        # current driver already holds would incorrectly look like a
        # conflict with itself. swap_driver_and_start() below also releases
        # this via the old driver's stop() - doing it here too is redundant
        # but harmless (GpioRegistry.release() is idempotent).
        gpio_registry.release(config.get("model", DEFAULT_MODEL))
        try:
            gpio_registry.check(new_config["pins"], owner=new_config.get("model", DEFAULT_MODEL))
        except GpioConflictError as exc:
            # The live driver itself was never touched
            # (swap_driver_and_start() hasn't run yet at this point) - it's
            # still actively holding
            # its current pins. Restore its registry claim before
            # returning, or the registry would incorrectly believe those
            # pins are free while the running driver still uses them.
            gpio_registry.claim(config.get("pins", {}), owner=config.get("model", DEFAULT_MODEL))
            return jsonify({"ok": False, "error": str(exc)}), 409

        try:
            new_driver = build_driver(new_config, gpio_registry)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        ok, error = display_manager.swap_driver_and_start(new_driver, timeout=REINIT_CHECK_TIMEOUT)

        if not ok:
            # Roll back to the previous, already-working configuration
            # rather than leaving the live manager on a driver we just
            # proved doesn't work.
            old_driver = build_driver(config, gpio_registry)
            display_manager.swap_driver_and_start(old_driver, timeout=REINIT_CHECK_TIMEOUT)
            return jsonify({"ok": False, "error": error}), 400

        config.update(new_config)
        save_epaper_config(config_path, config)
        return jsonify({"ok": True, "config": config})

    @app.route("/api/hardware/display/test", methods=["POST"])
    @handle_errors
    def api_hardware_display_test():
        if not epaper_enabled or display_manager is None:
            return _disabled_response()
        image = test_pattern.render(display_manager.capabilities)
        display_manager.mark_dirty(image, priority=EventPriority.CRITICAL)
        return jsonify({"ok": True})

    @app.route("/api/hardware/display/clear", methods=["POST"])
    @handle_errors
    def api_hardware_display_clear():
        if not epaper_enabled or display_manager is None:
            return _disabled_response()
        blank_image, _draw = new_canvas(display_manager.capabilities)
        display_manager.mark_dirty(blank_image, priority=EventPriority.CRITICAL)
        return jsonify({"ok": True})

    @app.route("/api/hardware/display/refresh", methods=["POST"])
    @handle_errors
    def api_hardware_display_refresh():
        if not epaper_enabled or display_manager is None:
            return _disabled_response()
        image = build_status_image_now()
        display_manager.mark_dirty(image, priority=EventPriority.CRITICAL)
        return jsonify({"ok": True})

    @app.route("/api/hardware/display/show/<page>", methods=["POST"])
    @handle_errors
    def api_hardware_display_show(page):
        """Manual page switching (plan section 34). Pins epaper_worker's
        poller to `page` (via ui_state, shared with epaper_worker) so it
        stays selected across subsequent polls instead of reverting to
        Status on the next tick, and pushes an immediate CRITICAL refresh
        so the user sees the switch happen right away rather than waiting
        out the debounce window."""
        if not epaper_enabled or display_manager is None:
            return _disabled_response()
        if page not in KNOWN_SHOW_PAGES:
            return jsonify({"ok": False, "error": f"Unknown page: {page!r}"}), 404

        if ui_state is not None:
            ui_state["active_page"] = page

        if page == "status":
            image = build_status_image_now()
        else:
            image = build_page_image_now(page) if build_page_image_now else None
            if image is None:
                return jsonify({"ok": False, "error": f"Page not available: {page!r}"}), 404

        display_manager.mark_dirty(image, priority=EventPriority.CRITICAL)
        return jsonify({"ok": True, "active_page": page})
