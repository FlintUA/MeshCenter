"""Meshtastic transport switching (Serial <-> Bluetooth <-> TCP) REST API -
Task 46, extended by Radio TCP Transport part 2.

Talks to the radio only through meshsrv.transport_router.TransportRouter /
adapters/meshtastic/*.py - no `import meshtastic` here, same rule as every
other Core file since Task 44/45.
"""
from flask import jsonify, request

from meshsrv.radio_endpoint import (
    DEFAULT_TCP_PORT,
    SWITCH_CONNECT_TIMEOUT_S as _SWITCH_CONNECT_TIMEOUT_S,
    SWITCH_DISCONNECT_TIMEOUT_S as _SWITCH_DISCONNECT_TIMEOUT_S,
    build_transport_connect_new,
)
from meshsrv.radio_identity import compare_radio_identity, fetch_connected_tcp_identity
from meshsrv.radio_transport import TransportError


def register_meshtastic_routes(
    app,
    handle_errors,
    state_lock,
    settings,
    save_settings,
    transport_router,
    serial_transport,
    ble_transport,
    tcp_transport,
    serial_port,
    local_node_id,
    core_serial_transport,
    instance_manager,
):
    """Task 48: `serial_transport`/`ble_transport`/`tcp_transport` here are
    Core-side IPC proxies (meshsrv.adapter_ipc_client.AdapterIPCTransport)
    talking to the adapter subprocess - everything below that calls
    .connect()/.disconnect()/.scan() on them is unchanged in structure
    from Task 46/47, just now crossing a process boundary underneath.
    `core_serial_transport` is a DIFFERENT object - server.py's own
    SerialPortSupervisor instance (meshsrv/serial_port_supervisor.py,
    see its construction site for why it's kept around) - used here for
    exactly one thing, get_listener_pid(), never for a real send/connect/get
    operation. Keeping these as two distinct parameters, not one object
    doing double duty, is deliberate: it makes "this route never
    accidentally calls a real radio operation on the Core-owned instance"
    checkable by reading the parameter list, not just by convention.

    `instance_manager` (Radio TCP Transport part 2, new): every
    successful switch below also writes INSTANCE_IDENTITY.radio.transport/
    endpoint through _persist_choice(), not just settings.meshtastic -
    that's the boot-time source of truth server.py's start_runtime()
    TRANSPORT RESTORE block reads to reconnect the right transport after
    a restart (see that block's own comment for the pre-existing gap this
    closes, uniformly for serial/bluetooth/tcp)."""

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
            # Serial-specific, not part of RadioTransport - deliberately
            # read from core_serial_transport (server.py's Core-owned
            # SerialPortSupervisor - see this function's own docstring),
            # never from the IPC-backed `serial_transport` param above,
            # since only the Core-owned instance's run_listener() thread
            # actually knows the real listener subprocess PID
            # (meshsrv/serial_port_supervisor.py's get_listener_pid()
            # docstring). None whenever Bluetooth is the active
            # transport, which is the correct answer, not a missing
            # value.
            "listener_pid": core_serial_transport.get_listener_pid(),
        }

    def _persist_choice(transport_name, ble_address="", ble_name="", tcp_host="", tcp_port=None, tcp_identity=None):
        """`tcp_identity` (Radio TCP Transport part 2 correction pass #4):
        the real node identity just read from the connected radio (via
        fetch_connected_tcp_identity(), called by _switch() below BEFORE
        this function ever runs) - only ever passed for transport_name
        == "tcp", and only once _switch() has already confirmed it's
        either a fresh onboarding (no accepted identity yet) or a
        confirmed MATCH against the accepted one. A bare transport/
        endpoint write with no identity update (the pre-correction
        behavior) would leave a stale node_id/long_name/etc. from
        whatever was accepted before - exactly the inconsistency a live
        pixel-111 test caught (transport+endpoint said "TCP to the
        T-Beam", but node_id still said a different, previously-accepted
        radio)."""
        with state_lock:
            section = dict(settings.get("meshtastic") or {})
            section["transport"] = transport_name
            if transport_name == "bluetooth":
                section["ble_address"] = ble_address
                section["ble_name"] = ble_name
            if transport_name == "tcp":
                section["tcp_host"] = tcp_host
                section["tcp_port"] = tcp_port
            settings["meshtastic"] = section
            save_settings()

        # Radio TCP Transport part 2: also keep INSTANCE_IDENTITY.radio.
        # transport/endpoint in sync, not just settings.meshtastic - this
        # is the boot-time source of truth server.py's start_runtime()
        # TRANSPORT RESTORE block reads to reconnect the right transport
        # after a restart (see that block's own comment - a pre-existing
        # gap this fixes uniformly for serial/bluetooth/tcp, previously
        # settings.meshtastic was written here but never read at boot at
        # all). Best-effort: a failure here must not fail the switch
        # itself (settings.meshtastic above already reflects the new
        # choice, and the live transport_router is already correct) - it
        # only means the NEXT restart might not restore correctly.
        try:
            identity = instance_manager.get()
            updated = dict(identity)
            radio = dict(updated.get("radio") or {})
            radio["transport"] = transport_name
            if transport_name == "bluetooth":
                radio["endpoint"] = {"address": ble_address, "label": ble_name}
            elif transport_name == "tcp":
                radio["endpoint"] = {"host": tcp_host, "port": tcp_port}
                if tcp_identity:
                    radio["node_id"] = tcp_identity.get("node_id", "")
                    radio["long_name"] = tcp_identity.get("long_name", "")
                    radio["short_name"] = tcp_identity.get("short_name", "")
                    radio["hardware"] = tcp_identity.get("hardware", "")
                    radio["role"] = tcp_identity.get("role", "")
                    radio["firmware_version"] = tcp_identity.get("firmware_version", "")
                # The legacy flat "port" field means a serial device path -
                # never meaningful for a TCP record (the real connection
                # info is "endpoint" above). Cleared rather than left
                # carrying over a stale serial port from whatever this
                # radio record was before, which must not influence TCP
                # behavior/UI (schema-compat only, some old readers still
                # touch radio.get("port")).
                radio["port"] = ""
            else:
                radio["port"] = serial_port
                radio["endpoint"] = {"port": serial_port}
            updated["radio"] = radio
            instance_manager.save(updated)
        except Exception as error:
            print(f"[MESHTASTIC] Could not persist transport choice to instance identity: {error}", flush=True)

    def _previous_transport_recovery(exclude):
        """Builds a recovery connect_new() callable for whichever
        transport was last known-good (settings.meshtastic.transport,
        written by _persist_choice() on every previous successful
        switch) - or (None, None) if that transport isn't actually
        available to recover to (no serial port configured on a Server
        Mode host, or no saved BLE/TCP address/host to reconnect with).
        `exclude` skips returning a recovery for the transport that just
        failed to connect - recovering "to itself" makes no sense.
        Returns (transport_name, connect_new) or (None, None)."""
        with state_lock:
            saved = dict(settings.get("meshtastic") or {})
        previous = str(saved.get("transport") or "serial").strip().lower()
        if previous == exclude:
            return None, None

        kwargs = dict(
            serial_transport=serial_transport,
            ble_transport=ble_transport,
            tcp_transport=tcp_transport,
            connect_timeout=_SWITCH_CONNECT_TIMEOUT_S,
            disconnect_timeout=_SWITCH_DISCONNECT_TIMEOUT_S,
        )
        if previous == "serial":
            if not serial_port:
                return None, None
            return previous, build_transport_connect_new("serial", serial_port=serial_port, **kwargs)
        if previous == "bluetooth":
            address = str(saved.get("ble_address") or "").strip()
            if not address:
                return None, None
            return previous, build_transport_connect_new(
                "bluetooth", ble_address=address, ble_name=str(saved.get("ble_name") or ""), **kwargs
            )
        if previous == "tcp":
            host = str(saved.get("tcp_host") or "").strip()
            if not host:
                return None, None
            return previous, build_transport_connect_new(
                "tcp", tcp_host=host, tcp_port=int(saved.get("tcp_port") or DEFAULT_TCP_PORT), **kwargs
            )
        return None, None

    def _revert_after_tcp_check_failure(message, error_code):
        """A TCP connect that succeeded at the transport level but then
        failed its post-connect identity check (Radio TCP Transport part
        2 correction pass #4 - either the identity read itself failed,
        or it came back a genuine MISMATCH against the accepted radio)
        must not be left "live" on the just-connected endpoint - that
        would mean outbound sends silently going through an unaccepted
        radio despite the response below saying the switch failed.
        Reuses the exact same recovery-to-previous-transport path a
        connect() failure already goes through in _switch() (never a
        second, ad-hoc recovery mechanism); when there's nothing viable
        to recover to (e.g. a Server Mode host with no serial ever
        configured), disconnects the TCP transport directly instead of
        leaving it connected-but-rejected. _persist_choice() is never
        called for this attempt either way - settings/instance identity
        stay exactly as they were before it started."""
        recovery_error = None
        recovery_name, recovery_connect_new = _previous_transport_recovery(exclude="tcp")
        if recovery_connect_new is not None:
            try:
                transport_router.switch(recovery_connect_new)
            except TransportError as recon_err:
                recovery_error = recon_err
        else:
            try:
                tcp_transport.disconnect(timeout=_SWITCH_DISCONNECT_TIMEOUT_S)
            except TransportError:
                pass

        if recovery_error is not None:
            return jsonify({
                "ok": False,
                "error": f"{message}; {recovery_name} reconnect also failed: {recovery_error}",
                "error_code": f"{error_code}_both_down",
            }), 503
        return jsonify({
            "ok": False,
            "error": message,
            "error_code": error_code,
        }), 409

    def _switch(connect_new, target_transport_name, ble_address="", ble_name="", tcp_host="", tcp_port=None):
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

        RECOVER THE PREVIOUS TRANSPORT, NOT ALWAYS SERIAL (Radio TCP
        Transport part 2 - the task's own explicit requirement): the
        original version of this always attempted to reconnect serial on
        any failure, regardless of what was actually active before the
        switch - wrong on a Server Mode host with no serial radio at all
        (attempting to "recover" a transport that was never configured,
        against a MESHTASTIC_PORT that may not even resolve to a real
        device). _previous_transport_recovery() determines what was
        ACTUALLY active (bluetooth/tcp/serial, whichever it was) and
        whether recovering to it is even viable (a saved address/host
        exists) - when it isn't, no recovery is attempted at all, and
        transport_router is left exactly as connect_new() left it
        (disconnected/ERROR) - "expose disconnected/degraded radio
        state; never silently bind another physical radio."

        RECOVERY MUST GO THROUGH THE ROUTER TOO (live Task 47 finding on
        TAP2, second bug caught by the same forced-failure test): the
        first version of this called serial_transport.connect(...)
        directly here. That reconnected the physical serial link fine,
        but transport_router.self._active was never told about it - it
        stayed pointed at the still-broken ble_transport, so every
        send_*/get_* call kept routing to a transport in ERROR state
        (observed live: a send failed with "BLETransport is not
        connected" even though the serial listener was genuinely running
        with a real PID). Wrapping the recovery connect in its own
        transport_router.switch() call fixes this the same way the
        primary switch already works: self._active only moves to the
        recovery transport if that connect succeeds, atomically, under
        the router's own lock - never a second, ad-hoc path that can
        desync the router's bookkeeping from physical reality."""
        try:
            transport_router.switch(connect_new)
        except TransportError as error:
            recovery_error = None
            recovery_name, recovery_connect_new = _previous_transport_recovery(exclude=target_transport_name)
            if recovery_connect_new is not None:
                try:
                    transport_router.switch(recovery_connect_new)
                except TransportError as recon_err:
                    recovery_error = recon_err
            if recovery_error is not None:
                return jsonify({
                    "ok": False,
                    "error": f"{error}; {recovery_name} reconnect also failed: {recovery_error}",
                    "error_code": "transport_switch_failed_both_down",
                }), 503
            return jsonify({
                "ok": False,
                "error": str(error),
                "error_code": "transport_switch_failed",
            }), 503

        # IDENTITY CHECK, TCP ONLY (Radio TCP Transport part 2 correction
        # pass #4): "Settings -> TCP Connect" reconnects the SAME accepted
        # profile over TCP - it is not an onboarding flow (that's Node
        # Manager -> Discover radio, already transport-aware since
        # correction pass #3). A bare transport/endpoint persist with no
        # identity check (the pre-correction behavior) let this route
        # silently point the accepted profile at whatever radio happens
        # to answer at the given host:port, with no verification at all -
        # live-caught on pixel-111 as a stale node_id that no longer
        # matched the configured TCP endpoint. Bluetooth/serial have no
        # such check (matching their own existing, accepted scope
        # boundary elsewhere in this file/server.py) - only TCP gets one
        # here, since only TCP can name an arbitrary new endpoint by IP.
        tcp_identity = None
        if target_transport_name == "tcp":
            try:
                tcp_identity = fetch_connected_tcp_identity(tcp_transport, tcp_host, tcp_port, timeout=15)
            except TransportError as error:
                return _revert_after_tcp_check_failure(
                    f"Connected, but could not verify the radio's identity over TCP: {error}",
                    "tcp_identity_check_failed",
                )

            accepted_radio = dict(instance_manager.get().get("radio") or {})
            identity_status = compare_radio_identity(accepted_radio, tcp_identity)
            if identity_status == "MISMATCH":
                detected_label = tcp_identity.get("long_name") or tcp_identity.get("node_id") or "the radio"
                accepted_label = accepted_radio.get("long_name") or accepted_radio.get("node_id") or "the accepted radio"
                return _revert_after_tcp_check_failure(
                    f"The radio reachable at {tcp_host}:{tcp_port} ({detected_label}) does not match "
                    f"{accepted_label}. Use Node Manager -> Discover radio to onboard a different radio.",
                    "identity_mismatch",
                )
            # MATCH (a re-connect to the already-accepted radio) or
            # NOT_CHECKED (no accepted identity yet - fresh install, this
            # connect establishes it as first onboarding) both proceed -
            # compare_radio_identity() never returns NOT_FOUND here since
            # fetch_connected_tcp_identity() already raised above on any
            # read failure, and a successful read always has a node_id.

        _persist_choice(target_transport_name, ble_address, ble_name, tcp_host, tcp_port, tcp_identity=tcp_identity)
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

    def _connect_new_for(transport_name, **endpoint_kwargs):
        """Thin wrapper around build_transport_connect_new() binding this
        module's own transport instances/timeouts - the ONE place every
        route below builds a connect_new() callable, so there is exactly
        one implementation of "how to connect to transport X" shared with
        _previous_transport_recovery()'s recovery path and server.py's
        startup transport-restore path (meshsrv/radio_endpoint.py)."""
        return build_transport_connect_new(
            transport_name,
            serial_transport=serial_transport,
            ble_transport=ble_transport,
            tcp_transport=tcp_transport,
            serial_port=serial_port,
            connect_timeout=_SWITCH_CONNECT_TIMEOUT_S,
            disconnect_timeout=_SWITCH_DISCONNECT_TIMEOUT_S,
            **endpoint_kwargs,
        )

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

        connect_new = _connect_new_for("bluetooth", ble_address=address, ble_name=name)
        return _switch(connect_new, "bluetooth", ble_address=address, ble_name=name)

    @app.route("/api/meshtastic/tcp/connect", methods=["POST"])
    @handle_errors
    def api_meshtastic_tcp_connect():
        """Connects to (and switches the active transport to) a
        Meshtastic radio reachable over TCP - the TCP counterpart to
        /bluetooth/connect. Body: {"host": ..., "port": ...} (port
        optional, defaults to the radio's own standard 4403)."""
        data = request.get_json(silent=True) or {}
        host = str(data.get("host") or "").strip()
        port_raw = data.get("port")

        if not host:
            return jsonify({
                "ok": False,
                "error": "TCP host is required",
                "error_code": "tcp_host_required",
            }), 400

        if port_raw in (None, ""):
            port = DEFAULT_TCP_PORT
        else:
            try:
                port = int(port_raw)
            except (TypeError, ValueError):
                return jsonify({
                    "ok": False,
                    "error": "TCP port must be an integer",
                    "error_code": "tcp_port_invalid",
                }), 400
            if not (1 <= port <= 65535):
                return jsonify({
                    "ok": False,
                    "error": "TCP port must be between 1 and 65535",
                    "error_code": "tcp_port_invalid",
                }), 400

        connect_new = _connect_new_for("tcp", tcp_host=host, tcp_port=port)
        return _switch(connect_new, "tcp", tcp_host=host, tcp_port=port)

    @app.route("/api/meshtastic/transport", methods=["POST"])
    @handle_errors
    def api_meshtastic_set_transport():
        """Generic switch, driven by settings.meshtastic for Bluetooth/TCP
        (reconnects to whichever device/endpoint was last used via
        /bluetooth/connect or /tcp/connect) - use those routes directly
        to connect to a newly-scanned device or a new TCP endpoint
        instead."""
        data = request.get_json(silent=True) or {}
        target = str(data.get("type") or "").strip().lower()
        if target not in ("serial", "bluetooth", "tcp"):
            return jsonify({
                "ok": False,
                "error": "type must be 'serial', 'bluetooth', or 'tcp'",
                "error_code": "invalid_transport_type",
            }), 400

        if target == "serial":
            connect_new = _connect_new_for("serial")
            return _switch(connect_new, "serial")

        with state_lock:
            saved = dict(settings.get("meshtastic") or {})

        if target == "tcp":
            host = str(saved.get("tcp_host") or "").strip()
            if not host:
                return jsonify({
                    "ok": False,
                    "error": "No previously-connected TCP endpoint - use Connect first",
                    "error_code": "tcp_host_required",
                }), 400
            port = int(saved.get("tcp_port") or DEFAULT_TCP_PORT)
            connect_new = _connect_new_for("tcp", tcp_host=host, tcp_port=port)
            return _switch(connect_new, "tcp", tcp_host=host, tcp_port=port)

        address = str(saved.get("ble_address") or "").strip()
        name = str(saved.get("ble_name") or "").strip()
        if not address:
            return jsonify({
                "ok": False,
                "error": "No previously-connected Bluetooth device - use Scan + Connect first",
                "error_code": "ble_address_required",
            }), 400

        connect_new = _connect_new_for("bluetooth", ble_address=address, ble_name=name)
        return _switch(connect_new, "bluetooth", ble_address=address, ble_name=name)

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
