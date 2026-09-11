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
import hashlib
import sqlite3
import time
import uuid

import pytest

from nacl.signing import SigningKey, VerifyKey

from meshsrv.attachments import codec, identity, receiver, sender
from meshsrv.attachments.commands import Command
from meshsrv.attachments.db import migrations
from meshsrv.attachments.delivery.base import DeliveryError, DeliveryReceipt
from meshsrv.attachments.delivery.fakes import FakeTextAdapter, InMemoryEther
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.probe_registry import PROBE_STATUS_PROBED, ProbeRecord
from meshsrv.attachments.provider_registry import (
    CLEAR,
    MAX_UPLOAD_TOKEN_BYTES,
    ProviderRegistry,
    compute_provider_id,
)
from meshsrv.attachments.relay.mock_server import MockRelayStore, create_mock_relay_app
from meshsrv.attachments.relay_client import RelayClient, RelayHTTPError, RelayInfo, RelayLimits
from meshsrv.attachments.relay_http import RelayNetworkError
from meshsrv.attachments.service import MAX_AUTO_KEY_REQUESTS_PER_TICK, AttachmentsService, InboundEvent, _provider_id_text
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


def _bind_recipient_unconfirmed(conn, principal2, *, transport_address="remote-addr", now=None):
    """Same shape as `_bind_recipient()` but leaves `tofu_confirmed_at`
    NULL - `AddressStatus.KEY_UNVERIFIED`, the "we've seen a KEY_ANNOUNCE
    for this address but nobody has pressed 'Доверять этому MCA-ключу'
    yet" state key_exchange.py's own module docstring describes. Used by
    defect #6's regression tests (PR #227): a recipient in this state
    must never receive a sealed envelope."""
    now = time.time() if now is None else now
    conn.execute(
        """
        INSERT INTO mca_recipient_bindings
            (id, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, public_identity,
             key_epoch, bound_at, tofu_confirmed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)
        """,
        (uuid.uuid4().hex, "local", ADAPTER_ID, transport_address, principal2.principal_id, principal2.key_id, principal2.public_identity.hex(), now),
    )
    conn.commit()


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


# ---- PR #231 review (3rd pass): service-layer upload readiness surface ----


def test_evaluate_upload_readiness_delegates_to_connectivity_monitor(service, registered_provider, connectivity_monitor, relay_session):
    """AttachmentsService.evaluate_upload_readiness() is the service-
    layer surface Step 1.6A's future REST endpoints should call - a thin
    delegation to ConnectivityMonitor.evaluate_upload_decision(), not a
    second implementation of the same logic."""
    connectivity_monitor.refresh(force=True)
    decision = service.evaluate_upload_readiness(registered_provider.provider_id)
    assert decision == connectivity_monitor.evaluate_upload_decision(registered_provider.provider_id)


