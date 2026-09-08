"""Tests for meshsrv/attachments/snapshots.py (internal-rest-api.md §3.3 /
§3.7 / §7.5 / §7.1; Execution Plan Step 1.6A.1).

Covers the two halves of the module:

1. `ContentDescriptor` + locator containment - the frozen descriptor's
   inline/attachment disposition, the lexical `validate_locator`/`make_
   locator` rules (absolute/traversal/NUL/backslash rejected, pure string
   math, no filesystem read), the serve-time `resolve_locator` (containment
   on the resolved path, symlink-escape rejection), and the no-locator-in-
   repr rule.
2. The immutable snapshot value types + serializers + `build_attachments_
   snapshot` + `AttachmentsSnapshotPublisher` - explicit serializer
   allowlists (never `dataclasses.asdict`, never leak `locator`/`saved_path`
   /`descriptor`), fail-closed timeline redaction, conservative
   `content_available`, batched (no-N+1) build, atomic reference swap,
   incremental dirty-id publication (never O(N) per tick), and
   last-known-good retention.

Pure stdlib + a temp sqlite schema - no Flask/network/radio, safe in CI.
"""

import dataclasses
import os
import sqlite3
import sys

import pytest

from meshsrv.attachments.snapshots import (
    MAX_DETAIL_EVENTS,
    AttachmentsSnapshotPublisher,
    AttachmentRecord,
    AttachmentsSnapshot,
    ContentDescriptor,
    ContentDisposition,
    ContentLocatorError,
    DeliveryRecord,
    IdempotencyEntry,
    RecipientRecord,
    TimelineEvent,
    build_attachments_snapshot,
    disposition_for_mime_type,
    list_dirty_attachment_ids,
    make_locator,
    resolve_locator,
    serialize_attachment_public,
    serialize_delivery,
    serialize_idempotency_entry,
    serialize_recipient,
    serialize_timeline_event,
    validate_locator,
)
from meshsrv.attachments.workspace import MCAWorkspaceManager

PRINCIPAL_ID = "0123456789abcdef"


@pytest.fixture()
def workspace_manager(tmp_path):
    return MCAWorkspaceManager(str(tmp_path / "data"))


@pytest.fixture()
def paths(workspace_manager):
    return workspace_manager.paths(PRINCIPAL_ID)


# --- disposition ------------------------------------------------------------

def test_disposition_inline_only_for_preview_images():
    assert disposition_for_mime_type("image/jpeg") is ContentDisposition.INLINE
    assert disposition_for_mime_type("image/png") is ContentDisposition.INLINE
    assert disposition_for_mime_type("image/webp") is ContentDisposition.INLINE


def test_disposition_attachment_for_everything_else():
    # PDF/SVG/HTML/archives are never served inline (§7.14); None/unknown
    # MIME types are attachment, never inline.
    for mime in ("application/pdf", "image/svg+xml", "text/html",
                 "application/zip", "text/plain", None, ""):
        assert disposition_for_mime_type(mime) is ContentDisposition.ATTACHMENT


# --- validate_locator (lexical, pure) ---------------------------------------

def test_validate_locator_accepts_relative_file_paths():
    validate_locator("files/photo.jpg")
    validate_locator("cache/incoming/x.bin")
    validate_locator("files/a b/c (2).png")  # spaces and parens are fine


@pytest.mark.parametrize("bad", [
    "",                    # empty
    "files/",              # trailing slash -> directory
    "/etc/passwd",         # POSIX absolute
    "C:/Windows/x",        # Windows drive absolute
    "files/../x",          # traversal
    "..",                  # bare parent
    "files/./x",           # dot segment
    "files//x",            # empty segment
    "files\\x",            # backslash separator
    "files/\x00x",         # NUL
])
def test_validate_locator_rejects_unsafe_locators(bad):
    with pytest.raises(ContentLocatorError):
        validate_locator(bad)


def test_validate_locator_rejects_non_string():
    with pytest.raises(ContentLocatorError):
        validate_locator(None)
    with pytest.raises(ContentLocatorError):
        validate_locator(123)


# --- make_locator (pure, no filesystem) -------------------------------------

def test_make_locator_none_or_empty_returns_none(paths):
    assert make_locator(paths, None) is None
    assert make_locator(paths, "") is None


def test_make_locator_rejects_relative_paths(paths):
    assert make_locator(paths, "files/photo.jpg") is None


def test_make_locator_files_directory(paths):
    locator = make_locator(paths, paths.files / "photo.jpg")
    assert locator == "files/photo.jpg"


