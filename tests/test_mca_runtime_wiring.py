"""Tests for Step 1.6A.1's runtime wiring (internal-rest-api.md §3.1/§3.2;
Execution Plan Step 1.6A.1, correction #1/#2).

Pins the facade plumbing into `meshsrv.attachments.mca_runtime`:

- `_MCARuntimeState` owns the six facade components (CommandQueue,
  CommandRegistry, PendingReservations, ProbeRegistry,
  AttachmentsSnapshotPublisher, AttachmentsFacade) and the worker-facing
  `AttachmentsService` is handed the *same* single instances;
- `get_attachments_facade()` returns `None` before startup (never lazy-
  creating the SQLite runtime) and the facade afterwards;
- the worker drains the bounded command queue (a command becomes a terminal
  `unsupported_command_kind` FAILED with the empty dispatcher) and
  republishes the snapshot - in the documented §3.2 tick order (drain
  before row-scan, snapshot after state transitions);
- request-thread reads through the facade never touch SQLite/filesystem/
  network, and never block on the worker's tick lock.

Uses the same FakeRadioTransport/InMemoryEther + `_AlwaysDownSession` shape
as tests/test_mca_runtime.py. Each test stops the worker thread immediately
after startup so the explicit `service.tick()` calls are deterministic
rather than racing the daemon thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from meshsrv.attachments import mca_runtime, receiver, sender, service as service_module
from meshsrv.attachments.command_registry import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    CommandRegistry,
)
from meshsrv.attachments.commands import COMMAND_KINDS, Command, CommandQueue
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.dispatch import COMMAND_EXECUTION_FAILED, CommandDispatcher, CommandOutcome
from meshsrv.attachments.facade import AttachmentsFacade, FacadeNotReady
from meshsrv.attachments.idempotency import PendingReservation, PendingReservations
from meshsrv.attachments.key_exchange import AddressStatus
from meshsrv.attachments.probe_registry import ProbeRegistry
from meshsrv.attachments.provider_registry import compute_provider_id, encode_provider_id
from meshsrv.attachments.service import AttachmentsService
from meshsrv.attachments.snapshots import AttachmentsSnapshot, AttachmentsSnapshotPublisher
from meshsrv.connectivity_monitor import ConnectivitySnapshot, UploadRejectionReason


class _AlwaysDownSession:
    """Feeds ConnectivityMonitor a permanently-unreachable fallback probe so
    no test in this file ever makes a real network call (same role as the
    identically-named helper in tests/test_mca_runtime.py)."""

    def request(self, method, url, json=None, data=None, headers=None, timeout=None):
        import requests

        raise requests.ConnectionError("down for this test")


def _started_state(tmp_path, tag):
    """Start the runtime and stop its worker thread for deterministic
    single-threaded `tick()` driving. Returns the `_MCARuntimeState`.

    After `start_attachments_service()`, the worker's first tick has already
    published the (empty) snapshot - but `stop()` clears runtime readiness
    (correction #1), so the facade's snapshot-backed reads/`submit()` would
    raise `FacadeNotReady` until the shared event is re-set. These tests
    drive `tick()` synchronously rather than racing the daemon thread, so we
    re-set readiness here; the readiness *lifecycle* itself (startup in
    progress, first-snapshot failure, successful publication, stop, retry) is
    covered by the dedicated readiness tests below, not this helper."""
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa")
    data_dir = str(tmp_path / tag)
    mca_runtime.start_attachments_service(data_dir, transport)
    state = mca_runtime._get_state(data_dir)  # noqa: SLF001
    assert state.service.stop(), "worker did not stop - cannot drive ticks deterministically"
    state.ready_event.set()
    return state, transport, ether


# --- ownership of the six facade components --------------------------------


def test_state_owns_the_six_facade_components(tmp_path):
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    try:
        state = mca_runtime._get_state(str(tmp_path / "a"))  # noqa: SLF001
        assert isinstance(state.command_queue, CommandQueue)
        assert isinstance(state.command_registry, CommandRegistry)
        assert isinstance(state.pending_reservations, PendingReservations)
        assert isinstance(state.probe_registry, ProbeRegistry)
        assert isinstance(state.snapshot_publisher, AttachmentsSnapshotPublisher)
        assert isinstance(state.facade, AttachmentsFacade)
        assert isinstance(state.dispatcher, CommandDispatcher)
    finally:
        mca_runtime.reset_state_for_tests()


def test_facade_and_service_share_the_same_single_instances(tmp_path):
    state, _, _ = _started_state(tmp_path, "b")
    try:
        # The facade wraps exactly the state's own instances - not copies.
        assert state.facade._command_queue is state.command_queue  # noqa: SLF001
        assert state.facade._command_registry is state.command_registry  # noqa: SLF001
        assert state.facade._pending_reservations is state.pending_reservations  # noqa: SLF001
        assert state.facade._probe_registry is state.probe_registry  # noqa: SLF001
        assert state.facade._snapshot_publisher is state.snapshot_publisher  # noqa: SLF001
        assert state.facade._wake_event is state.wake_event  # noqa: SLF001

        # And the worker-facing service was handed the same single instances,
        # so a facade.submit() from a request thread and the worker's drain
        # touch one queue/registry/publisher/Event.
        svc = state.service
        assert svc._command_queue is state.command_queue  # noqa: SLF001
        assert svc._command_registry is state.command_registry  # noqa: SLF001
        assert svc._dispatcher is state.dispatcher  # noqa: SLF001
        assert svc._snapshot_publisher is state.snapshot_publisher  # noqa: SLF001
        assert svc._wake_event is state.wake_event  # noqa: SLF001
    finally:
        mca_runtime.reset_state_for_tests()


def test_service_is_a_real_attachments_service(tmp_path):
    state, _, _ = _started_state(tmp_path, "c")
    try:
        assert isinstance(state.service, AttachmentsService)
    finally:
        mca_runtime.reset_state_for_tests()


# --- Finding 7: recipient-binding snapshot (worker-published, request-read) ---


def _seed_trusted_binding(state, source_address="!aaaaaaaa", *, confirmed=True):
    """Insert a TOFU recipient binding row directly into the worker-owned
    connection, mirroring the columns `KeyExchangeCoordinator._handle_key_
    announce` writes (with `tofu_confirmed_at` set only when `confirmed`, so a
    test can seed either an MCA_READY or a KEY_UNVERIFIED binding)."""
    state.conn.execute(
        """
        INSERT INTO mca_recipient_bindings
            (id, workspace_id, adapter_id, transport_address, principal_id,
             sender_key_id, public_identity, key_epoch, bound_at, tofu_confirmed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"meshtastic:{source_address}",
            "local",
            "meshtastic",
            source_address,
            "1" * 16,
            "1" * 16,
            "02" * 32,
            0,
            0.0,
            1.0 if confirmed else None,
        ),
    )
    state.conn.commit()


def test_facade_and_service_share_the_same_recipient_snapshot_publisher(tmp_path):
    state, _, _ = _started_state(tmp_path, "recipient-owner")
    try:
        # Finding 7: one publisher, shared between the request-facing facade
        # (which reads it) and the worker-facing service (which refreshes it).
        assert state.facade._recipient_snapshot_publisher is state.recipient_snapshot_publisher  # noqa: SLF001
        assert state.service._recipient_publisher is state.recipient_snapshot_publisher  # noqa: SLF001
    finally:
        mca_runtime.reset_state_for_tests()


def test_tick_refreshes_the_recipient_snapshot_from_the_binding_table(tmp_path):
    state, _, _ = _started_state(tmp_path, "recipient-refresh")
    try:
        # Before any binding exists the published snapshot is empty (fail-closed).
        assert state.facade.recipient_snapshot().by_address == {}

        _seed_trusted_binding(state)
        state.service.tick()

        binding = state.facade.recipient_snapshot().by_address["!aaaaaaaa"]
        assert binding.status is AddressStatus.MCA_READY
        assert binding.key_id == "1" * 16
        # No-secret discipline: the projection carries the public identifiers
        # only - never the recipient's `public_identity` bytes.
        assert not hasattr(binding, "public_identity")
    finally:
        mca_runtime.reset_state_for_tests()


def test_unconfirmed_binding_projects_as_key_unverified(tmp_path):
    state, _, _ = _started_state(tmp_path, "recipient-unverified")
    try:
        _seed_trusted_binding(state, confirmed=False)
        state.service.tick()

        binding = state.facade.recipient_snapshot().by_address["!aaaaaaaa"]
        assert binding.status is AddressStatus.KEY_UNVERIFIED
    finally:
        mca_runtime.reset_state_for_tests()


# --- get_attachments_facade: never lazy-create the SQLite runtime ----------


