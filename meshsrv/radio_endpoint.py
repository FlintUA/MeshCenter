"""Transport-neutral radio *record* shape - what's persisted about which
radio a profile/instance identity is bound to (node_id, transport type,
endpoint) - and the small set of pure functions that build a
ConnectionDescriptor/connect-orchestration callable from one.

Deliberately separate from meshsrv/radio_transport.py (the runtime
RadioTransport ABC/dataclasses) and from storage/profile_manager.py (the
on-disk persistence mechanics): this module only knows about the *shape*
of a radio record and how to turn it into what TransportRouter/
AdapterIPCTransport need. No `meshtastic` import, no file I/O - matches
this package's existing convention (meshsrv/*.py stays MIT/stdlib-plus-
first-party-only).

BACKWARD COMPATIBILITY (Radio TCP Transport, part 2): existing stored
records (data/instance.json's `radio` block, each profile's
profile.json `metadata.radio` block) predate the `transport`/`endpoint`
fields this module introduces and only ever had a flat `port` string
(a serial device path, implicitly). normalize_radio_record() is the ONE
place that defaulting happens - callers throughout server.py/api/*.py
call it instead of re-deriving "is this record legacy" logic themselves.
Existing records are never rewritten merely by reading them; a record
only gains the new fields the next time it's naturally saved (a profile
accept/activate, or a live transport switch).
"""
from __future__ import annotations

from typing import Callable, Mapping, Optional

from meshsrv.radio_transport import ConnectionDescriptor, ConnectionType, RadioTransport

# The Meshtastic radio's own default TCP port. Intentionally duplicated
# from adapters/meshtastic/tcp_transport.py's own DEFAULT_TCP_PORT rather
# than imported from there - Core must never import anything from
# adapters/meshtastic/ even for a harmless constant (see
# adapters/meshtastic/ipc_server.py's _ADAPTER_TIMEOUT_MARGIN_S for the
# same established precedent and the same reasoning).
DEFAULT_TCP_PORT = 4403

# Shared connect/disconnect timeout budget for every transport-switch-
# shaped operation (a live Settings switch, this same switch's recovery-
# to-previous-transport path, and the startup transport-restore path -
# see server.py's start_runtime()). Live-measured on TAP2 for Bluetooth
# specifically (a real connect() took 71.5-71.8s twice - see
# api/api_meshtastic.py's original _SWITCH_CONNECT_TIMEOUT_S comment,
# preserved here) and reused as one shared, generous budget for serial
# and TCP too rather than inventing separate unverified numbers for each
# - both are expected to connect considerably faster in practice, so this
# is a safe, if not tight, upper bound for either.
SWITCH_CONNECT_TIMEOUT_S = 90.0
SWITCH_DISCONNECT_TIMEOUT_S = 30.0


def normalize_radio_record(radio: Optional[Mapping]) -> dict:
    """Returns `radio` with `transport` and `endpoint` guaranteed present.

    A record with no `transport` key is legacy-serial: normalizes to
    transport="serial", endpoint={"port": <the existing `port` value>}.
    The legacy flat `port` field is ALWAYS preserved unchanged alongside
    the new fields - many existing call sites throughout server.py still
    read radio.get("port") directly, and removing it would be a breaking
    change this function's whole purpose is to avoid.
    """
    radio = dict(radio or {})
    transport = str(radio.get("transport") or "").strip().lower()
    endpoint = radio.get("endpoint")

    if transport and isinstance(endpoint, Mapping):
        radio["transport"] = transport
        radio["endpoint"] = dict(endpoint)
        return radio

    if not transport:
        transport = "serial"

    if not isinstance(endpoint, Mapping):
        if transport == "tcp":
            # A TCP record missing its endpoint has no meaningful default
            # (unlike serial, there's no historical flat field to recover
            # a host from) - host empty, port DEFAULT_TCP_PORT. Callers
            # that need a connectable endpoint must check for a blank
            # host themselves (see descriptor_from_radio_record()).
            endpoint = {"host": "", "port": DEFAULT_TCP_PORT}
        elif transport == "bluetooth":
            endpoint = {"address": str(radio.get("port") or "").strip(), "label": ""}
        else:
            endpoint = {"port": str(radio.get("port") or "").strip()}

    radio["transport"] = transport
    radio["endpoint"] = dict(endpoint)
    return radio


