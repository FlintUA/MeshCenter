"""Tests for api/api_waypoints.py's register_waypoint_routes() - the first
use of Flask's test_client() in this project's test suite (rather than
calling functions directly). register_waypoint_routes(app, ...) is exactly
the interface worth testing as a whole here: routing + JSON serialization +
the dependency-injection wiring itself, not just the underlying logic.

No server.py import needed - this module only depends on storage/
waypoint_store.py (a real, self-contained SQLite store) and stub
implementations of everything else register_waypoint_routes() takes as a
parameter. Radio-dependent stubs (is_radio_available, prepare_radio_command,
add_message, log_system_event, channel_chat_id) are simple fakes; the actual
radio-send subprocess path (api_waypoint_send()'s success branch, which
shells out to storage/waypoint_sender.py) is deliberately NOT exercised here
- that's a live-radio operation outside this test infrastructure's scope,
same limitation documented in CLAUDE.md for the rest of the project. Only
api_waypoint_send()'s validation branches (everything that returns before
reaching the subprocess call) are tested.
"""

import threading

import pytest
from flask import Flask

from api.api_waypoints import register_waypoint_routes
from storage.waypoint_store import WaypointStore


class _RadioStub:
    """Mutable is_radio_available()/prepare_radio_command() stand-in so
    individual tests can flip availability without rebuilding the app."""

    def __init__(self):
        self.available = True
        self.prepare_ok = True

    def is_radio_available(self):
        return self.available

    def prepare_radio_command(self, device=None, timeout=8):
        return self.prepare_ok


@pytest.fixture
def waypoint_env(tmp_path):
    store = WaypointStore(str(tmp_path / "waypoints.db"))
    radio = _RadioStub()
    add_message_calls = []
    log_event_calls = []

    def get_node_name(node_id):
        return f"Node {node_id}"

    def handle_errors(f):
        # Minimal stand-in for server.py's real decorator (try/except ->
        # 500 JSON) - register_waypoint_routes() always wraps routes with
        # this, so a callable of the right shape is required either way.
        from functools import wraps

        @wraps(f)
        def wrapped(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as error:
                return {"ok": False, "error": str(error)}, 500

        return wrapped

    def add_message(*args, **kwargs):
        add_message_calls.append((args, kwargs))

    def log_system_event(**kwargs):
        log_event_calls.append(kwargs)

    def channel_chat_id(index):
        return "channel" if index == 0 else f"channel:{index}"

    app = Flask(__name__)
    register_waypoint_routes(
        app,
        store,
        get_node_name,
        handle_errors,
        radio.is_radio_available,
        radio.prepare_radio_command,
        threading.RLock(),
        threading.Event(),
        add_message,
        log_system_event,
        channel_chat_id,
        "/dev/ttyACM0",
        "/usr/bin/meshtastic",
        str(tmp_path),
        "!aabbccdd",
        "Test Local Node",
        "LongFast",
    )

    return {
        "app": app,
        "client": app.test_client(),
        "store": store,
        "radio": radio,
        "add_message_calls": add_message_calls,
        "log_event_calls": log_event_calls,
    }


def _seed_waypoint(store, waypoint_id=1, **overrides):
    payload = {
        "waypoint_id": waypoint_id,
        "sender_id": "!820af75a",
        "name": "Trailhead",
        "description": "Parking lot",
        "latitude": 52.5,
        "longitude": 13.4,
        "icon": 128205,
        "expire_at": 0,
        "channel_index": 0,
        "raw_packet": '{"source": "test"}',
    }
    payload.update(overrides)
    return store.upsert(payload)


# ---------------- GET /api/waypoints ----------------

def test_list_waypoints_empty(waypoint_env):
    response = waypoint_env["client"].get("/api/waypoints")
    assert response.status_code == 200
    body = response.get_json()
    assert body == {"ok": True, "waypoints": [], "total": 0}


def test_list_waypoints_resolves_sender_name_and_strips_raw_packet(waypoint_env):
    _seed_waypoint(waypoint_env["store"])

    response = waypoint_env["client"].get("/api/waypoints")
    body = response.get_json()

    assert body["ok"] is True
    assert body["total"] == 1
    waypoint = body["waypoints"][0]
    assert waypoint["sender_name"] == "Node !820af75a"
    assert "raw_packet" not in waypoint


def test_list_waypoints_include_raw(waypoint_env):
    _seed_waypoint(waypoint_env["store"])

    response = waypoint_env["client"].get("/api/waypoints?include_raw=1")
    waypoint = response.get_json()["waypoints"][0]
    assert "raw_packet" in waypoint


# ---------------- GET /api/waypoints/<id> ----------------

def test_get_waypoint_detail(waypoint_env):
    _seed_waypoint(waypoint_env["store"])

    response = waypoint_env["client"].get("/api/waypoints/1")
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["waypoint"]["name"] == "Trailhead"


def test_get_waypoint_detail_not_found(waypoint_env):
    response = waypoint_env["client"].get("/api/waypoints/99999")
    assert response.status_code == 404
    assert response.get_json()["error_code"] == "waypoint_not_found"


# ---------------- POST /api/waypoints/<id>/hidden ----------------

def test_hide_waypoint_excludes_it_from_default_listing(waypoint_env):
    _seed_waypoint(waypoint_env["store"])

    response = waypoint_env["client"].post("/api/waypoints/1/hidden", json={"hidden": True})
    assert response.status_code == 200
    assert response.get_json()["waypoint"]["is_hidden"] is True

    default_list = waypoint_env["client"].get("/api/waypoints").get_json()
    assert default_list["total"] == 0

    with_hidden = waypoint_env["client"].get("/api/waypoints?include_hidden=1").get_json()
    assert with_hidden["total"] == 1


def test_hide_waypoint_not_found(waypoint_env):
    response = waypoint_env["client"].post("/api/waypoints/99999/hidden", json={"hidden": True})
    assert response.status_code == 404


# ---------------- DELETE /api/waypoints/<id> ----------------

def test_delete_waypoint(waypoint_env):
    _seed_waypoint(waypoint_env["store"])

    response = waypoint_env["client"].delete("/api/waypoints/1")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "deleted": 1, "waypoint_id": 1}

    # Second delete of the same id is now a 404, not a silent no-op success.
    response = waypoint_env["client"].delete("/api/waypoints/1")
    assert response.status_code == 404


