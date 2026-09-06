"""tests/test_mca_db_migrations.py

Migration and tombstone/cleanup tests for `attachments.db` (Execution Plan
Step 0.6 DoD: "миграции применяются на чистой БД и откатываются"; design
spec section 23.1 "Storage": cleanup/tombstones).
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from meshsrv.attachments.db.migrations import (
    ALL_TABLE_NAMES,
    LATEST_VERSION,
    current_version,
    migrate,
    open_attachments_db,
)
from meshsrv.attachments.db.tombstones import (
    is_tombstoned,
    purge_expired_tombstones,
    record_tombstone,
)


def _table_names(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


def test_migrate_on_clean_db_creates_all_ten_tables(conn):
    assert current_version(conn) == 0
    migrate(conn)
    assert current_version(conn) == LATEST_VERSION
    assert ALL_TABLE_NAMES.issubset(_table_names(conn))


def test_migrate_is_idempotent(conn):
    migrate(conn)
    migrate(conn)  # must not raise ("table already exists")
    assert current_version(conn) == LATEST_VERSION


def test_migrate_rolls_back_to_zero(conn):
    migrate(conn)
    migrate(conn, target_version=0)
    assert current_version(conn) == 0
    assert ALL_TABLE_NAMES.isdisjoint(_table_names(conn))


def test_open_attachments_db_migrates_a_real_file(tmp_path):
    db_path = tmp_path / "attachments.db"
    conn = open_attachments_db(db_path)
    try:
        assert current_version(conn) == LATEST_VERSION
        assert ALL_TABLE_NAMES.issubset(_table_names(conn))
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
    assert db_path.exists()

    # Re-opening an already-migrated file must be a fast no-op, not a
    # re-run of CREATE TABLE against existing tables.
    conn_again = open_attachments_db(db_path)
    try:
        assert current_version(conn_again) == LATEST_VERSION
    finally:
        conn_again.close()


def test_attachments_transfer_id_is_unique_per_workspace(conn):
    migrate(conn)
    now = int(time.time())
    row = {
        "id": "att-1",
        "workspace_id": "ws-1",
        "transfer_id": "deadbeefdeadbeef",
        "direction": "sent",
        "principal_id": "0123456789abcdef",
        "state": "READY_TO_SEND",
        "created_at": now,
        "hard_expires_at": now + 3600,
        "download_grace_seconds": 3600,
    }
    conn.execute(
        """INSERT INTO attachments
           (id, workspace_id, transfer_id, direction, principal_id, state,
            created_at, hard_expires_at, download_grace_seconds)
           VALUES (:id, :workspace_id, :transfer_id, :direction, :principal_id, :state,
                   :created_at, :hard_expires_at, :download_grace_seconds)""",
        row,
    )
    conn.commit()
    duplicate = dict(row, id="att-2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO attachments
               (id, workspace_id, transfer_id, direction, principal_id, state,
                created_at, hard_expires_at, download_grace_seconds)
               VALUES (:id, :workspace_id, :transfer_id, :direction, :principal_id, :state,
                       :created_at, :hard_expires_at, :download_grace_seconds)""",
            duplicate,
        )
    # A different workspace may reuse the same transfer_id without conflict.
    other_workspace = dict(row, id="att-3", workspace_id="ws-2")
    conn.execute(
        """INSERT INTO attachments
           (id, workspace_id, transfer_id, direction, principal_id, state,
            created_at, hard_expires_at, download_grace_seconds)
           VALUES (:id, :workspace_id, :transfer_id, :direction, :principal_id, :state,
                   :created_at, :hard_expires_at, :download_grace_seconds)""",
        other_workspace,
    )
    conn.commit()


def test_attachment_deliveries_cascade_deletes_with_parent(conn):
    migrate(conn)
    now = int(time.time())
    conn.execute(
        """INSERT INTO attachments
           (id, workspace_id, transfer_id, direction, principal_id, state,
            created_at, hard_expires_at, download_grace_seconds)
           VALUES ('att-1', 'ws-1', 'deadbeefdeadbeef', 'sent', '0123456789abcdef', 'READY_TO_SEND',
                   ?, ?, 3600)""",
        (now, now + 3600),
    )
    conn.execute(
        """INSERT INTO attachment_deliveries
           (id, attachment_id, adapter_id, connector_profile_id, route_type, route_id,
            wire_format, idempotency_key, state)
           VALUES ('del-1', 'att-1', 'meshtastic', 'dev', 'DIRECT', '!abc12345',
                   'MCA1_TEXT', 'idem-1', 'queued')"""
    )
    conn.commit()
    conn.execute("DELETE FROM attachments WHERE id = 'att-1'")
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM attachment_deliveries").fetchone()[0]
    assert remaining == 0


# ---- tombstones (design spec section 23.1 "Storage": cleanup/tombstones) --


def test_tombstone_record_and_lookup(conn):
    migrate(conn)
    record_tombstone(conn, transfer_id="deadbeefdeadbeef", workspace_id="ws-1", reason="revoked")
    assert is_tombstoned(conn, "deadbeefdeadbeef") is True
    assert is_tombstoned(conn, "0000000000000000") is False


def test_tombstone_purge_respects_retention_window(conn):
    migrate(conn)
    now = 1_000_000.0
    record_tombstone(
        conn, transfer_id="short-lived", workspace_id="ws-1", reason="expired", now=now, retention_seconds=10
    )
    record_tombstone(
        conn, transfer_id="long-lived", workspace_id="ws-1", reason="revoked", now=now, retention_seconds=1_000_000
    )
    purged = purge_expired_tombstones(conn, now=now + 20)
    assert purged == 1
    assert is_tombstoned(conn, "short-lived") is False
    assert is_tombstoned(conn, "long-lived") is True


def test_re_tombstoning_extends_retention_rather_than_erroring(conn):
    migrate(conn)
    now = 1_000_000.0
    record_tombstone(conn, transfer_id="x", workspace_id="ws-1", reason="expired", now=now, retention_seconds=10)
    record_tombstone(conn, transfer_id="x", workspace_id="ws-1", reason="revoked", now=now, retention_seconds=1_000_000)
    purged = purge_expired_tombstones(conn, now=now + 20)
    assert purged == 0
    assert is_tombstoned(conn, "x") is True
