"""CAM2 Phase 1b: /api/camera/stream/* routes and the network driver end to end with the fake ffmpeg."""
import os
import shutil
import sys
import time
from functools import wraps
from unittest.mock import MagicMock

import pytest
from flask import Flask

from conftest import _stub_libcamera
_stub_libcamera()

import api.api_camera as api_camera_module
import camera.camera as camera_module
from api.api_camera import register_camera_routes
from camera import mjpeg_fallback, rtsp_stream as rs
from camera.camera_manager import CameraManager
from camera.rtsp_driver import RtspCameraDriver
from camera.source_store import CameraSourceStore

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_ffmpeg_fmp4.py")
PASSWORD = "S3cret-pw-for-tests"


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:  # pragma: no cover
            return {"ok": False, "error": str(error)}, 500
    return wrapped


def _driver(tmp_path, monkeypatch, behavior="normal", limiter=None):
    monkeypatch.setenv("FAKE_FMP4_BEHAVIOR", behavior)
    store = CameraSourceStore(str(tmp_path), host_validator=lambda h: [h])
    source = store.create({"name": "Cam", "host": "192.168.2.50", "main_path": "m", "sub_path": "s"}, PASSWORD)
    limiter = limiter or rs.ViewerLimiter()
    upstream = rs.RtspUpstream(
        source, lambda: store.get_password(source["id"]), host_validator=lambda h: [h], limiter=limiter,
        cmd_provider=lambda: [sys.executable, FAKE], idle_stop_s=30, stall_after_s=5,
        backoff=rs.Backoff(steps=(0.05, 0.1), jitter=0.0),
    )
    driver = RtspCameraDriver(source, store, host_validator=lambda h: [h], limiter=limiter, upstream=upstream)
    return driver, limiter


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "screenshots").mkdir()
    monkeypatch.setattr(camera_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(camera_module, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    driver, limiter = _driver(tmp_path, monkeypatch)
    manager = CameraManager({driver.id: driver}, active_id=driver.id)
    state = {"manager": manager}
    app = Flask(__name__)
    register_camera_routes(app, camera_module, state, MagicMock(), _handle_errors)
    yield {"client": app.test_client(), "driver": driver, "limiter": limiter, "state": state, "tmp": tmp_path}
    driver.stop()


def _open(client, path="/api/camera/stream/fmp4"):
    return client.get(path, buffered=False)


def test_stream_returns_init_first_then_fragments_with_codec_header(env):
    response = _open(env["client"])
    assert response.status_code == 200 and response.mimetype == "video/mp4"
    assert response.headers["X-Codec"].startswith("avc1.") and response.headers["Cache-Control"] == "no-store"
    iterator = iter(response.response)
    first = next(iterator)
    assert first[4:8] == b"ftyp"
    seen = b""
    deadline = time.monotonic() + 10
    while b"moof" not in seen and time.monotonic() < deadline:
        seen += next(iterator)
    assert b"moof" in seen and b"mdat" in seen or b"moof" in seen
    response.close()
    assert env["limiter"].count() == 0  # slot released on close


def test_an_open_stream_ends_when_the_session_is_no_longer_valid(env, monkeypatch):
    """AUTH2: the stream is authorised once; a logout or a password change ends it at the next look."""
    import meshsrv.session_guard as sg
    monkeypatch.setattr(sg, "DEFAULT_RECHECK_S", 0.05)
    valid = {"ok": True}
    app = Flask(__name__)
    register_camera_routes(app, camera_module, env["state"], MagicMock(), _handle_errors)
    app.extensions["mc_session_guard"] = lambda: (lambda: valid["ok"])
    response = app.test_client().get("/api/camera/stream/fmp4", buffered=False)
    assert response.status_code == 200
    iterator = iter(response.response)
    assert next(iterator)[4:8] == b"ftyp"
    valid["ok"] = False  # the session was revoked
    ended = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            next(iterator)
        except StopIteration:
            ended = True
            break
    assert ended, "the response must end by itself, not wait for the 10 minute cap"
    response.close()
    assert env["limiter"].count() == 0  # and its viewer slot is free


def test_fifth_viewer_of_one_source_is_refused_and_slots_are_released(env):
    responses = [_open(env["client"]) for _ in range(4)]
    assert [r.status_code for r in responses] == [200] * 4
    refused = _open(env["client"])
    assert refused.status_code == 429 and refused.get_json()["error_code"] == "viewer_limit"
    assert env["limiter"].count() == 4
    responses[0].close()
    assert _open(env["client"]).status_code == 200
    for r in responses[1:]:
        r.close()


def test_closing_a_response_that_was_never_read_still_releases_the_slot(env):
    response = _open(env["client"])
    assert env["limiter"].count() == 1
    response.close()
    assert env["limiter"].count() == 0


def test_only_one_ffmpeg_for_many_viewers(env, monkeypatch):
    spawned = []
    original = env["driver"].upstream._spawn
    env["driver"].upstream._spawn = lambda argv: spawned.append(1) or original(argv)
    responses = [_open(env["client"]) for _ in range(3)]
    assert len(spawned) == 1
    for r in responses:
        r.close()


def test_not_ready_reports_the_state_instead_of_hanging(tmp_path, monkeypatch):
    driver, _limiter = _driver(tmp_path, monkeypatch, behavior="hevc")
    app = Flask(__name__)
    register_camera_routes(app, camera_module, {"manager": CameraManager({driver.id: driver}, active_id=driver.id)}, MagicMock(), _handle_errors)
    try:
        response = app.test_client().get("/api/camera/stream/fmp4")
        body = response.get_json()
        assert response.status_code == 503 and body["error_code"] == "stream_not_ready"
        assert body["state"] == "unsupported_codec"
        assert driver.get_status()["codec"] == "h265"
        assert driver.limiter.count() == 0
    finally:
        driver.stop()


def test_auth_failure_is_reported_not_retried(tmp_path, monkeypatch):
    driver, _ = _driver(tmp_path, monkeypatch, behavior="auth401")
    app = Flask(__name__)
    register_camera_routes(app, camera_module, {"manager": CameraManager({driver.id: driver}, active_id=driver.id)}, MagicMock(), _handle_errors)
    try:
        body = app.test_client().get("/api/camera/stream/fmp4").get_json()
        assert body["state"] == "auth_failed"
        assert PASSWORD not in str(body)
    finally:
        driver.stop()


def test_non_ip_active_camera_and_camera_off(env):
    class Hw:
        id, display_name, device_type = "csi", "csi", "camera"
        def stop(self): pass
        def get_status(self): return {"ok": True}
    env["state"]["manager"] = CameraManager({"csi": Hw()}, active_id="csi")
    r = env["client"].get("/api/camera/stream/fmp4")
    assert r.status_code == 404 and r.get_json()["error_code"] == "not_an_ip_source"
    assert env["client"].post("/api/camera/stream/profile", json={"profile": "main"}).status_code == 404
    assert env["client"].post("/api/camera/stream/reconnect").status_code == 404


def test_profile_and_reconnect_routes(env):
    client = env["client"]
    assert client.post("/api/camera/stream/profile", json={"profile": "huge"}).status_code == 400
    assert client.post("/api/camera/stream/profile", data="x", content_type="application/json").status_code == 400
    ok = client.post("/api/camera/stream/profile", json={"profile": "main"})
    assert ok.status_code == 200 and ok.get_json()["status"]["profile"] == "main"
    assert client.post("/api/camera/stream/reconnect").status_code == 200


def test_status_endpoint_carries_stream_details_and_no_secret(env):
    response = _open(env["client"])
    next(iter(response.response))
    status = env["client"].get("/api/camera/status").get_json()
    assert status["source_type"] == "rtsp" and status["stream_kind"] == "fmp4" and status["viewers"] == 1
    assert "state" in status and "resolution" in status and PASSWORD not in str(status)
    response.close()


def test_camera_off_blocks_the_stream(env):
    with_off = env["client"].post("/api/camera/power", json={"enabled": False})
    assert with_off.status_code == 200
    r = env["client"].get("/api/camera/stream/fmp4")
    assert r.status_code == 409 and r.get_json()["error_code"] == "camera_off"


def test_mjpeg_feed_is_refused_on_low_power_hosts(env, monkeypatch):
    monkeypatch.setattr(mjpeg_fallback, "fallback_disabled_reason", lambda: "low_power_host")
    r = env["client"].get("/video_feed")
    assert r.status_code == 503 and r.get_json()["error_code"] == "mjpeg_fallback_disabled"
    assert env["driver"].get_status()["mjpeg_fallback"] == "disabled_low_power"
    assert list(env["driver"].stream_mjpeg()) == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs a real ffmpeg binary")
def test_mjpeg_fallback_transcodes_the_shared_upstream(env, monkeypatch):
    monkeypatch.setattr(mjpeg_fallback, "fallback_disabled_reason", lambda: None)
    frames = env["driver"].stream_mjpeg()
    first = next(frames)
    assert first[:2] == b"\xff\xd8" and first[-2:] == b"\xff\xd9"
    assert env["limiter"].count() == 1
    frames.close()
    env["driver"].stop()
    assert env["limiter"].count() == 0


def test_stopping_the_driver_ends_viewers_and_the_process(env):
    response = _open(env["client"])
    iterator = iter(response.response)
    next(iterator)
    process = env["driver"].upstream._process
    env["driver"].stop()
    assert process.poll() is not None
    with pytest.raises(StopIteration):
        for _ in range(500):
            next(iterator)
    response.close()
    assert env["limiter"].count() == 0


def test_a_viewer_with_no_data_is_ended_and_its_slot_released(env, monkeypatch):
    """A page reload leaves a request nobody is reading; with the camera down nothing is written, so the
    only way to notice is a no-data timeout. Without it the slot (and a server thread) leaks."""
    import camera.rtsp_driver as driver_module
    monkeypatch.setattr(driver_module, "VIEWER_IDLE_S", 0.4)
    driver = env["driver"]
    relay = driver.upstream.relay
    relay.begin(b"INIT")               # an upstream session exists but never produces another fragment
    assert driver.limiter.acquire(driver.id)
    stream = driver_module.Fmp4Stream(driver, relay.open_cursor(), "avc1.640033")
    started = time.monotonic()
    assert list(stream) == []          # ends by itself, no data ever arrived
    assert 0.3 <= time.monotonic() - started < 3
    assert env["limiter"].count() == 0


def test_csi_global_routes_never_touch_the_sensor_while_a_network_camera_is_active(env, monkeypatch):
    """Live test finding: opening the Camera tab POSTs /api/camera/switch_mode, which started Picamera2
    (CPU/RAM on a Pi) although only an IP stream was being shown."""
    calls = []
    for name in ("switch_camera_mode", "start_camera", "stop_camera", "update_camera_settings", "capture_screenshot", "set_video_mode", "api_switch_mode"):
        monkeypatch.setattr(camera_module, name, lambda *a, _n=name, **k: calls.append(_n) or ({"ok": True}, 200))
    client = env["client"]
    ok = client.post("/api/camera/switch_mode", json={"mode": "video"})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True and ok.get_json()["not_applicable"] is True
    for method, url, body in [("post", "/api/camera/settings", {"video": {}}), ("post", "/api/camera/stop", {}),
                              ("post", "/api/camera/mode/photo", {}), ("post", "/api/camera/screenshot", {})]:
        r = getattr(client, method)(url, json=body)
        assert r.status_code == 409 and r.get_json()["error_code"] == "not_available_for_network_camera", url
    assert calls == []


def test_stream_socket_gets_send_timeouts_so_a_dead_viewer_releases_its_slot(env):
    """Live test finding: a viewer whose path died (Send-Q 224 KB, rto 120 s) pinned a slot, a thread and the
    upstream for ~15 minutes. The route now sets SO_SNDTIMEO and TCP_USER_TIMEOUT on the client socket."""
    import socket as socket_module
    sock = MagicMock()
    response = env["client"].get("/api/camera/stream/fmp4", buffered=False, environ_overrides={"gunicorn.socket": sock})
    assert response.status_code == 200
    options = {(call.args[0], call.args[1]) for call in sock.setsockopt.call_args_list}
    assert (socket_module.SOL_SOCKET, socket_module.SO_SNDTIMEO) in options
    if hasattr(socket_module, "TCP_USER_TIMEOUT"):
        assert (socket_module.IPPROTO_TCP, socket_module.TCP_USER_TIMEOUT) in options
        timeout_call = [c for c in sock.setsockopt.call_args_list if c.args[1] == socket_module.TCP_USER_TIMEOUT][0]
        assert timeout_call.args[2] == 30000
    response.close()
    assert env["limiter"].count() == 0


def test_a_stream_ends_after_its_max_age_so_the_next_request_is_authorised_again(env, monkeypatch):
    """Logout cannot reach an open response (signed-cookie login, no server session): bound its lifetime."""
    import camera.rtsp_driver as driver_module
    assert driver_module.STREAM_MAX_AGE_S == 600.0
    monkeypatch.setattr(driver_module, "STREAM_MAX_AGE_S", 0.5)
    stream = env["driver"].open_fmp4()
    started = time.monotonic()
    received = 0
    for chunk in stream:
        received += len(chunk)
        assert time.monotonic() - started < 10
    assert received > 0 and 0.4 < time.monotonic() - started < 5
    assert env["limiter"].count() == 0
