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

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

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


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_sql: str
    down_sql: str


MIGRATIONS: Sequence[Migration] = (
    Migration(1, "create_core_tables", _MIGRATION_0001_UP, _MIGRATION_0001_DOWN),
    Migration(2, "extend_provider_profiles", _MIGRATION_0002_UP, _MIGRATION_0002_DOWN),
    Migration(3, "rename_provider_public_key_to_b64url", _MIGRATION_0003_UP, _MIGRATION_0003_DOWN),
    Migration(4, "mca_principal_and_key_exchange_state", _MIGRATION_0004_UP, _MIGRATION_0004_DOWN),
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
    }
)


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def migrate(conn: sqlite3.Connection, target_version: Optional[int] = None) -> None:
    """Bring `conn`'s schema to `target_version` (latest, by default),
    applying up migrations if behind or down migrations if ahead. Safe to
    call repeatedly - a `conn` already at `target_version` is a no-op.

    `PRAGMA user_version` cannot use parameter binding; the integers
    interpolated below always come from this module's own `MIGRATIONS`
    list, never from external input.
    """
    if target_version is None:
        target_version = LATEST_VERSION
    current = current_version(conn)
    if target_version == current:
        return
    if target_version > current:
        for migration in MIGRATIONS:
            if current < migration.version <= target_version:
                conn.executescript(migration.up_sql)
                conn.execute(f"PRAGMA user_version = {migration.version}")
                current = migration.version
    else:
        for migration in reversed(MIGRATIONS):
            if target_version < migration.version <= current:
                conn.executescript(migration.down_sql)
                conn.execute(f"PRAGMA user_version = {migration.version - 1}")
                current = migration.version - 1
    conn.commit()


def open_attachments_db(path) -> sqlite3.Connection:
    """Open (creating if needed) an `attachments.db` at `path` and migrate
    it to the latest schema. Foreign keys are off by default in SQLite and
    must be turned on per-connection."""
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
