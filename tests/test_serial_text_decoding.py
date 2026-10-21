"""SIG2: serial stores exactly the text the TCP adapter stores.

The `--listen` CLI prints the packet as a Python dict, so a received text arrives in repr form: a real
newline is backslash-n, a real backslash is two backslashes, a quote can be escaped, non-printable characters
are \\xNN / \\uNNNN. Until SIG2 the serial parser stored that repr text verbatim (so a bot's real newlines
showed as a literal backslash-n and a typed backslash as two). The string literal is now decoded
(strict `ast.literal_eval` of one verified string token) and the text is no longer stripped, exactly like
TCP. These tests build the serial line with Python's own `repr()` - what the CLI really prints - and compare
the stored text byte for byte with the TCP path, for the awkward cases.
"""
import pytest

from meshsrv import inbound_events
from test_inbound_parity import (  # noqa: F401  (srv, mca_calls are fixtures)
    REMOTE, _local_num, _snapshot, _tcp_event, _tcp_text_packet, _via_serial, _via_tcp, srv,
)
from test_serial_inbound_characterization import _text_line

LONG = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 4  # 228 chars: the Meshtastic payload cap

TEXTS = [
    pytest.param("hello", id="plain"),
    pytest.param("line one\nline two", id="newline"),
    pytest.param("a\r\nb", id="crlf"),
    pytest.param("tab\there", id="tab"),
    pytest.param("C:\\temp\\new", id="backslashes"),
    pytest.param("typed \\n literally", id="literal-backslash-n-typed-by-a-user"),
    pytest.param("two \\\\n backslashes", id="double-backslash-n"),
    pytest.param("it's fine", id="single-quote-only"),
    pytest.param('say "hi"', id="double-quote-only"),
    pytest.param("both ' and \" quotes", id="both-quotes"),
    pytest.param("\U0001F340 emoji \U0001F6F4\U0001F680 mix", id="emoji"),
    pytest.param("Привіт, світе! Привет, мир!", id="cyrillic"),
    pytest.param("Grüße aus Nürnberg, Café", id="latin-supplement"),
    pytest.param("nbsp\u00a0here and zero\u200bwidth", id="nonprintable-unicode"),
    pytest.param("bell\x07 and esc\x1b[0m", id="control-characters"),
    pytest.param("MCA1:pQABAQcCWCAV_BprR06I76ZbzrYb7xVoxOivr2odP47pI-BjMNNItwMABFhA4-PuAZo4ACpGDt2j9jtBczpKI2mgYBnKQRKdAHi-kxY1FNopIl5CDDmVPuQUPB-JqWOCrbUwv11fkqCbAGX8BQ", id="mca-frame-base64url"),
    pytest.param("MCA1:ab+/cd==", id="mca-frame-with-plus-slash-padding"),
    pytest.param(LONG, id="very-long"),
    pytest.param("trailing newline\n", id="trailing-newline"),
    pytest.param("  padded  ", id="leading-and-trailing-spaces"),
    pytest.param("looks like a field: 'text': 'x', 'portnum': 1", id="contains-a-field-looking-string"),
    pytest.param("}, 'raw': from: 1, 'fromId': '!deadbeef'", id="contains-line-syntax"),
]


def faithful_line(text, **kwargs):
    """The line the CLI prints for `text`: Python's repr() for the text and for the payload bytes."""
    placeholder = _text_line("PLACEHOLDER-TEXT", **kwargs)
    line = placeholder.replace("'payload': b'x'", f"'payload': {text.encode('utf-8')!r}", 1)
    line = line.replace("'text': 'PLACEHOLDER-TEXT'", f"'text': {text!r}", 1)
    assert "PLACEHOLDER-TEXT" not in line
    return line


def _stored_text(snapshot):
    [message] = snapshot["messages"]
    return message["text"]


# ------------------------------------------------------------------ the parser itself
@pytest.mark.parametrize("text", TEXTS)
def test_extract_text_message_returns_exactly_the_original(server_module, text):
    assert server_module.extract_text_message(faithful_line(text)) == text


