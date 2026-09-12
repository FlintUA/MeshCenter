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

  - a `contact_request_key` command still sitting in the bounded command
    queue (queued, not yet drained by the worker) - see
    `commands.CommandQueue.iter_commands()`, the one read that can see a
    command's *payload* (the registry deliberately discards payloads, so
    per-address "queued" cannot be derived from `CommandRegistry`);
  - the persisted `mca_key_exchange_contact_state.last_request_sent_at`
    timestamp (a request was already sent) - which the §11 no-secret
    discipline forbids exposing directly.

The publisher folds those two facts into one non-secret `KeyRequestState`
per address (`idle`/`queued`/`waiting_response`/`retry_available`), plus a
derived `can_request_key` boolean, and swaps the whole mapping in with one
reference assignment - atomic under the GIL. A request thread reads only
`snapshot()`; it never touches `conn`, the filesystem, the network, or the
tick lock.

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
  - `queued` - a `contact_request_key` command for the address is still in
    the command queue (not yet drained). A request is about to go out, so
    the action must be disabled.
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

from meshsrv.attachments.commands import CommandQueue
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

    - `refresh()` is the *only* place that reads the command queue and the
      persisted `last_request_sent_at` table (worker/startup-thread reads
      only). It builds a whole new snapshot and swaps it in with one
      reference assignment - never mutating the previously-published one.
    - `snapshot()` is the request-thread read: a single reference read under
      a short lock, never `conn`.

    Construction performs an eager `refresh()` so the snapshot is populated
    from the moment the object exists, not only after the first tick - the
    same reason `RecipientSnapshotPublisher` builds its snapshot in its own
    `__init__` (construction happens on the startup thread, so that eager
    SQLite read does not violate the single-owner model)."""

    def __init__(
        self,
        coordinator: KeyExchangeCoordinator,
        command_queue: CommandQueue,
        *,
        now_fn=time.time,
        min_seconds_between_key_requests: int = MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS,
    ):
        self._coordinator = coordinator
        self._command_queue = command_queue
        self._now = now_fn
        self._min_interval = min_seconds_between_key_requests
        self._lock = threading.Lock()
        self._published: KeyRequestSnapshot = KeyRequestSnapshot(by_address={})
        self.refresh()

    def refresh(self) -> KeyRequestSnapshot:
        """Worker/startup thread only: rebuild the snapshot from the queued
        `contact_request_key` commands and the persisted per-address request
        timestamps, then swap it in atomically. Returns the fresh snapshot.

        Never raises - a transient read failure must not kill the tick; the
        request thread simply keeps the last-known-good snapshot."""
        queued: Set[str] = set()
        try:
            for command in self._command_queue.iter_commands():
                if command.kind != "contact_request_key":
                    continue
                contact_id = command.payload.get("contact_id")
                if isinstance(contact_id, str) and contact_id:
                    queued.add(contact_id)
        except Exception:  # noqa: BLE001 - a queue read failure keeps last-known-good
            queued = set()

        try:
            timestamps: Dict[str, float] = self._coordinator.list_key_request_sent_at()
            bindings: Set[str] = {
                b.transport_address for b in self._coordinator.list_bindings()
            }
        except Exception:  # noqa: BLE001 - a SQLite read failure keeps last-known-good
            return self.snapshot()

        now = self._now()
        by_address: Dict[str, KeyRequestCapability] = {}
        for address in sorted(queued | set(timestamps)):
            if address in queued:
                state = KeyRequestState.QUEUED
                can_request = False
            elif now - timestamps[address] < self._min_interval:
                state = KeyRequestState.WAITING_RESPONSE
                can_request = False
            else:
                state = KeyRequestState.RETRY_AVAILABLE
                can_request = address not in bindings
            by_address[address] = KeyRequestCapability(
                key_request_state=state, can_request_key=can_request
            )
        fresh = KeyRequestSnapshot(by_address=by_address)
        with self._lock:
            self._published = fresh
        return fresh

    def snapshot(self) -> KeyRequestSnapshot:
        """The request-thread read: the last published immutable snapshot (a
        single reference read under a short lock - never `conn`). Never
        `None`: construction published an (empty) snapshot before any reader
        could observe the object."""
        with self._lock:
            return self._published
