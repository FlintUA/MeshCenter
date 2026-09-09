"""meshsrv/attachments/facade.py

The request-facing attachments facade (internal-rest-api.md §3.2; Execution
Plan Step 1.6A.1). MIT-licensed Core code - stdlib only, never
`meshtastic`, never Flask.

Step 1.6A.1 is the plumbing pass, and this facade is its one request-
thread touchpoint. It does **not** own the SQLite connection, the tick
lock, the filesystem, or the network - it only ever reaches in-memory
worker-owned components handed to it at construction:

- `CommandQueue` / `CommandRegistry` - the bounded mutation queue and the
  command-result store (the §3.4 sync/async split);
- `PendingReservations` / `ProbeRegistry` - the two other request-thread-
  readable in-memory stores (§3.6 idempotency, §7.11 provider probes);
- `AttachmentsSnapshotPublisher` - the worker-published immutable read
  projection (§3.3);
- `ConnectivityMonitor` - the worker-published connectivity/provider read
  projection (§3.2; `connectivity_snapshot()`, `provider_snapshot()`,
  `evaluate_upload_readiness()`);
- `MCAPrincipal` - the immutable identity record (`identity_snapshot()`);
- a shared `ready_event` - the runtime-readiness signal (§3.2).

That is the whole point of the §3.1 single-owner threading model, made
mechanical rather than a convention the request handlers have to remember:
every read method here resolves to one of those in-memory components' own
lock-guarded reads, and the one write method (`submit()`) resolves to
`CommandQueue.put_nowait()` plus a `CommandRegistry.register()`/
`discard_queued()` rollback. None of them can reach `conn`, so a Flask
request thread calling them is SQLite-free, filesystem-free, network-free,
and tick-lock-free *by construction* - the property the Step 1.6A.1 runtime
tests assert explicitly (see tests/test_mca_runtime.py's request-thread
tests).

The facade never lazy-creates anything, and it never creates the SQLite
runtime: it is constructed once by `mca_runtime._MCARuntimeState` alongside
the components it wraps, and `mca_runtime.get_attachments_facade()`
returns `None` until that construction has run (an explicit "not ready"
signal a request handler maps to 503/404, never a fallback that would
initialize the database from a request thread - §3.2).

**Readiness (Step 1.6A.1, correction #1).** The facade must never treat an
uninitialized snapshot as an empty database. The snapshot-backed methods -
`attachments_snapshot()`, `get_attachment()`, `committed_idempotency()`,
and the write path `submit()` - raise `FacadeNotReady` (mapping to
`503 mca_not_ready`) until the shared `ready_event` is set, which the
worker does only once it has *both* started and published a first snapshot
successfully. Before that moment, `get_attachment()` raises rather than
returning a false "not found", `committed_idempotency()` raises rather than
returning an authoritative empty mapping, and `submit()` rejects without
registering or enqueueing anything. `get_command()`/`get_probe()` (pure
registry reads) and the connectivity/provider/identity reads are independent
of snapshot publication and are deliberately *not* gated.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Mapping, Optional

from meshsrv.attachments.command_registry import CommandRegistry, CommandResult
from meshsrv.attachments.commands import Command, CommandQueue, CommandQueueFull
from meshsrv.attachments.idempotency import (
    IdempotencyEntry,
    PendingReservation,
    PendingReservations,
    ReservationOutcome,
)
from meshsrv.attachments.identity import MCAPrincipal
from meshsrv.attachments.probe_registry import ProbeRecord, ProbeRegistry
from meshsrv.attachments.provider_registry import ProviderProfile
from meshsrv.attachments.recipient_snapshot import RecipientSnapshot, RecipientSnapshotPublisher
from meshsrv.attachments.snapshots import AttachmentRecord, AttachmentsSnapshot, AttachmentsSnapshotPublisher
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.connectivity_monitor import ConnectivityMonitor, ConnectivitySnapshot, UploadDecision


class FacadeNotReady(RuntimeError):
    """Raised by the facade's snapshot-backed reads (`attachments_snapshot()`,
    `get_attachment()`, `committed_idempotency()`) and its write path
    (`submit()`) when the attachments runtime is not ready - the service has
    not been started and published a first snapshot, or has since been
    stopped. A Step 1.6A REST caller maps this to 503 with
    `error_code = "mca_not_ready"`; the facade never itself fabricates an
    empty snapshot/idempotency mapping or a false "not found" from an
    unavailable snapshot (§3.2)."""

    error_code = "mca_not_ready"

    def __init__(self, message: str = "attachments runtime is not ready"):
        super().__init__(message)


class AttachmentsFacade:
    """The request-thread surface over the in-memory worker-owned components
    (module docstring). Read methods are lock-guarded single reads of an
    in-memory store; `submit()` is the §3.4 enqueue with the §3.6 step-5
    rollback. Holds no `conn`, no tick lock, no filesystem/network handle -
    so it is safe to call from any Flask request thread.

    Snapshot-backed methods are gated on `ready_event` (see the module
    docstring's readiness note); the connectivity/provider/identity/registry
    reads are not, because they resolve to components that exist - and are
    already safe to read - from the moment the facade is constructed."""

    def __init__(
        self,
        *,
        command_queue: CommandQueue,
        command_registry: CommandRegistry,
        pending_reservations: PendingReservations,
        probe_registry: ProbeRegistry,
        snapshot_publisher: AttachmentsSnapshotPublisher,
        wake_event: threading.Event,
        ready_event: threading.Event,
        connectivity_monitor: ConnectivityMonitor,
        principal: MCAPrincipal,
        workspace_manager: MCAWorkspaceManager,
        recipient_snapshot_publisher: RecipientSnapshotPublisher,
    ):
        self._command_queue = command_queue
        self._command_registry = command_registry
        self._pending_reservations = pending_reservations
        self._probe_registry = probe_registry
        self._snapshot_publisher = snapshot_publisher
        self._wake_event = wake_event
        self._ready_event = ready_event
        self._connectivity_monitor = connectivity_monitor
        self._principal = principal
        self._workspace_manager = workspace_manager
        self._recipient_snapshot_publisher = recipient_snapshot_publisher

    def _require_ready(self) -> None:
        """Gate the snapshot-backed read/write methods: raise `FacadeNotReady`
        until the worker has started and published a first snapshot (the
        shared `ready_event` is set by `AttachmentsService`). A request
        thread hitting this during startup/after stop gets a 503-mappable
        signal, never an empty snapshot or a false "not found"."""
        if not self._ready_event.is_set():
            raise FacadeNotReady()

    # ---- request-thread reads (SQLite-free, tick-lock-free) -------------

    def attachments_snapshot(self) -> AttachmentsSnapshot:
        """The last worker-published immutable snapshot. Raises
        `FacadeNotReady` before the first successful publish (never returns
        `None` as if an empty database existed). A single reference read under
        the publisher's own short lock - never `conn` (§3.3)."""
        self._require_ready()
        return self._snapshot_publisher.snapshot()

    def get_attachment(self, attachment_id: str) -> Optional[AttachmentRecord]:
        """The immutable projection for one attachment, or `None` if it is
        not in the published snapshot. Raises `FacadeNotReady` before the
        first publish, so "snapshot unavailable" is never conflated with
        "attachment not found". Reads `snapshot().by_id`, so it never touches
        `conn` - it reflects the last publish, not a live row."""
        self._require_ready()
        return self._snapshot_publisher.snapshot().by_id.get(attachment_id)

    def committed_idempotency(self) -> Mapping[str, IdempotencyEntry]:
        """The committed `client_request_id -> IdempotencyEntry` index from
        the last published snapshot (§3.5/§3.6). Raises `FacadeNotReady`
        before the first publish rather than returning an authoritative empty
        mapping. Read-only (a `MappingProxyType`), so the caller cannot
        mutate the published index."""
        self._require_ready()
        return self._snapshot_publisher.snapshot().idempotency

    def get_command(self, command_id: str) -> Optional[CommandResult]:
        """The current `CommandResult` for a command id (§3.4/§7.9), or
        `None` if unknown/evicted. Lock-guarded read of the registry, never
        `conn`. Not readiness-gated: the registry is populated from the
        moment of construction."""
        return self._command_registry.get(command_id)

    def get_probe(self, probe_id: str) -> Optional[ProbeRecord]:
        """The current `ProbeRecord` for a probe id (§7.11), or `None` if
        unknown/expired/consumed. Non-consuming, lock-guarded read. Not
        readiness-gated (same reason as `get_command()`)."""
        return self._probe_registry.get(probe_id)

    def connectivity_snapshot(self) -> ConnectivitySnapshot:
        """The worker-published connectivity snapshot (§3.2): the current
        `InternetStatus` plus the per-provider `RelayStatus` mapping. A pure
        in-memory read of the monitor's atomically-published state - no
        SQLite, filesystem, network, or tick lock. Not readiness-gated: the
        monitor publishes an `UNKNOWN`/empty snapshot from construction."""
        return self._connectivity_monitor.snapshot()

    def provider_snapshot(self) -> Mapping[str, ProviderProfile]:
        """The worker-published provider profile mapping (§3.2/§7.13), as a
        read-only view of the monitor's `_profile_snapshot`. No SQLite,
        filesystem, network, or tick lock. Not readiness-gated."""
        return self._connectivity_monitor.profile_snapshot()

    def recipient_snapshot(self) -> RecipientSnapshot:
        """The worker-published immutable TOFU recipient-binding snapshot
        (Finding 7) - the request thread's SQLite-free view of which transport
        addresses have a known, trusted binding. A pure in-memory read of the
        publisher's atomically-swapped snapshot, never `conn`. Not
        readiness-gated: the publisher publishes an (empty, fail-closed)
        snapshot from construction, so an unknown recipient is a 400
        `recipient_not_found`, never a `FacadeNotReady`, at any point."""
        return self._recipient_snapshot_publisher.snapshot()

    def evaluate_upload_readiness(
        self,
        provider_id: str,
        *,
        ciphertext_bytes: Optional[int] = None,
        requested_ttl_seconds: Optional[int] = None,
    ) -> UploadDecision:
        """The structured upload-readiness decision for one provider
        (`UploadDecision`, §3.2) - the facade-level equivalent of
        `AttachmentsService.evaluate_upload_readiness()`. A pure in-memory
        read of the monitor's published provider/relay view - no SQLite,
        filesystem, network, or tick lock. Not readiness-gated."""
        return self._connectivity_monitor.evaluate_upload_decision(
            provider_id,
            ciphertext_bytes=ciphertext_bytes,
            requested_ttl_seconds=requested_ttl_seconds,
        )

    def identity_snapshot(self) -> MCAPrincipal:
        """This workspace's one MCA principal (`MCAPrincipal`, §7.1), an
        immutable, public-info-only record (never private key material). Not
        readiness-gated: the principal is resolved at construction."""
        return self._principal

    # ---- request-thread write: submit a mutation -------------------------

    def submit(self, command: Command) -> str:
        """The §3.4 enqueue: record `queued` in the registry *before* the
        queue write (so the worker can never dequeue a command whose entry
        does not exist, and an immediate `GET` sees `queued`), enqueue
        without blocking, and wake the worker. Returns `command.command_id`
        for the caller to hand back as the §7.9 `202` payload.

        Raises `FacadeNotReady` before the runtime is ready - the command is
        *not* registered or enqueued, so nothing is left half-submitted for a
        client that retries after the service comes up.

        On a full queue: rolls back the registry entry (`discard_queued`,
        §3.6 step 5 - the reservation rollback is the *create* path's own
        concern, not this generic submit's) and re-raises `CommandQueueFull`
        for the caller to map to `429 command_queue_full`. Never blocks, and
        never touches `conn`/filesystem/network/tick lock."""
        self._require_ready()
        self._command_registry.register(command)
        try:
            self._command_queue.put_nowait(command)
        except CommandQueueFull:
            self._command_registry.discard_queued(command.command_id)
            raise
        self._wake_event.set()
        return command.command_id

    # ---- request-thread write: idempotent create (§3.5/§3.6) ---------------

    def spool_outgoing_dir(self) -> Path:
        """The workspace's outgoing spool directory path. This is a pure path
        computation (`workspace_manager.paths()`) - it performs no filesystem
        write or open; the create endpoint (§7.2) does the actual staging
        write under this directory, which is the one filesystem operation the
        request thread is permitted by design."""
        return self._workspace_manager.paths(self._principal.principal_id).spool_outgoing

    def submit_create(
        self,
        command: Command,
        *,
        client_request_id: str,
        reservation: PendingReservation,
    ) -> "ReservationOutcome":
        """The §3.6 idempotent-create enqueue, distinct from `submit()` only in
        that it performs the atomic reservation *before* registering/enqueueing
        (and rolls the reservation back alongside the registry entry on a full
        queue). Returns the `ReservationOutcome` so the caller can translate the
        four §3.5 cases (`fresh`/`replay_pending`/`replay_committed`/`conflict`)
        into the correct HTTP response.

        On `fresh` the command is registered (`queued`) and enqueued exactly as
        `submit()` does; on any non-fresh outcome nothing is registered/enqueued
        (the reservation map already decided the request is a replay or a
        conflict). Raises `FacadeNotReady` before the runtime is ready, and
        re-raises `CommandQueueFull` (after rolling back both the registry entry
        and the reservation, §3.6 step 5) on a full queue. Never blocks, and
        never touches `conn`/filesystem/network/tick lock."""
        self._require_ready()
        committed = self.committed_idempotency()
        outcome = self._pending_reservations.reserve(
            client_request_id, reservation, committed_entries=committed
        )
        if outcome.kind != "fresh":
            return outcome
        self._command_registry.register(command)
        try:
            self._command_queue.put_nowait(command)
        except CommandQueueFull:
            self._command_registry.discard_queued(command.command_id)
            self._pending_reservations.remove(client_request_id)
            raise
        self._wake_event.set()
        return outcome
