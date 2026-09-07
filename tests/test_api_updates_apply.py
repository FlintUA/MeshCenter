"""tests/test_api_updates_apply.py -- api/api_updates.py's
register_updates_routes(), POST /api/updates/apply specifically.

Same flask test_client() pattern as test_api_hardware_i2c.py. Mocks
meshsrv.update_service.git_preflight()/apply_update() (already covered
directly by tests/test_update_service_requirements_changed.py) so this
file is purely about the route's own decision logic: PR #231 review
(3rd pass) - apply_update() itself now refuses to merge at all when
requirements.txt/adapters/meshtastic/requirements.txt changed (a
distinct "blocked" outcome, not ok=True + skip-the-restart as an
earlier pass of this fix had it). The route must surface that as a
non-2xx, blocked JSON response - never scheduling a restart - and
otherwise behave exactly as before.
"""

from functools import wraps
from unittest.mock import patch

import pytest
from flask import Flask

from api.api_updates import register_updates_routes


def _handle_errors(f):
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
    register_updates_routes(app, lambda: "test-version", str(tmp_path), _handle_errors)
    return {"app": app, "client": app.test_client()}


def test_apply_returns_blocked_status_when_requirements_changed(api_env):
    with patch("api.api_updates.update_service.git_preflight", return_value={"ok": True, "upstream": "origin/main"}), \
         patch(
             "api.api_updates.update_service.apply_update",
             return_value={
                 "ok": False, "blocked": True, "reason": "requirements_changed",
                 "previous_sha": "abc123", "requirements_changed": True,
                 "changed_requirements_files": ["requirements.txt"],
                 "output": "", "instructions": "git merge --ff-only origin/main; pip install -r requirements.txt; systemctl restart",
             },
         ), \
         patch("api.api_updates.threading.Thread") as mock_thread:
        response = api_env["client"].post("/api/updates/apply")

    assert response.status_code == 409
    body = response.get_json()
    assert body["ok"] is False
    assert body["blocked"] is True
    assert body["requirements_changed"] is True
    assert body["changed_requirements_files"] == ["requirements.txt"]
    assert "instructions" in body and body["instructions"]
    assert body["restarted"] is False
    mock_thread.assert_not_called()


def test_apply_restarts_normally_when_requirements_unchanged(api_env):
    with patch("api.api_updates.update_service.git_preflight", return_value={"ok": True, "upstream": "origin/main"}), \
         patch(
             "api.api_updates.update_service.apply_update",
             return_value={
                 "ok": True, "blocked": False, "previous_sha": "abc123", "output": "",
                 "requirements_changed": False, "changed_requirements_files": [],
             },
         ), \
         patch("api.api_updates.threading.Thread") as mock_thread:
        response = api_env["client"].post("/api/updates/apply")

    assert response.status_code == 202
    body = response.get_json()
    assert body["ok"] is True
    assert body["blocked"] is False
    assert body["requirements_changed"] is False
    assert body["restarted"] is True
    mock_thread.assert_called_once()
    mock_thread.return_value.start.assert_called_once()


def test_apply_returns_409_when_preflight_not_ok(api_env):
    with patch(
        "api.api_updates.update_service.git_preflight",
        return_value={"ok": False, "reason": "dirty_tree"},
    ), patch("api.api_updates.threading.Thread") as mock_thread:
        response = api_env["client"].post("/api/updates/apply")

    assert response.status_code == 409
    mock_thread.assert_not_called()
