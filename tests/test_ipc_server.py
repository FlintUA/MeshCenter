"""Tests for adapters/meshtastic/ipc_server.py's _AdapterDispatcher -
operation routing, request/response (de)serialization via
meshsrv/ipc_protocol.py, and error handling (TransportError passes
through as a structured error response, any other exception is wrapped,
never a raw traceback crosses the protocol boundary). Uses fake
stand-in transport objects (not real SerialTransport/BLETransport, and
not a real subprocess) - see tests/test_adapter_ipc_client.py for the
subprocess-level mechanics (spawn/kill/respawn), and
tests/test_ble_transport.py / tests/test_serial_transport_timeout.py for
the real transport classes' own behavior. This file is specifically
about whether _AdapterDispatcher routes and (de)serializes correctly,
independent of either.
"""
import io
import json

import pytest

from adapters.meshtastic.ipc_server import _AdapterDispatcher, _adapter_side_timeout, serve_forever
from meshsrv.radio_transport import (
    ChannelInfo,
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    NodeInfo,
    SendResult,
    TransportError,
    TransportErrorCode,
)


class _FakeTarget:
    """Records every call it receives (operation + kwargs) so tests can
    assert both the return value AND that the right timeout/params
    reached the real method call - not just that *some* response came
    back looking plausible."""

    def __init__(self):
        self.calls = []

    def connect(self, descriptor, *, force, timeout):
        self.calls.append(("connect", descriptor, force, timeout))
        return ConnectionInfo(
            state=ConnectionState.CONNECTED,
            descriptor=descriptor,
            node_id="!756f9960",
            connected_since=1787600000.0,
        )

    def get_connection_info(self):
        return ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=None, node_id="!756f9960")

    def send_text(self, message, *, timeout):
        self.calls.append(("send_text", message, timeout))
        return SendResult(accepted=True, packet_id=42)

    def get_nodes(self, *, timeout):
        self.calls.append(("get_nodes", timeout))
        return [NodeInfo(node_id="!aaaaaaaa", num=1, user=None), NodeInfo(node_id="!bbbbbbbb", num=2, user=None)]

    def get_channels(self, *, timeout):
        self.calls.append(("get_channels", timeout))
        return [ChannelInfo(index=0, name="LongFast", role="PRIMARY")]

    def scan(self, *, timeout):
        self.calls.append(("scan", timeout))
        return [{"name": "FLT2_9960", "address": "3C:DC:75:6F:99:61"}]


class _FakeTargetWithoutScan:
    """Stands in for SerialTransport, which genuinely has no scan()
    method - proves the "wrong transport_type" case surfaces as a
    structured UNKNOWN error, not a crash, without special-casing it in
    _dispatch() itself (see that branch's own comment)."""


class _RaisesTransportError:
    def get_metadata(self, *, timeout):
        raise TransportError(TransportErrorCode.DEVICE_NOT_FOUND, "no such device")


class _RaisesGenericException:
    def get_metadata(self, *, timeout):
        raise ValueError("something unrelated to TransportError broke")


def _dispatcher(serial=None, ble=None):
    return _AdapterDispatcher(serial_transport=serial or _FakeTarget(), ble_transport=ble or _FakeTarget())


def test_connect_routes_to_serial_and_serializes_the_response():
    serial = _FakeTarget()
    dispatcher = _dispatcher(serial=serial)

    response = dispatcher.handle({
        "operation": "connect",
        "transport_type": "serial",
        "params": {"descriptor": {"type": "serial", "address": "/dev/ttyACM0", "label": ""}, "force": True},
        "timeout": 30.0,
    })

    assert response["ok"] is True
    assert response["result"]["state"] == "connected"
    assert response["result"]["node_id"] == "!756f9960"

    op, descriptor, force, timeout = serial.calls[0]
    assert op == "connect"
    assert descriptor.address == "/dev/ttyACM0"
    assert force is True
    # Margin applied: 30.0 - _ADAPTER_TIMEOUT_MARGIN_S, not the raw 30.0.
    assert timeout == _adapter_side_timeout(30.0)
    assert timeout < 30.0


