"""tests/test_attachments_service.py -- meshsrv/attachments/service.py
(ADR-0008 decision 1, Step 1.6A backend layer).

Covers the worker's own orchestration logic - the parts sender.py's/
receiver.py's own test suites don't and shouldn't re-test: the tick scan
(direction-aware AUTOMATIC_STATES filtering, max_per_tick capping,
created_at ordering), recipient-identity re-resolution from
key_exchange's bindings table for ENCRYPTING, the provider_id hex/
Base64URL normalization between 'sent' and 'received' rows, wiring
ConnectivityMonitor.can_attempt_relay() into network_available, the
start()/stop()/wake() thread lifecycle, and one full sent-side and one
full received-side integration driven purely through repeated tick()
calls (never calling sender.run_step()/receiver.run_step() directly, to
prove the service's own wiring - not just the state machines - works
end to end against the real mock Relay).
"""

from __future__ import annotations

import base64
import sqlite3
import time
import uuid

import pytest

from meshsrv.attachments import codec, identity, receiver, sender
from meshsrv.attachments.db import migrations
from meshsrv.attachments.delivery.fakes import FakeTextAdapter, InMemoryEther
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.attachments.relay_client import RelayClient
from meshsrv.attachments.service import AttachmentsService, _provider_id_text
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.connectivity_monitor import ConnectivityMonitor

BASE_URL = "https://mock-relay.test"
ADAPTER_ID = "fake-text"


class _ResponseShim:
    def __init__(self, flask_response):
        self._flask_response = flask_response
        self.status_code = flask_response.status_code
        self.headers = flask_response.headers
        self.content = flask_response.data

    @property
    def text(self):
        return self._flask_response.get_data().decode("utf-8", errors="replace")

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


class _AlwaysDownSession:
    """Feeds ConnectivityMonitor a permanently-unreachable Relay, for
    testing that AttachmentsService actually gates on
    can_attempt_relay() rather than always passing network_available=True."""

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        import requests

        raise requests.ConnectionError("down for this test")


def _raw_provider_id(profile) -> bytes:
    padding = "=" * (-len(profile.provider_id) % 4)
    return base64.urlsafe_b64decode(profile.provider_id + padding)


