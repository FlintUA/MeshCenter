"""Gallery G1: thumbnails, listing fields, bulk delete, zip download and the media settings section."""
import io
import json
import os
import time
import zipfile
from functools import wraps
from unittest.mock import MagicMock

import pytest
from flask import Flask
from PIL import Image

from conftest import _stub_libcamera
_stub_libcamera()

import api.api_camera as api_camera_module
import camera.camera as camera
from api.api_camera import register_camera_routes
from api.api_settings import normalize_settings


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


def _picture(directory, name, size=(1600, 900), color=(200, 30, 30)):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    Image.new("RGB", size, color).save(path, "JPEG", quality=90)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    shots = tmp_path / "screenshots"
    shots.mkdir()
    monkeypatch.setattr(camera, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(camera, "SCREENSHOTS_DIR", str(shots))
    monkeypatch.setattr(camera, "THUMB_BACKFILL_PAUSE_S", 0.0)
    app = Flask(__name__)
    register_camera_routes(app, camera, {"manager": None}, MagicMock(), _handle_errors)
    return {"client": app.test_client(), "shots": shots, "day": shots / "2026" / "10" / "30"}


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_thumbnail_is_made_on_first_request_and_is_small(env):
    path = _picture(env["day"], "MC_a.jpg", size=(3840, 2160))
    response = env["client"].get("/api/camera/screenshot/2026/10/30/MC_a.jpg?thumb=1")
    assert response.status_code == 200 and response.mimetype == "image/jpeg"
    with Image.open(io.BytesIO(response.data)) as thumb:
        assert max(thumb.size) == camera.THUMB_LONG_EDGE and thumb.size[0] / thumb.size[1] == pytest.approx(16 / 9, rel=0.02)
    assert len(response.data) < os.path.getsize(path) / 10
    assert camera.read_thumb_info(str(path)) == {"width": 3840, "height": 2160}, "the size of the original is kept"
    assert "max-age" in response.headers["Cache-Control"]


def test_the_original_is_served_without_thumb_and_when_it_cannot_be_made(env, monkeypatch):
    _picture(env["day"], "MC_b.jpg")
    full = env["client"].get("/api/camera/screenshot/2026/10/30/MC_b.jpg")
    assert Image.open(io.BytesIO(full.data)).size == (1600, 900)
    monkeypatch.setattr(camera, "ensure_thumbnail", lambda path: None)
    fallback = env["client"].get("/api/camera/screenshot/2026/10/30/MC_b.jpg?thumb=1")
    assert Image.open(io.BytesIO(fallback.data)).size == (1600, 900)


def test_the_thumbnail_tree_is_never_listed_as_a_picture(env):
    path = _picture(env["day"], "MC_c.jpg")
    assert camera.ensure_thumbnail(str(path))
    listed = camera.list_screenshots()[0]["screenshots"]
    assert [item["filename"] for item in listed] == ["2026/10/30/MC_c.jpg"]
    assert not any(".thumbs" in rel for _full, rel, _stat in camera.iter_screenshot_files())


def test_listing_has_thumb_url_resolution_camera_and_starts_the_backfill(env):
    a = _picture(env["day"], "MC_d.jpg", size=(1920, 1080))
    (env["day"] / "MC_d.jpg.json").write_text(json.dumps({"source_name": "Garage", "width": 1920, "height": 1080}))
    _picture(env["day"], "MC_e.jpg", size=(1280, 720))
    first = camera.list_screenshots()[0]["screenshots"]
    by_name = {item["display_name"]: item for item in first}
    assert by_name["MC_d.jpg"]["camera"] == "Garage" and by_name["MC_d.jpg"]["thumb_url"].endswith("MC_d.jpg?thumb=1")
    assert by_name["MC_e.jpg"]["camera"] == ""
    assert _wait(lambda: os.path.isfile(camera.thumb_paths(str(a))[0]) and not camera._backfill_state["running"]), "backfill made them"
    second = {item["display_name"]: item for item in camera.list_screenshots()[0]["screenshots"]}
    assert (second["MC_e.jpg"]["width"], second["MC_e.jpg"]["height"]) == (1280, 720), "resolution known once the thumbnail exists"


def test_deleting_a_picture_removes_its_thumbnail_and_metadata(env):
    path = _picture(env["day"], "MC_f.jpg")
    (env["day"] / "MC_f.jpg.json").write_text("{}")
    thumb, info = camera.thumb_paths(str(path))
    camera.ensure_thumbnail(str(path))
    assert os.path.isfile(thumb) and os.path.isfile(info)
    camera.remove_screenshot_file(str(path))
    assert not any(os.path.exists(p) for p in (str(path), thumb, info, str(path) + ".json"))


def test_bulk_delete_removes_the_selection_and_reports_the_rest(env):
    _picture(env["day"], "MC_1.jpg")
    _picture(env["day"], "MC_2.jpg")
    _picture(env["day"], "MC_3.jpg")
    response = env["client"].post("/api/camera/screenshots/delete", json={"filenames": [
        "2026/10/30/MC_1.jpg", "2026/10/30/MC_2.jpg", "2026/10/30/nope.jpg", "../../etc/passwd", ".thumbs/2026/x.jpg"]})
    body = response.get_json()
    assert response.status_code == 200 and body["deleted_count"] == 2 and len(body["failed"]) == 3
    assert [i["display_name"] for i in camera.list_screenshots()[0]["screenshots"]] == ["MC_3.jpg"]
    assert env["client"].post("/api/camera/screenshots/delete", json={"filenames": []}).status_code == 400
    assert env["client"].post("/api/camera/screenshots/delete", json={"filenames": ["x"] * 501}).status_code == 400


def test_zip_download_is_a_one_time_link_with_the_selected_pictures(env):
    _picture(env["day"], "MC_z1.jpg")
    _picture(env["day"], "MC_z2.jpg")
    ticket = env["client"].post("/api/camera/screenshots/zip", json={"filenames": ["2026/10/30/MC_z1.jpg", "2026/10/30/MC_z2.jpg", "../x.jpg"]})
    body = ticket.get_json()
    assert ticket.status_code == 200 and body["count"] == 2 and body["total_bytes"] > 0
    download = env["client"].get(body["url"])
    assert download.status_code == 200 and download.mimetype == "application/zip"
    assert "attachment" in download.headers["Content-Disposition"] and ".zip" in download.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
        assert sorted(archive.namelist()) == ["MC_z1.jpg", "MC_z2.jpg"]
        assert archive.testzip() is None
        assert Image.open(io.BytesIO(archive.read("MC_z1.jpg"))).size == (1600, 900)
    assert env["client"].get(body["url"]).status_code == 410, "the link works once"


def test_zip_size_cap_is_enforced_with_the_numbers(env, monkeypatch):
    _picture(env["day"], "MC_big.jpg")
    monkeypatch.setattr(api_camera_module, "GALLERY_ZIP_MAX_BYTES", 1000)
    response = env["client"].post("/api/camera/screenshots/zip", json={"filenames": ["2026/10/30/MC_big.jpg"]})
    body = response.get_json()
    assert response.status_code == 413 and body["error_code"] == "zip_too_large" and body["max_bytes"] == 1000 and body["total_bytes"] > 1000


def test_zip_tickets_expire_and_are_bounded():
    now = [0.0]
    tickets = api_camera_module._ZipTickets(ttl_s=10, limit=2, clock=lambda: now[0])
    first = tickets.issue(["a"])
    now[0] = 11
    assert tickets.take(first) is None, "expired"
    one, two, three = tickets.issue(["1"]), tickets.issue(["2"]), tickets.issue(["3"])
    assert tickets.take(one) is None and tickets.take(two) == ["2"] and tickets.take(three) == ["3"], "the oldest is dropped at the limit"


def test_duplicate_names_in_a_zip_are_kept_apart(tmp_path):
    a, b = _picture(tmp_path / "a", "same.jpg"), _picture(tmp_path / "b", "same.jpg")
    data = b"".join(api_camera_module.stream_zip([(str(a), "a/same.jpg", 1), (str(b), "b/same.jpg", 1)]))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == ["same.jpg", "same_2.jpg"]


def test_media_settings_are_normalized():
    default = {"sort": "date_desc", "view_desktop": "large", "view_mobile": "large"}
    assert normalize_settings({})["media"] == default
    assert normalize_settings({"media": {"sort": "SIZE", "view_desktop": "list", "view_mobile": "small"}})["media"] == {
        "sort": "size", "view_desktop": "list", "view_mobile": "small"}
    assert normalize_settings({"media": {"sort": "bogus", "view_desktop": 7, "view_mobile": "nope"}})["media"] == default
    assert normalize_settings({"media": "x"})["media"] == default


def test_the_view_is_remembered_per_device_and_the_old_single_view_seeds_both():
    legacy = normalize_settings({"media": {"view": "list"}})["media"]
    assert legacy["view_desktop"] == "list" and legacy["view_mobile"] == "list" and "view" not in legacy
    mixed = normalize_settings({"media": {"view": "list", "view_mobile": "small"}})["media"]
    assert mixed["view_desktop"] == "list" and mixed["view_mobile"] == "small", "a field a device chose wins over the old value"


def test_choosing_on_one_device_leaves_the_other_and_the_shared_sort_alone():
    from api.api_settings import _deep_merge
    stored = normalize_settings({"media": {"sort": "name", "view_desktop": "list", "view_mobile": "small"}})
    after_phone = normalize_settings(_deep_merge(stored, {"media": {"view_mobile": "large"}}))["media"]
    assert after_phone == {"sort": "name", "view_desktop": "list", "view_mobile": "large"}
    after_sort = normalize_settings(_deep_merge(stored, {"media": {"sort": "size"}}))["media"]
    assert after_sort == {"sort": "size", "view_desktop": "list", "view_mobile": "small"}
