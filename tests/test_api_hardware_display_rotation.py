"""Tests for api/api_hardware_display.py's task-40 additions to
POST /api/hardware/display/settings: rotation_enabled/rotation_pages/
rotation_interval_seconds validation and persistence. Same flask
test_client() pattern as test_api_hardware_i2c.py; display_manager itself
is a MagicMock since these fields need no live-apply call to it (unlike
refresh_mode/debounce_seconds) - only config gets updated and saved.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from api.api_hardware_display import register_hardware_display_routes
from modules.display.config_store import DEFAULT_EPAPER_CONFIG


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
    ui_state = {"active_page": "status"}
    with patch("api.api_hardware_display.save_epaper_config") as mock_save:
        register_hardware_display_routes(
            app, display_manager, True, _handle_errors,
            config=config, config_path=config_path, gpio_registry=MagicMock(),
            build_status_image_now=lambda: None, build_page_image_now=lambda page: None,
            ui_state=ui_state,
        )
        yield {"app": app, "client": app.test_client(), "config": config, "mock_save": mock_save}


def _post(client, body):
    return client.post(
        "/api/hardware/display/settings",
        json=body,
    )


# ---------------- rotation_enabled ----------------

def test_rotation_enabled_true_saved(api_env):
    response = _post(api_env["client"], {"rotation_enabled": True})
    assert response.status_code == 200
    assert api_env["config"]["rotation_enabled"] is True
    api_env["mock_save"].assert_called()


def test_rotation_enabled_false_saved(api_env):
    api_env["config"]["rotation_enabled"] = True
    response = _post(api_env["client"], {"rotation_enabled": False})
    assert response.status_code == 200
    assert api_env["config"]["rotation_enabled"] is False


# ---------------- rotation_pages ----------------

def test_rotation_pages_valid_subset_saved(api_env):
    response = _post(api_env["client"], {"rotation_pages": ["status", "power"]})
    assert response.status_code == 200
    assert api_env["config"]["rotation_pages"] == ["status", "power"]


def test_rotation_pages_rejects_message(api_env):
    response = _post(api_env["client"], {"rotation_pages": ["status", "message"]})
    assert response.status_code == 400
    body = response.get_json()
    assert body["ok"] is False
    assert "message" in body["error"]
    # Rejected wholesale - config must not have been partially updated.
    assert api_env["config"]["rotation_pages"] == []


def test_rotation_pages_rejects_unknown_value(api_env):
    response = _post(api_env["client"], {"rotation_pages": ["status", "not_a_real_page"]})
    assert response.status_code == 400
    assert "not_a_real_page" in response.get_json()["error"]


def test_rotation_pages_rejects_non_list(api_env):
    response = _post(api_env["client"], {"rotation_pages": "status"})
    assert response.status_code == 400


def test_rotation_pages_empty_list_allowed(api_env):
    api_env["config"]["rotation_pages"] = ["status"]
    response = _post(api_env["client"], {"rotation_pages": []})
    assert response.status_code == 200
    assert api_env["config"]["rotation_pages"] == []


# ---------------- rotation_interval_seconds ----------------

def test_rotation_interval_valid_value_saved(api_env):
    response = _post(api_env["client"], {"rotation_interval_seconds": 45})
    assert response.status_code == 200
    assert api_env["config"]["rotation_interval_seconds"] == 45.0


def test_rotation_interval_rejects_non_numeric(api_env):
    response = _post(api_env["client"], {"rotation_interval_seconds": "soon"})
    assert response.status_code == 400


def test_rotation_interval_rejects_zero_or_negative(api_env):
    response = _post(api_env["client"], {"rotation_interval_seconds": 0})
    assert response.status_code == 400
    response = _post(api_env["client"], {"rotation_interval_seconds": -5})
    assert response.status_code == 400


def test_rotation_interval_rejects_too_large(api_env):
    response = _post(api_env["client"], {"rotation_interval_seconds": 999999})
    assert response.status_code == 400


# ---------------- combined ----------------

def test_all_rotation_fields_together(api_env):
    response = _post(api_env["client"], {
        "rotation_enabled": True,
        "rotation_pages": ["status", "radio", "power", "system"],
        "rotation_interval_seconds": 20,
    })
    assert response.status_code == 200
    config = api_env["config"]
    assert config["rotation_enabled"] is True
    assert config["rotation_pages"] == ["status", "radio", "power", "system"]
    assert config["rotation_interval_seconds"] == 20.0
