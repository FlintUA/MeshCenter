"""Tests for storage/profile_manager.py's ProfileManager - keeps each
accepted radio's data isolated under data/profiles/<node-id>/ (see
CLAUDE.md's "Multi-radio profiles" section). Pure filesystem + dict logic,
no server.py/hardware dependency of its own - takes a plain data_dir.
"""

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
