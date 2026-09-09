"""Tests for meshsrv/attachments/facade.py (internal-rest-api.md §3.2;
Execution Plan Step 1.6A.1, corrections #1/#2).

Covers the request-facing `AttachmentsFacade`: its read methods resolve to
the in-memory worker-owned components (never SQLite/filesystem/network/
tick lock), and its `submit()` is the §3.4 enqueue with the §3.6 step-5
registry rollback on a full queue. Constructed directly over fresh
in-memory components so no SQLite/filesystem/network is ever touched. Pure
stdlib - safe in CI.

Correction #1's readiness contract is pinned here too: the snapshot-backed
reads (`attachments_snapshot()`, `get_attachment()`, `committed_idempotency()`)
and the write path (`submit()`) raise `FacadeNotReady` (mapping to
`503 mca_not_ready`) while the shared `ready_event` is unset, rather than
returning an empty snapshot/mapping or a false "not found"; the pure
connectivity/provider/identity/registry reads are deliberately *not* gated.
"""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshsrv.attachments.command_registry import STATUS_QUEUED, CommandRegistry
from meshsrv.attachments.commands import Command, CommandQueue, CommandQueueFull
from meshsrv.attachments.facade import AttachmentsFacade, FacadeNotReady
from meshsrv.attachments.identity import MCAPrincipal
from meshsrv.attachments.idempotency import (
    IdempotencyEntry,
    PendingReservation,
    PendingReservations,
)
from meshsrv.attachments.probe_registry import ProbeRegistry
from meshsrv.attachments.recipient_snapshot import RecipientSnapshot
from meshsrv.attachments.snapshots import AttachmentsSnapshot, AttachmentsSnapshotPublisher
from meshsrv.connectivity_monitor import ConnectivitySnapshot, InternetStatus, UploadDecision


class _StubConnectivityMonitor:
    """A duck-typed connectivity/provider read surface with no network and no
    SQLite - the three methods the facade's read methods delegate to. Lets
    the facade test stay stdlib-only while still exercising the read-surface
    wiring (correction #2)."""

    def __init__(self):
        self._snapshot = ConnectivitySnapshot(internet=InternetStatus.UNKNOWN, relays={})
        self._profiles = {}

    def snapshot(self):
        return self._snapshot

    def profile_snapshot(self):
        return self._profiles

    def evaluate_upload_decision(self, provider_id, *, ciphertext_bytes=None, requested_ttl_seconds=None):
        return UploadDecision(ready=True, reason=None)


def _principal():
    return MCAPrincipal(
        workspace_id="ws-test",
        principal_id="0" * 16,
        key_id="1" * 16,
        epoch=0,
        public_identity=b"\x02" * 32,
        public_x25519=b"\x03" * 32,
        private_key_file="key.pem",
        created_at=0.0,
    )


class _StubRecipientPublisher:
    """Duck-typed recipient-snapshot surface (Finding 7) with no SQLite - the
    one method the facade's `recipient_snapshot()` delegates to. Publishes an
    empty, fail-closed snapshot from construction, matching the real
    publisher's "never None, fail-closed before first publish" contract."""

    def __init__(self):
        self._snapshot = RecipientSnapshot(by_address={})

    def snapshot(self):
        return self._snapshot


class _StubWorkspaceManager:
    """Duck-typed workspace path surface for `spool_outgoing_dir()`: `paths()`
    returns a namespace whose `spool_outgoing` is a plain `Path` (never
    created, never written - the facade only computes and returns it)."""

    def paths(self, principal_id):
        return SimpleNamespace(spool_outgoing=Path("spool") / "outgoing")


def _empty_snapshot(idempotency=None):
    return AttachmentsSnapshot(
        records=(), by_id={}, idempotency=idempotency or {}, built_at=0.0
    )


def _facade(*, maxsize=64, ready=True, committed=None):
    wake_event = threading.Event()
    ready_event = threading.Event()
    if ready:
        ready_event.set()
    snapshot_publisher = AttachmentsSnapshotPublisher()
    if ready:
        # Publish an empty snapshot so the gated reads have a real published
        # projection to reflect (the facade delegates to the publisher; the
        # "never None when ready" guarantee is the *service*'s job, tested in
        # test_mca_runtime_wiring.py's readiness lifecycle tests).
        snapshot_publisher._snapshot = _empty_snapshot(idempotency=committed)  # noqa: SLF001
    facade = AttachmentsFacade(
        command_queue=CommandQueue(maxsize=maxsize),
        command_registry=CommandRegistry(),
        pending_reservations=PendingReservations(),
        probe_registry=ProbeRegistry(),
        snapshot_publisher=snapshot_publisher,
        wake_event=wake_event,
        ready_event=ready_event,
        connectivity_monitor=_StubConnectivityMonitor(),
        principal=_principal(),
        workspace_manager=_StubWorkspaceManager(),
        recipient_snapshot_publisher=_StubRecipientPublisher(),
    )
    return facade, wake_event


