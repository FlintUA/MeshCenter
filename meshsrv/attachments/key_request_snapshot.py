"""meshsrv/attachments/key_request_snapshot.py

PR 4 (shared target model): the worker-published, immutable key-request
capability projection. This is the backend half of the shared
Nodes/Channels/MCA-Contacts target model's "request key" capability, and
it mirrors `recipient_snapshot.py`'s `RecipientSnapshotPublisher` shape
verbatim (see that module's docstring for the threading reasoning this
module reuses unchanged).

Why this exists. The send dialog and the Files contact list must know, for
a node whose MCA key is still unknown (`trust_state == unknown`), whether
the "request key" action is currently offered - and, if a request is
already in flight, *why* it is disabled. That state is not a single row in
one table; it is two facts the worker alone can read safely:

  - a `contact_request_key` command that has been *accepted* into the
    bounded command queue but not yet drained by the worker (queued). This is
    tracked by a lock-guarded, in-memory pending-marker map keyed by command
    id owned by this publisher (`mark_queued()` / `mark_drained()`), not by
    iterating the
    queue's internal deque (that read raced a concurrent `put_nowait()` and
    could raise `deque mutated during iteration` - see commands.py's own
    `iter_commands()`), and not from `CommandRegistry` (the registry
    deliberately discards payloads, so per-address "queued" cannot be derived
    from it);
  - the persisted `mca_key_exchange_contact_state.last_request_sent_at`
    timestamp (a request was already sent) - which the §11 no-secret
    discipline forbids exposing directly.

The two facts are *not* merged into one map. The publisher keeps them in two
layers (PR #256 final correction, Finding 2):

  - `_published` is the **persisted base**: only states derived from
    persisted worker-owned facts - `waiting_response`, `retry_available`, and
    absence for `idle`. A live `queued` marker is **never** written into it,
    because a marker can be removed between a worker's read and its publish
    and a stale copy would leave the address falsely non-requestable until the
    next tick.
  - the **live `queued` overlay** is the lock-guarded, command-id-keyed
    pending map (`_pending_queued`). `snapshot()` copies it under the lock and
    overlays `queued` on top of the persisted base at read time, so a removed
    marker disappears from the very next read without waiting for a tick.

The folded result is one non-secret `KeyRequestState` per address
(`idle`/`queued`/`waiting_response`/`retry_available`) plus a derived
`can_request_key` boolean. A request thread reads only `snapshot()`; it never
touches `conn`, the filesystem, the network, or the tick lock.

No-secret discipline (§11), enforced by construction: the published
snapshot carries only the enum's public string values and a boolean. It
never projects `last_request_sent_at`, any other retry/throttle timestamp,
the rate-limit window, a raw public identity, X25519 material, a signing
key, a private-key path, a DB row, or exception text. The
`retry_available`/`waiting_response` split is the *only* place a timestamp
is consulted, and it is consumed entirely to pick a state string - the raw
value never leaves the worker.

State meanings (a single, closed vocabulary - see the enum):

  - `idle` - no request queued and none ever sent. This is the *default*
    for an address absent from the snapshot; the publisher only lists an
    address once it has real activity (queued or sent), so `idle` never
    appears in the map and the request thread/frontend default to it.
  - `queued` - a `contact_request_key` command for the address has been
    accepted into the command queue and not yet *terminally* executed. A
    request is about to go out, so the action must be disabled. Observable
    immediately after `AttachmentsFacade.submit()` returns, before any worker
    tick, via the live overlay `snapshot()` applies (see `mark_queued()`). It
    is a **live overlay only**: it is never stored in `_published`, and it
    disappears from the next `snapshot()` the moment the marker is removed.
  - `waiting_response` - a request was already sent and the per-address
    rate-limit window (`MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS`)
    has not yet elapsed; we are waiting for the contact's KEY_ANNOUNCE.
  - `retry_available` - a request was sent but the window has elapsed with
    no key arriving; a fresh request is permitted again.

`can_request_key` in the snapshot is the worker's own answer for an address
that *has* activity: `True` only when the address has no TOFU binding (its
key is still unknown) and its state is `retry_available`. It is a
convenience for the API/tests and must agree with the frontend's
centralized capability matrix; the frontend's own derivation (which also
covers the `idle` default and every non-`unknown` trust state) remains the
single place the *full* matrix is computed.
"""

from __future__ import annotations

import dataclasses
import enum
import threading
import time
import types
from typing import Dict, Mapping, Set

from meshsrv.attachments.key_exchange import (
    MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS,
    KeyExchangeCoordinator,
)


