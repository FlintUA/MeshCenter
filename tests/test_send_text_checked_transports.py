"""Tests for the real transports' `send_text_checked()` - the atomic
resolve-channel-and-send operation the MCAttach control-channel correction
introduced (see meshsrv/radio_transport.py's `CheckedSendResult` and
adapters/meshtastic/{serial,ble}_transport.py).

Where tests/test_mca_control_channel.py proves the *adapter*
(MeshtasticTextAdapter) makes exactly one `send_text_checked()` call per
send, this file proves the *transports* themselves honor the fail-closed
single-session contract on real-ish objects: one exclusive serial session
(one open / one close / one waitForConfig) for SerialTransport, and one
already-open persistent interface for BLETransport - with the requested
channel resolved against the live radio and UNSUPPORTED raised (never a
silent channel-0 fallback) when it is absent or DISABLED.

The meshtastic library is never imported here: `_open_interface()` is
stubbed for serial, and BLE is mocked at the `meshtastic.ble_interface`
module boundary - the same division test_serial_transport_timeout.py and
test_ble_transport.py draw. Role is read as the raw int (0=DISABLED,
1=PRIMARY, 2=SECONDARY) off the fake channel objects, exactly as the real
transports read it off the meshtastic library's `mesh_pb2.Channel.Role`.
"""
from __future__ import annotations

import contextlib
import sys
import threading
import types

import pytest

from adapters.meshtastic import serial_transport as serial_transport_module
from adapters.meshtastic.ble_transport import BLETransport
from adapters.meshtastic.serial_transport import SerialTransport
from meshsrv.radio_transport import (
    CheckedSendResult,
    ConnectionDescriptor,
    ConnectionState,
    ConnectionType,
    OutgoingMessage,
    SendResult,
    TransportError,
    TransportErrorCode,
)


# --------------------------------------------------------------------------
# Shared fakes: a raw channel object (int role) and a fake interface that
# records every lifecycle call the transports make.
# --------------------------------------------------------------------------

class _FakeChannel:
    """A raw meshtastic-library channel object as the transports read it off
    `interface.localNode.channels`: int `index`, int `role`
    (mesh_pb2.Channel.Role: 0=DISABLED, 1=PRIMARY, 2=SECONDARY), and
    `settings.name`. Role is the raw int, NOT the normalized ChannelInfo
    string - the transports skip `role == 0` directly."""

    def __init__(self, index, name, role):
        self.index = index
        self.role = role
        self.settings = types.SimpleNamespace(name=name)


PUBLIC_CHANNEL = _FakeChannel(0, "LongFast", 1)  # PRIMARY


class _FakeSerialInterface:
    """Stands in for a meshtastic SerialInterface. Records waitForConfig()/
    sendText()/close() so the serial transport's single-session lifecycle
    can be asserted. `fail_send` (when set) makes sendText raise."""

    def __init__(self, channels, *, fail_send=None):
        self.localNode = types.SimpleNamespace(channels=list(channels))
        self.sent = []
        self.closed = False
        self.config_waits = 0
        self.fail_send = fail_send

    def waitForConfig(self):
        self.config_waits += 1

    def sendText(self, **kwargs):
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(kwargs)
        return types.SimpleNamespace(id=42)

    def close(self):
        self.closed = True


class _FakeSupervisor:
    """Counts exclusive-access claims. `claim_exclusive_access` is the only
    supervisor method SerialTransport.send_text_checked() reaches."""

    def __init__(self):
        self.claims = 0

    @contextlib.contextmanager
    def claim_exclusive_access(self, *, timeout, cooldown):
        self.claims += 1
        yield


def _make_serial_transport(interface_factory, supervisor=None):
    supervisor = supervisor or _FakeSupervisor()
    transport = SerialTransport(
        cli_path="/does/not/matter/for/this/test",
        port="/dev/ttyFAKE",
        radio_lock=threading.RLock(),
        pause_listen=threading.Event(),
        supervisor=supervisor,
    )
    transport._open_interface = interface_factory
    return transport, supervisor


@pytest.fixture
def _no_settle_sleep(monkeypatch):
    """The serial transport's _settle() sleeps 1.5s after waitForConfig().
    Patch time.sleep away so the tests don't each pay that cost - _settle()
    still runs its real waitForConfig() call, which is what we count."""
    monkeypatch.setattr(serial_transport_module.time, "sleep", lambda *a, **k: None)


# --------------------------------------------------------------------------
# SerialTransport.send_text_checked()
# --------------------------------------------------------------------------