def test_make_locator_cache_incoming_directory(paths):
    locator = make_locator(paths, paths.cache_incoming / "blob.bin")
    assert locator == "cache/incoming/blob.bin"


def test_make_locator_rejects_spool_and_outside_root(paths):
    assert make_locator(paths, paths.spool_outgoing / "uuid") is None
    assert make_locator(paths, paths.root / "somewhere_else" / "x") is None
    assert make_locator(paths, paths.root.parent / "elsewhere" / "x") is None


def test_make_locator_rejects_traversal_even_if_absolute(paths):
    # An absolute path that lexically contains ".." must not produce a
    # locator - relative_to() does not resolve "..", so the segment survives
    # and validate_locator rejects it (fail closed).
    sneaky = paths.files / ".." / ".." / "etc" / "passwd"
    assert make_locator(paths, sneaky) is None


def test_make_locator_rejects_directory_itself(paths):
    assert make_locator(paths, paths.files) is None
    assert make_locator(paths, paths.cache_incoming) is None


# --- resolve_locator (serve-time, resolved) ---------------------------------

def test_resolve_locator_returns_resolved_path_inside_files(paths):
    result = resolve_locator(paths, "files/photo.jpg")
    assert result == (paths.files / "photo.jpg").resolve()


def test_resolve_locator_rejects_locator_outside_content_area(paths):
    # "keys/..." is under the workspace root but not a controlled content dir.
    with pytest.raises(ContentLocatorError):
        resolve_locator(paths, "keys/secret")


def test_resolve_locator_rejects_traversal(paths):
    with pytest.raises(ContentLocatorError):
        resolve_locator(paths, "files/../../etc/passwd")


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_resolve_locator_rejects_symlink_escape(paths):
    paths.files.mkdir(parents=True, exist_ok=True)
    outside = paths.root.parent / "outside.txt"
    outside.write_text("secret")
    link = paths.files / "linked.txt"
    link.symlink_to(outside)
    # A symlink under files/ that points outside the controlled area must be
    # rejected after .resolve() follows it (§3.7).
    with pytest.raises(ContentLocatorError):
        resolve_locator(paths, "files/linked.txt")


# --- ContentDescriptor ------------------------------------------------------

