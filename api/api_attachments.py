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
internal SQLite fields never reach the wire. `handle_errors` (server.py)
would otherwise turn an uncaught exception into a 500 envelope that leaks
`str(e)`, so the readiness-gated reads catch `FacadeNotReady` themselves
and the id/query validation returns the documented 400/404 codes directly.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from flask import jsonify, request

from meshsrv.attachments import mca_runtime, receiver, sender
from meshsrv.attachments.delivery.meshtastic import MESHTASTIC_TEXT_MAX_PAYLOAD_BYTES
from meshsrv.attachments.facade import FacadeNotReady
from meshsrv.attachments.snapshots import (
    serialize_attachment_public,
    serialize_delivery,
    serialize_timeline_event,
)
from meshsrv.connectivity_monitor import RelayState, evaluate_upload_readiness

# ---- id shapes (§7.9: attachment_id and command_id are both uuid4().hex) --

_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


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


# ---- small response helpers ----------------------------------------------


def _not_ready():
    """The one 503 every handler returns before the MCA runtime exists."""
    return jsonify({
        "ok": False,
        "error": "MCAttach service is not ready",
        "error_code": "mca_not_ready",
    }), 503


def _json_error(error_code: str, message: str):
    return jsonify({"ok": False, "error": message, "error_code": error_code})


def _parse_int(raw, *, default, minimum=None, maximum=None) -> int:
    """Parse an integer query param, falling back to `default` on anything
    unparseable and clamping into `[minimum, maximum]`. Pagination bounds
    are best-effort (§7.1 documents no error code for a malformed
    limit/offset), so a garbage value degrades to the default rather than
    inventing a 400."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _parse_optional_int(raw):
    """Parse an optional non-negative integer query param. Returns `None`
    when absent/empty, the int when valid, and raises `ValueError` when
    present but not a valid non-negative integer (a caller bug worth a
    400, not a silent skip)."""
    if raw is None or raw == "":
        return None
    value = int(raw)
    if value < 0:
        raise ValueError("must be non-negative")
    return value


def _resolve_attachment(facade, attachment_id):
    """Validate `attachment_id` and look it up in the published snapshot.
    Returns `(record, None)` on success, or `(None, (body, status))` with a
    fully-built response on a validation/not-found/not-ready error. Catches
    `FacadeNotReady` itself so `handle_errors` can never leak exception
    text into a 500 for the normal "service still starting" case."""
    if not _is_hex32(attachment_id):
        return None, (_json_error("invalid_attachment_id", "invalid attachment id"), 400)
    try:
        record = facade.get_attachment(attachment_id)
    except FacadeNotReady:
        return None, (_not_ready()[0], 503)
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

        limit = _parse_int(request.args.get("limit"), default=100, minimum=1, maximum=500)
        offset = _parse_int(request.args.get("offset"), default=0, minimum=0)

        try:
            snapshot = facade.attachments_snapshot()
        except FacadeNotReady:
            return _not_ready()

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
    def mca_delivery_adapters():
        facade = _facade()
        if facade is None:
            return _not_ready()

        # Read-only: the one real adapter (delivery/meshtastic.py) reports
        # `connector_state`/`ack_semantics` from the *live* radio transport,
        # which a read endpoint must not touch (§7.1 is a static capability
        # snapshot, not a radio probe). This is the documented §7.1 shape,
        # with the payload ceiling taken from the adapter's own constant.
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
                    "connector_state": "READY",
                },
            }],
        })

    @app.route("/api/mca/providers", methods=["GET"])
    @handle_errors
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
    def mca_provider(provider_id):
        facade = _facade()
        if facade is None:
            return _not_ready()

        profile = facade.provider_snapshot().get(provider_id)
        if profile is None:
            return _json_error("provider_not_found", "provider not found"), 404

        relay_status = facade.connectivity_snapshot().relays.get(provider_id)
        return jsonify({"ok": True, "provider": _serialize_provider(profile, relay_status)})

    @app.route("/api/mca/providers/<provider_id>/upload-readiness", methods=["GET"])
    @handle_errors
    def mca_provider_upload_readiness(provider_id):
        facade = _facade()
        if facade is None:
            return _not_ready()

        try:
            ciphertext_bytes = _parse_optional_int(request.args.get("ciphertext_bytes"))
            requested_ttl_seconds = _parse_optional_int(request.args.get("requested_ttl_seconds"))
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
    def mca_identity():
        facade = _facade()
        if facade is None:
            return _not_ready()

        return jsonify({"ok": True, **_serialize_identity(facade.identity_snapshot())})

    @app.route("/api/mca/commands/<command_id>", methods=["GET"])
    @handle_errors
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
