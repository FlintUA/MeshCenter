"""CAM2 saved network camera sources: camera/source_store.py."""
import json
import os
import stat

import pytest

from camera import address_policy
from camera.source_store import (
    MAX_SOURCES,
    UNSET,
    CameraSourceStore,
    SourceNotFound,
    SourceStorageError,
    SourceValidationError,
)

PASSWORD = "S3cret-pw-for-tests"  # made up, only ever used by this test file


def _lan(host):
    if host == "bad.example":
        raise address_policy.AddressPolicyError("not_lan_address", "Only local network addresses are allowed.")
    return [host]


@pytest.fixture
def store(tmp_path):
    return CameraSourceStore(str(tmp_path), host_validator=_lan)


def _fields(**over):
    base = {"name": "Garden", "host": "192.168.2.50", "port": 554, "username": "viewer",
            "main_path": "Preview_01_main", "sub_path": "Preview_01_sub"}
    base.update(over)
    return base


def test_create_returns_public_source_without_password(store):
    source = store.create(_fields(), PASSWORD)
    assert source["id"].startswith("ip:")
    assert source["password_set"] is True
    assert PASSWORD not in json.dumps(source)
    assert source["preferred_profile"] == "sub" and source["transport"] == "tcp"
    assert source["connect_timeout_s"] == 10


def test_password_lives_only_in_the_secrets_file(store, tmp_path):
    source = store.create(_fields(), PASSWORD)
    assert PASSWORD not in (tmp_path / "camera_sources.json").read_text(encoding="utf-8")
    assert PASSWORD in (tmp_path / "camera_secrets.json").read_text(encoding="utf-8")
    assert store.get_password(source["id"]) == PASSWORD
    assert PASSWORD not in json.dumps(store.list_sources())
    assert PASSWORD not in json.dumps(store.get_source(source["id"]))


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_secrets_file_is_owner_only(store, tmp_path):
    store.create(_fields(), PASSWORD)
    mode = stat.S_IMODE(os.stat(tmp_path / "camera_secrets.json").st_mode)
    assert mode == 0o600


def test_survives_a_new_store_instance(store, tmp_path):
    created = store.create(_fields(), PASSWORD)
    again = CameraSourceStore(str(tmp_path), host_validator=_lan)
    assert [s["id"] for s in again.list_sources()] == [created["id"]]
    assert again.get_password(created["id"]) == PASSWORD


def test_update_keeps_password_by_default_and_can_replace_or_clear_it(store):
    created = store.create(_fields(), PASSWORD)
    sid = created["id"]
    store.update(sid, {"name": "Garden 2"})
    assert store.get_password(sid) == PASSWORD
    assert store.get_source(sid)["name"] == "Garden 2"
    store.update(sid, {}, password="another-pw")
    assert store.get_password(sid) == "another-pw"
    cleared = store.update(sid, {}, password=None)
    assert cleared["password_set"] is False and store.get_password(sid) == ""


def test_update_unknown_or_delete_unknown_raises(store):
    with pytest.raises(SourceNotFound):
        store.update("ip:nope", {"name": "x"})
    with pytest.raises(SourceNotFound):
        store.delete("ip:nope")


def test_delete_removes_source_and_secret(store, tmp_path):
    sid = store.create(_fields(), PASSWORD)["id"]
    store.delete(sid)
    assert store.list_sources() == []
    assert PASSWORD not in (tmp_path / "camera_secrets.json").read_text(encoding="utf-8")


def test_address_policy_is_enforced_on_create_and_update(store):
    with pytest.raises(SourceValidationError) as info:
        store.create(_fields(host="bad.example"))
    assert info.value.code == "not_lan_address" and info.value.field == "host"
    sid = store.create(_fields())["id"]
    with pytest.raises(SourceValidationError):
        store.update(sid, {"host": "bad.example"})
    assert store.get_source(sid)["host"] == "192.168.2.50"