def test_get_attachments_facade_returns_none_before_startup_and_creates_nothing(tmp_path):
    # correction #1's hard guarantee: a request-facing accessor must return
    # an explicit not-ready result before startup completes, never fall back
    # to initializing the SQLite runtime (opening attachments.db) from a
    # request thread. No data_dir is ever handed here, so a lazy-create
    # would crash - and `_state` stays None throughout.
    mca_runtime.reset_state_for_tests()
    try:
        assert mca_runtime.get_attachments_facade() is None
        assert mca_runtime._state is None  # noqa: SLF001 - still never initialized
    finally:
        mca_runtime.reset_state_for_tests()


def test_get_attachments_facade_returns_the_facade_after_startup(tmp_path):
    state, _, _ = _started_state(tmp_path, "d")
    try:
        facade = mca_runtime.get_attachments_facade()
        assert facade is state.facade
        assert isinstance(facade, AttachmentsFacade)
    finally:
        mca_runtime.reset_state_for_tests()


# --- command drain (worker executes commands) ------------------------------


def test_command_submitted_through_the_facade_is_drained_to_a_terminal_result(tmp_path):
    state, _, _ = _started_state(tmp_path, "e")
    try:
        facade = state.facade
        command = Command(command_id="cmd-1", kind="attachment_cancel", payload={}, created_at=0.0)
        facade.submit(command)
        # Before any tick: registered queued (an immediate poll sees queued).
        assert facade.get_command("cmd-1").status == STATUS_QUEUED

        state.service.tick()

        # The empty dispatcher means the enumerated kind has no handler, so
        # the worker drains it to a terminal unsupported_command_kind FAILED
        # - never a crash, never left stuck in running.
        result = facade.get_command("cmd-1")
        assert result.status == STATUS_FAILED
        assert result.error_code == "unsupported_command_kind"
    finally:
        mca_runtime.reset_state_for_tests()


def test_a_raising_handler_is_recorded_as_command_execution_failed(tmp_path):
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    try:
        ether = InMemoryEther()
        transport = FakeRadioTransport(ether, "!aaaaaaaa")
        data_dir = str(tmp_path / "f")
        state = mca_runtime._get_state(data_dir)  # noqa: SLF001 - builds state, empty dispatcher

        def handler(command):
            raise RuntimeError("handler bug")

        # Wire the handler into the state's dispatcher *before* ensure_service
        # builds the worker, so the worker drains through the new table.
        state.dispatcher = CommandDispatcher({"attachment_cancel": handler})
        state.ensure_service(transport)
        assert state.service.stop()
        state.ready_event.set()  # deterministic tick driving (see _started_state)

        facade = state.facade
        facade.submit(Command(command_id="cmd-2", kind="attachment_cancel", payload={}, created_at=0.0))
        state.service.tick()

        result = facade.get_command("cmd-2")
        assert result.status == STATUS_FAILED
        assert result.error_code == "command_execution_failed"
    finally:
        mca_runtime.reset_state_for_tests()


# --- snapshot publication --------------------------------------------------


def test_tick_publishes_a_snapshot_the_facade_can_read(tmp_path):
    state, _, _ = _started_state(tmp_path, "g")
    try:
        facade = state.facade
        # The worker's immediate first tick already published the (empty)
        # snapshot; an explicit tick keeps it current with no dirty ids.
        state.service.tick()
        snapshot = facade.attachments_snapshot()
        assert snapshot is not None
        assert snapshot.records == ()
        assert facade.committed_idempotency() == {}
        assert facade.get_attachment("nonexistent") is None
    finally:
        mca_runtime.reset_state_for_tests()


# --- documented §3.2 tick order --------------------------------------------


def test_tick_drains_commands_before_row_scan_and_refreshes_snapshot_after(tmp_path, monkeypatch):
    state, _, _ = _started_state(tmp_path, "h")
    try:
        order = []

        def _inbound():
            order.append("inbound")

        def _drain_commands():
            order.append("drain_commands")

        def _connectivity():
            order.append("connectivity")

        def _due_rows():
            order.append("row_scan")
            return []

        def _replies():
            order.append("replies")

        def _snapshot():
            order.append("snapshot")

        monkeypatch.setattr(state.service, "_drain_inbound_events", _inbound)
        monkeypatch.setattr(state.service, "_drain_commands", _drain_commands)
        monkeypatch.setattr(state.service._connectivity, "refresh", _connectivity)
        monkeypatch.setattr(state.service, "_due_rows", _due_rows)
        monkeypatch.setattr(state.service, "_dispatch_outgoing_replies", _replies)
        monkeypatch.setattr(state.service, "_refresh_snapshot", _snapshot)

        state.service.tick()

        # The §3.2 documented order: inbound drain -> command drain (before
        # the row-scan) -> connectivity -> row-scan -> replies -> snapshot
        # refresh (after the state transitions).
        assert order == ["inbound", "drain_commands", "connectivity", "row_scan", "replies", "snapshot"]
    finally:
        mca_runtime.reset_state_for_tests()


# --- request-thread reads never touch SQLite/FS/network/tick lock ----------


def test_facade_reads_never_touch_the_tick_lock(tmp_path):
    state, _, _ = _started_state(tmp_path, "i")
    try:
        facade = state.facade

        # The facade holds no SQLite connection, no filesystem path, and no
        # network session - request-thread reads resolve to in-memory
        # components only.
        for attr in ("conn", "_conn", "tick_lock", "_tick_lock", "session", "paths"):
            assert not hasattr(facade, attr), f"facade unexpectedly holds {attr!r}"

        # Hold the worker's tick lock from this thread; a request-thread read
        # that needed the tick lock would block (and this join would time
        # out). It must not.
        state.tick_lock.acquire()
        results = {}
        try:
            def read_from_request_thread():
                results["snapshot"] = facade.attachments_snapshot()
                results["attachment"] = facade.get_attachment("nonexistent")
                results["idempotency"] = facade.committed_idempotency()
                results["command"] = facade.get_command("nonexistent")
                results["probe"] = facade.get_probe("nonexistent")

            reader = threading.Thread(target=read_from_request_thread, name="request-thread")
            reader.start()
            reader.join(timeout=2.0)
            assert not reader.is_alive(), "facade reads blocked on the worker's tick lock"
        finally:
            state.tick_lock.release()

        assert results["snapshot"] is not None
        assert results["attachment"] is None
        assert results["idempotency"] == {}
        assert results["command"] is None
        assert results["probe"] is None
    finally:
        mca_runtime.reset_state_for_tests()


# --- total command execution (correction #4) -------------------------------


def test_handler_returning_a_wrong_type_is_recorded_as_command_execution_failed(tmp_path):
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    try:
        ether = InMemoryEther()
        transport = FakeRadioTransport(ether, "!aaaaaaaa")
        state = mca_runtime._get_state(str(tmp_path / "s"))  # noqa: SLF001

        def handler(command):
            return "not-a-CommandOutcome"

        state.dispatcher = CommandDispatcher({"attachment_cancel": handler})
        state.ensure_service(transport)
        assert state.service.stop()
        state.ready_event.set()  # deterministic tick driving (see _started_state)

        state.facade.submit(Command(command_id="cmd-4", kind="attachment_cancel", payload={}, created_at=0.0))
        state.service.tick()

        result = state.facade.get_command("cmd-4")
        assert result.status == STATUS_FAILED
        assert result.error_code == COMMAND_EXECUTION_FAILED
    finally:
        mca_runtime.reset_state_for_tests()


def test_handler_constructing_an_invalid_outcome_is_recorded_as_command_execution_failed(tmp_path):
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    try:
        ether = InMemoryEther()
        transport = FakeRadioTransport(ether, "!aaaaaaaa")
        state = mca_runtime._get_state(str(tmp_path / "t"))  # noqa: SLF001

        def handler(command):
            # CommandOutcome.__post_init__ raises (empty error_code), and the
            # dispatch try/except must convert it to command_execution_failed.
            return CommandOutcome.failed("")

        state.dispatcher = CommandDispatcher({"attachment_cancel": handler})
        state.ensure_service(transport)
        assert state.service.stop()
        state.ready_event.set()  # deterministic tick driving (see _started_state)

        state.facade.submit(Command(command_id="cmd-5", kind="attachment_cancel", payload={}, created_at=0.0))
        state.service.tick()

        result = state.facade.get_command("cmd-5")
        assert result.status == STATUS_FAILED
        assert result.error_code == COMMAND_EXECUTION_FAILED
    finally:
        mca_runtime.reset_state_for_tests()


# --- the completed read facade over the real runtime (correction #2) -------


