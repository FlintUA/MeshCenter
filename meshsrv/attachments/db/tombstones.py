"""meshsrv/attachments/db/tombstones.py

`mca_tombstones` read/write helpers (design spec section 16.4; ADR-0001
section 6: revoked/expired transfer IDs stay tombstoned for at least 7
days so a re-delivered/replayed OFFER for a dead transfer_id is recognized
and rejected rather than treated as new). MIT-licensed Core code.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

DEFAULT_TOMBSTONE_RETENTION_SECONDS = 7 * 24 * 3600  # ADR-0001 section 6: "at least 7 days"


def record_tombstone(
    conn: sqlite3.Connection,
    *,
    transfer_id: str,
    workspace_id: str,
    reason: str,
    now: Optional[float] = None,
    retention_seconds: int = DEFAULT_TOMBSTONE_RETENTION_SECONDS,
) -> None:
    """Record `transfer_id` as revoked/expired. Idempotent: re-tombstoning
    an already-tombstoned ID with a later `purge_after` extends retention
    rather than erroring, since two different events (e.g. CANCEL then
    independently observed EXPIRED) may tombstone the same ID."""
    now = time.time() if now is None else now
    purge_after = now + retention_seconds
    conn.execute(
        """
        INSERT INTO mca_tombstones (transfer_id, workspace_id, reason, tombstoned_at, purge_after)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(transfer_id) DO UPDATE SET
            reason = excluded.reason,
            purge_after = MAX(mca_tombstones.purge_after, excluded.purge_after)
        """,
        (transfer_id, workspace_id, reason, now, purge_after),
    )
    conn.commit()


def is_tombstoned(conn: sqlite3.Connection, transfer_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM mca_tombstones WHERE transfer_id = ?", (transfer_id,)
    ).fetchone()
    return row is not None


def purge_expired_tombstones(conn: sqlite3.Connection, now: Optional[float] = None) -> int:
    """Delete tombstones whose retention window has passed. Returns the
    number of rows removed. Never purges anything still inside its
    `purge_after` window, even if called frequently."""
    now = time.time() if now is None else now
    cursor = conn.execute("DELETE FROM mca_tombstones WHERE purge_after <= ?", (now,))
    conn.commit()
    return cursor.rowcount