def descriptor_from_radio_record(radio: Optional[Mapping]) -> ConnectionDescriptor:
    """Builds the ConnectionDescriptor TransportRouter/AdapterIPCTransport
    need from a (possibly legacy) stored radio record. Normalizes first,
    so this is safe to call directly on raw INSTANCE_IDENTITY.radio /
    profile metadata without a separate normalize_radio_record() call."""
    normalized = normalize_radio_record(radio)
    transport = normalized["transport"]
    endpoint = normalized["endpoint"]

    if transport == "tcp":
        host = str(endpoint.get("host") or "").strip()
        port = int(endpoint.get("port") or DEFAULT_TCP_PORT)
        return ConnectionDescriptor(type=ConnectionType.TCP, address=f"{host}:{port}")
    if transport == "bluetooth":
        return ConnectionDescriptor(
            type=ConnectionType.BLUETOOTH,
            address=str(endpoint.get("address") or "").strip(),
            label=str(endpoint.get("label") or "").strip(),
        )
    return ConnectionDescriptor(type=ConnectionType.SERIAL, address=str(endpoint.get("port") or "").strip())


def build_transport_connect_new(
    transport_type: str,
    *,
    serial_transport: RadioTransport,
    ble_transport: RadioTransport,
    tcp_transport: RadioTransport,
    serial_port: str = "",
    ble_address: str = "",
    ble_name: str = "",
    tcp_host: str = "",
    tcp_port: int = DEFAULT_TCP_PORT,
    connect_timeout: float = SWITCH_CONNECT_TIMEOUT_S,
    disconnect_timeout: float = SWITCH_DISCONNECT_TIMEOUT_S,
) -> Callable[[], RadioTransport]:
    """Returns a zero-arg callable for TransportRouter.switch(): the ONE
    place that builds "disconnect whatever else might be holding a link,
    then connect(force=True) the target transport" for any of the three
    transport types. Used by api/api_meshtastic.py's live Settings switch,
    its recovery-to-previous-transport path, AND server.py's start_runtime()
    transport-restore-on-boot path - previously three separate, drifting
    implementations of the same sequence (two in api_meshtastic.py, none
    at all for startup - see start_runtime()'s own docstring for that gap).

    Disconnecting BOTH of the two non-target transports (not just "the
    other one", now that there are three) is safe and idempotent -
    disconnect() on an already-disconnected transport is a documented
    no-op on all three implementations (SerialTransport by design, BLE/
    TCP via their shared _detach_and_close_async() pattern) - and at most
    one of the two is ever actually holding a real link, since
    TransportRouter only ever has one _active transport at a time.
    """

    def _disconnect_others(target: RadioTransport) -> None:
        for other in (serial_transport, ble_transport, tcp_transport):
            if other is not target:
                other.disconnect(timeout=disconnect_timeout)

    if transport_type == "serial":
        def _connect_new():
            _disconnect_others(serial_transport)
            serial_transport.connect(
                ConnectionDescriptor(type=ConnectionType.SERIAL, address=serial_port),
                force=True,
                timeout=connect_timeout,
            )
            return serial_transport

        return _connect_new

    if transport_type == "bluetooth":
        def _connect_new():
            _disconnect_others(ble_transport)
            ble_transport.connect(
                ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address=ble_address, label=ble_name),
                force=True,
                timeout=connect_timeout,
            )
            return ble_transport

        return _connect_new

    if transport_type == "tcp":
        def _connect_new():
            _disconnect_others(tcp_transport)
            tcp_transport.connect(
                ConnectionDescriptor(type=ConnectionType.TCP, address=f"{tcp_host}:{tcp_port}"),
                force=True,
                timeout=connect_timeout,
            )
            return tcp_transport

        return _connect_new

    raise ValueError(f"unknown transport_type: {transport_type!r}")
