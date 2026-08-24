"""Meshtastic transport switching (Serial <-> Bluetooth) REST API - Task 46.

Talks to the radio only through meshsrv.transport_router.TransportRouter /
adapters/meshtastic/*.py - no `import meshtastic` here, same rule as every
other Core file since Task 44/45.
"""
from flask import jsonify, request

from meshsrv.radio_transport import ConnectionDescriptor, ConnectionType, TransportError

# Live-measured on TAP2 (Task 45): a real BLE connect() took 71.5-71.8s
# twice. Switch operations use the same margin as BLETransport's own
# raised default, not the ABC's 30.0.
_SWITCH_CONNECT_TIMEOUT_S = 90.0
_SWITCH_DISCONNECT_TIMEOUT_S = 30.0

# TEMPORARY ESTIMATE, needs live verification in Task 47: how long the
# old (usually serial) transport's recovery reconnect gets, after a
# failed switch, before giving up. Deliberately NOT _SWITCH_CONNECT_
# TIMEOUT_S (90.0) - that figure was calibrated from live BLE connect()
# measurements specifically, and carrying it over unexamined to
# SerialTransport.connect(force=True) would repeat the exact mistake
# the 30.0s BLE default was (Task 45: an untested number copied from
# the ABC's docstring, not the real hardware). 45.0 is a margin over
# the already-accepted disconnect() default (30.0s), not a measurement.
_RECOVERY_CONNECT_TIMEOUT_S = 45.0


