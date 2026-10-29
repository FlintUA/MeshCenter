"""KEY_REQUEST auto-retry: the pure policy (key_request_retry), the persisted series (KeyExchangeCoordinator), the
service tick (AttachmentsService._retry_key_requests) and the published state (KeyRequestStatePublisher), all on a
fake clock."""
from __future__ import annotations

import sqlite3

import pytest

from meshsrv.attachments import key_request_retry as policy
from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.delivery.base import DeliveryError
from meshsrv.attachments.identity import create_principal
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.key_request_snapshot import KeyRequestState, KeyRequestStatePublisher
from meshsrv.attachments.service import AttachmentsService
from meshsrv.attachments.workspace import MCAWorkspaceManager

ADDR = "!0000d001"


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeAdapter:
    adapter_id = "fake-text"
    connector_profile_id = "fake"

    def __init__(self):
        self.sent = []
        self.fail = False
        self.unsent = False

    def encode(self, message, route):
        return b"x"

    def send(self, payload, route, idempotency_key=None):
        if self.fail:
            raise DeliveryError("busy")
        self.sent.append(idempotency_key)

        class Receipt:
            pass

        receipt = Receipt()
        receipt.sent = not self.unsent
        return receipt


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def rig(tmp_path, clock):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    migrate(conn)
    manager = MCAWorkspaceManager(tmp_path)
    principal = create_principal(conn, manager, "ws-a", now=clock())
    coordinator = KeyExchangeCoordinator(conn, manager, principal, "fake-text", now_fn=clock)
    adapter = FakeAdapter()
    service = AttachmentsService.__new__(AttachmentsService)
    service._key_exchange = coordinator
    service._delivery_adapter = adapter
    service._contact_exists_fn = None
    service._now = clock
    publisher = KeyRequestStatePublisher(coordinator, now_fn=clock)

    class Rig:
        pass

    r = Rig()
    r.conn, r.coordinator, r.adapter, r.service, r.publisher, r.clock = conn, coordinator, adapter, service, publisher, clock
    r.request = lambda: (coordinator.record_key_request_sent(ADDR, clock()), coordinator.start_key_request_series(ADDR, clock()))
    r.series = lambda: coordinator.list_key_request_series().get(ADDR)
    return r


# ---- policy -----------------------------------------------------------------


def test_schedule_is_10m_30m_2h_6h():
    assert policy.RETRY_DELAYS_S == (600, 1800, 7200, 21600)
    assert policy.first_attempt(0) == (1, 600)
    assert policy.after_retry_sent(1, 0) == (2, 1800)
    assert policy.after_retry_sent(2, 0) == (3, 7200)
    assert policy.after_retry_sent(3, 0) == (4, 21600)


def test_due_action():
    assert policy.due_action(1, 100.0, 99.0) == policy.ACTION_NONE
    assert policy.due_action(1, 100.0, 100.0) == policy.ACTION_RETRY
    assert policy.due_action(4, 100.0, 100.0) == policy.ACTION_GIVE_UP
    assert policy.due_action(0, None, 100.0) == policy.ACTION_NONE
    assert policy.due_action(4, None, 100.0) == policy.ACTION_NONE


def test_seconds_until_next_rounds_up_to_30():
    assert policy.seconds_until_next(100.0, 100.0) == 0
    assert policy.seconds_until_next(131.0, 100.0) == 60
    assert policy.seconds_until_next(None, 100.0) is None


# ---- service ----------------------------------------------------------------


def test_full_series_sends_four_requests_then_no_answer(rig):
    rig.request()
    assert rig.series() == (1, rig.clock.now + 600)
    for expected_attempts, wait in ((2, 600), (3, 1800), (4, 7200)):
        rig.clock.advance(wait)
        rig.service._retry_key_requests()
        assert rig.series()[0] == expected_attempts
    assert len(rig.adapter.sent) == 3
    rig.clock.advance(21600)
    rig.service._retry_key_requests()
    assert rig.series() == (4, None)
    assert len(rig.adapter.sent) == 3
    rig.publisher.refresh()
    assert rig.publisher.snapshot().by_address[ADDR].key_request_state is KeyRequestState.NO_ANSWER
    assert rig.publisher.snapshot().by_address[ADDR].can_request_key is True


