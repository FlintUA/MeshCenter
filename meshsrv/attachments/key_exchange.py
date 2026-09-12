"""meshsrv/attachments/key_exchange.py

KEY_REQUEST / KEY_ANNOUNCE / KEY_ACK protocol logic (Execution Plan Step
1.2; design spec sections 7.2-7.4; ADR-0001 for the wire format).
MIT-licensed Core code - does not import `meshtastic` and does not know
about any specific transport: it is driven entirely through the existing,
already-tested `DeliveryAdapter` contract
(meshsrv/attachments/delivery/base.py, Step 0.4), the same abstraction
`FakeTextAdapter`/`FakeBinaryAdapter` implement for tests and a real
`MeshtasticTextAdapter` (Step 1.3) will implement for hardware. This is
what satisfies Step 1.2's DoD ("используя уже существующий
AdapterIPCTransport, без изменений в adapters/meshtastic/") without this
module importing AdapterIPCTransport directly: `DeliveryAdapter` already
sits on top of it, and Core's own wiring layer (not this module) is the
only place that needs to know AdapterIPCTransport exists at all.

Rate limiting (spec 7.4), the concrete rules this module enforces:
  - `KEY_REQUEST` is answered automatically only for a DIRECT route -
    broadcast/channel/group requests are ignored outright in MVP.
  - at most one automatic KEY_ANNOUNCE reply per source transport address
    every 10 minutes (`MIN_SECONDS_BETWEEN_ANNOUNCES_TO_SAME_ADDRESS`).
  - at most 12 automatic KEY_ANNOUNCE replies per hour, workspace-wide
    (`DEFAULT_MAX_ANNOUNCES_PER_HOUR`) - a rolling window, persisted so it
    survives a restart (`mca_key_exchange_quota`, migration 4).
  - repeated requests from the same address within that same 10-minute
    window are deduplicated by the same per-address gate above - a
    KEY_REQUEST carries no epoch field of its own to dedupe against
    separately, so the two "dedup" and "at most once per 10 minutes"
    bullets in spec 7.4 collapse into one persisted gate here.

TOFU (spec 7.3's last paragraphs) is handled as: `handle_incoming()` never
sets `tofu_confirmed_at` itself - only the explicit `confirm_tofu()` call
does, standing in for the UI's "Доверять этому MCA-ключу" button. A
KEY_ANNOUNCE that contradicts an *already-confirmed* binding's public
identity is never applied in place; it is parked in
`pending_public_identity`/`pending_key_epoch` until `accept_pending_key_
change()` (another explicit action) promotes it - see migration 4's
docstring for why those columns exist.
"""

from __future__ import annotations

import dataclasses
import enum
import sqlite3
import time
from typing import Optional

from meshsrv.attachments import codec
from meshsrv.attachments.delivery.base import DeliveryEnvelope, RouteType
from meshsrv.attachments.identity import MCAPrincipal, compute_key_id, load_signing_key
from meshsrv.attachments.workspace import MCAWorkspaceManager

DEFAULT_MAX_ANNOUNCES_PER_HOUR = 12
MIN_SECONDS_BETWEEN_ANNOUNCES_TO_SAME_ADDRESS = 10 * 60
MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS = 10 * 60
_QUOTA_WINDOW_SECONDS = 3600

# PR 1 (recoverable missing-key workflow): workspace-wide cap on *automatic*
# outbound KEY_REQUESTs - the receiver-side auto-request scan that prompts a
# sender to announce its key for a parked transfer. Mirrors the announce
# quota's shape (one rolling hourly window per workspace, persisted in
# mca_auto_key_request_quota), but is a separate counter so asking for keys
# never shares a window with announcing one's own (see the docstring on
# check_key_request_rate_limit for that separation). The per-address interval
# (MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS) and the service's
# per-tick cap (MAX_AUTO_KEY_REQUESTS_PER_TICK) remain independent, tighter
# guards on top of this aggregate bound.
DEFAULT_MAX_AUTO_KEY_REQUESTS_PER_HOUR = 12


