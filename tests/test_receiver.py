"""tests/test_receiver.py -- meshsrv/attachments/receiver.py (Step 1.5).

In-process tests (mock Relay via a Flask test client, exactly like
test_sender.py) covering the receiver state machine's normal-path
transitions (OFFER_RECEIVED -> WAITING_KEY/WAITING_PROVIDER/
WAITING_NETWORK/WAITING_CONSENT -> DOWNLOADING -> AVAILABLE/FAILED),
ACK dedup, reconcile_pending(), and the tamper/failure paths (criteria
#16, #17, #18 in design spec section 24).

A remote sender is a *second*, independent principal/workspace (its own
conn/wsm/tmp_path) - it plays the role of the party sending files to the
receiver under test, using sender.py's own real state machine to produce
a genuine encrypted object on the shared mock Relay, then this module's
`codec.encode_offer()` is used directly to build the OFFER frame that
would have been delivered (handle_offer() takes already-`ingest()`-ed
logical bytes, not adapter-wire-encoded ones - see receiver.py's own
docstring).
"""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import time
import uuid

import pytest
from nacl.signing import SigningKey

from meshsrv.attachments import codec, identity, receiver, sender
from meshsrv.attachments.delivery.fakes import FakeTextAdapter, InMemoryEther
from meshsrv.attachments.db import migrations
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.attachments.relay_client import RelayClient
from meshsrv.attachments.workspace import MCAWorkspaceManager


class _ResponseShim:
    def __init__(self, flask_response):
        self._flask_response = flask_response
        self.status_code = flask_response.status_code
        self.headers = flask_response.headers
        self.content = flask_response.data

    @property
    def text(self):
        # Lazy, and tolerant of non-UTF-8 bodies (raw ciphertext chunks) -
        # only ever actually read for JSON/text error bodies in practice.
        return self._flask_response.get_data(as_text=False).decode("utf-8", errors="replace")

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
ADAPTER_ID = "fake-text"


def _raw_provider_id(profile) -> bytes:
    padding = "=" * (-len(profile.provider_id) % 4)
    return base64.urlsafe_b64decode(profile.provider_id + padding)


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
def provider_registry(conn):
    return ProviderRegistry(conn, "local")


@pytest.fixture
def key_exchange(conn, wsm, principal):
    return KeyExchangeCoordinator(conn, wsm, principal, ADAPTER_ID)


@pytest.fixture
def registered_provider(provider_registry, store):
    return provider_registry.register(
        display_name="Mock Relay",
        base_url=BASE_URL,
        service_public_key=store.service_public_key,
        max_ciphertext_bytes=10_000_000,
    )


@pytest.fixture
def remote_sender(tmp_path):
    """A second, independent workspace/principal playing the role of the
    party sending files to the receiver under test."""
    conn2 = sqlite3.connect(str(tmp_path / "sender_attachments.db"))
    conn2.execute("PRAGMA foreign_keys = ON")
    migrations.migrate(conn2)
    wsm2 = MCAWorkspaceManager(str(tmp_path / "sender_data"))
    principal2 = identity.ensure_principal(conn2, wsm2, "local")
    return conn2, wsm2, principal2


