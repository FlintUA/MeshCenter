"""meshsrv/attachments/facade.py

The request-facing attachments facade (internal-rest-api.md §3.2; Execution
Plan Step 1.6A.1). MIT-licensed Core code - stdlib only, never
`meshtastic`, never Flask.

Step 1.6A.1 is the plumbing pass, and this facade is its one request-
thread touchpoint. It does **not** own the SQLite connection, the tick
lock, the filesystem, or the network - it only ever reaches five in-memory
worker-owned components handed to it at construction:

- `CommandQueue` / `CommandRegistry` - the bounded mutation queue and the
  command-result store (the §3.4 sync/async split);
- `PendingReservations` / `ProbeRegistry` - the two other request-thread-
  readable in-memory stores (§3.6 idempotency, §7.11 provider probes);
- `AttachmentsSnapshotPublisher` - the worker-published immutable read
  projection (§3.3).

That is the whole point of the §3.1 single-owner threading model, made
mechanical rather than a convention the request handlers have to remember:
every read method here (`attachments_snapshot()`, `get_attachment()`,
`committed_idempotency()`, `get_command()`, `get_probe()`) resolves to one
of those five in-memory components' own lock-guarded reads, and the one
write method (`submit()`) resolves to `CommandQueue.put_nowait()` plus a
`CommandRegistry.register()`/`discard_queued()` rollback. None of them can
reach `conn`, so a Flask request thread calling them is SQLite-free,
filesystem-free, network-free, and tick-lock-free *by construction* - the
property the Step 1.6A.1 runtime tests assert explicitly (see
tests/test_mca_runtime.py's request-thread tests).

The facade never lazy-creates anything, and it never creates the SQLite
runtime: it is constructed once by `mca_runtime._MCARuntimeState` alongside
the five components it wraps, and `mca_runtime.get_attachments_facade()`
returns `None` until that construction has run (an explicit "not ready"
signal a request handler maps to 503/404, never a fallback that would
initialize the database from a request thread - §3.2).
"""

from __future__ import annotations

import threading
from typing import Mapping, Optional

from meshsrv.attachments.command_registry import CommandRegistry, CommandResult
from meshsrv.attachments.commands import Command, CommandQueue, CommandQueueFull
from meshsrv.attachments.idempotency import IdempotencyEntry, PendingReservations
from meshsrv.attachments.probe_registry import ProbeRecord, ProbeRegistry
from meshsrv.attachments.snapshots import AttachmentRecord, AttachmentsSnapshot, AttachmentsSnapshotPublisher


class AttachmentsFacade:
    """The request-thread surface over the five in-memory worker-owned
    components (module docstring). Read methods are lock-guarded single
    reads of an in-memory store; `submit()` is the §3.4 enqueue with the
    §3.6 step-5 rollback. Holds no `conn`, no tick lock, no filesystem/network
    handle - so it is safe to call from any Flask request thread."""

    def __init__(
        self,
        *,
        command_queue: CommandQueue,
        command_registry: CommandRegistry,
        pending_reservations: PendingReservations,
        probe_registry: ProbeRegistry,
        snapshot_publisher: AttachmentsSnapshotPublisher,
        wake_event: threading.Event,
    ):
        self._command_queue = command_queue
        self._command_registry = command_registry
        self._pending_reservations = pending_reservations
        self._probe_registry = probe_registry
        self._snapshot_publisher = snapshot_publisher
        self._wake_event = wake_event

    # ---- request-thread reads (SQLite-free, tick-lock-free) -------------

    def attachments_snapshot(self) -> Optional[AttachmentsSnapshot]:
        """The last worker-published immutable snapshot, or `None` before
        the worker's first successful publish. A single reference read under
        the publisher's own short lock - never `conn` (§3.3)."""
        return self._snapshot_publisher.snapshot()

    def get_attachment(self, attachment_id: str) -> Optional[AttachmentRecord]:
        """The immutable projection for one attachment, or `None` if it is
        not in the published snapshot. Reads `snapshot().by_id`, so it never
        touches `conn` - it reflects the last publish, not a live row."""
        snapshot = self._snapshot_publisher.snapshot()
        if snapshot is None:
            return None
        return snapshot.by_id.get(attachment_id)

    def committed_idempotency(self) -> Mapping[str, IdempotencyEntry]:
        """The committed `client_request_id -> IdempotencyEntry` index from
        the last published snapshot (§3.5/§3.6), or an empty mapping before
        the first publish. Read-only (a `MappingProxyType`), so the caller
        cannot mutate the published index."""
        snapshot = self._snapshot_publisher.snapshot()
        if snapshot is None:
            return {}
        return snapshot.idempotency

    def get_command(self, command_id: str) -> Optional[CommandResult]:
        """The current `CommandResult` for a command id (§3.4/§7.9), or
        `None` if unknown/evicted. Lock-guarded read of the registry, never
        `conn`."""
        return self._command_registry.get(command_id)

    def get_probe(self, probe_id: str) -> Optional[ProbeRecord]:
        """The current `ProbeRecord` for a probe id (§7.11), or `None` if
        unknown/expired/consumed. Non-consuming, lock-guarded read."""
        return self._probe_registry.get(probe_id)

    # ---- request-thread write: submit a mutation -------------------------

    def submit(self, command: Command) -> str:
        """The §3.4 enqueue: record `queued` in the registry *before* the
        queue write (so the worker can never dequeue a command whose entry
        does not exist, and an immediate `GET` sees `queued`), enqueue
        without blocking, and wake the worker. Returns `command.command_id`
        for the caller to hand back as the §7.9 `202` payload.

        On a full queue: rolls back the registry entry (`discard_queued`,
        §3.6 step 5 - the reservation rollback is the *create* path's own
        concern, not this generic submit's) and re-raises `CommandQueueFull`
        for the caller to map to `429 command_queue_full`. Never blocks, and
        never touches `conn`/filesystem/network/tick lock."""
        self._command_registry.register(command)
        try:
            self._command_queue.put_nowait(command)
        except CommandQueueFull:
            self._command_registry.discard_queued(command.command_id)
            raise
        self._wake_event.set()
        return command.command_id
