"""One store for received files (FX1 Phase B): move the received copies that still sit in ``cache/incoming/`` into ``files/``.

Before this change a received file lived in ``cache/incoming/<attachment id>`` until the user pressed "Save to Files", which
moved it into ``files/``. There was one more place to look and one more kind of copy to explain. Now a received file is
written straight into ``files/`` and this module moves the ones that were received earlier.

The move is a same-filesystem ``os.replace`` (atomic), one file at a time, journaled so that an interrupted run (power loss
on a Pi) resumes exactly where it stopped:

* ``storage_merge.journal`` (JSON lines, in the workspace folder) gets a ``plan`` line (id, source, destination) BEFORE a file
  moves and a ``done`` line after the database row points at the new place; a ``complete`` line ends a run.
* The destination of a planned move is fixed in the journal, so a resumed run never picks a different name.
* A file is never copied, never decrypted and never overwritten: with the vault on, the encrypted ``<id>.mcv`` file is moved
  as it is (the same key and associated data open it in either folder, exactly as "Save to Files" always did); a plain file
  gets a sanitized, collision-free name through ``unique_file_name`` (``Name (2).pdf``).
* Rows are updated with ``WHERE saved_path = <old path>``, so a row that changed meanwhile is left alone.

Rolling back to an older build needs nothing: an older build already treats a path inside ``files/`` as a saved file.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

JOURNAL_NAME = "storage_merge.journal"
MCV = ".mcv"


def _append(journal: Path, record: dict) -> None:
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(str(journal), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def read_journal(journal: Path) -> List[dict]:
    """Every readable line; a torn last line (power loss in the middle of a write) is ignored."""
    records: List[dict] = []
    try:
        text = journal.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _pending_from_journal(records: List[dict]) -> Dict[str, dict]:
    """Planned moves that never reached ``done``, keyed by attachment id (the last plan of an id wins)."""
    pending: Dict[str, dict] = {}
    for record in records:
        op, attachment_id = record.get("op"), record.get("id")
        if op == "plan" and isinstance(attachment_id, str):
            pending[attachment_id] = record
        elif op in ("done", "skip") and isinstance(attachment_id, str):
            pending.pop(attachment_id, None)
    return pending


def _move(src: Path, dest: Path) -> None:
    try:
        os.replace(str(src), str(dest))
        return
    except OSError:
        if not src.exists():
            raise
    # a different filesystem (a mounted cache folder): copy, flush, then remove the source
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(str(src), "rb") as source, open(str(tmp), "wb") as target:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        os.replace(str(tmp), str(dest))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    src.unlink()


def merge_received_cache(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    cache_dir: Path,
    files_dir: Path,
    root: Path,
    unique_file_name: Callable[[str], Path],
    sanitize_name: Callable[[Optional[str]], str],
    vault_active: bool,
    now: Optional[float] = None,
) -> dict:
    """Move every received copy in ``cache_dir`` into ``files_dir``. Returns ``{"moved", "missing", "skipped", "bytes"}``.
    Safe to call at every start: with nothing left in the cache it does nothing and writes nothing."""
    now = time.time() if now is None else now
    journal = Path(root) / JOURNAL_NAME
    cache_real = Path(os.path.realpath(str(cache_dir)))
    files_real = Path(os.path.realpath(str(files_dir)))
    result = {"moved": 0, "missing": 0, "skipped": 0, "bytes": 0}
    conn.row_factory = sqlite3.Row

    def _row_for(attachment_id: str):
        return conn.execute("SELECT id, file_name, saved_path FROM attachments WHERE id = ?", (attachment_id,)).fetchone()

    def _finish_move(attachment_id: str, src: Path, dest: Path, size: int) -> None:
        cursor = conn.execute(
            "UPDATE attachments SET saved_path = ? WHERE id = ? AND saved_path = ?", (str(dest), attachment_id, str(src))
        )
        conn.commit()
        _append(journal, {"op": "done", "id": attachment_id, "to": str(dest), "updated": cursor.rowcount})
        result["moved"] += 1
        result["bytes"] += size

    # 1) a run that was interrupted: finish exactly the planned moves, with the planned names
    for attachment_id, plan in _pending_from_journal(read_journal(journal)).items():
        row = _row_for(attachment_id)
        if row is None or not plan.get("from") or not plan.get("to"):
            _append(journal, {"op": "skip", "id": attachment_id, "why": "no_row"})
            result["skipped"] += 1
            continue
        src, dest = Path(plan["from"]), Path(plan["to"])
        if src.is_file() and not dest.exists():
            _move(src, dest)
            _finish_move(attachment_id, src, dest, dest.stat().st_size)
        elif dest.is_file() and not src.exists():
            _finish_move(attachment_id, src, dest, dest.stat().st_size)      # the move happened, the row did not follow yet
        else:
            _append(journal, {"op": "skip", "id": attachment_id, "why": "unexpected_state"})
            logger.warning("MCAttach storage merge: left one file where it is (unexpected state of an interrupted move)")
            result["skipped"] += 1

    # 2) everything that is still in the cache folder
    rows = conn.execute(
        "SELECT id, file_name, saved_path FROM attachments WHERE workspace_id = ? AND direction = 'received' "
        "AND saved_path IS NOT NULL",
        (workspace_id,),
    ).fetchall()
    started = False
    for row in rows:
        src = Path(row["saved_path"])
        if Path(os.path.realpath(str(src.parent))) != cache_real:
            continue                                  # not a cache copy: a saved file, or a sent row's spool
        if not src.is_file():
            result["missing"] += 1                    # the row claims content that is gone; the row is left as it is
            continue
        if src.name.endswith(MCV):
            if not vault_active:
                result["skipped"] += 1               # an encrypted file without an active vault: never guess
                continue
            dest = files_real / (row["id"] + MCV)
        else:
            if vault_active:
                result["skipped"] += 1               # a plain file next to an enabled vault: the vault migration owns it
                continue
            dest = unique_file_name(sanitize_name(row["file_name"]))
        if dest.exists():
            result["skipped"] += 1                    # never overwrite
            continue
        if not started:
            _append(journal, {"op": "start", "at": now, "version": 1})
            started = True
        size = src.stat().st_size
        _append(journal, {"op": "plan", "id": row["id"], "from": str(src), "to": str(dest)})
        _move(src, dest)
        _finish_move(row["id"], src, dest, size)

    if started or result["moved"] or result["skipped"]:
        _append(journal, {"op": "complete", "at": time.time(), **result})
        logger.info("MCAttach storage merge: %d moved, %d missing, %d left in place", result["moved"], result["missing"], result["skipped"])
    return result
