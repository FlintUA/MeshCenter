"""api/api_attachments.py

The MCAttach REST surface (internal-rest-api.md §7). MIT-licensed Core
code - never `meshtastic`, never anything under `adapters/meshtastic/`.

The ten read-only `GET` endpoints §7.1 defines:

    GET /api/attachments
    GET /api/attachments/{attachment_id}
    GET /api/attachments/{attachment_id}/deliveries
    GET /api/mca/delivery-adapters
    GET /api/mca/providers
    GET /api/mca/providers/{provider_id}
    GET /api/mca/providers/{provider_id}/upload-readiness
    GET /api/mca/connectivity
    GET /api/mca/identity
    GET /api/mca/commands/{command_id}

plus the mutation endpoints implemented so far: the three Step 1.6A.3A
lifecycle actions — `POST /api/attachments/{id}/retry`, `/download`,
`/reject` (§7.3) — and the Step 1.6A.3B idempotent multipart create
`POST /api/attachments` (§7.2). Everything else (`GET
/api/attachments/{id}/content`, cancel, save, revoke, local-content,
contacts/connectors, provider onboarding) remains out of scope (1.6A.3+).

Threading boundary (the point of Step 1.6A.1's facade - §3.1/§3.2): these
handlers read the worker-published in-memory snapshots and registries
through `AttachmentsFacade` and submit mutations through its command
queue. They never touch SQLite, the network, the radio, or the worker's
tick lock, and they never create/lazily-initialize the MCA runtime -
`mca_runtime.get_attachments_facade()` returns `None` until the runtime
has actually been constructed (an explicit "not ready" signal, mapped to
503 here, never a fallback that would open `attachments.db` from a
request thread). The one deliberate filesystem exception is `POST
/api/attachments` (§7.2), which writes the staged plaintext to
`spool/outgoing/<attachment_id>` and removes it again on any non-fresh/
failed path - the only request-thread filesystem operation the design
permits.

No-secret discipline (§11): every response is built by an explicit
allowlist serializer. No `dataclasses.asdict()`, no `__dict__`, no generic
encoder. Upload tokens, token filenames, private-key filenames, raw public
key bytes, filesystem paths, `ContentDescriptor.locator`, ciphertext, and
internal SQLite fields never reach the wire.

Error boundary (§11 "no secret logging"): the project-wide `handle_errors`
(server.py) turns an uncaught exception into a 500 envelope that leaks
`str(e)` and, in debug mode, a traceback. Every handler here is therefore
additionally wrapped in a local `_mca_error_boundary` (innermost, beneath
`handle_errors`) that maps `FacadeNotReady` to the 503 `mca_not_ready`
envelope and any other unexpected exception to a clean 500 `internal_error`
envelope - no exception text, class name, traceback, path, or identifier in
the response, and only the handler name + exception class logged. The
legacy wrapper never sees an exception, so it cannot re-wrap or leak one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections.abc import Mapping
from functools import wraps
from pathlib import Path

from flask import jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from meshsrv.attachments import mca_runtime, mime_allowlist, receiver, sender
from meshsrv.attachments.commands import Command, CommandQueueFull, mint_command_id
from meshsrv.attachments.crypto import ciphertext_size
from meshsrv.attachments.delivery.base import RouteType
from meshsrv.attachments.delivery.meshtastic import MESHTASTIC_TEXT_MAX_PAYLOAD_BYTES
from meshsrv.attachments.facade import FacadeNotReady
from meshsrv.attachments.idempotency import (
    PendingReservation,
    build_canonical_json,
    compute_canonical_hash,
    validate_client_request_id,
)
from meshsrv.attachments.provider_registry import (
    ProviderRegistryError,
    decode_provider_id,
    encode_provider_id,
)
from meshsrv.attachments.recipient_snapshot import RecipientRejectionReason, evaluate_recipient_trust
from meshsrv.attachments.snapshots import (
    serialize_attachment_public,
    serialize_delivery,
    serialize_timeline_event,
)
from meshsrv.connectivity_monitor import RelayState, evaluate_upload_readiness

_log = logging.getLogger("meshsrv.attachments.api")

# ---- id shapes (§7.9: attachment_id and command_id are both uuid4().hex) --

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

# Strict ASCII-decimal shape for the numeric query params (§7.1): digits
# only, no sign, no whitespace, no leading/trailing junk. `fullmatch` (not
# `match`) so a trailing newline or space can never sneak past.
_ASCII_DECIMAL_RE = re.compile(r"[0-9]+")


def _is_hex32(value: str) -> bool:
    return isinstance(value, str) and _HEX32_RE.match(value) is not None


# ---- list-filter state sets ----------------------------------------------
#
# `error_code` alone is NOT a reliable "errors" signal: the sender state
# machine stamps `error_code` on non-terminal retrying states too
# (QUEUED_UPLOAD="relay_unavailable", READY_TO_SEND="radio_send_failed").
# The `errors` filter is therefore driven by the terminal FAILED_* states,
# and `pending` by the terminal-state complement.

_ALL_STATES = frozenset(
    {
        sender.DRAFT, sender.VALIDATING, sender.ENCRYPTING, sender.QUEUED_UPLOAD,
        sender.UPLOADING, sender.READY_TO_SEND, sender.SENT, sender.RECEIVED,
        sender.DOWNLOADED, sender.EXPIRED, sender.REVOKED, sender.CANCELLED,
        sender.FAILED_VALIDATION, sender.FAILED_UPLOAD, sender.FAILED_RADIO,
        receiver.OFFER_RECEIVED, receiver.WAITING_KEY, receiver.WAITING_PROVIDER,
        receiver.WAITING_NETWORK, receiver.WAITING_CONSENT, receiver.DOWNLOADING,
        receiver.VERIFYING, receiver.AVAILABLE, receiver.EXPIRED, receiver.REJECTED,
        receiver.FAILED,
    }
)

_TERMINAL_STATES = frozenset(sender.TERMINAL_STATES | receiver.TERMINAL_STATES)

_FILTER_ERROR_STATES = frozenset(
    {sender.FAILED_VALIDATION, sender.FAILED_UPLOAD, sender.FAILED_RADIO, receiver.FAILED}
)

_VALID_DIRECTIONS = frozenset({"sent", "received", "all"})
_VALID_FILTERS = frozenset({"pending", "errors", "saved", "all"})

_LIST_LIMIT_DEFAULT = 100
_LIST_LIMIT_MIN = 1
_LIST_LIMIT_MAX = 500

# §7.2: the create endpoint's 5 MiB plaintext cap, enforced server-side
# (not only client-side - §13 gap 11).
_MAX_FILE_BYTES = 5 * 1024 * 1024

# Finding 4 (bounded + atomic multipart staging): the file cap above is the
# *plaintext* bound; the total multipart body is additionally capped a little
# above it so Werkzeug stops parsing (and spooling) an over-large request
# *before* this route's own file-size check runs. That cap is set per-request
# (`request.max_content_length`, supported since Flask 3.1), never via the
# global `MAX_CONTENT_LENGTH` config, so no other endpoint's upload limit is
# affected. The non-file `metadata` part is bounded separately with an explicit
# UTF-8 byte-length check before `json.loads` runs - *not* via Werkzeug's
# `max_form_memory_size`, which applies to the raw multipart chunk buffer and
# would reject any file part larger than the (64 KiB) chunk size.
_MAX_METADATA_BYTES = 16 * 1024
_MULTIPART_OVERHEAD_BYTES = 8 * 1024
_MAX_REQUEST_BYTES = _MAX_FILE_BYTES + _MAX_METADATA_BYTES + _MULTIPART_OVERHEAD_BYTES

# Fixed-size staging chunk (streamed, never whole-file buffered) and the head
# window retained in memory for MIME sniffing (Finding 4/8).
_STAGING_CHUNK_BYTES = 64 * 1024
_SNIFF_HEAD_BYTES = 512
# Temporary staging files are dot-prefixed and `.tmp`-suffixed, distinct from a
# committed spool file (a bare 32-hex `attachment_id`) - the convention
# Finding 5's bounded orphan-staging recovery keys off.
_TEMP_SUFFIX = ".tmp"


# ---- small response helpers ----------------------------------------------


def _not_ready():
    """The one 503 every handler returns before the MCA runtime exists (or a
    snapshot-backed read raises `FacadeNotReady`)."""
    return jsonify({
        "ok": False,
        "error": "MCAttach service is not ready",
        "error_code": "mca_not_ready",
    }), 503


def _json_error(error_code: str, message: str):
    return jsonify({"ok": False, "error": message, "error_code": error_code})


def _invalid_state_transition(record):
    """The §7.3 synchronous 409 for a violated state precondition: the stable
    `invalid_state_transition` envelope plus the single safe `state` value
    from the published snapshot. Only `state` is added - never direction,
    identifiers, paths, comments, filenames, keys, tokens, exception text, or
    raw database values."""
    return jsonify({
        "ok": False,
        "error": "invalid state transition",
        "error_code": "invalid_state_transition",
        "state": record.state,
    }), 409


def _internal_error_response():
    """The one sanitized 500 an unexpected exception maps to (§11): a stable
    public message + `internal_error` - never `str(e)`, a class name, a
    traceback, a path, or an identifier."""
    return jsonify({
        "ok": False,
        "error": "Internal server error",
        "error_code": "internal_error",
    }), 500


def _request_too_large():
    """The 413 for a multipart body that exceeds the create endpoint's
    per-request cap (Finding 4). Werkzeug raises `RequestEntityTooLarge`
    during form parsing - before the route's own file-size check runs; this
    maps it to a clean JSON envelope so the sanitized boundary never leaks a
    Werkzeug HTML page, `str(e)`, or a traceback."""
    return jsonify({
        "ok": False,
        "error": "request body too large",
        "error_code": "request_too_large",
    }), 413


def _mca_error_boundary(fn):
    """The local sanitized exception boundary for every MCAttach read
    handler. It sits *beneath* the project-wide `handle_errors` decorator,
    so it sees (and fully handles) every exception first: `handle_errors`
    then only ever returns the clean response, never its own leaky 500.

    `FacadeNotReady` -> 503 `mca_not_ready`; a Werkzeug `RequestEntityTooLarge`
    (the create endpoint's per-request multipart cap, Finding 4) -> 413
    `request_too_large`; anything else -> 500 `internal_error`, logging only the
    handler name and the exception class (never `str(exc)`, args, `exc_info`,
    the request body, the query string, or any identifier)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FacadeNotReady:
            return _not_ready()
        except RequestEntityTooLarge:
            return _request_too_large()
        except Exception as exc:  # noqa: BLE001 - this is the sanitized boundary itself
            _log.error(
                "MCAttach read endpoint '%s' raised %s", fn.__name__, type(exc).__name__
            )
            return _internal_error_response()

    return wrapper


def _parse_ascii_int(raw) -> int:
    """Parse a strict ASCII decimal string. Raises `ValueError` for anything
    that is not exactly ASCII digits - no `+`/`-` sign, no whitespace, no
    `.`, no empty string. Callers decide bounds and the error envelope."""
    if not isinstance(raw, str) or _ASCII_DECIMAL_RE.fullmatch(raw) is None:
        raise ValueError("not an ASCII decimal integer")
    return int(raw)


def _parse_limit_offset():
    """Strict pagination for `GET /api/attachments` (§7.1). An absent
    `limit`/`offset` keeps the documented defaults (100 / 0); a present value
    must be a strict ASCII decimal integer within bounds (`limit` 1..500,
    `offset` >= 0), otherwise a 400 `invalid_pagination` - never silently
    clamped or coerced to a default, and never a partially-executed query.

    Returns `(limit, offset, None)` or `(None, None, (body, status))`."""
    limit_raw = request.args.get("limit")
    offset_raw = request.args.get("offset")
    try:
        limit = _LIST_LIMIT_DEFAULT if limit_raw is None else _parse_ascii_int(limit_raw)
        offset = 0 if offset_raw is None else _parse_ascii_int(offset_raw)
    except ValueError:
        return None, None, (_json_error("invalid_pagination", "invalid pagination"), 400)
    if not (_LIST_LIMIT_MIN <= limit <= _LIST_LIMIT_MAX):
        return None, None, (_json_error("invalid_pagination", "invalid pagination"), 400)
    return limit, offset, None


def _parse_optional_query_int(raw, *, minimum: int):
    """Parse an optional, non-negative-by-default integer query param. Returns
    `None` when absent; the int when a valid strict ASCII decimal >= `minimum`;
    raises `ValueError` when present but blank/malformed/signed/fractional, or
    below `minimum` (a caller bug worth a 400, not a silent skip)."""
    if raw is None:
        return None
    value = _parse_ascii_int(raw)
    if value < minimum:
        raise ValueError(f"must be >= {minimum}")
    return value


def _validate_provider_id(value):
    """Canonical-provider-id validation shared by the two provider routes
    (§7.1). Decodes with the registry's own `decode_provider_id()` (the one
    existing encoding implementation - no second, hand-written one) and
    requires canonical round-trip equality (`encode_provider_id(decode(v))
    == v`), so padded, wrong-length, wrong-alphabet, and non-canonical
    spellings are all rejected before any snapshot lookup or readiness
    evaluation.

    Returns `None` on success, or a `(body, status)` 400 `invalid_provider_id`
    response on a malformed id."""
    try:
        decoded = decode_provider_id(value)
    except ProviderRegistryError:
        return _json_error("invalid_provider_id", "invalid provider id"), 400
    if encode_provider_id(decoded) != value:
        return _json_error("invalid_provider_id", "invalid provider id"), 400
    return None


def _resolve_attachment(facade, attachment_id):
    """Validate `attachment_id` and look it up in the published snapshot.
    Returns `(record, None)` on success, or `(None, (body, status))` with a
    fully-built response on a validation/not-found error. `FacadeNotReady`
    is left to propagate to `_mca_error_boundary` (the single 503 mapping),
    rather than being caught here."""
    if not _is_hex32(attachment_id):
        return None, (_json_error("invalid_attachment_id", "invalid attachment id"), 400)
    record = facade.get_attachment(attachment_id)
    if record is None:
        return None, (_json_error("attachment_not_found", "attachment not found"), 404)
    return record, None


# ---- create-endpoint helpers (Step 1.6A.3B, §7.2) -------------------------


def _sanitize_source_name(raw_filename):
    """Derive the safe, single-component `source_name` recorded for a new
    outgoing attachment (§3.5/§7.2). The client filename is never trusted as
    a path - the staged spool file is named by the minted `attachment_id`,
    not this string; `source_name` is only the human-readable `file_name`
    folded into the canonical hash. Fail closed to a neutral name rather than
    rejecting the upload: take the basename (so a hostile `../../etc/passwd`
    cannot survive even as a display name), drop control characters, and fall
    back to `"attachment"` when nothing safe remains."""
    if not isinstance(raw_filename, str):
        return "attachment"
    base = raw_filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable())
    base = base.strip().strip(".")
    if not base:
        return "attachment"
    return base[:255]