def test_nothing_before_due(rig):
    rig.request()
    rig.clock.advance(599)
    rig.service._retry_key_requests()
    assert rig.adapter.sent == []


def test_unsent_retry_postpones_without_consuming_an_attempt(rig):
    rig.request()
    rig.clock.advance(600)
    rig.adapter.fail = True
    rig.service._retry_key_requests()
    assert rig.series() == (1, rig.clock.now + 60)
    rig.adapter.fail = False
    rig.clock.advance(60)
    rig.service._retry_key_requests()
    assert rig.series()[0] == 2


def test_false_receipt_postpones(rig):
    rig.request()
    rig.clock.advance(600)
    rig.adapter.unsent = True
    rig.service._retry_key_requests()
    assert rig.series()[0] == 1


def test_no_delivery_adapter_postpones(rig):
    rig.request()
    rig.clock.advance(600)
    rig.service._delivery_adapter = None
    rig.service._retry_key_requests()
    assert rig.series()[0] == 1


def test_removed_node_ends_the_series(rig):
    rig.request()
    rig.service._contact_exists_fn = lambda address: False
    rig.clock.advance(600)
    rig.service._retry_key_requests()
    assert rig.series() is None
    assert rig.adapter.sent == []


def test_known_node_keeps_the_series(rig):
    rig.request()
    rig.service._contact_exists_fn = lambda address: True
    rig.clock.advance(600)
    rig.service._retry_key_requests()
    assert rig.series()[0] == 2


def test_announce_ends_the_series(rig):
    rig.request()
    rig.conn.execute(
        "INSERT INTO mca_recipient_bindings (id, workspace_id, adapter_id, transport_address, principal_id, sender_key_id, "
        "public_identity, key_epoch, bound_at, tofu_confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("b1", rig.coordinator._principal.workspace_id, "fake-text", ADDR, "p", "k", "ab" * 32, 0, 0.0, None),
    )
    rig.clock.advance(600)
    rig.service._retry_key_requests()
    assert rig.series() is None
    assert rig.adapter.sent == []


def test_cancel_ends_the_series_and_keeps_the_gate(rig):
    rig.request()
    rig.coordinator.end_key_request_series(ADDR)
    assert rig.series() is None
    from meshsrv.attachments.key_exchange import RateLimited

    with pytest.raises(RateLimited):
        rig.coordinator.check_key_request_rate_limit(ADDR, rig.clock.now + 5)


def test_exhausted_hourly_quota_postpones(rig):
    rig.request()
    rig.clock.advance(600)
    for _ in range(12):
        rig.coordinator.record_auto_key_request_sent(rig.clock.now)
    rig.service._retry_key_requests()
    assert rig.series()[0] == 1
    assert rig.adapter.sent == []


def test_series_survives_a_restart(rig, tmp_path):
    rig.request()
    rig.clock.advance(1000)
    again = KeyExchangeCoordinator(
        rig.conn, rig.coordinator._workspace_manager, rig.coordinator._principal, "fake-text", now_fn=rig.clock
    )
    assert again.list_key_request_series()[ADDR][0] == 1


# ---- published state --------------------------------------------------------


def test_running_series_is_waiting_response_with_counters_and_no_timestamp(rig):
    rig.request()
    rig.publisher.refresh()
    cap = rig.publisher.snapshot().by_address[ADDR]
    assert cap.key_request_state is KeyRequestState.WAITING_RESPONSE
    assert (cap.retry_attempt, cap.retry_max) == (1, 4)
    assert cap.can_request_key is False
    assert cap.retry_in_s(rig.clock.now) == 600
    assert str(rig.clock.now + 600) not in repr(cap)
