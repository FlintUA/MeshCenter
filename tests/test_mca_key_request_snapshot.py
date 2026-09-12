"""Tests for meshsrv/attachments/key_request_snapshot.py (PR 4 shared
target model: the worker-published, immutable key-request capability
projection backing `GET /api/mca/key-requests` and the frontend store's
"request key" capability).

Covers, per PR 4's required backend tests:

1. Capability states - `queued` / `waiting_response` / `retry_available`
   from the two worker-only sources (the bounded command queue's
   `contact_request_key` commands, and the persisted
   `last_request_sent_at` timestamp), with the `can_request_key` boolean
   following the binding presence, and `queued` taking precedence over a
   still-visible sent timestamp.
2. Controllable clock - the `waiting_response`/`retry_available` split is
   driven entirely by `now_fn`, so a test clock pins the exact boundary
   (elapsed < interval -> waiting; elapsed >= interval -> retry_available).
3. No-secret projection - the published snapshot carries only the enum
   state string and a boolean; the raw `last_request_sent_at` timestamp
   (and every other raw identity/key material) is consumed inside the
   worker and never reaches the snapshot.
4. Snapshot survival - a coordinator (SQLite) read failure keeps the
   last-known-good snapshot rather than publishing a partial/empty one.

Pure stdlib + an in-memory sqlite schema - no Flask/network/radio, safe
in CI.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest

from meshsrv.attachments.commands import Command, CommandQueue, mint_command_id
from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.identity import create_principal
from meshsrv.attachments.key_exchange import (
    MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS,
    KeyExchangeCoordinator,
)
from meshsrv.attachments.key_request_snapshot import (
    KeyRequestCapability,
    KeyRequestSnapshot,
    KeyRequestState,
    KeyRequestStatePublisher,
)
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


def _make_node(tmp_path, name, clock):
    db_dir = tmp_path / name
    db_dir.mkdir()
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    workspace_manager = MCAWorkspaceManager(db_dir)
    principal = create_principal(conn, workspace_manager, f"ws-{name}", now=clock())
    coordinator = KeyExchangeCoordinator(conn, workspace_manager, principal, "fake-text", now_fn=clock)
    return conn, workspace_manager, principal, coordinator


def _publisher(coordinator, command_queue, clock, *, min_interval=None):
    kwargs = {"now_fn": clock}
    if min_interval is not None:
        kwargs["min_seconds_between_key_requests"] = min_interval
    return KeyRequestStatePublisher(coordinator, command_queue, **kwargs)


def _key_request_command(contact_id, *, command_id=None):
    return Command(
        command_id=command_id or mint_command_id(),
        kind="contact_request_key",
        payload={"contact_id": contact_id},
        created_at=0.0,
    )


def _insert_binding(conn, transport_address, *, tofu_confirmed_at=None):
    """Insert a minimal, valid TOFU recipient binding row directly so the
    "binding exists -> can_request_key False" case can be exercised without
    driving the full KEY_ANNOUNCE flow."""
    conn.execute(
        "INSERT INTO mca_recipient_bindings "
        "(id, workspace_id, adapter_id, transport_address, principal_id, "
        "sender_key_id, public_identity, key_epoch, bound_at, tofu_confirmed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"binding-{transport_address}",
            "ws-a",
            "fake-text",
            transport_address,
            "recipient-principal",
            "sender-key-id",
            "ab" * 32,
            0,
            0.0,
            tofu_confirmed_at,
        ),
    )


def _plain(cap: KeyRequestCapability) -> dict:
    """Project a capability to the exact JSON-safe shape the REST layer
    publishes (and the only two fields the dataclass carries)."""
    return {
        "key_request_state": cap.key_request_state.value,
        "can_request_key": cap.can_request_key,
    }


# --- capability states -------------------------------------------------------


def test_construction_publishes_a_non_none_empty_snapshot(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, CommandQueue(), clock)
    snap = publisher.snapshot()
    assert snap is not None
    assert snap.by_address == {}


def test_idle_address_is_absent_from_the_snapshot(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, CommandQueue(), clock)
    assert "!nobody" not in publisher.snapshot().by_address


def test_queued_command_projects_queued_and_disabled(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    queue = CommandQueue()
    queue.put_nowait(_key_request_command("!contact"))
    publisher = _publisher(coordinator, queue, clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.QUEUED
    assert cap.can_request_key is False
    assert _plain(cap) == {"key_request_state": "queued", "can_request_key": False}


def test_sent_within_window_projects_waiting_response(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    publisher = _publisher(coordinator, CommandQueue(), clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.WAITING_RESPONSE
    assert cap.can_request_key is False


def test_sent_then_window_elapsed_projects_retry_available_without_binding(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    clock.advance(MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS + 1)
    publisher = _publisher(coordinator, CommandQueue(), clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.RETRY_AVAILABLE
    assert cap.can_request_key is True  # no binding -> key still unknown


def test_retry_available_with_existing_binding_is_not_requestable(tmp_path, clock):
    conn, _, _, coordinator = _make_node(tmp_path, "a", clock)
    _insert_binding(conn, "!contact", tofu_confirmed_at=1.0)
    coordinator.record_key_request_sent("!contact", clock())
    clock.advance(MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS + 1)
    publisher = _publisher(coordinator, CommandQueue(), clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.RETRY_AVAILABLE
    # A binding exists, so a fresh key request must not be offered.
    assert cap.can_request_key is False


def test_queued_takes_precedence_over_a_still_visible_sent_timestamp(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    queue = CommandQueue()
    queue.put_nowait(_key_request_command("!contact"))
    publisher = _publisher(coordinator, queue, clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.QUEUED


def test_non_key_request_commands_are_ignored(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    queue = CommandQueue()
    queue.put_nowait(
        Command(
            command_id=mint_command_id(),
            kind="attachment_cancel",
            payload={"attachment_id": "att-1"},
            created_at=0.0,
        )
    )
    publisher = _publisher(coordinator, queue, clock)
    assert publisher.snapshot().by_address == {}


def test_missing_contact_id_in_payload_is_ignored(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    queue = CommandQueue()
    queue.put_nowait(
        Command(command_id=mint_command_id(), kind="contact_request_key", payload={}, created_at=0.0)
    )
    publisher = _publisher(coordinator, queue, clock)
    assert publisher.snapshot().by_address == {}


# --- controllable clock (boundary) ------------------------------------------


def test_rate_limit_window_boundary_is_inclusive_of_interval(tmp_path, clock):
    """`waiting_response` is `elapsed < interval`; at exactly the interval the
    window has elapsed and the state flips to `retry_available`. A test clock
    pins both sides of the boundary."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, CommandQueue(), clock, min_interval=600)

    coordinator.record_key_request_sent("!contact", clock())

    clock.advance(599)
    waiting = publisher.refresh()
    assert waiting.by_address["!contact"].key_request_state is KeyRequestState.WAITING_RESPONSE

    clock.advance(1)  # elapsed is now exactly 600
    retry = publisher.refresh()
    assert retry.by_address["!contact"].key_request_state is KeyRequestState.RETRY_AVAILABLE


