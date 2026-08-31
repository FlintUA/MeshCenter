"""Tests for GET /api/instance (server.py's api_instance_identity()) - Task D
of the installation-ID rollout, which added the "installation" key (schema
v2's id/assigned_at/time_source/assignment_reason, from Tasks A-C) to an
otherwise pre-existing response. No prior test in this repo exercises a
server.py-defined route end-to-end via the real Flask app, so this calls
the view function directly inside an app context - exercises the real
route logic without needing the HTTP layer (auth gating is confirmed
separately by reading api/api_auth.py's before_request hook, not
re-tested here). instance_manager/RADIO_IDENTITY_RESULT are snapshotted
and restored around each test since server_module is session-scoped and
the existing autouse _reset_server_state fixture doesn't cover either.
"""

import pytest


@pytest.fixture
def _preserve_instance_state(server_module):
    original_identity = server_module.instance_manager.get()
    original_radio_result = dict(server_module.RADIO_IDENTITY_RESULT)
    yield
    server_module.instance_manager.save(original_identity)
    server_module.RADIO_IDENTITY_RESULT = original_radio_result


def _call_api_instance(server_module):
    with server_module.app.app_context():
        response = server_module.api_instance_identity()
    return response.get_json()


def test_installation_key_reflects_a_resolved_system_ntp_assignment(server_module, _preserve_instance_state):
    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["installation"] = dict(identity["installation"])
    updated["installation"]["assigned_at"] = "2026-09-01T07:00:00+00:00"
    updated["installation"]["time_source"] = "system_ntp"
    server_module.instance_manager.save(updated)

    data = _call_api_instance(server_module)
    assert data["installation"]["id"] == identity["installation"]["id"]
    assert data["installation"]["assigned_at"] == "2026-09-01T07:00:00+00:00"
    assert data["installation"]["time_source"] == "system_ntp"
    assert data["installation"]["assignment_reason"] == identity["installation"]["assignment_reason"]


def test_installation_key_reflects_a_still_pending_assignment(server_module, _preserve_instance_state):
    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["installation"] = dict(identity["installation"])
    updated["installation"]["assigned_at"] = None
    updated["installation"]["time_source"] = "pending"
    server_module.instance_manager.save(updated)

    data = _call_api_instance(server_module)
    assert data["installation"]["assigned_at"] is None
    assert data["installation"]["time_source"] == "pending"
    assert data["installation"]["id"] == identity["installation"]["id"]


def test_pre_existing_fields_are_unchanged(server_module, _preserve_instance_state):
    # Regression guard: this is a pure addition - every field that existed
    # before Task D must keep deriving from exactly the same source, and no
    # key may have been silently dropped or renamed.
    identity = server_module.instance_manager.get()
    result = dict(server_module.RADIO_IDENTITY_RESULT)

    data = _call_api_instance(server_module)

    assert data["ok"] is True
    assert data["instance_name"] == identity.get("instance_name", "MeshCenter")
    assert data["hostname"] == identity.get("hostname", "")
    assert data["active_profile_id"] == identity.get("active_profile_id", "")
    assert data["profile_path"] == server_module.PROFILE_DATA_DIR
    assert data["configured"] == dict(identity.get("radio", {}))
    assert data["detected"] == dict(result.get("detected") or identity.get("runtime", {}).get("last_detected_radio", {}))
    assert data["status"] == (result.get("status") or identity.get("runtime", {}).get("identity_status", "NOT_CHECKED"))
    assert data["checked_at"] == (result.get("checked_at") or identity.get("runtime", {}).get("last_detected_at"))
    assert data["error"] == (result.get("error") or identity.get("runtime", {}).get("last_error"))
    assert set(data.keys()) == {
        "ok", "instance_name", "hostname", "active_profile_id", "profile_path",
        "configured", "detected", "status", "checked_at", "error", "installation",
    }
