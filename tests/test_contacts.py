"""tests/test_contacts.py -- meshsrv/attachments/contacts.py (ADR-0008
decision 4, Step 1.6A backend layer).

This module is a thin translation layer over key_exchange.py's already-
tested TOFU/key-change machinery, so these tests deliberately don't
re-derive every rate-limit/wire-format edge case test_key_exchange.py
already covers - they check the translation itself: the AddressStatus ->
ContactStatus mapping, that confirm_binding()/accept_key_change()/
reject_key_change() actually delegate to the right KeyExchangeCoordinator
method with the right arguments, and that a KeyExchangeError at that
boundary comes back out as a ContactError, not a leaked internal type.
"""

from __future__ import annotations

import sqlite3

import pytest

from meshsrv.attachments import codec
from meshsrv.attachments.contacts import (
    ContactError,
    ContactStatus,
    accept_key_change,
    confirm_binding,
    contact_status,
    reject_key_change,
)
from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.delivery.base import DeliveryEnvelope, RouteType, WireFormat
from meshsrv.attachments.identity import create_principal, load_signing_key
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.workspace import MCAWorkspaceManager


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return _Clock()


def _make_node(tmp_path, name, now_fn):
    db_dir = tmp_path / name
    db_dir.mkdir()
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    workspace_manager = MCAWorkspaceManager(db_dir)
    principal = create_principal(conn, workspace_manager, f"ws-{name}", now=now_fn())
    coordinator = KeyExchangeCoordinator(conn, workspace_manager, principal, "fake-text", now_fn=now_fn)
    return conn, workspace_manager, principal, coordinator


def _direct_envelope(logical_message: bytes, source_address: str, now: float) -> DeliveryEnvelope:
    return DeliveryEnvelope(
        logical_message=logical_message,
        wire_format=WireFormat.MCA1_TEXT,
        adapter_id="fake-text",
        connector_profile_id="fake-connector",
        route_type=RouteType.DIRECT,
        route_id=source_address,
        source_address=source_address,
        external_message_id=None,
        received_at=now,
    )


def _announce_from(principal, workspace_manager, *, epoch=0):
    signing_key = load_signing_key(workspace_manager, principal)
    return codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=principal.public_identity, epoch=epoch), signing_key
    )


# ---- contact_status() mapping ---------------------------------------------


def test_contact_status_key_unknown_before_any_announce(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    assert contact_status(coordinator, "!never-seen") == ContactStatus.KEY_UNKNOWN


def test_contact_status_confirmation_required_after_unconfirmed_announce(tmp_path, clock):
    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, wsm_b, principal_b, _ = _make_node(tmp_path, "b", clock)
    coordinator_a.handle_incoming(_direct_envelope(_announce_from(principal_b, wsm_b), "!node-b", clock()))
    assert contact_status(coordinator_a, "!node-b") == ContactStatus.CONFIRMATION_REQUIRED


def test_contact_status_trusted_after_confirm_binding(tmp_path, clock):
    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, wsm_b, principal_b, _ = _make_node(tmp_path, "b", clock)
    coordinator_a.handle_incoming(_direct_envelope(_announce_from(principal_b, wsm_b), "!node-b", clock()))
    confirm_binding(coordinator_a, "!node-b")
    assert contact_status(coordinator_a, "!node-b") == ContactStatus.TRUSTED


def test_contact_status_key_changed_after_conflicting_announce(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, wsm_b, principal_b, _ = _make_node(tmp_path, "b", clock)
    coordinator_a.handle_incoming(_direct_envelope(_announce_from(principal_b, wsm_b), "!node-b", clock()))
    confirm_binding(coordinator_a, "!node-b")

    impostor_key = SigningKey.generate()
    impostor_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=bytes(impostor_key.verify_key), epoch=0), impostor_key
    )
    coordinator_a.handle_incoming(_direct_envelope(impostor_announce, "!node-b", clock()))
    assert contact_status(coordinator_a, "!node-b") == ContactStatus.KEY_CHANGED


# ---- confirm_binding() -----------------------------------------------------


def test_confirm_binding_delegates_to_confirm_tofu(tmp_path, clock):
    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, wsm_b, principal_b, _ = _make_node(tmp_path, "b", clock)
    coordinator_a.handle_incoming(_direct_envelope(_announce_from(principal_b, wsm_b), "!node-b", clock()))

    confirm_binding(coordinator_a, "!node-b")

    binding = coordinator_a.get_binding("!node-b")
    assert binding.tofu_confirmed_at is not None


def test_confirm_binding_refuses_when_key_is_unknown(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    with pytest.raises(ContactError):
        confirm_binding(coordinator, "!never-seen")


# ---- accept_key_change() / reject_key_change() -----------------------------


def test_accept_key_change_delegates_and_requires_fresh_confirmation(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, wsm_b, principal_b, _ = _make_node(tmp_path, "b", clock)
    coordinator_a.handle_incoming(_direct_envelope(_announce_from(principal_b, wsm_b), "!node-b", clock()))
    confirm_binding(coordinator_a, "!node-b")

    new_key = SigningKey.generate()
    new_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=bytes(new_key.verify_key), epoch=1), new_key
    )
    coordinator_a.handle_incoming(_direct_envelope(new_announce, "!node-b", clock()))

    accept_key_change(coordinator_a, "!node-b")

    assert contact_status(coordinator_a, "!node-b") == ContactStatus.CONFIRMATION_REQUIRED
    binding = coordinator_a.get_binding("!node-b")
    assert binding.public_identity == bytes(new_key.verify_key)


def test_accept_key_change_without_a_pending_change_raises_contact_error(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    with pytest.raises(ContactError):
        accept_key_change(coordinator, "!nobody")


def test_reject_key_change_delegates_and_leaves_binding_untouched(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, wsm_b, principal_b, _ = _make_node(tmp_path, "b", clock)
    coordinator_a.handle_incoming(_direct_envelope(_announce_from(principal_b, wsm_b), "!node-b", clock()))
    confirm_binding(coordinator_a, "!node-b")

    impostor_key = SigningKey.generate()
    impostor_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=bytes(impostor_key.verify_key), epoch=0), impostor_key
    )
    coordinator_a.handle_incoming(_direct_envelope(impostor_announce, "!node-b", clock()))

    reject_key_change(coordinator_a, "!node-b")

    assert contact_status(coordinator_a, "!node-b") == ContactStatus.TRUSTED
    binding = coordinator_a.get_binding("!node-b")
    assert binding.public_identity == principal_b.public_identity


def test_reject_key_change_without_a_pending_change_raises_contact_error(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    with pytest.raises(ContactError):
        reject_key_change(coordinator, "!nobody")
