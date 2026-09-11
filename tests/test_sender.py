"""tests/test_sender.py -- meshsrv/attachments/sender.py (Step 1.4).

In-process tests (no subprocess/real HTTP - see test_sender_crash_recovery.py
for the scripted kill-9 tests) covering the state machine's normal-path
transitions, validation failures, the ACK-driven SENT->RECEIVED->DOWNLOADED
tail, cancel(), resume_pending(), and the create_upload/409 orphaned-session
recovery path.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3

import pytest
from nacl.signing import SigningKey

from meshsrv.attachments import codec, identity, manifest, sender
from meshsrv.attachments.db import migrations
from meshsrv.attachments.delivery.fakes import FakeTextAdapter, InMemoryEther
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.attachments.relay_client import RelayClient, RelayHTTPError
from meshsrv.attachments.workspace import MCAWorkspaceManager


class _ResponseShim:
    def __init__(self, flask_response):
        self._flask_response = flask_response
        self.status_code = flask_response.status_code
        self.headers = flask_response.headers
        self.content = flask_response.data
        self.text = flask_response.get_data(as_text=True)

    def json(self):
        return self._flask_response.get_json()


class _FlaskTestClientSession:
    def __init__(self, flask_test_client, base_url: str):
        self._client = flask_test_client
        self._base_url = base_url

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        path = url[len(self._base_url):]
        flask_response = self._client.open(path, method=method, json=json, data=data, headers=headers or {})
        return _ResponseShim(flask_response)


BASE_URL = "https://mock-relay.test"


@pytest.fixture
def store():
    return MockRelayStore(base_url=BASE_URL)


@pytest.fixture
def relay_client(store):
    app = create_mock_relay_app(store)
    session = _FlaskTestClientSession(app.test_client(), BASE_URL)
    return RelayClient(BASE_URL, upload_access_token=store.upload_access_token, session=session, sleep=lambda _s: None)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "attachments.db"))
    c.execute("PRAGMA foreign_keys = ON")
    migrations.migrate(c)
    return c


@pytest.fixture
def wsm(tmp_path):
    return MCAWorkspaceManager(str(tmp_path / "data"))


@pytest.fixture
def principal(conn, wsm):
    return identity.ensure_principal(conn, wsm, "local")


@pytest.fixture
def recipient():
    sk = SigningKey.generate()
    pub = bytes(sk.verify_key)
    key_id = identity.compute_key_id(pub)
    return sk, pub, key_id


def _draft(conn, wsm, principal, recipient, tmp_path, *, file_name="a.txt", mime_type="text/plain", content=b"hello" * 1000, adapter_id="fake-text", route_id="receiver", comment=None):
    _, pub, key_id = recipient
    source_path = tmp_path / file_name
    source_path.write_bytes(content)
    attachment_id = sender.create_draft(
        conn, wsm, principal,
        workspace_id="local",
        source_path=str(source_path),
        file_name=file_name,
        mime_type=mime_type,
        recipients=[sender.RecipientTarget(public_identity=pub, key_id=key_id)],
        adapter_id=adapter_id,
        connector_profile_id="default",
        route_type="DIRECT",
        route_id=route_id,
        provider_id=b"\x01" * 8,
        comment=comment,
    )
    return attachment_id, str(source_path)


def _decrypt_sender_manifest_header(conn, attachment_id, recipient):
    """Decrypts the header of the manifest sender.py has already built and
    persisted to mca_sender_state for `attachment_id`, using the
    recipient's own private key - exactly what a real receiver would do,
    but reading the manifest_blob straight out of local sender-side state
    instead of round-tripping it through a mock Relay + receiver.py."""

    _, _, key_id = recipient
    sk = recipient[0]
    row = conn.execute("SELECT plain_size FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    state_row = sender._get_sender_state(conn, attachment_id)
    parsed = manifest.parse_manifest_blob(state_row["manifest_blob"])
    envelope = manifest.find_recipient_envelope(parsed, bytes.fromhex(key_id))
    secret = manifest.open_recipient_secret(envelope.sealed_envelope, sk)
    return manifest.decrypt_manifest_header(
        parsed, data_key=secret.data_key, nonce_prefix=secret.nonce_prefix,
        chunk_count=secret.chunk_count, plain_size=row["plain_size"],
    )


def _drive_to(conn, wsm, principal, recipient, relay_client, delivery_adapter, attachment_id, target_states, max_steps=20):
    _, pub, key_id = recipient
    recipient_identities = {key_id: pub}
    for _ in range(max_steps):
        state = sender.get_state(conn, attachment_id)
        if state in target_states:
            return state
        new_state = sender.run_step(
            conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=delivery_adapter, attachment_id=attachment_id,
        )
        if new_state == state:
            return state
    return sender.get_state(conn, attachment_id)


def test_create_draft_starts_in_draft_with_recipient_and_delivery_rows(conn, wsm, principal, recipient, tmp_path):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    assert sender.get_state(conn, attachment_id) == sender.DRAFT
    assert len(sender._recipients_for(conn, attachment_id)) == 1
    assert sender._delivery_for(conn, attachment_id)["route_id"] == "receiver"


def test_create_draft_stores_provider_id_as_base64url_not_hex(conn, wsm, principal, recipient, tmp_path):
    """Regression test for a reviewer-found, locally-reproduced defect:
    `attachments.provider_id` used to be stored as hex for 'sent' rows
    while `ProviderRegistry` (and 'received' rows) key everything by
    Base64URL text - `ProviderRegistry.remove_or_disable()`'s "is this
    provider_id still referenced?" check compares against the Base64URL
    form, so it never matched a 'sent' row and could delete a Relay
    profile a real outgoing attachment still depended on. This drives the
    real `sender.create_draft()` entry point (not a raw INSERT standing in
    for it) and checks the actual stored column."""
    from meshsrv.attachments.provider_registry import encode_provider_id

    raw_provider_id = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    # _draft()'s own default provider_id (b"\x01" * 8) exercises the same
    # code path; assert against the real one this test cares about by
    # creating a second draft with a distinct, easily-recognized value.
    source_path = tmp_path / "b.txt"
    source_path.write_bytes(b"second")
    _, pub, key_id = recipient
    attachment_id_2 = sender.create_draft(
        conn, wsm, principal, workspace_id="local", source_path=str(source_path), file_name="b.txt",
        mime_type="text/plain", recipients=[sender.RecipientTarget(public_identity=pub, key_id=key_id)],
        adapter_id="fake-text", connector_profile_id="default", route_type="DIRECT", route_id="receiver",
        provider_id=raw_provider_id,
    )
    stored = conn.execute(
        "SELECT provider_id FROM attachments WHERE id = ?", (attachment_id_2,)
    ).fetchone()[0]
    assert stored == encode_provider_id(raw_provider_id)
    assert stored != raw_provider_id.hex()


def test_remove_or_disable_recognizes_a_real_create_draft_attachment_as_in_use(conn, wsm, principal, recipient, tmp_path):
    """The end-to-end version of the regression above, going through
    `ProviderRegistry.remove_or_disable()` itself - this is the exact
    scenario the reviewer reproduced: register a provider, create a real
    outgoing draft against it via `sender.create_draft()`, then confirm
    `remove_or_disable()` correctly sees it as in-use (disables rather
    than deletes) instead of silently missing it due to an encoding
    mismatch."""
    from meshsrv.attachments.provider_registry import ProviderRegistry

    registry = ProviderRegistry(conn, "local")
    profile = registry.register(
        display_name="Real Relay", base_url="https://real.example.net",
        service_public_key=b"\xab" * 32, max_ciphertext_bytes=6 * 1024 * 1024,
    )
    raw_provider_id = base64_decode_provider_id(profile.provider_id)
    _draft(conn, wsm, principal, recipient, tmp_path)  # unrelated draft, default provider_id
    source_path = tmp_path / "c.txt"
    source_path.write_bytes(b"third")
    _, pub, key_id = recipient
    sender.create_draft(
        conn, wsm, principal, workspace_id="local", source_path=str(source_path), file_name="c.txt",
        mime_type="text/plain", recipients=[sender.RecipientTarget(public_identity=pub, key_id=key_id)],
        adapter_id="fake-text", connector_profile_id="default", route_type="DIRECT", route_id="receiver",
        provider_id=raw_provider_id,
    )

    result = registry.remove_or_disable(profile.provider_id, wsm, principal.principal_id)
    assert result == "disabled"
    still_there = registry.resolve(profile.provider_id)
    assert still_there is not None and still_there.enabled is False


def base64_decode_provider_id(provider_id_text: str) -> bytes:
    from meshsrv.attachments.provider_registry import decode_provider_id

    return decode_provider_id(provider_id_text)


def test_create_draft_requires_at_least_one_recipient(conn, wsm, principal, tmp_path):
    source_path = tmp_path / "a.txt"
    source_path.write_bytes(b"x")
    with pytest.raises(sender.SenderError):
        sender.create_draft(
            conn, wsm, principal, workspace_id="local", source_path=str(source_path), file_name="a.txt",
            mime_type="text/plain", recipients=[], adapter_id="fake-text", connector_profile_id="default",
            route_type="DIRECT", route_id="r", provider_id=b"\x01" * 8,
        )


def test_full_happy_path_reaches_sent_then_downloaded(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, content=os.urandom(300_000))
    ether = InMemoryEther()
    sender_adapter = FakeTextAdapter(ether, "sender-addr")

    final = _drive_to(conn, wsm, principal, recipient, relay_client, sender_adapter, attachment_id, {sender.SENT})
    assert final == sender.SENT

    events = ether.drain("receiver")
    assert len(events) == 1
    assert events[0]["text"].startswith("MCA1:")

    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_RECEIVED) == sender.RECEIVED
    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_DOWNLOADED) == sender.DOWNLOADED

    # sender-side transient key material is cleared once fully downloaded
    assert sender._get_sender_state(conn, attachment_id) is None

    # ... but the retained revoke capability survives the download, so a
    # post-download revoke is still possible (ADR-0009 Decision 5).
    revoke_row = conn.execute(
        "SELECT revoke_token FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()
    assert revoke_row is not None and revoke_row[0] is not None


def test_validation_fails_on_missing_source_file(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, source_path = _draft(conn, wsm, principal, recipient, tmp_path)
    os.remove(source_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    final = _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, sender.TERMINAL_STATES)
    assert final == sender.FAILED_VALIDATION


def test_validation_fails_on_disallowed_mime_type(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, mime_type="application/x-msdownload")
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    final = _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, sender.TERMINAL_STATES)
    assert final == sender.FAILED_VALIDATION


def test_queued_upload_stays_put_when_network_unavailable(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    recipient_identities = {recipient[2]: recipient[1]}
    # drive to QUEUED_UPLOAD
    for _ in range(5):
        if sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD:
            break
        sender.run_step(
            conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
        )
    assert sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD

    new_state = sender.run_step(
        conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
        relay_client=relay_client, delivery_adapter=None, network_available=False, attachment_id=attachment_id,
    )
    assert new_state == sender.QUEUED_UPLOAD


def test_ack_received_on_draft_is_dropped_not_raised(conn, wsm, principal, recipient, tmp_path):
    """ADR-0009 Decision 4: apply_ack() is monotonic and idempotent - a stray
    ACK_RECEIVED against a still-DRAFT attachment is *dropped* (state
    unchanged, no exception), never treated as a caller error. Mesh delivery
    is unordered, so a real wire consumer will hit out-of-order ACKs and must
    not blow up."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    assert sender.get_state(conn, attachment_id) == sender.DRAFT
    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_RECEIVED) == sender.DRAFT
    assert sender.get_state(conn, attachment_id) == sender.DRAFT