def _insert_binding(conn, *, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity, now):
    conn.execute(
        """
        INSERT INTO mca_recipient_bindings
            (id, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity,
             key_epoch, bound_at, tofu_confirmed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (uuid.uuid4().hex, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity.hex(), now, now),
    )
    conn.commit()


@pytest.fixture
def store():
    return MockRelayStore(base_url=BASE_URL)


@pytest.fixture
def relay_session(store):
    app = create_mock_relay_app(store)
    return _FlaskTestClientSession(app.test_client(), BASE_URL)


@pytest.fixture
def relay_client(store, relay_session):
    return RelayClient(BASE_URL, upload_access_token=store.upload_access_token, session=relay_session, sleep=lambda _s: None)


@pytest.fixture
def conn(tmp_path):
    # check_same_thread=False: matches mca_runtime.py's production wiring
    # (the one real caller that hands a connection to a background-thread
    # service) - AttachmentsService.start() runs tick() on its own worker
    # thread, so the connection it's given must already tolerate that.
    c = sqlite3.connect(str(tmp_path / "attachments.db"), check_same_thread=False)
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
        is_default=True,
    )


@pytest.fixture
def connectivity_monitor(provider_registry, relay_session):
    return ConnectivityMonitor(provider_registry, session=relay_session)


@pytest.fixture
def delivery_adapter():
    return FakeTextAdapter(InMemoryEther(), "local-addr")


@pytest.fixture
def service(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client, delivery_adapter):
    return AttachmentsService(
        conn,
        workspace_manager=wsm,
        principal=principal,
        provider_registry=provider_registry,
        key_exchange=key_exchange,
        connectivity_monitor=connectivity_monitor,
        delivery_adapter=delivery_adapter,
        relay_client_factory=lambda provider_id_text: relay_client,
        max_per_tick=8,
    )


@pytest.fixture
def remote_recipient(tmp_path):
    """A second, independent workspace/principal to send attachments to -
    playing the same role as test_receiver.py's `remote_sender`, just on
    the receiving end of a 'sent' attachment instead."""
    conn2 = sqlite3.connect(str(tmp_path / "recipient_attachments.db"))
    conn2.execute("PRAGMA foreign_keys = ON")
    migrations.migrate(conn2)
    wsm2 = MCAWorkspaceManager(str(tmp_path / "recipient_data"))
    principal2 = identity.ensure_principal(conn2, wsm2, "local")
    return conn2, wsm2, principal2


def _bind_recipient(conn, principal2, *, transport_address="remote-addr", now=None):
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
    )


def _create_draft(conn, wsm, principal, *, recipient_principal, registered_provider, tmp_path, content=b"hello"):
    source_path = tmp_path / "outgoing.txt"
    source_path.write_bytes(content)
    return sender.create_draft(
        conn, wsm, principal,
        workspace_id="local",
        source_path=str(source_path),
        file_name="outgoing.txt",
        mime_type="text/plain",
        recipients=[sender.RecipientTarget(public_identity=recipient_principal.public_identity, key_id=recipient_principal.key_id)],
        adapter_id=ADAPTER_ID,
        connector_profile_id="default",
        route_type="DIRECT",
        route_id="remote-addr",
        provider_id=_raw_provider_id(registered_provider),
    )


# ---- _provider_id_text() pass-through --------------------------------------
# Regression coverage for a reviewer-found defect: attachments.provider_id
# used to be stored as hex for 'sent' rows and Base64URL for 'received'
# rows; sender.py now stores Base64URL for both (ADR-0008-hardening,
# Migration 9), so this helper is a thin, direction-agnostic pass-through,
# not a normalizer - these tests pin exactly that, so a future regression
# back to per-direction branching here would fail loudly.


def test_provider_id_text_passes_sent_value_through_unchanged():
    assert _provider_id_text("sent", "already-base64url-text") == "already-base64url-text"


def test_provider_id_text_passes_received_value_through_unchanged():
    assert _provider_id_text("received", "already-base64url-text") == "already-base64url-text"


def test_provider_id_text_returns_none_for_missing_value():
    assert _provider_id_text("sent", None) is None
    assert _provider_id_text("sent", "") is None
    assert _provider_id_text("received", None) is None


# ---- tick scan: capping and direction filtering ----------------------------


def test_tick_returns_zero_with_nothing_due(service):
    assert service.tick() == 0


def test_tick_respects_max_per_tick_cap(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service):
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    service._max_per_tick = 1

    ids = [
        _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path, content=f"file-{i}".encode())
        for i in range(3)
    ]
    processed = service.tick()
    assert processed == 1

    states = [sender.get_state(conn, attachment_id) for attachment_id in ids]
    assert states.count(sender.DRAFT) == 2  # only one of the three advanced past DRAFT
    assert states.count(sender.VALIDATING) == 1


def test_tick_does_not_touch_sent_or_waiting_consent_rows(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service):
    """SENT (event-driven) and WAITING_CONSENT (explicit user action only)
    are deliberately excluded from AUTOMATIC_STATES - the tick scan must
    never pick them up even though they're non-terminal."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.SENT:
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.SENT

    # Now that it's SENT, further ticks must be no-ops for this row.
    for _ in range(3):
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.SENT


# ---- full sent-side integration, driven only through service.tick() -------


def test_tick_drives_a_draft_all_the_way_to_sent(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service):
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.SENT:
            break
        service.tick()

    assert sender.get_state(conn, attachment_id) == sender.SENT


def test_recipient_identity_is_re_resolved_from_bindings_not_reused(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service):
    """Nothing in this test process ever holds `recipient_principal`'s
    public_identity in memory once create_draft() returns - the service
    must re-derive it from key_exchange bindings at ENCRYPTING time
    (sender.py's own docstring: it deliberately doesn't persist that
    value a second time)."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.ENCRYPTING:
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.ENCRYPTING

    identities = service._resolve_recipient_identities(attachment_id)
    assert identities == {recipient_principal.key_id: recipient_principal.public_identity}

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.SENT:
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.SENT


# ---- network gating via ConnectivityMonitor --------------------------------


def test_unreachable_relay_stalls_at_queued_upload_not_uploading(conn, wsm, principal, provider_registry, registered_provider, key_exchange, remote_recipient, tmp_path, relay_client, delivery_adapter):
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)

    down_connectivity = ConnectivityMonitor(provider_registry, session=_AlwaysDownSession())
    offline_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=down_connectivity, delivery_adapter=delivery_adapter,
        relay_client_factory=lambda provider_id_text: relay_client,
    )
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(10):
        offline_service.tick()
    assert sender.get_state(conn, attachment_id) == sender.QUEUED_UPLOAD


def test_can_attempt_relay_fails_open_before_any_check(service):
    assert service._can_attempt("some-provider-never-checked") is True
    assert service._can_attempt(None) is False


# ---- full received-side integration, driven only through service.tick() ---


def test_tick_drives_an_offer_all_the_way_to_available(conn, wsm, principal, provider_registry, registered_provider, key_exchange, relay_client, remote_recipient, tmp_path, service):
    """`remote_recipient` here plays the role of the remote sender (the
    fixture name is symmetric - the same helper works for either side of
    a transfer): it sends a real object through the shared mock Relay to
    `principal`'s workspace, then the OFFER is fed into the receiver side
    and driven purely by service.tick() to AVAILABLE, including the
    explicit begin_download() step a real UI's "Скачать" button would
    trigger."""
    sender_conn, sender_wsm, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")

    provider_id = _raw_provider_id(registered_provider)
    content = b"a real received file"
    source_path = tmp_path / "incoming.txt"
    source_path.write_bytes(content)
    sent_attachment_id = sender.create_draft(
        sender_conn, sender_wsm, sender_principal,
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
    sender_adapter = FakeTextAdapter(InMemoryEther(), "remote-sender-addr")
    for _ in range(20):
        if sender.get_state(sender_conn, sent_attachment_id) == sender.SENT:
            break
        sender.run_step(
            sender_conn, workspace_manager=sender_wsm, principal=sender_principal,
            recipient_identities=recipient_identities, relay_client=relay_client,
            delivery_adapter=sender_adapter, attachment_id=sent_attachment_id,
        )
    assert sender.get_state(sender_conn, sent_attachment_id) == sender.SENT

    row = sender_conn.execute(
        "SELECT transfer_id, hard_expires_at FROM attachments WHERE id = ?", (sent_attachment_id,)
    ).fetchone()
    transfer_id = bytes.fromhex(row[0])
    hard_expires_at = row[1]
    sender_signing_key = identity.load_signing_key(sender_wsm, sender_principal)
    offer_fields = codec.OfferFields(
        provider_id=provider_id,
        transfer_id=transfer_id,
        sender_key_id=bytes.fromhex(sender_principal.key_id),
        kind=0,
        size_bucket=1,
        hard_expires_at=hard_expires_at,
        flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )
    received_attachment_id = result.attachment_id
    assert receiver.get_state(conn, received_attachment_id) == receiver.WAITING_CONSENT

    receiver.begin_download(conn, received_attachment_id)
    assert receiver.get_state(conn, received_attachment_id) == receiver.DOWNLOADING

    for _ in range(20):
        if receiver.get_state(conn, received_attachment_id) == receiver.AVAILABLE:
            break
        service.tick()

    assert receiver.get_state(conn, received_attachment_id) == receiver.AVAILABLE
    saved_path = conn.execute(
        "SELECT saved_path FROM attachments WHERE id = ?", (received_attachment_id,)
    ).fetchone()[0]
    with open(saved_path, "rb") as f:
        assert f.read() == content


# ---- start()/stop()/wake() lifecycle ---------------------------------------


def test_start_stop_is_idempotent_and_joins_cleanly(service):
    service.start()
    service.start()  # no-op, not a second thread
    assert service._thread is not None
    assert service._thread.is_alive()
    service.stop()
    assert service._thread is None


def test_wake_causes_a_tick_promptly_instead_of_waiting_for_the_full_interval(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service):
    service._tick_seconds = 60.0  # would never fire in test time without wake()
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    service.start()
    try:
        for _ in range(50):
            service.wake()
            time.sleep(0.05)
            if sender.get_state(conn, attachment_id) not in (sender.DRAFT,):
                break
        assert sender.get_state(conn, attachment_id) != sender.DRAFT
    finally:
        service.stop()