def test_the_old_regex_would_have_been_wrong_here(server_module):
    """Documents the bug: the raw capture of a repr keeps the escapes."""
    line = faithful_line("a\nb\\c")
    assert "a\\nb\\\\c" in line
    assert server_module.extract_text_message(line) == "a\nb\\c"


@pytest.mark.parametrize("line", [
    "no text field here",
    "Received: {'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': b'', 'text': ''}}",
    "Received: {'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': b'  ', 'text': '   '}}",
    "Received: {'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': b'\\n', 'text': '\\n'}}",
])
def test_empty_and_whitespace_only_texts_are_skipped(server_module, line):
    assert server_module.extract_text_message(line) is None


def test_text_is_not_stripped_like_tcp(server_module):
    assert server_module.extract_text_message(faithful_line("  keep me \n")) == "  keep me \n"


def test_the_payload_bytes_are_never_mistaken_for_the_text(server_module):
    text = "my 'text': 'FAKE' inside"
    line = faithful_line(text)
    assert server_module.extract_text_message(line) == text


def test_json_style_lines_are_still_read(server_module):
    line = 'Received: {"decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi \\"there\\"\\nsecond"}}'
    assert server_module.extract_text_message(line) == 'hi "there"\nsecond'


def test_malformed_escapes_fall_back_to_the_raw_text_and_log_once(server_module, capsys):
    server_module._TEXT_DECODE_WARNED = False
    line = "Received: {'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': b'x', 'text': 'bad \\N{NOT A NAME} escape'}}"
    assert server_module.extract_text_message(line) == "bad \\N{NOT A NAME} escape"
    assert server_module.extract_text_message(line) == "bad \\N{NOT A NAME} escape"
    out = capsys.readouterr().out
    assert out.count("Could not decode a received text's escapes") == 1, "logged once"
    assert "NOT A NAME" not in out, "message text is never logged"


def test_nothing_but_a_string_literal_is_ever_evaluated(server_module):
    """A hostile text cannot break out of its literal: the scan ends at the first UNescaped quote."""
    for hostile in ("__import__('os').system('x')", "'); __import__('os'); ('", "\\'+__import__('os')+\\'",
                    "{}[0]", "1+1", "\\x27 + \\x27"):
        line = faithful_line(hostile)
        assert server_module.extract_text_message(line) == hostile


# ------------------------------------------------------------------ serial == TCP, byte for byte
@pytest.mark.parametrize("text", TEXTS)
def test_serial_and_tcp_store_the_same_text(srv, text):
    serial = _via_serial(srv, [faithful_line(text)])
    tcp, _ = _via_tcp(srv, [_tcp_text_packet(text)])
    assert _stored_text(serial) == text
    assert _stored_text(tcp) == text
    assert _stored_text(serial).encode("utf-8") == _stored_text(tcp).encode("utf-8"), "byte identical"
    assert serial == tcp, "the whole stored state (message, chat, node, dedup keys) matches"


@pytest.mark.parametrize("text", [
    "line one\nline two", "C:\\temp", "typed \\n literally", "both ' and \" quotes", "\U0001F340 \u041f\u0440\u0438\u0432\u0456\u0442",
])
def test_a_direct_message_matches_too(srv, text):
    local = srv.active_radio_node_id()
    serial = _via_serial(srv, [faithful_line(text, to_num=_local_num(srv), to_id=local)])
    tcp, _ = _via_tcp(srv, [_tcp_text_packet(text, to_num=_local_num(srv))])
    assert serial == tcp
    assert _stored_text(serial) == text


