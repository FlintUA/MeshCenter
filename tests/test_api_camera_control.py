"""CAM2 Phase 2: control config in the source store, the control + snapshot routes, snapshot sharing and
leases, and "a failing control API never stops the RTSP preview"."""
import os
import ssl
import sys
import time
from unittest.mock import MagicMock

import pytest
from flask import Flask

from conftest import _stub_libcamera
_stub_libcamera()

import camera.camera as camera_module
from api.api_camera import register_camera_routes
from api.api_camera_control import register_camera_control_routes
from camera import reolink_client as rc
from camera import rtsp_stream as rs
from camera.camera_manager import CameraManager
from camera.reolink_control import ReolinkControlManager, SnapshotLeases
from camera.source_store import CameraSourceStore, SourceValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
from fake_reolink_server import FakeReolink, make_self_signed_cert  # noqa: E402
from test_api_camera_stream import _driver, _handle_errors  # noqa: E402

ADMIN_PW = "pw-admin-test"
GUEST_PW = "pw-guest-test"


def _lan(host):
    return [host]


def _control(fake, **extra):
    return {"type": "reolink", "scheme": "http", "port": fake.port, "allow_http": True, **extra}


@pytest.fixture
def fake():
    server = FakeReolink().start()
    yield server
    server.stop()


