"""api/api_attachments.py

Step 1.6A.2: the read-only MCAttach REST surface (internal-rest-api.md
§7.1). MIT-licensed Core code - never `meshtastic`, never anything under
`adapters/meshtastic/`.

Exactly the ten `GET` endpoints §7.1 defines, and nothing else:

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

Every mutation, `GET /api/attachments/{id}/content`, contacts/connectors,
multipart upload, and provider onboarding is deliberately out of scope
here (1.6A.3+).

Threading boundary (the point of Step 1.6A.1's facade - §3.1/§3.2): these
handlers only ever read the worker-published in-memory snapshots and
registries through `AttachmentsFacade`. They never touch SQLite, the
filesystem, the network, the radio, or the worker's tick lock, and they
never create/lazily-initialize the MCA runtime - `mca_runtime.
get_attachments_facade()` returns `None` until the runtime has actually
been constructed (an explicit "not ready" signal, mapped to 503 here,
never a fallback that would open `attachments.db` from a request thread).

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
import logging
import re
import time
from collections.abc import Mapping
from functools import wraps

from flask import jsonify, request

from meshsrv.attachments import mca_runtime, receiver, sender
from meshsrv.attachments.commands import Command, CommandQueueFull, mint_command_id
from meshsrv.attachments.delivery.meshtastic import MESHTASTIC_TEXT_MAX_PAYLOAD_BYTES
from meshsrv.attachments.facade import FacadeNotReady
from meshsrv.attachments.provider_registry import (
    ProviderRegistryError,
    decode_provider_id,
    encode_provider_id,
)
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


def _internal_error_response():
    """The one sanitized 500 an unexpected exception maps to (§11): a stable
    public message + `internal_error` - never `str(e)`, a class name, a
    traceback, a path, or an identifier."""
    return jsonify({
        "ok": False,
        "error": "Internal server error",
        "error_code": "internal_error",
    }), 500


def _mca_error_boundary(fn):
    """The local sanitized exception boundary for every MCAttach read
    handler. It sits *beneath* the project-wide `handle_errors` decorator,
    so it sees (and fully handles) every exception first: `handle_errors`
    then only ever returns the clean response, never its own leaky 500.

    `FacadeNotReady` -> 503 `mca_not_ready`; anything else -> 500
    `internal_error`, logging only the handler name and the exception class
    (never `str(exc)`, args, `exc_info`, the request body, the query string,
    or any identifier)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FacadeNotReady:
            return _not_ready()
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
        before anything is enqueued. `CommandQueueFull` maps to 429
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
            return _json_error("invalid_state_transition", "invalid state transition"), 409
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