def test_ack_downloaded_on_sent_applies_received_then_downloaded(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0009 Decision 4: ACK_DOWNLOADED arriving without a preceding
    ACK_RECEIVED (lost/reordered on the mesh) still carries SENT all the way
    to DOWNLOADED - the exact tolerance the old on_ack_downloaded() (which
    required state == RECEIVED exactly and raised otherwise) did not have."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_DOWNLOADED) == sender.DOWNLOADED
    assert sender.get_state(conn, attachment_id) == sender.DOWNLOADED


def test_create_draft_fails_closed_on_non_32_byte_identity(conn, wsm, principal, recipient, tmp_path):
    """ADR-0009 Decision 2/6: a draft handed a public_identity that is not
    exactly 32 bytes is refused now (it could never be ACK-verified later),
    not stored as an unverifiable transfer."""
    _, pub, key_id = recipient
    source_path = tmp_path / "a.txt"
    source_path.write_bytes(b"hello")
    with pytest.raises(sender.SenderError):
        sender.create_draft(
            conn, wsm, principal, workspace_id="local", source_path=str(source_path), file_name="a.txt",
            mime_type="text/plain", recipients=[sender.RecipientTarget(public_identity=b"\x00" * 31, key_id=key_id)],
            adapter_id="fake-text", connector_profile_id="default", route_type="DIRECT", route_id="r",
            provider_id=b"\x01" * 8,
        )


def test_create_draft_fails_closed_on_mismatched_key_id(conn, wsm, principal, recipient, tmp_path):
    """ADR-0009 Decision 2/6: a draft whose recipient key_id does not derive
    from its public_identity is refused - the pinned-key check at ACK time
    would never match, so the draft fails closed at creation instead."""
    _, pub, _ = recipient
    source_path = tmp_path / "a.txt"
    source_path.write_bytes(b"hello")
    with pytest.raises(sender.SenderError):
        sender.create_draft(
            conn, wsm, principal, workspace_id="local", source_path=str(source_path), file_name="a.txt",
            mime_type="text/plain", recipients=[sender.RecipientTarget(public_identity=pub, key_id="deadbeefdeadbeef")],
            adapter_id="fake-text", connector_profile_id="default", route_type="DIRECT", route_id="r",
            provider_id=b"\x01" * 8,
        )


def test_create_draft_fails_closed_on_multi_recipient(conn, wsm, principal, recipient, tmp_path):
    """ADR-0009 Stage 1 scope: exactly one recipient, one DIRECT delivery.
    A multi-recipient draft has no inbound-ACK semantics defined yet, so it
    fails closed rather than being half-supported."""
    _, pub, key_id = recipient
    source_path = tmp_path / "a.txt"
    source_path.write_bytes(b"hello")
    with pytest.raises(sender.SenderError):
        sender.create_draft(
            conn, wsm, principal, workspace_id="local", source_path=str(source_path), file_name="a.txt",
            mime_type="text/plain",
            recipients=[
                sender.RecipientTarget(public_identity=pub, key_id=key_id),
                sender.RecipientTarget(public_identity=pub, key_id=key_id),
            ],
            adapter_id="fake-text", connector_profile_id="default", route_type="DIRECT", route_id="r",
            provider_id=b"\x01" * 8,
        )


def test_revoke_state_materialized_on_ready_to_send(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0009 Decision 5: the retained revoke capability is written at the
    READY_TO_SEND transition (the first point both revoke_token and
    hard_expires_at are known), so a later ACK_DOWNLOADED can drop the
    transient mca_sender_state without destroying the revoke token."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    assert sender.get_state(conn, attachment_id) == sender.SENT

    row = conn.execute(
        "SELECT revoke_token, delete_after, created_at, updated_at "
        "FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()
    assert row is not None
    revoke_token, delete_after, created_at, updated_at = row
    # The token matches the transient row's own; delete_after is a decimal
    # epoch-string bound (hard_expires_at + download_grace).
    assert revoke_token == sender._get_sender_state(conn, attachment_id)["revoke_token"]
    assert isinstance(delete_after, str) and delete_after.isdigit()
    assert int(delete_after) > 0
    assert created_at is not None and updated_at is not None


def test_ack_downloaded_retains_revoke_state_after_sender_state_dropped(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0009 Decision 5: the ACK_DOWNLOADED path deletes the transient
    mca_sender_state row (its encryption/upload secrets are now useless) but
    *preserves* mca_sender_revoke_state, so a post-download revoke still has
    its token."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    revoke_token_before = sender._get_sender_state(conn, attachment_id)["revoke_token"]

    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_DOWNLOADED) == sender.DOWNLOADED
    assert sender._get_sender_state(conn, attachment_id) is None  # transient row gone

    retained = conn.execute(
        "SELECT revoke_token FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()
    assert retained is not None and retained[0] == revoke_token_before


def test_ack_provider_unknown_is_non_terminal_and_recovers(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0009 Decision 4a: ACK_PROVIDER_UNKNOWN leaves the attachment SENT
    (not a terminal state) with a non-terminal error_code, and a later
    ACK_RECEIVED/ACK_DOWNLOADED still transitions cleanly."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})

    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_PROVIDER_UNKNOWN) == sender.SENT
    assert sender.get_state(conn, attachment_id) == sender.SENT
    row = conn.execute("SELECT error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row[0] == sender.PROVIDER_UNKNOWN_ERROR
    # Retained upload + revoke capability - the object may still be fetched.
    assert sender._get_sender_state(conn, attachment_id) is not None

    # A later ACK_RECEIVED still transitions cleanly (never regressed).
    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_RECEIVED) == sender.RECEIVED


def test_cancel_deletes_revoke_state(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0009 Decision 5a: a confirmed cancel retires both secret tables -
    the transient row and the retained revoke capability."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone() is not None

    # cancel() from SENT raises (only pre-send drafts are cancellable) - use a
    # fresh draft that hasn't reached SENT, and drive it only to READY_TO_SEND
    # so a revoke-state row exists before the cancel.
    attachment_id2, _ = _draft(conn, wsm, principal, recipient, tmp_path, file_name="b.txt", route_id="receiver2")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id2, {sender.READY_TO_SEND})
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id2,)
    ).fetchone() is not None

    assert sender.cancel(conn, attachment_id2) == sender.CANCELLED
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id2,)
    ).fetchone() is None


