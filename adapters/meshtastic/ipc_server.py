"""Task 48 adapter subprocess entrypoint - the GPLv3 side of the process
boundary. Launched by Core (meshsrv/adapter_ipc_client.py) as
`<adapter venv>/bin/python -m adapters.meshtastic.ipc_server`, with
cwd=PROJECT_DIR and PYTHONPATH=PROJECT_DIR so this process's first-party
imports (meshsrv.radio_transport, meshsrv.ipc_protocol,
adapters.meshtastic.*) resolve against the SAME project source tree Core
uses - only the third-party site-packages (meshtastic, bleak) are
isolated to this process's own venv. See the Task 48 investigation
report for why: venv separation isolates dependencies, not the project's
own code, so meshsrv/radio_transport.py's dataclasses are literally the
same Python types on both ends of the boundary, not independently
redefined - a response deserializes back into a real TransportError/
ConnectionInfo instance on Core's side, not a raw dict.

Protocol: newline-delimited JSON on stdin/stdout, one request answered by
exactly one response per line - see docs/BACKEND_API.md's documented
wire shape. Core's TransportRouter already serializes every call through
one lock (Task 47.5), so there is never more than one request in flight;
no request-ID correlation is needed, a strictly synchronous read-dispatch-
write loop is sufficient.

STATELESS ROUTING (deliberate small addition to the documented wire
shape, not a redesign): every request carries a `transport_type`
("serial"/"bluetooth") field alongside `operation`/`params`/`timeout`,
telling this process which of its two transport instances to use for
THIS call. Core's TransportRouter already knows which transport is
active - forwarding that on every request keeps this process
completely stateless about "which one is active", so Core and the
adapter can never disagree about it after a partial failure the way two
independently-tracked "active" flags could.

Wired into server.py: `AdapterSupervisor` spawns this module as
`<adapter venv>/bin/python -m adapters.meshtastic.ipc_server`, and
`AdapterIPCTransport` (both in `meshsrv/adapter_ipc_client.py`) is the
concrete `RadioTransport` implementation `server.py` actually constructs
and routes through `TransportRouter` - not an in-process
SerialTransport/BLETransport construction any more.
"""
from __future__ import annotations

import json
import sys
import threading

from adapters.meshtastic.ble_transport import BLETransport
from adapters.meshtastic.serial_transport import SerialTransport
from meshsrv import ipc_protocol
from meshsrv.radio_transport import (
    ConnectionType,
    TransportError,
    TransportErrorCode,
)

# Three-tier timeout contract (see meshsrv/adapter_ipc_client.py's module
# docstring for the full ordering): Core sends the FULL, un-reduced
# timeout in every request - this process is the one that subtracts a
# margin before using it as ITS OWN internal TimeoutEnforced budget, so
# there's always a window to report a graceful TransportError(TIMEOUT)
# back over the pipe before Core's own read-deadline (set to the full,
# un-reduced value) would fire and kill this process instead. Kept as
# the exact same constant Core's module documents, duplicated rather
# than imported from there, since this process must never depend on
# anything in meshsrv/adapter_ipc_client.py (that module can import
# subprocess/os freely; keeping this process's own import surface
# minimal and one-directional - it imports meshsrv.radio_transport and
# meshsrv.ipc_protocol, never the other way around - is deliberate, not
# an oversight).
_ADAPTER_TIMEOUT_MARGIN_S = 2.0


def _adapter_side_timeout(core_timeout) -> float:
    total = float(core_timeout) if core_timeout is not None else 15.0
    return max(1.0, total - _ADAPTER_TIMEOUT_MARGIN_S)


