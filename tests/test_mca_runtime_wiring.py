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

import threading

import pytest

from meshsrv.attachments import mca_runtime
from meshsrv.attachments.command_registry import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SUCCEEDED,
    CommandRegistry,
)
from meshsrv.attachments.commands import Command, CommandQueue
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.dispatch import COMMAND_EXECUTION_FAILED, CommandDispatcher, CommandOutcome
from meshsrv.attachments.facade import AttachmentsFacade, FacadeNotReady
from meshsrv.attachments.idempotency import PendingReservations
from meshsrv.attachments.probe_registry import ProbeRegistry
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
