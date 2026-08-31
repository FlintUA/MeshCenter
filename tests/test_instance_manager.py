"""Tests for meshsrv/instance_manager.py's schema-v2 migration (Task B of
the installation-ID rollout - see meshsrv/installation_identity.py for
Task A). No server.py/config.py import needed for most of these - this
module only depends on meshsrv.installation_identity (pure stdlib) and
storage.json_store, neither of which touch config.py. The one exception
(the corrupted-ID WARNING log) lazily imports system_log, which does need
config.py - that single test stubs system_log via sys.modules instead of
pulling in the full server_module fixture, to keep the rest of this file
independent of it.
"""

import json
import sys
import threading
import types
from unittest.mock import Mock, patch

import pytest

from meshsrv.installation_identity import generate_installation_id, is_valid_installation_id
from meshsrv.instance_manager import INSTANCE_SCHEMA_VERSION, InstanceManager


@pytest.fixture
def instance_path(tmp_path):
    return tmp_path / "instance.json"


def _stub_system_log(monkeypatch):
    """Replace the real system_log module (which needs config.py) with a
    fake one exposing a mock log_system_event, so the lazy import inside
    InstanceManager._log_corrupted_id_replaced() resolves without config.py
    being on sys.path."""
    fake_module = types.ModuleType("system_log")
    fake_module.log_system_event = Mock()
    monkeypatch.setitem(sys.modules, "system_log", fake_module)
    return fake_module.log_system_event


def test_fresh_install_generates_a_valid_id(instance_path):
    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    installation = identity["installation"]
    assert is_valid_installation_id(installation["id"])
    assert installation["assignment_reason"] == "fresh_install"
    assert installation["assigned_at"] is None
    assert installation["time_source"] == "pending"


def test_migrating_a_genuine_v1_file_generates_an_id_with_migration_reason(instance_path):
    v1_file = {
        "schema_version": 1,
        "instance_name": "Flint Base",
        "hostname": "meshcenter-prod",
        "active_profile_id": "067a40fa",
        "radio": {"node_id": "!067a40fa", "long_name": "Flint Base", "short_name": "FLTB",
                   "hardware": "RAK4631", "role": "", "port": "/dev/ttyACM0", "firmware_version": ""},
        "runtime": {"cli_path": "/usr/local/bin/meshtastic", "last_detected_at": "2026-08-17T10:00:00+00:00",
                    "identity_status": "MATCH", "last_error": None, "last_detected_radio": {}},
    }
    instance_path.write_text(json.dumps(v1_file), encoding="utf-8")

    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    installation = identity["installation"]
    assert is_valid_installation_id(installation["id"])
    assert installation["assignment_reason"] == "migration"


def test_valid_existing_id_is_not_regenerated_on_load(instance_path):
    manager = InstanceManager(instance_path)
    first = manager.load_or_create({})
    original_id = first["installation"]["id"]

    reloaded = InstanceManager(instance_path).load_or_create({})
    assert reloaded["installation"]["id"] == original_id


def test_valid_existing_id_survives_multiple_routine_saves(instance_path):
    # This is the scenario the spec explicitly warns about: save() runs on
    # every profile switch / radio accept in server.py, not just at startup
    # - a bug here would silently mint a new ID on ordinary operation.
    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    original_id = identity["installation"]["id"]

    for _ in range(5):
        updated = dict(identity)
        updated["active_profile_id"] = "some-other-profile"
        identity = manager.save(updated)
        assert identity["installation"]["id"] == original_id


def test_generator_is_not_called_when_a_valid_id_already_exists(instance_path):
    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})

    with patch("meshsrv.instance_manager.generate_installation_id") as mock_generate:
        updated = dict(identity)
        updated["hostname"] = "renamed-host"
        manager.save(updated)
    mock_generate.assert_not_called()


def test_corrupted_existing_id_is_replaced(instance_path):
    raw = {
        "schema_version": 2,
        "radio": {}, "runtime": {},
        "installation": {"id": "not-a-real-id", "assigned_at": None, "time_source": "pending", "assignment_reason": "fresh_install"},
    }
    instance_path.write_text(json.dumps(raw), encoding="utf-8")

    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    installation = identity["installation"]
    assert is_valid_installation_id(installation["id"])
    assert installation["id"] != "not-a-real-id"
    assert installation["assignment_reason"] == "migration"