def test_connect_routes_to_ble_not_serial():
    serial = _FakeTarget()
    ble = _FakeTarget()
    dispatcher = _dispatcher(serial=serial, ble=ble)

    dispatcher.handle({
        "operation": "connect",
        "transport_type": "bluetooth",
        "params": {"descriptor": {"type": "bluetooth", "address": "3C:DC:75:6F:99:61", "label": ""}, "force": False},
        "timeout": 90.0,
    })

    assert len(ble.calls) == 1
    assert len(serial.calls) == 0


def test_send_text_round_trips_through_the_real_serializers():
    serial = _FakeTarget()
    dispatcher = _dispatcher(serial=serial)

    response = dispatcher.handle({
        "operation": "send_text",
        "transport_type": "serial",
        "params": {"message": {"text": "hi", "destination_id": "^all", "channel_index": 0, "want_ack": False, "reply_id": None}},
        "timeout": 15.0,
    })

    assert response["ok"] is True
    assert response["result"]["accepted"] is True
    assert response["result"]["packet_id"] == 42

    op, message, timeout = serial.calls[0]
    assert message.text == "hi"
    assert message.destination_id == "^all"


def test_get_nodes_returns_a_serialized_list():
    dispatcher = _dispatcher()

    response = dispatcher.handle({"operation": "get_nodes", "transport_type": "serial", "params": {}, "timeout": 15.0})

    assert response["ok"] is True
    assert [n["node_id"] for n in response["result"]] == ["!aaaaaaaa", "!bbbbbbbb"]


def test_get_channels_returns_a_serialized_list():
    dispatcher = _dispatcher()

    response = dispatcher.handle({"operation": "get_channels", "transport_type": "serial", "params": {}, "timeout": 15.0})

    assert response["ok"] is True
    assert response["result"] == [{"index": 0, "name": "LongFast", "role": "PRIMARY"}]


def test_scan_routes_to_ble_and_returns_the_device_list():
    ble = _FakeTarget()
    dispatcher = _dispatcher(ble=ble)

    response = dispatcher.handle({"operation": "scan", "transport_type": "bluetooth", "params": {}, "timeout": 20.0})

    assert response["ok"] is True
    assert response["result"] == [{"name": "FLT2_9960", "address": "3C:DC:75:6F:99:61"}]

    op, timeout = ble.calls[0]
    assert op == "scan"
    # Margin applied here too - _dispatch() reduces the timeout once,
    # before branching on operation, so scan is not a special case.
    assert timeout == _adapter_side_timeout(20.0)


def test_scan_against_a_target_without_scan_is_a_structured_unknown_error_not_a_crash():
    """The real-world version of this: transport_type=serial (SerialTransport
    genuinely has no scan() method) reaching the scan branch - proves the
    deliberate lack of a special case in _dispatch() (see that branch's
    own comment) degrades to a clean error response, not an unhandled
    AttributeError escaping to the protocol boundary."""
    dispatcher = _dispatcher(serial=_FakeTargetWithoutScan())

    response = dispatcher.handle({"operation": "scan", "transport_type": "serial", "params": {}, "timeout": 15.0})

    assert response["ok"] is False
    assert response["error"]["code"] == "unknown"


def test_transport_error_becomes_a_structured_error_response():
    dispatcher = _dispatcher(serial=_RaisesTransportError())

    response = dispatcher.handle({"operation": "get_metadata", "transport_type": "serial", "params": {}, "timeout": 15.0})

    assert response["ok"] is False
    assert response["error"]["code"] == "device_not_found"
    assert response["error"]["message"] == "no such device"


def test_generic_exception_is_wrapped_never_a_raw_traceback():
    dispatcher = _dispatcher(serial=_RaisesGenericException())

    response = dispatcher.handle({"operation": "get_metadata", "transport_type": "serial", "params": {}, "timeout": 15.0})

    assert response["ok"] is False
    assert response["error"]["code"] == "unknown"
    assert "something unrelated to TransportError broke" in response["error"]["message"]
    assert "Traceback" not in str(response)


