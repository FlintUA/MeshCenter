"""SIG3: waypoint names/descriptions and NodeInfo long/short names from the serial `--listen` line are DECODED like
a received text is (SIG2): the CLI prints the packet as a Python dict, so a name arrives in repr form. The old regexes
stopped at the first quote of either kind (an apostrophe cut "Joe's" to "Joe") and kept escapes verbatim ("a\\nb" with a
literal backslash). Serial must leave exactly what the TCP adapter leaves for the same packet."""
import pytest

from meshsrv import inbound_events
from test_inbound_parity import (  # noqa: F401  (srv is a fixture)
    REMOTE, REMOTE_NUM, _assert_nodeinfo_parity, _tcp_nodeinfo_packet, _tcp_waypoint_event, _waypoint_via_serial,
    _waypoint_via_tcp, srv,
)

TRICKY = [
    "Joe's Cafe",                       # apostrophe: the CLI prints it inside double quotes
    'say "hi"',                         # double quotes inside single quotes
    "it's \"both\"",                    # both: the CLI escapes the apostrophe
    "C:\\temp\\new",                    # backslashes
    "two\nlines",                       # a real newline prints as backslash-n
    "tab\there",
    "Кафе \U0001F600 café",             # Cyrillic, emoji, accent
    "bell\x07char",                     # non-printable prints as \x07
    "ends with backslash\\",
    "fake 'shortName': 'EVIL' inside",  # a name that looks like the next field
]


def _repr(value):
    return repr(value)


def _waypoint_serial_line(waypoint_id, name, description):
    return (
        "Received: {'from': %d, 'to': 4294967295, 'channel': 1, 'decoded': {'portnum': 'WAYPOINT_APP', "
        "'payload': b'y', 'waypoint': {'id': %d, 'latitudeI': 504501000, 'longitudeI': 305234000, 'expire': 4102444800, "
        "'name': %s, 'description': %s, 'icon': 128205, 'raw': id: 1}}, 'id': 102, 'fromId': '%s', 'toId': '^all'}"
        % (REMOTE_NUM, waypoint_id, _repr(name), _repr(description), REMOTE)
    )


def _nodeinfo_serial_line(long_name, short_name):
    return (
        "Received: {'from': %d, 'decoded': {'portnum': 'NODEINFO_APP', 'user': {'id': '%s', 'longName': %s, "
        "'shortName': %s, 'hwModel': 'RAK4631', 'role': 'ROUTER'}}, 'id': 201, 'rxRssi': -80, 'rxSnr': 5.5, "
        "'hopStart': 3, 'relayNode': 240, 'fromId': '%s'}" % (REMOTE_NUM, REMOTE, _repr(long_name), _repr(short_name), REMOTE)
    )


@pytest.mark.parametrize("name", TRICKY)
def test_a_waypoint_name_is_what_the_radio_sent(srv, name):
    description = f"about {name}"
    serial_rows, _raw = _waypoint_via_serial(srv, [_waypoint_serial_line(930001, name, description)])
    assert serial_rows[930001]["name"] == name
    assert serial_rows[930001]["description"] == description


@pytest.mark.parametrize("name", TRICKY)
def test_serial_and_tcp_leave_the_same_waypoint_row(srv, name):
    description = f"about {name}"
    serial_rows, _raw = _waypoint_via_serial(srv, [_waypoint_serial_line(930002, name, description)])
    tcp_rows, _raw, outcomes = _waypoint_via_tcp(srv, [_tcp_waypoint_event(srv, 930002, name=name, description=description)])
    assert outcomes == [inbound_events.CREATED]
    assert tcp_rows == serial_rows


@pytest.mark.parametrize("name", TRICKY)
def test_serial_and_tcp_leave_the_same_node_names(srv, name):
    long_name = name.strip() or "x"
    snapshot, outcomes = _assert_nodeinfo_parity(
        srv, [_nodeinfo_serial_line(long_name, "SH")], [_tcp_nodeinfo_packet(long_name=long_name, short_name="SH")]
    )
    assert outcomes == [inbound_events.STORED]
    assert snapshot["nodes"][REMOTE]["name"] == long_name
    assert snapshot["nodes"][REMOTE]["short_name"] == "SH"


def test_a_long_name_that_looks_like_the_short_name_field_cannot_forge_it(srv):
    """The short name is read from the real field, never from inside the (masked) long name text."""
    long_name = "x 'shortName': 'EVIL' y"
    snapshot, _ = _assert_nodeinfo_parity(
        srv, [_nodeinfo_serial_line(long_name, "REAL")], [_tcp_nodeinfo_packet(long_name=long_name, short_name="REAL")]
    )
    assert snapshot["nodes"][REMOTE]["short_name"] == "REAL"


def test_both_names_of_a_nodeinfo_with_quotes_and_unicode(srv):
    snapshot, _ = _assert_nodeinfo_parity(
        srv, [_nodeinfo_serial_line("O'Brien \U0001F4E1", 'q"t')], [_tcp_nodeinfo_packet(long_name="O'Brien \U0001F4E1", short_name='q"t')]
    )
    assert snapshot["nodes"][REMOTE]["name"] == "O'Brien \U0001F4E1"
    assert snapshot["nodes"][REMOTE]["short_name"] == 'q"t'


# ---------------------------------------------------------------- the helper itself
def test_string_field_reads_only_real_fields(server_module):
    text = "{'a': 'x \\'name\\': \\'no\\'', 'name': 'yes', 'other': 1}"
    assert server_module._string_field(text, ("name",)) == "yes"


def test_string_field_missing_is_none_and_bare_protobuf_style_works(server_module):
    assert server_module._string_field("{'a': 'b'}", ("name",)) is None
    assert server_module._string_field('long_name: "Plain Name"', ("long_name",)) == "Plain Name"


def test_text_proto_octal_utf8_is_repaired(server_module):
    # protobuf text format escapes the UTF-8 bytes of "é" (0xC3 0xA9) as \303\251
    assert server_module._string_field('long_name: "caf\\303\\251"', ("long_name",)) == "café"


def test_a_malformed_literal_falls_back_to_the_raw_text_and_logs_once(server_module, capsys):
    server_module._FIELD_DECODE_WARNED = False
    first = server_module._string_field("{'name': 'bad \\x'}", ("name",))
    second = server_module._string_field("{'name': 'bad \\x'}", ("name",))
    assert first == "bad \\x" and second == "bad \\x"
    out = capsys.readouterr().out
    assert out.count("Could not decode") == 1, "one log line, not one per field"


def test_nothing_but_one_string_token_is_ever_evaluated(server_module):
    text = "{'name': 'ok', 'x': __import__('os').system('echo hacked')}"
    assert server_module._string_field(text, ("name",)) == "ok"
    assert server_module._string_field(text, ("x",)) is None, "a call is not a string literal"
