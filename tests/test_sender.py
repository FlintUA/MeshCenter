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

from meshsrv.attachments import identity, manifest, sender
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

    assert sender.on_ack_received(conn, attachment_id) == sender.RECEIVED
    assert sender.on_ack_downloaded(conn, attachment_id) == sender.DOWNLOADED

    # sender-side transient key material is cleared once fully downloaded
    assert sender._get_sender_state(conn, attachment_id) is None


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


def test_on_ack_received_requires_sent_state(conn, wsm, principal, recipient, tmp_path):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    with pytest.raises(sender.SenderError):
        sender.on_ack_received(conn, attachment_id)


def test_on_ack_downloaded_requires_received_state(conn, wsm, principal, recipient, tmp_path, relay_client):
    attachment_id, _ = _draft(conn, wsm, principal, recipient, tmp_path)
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "sender-addr")
    _drive_to(conn, wsm, principal, recipient, relay_client, adapter, attachment_id, {sender.SENT})
    with pytest.raises(sender.SenderError):
        sender.on_ack_downloaded(conn, attachment_id)


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