class _AdapterDispatcher:
    """Owns one SerialTransport and one BLETransport instance for the
    lifetime of this subprocess, and dispatches each incoming request to
    whichever one `transport_type` names. This process only ever
    exercises the connect/disconnect/send_*/get_* half of each class's
    surface - SerialTransport no longer even has a run_listener() method
    to call (stabilization follow-up, P0 #1 of the independent audit:
    that logic moved to meshsrv/serial_port_supervisor.py, used directly
    by Core, never composed here); Stage A keeps the listener in Core
    either way."""

    def __init__(self, *, serial_transport, ble_transport):
        # Takes both transports by DI (matching this project's convention
        # everywhere else) rather than constructing them internally - lets
        # tests exercise the real dispatch/serialization logic against
        # fake stand-ins without needing meshtastic/bleak installed. See
        # main() for the production construction (real SerialTransport/
        # BLETransport, each composing their own local-only
        # SerialPortSupervisor/radio_lock/pause_listen - Task 48
        # investigation report: these no longer coordinate with anything
        # cross-process, Core owns that via claim_exclusive_access() on
        # its OWN SerialPortSupervisor instance before ever sending a
        # request here; they only provide intra-process safety for this
        # instance's own _call_with_timeout watchdog threads now).
        self._serial = serial_transport
        self._ble = ble_transport

    def _target(self, transport_type: str):
        if transport_type == ConnectionType.SERIAL.value:
            return self._serial
        if transport_type == ConnectionType.BLUETOOTH.value:
            return self._ble
        raise TransportError(TransportErrorCode.UNSUPPORTED, f"unknown transport_type: {transport_type!r}")

    def handle(self, request: dict) -> dict:
        operation = request.get("operation")
        transport_type = request.get("transport_type")
        params = request.get("params") or {}
        timeout = request.get("timeout")

        try:
            target = self._target(transport_type)
            result = self._dispatch(target, operation, params, timeout)
        except TransportError as error:
            return ipc_protocol.make_error_response(error)
        except Exception as error:  # noqa: BLE001 - never let a raw traceback cross the protocol boundary
            return ipc_protocol.make_error_response(TransportError(TransportErrorCode.UNKNOWN, str(error)))

        return ipc_protocol.make_ok_response(result)

    def _dispatch(self, target, operation: str, params: dict, core_timeout):
        # Margin applied once, here - every branch below uses this
        # reduced budget for its own target.<method>(timeout=...) call,
        # never the raw value Core sent. See _ADAPTER_TIMEOUT_MARGIN_S.
        timeout = _adapter_side_timeout(core_timeout)

        if operation == "connect":
            info = target.connect(
                ipc_protocol.descriptor_from_dict(params.get("descriptor")),
                force=bool(params.get("force", False)),
                timeout=timeout,
            )
            return ipc_protocol.connection_info_to_dict(info)

        if operation == "disconnect":
            target.disconnect(timeout=timeout)
            return ipc_protocol.connection_info_to_dict(target.get_connection_info())

        if operation == "reconnect":
            info = target.reconnect(timeout=timeout)
            return ipc_protocol.connection_info_to_dict(info)

        if operation == "send_text":
            result = target.send_text(ipc_protocol.outgoing_message_from_dict(params["message"]), timeout=timeout)
            return ipc_protocol.send_result_to_dict(result)

        if operation == "send_packet":
            result = target.send_packet(
                bytes.fromhex(params["payload_hex"]),
                params["destination_id"],
                port_num=int(params["port_num"]),
                want_ack=bool(params.get("want_ack", False)),
                timeout=timeout,
            )
            return ipc_protocol.send_result_to_dict(result)

        if operation == "send_messages":
            messages = [ipc_protocol.outgoing_message_from_dict(m) for m in params["messages"]]
            results = target.send_messages(messages, timeout=timeout)
            return [ipc_protocol.send_result_to_dict(r) for r in results]

        if operation == "send_waypoint":
            result = target.send_waypoint(ipc_protocol.outgoing_waypoint_from_dict(params["waypoint"]), timeout=timeout)
            return ipc_protocol.waypoint_result_to_dict(result)

        if operation == "get_nodes":
            nodes = target.get_nodes(timeout=timeout)
            return [ipc_protocol.node_info_to_dict(n) for n in nodes]

        if operation == "get_local_node":
            return ipc_protocol.node_info_to_dict(target.get_local_node(timeout=timeout))

        if operation == "get_channels":
            channels = target.get_channels(timeout=timeout)
            return [ipc_protocol.channel_info_to_dict(c) for c in channels]

        if operation == "get_metadata":
            return target.get_metadata(timeout=timeout)

        if operation == "set_device_time":
            ok = target.set_device_time(int(params["epoch_seconds"]), timeout=timeout)
            return {"ok": bool(ok)}

        if operation == "close":
            target.close()
            return None

        if operation == "scan":
            # BLE-only, not part of the RadioTransport ABC (see
            # BLETransport.scan()'s own docstring) - calling this against
            # the serial target would raise AttributeError, caught by
            # handle()'s generic except and reported as a structured
            # UNKNOWN error, which is the right outcome for a request
            # that should never be sent with transport_type=serial in
            # the first place, not a case worth special-casing here.
            return target.scan(timeout=timeout)

        raise TransportError(TransportErrorCode.UNSUPPORTED, f"unknown operation: {operation!r}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--serial-port", required=True)
    parser.add_argument("--meshtastic-cli", required=True)
    args = parser.parse_args()

    dispatcher = _AdapterDispatcher(
        serial_transport=SerialTransport(
            cli_path=args.meshtastic_cli,
            port=args.serial_port,
            radio_lock=threading.RLock(),
            pause_listen=threading.Event(),
        ),
        ble_transport=BLETransport(address=""),
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            response = ipc_protocol.make_error_response(
                TransportError(TransportErrorCode.UNKNOWN, f"malformed request JSON: {error}")
            )
        else:
            response = dispatcher.handle(request)

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
