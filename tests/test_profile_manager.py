"""Tests for storage/profile_manager.py's ProfileManager - keeps each
accepted radio's data isolated under data/profiles/<node-id>/ (see
CLAUDE.md's "Multi-radio profiles" section). Pure filesystem + dict logic,
no server.py/hardware dependency of its own - takes a plain data_dir.
"""

import json
from pathlib import Path

import pytest

from storage.profile_manager import ProfileManager


def _radio(node_id="!75fea2aa", long_name="Flint TAP2", short_name="FTP2"):
    return {
        "node_id": node_id,
        "long_name": long_name,
        "short_name": short_name,
        "hardware": "RAK4631",
        "role": "CLIENT",
        "port": "/dev/ttyACM0",
    }


def test_profile_id_from_node_id_normalizes_case():
    assert ProfileManager.profile_id_from_node_id("!75FEA2AA") == "75fea2aa"


def test_profile_id_from_node_id_rejects_malformed_ids():
    for bad in ("not-a-node-id", "!short", "!toolong123456", "", None):
        with pytest.raises(ValueError):
            ProfileManager.profile_id_from_node_id(bad)


def test_ensure_profile_creates_isolated_directory_per_radio(tmp_path):
    manager = ProfileManager(tmp_path)

    context = manager.ensure_profile(_radio(), migrate_legacy=False)

    assert context["profile_id"] == "75fea2aa"
    assert (tmp_path / "profiles" / "75fea2aa").is_dir()
    assert (tmp_path / "profiles" / "75fea2aa" / "profile.json").is_file()
    assert context["metadata"]["radio"]["node_id"] == "!75fea2aa"
    assert context["metadata"]["radio"]["long_name"] == "Flint TAP2"


def test_ensure_profile_two_different_radios_get_separate_profiles(tmp_path):
    manager = ProfileManager(tmp_path)

    context_a = manager.ensure_profile(_radio(node_id="!75fea2aa", long_name="Radio A"), migrate_legacy=False)
    context_b = manager.ensure_profile(_radio(node_id="!aabbccdd", long_name="Radio B"), migrate_legacy=False)

    assert context_a["profile_id"] != context_b["profile_id"]
    assert context_a["profile_dir"] != context_b["profile_dir"]
    # Confirms profiles are actually isolated, not sharing a data file.
    assert context_a["paths"]["nodes"] != context_b["paths"]["nodes"]


def test_ensure_profile_is_idempotent_and_preserves_created_at(tmp_path):
    manager = ProfileManager(tmp_path)

    first = manager.ensure_profile(_radio(), migrate_legacy=False)
    second = manager.ensure_profile(_radio(), migrate_legacy=False)

    assert first["profile_id"] == second["profile_id"]
    # created_at must survive a repeat call (e.g. every server.py restart
    # against the same accepted radio) - only last_used_at should move.
    assert first["metadata"]["created_at"] == second["metadata"]["created_at"]


def test_ensure_profile_rejects_invalid_radio_node_id(tmp_path):
    manager = ProfileManager(tmp_path)

    with pytest.raises(ValueError):
        manager.ensure_profile(_radio(node_id="not-a-valid-id"), migrate_legacy=False)


def test_create_clean_profile_initializes_empty_state_files(tmp_path):
    manager = ProfileManager(tmp_path)

    profile = manager.create_clean_profile(_radio())

    profile_dir = tmp_path / "profiles" / "75fea2aa"
    assert (profile_dir / "messages.json").read_text(encoding="utf-8").strip() == "[]"
    assert (profile_dir / "nodes.json").read_text(encoding="utf-8").strip() == "{}"
    assert profile["profile_id"] == "75fea2aa"


