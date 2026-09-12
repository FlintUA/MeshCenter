"""Tests for meshsrv/attachments/key_request_snapshot.py (PR 4 shared
target model: the worker-published, immutable key-request capability
projection backing `GET /api/mca/key-requests` and the frontend store's
"request key" capability).

Covers, per PR 4's required backend tests (and the PR #256 review's
Finding 2 / Finding 5 corrections):

1. Capability states - `queued` / `waiting_response` / `retry_available`
   from the two worker-only sources (the pending queued-marker map keyed by
   command id driven by `mark_queued()`/`mark_drained()`, and the persisted
   `last_request_sent_at` timestamp), with the `can_request_key` boolean
   following the binding presence, and `queued` taking precedence over a
   still-visible sent timestamp.
2. `queued` observability - a `mark_queued()` address is observable through
   `snapshot()` immediately (the live overlay), before any `refresh()` or
   worker tick, and `mark_drained()` removes it (back to the timestamp-
   derived state). This is what lets the real
   `facade.submit() -> queued -> worker tick -> waiting_response` sequence
   project `queued` at all (Finding 2).
3. Controllable clock - the `waiting_response`/`retry_available` split is
   driven entirely by `now_fn`, so a test clock pins the exact boundary
   (elapsed < interval -> waiting; elapsed >= interval -> retry_available).
4. No-secret projection - the published snapshot carries only the enum
   state string and a boolean; the raw `last_request_sent_at` timestamp
   (and every other raw identity/key material) is consumed inside the
   worker and never reaches the snapshot.
5. Snapshot survival - a coordinator (SQLite) read failure keeps the
   last-known-good snapshot *plus* the live `queued` overlay, so a queued
   address never becomes wrongly re-permitted to `idle` (Finding 5).

Pure stdlib + an in-memory sqlite schema - no Flask/network/radio, safe
in CI.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading

import pytest

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
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    migrate(conn)
    workspace_manager = MCAWorkspaceManager(db_dir)
    principal = create_principal(conn, workspace_manager, f"ws-{name}", now=clock())
    coordinator = KeyExchangeCoordinator(conn, workspace_manager, principal, "fake-text", now_fn=clock)
    return conn, workspace_manager, principal, coordinator


def _publisher(coordinator, clock, *, min_interval=None):
    kwargs = {"now_fn": clock}
    if min_interval is not None:
        kwargs["min_seconds_between_key_requests"] = min_interval
    return KeyRequestStatePublisher(coordinator, **kwargs)


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
    publisher = _publisher(coordinator, clock)
    snap = publisher.snapshot()
    assert snap is not None
    assert snap.by_address == {}


def test_idle_address_is_absent_from_the_snapshot(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)
    assert "!nobody" not in publisher.snapshot().by_address


def test_marked_queued_address_projects_queued_and_disabled(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)
    publisher.mark_queued("cmd-1", "!contact")

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.QUEUED
    assert cap.can_request_key is False
    assert _plain(cap) == {"key_request_state": "queued", "can_request_key": False}


def test_queued_is_observable_immediately_without_any_refresh(tmp_path, clock):
    """Finding 2: `queued` must be visible through `snapshot()` as soon as
    `mark_queued()` runs (what `facade.submit()` does synchronously after a
    successful enqueue) - no `refresh()` / worker tick in between."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)

    publisher.mark_queued("cmd-1", "!contact")

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.QUEUED
    assert cap.can_request_key is False


def test_mark_drained_removes_queued_back_to_absent(tmp_path, clock):
    """Finding 2: when the worker dequeues the command it calls
    `mark_drained()`, so the address is no longer `queued` - absent here
    (no persisted timestamp), i.e. back to the `idle` default."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)
    publisher.mark_queued("cmd-1", "!contact")
    assert "!contact" in publisher.snapshot().by_address

    publisher.mark_drained("cmd-1")

    assert "!contact" not in publisher.snapshot().by_address


def test_mark_drained_reveals_the_persisted_timestamp_state(tmp_path, clock):
    """Finding 2: after a successful send the worker persists the timestamp,
    so draining the queued marker surfaces `waiting_response` (not `idle`)."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    publisher = _publisher(coordinator, clock)
    publisher.mark_queued("cmd-1", "!contact")
    assert publisher.snapshot().by_address["!contact"].key_request_state is KeyRequestState.QUEUED

    publisher.mark_drained("cmd-1")
    publisher.refresh()

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.WAITING_RESPONSE