def _discard_spool(spool_path):
    """Best-effort removal of a staged-but-unused spool file (a temp file or a
    published final file) - the one filesystem write the create endpoint does,
    matched by this one cleanup on every non-fresh/failed path (a
    replay/conflict stages a file the worker will never reference, and a
    full-queue/not-ready submit leaves it orphaned). Never raises: an unlink
    failure is a disk-hygiene issue, not a correctness issue, and is logged
    without the path or any identifier."""
    try:
        spool_path.unlink(missing_ok=True)
    except OSError:
        _log.warning("MCAttach create endpoint: could not remove a staged-but-unused spool file")


class _FileTooLarge(Exception):
    """Internal signal from `_stage_spool_file`: the staged file exceeded
    `_MAX_FILE_BYTES` (read at most one chunk beyond). Mapped to the 400
    `file_too_large` envelope by the caller; never propagates to the sanitized
    boundary."""


def _stage_spool_file(file_storage, spool_dir, attachment_id):
    """Stream `file_storage` to a server-generated exclusive temporary file in
    fixed-size chunks, computing SHA-256, the total plaintext size, and the
    full-stream text-validity result incrementally (Findings 4/8) - the whole
    file is never buffered, only the first `_SNIFF_HEAD_BYTES` retained in
    memory for MIME sniffing.

    The temp file is created with `tempfile.mkstemp`: exclusive creation
    (O_CREAT|O_EXCL), mode 0600, and a dot-prefixed name derived from the
    minted `attachment_id` - never the browser filename - so an ID collision
    can never overwrite an existing spool file, and the final publish stays a
    same-directory `os.replace` (atomic). Returns
    `(temp_path, file_sha256, head, size, text_clean)`, where `text_clean` is
    `TextStreamValidator`'s whole-stream UTF-8/NUL verdict (only meaningful -
    and only enforced - for text-family MIME types; binary formats are
    identified by magic bytes and may legitimately contain NUL bytes). Raises
    `_FileTooLarge` after reading at most one chunk beyond `_MAX_FILE_BYTES`;
    on any other error the temp file is removed before re-raising."""
    spool_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=spool_dir, prefix=f".{attachment_id}.", suffix=_TEMP_SUFFIX
    )
    hasher = hashlib.sha256()
    size = 0
    head = bytearray()
    text_validator = mime_allowlist.TextStreamValidator()
    try:
        with os.fdopen(fd, "wb") as fh:
            stream = file_storage.stream
            while True:
                chunk = stream.read(_STAGING_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_FILE_BYTES:
                    # One chunk beyond the cap - stop and reject, never read
                    # the rest of an oversized file into memory or disk.
                    raise _FileTooLarge()
                hasher.update(chunk)
                fh.write(chunk)
                text_validator.feed(chunk)
                if len(head) < _SNIFF_HEAD_BYTES:
                    head.extend(chunk[:_SNIFF_HEAD_BYTES - len(head)])
    except Exception:
        _discard_spool(Path(temp_path))
        raise
    return Path(temp_path), hasher.hexdigest(), bytes(head), size, text_validator.finish()


def _resolve_create_provider(profiles, provider_id_arg):
    """Resolve the create request's `provider_id` (§7.2) to a canonical
    Base64URL id from the published provider snapshot. An explicit
    `provider_id` must be present in the snapshot; an absent one falls back to
    the `is_default` profile. Returns `(provider_id, profile, None)` on
    success, or `(None, None, (body, status))` on a miss - a `400
    provider_not_found`, never `invalid_provider_id` (a malformed id simply
    does not resolve to a profile)."""
    if provider_id_arg is not None:
        profile = profiles.get(provider_id_arg)
        if profile is None:
            return None, None, (_json_error("provider_not_found", "provider not found"), 400)
        return provider_id_arg, profile, None
    for pid, profile in profiles.items():
        if profile.is_default:
            return pid, profile, None
    return None, None, (_json_error("provider_not_found", "no default provider configured"), 400)


def _validate_ttl(profile, hard_ttl_seconds):
    """Validate `hard_ttl_seconds` against the resolved provider's configured
    `[min_ttl_seconds, max_ttl_seconds]` bounds (§7.2). A `None` bound is
    unconstrained. Returns `None` on success, or a `(body, status)` 400
    `ttl_out_of_range`."""
    if profile.min_ttl_seconds is not None and hard_ttl_seconds < profile.min_ttl_seconds:
        return _json_error("ttl_out_of_range", "hard_ttl_seconds below the provider minimum"), 400
    if profile.max_ttl_seconds is not None and hard_ttl_seconds > profile.max_ttl_seconds:
        return _json_error("ttl_out_of_range", "hard_ttl_seconds above the provider maximum"), 400
    return None


def _provider_policy_error(profile):
    """Finding 6: enforce the create endpoint's *local* provider-policy
    preconditions on the request thread, from the immutable published
    snapshot. Only local configuration is checked - `enabled`,
    `upload_allowed`, `upload_token_configured`, in the same order
    `ConnectivityMonitor.evaluate_upload_decision()` applies them - so the
    error codes are the stable readiness reasons a caller can rely on.

    Deliberately NOT checked here (and never a create precondition):
    current Relay/Internet reachability (offline creation and queuing must
    remain possible - a Relay can be upload-READY while currently
    unreachable), and radio availability. The Relay-state branches of
    `evaluate_upload_decision()` are therefore skipped entirely.

    Returns `None` when the profile passes, or a `(body, status)` 400 for
    the first failing policy. `provider_disabled` follows the create
    endpoint's existing `provider_not_found` naming (a *profile* level
    property, not the `UploadRejectionReason.PROFILE_DISABLED` value);
    `upload_not_allowed`/`upload_token_missing` match their
    `UploadRejectionReason` values verbatim."""
    if not profile.enabled:
        return _json_error("provider_disabled", "provider is disabled"), 400
    if not profile.upload_allowed:
        return _json_error("upload_not_allowed", "uploads are not allowed for this provider"), 400
    if not profile.upload_token_configured:
        return _json_error("upload_token_missing", "provider has no upload token configured"), 400
    return None


# ---- serializers (explicit allowlists, no dataclasses.asdict) ------------


def _serialize_provider(profile, relay_status, *, include_latency_error=False):
    """The §7.13 public provider projection, joined with the connectivity
    snapshot's per-provider `RelayStatus` (§7.1). Never emits the raw
    `service_public_key` bytes (replaced by its SHA-256 fingerprint), the
    upload-token filename, or any upload token.

    `state`/`upload_readiness` come from the relay status when it exists
    (the monitor publishes one per registered profile during `refresh()`);
    before the first refresh there is no `RelayStatus`, so `state` falls
    back to `unknown` and `upload_readiness` to the pure, config-derived
    `evaluate_upload_readiness(profile)` value - both are still correct,
    they just reflect "not yet probed" rather than a live reading.

    `include_latency_error` adds the live `latency_ms`/`error_code` the
    list endpoint §7.1 joins (distinct from the profile's persisted
    `last_latency_ms`/`last_error_code` cache)."""
    fingerprint = hashlib.sha256(profile.service_public_key).hexdigest()
    if relay_status is not None:
        state = relay_status.state.value
        upload_readiness = relay_status.upload_readiness.value
    else:
        state = RelayState.UNKNOWN.value
        upload_readiness = evaluate_upload_readiness(profile).value
    out = {
        "provider_id": profile.provider_id,
        "display_name": profile.display_name,
        "origin": profile.origin,
        "service_key_fingerprint": fingerprint,
        "kind": profile.kind,
        "tls_required": profile.tls_required,
        "upload_allowed": profile.upload_allowed,
        "download_allowed": profile.download_allowed,
        "max_ciphertext_bytes": profile.max_ciphertext_bytes,
        "is_default": profile.is_default,
        "enabled": profile.enabled,
        "min_ttl_seconds": profile.min_ttl_seconds,
        "max_ttl_seconds": profile.max_ttl_seconds,
        "protocol_version": profile.protocol_version,
        "upload_token_configured": profile.upload_token_configured,
        "last_checked_at": profile.last_checked_at,
        "last_check_result": profile.last_check_result,
        "last_latency_ms": profile.last_latency_ms,
        "last_error_code": profile.last_error_code,
        "state": state,
        "upload_readiness": upload_readiness,
    }
    if include_latency_error:
        out["latency_ms"] = relay_status.latency_ms if relay_status is not None else None
        out["error_code"] = relay_status.error_code if relay_status is not None else None
    return out


def _serialize_identity(principal):
    """The §7.1 identity projection. `fingerprint` is the full-key SHA-256
    hex of `public_identity`, deliberately distinct from the 64-bit
    `key_id`. Never emits `public_identity`/`public_x25519` bytes or the
    private-key filename."""
    return {
        "principal_id": principal.principal_id,
        "key_id": principal.key_id,
        "epoch": principal.epoch,
        "fingerprint": hashlib.sha256(principal.public_identity).hexdigest(),
        "status": principal.status,
    }


def _jsonable(value):
    """Recursively convert a worker-frozen `CommandResult.result` (nested
    `MappingProxyType`/tuple - already JSON-native and validated, just
    wrapped in read-only containers) back into plain dict/list values
    `jsonify` can serialize. No `default=str`, no generic fallback."""
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _serialize_command(result):
    """The §7.9 command-result projection. `type` is the command kind; the
    `result` payload was already validated JSON-native and no-secret by the
    worker (command_registry), so `_jsonable` only un-freezes its shape."""
    return {
        "command_id": result.command_id,
        "type": result.kind,
        "status": result.status,
        "resource_id": result.resource_id,
        "result": _jsonable(result.result),
        "error_code": result.error_code,
        "created_at": result.created_at,
        "updated_at": result.updated_at,
    }


# ---- route registration (DI pattern, no Blueprints) ----------------------


def register_attachments_routes(app, handle_errors):
    """Wire the ten §7.1 read endpoints onto `app`. Closes over nothing but
    `mca_runtime.get_attachments_facade()` (the same already-constructed
    runtime `server.py`'s `start_runtime()` builds) - the facade itself is
    fetched per request, never captured, so a request thread can never hold
    a stale facade across a profile switch / runtime reset."""

    def _facade():
        return mca_runtime.get_attachments_facade()

    @app.route("/api/attachments", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def list_attachments():
        facade = _facade()
        if facade is None:
            return _not_ready()

        direction = request.args.get("direction", "all")
        if direction not in _VALID_DIRECTIONS:
            return _json_error("invalid_direction", "invalid direction"), 400

        state = request.args.get("state", "all")
        if state != "all" and state not in _ALL_STATES:
            return _json_error("invalid_state", "invalid state"), 400

        filter_ = request.args.get("filter", "all")
        if filter_ not in _VALID_FILTERS:
            return _json_error("invalid_filter", "invalid filter"), 400

        limit, offset, err = _parse_limit_offset()
        if err is not None:
            body, status = err
            return body, status

        snapshot = facade.attachments_snapshot()

        matching = []
        for record in snapshot.records:
            if direction != "all" and record.direction != direction:
                continue
            if state != "all" and record.state != state:
                continue
            if filter_ == "pending" and record.state in _TERMINAL_STATES:
                continue
            if filter_ == "errors" and record.state not in _FILTER_ERROR_STATES:
                continue
            if filter_ == "saved" and not record.saved:
                continue
            matching.append(record)

        total = len(matching)
        page = matching[offset:offset + limit]
        return jsonify({
            "ok": True,
            "attachments": [serialize_attachment_public(r) for r in page],
            "total": total,
        })

    @app.route("/api/attachments/<attachment_id>", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def get_attachment_detail(attachment_id):
        facade = _facade()
        if facade is None:
            return _not_ready()

        record, err = _resolve_attachment(facade, attachment_id)
        if err is not None:
            body, status = err
            return body, status

        return jsonify({
            "ok": True,
            "attachment": serialize_attachment_public(record),
            "timeline": [serialize_timeline_event(e) for e in record.timeline],
        })

    @app.route("/api/attachments/<attachment_id>/deliveries", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def get_attachment_deliveries(attachment_id):
        facade = _facade()
        if facade is None:
            return _not_ready()

        record, err = _resolve_attachment(facade, attachment_id)
        if err is not None:
            body, status = err
            return body, status

        return jsonify({
            "ok": True,
            "deliveries": [serialize_delivery(d) for d in record.deliveries],
        })

    @app.route("/api/mca/delivery-adapters", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def mca_delivery_adapters():
        facade = _facade()
        if facade is None:
            return _not_ready()

        # Read-only: the real adapter's `connector_state` (READY/DEGRADED/
        # UNAVAILABLE) is derived from the *live* radio transport's
        # `get_connection_info()`, which a request thread must not touch.
        # There is no request-thread-safe, immutable snapshot of the local
        # radio's connection state (and internet/Relay state must never be
        # conflated with it), so the read endpoint reports UNKNOWN rather
        # than fabricating READY. The remaining fields are static
        # capabilities (§7.1), with the payload ceiling from the adapter's
        # own constant.
        return jsonify({
            "ok": True,
            "adapters": [{
                "adapter_id": "meshtastic",
                "connector_profile_id": "meshtastic",
                "capabilities": {
                    "wire_formats": ["MCA1_TEXT"],
                    "max_payload_bytes": MESHTASTIC_TEXT_MAX_PAYLOAD_BYTES,
                    "supports_direct": True,
                    "supports_channel": False,
                    "supports_incoming": True,
                    "ack_semantics": "CONFIRMED",
                    "connector_state": "UNKNOWN",
                },
            }],
        })

    @app.route("/api/mca/providers", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def mca_providers():
        facade = _facade()
        if facade is None:
            return _not_ready()

        profiles = facade.provider_snapshot()
        connectivity = facade.connectivity_snapshot()
        providers = [
            _serialize_provider(profile, connectivity.relays.get(provider_id), include_latency_error=True)
            for provider_id, profile in profiles.items()
        ]
        return jsonify({"ok": True, "providers": providers})

    @app.route("/api/mca/providers/<provider_id>", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def mca_provider(provider_id):
        facade = _facade()
        if facade is None:
            return _not_ready()

        err = _validate_provider_id(provider_id)
        if err is not None:
            body, status = err
            return body, status

        profile = facade.provider_snapshot().get(provider_id)
        if profile is None:
            return _json_error("provider_not_found", "provider not found"), 404

        relay_status = facade.connectivity_snapshot().relays.get(provider_id)
        return jsonify({"ok": True, "provider": _serialize_provider(profile, relay_status)})

    @app.route("/api/mca/providers/<provider_id>/upload-readiness", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def mca_provider_upload_readiness(provider_id):
        facade = _facade()
        if facade is None:
            return _not_ready()

        err = _validate_provider_id(provider_id)
        if err is not None:
            body, status = err
            return body, status

        try:
            ciphertext_bytes = _parse_optional_query_int(
                request.args.get("ciphertext_bytes"), minimum=0
            )
            requested_ttl_seconds = _parse_optional_query_int(
                request.args.get("requested_ttl_seconds"), minimum=1
            )
        except ValueError:
            return _json_error("invalid_query", "invalid upload-readiness query parameter"), 400

        decision = facade.evaluate_upload_readiness(
            provider_id,
            ciphertext_bytes=ciphertext_bytes,
            requested_ttl_seconds=requested_ttl_seconds,
        )
        return jsonify({
            "ok": True,
            "ready": decision.ready,
            "reason": decision.reason.value if decision.reason is not None else None,
            "detail": None,
        })

    @app.route("/api/mca/connectivity", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def mca_connectivity():
        facade = _facade()
        if facade is None:
            return _not_ready()

        snapshot = facade.connectivity_snapshot()
        relays = {
            provider_id: {
                "state": status.state.value,
                "upload_readiness": status.upload_readiness.value,
                "checked_at": status.checked_at,
                "latency_ms": status.latency_ms,
                "error_code": status.error_code,
            }
            for provider_id, status in snapshot.relays.items()
        }
        return jsonify({"ok": True, "internet": snapshot.internet.value, "relays": relays})

    @app.route("/api/mca/identity", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def mca_identity():
        facade = _facade()
        if facade is None:
            return _not_ready()

        return jsonify({"ok": True, **_serialize_identity(facade.identity_snapshot())})

    @app.route("/api/mca/commands/<command_id>", methods=["GET"])
    @handle_errors
    @_mca_error_boundary
    def mca_command(command_id):
        facade = _facade()
        if facade is None:
            return _not_ready()

        if not _is_hex32(command_id):
            return _json_error("invalid_command_id", "invalid command id"), 400

        result = facade.get_command(command_id)
        if result is None:
            return _json_error("command_not_found", "command not found"), 404

        return jsonify({"ok": True, "command": _serialize_command(result)})

    # ---- Step 1.6A.3A lifecycle mutations (POST) ---------------------------
    #
    # retry/download/reject are the first three *mutating* endpoints (§7.3),
    # still going through the same single-owner command queue as every other
    # mutation: the request thread validates the id, does a cheap synchronous
    # state check against the published snapshot (fast 409 feedback), then
    # enqueues a frozen `Command` and returns 202 with the command_id - the
    # worker executes it and the client polls GET /api/mca/commands/
    # {command_id} for the real result (§3.4). The worker re-checks the
    # persisted row, so the snapshot check here is defense-in-depth, not the
    # authority. A success 202 means "accepted", never "already executed".

    def _submit_lifecycle_command(attachment_id, kind, precondition):
        """Validate id -> resolve snapshot -> synchronous state precondition
        -> enqueue the frozen `Command` -> 202 {ok, command_id}. `precondition`
        is a `record -> bool` declaring this endpoint's allowed
        direction/state; a `False` returns 409 `invalid_state_transition`
        carrying the snapshot's current `state` (and nothing else) before
        anything is enqueued. `CommandQueueFull` maps to 429
        `command_queue_full` (§3.4) - deliberately caught here, not left to
        `_mca_error_boundary`, which would mis-map it to a 500."""
        facade = _facade()
        if facade is None:
            return _not_ready()
        record, err = _resolve_attachment(facade, attachment_id)
        if err is not None:
            body, status = err
            return body, status
        if not precondition(record):
            return _invalid_state_transition(record)
        command = Command(
            command_id=mint_command_id(),
            kind=kind,
            payload={"attachment_id": attachment_id},
            created_at=time.time(),
        )
        try:
            command_id = facade.submit(command)
        except CommandQueueFull:
            return _json_error("command_queue_full", "command queue is full"), 429
        return jsonify({"ok": True, "command_id": command_id}), 202

    def _retry_precondition(record):
        # §7.3 retry: only the row's own direction's AUTOMATIC_STATES - a
        # terminal FAILED_*/REJECTED/EXPIRED/CANCELLED/REVOKED row is not
        # retryable here (terminal-failure recovery is a future change).
        if record.direction == "sent":
            return record.state in sender.AUTOMATIC_STATES
        if record.direction == "received":
            return record.state in receiver.AUTOMATIC_STATES
        return False

    def _consent_precondition(record):
        # §7.3 download/reject: a received attachment awaiting consent only.
        return record.direction == "received" and record.state == receiver.WAITING_CONSENT

    @app.route("/api/attachments/<attachment_id>/retry", methods=["POST"])
    @handle_errors
    @_mca_error_boundary
    def retry_attachment(attachment_id):
        return _submit_lifecycle_command(attachment_id, "attachment_retry", _retry_precondition)

    @app.route("/api/attachments/<attachment_id>/download", methods=["POST"])
    @handle_errors
    @_mca_error_boundary
    def download_attachment(attachment_id):
        return _submit_lifecycle_command(attachment_id, "attachment_download", _consent_precondition)

    @app.route("/api/attachments/<attachment_id>/reject", methods=["POST"])
    @handle_errors
    @_mca_error_boundary
    def reject_attachment(attachment_id):
        return _submit_lifecycle_command(attachment_id, "attachment_reject", _consent_precondition)

    # ---- Step 1.6A.3B: idempotent multipart create (§7.2) -------------------

    @app.route("/api/attachments", methods=["POST"])
    @handle_errors
    @_mca_error_boundary
    def create_attachment():
        """The §7.2 idempotent multipart create. Splits its work across the two
        threads exactly as §3.5/§3.6/§7.2 prescribe:

        On this request thread - a framework-level multipart body cap plus pure
        validation (id shape, comment bound, provider resolution, TTL range),
        then stream the plaintext in fixed-size chunks to an exclusive temp
        file while computing `file_sha256`/size (never whole-file buffered),
        sniff the MIME type, and publish it atomically to
        `spool/outgoing/<attachment_id>` only after size/MIME/metadata all
        pass - then mint `command_id`, compute `canonical_hash`, and hand the
        whole thing to `facade.submit_create()` for the atomic reservation +
        enqueue. The temp and final spool names are server-generated (never the
        browser filename), and both are removed on every rejected/replay/
        conflict/not-ready/queue-full/internal-error path.

        On the worker thread - `AttachmentsService._command_create` resolves the
        recipient's *trusted* binding (a SQLite read that cannot run here) and
        calls `sender.create_draft()` with the already-minted ids/hash, so the
        recipient-not-found / recipient-not-trusted outcomes are observed via
        the command result rather than synchronously.

        The `route` field is accepted for forward-compatibility but only the
        DIRECT route to `source_address` is supported; the actual route is
        derived here (never trusted from the payload) so it cannot disagree
        with the hash the worker and this thread both compute."""
        facade = _facade()
        if facade is None:
            return _not_ready()

        # Finding 4: bound the whole multipart body *before* Werkzeug parses
        # it. Per-request (never the global MAX_CONTENT_LENGTH), so no other
        # endpoint's upload limit is affected. An over-large body is rejected
        # here - during form parsing - rather than being spooled to disk and
        # only caught by this route's own file-size check.
        request.max_content_length = _MAX_REQUEST_BYTES

        # --- metadata (JSON string part): pure validation only --------------
        metadata_raw = request.form.get("metadata")
        if metadata_raw is None:
            return _json_error("invalid_metadata", "missing metadata part"), 400
        # Bound the metadata part separately (Finding 4): reject an oversized
        # metadata string before `json.loads` ever parses it. Measured in UTF-8
        # bytes so the bound is tight regardless of multi-byte characters.
        if len(metadata_raw.encode("utf-8")) > _MAX_METADATA_BYTES:
            return _json_error("metadata_too_large", "metadata part is too large"), 400
        try:
            metadata = json.loads(metadata_raw)
        except ValueError:
            return _json_error("invalid_metadata", "metadata is not valid JSON"), 400
        if not isinstance(metadata, dict):
            return _json_error("invalid_metadata", "metadata must be a JSON object"), 400

        client_request_id = metadata.get("client_request_id")
        if not isinstance(client_request_id, str):
            return _json_error("invalid_metadata", "client_request_id is required"), 400
        try:
            validate_client_request_id(client_request_id)
        except ValueError:
            return _json_error("invalid_metadata", "invalid client_request_id"), 400

        recipient = metadata.get("recipient")
        if (
            not isinstance(recipient, dict)
            or not isinstance(recipient.get("source_address"), str)
            or not recipient["source_address"]
        ):
            return _json_error("invalid_metadata", "recipient.source_address is required"), 400
        source_address = recipient["source_address"]

        # Finding 7: reject an unknown / not-yet-trusted recipient
        # *synchronously*, from the worker-published immutable binding snapshot
        # (no SQLite on this request thread). This is fast feedback only - the
        # worker re-validates against the live binding at commit time
        # (`AttachmentsService._command_create`), which remains the authority.
        # A binding that became trusted (or revoked) between this read and the
        # worker's re-check is decided there, never here.
        reason = evaluate_recipient_trust(facade.recipient_snapshot(), source_address)
        if reason is RecipientRejectionReason.RECIPIENT_NOT_FOUND:
            return _json_error("recipient_not_found", "no known recipient binding for the address"), 400
        if reason is RecipientRejectionReason.RECIPIENT_NOT_TRUSTED:
            return _json_error("recipient_not_trusted", "recipient binding is not trusted"), 400

        route = metadata.get("route")
        if route is not None:
            if not isinstance(route, dict):
                return _json_error("invalid_metadata", "route must be an object"), 400
            if route.get("route_type") not in (None, RouteType.DIRECT.value):
                return _json_error("invalid_metadata", "only DIRECT route_type is supported"), 400
            if route.get("route_id") not in (None, source_address):
                return _json_error("invalid_metadata", "route_id must equal the recipient address"), 400

        comment = metadata.get("comment")
        if comment is not None and not isinstance(comment, str):
            return _json_error("invalid_metadata", "comment must be a string"), 400
        try:
            comment = sender.normalize_comment(comment)
        except sender.SenderError:
            return _json_error("invalid_metadata", "comment is not valid"), 400

        # --- provider resolution + TTL bounds (pure, from the snapshot) ------
        profiles = facade.provider_snapshot()
        provider_id_text, profile, err = _resolve_create_provider(
            profiles, metadata.get("provider_id")
        )
        if err is not None:
            body, status = err
            return body, status

        # Finding 6: reject a disabled profile / upload-disabled policy /
        # missing upload token *synchronously* from the immutable snapshot,
        # before any staging. Never a reachability check - offline creation
        # stays possible.
        err = _provider_policy_error(profile)
        if err is not None:
            body, status = err
            return body, status

        hard_ttl = metadata.get("hard_ttl_seconds", sender.DEFAULT_HARD_TTL_SECONDS)
        if not isinstance(hard_ttl, int) or isinstance(hard_ttl, bool) or hard_ttl <= 0:
            return _json_error("invalid_metadata", "hard_ttl_seconds must be a positive integer"), 400
        err = _validate_ttl(profile, hard_ttl)
        if err is not None:
            body, status = err
            return body, status

        download_grace = metadata.get(
            "download_grace_seconds", sender.DEFAULT_DOWNLOAD_GRACE_SECONDS
        )
        if (
            not isinstance(download_grace, int)
            or isinstance(download_grace, bool)
            or download_grace <= 0
        ):
            return _json_error("invalid_metadata", "download_grace_seconds must be a positive integer"), 400

        # --- file part: bounded, chunked, atomic staging (Finding 4) ---------
        file_storage = request.files.get("file")
        if file_storage is None or file_storage.filename == "":
            return _json_error("invalid_metadata", "missing file part"), 400

        # Mint the ids up front so the server-generated temp/spool names are
        # derived from them, never from the browser filename.
        attachment_id = uuid.uuid4().hex
        command_id = mint_command_id()
        spool_dir = facade.spool_outgoing_dir()
        spool_path = spool_dir / attachment_id

        try:
            temp_path, file_sha256, head, size, text_clean = _stage_spool_file(
                file_storage, spool_dir, attachment_id
            )
        except _FileTooLarge:
            return _json_error("file_too_large", "file exceeds the 5 MiB cap"), 400
        except OSError as exc:
            _log.error(
                "MCAttach create endpoint: spool staging failed (%s)", type(exc).__name__
            )
            return _internal_error_response()

        # Finding 8: MIME is established from content, and only then does the
        # rest of the validation run. Zero-byte policy first: an empty file has
        # no content to establish a MIME from, so it is `mime_not_allowed`
        # rather than defaulting to `text/plain`.
        if size == 0:
            _discard_spool(temp_path)
            return _json_error("mime_not_allowed", "empty file has no detectable content type"), 400

        mime_type = mime_allowlist.sniff_mime_type(head)
        if mime_type is None:
            _discard_spool(temp_path)
            return _json_error("mime_not_allowed", "file content is not an allowed type"), 400

        # Finding 8: for text-family MIME the *whole* stream must be clean
        # UTF-8 with no NUL byte - the head alone is not enough (binary/
        # invalid content appearing after byte 512 must still be rejected).
        # Binary formats are exempt: they are identified by magic bytes and
        # legitimately contain NUL/non-UTF-8 bytes.
        if mime_type in mime_allowlist.TEXT_FAMILY_MIME_TYPES and not text_clean:
            _discard_spool(temp_path)
            return _json_error(
                "mime_not_allowed", "text content contains binary or invalid UTF-8 bytes"
            ), 400

        # Finding 8: a leading `{`/`[` is not enough to claim JSON - the whole
        # document must parse (bounded by the 5 MiB cap, only for JSON).
        if mime_type == "application/json":
            try:
                mime_allowlist.validate_json_document(temp_path)
            except ValueError:
                _discard_spool(temp_path)
                return _json_error("mime_not_allowed", "file content is not valid JSON"), 400

        # Finding 8: the filename's extension is normalized to be consistent
        # with the *sniffed* MIME (never the reverse), so the worker's later
        # `is_allowed_extension` re-check in `_step_validating` cannot reject
        # a request accepted here.
        source_name = mime_allowlist.normalize_file_name_for_mime(
            _sanitize_source_name(file_storage.filename), mime_type
        )

        # Finding 6: provider size policy - reject *synchronously* when the
        # deterministic ciphertext upper bound for this plaintext already
        # exceeds the snapshot's `max_ciphertext_bytes`, before publishing.
        # This is a conservative pre-encryption check only; the authoritative
        # post-encryption limit (the Relay's own `total_size` check at
        # `create_upload`) still runs unchanged before upload.
        if ciphertext_size(size) > profile.max_ciphertext_bytes:
            _discard_spool(temp_path)
            return _json_error(
                "ciphertext_too_large",
                "file ciphertext would exceed the provider maximum",
            ), 400

        # --- compute canonical hash (§3.5) -----------------------------------
        canonical_hash = compute_canonical_hash(
            file_sha256,
            build_canonical_json(
                source_address=source_address,
                adapter_id=mca_runtime.ADAPTER_ID,
                connector_profile_id=mca_runtime.ADAPTER_ID,
                route_type=RouteType.DIRECT.value,
                route_id=source_address,
                provider_id=provider_id_text,
                comment=comment,
                hard_ttl_seconds=hard_ttl,
                download_grace_seconds=download_grace,
                source_name=source_name,
                mime_type=mime_type,
            ),
        )

        # --- atomic publish (§7.2: spool/outgoing/<attachment_id>, Finding 4) -
        # Only after size, MIME, and metadata validation have all passed. The
        # temp file was created exclusively (mkstemp); the final name is a
        # freshly-minted uuid4 hex, so a collision cannot overwrite an existing
        # spool file - the exists() guard below fails closed rather than ever
        # clobbering one, and `os.replace` is the same-directory atomic rename.
        try:
            if spool_path.exists():
                _log.error(
                    "MCAttach create endpoint: spool id collision for a minted attachment id"
                )
                _discard_spool(temp_path)
                return _internal_error_response()
            os.replace(temp_path, spool_path)
        except OSError as exc:
            _log.error(
                "MCAttach create endpoint: spool publish failed (%s)", type(exc).__name__
            )
            _discard_spool(temp_path)
            return _internal_error_response()

        # --- reserve + enqueue (§3.6) ---------------------------------------
        reservation = PendingReservation(
            canonical_hash=canonical_hash,
            attachment_id=attachment_id,
            command_id=command_id,
        )
        command = Command(
            command_id=command_id,
            kind="attachment_create",
            payload={
                "attachment_id": attachment_id,
                "client_request_id": client_request_id,
                "canonical_hash": canonical_hash,
                "source_address": source_address,
                "source_name": source_name,
                "mime_type": mime_type,
                "provider_id": provider_id_text,
                "comment": comment,
                "hard_ttl_seconds": hard_ttl,
                "download_grace_seconds": download_grace,
            },
            created_at=time.time(),
        )
        try:
            outcome = facade.submit_create(
                command, client_request_id=client_request_id, reservation=reservation
            )
        except CommandQueueFull:
            _discard_spool(spool_path)
            return _json_error("command_queue_full", "command queue is full"), 429
        except FacadeNotReady:
            _discard_spool(spool_path)
            raise

        if outcome.kind == "fresh":
            return jsonify({
                "ok": True,
                "command_id": command_id,
                "attachment_id": attachment_id,
            }), 202

        # Non-fresh: this request's freshly-staged file is unused (the worker
        # will not create a row for it), so discard it before returning.
        _discard_spool(spool_path)
        if outcome.kind == "replay_pending":
            existing = outcome.reservation
            return jsonify({
                "ok": True,
                "command_id": existing.command_id,
                "attachment_id": existing.attachment_id,
                "replayed": True,
            }), 202
        if outcome.kind == "replay_committed":
            entry = outcome.committed_entry
            record = facade.get_attachment(entry.attachment_id)
            body = {"ok": True, "attachment_id": entry.attachment_id}
            if record is not None:
                body["state"] = record.state
            return jsonify(body), 200
        return _json_error("idempotency_conflict", "idempotency conflict"), 409
