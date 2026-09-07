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

from nacl.signing import VerifyKey

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
