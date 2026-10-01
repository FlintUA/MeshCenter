"""Tests for api/api_settings.py's POST /api/settings (F4.1 PR 2).

F4.0 finding: api_update_settings() mutated the live `settings` dict and
called save_settings() with its result completely ignored - a failed write
(full disk, read-only remount) still answered ok:true, while the file on
disk stayed unchanged. Fixed with persist-then-commit: save_settings(new_settings)
writes the new state first and only commits it into the live `settings` dict
on success.

Standalone Flask app + register_settings_routes() directly, not the full
server_module singleton - this project's established pattern for testing an
api/*.py route module in isolation (see tests/test_api_meshtastic.py).
"""

import threading

import pytest
from flask import Flask

from api.api_settings import register_settings_routes


def _make_app(initial_settings=None, save_settings_impl=None):
    app = Flask(__name__)
    state_lock = threading.RLock()
    settings = dict(initial_settings or {})

    if save_settings_impl is None:
        def save_settings_impl(new_state=None):
            if new_state is not None:
                settings.clear()
                settings.update(new_state)
            return True

    register_settings_routes(app, state_lock, settings, save_settings_impl, lambda f: f)
    return app, settings


def test_post_settings_success_commits_the_new_state():
    app, settings = _make_app({"language": "en"})
    client = app.test_client()

    resp = client.post("/api/settings", json={"settings": {"language": "de"}})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert settings["language"] == "de"


def test_post_settings_failed_write_returns_storage_error_and_does_not_commit():
    original = {"language": "en"}
    app, settings = _make_app(dict(original), save_settings_impl=lambda new_state=None: False)
    client = app.test_client()

    resp = client.post("/api/settings", json={"settings": {"language": "de"}})

    assert resp.status_code == 500
    assert resp.get_json()["error_code"] == "storage_write_failed"
    assert settings == original, "a failed write must leave the live settings dict completely untouched"


def test_post_settings_failed_write_then_a_working_write_still_succeeds():
    """Not a permanent lockout - once storage recovers, a later save works
    normally (proves the failure path leaves no stale partial state that
    would trip up a subsequent, successful attempt)."""
    state = {"fail": True}

    def flaky_save(new_state=None):
        if state["fail"]:
            return False
        if new_state is not None:
            settings.clear()
            settings.update(new_state)
        return True

    app, settings = _make_app({"language": "en"}, save_settings_impl=flaky_save)
    client = app.test_client()

    resp = client.post("/api/settings", json={"settings": {"language": "de"}})
    assert resp.status_code == 500
    assert settings["language"] == "en"

    state["fail"] = False
    resp = client.post("/api/settings", json={"settings": {"language": "de"}})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert settings["language"] == "de"
