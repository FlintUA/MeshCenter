"""SIG1: every received text keeps its OWN radio metrics (SNR / RSSI / hops), serial and TCP alike.

"Reply with signal" and Message Info need the values of THIS packet; the node record only holds the
latest values of the sender. The message gets an `rx` dict ({snr, rssi, hop_start, hop_limit}) with only
the real, finite numbers; a message without any metrics carries no `rx` key.
"""
import re

import pytest

from meshsrv import inbound_events
from test_inbound_parity import (  # noqa: F401  (srv is a fixture)
    BROADCAST, REMOTE, _tcp_event, _tcp_text_packet, srv,
)
from test_serial_inbound_characterization import _text_line


# ------------------------------------------------------------------ the cleaner
@pytest.mark.parametrize("raw,expected", [
    ({"snr": -5.5, "rssi": -87, "hop_start": 3, "hop_limit": 3}, {"snr": -5.5, "rssi": -87, "hop_start": 3, "hop_limit": 3}),
    ({"snr": "6.25", "rssi": "-90"}, {"snr": 6.25, "rssi": -90}),
    ({"snr": 7.123456}, {"snr": 7.12}),
    ({"snr": float("nan"), "rssi": float("inf")}, None),
    ({"snr": True, "rssi": False, "hop_start": True}, None),
    ({"snr": None, "rssi": None, "hop_start": None, "hop_limit": None}, None),
    ({"snr": "abc", "rssi": "x"}, None),
    ({"hop_start": 0, "hop_limit": 0}, {"hop_start": 0, "hop_limit": 0}),
    ({"snr": 0.0}, {"snr": 0.0}),
    ({}, None),
    (None, None),
    ("not a dict", None),
])
def test_clean_rx_metrics(server_module, raw, expected):
    assert server_module.clean_rx_metrics(raw) == expected


def test_add_message_stores_rx_only_when_there_is_something(srv):
    stored = srv.add_message("rx", "Remote", "hi", REMOTE, srv.CHANNEL_CHAT_ID, packet_id=1,
                             rx={"snr": -5.5, "rssi": -87, "hop_start": 3, "hop_limit": 0})
    assert stored["rx"] == {"snr": -5.5, "rssi": -87, "hop_start": 3, "hop_limit": 0}
    plain = srv.add_message("rx", "Remote", "no metrics", REMOTE, srv.CHANNEL_CHAT_ID, packet_id=2)
    assert "rx" not in plain
    empty = srv.add_message("rx", "Remote", "empty", REMOTE, srv.CHANNEL_CHAT_ID, packet_id=3, rx={"snr": None})
    assert "rx" not in empty


# ------------------------------------------------------------------ both transports
def _rx_of(srv):
    [message] = [m for m in srv.messages if m["kind"] == "rx"]
    return message.get("rx")


def _own_metrics(rx):
    """The SIG1 numbers only (SIG4 adds relay_node / relay beside them)."""
    return {k: v for k, v in (rx or {}).items() if k not in ("relay_node", "relay")}


def test_serial_text_keeps_its_own_metrics(srv):
    srv._handle_listener_line(_text_line("hello", snr=-5.5, rssi=-87, hop_start=3))
    assert _own_metrics(_rx_of(srv)) == {"snr": -5.5, "rssi": -87, "hop_start": 3, "hop_limit": 3}


def test_tcp_text_keeps_its_own_metrics(srv):
    inbound_events.ingest_received_text(_tcp_event(srv, _tcp_text_packet("hello", snr=-5.5, rssi=-87, hop_start=3)), srv._inbound_deps())
    assert _own_metrics(_rx_of(srv)) == {"snr": -5.5, "rssi": -87, "hop_start": 3, "hop_limit": 3}


@pytest.mark.parametrize("snr,rssi,hop_start", [(5.5, -80, 3), (-12.25, -118, 7), (0.0, -60, 1)])
def test_serial_and_tcp_store_the_same_rx(srv, snr, rssi, hop_start):
    srv._handle_listener_line(_text_line("same", snr=snr, rssi=rssi, hop_start=hop_start))
    serial = _rx_of(srv)
    srv.reset_inbound()
    inbound_events.ingest_received_text(_tcp_event(srv, _tcp_text_packet("same", snr=snr, rssi=rssi, hop_start=hop_start)), srv._inbound_deps())
    assert _rx_of(srv) == serial


def test_a_text_without_any_metrics_has_no_rx_key(srv):
    line = _text_line("bare").replace("'rxSnr': 5.5, ", "").replace("'rxRssi': -80, ", "").replace("'hopStart': 3, ", "").replace("'hopLimit': 3, ", "")
    line = re.sub(r"'relayNode': \d+, ", "", line)
    assert "rxSnr" not in line and "rxRssi" not in line and "hopStart" not in line and "hopLimit" not in line
    srv._handle_listener_line(line)
    assert _rx_of(srv) is None


def test_serial_and_tcp_store_the_same_relay(srv):
    srv._handle_listener_line(_text_line("same", snr=5.5, rssi=-80, hop_start=3))
    serial = _rx_of(srv)
    srv.reset_inbound()
    inbound_events.ingest_received_text(_tcp_event(srv, _tcp_text_packet("same", snr=5.5, rssi=-80, hop_start=3)), srv._inbound_deps())
    tcp = _rx_of(srv)
    assert serial["relay_node"] == tcp["relay_node"] and serial["relay"] == tcp["relay"]
    assert serial["relay"]["status"] in ("direct", "resolved", "ambiguous", "unknown")


@pytest.mark.parametrize("raw,expected", [
    ({"relay_node": 132, "relay": {"status": "resolved", "node_id": "!aaaaaa84", "short_name": "FMG", "basis": "direct24h"}},
     {"relay_node": 132, "relay": {"status": "resolved", "node_id": "!aaaaaa84", "short_name": "FMG", "basis": "direct24h"}}),
    ({"relay_node": 0, "relay": {"status": "resolved"}}, None),
    ({"relay_node": 256}, None),
    ({"relay_node": True}, None),
    ({"relay_node": 7, "relay": {"status": "bogus"}}, {"relay_node": 7}),
    ({"relay_node": 7, "relay": "x"}, {"relay_node": 7}),
])
def test_clean_rx_metrics_relay(server_module, raw, expected):
    assert server_module.clean_rx_metrics(raw) == expected


def test_a_direct_packet_records_hop_start_equal_to_hop_limit(srv):
    """Heard directly: hop_start == hop_limit (the receiver counts hops = start - limit = 0)."""
    inbound_events.ingest_received_text(_tcp_event(srv, _tcp_text_packet("direct", hop_start=3)), srv._inbound_deps())
    rx = _rx_of(srv)
    assert rx["hop_start"] - rx["hop_limit"] == 0


def test_the_messages_of_a_chat_carry_rx_for_the_frontend(srv):
    srv._handle_listener_line(_text_line("visible", snr=-5.5, rssi=-87))
    chat = srv.get_chat_messages(srv.CHANNEL_CHAT_ID)
    assert [m["rx"]["snr"] for m in chat if m["kind"] == "rx"] == [-5.5]
