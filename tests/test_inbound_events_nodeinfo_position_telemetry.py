"""Direct unit tests for ingest_received_nodeinfo/position/telemetry() (PR C).

Cross-transport PARITY (serial vs TCP producing identical node state) lives in
test_inbound_parity.py alongside the existing text/waypoint parity tests -
this file covers what parity tests don't: the safety gate, and the outcomes
that only ever happen on a standalone event (no resolvable id, no position,
no metric values, the local-node skip).

Position has no serial counterpart at all (the CLI --listen path only ever
saw a position bundled inside a "Received nodeinfo:" line - see
ingest_received_position()'s own docstring in meshsrv/inbound_events.py), so
its node-merge behavior is asserted here directly rather than via parity.
"""
import pytest

from meshsrv import inbound_events
from meshsrv.radio_transport import ReceivedNodeInfoEvent, ReceivedPositionEvent, ReceivedTelemetryEvent

REMOTE = "!1fa065f0"
OTHER_RADIO = "!0badf00d"


@pytest.fixture
def srv(server_module):
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.telemetry.add_node_telemetry_record = lambda *a, **k: None
    inbound_events.reset_inbound_stats()
    yield server_module
    server_module.nodes.clear()
    server_module.chats.clear()


def _local(srv):
    return srv.active_radio_node_id()


def _nodeinfo_event(srv, **overrides):
    fields = dict(
        node_id=REMOTE, sender_id=REMOTE, received_at=1790000000.0, local_radio_node_id=_local(srv),
        packet_id=1, long_name="Test Node", short_name="TST", hw_model="RAK4631", role="ROUTER",
        rx_rssi=-80, rx_snr=5.5, hop_start=3, relay_node=240,
    )
    fields.update(overrides)
    return ReceivedNodeInfoEvent(**fields)


def _position_event(srv, **overrides):
    fields = dict(
        sender_id=REMOTE, received_at=1790000000.0, local_radio_node_id=_local(srv),
        packet_id=2, latitude=50.4501, longitude=30.5234, altitude=120,
        ground_speed=3, sats_in_view=9, position_time=1790000001,
    )
    fields.update(overrides)
    return ReceivedPositionEvent(**fields)


def _telemetry_event(srv, **overrides):
    fields = dict(
        sender_id=REMOTE, kind="device", metrics={"batteryLevel": 80, "voltage": 3.9},
        received_at=1790000000.0, local_radio_node_id=_local(srv), packet_id=3,
    )
    fields.update(overrides)
    return ReceivedTelemetryEvent(**fields)


# ---------------------------------------------------------------------------
# NodeInfo
# ---------------------------------------------------------------------------


def test_a_nodeinfo_creates_a_node_via_the_shared_merge_helper(srv):
    outcome = inbound_events.ingest_received_nodeinfo(_nodeinfo_event(srv), srv._inbound_deps())

    assert outcome == inbound_events.STORED
    node = srv.nodes[REMOTE]
    assert node["name"] == "Test Node"
    assert (node["short_name"], node["hw_model"], node["role"]) == ("TST", "RAK4631", "ROUTER")
    assert (node["rssi"], node["snr"], node["hop_start"], node["relay_node"]) == ("-80", "5.5", "3", "240")
    assert REMOTE in srv.chats
    assert inbound_events.get_inbound_stats()["nodeinfo_stored"] == 1


def test_a_nodeinfo_with_no_node_id_is_rejected(srv):
    outcome = inbound_events.ingest_received_nodeinfo(_nodeinfo_event(srv, node_id=""), srv._inbound_deps())

    assert outcome == inbound_events.NO_NODE_ID
    assert srv.nodes == {}


def test_the_local_node_is_skipped_like_process_nodeinfo_skips_it(srv):
    local = _local(srv)
    outcome = inbound_events.ingest_received_nodeinfo(
        _nodeinfo_event(srv, node_id=local, sender_id=local), srv._inbound_deps(),
    )

    assert outcome == inbound_events.SKIPPED_LOCAL
    assert local not in srv.nodes


def test_a_nodeinfo_from_another_radio_is_dropped(srv):
    outcome = inbound_events.ingest_received_nodeinfo(
        _nodeinfo_event(srv, local_radio_node_id=OTHER_RADIO), srv._inbound_deps(),
    )

    assert outcome == inbound_events.STALE_RADIO
    assert srv.nodes == {}
    assert inbound_events.get_inbound_stats()["stale_identity_dropped"] == 1


def test_a_repeated_nodeinfo_never_erases_a_known_position(srv):
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Old", "position": {"latitude": 1.0, "longitude": 2.0}}

    inbound_events.ingest_received_nodeinfo(_nodeinfo_event(srv), srv._inbound_deps())

    assert srv.nodes[REMOTE]["position"] == {"latitude": 1.0, "longitude": 2.0}


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


