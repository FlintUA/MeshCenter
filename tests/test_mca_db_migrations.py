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


def test_migration_8_dedupes_multiple_is_default_rows_before_indexing(conn):
    """Regression test for a real reviewer-found defect: register(...,
    is_default=True)'s pre-ADR-0008 bug (documented directly above
    migration 8's own SQL) never cleared is_default on any other row in
    the workspace, so a real install could already have two or more
    is_default=1 rows in the same workspace by the time it upgrades to
    this migration. Creating a partial UNIQUE index over that column
    without first collapsing such a row set would fail the migration
    outright - this seeds exactly that pre-existing-bug state at
    migration 7 and proves migrating to 8 both succeeds and leaves
    exactly one is_default=1 row per workspace, chosen deterministically
    (earliest added_at)."""
    migrate(conn, target_version=7)
    conn.execute(
        """
        INSERT INTO mca_provider_profiles
            (provider_id, workspace_id, origin, service_public_key_b64url,
             max_ciphertext_bytes, hard_expiry_default_seconds, is_default, added_at)
        VALUES
            ('provider-older', 'local', 'https://a.example', 'aa', 1000, 3600, 1, 100),
            ('provider-newer', 'local', 'https://b.example', 'bb', 1000, 3600, 1, 200),
            ('provider-other-workspace', 'other', 'https://c.example', 'cc', 1000, 3600, 1, 50)
        """
    )
    conn.commit()

    migrate(conn, target_version=8)
    assert current_version(conn) == 8

    local_defaults = conn.execute(
        "SELECT provider_id FROM mca_provider_profiles WHERE workspace_id = 'local' AND is_default = 1"
    ).fetchall()
    assert [row[0] for row in local_defaults] == ["provider-older"]

    # The other workspace's own single default row must be untouched -
    # this cleanup is scoped per-workspace, not global.
    other_defaults = conn.execute(
        "SELECT provider_id FROM mca_provider_profiles WHERE workspace_id = 'other' AND is_default = 1"
    ).fetchall()
    assert [row[0] for row in other_defaults] == ["provider-other-workspace"]

    # The unique index is real and enforced going forward, not just
    # created over already-clean data by coincidence.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO mca_provider_profiles
                (provider_id, workspace_id, origin, service_public_key_b64url,
                 max_ciphertext_bytes, hard_expiry_default_seconds, is_default, added_at)
            VALUES ('provider-third', 'local', 'https://d.example', 'dd', 1000, 3600, 1, 300)
            """
        )


def test_migration_8_is_a_noop_for_a_workspace_with_only_one_default(conn):
    """The common case - already exactly one is_default=1 row per
    workspace - must not be touched by the new cleanup UPDATE."""
    migrate(conn, target_version=7)
    conn.execute(
        """
        INSERT INTO mca_provider_profiles
            (provider_id, workspace_id, origin, service_public_key_b64url,
             max_ciphertext_bytes, hard_expiry_default_seconds, is_default, added_at)
        VALUES ('provider-only', 'local', 'https://a.example', 'aa', 1000, 3600, 1, 100)
        """
    )
    conn.commit()

    migrate(conn, target_version=8)

    row = conn.execute(
        "SELECT is_default FROM mca_provider_profiles WHERE provider_id = 'provider-only'"
    ).fetchone()
    assert row[0] == 1


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


# --------------------------------------------------------------------------
# PR #231 review, section 11: migration atomicity and direct up/down/re-up
# coverage (not just "versions 7-10 appear in the correct order").
# --------------------------------------------------------------------------


def test_failed_migration_rolls_back_completely(conn, monkeypatch):
    """A migration that fails partway through must not leave the schema
    partially modified with the old user_version - this is the exact
    atomicity gap found in review: executescript() does not honor an
    explicit outer transaction on this project's Python/sqlite3 build,
    confirmed by direct reproduction (see migrations.py's own
    _split_sql_statements()/migrate() docstrings). This test injects a
    real failure via a monkeypatched, deliberately broken migration 8
    up_sql (references a table that does not exist) and asserts nothing
    from it survives."""
    import meshsrv.attachments.db.migrations as migrations_module

    migrate(conn, target_version=7)
    assert current_version(conn) == 7

    broken_migrations = list(migrations_module.MIGRATIONS)
    broken_index = next(i for i, m in enumerate(broken_migrations) if m.version == 8)
    original = broken_migrations[broken_index]
    broken_migrations[broken_index] = migrations_module.Migration(
        version=8,
        name=original.name,
        up_sql=(
            "ALTER TABLE mca_provider_profiles ADD COLUMN kind TEXT NOT NULL DEFAULT 'own';\n"
            "INSERT INTO this_table_does_not_exist (x) VALUES (1);\n"
        ),
        down_sql=original.down_sql,
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", tuple(broken_migrations))

    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, target_version=8)

    # user_version must still be 7 - not 8, and not left in some
    # in-between value - and the column the broken migration's first
    # (successful-if-run-alone) statement would have added must not
    # exist either, proving the whole migration rolled back together,
    # not just the statement that actually raised.
    assert current_version(conn) == 7
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mca_provider_profiles)").fetchall()}
    assert "kind" not in columns


def test_retry_after_injected_migration_failure_succeeds(conn, monkeypatch):
    """Directly exercises the DoD's "retrying after an injected migration
    failure must be safe" requirement: the same failure as above, but
    followed by a real migrate() call (unpatched) proving the database is
    left in a state where migration can simply be retried, not stuck."""
    import meshsrv.attachments.db.migrations as migrations_module

    migrate(conn, target_version=7)

    broken_migrations = list(migrations_module.MIGRATIONS)
    broken_index = next(i for i, m in enumerate(broken_migrations) if m.version == 8)
    original = broken_migrations[broken_index]
    broken_migrations[broken_index] = migrations_module.Migration(
        version=8, name=original.name,
        up_sql="INSERT INTO this_table_does_not_exist (x) VALUES (1);",
        down_sql=original.down_sql,
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", tuple(broken_migrations))
    with pytest.raises(sqlite3.OperationalError):
        migrate(conn, target_version=8)
    assert current_version(conn) == 7

    monkeypatch.undo()
    migrate(conn)  # retry with the real migration list
    assert current_version(conn) == LATEST_VERSION


@pytest.mark.parametrize("start_version", [7, 8, 9])
def test_upgrade_from_each_intermediate_version_to_latest(conn, start_version):
    migrate(conn, target_version=start_version)
    assert current_version(conn) == start_version
    migrate(conn)
    assert current_version(conn) == LATEST_VERSION
    assert ALL_TABLE_NAMES.issubset(_table_names(conn))


@pytest.mark.parametrize("target_version", [9, 8, 7])
def test_downgrade_from_latest_to_each_intermediate_version(conn, target_version):
    migrate(conn)
    migrate(conn, target_version=target_version)
    assert current_version(conn) == target_version


def test_re_upgrade_after_downgrade_reaches_latest_again(conn):
    migrate(conn)
    migrate(conn, target_version=7)
    assert current_version(conn) == 7
    migrate(conn)
    assert current_version(conn) == LATEST_VERSION
    assert ALL_TABLE_NAMES.issubset(_table_names(conn))


def test_migration_9_only_converts_legacy_hex_sent_provider_ids(conn):
    """Migration 9's data fixup must be conservative (its own docstring):
    only a 'sent' row whose provider_id looks exactly like the old
    16-hex-char encoding gets re-encoded; an already-Base64URL 'sent' row
    and every 'received' row (always Base64URL, ADR-0007) are left
    untouched."""
    from meshsrv.attachments.provider_registry import encode_provider_id

    migrate(conn, target_version=8)
    now = time.time()
    legacy_hex = bytes.fromhex("0123456789abcdef")
    already_b64url = encode_provider_id(bytes.fromhex("fedcba9876543210"))
    received_b64url = encode_provider_id(bytes.fromhex("1111111111111111"))

    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, provider_id, state, "
        "created_at, hard_expires_at, download_grace_seconds) VALUES (?, 'ws-1', 'tx-1', 'sent', 'p-1', ?, 'DRAFT', ?, ?, 0)",
        ("att-1", legacy_hex.hex(), now, now + 3600),
    )
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, provider_id, state, "
        "created_at, hard_expires_at, download_grace_seconds) VALUES (?, 'ws-1', 'tx-2', 'sent', 'p-1', ?, 'DRAFT', ?, ?, 0)",
        ("att-2", already_b64url, now, now + 3600),
    )
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, provider_id, state, "
        "created_at, hard_expires_at, download_grace_seconds) VALUES (?, 'ws-1', 'tx-3', 'received', 'p-1', ?, "
        "'OFFER_RECEIVED', ?, ?, 0)",
        ("att-3", received_b64url, now, now + 3600),
    )
    conn.commit()

    migrate(conn, target_version=9)

    rows = {
        row[0]: row[1]
        for row in conn.execute("SELECT id, provider_id FROM attachments").fetchall()
    }
    assert rows["att-1"] == encode_provider_id(legacy_hex)  # converted
    assert rows["att-2"] == already_b64url  # untouched
    assert rows["att-3"] == received_b64url  # untouched (received row)


def test_migration_9_downgrade_restores_legacy_hex_for_sent_rows(conn):
    from meshsrv.attachments.provider_registry import encode_provider_id

    migrate(conn)
    now = time.time()
    legacy_hex = bytes.fromhex("0123456789abcdef")
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, provider_id, state, "
        "created_at, hard_expires_at, download_grace_seconds) VALUES (?, 'ws-1', 'tx-1', 'sent', 'p-1', ?, 'DRAFT', ?, ?, 0)",
        ("att-1", encode_provider_id(legacy_hex), now, now + 3600),
    )
    conn.commit()

    migrate(conn, target_version=8)

    row = conn.execute("SELECT provider_id FROM attachments WHERE id = 'att-1'").fetchone()
    assert row[0] == legacy_hex.hex()


def test_migration_10_creates_outbox_and_reply_route_columns(conn):
    migrate(conn, target_version=9)
    columns_before = {row[1] for row in conn.execute("PRAGMA table_info(attachments)").fetchall()}
    assert "reply_route_type" not in columns_before

    migrate(conn, target_version=10)
    columns_after = {row[1] for row in conn.execute("PRAGMA table_info(attachments)").fetchall()}
    assert {"reply_route_type", "reply_route_id"}.issubset(columns_after)
    assert "mca_outgoing_replies" in _table_names(conn)


# PR #231 review (3rd pass), "strengthen Migration 10 tests": the test
# above only ever checked reply_route_type/reply_route_id and
# mca_outgoing_replies - not the full set of columns/tables migration 10
# actually creates (reply_adapter_id/reply_connector_profile_id/
# reply_destination_address, added in the PR #231 review section 4.2
# extension; mca_ack_quota, added in the section 4.3 extension), and
# never exercised downgrade/re-upgrade for this migration specifically,
# unlike migrations 7-9's own dedicated coverage above.

_MIGRATION_10_ATTACHMENTS_COLUMNS = frozenset({
    "reply_route_type", "reply_route_id",
    "reply_adapter_id", "reply_connector_profile_id", "reply_destination_address",
})
_MIGRATION_10_TABLES = frozenset({"mca_outgoing_replies", "mca_ack_quota"})


def _attachments_columns(conn) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(attachments)").fetchall()}


def test_migration_10_creates_every_reply_route_column_and_both_new_tables(conn):
    migrate(conn, target_version=9)
    columns_before = _attachments_columns(conn)
    assert _MIGRATION_10_ATTACHMENTS_COLUMNS.isdisjoint(columns_before)
    tables_before = _table_names(conn)
    assert _MIGRATION_10_TABLES.isdisjoint(tables_before)

    migrate(conn, target_version=10)

    columns_after = _attachments_columns(conn)
    assert _MIGRATION_10_ATTACHMENTS_COLUMNS.issubset(columns_after)
    tables_after = _table_names(conn)
    assert _MIGRATION_10_TABLES.issubset(tables_after)

    # mca_ack_quota's own shape - not just "the table exists".
    ack_quota_columns = {row[1] for row in conn.execute("PRAGMA table_info(mca_ack_quota)").fetchall()}
    assert ack_quota_columns == {"workspace_id", "scope", "window_start_at", "count"}


def test_migration_10_downgrade_removes_every_reply_route_column_and_both_new_tables(conn):
    migrate(conn, target_version=10)
    assert _MIGRATION_10_ATTACHMENTS_COLUMNS.issubset(_attachments_columns(conn))
    assert _MIGRATION_10_TABLES.issubset(_table_names(conn))

    migrate(conn, target_version=9)

    columns_after_downgrade = _attachments_columns(conn)
    assert _MIGRATION_10_ATTACHMENTS_COLUMNS.isdisjoint(columns_after_downgrade)
    tables_after_downgrade = _table_names(conn)
    assert _MIGRATION_10_TABLES.isdisjoint(tables_after_downgrade)


def test_migration_10_survives_downgrade_then_re_upgrade_with_data_intact(conn):
    """Not just "the schema comes back" - a real mca_ack_quota row
    written before the downgrade must not silently reappear corrupted
    (it can't survive the downgrade itself, DROP TABLE is DROP TABLE -
    but the re-upgrade must produce a working, empty table a fresh write
    to it succeeds against, not a broken one)."""
    migrate(conn, target_version=10)
    conn.execute(
        "INSERT INTO mca_ack_quota (workspace_id, scope, window_start_at, count) VALUES ('local', '__global__', 1000, 3)"
    )
    conn.commit()
    assert conn.execute("SELECT count FROM mca_ack_quota WHERE workspace_id = 'local'").fetchone()[0] == 3

    migrate(conn, target_version=7)
    assert current_version(conn) == 7
    assert _MIGRATION_10_TABLES.isdisjoint(_table_names(conn))

    migrate(conn)  # re-upgrade to LATEST_VERSION
    assert current_version(conn) == LATEST_VERSION
    assert _MIGRATION_10_ATTACHMENTS_COLUMNS.issubset(_attachments_columns(conn))
    assert _MIGRATION_10_TABLES.issubset(_table_names(conn))

    # A fresh row writes cleanly against the re-created table - proves it
    # isn't just present by name but actually functional (right columns,
    # right constraints).
    conn.execute(
        "INSERT INTO mca_ack_quota (workspace_id, scope, window_start_at, count) VALUES ('local', '__global__', 2000, 1)"
    )
    conn.commit()
    assert conn.execute("SELECT count FROM mca_ack_quota WHERE workspace_id = 'local'").fetchone()[0] == 1


def test_migrate_on_up_to_date_db_is_a_true_no_op(conn):
    migrate(conn)
    before = _table_names(conn)
    migrate(conn)  # already at LATEST_VERSION
    assert current_version(conn) == LATEST_VERSION
    assert _table_names(conn) == before