def test_revoke_deletes_revoke_state(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0009 Decision 5a: a confirmed revoke retires both secret tables."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone() is not None

    assert sender.revoke(conn, attachment_id) == sender.REVOKED
    assert sender._get_sender_state(conn, attachment_id) is None
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone() is None


def test_apply_rejected_transitions_sent_to_rejected_and_retains_revoke_state(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0010 Decision 5: an inbound REJECTED (recipient declined the offer)
    moves SENT -> REJECTED (terminal) and drops only the transient
    mca_sender_state row; the retained mca_sender_revoke_state row survives so
    the revoke capability stays durable until bounded cleanup retires it at
    delete_after (or a future confirmed Relay revoke removes it)."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    revoke_token_before = sender._get_sender_state(conn, attachment_id)["revoke_token"]
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone() is not None

    assert sender.apply_rejected(conn, attachment_id) == sender.REJECTED
    assert sender.get_state(conn, attachment_id) == sender.REJECTED
    assert sender.is_terminal(sender.REJECTED)
    assert sender._get_sender_state(conn, attachment_id) is None  # transient dropped
    retained = conn.execute(
        "SELECT revoke_token FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()
    assert retained is not None and retained[0] == revoke_token_before  # revoke capability preserved


def test_apply_expired_transitions_sent_to_expired_and_retains_revoke_state(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0010 Decision 5: an inbound EXPIRED (past the sender's authoritative
    hard_expires_at) moves SENT -> EXPIRED (terminal) and drops only the
    transient mca_sender_state row, retaining mca_sender_revoke_state exactly
    like apply_rejected()."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    revoke_token_before = sender._get_sender_state(conn, attachment_id)["revoke_token"]
    hard_expires_at = conn.execute(
        "SELECT hard_expires_at FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()[0]
    assert hard_expires_at > 0

    assert sender.apply_expired(conn, attachment_id, now=hard_expires_at + 100) == sender.EXPIRED
    assert sender.get_state(conn, attachment_id) == sender.EXPIRED
    assert sender._get_sender_state(conn, attachment_id) is None  # transient dropped
    retained = conn.execute(
        "SELECT revoke_token FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()
    assert retained is not None and retained[0] == revoke_token_before  # revoke capability preserved


def test_apply_expired_rejects_before_authoritative_hard_expiry_then_accepts_at_boundary(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0010 Decision 2: an inbound EXPIRED arriving *before* the sender's
    authoritative hard_expires_at is dropped with no state change - the
    sender's own expiry timestamp, not the recipient's observation of it, is
    authoritative. At the exact boundary it is accepted."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    hard_expires_at = conn.execute(
        "SELECT hard_expires_at FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()[0]
    assert hard_expires_at > 0

    # Before the boundary: dropped, state and both secret tables unchanged.
    assert sender.apply_expired(conn, attachment_id, now=hard_expires_at - 1) == sender.SENT
    assert sender.get_state(conn, attachment_id) == sender.SENT
    assert sender._get_sender_state(conn, attachment_id) is not None
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone() is not None

    # Exact boundary: accepted.
    assert sender.apply_expired(conn, attachment_id, now=hard_expires_at) == sender.EXPIRED
    assert sender.get_state(conn, attachment_id) == sender.EXPIRED


def test_apply_expired_accepts_after_boundary_and_duplicate_is_noop(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0010 Decision 2/4: an EXPIRED after the boundary is applied; a
    duplicate/stale EXPIRED against the now-terminal row is a no-op (never
    regresses, never raises)."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    hard_expires_at = conn.execute(
        "SELECT hard_expires_at FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()[0]

    assert sender.apply_expired(conn, attachment_id, now=hard_expires_at + 100) == sender.EXPIRED
    assert sender.get_state(conn, attachment_id) == sender.EXPIRED
    # Duplicate (later, still after the boundary): unchanged, still terminal.
    assert sender.apply_expired(conn, attachment_id, now=hard_expires_at + 200) == sender.EXPIRED
    assert sender.get_state(conn, attachment_id) == sender.EXPIRED


def test_apply_expired_fails_closed_when_hard_expires_at_is_null():
    """ADR-0010 Decision 2 (fail closed): with no authoritative deadline
    (hard_expires_at NULL) an inbound EXPIRED is dropped - no state,
    secret-table, or timeline mutation, even at a `now` far past any plausible
    boundary. Uses a reduced, nullable `hard_expires_at` schema because the
    production schema's `NOT NULL` makes the NULL value unreachable there - the
    guard is defence-in-depth for exactly that legacy/never-assigned case."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE attachments (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, hard_expires_at INTEGER
        );
        CREATE TABLE mca_sender_state (
            attachment_id TEXT PRIMARY KEY, revoke_token TEXT
        );
        CREATE TABLE mca_sender_revoke_state (
            attachment_id TEXT PRIMARY KEY, revoke_token TEXT,
            delete_after TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE attachment_events (
            id INTEGER PRIMARY KEY, attachment_id TEXT, occurred_at REAL,
            event_type TEXT, detail_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO attachments (id, state, hard_expires_at) VALUES (?, ?, ?)",
        ("att-null", sender.SENT, None),
    )
    conn.execute(
        "INSERT INTO mca_sender_state (attachment_id, revoke_token) VALUES (?, ?)",
        ("att-null", "tok-1"),
    )
    conn.execute(
        "INSERT INTO mca_sender_revoke_state (attachment_id, revoke_token, delete_after, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("att-null", "tok-1", "9999999999.0", "1.0", "1.0"),
    )
    conn.execute(
        "INSERT INTO attachment_events (attachment_id, occurred_at, event_type, detail_json) VALUES (?, ?, ?, ?)",
        ("att-null", 1.0, "created", "{}"),
    )

    assert sender.apply_expired(conn, "att-null", now=10**12) == sender.SENT
    assert sender.get_state(conn, "att-null") == sender.SENT
    # Secret tables unchanged (transient row still present, revoke row intact).
    assert conn.execute(
        "SELECT revoke_token FROM mca_sender_state WHERE attachment_id = 'att-null'"
    ).fetchone()[0] == "tok-1"
    assert conn.execute(
        "SELECT revoke_token FROM mca_sender_revoke_state WHERE attachment_id = 'att-null'"
    ).fetchone()[0] == "tok-1"
    # Timeline unchanged (no "expired" event recorded).
    assert conn.execute(
        "SELECT COUNT(*) FROM attachment_events WHERE attachment_id = 'att-null'"
    ).fetchone()[0] == 1


def test_apply_expired_fails_closed_when_hard_expires_at_is_zero(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0010 Decision 2 (fail closed): hard_expires_at = 0 is not a valid
    positive authoritative deadline, so an inbound EXPIRED is dropped - no
    state, secret-table, or timeline mutation."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})

    conn.execute("UPDATE attachments SET hard_expires_at = 0 WHERE id = ?", (attachment_id,))
    conn.commit()

    transient_before = tuple(sender._get_sender_state(conn, attachment_id))
    revoke_before = tuple(conn.execute(
        "SELECT * FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone())
    events_before = conn.execute(
        "SELECT COUNT(*) FROM attachment_events WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()[0]

    assert sender.apply_expired(conn, attachment_id, now=1) == sender.SENT
    assert sender.get_state(conn, attachment_id) == sender.SENT
    assert tuple(sender._get_sender_state(conn, attachment_id)) == transient_before
    assert tuple(conn.execute(
        "SELECT * FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()) == revoke_before
    assert conn.execute(
        "SELECT COUNT(*) FROM attachment_events WHERE attachment_id = ?", (attachment_id,)
    ).fetchone()[0] == events_before


def test_apply_rejected_on_terminal_is_noop(conn, wsm, principal, recipient, tmp_path, relay_client):
    """ADR-0010: a stale REJECTED arriving after DOWNLOADED must never regress
    the terminal state - monotonic and idempotent, mirroring apply_ack()."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    assert sender.apply_ack(conn, attachment_id, codec.MessageType.ACK_DOWNLOADED) == sender.DOWNLOADED

    assert sender.apply_rejected(conn, attachment_id) == sender.DOWNLOADED  # unchanged
    assert sender.get_state(conn, attachment_id) == sender.DOWNLOADED
    assert sender.apply_expired(conn, attachment_id) == sender.DOWNLOADED  # unchanged
    assert sender.get_state(conn, attachment_id) == sender.DOWNLOADED


def test_apply_rejected_on_draft_is_dropped(conn, wsm, principal, recipient, tmp_path):
    """ADR-0010: a REJECTED/EXPIRED against a still-DRAFT attachment is
    dropped (state unchanged, no write, no exception) - mesh delivery is
    unordered, so a wire consumer must not blow up on an out-of-order
    lifecycle ack, exactly like apply_ack()'s own tolerance."""
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    assert sender.apply_rejected(conn, attachment_id) == sender.DRAFT
    assert sender.get_state(conn, attachment_id) == sender.DRAFT
    assert sender.apply_expired(conn, attachment_id) == sender.DRAFT
    assert sender.get_state(conn, attachment_id) == sender.DRAFT


def test_cancel_from_draft_and_rejects_from_sent(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    assert sender.cancel(conn, attachment_id) == sender.CANCELLED

    attachment_id2, _ = _draft(conn, wsm, principal, recipient, tmp_path, file_name="b.txt", route_id="receiver2")
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id2, {sender.SENT})
    with pytest.raises(sender.SenderError):
        sender.cancel(conn, attachment_id2)


def test_resume_pending_drives_multiple_attachments_to_sent(conn, wsm, principal, recipient, tmp_path, relay_client):
    id1, _ = _draft(conn, wsm, principal, recipient, tmp_path, file_name="a.txt", route_id="r1")
    id2, _ = _draft(conn, wsm, principal, recipient, tmp_path, file_name="b.txt", route_id="r2")

    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _, pub, key_id = recipient
    touched = sender.resume_pending(
        conn, workspace_manager=wsm, principal=principal,
        recipient_identities_by_attachment={id1: {key_id: pub}, id2: {key_id: pub}},
        relay_client=relay_client, delivery_adapter=adapter,
    )
    assert set(touched) == {id1, id2}
    assert sender.get_state(conn, id1) == sender.SENT
    assert sender.get_state(conn, id2) == sender.SENT


def test_create_upload_409_with_no_local_upload_id_tombstones_and_restarts(
    conn, wsm, principal, recipient, tmp_path, relay_client, store
):
    """Simulates the orphaned-upload-session recovery path (ADR-0006/
    migration 5's documented 409 story): a transfer_id that the Relay
    already has an upload session for, but with no locally-known
    upload_id, must never be retried as-is - it's tombstoned and a fresh
    transfer_id takes over, restarting from ENCRYPTING."""

    attachment_id, source_path = _draft(conn, wsm, principal, recipient, tmp_path)
    recipient_identities = {recipient[2]: recipient[1]}

    # Drive to QUEUED_UPLOAD (through VALIDATING/ENCRYPTING).
    for _ in range(5):
        if sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD:
            break
        sender.run_step(
            conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
        )
    assert sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD

    original_transfer_id = conn.execute(
        "SELECT transfer_id FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()[0]

    # Pre-create a Relay upload session under this exact transfer_id
    # out-of-band, simulating "a previous attempt already called
    # create_upload and then crashed before persisting upload_id locally".
    state_row = sender._get_sender_state(conn, attachment_id)
    from meshsrv.attachments.relay_client import ChunkDeclaration

    relay_client.create_upload(
        transfer_id=bytes.fromhex(original_transfer_id),
        total_size=1,
        ciphertext_sha256=hashlib.sha256(b"x").digest(),
        manifest_size=len(state_row["manifest_blob"]),
        manifest_sha256=bytes.fromhex(state_row["manifest_sha256"]),
        chunks=[ChunkDeclaration(size=1, sha256=hashlib.sha256(b"x").digest())],
        receipt_hashes=[bytes.fromhex(r["receipt_secret_hash"]) for r in sender._recipients_for(conn, attachment_id)],
    )

    # Now let the state machine try to move to UPLOADING - it will hit the
    # 409 with no local upload_id and must recover, not crash or loop.
    new_state = sender.run_step(
        conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
        relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
    )
    assert new_state == sender.UPLOADING
    new_state = sender.run_step(
        conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
        relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
    )
    assert new_state == sender.ENCRYPTING

    new_transfer_id = conn.execute(
        "SELECT transfer_id FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()[0]
    assert new_transfer_id != original_transfer_id

    tombstoned = conn.execute(
        "SELECT reason FROM mca_tombstones WHERE transfer_id = ?", (original_transfer_id,)
    ).fetchone()
    assert tombstoned is not None
    assert tombstoned[0] == "orphaned_upload_session"

    # And the attachment can still complete normally from here on a fresh transfer_id.
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    final = _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    assert final == sender.SENT


# ---- draft_comment (fixes the comment-loss bug: create_draft() accepted ---
# ---- `comment` but _step_encrypting() hard-coded comment=None) -----------


def test_comment_survives_from_create_draft_to_decrypted_manifest(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, comment="hello from the sender")
    recipient_identities = {recipient[2]: recipient[1]}
    for _ in range(5):
        if sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD:
            break
        sender.run_step(
            conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
        )
    assert sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD

    header = _decrypt_sender_manifest_header(conn, attachment_id, recipient)
    assert header.comment == "hello from the sender"


def test_none_comment_stays_none(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, comment=None)
    recipient_identities = {recipient[2]: recipient[1]}
    for _ in range(5):
        if sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD:
            break
        sender.run_step(
            conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
        )
    header = _decrypt_sender_manifest_header(conn, attachment_id, recipient)
    assert header.comment is None


def test_empty_and_whitespace_comment_normalizes_to_none(conn, wsm, principal, recipient, tmp_path, relay_client):
    for raw in ("", "   ", "\t\n"):
        attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, file_name=f"a-{len(raw)}.txt", route_id=f"r-{len(raw)}", comment=raw)
        row = conn.execute("SELECT draft_comment FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
        assert row[0] is None


def test_comment_survives_a_restart_between_draft_and_encrypting(conn, wsm, principal, recipient, tmp_path, relay_client):
    """A crash between create_draft() and _step_encrypting() actually
    running must not lose the comment - it has to come from persisted
    state, not a variable held only in the caller's process."""

    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, comment="survives a restart")
    # simulate "restart": nothing but the DB row exists at this point: no
    # in-memory reference to the original comment string is used below.
    recipient_identities = {recipient[2]: recipient[1]}
    for _ in range(5):
        if sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD:
            break
        sender.run_step(
            conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
        )
    header = _decrypt_sender_manifest_header(conn, attachment_id, recipient)
    assert header.comment == "survives a restart"


def test_comment_over_length_limit_rejected_before_draft_created(conn, wsm, principal, recipient, tmp_path):
    too_long = "x" * (sender.MAX_COMMENT_BYTES + 1)
    source_path = tmp_path / "a.txt"
    source_path.write_bytes(b"hello")
    _, pub, key_id = recipient
    with pytest.raises(sender.SenderError):
        sender.create_draft(
            conn, wsm, principal, workspace_id="local", source_path=str(source_path), file_name="a.txt",
            mime_type="text/plain", recipients=[sender.RecipientTarget(public_identity=pub, key_id=key_id)],
            adapter_id="fake-text", connector_profile_id="default", route_type="DIRECT", route_id="r",
            provider_id=b"\x01" * 8, comment=too_long,
        )
    # rejected before any row was created at all
    count = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    assert count == 0


def test_comment_never_reaches_relay_in_plaintext(conn, wsm, principal, recipient, tmp_path, relay_client, store):
    secret_comment = "this must never appear in cleartext on the wire"
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, comment=secret_comment)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    final = _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    assert final == sender.SENT

    transfer_id = bytes.fromhex(conn.execute("SELECT transfer_id FROM attachments WHERE id = ?", (attachment_id,)).fetchone()[0])
    transfer = store._fetch_object(transfer_id)
    haystacks = [transfer.manifest_data] + [c.data for c in transfer.chunks.values()]
    needle = secret_comment.encode("utf-8")
    for blob in haystacks:
        assert needle not in blob


def test_draft_comment_column_cleared_after_encrypting(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path, comment="temporary plaintext")
    recipient_identities = {recipient[2]: recipient[1]}
    for _ in range(5):
        if sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD:
            break
        sender.run_step(
            conn, workspace_manager=wsm, principal=principal, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=None, attachment_id=attachment_id,
        )
    assert sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD
    row = conn.execute("SELECT draft_comment FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row[0] is None
