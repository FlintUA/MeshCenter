"""tests/test_mca_workspace.py

Storage-block tests for `MCAWorkspaceManager` (Execution Plan Step 0.6,
design spec section 23.1 "Storage": quota, low disk, filename traversal,
duplicate name, workspace isolation when the connector changes) plus the
Step 0.6 DoD's repo-wide grep test for stray `data/mca` path construction.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from meshsrv.attachments.workspace import (
    LowDiskLevel,
    MCAWorkspaceManager,
    WorkspacePathError,
)

PRINCIPAL_A = "0123456789abcdef"
PRINCIPAL_B = "fedcba9876543210"


@pytest.fixture
def manager(tmp_path):
    return MCAWorkspaceManager(tmp_path / "data")


def test_paths_are_transport_neutral_and_under_data_mca(manager, tmp_path):
    paths = manager.paths(PRINCIPAL_A)
    expected_root = tmp_path / "data" / "mca" / PRINCIPAL_A
    assert paths.root == expected_root
    assert paths.attachments_db == expected_root / "attachments.db"
    assert paths.spool_outgoing == expected_root / "spool" / "outgoing"
    assert paths.cache_incoming == expected_root / "cache" / "incoming"
    assert paths.files == expected_root / "files"
    assert paths.quarantine == expected_root / "quarantine"
    assert paths.keys == expected_root / "keys"
    # Never anywhere under data/profiles/ (design spec section 16.2).
    assert "profiles" not in expected_root.parts


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits not meaningful on Windows")
def test_ensure_workspace_creates_directories_and_locks_keys_dir(manager):
    paths = manager.ensure_workspace(PRINCIPAL_A)
    for directory in (paths.root, paths.spool_outgoing, paths.cache_incoming, paths.files, paths.quarantine, paths.keys):
        assert directory.is_dir()
    mode = stat.S_IMODE(os.stat(paths.keys).st_mode)
    assert mode == 0o700


def test_ensure_workspace_is_idempotent(manager):
    manager.ensure_workspace(PRINCIPAL_A)
    paths_again = manager.ensure_workspace(PRINCIPAL_A)  # must not raise
    assert paths_again.root.is_dir()


@pytest.mark.parametrize(
    "bad_principal_id",
    ["", "not-hex", "0123456789abcde", "../../../etc/passwd", "0123456789ABCDEF!", "0123456789abcdef/extra"],
)
def test_rejects_invalid_principal_id(manager, bad_principal_id):
    with pytest.raises(WorkspacePathError):
        manager.paths(bad_principal_id)


def test_two_workspaces_are_fully_isolated(manager):
    paths_a = manager.ensure_workspace(PRINCIPAL_A)
    paths_b = manager.ensure_workspace(PRINCIPAL_B)
    assert paths_a.root != paths_b.root
    (paths_a.files / "secret.txt").write_bytes(b"a-only")
    assert not (paths_b.files / "secret.txt").exists()


def test_workspace_paths_do_not_depend_on_connector_or_adapter(manager):
    """Design spec section 16.2: an MCA workspace/principal survives a
    connector switch (Meshtastic -> MeshCore) unchanged. `paths()` takes no
    adapter/connector argument at all, which is the point - calling it
    repeatedly for the same principal_id, regardless of whatever connector
    context the caller happens to be in, must always resolve to the exact
    same archive."""
    first_call = manager.paths(PRINCIPAL_A)
    second_call = manager.paths(PRINCIPAL_A)
    assert first_call == second_call


@pytest.mark.parametrize(
    "hostile_name",
    ["../../etc/passwd", "..\\..\\windows\\system32\\config", "../secret.txt", "/etc/passwd", "..", "."],
)
def test_resolve_saved_path_rejects_traversal(manager, hostile_name):
    manager.ensure_workspace(PRINCIPAL_A)
    with pytest.raises(WorkspacePathError):
        manager.resolve_saved_path(PRINCIPAL_A, hostile_name)


def test_resolve_saved_path_accepts_a_plain_file_name(manager):
    manager.ensure_workspace(PRINCIPAL_A)
    resolved = manager.resolve_saved_path(PRINCIPAL_A, "photo.jpg")
    assert resolved.parent == manager.paths(PRINCIPAL_A).files.resolve()
    assert resolved.name == "photo.jpg"


def test_unique_file_name_avoids_overwriting_existing_file(manager):
    paths = manager.ensure_workspace(PRINCIPAL_A)
    (paths.files / "photo.jpg").write_bytes(b"first")
    second = manager.unique_file_name(PRINCIPAL_A, "photo.jpg")
    assert second.name == "photo (2).jpg"
    second.write_bytes(b"second")
    third = manager.unique_file_name(PRINCIPAL_A, "photo.jpg")
    assert third.name == "photo (3).jpg"


def test_quota_check(manager):
    paths = manager.ensure_workspace(PRINCIPAL_A)
    (paths.files / "a.bin").write_bytes(b"x" * 1000)
    assert manager.is_over_quota(PRINCIPAL_A, max_bytes=500) is True
    assert manager.is_over_quota(PRINCIPAL_A, max_bytes=2000) is False
    # Accounts for a not-yet-written incoming file too.
    assert manager.is_over_quota(PRINCIPAL_A, max_bytes=1500, additional_bytes=600) is True


class _Usage:
    def __init__(self, total, free):
        self.total = total
        self.free = free


def test_low_disk_level(manager, monkeypatch):
    manager.ensure_workspace(PRINCIPAL_A)

    monkeypatch.setattr(
        "meshsrv.attachments.workspace.shutil.disk_usage", lambda _p: _Usage(total=100, free=50)
    )
    assert manager.low_disk_level(PRINCIPAL_A) == LowDiskLevel.OK

    monkeypatch.setattr(
        "meshsrv.attachments.workspace.shutil.disk_usage", lambda _p: _Usage(total=100, free=10)
    )
    assert manager.low_disk_level(PRINCIPAL_A) == LowDiskLevel.WARN

    monkeypatch.setattr(
        "meshsrv.attachments.workspace.shutil.disk_usage", lambda _p: _Usage(total=100, free=3)
    )
    assert manager.low_disk_level(PRINCIPAL_A) == LowDiskLevel.BLOCK


def test_no_stray_data_mca_path_construction():
    """Step 0.6 DoD: no path under data/mca/ may be computed anywhere in
    the codebase except inside MCAWorkspaceManager itself. A grep-level
    check, not a type-level guarantee, but it is exactly what the
    execution plan asks for."""
    repo_root = Path(__file__).resolve().parents[1]
    allowed_files = {
        (repo_root / "meshsrv" / "attachments" / "workspace.py").resolve(),
        (repo_root / "docs" / "architecture" / "ADR-0003-attachments-sqlite-exception.md").resolve(),
        Path(__file__).resolve(),
    }
    skip_dir_names = {".git", "node_modules", "__pycache__", "venv", ".venv"}
    offenders = []
    for path in repo_root.rglob("*.py"):
        if path.resolve() in allowed_files:
            continue
        if any(part in skip_dir_names for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "data/mca" in text or "data\\mca" in text:
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == [], f"found data/mca path construction outside MCAWorkspaceManager: {offenders}"