def test_legacy_radio_dict_with_no_transport_key_normalizes_to_serial(tmp_path):
    """Every existing call site (detect_connected_radio()'s output,
    INSTANCE_IDENTITY.radio before this feature) hands ensure_profile() a
    bare dict with a flat `port` field and no transport/endpoint keys at
    all - must still produce a correctly-shaped stored record."""
    manager = ProfileManager(tmp_path)

    context = manager.ensure_profile(_radio(), migrate_legacy=False)

    assert context["metadata"]["radio"]["transport"] == "serial"
    assert context["metadata"]["radio"]["endpoint"] == {"port": "/dev/ttyACM0"}
    # The legacy flat field is preserved unchanged, not removed.
    assert context["metadata"]["radio"]["port"] == "/dev/ttyACM0"


def test_tcp_radio_profile_persists_transport_and_endpoint(tmp_path):
    manager = ProfileManager(tmp_path)
    radio = {
        "node_id": "!1fa065f0",
        "long_name": "T-Beam",
        "short_name": "TBM",
        "hardware": "TBEAM",
        "role": "CLIENT",
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }

    context = manager.ensure_profile(radio, migrate_legacy=False)

    assert context["profile_id"] == "1fa065f0"
    assert context["metadata"]["radio"]["transport"] == "tcp"
    assert context["metadata"]["radio"]["endpoint"] == {"host": "192.168.2.34", "port": 4403}

    # Round-trips through get_profile() too, not just the ensure_profile()
    # return value.
    reloaded = manager.get_profile("1fa065f0")
    assert reloaded["metadata"]["radio"]["transport"] == "tcp"
    assert reloaded["metadata"]["radio"]["endpoint"] == {"host": "192.168.2.34", "port": 4403}


# ---------------------------------------------------------------------------
# Radio Profiles & Connections Model, PR 1
# ---------------------------------------------------------------------------

def _tcp_radio(node_id="!1fa065f0", long_name="T-Beam", firmware_version="2.7.15.567b8ea"):
    return {
        "node_id": node_id,
        "long_name": long_name,
        "short_name": "TBM",
        "hardware": "TBEAM",
        "role": "CLIENT",
        "firmware_version": firmware_version,
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }


def test_one_node_id_over_tcp_gets_a_connections_entry(tmp_path):
    manager = ProfileManager(tmp_path)

    context = manager.ensure_profile(_tcp_radio(), migrate_legacy=False)

    radio = context["metadata"]["radio"]
    assert radio["connections"] == {"tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}}}
    assert radio["preferred_transport"] == "tcp"
    assert radio["firmware_version"] == "2.7.15.567b8ea"


def test_same_node_id_later_over_serial_reuses_the_profile_and_keeps_both_connections(tmp_path):
    """The actual scenario this PR fixes: accepting a radio over TCP,
    later connecting to the SAME physical radio over serial, must land
    in the same profile.json - not a duplicate, and not losing the
    earlier TCP connection either (the pre-existing directory-per-
    node_id behavior already prevented a duplicate profile; it never
    preserved the other transport's connections entry)."""
    manager = ProfileManager(tmp_path)

    tcp_context = manager.ensure_profile(_tcp_radio(), migrate_legacy=False)

    serial_radio = {
        "node_id": "!1fa065f0",
        "long_name": "T-Beam",
        "short_name": "TBM",
        "hardware": "TBEAM",
        "role": "CLIENT",
        "port": "/dev/ttyACM0",
    }
    serial_context = manager.ensure_profile(serial_radio, migrate_legacy=False)

    assert serial_context["profile_id"] == tcp_context["profile_id"] == "1fa065f0"
    radio = serial_context["metadata"]["radio"]
    assert radio["connections"]["tcp"] == {"endpoint": {"host": "192.168.2.34", "port": 4403}}
    assert radio["connections"]["serial"] == {"endpoint": {"port": "/dev/ttyACM0"}}
    # The most recent call's transport becomes current/preferred, matching
    # this function's pre-existing "last write wins" behavior for the
    # legacy singular fields.
    assert radio["preferred_transport"] == "serial"
    assert radio["transport"] == "serial"


def test_different_node_id_on_the_same_tcp_endpoint_gets_a_separate_profile_not_a_swap(tmp_path):
    """Persistence-layer guarantee only: this does NOT test radio
    identity verification (meshsrv/radio_identity.py's MISMATCH
    detection is a separate, unrelated concern, out of this PR's scope)
    - it tests that ProfileManager itself never lets a second node_id
    overwrite or merge into a first node_id's profile just because they
    share the same TCP address."""
    manager = ProfileManager(tmp_path)

    first = manager.ensure_profile(_tcp_radio(node_id="!1fa065f0", long_name="Real Radio"), migrate_legacy=False)
    second = manager.ensure_profile(_tcp_radio(node_id="!deadbeef", long_name="Different Radio"), migrate_legacy=False)

    assert first["profile_id"] != second["profile_id"]
    assert first["profile_dir"] != second["profile_dir"]

    reloaded_first = manager.get_profile(first["profile_id"])
    assert reloaded_first["metadata"]["radio"]["node_id"] == "!1fa065f0"
    assert reloaded_first["metadata"]["radio"]["long_name"] == "Real Radio"


def test_legacy_serial_profile_with_no_transport_key_normalizes_connections_correctly(tmp_path):
    manager = ProfileManager(tmp_path)

    context = manager.ensure_profile(_radio(), migrate_legacy=False)

    radio = context["metadata"]["radio"]
    assert radio["connections"] == {"serial": {"endpoint": {"port": "/dev/ttyACM0"}}}
    assert radio["preferred_transport"] == "serial"


def test_legacy_flat_tcp_profile_from_before_pr1_normalizes_into_connections_tcp(tmp_path):
    """Simulates a profile.json written by the pre-PR1 code (PR #278 era):
    flat `transport`/`endpoint`, no `connections` key at all. The next
    ensure_profile() call against it must synthesize connections.tcp
    correctly rather than treating the profile as brand new."""
    manager = ProfileManager(tmp_path)
    profile_dir = tmp_path / "profiles" / "1fa065f0"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.json").write_text(
        json.dumps({
            "schema_version": 1,
            "profile_id": "1fa065f0",
            "radio": {
                "node_id": "!1fa065f0",
                "long_name": "T-Beam",
                "short_name": "TBM",
                "hardware": "TBEAM",
                "role": "CLIENT",
                "port": "",
                "transport": "tcp",
                "endpoint": {"host": "192.168.2.34", "port": 4403},
            },
            "created_at": "2026-09-01T00:00:00+00:00",
            "last_used_at": "2026-09-01T00:00:00+00:00",
            "migration": {},
        }),
        encoding="utf-8",
    )

    context = manager.ensure_profile(_tcp_radio(), migrate_legacy=False)

    radio = context["metadata"]["radio"]
    assert radio["connections"] == {"tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}}}
    assert radio["preferred_transport"] == "tcp"
    assert context["metadata"]["created_at"] == "2026-09-01T00:00:00+00:00"


def test_preferred_transport_survives_a_simulated_restart(tmp_path):
    """ensure_profile() is called once per server.py boot with the
    already-accepted radio (see server.py's module-level PROFILE_CONTEXT
    assignment) - calling it again with the SAME transport (simulating a
    second boot) must not lose or reset preferred_transport."""
    manager = ProfileManager(tmp_path)

    manager.ensure_profile(_tcp_radio(), migrate_legacy=False)
    second_boot = manager.ensure_profile(_tcp_radio(), migrate_legacy=False)

    assert second_boot["metadata"]["radio"]["preferred_transport"] == "tcp"
    assert second_boot["metadata"]["radio"]["connections"]["tcp"] == {
        "endpoint": {"host": "192.168.2.34", "port": 4403}
    }


def test_get_profile_does_not_rewrite_the_file(tmp_path):
    manager = ProfileManager(tmp_path)
    context = manager.ensure_profile(_tcp_radio(), migrate_legacy=False)
    metadata_path = Path(context["profile_dir"]) / "profile.json"

    before_mtime = metadata_path.stat().st_mtime_ns
    before_content = metadata_path.read_bytes()

    manager.get_profile(context["profile_id"])

    assert metadata_path.stat().st_mtime_ns == before_mtime
    assert metadata_path.read_bytes() == before_content
