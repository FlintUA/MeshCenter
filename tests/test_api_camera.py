"""Tests for api/api_camera.py, specifically path traversal prevention in screenshot endpoints."""

from functools import wraps
from unittest.mock import MagicMock
import os
import pytest
from flask import Flask

from conftest import _stub_libcamera
_stub_libcamera()

from api.api_camera import register_camera_routes
import camera.camera as camera_module


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(camera_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(camera_module, "SCREENSHOTS_DIR", str(screenshots_dir))

    app = Flask(__name__)
    camera_manager_state = {"manager": None}
    device_manager = MagicMock()

    register_camera_routes(
        app,
        camera_module,
        camera_manager_state,
        device_manager,
        _handle_errors
    )
    return {"app": app, "client": app.test_client(), "screenshots_dir": screenshots_dir, "tmp_path": tmp_path}


def test_api_camera_screenshot_file_valid(api_env):
    screenshots_dir = api_env["screenshots_dir"]
    sub_dir = screenshots_dir / "2025" / "01" / "01"
    sub_dir.mkdir(parents=True, exist_ok=True)
    file_path = sub_dir / "MC_test.jpg"
    file_path.write_bytes(b"fake jpeg data")

    response = api_env["client"].get("/api/camera/screenshot/2025/01/01/MC_test.jpg")
    assert response.status_code == 200
    assert response.data == b"fake jpeg data"


def test_api_camera_screenshot_file_not_found(api_env):
    response = api_env["client"].get("/api/camera/screenshot/nonexistent.jpg")
    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": "File not found"}


def test_api_camera_screenshot_file_path_traversal(api_env):
    # Try traversing out to parent directory
    secret_file = api_env["tmp_path"] / "secret.txt"
    secret_file.write_text("secret data")

    response = api_env["client"].get("/api/camera/screenshot/../secret.txt")
    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": "File not found"}

    response = api_env["client"].get("/api/camera/screenshot/../../server.py")
    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": "File not found"}
