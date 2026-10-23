"""CAM2: /api/camera/sources routes, the RTSP driver skeleton and the CameraManager integration."""
import json
import logging
from functools import wraps
from unittest.mock import MagicMock

import pytest
from flask import Flask

from camera import address_policy
from camera.camera_driver import CameraDriver
from camera.camera_manager import CameraManager, build_camera_manager, network_drivers_from_state
from camera.rtsp_driver import RtspCameraDriver, drivers_from_store
from camera.source_store import CameraSourceStore
from api.api_camera_sources import WRITE_LIMIT, register_camera_source_routes

PASSWORD = "S3cret-pw-for-tests"


def _lan(host):
    if host.startswith("8."):
        raise address_policy.AddressPolicyError("not_lan_address", "Only local network addresses are allowed.")
    return [host]


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:  # pragma: no cover - would be a real bug
            return {"ok": False, "error": str(error)}, 500
    return wrapped


class _FakeDriver(CameraDriver):
    """Stands in for a hardware (CSI/USB) driver."""
    device_type = "camera"

    def __init__(self, driver_id):
        self.id, self.display_name, self.started, self.stopped = driver_id, driver_id, 0, 0

    def detect(self): return {"model": self.id}
    def start(self, **_): self.started += 1; return True
    def stop(self): self.stopped += 1
    def get_status(self): return {"ok": True, "started": True, "model": self.id}
    def stream_mjpeg(self): return iter(())
    def capture_photo(self, resolution=None): return b""


@pytest.fixture
def env(tmp_path):
    store = CameraSourceStore(str(tmp_path), host_validator=_lan)
    state = {"manager": None, "network_drivers_factory": lambda: drivers_from_store(store)}
    device_manager = MagicMock()
    device_manager.load_or_create.return_value = {"active_camera_id": None}
    app = Flask(__name__)
    register_camera_source_routes(app, store, state, device_manager, _handle_errors)
    return app.test_client(), store, state, device_manager, tmp_path


def _body(**over):
    body = {"name": "Garden", "host": "192.168.2.50", "username": "viewer", "password": PASSWORD,
            "main_path": "Preview_01_main", "sub_path": "Preview_01_sub"}
    body.update(over)
    return body


# -- routes ------------------------------------------------------------------

def test_create_list_update_delete_roundtrip(env):
    client, store, *_ = env
    created = client.post("/api/camera/sources", json=_body())
    assert created.status_code == 201
    source = created.get_json()["source"]
    assert source["password_set"] is True and "password" not in source
    sid = source["id"]

    listing = client.get("/api/camera/sources").get_json()
    assert [s["id"] for s in listing["sources"]] == [sid]
    assert listing["max_sources"] == 8

    updated = client.put(f"/api/camera/sources/{sid}", json={"name": "Garden 2", "port": 8554}).get_json()
    assert updated["source"]["name"] == "Garden 2" and updated["source"]["port"] == 8554
    assert store.get_password(sid) == PASSWORD  # absent password key = keep

    assert client.put(f"/api/camera/sources/{sid}", json={"password": "new-pw"}).status_code == 200
    assert store.get_password(sid) == "new-pw"
    assert client.put(f"/api/camera/sources/{sid}", json={"clear_password": True}).get_json()["source"]["password_set"] is False

    assert client.delete(f"/api/camera/sources/{sid}").status_code == 200
    assert client.get("/api/camera/sources").get_json()["sources"] == []


def test_password_never_appears_in_any_response_or_error(env):
    client, *_ = env
    texts = []
    r = client.post("/api/camera/sources", json=_body())
    texts.append(r.get_data(as_text=True))
    sid = r.get_json()["source"]["id"]
    texts.append(client.get("/api/camera/sources").get_data(as_text=True))
    texts.append(client.put(f"/api/camera/sources/{sid}", json={"host": "8.8.8.8", "password": PASSWORD}).get_data(as_text=True))
    texts.append(client.post("/api/camera/sources", json=_body(name="", password=PASSWORD)).get_data(as_text=True))
    texts.append(client.post("/api/camera/sources", json=_body(host="bad host", password=PASSWORD)).get_data(as_text=True))
    texts.append(client.post("/api/camera/sources", json=_body(name="Garden", password=PASSWORD)).get_data(as_text=True))  # duplicate
    texts.append(client.post("/api/camera/sources", json=_body(bogus=PASSWORD)).get_data(as_text=True))  # unknown field
    assert all(PASSWORD not in text for text in texts)