def test_content_descriptor_is_frozen():
    desc = ContentDescriptor(
        attachment_id="att-1", locator="files/x.jpg", mime_type="image/jpeg",
        disposition=ContentDisposition.INLINE, plain_size=123,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        desc.locator = "files/y.jpg"


def test_content_descriptor_repr_omits_locator():
    desc = ContentDescriptor(
        attachment_id="att-1", locator="files/top-secret.jpg",
        mime_type="image/jpeg", disposition=ContentDisposition.INLINE, plain_size=1,
    )
    text = repr(desc)
    assert "top-secret" not in text
    assert "files/" not in text
    assert "att-1" in text


def test_content_descriptor_rejects_bad_locator():
    with pytest.raises(ContentLocatorError):
        ContentDescriptor(
            attachment_id="att-1", locator="/etc/passwd", mime_type="text/plain",
            disposition=ContentDisposition.ATTACHMENT, plain_size=0,
        )


def test_content_descriptor_rejects_bad_disposition_and_size():
    with pytest.raises(ValueError):
        ContentDescriptor(
            attachment_id="att-1", locator="files/x", mime_type="text/plain",
            disposition="inline", plain_size=0,  # not a ContentDisposition
        )
    with pytest.raises(ValueError):
        ContentDescriptor(
            attachment_id="att-1", locator="files/x", mime_type="text/plain",
            disposition=ContentDisposition.ATTACHMENT, plain_size=-1,
        )


# --- TimelineEvent ----------------------------------------------------------

def test_timeline_event_detail_is_frozen():
    evt = TimelineEvent(event_type="created", detail={"recipients": 2}, created_at=1.0)
    with pytest.raises(TypeError):
        evt.detail["recipients"] = 99
    with pytest.raises(TypeError):
        evt.detail["new"] = "x"


def test_timeline_event_repr_omits_detail():
    evt = TimelineEvent(event_type="state_changed", detail={"error_code": "SECRET"}, created_at=1.0)
    assert "SECRET" not in repr(evt)


# --- serialize_timeline_event (fail-closed redaction) -----------------------

def test_timeline_redaction_allows_only_safe_keys():
    evt = TimelineEvent(
        event_type="created",
        detail={"recipients": 2, "to": "available", "error_code": "x",
                "file_name": "SECRET.txt", "comment": "SECRET", "token": "SECRET"},
        created_at=1.0,
    )
    out = serialize_timeline_event(evt)
    assert out["detail"] == {"recipients": 2, "to": "available", "error_code": "x"}
    # The unsafe keys are dropped, not leaked - fail closed.
    assert "file_name" not in out["detail"]
    assert "comment" not in out["detail"]
    assert "token" not in out["detail"]


def test_timeline_redaction_handles_empty_detail():
    evt = TimelineEvent(event_type="ack_downloaded_sent", detail={}, created_at=1.0)
    assert serialize_timeline_event(evt)["detail"] == {}


# --- serialize_recipient / serialize_delivery / serialize_idempotency -------

def test_serialize_recipient_uses_only_two_identifiers():
    r = RecipientRecord(key_id="env-1", principal_id="abc123")
    assert serialize_recipient(r) == {"key_id": "env-1", "principal_id": "abc123"}


def test_serialize_delivery_projection_is_exact():
    d = DeliveryRecord(
        id="d1", adapter_id="meshtastic", connector_profile_id="cp-1",
        route_type="direct", route_id="!deadbeef", state="sent",
        external_message_id="ext-9", sent_at=5.0,
    )
    out = serialize_delivery(d)
    # No idempotency_key / error_code / retry_at leak (§7.1 is exact).
    assert out == {
        "id": "d1", "adapter_id": "meshtastic", "connector_profile_id": "cp-1",
        "route_type": "direct", "route_id": "!deadbeef", "state": "sent",
        "external_message_id": "ext-9", "sent_at": 5.0,
    }


def test_serialize_idempotency_entry():
    e = IdempotencyEntry(attachment_id="att-1", canonical_hash="a" * 64, created_at=1.0)
    assert serialize_idempotency_entry(e) == {
        "attachment_id": "att-1", "canonical_hash": "a" * 64, "created_at": 1.0,
    }


# --- serialize_attachment_public --------------------------------------------

def _record(**overrides):
    base = dict(
        id="att-1", direction="received", state="AVAILABLE",
        file_name="photo.jpg", mime_type="image/jpeg", plain_size=10,
        cipher_size=64, created_at=0.0, hard_expires_at=999.0,
        download_grace_seconds=300, provider_id="AbCdEfGhIjK", saved=True,
        primary_delivery_id=None, error_code=None,
        recipients=(), deliveries=(), descriptor=None, timeline=(),
    )
    base.update(overrides)
    return AttachmentRecord(**base)


def test_public_serializer_never_leaks_locator_or_descriptor(paths):
    desc = ContentDescriptor(
        attachment_id="att-1", locator="files/photo.jpg", mime_type="image/jpeg",
        disposition=ContentDisposition.INLINE, plain_size=10,
    )
    record = _record(descriptor=desc)
    out = serialize_attachment_public(record)
    # The public projection carries booleans only - never the descriptor or
    # its locator, never a saved_path.
    assert out["content_available"] is True
    assert "descriptor" not in out
    assert "locator" not in out
    assert "saved_path" not in out
    assert "files/photo.jpg" not in repr(out)


def test_public_serializer_content_available_false_without_descriptor():
    out = serialize_attachment_public(_record(descriptor=None, saved=False))
    assert out["content_available"] is False
    assert out["saved"] is False


def test_list_serialization_omits_timeline_but_detail_includes_it():
    record = _record(timeline=(TimelineEvent(event_type="created", detail={"recipients": 1}, created_at=1.0),))
    assert "timeline" not in serialize_attachment_public(record)
    out = serialize_attachment_public(record, include_timeline=True)
    assert out["timeline"] == [{"event_type": "created", "detail": {"recipients": 1}, "created_at": 1.0}]


# --- AttachmentRecord.content_available -------------------------------------

def test_attachment_record_content_available_derived_from_descriptor():
    assert _record(descriptor=None).content_available is False
    desc = ContentDescriptor(
        attachment_id="att-1", locator="files/x", mime_type="text/plain",
        disposition=ContentDisposition.ATTACHMENT, plain_size=0,
    )
    assert _record(descriptor=desc).content_available is True


# --- build_attachments_snapshot ---------------------------------------------

_SCHEMA_BASE = """
CREATE TABLE attachments (
    id TEXT, workspace_id TEXT, direction TEXT, state TEXT, file_name TEXT,
    mime_type TEXT, plain_size INTEGER, cipher_size INTEGER, created_at REAL,
    hard_expires_at REAL, download_grace_seconds INTEGER, provider_id TEXT,
    saved_path TEXT, primary_delivery_id TEXT, error_code TEXT,
    client_request_id TEXT, canonical_hash TEXT
);
CREATE TABLE attachment_recipients (
    id INTEGER, attachment_id TEXT, envelope_id TEXT, recipient_principal_id TEXT
);
CREATE TABLE attachment_deliveries (
    id TEXT, attachment_id TEXT, adapter_id TEXT, connector_profile_id TEXT,
    route_type TEXT, route_id TEXT, state TEXT, external_message_id TEXT,
    sent_at REAL
);
CREATE TABLE attachment_events (
    id INTEGER, attachment_id TEXT, occurred_at REAL, event_type TEXT,
    detail_json TEXT
);
"""

# The snapshot-dirty tracking (migration 12): a `mca_dirty_attachments`
# singleton table plus one AFTER trigger per (table, event) on exactly the
# four projected tables. Each trigger records the affected `attachment_id`
# (NEW.id/OLD.id for `attachments`, NEW/OLD.attachment_id for the children;
# UPDATE dirties both) via INSERT OR IGNORE so repeated writes coalesce. The
# UPDATE triggers carry a *multi-statement* BEGIN...END body (two INSERTs), so
# they cannot be fed through executescript() (which splits on `;`); the real
# migration works around this with its data_fixup hook, and `_create_schema`
# mirrors that by issuing each trigger with `conn.execute()`.
_DIRTY_TABLES = ("attachments", "attachment_recipients", "attachment_deliveries", "attachment_events")
_DIRTY_EVENTS = ("INSERT", "UPDATE", "DELETE")


def _dirty_id_column(table):
    return "id" if table == "attachments" else "attachment_id"


def _dirty_refs(table, event):
    column = _dirty_id_column(table)
    if event == "INSERT":
        return (f"NEW.{column}",)
    if event == "UPDATE":
        return (f"NEW.{column}", f"OLD.{column}")
    return (f"OLD.{column}",)


def _dirty_trigger_sql(table, event):
    body = " ".join(
        f"INSERT OR IGNORE INTO mca_dirty_attachments (attachment_id) VALUES ({ref});"
        for ref in _dirty_refs(table, event)
    )
    return (
        f"CREATE TRIGGER trg_{table}_snapshot_dirty_{event.lower()} "
        f"AFTER {event} ON {table} BEGIN {body} END"
    )


# An unrelated MCA table with NO trigger - a write here must NOT record a
# dirty id (unrelated writes must not force a rebuild).
_UNRELATED_SCHEMA = """
CREATE TABLE mca_ack_quota (
    workspace_id TEXT, scope TEXT, window_start_at INTEGER, count INTEGER
);
"""

_BASE_SCHEMA = (
    _SCHEMA_BASE
    + "CREATE TABLE mca_dirty_attachments (attachment_id TEXT PRIMARY KEY);"
    + _UNRELATED_SCHEMA
)


def _create_schema(conn):
    conn.executescript(_BASE_SCHEMA)
    for table in _DIRTY_TABLES:
        for event in _DIRTY_EVENTS:
            conn.execute(_dirty_trigger_sql(table, event))


@pytest.fixture()
def conn():
    # isolation_level=None -> autocommit, so each statement (and its dirty-id
    # trigger) commits independently and an explicit `BEGIN`/`rollback` in a
    # test is a real transaction, not an error atop an implicit one.
    c = sqlite3.connect(":memory:", isolation_level=None)
    _create_schema(c)
    return c


def _build(conn, workspace_manager, *, max_detail_events=MAX_DETAIL_EVENTS, now=0.0):
    return build_attachments_snapshot(
        conn,
        workspace_id="local",
        workspace_manager=workspace_manager,
        principal_id=PRINCIPAL_ID,
        now=now,
        max_detail_events=max_detail_events,
    )


def test_build_sent_row_has_no_content(paths, conn, workspace_manager):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, direction, state, file_name, "
        "mime_type, plain_size, cipher_size, created_at, hard_expires_at, "
        "download_grace_seconds, provider_id, saved_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("att-1", "local", "sent", "queued", "photo.jpg", "image/jpeg", 10, 64,
         0.0, 999.0, 300, "AbCdEfGhIjK", str(paths.spool_outgoing / "uuid")),
    )
    snap = _build(conn, workspace_manager)
    record = snap.by_id["att-1"]
    assert record.saved is False
    assert record.content_available is False
    assert record.descriptor is None
    # No recipients/deliveries for this row.
    assert record.recipients == ()
    assert record.deliveries == ()


