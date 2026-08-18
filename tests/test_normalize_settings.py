"""Tests for api/api_settings.py's normalize_settings() - the single place
that turns a possibly-malformed settings.json (or a partial POST /api/settings
body) into a fully-shaped, safe-to-use dict. No server.py import needed here -
this module has no hardware/CLI dependencies of its own.
"""

from api.api_settings import normalize_settings


def test_empty_input_returns_full_default_shape():
    result = normalize_settings({})
    assert result["language"] == "auto"
    assert result["units"]["temperature"] == "c"
    assert result["maps"]["provider"] == "osm"
    assert result["weather"]["provider"] == "openweather"
    assert result["waypoints"]["last_duration_seconds"] == 3600
    assert result["updates"]["interval"] == 86400


def test_non_dict_input_treated_as_empty():
    for bad_input in (None, [], "not a dict", 42):
        result = normalize_settings(bad_input)
        assert result["language"] == "auto"


def test_invalid_language_falls_back_to_auto():
    assert normalize_settings({"language": "xx"})["language"] == "auto"
    assert normalize_settings({"language": "EN"})["language"] == "en"


def test_invalid_unit_values_fall_back_to_defaults():
    result = normalize_settings({
        "units": {"temperature": "kelvin", "pressure": "bar", "wind": "knots", "time_format": "36"},
    })
    assert result["units"] == {"temperature": "c", "pressure": "hpa", "wind": "ms", "time_format": "24"}


def test_valid_unit_values_pass_through():
    result = normalize_settings({
        "units": {"temperature": "f", "pressure": "mmhg", "wind": "kmh", "time_format": "12"},
    })
    assert result["units"] == {"temperature": "f", "pressure": "mmhg", "wind": "kmh", "time_format": "12"}


def test_battery_capacity_clamped_to_valid_range():
    assert normalize_settings({"power": {"battery_capacity_mah": 50}})["power"]["battery_capacity_mah"] == 100
    assert normalize_settings({"power": {"battery_capacity_mah": 999999}})["power"]["battery_capacity_mah"] == 50000
    assert normalize_settings({"power": {"battery_capacity_mah": "not a number"}})["power"]["battery_capacity_mah"] == 3000


def test_listener_autorecovery_rejects_arbitrary_delay():
    result = normalize_settings({"listener_autorecovery": {"enabled": True, "delay": 45}})
    assert result["listener_autorecovery"]["enabled"] is True
    # 45 isn't one of the allowed steps (30/60/90/120/180/300) - falls back.
    assert result["listener_autorecovery"]["delay"] == 60


def test_map_and_weather_provider_reject_unknown_values():
    result = normalize_settings({"maps": {"provider": "bing"}, "weather": {"provider": "accuweather"}})
    assert result["maps"]["provider"] == "osm"
    assert result["weather"]["provider"] == "openweather"


def test_reference_location_coordinates_out_of_range_become_none():
    result = normalize_settings({
        "reference_location": {"mode": "manual", "manual": {"latitude": 200, "longitude": -200}},
    })
    assert result["reference_location"]["manual"]["latitude"] is None
    assert result["reference_location"]["manual"]["longitude"] is None


def test_reference_location_backward_compat_flat_coordinates():
    # Old shape: latitude/longitude directly on reference_location, not nested
    # under "manual" - normalize_settings() must still pick them up.
    result = normalize_settings({
        "reference_location": {"mode": "manual", "latitude": 52.5, "longitude": 13.4},
    })
    assert result["reference_location"]["manual"]["latitude"] == 52.5
    assert result["reference_location"]["manual"]["longitude"] == 13.4


def test_waypoint_channel_index_and_duration_clamped():
    result = normalize_settings({
        "waypoints": {"last_channel_index": 99, "last_duration_seconds": 12345},
    })
    assert result["waypoints"]["last_channel_index"] == 7
    # 12345 isn't an allowed duration step - falls back to the default.
    assert result["waypoints"]["last_duration_seconds"] == 3600


def test_waypoint_profile_defaults_drop_invalid_profile_ids():
    result = normalize_settings({
        "waypoints": {
            "profile_defaults": {
                "valid-id_1": {"last_channel_index": 2},
                "Invalid ID With Spaces!": {"last_channel_index": 3},
            },
        },
    })
    assert "valid-id_1" in result["waypoints"]["profile_defaults"]
    assert "Invalid ID With Spaces!" not in result["waypoints"]["profile_defaults"]
    assert result["waypoints"]["profile_defaults"]["valid-id_1"]["last_channel_index"] == 2


def test_timer_target_type_and_channel_index_normalized():
    result = normalize_settings({"timers": {"target_type": "satellite", "channel_index": -5}})
    assert result["timers"]["target_type"] == "node"
    assert result["timers"]["channel_index"] == 0


def test_updates_interval_floored_at_five_minutes():
    result = normalize_settings({"updates": {"interval": 10}})
    assert result["updates"]["interval"] == 300


def test_browser_notifications_categories_fill_in_missing_keys_with_defaults():
    result = normalize_settings({
        "browser_notifications": {"enabled": True, "categories": {"timer": False}},
    })
    assert result["browser_notifications"]["enabled"] is True
    assert result["browser_notifications"]["categories"]["timer"] is False
    # Untouched categories keep DEFAULT_SETTINGS' own defaults, not True/False
    # picked arbitrarily - dm_message defaults True, channel_message False.
    assert result["browser_notifications"]["categories"]["dm_message"] is True
    assert result["browser_notifications"]["categories"]["channel_message"] is False