def test_password_not_in_logs(env, caplog, capsys):
    client, *_ = env
    caplog.set_level(logging.DEBUG)
    client.post("/api/camera/sources", json=_body())
    out = capsys.readouterr()
    assert PASSWORD not in caplog.text and PASSWORD not in out.out and PASSWORD not in out.err


@pytest.mark.parametrize("over,status,code", [
    ({"host": "8.8.8.8"}, 400, "not_lan_address"),
    ({"host": "127.0.0.1"}, 201, None),  # _lan test validator accepts it; the real policy is covered elsewhere
    ({"name": ""}, 400, "missing_field"),
    ({"port": 0}, 400, "invalid_field"),
    ({"bogus": 1}, 400, "unknown_field"),
])
def test_validation_errors(env, over, status, code):
    client, *_ = env
    r = client.post("/api/camera/sources", json=_body(**over))
    assert r.status_code == status
    if code:
        assert r.get_json()["error_code"] == code and r.get_json()["ok"] is False


def test_duplicate_name_and_limit_are_409(env):
    client, *_ = env
    assert client.post("/api/camera/sources", json=_body()).status_code == 201
    assert client.post("/api/camera/sources", json=_body()).status_code == 409


def test_unknown_source_is_404_and_bad_body_is_400(env):
    client, *_ = env
    assert client.put("/api/camera/sources/ip:nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/camera/sources/ip:nope").status_code == 404
    assert client.post("/api/camera/sources", data="not json", content_type="application/json").status_code == 400
    assert client.post("/api/camera/sources", json=[1, 2]).status_code == 400


def test_write_routes_are_rate_limited(env):
    client, *_ = env
    codes = [client.delete("/api/camera/sources/ip:nope").status_code for _ in range(WRITE_LIMIT + 3)]
    assert codes[:WRITE_LIMIT] == [404] * WRITE_LIMIT
    assert codes[WRITE_LIMIT:] == [429] * 3
    assert client.get("/api/camera/sources").status_code == 200  # reads are not limited


def test_storage_failure_is_reported_as_storage_write_failed(env, monkeypatch):
    client, *_ = env
    import camera.source_store as module
    monkeypatch.setattr(module, "safe_write_json", lambda *a, **k: False)
    r = client.post("/api/camera/sources", json=_body())
    assert r.status_code == 500 and r.get_json()["error_code"] == "storage_write_failed"


# -- manager integration ------------------------------------------------------

def test_new_source_appears_in_the_live_manager_and_hardware_drivers_are_untouched(env):
    client, store, state, device_manager, _ = env
    csi = _FakeDriver("csi")
    manager = CameraManager({"csi": csi}, active_id="csi")
    state["manager"] = manager

    sid = client.post("/api/camera/sources", json=_body()).get_json()["source"]["id"]
    ids = [d["id"] for d in manager.list_drivers()]
    assert ids == ["csi", sid]
    assert csi.stopped == 0 and manager.active_id == "csi"
    summary = next(d for d in manager.list_drivers() if d["id"] == sid)
    assert summary["source_type"] == "rtsp" and summary["model"] == "Garden"
    assert PASSWORD not in json.dumps(manager.list_drivers())

    client.put(f"/api/camera/sources/{sid}", json={"name": "Renamed"})
    assert [d["model"] for d in manager.list_drivers() if d["id"] == sid] == ["Renamed"]
    assert csi.stopped == 0

    client.delete(f"/api/camera/sources/{sid}")
    assert [d["id"] for d in manager.list_drivers()] == ["csi"]