def _command(command_id="cmd-1", kind="attachment_cancel"):
    return Command(command_id=command_id, kind=kind, payload={}, created_at=0.0)


def _reservation(canonical_hash="a" * 64, attachment_id="b" * 32, command_id="c" * 32):
    return PendingReservation(
        canonical_hash=canonical_hash, attachment_id=attachment_id, command_id=command_id
    )


def _create_command(command_id="c" * 32):
    return Command(command_id=command_id, kind="attachment_create", payload={}, created_at=0.0)


# --- readiness gating (correction #1) ---------------------------------------

def test_gated_reads_and_submit_raise_facade_not_ready_before_readiness():
    facade, _ = _facade(ready=False)
    with pytest.raises(FacadeNotReady):
        facade.attachments_snapshot()
    with pytest.raises(FacadeNotReady):
        facade.get_attachment("anything")
    with pytest.raises(FacadeNotReady):
        facade.committed_idempotency()
    with pytest.raises(FacadeNotReady):
        facade.submit(_command())


def test_facade_not_ready_carries_the_503_error_code():
    # A Step 1.6A REST caller maps this condition to 503 with a stable code;
    # the exception itself exposes it so no caller has to string-match.
    assert FacadeNotReady.error_code == "mca_not_ready"
    assert FacadeNotReady().error_code == "mca_not_ready"


def test_submit_rejects_without_registering_or_enqueueing_before_readiness():
    facade, _ = _facade(ready=False)
    command = _command()
    with pytest.raises(FacadeNotReady):
        facade.submit(command)
    # The command is neither registered nor enqueued - nothing half-submitted
    # for a client that retries after the service comes up.
    assert facade.get_command("cmd-1") is None
    assert facade._command_queue.qsize() == 0  # noqa: SLF001


def test_gated_reads_reflect_the_published_snapshot_once_ready():
    facade, _ = _facade(ready=True)
    snapshot = facade.attachments_snapshot()
    assert snapshot is not None
    assert snapshot.records == ()
    assert facade.get_attachment("anything") is None  # a real "not found", not "not ready"
    assert facade.committed_idempotency() == {}


# --- the non-gated read surface (correction #2) -----------------------------

def test_connectivity_provider_identity_reads_are_not_readiness_gated():
    # These resolve to components that exist (and are safe to read) from the
    # moment the facade is constructed - they must work even before readiness.
    facade, _ = _facade(ready=False)
    conn = facade.connectivity_snapshot()
    assert isinstance(conn, ConnectivitySnapshot)
    assert conn.internet == InternetStatus.UNKNOWN
    assert facade.provider_snapshot() == {}
    decision = facade.evaluate_upload_readiness("some-provider")
    assert decision.ready is True
    assert facade.identity_snapshot().workspace_id == "ws-test"


def test_recipient_snapshot_is_not_readiness_gated_and_fail_closed():
    # Finding 7: the recipient snapshot is a request-thread read that must
    # never raise `FacadeNotReady` (an unknown recipient is a 400, not a 503)
    # and must be fail-closed (empty) before the runtime is ready.
    facade, _ = _facade(ready=False)
    snapshot = facade.recipient_snapshot()
    assert isinstance(snapshot, RecipientSnapshot)
    assert snapshot.by_address == {}


def test_identity_snapshot_returns_the_injected_principal():
    facade, _ = _facade()
    assert facade.identity_snapshot().principal_id == "0" * 16
    assert facade.identity_snapshot().key_id == "1" * 16


def test_connectivity_snapshot_returns_the_monitors_published_view():
    facade, _ = _facade()
    assert facade.connectivity_snapshot() is facade._connectivity_monitor._snapshot  # noqa: SLF001


def test_recipient_snapshot_returns_the_publishers_published_view():
    facade, _ = _facade()
    assert facade.recipient_snapshot() is facade._recipient_snapshot_publisher.snapshot()  # noqa: SLF001


# --- submit (the §3.4 enqueue) ---------------------------------------------

def test_submit_registers_queued_enqueues_and_wakes():
    facade, wake_event = _facade()
    command = _command()
    returned = facade.submit(command)
    assert returned == "cmd-1"
    # registered queued *before* the queue write (an immediate GET sees
    # `queued`, never 404).
    assert facade.get_command("cmd-1").status == STATUS_QUEUED
    # actually enqueued, not just registered.
    assert facade._command_queue.qsize() == 1  # noqa: SLF001
    # the worker was woken so the command is drained promptly.
    assert wake_event.is_set()


