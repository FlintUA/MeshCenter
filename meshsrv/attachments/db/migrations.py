"""meshsrv/attachments/db/migrations.py

Versioned schema for `attachments.db` (design spec section 16.4; ADR-0003).
Ten tables in one migration: `attachments`, `attachment_recipients`,
`attachment_deliveries`, `attachment_events`, `mca_contacts`,
`mca_recipient_bindings`, `mca_connector_profiles`, `mca_provider_profiles`,
`mca_jobs`, `mca_tombstones`. Column sets for `attachments` and
`attachment_deliveries` are taken verbatim from the design spec's own
tables; the other eight tables are not given column-level detail by the
spec (it names them and, for a few, describes their role in prose) - their
schemas here are this ADR's own reasonable design, to be revised by
whichever later step (Provider Registry Step 0.7, principal/binding Step
1.2, job queue Step 1.4) first needs a column this migration doesn't have.

Later migrations: 2 extends `mca_provider_profiles` (Step 0.7); 3 renames
its public-key column to match the real Relay's Base64URL encoding
(Step 1.1/ADR-0005); 4 adds `mca_principal` (this workspace's own MCA
identity) and the key-exchange rate-limit state tables `mca_key_exchange_
contact_state`/`mca_key_exchange_quota` (Step 1.2, design spec section
7.1/7.4).

Migrations are tracked via `PRAGMA user_version` - a plain SQLite
mechanism, deliberately not a bespoke `schema_migrations` table, since
`attachments.db` has no other consumer to keep compatible with it.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

_MIGRATION_0001_UP = """
CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    transfer_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('sent', 'received')),
    principal_id TEXT NOT NULL,
    sender_principal_id TEXT,
    provider_id TEXT,
    state TEXT NOT NULL,
    recipient_key_epoch INTEGER,
    file_name TEXT,
    mime_type TEXT,
    plain_size INTEGER,
    cipher_size INTEGER,
    plain_sha256 TEXT,
    created_at INTEGER NOT NULL,
    hard_expires_at INTEGER NOT NULL,
    download_grace_seconds INTEGER NOT NULL,
    saved_path TEXT,
    error_code TEXT,
    retry_at INTEGER,
    primary_delivery_id TEXT,
    UNIQUE (workspace_id, transfer_id)
);
CREATE INDEX idx_attachments_workspace_state ON attachments(workspace_id, state);

CREATE TABLE attachment_recipients (
    id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    envelope_id TEXT NOT NULL,
    recipient_principal_id TEXT,
    receipt_secret_hash TEXT,
    received_at INTEGER,
    downloaded_at INTEGER,
    UNIQUE (attachment_id, envelope_id)
);

CREATE TABLE attachment_deliveries (
    id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    adapter_id TEXT NOT NULL,
    connector_profile_id TEXT NOT NULL,
    route_type TEXT NOT NULL,
    route_id TEXT NOT NULL,
    wire_format TEXT NOT NULL,
    external_message_id TEXT,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL,
    sent_at INTEGER,
    ack_at INTEGER,
    error_code TEXT,
    retry_at INTEGER,
    UNIQUE (attachment_id, idempotency_key)
);
CREATE INDEX idx_attachment_deliveries_attachment ON attachment_deliveries(attachment_id);

CREATE TABLE attachment_events (
    id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    occurred_at INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX idx_attachment_events_attachment ON attachment_events(attachment_id, occurred_at);

CREATE TABLE mca_contacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    display_name TEXT,
    principal_id TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (workspace_id, principal_id)
);

CREATE TABLE mca_recipient_bindings (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    transport_address TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    sender_key_id TEXT NOT NULL,
    public_identity TEXT NOT NULL,
    key_epoch INTEGER NOT NULL,
    bound_at INTEGER NOT NULL,
    tofu_confirmed_at INTEGER,
    UNIQUE (workspace_id, adapter_id, transport_address)
);

CREATE TABLE mca_connector_profiles (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    connector_profile_id TEXT NOT NULL,
    display_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    UNIQUE (workspace_id, adapter_id, connector_profile_id)
);

CREATE TABLE mca_provider_profiles (
    provider_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    service_public_key_hex TEXT NOT NULL,
    max_ciphertext_bytes INTEGER NOT NULL,
    hard_expiry_default_seconds INTEGER NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL
);