def test_facade_read_surface_resolves_to_the_real_monitor_and_principal(tmp_path):
    state, _, _ = _started_state(tmp_path, "u")
    try:
        facade = state.facade
        # identity_snapshot() is the state's own principal (no copy).
        assert facade.identity_snapshot() is state.principal
        # connectivity_snapshot() is the monitor's own published view (the
        # same object, not a copy) - a request thread reads in-memory state.
        conn = facade.connectivity_snapshot()
        assert isinstance(conn, ConnectivitySnapshot)
        assert conn is state.connectivity_monitor.snapshot()
        assert conn.relays == {}  # no providers registered in this test
        # provider_snapshot() is the (empty) registered-provider view.
        assert facade.provider_snapshot() == {}
        # evaluate_upload_readiness() answers for an unknown provider with a
        # structured rejection, never an exception.
        decision = facade.evaluate_upload_readiness("never-registered")
        assert decision.ready is False
        assert decision.reason == UploadRejectionReason.PROFILE_NOT_FOUND
    finally:
        mca_runtime.reset_state_for_tests()


# --- runtime readiness lifecycle (correction #1) ----------------------------

def _empty_snapshot():
    return AttachmentsSnapshot(records=(), by_id={}, idempotency={}, built_at=0.0)


def test_facade_raises_not_ready_before_startup(tmp_path):
    # Startup in progress: the state exists (facade is constructed) but the
    # service has not started, so readiness is unset and the snapshot-backed
    # surface raises - and submit() rejects without registering/enqueueing.
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    try:
        state = mca_runtime._get_state(str(tmp_path / "v"))  # noqa: SLF001
        assert not state.ready_event.is_set()
        with pytest.raises(FacadeNotReady):
            state.facade.attachments_snapshot()
        with pytest.raises(FacadeNotReady):
            state.facade.submit(Command(command_id="c", kind="attachment_cancel", payload={}, created_at=0.0))
        assert state.facade.get_command("c") is None
        assert state.command_queue.qsize() == 0
    finally:
        mca_runtime.reset_state_for_tests()


def test_publish_readiness_requires_started_and_a_published_snapshot(tmp_path):
    # The two halves of readiness: started *and* first snapshot published.
    # Also covers first-snapshot failure (snapshot stays None -> not ready)
    # and the successful retry.
    state, _, _ = _started_state(tmp_path, "w")
    svc = state.service
    try:
        # Not started (stop() cleared _started): readiness stays unset even
        # though the snapshot is already published.
        state.ready_event.clear()
        svc._started = False  # noqa: SLF001
        svc._publish_readiness()
        assert not state.ready_event.is_set()

        # Started, but the first snapshot publish failed (snapshot() is None):
        # still not ready - a failed first build must not flip readiness.
        svc._snapshot_publisher._snapshot = None  # noqa: SLF001
        svc._started = True  # noqa: SLF001
        svc._publish_readiness()
        assert not state.ready_event.is_set()

        # Retry: the snapshot is now published, so readiness flips true.
        svc._snapshot_publisher._snapshot = _empty_snapshot()  # noqa: SLF001
        svc._publish_readiness()
        assert state.ready_event.is_set()
    finally:
        mca_runtime.reset_state_for_tests()


def test_stop_clears_readiness(tmp_path):
    # Correction #1: readiness is cleared the moment the service stops, so a
    # request thread observing a stopped service gets FacadeNotReady, never a
    # stale "ready" signal. After the deterministic first stop, a further
    # stop() (idempotent, no worker thread left to race the clear) must still
    # clear an already-set event.
    state, _, _ = _started_state(tmp_path, "x")
    try:
        assert state.ready_event.is_set()  # set by _started_state
        assert state.service.stop()
        assert not state.ready_event.is_set()
    finally:
        mca_runtime.reset_state_for_tests()


# --- terminalization recovery (final correction pass) ----------------------
#
# The worker must never leave a dequeued command stuck in `running` (or
# silently dropped) just because a *registry transition itself* failed. These
# tests force `mark_running`/`mark_succeeded`/`mark_failed` to raise and assert
# the registry's internal-recovery fail-safe produces a pollable terminal
# FAILED with `command_execution_failed`, and that one broken command never
# stops the drain.


def _state_with_handlers(tmp_path, tag, handlers):
    """Build the runtime with a *custom* dispatcher (so a handler can return a
    success/failure outcome), stop the worker for deterministic tick driving,
    and re-set readiness - mirroring the setup inside the raising-handler tests
    above but for arbitrary kind->handler tables."""
    mca_runtime.reset_state_for_tests()
    mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa")
    state = mca_runtime._get_state(str(tmp_path / tag))  # noqa: SLF001
    state.dispatcher = CommandDispatcher(handlers)
    state.ensure_service(transport)
    assert state.service.stop()
    state.ready_event.set()
    return state


def test_mark_succeeded_failure_recovers_to_command_execution_failed(tmp_path, monkeypatch):
    state = _state_with_handlers(
        tmp_path, "rec-ok",
        {"attachment_cancel": lambda c: CommandOutcome.succeeded(resource_id="att-1", result={"attachment_id": "att-1"})},
    )
    try:
        monkeypatch.setattr(
            state.command_registry, "mark_succeeded",
            lambda command_id, **kwargs: (_ for _ in ()).throw(RuntimeError("registry transition failed")),
        )

        state.facade.submit(Command(command_id="cmd-1", kind="attachment_cancel", payload={}, created_at=0.0))
        state.service.tick()  # must not raise

        result = state.facade.get_command("cmd-1")
        assert result.status == STATUS_FAILED
        assert result.error_code == COMMAND_EXECUTION_FAILED
        assert result.resource_id is None  # the failed transition carried no domain id
        assert result.result is None       # nor any payload
    finally:
        mca_runtime.reset_state_for_tests()


def test_mark_failed_failure_recovers_to_command_execution_failed(tmp_path, monkeypatch):
    def handler(command):
        raise RuntimeError("handler bug")

    state = _state_with_handlers(tmp_path, "rec-fail", {"attachment_cancel": handler})
    try:
        monkeypatch.setattr(
            state.command_registry, "mark_failed",
            lambda command_id, **kwargs: (_ for _ in ()).throw(RuntimeError("registry transition failed")),
        )

        state.facade.submit(Command(command_id="cmd-2", kind="attachment_cancel", payload={}, created_at=0.0))
        state.service.tick()  # must not raise

        result = state.facade.get_command("cmd-2")
        assert result.status == STATUS_FAILED
        assert result.error_code == COMMAND_EXECUTION_FAILED
        assert result.resource_id is None
        assert result.result is None
    finally:
        mca_runtime.reset_state_for_tests()


def test_missing_registry_entry_before_execution_recovers_to_terminal_failure(tmp_path):
    state, _, _ = _started_state(tmp_path, "rec-missing")
    try:
        facade = state.facade
        facade.submit(Command(command_id="cmd-3", kind="attachment_cancel", payload={}, created_at=0.0))
        assert facade.get_command("cmd-3").status == STATUS_QUEUED
        # Invalidate the entry before the worker drains it (as if the queue
        # write had raced/rolled back): mark_running now finds no entry and
        # raises, and the fail-safe materializes a pollable terminal FAILED.
        state.command_registry.discard_queued("cmd-3")
        assert facade.get_command("cmd-3") is None

        state.service.tick()  # must not raise

        result = facade.get_command("cmd-3")
        assert result.status == STATUS_FAILED
        assert result.error_code == COMMAND_EXECUTION_FAILED
        assert result.kind == "attachment_cancel"
    finally:
        mca_runtime.reset_state_for_tests()


def test_one_broken_command_does_not_stop_the_drain(tmp_path, monkeypatch):
    def ok_handler(command):
        return CommandOutcome.succeeded(resource_id=command.command_id, result={})

    state = _state_with_handlers(tmp_path, "rec-drain", {"attachment_cancel": ok_handler})
    try:
        original = state.command_registry.mark_succeeded

        def flaky_mark_succeeded(command_id, **kwargs):
            if command_id == "cmd-a":
                raise RuntimeError("registry transition failed")
            return original(command_id, **kwargs)

        monkeypatch.setattr(state.command_registry, "mark_succeeded", flaky_mark_succeeded)

        facade = state.facade
        facade.submit(Command(command_id="cmd-a", kind="attachment_cancel", payload={}, created_at=0.0))
        facade.submit(Command(command_id="cmd-b", kind="attachment_cancel", payload={}, created_at=0.0))

        state.service.tick()  # drains both; the first failure must not stop cmd-b

        first = facade.get_command("cmd-a")
        assert first.status == STATUS_FAILED
        assert first.error_code == COMMAND_EXECUTION_FAILED

        second = facade.get_command("cmd-b")
        assert second.status == STATUS_SUCCEEDED
        assert second.resource_id == "cmd-b"
    finally:
        mca_runtime.reset_state_for_tests()