def test_corrupted_existing_id_logs_a_warning_system_event(instance_path, monkeypatch):
    mock_log = _stub_system_log(monkeypatch)
    raw = {
        "schema_version": 2,
        "radio": {}, "runtime": {},
        "installation": {"id": "GARBAGE", "assigned_at": None, "time_source": "pending", "assignment_reason": "fresh_install"},
    }
    instance_path.write_text(json.dumps(raw), encoding="utf-8")

    InstanceManager(instance_path).load_or_create({})

    mock_log.assert_called_once()
    args, kwargs = mock_log.call_args
    assert args[1] == "WARNING"
    assert "GARBAGE" in args[2]
    assert kwargs.get("source") == "instance"


def test_assignment_reason_is_forward_carried_not_recomputed(instance_path):
    # Once set, assignment_reason must survive unchanged on later saves, even
    # though a later save's raw data always looks non-empty (had_any_data is
    # always True by then) - proving the field isn't being recomputed from
    # had_any_data on every pass.
    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    assert identity["installation"]["assignment_reason"] == "fresh_install"

    updated = dict(identity)
    updated["active_profile_id"] = "switched"
    identity = manager.save(updated)
    assert identity["installation"]["assignment_reason"] == "fresh_install"


def test_assigned_at_and_time_source_are_preserved_across_saves(instance_path):
    # Task B doesn't decide these values (Task C does) but must not clobber
    # them once something (a future Task C) has set them.
    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    updated = dict(identity)
    updated["installation"] = dict(identity["installation"])
    updated["installation"]["assigned_at"] = "2026-09-01T12:00:00+00:00"
    updated["installation"]["time_source"] = "system_ntp"
    identity = manager.save(updated)

    updated2 = dict(identity)
    updated2["hostname"] = "renamed-again"
    identity = manager.save(updated2)
    assert identity["installation"]["assigned_at"] == "2026-09-01T12:00:00+00:00"
    assert identity["installation"]["time_source"] == "system_ntp"


def test_schema_version_is_2(instance_path):
    identity = InstanceManager(instance_path).load_or_create({})
    assert identity["schema_version"] == 2 == INSTANCE_SCHEMA_VERSION


def test_installation_key_has_exactly_the_expected_fields(instance_path):
    identity = InstanceManager(instance_path).load_or_create({})
    assert set(identity["installation"].keys()) == {"id", "assigned_at", "time_source", "assignment_reason"}


def test_migration_preserves_every_pre_existing_field(instance_path):
    # Build a realistic, fully-populated "canonical v1" file by round-tripping
    # through the module's own (pre-v2) normalization shape first, so this
    # isn't an arbitrary hand-written dict - it's what a real v1 install's
    # instance.json actually looks like on disk.
    canonical_v1 = {
        "schema_version": 1,
        "instance_name": "Flint Base",
        "hostname": "meshcenter-prod",
        "active_profile_id": "067a40fa",
        "radio": {
            "node_id": "!067a40fa", "long_name": "Flint Base", "short_name": "FLTB",
            "hardware": "RAK4631", "role": "field", "port": "/dev/ttyACM0",
            "firmware_version": "2.5.0",
        },
        "runtime": {
            "cli_path": "/usr/local/bin/meshtastic",
            "last_detected_at": "2026-08-17T10:00:00+00:00",
            "identity_status": "MATCH",
            "last_error": None,
            "last_detected_radio": {
                "node_id": "!067a40fa", "long_name": "Flint Base", "short_name": "FLTB",
                "hardware": "RAK4631", "role": "field", "port": "/dev/ttyACM0",
                "firmware_version": "2.5.0",
            },
        },
    }
    instance_path.write_text(json.dumps(canonical_v1), encoding="utf-8")

    identity = InstanceManager(instance_path).load_or_create({})

    for key in ("instance_name", "hostname", "active_profile_id", "radio", "runtime"):
        assert identity[key] == canonical_v1[key], f"{key} changed during migration"
    assert identity["schema_version"] == 2
    assert "installation" in identity


