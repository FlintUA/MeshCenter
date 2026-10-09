"""H3: global security headers, initial_password.txt startup warning, and the
reimplemented fixes from the stale Sentinel PR triage (Wi-Fi SSID validation,
JSON-body type checks, top-processes error leakage, redirect control chars,
screenshot directory deletion)."""
import sys
import types

import pytest
from flask import Flask, Response
from werkzeug.security import generate_password_hash

from conftest import _stub_libcamera
_stub_libcamera()

if "config" not in sys.modules:
    _fake_config = types.ModuleType("config")
    _fake_config.DATA_DIR = "/tmp/meshcenter_test_data"
    sys.modules["config"] = _fake_config

import api.api_system as api_system_module
from api.api_auth import _is_safe_redirect_target
from api.api_system import register_system_routes
from api.security_headers import register_security_headers
from meshsrv import network_config
from meshsrv.initial_password_check import MSG_OBSOLETE, MSG_VALID, warn_if_initial_password_file


# ---------------------------------------------------------------- headers

@pytest.fixture
def header_client():
    app = Flask(__name__)
    register_security_headers(app)

    @app.route("/plain")
    def plain():
        return "ok"

    @app.route("/own")
    def own():
        resp = Response("x")
        resp.headers["Content-Security-Policy"] = "sandbox"
        resp.headers["X-Content-Type-Options"] = "custom"
        return resp

    @app.route("/stream")
    def stream():
        def gen():
            yield b"--frame\r\n"
            yield b"data\r\n"
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

    return app.test_client()


def test_headers_added_to_every_response(header_client):
    h = header_client.get("/plain").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "SAMEORIGIN"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" not in h


def test_headers_do_not_overwrite_route_values(header_client):
    h = header_client.get("/own").headers
    assert h["Content-Security-Policy"] == "sandbox"
    assert h["X-Content-Type-Options"] == "custom"
    assert h["X-Frame-Options"] == "SAMEORIGIN"


def test_headers_leave_streaming_body_intact(header_client):
    resp = header_client.get("/stream")
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert resp.mimetype == "multipart/x-mixed-replace"
    assert resp.data == b"--frame\r\ndata\r\n"


# ------------------------------------------------- initial_password.txt

SECRET = "hunter2-hunter2"


def _run(tmp_path, auth_state):
    events, notes, logs = [], [], []
    result = warn_if_initial_password_file(
        str(tmp_path), auth_state,
        lambda *a, **k: events.append((a, k)),
        lambda *a, **k: notes.append((a, k)),
        log=lambda msg, **k: logs.append(msg),
    )
    return result, events, notes, logs


def test_no_file_no_warning(tmp_path):
    result, events, notes, logs = _run(tmp_path, {"password_hash": generate_password_hash(SECRET)})
    assert result is None and not events and not notes and not logs


def test_file_matching_current_password_warns_valid(tmp_path):
    (tmp_path / "initial_password.txt").write_text(SECRET + "\n")
    result, events, notes, logs = _run(tmp_path, {"password_hash": generate_password_hash(SECRET)})
    assert result == "valid"
    assert len(events) == 1 and len(notes) == 1 and len(logs) == 1
    assert events[0][0][1] == "WARNING"
    assert MSG_VALID in events[0][0] and notes[0][0][0] == "warning" and MSG_VALID in notes[0][0]
    assert "Settings" in MSG_VALID and "initial_password.txt" in MSG_VALID
    assert (tmp_path / "initial_password.txt").exists()  # never auto-deleted
    for blob in (events, notes, logs):
        assert SECRET not in repr(blob)


def test_file_with_label_still_matches(tmp_path):
    (tmp_path / "initial_password.txt").write_text(f"Initial password: {SECRET}\n")
    result, *_ = _run(tmp_path, {"password_hash": generate_password_hash(SECRET)})
    assert result == "valid"


def test_file_not_matching_warns_obsolete(tmp_path):
    (tmp_path / "initial_password.txt").write_text(SECRET)
    result, events, notes, logs = _run(tmp_path, {"password_hash": generate_password_hash("a-new-password-1")})
    assert result == "obsolete"
    assert MSG_OBSOLETE in events[0][0] and MSG_OBSOLETE in notes[0][0]
    assert SECRET not in repr((events, notes, logs))
    assert (tmp_path / "initial_password.txt").exists()


def test_no_password_hash_counts_as_obsolete(tmp_path):
    (tmp_path / "initial_password.txt").write_text(SECRET)
    result, *_ = _run(tmp_path, {"password_hash": ""})
    assert result == "obsolete"


# ----------------------------------------------------- system routes

@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    register_system_routes(app)
    monkeypatch.setattr(api_system_module, "log_system_event", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(network_config, "connect", lambda ssid, pw="": calls.append(("connect", ssid)) or {"ok": True})
    monkeypatch.setattr(network_config, "forget", lambda ssid: calls.append(("forget", ssid)) or {"ok": True})
    c = app.test_client()
    c.calls = calls
    return c


@pytest.mark.parametrize("ssid", ["-oProxy", "a\nb", "a\x00b", "x" * 33, "é" * 17, "   "])
@pytest.mark.parametrize("route", ["connect", "forget"])
def test_wifi_rejects_bad_ssid(client, route, ssid):
    r = client.post(f"/api/system/wifi/{route}", json={"ssid": ssid, "password": "pw"})
    assert r.status_code == 400
    assert client.calls == []


@pytest.mark.parametrize("route", ["connect", "forget"])
def test_wifi_accepts_good_ssid(client, route):
    r = client.post(f"/api/system/wifi/{route}", json={"ssid": "Home WiFi-5G", "password": "pw"})
    assert r.status_code == 200
    assert client.calls == [(route, "Home WiFi-5G")]


@pytest.mark.parametrize("path", ["/api/system/wifi/connect", "/api/system/wifi/forget", "/api/schedules", "/api/timers"])
@pytest.mark.parametrize("body", ["[1,2]", "not json", '"str"'])
def test_non_object_json_body_is_400_not_500(client, path, body):
    r = client.post(path, data=body, content_type="application/json")
    assert r.status_code == 400


def test_top_processes_error_does_not_leak_detail(client, monkeypatch):
    import psutil

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /home/pi")
    monkeypatch.setattr(psutil, "process_iter", boom)
    r = client.get("/api/system/top-processes")
    assert r.status_code == 500
    assert "secret" not in r.get_data(as_text=True)


# ------------------------------------------------------- other fixes

def test_redirect_target_rejects_control_characters():
    assert _is_safe_redirect_target("/ok") is True
    for bad in ("/a\x01b", "/a\nb", "/\t/evil.example", "/a\x7fb"):
        assert _is_safe_redirect_target(bad) is False


def test_delete_screenshot_refuses_directories(tmp_path, monkeypatch):
    import camera.camera as cam

    monkeypatch.setattr(cam, "SCREENSHOTS_DIR", str(tmp_path))
    (tmp_path / "2026-10-01").mkdir()
    (tmp_path / "2026-10-01" / "a.jpg").write_bytes(b"x")
    body, status = cam.delete_screenshot("2026-10-01")
    assert status == 404
    assert (tmp_path / "2026-10-01" / "a.jpg").exists()
    body, status = cam.delete_screenshot("2026-10-01/a.jpg")
    assert status == 200
    assert not (tmp_path / "2026-10-01" / "a.jpg").exists()
