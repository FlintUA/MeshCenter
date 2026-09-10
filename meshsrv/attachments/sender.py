"""meshsrv/attachments/sender.py

The sender-side state machine and (minimal) job queue for outgoing MCA
attachments (Execution Plan Step 1.4; design spec sections 13-15.1;
ADR-0006 for the encryption/manifest layer this drives).

State machine (design spec 15.1):

    Draft -> Validating -> Encrypting -> QueuedUpload -> Uploading
        -> ReadyToSend -> Sent -> Received -> Downloaded

with `Uploading -> QueuedUpload` on a transient Relay error,
`Sent`/`Received` -> `Expired` (not implemented in this pass - no expiry
sweep exists yet), `Sent -> Revoked` (not implemented in this pass), and
terminal failure states `FAILED_VALIDATION`, `FAILED_UPLOAD`,
`CANCELLED` (`FAILED_RADIO` is reserved but not yet reached
automatically - see `_step_ready_to_send`'s docstring).

Crash-safety design (this is the load-bearing part of Step 1.4's DoD):
every transition is exactly one `run_step()` call, and every call ends
with exactly one SQLite transaction that both does the transition's
durable side effect (if any) and advances `attachments.state` - so a
process killed at any point either has not made the transition at all
(state still reads the old value; resuming re-runs the same handler,
which is written to be safe to redo) or has fully made it (state already
reads the new value; resuming runs the *next* handler). No handler ever
leaves `attachments.state` referring to a transition that is only
half-done.

Two kinds of data are ever persisted mid-flight, deliberately no more
than these two (see migration 5's own comment in
`meshsrv/attachments/db/migrations.py` for why nothing else needs to be):
`data_key`/`nonce_prefix` (crypto.py's chunk/header encryption is fully
deterministic from these plus the plaintext file, so ciphertext is
reproducible on any retry without re-storing it) and the manifest blob
itself (NOT reproducible on retry, because sealing a recipient secret is
randomized - see migration 5's comment for the `create_upload`/409
recovery story that follows from this).

This module does not decide *when* to call `run_step()` - a caller (a
background job loop, or a synchronous "send now" UI action) drives that;
`resume_pending()` is the one entry point meant for "on startup, keep
going wherever every in-flight attachment left off".
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import sqlite3
import time
import uuid
from typing import List, Optional, Sequence

from meshsrv.attachments import codec, crypto, identity, manifest, mime_allowlist
from meshsrv.attachments.provider_registry import decode_provider_id, encode_provider_id
from meshsrv.attachments.delivery.base import DeliveryAdapter, DeliveryError, Route, RouteType
from meshsrv.attachments.identity import MCAPrincipal
from meshsrv.attachments.relay_client import (
    ChunkDeclaration,
    RelayClient,
    RelayError,
    RelayHTTPError,
    RelayUnavailableError,
)
from meshsrv.attachments.workspace import LowDiskLevel, MCAWorkspaceManager

# ---- state constants (design spec 15.1) -----------------------------------

DRAFT = "DRAFT"
VALIDATING = "VALIDATING"
ENCRYPTING = "ENCRYPTING"
QUEUED_UPLOAD = "QUEUED_UPLOAD"
UPLOADING = "UPLOADING"
READY_TO_SEND = "READY_TO_SEND"
SENT = "SENT"
RECEIVED = "RECEIVED"
DOWNLOADED = "DOWNLOADED"
EXPIRED = "EXPIRED"
REVOKED = "REVOKED"
CANCELLED = "CANCELLED"
FAILED_VALIDATION = "FAILED_VALIDATION"
FAILED_UPLOAD = "FAILED_UPLOAD"
FAILED_RADIO = "FAILED_RADIO"

TERMINAL_STATES = frozenset(
    {DOWNLOADED, EXPIRED, REVOKED, CANCELLED, FAILED_VALIDATION, FAILED_UPLOAD, FAILED_RADIO}
)
# States run_step() can make forward progress on by itself, without
# waiting for an external event (an inbound ACK, a UI "send now" click).
# SENT/RECEIVED are intentionally excluded - see apply_ack().
AUTOMATIC_STATES = frozenset({DRAFT, VALIDATING, ENCRYPTING, QUEUED_UPLOAD, UPLOADING, READY_TO_SEND})

DEFAULT_HARD_TTL_SECONDS = 72 * 3600  # design spec 14: hard_expiry = 72h
DEFAULT_DOWNLOAD_GRACE_SECONDS = 3600  # design spec 14: download_grace = 1h

# ADR-0009 (v2): the non-terminal error_code an ACK_PROVIDER_UNKNOWN stamps on
# a still-SENT row (and its delivery). Deliberately NOT a terminal state - the
# upload succeeded, the recipient's own Provider Registry simply didn't know the
# provider this workspace named; a later ACK_RECEIVED/ACK_DOWNLOADED still
# transitions cleanly (see `apply_ack()`).
PROVIDER_UNKNOWN_ERROR = "recipient_provider_unknown"


def revoke_state_ts(unix_seconds: float) -> str:
    """On-disk TEXT timestamp format for `mca_sender_revoke_state`'s
    `delete_after`/`created_at`/`updated_at` columns. The migration schema
    (Migration 14) fixes these columns as TEXT; the value is the Unix epoch
    second rendered as a decimal string, so the derivation from the
    already-integer `attachments.hard_expires_at` stays transparent and the
    worker's cleanup (`CAST(delete_after AS INTEGER) <= now`) never has to
    parse an ISO-8601 string. Shared with Migration 14's backfill fixup so
    the two can never drift into two encodings."""
    return str(int(unix_seconds))


def revoke_delete_after(
    hard_expires_at: Optional[float],
    download_grace_seconds: Optional[int],
    now: float,
) -> float:
    """The epoch second after which the Relay object protected by a retained
    revoke token is guaranteed gone (`hard_expires_at + download_grace_seconds`),
    i.e. the `mca_sender_revoke_state.delete_after` bound. When
    `hard_expires_at` is not yet known (None/<=0 - an in-flight row whose
    commit() hasn't reported one), falls back to `now + DEFAULT_HARD_TTL_SECONDS`
    as a conservative upper bound: a not-yet-committed object, once committed,
    lives at most the hard TTL plus grace."""
    hard = hard_expires_at if (hard_expires_at and hard_expires_at > 0) else (now + DEFAULT_HARD_TTL_SECONDS)
    return hard + (download_grace_seconds or DEFAULT_DOWNLOAD_GRACE_SECONDS)

KIND_GENERIC = 0

# design spec 17.4 point 5: an optional caption, stored only in the
# encrypted manifest, never sent to the Relay in the clear. Bounded so a
# UI text field can't be used to smuggle an arbitrarily large plaintext
# blob into local storage under the guise of a "comment".
MAX_COMMENT_BYTES = 1000


def normalize_comment(comment: Optional[str]) -> Optional[str]:
    """The one public comment validation/normalization helper (Finding 8) -
    used by both `create_draft()` and the API create endpoint, so the two can
    never drift. None and the empty/whitespace-only string are treated
    identically (both mean "no comment") - a UI text field that the user left
    empty must not round-trip as a comment=="" manifest header down the line.
    Rejects an embedded NUL (would truncate as a C string in some consumers)
    and anything over MAX_COMMENT_BYTES once UTF-8 encoded."""

    if comment is None:
        return None
    stripped = comment.strip()
    if not stripped:
        return None
    if "\x00" in stripped:
        raise SenderError("comment must not contain a NUL byte")
    encoded = stripped.encode("utf-8", errors="strict")
    if len(encoded) > MAX_COMMENT_BYTES:
        raise SenderError(f"comment exceeds {MAX_COMMENT_BYTES} bytes once UTF-8 encoded (got {len(encoded)})")
    return stripped

KIND_IMAGE = 1
KIND_VIDEO = 2
KIND_AUDIO = 3
KIND_DOCUMENT = 4

_SIZE_BUCKET_THRESHOLDS = (
    64 * 1024,
    256 * 1024,
    1024 * 1024,
    4 * 1024 * 1024,
    16 * 1024 * 1024,
    64 * 1024 * 1024,
)


class SenderError(RuntimeError):
    """Raised for programmer errors (unknown attachment_id, calling a
    transition on a state it cannot apply to) - never for an ordinary
    "this send failed" outcome, which is always represented as a
    persisted state (`FAILED_*`) instead of an exception."""


def size_bucket_for(plain_size: int) -> int:
    """Coarse UI-facing size bucket (design spec 6's `size_bucket` field) -
    never used for any access-control or capacity decision, only display."""

    for index, threshold in enumerate(_SIZE_BUCKET_THRESHOLDS):
        if plain_size <= threshold:
            return index
    return len(_SIZE_BUCKET_THRESHOLDS)


@dataclasses.dataclass(frozen=True)
class RecipientTarget:
    """One resolved recipient, as sender.py needs it. Callers are expected
    to resolve this from `mca_recipient_bindings` (Step 1.2's
    `KeyExchangeCoordinator`/`key_exchange.get_binding()`) before calling
    `create_draft()` - this module does not do key-exchange lookups
    itself, matching the existing separation between `key_exchange.py`
    (owns bindings) and everything downstream of it."""

    public_identity: bytes  # 32 raw bytes, Ed25519
    key_id: str  # hex, 16 chars - matches identity.compute_key_id()


def _now() -> float:
    return time.time()


def _row(conn: sqlite3.Connection, attachment_id: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        raise SenderError(f"no attachment {attachment_id!r}")
    return row


def get_state(conn: sqlite3.Connection, attachment_id: str) -> str:
    return _row(conn, attachment_id)["state"]


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


# ---- Draft -----------------------------------------------------------------


def create_draft(
    conn: sqlite3.Connection,
    workspace_manager: MCAWorkspaceManager,
    principal: MCAPrincipal,
    *,
    workspace_id: str,
    source_path: str,
    file_name: str,
    mime_type: str,
    recipients: Sequence[RecipientTarget],
    adapter_id: str,
    connector_profile_id: str,
    route_type: str,
    route_id: str,
    provider_id: bytes,
    kind: int = KIND_GENERIC,
    comment: Optional[str] = None,
    hard_ttl_seconds: int = DEFAULT_HARD_TTL_SECONDS,
    download_grace_seconds: int = DEFAULT_DOWNLOAD_GRACE_SECONDS,
    now: Optional[float] = None,
    attachment_id: Optional[str] = None,
    client_request_id: Optional[str] = None,
    canonical_hash: Optional[str] = None,
) -> str:
    """Create a new outgoing attachment in state DRAFT. Does no I/O on
    `source_path` beyond what's needed to record it - `run_step()`'s
    VALIDATING handler is what actually opens/measures/checks the file, so
    a draft can be created even for a file that doesn't exist yet (e.g. a
    UI that lets the user pick recipients before finishing a capture).

    Step 1.6A.3B (idempotent create): the three optional trailing params
    let the create *command* supply the ids/columns the request thread
    already minted/computed during staging (§3.5/§3.6) - `attachment_id`
    (so the staged spool filename and the row id agree), and the two
    Migration-11 idempotency columns `client_request_id`/`canonical_hash`.
    Omitted (a plain `create_draft` call from anywhere else) they default
    to a freshly-minted id and NULL idempotency columns, exactly as before
    this sub-stage."""

    if not recipients:
        raise SenderError("a draft must have at least one recipient")
    if len(recipients) != 1:
        # Stage 1 scope (ADR-0009): exactly one recipient, one DIRECT
        # delivery - a multi-recipient draft has no inbound-ACK semantics
        # defined yet, so it fails closed rather than being half-supported.
        raise SenderError(f"Stage 1 drafts require exactly one recipient, got {len(recipients)}")
    if len(provider_id) != 8:
        raise SenderError(f"provider_id must be 8 raw bytes, got {len(provider_id)}")
    for recipient in recipients:
        # ADR-0009 Decision 2/6: pin the recipient's exact Ed25519 public
        # identity on the transfer, so a later inbound ACK is verified against
        # the key this envelope was actually sealed to (never the *current*
        # TOFU binding). Fail closed on a wrong-sized identity or a key_id
        # that doesn't derive from it - a draft that can never be ACK-verified
        # is refused now, not discovered later as an unverifiable transfer.
        if len(recipient.public_identity) != 32:
            raise SenderError(
                f"recipient public_identity must be 32 raw bytes (Ed25519), got {len(recipient.public_identity)}"
            )
        if identity.compute_key_id(recipient.public_identity) != recipient.key_id:
            raise SenderError("recipient key_id does not match the key id derived from its public_identity")
    comment = normalize_comment(comment)

    now = _now() if now is None else now
    attachment_id = uuid.uuid4().hex if attachment_id is None else attachment_id
    transfer_id = os.urandom(16)

    conn.execute(
        """
        INSERT INTO attachments
            (id, workspace_id, transfer_id, direction, principal_id, provider_id, state,
             file_name, mime_type, created_at, hard_expires_at, download_grace_seconds, saved_path,
             draft_comment, client_request_id, canonical_hash)
        VALUES (?, ?, ?, 'sent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            workspace_id,
            transfer_id.hex(),
            principal.principal_id,
            # Base64URL, matching ProviderRegistry's own key format and
            # receiver.py's own storage (encode_provider_id()) - not hex.
            # Fixed here (reviewer-found defect, reproduced locally):
            # storing 'sent' rows as hex while ProviderRegistry.resolve()/
            # remove_or_disable() key everything by Base64URL text meant a
            # 'sent' attachment's provider_id NEVER matched
            # remove_or_disable()'s "is this provider still referenced?"
            # query - silently letting it delete a Relay profile a
            # real outgoing attachment still depended on. See Migration 9
            # for the one-time re-encoding of any already-hex-stored rows.
            encode_provider_id(provider_id),
            DRAFT,
            file_name,
            mime_type,
            now,
            0,  # hard_expires_at: unknown until commit(); 0 is never a valid real value
            download_grace_seconds,
            source_path,
            comment,
            client_request_id,
            canonical_hash,
        ),
    )
    for recipient in recipients:
        conn.execute(
            """
            INSERT INTO attachment_recipients
                (id, attachment_id, envelope_id, recipient_principal_id, recipient_public_identity)
            VALUES (?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, attachment_id, recipient.key_id, recipient.key_id, recipient.public_identity),
        )
    conn.execute(
        """
        INSERT INTO attachment_deliveries
            (id, attachment_id, adapter_id, connector_profile_id, route_type, route_id,
             wire_format, idempotency_key, state)
        VALUES (?, ?, ?, ?, ?, ?, 'MCA1_TEXT', ?, 'PENDING')
        """,
        (uuid.uuid4().hex, attachment_id, adapter_id, connector_profile_id, route_type, route_id, attachment_id),
    )
    _record_event(conn, attachment_id, now, "created", {"recipients": len(recipients)})
    conn.commit()
    return attachment_id


def _recipients_for(conn: sqlite3.Connection, attachment_id: str) -> List[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "SELECT * FROM attachment_recipients WHERE attachment_id = ? ORDER BY id", (attachment_id,)
        ).fetchall()
    )


def _delivery_for(conn: sqlite3.Connection, attachment_id: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM attachment_deliveries WHERE attachment_id = ? ORDER BY id LIMIT 1", (attachment_id,)
    ).fetchone()
    if row is None:
        raise SenderError(f"attachment {attachment_id!r} has no delivery row")
    return row


def _record_event(conn: sqlite3.Connection, attachment_id: str, now: float, event_type: str, detail: dict) -> None:
    import json

    conn.execute(
        "INSERT INTO attachment_events (id, attachment_id, occurred_at, event_type, detail_json) VALUES (?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, attachment_id, now, event_type, json.dumps(detail)),
    )


def _set_state(
    conn: sqlite3.Connection,
    attachment_id: str,
    new_state: str,
    now: float,
    *,
    error_code: Optional[str] = None,
    extra_sql: str = "",
    extra_params: Sequence = (),
) -> None:
    conn.execute(
        f"UPDATE attachments SET state = ?, error_code = ? {extra_sql} WHERE id = ?",
        (new_state, error_code, *extra_params, attachment_id),
    )
    _record_event(conn, attachment_id, now, "state_changed", {"to": new_state, "error_code": error_code})
    conn.commit()


def fail_recipients_not_trusted(conn: sqlite3.Connection, attachment_id: str, now: float) -> str:
    """Reviewer-found defect (PR #227 defect #6): nothing in this module
    ever checked a recipient's `key_exchange.RecipientBinding.status`
    before sealing their envelope in `_step_encrypting()` -
    `_public_identity_for()` happily used whatever `public_identity`
    bytes the caller handed it, whether the binding backing that identity
    was TOFU-confirmed (`AddressStatus.MCA_READY`), still unverified
    (`KEY_UNVERIFIED`), or had since received a conflicting KEY_ANNOUNCE
    parked in `pending_public_identity` (`KEY_CHANGED`, meaning the
    identity this attachment was drafted against may no longer be the
    recipient's current key at all). Encrypting to a stale or unconfirmed
    key before the caller can act on that violates the whole point of
    TOFU pinning.

    The caller (`AttachmentsService._step_sent()`) is the one positioned
    to make this check - it already owns the `KeyExchangeCoordinator`
    instance `sender.py` deliberately does not import here (this module
    doesn't know about `key_exchange.py`'s trust bookkeeping, see
    `_public_identity_for()`'s own docstring) - by calling this function
    for an attachment stuck in ENCRYPTING with one or more recipients it
    could not resolve a currently-trusted identity for, instead of ever
    invoking `run_step()`. This is the terminal, safe outcome for that
    case: FAILED_VALIDATION, exactly like any other precondition
    `_step_draft()` enforces before this attachment is allowed to leave
    DRAFT - a recipient whose key needs re-confirming is a data problem
    for the user to resolve (re-run `confirm_tofu()`/
    `accept_pending_key_change()` and start a new draft), not something
    this worker retries on its own."""
    _set_state(conn, attachment_id, FAILED_VALIDATION, now, error_code="recipient_not_trusted")
    return FAILED_VALIDATION


# ---- VALIDATING -------------------------------------------------------------


def _step_draft(conn: sqlite3.Connection, attachment_id: str, now: float) -> str:
    _set_state(conn, attachment_id, VALIDATING, now)
    return VALIDATING


def _step_validating(conn: sqlite3.Connection, workspace_manager: MCAWorkspaceManager, row: sqlite3.Row, now: float) -> str:
    attachment_id = row["id"]
    source_path = row["saved_path"]
    recipients = _recipients_for(conn, attachment_id)

    if not recipients:
        _set_state(conn, attachment_id, FAILED_VALIDATION, now, error_code="no_recipients")
        return FAILED_VALIDATION
    if not source_path or not os.path.isfile(source_path):
        _set_state(conn, attachment_id, FAILED_VALIDATION, now, error_code="source_file_missing")
        return FAILED_VALIDATION
    if not mime_allowlist.is_allowed_mime_type(row["mime_type"] or ""):
        _set_state(conn, attachment_id, FAILED_VALIDATION, now, error_code="mime_not_allowed")
        return FAILED_VALIDATION
    if not mime_allowlist.is_allowed_extension(row["file_name"] or ""):
        _set_state(conn, attachment_id, FAILED_VALIDATION, now, error_code="extension_not_allowed")
        return FAILED_VALIDATION
    if workspace_manager.low_disk_level(row["principal_id"]) == LowDiskLevel.BLOCK:
        _set_state(conn, attachment_id, FAILED_VALIDATION, now, error_code="disk_low_block")
        return FAILED_VALIDATION

    plain_size = os.path.getsize(source_path)
    plain_sha256 = _sha256_file(source_path)

    _set_state(
        conn,
        attachment_id,
        ENCRYPTING,
        now,
        extra_sql=", plain_size = ?, plain_sha256 = ?",
        extra_params=(plain_size, plain_sha256.hex()),
    )
    return ENCRYPTING


def _sha256_file(path: str, chunk_size: int = crypto.CHUNK_SIZE_BYTES) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.digest()


# ---- ENCRYPTING -------------------------------------------------------------


def _get_sender_state(conn: sqlite3.Connection, attachment_id: str) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)).fetchone()


def _ensure_revoke_state(
    conn: sqlite3.Connection,
    attachment_id: str,
    revoke_token: str,
    hard_expires_at: Optional[float],
    download_grace_seconds: Optional[int],
    now: float,
) -> None:
    """ADR-0009 Decision 5: materialize (or refresh) the *retained* revoke
    capability for an attachment whose Relay object is (about to be) committed.
    Upsert-on-conflict so a crash-resume re-running the READY_TO_SEND
    transition, or the ACK_DOWNLOADED path's defensive re-materialization, can
    never duplicate the row. This row is deliberately NOT deleted on
    ACK_DOWNLOADED - it is the one thing that keeps a post-download revoke
    possible after `mca_sender_state` is dropped."""
    delete_after = revoke_delete_after(hard_expires_at, download_grace_seconds, now)
    conn.execute(
        "INSERT INTO mca_sender_revoke_state "
        "(attachment_id, revoke_token, delete_after, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(attachment_id) DO UPDATE SET revoke_token = excluded.revoke_token, "
        "delete_after = excluded.delete_after, updated_at = excluded.updated_at",
        (attachment_id, revoke_token, revoke_state_ts(delete_after), revoke_state_ts(now), revoke_state_ts(now)),
    )


def _step_encrypting(
    conn: sqlite3.Connection, principal: MCAPrincipal, row: sqlite3.Row, now: float, recipient_identities: dict
) -> str:
    attachment_id = row["id"]
    source_path = row["saved_path"]
    transfer_id = bytes.fromhex(row["transfer_id"])
    plain_size = row["plain_size"]
    plain_sha256 = bytes.fromhex(row["plain_sha256"])
    recipients = _recipients_for(conn, attachment_id)

    # Reuse already-generated key material if a previous attempt got this
    # far before crashing - regenerating would be harmless (nothing has
    # been sent to the Relay yet at this state) but reusing avoids
    # needless extra CSPRNG calls and keeps this handler's behavior
    # obviously idempotent rather than "idempotent by coincidence".
    existing = _get_sender_state(conn, attachment_id)
    if existing is not None:
        data_key = bytes.fromhex(existing["data_key"])
        nonce_prefix = bytes.fromhex(existing["nonce_prefix"])
    else:
        data_key = crypto.generate_data_key()
        nonce_prefix = crypto.generate_nonce_prefix()

    plan = crypto.ChunkPlan.for_size(plain_size)

    chunk_declarations: List[ChunkDeclaration] = []
    ciphertext_sha256 = hashlib.sha256()
    with open(source_path, "rb") as fh:
        for index in range(plan.chunk_count):
            start, end = plan.bounds(index)
            fh.seek(start)
            plaintext_chunk = fh.read(end - start)
            ciphertext = crypto.encrypt_chunk(
                data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
                index=index, chunk_count=plan.chunk_count, plain_size=plain_size, plaintext=plaintext_chunk,
            )
            ciphertext_sha256.update(ciphertext)
            chunk_declarations.append(ChunkDeclaration(size=len(ciphertext), sha256=hashlib.sha256(ciphertext).digest()))

    header = manifest.ManifestHeader(
        file_name=row["file_name"] or "",
        mime_type=row["mime_type"] or "application/octet-stream",
        plain_size=plain_size,
        plain_sha256=plain_sha256,
        chunk_count=plan.chunk_count,
        comment=row["draft_comment"],
    )

    envelopes = []
    receipt_hashes = []
    receipt_secret_hashes_by_recipient = {}
    for recipient_row in recipients:
        receipt_secret = os.urandom(32)
        secret = manifest.RecipientSecret(
            data_key=data_key, nonce_prefix=nonce_prefix, receipt_secret=receipt_secret, chunk_count=plan.chunk_count,
        )
        sealed = manifest.seal_recipient_secret(secret, _public_identity_for(recipient_row, recipient_identities))
        key_id_bytes = bytes.fromhex(recipient_row["envelope_id"])
        envelopes.append(manifest.RecipientEnvelope(recipient_key_id=key_id_bytes, sealed_envelope=sealed))
        receipt_hash = hashlib.sha256(receipt_secret).digest()
        receipt_hashes.append(receipt_hash)
        receipt_secret_hashes_by_recipient[recipient_row["id"]] = receipt_hash.hex()

    manifest_blob = manifest.build_manifest_blob(
        transfer_id=transfer_id, data_key=data_key, nonce_prefix=nonce_prefix, header=header, recipients=envelopes,
    )
    manifest_sha256 = hashlib.sha256(manifest_blob).digest()
    total_ciphertext_size = sum(c.size for c in chunk_declarations)

    if existing is None:
        conn.execute(
            """
            INSERT INTO mca_sender_state
                (attachment_id, data_key, nonce_prefix, manifest_blob, manifest_sha256, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (attachment_id, data_key.hex(), nonce_prefix.hex(), manifest_blob, manifest_sha256.hex(), now, now),
        )
    else:
        conn.execute(
            """
            UPDATE mca_sender_state SET manifest_blob = ?, manifest_sha256 = ?, updated_at = ?
            WHERE attachment_id = ?
            """,
            (manifest_blob, manifest_sha256.hex(), now, attachment_id),
        )
    for recipient_id, receipt_hash_hex in receipt_secret_hashes_by_recipient.items():
        conn.execute(
            "UPDATE attachment_recipients SET receipt_secret_hash = ? WHERE id = ?",
            (receipt_hash_hex, recipient_id),
        )

    _set_state(
        conn,
        attachment_id,
        QUEUED_UPLOAD,
        now,
        # draft_comment was only ever needed to build `header` above (it's
        # now sealed inside manifest_blob, already persisted to
        # mca_sender_state a few lines up) - clear the local plaintext
        # copy now that it has done its one job, same spirit as clearing
        # `pending_offer_cbor` once receiver.py is done with it.
        extra_sql=", cipher_size = ?, draft_comment = NULL",
        extra_params=(total_ciphertext_size,),
    )
    return QUEUED_UPLOAD


def _public_identity_for(recipient_row: sqlite3.Row, recipient_identities: dict) -> bytes:
    """The recipient's raw 32-byte Ed25519 public identity, for sealing
    their envelope. Sealing uses the caller-supplied `recipient_identities`
    mapping (re-resolved and trust-checked fresh on every ENCRYPTING call by
    the service's `_resolve_recipient_identities()`), *not* a value read back
    off the recipient row.

    Note for ADR-0009: `attachment_recipients` now *does* carry a
    `recipient_public_identity` column (added by Migration 14), but that
    column exists for a different purpose - pinning the exact key an inbound
    ACK is later verified against (`service._process_inbound_ack()`), which
    must keep trusting the key this envelope was sealed to even after a
    contact-key rotation. ENCRYPTING still goes through the caller's mapping
    so it re-resolves the *currently-trusted* binding (and fails closed via
    `fail_recipients_not_trusted()` when that binding is no longer
    MCA_READY), rather than blindly re-encrypting to a key that may have
    rotated since the draft was created."""

    key_id = recipient_row["envelope_id"]
    if key_id not in recipient_identities:
        raise SenderError(f"no public_identity supplied for recipient key_id {key_id!r}")
    return recipient_identities[key_id]


# ---- Uploading ---------------------------------------------------------------


def _step_queued_upload(conn: sqlite3.Connection, row: sqlite3.Row, now: float, *, network_available: bool) -> str:
    attachment_id = row["id"]
    if not network_available:
        return QUEUED_UPLOAD  # stay put; caller's scheduler retries later
    _set_state(conn, attachment_id, UPLOADING, now)
    return UPLOADING


def _step_uploading(
    conn: sqlite3.Connection,
    relay_client: RelayClient,
    workspace_manager: MCAWorkspaceManager,
    row: sqlite3.Row,
    now: float,
) -> str:
    attachment_id = row["id"]
    transfer_id = bytes.fromhex(row["transfer_id"])
    state = _get_sender_state(conn, attachment_id)
    if state is None:
        raise SenderError(f"attachment {attachment_id!r} reached UPLOADING with no mca_sender_state row")

    data_key = bytes.fromhex(state["data_key"])
    nonce_prefix = bytes.fromhex(state["nonce_prefix"])
    manifest_blob = bytes(state["manifest_blob"])
    manifest_sha256 = bytes.fromhex(state["manifest_sha256"])
    plain_size = row["plain_size"]
    plan = crypto.ChunkPlan.for_size(plain_size)

    upload_id = state["upload_id"]
    upload_token = state["upload_token"]
    revoke_token = state["revoke_token"]

    if upload_id is None:
        recipients = _recipients_for(conn, attachment_id)
        receipt_hashes = [bytes.fromhex(r["receipt_secret_hash"]) for r in recipients]
        chunk_declarations, ciphertext_sha256 = _recompute_chunk_declarations(
            row["saved_path"], data_key, nonce_prefix, transfer_id, plan
        )
        ciphertext_total = sum(c.size for c in chunk_declarations)
        try:
            session = relay_client.create_upload(
                transfer_id=transfer_id,
                total_size=ciphertext_total,
                ciphertext_sha256=ciphertext_sha256,
                manifest_size=len(manifest_blob),
                manifest_sha256=manifest_sha256,
                chunks=chunk_declarations,
                receipt_hashes=receipt_hashes,
                hard_ttl_seconds=DEFAULT_HARD_TTL_SECONDS,
                download_grace_seconds=row["download_grace_seconds"] or DEFAULT_DOWNLOAD_GRACE_SECONDS,
            )
        except RelayHTTPError as exc:
            if exc.code == "transfer_exists":
                # We (or a previous, crashed attempt) already created an
                # upload session under this transfer_id, but we have no
                # local upload_id to resume it - the session is orphaned.
                # Never retry create_upload with the same transfer_id
                # again; tombstone it and start over under a fresh one.
                return _abandon_transfer_id_and_restart(conn, row, now, reason="orphaned_upload_session")
            # Any other non-2xx from create_upload (e.g. 422 on our own
            # declared sizes/hashes) is a request our own code built
            # wrong, not a transient condition - retrying unchanged would
            # just fail identically forever, so this is terminal, not a
            # QueuedUpload bounce.
            _set_state(conn, attachment_id, FAILED_UPLOAD, now, error_code=f"create_upload_http_{exc.status_code}_{exc.code}")
            return FAILED_UPLOAD
        except RelayUnavailableError:
            _set_state(conn, attachment_id, QUEUED_UPLOAD, now, error_code="relay_unavailable")
            return QUEUED_UPLOAD

        upload_id, upload_token, revoke_token = session.upload_id, session.upload_token, session.revoke_token
        conn.execute(
            "UPDATE mca_sender_state SET upload_id = ?, upload_token = ?, revoke_token = ?, updated_at = ? WHERE attachment_id = ?",
            (upload_id, upload_token, revoke_token, now, attachment_id),
        )
        conn.commit()

    try:
        status = relay_client.get_upload_status(upload_id, upload_token)
        uploaded_indices = {c.index for c in status.chunks if c.uploaded}
        for index in range(plan.chunk_count):
            if index in uploaded_indices:
                continue
            start, end = plan.bounds(index)
            with open(row["saved_path"], "rb") as fh:
                fh.seek(start)
                plaintext_chunk = fh.read(end - start)
            ciphertext = crypto.encrypt_chunk(
                data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
                index=index, chunk_count=plan.chunk_count, plain_size=plain_size, plaintext=plaintext_chunk,
            )
            relay_client.upload_chunk(upload_id, upload_token, index, ciphertext)

        if not status.manifest_uploaded:
            relay_client.upload_manifest(upload_id, upload_token, manifest_blob)

        descriptor = relay_client.commit(upload_id, upload_token)
    except RelayUnavailableError:
        _set_state(conn, attachment_id, QUEUED_UPLOAD, now, error_code="relay_unavailable")
        return QUEUED_UPLOAD
    except RelayHTTPError as exc:
        _set_state(conn, attachment_id, FAILED_UPLOAD, now, error_code=f"relay_http_{exc.status_code}_{exc.code}")
        return FAILED_UPLOAD

    hard_expires_at = _parse_relay_timestamp(descriptor.hard_expires_at)
    # ADR-0009 Decision 5: the Relay object is now committed - persist the
    # retained revoke capability (revoke_token + a delete_after bound) *before*
    # this attachment can ever reach SENT, so a later ACK_DOWNLOADED may delete
    # the transient `mca_sender_state` without destroying the token a
    # post-download revoke needs. Skipped only when the session somehow failed
    # to hand back a revoke_token (nothing to retain).
    if revoke_token is not None:
        _ensure_revoke_state(
            conn, attachment_id, revoke_token, hard_expires_at, row["download_grace_seconds"], now
        )
    _set_state(
        conn,
        attachment_id,
        READY_TO_SEND,
        now,
        extra_sql=", hard_expires_at = ?",
        extra_params=(hard_expires_at,),
    )
    return READY_TO_SEND


def _abandon_transfer_id_and_restart(conn: sqlite3.Connection, row: sqlite3.Row, now: float, *, reason: str) -> str:
    attachment_id = row["id"]
    old_transfer_id = row["transfer_id"]
    conn.execute(
        "INSERT OR REPLACE INTO mca_tombstones (transfer_id, workspace_id, reason, tombstoned_at, purge_after) VALUES (?, ?, ?, ?, ?)",
        (old_transfer_id, row["workspace_id"], reason, now, now + 7 * 86400),
    )
    conn.execute("DELETE FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,))
    new_transfer_id = os.urandom(16).hex()
    conn.execute("UPDATE attachments SET transfer_id = ? WHERE id = ?", (new_transfer_id, attachment_id))
    _set_state(conn, attachment_id, ENCRYPTING, now, error_code=reason)
    return ENCRYPTING


def _recompute_chunk_declarations(source_path, data_key, nonce_prefix, transfer_id, plan):
    """Recompute every chunk's ciphertext (deterministic - see module
    docstring) in one pass, returning both the per-chunk declarations
    `create_upload` needs and the overall `ciphertext_sha256` it also
    needs - one file read, one encryption pass, instead of two."""

    plain_size = plan.plain_size
    declarations: List[ChunkDeclaration] = []
    overall_digest = hashlib.sha256()
    with open(source_path, "rb") as fh:
        for index in range(plan.chunk_count):
            start, end = plan.bounds(index)
            fh.seek(start)
            plaintext_chunk = fh.read(end - start)
            ciphertext = crypto.encrypt_chunk(
                data_key=data_key, nonce_prefix=nonce_prefix, transfer_id=transfer_id,
                index=index, chunk_count=plan.chunk_count, plain_size=plain_size, plaintext=plaintext_chunk,
            )
            declarations.append(ChunkDeclaration(size=len(ciphertext), sha256=hashlib.sha256(ciphertext).digest()))
            overall_digest.update(ciphertext)
    return declarations, overall_digest.digest()


def _parse_relay_timestamp(value) -> int:
    """`ObjectDescriptor.hard_expires_at` is documented as `str` but, in
    practice, the mock Relay serializes it as a raw Unix-epoch number
    while `get_upload_status()`'s own `hard_expires_at` goes through an
    ISO8601 `_iso()` helper - a real, observed inconsistency between two
    Relay endpoints (or between the mock and ADR-0005's real deployment;
    not fully resolved by this pass). Accept either shape rather than
    assume one, since guessing wrong here would corrupt every attachment's
    `hard_expires_at` silently."""

    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    import datetime

    text = str(value).replace("Z", "+00:00")
    return int(datetime.datetime.fromisoformat(text).timestamp())


# ---- ReadyToSend -------------------------------------------------------------


def _step_ready_to_send(
    conn: sqlite3.Connection,
    principal: MCAPrincipal,
    signing_key,
    delivery_adapter: DeliveryAdapter,
    row: sqlite3.Row,
    now: float,
) -> str:
    """Build and sign the OFFER, then send it. This is the one handler the
    Step 1.4 DoD's specific crash scenario is about: `_step_uploading()`
    above never calls this - it only ever ends at READY_TO_SEND and
    returns, so a crash between the two `run_step()` calls (after
    `commit()` succeeded and READY_TO_SEND was durably persisted, before
    the OFFER is ever sent) resumes here and sends exactly once, never
    re-running the upload.

    `FAILED_RADIO` is reserved for a caller-driven "give up after N
    retries" policy (mca_jobs.attempts) - this pass leaves a failed send
    parked in READY_TO_SEND (retryable indefinitely by calling run_step
    again) rather than auto-failing it after some attempt count, since
    that count is a job-queue/scheduling policy this pass does not
    implement.
    """

    attachment_id = row["id"]
    delivery = _delivery_for(conn, attachment_id)
    # `attachment_deliveries.route_type` is persisted as the enum's plain
    # text value (`RouteType.DIRECT.value`, see create_draft()'s caller
    # contract) - reconstructed here rather than trusted as an already-typed
    # value, since it round-tripped through SQLite as TEXT.
    route = Route(
        route_type=RouteType(delivery["route_type"]),
        route_id=delivery["route_id"],
        destination_address=delivery["route_id"],
    )

    fields = codec.OfferFields(
        provider_id=decode_provider_id(row["provider_id"]),
        transfer_id=bytes.fromhex(row["transfer_id"]),
        sender_key_id=bytes.fromhex(principal.key_id),
        kind=KIND_GENERIC,
        size_bucket=size_bucket_for(row["plain_size"] or 0),
        hard_expires_at=row["hard_expires_at"],
        flags=0,
    )
    logical_message = codec.encode_offer(fields, signing_key)
    wire_payload = delivery_adapter.encode(logical_message, route)

    try:
        receipt = delivery_adapter.send(wire_payload, route, idempotency_key=attachment_id)
    except DeliveryError as exc:
        conn.execute(
            "UPDATE attachment_deliveries SET state = 'FAILED', error_code = ? WHERE id = ?",
            (type(exc).__name__, delivery["id"]),
        )
        _set_state(conn, attachment_id, READY_TO_SEND, now, error_code="radio_send_failed")
        return READY_TO_SEND

    if not receipt.sent:
        _set_state(conn, attachment_id, READY_TO_SEND, now, error_code="radio_send_not_confirmed")
        return READY_TO_SEND

    conn.execute(
        "UPDATE attachment_deliveries SET state = 'SENT', sent_at = ?, external_message_id = ? WHERE id = ?",
        (now, receipt.external_message_id, delivery["id"]),
    )
    _set_state(conn, attachment_id, SENT, now)
    return SENT


# ---- external events (driven by inbound ACKs, not run_step) ----------------


def apply_ack(conn: sqlite3.Connection, attachment_id: str, message_type, now: Optional[float] = None) -> str:
    """ADR-0009 Decision 4: verify-and-apply one inbound simple ACK against a
    *sent* attachment, atomically and idempotently. `message_type` is one of
    `codec.MessageType.ACK_RECEIVED` / `ACK_DOWNLOADED` / `ACK_PROVIDER_UNKNOWN`
    (the caller has already peeked and signature-verified it). Returns the
    resulting `attachments.state`; an out-of-order/duplicate/stale ACK is
    *dropped* by returning the current state unchanged rather than raising, so
    the worker's inbound dispatch can apply any ACK to any row with no
    try/except around the state machine. Raises `SenderError` only for a
    genuinely invalid input (unknown `attachment_id`, or a `message_type` that
    is not an ACK - a programmer error, never a wire event)."""
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if message_type == codec.MessageType.ACK_RECEIVED:
        return _apply_ack_received(conn, row, now)
    if message_type == codec.MessageType.ACK_DOWNLOADED:
        return _apply_ack_downloaded(conn, row, now)
    if message_type == codec.MessageType.ACK_PROVIDER_UNKNOWN:
        return _apply_ack_provider_unknown(conn, row, now)
    raise SenderError(f"{message_type!r} is not an inbound-ACK message type")


def _apply_ack_received(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> str:
    """SENT -> RECEIVED (set received_at/ack_at if null, clear errors, one
    timeline event); RECEIVED/DOWNLOADED -> no-op (duplicate/superseded); any
    other state -> dropped (no write)."""
    attachment_id = row["id"]
    state = row["state"]
    if state in (RECEIVED, DOWNLOADED):
        return state
    if state != SENT:
        return state
    delivery = _delivery_for(conn, attachment_id)
    for recipient in _recipients_for(conn, attachment_id):
        conn.execute(
            "UPDATE attachment_recipients SET received_at = COALESCE(received_at, ?) WHERE id = ?",
            (now, recipient["id"]),
        )
    conn.execute(
        "UPDATE attachment_deliveries SET state = 'RECEIVED', ack_at = COALESCE(ack_at, ?), error_code = NULL "
        "WHERE id = ?",
        (now, delivery["id"]),
    )
    conn.execute("UPDATE attachments SET state = ?, error_code = NULL WHERE id = ?", (RECEIVED, attachment_id))
    _record_event(conn, attachment_id, now, "ack_received", {"to": RECEIVED})
    conn.commit()
    return RECEIVED


def _apply_ack_downloaded(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> str:
    """SENT -> (apply RECEIVED, then) DOWNLOADED; RECEIVED -> DOWNLOADED;
    DOWNLOADED -> no-op; any other state -> dropped. Deletes the transient
    `mca_sender_state` row only after the revoke capability is durable (ADR-0009
    Decision 5), preserving `mca_sender_revoke_state`."""
    attachment_id = row["id"]
    state = row["state"]
    if state == DOWNLOADED:
        return state
    if state not in (SENT, RECEIVED):
        return state
    delivery = _delivery_for(conn, attachment_id)
    for recipient in _recipients_for(conn, attachment_id):
        conn.execute(
            "UPDATE attachment_recipients SET received_at = COALESCE(received_at, ?), "
            "downloaded_at = COALESCE(downloaded_at, ?) WHERE id = ?",
            (now, now, recipient["id"]),
        )
    conn.execute(
        "UPDATE attachment_deliveries SET state = 'DOWNLOADED', ack_at = COALESCE(ack_at, ?), error_code = NULL "
        "WHERE id = ?",
        (now, delivery["id"]),
    )
    conn.execute("UPDATE attachments SET state = ?, error_code = NULL WHERE id = ?", (DOWNLOADED, attachment_id))
    _record_event(conn, attachment_id, now, "ack_downloaded", {"to": DOWNLOADED})
    _delete_transient_sender_state_keeping_revoke(conn, row, now)
    conn.commit()
    return DOWNLOADED


def _apply_ack_provider_unknown(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> str:
    """SENT -> keep SENT, stamp a non-terminal `error_code` and delivery
    `PROVIDER_UNKNOWN` (never a terminal state - ADR-0009 Decision 4a);
    RECEIVED/DOWNLOADED -> ignored (never regress); any other state -> dropped.
    Idempotent on the delivery already being PROVIDER_UNKNOWN. The upload and
    revoke capability are retained - the object may still be fetched later."""
    attachment_id = row["id"]
    state = row["state"]
    if state in (RECEIVED, DOWNLOADED):
        return state
    if state != SENT:
        return state
    delivery = _delivery_for(conn, attachment_id)
    if delivery["state"] == "PROVIDER_UNKNOWN":
        return state  # already applied - no second event, no re-write
    conn.execute("UPDATE attachments SET error_code = ? WHERE id = ?", (PROVIDER_UNKNOWN_ERROR, attachment_id))
    conn.execute(
        "UPDATE attachment_deliveries SET state = 'PROVIDER_UNKNOWN', ack_at = COALESCE(ack_at, ?), "
        "error_code = ? WHERE id = ?",
        (now, PROVIDER_UNKNOWN_ERROR, delivery["id"]),
    )
    _record_event(conn, attachment_id, now, "ack_provider_unknown", {"to": SENT})
    conn.commit()
    return SENT


def _delete_transient_sender_state_keeping_revoke(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
    """ADR-0009 Decision 5: before dropping the transient `mca_sender_state`
    row (its encryption/upload secrets are useless once the recipient has the
    object), make sure the retained revoke capability is durable. Normally the
    READY_TO_SEND transition already wrote `mca_sender_revoke_state`; this
    defensive re-materialization covers a row that somehow reached SENT without
    one (a pre-migration row whose token was backfilled, or a crash between the
    two writes). If there is no revoke_token at all, there is nothing to retain
    and the transient row is simply dropped."""
    sender_state = _get_sender_state(conn, row["id"])
    revoke_token = sender_state["revoke_token"] if sender_state is not None else None
    if revoke_token is not None:
        _ensure_revoke_state(
            conn, row["id"], revoke_token, row["hard_expires_at"], row["download_grace_seconds"], now
        )
    conn.execute("DELETE FROM mca_sender_state WHERE attachment_id = ?", (row["id"],))


def cancel(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if is_terminal(row["state"]) or row["state"] in (SENT, RECEIVED, DOWNLOADED):
        raise SenderError(f"attachment {attachment_id!r} is {row['state']!r} - cannot cancel")
    _set_state(conn, attachment_id, CANCELLED, now)
    conn.execute("DELETE FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,))
    # ADR-0009 Decision 5/5a: a confirmed cancel also retires the retained
    # revoke capability - there is no Relay object left to revoke.
    conn.execute("DELETE FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,))
    conn.commit()
    return CANCELLED


def revoke(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    """Step 1.6A.5 (§7.3/§8): the *local* half of a revoke, transitioning a
    sent attachment out of SENT/RECEIVED/DOWNLOADED into REVOKED and dropping
    its `mca_sender_state` row. Pure and local by design - the remote Relay
    revoke is the caller's responsibility (and must complete *before* this is
    called, so a committed-but-unreachable object is never marked REVOKED on
    an unconfirmed remote failure). Only SENT/RECEIVED/DOWNLOADED are
    revocable here; anything else (including every terminal state) raises
    `SenderError`, exactly like `cancel()`'s own state guard."""
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if row["state"] not in (SENT, RECEIVED, DOWNLOADED):
        raise SenderError(
            f"attachment {attachment_id!r} is {row['state']!r}, not SENT/RECEIVED/DOWNLOADED - cannot revoke"
        )
    _set_state(conn, attachment_id, REVOKED, now)
    conn.execute("DELETE FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,))
    # ADR-0009 Decision 5/5a: a confirmed revoke also retires the retained
    # revoke capability (its one job is now done).
    conn.execute("DELETE FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,))
    conn.commit()
    return REVOKED


# ---- the one step-dispatcher -------------------------------------------------


def run_step(
    conn: sqlite3.Connection,
    *,
    workspace_manager: MCAWorkspaceManager,
    principal: MCAPrincipal,
    recipient_identities: Optional[dict] = None,
    relay_client: Optional[RelayClient] = None,
    delivery_adapter: Optional[DeliveryAdapter] = None,
    network_available: bool = True,
    attachment_id: str,
    now: Optional[float] = None,
) -> str:
    """Perform exactly one state transition for `attachment_id` and return
    the resulting state. Safe to call repeatedly, including immediately
    after a crash mid-transition (see module docstring).

    `recipient_identities` is required only when the current state is
    ENCRYPTING: a `{key_id_hex: public_identity_bytes}` mapping for every
    recipient on this attachment, since ENCRYPTING is the one step that
    needs each recipient's raw 32-byte public identity to seal their
    envelope. The caller re-resolves that mapping from its current trusted
    bindings on every ENCRYPTING call (see `_public_identity_for()`'s
    docstring) - even though ADR-0009 now pins the identity on the
    `attachment_recipients` row for inbound-ACK verification, ENCRYPTING
    still goes through this fresh, trust-checked mapping rather than
    re-encrypting to a possibly-rotated key.
    """

    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    state = row["state"]

    if state == DRAFT:
        return _step_draft(conn, attachment_id, now)
    if state == VALIDATING:
        return _step_validating(conn, workspace_manager, row, now)
    if state == ENCRYPTING:
        if recipient_identities is None:
            raise SenderError("ENCRYPTING requires recipient_identities")
        return _step_encrypting(conn, principal, row, now, recipient_identities)
    if state == QUEUED_UPLOAD:
        return _step_queued_upload(conn, row, now, network_available=network_available)
    if state == UPLOADING:
        if relay_client is None:
            raise SenderError("UPLOADING requires relay_client")
        return _step_uploading(conn, relay_client, workspace_manager, row, now)
    if state == READY_TO_SEND:
        if delivery_adapter is None:
            raise SenderError("READY_TO_SEND requires delivery_adapter")
        signing_key = identity.load_signing_key(workspace_manager, principal)
        return _step_ready_to_send(conn, principal, signing_key, delivery_adapter, row, now)

    # SENT/RECEIVED and every terminal state: nothing for run_step() to do
    # on its own - progress from here is event-driven (apply_ack, driven by
    # an inbound ACK) or already final.
    return state


def resume_pending(
    conn: sqlite3.Connection,
    *,
    workspace_manager: MCAWorkspaceManager,
    principal: MCAPrincipal,
    recipient_identities_by_attachment: Optional[dict] = None,
    relay_client: Optional[RelayClient] = None,
    delivery_adapter: Optional[DeliveryAdapter] = None,
    network_available: bool = True,
    max_steps_per_attachment: int = 50,
) -> List[str]:
    """On startup: drive every non-terminal, non-waiting 'sent' attachment
    forward as far as it will automatically go. Returns the attachment_ids
    it touched. `max_steps_per_attachment` is a runaway guard, not a
    normal limit - each real transition is a distinct state, so a single
    attachment should never need more than the state machine's own depth."""

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, state FROM attachments WHERE direction = 'sent'"
    ).fetchall()
    touched = []
    for row in rows:
        if row["state"] not in AUTOMATIC_STATES:
            continue
        touched.append(row["id"])
        recipient_identities = (
            (recipient_identities_by_attachment or {}).get(row["id"])
        )
        for _ in range(max_steps_per_attachment):
            state = get_state(conn, row["id"])
            if state not in AUTOMATIC_STATES:
                break
            if state == QUEUED_UPLOAD and not network_available:
                break
            new_state = run_step(
                conn,
                workspace_manager=workspace_manager,
                principal=principal,
                recipient_identities=recipient_identities,
                relay_client=relay_client,
                delivery_adapter=delivery_adapter,
                network_available=network_available,
                attachment_id=row["id"],
            )
            if new_state == state:
                break
    return touched