def test_real_policy_rejects_loopback_by_default(tmp_path):
    real = CameraSourceStore(str(tmp_path))
    with pytest.raises(SourceValidationError) as info:
        real.create(_fields(host="127.0.0.1"))
    assert info.value.code == "loopback"


@pytest.mark.parametrize("over,field", [
    ({"name": ""}, "name"), ({"name": "x" * 61}, "name"), ({"name": "bad\nname"}, "name"),
    ({"host": ""}, "host"),
    ({"port": 0}, "port"), ({"port": 70000}, "port"), ({"port": "554"}, "port"), ({"port": True}, "port"),
    ({"username": "a:b"}, "username"), ({"username": "a@b"}, "username"), ({"username": "x" * 65}, "username"),
    ({"sub_path": "a b"}, "sub_path"), ({"sub_path": "a'b"}, "sub_path"), ({"sub_path": "rtsp://x/y"}, "sub_path"),
    ({"sub_path": "../etc"}, "sub_path"), ({"sub_path": "a\nb"}, "sub_path"), ({"sub_path": "a#b"}, "sub_path"),
    ({"preferred_profile": "huge"}, "preferred_profile"),
    ({"preferred_profile": "main", "main_path": ""}, "main_path"),
    ({"transport": "sctp"}, "transport"),
    ({"connect_timeout_s": 1}, "connect_timeout_s"), ({"connect_timeout_s": 61}, "connect_timeout_s"),
])
def test_field_validation(store, over, field):
    with pytest.raises(SourceValidationError) as info:
        store.create(_fields(**over))
    assert info.value.field == field


@pytest.mark.parametrize("pw", ["x" * 129, "bad\npw", "bad\x00pw"])
def test_password_validation(store, pw):
    with pytest.raises(SourceValidationError) as info:
        store.create(_fields(), pw)
    assert info.value.field == "password"
    assert store.list_sources() == []


def test_query_string_paths_are_allowed(store):
    source = store.create(_fields(sub_path="live?channel=1&subtype=1"))
    assert source["sub_path"] == "live?channel=1&subtype=1"


def test_duplicate_name_is_case_insensitive(store):
    store.create(_fields())
    with pytest.raises(SourceValidationError) as info:
        store.create(_fields(name="garden"))
    assert info.value.code == "duplicate_name"


def test_source_limit(store):
    for i in range(MAX_SOURCES):
        store.create(_fields(name=f"cam{i}"))
    with pytest.raises(SourceValidationError) as info:
        store.create(_fields(name="one too many"))
    assert info.value.code == "too_many_sources"


def test_failed_write_leaves_disk_unchanged(store, monkeypatch):
    first = store.create(_fields(), PASSWORD)
    import camera.source_store as module

    real = module.safe_write_json
    calls = {"n": 0}

    def flaky(path, data, **kw):
        calls["n"] += 1
        if path.endswith("camera_sources.json") and calls["n"] <= 2:
            return False  # the first attempt for sources.json fails; the restore write goes through
        return real(path, data, **kw)

    monkeypatch.setattr(module, "safe_write_json", flaky)
    with pytest.raises(SourceStorageError):
        store.create(_fields(name="second"), "second-pw")
    monkeypatch.setattr(module, "safe_write_json", real)
    assert [s["id"] for s in store.list_sources()] == [first["id"]]
    assert "second-pw" not in json.dumps(json.load(open(store.secrets_path, encoding="utf-8")))


def test_corrupt_file_is_quarantined_not_fatal(store, tmp_path):
    (tmp_path / "camera_sources.json").write_text("{not json", encoding="utf-8")
    assert store.list_sources() == []
    store.create(_fields())
    assert len(store.list_sources()) == 1
    assert any(p.name.startswith("camera_sources.json.corrupt-") for p in tmp_path.iterdir())


def test_unset_sentinel_is_not_a_string():
    assert UNSET is not None and not isinstance(UNSET, str)