def test_handler_exception_text_is_not_logged(tmp_path, caplog):
    # Step 1.6A.2 safe-logging correction: a handler exception's *text* may
    # embed file names, Relay tokens, keys, comments or other untrusted
    # command input, so the worker logs only the safe identifier, kind and
    # exception class - never the message, and never a traceback.
    marker = "SENSITIVE-MARKER-DO-NOT-LOG-9f2c"

    def handler(command):
        raise RuntimeError(f"upload failed for {marker}")

    state = _state_with_handlers(tmp_path, "log", {"attachment_cancel": handler})
    try:
        state.facade.submit(
            Command(command_id="cmd-log", kind="attachment_cancel", payload={}, created_at=0.0)
        )

        with caplog.at_level(logging.ERROR, logger="meshsrv.attachments.service"):
            state.service.tick()

        # The command still reaches the correct terminal state.
        result = state.facade.get_command("cmd-log")
        assert result.status == STATUS_FAILED
        assert result.error_code == COMMAND_EXECUTION_FAILED

        logged = caplog.text
        assert "RuntimeError" in logged   # exception *class* is logged
        assert "cmd-log" in logged        # command id is logged
        assert marker not in logged       # sensitive message is not
    finally:
        mca_runtime.reset_state_for_tests()


# --- Step 1.6A.3A/3B command handlers (worker side) -------------------------
#
# The wired kinds are the service's own methods, and the worker is the sole
# executor. These tests drive the real runtime with the network pinned down
# (_AlwaysDownSession), so any handler that strayed into radio/Relay/
# provider I/O would raise a ConnectionError rather than silently pass. They
# pin: the dispatcher wires exactly the three lifecycle kinds plus the 3B
# create kind; an unknown id drains to `attachment_not_found`; a wrong
# direction/state drains to `invalid_state_transition` against the *persisted*
# row (re-read on the worker, not the request thread's snapshot); and the
# handlers delegate to the same sender/receiver primitives the tick itself
# uses.


def _seed_row(state, attachment_id, direction, state_name):
    """Insert one minimal `attachments` row directly into the worker-owned
    connection (the sole owner of `conn`), so a lifecycle-command handler has
    a persisted row to re-validate against. Only the NOT-NULL columns are set;
    every nullable field stays NULL."""
    state.conn.execute(
        """
        INSERT INTO attachments
            (id, workspace_id, transfer_id, direction, principal_id, state,
             created_at, hard_expires_at, download_grace_seconds)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, 3600)
        """,
        (attachment_id, state.principal.workspace_id, uuid.uuid4().hex,
         direction, state.principal.principal_id, state_name),
    )
    state.conn.commit()


def test_real_service_wires_the_lifecycle_and_create_handlers(tmp_path):
    state, _, _ = _started_state(tmp_path, "lifecycle-wire")
    try:
        dispatcher = state.dispatcher
        # The service built the real dispatcher (we passed None at
        # ensure_service) and mirrored it back onto the state - one fixed
        # kind->handler table shared by the state and the worker.
        assert dispatcher.supported_kinds() == frozenset({
            "attachment_create", "attachment_retry", "attachment_download",
            "attachment_reject",
        })
        # Every other enumerated kind remains unwired -> unsupported.
        for kind in COMMAND_KINDS - dispatcher.supported_kinds():
            assert dispatcher.handler_for(kind) is None
    finally:
        mca_runtime.reset_state_for_tests()


def test_lifecycle_commands_drain_to_attachment_not_found_on_unknown_id(tmp_path):
    state, _, _ = _started_state(tmp_path, "lifecycle-unknown")
    try:
        facade = state.facade
        unknown = "a" * 32
        for kind in ("attachment_retry", "attachment_download", "attachment_reject"):
            facade.submit(Command(
                command_id=f"cmd-{kind}", kind=kind,
                payload={"attachment_id": unknown}, created_at=0.0,
            ))
        state.service.tick()
        for kind in ("attachment_retry", "attachment_download", "attachment_reject"):
            result = facade.get_command(f"cmd-{kind}")
            assert result.status == STATUS_FAILED
            assert result.error_code == "attachment_not_found"
    finally:
        mca_runtime.reset_state_for_tests()