class KeyExchangeError(RuntimeError):
    """Base class for this module's errors."""


class RateLimited(KeyExchangeError):
    """Raised by `handle_incoming()` when an incoming KEY_REQUEST would
    have triggered an automatic KEY_ANNOUNCE reply, but a rate limit (spec
    7.4) blocked it. Callers (UI/API) can catch this to show the reason
    (spec 7.4's last bullet: "UI показывает причину ограничения") rather
    than silently dropping the request - a manual admin reply is still
    possible via `force_announce()`."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AddressStatus(enum.Enum):
    """Maps onto design spec section 7.2's status table for one
    (adapter_id, source_address) pair - excluding `MCA не поддерживается`/
    `Маршрут не привязан`, which are adapter-capability facts this module
    has no opinion on."""

    MCA_READY = "MCA_READY"  # "MCA готов"
    KEY_UNVERIFIED = "KEY_UNVERIFIED"  # "Ключ не проверен"
    KEY_CHANGED = "KEY_CHANGED"  # "Ключ изменился"
    KEY_UNKNOWN = "KEY_UNKNOWN"  # "Ключ неизвестен"


@dataclasses.dataclass(frozen=True)
class RecipientBinding:
    workspace_id: str
    adapter_id: str
    transport_address: str
    principal_id: str
    sender_key_id: str
    public_identity: bytes
    key_epoch: int
    bound_at: float
    tofu_confirmed_at: Optional[float]
    pending_public_identity: Optional[bytes]
    pending_key_epoch: Optional[int]
    pending_detected_at: Optional[float]

    @property
    def status(self) -> AddressStatus:
        if self.pending_public_identity is not None:
            return AddressStatus.KEY_CHANGED
        if self.tofu_confirmed_at is not None:
            return AddressStatus.MCA_READY
        return AddressStatus.KEY_UNVERIFIED


def _row_to_binding(row: sqlite3.Row) -> RecipientBinding:
    return RecipientBinding(
        workspace_id=row["workspace_id"],
        adapter_id=row["adapter_id"],
        transport_address=row["transport_address"],
        principal_id=row["principal_id"],
        sender_key_id=row["sender_key_id"],
        public_identity=bytes.fromhex(row["public_identity"]),
        key_epoch=row["key_epoch"],
        bound_at=row["bound_at"],
        tofu_confirmed_at=row["tofu_confirmed_at"],
        pending_public_identity=(
            bytes.fromhex(row["pending_public_identity"]) if row["pending_public_identity"] else None
        ),
        pending_key_epoch=row["pending_key_epoch"],
        pending_detected_at=row["pending_detected_at"],
    )


class KeyExchangeCoordinator:
    """One instance per (workspace, adapter). Holds no transport of its
    own - `handle_incoming()` takes an already-`ingest()`-ed
    `DeliveryEnvelope` (see delivery/base.py) and, when a reply is due,
    returns the reply's *logical* MCA1 CBOR bytes (already signed) for the
    caller to `encode()`/`send()` through whichever `DeliveryAdapter`
    instance received the original event. This module never imports or
    constructs a `DeliveryAdapter` itself, keeping it transport-agnostic
    exactly like the contract it's built on.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        workspace_manager: MCAWorkspaceManager,
        principal: MCAPrincipal,
        adapter_id: str,
        *,
        max_announces_per_hour: int = DEFAULT_MAX_ANNOUNCES_PER_HOUR,
        min_seconds_between_announces: int = MIN_SECONDS_BETWEEN_ANNOUNCES_TO_SAME_ADDRESS,
        min_seconds_between_key_requests: int = MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS,
        max_auto_key_requests_per_hour: int = DEFAULT_MAX_AUTO_KEY_REQUESTS_PER_HOUR,
        now_fn=time.time,
    ):
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._workspace_manager = workspace_manager
        self._principal = principal
        self._adapter_id = adapter_id
        self._max_per_hour = max_announces_per_hour
        self._min_interval = min_seconds_between_announces
        self._min_key_request_interval = min_seconds_between_key_requests
        self._max_auto_key_requests_per_hour = max_auto_key_requests_per_hour
        self._now = now_fn

    # ---- incoming message dispatch --------------------------------------

    def handle_incoming(self, envelope: DeliveryEnvelope) -> Optional[bytes]:
        """Decode `envelope.logical_message` and dispatch by MCA message
        type. Returns encoded (signed) reply bytes to send back, or `None`
        if this envelope was not a key-exchange message, or no reply is
        due. Raises `RateLimited` (not `None`) when a KEY_REQUEST was
        recognized but throttled, so a caller can tell "ignored, not a key
        message" apart from "recognized, but rate-limited" - spec 7.4
        explicitly wants the second case surfaced, not silently dropped.
        """
        try:
            message_type = codec.peek_message_type(envelope.logical_message)
        except codec.CodecError:
            return None  # not a well-formed MCA1 message at all - not this module's concern
        if message_type == codec.MessageType.KEY_REQUEST:
            return self._handle_key_request(envelope)
        if message_type == codec.MessageType.KEY_ANNOUNCE:
            return self._handle_key_announce(envelope)
        if message_type == codec.MessageType.KEY_ACK:
            self._handle_key_ack(envelope)
            return None
        return None  # some other MCA message type - not this module's concern

    def _handle_key_request(self, envelope: DeliveryEnvelope) -> Optional[bytes]:
        # Spec 7.4: "broadcast/channel/group KEY_REQUEST в MVP игнорируется".
        if envelope.route_type != RouteType.DIRECT:
            return None
        if envelope.source_address is None:
            return None
        codec.decode_key_request(envelope.logical_message, verify_key=None)
        return self._build_and_record_announce(envelope.source_address, now=self._now())

    def force_announce(self, source_address: str) -> bytes:
        """Manual admin reply (spec 7.4's last bullet), bypassing the rate
        limiter entirely - still recorded so subsequent *automatic*
        replies see an up-to-date `last_announce_sent_at`."""
        return self._build_and_record_announce(source_address, now=self._now(), bypass_rate_limit=True)

    def _build_and_record_announce(
        self, source_address: str, *, now: float, bypass_rate_limit: bool = False
    ) -> bytes:
        if not bypass_rate_limit:
            self._check_rate_limits(source_address, now)
        signing_key = load_signing_key(self._workspace_manager, self._principal)
        announce = codec.encode_key_announce(
            codec.KeyAnnounceFields(
                public_identity=self._principal.public_identity,
                epoch=self._principal.epoch,
            ),
            signing_key,
        )
        self._record_announce_sent(source_address, now)
        return announce

    def _check_rate_limits(self, source_address: str, now: float) -> None:
        state = self._conn.execute(
            "SELECT last_announce_sent_at FROM mca_key_exchange_contact_state "
            "WHERE workspace_id = ? AND adapter_id = ? AND source_address = ?",
            (self._principal.workspace_id, self._adapter_id, source_address),
        ).fetchone()
        if state is not None and state["last_announce_sent_at"] is not None:
            elapsed = now - state["last_announce_sent_at"]
            if elapsed < self._min_interval:
                raise RateLimited(
                    f"already replied to {source_address!r} {elapsed:.0f}s ago "
                    f"(minimum interval is {self._min_interval}s)"
                )
        quota_row = self._conn.execute(
            "SELECT window_start_at, announces_sent FROM mca_key_exchange_quota WHERE workspace_id = ?",
            (self._principal.workspace_id,),
        ).fetchone()
        if quota_row is not None and now - quota_row["window_start_at"] < _QUOTA_WINDOW_SECONDS:
            if quota_row["announces_sent"] >= self._max_per_hour:
                raise RateLimited(
                    f"hourly automatic KEY_ANNOUNCE quota reached ({self._max_per_hour}/hour)"
                )

    def _record_announce_sent(self, source_address: str, now: float) -> None:
        self._conn.execute(
            """
            INSERT INTO mca_key_exchange_contact_state
                (workspace_id, adapter_id, source_address, last_announce_sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_id, adapter_id, source_address)
            DO UPDATE SET last_announce_sent_at = excluded.last_announce_sent_at
            """,
            (self._principal.workspace_id, self._adapter_id, source_address, now),
        )
        quota_row = self._conn.execute(
            "SELECT window_start_at, announces_sent FROM mca_key_exchange_quota WHERE workspace_id = ?",
            (self._principal.workspace_id,),
        ).fetchone()
        if quota_row is None or now - quota_row["window_start_at"] >= _QUOTA_WINDOW_SECONDS:
            self._conn.execute(
                """
                INSERT INTO mca_key_exchange_quota (workspace_id, window_start_at, announces_sent)
                VALUES (?, ?, 1)
                ON CONFLICT(workspace_id) DO UPDATE SET window_start_at = excluded.window_start_at, announces_sent = 1
                """,
                (self._principal.workspace_id, now),
            )
        else:
            self._conn.execute(
                "UPDATE mca_key_exchange_quota SET announces_sent = announces_sent + 1 WHERE workspace_id = ?",
                (self._principal.workspace_id,),
            )
        self._conn.commit()

    # ---- outgoing KEY_REQUEST throttle (Step 1.6A.3C) -------------------

    def check_key_request_rate_limit(self, source_address: str, now: float) -> None:
        """Gate an outgoing KEY_REQUEST to `source_address` behind the
        per-contact interval (spec: one accepted key request per contact
        address every `MIN_SECONDS_BETWEEN_KEY_REQUESTS_TO_SAME_ADDRESS`).
        Raises `RateLimited` when a request was already sent within the
        window; a NULL `last_request_sent_at` (never asked) is allowed.
        Deliberately independent of `_check_rate_limits()` - announcing
        one's own key and requesting a contact's key are different actions
        and never share a quota window."""
        state = self._conn.execute(
            "SELECT last_request_sent_at FROM mca_key_exchange_contact_state "
            "WHERE workspace_id = ? AND adapter_id = ? AND source_address = ?",
            (self._principal.workspace_id, self._adapter_id, source_address),
        ).fetchone()
        if state is not None and state["last_request_sent_at"] is not None:
            elapsed = now - state["last_request_sent_at"]
            if elapsed < self._min_key_request_interval:
                raise RateLimited(
                    f"already requested {source_address!r}'s key {elapsed:.0f}s ago "
                    f"(minimum interval is {self._min_key_request_interval}s)"
                )

    def record_key_request_sent(self, source_address: str, now: float) -> None:
        """Persist the quota timestamp only after a request has actually
        been accepted for send (worker checks the delivery receipt's
        `sent` flag before calling this - a failed send never consumes the
        quota). Mirrors `_record_announce_sent()`'s UPSERT shape but writes
        `last_request_sent_at`, not `last_announce_sent_at`, so the two
        throttles stay independent."""
        self._conn.execute(
            """
            INSERT INTO mca_key_exchange_contact_state
                (workspace_id, adapter_id, source_address, last_request_sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(workspace_id, adapter_id, source_address)
            DO UPDATE SET last_request_sent_at = excluded.last_request_sent_at
            """,
            (self._principal.workspace_id, self._adapter_id, source_address, now),
        )
        self._conn.commit()

    # ---- automatic KEY_REQUEST quota (PR 1) ------------------------------

    def check_auto_key_request_quota(self, now: float) -> None:
        """PR 1: workspace-wide cap on *automatic* KEY_REQUESTs (the
        receiver-side auto-request scan), independent of the per-address
        interval above and of the KEY_ANNOUNCE quota. Raises `RateLimited`
        when the rolling hourly window is already full; a NULL/absent row
        (never auto-requested) is allowed."""
        quota_row = self._conn.execute(
            "SELECT window_start_at, requests_sent FROM mca_auto_key_request_quota WHERE workspace_id = ?",
            (self._principal.workspace_id,),
        ).fetchone()
        if quota_row is not None and now - quota_row["window_start_at"] < _QUOTA_WINDOW_SECONDS:
            if quota_row["requests_sent"] >= self._max_auto_key_requests_per_hour:
                raise RateLimited(
                    f"hourly automatic KEY_REQUEST quota reached ({self._max_auto_key_requests_per_hour}/hour)"
                )

    def record_auto_key_request_sent(self, now: float) -> None:
        """Persist the workspace-wide auto-request count only after a request
        was actually accepted for send (same principle as
        `record_key_request_sent` - a failed send never consumes the quota).
        Mirrors `_record_announce_sent()`'s rolling-window UPSERT but writes
        `mca_auto_key_request_quota`, not `mca_key_exchange_quota`, so the
        two budgets stay independent."""
        quota_row = self._conn.execute(
            "SELECT window_start_at, requests_sent FROM mca_auto_key_request_quota WHERE workspace_id = ?",
            (self._principal.workspace_id,),
        ).fetchone()
        if quota_row is None or now - quota_row["window_start_at"] >= _QUOTA_WINDOW_SECONDS:
            self._conn.execute(
                """
                INSERT INTO mca_auto_key_request_quota (workspace_id, window_start_at, requests_sent)
                VALUES (?, ?, 1)
                ON CONFLICT(workspace_id) DO UPDATE SET window_start_at = excluded.window_start_at, requests_sent = 1
                """,
                (self._principal.workspace_id, now),
            )
        else:
            self._conn.execute(
                "UPDATE mca_auto_key_request_quota SET requests_sent = requests_sent + 1 WHERE workspace_id = ?",
                (self._principal.workspace_id,),
            )
        self._conn.commit()

    # ---- KEY_ANNOUNCE handling (someone else's identity arriving) -------

    def _handle_key_announce(self, envelope: DeliveryEnvelope) -> Optional[bytes]:
        if envelope.source_address is None:
            return None
        fields = codec.decode_key_announce(envelope.logical_message, verify_against_self=True)
        now = self._now()
        sender_key_id = compute_key_id(fields.public_identity)
        existing = self._conn.execute(
            "SELECT * FROM mca_recipient_bindings WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?",
            (self._principal.workspace_id, self._adapter_id, envelope.source_address),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """
                INSERT INTO mca_recipient_bindings
                    (id, workspace_id, adapter_id, transport_address, principal_id,
                     sender_key_id, public_identity, key_epoch, bound_at, tofu_confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    f"{self._adapter_id}:{envelope.source_address}",
                    self._principal.workspace_id,
                    self._adapter_id,
                    envelope.source_address,
                    sender_key_id,
                    sender_key_id,
                    fields.public_identity.hex(),
                    fields.epoch,
                    now,
                ),
            )
        elif existing["tofu_confirmed_at"] is None:
            # Not yet trusted - freely update in place, same as a brand
            # new candidate binding (spec 7.3: TOFU only protects an
            # *already* trusted key, not the very first introduction).
            self._conn.execute(
                """
                UPDATE mca_recipient_bindings
                SET principal_id = ?, sender_key_id = ?, public_identity = ?, key_epoch = ?,
                    bound_at = ?, pending_public_identity = NULL, pending_key_epoch = NULL,
                    pending_detected_at = NULL
                WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?
                """,
                (
                    sender_key_id,
                    sender_key_id,
                    fields.public_identity.hex(),
                    fields.epoch,
                    now,
                    self._principal.workspace_id,
                    self._adapter_id,
                    envelope.source_address,
                ),
            )
        elif bytes.fromhex(existing["public_identity"]) == fields.public_identity:
            # Same key as already trusted - nothing to do beyond keeping
            # epoch bookkeeping current (no rotation logic here, epoch 0
            # only in Step 1.2, but tolerate a later epoch value).
            if fields.epoch != existing["key_epoch"]:
                self._conn.execute(
                    "UPDATE mca_recipient_bindings SET key_epoch = ? "
                    "WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?",
                    (fields.epoch, self._principal.workspace_id, self._adapter_id, envelope.source_address),
                )
        else:
            # Spec 7.3: an already-confirmed key must never be silently
            # replaced. Park the new identity as a pending candidate.
            self._conn.execute(
                """
                UPDATE mca_recipient_bindings
                SET pending_public_identity = ?, pending_key_epoch = ?, pending_detected_at = ?
                WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?
                """,
                (
                    fields.public_identity.hex(),
                    fields.epoch,
                    now,
                    self._principal.workspace_id,
                    self._adapter_id,
                    envelope.source_address,
                ),
            )
        self._conn.commit()

        signing_key = load_signing_key(self._workspace_manager, self._principal)
        return codec.encode_key_ack(
            codec.KeyAckFields(sender_key_id=bytes.fromhex(self._principal.key_id), epoch=self._principal.epoch),
            signing_key,
        )

    def _handle_key_ack(self, envelope: DeliveryEnvelope) -> None:
        # Structural validation only for Step 1.2 - no persistent
        # "acked" state exists yet in the schema, and none is required by
        # this step's DoD. A malformed KEY_ACK still raises (propagates to
        # the caller) rather than being swallowed here.
        codec.decode_key_ack(envelope.logical_message, verify_key=None)

    # ---- explicit user actions (never called from handle_incoming) ------

    def confirm_tofu(self, source_address: str, *, now: Optional[float] = None) -> None:
        """Spec 7.3: "Доверять этому MCA-ключу" - the only path that ever
        sets `tofu_confirmed_at`. Never invoked automatically."""
        now = self._now() if now is None else now
        self._conn.execute(
            "UPDATE mca_recipient_bindings SET tofu_confirmed_at = ? "
            "WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?",
            (now, self._principal.workspace_id, self._adapter_id, source_address),
        )
        self._conn.commit()

    def accept_pending_key_change(self, source_address: str, *, now: Optional[float] = None) -> None:
        """Promote a parked `pending_*` identity (see `_handle_key_announce`)
        into the trusted columns, and reset `tofu_confirmed_at` to NULL -
        the new key must go through its own explicit `confirm_tofu()`,
        exactly like a first-time introduction, rather than inheriting
        trust from the key it replaced."""
        now = self._now() if now is None else now
        row = self._conn.execute(
            "SELECT pending_public_identity, pending_key_epoch FROM mca_recipient_bindings "
            "WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?",
            (self._principal.workspace_id, self._adapter_id, source_address),
        ).fetchone()
        if row is None or row["pending_public_identity"] is None:
            raise KeyExchangeError(f"no pending key change for {source_address!r}")
        new_public_identity = bytes.fromhex(row["pending_public_identity"])
        new_key_id = compute_key_id(new_public_identity)
        self._conn.execute(
            """
            UPDATE mca_recipient_bindings
            SET principal_id = ?, sender_key_id = ?, public_identity = ?, key_epoch = ?, bound_at = ?,
                tofu_confirmed_at = NULL, pending_public_identity = NULL, pending_key_epoch = NULL,
                pending_detected_at = NULL
            WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?
            """,
            (
                new_key_id,
                new_key_id,
                new_public_identity.hex(),
                row["pending_key_epoch"],
                now,
                self._principal.workspace_id,
                self._adapter_id,
                source_address,
            ),
        )
        self._conn.commit()

    def reject_pending_key_change(self, source_address: str) -> None:
        """The other half of `accept_pending_key_change()` (ADR-0008,
        contacts backend): dismiss a parked `pending_*` identity without
        promoting it - the existing trusted binding (and its
        `tofu_confirmed_at`) is left exactly as it was. A KEY_ANNOUNCE
        carrying the same contradicting identity can still park it again
        later; this only clears today's pending flag, it does not
        blacklist the new key."""
        cur = self._conn.execute(
            """
            UPDATE mca_recipient_bindings
            SET pending_public_identity = NULL, pending_key_epoch = NULL, pending_detected_at = NULL
            WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ? AND pending_public_identity IS NOT NULL
            """,
            (self._principal.workspace_id, self._adapter_id, source_address),
        )
        if cur.rowcount == 0:
            raise KeyExchangeError(f"no pending key change for {source_address!r}")
        self._conn.commit()

    # ---- read-only status -------------------------------------------------

    def get_binding(self, source_address: str) -> Optional[RecipientBinding]:
        row = self._conn.execute(
            "SELECT * FROM mca_recipient_bindings WHERE workspace_id = ? AND adapter_id = ? AND transport_address = ?",
            (self._principal.workspace_id, self._adapter_id, source_address),
        ).fetchone()
        return _row_to_binding(row) if row is not None else None

    def get_binding_by_key_id(self, sender_key_id: str) -> Optional[RecipientBinding]:
        """ADR-0007: a second, independent index into the same table for
        resolving an inbound OFFER's ``sender_key_id`` field to a public
        identity for signature verification - regardless of whether this
        particular delivery arrived over the same transport address the
        binding was originally established on. Does not replace
        `get_binding()`: the address-keyed lookup above stays the one used
        for this module's own rate-limiting/TOFU-pinning logic, unchanged.
        `None` means the OFFER's signer is unknown - expected and common
        for a brand-new contact's first file, not an error."""
        row = self._conn.execute(
            "SELECT * FROM mca_recipient_bindings WHERE workspace_id = ? AND adapter_id = ? AND sender_key_id = ?",
            (self._principal.workspace_id, self._adapter_id, sender_key_id),
        ).fetchone()
        return _row_to_binding(row) if row is not None else None

    def list_bindings(self) -> "list[RecipientBinding]":
        """All TOFU recipient bindings for this coordinator's (workspace,
        adapter), in a deterministic (`transport_address`-ascending) order.
        Worker/startup-thread only - it reads `conn`, so the request thread
        must reach this data only through `recipient_snapshot.py`'s immutable
        `RecipientSnapshotPublisher` projection (Finding 7), never this method
        directly."""
        rows = self._conn.execute(
            "SELECT * FROM mca_recipient_bindings "
            "WHERE workspace_id = ? AND adapter_id = ? ORDER BY transport_address",
            (self._principal.workspace_id, self._adapter_id),
        ).fetchall()
        return [_row_to_binding(row) for row in rows]

    def list_key_request_sent_at(self) -> "dict[str, float]":
        """All (source_address -> last_request_sent_at) pairs that have ever
        sent an outbound KEY_REQUEST, for the key-request capability
        projection (`key_request_snapshot.py`). Returns only rows where the
        timestamp is non-NULL (a NULL means "never asked", which is the
        caller's `idle` default and therefore absent here). Worker/startup-
        thread only - it reads `conn`, so the request thread must reach this
        data only through `KeyRequestStatePublisher.snapshot()`, never this
        method directly (the same boundary Finding 7 drew for `list_bindings`).
        The raw timestamps must not be projected onward - they exist solely
        for the publisher to fold into a `waiting_response`/`retry_available`
        state string (see the no-secret discipline in that module's
        docstring)."""
        rows = self._conn.execute(
            "SELECT source_address, last_request_sent_at FROM mca_key_exchange_contact_state "
            "WHERE workspace_id = ? AND adapter_id = ? AND last_request_sent_at IS NOT NULL",
            (self._principal.workspace_id, self._adapter_id),
        ).fetchall()
        return {row["source_address"]: row["last_request_sent_at"] for row in rows}

    def get_status(self, source_address: str) -> AddressStatus:
        binding = self.get_binding(source_address)
        return AddressStatus.KEY_UNKNOWN if binding is None else binding.status

    def build_key_request(self) -> bytes:
        """Encode a self-identifying KEY_REQUEST to send to an address
        whose key is unknown (spec 7.2's `Ключ неизвестен` -> `Запросить
        MCA-ключ` action)."""
        signing_key = load_signing_key(self._workspace_manager, self._principal)
        return codec.encode_key_request(
            codec.KeyRequestFields(sender_key_id=bytes.fromhex(self._principal.key_id)), signing_key
        )