class KeyRequestState(str, enum.Enum):
    """The one public vocabulary for the outgoing key-request state of a
    single transport address. Members double as the wire strings the REST
    layer returns, so the mapping from state to HTTP body is one `.value`,
    not a re-derivation at every call site. See the module docstring for the
    exact meaning of each member."""

    IDLE = "idle"
    QUEUED = "queued"
    WAITING_RESPONSE = "waiting_response"
    RETRY_AVAILABLE = "retry_available"


@dataclasses.dataclass(frozen=True)
class KeyRequestCapability:
    """The immutable request-thread-readable projection for one address that
    has key-request activity. Carries only the non-secret state string and a
    boolean - never a timestamp (see module docstring)."""

    key_request_state: KeyRequestState
    can_request_key: bool


@dataclasses.dataclass(frozen=True)
class KeyRequestSnapshot:
    """The whole immutable key-request snapshot: `by_address` maps transport
    address -> `KeyRequestCapability`. Frozen; `by_address` is a read-only
    proxy so a reader can neither mutate the mapping nor observe later
    in-place changes of the worker's live dict. Addresses with `idle` state
    are simply absent (the caller defaults to `idle`)."""

    by_address: Mapping[str, KeyRequestCapability]

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_address", types.MappingProxyType(dict(self.by_address)))


