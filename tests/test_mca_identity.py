"""tests/test_mca_identity.py

Execution Plan Step 1.2: MCA principal creation (design spec section 7.1),
built on the confirmed ADR-0002 key-derivation scheme.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.identity import (
    IdentityError,
    compute_key_id,
    create_principal,
    derive_x25519_public,
    ensure_principal,
    load_principal,
    load_signing_key,
)
from meshsrv.attachments.workspace import MCAWorkspaceManager


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def workspace_manager(tmp_path):
    return MCAWorkspaceManager(tmp_path)


def test_create_principal_generates_ed25519_and_derived_x25519(conn, workspace_manager):
    principal = create_principal(conn, workspace_manager, "ws-1")
    assert len(principal.public_identity) == 32
    assert len(principal.public_x25519) == 32
    assert principal.public_x25519 == derive_x25519_public(principal.public_identity)
    assert principal.epoch == 0
    assert principal.status == "ACTIVE"


def test_principal_id_and_key_id_are_16_hex_chars_matching_workspace_regex(conn, workspace_manager):
    principal = create_principal(conn, workspace_manager, "ws-1")
    assert len(principal.principal_id) == 16
    assert len(principal.key_id) == 16
    assert principal.principal_id == principal.key_id  # genesis epoch only
    int(principal.principal_id, 16)  # must be valid hex


def test_key_id_matches_reference_formula(conn, workspace_manager):
    principal = create_principal(conn, workspace_manager, "ws-1")
    assert principal.key_id == compute_key_id(principal.public_identity)


def test_two_different_workspaces_get_two_different_principals(conn, workspace_manager):
    """Step 1.2 DoD, verbatim: two different MCA principals with unique
    key_ids must be creatable (standing in here for "on dev and prod" -
    two workspaces in one process is the same code path as two
    installations)."""
    dev = create_principal(conn, workspace_manager, "dev")
    prod = create_principal(conn, workspace_manager, "prod")
    assert dev.principal_id != prod.principal_id
    assert dev.key_id != prod.key_id
    assert dev.public_identity != prod.public_identity


def test_create_principal_twice_in_same_workspace_raises(conn, workspace_manager):
    create_principal(conn, workspace_manager, "ws-1")
    with pytest.raises(IdentityError):
        create_principal(conn, workspace_manager, "ws-1")


def test_ensure_principal_is_idempotent(conn, workspace_manager):
    first = ensure_principal(conn, workspace_manager, "ws-1")
    second = ensure_principal(conn, workspace_manager, "ws-1")
    assert first.principal_id == second.principal_id
    assert first.public_identity == second.public_identity


def test_load_principal_returns_none_before_creation(conn):
    assert load_principal(conn, "ws-never-enabled") is None


def test_private_key_file_is_0600_inside_keys_dir_which_is_0700(conn, workspace_manager):
    principal = create_principal(conn, workspace_manager, "ws-1")
    paths = workspace_manager.paths(principal.principal_id)
    key_path = paths.keys / principal.private_key_file
    assert key_path.exists()
    assert oct(paths.keys.stat().st_mode)[-3:] == "700"
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_load_signing_key_round_trips_and_matches_public_identity(conn, workspace_manager):
    principal = create_principal(conn, workspace_manager, "ws-1")
    signing_key = load_signing_key(workspace_manager, principal)
    assert bytes(signing_key.verify_key) == principal.public_identity


def test_load_signing_key_raises_if_public_identity_was_tampered(conn, workspace_manager):
    principal = create_principal(conn, workspace_manager, "ws-1")
    tampered = dataclasses.replace(principal, public_identity=b"\x00" * 32)
    with pytest.raises(IdentityError):
        load_signing_key(workspace_manager, tampered)


def test_load_signing_key_raises_if_key_file_missing(conn, workspace_manager, tmp_path):
    principal = create_principal(conn, workspace_manager, "ws-1")
    paths = workspace_manager.paths(principal.principal_id)
    (paths.keys / principal.private_key_file).unlink()
    with pytest.raises(IdentityError):
        load_signing_key(workspace_manager, principal)


def test_compute_key_id_rejects_wrong_length():
    with pytest.raises(IdentityError):
        compute_key_id(b"\x00" * 10)


def test_derive_x25519_public_rejects_wrong_length():
    with pytest.raises(IdentityError):
        derive_x25519_public(b"\x00" * 10)