def test_build_received_available_row_has_content(paths, conn, workspace_manager):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, direction, state, file_name, "
        "mime_type, plain_size, cipher_size, created_at, hard_expires_at, "
        "download_grace_seconds, provider_id, saved_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("att-2", "local", "received", "AVAILABLE", "photo.jpg", "image/jpeg", 10, 64,
         0.0, 999.0, 300, "AbCdEfGhIjK", str(paths.files / "photo.jpg")),
    )
    snap = _build(conn, workspace_manager)
    record = snap.by_id["att-2"]
    assert record.saved is True
    assert record.content_available is True
    assert record.descriptor is not None
    assert record.descriptor.disposition is ContentDisposition.INLINE
    assert record.descriptor.locator == "files/photo.jpg"


def test_build_idempotency_index_from_client_request_id(paths, conn, workspace_manager):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, direction, state, created_at, "
        "hard_expires_at, download_grace_seconds, client_request_id, canonical_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("att-3", "local", "sent", "queued", 0.0, 999.0, 300, "req-abc-123", "f" * 64),
    )
    snap = _build(conn, workspace_manager)
    entry = snap.idempotency["req-abc-123"]
    assert entry.attachment_id == "att-3"
    assert entry.canonical_hash == "f" * 64


def test_build_skips_idempotency_row_with_null_hash(paths, conn, workspace_manager):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, direction, state, created_at, "
        "hard_expires_at, download_grace_seconds, client_request_id, canonical_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("att-4", "local", "sent", "queued", 0.0, 999.0, 300, "req-null", None),
    )
    snap = _build(conn, workspace_manager)
    assert "req-null" not in snap.idempotency


