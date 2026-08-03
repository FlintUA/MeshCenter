"""Persistent storage for received Meshtastic waypoints.

Waypoints are unique by their protocol waypoint ID. Meshtastic CLI may emit the
same radio packet more than once (a full packet event and a decoded callback),
so the store merges those events and preserves the best available sender data.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


class WaypointStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(database_path), exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _create_table(connection: sqlite3.Connection, table_name: str = "waypoints") -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                waypoint_id INTEGER PRIMARY KEY,
                sender_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                latitude REAL,
                longitude REAL,
                icon INTEGER,
                expire_at INTEGER,
                channel_index INTEGER,
                received_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_hidden INTEGER NOT NULL DEFAULT 0,
                raw_packet TEXT NOT NULL DEFAULT ''
            )
            """
        )

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            schema_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='waypoints'"
            ).fetchone()
            schema = (schema_row["sql"] if schema_row else "") or ""

            # Migrate the stage-1 composite key (waypoint_id, sender_id) to a
            # protocol-correct single key. Duplicate rows are merged, preferring
            # a known sender and the most recently updated payload.
            if schema and "PRIMARY KEY (waypoint_id, sender_id)" in schema.replace("\n", " "):
                connection.execute("DROP TABLE IF EXISTS waypoints_v2")
                self._create_table(connection, "waypoints_v2")
                rows = connection.execute(
                    "SELECT * FROM waypoints ORDER BY waypoint_id, "
                    "CASE WHEN sender_id <> '' THEN 0 ELSE 1 END, updated_at DESC"
                ).fetchall()
                merged: Dict[int, Dict[str, Any]] = {}
                for row in rows:
                    item = dict(row)
                    waypoint_id = int(item["waypoint_id"])
                    current = merged.get(waypoint_id)
                    if current is None:
                        merged[waypoint_id] = item
                        continue
                    if not current.get("sender_id") and item.get("sender_id"):
                        current["sender_id"] = item["sender_id"]
                    for field in ("name", "description", "latitude", "longitude", "icon", "expire_at", "channel_index"):
                        if current.get(field) in (None, "") and item.get(field) not in (None, ""):
                            current[field] = item[field]
                    current["received_at"] = min(float(current["received_at"]), float(item["received_at"]))
                    if float(item["updated_at"]) > float(current["updated_at"]):
                        current["updated_at"] = item["updated_at"]
                        current["raw_packet"] = item.get("raw_packet", current.get("raw_packet", ""))
                    current["is_hidden"] = int(bool(current.get("is_hidden") or item.get("is_hidden")))

                for item in merged.values():
                    connection.execute(
                        """
                        INSERT INTO waypoints_v2 (
                            waypoint_id, sender_id, name, description, latitude,
                            longitude, icon, expire_at, channel_index, received_at,
                            updated_at, is_active, is_hidden, raw_packet
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["waypoint_id"], item.get("sender_id", ""), item.get("name", ""),
                            item.get("description", ""), item.get("latitude"), item.get("longitude"),
                            item.get("icon"), item.get("expire_at"), item.get("channel_index"),
                            item.get("received_at", time.time()), item.get("updated_at", time.time()),
                            item.get("is_active", 1), item.get("is_hidden", 0), item.get("raw_packet", ""),
                        ),
                    )
                connection.execute("DROP TABLE waypoints")
                connection.execute("ALTER TABLE waypoints_v2 RENAME TO waypoints")
            else:
                self._create_table(connection)

            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_waypoints_active ON waypoints(is_active, expire_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_waypoints_received ON waypoints(received_at DESC)"
            )
            self._refresh_expired(connection)

    @staticmethod
    def _refresh_expired(connection: sqlite3.Connection) -> None:
        now = int(time.time())
        connection.execute(
            "UPDATE waypoints SET is_active = CASE "
            "WHEN expire_at IS NOT NULL AND expire_at > 0 AND expire_at <= ? THEN 0 ELSE 1 END",
            (now,),
        )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["is_active"] = bool(result.get("is_active"))
        result["is_hidden"] = bool(result.get("is_hidden"))
        return result

    def upsert(self, waypoint: Dict[str, Any]) -> Dict[str, Any]:
        now = time.time()
        waypoint_id = int(waypoint.get("waypoint_id") or 0)
        if waypoint_id <= 0:
            raise ValueError("waypoint_id must be a positive integer")

        expire_at = waypoint.get("expire_at")
        active = 1
        if expire_at:
            try:
                expire_at = int(expire_at)
                active = 1 if expire_at > int(now) else 0
            except (TypeError, ValueError):
                expire_at = None

        raw_packet = waypoint.get("raw_packet", "")
        if not isinstance(raw_packet, str):
            raw_packet = json.dumps(raw_packet, ensure_ascii=False, default=str)

        incoming = {
            "waypoint_id": waypoint_id,
            "sender_id": str(waypoint.get("sender_id") or "").strip(),
            "name": str(waypoint.get("name") or "").strip(),
            "description": str(waypoint.get("description") or "").strip(),
            "latitude": waypoint.get("latitude"),
            "longitude": waypoint.get("longitude"),
            "icon": waypoint.get("icon"),
            "expire_at": expire_at,
            "channel_index": waypoint.get("channel_index"),
            "received_at": float(waypoint.get("received_at") or now),
            "updated_at": now,
            "is_active": active,
            "raw_packet": raw_packet[:20000],
        }

        with self._lock, self._connect() as connection:
            existing_row = connection.execute(
                "SELECT * FROM waypoints WHERE waypoint_id = ?", (waypoint_id,)
            ).fetchone()
            existing = dict(existing_row) if existing_row else None

            if existing:
                merged = dict(existing)
                # Never replace a known sender with the sender-less duplicate
                # callback emitted by Meshtastic CLI.
                if incoming["sender_id"]:
                    merged["sender_id"] = incoming["sender_id"]
                for field in ("name", "description", "latitude", "longitude", "icon", "expire_at", "channel_index"):
                    if incoming[field] not in (None, ""):
                        merged[field] = incoming[field]
                merged["updated_at"] = now
                merged["is_active"] = active
                if incoming["raw_packet"] and (incoming["sender_id"] or not merged.get("raw_packet")):
                    merged["raw_packet"] = incoming["raw_packet"]

                meaningful_fields = (
                    "sender_id", "name", "description", "latitude", "longitude",
                    "icon", "expire_at", "channel_index", "is_active",
                )
                event = "updated" if any(existing.get(k) != merged.get(k) for k in meaningful_fields) else "duplicate"
                connection.execute(
                    """
                    UPDATE waypoints SET sender_id=:sender_id, name=:name,
                        description=:description, latitude=:latitude,
                        longitude=:longitude, icon=:icon, expire_at=:expire_at,
                        channel_index=:channel_index, updated_at=:updated_at,
                        is_active=:is_active, raw_packet=:raw_packet
                    WHERE waypoint_id=:waypoint_id
                    """,
                    merged,
                )
                result = merged
            else:
                connection.execute(
                    """
                    INSERT INTO waypoints (
                        waypoint_id, sender_id, name, description, latitude,
                        longitude, icon, expire_at, channel_index, received_at,
                        updated_at, is_active, raw_packet
                    ) VALUES (
                        :waypoint_id, :sender_id, :name, :description, :latitude,
                        :longitude, :icon, :expire_at, :channel_index, :received_at,
                        :updated_at, :is_active, :raw_packet
                    )
                    """,
                    incoming,
                )
                result = incoming
                result["is_hidden"] = 0
                event = "created"

        result = dict(result)
        result["is_active"] = bool(result.get("is_active"))
        result["is_hidden"] = bool(result.get("is_hidden", 0))
        result["_event"] = event
        return result

    def list(self, include_expired: bool = False, include_hidden: bool = False) -> List[Dict[str, Any]]:
        now = int(time.time())
        clauses = []
        parameters: List[Any] = []
        with self._lock, self._connect() as connection:
            self._refresh_expired(connection)
            if not include_hidden:
                clauses.append("is_hidden = 0")
            if not include_expired:
                clauses.append("is_active = 1")
                clauses.append("(expire_at IS NULL OR expire_at = 0 OR expire_at > ?)")
                parameters.append(now)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"SELECT * FROM waypoints {where} ORDER BY received_at DESC",
                parameters,
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get(self, waypoint_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            self._refresh_expired(connection)
            row = connection.execute(
                "SELECT * FROM waypoints WHERE waypoint_id = ?",
                (int(waypoint_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None


    def set_hidden(self, waypoint_id: int, hidden: bool) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as connection:
            self._refresh_expired(connection)
            connection.execute(
                "UPDATE waypoints SET is_hidden = ?, updated_at = ? WHERE waypoint_id = ?",
                (1 if hidden else 0, time.time(), int(waypoint_id)),
            )
            row = connection.execute(
                "SELECT * FROM waypoints WHERE waypoint_id = ?",
                (int(waypoint_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def delete(self, waypoint_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM waypoints WHERE waypoint_id = ?",
                (int(waypoint_id),),
            )
        return cursor.rowcount > 0

    def delete_many(self, waypoint_ids: List[int]) -> int:
        ids = sorted({int(value) for value in waypoint_ids if int(value) > 0})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM waypoints WHERE waypoint_id IN ({placeholders})",
                ids,
            )
        return int(cursor.rowcount or 0)

    def delete_all(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM waypoints")
        return int(cursor.rowcount or 0)

    def count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM waypoints").fetchone()
        return int(row["count"] if row else 0)