def test_retry_on_terminal_sent_row_is_invalid_state_transition(tmp_path):
    state, _, _ = _started_state(tmp_path, "lifecycle-retry-term")
    try:
        aid = "b" * 32
        _seed_row(state, aid, "sent", sender.FAILED_UPLOAD)
        state.facade.submit(Command(
            command_id="cmd-r", kind="attachment_retry",
            payload={"attachment_id": aid}, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-r")
        assert result.status == STATUS_FAILED
        assert result.error_code == "invalid_state_transition"
    finally:
        mca_runtime.reset_state_for_tests()


def test_retry_on_waiting_consent_received_row_is_not_retryable(tmp_path):
    # WAITING_CONSENT is a manual-action state, not in
    # receiver.AUTOMATIC_STATES - retry must never bypass consent.
    state, _, _ = _started_state(tmp_path, "lifecycle-retry-consent")
    try:
        aid = "c" * 32
        _seed_row(state, aid, "received", receiver.WAITING_CONSENT)
        state.facade.submit(Command(
            command_id="cmd-r", kind="attachment_retry",
            payload={"attachment_id": aid}, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-r")
        assert result.status == STATUS_FAILED
        assert result.error_code == "invalid_state_transition"
        assert receiver.get_state(state.conn, aid) == receiver.WAITING_CONSENT
    finally:
        mca_runtime.reset_state_for_tests()


def test_download_on_a_sent_row_is_invalid_state_transition(tmp_path):
    state, _, _ = _started_state(tmp_path, "lifecycle-dl-dir")
    try:
        aid = "d" * 32
        _seed_row(state, aid, "sent", sender.DRAFT)
        state.facade.submit(Command(
            command_id="cmd-dl", kind="attachment_download",
            payload={"attachment_id": aid}, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-dl")
        assert result.status == STATUS_FAILED
        assert result.error_code == "invalid_state_transition"
    finally:
        mca_runtime.reset_state_for_tests()


def test_download_command_drives_begin_download(tmp_path, monkeypatch):
    # DOWNLOADING is in receiver.AUTOMATIC_STATES, so the tick's own row-scan
    # would legitimately advance it (to FAILED, with no provider/network) in
    # the same tick. Pin _due_rows to [] to isolate the command handler's
    # effect: begin_download() moves WAITING_CONSENT -> DOWNLOADING, nothing else.
    state, _, _ = _started_state(tmp_path, "lifecycle-download")
    try:
        aid = "e" * 32
        _seed_row(state, aid, "received", receiver.WAITING_CONSENT)
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])
        state.facade.submit(Command(
            command_id="cmd-dl", kind="attachment_download",
            payload={"attachment_id": aid}, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-dl")
        assert result.status == STATUS_SUCCEEDED
        assert result.result == {"attachment_id": aid, "state": receiver.DOWNLOADING}
        assert receiver.get_state(state.conn, aid) == receiver.DOWNLOADING
    finally:
        mca_runtime.reset_state_for_tests()


def test_reject_command_drives_reject(tmp_path):
    state, _, _ = _started_state(tmp_path, "lifecycle-reject")
    try:
        aid = "f" * 32
        _seed_row(state, aid, "received", receiver.WAITING_CONSENT)
        state.facade.submit(Command(
            command_id="cmd-rj", kind="attachment_reject",
            payload={"attachment_id": aid}, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-rj")
        assert result.status == STATUS_SUCCEEDED
        assert result.result == {"attachment_id": aid, "state": receiver.REJECTED}
        assert receiver.get_state(state.conn, aid) == receiver.REJECTED
    finally:
        mca_runtime.reset_state_for_tests()


def test_retry_command_delegates_to_the_same_step_path(tmp_path, monkeypatch):
    # retry re-drives the tick's own _step_sent/_step_received - it neither
    # mints a new transfer_id nor bypasses per-row provider selection. Stub the
    # step to record the delegated row id, and pin _due_rows to [] so the
    # command handler is the *only* thing driving it this tick.
    state, _, _ = _started_state(tmp_path, "lifecycle-retry-delegate")
    try:
        aid = "ab" + "0" * 30
        _seed_row(state, aid, "sent", sender.DRAFT)
        calls = []
        monkeypatch.setattr(state.service, "_step_sent", lambda row: calls.append(row["id"]))
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])
        state.facade.submit(Command(
            command_id="cmd-retry", kind="attachment_retry",
            payload={"attachment_id": aid}, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-retry")
        assert result.status == STATUS_SUCCEEDED
        assert calls == [aid]
    finally:
        mca_runtime.reset_state_for_tests()


# --- create-worker failure injection (Finding 3) ----------------------------
#
# The worker-side `_command_create` re-validates the complete payload and, on
# every failure before a successful commit, must roll back any partial row,
# remove the pending reservation, and remove the staged plaintext - never
# reporting success while the newly-unused plaintext is known to still exist.
# These tests inject failures to pin each guarantee: no partial rows, no stale
# reservation, no orphaned plaintext, a terminal command result, and a
# subsequent command that still drains normally.


def _valid_create_payload(*, attachment_id="a" * 32, client_request_id="req-create-1", provider_id=None):
    return {
        "attachment_id": attachment_id,
        "client_request_id": client_request_id,
        "canonical_hash": "f" * 64,
        "source_address": "!aaaaaaaa",
        "source_name": "file.bin",
        "mime_type": "application/octet-stream",
        "provider_id": provider_id if provider_id is not None else encode_provider_id(b"\x01" * 8),
        "comment": None,
        "hard_ttl_seconds": 3600,
        "download_grace_seconds": 3600,
    }


def _register_ready_provider(state, *, enabled=True, upload_allowed=True, token=True, max_ciphertext_bytes=5 * 1024 * 1024):
    """Register a provider in the worker's registry and return its canonical
    `provider_id`. Ready by default (`enabled`, `upload_allowed`, and an
    upload token on file), so the worker-side create recheck (Finding 6)
    passes; individual tests flip one flag to exercise a specific rejection."""
    registry = state.service._provider_registry  # noqa: SLF001
    provider_id = compute_provider_id("https://relay.example.net", b"K" * 32)
    registry.register(
        display_name="Example Relay",
        base_url="https://relay.example.net",
        service_public_key=b"K" * 32,
        max_ciphertext_bytes=max_ciphertext_bytes,
        upload_allowed=upload_allowed,
    )
    if token:
        registry.set_upload_token(
            provider_id, state.workspace_manager, state.principal.principal_id, "test-upload-token"
        )
    if not enabled:
        registry.update_profile(provider_id, enabled=False)
    return provider_id


def _stage_spool(state, attachment_id, data=b"staged-plaintext"):
    spool_dir = state.workspace_manager.paths(state.principal.principal_id).spool_outgoing
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / attachment_id
    path.write_bytes(data)
    return path


def _spool_path(state, attachment_id):
    return state.workspace_manager.paths(state.principal.principal_id).spool_outgoing / attachment_id


def _trusted_binding():
    return SimpleNamespace(
        status=AddressStatus.MCA_READY,
        public_identity=b"\x02" * 32,
        sender_key_id="1" * 16,
    )


def _reserve(state, client_request_id, attachment_id, command_id):
    state.pending_reservations.reserve(
        client_request_id,
        PendingReservation(
            canonical_hash="f" * 64, attachment_id=attachment_id, command_id=command_id
        ),
        committed_entries={},
    )


def _seed_committed_create_row(state, attachment_id, client_request_id, canonical_hash):
    """Insert a committed row carrying the idempotency columns, so a duplicate
    create collides on the Migration-11 unique index."""
    state.conn.execute(
        """
        INSERT INTO attachments
            (id, workspace_id, transfer_id, direction, principal_id, state,
             created_at, hard_expires_at, download_grace_seconds,
             client_request_id, canonical_hash)
        VALUES (?, ?, ?, 'sent', ?, 'DRAFT', 0, 0, 3600, ?, ?)
        """,
        (attachment_id, state.principal.workspace_id, uuid.uuid4().hex,
         state.principal.principal_id, client_request_id, canonical_hash),
    )
    state.conn.commit()


def test_spool_path_for_rejects_non_hex_attachment_ids(tmp_path):
    # The spool path must be derivable only from a strictly-validated 32-hex id
    # (Finding 3/4) - a path-traversal, wrong-case, wrong-length, or
    # newline-terminated id must never produce a filesystem path.
    state, _, _ = _started_state(tmp_path, "spool-path-safety")
    try:
        svc = state.service
        assert svc._spool_path_for("a" * 32) is not None
        for bad in ("../../evil", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n", "", None, 123):
            assert svc._spool_path_for(bad) is None
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_invalid_payload_cleans_reservation_and_spool(tmp_path):
    state, _, _ = _started_state(tmp_path, "create-invalid-cleanup")
    try:
        attachment_id = "a" * 32
        client_request_id = "req-invalid"
        _stage_spool(state, attachment_id)
        _reserve(state, client_request_id, attachment_id, "cmd-invalid")
        payload = _valid_create_payload(
            attachment_id=attachment_id, client_request_id=client_request_id
        )
        payload["canonical_hash"] = "not-hex"  # invalid -> the worker must reject
        state.facade.submit(Command(
            command_id="cmd-invalid", kind="attachment_create",
            payload=payload, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-invalid")
        assert result.status == STATUS_FAILED
        assert result.error_code == "invalid_payload"
        # no stale reservation, no orphaned spool, no partial row.
        assert len(state.pending_reservations) == 0
        assert not _spool_path(state, attachment_id).exists()
        assert state.conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_invalid_attachment_id_builds_no_spool_path(tmp_path):
    state, _, _ = _started_state(tmp_path, "create-invalid-id")
    try:
        payload = _valid_create_payload()
        payload["attachment_id"] = "../../evil"
        state.facade.submit(Command(
            command_id="cmd-evil", kind="attachment_create",
            payload=payload, created_at=0.0,
        ))
        state.service.tick()
        result = state.facade.get_command("cmd-evil")
        assert result.status == STATUS_FAILED
        assert result.error_code == "invalid_payload"
        # Nothing was committed, and no path was ever derived from the hostile id.
        assert state.conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_failure_rolls_back_partial_row_and_removes_reservation_and_spool(tmp_path, monkeypatch):
    state, _, _ = _started_state(tmp_path, "create-partial-rollback")
    try:
        attachment_id = "b" * 32
        client_request_id = "req-partial"
        _stage_spool(state, attachment_id)
        _reserve(state, client_request_id, attachment_id, "cmd-partial")
        provider_id = _register_ready_provider(state)
        monkeypatch.setattr(state.service._key_exchange, "get_binding", lambda addr: _trusted_binding())
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])

        def _partial_then_raise(conn, *args, **kwargs):
            # Write one row inside the open transaction, then raise - the worker
            # must roll it back so no partial row survives the failure.
            conn.execute(
                "INSERT INTO attachments "
                "(id, workspace_id, transfer_id, direction, principal_id, state, "
                "created_at, hard_expires_at, download_grace_seconds) "
                "VALUES (?, ?, ?, 'sent', ?, 'DRAFT', 0, 0, 3600)",
                ("cc" * 16, "ws-partial", "00" * 16, "p" * 16),
            )
            raise RuntimeError("injected pre-commit failure")

        monkeypatch.setattr(sender, "create_draft", _partial_then_raise)

        state.facade.submit(Command(
            command_id="cmd-partial", kind="attachment_create",
            payload=_valid_create_payload(
                attachment_id=attachment_id, client_request_id=client_request_id, provider_id=provider_id
            ),
            created_at=0.0,
        ))
        state.service.tick()

        result = state.facade.get_command("cmd-partial")
        assert result.status == STATUS_FAILED
        assert result.error_code == COMMAND_EXECUTION_FAILED
        # No partial row (the injected INSERT was rolled back), no stale
        # reservation, no orphaned staged plaintext.
        assert state.conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        assert len(state.pending_reservations) == 0
        assert not _spool_path(state, attachment_id).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_failed_create_does_not_block_the_next_command(tmp_path, monkeypatch):
    state, _, _ = _started_state(tmp_path, "create-next-drains")
    try:
        monkeypatch.setattr(state.service._key_exchange, "get_binding", lambda addr: None)
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])

        attachment_id = "d" * 32
        provider_id = _register_ready_provider(state)
        _stage_spool(state, attachment_id)
        _reserve(state, "req-a", attachment_id, "cmd-a")
        state.facade.submit(Command(
            command_id="cmd-a", kind="attachment_create",
            payload=_valid_create_payload(attachment_id=attachment_id, client_request_id="req-a", provider_id=provider_id),
            created_at=0.0,
        ))
        # A second command queued in the same tick must still drain to its own
        # terminal result after the failed create.
        state.facade.submit(Command(
            command_id="cmd-b", kind="attachment_reject",
            payload={"attachment_id": "e" * 32}, created_at=0.0,
        ))

        state.service.tick()

        assert state.facade.get_command("cmd-a").status == STATUS_FAILED
        assert state.facade.get_command("cmd-a").error_code == "recipient_not_found"
        assert state.facade.get_command("cmd-b").status == STATUS_FAILED
        assert state.facade.get_command("cmd-b").error_code == "attachment_not_found"
        # The failed create cleaned up its own reservation and staged file.
        assert len(state.pending_reservations) == 0
        assert not _spool_path(state, attachment_id).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_duplicate_create_reports_spool_cleanup_failed_and_retries(tmp_path, monkeypatch):
    import pathlib

    state, _, _ = _started_state(tmp_path, "create-dup-cleanup-fail")
    try:
        client_request_id = "req-dup"
        existing_id = "aa" + "0" * 30
        new_id = "bb" + "0" * 30
        canonical_hash = "f" * 64
        _seed_committed_create_row(state, existing_id, client_request_id, canonical_hash)
        _stage_spool(state, new_id)
        _reserve(state, client_request_id, new_id, "cmd-dup")
        provider_id = _register_ready_provider(state)
        monkeypatch.setattr(state.service._key_exchange, "get_binding", lambda addr: _trusted_binding())
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])

        real_unlink = pathlib.Path.unlink

        def _failing_unlink(self, *args, **kwargs):
            raise OSError("injected unlink failure")

        monkeypatch.setattr(pathlib.Path, "unlink", _failing_unlink)

        state.facade.submit(Command(
            command_id="cmd-dup", kind="attachment_create",
            payload=_valid_create_payload(attachment_id=new_id, client_request_id=client_request_id, provider_id=provider_id),
            created_at=0.0,
        ))
        state.service.tick()

        result = state.facade.get_command("cmd-dup")
        assert result.status == STATUS_FAILED
        assert result.error_code == "spool_cleanup_failed"
        # The reservation is gone, but the duplicate staged file still exists
        # and its id is retained for a bounded retry.
        assert len(state.pending_reservations) == 0
        assert new_id in state.service._spool_cleanup_backlog
        assert _spool_path(state, new_id).exists()

        # Once unlink works again, the bounded drain removes the orphan.
        monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)
        state.service._drain_spool_cleanup()
        assert not _spool_path(state, new_id).exists()
        assert len(state.service._spool_cleanup_backlog) == 0
    finally:
        mca_runtime.reset_state_for_tests()


# --- Finding 6: worker-side provider recheck ---------------------------------
#
# The request thread validates provider policy from its immutable snapshot; the
# worker must re-resolve from its own registry and recheck the same local
# configuration immediately before committing the draft, because the profile
# may have been removed/disabled or its policy/token changed in the window
# between the request snapshot and execution. Each rejection must fail the
# command safely (terminal result, no row, no stale reservation, no orphaned
# staged plaintext).


def _stage_reserve_enqueue(state, command_id, attachment_id, client_request_id, provider_id):
    """Simulate a request thread that was *accepted* against a valid snapshot:
    stage the plaintext, add the pending reservation, and enqueue the create."""
    _stage_spool(state, attachment_id)
    _reserve(state, client_request_id, attachment_id, command_id)
    state.facade.submit(Command(
        command_id=command_id, kind="attachment_create",
        payload=_valid_create_payload(
            attachment_id=attachment_id, client_request_id=client_request_id, provider_id=provider_id
        ),
        created_at=0.0,
    ))


def _assert_create_failed_and_cleaned(state, command_id, attachment_id, error_code):
    result = state.facade.get_command(command_id)
    assert result.status == STATUS_FAILED
    assert result.error_code == error_code
    assert state.conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert len(state.pending_reservations) == 0
    assert not _spool_path(state, attachment_id).exists()


def test_create_worker_rejects_unregistered_provider(tmp_path):
    # A create whose provider_id resolves to nothing (never registered) must
    # fail cleanly before any draft is committed.
    state, _, _ = _started_state(tmp_path, "create-provider-missing")
    try:
        attachment_id = "1a" + "0" * 30
        _stage_reserve_enqueue(
            state, "cmd-provider-missing", attachment_id, "req-provider-missing",
            encode_provider_id(b"\x01" * 8),
        )
        state.service.tick()
        _assert_create_failed_and_cleaned(
            state, "cmd-provider-missing", attachment_id, "provider_not_found"
        )
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_worker_rechecks_provider_disabled_after_snapshot(tmp_path):
    # The stale-snapshot race: the request thread saw an enabled provider and
    # staged+enqueued; the provider is then disabled before the worker commits.
    # The worker's fresh resolve must reject with provider_disabled.
    state, _, _ = _started_state(tmp_path, "create-provider-disabled-drift")
    try:
        provider_id = _register_ready_provider(state)
        attachment_id = "2b" + "0" * 30
        _stage_reserve_enqueue(state, "cmd-provider-disabled", attachment_id, "req-disabled", provider_id)
        state.service._provider_registry.update_profile(provider_id, enabled=False)  # noqa: SLF001
        state.service.tick()
        _assert_create_failed_and_cleaned(state, "cmd-provider-disabled", attachment_id, "provider_disabled")
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_worker_rechecks_upload_not_allowed_after_snapshot(tmp_path):
    state, _, _ = _started_state(tmp_path, "create-upload-disallowed-drift")
    try:
        provider_id = _register_ready_provider(state)
        attachment_id = "3c" + "0" * 30
        _stage_reserve_enqueue(state, "cmd-upload-disallowed", attachment_id, "req-disallowed", provider_id)
        state.service._provider_registry.update_profile(provider_id, upload_allowed=False)  # noqa: SLF001
        state.service.tick()
        _assert_create_failed_and_cleaned(state, "cmd-upload-disallowed", attachment_id, "upload_not_allowed")
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_worker_rechecks_upload_token_missing_after_snapshot(tmp_path):
    state, _, _ = _started_state(tmp_path, "create-token-missing-drift")
    try:
        provider_id = _register_ready_provider(state)
        attachment_id = "4d" + "0" * 30
        _stage_reserve_enqueue(state, "cmd-token-missing", attachment_id, "req-token-missing", provider_id)
        state.service._provider_registry.clear_upload_token(  # noqa: SLF001
            provider_id, state.workspace_manager, state.principal.principal_id
        )
        state.service.tick()
        _assert_create_failed_and_cleaned(state, "cmd-token-missing", attachment_id, "upload_token_missing")
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_worker_rechecks_provider_removed_after_snapshot(tmp_path):
    # Removed (not just disabled): remove_or_disable deletes the row when no
    # attachment references it, so the worker's resolve returns None.
    state, _, _ = _started_state(tmp_path, "create-provider-removed-drift")
    try:
        provider_id = _register_ready_provider(state)
        attachment_id = "5e" + "0" * 30
        _stage_reserve_enqueue(state, "cmd-provider-removed", attachment_id, "req-removed", provider_id)
        assert state.service._provider_registry.remove_or_disable(  # noqa: SLF001
            provider_id, state.workspace_manager, state.principal.principal_id
        ) == "deleted"
        state.service.tick()
        _assert_create_failed_and_cleaned(state, "cmd-provider-removed", attachment_id, "provider_not_found")
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_worker_rechecks_ciphertext_size(tmp_path):
    # Provider size policy: the deterministic ciphertext upper bound for the
    # staged plaintext must be checked against the worker's fresh profile. The
    # staged plaintext is 16 bytes, so its bound is 16 + 1*16 = 32; a provider
    # with max_ciphertext_bytes=31 must reject it as ciphertext_too_large.
    state, _, _ = _started_state(tmp_path, "create-ciphertext-too-large")
    try:
        provider_id = _register_ready_provider(state, max_ciphertext_bytes=31)
        attachment_id = "6f" + "0" * 30
        _stage_spool(state, attachment_id, data=b"x" * 16)
        _reserve(state, "req-ciphertext", attachment_id, "cmd-ciphertext")
        state.facade.submit(Command(
            command_id="cmd-ciphertext", kind="attachment_create",
            payload=_valid_create_payload(
                attachment_id=attachment_id, client_request_id="req-ciphertext", provider_id=provider_id
            ),
            created_at=0.0,
        ))
        state.service.tick()
        _assert_create_failed_and_cleaned(state, "cmd-ciphertext", attachment_id, "ciphertext_too_large")
    finally:
        mca_runtime.reset_state_for_tests()


# --- Finding 9: worker-side regression coverage ------------------------------
#
# The worker-side create half's remaining acceptance requirements: it must
# re-validate the *recipient* (not just the provider) against the live SQLite
# binding at commit time (KEY_CHANGED / removed drift), and its DB-level
# idempotency backstop (Migration-11 unique index -> IntegrityError fallback)
# must return the original attachment for a matching hash and a terminal
# conflict for a differing hash - never a second logical attachment.


def test_create_worker_rechecks_recipient_key_changed_after_snapshot(tmp_path):
    # The stale-snapshot race for the recipient: the request thread saw an
    # MCA_READY binding; the binding then gained a pending key change
    # (KEY_CHANGED) before the worker committed. The worker re-resolves from
    # SQLite and must reject with recipient_not_trusted - it never trusts the
    # queue's snapshot.
    state, _, _ = _started_state(tmp_path, "create-recipient-key-changed")
    try:
        provider_id = _register_ready_provider(state)
        _seed_trusted_binding(state)
        state.conn.execute(
            "UPDATE mca_recipient_bindings SET pending_public_identity = ? WHERE transport_address = ?",
            ("03" * 32, "!aaaaaaaa"),
        )
        state.conn.commit()
        attachment_id = "7a" + "0" * 30
        _stage_reserve_enqueue(
            state, "cmd-key-changed", attachment_id, "req-key-changed", provider_id
        )
        state.service.tick()
        _assert_create_failed_and_cleaned(
            state, "cmd-key-changed", attachment_id, "recipient_not_trusted"
        )
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_worker_rechecks_recipient_removed_after_snapshot(tmp_path):
    # Same race, other direction: the binding the request thread saw is gone by
    # commit time, so the worker re-resolves to None -> recipient_not_found.
    state, _, _ = _started_state(tmp_path, "create-recipient-removed")
    try:
        provider_id = _register_ready_provider(state)
        _seed_trusted_binding(state)
        state.conn.execute(
            "DELETE FROM mca_recipient_bindings WHERE transport_address = ?", ("!aaaaaaaa",)
        )
        state.conn.commit()
        attachment_id = "8b" + "0" * 30
        _stage_reserve_enqueue(
            state, "cmd-recipient-removed", attachment_id, "req-recipient-removed", provider_id
        )
        state.service.tick()
        _assert_create_failed_and_cleaned(
            state, "cmd-recipient-removed", attachment_id, "recipient_not_found"
        )
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_duplicate_matching_hash_returns_the_original_not_a_second_row(tmp_path, monkeypatch):
    # The DB-level idempotency backstop fires when a duplicate create collides on
    # the (workspace_id, client_request_id) unique index: the IntegrityError
    # fallback must return idempotent success with the ORIGINAL attachment_id -
    # never a second logical attachment, never a phantom command failure.
    state, _, _ = _started_state(tmp_path, "create-dup-match")
    try:
        client_request_id = "req-dup-match"
        existing_id = "aa" + "0" * 30
        new_id = "bb" + "0" * 30
        _seed_committed_create_row(state, existing_id, client_request_id, "f" * 64)
        provider_id = _register_ready_provider(state)
        monkeypatch.setattr(state.service._key_exchange, "get_binding", lambda addr: _trusted_binding())
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])
        _stage_reserve_enqueue(state, "cmd-dup-match", new_id, client_request_id, provider_id)

        state.service.tick()

        result = state.facade.get_command("cmd-dup-match")
        assert result.status == STATUS_SUCCEEDED
        assert result.resource_id == existing_id
        assert result.result == {"attachment_id": existing_id, "state": sender.DRAFT}
        assert state.conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert len(state.pending_reservations) == 0
        assert not _spool_path(state, new_id).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_create_duplicate_different_hash_is_conflict_without_a_second_row(tmp_path, monkeypatch):
    # Same client_request_id, different canonical_hash: the fallback must be a
    # terminal idempotency_conflict - no success, no second row, staged file
    # discarded.
    state, _, _ = _started_state(tmp_path, "create-dup-conflict")
    try:
        client_request_id = "req-dup-conflict"
        existing_id = "aa" + "0" * 30
        new_id = "bb" + "0" * 30
        _seed_committed_create_row(state, existing_id, client_request_id, "0" * 64)
        provider_id = _register_ready_provider(state)
        monkeypatch.setattr(state.service._key_exchange, "get_binding", lambda addr: _trusted_binding())
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])
        _stage_reserve_enqueue(state, "cmd-dup-conflict", new_id, client_request_id, provider_id)

        state.service.tick()

        result = state.facade.get_command("cmd-dup-conflict")
        assert result.status == STATUS_FAILED
        assert result.error_code == "idempotency_conflict"
        assert state.conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert len(state.pending_reservations) == 0
        assert not _spool_path(state, new_id).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_committed_replay_index_survives_restart(tmp_path, monkeypatch):
    # The idempotency index is rebuilt from the persisted `attachments` rows
    # (client_request_id + canonical_hash), not from in-memory state - so a
    # committed create is still replayed as "the original attachment" after a
    # full process restart (the Migration-11 index's whole point).
    data_dir = str(tmp_path / "idempotency-restart")
    client_request_id = "req-survives-restart"
    attachment_id = "cc" + "0" * 30
    try:
        mca_runtime.reset_state_for_tests()
        mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
        ether = InMemoryEther()
        transport = FakeRadioTransport(ether, "!aaaaaaaa")
        mca_runtime.start_attachments_service(data_dir, transport)
        state = mca_runtime._get_state(data_dir)  # noqa: SLF001
        assert state.service.stop()
        state.ready_event.set()
        monkeypatch.setattr(state.service, "_due_rows", lambda: [])
        _seed_committed_create_row(state, attachment_id, client_request_id, "f" * 64)
        state.service.tick()
        assert state.facade.committed_idempotency()[client_request_id].attachment_id == attachment_id

        # "Restart": drop the singleton (closes conn), then re-init the same
        # data_dir - the idempotency entry is re-read from disk, not memory.
        mca_runtime.reset_state_for_tests()
        mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
        ether2 = InMemoryEther()
        transport2 = FakeRadioTransport(ether2, "!aaaaaaaa")
        mca_runtime.start_attachments_service(data_dir, transport2)
        state2 = mca_runtime._get_state(data_dir)  # noqa: SLF001
        assert state2.service.stop()
        state2.ready_event.set()
        monkeypatch.setattr(state2.service, "_due_rows", lambda: [])
        state2.service.tick()
        assert state2.facade.committed_idempotency()[client_request_id].attachment_id == attachment_id
    finally:
        mca_runtime.reset_state_for_tests()


