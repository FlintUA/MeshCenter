"""Tests for meshsrv/radio_identity.py's compare_radio_identity() - the
protective check that decides whether server.py is allowed to start the
listener and write into a radio profile: on the configured radio matching
the physically detected one, or blocked on a mismatch (see server.py's
verify_radio_identity()/start_runtime(), which gate listen_meshtastic() and
parse_nodes_from_info() on `identity_status == "MATCH"`). No server.py
import needed - this module only depends on meshsrv.meshtastic_transport
(subprocess wrapper, not called here) and meshsrv.runtime_identity, neither
of which touch hardware at import time.
"""

from meshsrv.radio_identity import compare_radio_identity, detect_tcp_radio_identity, parse_radio_identity
from meshsrv.radio_transport import NodeInfo, NodeUser, TransportError, TransportErrorCode


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


class _FakeTcpRadioTransport:
    """Stands in for the tcp_ipc_transport parameter detect_tcp_radio_identity()
    takes by DI - records connect()/get_local_node()/get_metadata() calls,
    can be told to raise a specific TransportError instead."""

    def __init__(self, *, node_id="!1fa065f0", raises=None, metadata_json=None):
        self._node_id = node_id
        self._raises = raises
        self._metadata_json = metadata_json
        self.connect_calls = []

    def connect(self, descriptor, *, timeout):
        self.connect_calls.append((descriptor, timeout))
        if self._raises is not None:
            raise self._raises

    def get_local_node(self, *, timeout):
        if self._node_id is None:
            return NodeInfo(node_id="", num=0, user=None)
        user = NodeUser(id=self._node_id, long_name="T-Beam", short_name="TBM", hw_model="TBEAM")
        return NodeInfo(node_id=self._node_id, num=1, user=user)

    def get_metadata(self, *, timeout):
        import json as _json

        return {"metadata_json": _json.dumps(self._metadata_json or {"firmwareVersion": "2.7.15.567b8ea"})}


def test_detect_tcp_radio_identity_success():
    transport = _FakeTcpRadioTransport(node_id="!1fa065f0")

    result, output = detect_tcp_radio_identity(transport, "192.168.2.34", 4403, timeout=10)

    assert result["status"] == "MATCH"
    assert result["detected"]["node_id"] == "!1fa065f0"
    assert result["detected"]["long_name"] == "T-Beam"
    assert result["detected"]["firmware_version"] == "2.7.15.567b8ea"
    assert result["error"] is None
    assert result["error_code"] is None
    descriptor, timeout_arg = transport.connect_calls[0]
    assert descriptor.address == "192.168.2.34:4403"
    assert timeout_arg == 10


def test_detect_tcp_radio_identity_no_host_configured_short_circuits():
    transport = _FakeTcpRadioTransport()

    result, _ = detect_tcp_radio_identity(transport, "", 4403, timeout=10)

    assert result["status"] == "DETECTION_ERROR"
    assert result["error"] == "No TCP host configured"
    assert transport.connect_calls == []


def test_detect_tcp_radio_identity_surfaces_specific_error_code():
    transport = _FakeTcpRadioTransport(
        raises=TransportError(TransportErrorCode.CONNECT_REFUSED, "192.168.2.34:4403 refused the connection")
    )

    result, _ = detect_tcp_radio_identity(transport, "192.168.2.34", 4403, timeout=10)

    assert result["status"] == "DETECTION_ERROR"
    assert result["error_code"] == "connect_refused"
    assert "refused" in result["error"]


def test_detect_tcp_radio_identity_protocol_sync_timeout_is_surfaced_distinctly():
    transport = _FakeTcpRadioTransport(
        raises=TransportError(TransportErrorCode.PROTOCOL_SYNC_TIMEOUT, "handshake never completed")
    )

    result, _ = detect_tcp_radio_identity(transport, "192.168.2.34", 4403, timeout=10)

    assert result["error_code"] == "protocol_sync_timeout"


def test_detect_tcp_radio_identity_no_node_id_is_not_found():
    transport = _FakeTcpRadioTransport(node_id=None)

    result, _ = detect_tcp_radio_identity(transport, "192.168.2.34", 4403, timeout=10)

    assert result["status"] == "NOT_FOUND"
    assert result["detected"]["node_id"] == ""


def test_detect_tcp_radio_identity_metadata_failure_is_not_fatal():
    class _NoMetadataTransport(_FakeTcpRadioTransport):
        def get_metadata(self, *, timeout):
            raise RuntimeError("metadata unavailable")

    transport = _NoMetadataTransport(node_id="!1fa065f0")

    result, _ = detect_tcp_radio_identity(transport, "192.168.2.34", 4403, timeout=10)

    assert result["status"] == "MATCH"
    assert result["detected"]["node_id"] == "!1fa065f0"
    assert result["detected"]["firmware_version"] == ""


def test_detect_tcp_radio_identity_then_compare_end_to_end():
    configured = {"node_id": "!1fa065f0"}
    transport = _FakeTcpRadioTransport(node_id="!1fa065f0")
    result, _ = detect_tcp_radio_identity(transport, "192.168.2.34", 4403, timeout=10)
    assert compare_radio_identity(configured, result["detected"]) == "MATCH"

    other_transport = _FakeTcpRadioTransport(node_id="!deadbeef")
    other_result, _ = detect_tcp_radio_identity(other_transport, "192.168.2.34", 4403, timeout=10)
    assert compare_radio_identity(configured, other_result["detected"]) == "MISMATCH"