def register_meshtastic_routes(
    app,
    handle_errors,
    state_lock,
    settings,
    save_settings,
    transport_router,
    serial_transport,
    ble_transport,
    serial_port,
    local_node_id,
):
    def _connection_payload():
        info = transport_router.get_connection_info()
        return {
            "state": info.state.value,
            "type": info.descriptor.type.value if info.descriptor else None,
            "address": info.descriptor.address if info.descriptor else None,
            "label": info.descriptor.label if info.descriptor else None,
            # SerialTransport.get_connection_info() hard-codes node_id=None
            # (adapters/meshtastic/serial_transport.py - the protocol
            # doesn't hand this back on the --listen path the way BLE's
            # config stream does, and adding it there would mean scraping
            # NODEINFO_APP output just for a value Core already knows from
            # its own startup config). This is our own node either way -
            # substitute the configured LOCAL_NODE_ID whenever the
            # transport itself didn't supply one, instead of showing a
            # blank in the UI.
            "node_id": info.node_id or local_node_id,
            "connected_since": info.connected_since,
            "last_error": str(info.last_error) if info.last_error else None,
            # Serial-specific, not part of RadioTransport - server.py keeps
            # a direct reference to serial_transport for exactly this (see
            # adapters/meshtastic/serial_transport.py's get_listener_pid()
            # docstring). None whenever Bluetooth is the active transport,
            # which is the correct answer, not a missing value.
            "listener_pid": serial_transport.get_listener_pid(),
        }

    def _persist_choice(transport_name, ble_address="", ble_name=""):
        with state_lock:
            section = dict(settings.get("meshtastic") or {})
            section["transport"] = transport_name
            if transport_name == "bluetooth":
                section["ble_address"] = ble_address
                section["ble_name"] = ble_name
            settings["meshtastic"] = section
            save_settings()

    def _switch(connect_new, target_transport_name, ble_address="", ble_name=""):
        """Runs connect_new() through transport_router.switch() (see
        meshsrv/transport_router.py - the whole disconnect-old/connect-
        new/reassign sequence is mutually exclusive with any other call
        on the router, by design).

        FAIL-CLOSED, single recovery path: connect_new() is expected to
        call the old transport's disconnect() with no try/except of its
        own - a disconnect() that times out (teardown genuinely still in
        flight on the physical link - see the review discussion this
        round) must abort the switch exactly like a failed connect() on
        the new transport does, not proceed on an unverified physical
        state. Both failure shapes surface as the same TransportError
        raised out of connect_new(), so there is exactly one recovery
        branch below, not two to keep in sync.

        On any such failure, the old (usually serial) transport may
        already be mid-disconnect or fully disconnected - attempt to
        bring it back so the system doesn't end up with neither
        transport usable."""
        try:
            transport_router.switch(connect_new)
        except TransportError as error:
            recovery_error = None
            if target_transport_name != "serial":
                try:
                    serial_transport.connect(
                        ConnectionDescriptor(type=ConnectionType.SERIAL, address=serial_port),
                        force=True,
                        timeout=_RECOVERY_CONNECT_TIMEOUT_S,
                    )
                except TransportError as recon_err:
                    recovery_error = recon_err
            if recovery_error is not None:
                return jsonify({
                    "ok": False,
                    "error": f"{error}; serial reconnect also failed: {recovery_error}",
                    "error_code": "transport_switch_failed_both_down",
                }), 503
            return jsonify({
                "ok": False,
                "error": str(error),
                "error_code": "transport_switch_failed",
            }), 503

        _persist_choice(target_transport_name, ble_address, ble_name)
        return jsonify({"ok": True, "connection": _connection_payload()})

    @app.route("/api/meshtastic/connection", methods=["GET"])
    @handle_errors
    def api_meshtastic_connection():
        return jsonify({"ok": True, "connection": _connection_payload()})

    @app.route("/api/meshtastic/bluetooth/scan", methods=["POST"])
    @handle_errors
    def api_meshtastic_bluetooth_scan():
        try:
            devices = ble_transport.scan(timeout=20)
        except TransportError as error:
            return jsonify({"ok": False, "error": str(error), "error_code": "ble_scan_failed"}), 503
        return jsonify({"ok": True, "devices": devices})

    @app.route("/api/meshtastic/bluetooth/connect", methods=["POST"])
    @handle_errors
    def api_meshtastic_bluetooth_connect():
        data = request.get_json(silent=True) or {}
        address = str(data.get("address") or "").strip()
        name = str(data.get("name") or "").strip()
        if not address:
            return jsonify({
                "ok": False,
                "error": "BLE address is required",
                "error_code": "ble_address_required",
            }), 400

        def _connect_new():
            serial_transport.disconnect(timeout=_SWITCH_DISCONNECT_TIMEOUT_S)
            ble_transport.connect(
                ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address=address, label=name),
                force=True,
                timeout=_SWITCH_CONNECT_TIMEOUT_S,
            )
            return ble_transport

        return _switch(_connect_new, "bluetooth", ble_address=address, ble_name=name)

    @app.route("/api/meshtastic/transport", methods=["POST"])
    @handle_errors
    def api_meshtastic_set_transport():
        """Generic switch, driven by settings.meshtastic for Bluetooth
        (reconnects to whichever device was last used via /bluetooth/
        connect) - use /bluetooth/connect directly to connect to a
        newly-scanned device instead."""
        data = request.get_json(silent=True) or {}
        target = str(data.get("type") or "").strip().lower()
        if target not in ("serial", "bluetooth"):
            return jsonify({
                "ok": False,
                "error": "type must be 'serial' or 'bluetooth'",
                "error_code": "invalid_transport_type",
            }), 400

        if target == "serial":
            def _connect_new():
                ble_transport.disconnect(timeout=_SWITCH_DISCONNECT_TIMEOUT_S)
                serial_transport.connect(
                    ConnectionDescriptor(type=ConnectionType.SERIAL, address=serial_port),
                    force=True,
                    timeout=_SWITCH_CONNECT_TIMEOUT_S,
                )
                return serial_transport

            return _switch(_connect_new, "serial")

        with state_lock:
            saved = dict(settings.get("meshtastic") or {})
        address = str(saved.get("ble_address") or "").strip()
        name = str(saved.get("ble_name") or "").strip()
        if not address:
            return jsonify({
                "ok": False,
                "error": "No previously-connected Bluetooth device - use Scan + Connect first",
                "error_code": "ble_address_required",
            }), 400

        def _connect_new():
            serial_transport.disconnect(timeout=_SWITCH_DISCONNECT_TIMEOUT_S)
            ble_transport.connect(
                ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address=address, label=name),
                force=True,
                timeout=_SWITCH_CONNECT_TIMEOUT_S,
            )
            return ble_transport

        return _switch(_connect_new, "bluetooth", ble_address=address, ble_name=name)

    @app.route("/api/meshtastic/reconnect", methods=["POST"])
    @handle_errors
    def api_meshtastic_reconnect():
        """Reconnects whichever transport is currently active - does not
        switch types. transport_router.reconnect() delegates to the
        active transport's own reconnect() (naive fixed-attempts-with-
        backoff on BLETransport, disconnect+connect(force=True) on
        SerialTransport)."""
        try:
            transport_router.reconnect(timeout=_SWITCH_CONNECT_TIMEOUT_S)
        except TransportError as error:
            return jsonify({"ok": False, "error": str(error), "error_code": "reconnect_failed"}), 503
        return jsonify({"ok": True, "connection": _connection_payload()})