def test_build_timeline_is_bounded(paths, conn, workspace_manager):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, direction, state, created_at, "
        "hard_expires_at, download_grace_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("att-5", "local", "sent", "queued", 0.0, 999.0, 300),
    )
    for i in range(10):
        conn.execute(
            "INSERT INTO attachment_events (attachment_id, occurred_at, event_type, detail_json) "
            "VALUES (?, ?, ?, ?)",
            ("att-5", float(i), "state_changed", '{"to": "x"}'),
        )
    snap = _build(conn, workspace_manager, max_detail_events=3)
    record = snap.by_id["att-5"]
    assert len(record.timeline) == 3  # bounded to the most recent three


def test_build_tolerates_malformed_detail_json(paths, conn, workspace_manager):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, direction, state, created_at, "
        "hard_expires_at, download_grace_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("att-6", "local", "sent", "queued", 0.0, 999.0, 300),
    )
    conn.execute(
        "INSERT INTO attachment_events (attachment_id, occurred_at, event_type, detail_json) "
        "VALUES (?, ?, ?, ?)",
        ("att-6", 1.0, "state_changed", "{not json"),
    )
    snap = _build(conn, workspace_manager)
    assert snap.by_id["att-6"].timeline[0].detail == {}


def test_build_assembles_recipients_and_deliveries(paths, conn, workspace_manager):
    conn.execute(
        "INSERT INTO attachments (id, workspace_id, direction, state, created_at, "
        "hard_expires_at, download_grace_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("att-7", "local", "received", "AVAILABLE", 0.0, 999.0, 300),
    )
    conn.execute(
        "INSERT INTO attachment_recipients (attachment_id, envelope_id, recipient_principal_id) "
        "VALUES (?, ?, ?)",
        ("att-7", "env-1", "recip-a"),
    )
    conn.execute(
        "INSERT INTO attachment_deliveries (id, attachment_id, adapter_id, connector_profile_id, "
        "route_type, route_id, state, external_message_id, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("d1", "att-7", "meshtastic", "cp-1", "direct", "!dead", "sent", "ext-9", 5.0),
    )
    snap = _build(conn, workspace_manager)
    record = snap.by_id["att-7"]
    assert record.recipients == (RecipientRecord(key_id="env-1", principal_id="recip-a"),)
    assert record.deliveries[0].id == "d1"
    assert record.deliveries[0].external_message_id == "ext-9"


def test_build_snapshot_is_frozen(paths, conn, workspace_manager):
    snap = _build(conn, workspace_manager)
    with pytest.raises(TypeError):
        snap.by_id["new"] = None
    with pytest.raises(TypeError):
        snap.idempotency["new"] = None


