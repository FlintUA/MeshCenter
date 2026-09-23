"""Runtime connection status projection - turns TransportRouter's live
RadioTransport.get_connection_info() into the plain dict shape every REST
route that reports "what's the radio doing right now" wants.

Deliberately separate from meshsrv/radio_endpoint.py/meshsrv/radio_connections.py
(both PERSISTED radio-record shape, no live objects, no I/O of any kind):
this module's one function takes live objects (a TransportRouter instance,
a SerialPortSupervisor instance) and reads their CURRENT state - it has no
persisted-data concern at all, the inverse split from those two modules.

Radio Profiles & Connections Model, PR 2: extracted from
api/api_meshtastic.py's register_meshtastic_routes()-local closure
(`_connection_payload()`, still there as a thin wrapper around this
function - its own 3 call sites are unchanged) so server.py's
api_devices_dashboard() can call the SAME transport-aware source instead
of building its own picture from meshsrv/radio_manager.py's
RadioConnectionManager - a serial-only "release the port for an external
app" status tracker that was previously being shown, unconditionally, as
if it were the radio's actual connection state regardless of which
transport was really active (see api_devices_dashboard()'s own comment
for the live-caught bug this fixes: a genuinely-connected TCP radio
showed as if the SERIAL listener were the thing to look at).
"""
from __future__ import annotations


def connection_payload(transport_router, local_node_id: str, core_serial_transport) -> dict:
    """`core_serial_transport` is server.py's Core-owned SerialPortSupervisor
    instance (get_listener_pid() only - see api/api_meshtastic.py's
    register_meshtastic_routes() docstring for why this must be the
    Core-owned instance, never an IPC-backed transport proxy)."""
    info = transport_router.get_connection_info()
    return {
        "state": info.state.value,
        "type": info.descriptor.type.value if info.descriptor else None,
        "address": info.descriptor.address if info.descriptor else None,
        "label": info.descriptor.label if info.descriptor else None,
        # SerialTransport.get_connection_info() hard-codes node_id=None
        # (adapters/meshtastic/serial_transport.py - the protocol doesn't
        # hand this back on the --listen path the way BLE's config stream
        # does, and adding it there would mean scraping NODEINFO_APP
        # output just for a value Core already knows from its own startup
        # config). This is our own node either way - substitute the
        # configured local_node_id whenever the transport itself didn't
        # supply one, instead of showing a blank in the UI.
        "node_id": info.node_id or local_node_id,
        "connected_since": info.connected_since,
        "last_error": str(info.last_error) if info.last_error else None,
        # Serial-specific, not part of RadioTransport - deliberately read
        # from core_serial_transport (Core's own SerialPortSupervisor),
        # never from an IPC-backed transport proxy, since only the
        # Core-owned instance's run_listener() thread actually knows the
        # real listener subprocess PID (meshsrv/serial_port_supervisor.py's
        # get_listener_pid() docstring). None whenever a non-serial
        # transport is active, which is the correct answer, not a missing
        # value.
        "listener_pid": core_serial_transport.get_listener_pid(),
    }
