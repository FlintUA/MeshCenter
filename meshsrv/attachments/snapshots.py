"""meshsrv/attachments/snapshots.py

The worker-owned snapshot publisher and the immutable projection types it
publishes (internal-rest-api.md §3.2/§3.3/§3.7, §7.5/§7.9/§7.12; Execution
Plan Step 1.6A.1). MIT-licensed Core code - stdlib only, never
`meshtastic`, never Flask.

Threading model (the whole point of §3.1, and the reason this module
exists as a separate concern rather than a pile of `SELECT`s scattered
through a request handler):

- **the worker thread** (`AttachmentsService`'s own thread, the single
  owner of `conn`) is the *only* thing that ever calls `refresh()`/the
  `build_attachments_snapshot()` read pass below. It reads `conn`, builds
  a whole new immutable `AttachmentsSnapshot` by copy, and swaps it in
  with one reference assignment (`AttachmentsSnapshotPublisher._snapshot`).
- **request threads** read only `AttachmentsSnapshotPublisher.snapshot()`
  - a single reference read, atomic under the GIL, never `conn`, never the
  filesystem, never the network. The immutable snapshot is the *only*
  thing `GET /api/attachments` (and its detail/deliveries siblings, Step
  1.6A.2) and the facade's read methods will ever touch.

`ConnectivityMonitor` already demonstrates this exact pattern
(`_profile_snapshot`); this module is the same idea applied to the
attachment/recipient/delivery/event/idempotency tables.

Why a dedicated publisher, not the worker's existing automatic-state row
scan (§3.3): that scan walks only the bounded set of `AUTOMATIC_STATES`
rows the tick must advance. It does not contain full history, recipients,
deliveries, or the (redacted) event timeline - and the request-facing read
surface needs all of those. The publisher rebuilds *incrementally*: the
four projected tables carry AFTER triggers (migration 12) that record the
affected `attachment_id` in `mca_dirty_attachments` transactionally; the
worker drains and deduplicates those dirty ids each tick and rebuilds only
the affected attachment's projection (and its bounded timeline), never the
whole O(N) snapshot. Deleting an attachment removes its projection. The
complete immutable snapshot reference is then swapped in atomically, and
request threads remain SQLite-free (they read only the published
reference). A full build (`build_attachments_snapshot`) still exists for
the very first publish and the benchmark, but it is not the per-tick path.
The per-dirty *rebuild* is O(1), but each publish still materializes a
fresh immutable container — `records` (tuple) plus `by_id`/`idempotency`
(dicts) — so the complete snapshot stays reference-atomic; that shallow
reference copy is O(N) in the number of attachments (measured ~22 ms at
N=5000 vs ~3.6 s for the full build on a Pi Zero 2 W — the §15.2 benchmark
reports this residual, which the target hardware's RAM ceiling keeps to tens
of milliseconds).
`conn.total_changes` is deliberately *not* used: it is connection-global
and is advanced by unrelated MCA writes (ACK quota, Relay health, reply
outbox), which under an active-write workload would otherwise force a
rebuild every tick.

No-secret discipline (§11), enforced by *construction* here, not by
convention at every call site:

- `AttachmentRecord` carries a `ContentDescriptor` (internal file
  location) but the public serializer (`serialize_attachment_public`)
  never emits the descriptor's `locator` - only `content_available`
  (a bool) and `saved` (a bool) reach the browser, never `saved_path`,
  never an absolute path, never `draft_comment`/`receipt_secret_hash`/
  tokens/keys/raw MCA pointers (§7.5, §3.7).
- The timeline serializer applies a **safe-key allowlist** to each
  event's `detail` and drops any key outside it (fail-closed redaction) -
  so a future event type that accidentally writes a filename/comment/token
  into `detail_json` is silently stripped, never leaked (§11 item 6).

The constants below are named, single-source placeholders chosen *after*
the Step 1.6A.1 snapshot-cost benchmark (§15.2), not measured numbers.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
import sqlite3
import threading
import time
import types
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from meshsrv.attachments.idempotency import IdempotencyEntry
from meshsrv.attachments.workspace import MCAWorkspaceManager, WorkspacePaths

logger = logging.getLogger(__name__)

# ---- constants (§15.2: chosen from the Step 1.6A.1 snapshot-cost
# benchmark on a real Pi Zero 2 W - see scripts/benchmark_mca_snapshots.py)

# §3.3 / §7.1: upper bound on how many (redacted) timeline events one
# attachment's detail projection carries. Bounds snapshot memory per row.
MAX_DETAIL_EVENTS = 200


# ---- ContentDescriptor (§3.7) -------------------------------------------
#
# The content route serves a file from disk, not a SQLite row. A
# `content_available`/`saved` boolean alone is not enough for a request
# thread to locate the file safely, so the publisher attaches to each
# content-available attachment an immutable `ContentDescriptor` carrying a
# workspace-root-relative `locator`. The locator is *internal only*: it is
# stripped by the public serializer and never leaves the process. It is
# also held here (never an absolute path) and re-validated against the
# controlled workspace root by the content route at serve time (§3.7's
# "re-validates locator ... one path validation, not two independent
# resolutions" - the api_camera.py screenshot pattern).


class ContentLocatorError(ValueError):
    """Raised when a locator is absolute, traverses, or resolves outside
    the controlled content area - never silently corrected (fail closed,
    like `workspace.WorkspacePathError`)."""


class ContentDisposition(str, enum.Enum):
    """§7.14: inline only for decoded-and-verified preview images; every
    other MIME type is served as a download (`attachment`)."""

    INLINE = "inline"
    ATTACHMENT = "attachment"


# §7.14: the only MIME types served inline. Everything else - including
# PDF, SVG, HTML, archives - is `application/octet-stream` + attachment.
_INLINE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


def disposition_for_mime_type(mime_type: Optional[str]) -> ContentDisposition:
    """The one place the inline-vs-attachment decision is made. Pure. A
    `None`/unknown MIME type is `attachment`, never inline."""
    if mime_type in _INLINE_MIME_TYPES:
        return ContentDisposition.INLINE
    return ContentDisposition.ATTACHMENT


def validate_locator(locator: str) -> None:
    """Reject a locator that is not a safe, workspace-root-relative, file-
    naming path. Pure string math - no filesystem access (the snapshot
    build must not read the filesystem, §3.7). Enforces, fail-closed:

    - non-empty `str`;
    - no NUL;
    - POSIX `/` separators only (no `\\` - locators are always built via
      `Path.as_posix()`);
    - workspace-root-relative (not absolute: no leading `/`, no Windows
      drive `X:`);
    - names a file, not a directory (no trailing `/`);
    - no `.`/`..`/empty segments (traversal).

    Raises `ContentLocatorError` on the first violation."""
    if not isinstance(locator, str) or not locator:
        raise ContentLocatorError("locator must be a non-empty string")
    if "\x00" in locator:
        raise ContentLocatorError("locator contains a NUL byte")
    if "\\" in locator:
        raise ContentLocatorError("locator must use '/' separators, not '\\\\'")
    if locator.startswith("/") or (len(locator) >= 2 and locator[1] == ":"):
        raise ContentLocatorError("locator must be workspace-root-relative, not absolute")
    if locator.endswith("/"):
        raise ContentLocatorError("locator must name a file, not a directory")
    for segment in locator.split("/"):
        if segment in ("", ".", ".."):
            raise ContentLocatorError(f"locator has an unsafe segment: {segment!r}")


def make_locator(paths: WorkspacePaths, absolute_path: "Optional[Any]") -> Optional[str]:
    """Build a workspace-root-relative locator from an absolute path, or
    return `None` if the path is not a servable content file. Pure lexical
    math - **no filesystem access** (the snapshot build must never read the
    filesystem, and must never throw on one bad row, §3.7).

    A path is servable only when it is absolute, lexically inside the
    workspace root, and under `files/` or `cache/incoming/`, naming a file
    (not the directory itself). Anything else - a sent row's `saved_path`
    still pointing at `spool/outgoing/`, a malformed/traversing path, a
    relative path - is not servable and returns `None` rather than raising,
    so one corrupt row cannot fail the whole snapshot build. The locator is
    then lexically re-validated (`validate_locator`) before being trusted.
    """
    if absolute_path is None:
        return None
    raw = str(absolute_path)
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None
    try:
        relative = candidate.relative_to(paths.root)
    except ValueError:
        return None
    parts = relative.parts
    if parts and parts[0] == "files":
        if len(parts) < 2:
            return None  # names the directory itself, not a file
    elif len(parts) >= 2 and parts[:2] == ("cache", "incoming"):
        if len(parts) < 3:
            return None  # names the directory itself, not a file
    else:
        return None
    locator = relative.as_posix()
    try:
        validate_locator(locator)
    except ContentLocatorError:
        return None
    return locator


def resolve_locator(paths: WorkspacePaths, locator: str) -> Path:
    """Resolve a descriptor's locator back to an absolute path inside the
    controlled content area, for the content route to stream (§3.7). This
    is the *serve-time* re-validation - the one place a filesystem read is
    made, since a symlink's target can only be observed by resolving it.

    `.resolve()` follows symlinks, so a symlink under `files/`/`cache/
    incoming/` that points *outside* the controlled area resolves outside
    it and is rejected here; one that points inside is fine (still within
    the controlled area). A locator that fails lexical validation, or that
    resolves outside `files/`/`cache/incoming/`, raises
    `ContentLocatorError` - never silently served.
    """
    validate_locator(locator)
    candidate = (paths.root / locator).resolve()
    allowed = (paths.files.resolve(), paths.cache_incoming.resolve())
    if not any(candidate == directory or directory in candidate.parents for directory in allowed):
        raise ContentLocatorError("locator resolves outside the controlled content area")
    return candidate


@dataclasses.dataclass(frozen=True)
class ContentDescriptor:
    """§3.7 - the internal, immutable file-location record attached to a
    content-available attachment. `locator` is workspace-root-relative and
    **never serialized**: the public serializer omits it, and this type's
    own `repr` omits it too, so it can never leak through a log line or a
    debugger. `plain_size` is the verified plaintext size; `disposition`
    comes from `disposition_for_mime_type()`."""

    attachment_id: str
    locator: str
    mime_type: str
    disposition: ContentDisposition
    plain_size: int

    def __post_init__(self) -> None:
        if not self.attachment_id:
            raise ValueError("attachment_id must not be empty")
        validate_locator(self.locator)
        if not isinstance(self.disposition, ContentDisposition):
            raise ValueError(f"disposition must be a ContentDisposition, got {self.disposition!r}")
        if not isinstance(self.plain_size, int) or self.plain_size < 0:
            raise ValueError(f"plain_size must be a non-negative int, got {self.plain_size!r}")

    def __repr__(self) -> str:
        # No locator - the one line of defence that keeps an accidentally
        # logged descriptor from leaking a filesystem path.
        return (
            f"ContentDescriptor(attachment_id={self.attachment_id!r}, "
            f"mime_type={self.mime_type!r}, disposition={self.disposition.value!r})"
        )


# ---- snapshot value types (immutable, worker-built) ---------------------


@dataclasses.dataclass(frozen=True)
class RecipientRecord:
    """One recipient of an attachment, the §7.5 `recipients` item. Only the
    two safe identifiers reach the browser - never `receipt_secret_hash`,
    `envelope_id`, `received_at`/`downloaded_at`."""

    key_id: str
    principal_id: Optional[str]


@dataclasses.dataclass(frozen=True)
class DeliveryRecord:
    """One delivery route, the §7.1 `deliveries` item. `external_message_id`
    is nullable; no token, no `idempotency_key`, no `error_code`/`retry_at`
    leak here (the projection is exactly §7.1)."""

    id: str
    adapter_id: str
    connector_profile_id: str
    route_type: str
    route_id: str
    state: str
    external_message_id: Optional[str]
    sent_at: Optional[float]


@dataclasses.dataclass(frozen=True)
class TimelineEvent:
    """One redacted timeline entry (§7.1). `detail` is frozen to a read-only
    mapping; its *contents* are further filtered by the serializer's safe-key
    allowlist (§11), not trusted verbatim."""

    event_type: str
    detail: Mapping[str, Any]
    created_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", types.MappingProxyType(dict(self.detail)))

    def __repr__(self) -> str:
        return f"TimelineEvent(event_type={self.event_type!r}, created_at={self.created_at!r})"


@dataclasses.dataclass(frozen=True)
class AttachmentRecord:
    """The internal, immutable per-attachment projection the snapshot holds.
    `descriptor` is `None` unless content is available; `content_available`
    is *derived* from that (a descriptor is only ever attached when content
    is actually available - §7.5), never from a `Path.exists()` filesystem
    probe. `timeline` is bounded to `MAX_DETAIL_EVENTS` by the builder."""

    id: str
    direction: str
    state: str
    file_name: Optional[str]
    mime_type: Optional[str]
    plain_size: Optional[int]
    cipher_size: Optional[int]
    created_at: float
    hard_expires_at: float
    download_grace_seconds: int
    provider_id: Optional[str]
    saved: bool
    primary_delivery_id: Optional[str]
    error_code: Optional[str]
    recipients: Tuple[RecipientRecord, ...]
    deliveries: Tuple[DeliveryRecord, ...]
    descriptor: Optional[ContentDescriptor]
    timeline: Tuple[TimelineEvent, ...]

    @property
    def content_available(self) -> bool:
        """§7.5: true only when a `ContentDescriptor` was attached (a
        received attachment in `AVAILABLE`, or a sent attachment with
        `saved=true`) - derived from state, never from a filesystem probe."""
        return self.descriptor is not None

    def __post_init__(self) -> None:
        object.__setattr__(self, "recipients", tuple(self.recipients))
        object.__setattr__(self, "deliveries", tuple(self.deliveries))
        object.__setattr__(self, "timeline", tuple(self.timeline))


@dataclasses.dataclass(frozen=True)
class AttachmentsSnapshot:
    """The whole immutable snapshot: the compact list of records (each with
    recipients/deliveries/descriptor, bounded timeline), a by-id lookup, and
    the committed idempotency index. Frozen; `by_id`/`idempotency` are
    read-only mappings so a reader can never mutate an already-published
    snapshot."""

    records: Tuple[AttachmentRecord, ...]
    by_id: Mapping[str, AttachmentRecord]
    idempotency: Mapping[str, IdempotencyEntry]
    built_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "by_id", types.MappingProxyType(dict(self.by_id)))
        object.__setattr__(self, "idempotency", types.MappingProxyType(dict(self.idempotency)))


# ---- serializers (explicit allowlists, never dataclasses.asdict) --------

# §11: the only timeline `detail` keys that are safe to surface. Anything
# else (a future event writing a filename/comment/token) is dropped -
# fail-closed redaction, not a best-effort scrub.
_TIMELINE_DETAIL_ALLOWLIST = frozenset({"recipients", "to", "error_code"})


def serialize_recipient(recipient: RecipientRecord) -> dict:
    return {"key_id": recipient.key_id, "principal_id": recipient.principal_id}


def serialize_delivery(delivery: DeliveryRecord) -> dict:
    return {
        "id": delivery.id,
        "adapter_id": delivery.adapter_id,
        "connector_profile_id": delivery.connector_profile_id,
        "route_type": delivery.route_type,
        "route_id": delivery.route_id,
        "state": delivery.state,
        "external_message_id": delivery.external_message_id,
        "sent_at": delivery.sent_at,
    }


def serialize_timeline_event(event: TimelineEvent) -> dict:
    """Redacted §7.1 timeline entry - only the safe-key allowlist survives."""
    detail = {key: value for key, value in event.detail.items() if key in _TIMELINE_DETAIL_ALLOWLIST}
    return {"event_type": event.event_type, "detail": detail, "created_at": event.created_at}


def serialize_attachment_public(record: AttachmentRecord, *, include_timeline: bool = False) -> dict:
    """The §7.5 public projection, built field-by-field (never
    `dataclasses.asdict`). Deliberately omits `descriptor`/`locator` and
    `saved_path` - only `saved`/`content_available` booleans reach the
    browser. The list projection (`include_timeline=False`) carries no
    timeline; the detail projection (`include_timeline=True`) adds the
    bounded, redacted one."""
    out = {
        "id": record.id,
        "direction": record.direction,
        "state": record.state,
        "file_name": record.file_name,
        "mime_type": record.mime_type,
        "plain_size": record.plain_size,
        "cipher_size": record.cipher_size,
        "created_at": record.created_at,
        "hard_expires_at": record.hard_expires_at,
        "download_grace_seconds": record.download_grace_seconds,
        "provider_id": record.provider_id,
        "saved": record.saved,
        "content_available": record.content_available,
        "primary_delivery_id": record.primary_delivery_id,
        "error_code": record.error_code,
        "recipients": [serialize_recipient(r) for r in record.recipients],
        "deliveries": [serialize_delivery(d) for d in record.deliveries],
    }
    if include_timeline:
        out["timeline"] = [serialize_timeline_event(e) for e in record.timeline]
    return out


def serialize_idempotency_entry(entry: IdempotencyEntry) -> dict:
    return {
        "attachment_id": entry.attachment_id,
        "canonical_hash": entry.canonical_hash,
        "created_at": entry.created_at,
    }


# ---- the read-only build pass (worker-only) -----------------------------


def _parse_detail(detail_json: Optional[str]) -> Mapping[str, Any]:
    """Parse an event's `detail_json` into a dict, fail-closed: missing/`None`
    or malformed JSON becomes `{}` rather than raising - one corrupt row
    must never fail the whole snapshot build."""
    if not detail_json:
        return {}
    try:
        parsed = json.loads(detail_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _build_descriptor(
    paths: WorkspacePaths,
    *,
    attachment_id: str,
    direction: str,
    state: str,
    saved_path: Optional[str],
    mime_type: Optional[str],
    plain_size: Optional[int],
    saved: bool,
) -> Optional[ContentDescriptor]:
    """Attach a `ContentDescriptor` iff content is genuinely available, per
    §7.5: a received attachment in `AVAILABLE`, or a sent attachment with
    `saved=true`. The locator is derived from `saved_path` by pure lexical
    math (`make_locator`) - no filesystem read, and no descriptor (rather
    than a bogus one) if the path isn't servable."""
    content_available = (direction == "received" and state == "AVAILABLE") or saved
    if not content_available:
        return None
    locator = make_locator(paths, saved_path)
    if locator is None:
        return None
    return ContentDescriptor(
        attachment_id=attachment_id,
        locator=locator,
        mime_type=mime_type or "application/octet-stream",
        disposition=disposition_for_mime_type(mime_type),
        plain_size=plain_size if plain_size is not None else 0,
    )


