"""Tests for meshsrv/radio_identity.py's compare_radio_identity() - the
protective check that decides whether server.py is allowed to start the
listener and write into a radio profile: on the configured radio matching
the physically detected one, or blocked on a mismatch (see server.py's
verify_radio_identity()/start_runtime(), which gate listen_meshtastic() and
parse_nodes_from_info() on `identity_status == "MATCH"`). No server.py
import needed - this module only depends on meshsrv.meshsrv (subprocess
wrapper, not called here) and meshsrv.runtime_identity, neither of which
touch hardware at import time.
"""

from meshsrv.radio_identity import compare_radio_identity, parse_radio_identity


def test_matching_node_id_is_a_match():
    saved = {"node_id": "!820af75a"}
    detected = {"node_id": "!820af75a"}
    assert compare_radio_identity(saved, detected) == "MATCH"


def test_matching_node_id_is_case_and_format_insensitive():
    # The configured value may be stored with different casing/prefix
    # conventions than what --info reports - both normalize through
    # _normalize_node_id() before comparison.
    saved = {"node_id": "!820AF75A"}
    detected = {"node_id": "!820af75a"}
    assert compare_radio_identity(saved, detected) == "MATCH"


def test_different_node_id_is_a_mismatch():
    saved = {"node_id": "!820af75a"}
    detected = {"node_id": "!aabbccdd"}
    assert compare_radio_identity(saved, detected) == "MISMATCH"


def test_no_detected_radio_is_not_found():
    # Nothing answered the --info probe - status is NOT_FOUND regardless of
    # what's configured, distinct from a MISMATCH (a different radio
    # answered) - server.py's verify_radio_identity() treats both as "do
    # not start the listener", but the distinction matters for the message
    # shown to the user.
    saved = {"node_id": "!820af75a"}
    detected = {}
    assert compare_radio_identity(saved, detected) == "NOT_FOUND"


def test_no_configured_radio_is_not_checked():
    # First-ever run: nothing has been accepted/configured yet, but a radio
    # did answer - MATCH/MISMATCH can't be decided yet.
    saved = {}
    detected = {"node_id": "!820af75a"}
    assert compare_radio_identity(saved, detected) == "NOT_CHECKED"


def test_neither_configured_nor_detected_is_not_found():
    # detected_id emptiness is checked first - see compare_radio_identity()'s
    # own order of checks.
    assert compare_radio_identity({}, {}) == "NOT_FOUND"


def test_parse_radio_identity_extracts_local_node_from_info_output():
    info_output = (
        'Connected to radio\n'
        'Owner: Flint TAP2 (FTP2)\n'
        '{"myNodeNum": 1979622058}\n'
        'Nodes in mesh: {\n'
        '  "!75fea2aa": {\n'
        '    "num": 1979622058,\n'
        '    "user": {\n'
        '      "id": "!75fea2aa",\n'
        '      "longName": "Flint TAP2",\n'
        '      "shortName": "FTP2",\n'
        '      "hwModel": "RAK4631",\n'
        '      "role": "CLIENT"\n'
        '    }\n'
        '  }\n'
        '}\n'
        'Metadata: {"firmwareVersion": "2.5.20.abcdef"}\n'
    )

    identity = parse_radio_identity(info_output, serial_port="/dev/ttyACM0")

    assert identity["node_id"] == "!75fea2aa"
    assert identity["long_name"] == "Flint TAP2"
    assert identity["short_name"] == "FTP2"
    assert identity["hardware"] == "RAK4631"
    assert identity["firmware_version"] == "2.5.20.abcdef"
    assert identity["port"] == "/dev/ttyACM0"


def test_parse_radio_identity_returns_empty_node_id_for_unrelated_output():
    identity = parse_radio_identity("some unrelated CLI error output", serial_port="/dev/ttyACM0")
    assert identity["node_id"] == ""


def test_match_then_mismatch_end_to_end_with_parsed_output():
    # A more end-to-end shape: parse two different --info outputs (a radio
    # swap) and confirm the match/mismatch verdict follows the physically
    # detected node, not the configured one.
    configured = {"node_id": "!75fea2aa"}

    same_radio_info = 'Owner: Flint TAP2 (FTP2)\n{"myNodeNum": 1979622058}\n'
    same_radio = parse_radio_identity(same_radio_info)
    assert compare_radio_identity(configured, same_radio) == "MATCH"

    different_radio_info = 'Owner: Someone Else (SOME)\n{"myNodeNum": 2864434397}\n'
    different_radio = parse_radio_identity(different_radio_info)
    assert compare_radio_identity(configured, different_radio) == "MISMATCH"