class TestSerialSendTextChecked:
    def test_success_is_one_exclusive_session(self, _no_settle_sleep):
        channels = [PUBLIC_CHANNEL, _FakeChannel(1, "Flint-pvt", 2)]
        opened = []
        interface = _FakeSerialInterface(channels)

        def factory():
            opened.append(interface)
            return interface

        transport, supervisor = _make_serial_transport(factory)

        result = transport.send_text_checked(
            OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=1),
            timeout=5.0,
        )

        assert isinstance(result, CheckedSendResult)
        assert result.channel_name == "Flint-pvt"
        assert result.result.accepted is True
        assert result.result.packet_id == 42
        # One exclusive serial session: one claim, one open, one settle, one
        # send, one close - never a separate get_channels() round-trip.
        assert supervisor.claims == 1
        assert len(opened) == 1
        assert interface.config_waits == 1
        assert interface.closed is True
        assert [s["channelIndex"] for s in interface.sent] == [1]

    def test_missing_channel_raises_unsupported_and_still_closes(self, _no_settle_sleep):
        opened = []
        interface = _FakeSerialInterface([PUBLIC_CHANNEL])

        def factory():
            opened.append(interface)
            return interface

        transport, supervisor = _make_serial_transport(factory)

        with pytest.raises(TransportError) as exc_info:
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=1),
                timeout=5.0,
            )

        assert exc_info.value.code == TransportErrorCode.UNSUPPORTED
        # Fail-closed, but still a clean session: the interface was opened and
        # closed even though the requested channel was absent.
        assert supervisor.claims == 1
        assert len(opened) == 1
        assert interface.closed is True
        assert interface.sent == []  # nothing transmitted, certainly not on 0

    def test_disabled_channel_raises_unsupported_not_channel_0(self, _no_settle_sleep):
        opened = []
        interface = _FakeSerialInterface(
            [PUBLIC_CHANNEL, _FakeChannel(1, "Flint-pvt", 0)]  # DISABLED
        )

        def factory():
            opened.append(interface)
            return interface

        transport, _supervisor = _make_serial_transport(factory)

        with pytest.raises(TransportError) as exc_info:
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=1),
                timeout=5.0,
            )

        assert exc_info.value.code == TransportErrorCode.UNSUPPORTED
        assert interface.sent == []  # never a silent fallback to channel 0

    def test_send_failure_propagates_and_closes(self, _no_settle_sleep):
        opened = []
        failure = TransportError(TransportErrorCode.UNKNOWN, "radio busy")
        interface = _FakeSerialInterface([PUBLIC_CHANNEL], fail_send=failure)

        def factory():
            opened.append(interface)
            return interface

        transport, _supervisor = _make_serial_transport(factory)

        with pytest.raises(TransportError) as exc_info:
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
                timeout=5.0,
            )

        assert exc_info.value is failure
        assert interface.closed is True

    def test_retry_after_failure_opens_a_fresh_session(self, _no_settle_sleep):
        # First interface's sendText raises; the retry must open a brand-new
        # interface and succeed - each call is a fresh short-lived session, so
        # a failed send never leaves the transport stuck.
        first = _FakeSerialInterface(
            [PUBLIC_CHANNEL], fail_send=TransportError(TransportErrorCode.UNKNOWN, "radio busy")
        )
        second = _FakeSerialInterface([PUBLIC_CHANNEL])
        interfaces = [first, second]
        opened = []

        def factory():
            interface = interfaces.pop(0)
            opened.append(interface)
            return interface

        transport, supervisor = _make_serial_transport(factory)

        with pytest.raises(TransportError):
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
                timeout=5.0,
            )

        result = transport.send_text_checked(
            OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
            timeout=5.0,
        )

        assert result.result.accepted is True
        assert len(opened) == 2  # two separate sessions
        assert first.closed is True
        assert second.closed is True
        assert supervisor.claims == 2

    def test_open_failure_wraps_to_transport_error_without_send(self, _no_settle_sleep):
        def factory():
            raise RuntimeError("device disappeared")

        transport, _supervisor = _make_serial_transport(factory)

        with pytest.raises(TransportError) as exc_info:
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
                timeout=5.0,
            )

        # _call_with_timeout wraps a non-TransportError into UNKNOWN.
        assert exc_info.value.code == TransportErrorCode.UNKNOWN


# --------------------------------------------------------------------------
# BLETransport.send_text_checked()
# --------------------------------------------------------------------------

class _FakeMyInfo:
    def __init__(self, my_node_num):
        self.my_node_num = my_node_num


