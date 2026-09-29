"""Audit review 2026-09-29, finding F2b: NaN/Infinity injected through a
handful of `float(...)` conversions on incoming request data.

- api/api_settings.py's _normalize_coordinate() ran every value through
  `float()` then a plain `number < minimum or number > maximum` range
  check - which NEVER rejects NaN, because every comparison against NaN is
  False (`nan < x` and `nan > x` are both False, so `or` never fires).
  Infinity was already caught by that same check (inf > maximum is True),
  so only NaN was the actual hole here.
- api/api_hardware_display.py's `debounce_seconds`/`refresh_timeout` had
  NO range check at all - not even the leaky one above - so any finite OR
  non-finite number was accepted. A NaN debounce_seconds turns
  DisplayManager's `deadline = time.monotonic() + self._debounce_seconds`
  into NaN; every later `now >= deadline` check is then False forever
  (NaN comparisons are always False), silently freezing the panel's
  refresh cycle until a restart.
- Waypoints (api/api_waypoints.py) and rotation_interval_seconds
  (api/api_hardware_display.py) were ALREADY safe: both use a chained
  comparison (`not (-90 <= lat <= 90 ...)` / `not (MIN <= x <= MAX)`),
  which correctly rejects NaN (chained comparisons short-circuit through
  `and`, and `MIN <= nan` alone is already False) - confirmed here as a
  regression guard, not because they needed fixing.
"""
import math

import pytest
from flask import Flask
from unittest.mock import MagicMock, patch

from api.api_settings import _normalize_coordinate
from api.api_hardware_display import register_hardware_display_routes
from modules.display.config_store import DEFAULT_EPAPER_CONFIG, load_epaper_config, save_epaper_config


# ---------------------------------------------------------------------------
# _normalize_coordinate() - direct unit tests
# ---------------------------------------------------------------------------


def test_normalize_coordinate_rejects_nan():
    assert _normalize_coordinate(float("nan"), -90, 90) is None


def test_normalize_coordinate_rejects_positive_and_negative_infinity():
    assert _normalize_coordinate(float("inf"), -90, 90) is None
    assert _normalize_coordinate(float("-inf"), -90, 90) is None


def test_normalize_coordinate_still_accepts_a_normal_value():
    assert _normalize_coordinate(45.5, -90, 90) == 45.5
    assert _normalize_coordinate("45.5", -90, 90) == 45.5


def test_normalize_coordinate_still_rejects_out_of_range():
    assert _normalize_coordinate(200.0, -90, 90) is None
    assert _normalize_coordinate(-200.0, -90, 90) is None


def test_normalize_coordinate_still_rejects_garbage_and_none():
    assert _normalize_coordinate("not a number", -90, 90) is None
    assert _normalize_coordinate(None, -90, 90) is None
    assert _normalize_coordinate("", -90, 90) is None


# ---------------------------------------------------------------------------
# api/api_hardware_display.py - debounce_seconds / refresh_timeout
# ---------------------------------------------------------------------------


def _handle_errors(f):
    from functools import wraps

    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


@pytest.fixture
def api_env(tmp_path):
    app = Flask(__name__)
    config = dict(DEFAULT_EPAPER_CONFIG)
    config_path = str(tmp_path / "epaper_config.json")
    display_manager = MagicMock()
    gpio_registry = MagicMock()
    ui_state = {"active_page": "status"}
    with patch("api.api_hardware_display.save_epaper_config") as mock_save:
        register_hardware_display_routes(
            app, display_manager, True, _handle_errors,
            config=config, config_path=config_path, gpio_registry=gpio_registry,
            build_status_image_now=lambda: None, build_page_image_now=lambda page: None,
            ui_state=ui_state,
        )
        yield {
            "app": app, "client": app.test_client(), "config": config,
            "display_manager": display_manager, "gpio_registry": gpio_registry, "mock_save": mock_save,
        }


def _post_settings(client, body):
    return client.post("/api/hardware/display/settings", json=body)


def _post_reinit(client, body):
    return client.post("/api/hardware/display/reinit", json=body)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_debounce_seconds_rejects_non_finite(api_env, bad_value):
    response = _post_settings(api_env["client"], {"debounce_seconds": bad_value})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    assert api_env["config"]["debounce_seconds"] == DEFAULT_EPAPER_CONFIG["debounce_seconds"]
    api_env["display_manager"].set_refresh_mode.assert_not_called()


