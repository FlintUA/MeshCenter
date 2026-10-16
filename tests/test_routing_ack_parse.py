"""Unit-level evidence for DK2's "does serial DM ACK work at all?" question.

LINE is verbatim `meshtastic --listen` output (CLI + library 2.7.11, captured
2026-10-14 from a ROUTING_APP response pushed by the repo's fake radio): the
text parser's regexes match it, so a DM whose packet_id is known resolves to
"delivered" - the parsing itself is not what stops serial ACKs from showing.
(What can: the --listen process is paused/restarting around the send, see the
DK2 report. TCP never feeds this function at all - no structured routing event
exists yet.)"""
LINE = (
    "DEBUG file:mesh_interface.py _handlePacketFromRadio line:1668 Publishing "
    "meshtastic.receive.routing: packet={'from': 2968603946, 'to': 1970248032, "
    "'decoded': {'portnum': 'ROUTING_APP', 'payload': b'\x18\x00', 'requestId': 123456, "
    "'routing': {'errorReason': 'NONE', 'raw': error_reason: NONE\n}}, 'id': 556}"
)
NAK = LINE.replace("'errorReason': 'NONE'", "'errorReason': 'MAX_RETRANSMIT'")


def _setup(srv, monkeypatch):
    monkeypatch.setattr(srv, "save_messages", lambda: None)
    message = {"id": "m1", "chat_id": "!b0f14d2a", "packet_id": 123456, "status": "sent", "ack_requested": True,
               "ack_deadline": 9e12}
    srv.messages.append(message)
    return message


def test_real_cli_routing_line_marks_the_dm_delivered(server_module, monkeypatch):
    message = _setup(server_module, monkeypatch)
    try:
        assert server_module.extract_request_id(LINE) == 123456
        server_module.process_routing_ack_line(LINE)
        assert message["status"] == "delivered" and "ack_deadline" not in message
    finally:
        server_module.messages.remove(message)


def test_real_cli_nak_line_marks_the_dm_unconfirmed(server_module, monkeypatch):
    message = _setup(server_module, monkeypatch)
    try:
        server_module.process_routing_ack_line(NAK)
        assert message["status"] == "unconfirmed" and message["error"] == "MAX_RETRANSMIT"
    finally:
        server_module.messages.remove(message)


def test_ack_for_an_unknown_or_unsent_message_is_ignored(server_module, monkeypatch):
    message = _setup(server_module, monkeypatch)
    message["status"] = "pending"  # an ACK racing the "sent" bookkeeping is silently dropped
    try:
        server_module.process_routing_ack_line(LINE)
        assert message["status"] == "pending"
    finally:
        server_module.messages.remove(message)