def test_sent_within_window_projects_waiting_response(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    publisher = _publisher(coordinator, clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.WAITING_RESPONSE
    assert cap.can_request_key is False


def test_sent_then_window_elapsed_projects_retry_available_without_binding(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    clock.advance(MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS + 1)
    publisher = _publisher(coordinator, clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.RETRY_AVAILABLE
    assert cap.can_request_key is True  # no binding -> key still unknown


def test_retry_available_with_existing_binding_is_not_requestable(tmp_path, clock):
    conn, _, _, coordinator = _make_node(tmp_path, "a", clock)
    _insert_binding(conn, "!contact", tofu_confirmed_at=1.0)
    coordinator.record_key_request_sent("!contact", clock())
    clock.advance(MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS + 1)
    publisher = _publisher(coordinator, clock)

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.RETRY_AVAILABLE
    # A binding exists, so a fresh key request must not be offered.
    assert cap.can_request_key is False


def test_queued_takes_precedence_over_a_still_visible_sent_timestamp(tmp_path, clock):
    """A fresh queued request supersedes the prior request's still-visible
    rate-limit timestamp: the live `queued` overlay wins."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.record_key_request_sent("!contact", clock())
    publisher = _publisher(coordinator, clock)

    publisher.mark_queued("cmd-1", "!contact")

    cap = publisher.snapshot().by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.QUEUED


# --- controllable clock (boundary) ------------------------------------------

def test_rate_limit_window_boundary_is_inclusive_of_interval(tmp_path, clock):
    """`waiting_response` is `elapsed < interval`; at exactly the interval the
    window has elapsed and the state flips to `retry_available`. A test clock
    pins both sides of the boundary."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock, min_interval=600)

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
    pub_a = _publisher(coordinator, clock)
    assert pub_a.snapshot().by_address["!contact"].key_request_state is KeyRequestState.WAITING_RESPONSE

    clock.advance(MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS)
    pub_b = _publisher(coordinator, clock)
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
    publisher = _publisher(coordinator, clock)

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
    publisher = _publisher(coordinator, clock)
    publisher.mark_queued("cmd-1", "!contact")
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
    publisher = _publisher(coordinator, clock)
    publisher.mark_queued("cmd-1", "!contact")

    good = publisher.refresh()
    assert good.by_address["!contact"].key_request_state is KeyRequestState.QUEUED

    def _boom():
        raise sqlite3.OperationalError("db gone")

    monkeypatch.setattr(coordinator, "list_key_request_sent_at", _boom)
    survived = publisher.refresh()

    # The prior (complete) snapshot is preserved - never replaced by empty.
    assert survived.by_address["!contact"].key_request_state is KeyRequestState.QUEUED
    assert publisher.snapshot().by_address["!contact"].key_request_state is KeyRequestState.QUEUED


def test_queued_survives_a_coordinator_read_failure_after_being_published(tmp_path, clock, monkeypatch):
    """Finding 5: a worker-side read failure *after* an address has already
    been projected `queued` must not suddenly re-permit that address. The
    last-known-good snapshot plus the live `queued` overlay keeps it `queued`
    (never wrongly `idle`/requestable), and the refresh never raises."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)
    publisher.mark_queued("cmd-1", "!contact")

    # The contact is observably queued (this is what the request thread saw).
    assert publisher.snapshot().by_address["!contact"].can_request_key is False

    def _boom():
        raise sqlite3.OperationalError("db gone")

    monkeypatch.setattr(coordinator, "list_key_request_sent_at", _boom)
    survived = publisher.refresh()  # must not raise

    cap = survived.by_address["!contact"]
    assert cap.key_request_state is KeyRequestState.QUEUED
    assert cap.can_request_key is False
    # And the request-thread read agrees - still not requestable.
    assert publisher.snapshot().by_address["!contact"].can_request_key is False


def test_refresh_returns_the_published_snapshot(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)
    coordinator.record_key_request_sent("!contact", clock())
    fresh = publisher.refresh()
    assert fresh is publisher.snapshot()


# --- Blocker 2: command-id-keyed pending-marker race semantics ----------------

def test_two_same_address_commands_stay_queued_until_both_drain(tmp_path, clock):
    """Blocker 2: two distinct `contact_request_key` commands for the SAME
    address are two pending entries keyed by command id. Draining one must not
    clear the other's still-in-flight `queued` - the address stays `queued`
    (non-requestable) until the LAST of its commands drains. The old
    address-keyed `Set` collapsed both commands into one entry, so draining the
    first cleared the address even while the second was still queued."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)

    publisher.mark_queued("cmd-1", "!contact")
    publisher.mark_queued("cmd-2", "!contact")
    assert publisher.snapshot().by_address["!contact"].key_request_state is KeyRequestState.QUEUED

    publisher.mark_drained("cmd-1")
    assert publisher.snapshot().by_address["!contact"].key_request_state is KeyRequestState.QUEUED

    publisher.mark_drained("cmd-2")
    assert "!contact" not in publisher.snapshot().by_address


def test_drain_of_an_unrelated_command_id_leaves_the_marker_intact(tmp_path, clock):
    """Blocker 2 (the enqueue/drain race): because the marker is keyed by
    command id (not address), the worker draining some *other* command can
    never clear this command's marker before it exists - `mark_drained` only
    ever removes the id it is handed. This is what makes the facade's
    mark-before-enqueue order safe: even if the worker drains between the
    marker registration and the queue write, it is draining a *different*
    command id, so this command's marker survives until its own drain."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)

    publisher.mark_queued("cmd-1", "!contact")
    publisher.mark_drained("cmd-unrelated")  # worker drained a different command
    assert publisher.snapshot().by_address["!contact"].key_request_state is KeyRequestState.QUEUED

    publisher.mark_drained("cmd-1")
    assert "!contact" not in publisher.snapshot().by_address


def test_mark_drained_is_idempotent_for_a_missing_command_id(tmp_path, clock):
    """Blocker 2: a `mark_drained(command_id)` with no matching marker is a
    no-op (the worker's `finally` can race a facade rollback or a double
    drain); it must never raise and must never disturb other markers."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)
    publisher.mark_drained("never-marked")
    assert publisher.snapshot().by_address == {}


def test_concurrent_snapshot_and_drain_do_not_raise(tmp_path, clock):
    """Blocker 2: `snapshot()` must copy the pending addresses under the lock,
    so a reader thread snapshotting while the worker thread calls
    `mark_drained()` never iterates a dict that is being mutated (the old code
    iterated the live set outside the lock and could raise "dictionary changed
    size during iteration"). Hammering both from two threads must not raise."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    publisher = _publisher(coordinator, clock)

    # Seed enough distinct entries that the writer's add/remove cycle has real
    # work to do against the reader's snapshot().
    for i in range(200):
        publisher.mark_queued(f"cmd-{i}", f"!{i:08x}")

    errors = []
    go = threading.Event()
    n_iterations = 2000

    def reader():
        try:
            go.wait()
            for _ in range(n_iterations):
                publisher.snapshot()
        except BaseException as exc:  # noqa: BLE001 - asserted empty below
            errors.append(exc)

    def writer():
        try:
            go.wait()
            for i in range(n_iterations):
                publisher.mark_queued(f"wcmd-{i}", f"!{i % 100:08x}")
                publisher.mark_drained(f"wcmd-{i}")
        except BaseException as exc:  # noqa: BLE001 - asserted empty below
            errors.append(exc)

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "race threads did not finish"
    assert errors == []


def test_removed_marker_cannot_be_resurrected_by_concurrent_refresh(tmp_path, clock, monkeypatch):
    """Blocker 2 (final correction): a `CommandQueueFull` rollback that removes
    a marker *while* a worker `refresh()` is mid-flight must not be published
    as a stale `queued`. `_published` is the persisted base only; `refresh()`
    never folds the pending marker map into it. So even when the rollback lands
    after `refresh()` has read the persisted data but before it publishes, the
    address projects its persisted state (here `retry_available`) - never a
    resurrected `queued` that falsely disables `can_request_key`.

    This test fails against a82b10a: that build copied the pending set into the
    snapshot before reading SQLite, so the mid-flight rollback was lost and the
    stale `queued` was published."""
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    # Persist a completed request whose rate-limit window has elapsed, so the
    # persisted base projects `retry_available` / `can_request_key=True`.
    coordinator.record_key_request_sent("!contact", clock())
    clock.advance(MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS + 1)
    publisher = _publisher(coordinator, clock)  # construction publishes the base

    publisher.mark_queued("cmd-C", "!contact")  # provisional marker (submit())

    reads_done = threading.Event()
    continue_refresh = threading.Event()
    real_list_bindings = coordinator.list_bindings

    def blocking_list_bindings():
        result = real_list_bindings()
        # Both persisted reads are complete; publication is the next step.
        reads_done.set()
        assert continue_refresh.wait(timeout=5), "test must release the blocked refresh"
        return result

    monkeypatch.setattr(coordinator, "list_bindings", blocking_list_bindings)

    result = {}

    def run_refresh():
        result["snap"] = publisher.refresh()

    thread = threading.Thread(target=run_refresh)
    thread.start()
    assert reads_done.wait(timeout=5), "refresh never reached the persisted-data read"

    # CommandQueueFull rollback removes the provisional marker mid-refresh.
    publisher.mark_drained("cmd-C")
    continue_refresh.set()
    thread.join(timeout=5)
    assert not thread.is_alive(), "refresh did not finish"

    snap = publisher.snapshot()
    assert snap.by_address["!contact"].key_request_state is KeyRequestState.RETRY_AVAILABLE
    assert snap.by_address["!contact"].can_request_key is True
    # The refresh() return value (the public view) agrees: not resurrected as queued.
    assert result["snap"].by_address["!contact"].key_request_state is KeyRequestState.RETRY_AVAILABLE
    assert result["snap"].by_address["!contact"].can_request_key is True