def _recipient_from_row(row: sqlite3.Row) -> RecipientRecord:
    return RecipientRecord(key_id=row["envelope_id"], principal_id=row["recipient_principal_id"])


def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        id=row["id"],
        adapter_id=row["adapter_id"],
        connector_profile_id=row["connector_profile_id"],
        route_type=row["route_type"],
        route_id=row["route_id"],
        state=row["state"],
        external_message_id=row["external_message_id"],
        sent_at=row["sent_at"],
    )


def _event_from_row(row: sqlite3.Row) -> TimelineEvent:
    return TimelineEvent(
        event_type=row["event_type"],
        detail=_parse_detail(row["detail_json"]),
        created_at=row["occurred_at"],
    )


def _record_from_row(
    paths: WorkspacePaths,
    row: sqlite3.Row,
    recipients: Sequence[RecipientRecord],
    deliveries: Sequence[DeliveryRecord],
    events: Sequence[TimelineEvent],
) -> AttachmentRecord:
    """Convert one `attachments` row plus its already-fetched children into a
    frozen `AttachmentRecord`. Shared by the full build and the incremental
    rebuild so both project a row *identically* (a partial rebuild must never
    disagree with a full one on any field).

    `events` must already be *bounded and chronological* (oldest-first): the
    SQL query that fetched them - not this projection helper - owns the
    `MAX_DETAIL_EVENTS` bound, so an unbounded read can never reach this far
    into memory (§7.1). Both callers supply exactly the retained window, in
    `occurred_at`/`id` ascending order."""
    attachment_id = row["id"]
    saved_path = row["saved_path"]
    saved = make_locator(paths, saved_path) is not None and _is_inside_files(paths, saved_path)
    descriptor = _build_descriptor(
        paths,
        attachment_id=attachment_id,
        direction=row["direction"],
        state=row["state"],
        saved_path=saved_path,
        mime_type=row["mime_type"],
        plain_size=row["plain_size"],
        saved=saved,
    )
    timeline = tuple(events)
    return AttachmentRecord(
        id=attachment_id,
        direction=row["direction"],
        state=row["state"],
        file_name=row["file_name"],
        mime_type=row["mime_type"],
        plain_size=row["plain_size"],
        cipher_size=row["cipher_size"],
        created_at=row["created_at"],
        hard_expires_at=row["hard_expires_at"],
        download_grace_seconds=row["download_grace_seconds"],
        provider_id=row["provider_id"],
        saved=saved,
        primary_delivery_id=row["primary_delivery_id"],
        error_code=row["error_code"],
        recipients=tuple(recipients),
        deliveries=tuple(deliveries),
        descriptor=descriptor,
        timeline=timeline,
    )