CREATE TABLE mca_jobs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    attachment_id TEXT REFERENCES attachments(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    not_before INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX idx_mca_jobs_workspace_state ON mca_jobs(workspace_id, state, not_before);

CREATE TABLE mca_tombstones (
    transfer_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    tombstoned_at INTEGER NOT NULL,
    purge_after INTEGER NOT NULL
);
CREATE INDEX idx_mca_tombstones_purge_after ON mca_tombstones(purge_after);
"""

_MIGRATION_0001_DOWN = """
DROP TABLE IF EXISTS mca_tombstones;
DROP TABLE IF EXISTS mca_jobs;
DROP TABLE IF EXISTS mca_provider_profiles;
DROP TABLE IF EXISTS mca_connector_profiles;
DROP TABLE IF EXISTS mca_recipient_bindings;
DROP TABLE IF EXISTS mca_contacts;
DROP TABLE IF EXISTS attachment_events;
DROP TABLE IF EXISTS attachment_deliveries;
DROP TABLE IF EXISTS attachment_recipients;
DROP TABLE IF EXISTS attachments;
"""


_MIGRATION_0002_UP = """
ALTER TABLE mca_provider_profiles ADD COLUMN display_name TEXT NOT NULL DEFAULT '';
ALTER TABLE mca_provider_profiles ADD COLUMN tls_required INTEGER NOT NULL DEFAULT 1;
ALTER TABLE mca_provider_profiles ADD COLUMN upload_allowed INTEGER NOT NULL DEFAULT 1;
ALTER TABLE mca_provider_profiles ADD COLUMN download_allowed INTEGER NOT NULL DEFAULT 1;
"""

# DROP COLUMN requires SQLite >= 3.35 (2021). Confirmed available in this
# dev environment (3.37.2) and expected on any Raspberry Pi OS Bookworm+
# target, but not yet confirmed against the actual `dev`/`prod` Pi Zero 2 W
# Python/SQLite build - flag for Step 1.9 hardware acceptance testing if
# migrations are ever exercised there directly.
_MIGRATION_0002_DOWN = """
ALTER TABLE mca_provider_profiles DROP COLUMN display_name;
ALTER TABLE mca_provider_profiles DROP COLUMN tls_required;
ALTER TABLE mca_provider_profiles DROP COLUMN upload_allowed;
ALTER TABLE mca_provider_profiles DROP COLUMN download_allowed;
"""


# ADR-0005: the real deployed Relay's `service_public_key` (and, by the
# same convention, provider_id) is Base64URL text, not hex - the Step
# 0.6/0.7 column name was written before that was confirmed. RENAME COLUMN
# requires SQLite >= 3.25 (2018), well below the >= 3.35 already required
# by migration 0002's DROP COLUMN.
_MIGRATION_0003_UP = """
ALTER TABLE mca_provider_profiles RENAME COLUMN service_public_key_hex TO service_public_key_b64url;
"""

_MIGRATION_0003_DOWN = """
ALTER TABLE mca_provider_profiles RENAME COLUMN service_public_key_b64url TO service_public_key_hex;
"""


# Execution Plan Step 1.2 (MCA principal + Meshtastic address binding):
# one workspace has exactly one MCA principal (design spec section 7.1,
# "для первой реализации используется один MCA principal на MCA
# workspace") - `mca_principal` is therefore a single-row-per-workspace
# table, not a list. `principal_id` is the stable, never-changing
# identifier the workspace directory itself is keyed by
# (`MCAWorkspaceManager`/ADR-0003) - it is set once, at creation, to the
# genesis epoch's `key_id`, and does NOT change on a future key rotation
# (spec section 7.5), even though `key_id`/`public_identity`/`epoch` do.
# That distinction has no rotation logic to exercise yet (out of scope
# for Step 1.2 - see ADR-0001 section 6 and spec 7.5) but the column
# split is here now so a later rotation step is an UPDATE, not a schema
# change. Private key material itself is never stored in this table or
# any other DB row (ADR-0003: private keys live under `keys/`, 0700) -
# `private_key_file` is only the filename (not full path) of the raw
# 32-byte Ed25519 seed file within that principal's own `keys/` dir.
_MIGRATION_0004_UP = """
CREATE TABLE mca_principal (
    workspace_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    public_identity TEXT NOT NULL,
    public_x25519 TEXT NOT NULL,
    private_key_file TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at INTEGER NOT NULL
);

CREATE TABLE mca_key_exchange_contact_state (
    workspace_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    source_address TEXT NOT NULL,
    last_announce_sent_at INTEGER,
    last_seen_sender_key_id TEXT,
    last_seen_key_epoch INTEGER,
    last_request_at INTEGER,
    PRIMARY KEY (workspace_id, adapter_id, source_address)
);

CREATE TABLE mca_key_exchange_quota (
    workspace_id TEXT PRIMARY KEY,
    window_start_at INTEGER NOT NULL,
    announces_sent INTEGER NOT NULL DEFAULT 0
);

-- Spec 7.3 last paragraph: "При неожиданной смене ключа... автоматическая
-- отправка блокируется" - an already-TOFU-confirmed binding must never be
-- silently overwritten by a later KEY_ANNOUNCE claiming a different
-- public_identity for the same transport_address. These three columns
-- hold that *candidate* replacement (mirroring the same not-yet-trusted
-- shape as a brand new binding) until an explicit user action promotes it
-- - see key_exchange.py's `accept_pending_key_change()`.
ALTER TABLE mca_recipient_bindings ADD COLUMN pending_public_identity TEXT;
ALTER TABLE mca_recipient_bindings ADD COLUMN pending_key_epoch INTEGER;
ALTER TABLE mca_recipient_bindings ADD COLUMN pending_detected_at INTEGER;
"""

_MIGRATION_0004_DOWN = """
ALTER TABLE mca_recipient_bindings DROP COLUMN pending_detected_at;
ALTER TABLE mca_recipient_bindings DROP COLUMN pending_key_epoch;
ALTER TABLE mca_recipient_bindings DROP COLUMN pending_public_identity;
DROP TABLE IF EXISTS mca_key_exchange_quota;
DROP TABLE IF EXISTS mca_key_exchange_contact_state;
DROP TABLE IF EXISTS mca_principal;
"""


# Execution Plan Step 1.4 (sender state machine and queue; ADR-0006). The
# sender-side pipeline (Encrypting -> QueuedUpload -> Uploading ->
# ReadyToSend) needs to survive a process restart at any point without
# re-deriving cryptographic material or double-sending. Two things must be
# persisted, and nothing else has to be, because everything else can be
# recomputed deterministically from what is:
#
# - `data_key`/`nonce_prefix`: chunk/header AEAD (crypto.py) is fully
#   deterministic given these plus the transfer_id and the original
#   plaintext file (still at `attachments.saved_path` for a 'sent'
#   attachment) - so ciphertext bytes and every chunk hash are
#   byte-for-byte reproducible on a retry without re-persisting the
#   ciphertext itself. Only the ~32+16 bytes of key material need saving,
#   not a second copy of the file.
# - `manifest_blob`: NOT reproducible on retry, unlike the chunks - it
#   embeds one `nacl.public.SealedBox` ciphertext per recipient, and
#   SealedBox is randomized (a fresh ephemeral key each call). If
#   `create_upload()` has already pinned a `manifest_sha256` and the
#   process then crashes before `upload_manifest()`, recomputing the
#   manifest blob from scratch on resume would produce different bytes
#   with a different hash, permanently breaking that upload session. The
#   manifest blob is therefore built exactly once (Encrypting) and stored
#   verbatim - it is capped at the Relay's own `max_manifest_bytes`
#   (256 KiB in the mock/real Relay's defaults), small enough for a
#   BLOB column, so no separate staging file is needed.
#
# `upload_id`/`upload_token`/`revoke_token` are the live Relay upload
# session handle, persisted the moment `create_upload()` returns them so a
# crash immediately after can resume via `get_upload_status()` instead of
# re-calling `create_upload()` (which the real Relay rejects with 409
# `transfer_exists` for a `transfer_id` it has already seen - sender.py's
# recovery path treats that specific conflict, when no local `upload_id`
# survived, as a lost/orphaned session: the old `transfer_id` is
# tombstoned via the already-existing `mca_tombstones` table and a fresh
# one is generated, since nothing was ever committed under it).
#
# One row per in-flight *sent* attachment; deleted once the attachment
# reaches a terminal state (Sent/Received/Downloaded/Expired/Revoked/
# Cancelled/FAILED_*) - this table only ever holds transient state for
# work still in progress, mirroring `mca_jobs`' own "queue, not ledger"
# character.
_MIGRATION_0005_UP = """
CREATE TABLE mca_sender_state (
    attachment_id TEXT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
    data_key TEXT NOT NULL,
    nonce_prefix TEXT NOT NULL,
    manifest_blob BLOB,
    manifest_sha256 TEXT,
    upload_id TEXT,
    upload_token TEXT,
    revoke_token TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

_MIGRATION_0005_DOWN = """
DROP TABLE IF EXISTS mca_sender_state;
"""


# Execution Plan Step 1.5 (receiver state machine; ADR-0007). The mirror
# image of migration 5's `mca_sender_state`: the receiver's own two
# pieces of mid-flight secret material that are NOT re-derivable from
# only the plaintext file on disk (there is none yet, on this side) and
# so must be persisted the moment they become known, so a restart between
# `WaitingConsent -> Downloading` and `Verifying -> Available` does not
# need to re-open the sealed envelope from a re-fetched manifest blob
# every time (though - ADR-0007 decision 6 - it is also always SAFE to
# just redo the network calls and re-derive this row from scratch if it's
# missing, since `Downloading` never does genuine chunk-level resume in
# this pass):
#
# - `data_key`/`nonce_prefix`/`receipt_secret`: opened once from this
#   principal's own sealed envelope (`manifest.open_recipient_secret()`,
#   randomized-at-seal-time so the *envelope* itself isn't
#   re-derivable, but once opened these three values are plain fixed
#   bytes worth keeping around rather than re-opening the envelope on
#   every retry).
# - `chunk_count`/`plain_size`: independently derived from the
#   already-signature-verified Relay descriptor's ciphertext chunk sizes
#   (ADR-0007 decision 2) - cheap to recompute, but storing them avoids
#   a second `get_descriptor()` round-trip purely to redo arithmetic that
#   was already done once this attachment's `Downloading` state was first
#   entered.
#
# One row per in-flight *received* attachment; deleted once the
# attachment reaches a terminal state (Available/Expired/Rejected/Failed)
# - same "queue, not ledger" lifetime as `mca_sender_state`.
_MIGRATION_0006_UP = """
CREATE TABLE mca_receiver_state (
    attachment_id TEXT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
    data_key TEXT NOT NULL,
    nonce_prefix TEXT NOT NULL,
    receipt_secret TEXT NOT NULL,
    chunk_count INTEGER NOT NULL,
    plain_size INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- design spec 15.2's WaitingKey -> WaitingProvider/WaitingNetwork/
-- WaitingConsent transition is drawn as automatic ("ключ получен") once a
-- binding for the sender's key_id appears - it does NOT require the
-- sender to re-deliver the OFFER over radio a second time. That means
-- this workspace must keep enough of the original OFFER around, from the
-- moment it lands in WAITING_KEY, to finish verifying it later without
-- redelivery: the full canonical CBOR bytes (~122 bytes - ADR-0001
-- section 4 - cheap to keep for the handful of pending-unknown-sender
-- offers this MVP will ever have outstanding at once). Cleared back to
-- NULL the moment the signature is successfully re-verified (receiver.py
-- never needs it again after that point - it is not re-checked on every
-- subsequent run_step() call, only once, at the WAITING_KEY -> * edge).
ALTER TABLE attachments ADD COLUMN pending_offer_cbor BLOB;
"""

_MIGRATION_0006_DOWN = """
ALTER TABLE attachments DROP COLUMN pending_offer_cbor;
DROP TABLE IF EXISTS mca_receiver_state;
"""


_MIGRATION_0007_UP = """
-- Fixes a real gap: create_draft()'s `comment` parameter (design spec
-- 17.4 point 5, "Комментарий") was accepted but silently dropped -
-- _step_encrypting() hard-coded ManifestHeader(comment=None) regardless
-- of what the caller passed. A comment must survive an arbitrary restart
-- between create_draft() and the ENCRYPTING step actually running (same
-- crash-recovery guarantee as every other draft field), so it cannot
-- live only in a local Python variable - it needs a persisted column,
-- exactly like `file_name`/`mime_type` already do. Nullable and plaintext
-- at rest (this workspace already stores the plaintext source file at
-- `saved_path` for the same DRAFT/VALIDATING/ENCRYPTING window, so this
-- is not a new trust boundary) - cleared back to NULL by
-- _step_encrypting() once the encrypted manifest has been built, since
-- nothing needs the plaintext copy again after that point and the Relay
-- itself only ever receives the already-encrypted manifest blob.
ALTER TABLE attachments ADD COLUMN draft_comment TEXT;
"""

_MIGRATION_0007_DOWN = """
ALTER TABLE attachments DROP COLUMN draft_comment;
"""


# ADR-0008 (Step 1.6A backend layer): extends the provider profile model
# for real Settings/worker use (kind/enabled/limits/health-cache columns)
# and fixes a real bug - register(..., is_default=True) never cleared
# is_default on any other row in the workspace, so two rows could end up
# with is_default=1 and get_default()'s unordered `LIMIT 1` would pick
# between them non-deterministically. The partial unique index below
# makes "at most one default per workspace" a schema-enforced invariant;
# ProviderRegistry.set_default() is the only sanctioned way to change it.
_MIGRATION_0008_UP = """
ALTER TABLE mca_provider_profiles ADD COLUMN kind TEXT NOT NULL DEFAULT 'own';
ALTER TABLE mca_provider_profiles ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE mca_provider_profiles ADD COLUMN min_ttl_seconds INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN max_ttl_seconds INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN protocol_version TEXT;
ALTER TABLE mca_provider_profiles ADD COLUMN upload_token_file TEXT;
ALTER TABLE mca_provider_profiles ADD COLUMN last_checked_at INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN last_check_result TEXT;
ALTER TABLE mca_provider_profiles ADD COLUMN last_latency_ms INTEGER;
ALTER TABLE mca_provider_profiles ADD COLUMN last_error_code TEXT;

-- Deterministic cleanup BEFORE the unique index below: this migration's
-- own comment (see the module-level ADR-0008 note above) already admits
-- register(..., is_default=True) never cleared is_default on any other
-- row in the workspace, so a real install that hit that bug can have two
-- or more is_default=1 rows in the same workspace today. Creating a
-- partial UNIQUE index over that column without first collapsing such a
-- row set would fail the migration outright (sqlite3.IntegrityError) on
-- exactly the installs this ADR is trying to fix. Keep the earliest-added
-- default per workspace (added_at, provider_id as a deterministic
-- tiebreaker so two rows with the same added_at don't leave the choice to
-- SQLite's own unspecified row order); clear every other is_default=1 row
-- in that same workspace. A workspace with only one is_default=1 row (the
-- overwhelming common case) matches its own subquery result and this
-- UPDATE touches zero rows for it.
UPDATE mca_provider_profiles
SET is_default = 0
WHERE is_default = 1
  AND provider_id != (
      SELECT p2.provider_id FROM mca_provider_profiles AS p2
      WHERE p2.workspace_id = mca_provider_profiles.workspace_id AND p2.is_default = 1
      ORDER BY p2.added_at ASC, p2.provider_id ASC
      LIMIT 1
  );

CREATE UNIQUE INDEX idx_mca_provider_profiles_one_default
    ON mca_provider_profiles(workspace_id) WHERE is_default = 1;
"""

_MIGRATION_0008_DOWN = """
DROP INDEX IF EXISTS idx_mca_provider_profiles_one_default;
ALTER TABLE mca_provider_profiles DROP COLUMN last_error_code;
ALTER TABLE mca_provider_profiles DROP COLUMN last_latency_ms;
ALTER TABLE mca_provider_profiles DROP COLUMN last_check_result;
ALTER TABLE mca_provider_profiles DROP COLUMN last_checked_at;
ALTER TABLE mca_provider_profiles DROP COLUMN upload_token_file;
ALTER TABLE mca_provider_profiles DROP COLUMN protocol_version;
ALTER TABLE mca_provider_profiles DROP COLUMN max_ttl_seconds;
ALTER TABLE mca_provider_profiles DROP COLUMN min_ttl_seconds;
ALTER TABLE mca_provider_profiles DROP COLUMN enabled;
ALTER TABLE mca_provider_profiles DROP COLUMN kind;
"""


_MIGRATION_0009_UP = ""  # data-only migration - see the fixup functions below

_MIGRATION_0009_DOWN = ""  # data-only migration - see the fixup functions below

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")


def _migration_0009_fixup_sent_provider_id_encoding(conn: sqlite3.Connection) -> None:
    """Reviewer-found defect, reproduced locally: `sender.create_draft()`
    stored `attachments.provider_id` as hex for 'sent' rows while
    `receiver.py` already stored it as Base64URL (matching
    `ProviderRegistry`'s own key format) for 'received' rows -
    `ProviderRegistry.remove_or_disable()`'s "is this provider_id still
    referenced by any attachment?" check compares against the Base64URL
    form, so it NEVER matched a 'sent' row and could delete a Relay
    profile a real outgoing attachment still depended on. `sender.py`'s
    write/read sites are now fixed to use Base64URL like everything else
    (this ADR-0008-hardening pass); this one-time fixup re-encodes any
    row a pre-fix process already wrote as hex, on any real install that
    already sent an attachment before this fix existed (dev/prod both
    did, during Step 1.3/1.4's hardware tests).

    Deliberately conservative: only touches a value that looks exactly
    like the old hex encoding (16 lowercase hex chars, matching an 8-byte
    provider_id) - anything else (already Base64URL, NULL, or some
    unexpected value) is left untouched rather than guessed at."""
    from meshsrv.attachments.provider_registry import encode_provider_id

    rows = conn.execute(
        "SELECT id, provider_id FROM attachments WHERE direction = 'sent' AND provider_id IS NOT NULL"
    ).fetchall()
    for attachment_id, provider_id_value in rows:
        if not _HEX16_RE.match(provider_id_value or ""):
            continue
        raw = bytes.fromhex(provider_id_value)
        conn.execute(
            "UPDATE attachments SET provider_id = ? WHERE id = ?",
            (encode_provider_id(raw), attachment_id),
        )


def _migration_0009_down_fixup_sent_provider_id_encoding(conn: sqlite3.Connection) -> None:
    """Inverse of the above, for symmetry with every other migration's
    down path - converts a 'sent' row's provider_id back to hex, the
    encoding a downgraded `sender.py` (pre-this-fix) would expect."""
    from meshsrv.attachments.provider_registry import ProviderRegistryError, decode_provider_id

    rows = conn.execute(
        "SELECT id, provider_id FROM attachments WHERE direction = 'sent' AND provider_id IS NOT NULL"
    ).fetchall()
    for attachment_id, provider_id_value in rows:
        try:
            raw = decode_provider_id(provider_id_value)
        except ProviderRegistryError:
            continue  # already hex (or unexpected) - never touch
        conn.execute(
            "UPDATE attachments SET provider_id = ? WHERE id = ?",
            (raw.hex(), attachment_id),
        )


# Execution Plan Step 1.6A-hardening (PR #227 defect #1). Nothing in
# receiver.py's own ACK generation ever persisted a real queue: the
# encoded ACK_RECEIVED/ACK_PROVIDER_UNKNOWN/ACK_DOWNLOADED frames it built
# were simply returned up the call stack as `ReceiveResult.replies`, and
# both real callers (mca_runtime.py's OFFER branch, AttachmentsService.
# _step_received()) discarded that return value entirely - no ACK was
# ever actually transmitted in production. Separately, the "has this
# already been sent" bookkeeping (`attachment_events`) was written
# *before* any transmission was even attempted (a commit-before-send
# ordering bug, wrong even if a caller HAD been sending the return value)
# - a crash or a failed send between that commit and the real wire send
# would have permanently and silently lost the ACK.
#
# `mca_outgoing_replies` is a real, persisted, retryable queue: a row is
# inserted (state=PENDING) the moment receiver.py decides an ACK is due
# (still at-most-once per (attachment_id, event_type), same as before),
# and is only ever marked SENT by AttachmentsService's own dispatch step,
# after `DeliveryAdapter.send()` actually succeeds - never by the code
# that builds the frame. A failed send bumps `attempts`/`next_attempt_at`
# (exponential backoff, same shape as ConnectivityMonitor's own Relay
# health backoff) rather than being lost. `attachments.reply_route_type`/
# `reply_route_id` (added on the same attachments row a 'received'
# attachment already has - never used for 'sent' rows) is where the
# return route is captured, once, at handle_offer() time from the
# inbound envelope's own source address - the only place that
# information is available at all today.
_MIGRATION_0010_UP = """
ALTER TABLE attachments ADD COLUMN reply_route_type TEXT;
ALTER TABLE attachments ADD COLUMN reply_route_id TEXT;
-- PR #231 review, section 4.2: a reply route must be reconstructable
-- after a restart without assuming "the" one global Meshtastic adapter
-- - adapter_id/connector_profile_id/destination_address are persisted
-- alongside route_type/route_id (source_address is not a separate
-- column: for the DIRECT-only MVP, reply_route_id already *is* the
-- sender's source_address - see receiver.handle_offer()'s own comment
-- on why "DIRECT" is the only route shape an OFFER can arrive over).
ALTER TABLE attachments ADD COLUMN reply_adapter_id TEXT;
ALTER TABLE attachments ADD COLUMN reply_connector_profile_id TEXT;
ALTER TABLE attachments ADD COLUMN reply_destination_address TEXT;

CREATE TABLE mca_outgoing_replies (
    id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    message BLOB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING', 'SENT', 'UNDELIVERABLE')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    next_attempt_at INTEGER NOT NULL,
    sent_at INTEGER,
    last_error TEXT,
    UNIQUE (attachment_id, event_type)
);
CREATE INDEX idx_mca_outgoing_replies_due ON mca_outgoing_replies(state, next_attempt_at);

-- PR #231 review, section 4.3: real, persisted ACK rate limiting - not
-- just "N rows per dispatch call" (which only ever bounded one SQL
-- query, never the actual send rate). One row per (workspace_id,
-- scope) - scope is either the literal string '__global__' or a
-- specific source_address - tracking a fixed window's start time and
-- how many replies have been sent/attempted in it. Persisted (not
-- in-memory) so a restart never resets the count mid-window.
CREATE TABLE mca_ack_quota (
    workspace_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    window_start_at INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, scope)
);
"""

_MIGRATION_0010_DOWN = """
DROP TABLE IF EXISTS mca_ack_quota;
DROP TABLE IF EXISTS mca_outgoing_replies;
ALTER TABLE attachments DROP COLUMN reply_destination_address;
ALTER TABLE attachments DROP COLUMN reply_connector_profile_id;
ALTER TABLE attachments DROP COLUMN reply_adapter_id;
ALTER TABLE attachments DROP COLUMN reply_route_id;
ALTER TABLE attachments DROP COLUMN reply_route_type;
"""


# ADR-0008 / Step 1.6A.1 (docs/attachments/internal-rest-api.md §3.5/§3.6):
# idempotent job creation by client_request_id. `POST /api/attachments` is a
# deferred worker command, so two concurrent Flask threads could both observe
# "this client_request_id is absent" and both enqueue a create before the
# worker publishes a fresh idempotency snapshot. The in-memory reservation
# (§3.6) closes that race for the common case; these two columns persist the
# outcome so an identical replay *after* the command has committed can be
# answered deterministically (200 + the original attachment_id) rather than
# re-creating a duplicate - and so the committed idempotency index can be
# rebuilt from disk after a restart (§3.6 "restart rebuild").
#
# - `client_request_id` is the client-chosen idempotency key, validated
#   `[A-Za-z0-9_-]{1,64}` on the request thread before it ever reaches here.
# - `canonical_hash` is the versioned SHA-256 over the full semantic field
#   set (§3.5) - stored so a replay of the *same* client_request_id with
#   *different* content is distinguishable from a true identical replay and
#   rejected as 409 idempotency_conflict, not silently deduplicated.
# - The partial unique index is the final database-level backstop: at most
#   one non-NULL client_request_id per workspace. NULL (legacy rows created
#   before this migration, and any future non-idempotent path) is exempt, so
#   pre-existing rows are not forced to invent a value on upgrade. SQLite
#   partial indexes (>= 3.8.0) are already well below the >= 3.35 floor
#   established by migration 2's DROP COLUMN.
#
# Both columns are written by sender.create_draft() on the worker thread (the
# single owner of `conn`) - never by a request thread - so no concurrent-write
# hazard is introduced; the index only ever defends against a race that
# slipped past the in-memory reservation, which is exactly its purpose.
_MIGRATION_0011_UP = """
ALTER TABLE attachments ADD COLUMN client_request_id TEXT;
ALTER TABLE attachments ADD COLUMN canonical_hash TEXT;
CREATE UNIQUE INDEX idx_attachments_client_request_id
    ON attachments(workspace_id, client_request_id) WHERE client_request_id IS NOT NULL;
"""

_MIGRATION_0011_DOWN = """
DROP INDEX IF EXISTS idx_attachments_client_request_id;
ALTER TABLE attachments DROP COLUMN canonical_hash;
ALTER TABLE attachments DROP COLUMN client_request_id;
"""


# Execution Plan Step 1.6A.1 (snapshot-dirty tracking; ADR-0008 §3.3).
# The attachment snapshot (internal-rest-api.md §3.3) projects exactly four
# tables - `attachments`, `attachment_recipients`, `attachment_deliveries`,
# `attachment_events` - and nothing else. The publisher's change-detection
# originally used `conn.total_changes`, but that is *connection-global* and
# is advanced every tick by writes the snapshot does NOT project: ACK-quota
# bookkeeping (`mca_ack_quota`), Relay health (`mca_provider_profiles`),
# reply outbox (`mca_outgoing_replies`), and sender/receiver mid-flight
# state (`mca_sender_state`/`mca_receiver_state`/`mca_tombstones`). Under an
# active-write workload that made `refresh()` rebuild the whole O(N) snapshot
# on every single tick - the exact cost the incremental design was built to
# eliminate (see the Step 1.6A.1 active-write benchmark, §15.2).
#
# `mca_dirty_attachments` is the explicit, narrowly-scoped snapshot-dirty
# set: AFTER INSERT/UPDATE/DELETE triggers on the four projected tables
# *only* record the affected `attachment_id` (with `INSERT OR IGNORE`, so a
# burst of writes to one attachment coalesces to a single dirty row). The
# publisher drains and deduplicates those ids and rebuilds *only* the
# affected projection + bounded timeline (see snapshots.py) - never the
# whole O(N) snapshot. Because a trigger's INSERT runs inside the same
# transaction as the statement that fired it, the dirty row is transactional
# (a rollback rolls it back too - no spurious rebuild, and a committed
# relevant write can never be missed) and database-global (it survives a
# connection close/recreate, unlike `conn.total_changes`, which is
# per-connection and resets to 0).
#
# On UPDATE the trigger dirties *both* `NEW` and `OLD` ids: a row's id (or,
# for a child table, its `attachment_id`) may itself be the thing that
# changed, so neither the old nor the new attachment's projection may be
# left stale. On DELETE only `OLD` is dirtied (a deleted attachment's
# projection is removed, not rebuilt).
#
# The triggers are created in `data_fixup` (not `up_sql`) because their
# bodies use `BEGIN ... END;`, which this module's own
# `_split_sql_statements()` cannot split (see its docstring) - exactly the
# "transformation plain SQL can't express" case the data_fixup hook exists
# for.
_DIRTY_TABLES = ("attachments", "attachment_recipients", "attachment_deliveries", "attachment_events")
_DIRTY_EVENTS = ("INSERT", "UPDATE", "DELETE")


def _dirty_trigger_names():
    return tuple(
        f"trg_{table}_snapshot_dirty_{event.lower()}"
        for table in _DIRTY_TABLES
        for event in _DIRTY_EVENTS
    )


def _dirty_id_column(table: str) -> str:
    """The column carrying the affected attachment's id for a projected
    table: the `id` primary key on `attachments` itself, the `attachment_id`
    foreign key on each child table."""
    return "id" if table == "attachments" else "attachment_id"


def _migration_0012_create_dirty_triggers(conn: sqlite3.Connection) -> None:
    """Create one AFTER trigger per (table, event) that records the affected
    `attachment_id` in `mca_dirty_attachments`. Runs inside migration 12's own
    transaction (via `migrate()`'s data_fixup hook), so the triggers and the
    table commit atomically - a failure here rolls the whole migration back."""
    for table in _DIRTY_TABLES:
        column = _dirty_id_column(table)
        for event in _DIRTY_EVENTS:
            if event == "INSERT":
                refs = (f"NEW.{column}",)
            elif event == "UPDATE":
                refs = (f"NEW.{column}", f"OLD.{column}")
            else:  # DELETE
                refs = (f"OLD.{column}",)
            body = " ".join(
                f"INSERT OR IGNORE INTO mca_dirty_attachments (attachment_id) VALUES ({ref});"
                for ref in refs
            )
            conn.execute(
                f"CREATE TRIGGER trg_{table}_snapshot_dirty_{event.lower()} "
                f"AFTER {event} ON {table} BEGIN {body} END"
            )


_MIGRATION_0012_UP = """
CREATE TABLE mca_dirty_attachments (
    attachment_id TEXT PRIMARY KEY
);
"""

_MIGRATION_0012_DOWN = "\n".join(
    f"DROP TRIGGER IF EXISTS {name};" for name in _dirty_trigger_names()
) + "\nDROP TABLE IF EXISTS mca_dirty_attachments;"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_sql: str
    down_sql: str
    # Optional Python step run after up_sql/down_sql's executescript() -
    # for a transformation plain SQL can't express (Migration 9: re-
    # encoding a column's text using this codebase's own Base64URL
    # helper). None for every migration that is pure schema DDL.
    data_fixup: Optional[Callable[[sqlite3.Connection], None]] = None
    down_data_fixup: Optional[Callable[[sqlite3.Connection], None]] = None


MIGRATIONS: Sequence[Migration] = (
    Migration(1, "create_core_tables", _MIGRATION_0001_UP, _MIGRATION_0001_DOWN),
    Migration(2, "extend_provider_profiles", _MIGRATION_0002_UP, _MIGRATION_0002_DOWN),
    Migration(3, "rename_provider_public_key_to_b64url", _MIGRATION_0003_UP, _MIGRATION_0003_DOWN),
    Migration(4, "mca_principal_and_key_exchange_state", _MIGRATION_0004_UP, _MIGRATION_0004_DOWN),
    Migration(5, "mca_sender_state", _MIGRATION_0005_UP, _MIGRATION_0005_DOWN),
    Migration(6, "mca_receiver_state", _MIGRATION_0006_UP, _MIGRATION_0006_DOWN),
    Migration(7, "attachments_draft_comment", _MIGRATION_0007_UP, _MIGRATION_0007_DOWN),
    Migration(8, "provider_profiles_v2", _MIGRATION_0008_UP, _MIGRATION_0008_DOWN),
    Migration(
        9, "unify_sent_provider_id_encoding", _MIGRATION_0009_UP, _MIGRATION_0009_DOWN,
        data_fixup=_migration_0009_fixup_sent_provider_id_encoding,
        down_data_fixup=_migration_0009_down_fixup_sent_provider_id_encoding,
    ),
    Migration(10, "receiver_ack_outbox", _MIGRATION_0010_UP, _MIGRATION_0010_DOWN),
    Migration(11, "attachments_idempotency", _MIGRATION_0011_UP, _MIGRATION_0011_DOWN),
    Migration(
        12, "snapshot_dirty_tracking", _MIGRATION_0012_UP, _MIGRATION_0012_DOWN,
        data_fixup=_migration_0012_create_dirty_triggers,
    ),
)

LATEST_VERSION: int = MIGRATIONS[-1].version if MIGRATIONS else 0

ALL_TABLE_NAMES = frozenset(
    {
        "attachments",
        "attachment_recipients",
        "attachment_deliveries",
        "attachment_events",
        "mca_contacts",
        "mca_recipient_bindings",
        "mca_connector_profiles",
        "mca_provider_profiles",
        "mca_jobs",
        "mca_tombstones",
        "mca_principal",
        "mca_key_exchange_contact_state",
        "mca_key_exchange_quota",
        "mca_sender_state",
        "mca_receiver_state",
        "mca_outgoing_replies",
        # PR #231 review (2nd pass): was missing here - added by the same
        # Migration 10 extension as mca_outgoing_replies above, but never
        # added to this completeness set, so every existing
        # ALL_TABLE_NAMES.issubset(...) test silently never verified this
        # table's presence after a full migration run.
        "mca_ack_quota",
        # Migration 12 (Step 1.6A.1): the snapshot-dirty set, populated by
        # AFTER triggers on the four projected tables (one row per affected
        # attachment_id, deduplicated by INSERT OR IGNORE).
        "mca_dirty_attachments",
    }
)


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


# Matches a `--` comment (through end of line) so _split_sql_statements()
# can strip it before splitting on `;` - none of this module's own SQL
# blocks use any other comment style, string literal containing a
# semicolon, or multi-statement construct (trigger/procedure body) that
# would need a smarter splitter than this.
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _split_sql_statements(script: str) -> list:
    """Split one migration's SQL block into individual statements, each
    to be run through `conn.execute()` rather than `conn.executescript()`.

    This exists because of a real, empirically-confirmed atomicity gap:
    `sqlite3.Connection.executescript()` does not honor an explicit outer
    `BEGIN`/`ROLLBACK` on this project's Python build (3.13/3.14) -
    reproduced directly: wrapping `executescript()` in `conn.execute("BEGIN")`
    / `except: conn.rollback()` still left earlier statements in the script
    committed after a later statement raised. Running each statement
    through `conn.execute()` individually inside the same explicit
    transaction *does* roll back correctly (also reproduced directly)
    - see `migrate()`'s own docstring for what this means for callers,
    and tests/test_mca_db_migrations.py::test_failed_migration_rolls_back_
    completely for the regression test this fix is verified against."""
    without_comments = _SQL_LINE_COMMENT_RE.sub("", script)
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


def migrate(conn: sqlite3.Connection, target_version: Optional[int] = None) -> None:
    """Bring `conn`'s schema to `target_version` (latest, by default),
    applying up migrations if behind or down migrations if ahead. Safe to
    call repeatedly - a `conn` already at `target_version` is a no-op.

    `PRAGMA user_version` cannot use parameter binding; the integers
    interpolated below always come from this module's own `MIGRATIONS`
    list, never from external input.

    Atomicity (fixed per PR #231 review): each migration's DDL/DML *and*
    its own `PRAGMA user_version` update now run inside one explicit
    `BEGIN`/`COMMIT` transaction, executed statement-by-statement via
    `conn.execute()` (see `_split_sql_statements()`'s docstring for why
    `executescript()` cannot be used here). If any statement or
    `data_fixup`/`down_data_fixup` callable raises, the whole migration
    (including everything already run earlier in the *same* migration)
    is rolled back and `user_version` is left exactly where it was before
    this call started - never left pointing at a partially-applied
    schema. A multi-migration `migrate()` call (e.g. version 7 -> 10)
    commits one migration at a time, so a failure partway through still
    leaves every *earlier* migration in that run fully applied and
    committed - only the migration that actually failed (and anything
    after it) is rolled back/not attempted.
    """
    if target_version is None:
        target_version = LATEST_VERSION
    current = current_version(conn)
    if target_version == current:
        return
    if target_version > current:
        for migration in MIGRATIONS:
            if current < migration.version <= target_version:
                try:
                    conn.execute("BEGIN")
                    for stmt in _split_sql_statements(migration.up_sql):
                        conn.execute(stmt)
                    if migration.data_fixup is not None:
                        migration.data_fixup(conn)
                    conn.execute(f"PRAGMA user_version = {migration.version}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                current = migration.version
    else:
        for migration in reversed(MIGRATIONS):
            if target_version < migration.version <= current:
                try:
                    conn.execute("BEGIN")
                    if migration.down_data_fixup is not None:
                        migration.down_data_fixup(conn)
                    for stmt in _split_sql_statements(migration.down_sql):
                        conn.execute(stmt)
                    conn.execute(f"PRAGMA user_version = {migration.version - 1}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                current = migration.version - 1


def open_attachments_db(path) -> sqlite3.Connection:
    """Open (creating if needed) an `attachments.db` at `path` and migrate
    it to the latest schema. Foreign keys are off by default in SQLite and
    must be turned on per-connection."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
