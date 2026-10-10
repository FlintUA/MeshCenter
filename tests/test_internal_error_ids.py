"""Unexpected exceptions never reach the client as str(e) / a traceback; they
get a generic body plus an error_id, and the cause is logged under that id.
Domain errors keep their own message and code."""
import re

import pytest
from flask import Flask, request

from meshsrv import error_reporting
from meshsrv.radio_transport import TransportError, TransportErrorCode

SECRET = "secret-internal /var/secrets/key.txt hunter2"


@pytest.fixture
def client(server_module, monkeypatch):
    events = []
    monkeypatch.setattr(server_module, "log_system_event",
                        lambda title, level="INFO", details="", source="system": events.append((title, level, details, source)))
    app = Flask(__name__)
    app.debug = True  # the old code returned a traceback in debug mode

    def route(name, fn):
        app.add_url_rule(f"/{name}", name, server_module.handle_errors(fn), methods=["GET", "POST"])

    def boom():
        raise RuntimeError(SECRET)

    def user_facing():
        raise error_reporting.UserFacingError("Name is too long", status=422, error_code="name_too_long")

    def busy():
        raise server_module.RadioBusyError("Serial port busy: /dev/ttyACM0")

    def transport():
        raise TransportError(TransportErrorCode.NOT_CONNECTED, "radio is not connected")

    def bad_json():
        return {"ok": True, "got": request.get_json(force=True)}

    def explicit_domain():
        return {"ok": False, "error": "kept", "error_code": "storage_write_failed"}, 500

    for name, fn in [("boom", boom), ("user", user_facing), ("busy", busy), ("transport", transport),
                     ("badjson", bad_json), ("explicit", explicit_domain)]:
        route(name, fn)
    c = app.test_client()
    c.events = events
    return c


def test_unexpected_exception_is_generic_with_error_id_even_in_debug(client):
    res = client.get("/boom")
    assert res.status_code == 500
    body = res.get_json()
    assert body["ok"] is False
    assert body["error"] == "Internal server error"
    assert body["error_code"] == "internal_error"
    assert re.fullmatch(r"[0-9a-f]{8}", body["error_id"])
    assert "traceback" not in body
    raw = res.get_data(as_text=True)
    for leaked in ("secret", "hunter2", "/var/secrets", "RuntimeError", "Traceback", "boom"):
        assert leaked not in raw


def test_cause_is_logged_under_the_same_error_id(client, capsys):
    body = client.get("/boom").get_json()
    error_id = body["error_id"]
    out = capsys.readouterr().out
    assert f"[ERROR {error_id}]" in out
    assert "Traceback (most recent call last)" in out and "RuntimeError" in out
    # exactly one System Log ERROR entry, tagged with the id
    entries = [e for e in client.events if e[1] == "ERROR"]
    assert len(entries) == 1
    assert error_id in entries[0][2] and "RuntimeError" in entries[0][2]


def test_two_failures_get_different_ids(client):
    assert client.get("/boom").get_json()["error_id"] != client.get("/boom").get_json()["error_id"]


def test_request_bodies_are_never_logged(client, capsys):
    client.post("/boom", data="password=hunter2-body-secret", content_type="text/plain")
    out = capsys.readouterr().out
    assert "hunter2-body-secret" not in out
    assert all("hunter2-body-secret" not in str(e) for e in client.events)


def test_user_facing_error_keeps_message_status_and_code(client):
    res = client.get("/user")
    assert res.status_code == 422
    assert res.get_json() == {"ok": False, "error": "Name is too long", "error_code": "name_too_long"}


def test_radio_busy_and_transport_errors_keep_their_messages_and_codes(client):
    busy = client.get("/busy")
    assert busy.status_code == 503
    assert busy.get_json()["error"] == "Serial port busy: /dev/ttyACM0"
    assert busy.get_json()["error_code"] == "radio_busy"
    tr = client.get("/transport")
    assert tr.status_code == 503
    assert tr.get_json()["error"] == "radio is not connected"
    assert tr.get_json()["error_code"] == TransportErrorCode.NOT_CONNECTED.value


def test_malformed_json_is_a_400_not_a_500(client):
    res = client.post("/badjson", data="{not json", content_type="application/json")
    assert res.status_code == 400
    assert res.get_json()["error_code"] == "http_error"
    assert "Traceback" not in res.get_data(as_text=True)


def test_explicit_domain_responses_are_untouched(client):
    res = client.get("/explicit")
    assert res.status_code == 500
    assert res.get_json() == {"ok": False, "error": "kept", "error_code": "storage_write_failed"}


def test_report_unexpected_error_survives_a_broken_logger():
    def broken(*a, **k):
        raise OSError("disk full")
    error_id = error_reporting.report_unexpected_error(ValueError("x"), "t", log_event=broken, log_print=lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert re.fullmatch(r"[0-9a-f]{8}", error_id)


# ---- the other places that used to return str(e) -----------------------------

def test_camera_helpers_do_not_leak_exceptions(monkeypatch):
    from camera import camera as cam

    def explode():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(cam, "iter_screenshot_files", explode)
    body, status = cam.list_screenshots()
    assert status == 500
    assert body["error"] == "Internal server error" and body["error_code"] == "internal_error"
    assert "error_id" in body and "secret" not in str(body)


def test_devices_route_hides_camera_status_exceptions(server_module, monkeypatch):
    def explode():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(server_module.camera, "get_camera_status", explode)
    with server_module.app.test_request_context("/api/devices"):
        res = server_module.app.view_functions["api_profile_devices"]()
    raw = res.get_data(as_text=True) if hasattr(res, "get_data") else str(res)
    assert "secret" not in raw and "hunter2" not in raw


def test_profile_save_error_is_generic(server_module, monkeypatch):
    def explode(*a, **k):
        raise OSError(SECRET)

    monkeypatch.setattr(server_module.instance_manager, "save", explode)
    with server_module.app.app_context():
        resp = server_module.internal_error_response(OSError(SECRET), "save_active_radio_profile")
    body, status = resp[0].get_json(), resp[1]
    assert status == 500 and body["error_code"] == "internal_error" and "secret" not in str(body)


def test_activate_unknown_profile_is_404_without_the_server_path(server_module, monkeypatch):
    path = "/home/meshcenter/meshcenter/data/profiles/deadbeef"
    events = []
    monkeypatch.setattr(server_module, "log_system_event",
                        lambda title, level="INFO", details="", source="system": events.append((title, level, details)))

    def missing(profile_id):
        raise FileNotFoundError(f"Radio profile not found: {path}")

    monkeypatch.setattr(server_module.profile_manager, "get_profile", missing)
    with server_module.app.test_request_context("/api/node-manager/profiles/deadbeef/activate", method="POST"):
        resp = server_module.api_activate_radio_profile("deadbeef")
    body, status = resp[0].get_json(), resp[1]
    assert status == 404
    assert body == {"ok": False, "error": "Profile not found", "error_code": "profile_not_found"}
    assert path not in str(body)
    # the path is kept server-side
    assert any(path in details for _, _, details in events)
