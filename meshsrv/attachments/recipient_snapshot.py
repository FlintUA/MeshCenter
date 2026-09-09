"""meshsrv/attachments/recipient_snapshot.py

Finding 7 (Step 1.6A.3B review): the worker-published, immutable TOFU
recipient-binding snapshot, and the pure trust decision a request thread
makes against it.

Why this module exists. `POST /api/attachments` must reject an unknown or
not-yet-trusted recipient *synchronously* (a 400 the client can act on
before it has finished staging a whole multipart upload), but the only
record of which recipients exist and are trusted lives in the
`mca_recipient_bindings` table - owned exclusively by the worker thread
(§3.1 single-owner SQLite model, see key_exchange.py). The request thread
must not read `conn`. The fix is the same pattern `ConnectivityMonitor`
already uses for provider profiles (`_profile_snapshot`):

- **the worker** (and, for one eager call at construction, the startup
  thread) reads every binding via `KeyExchangeCoordinator.list_bindings()`
  and swaps in a whole new immutable `RecipientSnapshot` with one reference
  assignment - atomic under the GIL;
- **the request thread** reads only the published snapshot and asks
  `evaluate_recipient_trust()` - never `conn`, never the filesystem, never
  the tick lock.

The request-thread check is deliberately **not** the authority: the worker
re-validates the live binding inside `AttachmentsService._command_create()`
(`key_exchange.get_binding(...)` -> `status != MCA_READY` -> FAILED) before
anything is committed. This module only gives the client fast, correct-until-
the-last-tick feedback; the worker's re-check is what actually enforces the
TOFU fail-closed rule at commit time. A binding whose trust changed between
the request thread's read and the worker's re-check (a newly-announced key
that has not yet been `confirm_tofu()`'d, a `KEY_CHANGED` pending) is caught
there, never silently accepted.

No-secret discipline (§11), enforced by construction: the published snapshot
carries only the *public identifiers* of a binding - `adapter_id`,
`transport_address`, `key_id`, and the derived `status`. The recipient's
`public_identity` bytes are never projected here: the worker resolves the
real key material from the live binding at commit time, never from anything
a browser supplied. A request thread can at most *name* a recipient address
it wants to send to; it cannot inject, read, or bypass the binding's key.
"""

from __future__ import annotations

import dataclasses
import enum
import threading
import types
from typing import Dict, List, Mapping, Optional

from meshsrv.attachments.key_exchange import AddressStatus, KeyExchangeCoordinator, RecipientBinding


class RecipientRejectionReason(str, enum.Enum):
    """The two reasons a recipient can be rejected synchronously - the enum
    members double as the `error_code` strings the REST layer returns, so the
    mapping from decision to HTTP body is one comparison, not a re-derivation
    at every call site."""

    RECIPIENT_NOT_FOUND = "recipient_not_found"
    RECIPIENT_NOT_TRUSTED = "recipient_not_trusted"


@dataclasses.dataclass(frozen=True)
class RecipientBindingSnapshot:
    """The immutable request-thread-readable projection of one TOFU binding.
    Public identifiers only - never `public_identity` (see module docstring).
    `key_id` is the binding's `sender_key_id` (the MCA key id a
    `sender.RecipientTarget` is built from)."""

    adapter_id: str
    transport_address: str
    key_id: str
    status: AddressStatus


@dataclasses.dataclass(frozen=True)
class RecipientSnapshot:
    """The whole immutable recipient snapshot: `by_address` maps transport
    address -> `RecipientBindingSnapshot`. Frozen; `by_address` is a read-only
    proxy so a reader can neither mutate the mapping nor observe later
    in-place changes of the worker's live dict."""

    by_address: Mapping[str, RecipientBindingSnapshot]

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_address", types.MappingProxyType(dict(self.by_address)))


def evaluate_recipient_trust(
    snapshot: RecipientSnapshot, source_address: str
) -> Optional[RecipientRejectionReason]:
    """The one place the synchronous recipient-trust decision is made. Pure -
    no `conn`, no filesystem, no tick lock. Returns `None` when the address is
    a known, `MCA_READY` binding; `RECIPIENT_NOT_FOUND` when there is no
    binding for it; `RECIPIENT_NOT_TRUSTED` when a binding exists but its
    status is not `MCA_READY` (`KEY_UNVERIFIED` - announced but never
    confirmed - or `KEY_CHANGED` - a pending key rotation awaiting an explicit
    accept). Fail-closed: anything other than an explicit, confirmed trust is
    a rejection, never a silent pass-through."""
    binding = snapshot.by_address.get(source_address)
    if binding is None:
        return RecipientRejectionReason.RECIPIENT_NOT_FOUND
    if binding.status is not AddressStatus.MCA_READY:
        return RecipientRejectionReason.RECIPIENT_NOT_TRUSTED
    return None


class RecipientSnapshotPublisher:
    """Owns the single atomically-published `RecipientSnapshot`, mirroring
    `ConnectivityMonitor._profile_snapshot` (see that class's own docstring
    for the threading reasoning this class reuses verbatim).

    - `refresh()` is the *only* place that reads the binding table (through
      `KeyExchangeCoordinator.list_bindings()`, a worker/startup-thread
      SQLite read). It builds a whole new snapshot and swaps it in with one
      reference assignment - never mutating the previously-published one.
    - `snapshot()` is the request-thread read: a single reference read,
      atomic under the GIL, never `conn`.

    Construction performs an eager `refresh()` so the snapshot is populated
    from the moment the object exists, not only after the first tick - the
    same reason `ConnectivityMonitor` builds `_profile_snapshot` in its own
    `__init__` (construction happens on the startup thread, see
    mca_runtime.py's `_MCARuntimeState.__init__`, so that eager SQLite read
    does not violate the single-owner model)."""

    def __init__(self, coordinator: KeyExchangeCoordinator, *, adapter_id: str):
        self._coordinator = coordinator
        self._adapter_id = adapter_id
        self._lock = threading.Lock()
        self._published: RecipientSnapshot = RecipientSnapshot(by_address={})
        self.refresh()

    def refresh(self) -> RecipientSnapshot:
        """Worker/startup thread only: rebuild the snapshot from the live
        binding table and swap it in atomically. Returns the fresh snapshot."""
        bindings: List[RecipientBinding] = self._coordinator.list_bindings()
        by_address: Dict[str, RecipientBindingSnapshot] = {}
        for binding in bindings:
            by_address[binding.transport_address] = RecipientBindingSnapshot(
                adapter_id=binding.adapter_id,
                transport_address=binding.transport_address,
                key_id=binding.sender_key_id,
                status=binding.status,
            )
        fresh = RecipientSnapshot(by_address=by_address)
        with self._lock:
            self._published = fresh
        return fresh

    def snapshot(self) -> RecipientSnapshot:
        """The request-thread read: the last published immutable snapshot (a
        single reference read under a short lock - never `conn`). Never
        `None`: construction published an (empty, fail-closed) snapshot before
        any reader could observe the object."""
        with self._lock:
            return self._published