def test_a_position_creates_a_minimal_node_and_sets_its_position(srv):
    outcome = inbound_events.ingest_received_position(_position_event(srv), srv._inbound_deps())

    assert outcome == inbound_events.STORED
    node = srv.nodes[REMOTE]
    assert node["position"] == {
        "latitude": 50.4501, "longitude": 30.5234, "altitude": 120,
        "time": 1790000001, "ground_speed": 3, "sats_in_view": 9,
    }
    assert node["name"] == srv.friendly_unknown_node_name(REMOTE)
    assert REMOTE in srv.chats
    assert inbound_events.get_inbound_stats()["position_stored"] == 1


def test_a_position_overlays_onto_an_existing_node_without_touching_identity(srv):
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Known Node", "short_name": "KWN", "hw_model": "TBEAM",
                          "role": "ROUTER", "rssi": "-70"}

    inbound_events.ingest_received_position(_position_event(srv), srv._inbound_deps())

    node = srv.nodes[REMOTE]
    assert (node["name"], node["short_name"], node["hw_model"], node["role"], node["rssi"]) == \
        ("Known Node", "KWN", "TBEAM", "ROUTER", "-70")
    assert node["position"]["latitude"] == pytest.approx(50.4501)


def test_a_new_position_overlays_only_the_keys_it_carries(srv):
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Old", "position": {"latitude": 1.0, "longitude": 2.0, "altitude": 5}}

    inbound_events.ingest_received_position(
        _position_event(srv, altitude=None, ground_speed=None, sats_in_view=None, position_time=None),
        srv._inbound_deps(),
    )

    assert srv.nodes[REMOTE]["position"] == {"latitude": 50.4501, "longitude": 30.5234, "altitude": 5}


def test_a_position_without_coordinates_is_skipped(srv):
    outcome = inbound_events.ingest_received_position(
        _position_event(srv, latitude=None, longitude=None), srv._inbound_deps(),
    )

    assert outcome == inbound_events.NO_POSITION
    assert srv.nodes == {}
    assert inbound_events.get_inbound_stats()["position_no_position"] == 1


def test_a_position_with_no_sender_id_is_rejected(srv):
    outcome = inbound_events.ingest_received_position(_position_event(srv, sender_id=""), srv._inbound_deps())

    assert outcome == inbound_events.NO_NODE_ID
    assert srv.nodes == {}


def test_a_position_from_another_radio_is_dropped(srv):
    outcome = inbound_events.ingest_received_position(
        _position_event(srv, local_radio_node_id=OTHER_RADIO), srv._inbound_deps(),
    )

    assert outcome == inbound_events.STALE_RADIO
    assert srv.nodes == {}


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_device_telemetry_is_a_thin_wrapper_over_apply_node_telemetry(srv):
    outcome = inbound_events.ingest_received_telemetry(_telemetry_event(srv), srv._inbound_deps())

    assert outcome == inbound_events.STORED
    node = srv.nodes[REMOTE]
    assert node["battery_level"] == 80 and node["voltage"] == 3.9
    assert node["telemetry_source"] == "tcp"
    assert inbound_events.get_inbound_stats()["telemetry_stored"] == 1


def test_environment_telemetry_routes_into_environment_metrics_only(srv):
    inbound_events.ingest_received_telemetry(
        _telemetry_event(srv, kind="environment", metrics={"temperature": 21.5, "relativeHumidity": 40.0}),
        srv._inbound_deps(),
    )

    node = srv.nodes[REMOTE]
    assert node["environment_metrics"]["temperature"] == 21.5
    # apply_node_telemetry() unconditionally setdefaults every metrics dict -
    # unchanged, pre-existing behavior; device_metrics stays empty, not absent.
    assert node["device_metrics"] == {}


def test_power_telemetry_computes_power_from_voltage_and_current(srv):
    inbound_events.ingest_received_telemetry(
        _telemetry_event(srv, kind="power", metrics={"ch1Voltage": 4.8, "ch1Current": 0.6}),
        srv._inbound_deps(),
    )

    assert srv.nodes[REMOTE]["power_metrics"]["channels"]["1"]["power"] == pytest.approx(2.88)


def test_telemetry_with_no_sender_id_is_rejected(srv):
    outcome = inbound_events.ingest_received_telemetry(_telemetry_event(srv, sender_id=""), srv._inbound_deps())

    assert outcome == inbound_events.NO_NODE_ID
    assert srv.nodes == {}


def test_telemetry_from_another_radio_is_dropped(srv):
    outcome = inbound_events.ingest_received_telemetry(
        _telemetry_event(srv, local_radio_node_id=OTHER_RADIO), srv._inbound_deps(),
    )

    assert outcome == inbound_events.STALE_RADIO
    assert srv.nodes == {}


def test_the_local_node_still_gets_its_telemetry_but_not_history(srv, monkeypatch):
    local = _local(srv)
    calls = []
    monkeypatch.setattr(srv.telemetry, "add_node_telemetry_record", lambda node_id, values, source: calls.append(node_id))

    outcome = inbound_events.ingest_received_telemetry(
        _telemetry_event(srv, sender_id=local, local_radio_node_id=local), srv._inbound_deps(),
    )

    assert outcome == inbound_events.STORED
    assert srv.nodes[local]["battery_level"] == 80
    assert calls == [], "apply_node_telemetry() itself skips history for the local node - unchanged"