def test_clock_is_the_single_source_of_time(tmp_path, clock):
    """Two publishers over the same coordinator read the same test clock, so
    the state flips only when the clock (not real time) crosses the window."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    pub_a = _publisher(coordinator, CommandQueue(), clock)
    assert pub_a.snapshot().by_address["!contact"].key_request_state is KeyRequestState.WAITING_RESPONSE

    clock.advance(MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS)
    pub_b = _publisher(coordinator, CommandQueue(), clock)
    assert pub_b.snapshot().by_address["!contact"].key_request_state is KeyRequestState.RETRY_AVAILABLE


# --- no-secret projection ----------------------------------------------------


def test_capability_carries_only_state_string_and_boolean():
    fields = {f.name for f in dataclasses.fields(KeyRequestCapability)}
    assert fields == {"key_request_state", "can_request_key"}


def test_snapshot_carries_only_the_by_address_mapping():
    fields = {f.name for f in dataclasses.fields(KeyRequestSnapshot)}
    assert fields == {"by_address"}


def test_published_snapshot_never_projects_the_raw_timestamp(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    secret_timestamp = 12345.678
    coordinator.record_key_request_sent("!contact", secret_timestamp)
    publisher = _publisher(coordinator, CommandQueue(), clock)

    # The raw timestamp genuinely exists in the source the worker reads...
    assert coordinator.list_key_request_sent_at()["!contact"] == secret_timestamp

    # ...but the published snapshot consumes it entirely to pick a state string.
    cap = publisher.snapshot().by_address["!contact"]
    assert not hasattr(cap, "last_request_sent_at")
    text = json.dumps(_plain(cap), sort_keys=True)
    assert str(secret_timestamp) not in text
    assert repr(secret_timestamp) not in text


def test_snapshot_is_frozen_and_read_only(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    queue = CommandQueue()
    queue.put_nowait(_key_request_command("!contact"))
    publisher = _publisher(coordinator, queue, clock)
    snap = publisher.snapshot()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.by_address = {}
    with pytest.raises(TypeError):
        snap.by_address["!contact"] = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.by_address["!contact"].can_request_key = True


# --- snapshot survival (last-known-good) ------------------------------------


def test_coordinator_read_failure_keeps_last_known_good(tmp_path, clock, monkeypatch):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    queue = CommandQueue()
    queue.put_nowait(_key_request_command("!contact"))
    publisher = _publisher(coordinator, queue, clock)

    good = publisher.refresh()
    assert good.by_address["!contact"].key_request_state is KeyRequestState.QUEUED

    def _boom():
        raise sqlite3.OperationalError("db gone")

    monkeypatch.setattr(coordinator, "list_key_request_sent_at", _boom)
    survived = publisher.refresh()

    # The prior (complete) snapshot is preserved - never replaced by empty.
    assert survived is good
    assert publisher.snapshot() is good
    assert good.by_address["!contact"].key_request_state is KeyRequestState.QUEUED


def test_queue_read_failure_degrades_queued_but_does_not_raise(tmp_path, clock, monkeypatch):
    """A command-queue read failure must not kill the tick; it degrades the
    'queued' detection (address reverts to absent / idle) but the refresh still
    completes and never raises."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    queue = CommandQueue()
    publisher = _publisher(coordinator, queue, clock)

    def _boom():
        raise RuntimeError("queue read failed")

    monkeypatch.setattr(queue, "iter_commands", _boom)
    snap = publisher.refresh()  # must not raise
    assert "!contact" not in snap.by_address


def test_refresh_returns_the_published_snapshot(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, CommandQueue(), clock)
    coordinator.record_key_request_sent("!contact", clock())
    fresh = publisher.refresh()
    assert fresh is publisher.snapshot()
