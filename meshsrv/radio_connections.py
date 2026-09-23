"""Radio Profiles & Connections Model, PR 1: the read/write domain API for
a radio record's `connections` dict - the persisted, multi-connection
evolution of the single `transport`/`endpoint` pair meshsrv/radio_endpoint.py
has always dealt with. One radio can be reachable over more than one
transport (USB, TCP/Wi-Fi, BLE all at once, physically) even though only
one is ever the *active* link at runtime (TransportRouter's own job,
untouched by this module) - this is where "which endpoints have we seen
work for this radio, and which one does the user prefer" lives.

SCOPE (PR 1 is domain model / persistence only): every function here is
pure - `radio: Mapping in, dict out`, never mutates its input, never
touches a file, never imports `meshtastic` or anything from
adapters/meshtastic/. Callers (storage/profile_manager.py,
api/api_meshtastic.py, server.py) own the actual persistence (calling
InstanceManager.save() / ProfileManager's own write path) and the policy
of WHEN to call which of these - e.g. whether accepting a new transport
should also make it preferred is a decision the call site makes by
composing remember_connection() + set_preferred_transport() explicitly,
not a hidden side effect of remember_connection() alone. See
meshsrv/radio_endpoint.py's own module docstring for the shared record
shape and the lazy/on-read migration strategy this builds on.

RUNTIME STATE IS NEVER HERE: CONNECTED/READY/ERROR/listener_pid/
connected_since always come from TransportRouter/RadioTransport.
get_connection_info() - this module only ever persists `endpoint` and
`last_successful_at`/`last_successful_transport`, nothing live.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Optional

from meshsrv.radio_endpoint import endpoint_descriptor, normalize_radio_record
from meshsrv.radio_transport import ConnectionDescriptor


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_connections(radio: Optional[Mapping]) -> dict:
    """Returns the radio's full `connections` dict, keyed by transport
    ("serial"/"tcp"/"bluetooth"), each value at minimum holding an
    `endpoint`. Never empty - normalize_radio_record() always synthesizes
    at least one entry from the record's legacy singular transport/
    endpoint if `connections` itself is missing."""
    return normalize_radio_record(radio)["connections"]


def get_connection(radio: Optional[Mapping], transport: str) -> Optional[dict]:
    """Returns the stored connection record for one transport, or None
    if this radio has never had one remembered."""
    return get_connections(radio).get(str(transport or "").strip().lower())


def remember_connection(radio: Optional[Mapping], transport: str, endpoint: Mapping) -> dict:
    """Returns a NEW radio dict with `connections[transport]` set to
    `endpoint`, merged in alongside whatever other transports' entries
    already existed (never drops them - this is the actual fix for "a
    profile accepted over TCP, later also connected over serial, must
    remember both", not just avoid a duplicate profile). Preserves any
    pre-existing `last_successful_at` for THIS transport if the new
    endpoint is unchanged; a genuinely new/changed endpoint clears it -
    a stale success timestamp for an address we've since moved away from
    would be actively misleading.

    Does NOT touch `preferred_transport` or `last_successful_transport` -
    see this module's own docstring for why that's a separate, explicit
    caller decision (set_preferred_transport() / record_success())."""
    transport = str(transport or "").strip().lower()
    normalized = normalize_radio_record(radio)
    connections = dict(normalized["connections"])

    new_endpoint = dict(endpoint or {})
    existing_entry = connections.get(transport) or {}
    existing_endpoint = existing_entry.get("endpoint") if isinstance(existing_entry, Mapping) else None
    entry = {"endpoint": new_endpoint}
    if existing_endpoint == new_endpoint and isinstance(existing_entry, Mapping) and existing_entry.get("last_successful_at"):
        entry["last_successful_at"] = existing_entry["last_successful_at"]

    connections[transport] = entry
    normalized["connections"] = connections
    return normalized


def set_preferred_transport(radio: Optional[Mapping], transport: str) -> dict:
    """Returns a NEW radio dict with `preferred_transport` set, and the
    legacy singular `transport`/`endpoint` mirrored from
    `connections[transport]` if a connection is already remembered for
    it (falling back to normalize_radio_record()'s own default endpoint
    shape for that transport type if not, so the legacy fields never end
    up structurally wrong for the new transport - e.g. a "tcp" transport
    carrying serial's `{"port": ...}` endpoint shape).

    This mirroring is *the* mechanism by which legacy code that still
    reads radio.get("transport")/radio.get("endpoint") directly (every
    call site not yet touched by this PR - restore_active_transport(),
    verify_radio_identity(), etc.) keeps seeing the right "current"
    transport without needing to know `connections` exists at all."""
    transport = str(transport or "").strip().lower()
    normalized = normalize_radio_record(radio)

    connection = normalized["connections"].get(transport)
    if isinstance(connection, Mapping) and isinstance(connection.get("endpoint"), Mapping):
        endpoint = dict(connection["endpoint"])
    else:
        # No remembered connection for this transport yet - fall through
        # to the same default-endpoint-shape logic normalize_radio_record()
        # already uses for a transport with no endpoint at all, by
        # normalizing a bare {"transport": transport} record.
        endpoint = normalize_radio_record({"transport": transport})["endpoint"]

    normalized["preferred_transport"] = transport
    normalized["transport"] = transport
    normalized["endpoint"] = endpoint
    return normalized


def record_success(radio: Optional[Mapping], transport: str, when: Optional[str] = None) -> dict:
    """Returns a NEW radio dict with `connections[transport].last_successful_at`
    and the top-level `last_successful_transport` updated to `transport`.
    If this transport has no remembered connection yet, creates a minimal
    one (endpoint = normalize_radio_record()'s default shape) rather than
    silently dropping the success record - a caller is expected to have
    already called remember_connection() with the real endpoint in the
    normal case, but this must never depend on call order to avoid data
    loss.

    `when` defaults to now (UTC, ISO-8601) - callers pass it explicitly
    only for determinism in tests."""
    transport = str(transport or "").strip().lower()
    normalized = normalize_radio_record(radio)
    connections = dict(normalized["connections"])

    existing_entry = connections.get(transport)
    if isinstance(existing_entry, Mapping) and isinstance(existing_entry.get("endpoint"), Mapping):
        entry = dict(existing_entry)
    else:
        entry = {"endpoint": normalize_radio_record({"transport": transport})["endpoint"]}

    entry["last_successful_at"] = when or _now_iso()
    connections[transport] = entry

    normalized["connections"] = connections
    normalized["last_successful_transport"] = transport
    return normalized


def connection_descriptor(radio: Optional[Mapping], transport: str) -> ConnectionDescriptor:
    """The multi-connection equivalent of
    meshsrv/radio_endpoint.py's descriptor_from_radio_record(): builds a
    ConnectionDescriptor for one SPECIFIC transport's remembered
    connection (not necessarily the record's current/preferred one).
    Falls back to normalize_radio_record()'s own default endpoint shape
    if this transport has never been remembered, same as
    set_preferred_transport() above - callers that need to distinguish
    "never connected via this transport" from "connected with a blank
    endpoint" should check get_connection() first."""
    transport = str(transport or "").strip().lower()
    connection = get_connection(radio, transport)
    if isinstance(connection, Mapping) and isinstance(connection.get("endpoint"), Mapping):
        endpoint = connection["endpoint"]
    else:
        endpoint = normalize_radio_record({"transport": transport})["endpoint"]
    return endpoint_descriptor(transport, endpoint)
