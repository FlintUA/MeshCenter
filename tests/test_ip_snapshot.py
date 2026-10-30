"""CAM2 Phase 3a: a network camera's picture into the Media gallery (camera/ip_snapshot.py, camera/frame_grab.py)."""
import errno
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from flask import Flask

from conftest import _stub_libcamera
_stub_libcamera()

import camera.camera as camera_module
import camera.ip_snapshot as ip_snapshot_module
from api.api_camera import register_camera_routes
from camera import fmp4, frame_grab, rtsp_stream
from camera.camera_manager import CameraManager
from camera.ip_snapshot import IpSnapshotService, jpeg_size, slug
from camera.reolink_control import ReolinkControlManager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
from fake_reolink_server import FakeReolink  # noqa: E402
from test_api_camera_stream import _driver, _handle_errors  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ADMIN_PW = "pw-admin-test"
GUEST_PW = "pw-guest-test"
REAL_FFMPEG = shutil.which("ffmpeg")


def _lan(host):
    return [host]


def _jpeg_fixture():
    with open(os.path.join(FIXTURES, "tiny_valid_frame.jpg"), "rb") as handle:
        return handle.read()


def _fill_relay(relay, fragments_after_keyframe=2):
    """The init segment and keyframe fragments of a real (tiny) H.264 stream, as the running upstream would have pushed."""
    splitter = fmp4.Fmp4Splitter()
    with open(os.path.join(FIXTURES, "tiny_h264_frag.mp4"), "rb") as handle:
        items = splitter.feed(handle.read())
    for kind, item in items:
        if kind == "init":
            relay.begin(item)
        else:
            relay.push(item)


