from copy import deepcopy

from flask import jsonify, request
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import re


DEFAULT_SETTINGS = {
    # "auto" resolves from the browser's language client-side; an explicit
    # value overrides that. See static/i18n.js for the supported locale list.
    "language": "auto",

    "units": {
        "temperature": "c",
        "pressure": "hpa",
        "wind": "ms",
        "time_format": "24",
    },

    "listener_autorecovery": {
        "enabled": False,
        "delay": 60,
    },

    "maps": {
        "provider": "osm",
    },

    "weather": {
        "provider": "openweather",
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

    "waypoints": {
        # Legacy/global fallback used until a radio-specific preference exists.
        "last_channel_index": 0,
        "last_duration_seconds": 3600,
        "post_notification": True,
        # Preferences are keyed by active radio profile ID.
        "profile_defaults": {},
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


SUPPORTED_LANGUAGES = ("auto", "en", "de", "ru", "uk")

# Kept as a plain tuple (like SUPPORTED_LANGUAGES) rather than importing the
# weather package here - normalize_settings() only needs to know which ids
# are valid, not the provider classes themselves.
SUPPORTED_WEATHER_PROVIDERS = ("openweather", "weatherapi")


def normalize_settings(settings):
    if not isinstance(settings, dict):
        settings = {}

    # ---------------- Language ----------------

    language = str(settings.get("language", "auto")).strip().lower()

    if language not in SUPPORTED_LANGUAGES:
        language = "auto"

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

    time_format = str(
        units.get("time_format", "24")
    ).strip().lower()

    if temperature not in ("c", "f", "both"):
        temperature = "c"

    if pressure not in ("hpa", "mmhg", "both"):
        pressure = "hpa"

    if wind not in ("ms", "kmh", "mph"):
        wind = "ms"

    if time_format not in ("12", "24"):
        time_format = "24"

    # ---------------- Power / battery ----------------

    power = settings.get("power", {})
    if not isinstance(power, dict):
        power = {}

    try:
        battery_capacity_mah = int(power.get("battery_capacity_mah", 3000))
    except (TypeError, ValueError):
        battery_capacity_mah = 3000

    battery_capacity_mah = max(100, min(50000, battery_capacity_mah))

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

    # ---------------- Weather provider ----------------

    weather = settings.get("weather", {})
    if not isinstance(weather, dict):
        weather = {}

    weather_provider = str(
        weather.get("provider", "openweather")
    ).strip().lower()

    if weather_provider not in SUPPORTED_WEATHER_PROVIDERS:
        weather_provider = "openweather"

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

    # ---------------- Waypoint composer defaults ----------------

    waypoint_settings = settings.get("waypoints", {})
    if not isinstance(waypoint_settings, dict):
        waypoint_settings = {}

    try:
        waypoint_channel_index = int(
            waypoint_settings.get("last_channel_index", 0)
        )
    except (TypeError, ValueError):
        waypoint_channel_index = 0

    waypoint_channel_index = max(0, min(7, waypoint_channel_index))

    allowed_waypoint_durations = {
        900, 1800, 3600, 10800, 21600,
        43200, 86400, 172800, 604800,
    }

    try:
        waypoint_duration_seconds = int(
            waypoint_settings.get("last_duration_seconds", 3600)
        )
    except (TypeError, ValueError):
        waypoint_duration_seconds = 3600

    if waypoint_duration_seconds not in allowed_waypoint_durations:
        waypoint_duration_seconds = 3600

    waypoint_post_notification = bool(
        waypoint_settings.get("post_notification", True)
    )

    raw_profile_defaults = waypoint_settings.get("profile_defaults", {})
    if not isinstance(raw_profile_defaults, dict):
        raw_profile_defaults = {}

    waypoint_profile_defaults = {}
    for raw_profile_id, raw_defaults in raw_profile_defaults.items():
        profile_id = str(raw_profile_id or "").strip().lower()
        if not profile_id or not re.fullmatch(r"[a-z0-9_-]{1,64}", profile_id):
            continue
        if not isinstance(raw_defaults, dict):
            continue

        try:
            profile_channel_index = int(
                raw_defaults.get("last_channel_index", waypoint_channel_index)
            )
        except (TypeError, ValueError):
            profile_channel_index = waypoint_channel_index
        profile_channel_index = max(0, min(7, profile_channel_index))

        try:
            profile_duration_seconds = int(
                raw_defaults.get(
                    "last_duration_seconds",
                    waypoint_duration_seconds,
                )
            )
        except (TypeError, ValueError):
            profile_duration_seconds = waypoint_duration_seconds
        if profile_duration_seconds not in allowed_waypoint_durations:
            profile_duration_seconds = waypoint_duration_seconds

        waypoint_profile_defaults[profile_id] = {
            "last_channel_index": profile_channel_index,
            "last_duration_seconds": profile_duration_seconds,
            "post_notification": bool(
                raw_defaults.get(
                    "post_notification",
                    waypoint_post_notification,
                )
            ),
        }

    return {
        "language": language,

        "units": {
            "temperature": temperature,
            "pressure": pressure,
            "wind": wind,
            "time_format": time_format,
        },

        "power": {
            "battery_capacity_mah": battery_capacity_mah,
        },

        "listener_autorecovery": {
            "enabled": enabled,
            "delay": delay,
        },

        "maps": {
            "provider": map_provider,
        },

        "weather": {
            "provider": weather_provider,
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

        "waypoints": {
            "last_channel_index": waypoint_channel_index,
            "last_duration_seconds": waypoint_duration_seconds,
            "post_notification": waypoint_post_notification,
            "profile_defaults": waypoint_profile_defaults,
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
    weather_manager=None,
    resolve_weather_language=None,
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
                "error_code": "invalid_coordinates",
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
                "error_code": "invalid_settings_payload",
            }), 400

        with state_lock:
            current = normalize_settings(settings)
            merged = _deep_merge(current, updates)
            new_settings = normalize_settings(merged)

            language_changed = new_settings.get("language") != current.get("language")
            provider_changed = (
                new_settings.get("weather", {}).get("provider")
                != current.get("weather", {}).get("provider")
            )

            settings.clear()
            settings.update(new_settings)
            save_settings()

            response_settings = normalize_settings(settings)

        # Weather data is a single cache shared by every connected client, so
        # a language or active-provider change here is a global update, not a
        # per-request one - see WeatherProvider.set_language()/set_api_key().
        if weather_manager is not None:
            if provider_changed:
                weather_manager.set_active(response_settings.get("weather", {}).get("provider", "openweather"))
            if (language_changed or provider_changed) and resolve_weather_language is not None:
                weather_manager.active().set_language(
                    resolve_weather_language(response_settings.get("language", "auto"))
                )

        return jsonify({
            "ok": True,
            "settings": response_settings,
        })