def _insert_binding(conn, *, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity, now, confirmed=True):
    conn.execute(
        """
        INSERT INTO mca_recipient_bindings
            (id, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity,
             key_epoch, bound_at, tofu_confirmed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (uuid.uuid4().hex, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity.hex(), now, now if confirmed else None),
    )
    conn.commit()


def _bind_remote_sender(conn, principal2, *, transport_address="remote-addr", now=None, confirmed=True):
    now = time.time() if now is None else now
    _insert_binding(
        conn,
        workspace_id="local",
        adapter_id=ADAPTER_ID,
        transport_address=transport_address,
        principal_id=principal2.principal_id,
        sender_key_id=principal2.key_id,
        public_identity=principal2.public_identity,
        now=now,
        confirmed=confirmed,
    )


def _build_offer(*, provider_id: bytes, transfer_id: bytes, sender_principal, sender_signing_key, hard_expires_at: int, flags: int = 0) -> bytes:
    fields = codec.OfferFields(
        provider_id=provider_id,
        transfer_id=transfer_id,
        sender_key_id=bytes.fromhex(sender_principal.key_id),
        kind=0,
        size_bucket=1,
        hard_expires_at=hard_expires_at,
        flags=flags,
    )
    return codec.encode_offer(fields, sender_signing_key)


def _create_real_transfer(remote_sender, principal, relay_client, provider_id: bytes, tmp_path, content: bytes):
    """Drives a genuine encrypted upload (via sender.py's own state
    machine) addressed to `principal`, all the way to SENT, so the
    receiver under test has a real object to fetch from the shared mock
    Relay. Returns (transfer_id_bytes, raw_offer_bytes)."""

    conn2, wsm2, principal2 = remote_sender
    source_path = tmp_path / "incoming.txt"
    source_path.write_bytes(content)
    attachment_id = sender.create_draft(
        conn2, wsm2, principal2,
        workspace_id="local",
        source_path=str(source_path),
        file_name="incoming.txt",
        mime_type="text/plain",
        recipients=[sender.RecipientTarget(public_identity=principal.public_identity, key_id=principal.key_id)],
        adapter_id=ADAPTER_ID,
        connector_profile_id="default",
        route_type="DIRECT",
        route_id="receiver-under-test",
        provider_id=provider_id,
    )
    recipient_identities = {principal.key_id: principal.public_identity}
    ether = InMemoryEther()
    sender_adapter = FakeTextAdapter(ether, "remote-sender-addr")
    for _ in range(20):
        state = sender.get_state(conn2, attachment_id)
        if state == sender.SENT:
            break
        sender.run_step(
            conn2, workspace_manager=wsm2, principal=principal2, recipient_identities=recipient_identities,
            relay_client=relay_client, delivery_adapter=sender_adapter, attachment_id=attachment_id,
        )
    assert sender.get_state(conn2, attachment_id) == sender.SENT

    row = conn2.execute("SELECT transfer_id, hard_expires_at FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    transfer_id = bytes.fromhex(row[0])
    hard_expires_at = row[1]
    sender_signing_key = identity.load_signing_key(wsm2, principal2)
    raw_offer = _build_offer(
        provider_id=provider_id, transfer_id=transfer_id, sender_principal=principal2,
        sender_signing_key=sender_signing_key, hard_expires_at=hard_expires_at,
    )
    return transfer_id, raw_offer


# ---- WAITING_KEY: unknown sender, zero network calls -----------------------


def test_unknown_sender_key_lands_in_waiting_key_with_no_network_call(conn, wsm, principal, provider_registry, key_exchange):
    sk = SigningKey.generate()
    fake_principal = type("P", (), {"key_id": identity.compute_key_id(bytes(sk.verify_key))})()
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=fake_principal,
        sender_signing_key=sk, hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_KEY
    assert len(result.replies) == 1  # ACK_RECEIVED only
    row = conn.execute("SELECT pending_offer_cbor FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()
    assert bytes(row[0]) == raw_offer


def test_waiting_key_advances_automatically_once_binding_recorded(conn, wsm, principal, provider_registry, key_exchange, remote_sender):
    conn2, wsm2, principal2 = remote_sender
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_KEY

    # binding appears later (e.g. a KEY_ANNOUNCE arrives) - no OFFER re-delivery
    _bind_remote_sender(conn, principal2)

    result2 = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
    )
    assert result2.state == receiver.WAITING_PROVIDER
    row = conn.execute("SELECT pending_offer_cbor, sender_principal_id FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()
    assert row[0] is None
    assert row[1] == principal2.principal_id


def test_waiting_key_does_not_advance_on_unverified_binding(conn, wsm, principal, provider_registry, key_exchange, remote_sender):
    """PR 1: a parked WAITING_KEY transfer must NOT resume merely because an
    UNVERIFIED binding for its sender_key_id now exists (a KEY_ANNOUNCE was
    delivered but no human confirmed it). Only an explicitly trusted key
    (MCA_READY, `tofu_confirmed_at` set) may authorize the file."""
    conn2, wsm2, principal2 = remote_sender
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_KEY

    # An UNVERIFIED binding appears (KEY_ANNOUNCE delivered, never confirmed).
    _bind_remote_sender(conn, principal2, confirmed=False)

    result2 = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
    )
    assert result2.state == receiver.WAITING_KEY
    row = conn.execute("SELECT pending_offer_cbor FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()
    assert bytes(row[0]) == raw_offer  # still parked, not yet authorized

    # The human explicitly confirms the key -> the transfer now resumes.
    key_exchange.confirm_tofu("remote-addr")

    result3 = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
    )
    assert result3.state == receiver.WAITING_PROVIDER


# ---- PR #231 review (2nd pass): inbound OFFER admission limits ------------


def test_offer_flood_from_one_source_is_rejected_once_per_source_limit_reached(conn, wsm, principal, provider_registry, key_exchange):
    """PR #231 review (2nd pass): before this fix, a flood of OFFERs
    carrying distinct, attacker-chosen transfer_ids had no ceiling at
    all - each one became a permanent attachments row (the existing
    transfer_id dedup only protects against a *repeated* transfer_id).
    Drives the per-source limit (MAX_PENDING_RECEIVED_PER_SOURCE) to
    exhaustion from one source_address and confirms the next OFFER is
    rejected outright, with no new row created."""
    sk = SigningKey.generate()
    fake_principal = type("P", (), {"key_id": identity.compute_key_id(bytes(sk.verify_key))})()

    for _ in range(receiver.MAX_PENDING_RECEIVED_PER_SOURCE):
        raw_offer = _build_offer(
            provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=fake_principal,
            sender_signing_key=sk, hard_expires_at=int(time.time()) + 3600,
        )
        result = receiver.handle_offer(
            conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
            key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
            source_address="!flooding-node",
        )
        assert result.state == receiver.WAITING_KEY

    count_before = conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE direction = 'received'"
    ).fetchone()[0]
    assert count_before == receiver.MAX_PENDING_RECEIVED_PER_SOURCE

    one_too_many = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=fake_principal,
        sender_signing_key=sk, hard_expires_at=int(time.time()) + 3600,
    )
    with pytest.raises(receiver.ReceiverError, match="pending_received_per_source_limit_exceeded"):
        receiver.handle_offer(
            conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
            key_exchange=key_exchange, raw_offer=one_too_many, network_available=True,
            source_address="!flooding-node",
        )

    count_after = conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE direction = 'received'"
    ).fetchone()[0]
    assert count_after == count_before  # nothing new was created


def test_offer_from_a_different_source_is_not_blocked_by_another_sources_limit(conn, wsm, principal, provider_registry, key_exchange):
    """The per-source limit must actually be per-source, not a disguised
    global one - a second, distinct source_address must still be able to
    send an OFFER even while the first is at its own per-source ceiling."""
    sk = SigningKey.generate()
    fake_principal = type("P", (), {"key_id": identity.compute_key_id(bytes(sk.verify_key))})()

    for _ in range(receiver.MAX_PENDING_RECEIVED_PER_SOURCE):
        raw_offer = _build_offer(
            provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=fake_principal,
            sender_signing_key=sk, hard_expires_at=int(time.time()) + 3600,
        )
        receiver.handle_offer(
            conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
            key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
            source_address="!flooding-node",
        )

    from_elsewhere = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=fake_principal,
        sender_signing_key=sk, hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=from_elsewhere, network_available=True,
        source_address="!a-different-node",
    )
    assert result.state == receiver.WAITING_KEY


def test_offer_flood_across_many_sources_is_rejected_once_global_limit_reached(conn, wsm, principal, provider_registry, key_exchange):
    """PR #231 review (3rd pass): the global ceiling must actually be
    global - reachable by spreading a flood across many distinct
    sources, each safely under its own per-source limit, not just by
    hammering a single source_address (already covered by the
    per-source test above). Uses exactly MAX_PENDING_RECEIVED_PER_SOURCE
    OFFERs from each of (MAX_PENDING_RECEIVED_GLOBAL /
    MAX_PENDING_RECEIVED_PER_SOURCE) distinct sources, so the global
    limit is reached at the same moment as the last source's own
    per-source limit - proving the rejection is the GLOBAL check firing,
    not a coincidental per-source one, since a source used only once
    more afterward is nowhere near ITS OWN per-source ceiling."""
    sk = SigningKey.generate()
    fake_principal = type("P", (), {"key_id": identity.compute_key_id(bytes(sk.verify_key))})()

    assert receiver.MAX_PENDING_RECEIVED_GLOBAL % receiver.MAX_PENDING_RECEIVED_PER_SOURCE == 0
    source_count = receiver.MAX_PENDING_RECEIVED_GLOBAL // receiver.MAX_PENDING_RECEIVED_PER_SOURCE

    for source_index in range(source_count):
        source_address = f"!source-{source_index:04d}"
        for _ in range(receiver.MAX_PENDING_RECEIVED_PER_SOURCE):
            raw_offer = _build_offer(
                provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=fake_principal,
                sender_signing_key=sk, hard_expires_at=int(time.time()) + 3600,
            )
            receiver.handle_offer(
                conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
                key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
                source_address=source_address,
            )

    count_before = conn.execute("SELECT COUNT(*) FROM attachments WHERE direction = 'received'").fetchone()[0]
    assert count_before == receiver.MAX_PENDING_RECEIVED_GLOBAL

    # One more OFFER, from a brand-new source that has never sent
    # anything before (nowhere near its OWN per-source limit of 1/20) -
    # must still be rejected, because the workspace-wide total is
    # already at the global ceiling.
    one_too_many = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=fake_principal,
        sender_signing_key=sk, hard_expires_at=int(time.time()) + 3600,
    )
    with pytest.raises(receiver.ReceiverError, match="pending_received_global_limit_exceeded"):
        receiver.handle_offer(
            conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
            key_exchange=key_exchange, raw_offer=one_too_many, network_available=True,
            source_address="!a-completely-fresh-source",
        )

    count_after = conn.execute("SELECT COUNT(*) FROM attachments WHERE direction = 'received'").fetchone()[0]
    assert count_after == count_before  # nothing new was created


# ---- WAITING_PROVIDER: known sender, unknown provider ----------------------


def test_known_sender_unknown_provider_sends_exactly_one_ack_provider_unknown(conn, wsm, principal, provider_registry, key_exchange, remote_sender):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_PROVIDER
    assert len(result.replies) == 2  # ACK_RECEIVED + ACK_PROVIDER_UNKNOWN, exactly once each

    # re-stepping while still unregistered must not resend the ack
    result2 = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
    )
    assert result2.state == receiver.WAITING_PROVIDER
    assert result2.replies == []


def test_reconcile_pending_advances_waiting_provider_after_registration(conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    # use a *different* (unregistered) provider_id first
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_PROVIDER

    # simulate the admin registering *that* provider after the fact by
    # pointing the stored provider_id at the now-registered one
    conn.execute("UPDATE attachments SET provider_id = ? WHERE id = ?", (registered_provider.provider_id, result.attachment_id))
    conn.commit()

    results = receiver.reconcile_pending(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True,
    )
    assert len(results) == 1
    assert results[0].state == receiver.WAITING_CONSENT


# ---- full happy path --------------------------------------------------------


def test_full_happy_path_reaches_available_with_correct_content(conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    provider_id = _raw_provider_id(registered_provider)
    content = os.urandom(300_000)
    transfer_id, raw_offer = _create_real_transfer(remote_sender, principal, relay_client, provider_id, tmp_path, content)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_CONSENT

    receiver.begin_download(conn, result.attachment_id)
    final = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
        relay_client=relay_client,
    )
    assert final.state == receiver.AVAILABLE
    assert len(final.replies) == 1  # ACK_DOWNLOADED

    row = conn.execute(
        "SELECT file_name, mime_type, plain_size, plain_sha256, saved_path FROM attachments WHERE id = ?",
        (result.attachment_id,),
    ).fetchone()
    assert row[0] == "incoming.txt"
    assert row[1] == "text/plain"
    assert row[2] == len(content)
    assert row[3] == hashlib.sha256(content).hexdigest()
    saved_path = row[4]
    assert saved_path is not None
    with open(saved_path, "rb") as f:
        assert f.read() == content


def test_repeat_offer_is_idempotent_no_duplicate_row_or_ack(conn, wsm, principal, provider_registry, key_exchange, remote_sender):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=int(time.time()) + 3600,
    )
    result1 = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    result2 = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result1.attachment_id == result2.attachment_id
    assert result2.replies == []  # ACK_RECEIVED already sent, WAITING_PROVIDER ack already sent too

    count = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    assert count == 1
    # PR #227 defect #1: `mca_outgoing_replies` (not `attachment_events`)
    # is now the real idempotency ledger for "has this ACK been decided
    # already" - its own UNIQUE(attachment_id, event_type) constraint is
    # what actually enforces at-most-once now, not just this COUNT(*).
    ack_events = conn.execute(
        "SELECT COUNT(*) FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result1.attachment_id,),
    ).fetchone()[0]
    assert ack_events == 1


# ---- tamper / failure paths --------------------------------------------------


def test_bad_signature_with_known_binding_creates_no_attachment_row(conn, wsm, principal, provider_registry, key_exchange, remote_sender):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    # signed with a *different* key than the one the binding actually trusts
    impostor_key = SigningKey.generate()
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=impostor_key, hard_expires_at=int(time.time()) + 3600,
    )
    with pytest.raises(receiver.ReceiverError):
        receiver.handle_offer(
            conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
            key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        )
    count = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    assert count == 0


def test_tampered_chunk_fails_download(conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, store, tmp_path):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    provider_id = _raw_provider_id(registered_provider)
    content = os.urandom(300_000)
    transfer_id, raw_offer = _create_real_transfer(remote_sender, principal, relay_client, provider_id, tmp_path, content)

    # corrupt the first chunk directly in the mock relay's store
    transfer = store._fetch_object(transfer_id)
    first_index = sorted(transfer.chunks.keys())[0]
    chunk = transfer.chunks[first_index]
    corrupted = bytearray(chunk.data)
    corrupted[0] ^= 0xFF
    chunk.data = bytes(corrupted)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_CONSENT
    receiver.begin_download(conn, result.attachment_id)
    final = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
        relay_client=relay_client,
    )
    assert final.state == receiver.FAILED


def test_tampered_descriptor_signature_fails_closed(conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider, relay_client, tmp_path):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    provider_id = _raw_provider_id(registered_provider)
    content = os.urandom(50_000)
    transfer_id, raw_offer = _create_real_transfer(remote_sender, principal, relay_client, provider_id, tmp_path, content)

    # register a provider profile whose provider_id matches, but pin a
    # *different* service_public_key than what actually signed the
    # descriptor - simulates a forged/mismatched descriptor signature.
    forged_key = SigningKey.generate()
    conn.execute(
        "UPDATE mca_provider_profiles SET service_public_key_b64url = ? WHERE provider_id = ?",
        (base64.urlsafe_b64encode(bytes(forged_key.verify_key)).rstrip(b"=").decode("ascii"), registered_provider.provider_id),
    )
    conn.commit()

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_CONSENT
    receiver.begin_download(conn, result.attachment_id)
    final = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
        relay_client=relay_client,
    )
    assert final.state == receiver.FAILED
    row = conn.execute("SELECT error_code FROM attachments WHERE id = ?", (result.attachment_id,)).fetchone()
    assert row[0] == "descriptor_signature_invalid"


# ---- network handling --------------------------------------------------------


def test_offline_receiver_lands_in_waiting_network_then_advances(conn, wsm, principal, provider_registry, key_exchange, remote_sender, registered_provider):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    provider_id = _raw_provider_id(registered_provider)
    raw_offer = _build_offer(
        provider_id=provider_id, transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=False,
    )
    assert result.state == receiver.WAITING_NETWORK

    result2 = receiver.run_step(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, network_available=True, attachment_id=result.attachment_id,
    )
    assert result2.state == receiver.WAITING_CONSENT


# ---- begin_download() state guard -------------------------------------------


def test_begin_download_requires_waiting_consent(conn, wsm, principal, provider_registry, key_exchange, remote_sender):
    conn2, wsm2, principal2 = remote_sender
    _bind_remote_sender(conn, principal2)
    raw_offer = _build_offer(
        provider_id=os.urandom(8), transfer_id=os.urandom(16), sender_principal=principal2,
        sender_signing_key=identity.load_signing_key(wsm2, principal2), hard_expires_at=int(time.time()) + 3600,
    )
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    assert result.state == receiver.WAITING_PROVIDER
    with pytest.raises(receiver.ReceiverError):
        receiver.begin_download(conn, result.attachment_id)