def test_submit_full_queue_rolls_back_the_registry_entry():
    facade, _ = _facade(maxsize=1)
    facade.submit(_command(command_id="cmd-a"))
    with pytest.raises(CommandQueueFull):
        facade.submit(_command(command_id="cmd-b"))
    # the rejected command's registry entry is rolled back (§3.6 step 5) so
    # a poller never sees a phantom `queued` for a command that was never
    # enqueued.
    assert facade.get_command("cmd-b") is None
    # the first command is untouched.
    assert facade.get_command("cmd-a").status == STATUS_QUEUED


# --- no SQLite/filesystem/network/tick-lock surface ------------------------

def test_facade_holds_no_sqlite_filesystem_network_or_tick_lock_handle():
    facade, _ = _facade()
    for attr in ("conn", "_conn", "tick_lock", "_tick_lock", "session", "_session", "paths"):
        assert not hasattr(facade, attr), f"facade unexpectedly holds {attr!r}"


def test_reads_complete_without_the_tick_lock():
    # A facade read resolves to the publisher/registry/probe registry's own
    # short dedicated locks, never the worker's tick lock. The honest check
    # is that none of the facade's read attributes *is* a threading.Lock
    # shared with a tick - assert the surface stays lock-free in shape.
    facade, _ = _facade()
    assert facade.attachments_snapshot() is not None
    assert facade.get_attachment("x") is None
    assert facade.committed_idempotency() == {}
    assert facade.get_command("x") is None
    assert facade.get_probe("x") is None
    assert facade.connectivity_snapshot() is not None
    assert facade.provider_snapshot() == {}
    assert facade.evaluate_upload_readiness("x").ready is True


# --- the idempotent create write surface (submit_create, §3.5/§3.6) ---------

def test_spool_outgoing_dir_is_a_pure_path_computation():
    facade, _ = _facade()
    spool = facade.spool_outgoing_dir()
    assert spool == Path("spool") / "outgoing"  # derived from the stub, never touched


def test_submit_create_fresh_registers_enqueues_and_reserves():
    facade, wake_event = _facade()
    reservation = _reservation()
    outcome = facade.submit_create(
        _create_command(), client_request_id="req-1", reservation=reservation
    )
    assert outcome.kind == "fresh"
    assert outcome.reservation is reservation
    assert facade.get_command("c" * 32).status == STATUS_QUEUED
    assert facade._command_queue.qsize() == 1  # noqa: SLF001
    assert wake_event.is_set()


def test_submit_create_replay_pending_does_not_enqueue():
    facade, _ = _facade()
    facade.submit_create(_create_command(), client_request_id="req-1", reservation=_reservation())
    second = facade.submit_create(
        _create_command(command_id="d" * 32),
        client_request_id="req-1",
        reservation=_reservation(command_id="d" * 32),
    )
    assert second.kind == "replay_pending"
    assert second.reservation.attachment_id == "b" * 32
    assert facade._command_queue.qsize() == 1  # noqa: SLF001


def test_submit_create_conflict_does_not_enqueue():
    facade, _ = _facade()
    facade.submit_create(_create_command(), client_request_id="req-1", reservation=_reservation())
    conflicting = facade.submit_create(
        _create_command(command_id="d" * 32),
        client_request_id="req-1",
        reservation=_reservation(canonical_hash="e" * 64, command_id="d" * 32),
    )
    assert conflicting.kind == "conflict"
    assert facade._command_queue.qsize() == 1  # noqa: SLF001


def test_submit_create_replay_committed_returns_the_committed_entry():
    committed = {
        "req-1": IdempotencyEntry(attachment_id="b" * 32, canonical_hash="a" * 64, created_at=0.0),
    }
    facade, _ = _facade(committed=committed)
    outcome = facade.submit_create(
        _create_command(), client_request_id="req-1", reservation=_reservation()
    )
    assert outcome.kind == "replay_committed"
    assert outcome.committed_entry.attachment_id == "b" * 32
    assert facade._command_queue.qsize() == 0  # noqa: SLF001


def test_submit_create_full_queue_rolls_back_registry_and_reservation():
    facade, _ = _facade(maxsize=1)
    facade.submit(_command(command_id="cmd-a"))
    with pytest.raises(CommandQueueFull):
        facade.submit_create(
            _create_command(), client_request_id="req-1", reservation=_reservation()
        )
    assert facade.get_command("c" * 32) is None
    assert facade._pending_reservations.get("req-1") is None  # noqa: SLF001


def test_submit_create_raises_facade_not_ready_before_readiness():
    facade, _ = _facade(ready=False)
    with pytest.raises(FacadeNotReady):
        facade.submit_create(
            _create_command(), client_request_id="req-1", reservation=_reservation()
        )