# --- orphan-staging recovery (Finding 5) ------------------------------------
#
# The create endpoint stages a temp file (`.{id}.<rand>.tmp`) then atomically
# publishes it as a committed spool file (`spool/outgoing/<32-hex id>`); a
# process crash between staging and enqueue/commit can leave either kind
# behind with no persisted row, reservation, or command referencing it.
# `_recover_orphaned_spool()` reclaims exactly those, and only those:
# old-enough, unreferenced, name-conforming files inside this workspace's
# spool/outgoing/ directory. It must preserve a referenced file (even across
# restart), an active request's fresh temp file, anything that does not match
# the two known name conventions, non-file entries, and symlinks - and it must
# stay bounded and never leak a path/identifier into a log line.


def _spool_dir(state):
    return state.workspace_manager.paths(state.principal.principal_id).spool_outgoing


def _set_old_mtime(path, now):
    """Backdate a spool file so it is unambiguously past the orphan age
    threshold (relative to the monkeypatched `service._now`)."""
    os.utime(path, (now - 7200, now - 7200))


def _old_orphan_committed(state, attachment_id, now):
    _stage_spool(state, attachment_id)
    _set_old_mtime(_spool_path(state, attachment_id), now)


def test_recovery_deletes_old_unreferenced_committed_orphan(tmp_path, monkeypatch):
    # Crash leftover: a committed spool file whose request died after
    # publish but before reserve/enqueue - no row, reservation, or command -
    # and which is now old. It must be reclaimed.
    state, _, _ = _started_state(tmp_path, "orphan-committed")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        aid = "a" * 32
        _old_orphan_committed(state, aid, now)

        state.service._recover_orphaned_spool()

        assert not _spool_path(state, aid).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_preserves_referenced_committed_file(tmp_path, monkeypatch):
    # A committed spool file referenced by a persisted `attachments` row must
    # be preserved no matter how old it is - it is a real, committed spool.
    state, _, _ = _started_state(tmp_path, "orphan-referenced")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        aid = "b" * 32
        _seed_row(state, aid, "sent", sender.DRAFT)
        _old_orphan_committed(state, aid, now)

        state.service._recover_orphaned_spool()

        assert _spool_path(state, aid).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_preserves_referenced_file_across_restart(tmp_path):
    # A committed spool file referenced by a persisted row survives a full
    # process restart: reset_state_for_tests() closes the connection and drops
    # the in-memory singleton, but the DB row (and the spool dir) persist on
    # disk, so a freshly-reopened runtime must still treat the file as
    # referenced - the reference check is DB-backed, not in-memory.
    data_dir = str(tmp_path / "orphan-restart")
    aid = "c" * 32
    try:
        mca_runtime.reset_state_for_tests()
        mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
        ether = InMemoryEther()
        transport = FakeRadioTransport(ether, "!aaaaaaaa")
        mca_runtime.start_attachments_service(data_dir, transport)
        state = mca_runtime._get_state(data_dir)  # noqa: SLF001
        assert state.service.stop()
        state.ready_event.set()
        _seed_row(state, aid, "received", receiver.WAITING_CONSENT)
        _stage_spool(state, aid)
        _set_old_mtime(_spool_path(state, aid), time.time())
        state.service._recover_orphaned_spool()
        assert _spool_path(state, aid).exists()

        # "Restart": drop the singleton (closes conn), then re-init the same
        # data_dir - the row is re-read from disk.
        mca_runtime.reset_state_for_tests()
        mca_runtime.set_connectivity_session_for_tests(_AlwaysDownSession())
        ether2 = InMemoryEther()
        transport2 = FakeRadioTransport(ether2, "!aaaaaaaa")
        mca_runtime.start_attachments_service(data_dir, transport2)
        state2 = mca_runtime._get_state(data_dir)  # noqa: SLF001
        assert state2.service.stop()
        state2.ready_event.set()
        state2.service._recover_orphaned_spool()
        assert _spool_path(state2, aid).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_preserves_fresh_temp_file(tmp_path, monkeypatch):
    # An active request's freshly-staged temp file (`.{id}.<rand>.tmp`) is far
    # younger than the age threshold and must never be deleted, even though it
    # has no row/reservation/command yet (the stage -> reserve window).
    state, _, _ = _started_state(tmp_path, "orphan-fresh-temp")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        spool_dir = _spool_dir(state)
        spool_dir.mkdir(parents=True, exist_ok=True)
        temp_path = spool_dir / ".deadbeef.1234.tmp"
        temp_path.write_bytes(b"staged")
        os.utime(temp_path, (now - 10, now - 10))  # 10s old - fresh

        state.service._recover_orphaned_spool()

        assert temp_path.exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_deletes_old_temp_orphan(tmp_path, monkeypatch):
    # A temp file older than the threshold is a crash leftover (staging began
    # but never completed the atomic publish) - it must be reclaimed.
    state, _, _ = _started_state(tmp_path, "orphan-old-temp")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        spool_dir = _spool_dir(state)
        spool_dir.mkdir(parents=True, exist_ok=True)
        temp_path = spool_dir / ".deadbeef.5678.tmp"
        temp_path.write_bytes(b"staged")
        os.utime(temp_path, (now - 7200, now - 7200))

        state.service._recover_orphaned_spool()

        assert not temp_path.exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_preserves_reservation_and_queued_command_references(tmp_path, monkeypatch):
    # A committed spool file that is referenced only in-memory - by a pending
    # §3.6 reservation or a queued command (before the worker has committed the
    # row) - must be preserved. These are in-flight requests, not orphans.
    state, _, _ = _started_state(tmp_path, "orphan-inflight")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)

        reserved_id = "d" * 32
        _reserve(state, "req-reserved", reserved_id, "cmd-reserved")
        _old_orphan_committed(state, reserved_id, now)

        queued_id = "e" * 32
        _old_orphan_committed(state, queued_id, now)
        state.command_queue.put_nowait(Command(
            command_id="cmd-queued", kind="attachment_create",
            payload={"attachment_id": queued_id}, created_at=0.0,
        ))

        state.service._recover_orphaned_spool()

        assert _spool_path(state, reserved_id).exists()
        assert _spool_path(state, queued_id).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_bounds_deletions_per_tick(tmp_path, monkeypatch):
    # At most MAX_ORPHAN_DELETE_PER_TICK files are deleted per tick, so a burst
    # of orphans is drained gradually rather than one tick doing unbounded work.
    state, _, _ = _started_state(tmp_path, "orphan-bounded-delete")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        monkeypatch.setattr(service_module, "MAX_ORPHAN_DELETE_PER_TICK", 2)
        monkeypatch.setattr(service_module, "MAX_ORPHAN_SCAN_PER_TICK", 100)
        ids = [f"{i:032x}" for i in range(5)]
        for aid in ids:
            _old_orphan_committed(state, aid, now)

        state.service._recover_orphaned_spool()

        remaining = [aid for aid in ids if _spool_path(state, aid).exists()]
        assert len(remaining) == 3  # exactly 2 deleted, 3 left for later ticks
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_bounds_scans_per_tick(tmp_path, monkeypatch):
    # At most MAX_ORPHAN_SCAN_PER_TICK entries are examined per tick, so a huge
    # spool directory cannot make one tick scan past the bound (even when every
    # entry is a deletable old orphan, the scan cap limits deletions).
    state, _, _ = _started_state(tmp_path, "orphan-bounded-scan")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        monkeypatch.setattr(service_module, "MAX_ORPHAN_SCAN_PER_TICK", 2)
        monkeypatch.setattr(service_module, "MAX_ORPHAN_DELETE_PER_TICK", 100)
        ids = [f"{i:032x}" for i in range(3)]
        for aid in ids:
            _old_orphan_committed(state, aid, now)

        state.service._recover_orphaned_spool()

        remaining = [aid for aid in ids if _spool_path(state, aid).exists()]
        assert len(remaining) >= 1  # at most 2 examined -> at most 2 deleted
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_deletion_failure_is_swallowed_and_logged_sanitized(tmp_path, monkeypatch, caplog):
    # A failed unlink must not raise (best-effort sweep) and the logged warning
    # must be sanitized - no filesystem path and no attachment identifier.
    state, _, _ = _started_state(tmp_path, "orphan-unlink-fail")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        aid = "f" * 32
        _old_orphan_committed(state, aid, now)
        spool_dir = _spool_dir(state)

        def _failing_unlink(path, *args, **kwargs):
            raise OSError("injected unlink failure")

        monkeypatch.setattr(os, "unlink", _failing_unlink)

        with caplog.at_level(logging.WARNING, logger="meshsrv.attachments.service"):
            state.service._recover_orphaned_spool()  # must not raise

        assert _spool_path(state, aid).exists()  # file is left for a later pass
        logged = caplog.text
        assert "could not remove an orphaned spool file" in logged
        assert str(spool_dir) not in logged  # no path leaked
        assert aid not in logged              # no identifier leaked
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_never_touches_non_conforming_or_non_file_entries(tmp_path, monkeypatch):
    # Path containment: only a name matching one of the two known conventions,
    # and only a regular file (never a directory or a symlink), is ever
    # deleted. A subdirectory, a stray non-conforming file, and a symlink
    # pointing outside the spool dir are all left untouched, while a conforming
    # old orphan beside them is still reclaimed.
    state, _, _ = _started_state(tmp_path, "orphan-containment")
    try:
        now = 1_700_000_000.0
        monkeypatch.setattr(state.service, "_now", lambda: now)
        spool_dir = _spool_dir(state)
        spool_dir.mkdir(parents=True, exist_ok=True)

        # A stray non-conforming file (not 32-hex, not .*.tmp) - never deleted.
        stray = spool_dir / "README.txt"
        stray.write_bytes(b"keep me")
        os.utime(stray, (now - 7200, now - 7200))

        # A subdirectory - never deleted (is_file() is False).
        subdir = spool_dir / "subdir"
        subdir.mkdir()

        # A symlink pointing outside the spool dir - never followed/deleted.
        outside = tmp_path / "outside-target"
        outside.write_bytes(b"outside")
        link = spool_dir / ("1" * 32)  # looks like a committed 32-hex name
        if hasattr(os, "symlink"):
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                link = None
        else:
            link = None

        # A conforming old orphan - the only entry that must be deleted.
        aid = "a" * 32
        _old_orphan_committed(state, aid, now)

        state.service._recover_orphaned_spool()

        assert stray.exists()
        assert subdir.is_dir()
        if link is not None:
            assert link.is_symlink()
        assert outside.exists()
        assert not _spool_path(state, aid).exists()
    finally:
        mca_runtime.reset_state_for_tests()


def test_recovery_skips_missing_spool_directory(tmp_path, monkeypatch):
    # The spool directory may not exist yet (no create has ever run) - recovery
    # must be a no-op, not a crash.
    state, _, _ = _started_state(tmp_path, "orphan-missing-dir")
    try:
        spool_dir = _spool_dir(state)
        # Ensure it does not exist (fresh workspace), then recover.
        if spool_dir.exists():
            import shutil

            shutil.rmtree(spool_dir)
        monkeypatch.setattr(state.service, "_now", lambda: 1_700_000_000.0)
        state.service._recover_orphaned_spool()  # must not raise
    finally:
        mca_runtime.reset_state_for_tests()