def test_evaluate_upload_readiness_passes_through_ciphertext_and_ttl(
    service, registered_provider, connectivity_monitor, provider_registry, wsm, principal
):
    provider_registry.set_upload_token(registered_provider.provider_id, wsm, principal.principal_id, "mca_up_test-token")
    connectivity_monitor.refresh(force=True)
    huge = registered_provider.max_ciphertext_bytes + 1
    decision = service.evaluate_upload_readiness(registered_provider.provider_id, ciphertext_bytes=huge)
    assert decision.ready is False
    assert decision.reason.value == "ciphertext_too_large"


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
    must re-derive it from key_exchange bindings at ENCRYPTING time. The
    identity IS now also pinned on `attachment_recipients.
    recipient_public_identity` (ADR-0009/Migration 14), but that copy is for
    inbound-ACK verification, not the ENCRYPTING seal - the seal still
    re-resolves the currently-trusted binding so a key rotation fails closed
    rather than re-encrypting to a stale key."""
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


# ---- TOFU/binding trust enforced before encrypting (PR #227 defect #6) ----
# Regression coverage for a reviewer-found defect: _resolve_recipient_
# identities() used to hand back a recipient's public_identity for ANY
# key_exchange binding it found, regardless of whether that binding was
# ever TOFU-confirmed. An attacker (or just a stale contact) could get a
# file encrypted and sent to a key nobody ever verified.


def test_resolve_recipient_identities_excludes_unconfirmed_binding(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service):
    _, _, recipient_principal = remote_recipient
    _bind_recipient_unconfirmed(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.ENCRYPTING:
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.ENCRYPTING

    assert service._resolve_recipient_identities(attachment_id) == {}


def test_tick_fails_attachment_instead_of_encrypting_to_an_unconfirmed_key(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service):
    _, _, recipient_principal = remote_recipient
    _bind_recipient_unconfirmed(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        state = sender.get_state(conn, attachment_id)
        if state in (sender.FAILED_VALIDATION, sender.SENT):
            break
        service.tick()

    assert sender.get_state(conn, attachment_id) == sender.FAILED_VALIDATION
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row["error_code"] == "recipient_not_trusted"
    # mca_sender_state must never have been created - no key material
    # was ever generated or sealed for this attachment.
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is None


# ---- PR #231 review, section 8 ---------------------------------------------
# get_binding_by_key_id() is deliberately address-agnostic (used correctly
# for OFFER signature verification) - reusing it unchanged for the sending
# path would decouple "which key we trust" from "where we're actually
# sending", the exact property TOFU exists to bind together. A recipient
# whose binding is TOFU-pinned to a *different* transport address than this
# attachment's own DIRECT destination must be excluded, fail-closed, exactly
# like an unconfirmed/KEY_UNVERIFIED binding.


def test_resolve_recipient_identities_excludes_a_binding_pinned_to_a_different_address(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    _, _, recipient_principal = remote_recipient
    # TOFU-confirmed, but at an address that is NOT this attachment's own
    # DIRECT destination ("remote-addr", _create_draft()'s fixed route_id).
    _bind_recipient(conn, recipient_principal, transport_address="a-completely-different-address")
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.ENCRYPTING:
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.ENCRYPTING

    assert service._resolve_recipient_identities(attachment_id) == {}


def test_tick_fails_attachment_instead_of_encrypting_to_a_key_bound_at_a_different_address(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="a-completely-different-address")
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        state = sender.get_state(conn, attachment_id)
        if state in (sender.FAILED_VALIDATION, sender.SENT):
            break
        service.tick()

    assert sender.get_state(conn, attachment_id) == sender.FAILED_VALIDATION
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row["error_code"] == "recipient_not_trusted"
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is None


_MISMATCHED_ADAPTER_ID = "a-completely-different-adapter"


def _service_with_mismatched_key_exchange_adapter(conn, wsm, principal, provider_registry, connectivity_monitor, relay_client):
    """PR #231 review (4th pass): `KeyExchangeCoordinator.
    get_binding_by_key_id()` itself filters by its OWN configured
    `adapter_id` (one coordinator per workspace/adapter - module
    docstring), so a binding recorded under a genuinely different
    adapter_id than the coordinator's own is simply never found at all
    (silently skipped by the existing `binding is None` guard, not by
    the new adapter check this test is actually after). To exercise
    `binding.adapter_id != delivery_row["adapter_id"]` for real, this
    builds a service around a KeyExchangeCoordinator deliberately
    misconfigured with `_MISMATCHED_ADAPTER_ID` - so its lookup finds a
    binding recorded under *that* adapter_id, while `_create_draft()`'s
    delivery record (built separately, always ADAPTER_ID="fake-text")
    still names the real one. This simulates a coordinator/delivery-
    adapter configuration mismatch - not reachable through this
    module's normal fixtures, which always keep the two in sync, but a
    real defense-in-depth case this check exists to catch regardless."""
    mismatched_key_exchange = KeyExchangeCoordinator(conn, wsm, principal, _MISMATCHED_ADAPTER_ID)
    delivery_adapter = FakeTextAdapter(InMemoryEther(), "local-addr")
    return AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=mismatched_key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=delivery_adapter, relay_client_factory=lambda provider_id_text: relay_client,
        max_per_tick=8,
    )


def test_resolve_recipient_identities_excludes_a_binding_recorded_under_a_different_adapter(
    conn, wsm, principal, provider_registry, registered_provider, connectivity_monitor, relay_client, remote_recipient, tmp_path, caplog
):
    """PR #231 review (4th pass): the binding's own adapter_id must also
    match the delivery's adapter_id - a binding whose address AND key
    are otherwise perfectly valid, but recorded under a *different*
    adapter than the one this attachment is actually being sent
    through, must still be excluded. Address alone is not the whole
    trust relationship TOFU establishes; the adapter it was bound
    through is part of it too."""
    import logging

    _, _, recipient_principal = remote_recipient
    mismatched_service = _service_with_mismatched_key_exchange_adapter(
        conn, wsm, principal, provider_registry, connectivity_monitor, relay_client
    )
    # Same transport_address _create_draft() uses as its own route_id
    # ("remote-addr") and a genuinely valid, TOFU-confirmed key - bound
    # under _MISMATCHED_ADAPTER_ID, matching mismatched_service's own
    # (deliberately misconfigured) key_exchange, but NOT ADAPTER_ID
    # ("fake-text"), which _create_draft()'s delivery record always uses.
    _insert_binding(
        conn, workspace_id="local", adapter_id=_MISMATCHED_ADAPTER_ID,
        transport_address="remote-addr", principal_id=recipient_principal.principal_id,
        sender_key_id=recipient_principal.key_id, public_identity=recipient_principal.public_identity,
        now=time.time(),
    )
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.ENCRYPTING:
            break
        mismatched_service.tick()
    assert sender.get_state(conn, attachment_id) == sender.ENCRYPTING

    # Sanity check that the binding really is found (otherwise this test
    # would trivially pass via the unrelated "binding is None" guard).
    found_binding = mismatched_service._key_exchange.get_binding_by_key_id(recipient_principal.key_id)
    assert found_binding is not None
    assert found_binding.adapter_id == _MISMATCHED_ADAPTER_ID

    with caplog.at_level(logging.INFO, logger="meshsrv.attachments.service"):
        result = mismatched_service._resolve_recipient_identities(attachment_id)
    assert result == {}

    rejection_records = [r for r in caplog.records if "different adapter" in r.message]
    assert len(rejection_records) == 1
    # Same logging-hygiene requirement as the other rejection paths -
    # no raw key_id/attachment_id/transport_address in the log line.
    assert recipient_principal.key_id not in rejection_records[0].message
    assert attachment_id not in rejection_records[0].message
    assert "remote-addr" not in rejection_records[0].message


def test_tick_fails_attachment_instead_of_encrypting_to_a_key_bound_under_a_different_adapter(
    conn, wsm, principal, provider_registry, registered_provider, connectivity_monitor, relay_client, remote_recipient, tmp_path
):
    _, _, recipient_principal = remote_recipient
    mismatched_service = _service_with_mismatched_key_exchange_adapter(
        conn, wsm, principal, provider_registry, connectivity_monitor, relay_client
    )
    _insert_binding(
        conn, workspace_id="local", adapter_id=_MISMATCHED_ADAPTER_ID,
        transport_address="remote-addr", principal_id=recipient_principal.principal_id,
        sender_key_id=recipient_principal.key_id, public_identity=recipient_principal.public_identity,
        now=time.time(),
    )
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        state = sender.get_state(conn, attachment_id)
        if state in (sender.FAILED_VALIDATION, sender.SENT):
            break
        mismatched_service.tick()

    assert sender.get_state(conn, attachment_id) == sender.FAILED_VALIDATION
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row["error_code"] == "recipient_not_trusted"
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is None


def test_resolve_recipient_identities_accepts_a_binding_matching_the_destination_address(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """Sanity check paired with the exclusion tests above: a binding whose
    transport_address DOES match the attachment's own DIRECT destination
    must still be accepted - this fix must not turn into a blanket
    rejection of every recipient."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")  # matches _create_draft()'s route_id
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.ENCRYPTING:
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.ENCRYPTING

    identities = service._resolve_recipient_identities(attachment_id)
    assert recipient_principal.key_id in identities
    assert identities[recipient_principal.key_id] == recipient_principal.public_identity


# ---- PR #231 review (3rd pass): TOFU verification tightened further -------
# require a delivery record, a supported DIRECT route, and a matching
# delivery adapter, all fail-closed BEFORE encryption - the earlier pass's
# fix only checked the transport address when a delivery record happened
# to already be DIRECT, silently skipping the check (fail OPEN) for a
# missing or non-DIRECT delivery.


def _draft_to_encrypting(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, service):
    """Shared setup: a real draft with a trusted binding, ticked forward
    to ENCRYPTING - the same starting point every test below needs before
    tampering with its own attachment_deliveries row."""
    attachment_id = _create_draft(
        conn, wsm, principal, recipient_principal=recipient_principal,
        registered_provider=registered_provider, tmp_path=tmp_path,
    )
    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.ENCRYPTING:
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.ENCRYPTING
    return attachment_id


def test_resolve_recipient_identities_excludes_everyone_when_delivery_record_is_missing(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")
    attachment_id = _draft_to_encrypting(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, service)

    conn.execute("DELETE FROM attachment_deliveries WHERE attachment_id = ?", (attachment_id,))
    conn.commit()

    assert service._resolve_recipient_identities(attachment_id) == {}


def test_tick_fails_attachment_when_delivery_record_is_missing(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")
    attachment_id = _draft_to_encrypting(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, service)
    conn.execute("DELETE FROM attachment_deliveries WHERE attachment_id = ?", (attachment_id,))
    conn.commit()

    for _ in range(20):
        state = sender.get_state(conn, attachment_id)
        if state in (sender.FAILED_VALIDATION, sender.SENT):
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.FAILED_VALIDATION


def test_resolve_recipient_identities_excludes_everyone_for_a_non_direct_route(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """For the current MVP, only a DIRECT delivery route can be
    TOFU-verified at all - a channel broadcast's route_id names the
    channel, not any one recipient's address, so this must fail closed
    rather than silently skip the address check (the earlier pass's bug)."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")
    attachment_id = _draft_to_encrypting(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, service)

    conn.execute("UPDATE attachment_deliveries SET route_type = 'CHANNEL' WHERE attachment_id = ?", (attachment_id,))
    conn.commit()

    assert service._resolve_recipient_identities(attachment_id) == {}


def test_resolve_recipient_identities_excludes_everyone_when_delivery_adapter_does_not_match(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """The delivery record's own adapter_id must match the service's
    actually-configured delivery_adapter - encrypting for a delivery that
    names a different adapter than the one this service is about to send
    through is the same "trust the key, not the destination" gap as a
    transport-address mismatch, just one field over."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")
    attachment_id = _draft_to_encrypting(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, service)

    conn.execute(
        "UPDATE attachment_deliveries SET adapter_id = 'a-completely-different-adapter' WHERE attachment_id = ?",
        (attachment_id,),
    )
    conn.commit()

    assert service._resolve_recipient_identities(attachment_id) == {}


def test_tick_fails_attachment_for_a_non_direct_route(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")
    attachment_id = _draft_to_encrypting(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, service)
    conn.execute("UPDATE attachment_deliveries SET route_type = 'CHANNEL' WHERE attachment_id = ?", (attachment_id,))
    conn.commit()

    for _ in range(20):
        state = sender.get_state(conn, attachment_id)
        if state in (sender.FAILED_VALIDATION, sender.SENT):
            break
        service.tick()
    assert sender.get_state(conn, attachment_id) == sender.FAILED_VALIDATION


def test_rejection_log_lines_never_include_raw_address_key_id_or_attachment_id(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service, caplog
):
    """PR #231 review (3rd pass): the rejection path must not leak raw
    transport addresses, key IDs, or attachment IDs into logs - checked
    across four rejection reasons in one pass (missing delivery,
    non-DIRECT route, delivery adapter mismatch, address mismatch). See
    the dedicated binding-adapter-mismatch tests below for that fifth
    reason's own log-content assertion - it needs a differently-
    configured service (a mismatched KeyExchangeCoordinator) that
    doesn't fit this shared-fixture test's setup."""
    import logging

    _, _, recipient_principal = remote_recipient
    sensitive_values = [recipient_principal.key_id, "remote-addr", "a-different-address"]

    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")

    scenarios = []
    for mutate in (
        lambda aid: conn.execute("DELETE FROM attachment_deliveries WHERE attachment_id = ?", (aid,)),
        lambda aid: conn.execute("UPDATE attachment_deliveries SET route_type = 'CHANNEL' WHERE attachment_id = ?", (aid,)),
        lambda aid: conn.execute("UPDATE attachment_deliveries SET adapter_id = 'other' WHERE attachment_id = ?", (aid,)),
    ):
        attachment_id = _draft_to_encrypting(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, service)
        mutate(attachment_id)
        conn.commit()
        scenarios.append(attachment_id)

    # A fourth scenario: a real address mismatch (a second, independent
    # recipient principal, bound at a different address than this
    # attachment's own delivery route_id).
    conn3 = sqlite3.connect(str(tmp_path / "second_recipient_attachments.db"))
    conn3.execute("PRAGMA foreign_keys = ON")
    migrations.migrate(conn3)
    wsm3 = MCAWorkspaceManager(str(tmp_path / "second_recipient_data"))
    mismatched_principal = identity.ensure_principal(conn3, wsm3, "local")
    sensitive_values.append(mismatched_principal.key_id)
    _bind_recipient(conn, mismatched_principal, transport_address="a-different-address")
    mismatched_attachment_id = _create_draft(
        conn, wsm, principal, recipient_principal=mismatched_principal,
        registered_provider=registered_provider, tmp_path=tmp_path,
    )
    for _ in range(20):
        if sender.get_state(conn, mismatched_attachment_id) == sender.ENCRYPTING:
            break
        service.tick()
    assert sender.get_state(conn, mismatched_attachment_id) == sender.ENCRYPTING
    scenarios.append(mismatched_attachment_id)

    with caplog.at_level(logging.INFO, logger="meshsrv.attachments.service"):
        for attachment_id in scenarios:
            service._resolve_recipient_identities(attachment_id)

    rejection_records = [r for r in caplog.records if "excluding" in r.message]
    assert len(rejection_records) >= 4
    for record in rejection_records:
        for sensitive in sensitive_values:
            assert sensitive not in record.message
        for attachment_id in scenarios:
            assert attachment_id not in record.message


def test_tick_fails_attachment_when_binding_has_a_pending_key_change(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service, key_exchange):
    """KEY_CHANGED (a conflicting KEY_ANNOUNCE parked in
    pending_public_identity) must be treated the same as KEY_UNVERIFIED:
    the identity this attachment was drafted against may no longer be
    the recipient's current key at all."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    # Simulate a later, conflicting KEY_ANNOUNCE parking a pending key
    # change on this same binding, without disturbing its now-trusted
    # public_identity/tofu_confirmed_at columns.
    conn.execute(
        "UPDATE mca_recipient_bindings SET pending_public_identity = ?, pending_key_epoch = 1, pending_detected_at = ? "
        "WHERE sender_key_id = ?",
        ((b"\x99" * 32).hex(), time.time(), recipient_principal.key_id),
    )
    conn.commit()

    for _ in range(20):
        state = sender.get_state(conn, attachment_id)
        if state in (sender.FAILED_VALIDATION, sender.SENT):
            break
        service.tick()

    assert sender.get_state(conn, attachment_id) == sender.FAILED_VALIDATION


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


# ---- receiver ACK outbox: real dispatch (PR #227 defect #1) ---------------


def test_tick_dispatches_a_real_ack_received_back_to_the_sender(conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient, tmp_path):
    """End-to-end regression coverage for a reviewer-found defect:
    handle_offer()/run_step() only ever returned encoded ACK frames as
    ReceiveResult.replies - both real production callers (mca_runtime.py,
    this module's own _step_received()) discarded that return value
    entirely, so no ACK was ever actually transmitted. This drives the
    fix through AttachmentsService.tick() exactly as production does:
    handle_offer(source_address=...) persists the reply route, tick()'s
    own dispatch step sends the queued ACK through a real DeliveryAdapter,
    and the frame is confirmed to actually arrive - decodable and
    correctly signed - at the sending node's own inbox."""
    sender_conn, sender_wsm, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")

    shared_ether = InMemoryEther()
    receiver_adapter = FakeTextAdapter(shared_ether, "receiver-addr")
    sender_adapter = FakeTextAdapter(shared_ether, "remote-addr")

    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=receiver_adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )

    provider_id = _raw_provider_id(registered_provider)
    content = b"a real received file"
    source_path = tmp_path / "incoming.txt"
    source_path.write_bytes(content)
    sent_attachment_id = sender.create_draft(
        sender_conn, sender_wsm, sender_principal,
        workspace_id="local", source_path=str(source_path), file_name="incoming.txt", mime_type="text/plain",
        recipients=[sender.RecipientTarget(public_identity=principal.public_identity, key_id=principal.key_id)],
        adapter_id=ADAPTER_ID, connector_profile_id="default", route_type="DIRECT", route_id="receiver-addr",
        provider_id=provider_id,
    )
    recipient_identities = {principal.key_id: principal.public_identity}
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
        provider_id=provider_id, transfer_id=transfer_id, sender_key_id=bytes.fromhex(sender_principal.key_id),
        kind=0, size_bucket=1, hard_expires_at=hard_expires_at, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        source_address="remote-addr",
        # PR #231 review (3rd pass): the dispatch step now fails closed
        # on a missing adapter_id/connector_profile_id, not just a
        # mismatched one - a full ReplyRoute (matching receiver_adapter,
        # the real adapter this service dispatches through below) is
        # required for the ACK to actually reach the wire in this test.
        reply_route=receiver.ReplyRoute(
            adapter_id=receiver_adapter.adapter_id, connector_profile_id=receiver_adapter.connector_profile_id,
            route_type="DIRECT", route_id="remote-addr", destination_address="remote-addr",
        ),
    )
    received_attachment_id = result.attachment_id

    # Enqueued, not yet sent - the send-before-mark-sent ordering this fix
    # establishes means nothing should be on the wire yet.
    assert shared_ether.drain("remote-addr") == []
    pending_state = conn.execute(
        "SELECT state FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (received_attachment_id,),
    ).fetchone()[0]
    assert pending_state == "PENDING"

    receiver_service.tick()

    sent_row = conn.execute(
        "SELECT state, sent_at FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (received_attachment_id,),
    ).fetchone()
    assert sent_row[0] == "SENT"
    assert sent_row[1] is not None

    events = shared_ether.drain("remote-addr")
    assert len(events) == 1
    envelope = sender_adapter.ingest(events[0])
    ack_fields = codec.decode_simple_ack(
        envelope.logical_message, codec.MessageType.ACK_RECEIVED, verify_key=VerifyKey(principal.public_identity)
    )
    assert ack_fields.transfer_id == transfer_id


def test_dispatch_marks_undeliverable_when_persisted_adapter_id_does_not_match(
    conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """PR #231 review (2nd pass): the reply route's own adapter_id/
    connector_profile_id (persisted at handle_offer() time from the real
    DeliveryEnvelope that received the OFFER) were being persisted but
    never actually consumed by the dispatch step - it built a Route from
    only route_type/route_id and sent it through whichever delivery_
    adapter the service happened to be constructed with, regardless of
    whether that's really the adapter this reply was recorded against.
    Proves the fix: a reply whose persisted adapter_id disagrees with the
    dispatching service's own adapter is marked UNDELIVERABLE and never
    reaches the wire, rather than being silently sent through the wrong
    adapter."""
    sender_conn, sender_wsm, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")

    ether = InMemoryEther()
    receiver_adapter = FakeTextAdapter(ether, "receiver-addr")
    assert receiver_adapter.adapter_id == "fake-text"

    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=receiver_adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )

    sender_signing_key = identity.load_signing_key(sender_wsm, sender_principal)
    transfer_id = uuid.uuid4().bytes
    offer_fields = codec.OfferFields(
        provider_id=_raw_provider_id(registered_provider), transfer_id=transfer_id,
        sender_key_id=bytes.fromhex(sender_principal.key_id), kind=0, size_bucket=1,
        hard_expires_at=int(time.time()) + 3600, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)

    # Persisted with a DIFFERENT adapter_id than the one this service is
    # actually constructed with ("fake-text") - simulates a stale/
    # mismatched reply route (e.g. a future multi-adapter world, or a
    # row surviving some hypothetical adapter reconfiguration).
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        source_address="remote-addr",
        reply_route=receiver.ReplyRoute(
            adapter_id="a-completely-different-adapter", connector_profile_id="default",
            route_type="DIRECT", route_id="remote-addr", destination_address="remote-addr",
        ),
    )

    receiver_service.tick()

    row = conn.execute(
        "SELECT state, last_error FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result.attachment_id,),
    ).fetchone()
    assert row[0] == "UNDELIVERABLE"
    assert "reply_adapter_mismatch" in row[1]
    assert ether.drain("remote-addr") == []


# ---- PR #231 review (3rd pass): strict adapter/connector/route validation -


def _dispatch_one_offer_reply(
    conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client,
    remote_recipient, *, receiver_adapter, reply_route,
):
    """Shared setup for the strict-validation tests below: binds the
    remote sender, builds a real signed OFFER, hands it to handle_offer()
    with the given reply_route, ticks a real AttachmentsService built
    around receiver_adapter, and returns (attachment_id, mca_outgoing_
    replies row). One helper rather than duplicating this ~20-line setup
    per rejection reason."""
    sender_conn, sender_wsm, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")

    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=receiver_adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )

    sender_signing_key = identity.load_signing_key(sender_wsm, sender_principal)
    offer_fields = codec.OfferFields(
        provider_id=_raw_provider_id(registered_provider), transfer_id=uuid.uuid4().bytes,
        sender_key_id=bytes.fromhex(sender_principal.key_id), kind=0, size_bucket=1,
        hard_expires_at=int(time.time()) + 3600, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        source_address="remote-addr", reply_route=reply_route,
    )
    receiver_service.tick()

    row = conn.execute(
        "SELECT state, last_error FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result.attachment_id,),
    ).fetchone()
    return result.attachment_id, row


def test_dispatch_marks_undeliverable_when_persisted_connector_profile_id_does_not_match(
    conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """Mirrors the adapter_id-mismatch test above, for connector_profile_id
    - the field this fix stops silently ignoring."""
    ether = InMemoryEther()
    receiver_adapter = FakeTextAdapter(ether, "receiver-addr")
    _, row = _dispatch_one_offer_reply(
        conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client,
        remote_recipient, receiver_adapter=receiver_adapter,
        reply_route=receiver.ReplyRoute(
            adapter_id=receiver_adapter.adapter_id, connector_profile_id="a-completely-different-connector",
            route_type="DIRECT", route_id="remote-addr", destination_address="remote-addr",
        ),
    )
    assert row[0] == "UNDELIVERABLE"
    assert "reply_connector_mismatch" in row[1]
    assert ether.drain("remote-addr") == []


def test_dispatch_marks_undeliverable_when_reply_route_is_entirely_missing(
    conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """No reply_route at all AND no source_address - route_type/route_id
    themselves are NULL, the original (pre-3rd-pass) undeliverable case."""
    ether = InMemoryEther()
    receiver_adapter = FakeTextAdapter(ether, "receiver-addr")
    sender_conn, sender_wsm, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")
    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=receiver_adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )
    sender_signing_key = identity.load_signing_key(sender_wsm, sender_principal)
    offer_fields = codec.OfferFields(
        provider_id=_raw_provider_id(registered_provider), transfer_id=uuid.uuid4().bytes,
        sender_key_id=bytes.fromhex(sender_principal.key_id), kind=0, size_bucket=1,
        hard_expires_at=int(time.time()) + 3600, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        # No source_address, no reply_route at all.
    )
    receiver_service.tick()
    row = conn.execute(
        "SELECT state, last_error FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result.attachment_id,),
    ).fetchone()
    assert row[0] == "UNDELIVERABLE"
    assert row[1] == "no_reply_route_recorded"


def test_dispatch_marks_undeliverable_when_route_present_but_adapter_identity_missing(
    conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """PR #231 review (3rd pass): a *tightening* from an earlier pass of
    this fix - route_type/route_id ARE recorded (bare source_address,
    no reply_route), but adapter_id/connector_profile_id are NOT. This
    used to dispatch anyway ("trust the currently-configured adapter");
    it must now fail closed, the same as a confirmed mismatch."""
    ether = InMemoryEther()
    receiver_adapter = FakeTextAdapter(ether, "receiver-addr")
    sender_conn, sender_wsm, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")
    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=receiver_adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )
    sender_signing_key = identity.load_signing_key(sender_wsm, sender_principal)
    offer_fields = codec.OfferFields(
        provider_id=_raw_provider_id(registered_provider), transfer_id=uuid.uuid4().bytes,
        sender_key_id=bytes.fromhex(sender_principal.key_id), kind=0, size_bucket=1,
        hard_expires_at=int(time.time()) + 3600, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        source_address="remote-addr",  # route recorded, but no reply_route -> no adapter identity
    )
    receiver_service.tick()
    row = conn.execute(
        "SELECT state, last_error FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result.attachment_id,),
    ).fetchone()
    assert row[0] == "UNDELIVERABLE"
    assert "reply_adapter_mismatch" in row[1]
    assert "persisted=None" in row[1]
    assert ether.drain("remote-addr") == []


def test_dispatch_uses_persisted_destination_address_even_when_it_differs_from_route_id(
    conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """destination_address is a separate persisted field precisely so it
    can differ from route_id (ReplyRoute's own docstring: a future non-
    DIRECT route shape) - proves the dispatch step actually sends to
    destination_address, not silently back to route_id, when the two are
    deliberately set apart. FakeTextAdapter.send() delivers into the
    ether keyed by Route.destination_address, so draining the
    destination_address inbox (not the route_id one) is the real proof."""
    ether = InMemoryEther()
    receiver_adapter = FakeTextAdapter(ether, "receiver-addr")
    # A third inbox, distinct from both "receiver-addr" and "remote-addr" -
    # registering it up front (ether.register()) means drain() finds a
    # real (possibly-empty) inbox rather than a KeyError on a name the
    # ether has never seen.
    ether.register("remote-addr-real-destination")

    attachment_id, row = _dispatch_one_offer_reply(
        conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client,
        remote_recipient, receiver_adapter=receiver_adapter,
        reply_route=receiver.ReplyRoute(
            adapter_id=receiver_adapter.adapter_id, connector_profile_id=receiver_adapter.connector_profile_id,
            route_type="DIRECT", route_id="remote-addr", destination_address="remote-addr-real-destination",
        ),
    )
    assert row[0] == "SENT"
    assert ether.drain("remote-addr") == []  # nothing delivered to the bare route_id
    real_events = ether.drain("remote-addr-real-destination")
    assert len(real_events) == 1  # delivered to destination_address instead


def test_dispatch_marks_undeliverable_when_destination_address_is_missing_not_falling_back_to_route_id(
    conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """PR #231 review (4th pass): a missing persisted destination_address
    must fail closed (UNDELIVERABLE), not silently fall back to route_id.
    ReplyRoute.destination_address is a required dataclass field, so this
    can't happen through the normal handle_offer(reply_route=...) API -
    simulated here via direct SQL, standing in for a data-integrity
    anomaly (e.g. a row from an unexpected write path). Confirms the
    explicit check actually fires and nothing is sent to route_id as an
    implicit fallback."""
    ether = InMemoryEther()
    receiver_adapter = FakeTextAdapter(ether, "receiver-addr")

    sender_conn, sender_wsm, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")

    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=receiver_adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )
    sender_signing_key = identity.load_signing_key(sender_wsm, sender_principal)
    offer_fields = codec.OfferFields(
        provider_id=_raw_provider_id(registered_provider), transfer_id=uuid.uuid4().bytes,
        sender_key_id=bytes.fromhex(sender_principal.key_id), kind=0, size_bucket=1,
        hard_expires_at=int(time.time()) + 3600, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)
    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        source_address="remote-addr",
        reply_route=receiver.ReplyRoute(
            adapter_id=receiver_adapter.adapter_id, connector_profile_id=receiver_adapter.connector_profile_id,
            route_type="DIRECT", route_id="remote-addr", destination_address="remote-addr",
        ),
    )
    attachment_id = result.attachment_id

    # Simulate the anomaly: null out destination_address directly,
    # leaving adapter_id/connector_profile_id/route_type/route_id intact
    # and matching - only destination_address is missing.
    conn.execute("UPDATE attachments SET reply_destination_address = NULL WHERE id = ?", (attachment_id,))
    conn.commit()

    receiver_service.tick()

    row = conn.execute(
        "SELECT state, last_error FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (attachment_id,),
    ).fetchone()
    assert row[0] == "UNDELIVERABLE"
    assert row[1] == "reply_destination_address_missing"
    assert ether.drain("remote-addr") == []  # never silently sent to route_id either


def test_dispatch_retries_a_failed_send_instead_of_marking_it_sent(conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient):
    """A DeliveryAdapter.send() failure must never be confused with
    success: the queued reply stays PENDING (retried on a later tick,
    with backoff), not silently marked SENT or dropped."""

    class _AlwaysFailsToSendAdapter(FakeTextAdapter):
        def send(self, wire_payload, route, idempotency_key):
            raise RuntimeError("simulated transport failure")

    _, _, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")

    failing_adapter = _AlwaysFailsToSendAdapter(InMemoryEther(), "receiver-addr")
    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=failing_adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )

    sender_signing_key = identity.load_signing_key(remote_recipient[1], sender_principal)
    transfer_id = uuid.uuid4().bytes
    offer_fields = codec.OfferFields(
        provider_id=_raw_provider_id(registered_provider), transfer_id=transfer_id,
        sender_key_id=bytes.fromhex(sender_principal.key_id), kind=0, size_bucket=1,
        hard_expires_at=int(time.time()) + 3600, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
        source_address="remote-addr",
        # PR #231 review (3rd pass): required now that a missing adapter_id/
        # connector_profile_id fails closed - see the sibling dispatch test
        # above for the same fix.
        reply_route=receiver.ReplyRoute(
            adapter_id=failing_adapter.adapter_id, connector_profile_id=failing_adapter.connector_profile_id,
            route_type="DIRECT", route_id="remote-addr", destination_address="remote-addr",
        ),
    )

    before = conn.execute(
        "SELECT attempts, next_attempt_at FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result.attachment_id,),
    ).fetchone()
    assert before[0] == 0

    receiver_service.tick()

    after = conn.execute(
        "SELECT state, attempts, next_attempt_at, last_error FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result.attachment_id,),
    ).fetchone()
    assert after[0] == "PENDING"  # never marked SENT on a failed send
    assert after[1] == 1
    assert after[2] > before[1]  # backed off into the future
    assert "simulated transport failure" in after[3]


def test_handle_offer_without_source_address_never_dispatches(conn, wsm, principal, provider_registry, registered_provider, key_exchange, connectivity_monitor, relay_client, remote_recipient):
    """A caller that never learns/records a return route (this module's
    own non-integration tests calling handle_offer() with no
    source_address) must not have its queued ACK retried forever with no
    possible destination - it's marked UNDELIVERABLE on the first
    dispatch attempt instead."""
    _, _, sender_principal = remote_recipient
    _bind_recipient(conn, sender_principal, transport_address="remote-addr")

    adapter = FakeTextAdapter(InMemoryEther(), "receiver-addr")
    receiver_service = AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=adapter, relay_client_factory=lambda provider_id_text: relay_client,
    )

    sender_signing_key = identity.load_signing_key(remote_recipient[1], sender_principal)
    offer_fields = codec.OfferFields(
        provider_id=_raw_provider_id(registered_provider), transfer_id=uuid.uuid4().bytes,
        sender_key_id=bytes.fromhex(sender_principal.key_id), kind=0, size_bucket=1,
        hard_expires_at=int(time.time()) + 3600, flags=0,
    )
    raw_offer = codec.encode_offer(offer_fields, sender_signing_key)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=raw_offer, network_available=True,
    )

    receiver_service.tick()

    state = conn.execute(
        "SELECT state FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = 'ack_received_sent'",
        (result.attachment_id,),
    ).fetchone()[0]
    assert state == "UNDELIVERABLE"


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


# ---- Step 1.6A.3C worker commands: attachment_cancel + contact_request_key -


def _cancel_command(attachment_id):
    return Command(
        command_id=uuid.uuid4().hex,
        kind="attachment_cancel",
        payload={"attachment_id": attachment_id},
        created_at=time.time(),
    )


def _request_key_command(contact_id, *, adapter_id="meshtastic", route_id=None):
    return Command(
        command_id=uuid.uuid4().hex,
        kind="contact_request_key",
        payload={
            "contact_id": contact_id,
            "adapter_id": adapter_id,
            "route_id": route_id if route_id is not None else contact_id,
        },
        created_at=time.time(),
    )


def _insert_sender_state(conn, attachment_id, *, upload_id=None, revoke_token=None):
    """Manually stage an `mca_sender_state` row (the two remote-cleanup
    signals `_command_cancel` reads; the key/nonce columns are never touched
    by the handler, so dummy values suffice)."""
    conn.execute(
        """
        INSERT INTO mca_sender_state
            (attachment_id, data_key, nonce_prefix, upload_id, revoke_token, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (attachment_id, "aa" * 16, "bb" * 12, upload_id, revoke_token, int(time.time()), int(time.time())),
    )
    conn.commit()


def _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, delivery_adapter, relay_client_factory=None):
    """A service built around a caller-supplied delivery adapter / relay
    factory, so a single command's edge case can be driven against a
    deliberately broken or instrumented collaborator."""
    return AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, connectivity_monitor=connectivity_monitor,
        delivery_adapter=delivery_adapter, relay_client_factory=relay_client_factory,
        max_per_tick=8,
    )


class _StubRelayClient:
    """A RelayClient-shaped object whose `revoke()` records its arguments and
    either succeeds ("ok") or raises the configured exception - for pinning
    the remote-revoke half of cancel without the full mock Relay's session
    bookkeeping."""

    def __init__(self, revoke_result="ok"):
        self.revoke_calls = []
        self._revoke_result = revoke_result

    def revoke(self, transfer_id, revoke_token):
        self.revoke_calls.append((transfer_id, revoke_token))
        if isinstance(self._revoke_result, Exception):
            raise self._revoke_result
        return None


class _SentFalseAdapter(FakeTextAdapter):
    def send(self, wire_payload, route, idempotency_key):
        return DeliveryReceipt(sent=False, idempotency_key=idempotency_key, external_message_id=None, sent_at=None)


class _RaisingSendAdapter(FakeTextAdapter):
    def send(self, wire_payload, route, idempotency_key):
        raise DeliveryError("simulated send failure")


# ---- attachment_cancel -----------------------------------------------------


def test_command_cancel_local_only_marks_cancelled_and_clears_saved_path(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """No `mca_sender_state` row -> no remote half; the worker clears
    `saved_path` and calls `sender.cancel()` to CANCELLED, leaving no
    sender-state row and preserving history."""
    _, _, recipient_principal = remote_recipient
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    outcome = service._dispatcher.dispatch(_cancel_command(attachment_id))

    assert outcome.error_code is None
    assert outcome.resource_id == attachment_id
    assert dict(outcome.result) == {"attachment_id": attachment_id, "state": sender.CANCELLED}
    assert sender.get_state(conn, attachment_id) == sender.CANCELLED
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT saved_path FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row["saved_path"] is None
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is None


def test_command_cancel_unknown_id_is_not_found(service):
    outcome = service._dispatcher.dispatch(_cancel_command("0" * 32))
    assert outcome.error_code == "attachment_not_found"
    assert outcome.resource_id is None and outcome.result is None


def test_command_cancel_revalidates_state_from_the_row_not_the_snapshot(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """The worker re-reads the row and re-applies the precondition - a row
    that has since moved to SENT (e.g. between the request thread's snapshot
    read and execution) must fail with `invalid_state_transition`, leaving
    the row and its saved_path untouched."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)
    conn.execute("UPDATE attachments SET state = ? WHERE id = ?", (sender.SENT, attachment_id))
    conn.commit()

    outcome = service._dispatcher.dispatch(_cancel_command(attachment_id))

    assert outcome.error_code == "invalid_state_transition"
    assert sender.get_state(conn, attachment_id) == sender.SENT
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT saved_path FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row["saved_path"] is not None


def test_command_cancel_remote_revoke_404_is_confirmed_absence(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """A remote session exists (`upload_id` + `revoke_token` persisted), but
    the mock Relay has no object for the transfer_id -> `revoke()` raises
    `RelayHTTPError(404)`, which the handler treats as the object already
    gone, so the local half still completes to CANCELLED."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)
    _insert_sender_state(conn, attachment_id, upload_id="upload-1", revoke_token="revoke-1")

    outcome = service._dispatcher.dispatch(_cancel_command(attachment_id))

    assert outcome.error_code is None
    assert sender.get_state(conn, attachment_id) == sender.CANCELLED
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is None


def test_command_cancel_remote_revoke_success_revokes_then_cleans_up(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, registered_provider, remote_recipient, tmp_path
):
    """The remote half resolves *before* the local half: `revoke()` is called
    with the row's own transfer_id (hex-decoded) and revoke token, and only
    after it returns does the row become CANCELLED."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)
    transfer_id_hex = conn.execute("SELECT transfer_id FROM attachments WHERE id = ?", (attachment_id,)).fetchone()[0]
    _insert_sender_state(conn, attachment_id, upload_id="upload-1", revoke_token="revoke-1")

    stub = _StubRelayClient(revoke_result="ok")
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, FakeTextAdapter(InMemoryEther(), "local-addr"), relay_client_factory=lambda _: stub)

    outcome = svc._dispatcher.dispatch(_cancel_command(attachment_id))

    assert outcome.error_code is None
    assert stub.revoke_calls == [(bytes.fromhex(transfer_id_hex), "revoke-1")]
    assert sender.get_state(conn, attachment_id) == sender.CANCELLED


def test_command_cancel_remote_revoke_non_404_is_relay_unreachable_and_preserves_row(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, registered_provider, remote_recipient, tmp_path
):
    """A non-404 Relay failure must NOT be treated as absence: the command
    fails `relay_unreachable` and the row, its saved_path, and its sender
    state are all preserved for a manual retry."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)
    _insert_sender_state(conn, attachment_id, upload_id="upload-1", revoke_token="revoke-1")

    stub = _StubRelayClient(revoke_result=RelayHTTPError(500, "relay_down", "down"))
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, FakeTextAdapter(InMemoryEther(), "local-addr"), relay_client_factory=lambda _: stub)

    outcome = svc._dispatcher.dispatch(_cancel_command(attachment_id))

    assert outcome.error_code == "relay_unreachable"
    assert sender.get_state(conn, attachment_id) == sender.DRAFT
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is not None


def test_command_cancel_remote_session_without_a_revoke_token_is_relay_unreachable(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """`upload_id` persisted but `revoke_token` missing -> the worker cannot
    revoke the remote object, so it must fail `relay_unreachable` and
    preserve everything, rather than complete a cancel it cannot finish
    remotely."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)
    _insert_sender_state(conn, attachment_id, upload_id="upload-1", revoke_token=None)

    outcome = service._dispatcher.dispatch(_cancel_command(attachment_id))

    assert outcome.error_code == "relay_unreachable"
    assert sender.get_state(conn, attachment_id) == sender.DRAFT
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is not None


def test_command_cancel_spool_unlink_failure_is_spool_cleanup_failed(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """A spool path that cannot be unlinked (here: a directory at the spool
    path, which `Path.unlink()` refuses) fails `spool_cleanup_failed` with
    the row unchanged - the cancel does not silently proceed past a spool it
    could not delete."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal, registered_provider=registered_provider, tmp_path=tmp_path)

    spool_path = service._spool_path_for(attachment_id)
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    spool_path.mkdir()  # a directory, not a file -> unlink() raises IsADirectoryError

    outcome = service._dispatcher.dispatch(_cancel_command(attachment_id))

    assert outcome.error_code == "spool_cleanup_failed"
    assert sender.get_state(conn, attachment_id) == sender.DRAFT


# ---- contact_request_key ---------------------------------------------------


def test_command_request_key_sends_direct_and_persists_the_quota(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client
):
    """The full happy path: revalidate -> rate-limit check (never asked ->
    allowed) -> build_key_request -> encode/send over the fixed DIRECT route
    (route_id == destination_address == the contact) -> `sent == True` ->
    persist the quota timestamp -> succeeded with `{"contact_id", "status":
    "requested"}`. The message actually reaches the contact's inbox."""
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "local-addr")
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, adapter, relay_client_factory=lambda _: relay_client)
    contact = "!756f9960"

    outcome = svc._dispatcher.dispatch(_request_key_command(contact))

    assert outcome.error_code is None
    assert outcome.resource_id == contact
    assert dict(outcome.result) == {"contact_id": contact, "status": "requested"}

    events = ether.drain(contact)
    assert len(events) == 1
    assert events[0]["from"] == "local-addr"
    assert events[0]["idempotency_key"]  # the command id, used as the send idempotency key

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_request_sent_at FROM mca_key_exchange_contact_state WHERE workspace_id = ? AND adapter_id = ? AND source_address = ?",
        (principal.workspace_id, ADAPTER_ID, contact),
    ).fetchone()
    assert row is not None and row["last_request_sent_at"] is not None


def test_command_request_key_revalidates_and_rejects_an_already_ready_contact(
    conn, remote_recipient, service
):
    """The worker re-reads the live binding, so a contact that became
    `MCA_READY` since the request thread's snapshot read fails with
    `key_already_known` and sends nothing."""
    _, _, recipient_principal = remote_recipient
    contact = "!756f9960"
    _bind_recipient(conn, recipient_principal, transport_address=contact)

    outcome = service._dispatcher.dispatch(_request_key_command(contact))

    assert outcome.error_code == "key_already_known"


def test_command_request_key_is_rate_limited(service, key_exchange):
    contact = "!756f9960"
    key_exchange.record_key_request_sent(contact, time.time())

    outcome = service._dispatcher.dispatch(_request_key_command(contact))

    assert outcome.error_code == "rate_limited"


def test_command_request_key_radio_unavailable_without_a_delivery_adapter(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor
):
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, None)
    outcome = svc._dispatcher.dispatch(_request_key_command("!756f9960"))
    assert outcome.error_code == "radio_unavailable"


def test_command_request_key_radio_unavailable_when_the_receipt_is_not_sent(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client
):
    """`send()` returning `sent=False` consumes no quota and fails
    `radio_unavailable`."""
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, _SentFalseAdapter(InMemoryEther(), "local-addr"), relay_client_factory=lambda _: relay_client)
    contact = "!756f9960"

    outcome = svc._dispatcher.dispatch(_request_key_command(contact))

    assert outcome.error_code == "radio_unavailable"
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_request_sent_at FROM mca_key_exchange_contact_state WHERE workspace_id = ? AND adapter_id = ? AND source_address = ?",
        (principal.workspace_id, ADAPTER_ID, contact),
    ).fetchone()
    assert row is None or row["last_request_sent_at"] is None  # no quota consumed


def test_command_request_key_radio_unavailable_when_send_raises(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client
):
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, _RaisingSendAdapter(InMemoryEther(), "local-addr"), relay_client_factory=lambda _: relay_client)
    outcome = svc._dispatcher.dispatch(_request_key_command("!756f9960"))
    assert outcome.error_code == "radio_unavailable"


def test_command_request_key_revalidates_contact_id_and_route(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client
):
    """The worker never trusts the queue's payload: a malformed contact id,
    a non-`meshtastic` adapter, or a route that disagrees with the contact
    all fail `invalid_contact_id` before any send."""
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, FakeTextAdapter(InMemoryEther(), "local-addr"), relay_client_factory=lambda _: relay_client)

    cases = [
        _request_key_command("not-hex"),
        _request_key_command("!756f9960", adapter_id="not-meshtastic"),
        _request_key_command("!756f9960", route_id="!aaaaaaaa"),
    ]
    for command in cases:
        outcome = svc._dispatcher.dispatch(command)
        assert outcome.error_code == "invalid_contact_id"


# ---- PR 1: automatic missing-key request (recoverable WAITING_KEY) -------


def _build_offer_from(principal2, signing_key):
    return codec.encode_offer(
        codec.OfferFields(
            provider_id=uuid.uuid4().bytes[:8],
            transfer_id=uuid.uuid4().bytes,
            sender_key_id=bytes.fromhex(principal2.key_id),
            kind=0,
            size_bucket=1,
            hard_expires_at=int(time.time()) + 3600,
            flags=0,
        ),
        signing_key,
    )


def test_auto_request_missing_key_sends_rate_limited_key_request(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """PR 1: a received transfer parked in WAITING_KEY with an unknown signer
    is proactively asked for its key once per rate-limit window, with the
    request persisted (`last_request_sent_at`) so a restart does not reset
    the throttle."""
    _, wsm2, principal2 = remote_recipient
    contact = "!756f9960"
    signing_key = identity.load_signing_key(wsm2, principal2)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=_build_offer_from(principal2, signing_key),
        network_available=True, source_address=contact,
    )
    assert result.state == receiver.WAITING_KEY

    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "local-addr")
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, adapter, relay_client_factory=lambda _: relay_client)

    svc._auto_request_missing_keys()

    events = ether.drain(contact)
    assert len(events) == 1
    assert codec.peek_message_type(codec.from_text(events[0]["text"])) == codec.MessageType.KEY_REQUEST
    assert events[0]["idempotency_key"] == f"auto-key-request-{result.attachment_id}"

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT last_request_sent_at FROM mca_key_exchange_contact_state WHERE workspace_id = ? AND adapter_id = ? AND source_address = ?",
        (principal.workspace_id, ADAPTER_ID, contact),
    ).fetchone()
    assert row is not None and row["last_request_sent_at"] is not None

    # A second pass within the same window is rate-limited: no new send.
    svc._auto_request_missing_keys()
    assert ether.drain(contact) == []


def test_auto_request_missing_key_skips_once_the_key_is_known(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client, remote_recipient
):
    """PR 1: once ANY binding for the signer's key exists (a KEY_ANNOUNCE
    arrived and is pending confirmation), the auto-request stops - the trust
    gate in `_step_waiting_key`, not a fresh key request, owns what happens
    next."""
    _, wsm2, principal2 = remote_recipient
    contact = "!756f9960"
    signing_key = identity.load_signing_key(wsm2, principal2)

    result = receiver.handle_offer(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
        key_exchange=key_exchange, raw_offer=_build_offer_from(principal2, signing_key),
        network_available=True, source_address=contact,
    )
    assert result.state == receiver.WAITING_KEY

    # A KEY_ANNOUNCE arrives: an UNVERIFIED binding now exists for this key.
    _bind_recipient_unconfirmed(conn, principal2, transport_address=contact)

    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "local-addr")
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, adapter, relay_client_factory=lambda _: relay_client)

    svc._auto_request_missing_keys()

    assert ether.drain(contact) == []  # no request: the key is already known


def test_auto_request_missing_key_caps_sends_per_tick(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, relay_client
):
    """PR 1: a flood of distinct unknown-key OFFERs must not turn into a
    like-sized burst of outbound KEY_REQUESTs - one tick sends at most
    MAX_AUTO_KEY_REQUESTS_PER_TICK requests, and the next tick drains the
    rest (fair, oldest-first) rather than them being silently dropped."""
    ether = InMemoryEther()
    adapter = FakeTextAdapter(ether, "local-addr")
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, adapter, relay_client_factory=lambda _: relay_client)

    total = MAX_AUTO_KEY_REQUESTS_PER_TICK + 5
    addresses = [f"!000000{i:02x}" for i in range(total)]
    for i, contact in enumerate(addresses):
        sk = SigningKey.generate()
        fake_principal = type("P", (), {"key_id": identity.compute_key_id(bytes(sk.verify_key))})()
        raw_offer = codec.encode_offer(
            codec.OfferFields(
                provider_id=uuid.uuid4().bytes[:8],
                transfer_id=uuid.uuid4().bytes,
                sender_key_id=bytes.fromhex(fake_principal.key_id),
                kind=0,
                size_bucket=1,
                hard_expires_at=int(time.time()) + 3600,
                flags=0,
            ),
            sk,
        )
        result = receiver.handle_offer(
            conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry,
            key_exchange=key_exchange, raw_offer=raw_offer, network_available=True, source_address=contact,
        )
        assert result.state == receiver.WAITING_KEY

    # First tick: exactly the cap, no more.
    svc._auto_request_missing_keys()
    sent_first = sum(len(ether.drain(addr)) for addr in addresses)
    assert sent_first == MAX_AUTO_KEY_REQUESTS_PER_TICK

    # Second tick: the remainder (still under the cap), not dropped.
    svc._auto_request_missing_keys()
    sent_second = sum(len(ether.drain(addr)) for addr in addresses)
    assert sent_second == total - MAX_AUTO_KEY_REQUESTS_PER_TICK


# ---- Step 1.6A.4: provider onboarding/management handlers ----------------
#
# The worker is the sole executor of DNS/HTTP (probe), SQLite writes
# (register/update/default/remove), and token-file side effects
# (set/clear token) - the request thread only enqueues. These tests drive
# the eight `_command_provider_*` handlers directly, with the Relay client
# swapped for a fake (never real DNS/HTTP), and pin: the two-phase probe
# (single-use consume, fingerprint match, identity fields come from the
# probe not the browser), the MAX_PROVIDER_PROFILES/idempotent-re-register
# gate, the CLEAR sentinel, and that no result ever echoes the upload
# token or the raw service key.


def _provider_command(kind, payload):
    return Command(
        command_id=uuid.uuid4().hex,
        kind=kind,
        payload=payload,
        created_at=time.time(),
    )


_RELAY_KEY = bytes(range(32))
_RELAY_ORIGIN = "https://relay.example.net"
_RELAY_FINGERPRINT = hashlib.sha256(_RELAY_KEY).hexdigest()


def _probe_record(*, probe_id="probe-1", max_ciphertext_bytes=10_000_000, fingerprint=None):
    """A completed phase-1 probe record, as the worker would have stored
    after `provider_probe` (identity fields server-fetched, single-use)."""
    return ProbeRecord(
        probe_id=probe_id,
        origin=_RELAY_ORIGIN,
        provider_id=compute_provider_id(_RELAY_ORIGIN, _RELAY_KEY),
        service_public_key=_RELAY_KEY,
        service_key_fingerprint=fingerprint or _RELAY_FINGERPRINT,
        protocol_version=None,
        max_ciphertext_bytes=max_ciphertext_bytes,
        min_ttl_seconds=None,
        max_ttl_seconds=86400,
        expires_at=time.time() + 100,
        status=PROBE_STATUS_PROBED,
    )


def _relay_info():
    return RelayInfo(
        protocol="MCA/1",
        relay_version="1.0.0",
        base_url=_RELAY_ORIGIN,
        provider_id=compute_provider_id(_RELAY_ORIGIN, _RELAY_KEY),
        service_public_key=_RELAY_KEY,
        limits=RelayLimits(
            max_ciphertext_bytes=10_000_000,
            max_manifest_bytes=1024,
            max_chunk_bytes=1024,
            max_chunks=100,
            max_recipients=10,
            default_hard_ttl_seconds=3600,
            max_hard_ttl_seconds=86400,
            default_download_grace_seconds=3600,
        ),
        anonymous_upload=True,
        download_authorization="none",
    )


class _FakeRelayClient:
    """A RelayClient stand-in whose constructor performs no network I/O
    (the real one eagerly runs the §12 SSRF policy), returning a fixed
    `/v1/info`."""

    def __init__(self, base_url):
        self.base_url = base_url

    def get_info(self):
        return _relay_info()


def test_provider_probe_records_a_single_use_probe(monkeypatch, service):
    import meshsrv.attachments.service as service_module

    monkeypatch.setattr(service_module, "RelayClient", _FakeRelayClient)
    outcome = service._command_provider_probe(
        _provider_command("provider_probe", {"base_url": _RELAY_ORIGIN})
    )
    assert outcome.error_code is None
    assert outcome.resource_id  # the minted probe_id (32 hex chars)
    assert outcome.result["probe_id"] == outcome.resource_id
    assert outcome.result["origin"] == _RELAY_ORIGIN
    assert outcome.result["provider_id"] == compute_provider_id(_RELAY_ORIGIN, _RELAY_KEY)
    assert outcome.result["service_key_fingerprint"] == _RELAY_FINGERPRINT
    # The result is §7.9-safe: never the raw service key, never the internal
    # status field.
    assert "service_public_key" not in outcome.result
    assert "status" not in outcome.result
    assert len(service._probe_registry) == 1


def test_provider_probe_rejects_non_https_origin(service):
    outcome = service._command_provider_probe(
        _provider_command("provider_probe", {"base_url": "http://relay.example.net"})
    )
    assert outcome.error_code == "invalid_origin"


def test_provider_probe_rejects_missing_base_url(service):
    outcome = service._command_provider_probe(_provider_command("provider_probe", {}))
    assert outcome.error_code == "invalid_origin"


def test_provider_probe_surfaces_unroutable_origin(monkeypatch, service):
    import meshsrv.attachments.service as service_module

    class _UnroutableClient:
        def __init__(self, base_url):
            raise RelayNetworkError("origin_not_routable", "not routable")

    monkeypatch.setattr(service_module, "RelayClient", _UnroutableClient)
    outcome = service._command_provider_probe(
        _provider_command("provider_probe", {"base_url": _RELAY_ORIGIN})
    )
    assert outcome.error_code == "origin_not_routable"


def test_provider_register_materializes_profile_from_probe(service, provider_registry):
    service._probe_registry.add(_probe_record())
    outcome = service._command_provider_register(
        _provider_command(
            "provider_register",
            {
                "probe_id": "probe-1",
                "display_name": "My Relay",
                "fingerprint_confirmation": _RELAY_FINGERPRINT,
                "policy": {"kind": "own", "upload_allowed": True, "download_allowed": True},
            },
        )
    )
    assert outcome.error_code is None
    provider_id = outcome.resource_id
    profile = provider_registry.resolve(provider_id)
    assert profile is not None
    assert profile.display_name == "My Relay"
    # Identity fields come from the probe (server-fetched), never the browser.
    assert profile.origin == _RELAY_ORIGIN
    assert profile.service_public_key == _RELAY_KEY
    # First registration becomes the default (§7.12).
    assert provider_registry.get_default().provider_id == provider_id


def test_provider_register_consumes_probe_single_use(service, provider_registry):
    service._probe_registry.add(_probe_record())
    payload = {
        "probe_id": "probe-1",
        "display_name": "My Relay",
        "fingerprint_confirmation": _RELAY_FINGERPRINT,
        "policy": {},
    }
    first = service._command_provider_register(_provider_command("provider_register", dict(payload)))
    assert first.error_code is None
    # A replayed register with the same probe_id is a replay, never a second
    # registration - the probe was consumed.
    second = service._command_provider_register(_provider_command("provider_register", dict(payload)))
    assert second.error_code == "probe_id_used"


def test_provider_register_rejects_fingerprint_mismatch(service):
    service._probe_registry.add(_probe_record())
    outcome = service._command_provider_register(
        _provider_command(
            "provider_register",
            {
                "probe_id": "probe-1",
                "display_name": "My Relay",
                "fingerprint_confirmation": "00" * 32,
                "policy": {},
            },
        )
    )
    assert outcome.error_code == "provider_id_mismatch"


def test_provider_register_rejects_invalid_kind(service):
    service._probe_registry.add(_probe_record())
    outcome = service._command_provider_register(
        _provider_command(
            "provider_register",
            {
                "probe_id": "probe-1",
                "display_name": "My Relay",
                "fingerprint_confirmation": _RELAY_FINGERPRINT,
                "policy": {"kind": "bogus"},
            },
        )
    )
    assert outcome.error_code == "invalid_metadata"


def test_provider_register_rejects_ciphertext_over_probe_limit(service):
    service._probe_registry.add(_probe_record(max_ciphertext_bytes=1000))
    outcome = service._command_provider_register(
        _provider_command(
            "provider_register",
            {
                "probe_id": "probe-1",
                "display_name": "My Relay",
                "fingerprint_confirmation": _RELAY_FINGERPRINT,
                "policy": {"max_ciphertext_bytes": 1001},
            },
        )
    )
    assert outcome.error_code == "invalid_metadata"


def test_provider_register_idempotent_same_identity_does_not_consume_slot(
    service, provider_registry
):
    # Re-registering the same (origin, key) is idempotent and must not
    # consume an extra slot (MAX_PROVIDER_PROFILES enforcement, §7.12).
    for _ in range(2):
        service._probe_registry.add(_probe_record())
        outcome = service._command_provider_register(
            _provider_command(
                "provider_register",
                {
                    "probe_id": "probe-1",
                    "display_name": "My Relay",
                    "fingerprint_confirmation": _RELAY_FINGERPRINT,
                    "policy": {},
                },
            )
        )
        assert outcome.error_code is None
    assert len(provider_registry.list_providers()) == 1


def test_provider_update_edits_non_identity_fields(service, provider_registry, registered_provider):
    outcome = service._command_provider_update(
        _provider_command(
            "provider_update",
            {"provider_id": registered_provider.provider_id, "display_name": "Renamed", "enabled": False},
        )
    )
    assert outcome.error_code is None
    profile = provider_registry.resolve(registered_provider.provider_id)
    assert profile.display_name == "Renamed"
    assert profile.enabled is False


def test_provider_update_clear_sentinel_clears_ttl(service, provider_registry, registered_provider):
    provider_registry.update_profile(registered_provider.provider_id, min_ttl_seconds=60)
    outcome = service._command_provider_update(
        _provider_command(
            "provider_update",
            {"provider_id": registered_provider.provider_id, "min_ttl_seconds": CLEAR},
        )
    )
    assert outcome.error_code is None
    assert provider_registry.resolve(registered_provider.provider_id).min_ttl_seconds is None


def test_provider_update_unknown_provider(service):
    outcome = service._command_provider_update(
        _provider_command("provider_update", {"provider_id": "AAAAAAAAAAA", "display_name": "x"})
    )
    assert outcome.error_code == "provider_not_found"


def test_provider_set_default_moves_default(service, provider_registry, registered_provider):
    second = provider_registry.register(
        display_name="Second",
        base_url="https://second.example",
        service_public_key=b"\x07" * 32,
        max_ciphertext_bytes=1000,
    )
    outcome = service._command_provider_set_default(
        _provider_command("provider_set_default", {"provider_id": second.provider_id})
    )
    assert outcome.error_code is None
    assert provider_registry.get_default().provider_id == second.provider_id


def test_provider_remove_deletes_unreferenced(service, provider_registry, registered_provider):
    outcome = service._command_provider_remove(
        _provider_command("provider_remove", {"provider_id": registered_provider.provider_id})
    )
    assert outcome.error_code is None
    assert outcome.result == {"action": "deleted"}
    assert provider_registry.resolve(registered_provider.provider_id) is None


def test_provider_remove_unknown(service):
    outcome = service._command_provider_remove(
        _provider_command("provider_remove", {"provider_id": "AAAAAAAAAAA"})
    )
    assert outcome.error_code == "provider_not_found"


def test_provider_set_upload_token(service, provider_registry, registered_provider):
    outcome = service._command_provider_set_upload_token(
        _provider_command(
            "provider_set_upload_token",
            {"provider_id": registered_provider.provider_id, "upload_token": "secret-token"},
        )
    )
    assert outcome.error_code is None
    assert outcome.result == {
        "provider_id": registered_provider.provider_id,
        "upload_token_configured": True,
    }
    # The token is never echoed - only the configured flag.
    assert "secret-token" not in repr(outcome.result)
    assert provider_registry.resolve(registered_provider.provider_id).upload_token_configured is True


def test_provider_set_upload_token_empty(service, registered_provider):
    outcome = service._command_provider_set_upload_token(
        _provider_command(
            "provider_set_upload_token",
            {"provider_id": registered_provider.provider_id, "upload_token": ""},
        )
    )
    assert outcome.error_code == "invalid_metadata"


def test_provider_set_upload_token_too_long(service, registered_provider):
    outcome = service._command_provider_set_upload_token(
        _provider_command(
            "provider_set_upload_token",
            {"provider_id": registered_provider.provider_id, "upload_token": "x" * (MAX_UPLOAD_TOKEN_BYTES + 1)},
        )
    )
    assert outcome.error_code == "upload_token_too_long"


def test_provider_clear_upload_token_is_idempotent(service, registered_provider):
    outcome = service._command_provider_clear_upload_token(
        _provider_command("provider_clear_upload_token", {"provider_id": registered_provider.provider_id})
    )
    assert outcome.error_code is None
    assert outcome.result == {
        "provider_id": registered_provider.provider_id,
        "upload_token_configured": False,
    }


def test_provider_check_forces_fresh_refresh(service, registered_provider, monkeypatch):
    calls = []
    monkeypatch.setattr(
        service._connectivity, "refresh", lambda *, force=False: calls.append(force)
    )
    outcome = service._command_provider_check(
        _provider_command("provider_check", {"provider_id": registered_provider.provider_id})
    )
    assert outcome.error_code is None
    assert outcome.result == {"provider_id": registered_provider.provider_id}
    assert calls == [True]


# ---- Step 1.6A.5: attachment_save / revoke / delete-local-content -----------


def _seed_received_available(conn, principal, wsm, *, attachment_id, file_name="photo.jpg",
                             mime_type="image/jpeg", plain_size=0, content=b"received plaintext"):
    """Seed a received AVAILABLE row whose `saved_path` points at a real
    `cache/incoming/<id>` plaintext file, so the save/delete command handlers
    have a servable source to move/unlink. Returns the cache file path."""
    paths = wsm.paths(principal.principal_id)
    paths.cache_incoming.mkdir(parents=True, exist_ok=True)
    cache_file = paths.cache_incoming / attachment_id
    cache_file.write_bytes(content)
    conn.execute(
        """
        INSERT INTO attachments
            (id, workspace_id, transfer_id, direction, principal_id, state,
             file_name, mime_type, plain_size, saved_path,
             created_at, hard_expires_at, download_grace_seconds)
        VALUES (?, ?, ?, 'received', ?, 'AVAILABLE', ?, ?, ?, ?, 0, 0, 3600)
        """,
        (attachment_id, principal.workspace_id, uuid.uuid4().hex,
         principal.principal_id, file_name, mime_type, plain_size, str(cache_file)),
    )
    conn.commit()
    return cache_file


def _saved_path_of(conn, attachment_id):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT saved_path FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()["saved_path"]


def _save_command(attachment_id):
    return Command(command_id=uuid.uuid4().hex, kind="attachment_save",
                   payload={"attachment_id": attachment_id}, created_at=time.time())


def _revoke_command(attachment_id):
    return Command(command_id=uuid.uuid4().hex, kind="attachment_revoke",
                   payload={"attachment_id": attachment_id}, created_at=time.time())


def _delete_local_content_command(attachment_id):
    return Command(command_id=uuid.uuid4().hex, kind="attachment_delete_local_content",
                   payload={"attachment_id": attachment_id}, created_at=time.time())


# ---- attachment_save -------------------------------------------------------


def test_command_save_moves_cache_content_into_files_and_flips_saved(
    conn, wsm, principal, service
):
    """A received AVAILABLE row's `cache/incoming/<id>` plaintext is moved
    (atomic replace) into `files/` under its display name; the cache copy is
    gone, the row's saved_path repoints into files/, and the content is intact."""
    attachment_id = "a" * 32
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id,
                             file_name="photo.jpg", mime_type="image/jpeg",
                             content=b"\xff\xd8\xff" + b"payload")

    outcome = service._dispatcher.dispatch(_save_command(attachment_id))

    assert outcome.error_code is None
    assert outcome.resource_id == attachment_id
    assert dict(outcome.result) == {"attachment_id": attachment_id, "saved": True, "file_name": "photo.jpg"}

    paths = wsm.paths(principal.principal_id)
    saved = paths.files / "photo.jpg"
    assert saved.exists()
    assert saved.read_bytes() == b"\xff\xd8\xff" + b"payload"
    assert not (paths.cache_incoming / attachment_id).exists()  # moved, not copied
    assert _saved_path_of(conn, attachment_id) == str(saved)
    assert receiver.get_state(conn, attachment_id) == receiver.AVAILABLE  # state unchanged


def test_command_save_is_idempotent_resave_returns_prior_result_without_copying(
    conn, wsm, principal, service
):
    """Re-saving an already-saved row returns the prior result (same file_name)
    without copying, and does not mint a second files/ name."""
    attachment_id = "b" * 32
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")

    first = service._dispatcher.dispatch(_save_command(attachment_id))
    second = service._dispatcher.dispatch(_save_command(attachment_id))

    assert first.error_code is None
    assert second.error_code is None
    assert dict(second.result) == dict(first.result) == {
        "attachment_id": attachment_id, "saved": True, "file_name": "photo.jpg",
    }
    paths = wsm.paths(principal.principal_id)
    assert [p.name for p in paths.files.iterdir()] == ["photo.jpg"]


def test_command_save_idempotent_resave_content_missing(conn, wsm, principal, service):
    """If the files/ copy was deleted after a save, a re-save fails content_missing
    rather than silently claiming saved=true."""
    attachment_id = "c" * 32
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")

    assert service._dispatcher.dispatch(_save_command(attachment_id)).error_code is None
    (wsm.paths(principal.principal_id).files / "photo.jpg").unlink()

    outcome = service._dispatcher.dispatch(_save_command(attachment_id))
    assert outcome.error_code == "content_missing"
    assert outcome.resource_id is None and outcome.result is None


def test_command_save_resolves_name_collision_without_overwriting(conn, wsm, principal, service):
    """Two received rows with the same display name save to `photo.jpg` and
    `photo (2).jpg` respectively - the second never overwrites the first."""
    first_id, second_id = "d" * 32, "e" * 32
    _seed_received_available(conn, principal, wsm, attachment_id=first_id,
                             file_name="photo.jpg", content=b"first")
    _seed_received_available(conn, principal, wsm, attachment_id=second_id,
                             file_name="photo.jpg", content=b"second")

    assert service._dispatcher.dispatch(_save_command(first_id)).error_code is None
    assert service._dispatcher.dispatch(_save_command(second_id)).error_code is None

    paths = wsm.paths(principal.principal_id)
    assert (paths.files / "photo.jpg").read_bytes() == b"first"
    assert (paths.files / "photo (2).jpg").read_bytes() == b"second"


def test_command_save_sanitizes_a_hostile_file_name(conn, wsm, principal, service):
    """A traversal/path file_name never becomes a files/ component: it is
    reduced to its basename and saved under that, not under the hostile path."""
    attachment_id = "f" * 32
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id,
                             file_name="../../etc/passwd", mime_type="text/plain",
                             content=b"text")

    outcome = service._dispatcher.dispatch(_save_command(attachment_id))

    assert outcome.error_code is None
    paths = wsm.paths(principal.principal_id)
    assert (paths.files / "passwd").exists()
    assert _saved_path_of(conn, attachment_id) == str(paths.files / "passwd")
    # No file escaped the files/ directory.
    assert (paths.files.parent / "etc").exists() is False


def test_command_save_rejects_a_non_available_row(conn, wsm, principal, service):
    """Save re-validates the persisted row: a received row not in AVAILABLE (or
    a sent row) fails invalid_state_transition and moves nothing."""
    attachment_id = "11" * 16
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")
    conn.execute("UPDATE attachments SET state = ? WHERE id = ?", (receiver.WAITING_CONSENT, attachment_id))
    conn.commit()

    outcome = service._dispatcher.dispatch(_save_command(attachment_id))

    assert outcome.error_code == "invalid_state_transition"
    assert (wsm.paths(principal.principal_id).cache_incoming / attachment_id).exists()  # untouched


def test_command_save_unknown_id_is_not_found(service):
    outcome = service._dispatcher.dispatch(_save_command("0" * 32))
    assert outcome.error_code == "attachment_not_found"


def test_command_save_content_missing_when_cache_file_gone(conn, wsm, principal, service):
    """A row whose saved_path points at a cache/incoming name that no longer
    exists fails content_missing, leaving the row unchanged."""
    attachment_id = "22" * 16
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")
    (wsm.paths(principal.principal_id).cache_incoming / attachment_id).unlink()

    outcome = service._dispatcher.dispatch(_save_command(attachment_id))

    assert outcome.error_code == "content_missing"
    assert _saved_path_of(conn, attachment_id) is not None  # row unchanged


# ---- attachment_revoke -----------------------------------------------------


def _seed_sent_downloadable(conn, wsm, principal, registered_provider, remote_recipient,
                            tmp_path, *, revoke_token="revoke-1"):
    """A real sent-side DRAFT (via sender.create_draft) advanced to SENT, plus
    a sender-state row carrying a revoke token - the persisted shape
    `_command_revoke` re-reads. Returns the attachment_id."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal,
                                  registered_provider=registered_provider, tmp_path=tmp_path)
    conn.execute("UPDATE attachments SET state = ? WHERE id = ?", (sender.SENT, attachment_id))
    conn.commit()
    _insert_sender_state(conn, attachment_id, upload_id="upload-1", revoke_token=revoke_token)
    return attachment_id


def test_command_revoke_revokes_remote_then_marks_revoked(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor,
    registered_provider, remote_recipient, tmp_path
):
    """Remote-first: `revoke()` is called with the row's own transfer_id
    (hex-decoded) + revoke token, and only after it succeeds does the row
    become REVOKED and its sender-state row is dropped."""
    attachment_id = _seed_sent_downloadable(conn, wsm, principal, registered_provider,
                                            remote_recipient, tmp_path)
    transfer_id_hex = conn.execute("SELECT transfer_id FROM attachments WHERE id = ?", (attachment_id,)).fetchone()[0]

    stub = _StubRelayClient(revoke_result="ok")
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange,
                                 connectivity_monitor, FakeTextAdapter(InMemoryEther(), "local-addr"),
                                 relay_client_factory=lambda _: stub)

    outcome = svc._dispatcher.dispatch(_revoke_command(attachment_id))

    assert outcome.error_code is None
    assert dict(outcome.result) == {"attachment_id": attachment_id, "state": sender.REVOKED}
    assert stub.revoke_calls == [(bytes.fromhex(transfer_id_hex), "revoke-1")]
    assert sender.get_state(conn, attachment_id) == sender.REVOKED
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is None


def test_command_revoke_remote_404_is_confirmed_absence(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor,
    registered_provider, remote_recipient, tmp_path
):
    """A Relay 404 means the object is already gone, so the remote half is
    satisfied and the local half still completes to REVOKED."""
    attachment_id = _seed_sent_downloadable(conn, wsm, principal, registered_provider,
                                            remote_recipient, tmp_path)
    stub = _StubRelayClient(revoke_result=RelayHTTPError(404, "not_found", "gone"))
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange,
                                 connectivity_monitor, FakeTextAdapter(InMemoryEther(), "local-addr"),
                                 relay_client_factory=lambda _: stub)

    outcome = svc._dispatcher.dispatch(_revoke_command(attachment_id))

    assert outcome.error_code is None
    assert sender.get_state(conn, attachment_id) == sender.REVOKED


def test_command_revoke_remote_non_404_is_relay_unreachable_and_preserves_row(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor,
    registered_provider, remote_recipient, tmp_path
):
    """A non-404 remote failure must NOT mark the row REVOKED - it fails
    relay_unreachable and preserves the row and its sender state."""
    attachment_id = _seed_sent_downloadable(conn, wsm, principal, registered_provider,
                                            remote_recipient, tmp_path)
    stub = _StubRelayClient(revoke_result=RelayHTTPError(500, "relay_down", "down"))
    svc = _service_with_delivery(conn, wsm, principal, provider_registry, key_exchange,
                                 connectivity_monitor, FakeTextAdapter(InMemoryEther(), "local-addr"),
                                 relay_client_factory=lambda _: stub)

    outcome = svc._dispatcher.dispatch(_revoke_command(attachment_id))

    assert outcome.error_code == "relay_unreachable"
    assert sender.get_state(conn, attachment_id) == sender.SENT
    assert conn.execute("SELECT 1 FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone() is not None


def test_command_revoke_missing_revoke_token_is_relay_unreachable(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """A sent row whose sender state lacks a revoke token cannot be revoked
    remotely, so revoke fails relay_unreachable and preserves everything."""
    attachment_id = _seed_sent_downloadable(conn, wsm, principal, registered_provider,
                                            remote_recipient, tmp_path, revoke_token=None)

    outcome = service._dispatcher.dispatch(_revoke_command(attachment_id))

    assert outcome.error_code == "relay_unreachable"
    assert sender.get_state(conn, attachment_id) == sender.SENT


def test_command_revoke_rejects_a_non_downloadable_row(
    conn, wsm, principal, registered_provider, remote_recipient, tmp_path, service
):
    """Revoke re-validates the persisted row: a sent row still in DRAFT is not
    downloadable, so it fails invalid_state_transition."""
    _, _, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal)
    attachment_id = _create_draft(conn, wsm, principal, recipient_principal=recipient_principal,
                                  registered_provider=registered_provider, tmp_path=tmp_path)

    outcome = service._dispatcher.dispatch(_revoke_command(attachment_id))

    assert outcome.error_code == "invalid_state_transition"
    assert sender.get_state(conn, attachment_id) == sender.DRAFT


def test_command_revoke_unknown_id_is_not_found(service):
    outcome = service._dispatcher.dispatch(_revoke_command("0" * 32))
    assert outcome.error_code == "attachment_not_found"


# ---- attachment_delete_local_content ---------------------------------------


def test_command_delete_local_content_unlinks_files_copy_and_flips_saved(
    conn, wsm, principal, service
):
    """After a save, deleting local content unlinks the files/ copy and clears
    saved_path (saved=false), keeping the row and its history."""
    attachment_id = "33" * 16
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")
    assert service._dispatcher.dispatch(_save_command(attachment_id)).error_code is None

    outcome = service._dispatcher.dispatch(_delete_local_content_command(attachment_id))

    assert outcome.error_code is None
    assert dict(outcome.result) == {"attachment_id": attachment_id, "saved": False}
    assert _saved_path_of(conn, attachment_id) is None
    assert not (wsm.paths(principal.principal_id).files / "photo.jpg").exists()
    assert receiver.get_state(conn, attachment_id) == receiver.AVAILABLE  # history kept


def test_command_delete_local_content_not_saved(conn, wsm, principal, service):
    """A row whose content is still only in cache/incoming (never saved) fails
    not_saved - there is no files/ copy to delete."""
    attachment_id = "44" * 16
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")

    outcome = service._dispatcher.dispatch(_delete_local_content_command(attachment_id))

    assert outcome.error_code == "not_saved"
    assert (wsm.paths(principal.principal_id).cache_incoming / attachment_id).exists()  # cache untouched


def test_command_delete_local_content_missing_file_is_clean(conn, wsm, principal, service):
    """A saved row whose files/ copy is already gone deletes cleanly (missing_ok):
    saved_path is cleared without error."""
    attachment_id = "55" * 16
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")
    assert service._dispatcher.dispatch(_save_command(attachment_id)).error_code is None
    (wsm.paths(principal.principal_id).files / "photo.jpg").unlink()

    outcome = service._dispatcher.dispatch(_delete_local_content_command(attachment_id))

    assert outcome.error_code is None
    assert dict(outcome.result) == {"attachment_id": attachment_id, "saved": False}
    assert _saved_path_of(conn, attachment_id) is None


def test_command_delete_local_content_unlink_failure_is_content_missing(
    conn, wsm, principal, service
):
    """A files/ path that cannot be unlinked (a directory there) fails
    content_missing, leaving saved_path intact so the row stays consistent."""
    attachment_id = "66" * 16
    _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="photo.jpg")
    assert service._dispatcher.dispatch(_save_command(attachment_id)).error_code is None

    files_dir = wsm.paths(principal.principal_id).files
    (files_dir / "photo.jpg").unlink()
    (files_dir / "photo.jpg").mkdir()  # a directory -> unlink() raises

    outcome = service._dispatcher.dispatch(_delete_local_content_command(attachment_id))

    assert outcome.error_code == "content_missing"
    assert _saved_path_of(conn, attachment_id) is not None  # row unchanged


# --------------------------------------------------------------------------
# ADR-0009 v2: signed inbound ACK routing (Decisions 2/3/4/4a/7/8). These
# drive the *worker-side* path - an ACK enqueued as an InboundEvent and
# drained by service.tick() - never sender.apply_ack() directly, to prove the
# routing/verification sequence (transfer lookup -> cardinality -> source
# route -> pinned key -> signature -> apply) is what actually gates the write.
# --------------------------------------------------------------------------


def _create_ack_draft(conn, wsm, principal, recipient_principal, registered_provider, tmp_path, *, connector_profile_id, route_id="remote-addr"):
    source_path = tmp_path / "ack-outgoing.txt"
    source_path.write_bytes(b"hello ack")
    return sender.create_draft(
        conn, wsm, principal,
        workspace_id="local",
        source_path=str(source_path),
        file_name="ack-outgoing.txt",
        mime_type="text/plain",
        recipients=[sender.RecipientTarget(public_identity=recipient_principal.public_identity, key_id=recipient_principal.key_id)],
        adapter_id=ADAPTER_ID,
        connector_profile_id=connector_profile_id,
        route_type="DIRECT",
        route_id=route_id,
        provider_id=_raw_provider_id(registered_provider),
    )


def _drive_sent(conn, wsm, principal, relay_client, recipient_principal, attachment_id):
    recipient_identities = {recipient_principal.key_id: recipient_principal.public_identity}
    sender_adapter = FakeTextAdapter(InMemoryEther(), "sender-side")
    for _ in range(20):
        if sender.get_state(conn, attachment_id) == sender.SENT:
            break
        sender.run_step(
            conn, workspace_manager=wsm, principal=principal,
            recipient_identities=recipient_identities, relay_client=relay_client,
            delivery_adapter=sender_adapter, attachment_id=attachment_id,
        )
    assert sender.get_state(conn, attachment_id) == sender.SENT
    row = conn.execute("SELECT transfer_id FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    return bytes.fromhex(row[0])


def _ack_event(recipient_wsm, recipient_principal, message_type, transfer_id, source_address="remote-addr"):
    """A signed, MCA1-TEXT-encoded inbound ACK as the recipient would send it,
    wrapped as the InboundEvent the listener would enqueue."""
    signing_key = identity.load_signing_key(recipient_wsm, recipient_principal)
    ack_cbor = codec.encode_simple_ack(message_type, transfer_id, signing_key)
    return InboundEvent(text=codec.to_text(ack_cbor), source_address=source_address, packet_id="p1", received_at=time.time())


def _sent_ack_fixture(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    """Bind the recipient, create + drive a draft to SENT whose delivery row's
    connector_profile_id matches the service's own adapter (so the source-route
    check can pass), and return (attachment_id, transfer_id, recipient_wsm,
    recipient_principal)."""
    _, recipient_wsm, recipient_principal = remote_recipient
    _bind_recipient(conn, recipient_principal, transport_address="remote-addr")
    connector_profile_id = service._delivery_adapter.connector_profile_id
    attachment_id = _create_ack_draft(
        conn, wsm, principal, recipient_principal, registered_provider, tmp_path,
        connector_profile_id=connector_profile_id,
    )
    transfer_id = _drive_sent(conn, wsm, principal, relay_client, recipient_principal, attachment_id)
    return attachment_id, transfer_id, recipient_wsm, recipient_principal


def test_inbound_ack_received_routes_and_applies(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_RECEIVED, transfer_id))
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.RECEIVED


def test_inbound_ack_downloaded_routes_and_applies(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_DOWNLOADED, transfer_id))
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.DOWNLOADED
    # Transient sender state dropped, retained revoke capability preserved.
    assert sender._get_sender_state(conn, attachment_id) is None
    assert conn.execute(
        "SELECT 1 FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
    ).fetchone() is not None


def test_inbound_ack_provider_unknown_marks_non_terminal(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_PROVIDER_UNKNOWN, transfer_id))
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.SENT  # not terminal
    row = conn.execute("SELECT error_code FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert row[0] == sender.PROVIDER_UNKNOWN_ERROR


def test_inbound_ack_wrong_signature_is_dropped(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    # Sign with a *different*, unrelated key - the pinned identity must reject it.
    impostor_key = identity.load_signing_key(wsm, principal)  # the sender's own key, not the recipient's
    ack_cbor = codec.encode_simple_ack(codec.MessageType.ACK_RECEIVED, transfer_id, impostor_key)
    service.enqueue_inbound(InboundEvent(text=codec.to_text(ack_cbor), source_address="remote-addr", packet_id="p1", received_at=time.time()))
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.SENT  # unchanged


def test_inbound_ack_pinned_key_missing_is_dropped(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    # A pre-migration (or somehow unverifiable) transfer with no pinned identity.
    conn.execute("UPDATE attachment_recipients SET recipient_public_identity = NULL WHERE attachment_id = ?", (attachment_id,))
    conn.commit()
    service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_RECEIVED, transfer_id))
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.SENT  # unchanged


def test_inbound_ack_source_route_mismatch_is_dropped(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    # A validly-signed ACK arriving over a *different* source route must not apply.
    service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_RECEIVED, transfer_id, source_address="some-other-route"))
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.SENT  # unchanged


def test_inbound_ack_unknown_transfer_is_dropped(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service, caplog):
    import logging
    _, recipient_wsm, recipient_principal = remote_recipient
    unknown_transfer_id = b"\x11" * 16
    with caplog.at_level(logging.WARNING, logger="meshsrv.attachments.service"):
        service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_RECEIVED, unknown_transfer_id))
        service.tick()
    assert any("unknown_transfer" in record.message for record in caplog.records)


def test_inbound_ack_tombstoned_transfer_is_dropped(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service, caplog):
    import logging
    from meshsrv.attachments.db.tombstones import record_tombstone
    _, recipient_wsm, recipient_principal = remote_recipient
    stale_transfer_id = b"\x22" * 16
    record_tombstone(conn, transfer_id=stale_transfer_id.hex(), workspace_id="local", reason="revoked")
    with caplog.at_level(logging.INFO, logger="meshsrv.attachments.service"):
        service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_RECEIVED, stale_transfer_id))
        service.tick()
    assert any("tombstoned_transfer" in record.message for record in caplog.records)


def test_inbound_ack_sends_no_radio_response(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    """ADR-0009 Decision 8: an ACK is the end of a conversation - the worker
    must never send anything back over the radio, valid or not."""
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    ether = service._delivery_adapter._ether
    service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_RECEIVED, transfer_id))
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.RECEIVED  # it *did* apply
    # ... but nothing was sent back to the ACK's source (or anywhere else).
    assert ether.drain("remote-addr") == []


def test_inbound_ack_is_enqueue_only_until_tick(conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service):
    """The listener->worker boundary holds for ACKs too: enqueuing an ACK must
    not mutate sender state; only the worker's tick applies it (Decision 1)."""
    attachment_id, transfer_id, recipient_wsm, recipient_principal = _sent_ack_fixture(
        conn, wsm, principal, registered_provider, remote_recipient, tmp_path, relay_client, service,
    )
    assert service.enqueue_inbound(_ack_event(recipient_wsm, recipient_principal, codec.MessageType.ACK_RECEIVED, transfer_id)) is True
    # No tick yet -> still SENT (the enqueue path never writes).
    assert sender.get_state(conn, attachment_id) == sender.SENT
    service.tick()
    assert sender.get_state(conn, attachment_id) == sender.RECEIVED
