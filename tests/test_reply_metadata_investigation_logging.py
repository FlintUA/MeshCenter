"""TEMPORARY - Reply Metadata Consistency investigation (PR 1, investigation
only, no architectural change). Covers the one-line diagnostic added to
ingest_received_text() in meshsrv/inbound_events.py: for any inbound text
that names a reply_id, log packet_id/reply_id/chat_id/original_found/
matched_original_packet_id/transport/local_radio_node_id - counts and
identifiers only, never message text.

Remove this file (and the logging it tests) once the live root cause behind
the PR 1 execution plan is confirmed and PR 2's architectural fix lands -
see docs/... (execution plan) for the full context. Not meant to be a
permanent part of the test suite.
"""
import pytest

from meshsrv import inbound_events
from test_serial_inbound_characterization import _text_line
from test_inbound_parity import _tcp_event, _tcp_text_packet

REMOTE = "!1fa065f0"


@pytest.fixture
def srv(server_module):
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    server_module.seen_ids.clear()
    server_module.seen_recent_texts.clear()
    inbound_events.reset_inbound_stats()
    yield server_module
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    server_module.seen_ids.clear()
    server_module.seen_recent_texts.clear()


def test_a_resolved_serial_reply_logs_the_match(srv, capsys):
    srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)

    srv._handle_listener_line(_text_line("the answer", packet_id=8, reply_id=555))

    out = capsys.readouterr().out
    [line] = [l for l in out.splitlines() if l.startswith("[INBOUND REPLY]")]
    assert "packet_id=8" in line and "reply_id=555" in line
    assert "original_found=true" in line and "matched_original_packet_id=555" in line
    assert "transport=serial" in line
    assert f"local_radio_node_id={srv.active_radio_node_id()}" in line


def test_an_unresolved_serial_reply_logs_the_miss(srv, capsys):
    srv._handle_listener_line(_text_line("orphan reply", packet_id=8, reply_id=999999))

    out = capsys.readouterr().out
    [line] = [l for l in out.splitlines() if l.startswith("[INBOUND REPLY]")]
    assert "reply_id=999999" in line
    assert "original_found=false" in line and "matched_original_packet_id=None" in line
    assert "transport=serial" in line


def test_a_non_reply_text_logs_nothing(srv, capsys):
    srv._handle_listener_line(_text_line("just a message", packet_id=9))

    out = capsys.readouterr().out
    assert "[INBOUND REPLY]" not in out


def test_a_resolved_tcp_reply_logs_transport_tcp(srv, capsys):
    srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)

    event = _tcp_event(srv, _tcp_text_packet("the answer", packet_id=8, reply_id=555))
    inbound_events.ingest_received_text(event, srv._inbound_deps())

    out = capsys.readouterr().out
    [line] = [l for l in out.splitlines() if l.startswith("[INBOUND REPLY]")]
    assert "original_found=true" in line and "matched_original_packet_id=555" in line
    assert "transport=tcp" in line


def test_an_unresolved_tcp_reply_logs_the_miss(srv, capsys):
    event = _tcp_event(srv, _tcp_text_packet("orphan", packet_id=8, reply_id=999999))
    inbound_events.ingest_received_text(event, srv._inbound_deps())

    out = capsys.readouterr().out
    [line] = [l for l in out.splitlines() if l.startswith("[INBOUND REPLY]")]
    assert "original_found=false" in line
    assert "transport=tcp" in line


def test_the_reply_log_line_never_contains_message_text(srv, capsys):
    srv.add_message("me", "Me", "SECRET_ORIGINAL_TEXT", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)

    srv._handle_listener_line(_text_line("SECRET_REPLY_TEXT", packet_id=8, reply_id=555))

    out = capsys.readouterr().out
    [line] = [l for l in out.splitlines() if l.startswith("[INBOUND REPLY]")]
    assert "SECRET_ORIGINAL_TEXT" not in line and "SECRET_REPLY_TEXT" not in line