# --- AttachmentsSnapshotPublisher -------------------------------------------

class Clock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture()
def clock():
    return Clock()


def _publisher(clock, *, max_detail_events=MAX_DETAIL_EVENTS):
    return AttachmentsSnapshotPublisher(now_fn=clock, max_detail_events=max_detail_events)


def _insert_attachment(conn, attachment_id, *, workspace_id="local", state="queued",
                       direction="sent", created_at=0.0, **extra):
    cols = ["id", "workspace_id", "direction", "state", "created_at", "hard_expires_at", "download_grace_seconds"]
    vals = [attachment_id, workspace_id, direction, state, created_at, 999.0, 300]
    for key, value in extra.items():
        cols.append(key)
        vals.append(value)
    conn.execute(f"INSERT INTO attachments ({', '.join(cols)}) VALUES ({','.join('?' for _ in cols)})", vals)


def test_publisher_snapshot_is_none_before_first_refresh(clock):
    pub = _publisher(clock)
    assert pub.snapshot() is None


def test_publisher_refresh_builds_and_publishes(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    snap = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert snap is not None
    assert pub.snapshot() is snap


def test_publisher_does_not_republish_unchanged_data(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    # No data change -> no dirty ids -> same object, no republish.
    assert first is second


def test_publisher_short_circuits_build_when_unchanged(monkeypatch, paths, conn, workspace_manager, clock):
    """No relevant write -> no dirty ids -> the tick returns the previous
    snapshot without running *either* the full build *or* a per-attachment
    projection fetch (the O(N)/per-row work never runs)."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is not None

    def _boom(*_args, **_kwargs):
        raise AssertionError("no per-tick rebuild when nothing changed")

    monkeypatch.setattr("meshsrv.attachments.snapshots.build_attachments_snapshot", _boom)
    monkeypatch.setattr("meshsrv.attachments.snapshots._fetch_attachment_projection", _boom)
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is second


def test_publisher_short_circuits_again_after_a_rebuild(monkeypatch, paths, conn, workspace_manager, clock):
    """After a real change forces an incremental rebuild, the dirty ids are
    drained, so a subsequent unchanged tick short-circuits again."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1", state="queued")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is not second
    assert second.by_id["att-1"].state == "sending"

    def _boom(*_args, **_kwargs):
        raise AssertionError("no rebuild after the dirty ids are drained")

    monkeypatch.setattr("meshsrv.attachments.snapshots._fetch_attachment_projection", _boom)
    third = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert third is second


def test_publisher_republishes_on_data_change(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1", state="queued")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is not second
    assert second.by_id["att-1"].state == "sending"


# --- incremental publication (Step 1.6A.1 correction; §7 correctness) -------

def test_incremental_insert_adds_new_projection(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is not None
    assert first.by_id == {}

    _insert_attachment(conn, "att-1")
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second is not first
    assert set(second.by_id) == {"att-1"}
    assert second.by_id["att-1"].state == "queued"


def test_incremental_update_rebuilds_only_that_attachment(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1", state="queued")
    _insert_attachment(conn, "att-2", state="queued")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert set(first.by_id) == {"att-1", "att-2"}

    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second is not first
    assert second.by_id["att-1"].state == "sending"
    # Incremental: att-2's record object is identity-preserved (only att-1 was
    # rebuilt) - the O(N) pass did not run.
    assert second.by_id["att-2"] is first.by_id["att-2"]


def test_incremental_delete_removes_projection(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    _insert_attachment(conn, "att-2")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert set(first.by_id) == {"att-1", "att-2"}

    conn.execute("DELETE FROM attachments WHERE id = 'att-1'")
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second is not first
    assert "att-1" not in second.by_id
    assert "att-2" in second.by_id
    assert second.records == (second.by_id["att-2"],)


def test_incremental_recipient_delivery_event_changes(paths, conn, workspace_manager, clock):
    """A write to any of the three child tables dirties its attachment and
    rebuilds just that projection - not a content-level fingerprint on the
    attachments row alone."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1", direction="received", state="AVAILABLE")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first.by_id["att-1"].recipients == ()
    assert first.by_id["att-1"].deliveries == ()
    assert first.by_id["att-1"].timeline == ()

    conn.execute(
        "INSERT INTO attachment_recipients (attachment_id, envelope_id, recipient_principal_id) "
        "VALUES (?, ?, ?)",
        ("att-1", "env-1", "recip-a"),
    )
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second is not first
    assert second.by_id["att-1"].recipients == (RecipientRecord(key_id="env-1", principal_id="recip-a"),)

    conn.execute(
        "INSERT INTO attachment_deliveries (id, attachment_id, adapter_id, connector_profile_id, "
        "route_type, route_id, state, external_message_id, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("d1", "att-1", "meshtastic", "cp-1", "direct", "!dead", "sent", "ext-9", 5.0),
    )
    third = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert third is not second
    assert third.by_id["att-1"].deliveries[0].id == "d1"

    conn.execute(
        "INSERT INTO attachment_events (attachment_id, occurred_at, event_type, detail_json) "
        "VALUES (?, ?, ?, ?)",
        ("att-1", 1.0, "state_changed", '{"to": "sending"}'),
    )
    fourth = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert fourth is not third
    assert fourth.by_id["att-1"].timeline[0].event_type == "state_changed"

    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    fifth = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert fifth is not fourth
    assert fifth.by_id["att-1"].state == "sending"


def test_incremental_several_attachments_in_one_transaction(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    conn.execute("BEGIN")
    _insert_attachment(conn, "att-2")
    _insert_attachment(conn, "att-3")
    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    conn.commit()

    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert set(second.by_id) == {"att-1", "att-2", "att-3"}
    assert second.by_id["att-1"].state == "sending"
    assert second.by_id["att-2"].state == "queued"
    assert second.by_id["att-3"].state == "queued"


def test_incremental_repeated_changes_coalesce_before_publication(paths, conn, workspace_manager, clock):
    """Several writes to the same attachment before any refresh coalesce to a
    single dirty id (INSERT OR IGNORE) and produce one rebuild with the final
    state - never three rebuilds, never an intermediate state published."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1", state="queued")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    conn.execute("UPDATE attachments SET state = 'sent' WHERE id = 'att-1'")
    conn.execute("UPDATE attachments SET state = 'failed' WHERE id = 'att-1'")

    assert list_dirty_attachment_ids(conn) == ["att-1"]  # coalesced

    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second.by_id["att-1"].state == "failed"
    assert list_dirty_attachment_ids(conn) == []  # drained


def test_incremental_rollback_leaves_no_dirty_id(monkeypatch, paths, conn, workspace_manager, clock):
    """A rolled-back relevant write leaves no dirty id (the trigger's INSERT
    rolls back with it), so it neither spurs a rebuild nor makes the set miss
    the next committed write."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    conn.execute("BEGIN")
    _insert_attachment(conn, "att-rolled-back")
    conn.rollback()
    assert list_dirty_attachment_ids(conn) == []  # trigger insert rolled back

    def _boom(*_args, **_kwargs):
        raise AssertionError("rolled-back write must not force a rebuild")

    monkeypatch.setattr("meshsrv.attachments.snapshots._fetch_attachment_projection", _boom)
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second is first  # no spurious rebuild

    monkeypatch.undo()
    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    third = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert third is not second  # the next committed write is NOT missed
    assert third.by_id["att-1"].state == "sending"


def test_incremental_delete_then_recreate(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1", state="queued")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first.by_id["att-1"].state == "queued"

    conn.execute("DELETE FROM attachments WHERE id = 'att-1'")
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert "att-1" not in second.by_id

    _insert_attachment(conn, "att-1", state="sending")
    third = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert "att-1" in third.by_id
    assert third.by_id["att-1"].state == "sending"


def test_incremental_restart_with_pending_dirty_records(paths, conn, workspace_manager, clock):
    """A publisher that dies (or a process restart) with dirty records still
    pending in the DB: a fresh publisher's first full build subsumes them and
    clears the set, producing a correct snapshot."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1", state="queued")
    pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    # A write that was never drained (e.g. crash before the next tick).
    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    _insert_attachment(conn, "att-2", state="queued")
    assert list_dirty_attachment_ids(conn) == ["att-1", "att-2"]

    pub2 = _publisher(clock)  # a brand-new publisher (fresh worker process)
    snap = pub2.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert snap is not None
    assert snap.by_id["att-1"].state == "sending"
    assert snap.by_id["att-2"].state == "queued"
    assert list_dirty_attachment_ids(conn) == []  # drained by the fresh publisher


def test_incremental_bounded_timeline(paths, conn, workspace_manager, clock):
    pub = _publisher(clock, max_detail_events=3)
    _insert_attachment(conn, "att-1")
    pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)

    for i in range(10):
        conn.execute(
            "INSERT INTO attachment_events (attachment_id, occurred_at, event_type, detail_json) "
            "VALUES (?, ?, ?, ?)",
            ("att-1", float(i), "state_changed", '{"to": "x"}'),
        )
    snap = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert len(snap.by_id["att-1"].timeline) == 3  # incremental rebuild bounds the timeline too


def test_incremental_idempotency_index_updates(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first.idempotency == {}

    conn.execute(
        "UPDATE attachments SET client_request_id = ?, canonical_hash = ? WHERE id = 'att-1'",
        ("req-1", "f" * 64),
    )
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second.idempotency["req-1"].attachment_id == "att-1"
    assert second.idempotency["req-1"].canonical_hash == "f" * 64

    conn.execute("UPDATE attachments SET canonical_hash = ? WHERE id = 'att-1'", ("a" * 64,))
    third = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert third.idempotency["req-1"].canonical_hash == "a" * 64
    assert len(third.idempotency) == 1  # updated in place, not duplicated

    conn.execute("UPDATE attachments SET canonical_hash = NULL WHERE id = 'att-1'")
    fourth = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert fourth.idempotency == {}  # entry removed when the hash is cleared


def test_publisher_unrelated_write_does_not_rebuild(monkeypatch, paths, conn, workspace_manager, clock):
    """An unrelated MCA write (ACK quota - a table with no trigger) records no
    dirty id, so the next tick short-circuits the per-row work entirely."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is not None

    conn.execute(
        "INSERT INTO mca_ack_quota (workspace_id, scope, window_start_at, count) VALUES (?, ?, ?, ?)",
        ("local", "__global__", 1, 1),
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("unrelated write must not force a rebuild")

    monkeypatch.setattr("meshsrv.attachments.snapshots._fetch_attachment_projection", _boom)
    second = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is second


def test_publisher_detects_change_after_connection_recreation(tmp_path, workspace_manager, clock):
    """The dirty table is persisted in the DB file, not the connection object:
    a reset/reconnect (new sqlite3.connect to the same file) must not make the
    publisher miss a relevant write made through the new connection."""
    db_path = tmp_path / "attachments.db"
    conn1 = sqlite3.connect(str(db_path), isolation_level=None)
    _create_schema(conn1)
    _insert_attachment(conn1, "att-1")

    pub = _publisher(clock)
    first = pub.refresh(conn1, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert first is not None
    conn1.close()

    conn2 = sqlite3.connect(str(db_path), isolation_level=None)
    conn2.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")

    second = pub.refresh(conn2, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert second is not first
    assert second.by_id["att-1"].state == "sending"
    conn2.close()


def test_publisher_publishes_atomically_consistent_snapshot(paths, conn, workspace_manager, clock):
    """A reader holding the old reference is unaffected by a later swap; the
    published snapshot is a single, complete, immutable object (records and
    by_id always agree - never a partial/tearing view)."""
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    _insert_attachment(conn, "att-2")
    first = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert [r.id for r in first.records] == ["att-1", "att-2"]
    assert set(first.records) == set(first.by_id.values())

    old_ref = pub.snapshot()
    _insert_attachment(conn, "att-3")
    new = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert old_ref is not new
    assert "att-3" not in old_ref.by_id  # the old snapshot is unchanged and complete
    assert "att-3" in new.by_id
    assert [r.id for r in new.records] == ["att-1", "att-2", "att-3"]


def test_publisher_keeps_last_known_good_on_incremental_failure(paths, conn, workspace_manager, clock):
    pub = _publisher(clock)
    _insert_attachment(conn, "att-1")
    good = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert good is not None

    # Drop a child table so the next incremental rebuild of att-1 raises; the
    # update's own trigger survives (attachments table is intact), so att-1 is
    # still dirty.
    conn.execute("DROP TABLE attachment_events")
    conn.execute("UPDATE attachments SET state = 'sending' WHERE id = 'att-1'")
    still_good = pub.refresh(conn, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    # Last-known-good retained - never replaced by a broken/partial snapshot.
    assert still_good is good
    assert pub.snapshot() is good


def test_publisher_first_build_failure_returns_none(workspace_manager, clock):
    pub = _publisher(clock)
    empty = sqlite3.connect(":memory:")  # no tables -> build raises internally
    result = pub.refresh(empty, workspace_id="local", workspace_manager=workspace_manager, principal_id=PRINCIPAL_ID)
    assert result is None
    assert pub.snapshot() is None
