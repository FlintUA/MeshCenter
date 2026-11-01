"""FX1 Phase B: the sender's copy of a finished transfer is deleted, one store for received files (journaled merge of the old
cache folder), and the optional auto-cleanup."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from meshsrv.attachments import storage_merge
from meshsrv.attachments.mime_allowlist import MAX_SOURCE_NAME_CODE_POINTS, sanitize_display_name
from meshsrv.attachments.service import AttachmentsService
from test_attachments_service import (  # noqa: F401 - fixtures of the service tests
    _purge_command, _saved_path_of, _seed_received_available, conn, connectivity_monitor, delivery_adapter, key_exchange,
    principal, provider_registry, registered_provider, relay_client, relay_session, remote_recipient, service, store, wsm,
)


def _seed_sent(conn, principal, wsm, state, *, content=b"outgoing", saved_path_override=None):
    attachment_id = uuid.uuid4().hex
    paths = wsm.paths(principal.principal_id)
    spool = paths.spool_outgoing / attachment_id
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_bytes(content)
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, file_name, mime_type, plain_size, "
        "saved_path, created_at, hard_expires_at, download_grace_seconds, draft_comment) "
        "VALUES (?, ?, ?, 'sent', ?, ?, 'report.pdf', 'application/pdf', ?, ?, 1.0, 2.0, 3600, 'kept comment')",
        (attachment_id, principal.workspace_id, uuid.uuid4().hex, principal.principal_id, state, len(content),
         str(saved_path_override or spool)),
    )
    conn.commit()
    return attachment_id, spool


# ---- the sender's copy ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["DOWNLOADED", "EXPIRED", "CANCELLED", "REVOKED", "REJECTED"])
def test_a_finished_transfer_loses_its_sender_copy_but_keeps_its_history(conn, wsm, principal, service, state):
    attachment_id, spool = _seed_sent(conn, principal, wsm, state)
    service._sweep_sent_copies()
    assert not spool.exists(), "the file is gone"
    row = conn.execute("SELECT state, saved_path, file_name, plain_size, draft_comment FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    assert tuple(row) == (state, None, "report.pdf", 8, "kept comment"), "the history entry stays (name, size, comment, status)"


@pytest.mark.parametrize("state", ["FAILED_UPLOAD", "FAILED_RADIO", "FAILED_VALIDATION", "SENT", "RECEIVED", "UPLOADING", "QUEUED_UPLOAD", "READY_TO_SEND"])
def test_an_unfinished_or_failed_transfer_keeps_its_sender_copy_for_retry(conn, wsm, principal, service, state):
    attachment_id, spool = _seed_sent(conn, principal, wsm, state)
    service._sweep_sent_copies()
    assert spool.exists()
    assert conn.execute("SELECT saved_path FROM attachments WHERE id = ?", (attachment_id,)).fetchone()[0] == str(spool)


def test_the_sweep_never_deletes_a_path_that_is_not_the_rows_own_spool_file(conn, wsm, principal, service, tmp_path):
    elsewhere = tmp_path / "precious.txt"
    elsewhere.write_bytes(b"do not delete")
    attachment_id, spool = _seed_sent(conn, principal, wsm, "DOWNLOADED", saved_path_override=elsewhere)
    service._sweep_sent_copies()
    assert elsewhere.exists() and spool.exists()


def test_the_sweep_is_idempotent_and_bounded(conn, wsm, principal, service):
    ids = [_seed_sent(conn, principal, wsm, "DOWNLOADED")[0] for _ in range(25)]
    service._sweep_sent_copies()
    left = conn.execute("SELECT COUNT(*) FROM attachments WHERE saved_path IS NOT NULL").fetchone()[0]
    assert left == 25 - 20, "at most 20 per tick"
    service._sweep_sent_copies()
    service._sweep_sent_copies()
    assert conn.execute("SELECT COUNT(*) FROM attachments WHERE saved_path IS NOT NULL").fetchone()[0] == 0
    assert len(ids) == 25


def test_a_tick_deletes_the_sender_copy(conn, wsm, principal, service):
    attachment_id, spool = _seed_sent(conn, principal, wsm, "REVOKED")
    service.tick()
    assert not spool.exists()


# ---- one store: the journaled merge ----------------------------------------------------------------------------------------


def _merge(conn, wsm, principal, *, vault_active=False):
    paths = wsm.paths(principal.principal_id)
    return storage_merge.merge_received_cache(
        conn, workspace_id=principal.workspace_id, cache_dir=paths.cache_incoming, files_dir=paths.files, root=paths.root,
        unique_file_name=lambda name: wsm.unique_file_name(principal.principal_id, name),
        sanitize_name=lambda name: sanitize_display_name(name or "")[:MAX_SOURCE_NAME_CODE_POINTS].strip(".") or "attachment",
        vault_active=vault_active,
    )


def test_cache_copies_move_into_files_with_their_names_and_content(conn, wsm, principal):
    a = "a1" * 16
    b = "b2" * 16
    src_a = _seed_received_available(conn, principal, wsm, attachment_id=a, file_name="photo.jpg", content=b"AAAA")
    src_b = _seed_received_available(conn, principal, wsm, attachment_id=b, file_name="photo.jpg", content=b"BBBBBB")
    result = _merge(conn, wsm, principal)
    files = wsm.paths(principal.principal_id).files
    assert result["moved"] == 2 and result["bytes"] == 10
    assert not src_a.exists() and not src_b.exists()
    assert (files / "photo.jpg").read_bytes() == b"AAAA"
    assert (files / "photo (2).jpg").read_bytes() == b"BBBBBB", "a name that is taken gets (2), nothing is overwritten"
    assert _saved_path_of(conn, a) == str(files / "photo.jpg")
    assert _saved_path_of(conn, b) == str(files / "photo (2).jpg")
    journal = storage_merge.read_journal(wsm.paths(principal.principal_id).root / storage_merge.JOURNAL_NAME)
    assert [r["op"] for r in journal] == ["start", "plan", "done", "plan", "done", "complete"]


def test_the_merge_does_nothing_the_second_time_and_writes_nothing(conn, wsm, principal):
    _seed_received_available(conn, principal, wsm, attachment_id="c3" * 16, content=b"x")
    _merge(conn, wsm, principal)
    journal_path = wsm.paths(principal.principal_id).root / storage_merge.JOURNAL_NAME
    size = journal_path.stat().st_size
    assert _merge(conn, wsm, principal) == {"moved": 0, "missing": 0, "skipped": 0, "bytes": 0}
    assert journal_path.stat().st_size == size


def test_a_file_a_sent_row_or_a_saved_row_owns_is_not_a_cache_copy(conn, wsm, principal):
    _, spool = _seed_sent(conn, principal, wsm, "SENT")
    paths = wsm.paths(principal.principal_id)
    saved = paths.files / "mine.pdf"
    saved.write_bytes(b"saved")
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, file_name, saved_path, created_at, "
        "hard_expires_at, download_grace_seconds) VALUES (?, ?, ?, 'received', ?, 'AVAILABLE', 'mine.pdf', ?, 0, 0, 3600)",
        ("d4" * 16, principal.workspace_id, uuid.uuid4().hex, principal.principal_id, str(saved)),
    )
    conn.commit()
    assert _merge(conn, wsm, principal)["moved"] == 0
    assert spool.exists() and saved.exists()


def test_a_row_whose_cache_file_is_gone_is_counted_and_left_alone(conn, wsm, principal):
    src = _seed_received_available(conn, principal, wsm, attachment_id="e5" * 16, content=b"x")
    src.unlink()
    result = _merge(conn, wsm, principal)
    assert result["missing"] == 1 and result["moved"] == 0
    assert _saved_path_of(conn, "e5" * 16) == str(src), "the row is not rewritten"


def test_vault_files_move_as_they_are_named_by_the_attachment_id(conn, wsm, principal):
    attachment_id = "f6" * 16
    paths = wsm.paths(principal.principal_id)
    src = paths.cache_incoming / (attachment_id + ".mcv")
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\x00ciphertext\x00")
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, transfer_id, direction, principal_id, state, file_name, saved_path, created_at, "
        "hard_expires_at, download_grace_seconds) VALUES (?, ?, ?, 'received', ?, 'AVAILABLE', 'v1:sealed', ?, 0, 0, 3600)",
        (attachment_id, principal.workspace_id, uuid.uuid4().hex, principal.principal_id, str(src)),
    )
    conn.commit()
    assert _merge(conn, wsm, principal, vault_active=True)["moved"] == 1
    dest = paths.files / (attachment_id + ".mcv")
    assert dest.read_bytes() == b"\x00ciphertext\x00" and not src.exists(), "the bytes are not touched"
    assert _saved_path_of(conn, attachment_id) == str(dest)


def test_a_plain_file_next_to_an_active_vault_and_an_encrypted_file_without_one_are_never_guessed_at(conn, wsm, principal):
    plain = _seed_received_available(conn, principal, wsm, attachment_id="01" * 16, content=b"plain")
    result = _merge(conn, wsm, principal, vault_active=True)
    assert result["skipped"] == 1 and plain.exists()


def _plan(journal, **fields):
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(fields) + "\n")


def test_an_interrupted_run_resumes_with_the_planned_name_when_the_file_had_not_moved(conn, wsm, principal):
    attachment_id = "02" * 16
    src = _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="a.txt", content=b"1")
    paths = wsm.paths(principal.principal_id)
    planned = paths.files / "planned name.txt"
    _plan(paths.root / storage_merge.JOURNAL_NAME, op="start", at=1)
    _plan(paths.root / storage_merge.JOURNAL_NAME, op="plan", id=attachment_id, **{"from": str(src), "to": str(planned)})
    assert _merge(conn, wsm, principal)["moved"] == 1
    assert planned.read_bytes() == b"1" and _saved_path_of(conn, attachment_id) == str(planned), "the planned name, not a new one"


def test_an_interrupted_run_finishes_the_row_when_the_file_had_already_moved(conn, wsm, principal):
    attachment_id = "03" * 16
    src = _seed_received_available(conn, principal, wsm, attachment_id=attachment_id, file_name="a.txt", content=b"1")
    paths = wsm.paths(principal.principal_id)
    planned = paths.files / "a.txt"
    os.replace(src, planned)                              # the crash happened after the move, before the row was updated
    _plan(paths.root / storage_merge.JOURNAL_NAME, op="plan", id=attachment_id, **{"from": str(src), "to": str(planned)})
    assert _merge(conn, wsm, principal)["moved"] == 1
    assert _saved_path_of(conn, attachment_id) == str(planned)


def test_a_torn_last_journal_line_is_ignored(conn, wsm, principal):
    paths = wsm.paths(principal.principal_id)
    journal = paths.root / storage_merge.JOURNAL_NAME
    journal.write_text('{"op":"start","at":1}\n{"op":"plan","id":"zz', encoding="utf-8")
    assert [r["op"] for r in storage_merge.read_journal(journal)] == ["start"]
    _seed_received_available(conn, principal, wsm, attachment_id="04" * 16, content=b"x")
    assert _merge(conn, wsm, principal)["moved"] == 1


def test_the_service_merges_once_at_its_first_tick(conn, wsm, principal, service):
    src = _seed_received_available(conn, principal, wsm, attachment_id="05" * 16, file_name="doc.pdf", content=b"x")
    service.tick()
    assert not src.exists() and (wsm.paths(principal.principal_id).files / "doc.pdf").exists()


# ---- optional auto-cleanup ---------------------------------------------------------------------------------------------------


def _service_with_settings(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, delivery_adapter, relay_client, settings):
    return AttachmentsService(
        conn, workspace_manager=wsm, principal=principal, provider_registry=provider_registry, key_exchange=key_exchange,
        connectivity_monitor=connectivity_monitor, delivery_adapter=delivery_adapter,
        relay_client_factory=lambda provider_id_text: relay_client, max_per_tick=8, files_settings_fn=lambda: settings,
    )


def test_auto_cleanup_is_off_by_default_and_deletes_old_copies_only_when_switched_on(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, delivery_adapter, relay_client
):
    old = _seed_received_available(conn, principal, wsm, attachment_id="06" * 16, content=b"old")          # created_at 0: ancient
    fresh = _seed_received_available(conn, principal, wsm, attachment_id="07" * 16, content=b"new")
    conn.execute("UPDATE attachments SET created_at = ? WHERE id = ?", (time.time(), "07" * 16))
    conn.commit()
    off = _service_with_settings(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, delivery_adapter, relay_client, {})
    off._auto_cleanup_received()
    assert old.exists() and fresh.exists(), "off: nothing is deleted"

    on = _service_with_settings(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, delivery_adapter, relay_client,
                                {"auto_cleanup_days": 30})
    on._auto_cleanup_received()
    assert not old.exists() and fresh.exists(), "on: only the copy older than 30 days"
    assert conn.execute("SELECT state FROM attachments WHERE id = ?", ("06" * 16,)).fetchone()[0] == "AVAILABLE", "the history row stays"


def test_auto_cleanup_runs_at_most_once_an_hour(
    conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, delivery_adapter, relay_client
):
    on = _service_with_settings(conn, wsm, principal, provider_registry, key_exchange, connectivity_monitor, delivery_adapter, relay_client,
                                {"auto_cleanup_days": 30})
    on._auto_cleanup_received()
    late = _seed_received_available(conn, principal, wsm, attachment_id="08" * 16, content=b"x")
    on._auto_cleanup_received()
    assert late.exists(), "the second call within the hour does nothing"


# ---- the comment reaches the projection on both sides ------------------------------------------------------------------------


def test_the_comment_of_a_sent_and_a_received_row_is_in_the_snapshot_and_the_public_projection(conn, wsm, principal, service):
    from meshsrv.attachments.snapshots import serialize_attachment_public

    sent_id, _ = _seed_sent(conn, principal, wsm, "SENT")
    _seed_received_available(conn, principal, wsm, attachment_id="09" * 16, content=b"x")
    conn.execute("UPDATE attachments SET draft_comment = 'from the sender' WHERE id = ?", ("09" * 16,))
    conn.commit()
    service._refresh_snapshot()
    snapshot = service._snapshot_publisher.snapshot()
    assert snapshot.by_id[sent_id].comment == "kept comment"
    assert snapshot.by_id["09" * 16].comment == "from the sender"
    assert serialize_attachment_public(snapshot.by_id["09" * 16])["comment"] == "from the sender"
    # a locked vault withholds it like the file name
    locked = serialize_attachment_public(snapshot.by_id["09" * 16], vault_locked=True)
    assert locked["comment"] is None and locked["file_name"] is None
