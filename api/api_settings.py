from copy import deepcopy

from flask import jsonify, request
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json


DEFAULT_SETTINGS = {
    "units": {
        "temperature": "c",
        "pressure": "hpa",
        "wind": "ms",
    },

    "listener_autorecovery": {
        "enabled": False,
        "delay": 60,
    },

    "maps": {
        "provider": "osm",
    },

    "reference_location": {
        "mode": "disabled",
        "manual": {
            "latitude": None,
            "longitude": None,
        },
        "node_id": "",
        "place_name": "",
    },
}


def _deep_merge(base, updates):
    """
    Recursively merge dictionaries without deleting unrelated settings.
    """
    result = deepcopy(base)

    if not isinstance(updates, dict):
        return result

    for key, value in updates.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)

    return result


def _normalize_coordinate(value, minimum, maximum):
    if value is None or value == "":
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if number < minimum or number > maximum:
        return None

    return number


def normalize_settings(settings):
    if not isinstance(settings, dict):
        settings = {}

    # ---------------- Units ----------------

    units = settings.get("units", {})
    if not isinstance(units, dict):
        units = {}

    temperature = str(
        units.get("temperature", "c")
    ).strip().lower()

    pressure = str(
        units.get("pressure", "hpa")
    ).strip().lower()

    wind = str(
        units.get("wind", "ms")
    ).strip().lower()

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

    try:
        delay = int(recovery.get("delay", 60))
    except (TypeError, ValueError):
        delay = 60

    if delay not in (30, 60, 90, 120, 180, 300):
        delay = 60

    # ---------------- Maps ----------------

    maps = settings.get("maps", {})
    if not isinstance(maps, dict):
        maps = {}

    map_provider = str(
        maps.get("provider", "osm")
    ).strip().lower()

    if map_provider not in ("osm", "google"):
        map_provider = "osm"

    # ---------------- Reference location ----------------

    reference_location = settings.get(
        "reference_location",
        {},
    )

    if not isinstance(reference_location, dict):
        reference_location = {}

    reference_mode = str(
        reference_location.get("mode", "disabled")
    ).strip().lower()

    if reference_mode not in ("disabled", "manual", "node"):
        reference_mode = "disabled"

    reference_node_id = str(
        reference_location.get("node_id", "")
    ).strip()

    reference_place_name = str(
        reference_location.get("place_name", "")
    ).strip()

    manual = reference_location.get("manual", {})
    if not isinstance(manual, dict):
        manual = {}

    # Backward compatibility with the old flat structure:
    # reference_location.latitude / longitude
    manual_latitude_raw = manual.get(
        "latitude",
        reference_location.get("latitude"),
    )

    manual_longitude_raw = manual.get(
        "longitude",
        reference_location.get("longitude"),
    )

    manual_latitude = _normalize_coordinate(
        manual_latitude_raw,
        -90.0,
        90.0,
    )

    manual_longitude = _normalize_coordinate(
        manual_longitude_raw,
        -180.0,
        180.0,
    )

    return {
        "units": {
            "temperature": temperature,
            "pressure": pressure,
            "wind": wind,
        },

        "listener_autorecovery": {
            "enabled": enabled,
            "delay": delay,
        },

        "maps": {
            "provider": map_provider,
        },

        "reference_location": {
            "mode": reference_mode,
            "manual": {
                "latitude": manual_latitude,
                "longitude": manual_longitude,
            },
            "node_id": reference_node_id,
            "place_name": reference_place_name,
        },
    }



def _reverse_geocode(latitude, longitude):
    query = urlencode({
        "format": "jsonv2",
        "lat": f"{latitude:.7f}",
        "lon": f"{longitude:.7f}",
        "zoom": 10,
        "addressdetails": 1,
        "accept-language": "en",
    })

    request_object = Request(
        f"https://nominatim.openstreetmap.org/reverse?{query}",
        headers={
            "User-Agent": "MeshCenter/1.0 (reference-location)",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request_object, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""

    address = payload.get("address") or {}
    locality = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or address.get("state")
        or ""
    )
    country = address.get("country") or ""

    if locality and country:
        return f"{locality}, {country}"

    return locality or country

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
            normalized = normalize_settings(settings)

        return jsonify({
            "ok": True,
            "settings": normalized,
        })


    @app.route("/api/settings/reference-location-name", methods=["POST"])
    @handle_errors
    def api_reference_location_name():
        data = request.get_json(force=True) or {}
        latitude = _normalize_coordinate(data.get("latitude"), -90.0, 90.0)
        longitude = _normalize_coordinate(data.get("longitude"), -180.0, 180.0)

        if latitude is None or longitude is None:
            return jsonify({
                "ok": False,
                "error": "Invalid coordinates",
            }), 400

        return jsonify({
            "ok": True,
            "place_name": _reverse_geocode(latitude, longitude),
        })

    @app.route("/api/settings", methods=["POST"])
    @handle_errors
    def api_update_settings():
        data = request.get_json(force=True) or {}
        updates = data.get("settings", data)

        if not isinstance(updates, dict):
            return jsonify({
                "ok": False,
                "error": "Invalid settings payload",
            }), 400

        with state_lock:
            current = normalize_settings(settings)
            merged = _deep_merge(current, updates)
            new_settings = normalize_settings(merged)

            settings.clear()
            settings.update(new_settings)
            save_settings()

            response_settings = normalize_settings(settings)

        return jsonify({
            "ok": True,
            "settings": response_settings,
        })