# ---------------- POST /api/waypoints/delete (bulk) ----------------

def test_delete_many_waypoints(waypoint_env):
    _seed_waypoint(waypoint_env["store"], waypoint_id=1)
    _seed_waypoint(waypoint_env["store"], waypoint_id=2)
    _seed_waypoint(waypoint_env["store"], waypoint_id=3)

    response = waypoint_env["client"].post("/api/waypoints/delete", json={"waypoint_ids": [1, 2]})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "deleted": 2}

    remaining = waypoint_env["client"].get("/api/waypoints?include_hidden=1").get_json()
    assert remaining["total"] == 1


def test_delete_many_rejects_non_list_payload(waypoint_env):
    response = waypoint_env["client"].post("/api/waypoints/delete", json={"waypoint_ids": "not-a-list"})
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "waypoint_ids_not_a_list"


# ---------------- DELETE /api/waypoints (all) ----------------

def test_delete_all_waypoints(waypoint_env):
    _seed_waypoint(waypoint_env["store"], waypoint_id=1)
    _seed_waypoint(waypoint_env["store"], waypoint_id=2)

    response = waypoint_env["client"].delete("/api/waypoints")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "deleted": 2}

    assert waypoint_env["client"].get("/api/waypoints").get_json()["total"] == 0


# ---------------- POST /api/waypoints/send - validation branches only ----------------
# The success path (real subprocess to storage/waypoint_sender.py) is
# deliberately not exercised - see module docstring.

def _valid_send_payload(**overrides):
    payload = {
        "name": "New Waypoint",
        "description": "",
        "latitude": 52.5,
        "longitude": 13.4,
        "channel_index": 0,
        "expire_at": 9999999999,  # far future
        "post_notification": False,
    }
    payload.update(overrides)
    return payload


def test_send_rejects_missing_name(waypoint_env):
    response = waypoint_env["client"].post("/api/waypoints/send", json=_valid_send_payload(name=""))
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "waypoint_invalid_name"


def test_send_rejects_name_too_long(waypoint_env):
    response = waypoint_env["client"].post("/api/waypoints/send", json=_valid_send_payload(name="x" * 31))
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "waypoint_invalid_name"


def test_send_rejects_description_too_long(waypoint_env):
    response = waypoint_env["client"].post(
        "/api/waypoints/send", json=_valid_send_payload(description="x" * 101),
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "waypoint_invalid_description"


def test_send_rejects_invalid_coordinates(waypoint_env):
    response = waypoint_env["client"].post(
        "/api/waypoints/send", json=_valid_send_payload(latitude=999),
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "waypoint_invalid_coordinates"


def test_send_rejects_out_of_range_channel_index(waypoint_env):
    response = waypoint_env["client"].post(
        "/api/waypoints/send", json=_valid_send_payload(channel_index=8),
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "waypoint_invalid_channel_index"


def test_send_rejects_expiration_in_the_past(waypoint_env):
    response = waypoint_env["client"].post(
        "/api/waypoints/send", json=_valid_send_payload(expire_at=1),
    )
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "waypoint_expiration_in_past"


def test_send_returns_503_when_radio_unavailable(waypoint_env):
    waypoint_env["radio"].available = False

    response = waypoint_env["client"].post("/api/waypoints/send", json=_valid_send_payload())

    assert response.status_code == 503
    assert response.get_json()["error_code"] == "radio_released"
    # Confirms validation passed and this really is the radio-availability
    # check that rejected it, not an earlier 400.
    assert not waypoint_env["add_message_calls"]
