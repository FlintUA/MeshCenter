"""REST route for the e-paper display's Hardware card (read-only part).
e-Paper Stage 1 plan, Phase 6 (section 36).

"Online" here means "successfully initialized and successfully completed
its last refresh" (DisplayManager.status == ONLINE), never "a display
model was recognized over SPI" - detect() only confirms /dev/spidevN.M
exists, it doesn't prove the panel is actually there or working. Separate
from api_camera_manager.py's pattern (no rescan step) since e-paper has
exactly one configured driver, not a dynamically discovered set.
"""

from __future__ import annotations

from flask import jsonify


def register_hardware_display_routes(app, display_manager, epaper_enabled: bool, handle_errors):
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
            "width": caps.width,
            "height": caps.height,
            "colors": list(caps.colors),
            **status,
        })