def build_attachments_snapshot(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    workspace_manager: MCAWorkspaceManager,
    principal_id: str,
    now: float,
    max_detail_events: int = MAX_DETAIL_EVENTS,
) -> AttachmentsSnapshot:
    """The worker-only, read-only pass over `attachments`/`attachment_
    recipients`/`attachment_deliveries`/`attachment_events` (§3.3). Builds a
    whole new immutable `AttachmentsSnapshot` from fresh copies - never
    mutates the previously-published one.

    Batched queries (no N+1): one query per table, grouped in Python, rather
    than a per-attachment query fan-out. The event timeline is bounded per
    attachment *in SQL* to `max_detail_events` - a window function
    (`ROW_NUMBER() ... PARTITION BY attachment_id ORDER BY occurred_at DESC,
    id DESC`) keeps only the most-recent events per attachment, so an
    attachment's unbounded event history is never loaded into memory (§7.1).
    A single malformed row (bad `detail_json`, a non-servable `saved_path`)
    is tolerated - redacted to `{}` / no descriptor - never raised, so one
    corrupt row cannot take down the whole snapshot."""
    conn.row_factory = sqlite3.Row
    paths = workspace_manager.paths(principal_id)

    rows = conn.execute(
        "SELECT * FROM attachments WHERE workspace_id = ? ORDER BY created_at DESC, id",
        (workspace_id,),
    ).fetchall()
    recipient_rows = conn.execute(
        "SELECT attachment_id, envelope_id, recipient_principal_id FROM attachment_recipients "
        "WHERE attachment_id IN (SELECT id FROM attachments WHERE workspace_id = ?) ORDER BY id",
        (workspace_id,),
    ).fetchall()
    delivery_rows = conn.execute(
        "SELECT * FROM attachment_deliveries "
        "WHERE attachment_id IN (SELECT id FROM attachments WHERE workspace_id = ?) ORDER BY id",
        (workspace_id,),
    ).fetchall()
    event_rows = conn.execute(
        """
        SELECT attachment_id, occurred_at, event_type, detail_json
        FROM (
            SELECT attachment_id, occurred_at, event_type, detail_json, id,
                   ROW_NUMBER() OVER (
                       PARTITION BY attachment_id
                       ORDER BY occurred_at DESC, id DESC
                   ) AS rn
            FROM attachment_events
            WHERE attachment_id IN (SELECT id FROM attachments WHERE workspace_id = ?)
        )
        WHERE rn <= ?
        ORDER BY attachment_id, occurred_at, id
        """,
        (workspace_id, max_detail_events),
    ).fetchall()
    idempotency_rows = conn.execute(
        "SELECT id, client_request_id, canonical_hash, created_at FROM attachments "
        "WHERE workspace_id = ? AND client_request_id IS NOT NULL",
        (workspace_id,),
    ).fetchall()

    recipients_by_attachment: "dict[str, list]" = {}
    for row in recipient_rows:
        recipients_by_attachment.setdefault(row["attachment_id"], []).append(_recipient_from_row(row))

    deliveries_by_attachment: "dict[str, list]" = {}
    for row in delivery_rows:
        deliveries_by_attachment.setdefault(row["attachment_id"], []).append(_delivery_from_row(row))

    events_by_attachment: "dict[str, list]" = {}
    for row in event_rows:
        events_by_attachment.setdefault(row["attachment_id"], []).append(_event_from_row(row))

    records: "list[AttachmentRecord]" = []
    by_id: "dict[str, AttachmentRecord]" = {}
    for row in rows:
        attachment_id = row["id"]
        record = _record_from_row(
            paths,
            row,
            recipients_by_attachment.get(attachment_id, ()),
            deliveries_by_attachment.get(attachment_id, ()),
            events_by_attachment.get(attachment_id, ()),
        )
        records.append(record)
        by_id[attachment_id] = record

    idempotency: "dict[str, IdempotencyEntry]" = {}
    for row in idempotency_rows:
        if row["canonical_hash"] is None:
            # A committed idempotency row must carry a canonical_hash (§3.6);
            # a NULL one is a data-integrity oddity, never indexed.
            continue
        idempotency[row["client_request_id"]] = IdempotencyEntry(
            attachment_id=row["id"],
            canonical_hash=row["canonical_hash"],
            created_at=row["created_at"],
        )

    return AttachmentsSnapshot(
        records=tuple(records),
        by_id=by_id,
        idempotency=idempotency,
        built_at=now,
    )