class _FakeBLEInterface:
    instances = []

    def __init__(self, address, timeout=300, *, channels=None):
        self.address = address
        self.timeout = timeout
        self.myInfo = _FakeMyInfo(my_node_num=0x756F9960)
        self.nodes = {}
        self.localNode = types.SimpleNamespace(
            nodeNum=0x756F9960, channels=list(channels or [PUBLIC_CHANNEL])
        )
        self.metadata = None
        self.closed = False
        self.sent = []
        self.fail_send = None
        _FakeBLEInterface.instances.append(self)

    def sendText(self, **kwargs):
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(kwargs)
        return types.SimpleNamespace(id=42)

    def sendData(self, *args, **kwargs):
        return types.SimpleNamespace(id=42)

    def close(self):
        self.closed = True


@pytest.fixture
def _fake_ble_module(monkeypatch):
    _FakeBLEInterface.instances = []
    fake_module = types.ModuleType("meshtastic.ble_interface")
    fake_module.BLEInterface = _FakeBLEInterface
    monkeypatch.setitem(sys.modules, "meshtastic.ble_interface", fake_module)
    yield
    monkeypatch.delitem(sys.modules, "meshtastic.ble_interface", raising=False)


def _ble_descriptor(address="3C:DC:75:6F:99:61"):
    return ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address=address)


class TestBLESendTextChecked:
    def test_reuses_persistent_interface_no_new_connection(self, _fake_ble_module):
        transport = BLETransport(address="3C:DC:75:6F:99:61")
        transport.connect(_ble_descriptor(), timeout=5)

        result = transport.send_text_checked(
            OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
            timeout=5.0,
        )

        assert isinstance(result, CheckedSendResult)
        assert result.channel_name == "LongFast"
        assert result.result.accepted is True
        # Ownership model: connect() opened one persistent interface; the send
        # reused it rather than opening a second.
        assert len(_FakeBLEInterface.instances) == 1
        assert _FakeBLEInterface.instances[0].closed is False

    def test_acquires_lock_once_and_sends_once(self, _fake_ble_module):
        transport = BLETransport(address="3C:DC:75:6F:99:61")
        transport.connect(_ble_descriptor(), timeout=5)
        interface = _FakeBLEInterface.instances[0]

        transport.send_text_checked(
            OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
            timeout=5.0,
        )

        # One send_text_checked() -> exactly one sendText() on the persistent
        # interface (a single hold of self._lock, no redundant re-send).
        assert len(interface.sent) == 1
        assert interface.sent[0]["channelIndex"] == 0
        assert interface.sent[0]["destinationId"] == "!bbbbbbbb"

    def test_missing_channel_blocks(self, _fake_ble_module):
        transport = BLETransport(address="3C:DC:75:6F:99:61")
        transport.connect(_ble_descriptor(), timeout=5)
        interface = _FakeBLEInterface.instances[0]

        with pytest.raises(TransportError) as exc_info:
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=1),
                timeout=5.0,
            )

        assert exc_info.value.code == TransportErrorCode.UNSUPPORTED
        assert interface.sent == []

    def test_disabled_channel_blocks(self, _fake_ble_module):
        _FakeBLEInterface.instances = []
        disabled = _FakeBLEInterface(
            "3C:DC:75:6F:99:61", channels=[PUBLIC_CHANNEL, _FakeChannel(1, "Flint-pvt", 0)]
        )
        transport = BLETransport(address="3C:DC:75:6F:99:61")
        transport.connect(_ble_descriptor(), timeout=5)

        with pytest.raises(TransportError) as exc_info:
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=1),
                timeout=5.0,
            )

        assert exc_info.value.code == TransportErrorCode.UNSUPPORTED
        assert disabled.sent == []  # never a silent fallback to channel 0

    def test_send_failure_releases_the_lock(self, _fake_ble_module):
        transport = BLETransport(address="3C:DC:75:6F:99:61")
        transport.connect(_ble_descriptor(), timeout=5)
        interface = _FakeBLEInterface.instances[0]

        interface.fail_send = TransportError(TransportErrorCode.UNKNOWN, "gatt gone")
        with pytest.raises(TransportError):
            transport.send_text_checked(
                OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
                timeout=5.0,
            )

        # The lock must have been released (not left held by the failed body) -
        # a subsequent send on the same persistent interface succeeds.
        interface.fail_send = None
        result = transport.send_text_checked(
            OutgoingMessage(text="MCA1:...", destination_id="!bbbbbbbb", channel_index=0),
            timeout=5.0,
        )

        assert result.result.accepted is True
        assert len(interface.sent) == 1  # only the successful send was recorded
