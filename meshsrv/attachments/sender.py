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
# SENT/RECEIVED are intentionally excluded - see on_ack_received()/
# on_ack_downloaded().
AUTOMATIC_STATES = frozenset({DRAFT, VALIDATING, ENCRYPTING, QUEUED_UPLOAD, UPLOADING, READY_TO_SEND})

DEFAULT_HARD_TTL_SECONDS = 72 * 3600  # design spec 14: hard_expiry = 72h
DEFAULT_DOWNLOAD_GRACE_SECONDS = 3600  # design spec 14: download_grace = 1h

KIND_GENERIC = 0
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
) -> str:
    """Create a new outgoing attachment in state DRAFT. Does no I/O on
    `source_path` beyond what's needed to record it - `run_step()`'s
    VALIDATING handler is what actually opens/measures/checks the file, so
    a draft can be created even for a file that doesn't exist yet (e.g. a
    UI that lets the user pick recipients before finishing a capture)."""

    if not recipients:
        raise SenderError("a draft must have at least one recipient")
    if len(provider_id) != 8:
        raise SenderError(f"provider_id must be 8 raw bytes, got {len(provider_id)}")

    now = _now() if now is None else now
    attachment_id = uuid.uuid4().hex
    transfer_id = os.urandom(16)

    conn.execute(
        """
        INSERT INTO attachments
            (id, workspace_id, transfer_id, direction, principal_id, provider_id, state,
             file_name, mime_type, created_at, hard_expires_at, download_grace_seconds, saved_path)
        VALUES (?, ?, ?, 'sent', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            workspace_id,
            transfer_id.hex(),
            principal.principal_id,
            provider_id.hex(),
            DRAFT,
            file_name,
            mime_type,
            now,
            0,  # hard_expires_at: unknown until commit(); 0 is never a valid real value
            download_grace_seconds,
            source_path,
        ),
    )
    for recipient in recipients:
        conn.execute(
            """
            INSERT INTO attachment_recipients (id, attachment_id, envelope_id, recipient_principal_id)
            VALUES (?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, attachment_id, recipient.key_id, recipient.key_id),
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
        comment=None,
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
        extra_sql=", cipher_size = ?",
        extra_params=(total_ciphertext_size,),
    )
    return QUEUED_UPLOAD


def _public_identity_for(recipient_row: sqlite3.Row, recipient_identities: dict) -> bytes:
    """The recipient's raw 32-byte Ed25519 public identity, for sealing
    their envelope. `attachment_recipients` deliberately has no
    `public_identity` column of its own (it is receiver-lookup state that
    already lives in `mca_recipient_bindings`, keyed by the same
    `envelope_id`/key_id this row stores) - `run_step()` is handed the
    mapping fresh on every ENCRYPTING call (`recipient_identities`) rather
    than this module persisting a second copy of key material that
    `key_exchange.py`/`mca_recipient_bindings` already owns."""

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
        provider_id=bytes.fromhex(row["provider_id"]),
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


def on_ack_received(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if row["state"] != SENT:
        raise SenderError(f"attachment {attachment_id!r} is {row['state']!r}, not SENT - cannot record ACK_RECEIVED")
    _set_state(conn, attachment_id, RECEIVED, now)
    return RECEIVED


def on_ack_downloaded(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if row["state"] != RECEIVED:
        raise SenderError(f"attachment {attachment_id!r} is {row['state']!r}, not RECEIVED - cannot record ACK_DOWNLOADED")
    _set_state(conn, attachment_id, DOWNLOADED, now)
    conn.execute("DELETE FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,))
    conn.commit()
    return DOWNLOADED


def cancel(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if is_terminal(row["state"]) or row["state"] in (SENT, RECEIVED, DOWNLOADED):
        raise SenderError(f"attachment {attachment_id!r} is {row['state']!r} - cannot cancel")
    _set_state(conn, attachment_id, CANCELLED, now)
    conn.execute("DELETE FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,))
    conn.commit()
    return CANCELLED


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
    envelope, and this module deliberately does not persist that value a
    second time (see `_public_identity_for()`'s docstring) - the caller
    already has it from resolving `RecipientTarget`s for `create_draft()`.
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
    # on its own - progress from here is event-driven (on_ack_received /
    # on_ack_downloaded) or already final.
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