@pytest.fixture
def env(tmp_path, monkeypatch, fake):
    """A real network driver (fake ffmpeg stream) whose source also has camera control pointing at the mock."""
    (tmp_path / "screenshots").mkdir()
    monkeypatch.setattr(camera_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(camera_module, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    driver, limiter = _driver(tmp_path, monkeypatch)
    store = driver._store
    store.update(driver.id, {"host": "127.0.0.1", "username": "admin", "control": _control(fake)}, ADMIN_PW)
    manager = CameraManager({driver.id: driver}, active_id=driver.id)
    state = {"manager": manager}
    control = ReolinkControlManager(store, host_validator=_lan)
    leases = SnapshotLeases(limiter, start_sweeper=False)
    app = Flask(__name__)
    register_camera_routes(app, camera_module, state, MagicMock(), _handle_errors)
    register_camera_control_routes(app, store, state, control, leases, _handle_errors)
    yield {"client": app.test_client(), "driver": driver, "store": store, "limiter": limiter, "control": control,
           "leases": leases, "fake": fake, "app": app, "id": driver.id, "tmp": tmp_path}
    driver.stop()


def _post(env, path, body=None):
    return env["client"].post(f"/api/camera/sources/{env['id']}/control{path}", json=body or {})


def _set_account(env, user, password):
    env["store"].update(env["id"], {"username": user}, password)


# ---------------------------------------------------------------- source store: the control section

def test_a_source_without_a_control_section_reads_as_rtsp_only(tmp_path):
    store = CameraSourceStore(str(tmp_path), host_validator=_lan)
    source = store.create({"name": "A", "host": "192.168.2.9", "main_path": "m", "sub_path": "s"})
    assert source["control"]["type"] == "none" and source["control"]["pinned_sha256"] == "" and source["control"]["capabilities"] == {}


def test_control_input_is_validated(tmp_path):
    store = CameraSourceStore(str(tmp_path), host_validator=_lan)
    base = {"name": "A", "host": "192.168.2.9", "main_path": "m", "sub_path": "s"}
    for bad in ({"type": "onvif"}, {"scheme": "ftp"}, {"port": 0}, {"port": True}, {"channel": 99}, {"allow_http": "yes"}, {"bogus": 1}):
        with pytest.raises(SourceValidationError):
            store.create({**base, "control": {"type": "reolink", **bad}})
    with pytest.raises(SourceValidationError) as raised:  # plain HTTP only with the explicit opt-in
        store.create({**base, "control": {"type": "reolink", "scheme": "http"}})
    assert raised.value.code == "http_not_allowed"
    assert store.create({**base, "control": {"type": "reolink", "scheme": "http", "allow_http": True, "port": 80}})["control"]["scheme"] == "http"


def test_the_pin_cannot_be_set_through_user_input_and_is_cleared_when_the_address_changes(tmp_path):
    store = CameraSourceStore(str(tmp_path), host_validator=_lan)
    source = store.create({"name": "A", "host": "192.168.2.9", "main_path": "m", "sub_path": "s", "control": {"type": "reolink"}})
    forged = store.update(source["id"], {"control": {"pinned_sha256": "a" * 64}})
    assert forged["control"]["pinned_sha256"] == ""
    store.set_control_state(source["id"], pinned_sha256="b" * 64, capabilities={"snapshot": True})
    assert store.get_source(source["id"])["control"]["pinned_sha256"] == "b" * 64
    assert store.update(source["id"], {"name": "Renamed"})["control"]["pinned_sha256"] == "b" * 64  # unrelated edit keeps it
    moved = store.update(source["id"], {"host": "192.168.2.10"})
    assert moved["control"]["pinned_sha256"] == "" and moved["control"]["capabilities"] == {}


def test_the_password_for_control_is_the_sources_own_and_never_in_the_public_source(env):
    public = env["store"].get_source(env["id"])
    assert ADMIN_PW not in str(public) and public["password_set"] is True


# ---------------------------------------------------------------- probe, capabilities, status

def test_probe_stores_the_capabilities_and_the_status_offers_the_snapshot_fallback(env):
    assert env["client"].get("/api/camera/status").get_json()["snapshot_fallback"] is False
    response = _post(env, "/probe")
    body = response.get_json()
    assert response.status_code == 200 and body["capabilities"]["snapshot"] and body["capabilities"]["zoom"]
    env["driver"]._control_cache = (-1e9, {})  # the status caches the saved section for a few seconds
    status = env["client"].get("/api/camera/status").get_json()
    assert status["snapshot_fallback"] is True and status["zoom_control"] is True and status["control_type"] == "reolink"
    assert env["store"].get_source(env["id"])["control"]["capabilities"]["snapshot"] is True


def test_a_limited_account_is_told_which_right_is_missing_and_gets_no_snapshot_fallback(env):
    _set_account(env, "guest", GUEST_PW)
    caps = _post(env, "/probe").get_json()["capabilities"]
    assert caps["snapshot"] is False and caps["denied"] == [{"command": "Snap", "right": "snapshot"}]
    response = _post(env, "/zoom", {"pos": 20})
    body = response.get_json()
    assert response.status_code == 403 and body["error_code"] == "permission_denied" and body["right"] == "lens_control"
    snap = env["client"].get("/api/camera/snapshot")
    assert snap.status_code == 404 and snap.get_json()["error_code"] == "snapshot_not_supported"


def test_control_off_means_rtsp_only(env):
    env["store"].update(env["id"], {"control": {"type": "none"}})
    response = _post(env, "/probe")
    assert response.status_code == 404 and response.get_json()["error_code"] == "not_supported"
    assert env["client"].get("/api/camera/snapshot").status_code == 404


def test_unknown_source(env):
    assert env["client"].post("/api/camera/sources/ip:nope/control/probe", json={}).status_code == 404


# ---------------------------------------------------------------- zoom routes

def test_zoom_read_set_and_read_back(env):
    read = env["client"].get(f"/api/camera/sources/{env['id']}/control/zoom").get_json()
    assert read["zoom"] == {"position": 10, "range": [0, 32], "focus_position": 100}
    moved = _post(env, "/zoom", {"pos": 15}).get_json()
    assert moved["ok"] and moved["position"] == 15 and moved["settled"] is True and moved["requested"] == 15


def test_zoom_out_of_range_is_refused_before_the_lens_is_touched(env):
    response = _post(env, "/zoom", {"pos": 99})
    assert response.status_code == 400 and response.get_json()["error_code"] == "zoom_out_of_range"
    assert env["fake"].zoom_commands == []
    assert _post(env, "/zoom", {}).status_code == 400


# ---------------------------------------------------------------- TLS pinning flow

def test_untrusted_certificate_flow_shows_the_fingerprint_and_pins_only_what_the_user_confirmed(env, tmp_path):
    pair = make_self_signed_cert(str(tmp_path))
    if pair is None:
        pytest.skip("needs the cryptography package or the openssl program")
    secure = FakeReolink(tls=pair).start()
    try:
        with open(pair[0]) as handle:
            real = rc.fingerprint_of(ssl.PEM_cert_to_DER_cert(handle.read()))
        env["store"].update(env["id"], {"control": {"type": "reolink", "scheme": "https", "port": secure.port, "allow_http": False}})
        probe = _post(env, "/probe")
        body = probe.get_json()
        assert probe.status_code == 409 and body["error_code"] == "tls_untrusted" and body["fingerprint"] == real
        assert secure.logins == 0  # the password was never sent to an untrusted certificate
        wrong = _post(env, "/trust", {"sha256": ("0" if real[0] != "0" else "1") + real[1:]})
        assert wrong.status_code == 409 and wrong.get_json()["error_code"] == "tls_pin_mismatch"
        assert env["store"].get_source(env["id"])["control"]["pinned_sha256"] == ""
        trusted = _post(env, "/trust", {"sha256": real.upper()})
        assert trusted.status_code == 200 and trusted.get_json()["pinned_sha256"] == real
        assert _post(env, "/probe").get_json()["capabilities"]["zoom"] is True
        # the certificate changes (another device on this address): fails closed, shows the new fingerprint
        env["store"].set_control_state(env["id"], pinned_sha256="f" * 64)
        env["control"].forget(env["id"])
        changed = _post(env, "/probe")
        assert changed.status_code == 409 and changed.get_json()["error_code"] == "tls_pin_mismatch" and changed.get_json()["fingerprint"] == real
    finally:
        secure.stop()


# ---------------------------------------------------------------- snapshot fallback

def _snap(env, ua="phone-a"):
    return env["client"].get("/api/camera/snapshot", headers={"User-Agent": ua})


def test_snapshot_is_the_cameras_jpeg_with_no_store(env):
    response = _snap(env)
    assert response.status_code == 200 and response.mimetype == "image/jpeg"
    assert response.headers["Cache-Control"] == "no-store" and response.data[:3] == b"\xff\xd8\xff"


def test_many_viewers_share_one_picture_per_second(env):
    for index in range(4):
        assert _snap(env, ua=f"phone-{index}").status_code == 200
    assert env["fake"].snaps == 1  # one fetch served four viewers


def test_the_cache_expires_after_a_second(env):
    now = [1000.0]
    env["control"]._clock = lambda: now[0]
    _snap(env)
    now[0] += 0.5
    _snap(env)
    assert env["fake"].snaps == 1
    now[0] += 0.6
    _snap(env)
    assert env["fake"].snaps == 2


def test_a_slow_fetch_does_not_make_the_next_poll_reuse_the_old_picture(env):
    """The picture's age counts from the START of its fetch (a fetch takes 0.5-1 s on a weak link)."""
    now = [1000.0]
    env["control"]._clock = lambda: now[0]
    real = env["control"]._client(env["control"]._source(env["id"]))

    class Slow:
        def snapshot(self, sub=True):
            now[0] += 0.6  # the fetch takes 0.6 s
            return real.snapshot(sub)

    env["control"]._client = lambda source: Slow()
    _snap(env)
    now[0] += 0.4  # the player asks again 1.0 s after it started the first request
    _snap(env)
    assert env["fake"].snaps == 2  # a new picture, not the cached one


def test_no_background_polling_without_a_viewer(env):
    time.sleep(0.3)
    assert env["fake"].snaps == 0 and env["fake"].logins == 0


def test_fallback_viewers_use_the_same_limits_as_stream_viewers(env):
    assert [_snap(env, ua=f"p{i}").status_code for i in range(4)] == [200] * 4
    refused = _snap(env, ua="p-fifth")
    assert refused.status_code == 429 and refused.get_json()["error_code"] == "viewer_limit"
    assert env["limiter"].count(env["id"]) == 4
    stream = env["client"].get("/api/camera/stream/fmp4", buffered=False)  # a stream viewer counts in the same limiter
    assert stream.status_code == 429


def test_a_silent_viewer_frees_its_slot_and_a_polling_one_keeps_it():
    limiter = rs.ViewerLimiter()
    now = [0.0]
    leases = SnapshotLeases(limiter, clock=lambda: now[0], start_sweeper=False)
    assert leases.touch("s", "a") and leases.touch("s", "b")
    assert limiter.count("s") == 2
    for _ in range(3):
        now[0] += 10
        assert leases.touch("s", "a")  # keeps asking
    assert limiter.count("s") == 1 and leases.count("s") == 1  # "b" was silent for more than 15 s


def test_a_lease_older_than_ten_minutes_is_counted_again():
    limiter = rs.ViewerLimiter(per_source=1, total=1)
    now = [0.0]
    leases = SnapshotLeases(limiter, clock=lambda: now[0], start_sweeper=False)
    assert leases.touch("s", "a")
    for _ in range(70):  # keeps asking every 10 s for 700 s
        now[0] += 10
        assert leases.touch("s", "a")
    assert limiter.count("s") == 1  # re-acquired, not leaked
    other = SnapshotLeases(limiter, clock=lambda: now[0], start_sweeper=False)
    assert other.touch("s", "b") is False  # the slot is really taken


def test_a_dead_camera_is_remembered_for_a_few_seconds_so_polling_viewers_do_not_pile_up(env):
    env["fake"].stop()
    first = _snap(env)
    started = time.monotonic()
    second = _snap(env, ua="phone-b")
    assert first.status_code == 502 and second.status_code == 502 and first.get_json()["error_code"] == "unreachable"
    assert time.monotonic() - started < 0.5  # answered from the remembered failure, no new connect timeout


def test_the_active_camera_must_be_a_network_camera(env):
    env["driver"].source_type = "csi"
    assert env["client"].get("/api/camera/snapshot").status_code == 404


# ---------------------------------------------------------------- the control API never stops the preview

def _read_some(response):
    iterator = iter(response.response)
    assert next(iterator)[4:8] == b"ftyp"
    seen = b""
    deadline = time.monotonic() + 10
    while b"moof" not in seen and time.monotonic() < deadline:
        seen += next(iterator)
    assert b"moof" in seen


def test_a_failing_control_api_leaves_the_rtsp_preview_running(env):
    stream = env["client"].get("/api/camera/stream/fmp4", buffered=False)
    assert stream.status_code == 200
    _read_some(stream)
    env["fake"].stop()  # the camera's web API goes away (the RTSP side is a separate service)
    for attempt in (lambda: _post(env, "/probe"), lambda: _snap(env), lambda: _post(env, "/zoom", {"pos": 12})):
        assert attempt().status_code == 502
    status = env["client"].get("/api/camera/status").get_json()
    assert status["state"] == "live" and status["ok"] is True  # the stream state is untouched
    iterator = iter(stream.response)
    assert len(next(iterator)) > 0  # and the open stream still delivers
    stream.close()
    # a new viewer can still join
    again = env["client"].get("/api/camera/stream/fmp4", buffered=False)
    assert again.status_code == 200
    again.close()


def test_a_control_error_does_not_change_the_stream_state_or_stop_the_upstream(env):
    env["fake"].login_status = 500
    stream = env["client"].get("/api/camera/stream/fmp4", buffered=False)
    _read_some(stream)
    assert _post(env, "/probe").status_code in (401, 502)
    assert env["driver"].get_status()["state"] == "live"
    stream.close()


# ---------------------------------------------------------------- secrets

def test_no_password_or_token_reaches_a_response_or_the_log(env, caplog, capsys):
    caplog.set_level("DEBUG")
    responses = [_post(env, "/probe"), _snap(env), _post(env, "/zoom", {"pos": 14}),
                 env["client"].get(f"/api/camera/sources/{env['id']}/control"),
                 env["client"].get("/api/camera/status")]
    _set_account(env, "admin", "wrong-password-for-test")
    env["control"].forget(env["id"])
    responses.append(_post(env, "/probe"))
    token = env["control"]._clients and next(iter(env["control"]._clients.values()))[1]._token
    text = "\n".join(r.get_data(as_text=True) for r in responses if r.mimetype != "image/jpeg")
    out = capsys.readouterr()
    everything = text + caplog.text + out.out + out.err
    for secret in (ADMIN_PW, "wrong-password-for-test", token or "no-token"):
        assert secret not in everything
    assert "token=" not in everything and "cgi-bin" not in everything