def test_unknown_operation_is_a_structured_unsupported_error():
    dispatcher = _dispatcher()

    response = dispatcher.handle({"operation": "not_a_real_operation", "transport_type": "serial", "params": {}, "timeout": 15.0})

    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported"


def test_unknown_transport_type_is_a_structured_unsupported_error():
    dispatcher = _dispatcher()

    response = dispatcher.handle({"operation": "get_metadata", "transport_type": "carrier_pigeon", "params": {}, "timeout": 15.0})

    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported"


def test_adapter_side_timeout_never_goes_below_the_one_second_floor():
    assert _adapter_side_timeout(1.0) == 1.0
    assert _adapter_side_timeout(0.1) == 1.0
    assert _adapter_side_timeout(None) == 13.0  # default 15.0 - margin 2.0


# --- P0 stabilization follow-up: serve_forever()'s stdout protocol isolation ---
# (Droidian-caught corruption cascade - a stray print() during request
# handling landed on stdout, which IS the JSON-RPC channel back to Core in
# this subprocess. serve_forever() wraps dispatcher.handle() in
# contextlib.redirect_stdout(sys.stderr) so any such stray output - here
# simulated from inside a fake dispatcher, standing in for either a not-yet-
# fixed first-party call site or an unaudited third-party one (the
# meshtastic library itself) - never reaches the real protocol channel.)

class _NoisyDispatcher:
    """Stands in for a dispatcher whose handle() does something noisy
    internally (a stray print()) before returning a normal response -
    simulating the exact mechanism of the Droidian-caught bug without
    depending on the real SerialPortSupervisor/lsof machinery this
    module's own regression test (tests/test_serial_port_supervisor.py)
    already covers directly."""

    def __init__(self, noisy_on_request_numbers=frozenset({1})):
        self._noisy_on = noisy_on_request_numbers
        self.n = 0

    def handle(self, request):
        self.n += 1
        if self.n in self._noisy_on:
            print(f"stray third-party output during request {self.n}")
        return {"protocol_version": 1, "ok": True, "result": {"n": self.n}}


def test_serve_forever_stray_print_never_reaches_protocol_stdout(capsys):
    stdin = io.StringIO('{"operation": "noop"}\n')
    protocol_out = io.StringIO()

    serve_forever(_NoisyDispatcher(), stdin=stdin, stdout=protocol_out)

    captured = capsys.readouterr()
    assert captured.out == "", f"a stray print() during handle() must never reach real stdout, got: {captured.out!r}"
    assert "stray third-party output" in captured.err

    # The actual protocol response still went through cleanly on the
    # separately-tracked protocol_stdout, unaffected by the redirect.
    response = json.loads(protocol_out.getvalue().strip())
    assert response["ok"] is True


def test_adapter_stdout_contains_only_json_across_multiple_requests(capsys):
    """Broader smoke test: several requests, one of them noisy partway
    through - every line written to the protocol channel must be valid,
    parseable JSON, none of them containing the stray output."""
    stdin = io.StringIO("{}\n{}\n{}\n")
    protocol_out = io.StringIO()

    serve_forever(_NoisyDispatcher(noisy_on_request_numbers={2}), stdin=stdin, stdout=protocol_out)

    lines = protocol_out.getvalue().splitlines()
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)  # raises if anything but clean JSON slipped in
        assert parsed["ok"] is True

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stray third-party output during request 2" in captured.err


def test_serve_forever_still_reports_malformed_request_json_normally():
    """Sanity check the redirect doesn't interfere with the pre-existing
    malformed-REQUEST handling (a bad line from Core, not a stray print
    from the adapter side) - that path never calls dispatcher.handle()
    at all, so it's outside the redirect_stdout block entirely."""
    stdin = io.StringIO("not valid json at all\n")
    protocol_out = io.StringIO()

    serve_forever(_NoisyDispatcher(), stdin=stdin, stdout=protocol_out)

    response = json.loads(protocol_out.getvalue().strip())
    assert response["ok"] is False
    assert response["error"]["code"] == "unknown"
    assert "malformed request JSON" in response["error"]["message"]
