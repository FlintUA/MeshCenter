from flask import jsonify, request


DEFAULT_SETTINGS = {
    "units": {
        "temperature": "c",
        "pressure": "hpa",
        "wind": "ms"
    },

    "listener_autorecovery": {
        "enabled": False,
        "delay": 60
    }
}


def normalize_settings(settings):

    if not isinstance(settings, dict):
        settings = {}

    # ---------------- Units ----------------

    units = settings.get("units", {})
    if not isinstance(units, dict):
        units = {}

    temperature = units.get("temperature", "c")
    pressure = units.get("pressure", "hpa")
    wind = units.get("wind", "ms")

    if temperature not in ("c", "f", "both"):
        temperature = "c"

    if pressure not in ("hpa", "mmhg", "both"):
        pressure = "hpa"

    if wind not in ("ms", "kmh", "mph"):
        wind = "ms"

    # -------- Listener Auto Recovery --------

    recovery = settings.get("listener_autorecovery", {})

    if not isinstance(recovery, dict):
        recovery = {}

    enabled = bool(recovery.get("enabled", False))

    delay = int(recovery.get("delay", 60))

    if delay not in (30, 60, 90, 120, 180, 300):
        delay = 60

    return {

        "units": {
            "temperature": temperature,
            "pressure": pressure,
            "wind": wind
        },

        "listener_autorecovery": {
            "enabled": enabled,
            "delay": delay
        }

    }


def register_settings_routes(
    app,
    state_lock,
    settings,
    save_settings,
    handle_errors,
):
    @app.route("/api/settings", methods=["GET"])
    @handle_errors
    def api_get_settings():
        with state_lock:
            return jsonify({
                "ok": True,
                "settings": normalize_settings(settings)
            })

    @app.route("/api/settings", methods=["POST"])
    @handle_errors
    def api_update_settings():
        data = request.get_json(force=True)
        new_settings = normalize_settings(data.get("settings", data))

        with state_lock:
            settings.clear()
            settings.update(new_settings)
            save_settings()

        return jsonify({
            "ok": True,
            "settings": normalize_settings(settings)
        })
