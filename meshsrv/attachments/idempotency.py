"""meshsrv/attachments/idempotency.py

Idempotency for `POST /api/attachments` (internal-rest-api.md §3.5 and
§3.6; Execution Plan Step 1.6A.1). MIT-licensed Core code - imports
nothing outside the stdlib, never `meshtastic`, never Flask.

Two halves of one concern, both pure (no `conn`, no filesystem, no
network):

1.  **The canonical content hash (§3.5).** A `create` request is
    replayed with the same `client_request_id` and must be recognized as
    either the identical request (idempotent replay) or a genuinely
    different one (409 `idempotency_conflict`). "Identical" cannot be
    decided by `client_request_id` alone - a client that reuses an id
    with different file bytes or metadata must be told it conflicted, not
    silently handed the original attachment. The hash therefore folds in
    *every* semantic field that influences the resulting transfer, into a
    version-prefixed, boundary-unambiguous digest:

        canonical_hash = hex(
            SHA-256(
                "MCA-IDEMPOTENCY-v1\\0" ||
                file_sha256_ascii ||          # lowercase hex, staged bytes
                canonical_json_bytes          # UTF-8, sorted keys, no ws
            )
        )

    `file_sha256_ascii` is the lowercase-hex SHA-256 of the staged plaintext
    (computed on the request thread during staging, §7.2), so two uploads
    of the same logical file with different byte content hash differently
    even when every other field matches. `canonical_json_bytes` is the
    deterministic serialization of the eleven semantic fields listed in
    `build_canonical_json()` - the same set §3.5 enumerates, serialized
    with keys sorted lexicographically and no insignificant whitespace, so
    two logically-identical requests byte-for-byte serialize identically
    regardless of the order the caller happened to put fields in.

    The `"MCA-IDEMPOTENCY-v1\\0"` prefix is a domain-separation label plus a
    version: if the field set ever has to grow (a future ADR), bumping the
    version changes every hash rather than silently letting an old-format
    and a new-format hash of the same logical request collide or be
    compared across versions as if equivalent. The `\\0` terminator keeps
    `file_sha256_ascii` and `canonical_json_bytes` unambiguous at the
    boundary (the digest length is fixed, so `file_sha256_ascii` cannot be
    shifted into the JSON, but the terminator is cheap insurance and makes
    the layout explicit).

2.  **The atomic pending reservation (§3.6, in `PendingReservations`
    below).** See that class's own docstring.

The two new `attachments` columns this writes back into
(`client_request_id`, `canonical_hash`) and the partial unique index on
`(workspace_id, client_request_id)` are Migration 11
(`meshsrv.attachments.db.migrations`) - this module produces the values
that migration stores; it never touches SQLite itself.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import time
from typing import Dict, Mapping, Optional

# §3.5: the domain-separation/version label, terminated by a NUL so the
# variable-length inputs on either side can never be ambiguous at the
# boundary.
_HASH_VERSION_PREFIX = b"MCA-IDEMPOTENCY-v1\0"

# §2.8 / Migration 11: client_request_id is `[A-Za-z0-9_-]{1,64}`.
# Anchored with \Z (not $) so a trailing newline cannot sneak through.
_CLIENT_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

# The canonical_hash this module produces is exactly 64 lowercase hex
# characters (SHA-256 hex digest).
_CANONICAL_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


def validate_client_request_id(client_request_id: str) -> None:
    """Raises `ValueError` unless `client_request_id` matches
    `[A-Za-z0-9_-]{1,64}` (§2.8). A single source of truth so the
    request-thread validation, the DB-level value, and any test all agree
    on what a legal id is rather than each re-deriving the same regex."""
    if not isinstance(client_request_id, str) or _CLIENT_REQUEST_ID_RE.match(client_request_id) is None:
        raise ValueError(
            "client_request_id must match [A-Za-z0-9_-]{1,64}"
        )


def validate_canonical_hash(canonical_hash: str) -> None:
    """Raises `ValueError` unless `canonical_hash` is 64 lowercase hex
    characters - the exact shape `compute_canonical_hash()` produces, and
    the shape Migration 11 stores. Fail-closed on a non-string or a
    mismatch (e.g. an uppercase-hex value would never compare equal to a
    lowercase one anyway, so rejecting it here is honest, not pedantic)."""
    if not isinstance(canonical_hash, str) or _CANONICAL_HASH_RE.match(canonical_hash) is None:
        raise ValueError("canonical_hash must be 64 lowercase hex characters")


def build_canonical_json(
    *,
    source_address: str,
    adapter_id: str,
    connector_profile_id: str,
    route_type: str,
    route_id: str,
    provider_id: str,
    comment: Optional[str],
    hard_ttl_seconds: int,
    download_grace_seconds: int,
    source_name: str,
    mime_type: str,
) -> bytes:
    """The deterministic UTF-8 JSON serialization (§3.5) of the *complete*
    semantic field set a `create` depends on. Every field that can change
    what the resulting transfer does is included; any field omitted here
    is, by definition, not part of idempotency - so a change to it would
    be treated as the "same" request. Deliberately does **not** include the
    raw client filename or anything path- or token-shaped (no `saved_path`,
    no upload token) - only the sanitized safe name (`source_name`) and the
    magic-byte-sniffed `mime_type`, both resolved at staging (§3.5), never
    trusted from the client.

    Serialization is `sort_keys=True` (keys sorted lexicographically,
    recursively - the nested `recipient` object is single-key so this is
    trivial there, but the rule holds for any future nested object) and
    `separators=(",", ":")` (no insignificant whitespace), `ensure_ascii=
    False` (literal UTF-8, so a non-ASCII safe name serializes as the
    bytes, not a `\\uXXXX` escape). Two logically-identical requests
    therefore produce byte-identical output regardless of field ordering
    or the caller's JSON formatting choices."""
    payload = {
        "recipient": {"source_address": source_address},
        "adapter_id": adapter_id,
        "connector_profile_id": connector_profile_id,
        "route_type": route_type,
        "route_id": route_id,
        "provider_id": provider_id,
        "comment": comment,
        "hard_ttl_seconds": hard_ttl_seconds,
        "download_grace_seconds": download_grace_seconds,
        "source_name": source_name,
        "mime_type": mime_type,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_canonical_hash(file_sha256_ascii: str, canonical_json_bytes: bytes) -> str:
    """§3.5: the lowercase-hex SHA-256 of `"MCA-IDEMPOTENCY-v1\\0" ||
    file_sha256_ascii || canonical_json_bytes`. Pure - no I/O, no state.

    `file_sha256_ascii` is expected to already be the lowercase-hex
    SHA-256 of the staged plaintext (64 chars); it is not re-validated
    here because this function is the inner loop of the idempotency
    computation and the caller (staging, §7.2) has already produced it,
    but a non-64-hex value simply yields a different digest rather than
    an error - idempotency degrades to "always a fresh id", never to a
    wrong match."""
    material = _HASH_VERSION_PREFIX + file_sha256_ascii.encode("ascii") + canonical_json_bytes
    return hashlib.sha256(material).hexdigest()


@dataclasses.dataclass(frozen=True)
class PendingReservation:
    """One §3.6 in-flight reservation: the three ids minted on the request
    thread *before* enqueue, keyed by `client_request_id` in
    `PendingReservations`. Frozen/immutable by construction - once a
    reservation is recorded it is never mutated in place, only replaced or
    removed, so a reader that grabbed the reference under the lock never
    sees a half-written record."""

    canonical_hash: str
    attachment_id: str
    command_id: str


@dataclasses.dataclass(frozen=True)
class IdempotencyEntry:
    """One committed idempotency index row (§3.5/§3.6): the immutable
    `client_request_id -> {attachment_id, canonical_hash, created_at}` entry
    the worker rebuilds from the `attachments` table at startup and
    republishes in the snapshot. The canonical home for this type is *here*
    (the idempotency concern), not `snapshots.py` - that module only
    projects it, it does not define it - so `PendingReservations.reserve()`
    can consume a committed index as a `Mapping[str, IdempotencyEntry]`
    without `idempotency.py` having to import the snapshot module (which
    would drag `sqlite3`/`workspace` into this otherwise stdlib-only
    module's import graph)."""

    attachment_id: str
    canonical_hash: str
    created_at: float


class PendingReservations:
    """The §3.6 transient reservation map - the in-memory half of
    idempotency that closes the "two concurrent Flask threads both see
    `client_request_id` absent and both enqueue a create" race. The
    committed half (rows already in `attachments`) lives in the worker-
    published idempotency-index snapshot, not here; callers consult both.

    Thread-safety contract (deliberate, not incidental): every mutation
    and every lookup is guarded by a *dedicated, short-lived* lock held
    only for the duration of the dict operation - **not** the tick lock,
    **not** a SQLite connection (this class owns neither and touches
    neither). A request thread holding this lock for a microsecond-long
    dict get/set can never be serialized behind a network-bound tick the
    way a tick-lock-sharing design would be.

    Restart-amnesic (§3.6): this map is in-memory only and discarded on
    restart; the committed idempotency index is rebuilt by the worker from
    the `attachments` table at startup. A reservation still pending at
    crash is simply gone, which is safe - the unique index on
    `(workspace_id, client_request_id)` (Migration 11) is the database-level
    backstop that rejects any duplicate that slips past this map, so the
    worst case of a lost reservation is a fresh create, never a double."""

    def __init__(self, now_fn=time.time):
        self._lock = threading.Lock()
        self._pending: Dict[str, PendingReservation] = {}
        self._now = now_fn

    def get(self, client_request_id: str) -> Optional[PendingReservation]:
        """Returns the reservation for `client_request_id`, or `None` if
        none is pending. Callers that need to atomically test-and-insert
        use `reserve()` instead; this is the read-only lookup for a
        committed-index-miss follow-up."""
        with self._lock:
            return self._pending.get(client_request_id)

    def reserve(
        self,
        client_request_id: str,
        reservation: PendingReservation,
        *,
        committed_entries: Mapping[str, "IdempotencyEntry"],
    ) -> "ReservationOutcome":
        """The §3.6 step-3 decision, made atomically under this map's lock
        against *both* the pending map and the committed index
        (`committed_entries`: `client_request_id -> IdempotencyEntry`, read
        from the worker-published idempotency snapshot by the caller).

        Consuming the full immutable `IdempotencyEntry` mapping - not just a
        `client_request_id -> canonical_hash` string map - is what lets a
        `replay_committed` outcome hand back the *original* `attachment_id`
        (the thing the caller must return to the client as the already-created
        attachment), not merely the matching hash. The entry is immutable
        (`IdempotencyEntry` is frozen), so reading it under this map's lock
        carries no aliasing risk.

        Returns a `ReservationOutcome` telling the caller exactly which of
        the four §3.5 cases applies, without releasing the lock between
        the check and the insert - so two concurrent callers can never
        both observe "absent" and both insert."""
        with self._lock:
            existing = self._pending.get(client_request_id)
            if existing is not None:
                if existing.canonical_hash == reservation.canonical_hash:
                    return ReservationOutcome.replay_pending(existing)
                return ReservationOutcome.conflict(existing.canonical_hash)
            committed = committed_entries.get(client_request_id)
            if committed is not None:
                if committed.canonical_hash == reservation.canonical_hash:
                    return ReservationOutcome.replay_committed(committed)
                return ReservationOutcome.conflict(committed.canonical_hash)
            self._pending[client_request_id] = reservation
            return ReservationOutcome.reserved(reservation)

    def remove(self, client_request_id: str) -> None:
        """Drops a pending reservation. Called on a `queue.Full` (both the
        reservation and the CommandRegistry entry are removed, §3.6 step 5)
        and by the worker when it commits or fails the create."""
        with self._lock:
            self._pending.pop(client_request_id, None)

    def snapshot_ids(self) -> Dict[str, PendingReservation]:
        """A copy of the pending map, for tests/inspection. Not part of
        the hot path - the facade never needs to read the whole map, only
        individual ids."""
        with self._lock:
            return dict(self._pending)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


@dataclasses.dataclass(frozen=True)
class ReservationOutcome:
    """The result of `PendingReservations.reserve()` - one of the four
    §3.5 cases, encoded so the facade can translate it into the correct
    HTTP response without re-reading either map (which would reintroduce a
    check-then-act race the reservation lock was meant to close)."""

    # "fresh" | "replay_pending" | "replay_committed" | "conflict"
    kind: str
    # The reservation that won, for replay_pending / fresh; else the
    # reservation the caller was *attempting* (so a conflict can carry its
    # ids harmlessly, though they are never used).
    reservation: Optional[PendingReservation] = None
    # The canonical_hash already on file when this conflicted (for a
    # diagnostic-only comparison; never an error message with secrets).
    existing_hash: Optional[str] = None
    # For replay_committed: the full immutable committed entry, so the
    # caller can return the *original* `attachment_id` (and its
    # `canonical_hash`) rather than having to re-derive them. `None` for
    # every other outcome.
    committed_entry: Optional["IdempotencyEntry"] = None

    @classmethod
    def reserved(cls, reservation: PendingReservation) -> "ReservationOutcome":
        return cls(kind="fresh", reservation=reservation)

    @classmethod
    def replay_pending(cls, reservation: PendingReservation) -> "ReservationOutcome":
        return cls(kind="replay_pending", reservation=reservation)

    @classmethod
    def replay_committed(cls, entry: "IdempotencyEntry") -> "ReservationOutcome":
        return cls(
            kind="replay_committed",
            existing_hash=entry.canonical_hash,
            committed_entry=entry,
        )

    @classmethod
    def conflict(cls, existing_hash: str) -> "ReservationOutcome":
        return cls(kind="conflict", existing_hash=existing_hash)
