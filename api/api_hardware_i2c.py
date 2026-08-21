"""REST routes for the I2C bus and RTC (DS3231) hardware cards - task 23,
first batch of the I2C subsystem. Read-only detection lives in
hardware/i2c_service.py and hardware/rtc_service.py; the two
reboot-required actions (enabling I2C, adding the RTC overlay) go through
hardware/hardware_config.py, the only module allowed to call the
privileged scripts/meshcenter-hw-config helper.

Deliberately modeled after api/api_hardware_display.py's shape (host-local
hardware, not radio telemetry) rather than the /api/devices list, which
represents Meshtastic sensor_data received over the mesh - see
loadPeripheralDevices()'s comment in static/chat.js for that distinction.
"""

from __future__ import annotations

from flask import jsonify

from hardware import hardware_config, i2c_service, rtc_service

DEFAULT_RTC_MODEL = "ds3231"


def register_hardware_i2c_routes(app, handle_errors, data_dir: str):
    @app.route("/api/hardware/i2c")
    @handle_errors
    def api_hardware_i2c_status():
        hardware_config.reconcile_pending(data_dir)
        scan = i2c_service.scan_bus()
        return jsonify({"ok": True, **scan})

    @app.route("/api/hardware/i2c/enable", methods=["POST"])
    @handle_errors
    def api_hardware_i2c_enable():
        result = hardware_config.enable_i2c(data_dir)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("reason", "enable-i2c failed")}), 500
        return jsonify({"ok": True, "requires_reboot": True})

    @app.route("/api/hardware/i2c/scan", methods=["POST"])
    @handle_errors
    def api_hardware_i2c_scan():
        # i2c_service.scan_bus() never caches - "scan" and the GET status
        # route both always run a fresh i2cdetect. Separate POST route
        # exists so the UI can distinguish "load status" from "user asked
        # for a rescan" (loading-state semantics only), not because the
        # underlying call differs.
        scan = i2c_service.scan_bus()
        return jsonify({"ok": True, **scan})

    @app.route("/api/hardware/rtc")
    @handle_errors
    def api_hardware_rtc_status():
        hardware_config.reconcile_pending(data_dir)
        status = rtc_service.get_status(model=DEFAULT_RTC_MODEL)
        pending = hardware_config.get_pending(data_dir)
        return jsonify({**status, "pending_setup": pending})

    @app.route("/api/hardware/rtc/configure", methods=["POST"])
    @handle_errors
    def api_hardware_rtc_configure():
        result = hardware_config.configure_rtc(data_dir, DEFAULT_RTC_MODEL)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("reason", "configure-rtc failed")}), 500
        return jsonify({"ok": True, "requires_reboot": True})