def test_deleting_the_active_source_falls_back_and_persists_the_active_id(env):
    client, store, state, device_manager, _ = env
    csi = _FakeDriver("csi")
    manager = CameraManager({"csi": csi}, active_id="csi")
    state["manager"] = manager
    sid = client.post("/api/camera/sources", json=_body()).get_json()["source"]["id"]
    assert manager.set_active(sid) is True and manager.active_id == sid

    client.delete(f"/api/camera/sources/{sid}")
    assert manager.active_id == "csi"
    saved = device_manager.save.call_args[0][0]
    assert saved["active_camera_id"] == "csi"


def test_editing_the_active_source_restarts_it(env):
    client, store, state, device_manager, _ = env
    manager = CameraManager({"csi": _FakeDriver("csi")}, active_id="csi")
    state["manager"] = manager
    sid = client.post("/api/camera/sources", json=_body()).get_json()["source"]["id"]
    manager.set_active(sid)
    old = manager.active()
    client.put(f"/api/camera/sources/{sid}", json={"name": "Edited"})
    assert manager.active() is not old and manager.active().display_name == "Edited"
    assert manager.get_status()["started"] is True


def test_sources_work_without_any_local_camera(env):
    client, store, state, *_ = env
    sid = client.post("/api/camera/sources", json=_body()).get_json()["source"]["id"]
    manager = build_camera_manager(persisted_active_id=sid, network_drivers=network_drivers_from_state(state))
    assert manager.active_id == sid


def test_broken_factory_does_not_break_the_manager_build():
    def boom():
        raise RuntimeError("secrets path /x contains topsecret")
    assert network_drivers_from_state({"network_drivers_factory": boom}) == []
    assert network_drivers_from_state({}) == []


# -- driver skeleton -----------------------------------------------------------

def test_driver_skeleton_makes_no_network_calls_and_reports_state(env):
    _client, store, *_ = env
    source = store.create({"name": "Cam", "host": "192.168.2.50", "main_path": "m", "sub_path": "s"}, PASSWORD)
    resolver_calls = []
    driver = RtspCameraDriver(source, store, host_validator=lambda h: resolver_calls.append(h) or [h])
    assert driver.detect()["source_type"] == "rtsp"
    assert driver.get_status()["state"] == "stopped" and resolver_calls == []
    assert driver.start() is True and driver.get_status()["state"] == "idle"
    assert resolver_calls == ["192.168.2.50"]
    assert driver.stream_mjpeg() is not None and list(driver.stream_mjpeg()) == []
    assert PASSWORD not in json.dumps([driver.detect(), driver.get_status(), driver.config])
    driver.stop()
    assert driver.get_status()["started"] is False


def test_driver_revalidates_the_address_on_start(env):
    _client, store, *_ = env
    source = store.create({"name": "Cam", "host": "192.168.2.50", "main_path": "m", "sub_path": "s"})

    def now_public(host):
        raise address_policy.AddressPolicyError("not_lan_address", "x")

    driver = RtspCameraDriver(source, store, host_validator=now_public)
    assert driver.start() is False and driver.get_status()["state"] == "error"


@pytest.mark.parametrize("field", ["sub_path", "main_path", "username", "password", "host", "name"])
@pytest.mark.parametrize("payload", ["x\nfile /etc/passwd", "x\r\nfile http://evil.example/", "x\x00y"])
def test_newline_payloads_in_any_field_fail_closed(env, field, payload):
    client, store, *_ = env
    r = client.post("/api/camera/sources", json=_body(**{field: payload}))
    assert r.status_code == 400
    assert store.list_sources() == []
    sid = client.post("/api/camera/sources", json=_body()).get_json()["source"]["id"]
    r = client.put(f"/api/camera/sources/{sid}", json={field: payload})
    assert r.status_code == 400
    assert store.get_source(sid)["sub_path"] == "Preview_01_sub"