@pytest.fixture
def fake():
    server = FakeReolink().start()
    yield server
    server.stop()


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.wall = datetime(2026, 10, 25, 9, 30, 0)

    def mono(self):
        return self.now

    def datetime(self):
        return self.wall

    def advance(self, seconds):
        self.now += seconds
        self.wall += timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path, monkeypatch, fake):
    gallery = tmp_path / "screenshots"
    gallery.mkdir()
    monkeypatch.setattr(camera_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(camera_module, "SCREENSHOTS_DIR", str(gallery))
    monkeypatch.setattr(camera_module, "start_thumbnail_backfill", lambda missing: False)  # a background reader would lock files on Windows
    driver, limiter = _driver(tmp_path, monkeypatch)
    store = driver._store
    store.update(driver.id, {"host": "127.0.0.1", "username": "admin",
                             "control": {"type": "reolink", "scheme": "http", "port": fake.port, "allow_http": True}}, ADMIN_PW)
    manager = CameraManager({driver.id: driver}, active_id=driver.id)
    state = {"manager": manager}
    control = ReolinkControlManager(store, host_validator=_lan)
    clock = Clock()
    grabbed = []

    def grabber(relay, **_kw):
        data = relay.snapshot_input()
        grabbed.append(data)
        if data is None:
            raise frame_grab.GrabError("preview_not_running", "Start the preview first.")
        return _jpeg_fixture()

    service = IpSnapshotService(store, control, camera_module, clock=clock.mono, wall=clock.datetime, grabber=grabber,
                                free_bytes=lambda _path: 10 * 1024 ** 3)
    # nothing may start the upstream: a picture must never wake or open a camera session
    driver.upstream.ensure_running = MagicMock(side_effect=AssertionError("the upstream must not be started for a picture"))
    app = Flask(__name__)
    register_camera_routes(app, camera_module, state, MagicMock(), _handle_errors, ip_snapshot=service.take)
    yield {"client": app.test_client(), "driver": driver, "store": store, "fake": fake, "clock": clock, "gallery": gallery,
           "service": service, "id": driver.id, "grabbed": grabbed, "state": state, "control": control, "app": app}
    driver.stop()


def _shoot(env):
    return env["client"].post("/api/camera/screenshot")


def _files(env):
    return sorted(str(p.relative_to(env["gallery"])).replace("\\", "/") for p in env["gallery"].rglob("*") if p.is_file())


# ---------------------------------------------------------------- the camera's own snapshot API

def test_the_default_is_the_full_resolution_picture_with_a_gallery_name_and_metadata(env):
    response = _shoot(env)
    body = response.get_json()
    assert response.status_code == 200 and body["ok"] is True and body["method"] == "camera_api_full"
    assert env["fake"].snap_types == [""]  # no snapType: the full picture
    assert body["display_name"].startswith("MC_") and "_2026-10-25_09-30-00.jpg" in body["display_name"]
    files = _files(env)
    jpg = next(f for f in files if f.endswith(".jpg"))
    assert files == sorted([jpg, jpg + ".json"]) and jpg == body["filename"]
    meta = json.loads((env["gallery"] / (jpg + ".json")).read_text(encoding="utf-8"))
    assert meta["source_id"] == env["id"] and meta["method"] == "camera_api_full" and meta["profile"] == "full"
    assert meta["taken_at"] == "2026-10-25T09:30:00" and meta["source_name"] == "Cam"
    assert "width" in meta and "height" in meta and body["resolution"] == f"{meta['width']}x{meta['height']}"
    assert (env["gallery"] / jpg).read_bytes()[:3] == b"\xff\xd8\xff"


def test_the_small_quality_setting_asks_for_the_sub_picture(env):
    env["store"].update(env["id"], {"control": {"snapshot_quality": "small"}})
    body = _shoot(env).get_json()
    assert body["method"] == "camera_api_small" and body["profile"] == "small"
    assert env["fake"].snap_types == ["sub"]


def test_the_gallery_listing_carries_the_metadata_and_a_delete_removes_the_sidecar_too(env):
    body = _shoot(env).get_json()
    listing = env["client"].get("/api/camera/screenshots").get_json()
    entry = next(item for item in listing["screenshots"] if item["filename"] == body["filename"])
    assert entry["meta"]["source_name"] == "Cam" and entry["meta"]["method"] == "camera_api_full"
    assert env["client"].delete(f"/api/camera/screenshot/{body['filename']}").status_code == 200
    assert _files(env) == []


def test_delete_all_and_the_gallery_cleanup_remove_the_metadata_files(env):
    _shoot(env)
    env["clock"].advance(3)
    _shoot(env)
    assert len([f for f in _files(env) if f.endswith(".json")]) == 2
    camera_module.cleanup_old_screenshots(max_mb=0, keep_days=30)  # over the size limit: everything goes
    assert _files(env) == []
    env["clock"].advance(3)
    assert _shoot(env).status_code == 200
    assert env["client"].delete("/api/camera/screenshots").get_json()["deleted_count"] == 1
    assert _files(env) == []


def test_an_old_picture_is_removed_with_its_metadata_by_the_age_limit(env):
    body = _shoot(env).get_json()
    path = env["gallery"] / body["filename"]
    old = 1_000_000_000
    os.utime(path, (old, old))
    camera_module.cleanup_old_screenshots(max_mb=500, keep_days=30)
    assert _files(env) == []


def test_a_non_ascii_source_name_gives_a_safe_file_name_and_keeps_the_real_name_in_the_metadata(env):
    env["store"].update(env["id"], {"name": "Камера двора"})
    body = _shoot(env).get_json()
    assert body["display_name"].startswith("MC_camera_") and body["source"] == "Камера двора"
    assert slug("../../etc/passwd") == "etc-passwd" and slug("") == "camera"


# ---------------------------------------------------------------- limits

def test_one_picture_per_two_seconds_per_source(env):
    assert _shoot(env).status_code == 200
    env["clock"].now += 0.5
    refused = _shoot(env)
    assert refused.status_code == 429 and refused.get_json()["error_code"] == "rate_limited"
    assert 1.0 < refused.get_json()["retry_after_s"] <= 1.5
    assert len([f for f in _files(env) if f.endswith(".jpg")]) == 1 and env["fake"].snaps == 1  # nothing was fetched
    env["clock"].now += 1.6
    assert _shoot(env).status_code == 200


def test_a_full_disk_is_a_clear_error_and_leaves_no_file(env):
    env["service"]._free = lambda _path: 1024
    response = _shoot(env)
    assert response.status_code == 507 and response.get_json()["error_code"] == "storage_full"
    assert _files(env) == []


def test_a_write_that_runs_out_of_space_leaves_no_partial_file(env, monkeypatch):
    real_replace = os.replace

    def failing(src, dst):
        if str(dst).endswith(".jpg"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(ip_snapshot_module.os, "replace", failing)
    response = _shoot(env)
    assert response.status_code == 507 and response.get_json()["error_code"] == "storage_full"
    assert _files(env) == []


# ---------------------------------------------------------------- one frame from the running stream

def test_a_source_without_the_api_takes_one_frame_from_the_running_stream(env):
    env["store"].update(env["id"], {"control": {"type": "none"}})
    _fill_relay(env["driver"].upstream.relay)
    response = _shoot(env)
    body = response.get_json()
    assert response.status_code == 200 and body["method"] == "stream_frame"
    data = env["grabbed"][0]
    assert data[4:8] == b"ftyp" and b"moof" in data  # init segment + keyframe fragment from the relay
    assert env["fake"].log == []  # the camera was not contacted at all
    env["driver"].upstream.ensure_running.assert_not_called()  # no upstream start, so no second RTSP session


def test_without_a_running_preview_the_message_is_start_the_preview_first(env):
    env["store"].update(env["id"], {"control": {"type": "none"}})
    response = _shoot(env)
    assert response.status_code == 409 and response.get_json()["error_code"] == "preview_not_running"
    assert _files(env) == []
    env["driver"].upstream.ensure_running.assert_not_called()


def test_the_relay_gives_no_input_without_a_keyframe_fragment():
    relay = rtsp_stream.FragmentRelay()
    assert relay.snapshot_input() is None
    relay.begin(b"INIT")
    assert relay.snapshot_input() is None  # init but no fragment yet
    relay.push(fmp4.Fragment(data=b"P1", sync=False, samples=1, duration_ticks=1))
    assert relay.snapshot_input() is None  # no keyframe fragment
    relay.push(fmp4.Fragment(data=b"K1", sync=True, samples=1, duration_ticks=1))
    relay.push(fmp4.Fragment(data=b"P2", sync=False, samples=1, duration_ticks=1))
    relay.push(fmp4.Fragment(data=b"P3", sync=False, samples=1, duration_ticks=1))
    relay.push(fmp4.Fragment(data=b"P4", sync=False, samples=1, duration_ticks=1))
    assert relay.snapshot_input() == b"INITK1P2P3"  # init + the newest keyframe + two after it
    relay.push(fmp4.Fragment(data=b"K2", sync=True, samples=1, duration_ticks=1))
    assert relay.snapshot_input() == b"INITK2"


@pytest.mark.skipif(not REAL_FFMPEG, reason="needs ffmpeg")
def test_a_real_one_shot_ffmpeg_makes_a_jpeg_from_the_relay_bytes():
    relay = rtsp_stream.FragmentRelay()
    _fill_relay(relay)
    jpeg = frame_grab.grab_frame(relay, timeout=30)
    assert jpeg[:3] == b"\xff\xd8\xff" and jpeg_size(jpeg) is not None and jpeg_size(jpeg)[0] > 0


def test_ffmpeg_missing_and_a_failed_grab_are_clear_errors():
    relay = rtsp_stream.FragmentRelay()
    _fill_relay(relay)
    with pytest.raises(frame_grab.GrabError) as missing:
        frame_grab.grab_frame(relay, cmd_provider=lambda: [])
    assert missing.value.code == "ffmpeg_missing"
    with pytest.raises(frame_grab.GrabError) as failed:
        frame_grab.grab_frame(relay, cmd_provider=lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])
    assert failed.value.code == "frame_grab_failed"
    with pytest.raises(frame_grab.GrabError) as empty:
        frame_grab.grab_frame(rtsp_stream.FragmentRelay())
    assert empty.value.code == "preview_not_running"


# ---------------------------------------------------------------- API failures

def test_an_unreachable_api_falls_back_to_the_running_stream(env):
    env["fake"].stop()
    _fill_relay(env["driver"].upstream.relay)
    body = _shoot(env).get_json()
    assert body["ok"] is True and body["method"] == "stream_frame"


def test_an_unreachable_api_without_a_preview_is_reported(env):
    env["fake"].stop()
    response = _shoot(env)
    assert response.status_code == 502 and response.get_json()["error_code"] == "unreachable"
    assert _files(env) == []


def test_a_wrong_password_or_a_missing_right_is_reported_not_hidden_by_the_fallback(env):
    _fill_relay(env["driver"].upstream.relay)
    env["store"].update(env["id"], {"username": "guest"}, GUEST_PW)
    denied = _shoot(env)
    assert denied.status_code == 403 and denied.get_json()["error_code"] == "permission_denied" and denied.get_json()["right"] == "snapshot"
    env["clock"].advance(3)
    env["store"].update(env["id"], {"username": "admin"}, "wrong-password-for-test")
    failed = _shoot(env)
    assert failed.status_code == 401 and failed.get_json()["error_code"] == "login_failed"
    assert "wrong-password-for-test" not in failed.get_data(as_text=True)
    assert _files(env) == []


# ---------------------------------------------------------------- other cameras and secrets

def test_a_csi_or_usb_camera_still_uses_the_existing_screenshot_path(tmp_path, monkeypatch):
    class Csi:
        id, source_type = "csi:0", "csi"

    service = MagicMock()
    manager = CameraManager({"csi:0": Csi()}, active_id="csi:0")
    monkeypatch.setattr(camera_module, "capture_screenshot", lambda: {"success": True, "filename": "x.jpg"})
    app = Flask(__name__)
    register_camera_routes(app, camera_module, {"manager": manager}, MagicMock(), _handle_errors, ip_snapshot=service)
    response = app.test_client().post("/api/camera/screenshot")
    assert response.status_code == 200 and response.get_json()["filename"] == "x.jpg"
    service.assert_not_called()


def test_without_the_snapshot_service_a_network_camera_keeps_the_old_refusal(env):
    app = Flask(__name__)
    register_camera_routes(app, camera_module, env["state"], MagicMock(), _handle_errors)
    response = app.test_client().post("/api/camera/screenshot")
    assert response.status_code == 409 and response.get_json()["error_code"] == "not_available_for_network_camera"


def test_no_password_or_token_in_a_response_the_gallery_or_the_log(env, caplog, capsys):
    caplog.set_level("DEBUG")
    responses = [_shoot(env), env["client"].get("/api/camera/screenshots")]
    env["clock"].advance(3)
    env["fake"].stop()
    responses.append(_shoot(env))
    text = "\n".join(r.get_data(as_text=True) for r in responses)
    gallery = "".join(p.read_text(encoding="utf-8") for p in env["gallery"].rglob("*.json"))
    out = capsys.readouterr()
    everything = text + gallery + caplog.text + out.out + out.err
    assert ADMIN_PW not in everything and "token=" not in everything and "cgi-bin" not in everything


def test_the_source_form_validates_the_snapshot_quality(env):
    from camera.source_store import SourceValidationError
    with pytest.raises(SourceValidationError):
        env["store"].update(env["id"], {"control": {"snapshot_quality": "huge"}})
    assert env["store"].get_source(env["id"])["control"]["snapshot_quality"] == "full"