class KeyRequestStatePublisher:
    """Owns the single atomically-published `KeyRequestSnapshot`, mirroring
    `RecipientSnapshotPublisher` (and, transitively, `ConnectivityMonitor`'s
    `_profile_snapshot`).

    - `refresh()` is the *only* place that reads the persisted
      `last_request_sent_at` table and bindings (worker/startup-thread reads
      only). It builds the persisted **base** snapshot - never consulting the
      pending queued-marker map - and swaps it in with one reference assignment,
      never mutating the previously-published one.
    - `snapshot()` is the request-thread read: a single lock-guarded read of
      `_published`, with the *current* pending addresses overlaid as `queued`
      on top (the live overlay - see `mark_queued()`), never `conn`. The
      overlay lives only here, so a removed marker vanishes from the next read.

    Construction performs an eager `refresh()` so the snapshot is populated
    from the moment the object exists, not only after the first tick - the
    same reason `RecipientSnapshotPublisher` builds its snapshot in its own
    `__init__` (construction happens on the startup thread, so that eager
    SQLite read does not violate the single-owner model)."""

    def __init__(
        self,
        coordinator: KeyExchangeCoordinator,
        *,
        now_fn=time.time,
        min_seconds_between_key_requests: int = MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS,
    ):
        self._coordinator = coordinator
        self._now = now_fn
        self._min_interval = min_seconds_between_key_requests
        self._lock = threading.Lock()
        # The pending queued-marker map, keyed by `command_id -> contact_id`:
        # one entry per `contact_request_key` command whose request is still in
        # flight (accepted into the command queue and not yet *terminally*
        # executed by the worker). Keyed by command id rather than by address so
        # two distinct commands for the SAME address are two entries - draining
        # one must not clear the other's still-pending `queued` state (PR #256
        # review, Finding 2 / Blocker 2). Written by `mark_queued()` (request
        # thread, via `AttachmentsFacade.submit()`, *before* the queue write) and
        # `mark_drained()` (worker thread in `_drain_commands()`'s `finally`,
        # plus the facade's own enqueue-failure rollback), read only by
        # `snapshot()` (which overlays it as a live `queued` view - never by
        # `refresh()`, which publishes only the persisted base). In-memory only,
        # so a restart never fabricates a `queued` state for a command the new
        # process never accepted.
        self._pending_queued: Dict[str, str] = {}
        # `_published` is the persisted base only: waiting_response /
        # retry_available / absence-for-idle. It NEVER carries a live `queued`
        # marker (see `refresh()`); `queued` is applied by `snapshot()` alone.
        self._published: KeyRequestSnapshot = KeyRequestSnapshot(by_address={})
        self.refresh()

    def mark_queued(self, command_id: str, contact_id: str) -> None:
        """Request-thread write (called by `AttachmentsFacade.submit()` *before*
        it enqueues the command - see that method's docstring for why the marker
        is registered ahead of the queue write): record that this command id's
        `contact_request_key` request is accepted-but-not-yet-terminally-executed,
        so its address projects `queued` immediately - before any worker tick -
        via `snapshot()`. Thread-safe (lock-guarded), never touches SQLite/
        `conn`/the filesystem/network.

        Keyed by `command_id`, so two same-address commands are two entries and
        a same-address drain keeps the address `queued` until the last of its
        commands drains. A full queue raises `CommandQueueFull` after this call,
        but the facade rolls the marker back (`mark_drained(command_id)`) on that
        path, so `queue_full` never leaves a false `queued`."""
        with self._lock:
            self._pending_queued[command_id] = contact_id

    def mark_drained(self, command_id: str) -> None:
        """Remove the pending marker for one `command_id`. Called from two
        threads, both removing-by-command-id so a same-address command's sibling
        marker is never dropped:

        - the worker thread, in `AttachmentsService._drain_commands()`'s
          `finally`, the moment the command reaches a *terminal* execution
          outcome - the marker is held through `_execute_command()` so the
          address stays `queued` (non-requestable) while the request is actually
          being sent, then cleared so its state derives from the persisted
          `last_request_sent_at` timestamp on the next `refresh()`;
        - the request thread, in `AttachmentsFacade.submit()`'s enqueue-failure
          rollback, undoing the `mark_queued()` it just made when the queue was
          full (so `queue_full` leaves no marker).

        Idempotent (a missing id is a no-op), so it is safe for both a
        successful and a failed execution - a failed send must never leave a
        false `queued` marker behind. Thread-safe, in-memory."""
        with self._lock:
            self._pending_queued.pop(command_id, None)

    def refresh(self) -> KeyRequestSnapshot:
        """Worker/startup thread only: rebuild the *persisted base* snapshot
        from the persisted per-address request timestamps and bindings, then
        swap it in atomically. The live `queued` overlay is deliberately NOT
        folded into `_published` here (see the module docstring / PR #256 final
        correction, Finding 2): a marker can be removed between the read and the
        publish, and a stale copy published as `queued` would leave the address
        falsely non-requestable until the next tick. `_published` therefore
        holds only states derived from persisted worker-owned facts
        (`waiting_response` / `retry_available` / absence-for-`idle`); `queued`
        is applied only by `snapshot()` from the *current* pending map.

        Returns the public current view - the freshly published base plus the
        live `queued` overlay (i.e. `self.snapshot()`), so a caller relying on
        the return value sees `queued` for any in-flight request.

        Never raises - a transient read failure must not kill the tick; the
        request thread simply keeps the last-known-good persisted base (plus the
        live `queued` overlay, which is derived from the lock-guarded
        pending-marker map and cannot fail). The only failure mode is a SQLite
        read of the persisted timestamps/bindings - and that keeps
        last-known-good rather than silently re-permitting or re-queuing an
        address."""
        try:
            timestamps: Dict[str, float] = self._coordinator.list_key_request_sent_at()
            bindings: Set[str] = {
                b.transport_address for b in self._coordinator.list_bindings()
            }
        except Exception:  # noqa: BLE001 - a SQLite read failure keeps last-known-good
            return self.snapshot()

        now = self._now()
        by_address: Dict[str, KeyRequestCapability] = {}
        for address in sorted(timestamps):
            if now - timestamps[address] < self._min_interval:
                state = KeyRequestState.WAITING_RESPONSE
                can_request = False
            else:
                state = KeyRequestState.RETRY_AVAILABLE
                can_request = address not in bindings
            by_address[address] = KeyRequestCapability(
                key_request_state=state, can_request_key=can_request
            )
        base = KeyRequestSnapshot(by_address=by_address)
        with self._lock:
            self._published = base
        return self.snapshot()

    def snapshot(self) -> KeyRequestSnapshot:
        """The request-thread read: the last published persisted base, with the
        *current* pending `queued` addresses overlaid on top (so `queued` is
        observable immediately after `submit()` returns, before the worker's
        next tick, and disappears the moment a marker is removed - no stale
        `queued` survives in `_published`). A single lock-guarded read - never
        `conn`, the filesystem, the network, or the tick lock. Never `None`:
        construction published an (empty) snapshot before any reader could
        observe the object."""
        with self._lock:
            published = self._published
            # Copy the pending addresses under the lock: the map is mutated by
            # the worker thread (`mark_drained()`) between this read and the
            # overlay loop below, and iterating a dict that is being mutated
            # from another thread raises RuntimeError ("dictionary changed size
            # during iteration"). The copy is also what makes the overlay a
            # consistent point-in-time view rather than a torn mix.
            pending_addresses = set(self._pending_queued.values())
        if not pending_addresses:
            return published
        # The live `queued` overlay: a `queued` address wins over any
        # still-visible `last_request_sent_at` entry in `published` (a fresh
        # request is in flight, superseding the prior one's rate-limit window).
        by_address = dict(published.by_address)
        for address in pending_addresses:
            by_address[address] = KeyRequestCapability(
                key_request_state=KeyRequestState.QUEUED,
                can_request_key=False,
            )
        return KeyRequestSnapshot(by_address=by_address)
