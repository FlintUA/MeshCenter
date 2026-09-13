"""tests/test_mca_control_channel.py

Regression tests for the configurable MCA DIRECT control-channel index
(correction: MCA key/transfer control messages must not transmit on the
public LongFast [0] channel).

The adapter under test (`MeshtasticTextAdapter`) is real; only the radio is
faked (`FakeRadioTransport`), the same division the delivery-contract tests
draw. Because `MeshtasticTextAdapter.send()` is the single place an
`OutgoingMessage` is constructed for MCA traffic (verified: no other
`OutgoingMessage(` site exists in meshsrv/attachments/), proving that `send()`
applies the configured channel to every message type covers every outbound MCA
control message - KEY_REQUEST, KEY_ANNOUNCE, OFFER, ACK, CANCEL, REJECTED,
EXPIRED - which all funnel through `AttachmentsService._delivery_adapter.send()`.

The tests prove four things:
  1. every outbound MCA control message uses the configured channel index
     (never the default 0), while the destination stays the selected node;
  2. an invalid or unavailable configured channel blocks transmission with a
     safe `ConnectorUnavailableError` (mapped to `radio_unavailable`) rather
     than silently falling back to channel 0;
  3. `supports_channel` stays `False` and ordinary chat's channel selection
     is untouched;
  4. inbound MCA is accepted on whatever channel it arrives on (the channel
     is a send-time selection, not a receive-time trust gate) and the actual
     received channel index is retained in `transport_metadata`.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest
from nacl.signing import SigningKey

from meshsrv.attachments import codec
from meshsrv.attachments.delivery.base import (
    ConnectorUnavailableError,
    Route,
    RouteType,
)
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter
from meshsrv.radio_transport import ChannelInfo, OutgoingMessage, TransportError, TransportErrorCode


# A realistic radio with a public primary channel (0) and a private secondary
# control channel (1) - the shape the correction is meant to run against.
PRIVATE_CHANNELS = [
    ChannelInfo(index=0, name="LongFast", role="PRIMARY"),
    ChannelInfo(index=1, name="Flint-pvt", role="SECONDARY"),
]

_SIGNING_KEY = SigningKey.generate()


def _wire_payload(message_type: codec.MessageType, signing_key: SigningKey) -> bytes:
    """Encode one real MCA message of the given type as an ASCII MCA1-TEXT
    wire payload, exactly what `MeshtasticTextAdapter.send()` receives from
    the service. Each type is genuinely encoded (not a placeholder) so the
    channel-selection assertions are made against realistic control traffic."""
    if message_type is codec.MessageType.OFFER:
        logical = codec.encode_offer(
            codec.OfferFields(
                provider_id=b"\xff" * 8,
                transfer_id=b"\xff" * 16,
                sender_key_id=b"\xff" * 8,
                kind=4,
                size_bucket=5,
                hard_expires_at=2_145_916_800,
                flags=3,
            ),
            signing_key,
        )
    elif message_type is codec.MessageType.KEY_REQUEST:
        logical = codec.encode_key_request(
            codec.KeyRequestFields(sender_key_id=b"\x01" * 8), signing_key
        )
    elif message_type is codec.MessageType.KEY_ANNOUNCE:
        logical = codec.encode_key_announce(
            codec.KeyAnnounceFields(public_identity=signing_key.verify_key.encode(), epoch=1),
            signing_key,
        )
    else:  # simple-ack family: ACK_*, CANCEL, REJECTED, EXPIRED
        logical = codec.encode_simple_ack(message_type, transfer_id=b"\x02" * 16, signing_key=signing_key)
    return codec.to_text(logical).encode("ascii")


def _recorded_channel(message_type, *, control_channel_index=1, channels=PRIVATE_CHANNELS) -> int:
    """Send one message through a real adapter over a fake radio and return the
    channel index the adapter actually selected on the `OutgoingMessage`."""
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa", channels=channels)
    adapter = MeshtasticTextAdapter(transport, control_channel_index=control_channel_index)
    route = Route(route_type=RouteType.DIRECT, route_id="!bbbbbbbb", destination_address="!bbbbbbbb")
    adapter.send(_wire_payload(message_type, _SIGNING_KEY), route, idempotency_key="idem-1")
    return transport._sent_messages[-1].channel_index  # noqa: SLF001


# --------------------------------------------------------------------------
# Test 1: MCA DIRECT send uses the configured channel index.
# --------------------------------------------------------------------------

def test_mca_direct_send_uses_configured_channel_index():
    assert _recorded_channel(codec.MessageType.KEY_REQUEST, control_channel_index=1) == 1


# --------------------------------------------------------------------------
# Test 2: no MCA send falls back to channel 0.
# --------------------------------------------------------------------------

def test_no_mca_send_falls_back_to_channel_0():
    """Configured for channel 1 but the radio only has channel 0. The send
    must fail closed, not silently fall back onto the public channel."""
    ether = InMemoryEther()
    transport = FakeRadioTransport(
        ether, "!aaaaaaaa", channels=[ChannelInfo(index=0, name="LongFast", role="PRIMARY")]
    )
    adapter = MeshtasticTextAdapter(transport, control_channel_index=1)
    route = Route(route_type=RouteType.DIRECT, route_id="!bbbbbbbb", destination_address="!bbbbbbbb")
    with pytest.raises(ConnectorUnavailableError):
        adapter.send(_wire_payload(codec.MessageType.KEY_REQUEST, _SIGNING_KEY), route, idempotency_key="idem-1")
    # Nothing was transmitted at all - so certainly nothing on channel 0.
    assert transport._sent_messages == []  # noqa: SLF001


# --------------------------------------------------------------------------
# Tests 3-5: each control message type uses the configured channel.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message_type",
    [codec.MessageType.KEY_REQUEST, codec.MessageType.KEY_ANNOUNCE],
)
def test_key_request_and_key_announce_use_control_channel(message_type):
    assert _recorded_channel(message_type) == 1


@pytest.mark.parametrize(
    "message_type",
    [codec.MessageType.OFFER, codec.MessageType.ACK_RECEIVED, codec.MessageType.ACK_DOWNLOADED],
)
def test_offer_and_ack_use_control_channel(message_type):
    assert _recorded_channel(message_type) == 1


@pytest.mark.parametrize(
    "message_type",
    [codec.MessageType.CANCEL, codec.MessageType.REJECTED, codec.MessageType.EXPIRED],
)
def test_cancel_rejected_expired_use_control_channel(message_type):
    assert _recorded_channel(message_type) == 1


# --------------------------------------------------------------------------
# Test 6: the destination remains the selected node, not the channel.
# --------------------------------------------------------------------------

def test_destination_remains_the_selected_node_not_the_channel():
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa", channels=PRIVATE_CHANNELS)
    adapter = MeshtasticTextAdapter(transport, control_channel_index=1)
    route = Route(route_type=RouteType.DIRECT, route_id="!bbbbbbbb", destination_address="!bbbbbbbb")
    adapter.send(_wire_payload(codec.MessageType.OFFER, _SIGNING_KEY), route, idempotency_key="idem-1")

    sent = transport._sent_messages[-1]  # noqa: SLF001
    assert sent.destination_id == "!bbbbbbbb"  # the node, unchanged
    assert sent.destination_id.startswith("!")
    assert sent.destination_id != "^all"  # never a channel broadcast
    assert sent.channel_index == 1  # the channel is a separate, independent selection


# --------------------------------------------------------------------------
# Test 7: supports_channel remains False.
# --------------------------------------------------------------------------

def test_supports_channel_remains_false():
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa", channels=PRIVATE_CHANNELS)
    adapter = MeshtasticTextAdapter(transport, control_channel_index=1)
    caps = adapter.capabilities()
    assert caps.supports_channel is False
    assert caps.supports_direct is True


# --------------------------------------------------------------------------
# Test 8: invalid/unavailable configured channel blocks transmission safely.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "control_channel_index,channels,expected",
    [
        (8, PRIVATE_CHANNELS, "invalid"),          # out of range (high)
        (-1, PRIVATE_CHANNELS, "invalid"),         # out of range (low)
        ("x", PRIVATE_CHANNELS, "invalid"),        # wrong type
        (True, PRIVATE_CHANNELS, "invalid"),       # bool is not a channel index
        (5, PRIVATE_CHANNELS, "not available"),    # in range but not on the radio
    ],
)
def test_invalid_or_unavailable_control_channel_blocks_transmission(control_channel_index, channels, expected):
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa", channels=channels)
    adapter = MeshtasticTextAdapter(transport, control_channel_index=control_channel_index)
    route = Route(route_type=RouteType.DIRECT, route_id="!bbbbbbbb", destination_address="!bbbbbbbb")
    with pytest.raises(ConnectorUnavailableError) as exc_info:
        adapter.send(_wire_payload(codec.MessageType.KEY_REQUEST, _SIGNING_KEY), route, idempotency_key="idem-1")
    # Safe, user-visible message: a fixed allowlisted token, no secret in it.
    assert expected in str(exc_info.value)
    assert transport._sent_messages == []  # noqa: SLF001


def test_channel_list_read_failure_blocks_transmission():
    class _FailingTransport(FakeRadioTransport):
        def get_channels(self, *, timeout: float = 15.0):
            raise TransportError(TransportErrorCode.ADAPTER_UNAVAILABLE, "channel list unavailable")

    ether = InMemoryEther()
    transport = _FailingTransport(ether, "!aaaaaaaa")
    adapter = MeshtasticTextAdapter(transport, control_channel_index=1)
    route = Route(route_type=RouteType.DIRECT, route_id="!bbbbbbbb", destination_address="!bbbbbbbb")
    with pytest.raises(ConnectorUnavailableError) as exc_info:
        adapter.send(_wire_payload(codec.MessageType.KEY_REQUEST, _SIGNING_KEY), route, idempotency_key="idem-1")
    assert "cannot verify" in str(exc_info.value)
    assert transport._sent_messages == []  # noqa: SLF001


# --------------------------------------------------------------------------
# Test 9: ordinary chat traffic keeps its independent channel selection.
# --------------------------------------------------------------------------

def test_ordinary_chat_traffic_keeps_independent_channel_selection():
    # Ordinary chat never goes through MeshtasticTextAdapter - it sends an
    # OutgoingMessage with its own channel selection directly. Prove the MCA
    # control-channel change left that shared path untouched.
    assert OutgoingMessage(text="hi", destination_id="!bbbbbbbb").channel_index == 0

    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa")
    chat_msg = OutgoingMessage(text="hi", destination_id="!bbbbbbbb", channel_index=2)
    transport.send_text(chat_msg)
    assert transport._sent_messages[-1].channel_index == 2  # noqa: SLF001


# --------------------------------------------------------------------------
# Test 10: no channel PSK or upload token in logs or API responses.
# --------------------------------------------------------------------------

def test_no_channel_psk_or_upload_token_in_logs_or_receipts(caplog):
    # Structural guarantee first: ChannelInfo carries index/name/role only -
    # a channel PSK cannot ride on the model the adapter logs from.
    assert {f.name for f in dataclasses.fields(ChannelInfo)} == {"index", "name", "role"}

    caplog.set_level(logging.INFO, logger="meshsrv.attachments.delivery.meshtastic")
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa", channels=PRIVATE_CHANNELS)
    adapter = MeshtasticTextAdapter(transport, control_channel_index=1)
    route = Route(route_type=RouteType.DIRECT, route_id="!bbbbbbbb", destination_address="!bbbbbbbb")
    receipt = adapter.send(
        _wire_payload(codec.MessageType.KEY_REQUEST, _SIGNING_KEY), route, idempotency_key="idem-1"
    )

    # The only log line records index + name + destination - never a PSK/token.
    log_text = caplog.text
    assert "channel 1" in log_text
    assert "Flint-pvt" in log_text
    lowered = log_text.lower()
    for secret_marker in ("psk", "token", "secret"):
        assert secret_marker not in lowered

    # The receipt (the send "API response") carries only the documented
    # non-secret fields - no field that could hold a PSK or upload token.
    assert {f.name for f in dataclasses.fields(receipt)} == {
        "sent", "idempotency_key", "external_message_id", "sent_at",
    }


# --------------------------------------------------------------------------
# Inbound: retain the received channel index; accept on any channel.
# --------------------------------------------------------------------------

def _request_text(signing_key: SigningKey) -> str:
    return codec.to_text(codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x01" * 8), signing_key))


def test_inbound_retains_received_channel_index():
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa")
    receiver = MeshtasticTextAdapter(transport)
    envelope = receiver.ingest(
        {"text": _request_text(_SIGNING_KEY), "source_address": "!bbbbbbbb", "packet_id": "p1", "channel_index": 1}
    )
    assert envelope is not None
    assert envelope.transport_metadata.get("channel_index") == 1


def test_inbound_without_channel_index_omits_metadata():
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa")
    receiver = MeshtasticTextAdapter(transport)
    envelope = receiver.ingest(
        {"text": _request_text(_SIGNING_KEY), "source_address": "!bbbbbbbb", "packet_id": "p1"}
    )
    assert envelope is not None
    assert envelope.transport_metadata == {}


def test_inbound_accepted_regardless_of_channel():
    """Inbound-channel policy (explicit): arrival channel is NOT an
    accept/reject gate - trust is signature/TOFU-based, the channel is a
    send-time selection. An MCA message on any channel (or with no channel
    known) still ingests to a valid envelope."""
    ether = InMemoryEther()
    transport = FakeRadioTransport(ether, "!aaaaaaaa")
    receiver = MeshtasticTextAdapter(transport, control_channel_index=1)
    text = _request_text(_SIGNING_KEY)
    for channel_index in (0, 1, 2, None):
        envelope = receiver.ingest(
            {"text": text, "source_address": "!bbbbbbbb", "channel_index": channel_index}
        )
        assert envelope is not None, f"ingest must accept inbound on channel {channel_index}"