def test_debounce_seconds_rejects_out_of_range(api_env):
    response = _post_settings(api_env["client"], {"debounce_seconds": -5})
    assert response.status_code == 400

    response = _post_settings(api_env["client"], {"debounce_seconds": 99999})
    assert response.status_code == 400


def test_debounce_seconds_rejects_non_numeric(api_env):
    response = _post_settings(api_env["client"], {"debounce_seconds": "soon"})
    assert response.status_code == 400


def test_debounce_seconds_accepts_a_valid_value(api_env):
    response = _post_settings(api_env["client"], {"debounce_seconds": 45})

    assert response.status_code == 200
    assert api_env["config"]["debounce_seconds"] == 45.0
    api_env["display_manager"].set_refresh_mode.assert_called_once()


def test_debounce_seconds_zero_is_a_valid_value(api_env):
    """0 means "no debounce, refresh immediately" - a real, meaningful
    value, not something the range check should reject."""
    response = _post_settings(api_env["client"], {"debounce_seconds": 0})

    assert response.status_code == 200
    assert api_env["config"]["debounce_seconds"] == 0.0


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_refresh_timeout_rejects_non_finite(api_env, bad_value):
    response = _post_reinit(api_env["client"], {"refresh_timeout": bad_value})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    # The rejection must happen before anything GPIO/driver-related runs.
    api_env["gpio_registry"].release.assert_not_called()


def test_refresh_timeout_rejects_out_of_range(api_env):
    response = _post_reinit(api_env["client"], {"refresh_timeout": 0})
    assert response.status_code == 400

    response = _post_reinit(api_env["client"], {"refresh_timeout": 99999})
    assert response.status_code == 400