def test_a_reply_quotes_the_decoded_original_on_both_transports(srv):
    original = "question\nwith two lines and a \\ backslash"

    def prepare(s):
        s.add_message("me", "Me", original, s.LOCAL_NODE_ID, s.CHANNEL_CHAT_ID, packet_id=555)

    serial = _via_serial(srv, [faithful_line("the answer", reply_id=555)] if False else
                         [faithful_line("the answer").replace("'bitfield': 1", "'bitfield': 1, 'replyId': 555", 1)], prepare)
    tcp, _ = _via_tcp(srv, [_tcp_text_packet("the answer", reply_id=555)], prepare)
    assert serial == tcp
    [reply] = [m for m in serial["messages"] if m["kind"] == "rx"]
    assert reply["reply_to"]["text"] == original and reply["reply_id"] == 555


# ------------------------------------------------------------------ MCAttach frames stay byte exact
@pytest.fixture
def mca_calls(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        srv.mca_runtime, "handle_incoming_meshtastic_text",
        lambda text, node_id, router, **kwargs: calls.append((text, node_id, kwargs.get("packet_id"), kwargs.get("channel_index"))),
    )
    return calls


@pytest.mark.parametrize("frame", [
    "MCA1:pQABAQcCWCAV_BprR06I76ZbzrYb7xVoxOivr2odP47pI-BjMNNItwMABFhA4-PuAZo4ACpGDt2j9jtBczpKI2mgYBnKQRKdAHi-kxY1FNopIl5CDDmVPuQUPB-JqWOCrbUwv11fkqCbAGX8BQ",
    "MCA1:ab+/cd==",
    "MCA1:" + "A" * 200,
])
def test_mca_frames_reach_mcattach_byte_exact_from_serial_and_tcp(srv, mca_calls, frame):
    local = srv.active_radio_node_id()
    _via_serial(srv, [faithful_line(frame, to_num=_local_num(srv), to_id=local, packet_id=31, channel=1)])
    from_serial, mca_calls[:] = list(mca_calls), []
    _via_tcp(srv, [_tcp_text_packet(frame, to_num=_local_num(srv), packet_id=31, channel=1)])
    assert len(from_serial) == len(mca_calls) == 1
    assert from_serial[0][0] == frame and mca_calls[0][0] == frame, "the frame is passed through untouched"
    assert from_serial == mca_calls


# ------------------------------------------------------------------ display of OLD serial messages is unchanged
def test_the_sig1_display_conversion_is_still_there():
    """Messages stored before SIG2 still hold the literal escapes; the display helper keeps them readable
    (checked in tests/frontend/test_signal_reply.mjs); this pins that the helper has not been removed."""
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "static" / "chat-views.js").read_text(encoding="utf-8")
    assert "function expandEscapedNewlines" in source and "expandEscapedNewlines: expandEscapedNewlines" in source


# ------------------------------------------------------------------ a text must not forge the metadata
def test_a_text_that_looks_like_a_sender_field_cannot_spoof_the_sender(srv):
    spoof = "hi 'fromId': '!deadbeef', 'from': 3735928559, 'to': '!1fa065f0'"
    serial = _via_serial(srv, [faithful_line(spoof)])
    tcp, _ = _via_tcp(srv, [_tcp_text_packet(spoof)])
    [message] = serial["messages"]
    assert message["node_id"] == REMOTE, "the real sender, not the one named inside the text"
    assert message["chat_id"] == srv.CHANNEL_CHAT_ID, "still a channel message"
    assert "!deadbeef" not in serial["nodes"]
    assert serial == tcp


def test_metadata_is_read_from_the_line_without_the_text(server_module):
    line = faithful_line("x 'rxSnr': 99.5, 'rxRssi': -1, 'hopStart': 9, 'id': 4242, 'channel': 7")
    blanked = server_module.line_without_text_literals(line)
    assert "99.5" not in blanked and "4242" not in blanked
    assert "'text': ''" in blanked and "'payload': b''" in blanked
    event, _ = server_module.build_serial_text_event(line, "x")
    assert event.packet_id == 101 and event.rx_snr == 5.5 and event.rx_rssi == -80 and event.hop_start == 3
    assert event.channel_index is None