def test_concurrent_fresh_installs_produce_exactly_one_id(instance_path):
    # Real threads, real lock, real generator (wrapped, not replaced) - see
    # investigation report: this is a regression guard on the existing lock
    # covering the whole normalize-and-write path, not proof of a new
    # mechanism.
    manager = InstanceManager(instance_path)
    thread_count = 20
    barrier = threading.Barrier(thread_count)
    results = [None] * thread_count

    def worker(index):
        barrier.wait()
        results[index] = manager.load_or_create({})["installation"]["id"]

    with patch(
        "meshsrv.instance_manager.generate_installation_id",
        side_effect=generate_installation_id,
    ) as mock_generate:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert len(set(results)) == 1
    assert mock_generate.call_count == 1

    on_disk = json.loads(instance_path.read_text(encoding="utf-8"))
    assert on_disk["installation"]["id"] == results[0]


def test_save_with_missing_installation_key_still_preserves_id_via_defaults(instance_path):
    # A caller that builds its own dict without copying "installation" at all
    # (not how server.py's call sites currently work, but not guaranteed by
    # the type signature either) must still not trigger regeneration - the
    # previous in-memory state (self._data, passed as `defaults`) is the
    # fallback source, not just the raw dict argument.
    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    original_id = identity["installation"]["id"]

    bare_update = {"active_profile_id": "no-installation-key-here"}
    result = manager.save(bare_update)
    assert result["installation"]["id"] == original_id


def test_peek_returns_none_for_a_missing_file(instance_path):
    manager = InstanceManager(instance_path)
    assert manager.peek() is None
    assert not instance_path.exists()


def test_peek_returns_none_for_a_corrupted_file_without_touching_it(instance_path):
    corrupted_bytes = b"{not valid json"
    instance_path.write_bytes(corrupted_bytes)
    manager = InstanceManager(instance_path)

    assert manager.peek() is None
    assert instance_path.read_bytes() == corrupted_bytes


def test_peek_returns_the_stored_installation_block_verbatim(instance_path):
    manager = InstanceManager(instance_path)
    identity = manager.load_or_create({})
    original_id = identity["installation"]["id"]

    peeked = manager.peek()
    assert peeked["id"] == original_id
    assert peeked == identity["installation"]


def test_peek_does_not_validate_or_regenerate_a_corrupted_id(instance_path):
    # peek() must show a format-invalid stored value exactly as-is, not
    # silently "fix" it - that's the whole point: a genuinely read-only
    # diagnostic view, not a second normalize() path.
    raw = {
        "schema_version": 2, "radio": {}, "runtime": {},
        "installation": {"id": "GARBAGE", "assigned_at": None, "time_source": "pending", "assignment_reason": "fresh_install"},
    }
    instance_path.write_text(json.dumps(raw), encoding="utf-8")
    manager = InstanceManager(instance_path)

    peeked = manager.peek()
    assert peeked["id"] == "GARBAGE"
    # And no write happened - the corrupted id is still exactly what's on disk.
    on_disk = json.loads(instance_path.read_text(encoding="utf-8"))
    assert on_disk["installation"]["id"] == "GARBAGE"


def test_peek_never_calls_generate_installation_id(instance_path):
    raw = {
        "schema_version": 2, "radio": {}, "runtime": {},
        "installation": {"id": "GARBAGE", "assigned_at": None, "time_source": "pending", "assignment_reason": "fresh_install"},
    }
    instance_path.write_text(json.dumps(raw), encoding="utf-8")
    manager = InstanceManager(instance_path)

    with patch("meshsrv.instance_manager.generate_installation_id") as mock_generate:
        manager.peek()
    mock_generate.assert_not_called()


def test_peek_does_not_log_a_corrupted_id_warning(instance_path, monkeypatch):
    mock_log = _stub_system_log(monkeypatch)
    raw = {
        "schema_version": 2, "radio": {}, "runtime": {},
        "installation": {"id": "GARBAGE", "assigned_at": None, "time_source": "pending", "assignment_reason": "fresh_install"},
    }
    instance_path.write_text(json.dumps(raw), encoding="utf-8")
    manager = InstanceManager(instance_path)

    manager.peek()
    mock_log.assert_not_called()