def _is_inside_files(paths: WorkspacePaths, absolute_path: "Optional[Any]") -> bool:
    """True iff `absolute_path` is lexically inside the workspace `files/`
    directory (a saved copy, §7.5's `saved`), pure string math - no
    filesystem access."""
    if absolute_path is None:
        return False
    raw = str(absolute_path)
    if not raw:
        return False
    candidate = Path(raw)
    if not candidate.is_absolute():
        return False
    try:
        relative = candidate.relative_to(paths.files)
    except ValueError:
        return False
    return len(relative.parts) >= 1


@dataclasses.dataclass(frozen=True)
class _AttachmentProjection:
    """One attachment's freshly-read projection plus the two idempotency
    columns needed to update the idempotency index incrementally (without a
    separate query)."""

    record: AttachmentRecord
    client_request_id: Optional[str]
    canonical_hash: Optional[str]


def _fetch_attachment_projection(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    paths: WorkspacePaths,
    attachment_id: str,
    max_detail_events: int,
) -> Optional[_AttachmentProjection]:
    """Read one attachment's full projection (its row, recipients, deliveries,
    and bounded timeline) - or `None` if the row no longer exists (deleted).
    Per-attachment queries: cheap for a single dirty attachment, and the whole
    point of the incremental rebuild - one relevant write rebuilds one
    projection, never O(N)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM attachments WHERE workspace_id = ? AND id = ?",
        (workspace_id, attachment_id),
    ).fetchone()
    if row is None:
        return None
    recipient_rows = conn.execute(
        "SELECT attachment_id, envelope_id, recipient_principal_id FROM attachment_recipients "
        "WHERE attachment_id = ? ORDER BY id",
        (attachment_id,),
    ).fetchall()
    delivery_rows = conn.execute(
        "SELECT * FROM attachment_deliveries WHERE attachment_id = ? ORDER BY id",
        (attachment_id,),
    ).fetchall()
    event_rows = conn.execute(
        "SELECT attachment_id, occurred_at, event_type, detail_json FROM attachment_events "
        "WHERE attachment_id = ? ORDER BY occurred_at DESC, id DESC LIMIT ?",
        (attachment_id, max_detail_events),
    ).fetchall()
    # The SQL read is bounded and most-recent-first (the deterministic
    # `occurred_at DESC, id DESC` tie-break, §7.1); reverse it so the
    # projection's timeline is chronological (occurred_at/id ascending),
    # identical to the full build's presentation order.
    events = [_event_from_row(r) for r in event_rows]
    events.reverse()
    record = _record_from_row(
        paths,
        row,
        [_recipient_from_row(r) for r in recipient_rows],
        [_delivery_from_row(r) for r in delivery_rows],
        events,
    )
    return _AttachmentProjection(
        record=record,
        client_request_id=row["client_request_id"],
        canonical_hash=row["canonical_hash"],
    )


def list_dirty_attachment_ids(conn: sqlite3.Connection) -> Optional[List[str]]:
    """Read the dirty attachment ids (migration 12's `mca_dirty_attachments`,
    populated transactionally by triggers on exactly the four projected
    tables), deduplicated and in a deterministic order. Worker-only (like
    everything else that touches `conn`).

    Returns `None` when the dirty table is absent - an un-migrated/legacy DB,
    or a minimal test schema - so the caller falls back to *a full build*,
    never to a false "nothing changed" that could miss a relevant write. A
    real install always runs migration 12 at `open_attachments_db()`, so
    `None` is the defensive legacy fallback, not the normal path."""
    try:
        rows = conn.execute(
            "SELECT attachment_id FROM mca_dirty_attachments ORDER BY attachment_id"
        ).fetchall()
    except sqlite3.Error:
        return None
    return [row[0] for row in rows]


def clear_dirty_attachment_ids(conn: sqlite3.Connection, attachment_ids: Sequence[str]) -> None:
    """Remove exactly the drained dirty ids after their projections were
    rebuilt. Only the ids the caller actually processed are cleared, so a
    dirty id recorded by a later write (in a future tick) survives to be
    drained then."""
    if not attachment_ids:
        return
    placeholders = ",".join("?" for _ in attachment_ids)
    conn.execute(
        f"DELETE FROM mca_dirty_attachments WHERE attachment_id IN ({placeholders})",
        list(attachment_ids),
    )


def clear_all_dirty_attachment_ids(conn: sqlite3.Connection) -> None:
    """Clear every dirty id - used only after a full build, which by
    definition incorporated every committed projected row."""
    try:
        conn.execute("DELETE FROM mca_dirty_attachments")
    except sqlite3.Error:
        # Un-migrated legacy DB: no dirty table; a full build has already
        # incorporated every committed row, so there is nothing to clear.
        pass


# ---- the publisher (atomic swap + last-known-good retention) ------------


class AttachmentsSnapshotPublisher:
    """Owns the single atomically-published `AttachmentsSnapshot`. The
    worker thread calls `refresh()` (the only thing that reads `conn`); any
    thread calls `snapshot()` for a cheap, non-blocking read of the last
    published result.

    - **atomic replacement**: the complete immutable snapshot reference is
      swapped with one assignment under the lock (atomic under the GIL) - a
      concurrent reader sees either the complete old or the complete new
      snapshot, never a partial one.
    - **incremental publication**: after the first full build, `refresh()`
      drains the dirty-attachment ids recorded by migration 12's triggers and
      rebuilds *only* the affected projections (and their bounded timelines);
      a deleted attachment's projection is removed. Unrelated writes (ACK
      quota, Relay health, reply outbox - tables with no trigger) record no
      dirty id, so they never force a rebuild. The very first publish is a
      full `build_attachments_snapshot()`.
    - **last-known-good retention**: a failed rebuild (an unexpected DB error)
      is logged and the previous good snapshot is kept, never replaced by a
      partial/broken one; the dirty ids are left in place so the next tick
      retries. The very first build has nothing to keep, so it surfaces the
      error (returns `None`)."""

    def __init__(
        self,
        *,
        now_fn=time.time,
        max_detail_events: int = MAX_DETAIL_EVENTS,
    ):
        self._lock = threading.Lock()
        self._snapshot: Optional[AttachmentsSnapshot] = None
        # Worker-owned mutable projection state (only `refresh()` mutates it;
        # request threads never see it directly, only via the published
        # snapshot reference). `_order` mirrors `records`' created_at-DESC-
        # then-id order; `_idempotency_by_attachment` is the reverse index
        # that makes removing/updating one attachment's idempotency entry O(1).
        self._by_id: Dict[str, AttachmentRecord] = {}
        self._order: List[str] = []
        self._idempotency: Dict[str, IdempotencyEntry] = {}
        self._idempotency_by_attachment: Dict[str, str] = {}
        self._initialized = False
        self._now = now_fn
        self._max_detail_events = max_detail_events

    def snapshot(self) -> Optional[AttachmentsSnapshot]:
        """The request-thread read: a single reference read, atomic under
        the GIL, never `conn`/filesystem/network. `None` only before the
        worker's first successful publish."""
        with self._lock:
            return self._snapshot

    def refresh(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        workspace_manager: MCAWorkspaceManager,
        principal_id: str,
    ) -> Optional[AttachmentsSnapshot]:
        """Worker-only: bring the published snapshot up to date. Never raises -
        a build failure keeps the last-known-good snapshot (or `None`, if this
        is the very first build).

        The first call does a full `build_attachments_snapshot()` and records
        its projection + order + idempotency index. Subsequent calls drain the
        dirty-attachment ids (migration 12's triggers) and rebuild only the
        affected projections; with no dirty ids (and no un-migrated legacy
        table) the current snapshot is returned unchanged - the O(N) build
        never runs per-tick.

        The rebuild is built into *local copies* and the complete immutable
        snapshot is constructed *before* any dirty id is acknowledged; only
        then is the reference atomically swapped and the processed dirty ids
        deleted-and-committed. So a failed build/construction keeps the old
        snapshot and every dirty id, and a failed ack commit leaves the
        already-correct snapshot published and just retries next tick.
        """
        now = self._now()

        with self._lock:
            prev = self._snapshot
            initialized = self._initialized

        if not initialized:
            return self._first_publish(
                conn,
                workspace_id=workspace_id,
                workspace_manager=workspace_manager,
                principal_id=principal_id,
                now=now,
            )

        dirty = list_dirty_attachment_ids(conn)
        if dirty is None:
            # Un-migrated legacy DB: no dirty table to trust - fall back to a
            # full build (defensive; a real install always migrates).
            return self._first_publish(
                conn,
                workspace_id=workspace_id,
                workspace_manager=workspace_manager,
                principal_id=principal_id,
                now=now,
            )
        if not dirty:
            return prev

        paths = workspace_manager.paths(principal_id)

        # Build the changes into *local copies* of the four worker-owned
        # containers, so a failed build leaves the published state untouched
        # (last-known-good retention) and the dirty ids in place.
        new_by_id = dict(self._by_id)
        new_order = list(self._order)
        new_idempotency = dict(self._idempotency)
        new_idempotency_by_attachment = dict(self._idempotency_by_attachment)

        order_dirty = False
        try:
            for attachment_id in dirty:
                projection = _fetch_attachment_projection(
                    conn,
                    workspace_id=workspace_id,
                    paths=paths,
                    attachment_id=attachment_id,
                    max_detail_events=self._max_detail_events,
                )
                if projection is None:
                    order_dirty = self._remove_projection(
                        new_by_id, new_order, new_idempotency,
                        new_idempotency_by_attachment, attachment_id,
                    ) or order_dirty
                else:
                    order_dirty = self._upsert_projection(
                        new_by_id, new_order, new_idempotency,
                        new_idempotency_by_attachment, attachment_id, projection,
                    ) or order_dirty
            if order_dirty:
                new_order.sort(key=lambda aid: (-new_by_id[aid].created_at, aid))
            # Construct the *complete* immutable snapshot before any dirty id
            # is acknowledged - a construction failure (e.g. MemoryError) must
            # keep the old snapshot and all dirty ids.
            fresh = AttachmentsSnapshot(
                records=tuple(new_by_id[aid] for aid in new_order),
                by_id=dict(new_by_id),
                idempotency=dict(new_idempotency),
                built_at=now,
            )
        except Exception:  # noqa: BLE001 - a failed publish must not kill the worker tick
            logger.exception("AttachmentsSnapshotPublisher: incremental rebuild failed - keeping last-known-good")
            with self._lock:
                return self._snapshot
            # (dirty ids are intentionally NOT cleared: the next tick retries.)

        # Atomically publish the complete snapshot, then acknowledge exactly
        # the processed dirty ids (and commit that deletion). The order
        # matters: a reader can only ever see the complete new snapshot, and
        # the dirty-id delete happens *after* the swap - a failed ack leaves
        # the already-published snapshot correct and is just a harmless retry
        # next tick.
        with self._lock:
            self._snapshot = fresh
            self._by_id = new_by_id
            self._order = new_order
            self._idempotency = new_idempotency
            self._idempotency_by_attachment = new_idempotency_by_attachment

        try:
            clear_dirty_attachment_ids(conn, dirty)
            conn.commit()
        except Exception:  # noqa: BLE001 - a failed ack is a harmless retry, never a lost change
            logger.exception("AttachmentsSnapshotPublisher: dirty-id ack/commit failed - will retry next tick")
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("AttachmentsSnapshotPublisher: rollback after failed ack also failed")
        return fresh

    # ---- helpers (worker-only) ------------------------------------------

    def _first_publish(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        workspace_manager: MCAWorkspaceManager,
        principal_id: str,
        now: float,
    ) -> Optional[AttachmentsSnapshot]:
        """The first publish (and the defensive legacy fallback): a full
        `build_attachments_snapshot()`, whose projection/order/idempotency
        index become the publisher's initial worker-owned state."""
        try:
            fresh = build_attachments_snapshot(
                conn,
                workspace_id=workspace_id,
                workspace_manager=workspace_manager,
                principal_id=principal_id,
                now=now,
                max_detail_events=self._max_detail_events,
            )
        except Exception:  # noqa: BLE001
            logger.exception("AttachmentsSnapshotPublisher: first snapshot build failed")
            with self._lock:
                return self._snapshot
        with self._lock:
            self._by_id = dict(fresh.by_id)
            self._order = [record.id for record in fresh.records]
            self._idempotency = dict(fresh.idempotency)
            self._idempotency_by_attachment = {
                entry.attachment_id: key for key, entry in fresh.idempotency.items()
            }
            self._snapshot = fresh
            self._initialized = True
        # A full build incorporated every committed projected row, so any
        # dirty ids accrued before it are now stale. Delete them and commit;
        # a failure here is a harmless retry (the next drain/full build
        # re-covers them) and must not leave the connection mid-transaction.
        try:
            clear_all_dirty_attachment_ids(conn)
            conn.commit()
        except Exception:  # noqa: BLE001 - a failed ack is a harmless retry, never a lost change
            logger.exception("AttachmentsSnapshotPublisher: full-build dirty clear/commit failed - will retry")
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("AttachmentsSnapshotPublisher: rollback after full-build clear also failed")
        return fresh

    def _remove_projection(self, by_id, order, idempotency, idempotency_by_attachment, attachment_id) -> bool:
        """Remove a deleted attachment's projection, order entry, and idempotency
        index entry from the *local copies* being built. Returns True if the
        order membership changed (a re-sort is needed before publishing)."""
        order_changed = attachment_id in order
        by_id.pop(attachment_id, None)
        if order_changed:
            order.remove(attachment_id)
        old_key = idempotency_by_attachment.pop(attachment_id, None)
        if old_key is not None:
            idempotency.pop(old_key, None)
        return order_changed

    def _upsert_projection(self, by_id, order, idempotency, idempotency_by_attachment, attachment_id, projection) -> bool:
        """Insert or update one attachment's projection and its idempotency
        index entry in the *local copies* being built. Returns True if the
        order membership/created_at changed (a re-sort is needed before
        publishing)."""
        old = by_id.get(attachment_id)
        by_id[attachment_id] = projection.record
        order_changed = False
        if old is None:
            order.append(attachment_id)
            order_changed = True
        elif old.created_at != projection.record.created_at:
            order_changed = True

        # Idempotency index: remove the old entry (if any), then add the fresh
        # one only when both client_request_id and canonical_hash are present
        # (matching the full build's own NULL-canonical_hash skip).
        old_key = idempotency_by_attachment.pop(attachment_id, None)
        if old_key is not None:
            idempotency.pop(old_key, None)
        if projection.canonical_hash is not None and projection.client_request_id is not None:
            idempotency[projection.client_request_id] = IdempotencyEntry(
                attachment_id=attachment_id,
                canonical_hash=projection.canonical_hash,
                created_at=projection.record.created_at,
            )
            idempotency_by_attachment[attachment_id] = projection.client_request_id
        return order_changed