def test_refresh_timeout_rejects_non_numeric(api_env):
    response = _post_reinit(api_env["client"], {"refresh_timeout": "forever"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# modules/display/config_store.py - sanitize pre-existing bad values on load
# ---------------------------------------------------------------------------


def test_load_epaper_config_sanitizes_a_nan_already_on_disk(tmp_path):
    path = str(tmp_path / "epaper_config.json")
    bad_config = dict(DEFAULT_EPAPER_CONFIG)
    bad_config["debounce_seconds"] = float("nan")
    bad_config["refresh_timeout"] = float("inf")
    bad_config["rotation_interval_seconds"] = float("-inf")
    save_epaper_config(path, bad_config)

    loaded = load_epaper_config(path)

    assert loaded["debounce_seconds"] == DEFAULT_EPAPER_CONFIG["debounce_seconds"]
    assert loaded["refresh_timeout"] == DEFAULT_EPAPER_CONFIG["refresh_timeout"]
    assert loaded["rotation_interval_seconds"] == DEFAULT_EPAPER_CONFIG["rotation_interval_seconds"]


def test_load_epaper_config_sanitizes_a_non_numeric_value_on_disk(tmp_path):
    path = str(tmp_path / "epaper_config.json")
    bad_config = dict(DEFAULT_EPAPER_CONFIG)
    bad_config["debounce_seconds"] = "not a number"
    save_epaper_config(path, bad_config)

    loaded = load_epaper_config(path)

    assert loaded["debounce_seconds"] == DEFAULT_EPAPER_CONFIG["debounce_seconds"]


def test_load_epaper_config_keeps_a_valid_value(tmp_path):
    path = str(tmp_path / "epaper_config.json")
    good_config = dict(DEFAULT_EPAPER_CONFIG)
    good_config["debounce_seconds"] = 12.5
    save_epaper_config(path, good_config)

    loaded = load_epaper_config(path)

    assert loaded["debounce_seconds"] == 12.5


# ---------------------------------------------------------------------------
# Regression guard: paths already safe via a chained comparison stay safe
# ---------------------------------------------------------------------------


def test_rotation_interval_chained_comparison_already_rejects_nan(api_env):
    """Not a new fix - confirms the EXISTING `not (MIN <= x <= MAX)` check
    already handles NaN correctly, so this doesn't regress."""
    response = _post_settings(api_env["client"], {"rotation_interval_seconds": float("nan")})
    assert response.status_code == 400


def test_a_plain_range_check_alone_does_not_reject_nan():
    """Sanity check on the underlying Python semantics this whole finding
    rests on - not a MeshCenter test, a language-behavior pin."""
    nan = float("nan")
    assert not (nan < -90 or nan > 90), "a plain `or` range check never catches NaN"
    assert not (-90 <= nan <= 90), "but a chained comparison correctly treats it as out of range"
    assert not math.isfinite(nan)


# ---------------------------------------------------------------------------
# server.py's load_settings() - item 4: sanitize settings.json on load,
# loudly (a WARNING naming the key, the bad value and the replacement)
# ---------------------------------------------------------------------------


def _write_settings_file(srv, data):
    import json as _json
    with open(srv.SETTINGS_FILE, "w", encoding="utf-8") as f:
        _json.dump(data, f, allow_nan=True)


def test_load_settings_sanitizes_a_nan_manual_coordinate(server_module, capsys):
    srv = server_module
    _write_settings_file(srv, {"reference_location": {"manual": {"latitude": float("nan"), "longitude": 30.5}}})

    srv.load_settings()

    assert srv.settings["reference_location"]["manual"]["latitude"] is None
    assert srv.settings["reference_location"]["manual"]["longitude"] == 30.5
    out = capsys.readouterr().out
    assert "WARNING" in out and "reference_location.manual.latitude" in out and "nan" in out.lower()


def test_load_settings_sanitizes_infinity_and_out_of_range_coordinates(server_module):
    srv = server_module
    _write_settings_file(srv, {"reference_location": {"manual": {
        "latitude": float("inf"), "longitude": float("-inf"),
    }}})

    srv.load_settings()

    assert srv.settings["reference_location"]["manual"]["latitude"] is None
    assert srv.settings["reference_location"]["manual"]["longitude"] is None


def test_load_settings_sanitizes_the_legacy_flat_coordinate_fields_too(server_module):
    srv = server_module
    _write_settings_file(srv, {"reference_location": {"latitude": float("nan"), "longitude": 12.0}})

    srv.load_settings()

    assert srv.settings["reference_location"]["latitude"] is None
    assert srv.settings["reference_location"]["longitude"] == 12.0


def test_load_settings_keeps_a_valid_manual_coordinate(server_module):
    srv = server_module
    _write_settings_file(srv, {"reference_location": {"manual": {"latitude": 45.5, "longitude": -73.5}}})

    srv.load_settings()

    assert srv.settings["reference_location"]["manual"]["latitude"] == 45.5
    assert srv.settings["reference_location"]["manual"]["longitude"] == -73.5


def test_load_settings_persists_the_sanitized_value_to_disk(server_module):
    """load_settings() already unconditionally re-saves via save_settings()
    at the end - the sanitized value must survive that, not just live in
    memory for this process's lifetime."""
    srv = server_module
    _write_settings_file(srv, {"reference_location": {"manual": {"latitude": float("nan"), "longitude": 1.0}}})

    srv.load_settings()

    with open(srv.SETTINGS_FILE, encoding="utf-8") as f:
        on_disk = f.read()
    assert "NaN" not in on_disk


def test_the_strict_json_provider_raises_for_nan(server_module):
    """Insurance measure (F2b, enabled only after confirming no endpoint
    serves NaN today on any deployed instance and no test relies on the
    default allow_nan=True): a NaN reaching jsonify() now raises loudly
    instead of silently producing the non-standard `NaN` token in the
    response body."""
    srv = server_module
    with srv.app.app_context():
        with pytest.raises(ValueError):
            srv.jsonify({"x": float("nan")})


def test_the_strict_json_provider_raises_for_infinity(server_module):
    srv = server_module
    with srv.app.app_context():
        with pytest.raises(ValueError):
            srv.jsonify({"x": float("inf")})


def test_the_strict_json_provider_still_serializes_normal_data(server_module):
    srv = server_module
    with srv.app.app_context():
        response = srv.jsonify({"ok": True, "value": 45.5, "text": "hi"})
        assert response.get_json() == {"ok": True, "value": 45.5, "text": "hi"}


def test_load_settings_does_not_crash_on_an_infinite_battery_capacity(server_module):
    """int(float('inf')) raises OverflowError, not ValueError/TypeError -
    the original except clause missed it, so this would have crashed
    load_settings() itself at server startup before this fix."""
    srv = server_module
    _write_settings_file(srv, {"power": {"battery_capacity_mah": float("inf")}})

    srv.load_settings()

    assert srv.settings["power"]["battery_capacity_mah"] == 3000
