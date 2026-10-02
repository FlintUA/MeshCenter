"""H1-C2 (F-finding): _handle_listener_line() used to fire
radio_event("packet") unconditionally for EVERY raw `meshtastic --listen`
stdout line - blank lines, log noise, WARNING/ERROR diagnostics included -
so radio_health["last_packet"]/packet_age (compute_radio_health_status()'s
OK/IDLE/NO_PACKETS classification) was really measuring "did the CLI
subprocess print anything recently", not "did the mesh actually deliver a
packet recently". A genuinely quiet mesh with a healthy listener and a
listener that stopped receiving real traffic but kept printing SOMETHING
could look identical.

radio_event("packet") now fires only for a line _handle_listener_line()
itself recognizes as one of the actual received-packet kinds it parses
(nodeinfo/waypoint/routing ack/telemetry/text) - mirroring the same marker
checks those branches already use. The old unconditional signal survives
as its own field, last_cli_activity, via a new "cli_activity" radio_event.
"""
import pytest

REMOTE = "!1fa065f0"
REMOTE_NUM = 0x1FA065F0


def _text_line(text="hello mesh", *, packet_id=101):
    return (
        "Received: {'from': %d, 'to': 4294967295, "
        "'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': b'x', 'bitfield': 1, 'text': '%s'}, "
        "'id': %d, 'rxTime': 1790455378, 'rxSnr': 5.5, 'hopLimit': 3, 'rxRssi': -80, "
        "'hopStart': 3, 'relayNode': 240, 'fromId': '%s', 'toId': '^all'}"
        % (REMOTE_NUM, text, packet_id, REMOTE)
    )


@pytest.fixture
def srv(server_module):
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    server_module.seen_ids.clear()
    server_module.seen_recent_texts.clear()
    server_module.radio_health["last_packet"] = 0
    server_module.radio_health["last_cli_activity"] = 0
    yield server_module
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    server_module.seen_ids.clear()
    server_module.seen_recent_texts.clear()


def test_empty_line_does_not_update_last_packet(srv):
    srv._handle_listener_line("")
    assert srv.radio_health["last_packet"] == 0


def test_empty_line_still_updates_last_cli_activity(srv):
    """The liveness signal is deliberately kept for every line, including
    an empty one - only last_packet becomes selective."""
    srv._handle_listener_line("")
    assert srv.radio_health["last_cli_activity"] > 0


def test_log_noise_line_does_not_update_last_packet(srv):
    srv._handle_listener_line("WARNING: some unrelated CLI log line")
    assert srv.radio_health["last_packet"] == 0
    assert srv.radio_health["last_cli_activity"] > 0


def test_unrelated_non_packet_line_does_not_update_last_packet(srv):
    srv._handle_listener_line("some diagnostic line with no packet markers at all")
    assert srv.radio_health["last_packet"] == 0


def test_real_text_packet_line_updates_last_packet(srv):
    srv._handle_listener_line(_text_line("hello"))
    assert srv.radio_health["last_packet"] > 0


def test_real_telemetry_line_updates_last_packet(srv):
    line = (
        "Received: {'from': %d, 'to': 4294967295, "
        "'decoded': {'portnum': 'TELEMETRY_APP', 'telemetry': {'deviceMetrics': "
        "{'batteryLevel': 80, 'voltage': 4.0}}}, 'id': 102}"
        % REMOTE_NUM
    )
    srv._handle_listener_line(line)
    assert srv.radio_health["last_packet"] > 0


def test_real_waypoint_line_updates_last_packet(srv):
    line = (
        "Received: {'from': %d, 'to': 4294967295, 'decoded': {'portnum': 'WAYPOINT_APP', "
        "'payload': b'y', 'waypoint': {'id': 1, 'latitudeI': 504501000, 'longitudeI': 305234000, "
        "'expire': 4102444800, 'name': 'Cafe', 'description': 'meet here', 'icon': 128205}}, "
        "'id': 103, 'fromId': '%s', 'toId': '^all'}"
        % (REMOTE_NUM, REMOTE)
    )
    srv.waypoint_store.delete_all()
    srv._handle_listener_line(line)
    srv.waypoint_store.delete_all()
    assert srv.radio_health["last_packet"] > 0


def test_routing_ack_line_updates_last_packet(srv):
    line = "Publishing meshtastic.receive.routing: {'packet_id': 104}"
    srv._handle_listener_line(line)
    assert srv.radio_health["last_packet"] > 0
