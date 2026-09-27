"""Characterization of the serial NODEINFO_APP text-buffer path
(process_nodeinfo()) and apply_node_telemetry(), written BEFORE the PR C
extraction and required to pass unchanged after it - same discipline as
tests/test_serial_inbound_characterization.py (PR #302).

process_nodeinfo() had ZERO test coverage before this file. Every assertion
below records what the code actually did at the time, including its existing
quirks (falsy-`or` precedence on rssi/hop_start/relay_node, KNOWN_NODE_INFO
overriding the packet's own short_name/hw_model, LOCAL_NODE_ID being skipped
entirely) - none of it bent to fit the refactor.

process_received_nodeinfo_line() (a different, richer CLI text shape bundling
position+telemetry) is NOT touched by PR C beyond extracting its position-merge
and telemetry-values-mapping sub-pieces into shared helpers - its own existing
tests in tests/test_cli_parsing.py are the regression net for that function.
"""
import pytest

REMOTE = "!1fa065f0"


@pytest.fixture
def srv(server_module):
    server_module.nodes.clear()
    server_module.chats.clear()
    yield server_module
    server_module.nodes.clear()
    server_module.chats.clear()


def _block(**fields):
    """A NODEINFO_APP text buffer shaped like the CLI's own dict-ish output -
    just enough substring content for extract_field()/extract_node_id() etc."""
    parts = ["NODEINFO_APP"]
    if "id" in fields:
        parts.append(f"'id': '{fields['id']}'")
    for key in ("longName", "shortName", "hwModel", "role"):
        if key in fields:
            parts.append(f"'{key}': '{fields[key]}'")
    for key in ("rxRssi", "rxSnr", "hopStart", "relayNode"):
        if key in fields:
            parts.append(f"'{key}': {fields[key]}")
    return "Received: {" + ", ".join(parts) + "}"


# --- basic create / update ---------------------------------------------------


def test_a_new_node_is_created_with_the_packets_own_fields(srv):
    handled = srv.process_nodeinfo(_block(
        id=REMOTE, longName="Test Node", shortName="TST", hwModel="RAK4631", role="ROUTER",
        rxRssi=-80, rxSnr=5.5, hopStart=3, relayNode=240,
    ))

    assert handled is True
    node = srv.nodes[REMOTE]
    assert node["name"] == "Test Node"
    assert (node["short_name"], node["hw_model"], node["role"]) == ("TST", "RAK4631", "ROUTER")
    assert (node["rssi"], node["snr"], node["hop_start"], node["relay_node"]) == ("-80", "5.5", "3", "240")
    assert node["ignored"] is False and node["favorite"] is False and node["position"] is None
    assert REMOTE in srv.chats


def test_the_local_node_is_skipped_entirely(srv):
    local = srv.LOCAL_NODE_ID
    handled = srv.process_nodeinfo(_block(id=local, longName="Me", shortName="ME"))

    assert handled is True
    assert local not in srv.nodes


def test_a_line_without_any_nodeinfo_marker_is_not_handled(srv):
    assert srv.process_nodeinfo("Received: {'from': 1, 'decoded': {'portnum': 'POSITION_APP'}}") is False
    assert srv.nodes == {}


def test_a_line_without_a_resolvable_node_id_is_not_handled(srv):
    assert srv.process_nodeinfo("NODEINFO_APP 'longName': 'No id here'") is False


# --- existing quirks, pinned as-is -------------------------------------------


def test_rssi_hop_start_relay_node_fall_back_only_when_the_line_omits_them(srv):
    """extract_rssi()/extract_hop_start()/extract_relay_node() return STRINGS
    ("-80", "0", ...) - a present "0" is a non-empty, truthy string, so the
    `x or old.get(...)` precedence only ever falls back on a genuine
    omission (no regex match -> None), never on a real zero value."""
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Old", "rssi": "-99", "hop_start": "5", "relay_node": "10"}

    srv.process_nodeinfo(_block(id=REMOTE, longName="Old", rxRssi=0, hopStart=0, relayNode=0))
    assert (srv.nodes[REMOTE]["rssi"], srv.nodes[REMOTE]["hop_start"], srv.nodes[REMOTE]["relay_node"]) == ("0", "0", "0")

    srv.process_nodeinfo(_block(id=REMOTE, longName="Old"))  # this line mentions none of the three
    assert (srv.nodes[REMOTE]["rssi"], srv.nodes[REMOTE]["hop_start"], srv.nodes[REMOTE]["relay_node"]) == ("0", "0", "0")


def test_known_node_info_overrides_the_packets_own_short_name_and_hw_model(srv, monkeypatch):
    monkeypatch.setitem(srv.KNOWN_NODE_INFO, REMOTE, {"short_name": "OVERRIDE", "hw_model": "TBEAM"})

    srv.process_nodeinfo(_block(id=REMOTE, longName="Test", shortName="PKT", hwModel="RAK4631"))

    node = srv.nodes[REMOTE]
    assert (node["short_name"], node["hw_model"]) == ("OVERRIDE", "TBEAM")


def test_known_nodes_name_overrides_the_packets_own_long_name(srv, monkeypatch):
    monkeypatch.setitem(srv.KNOWN_NODES, REMOTE, "Pinned Name")

    srv.process_nodeinfo(_block(id=REMOTE, longName="Packet Name"))

    assert srv.nodes[REMOTE]["name"] == "Pinned Name"


def test_the_name_is_not_stable_across_a_nodeinfo_with_no_name_fields(srv):
    """Unlike the text path's update_node()/_update_node_from_received_text()
    (which explicitly does `old.get("name") or name`), process_nodeinfo()'s
    own name precedence has no old-name fallback at all: with neither
    KNOWN_NODES nor a long/short name in the packet, the name RESETS to the
    generic friendly placeholder even if the node already had a real name.
    A real, pre-existing quirk - preserved exactly, not silently fixed here."""
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Already Named"}

    srv.process_nodeinfo(_block(id=REMOTE, rxRssi=-70))

    assert srv.nodes[REMOTE]["name"] == "Meshtastic 65f0"


def test_position_is_never_touched_by_this_path(srv):
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Old", "position": {"latitude": 1.0, "longitude": 2.0}}

    srv.process_nodeinfo(_block(id=REMOTE, longName="Old"))

    assert srv.nodes[REMOTE]["position"] == {"latitude": 1.0, "longitude": 2.0}


def test_role_defaults_to_client_when_never_set(srv):
    srv.process_nodeinfo(_block(id=REMOTE, longName="Test"))

    assert srv.nodes[REMOTE]["role"] == "CLIENT"


def test_missing_short_name_falls_back_to_the_last_four_hex_digits(srv):
    srv.process_nodeinfo(_block(id=REMOTE, longName="Test"))

    assert srv.nodes[REMOTE]["short_name"] == REMOTE[-4:]


def test_repeated_nodeinfo_refreshes_last_seen_and_last_time(srv):
    srv.process_nodeinfo(_block(id=REMOTE, longName="Test"))
    first_seen = srv.nodes[REMOTE]["last_seen"]

    srv.process_nodeinfo(_block(id=REMOTE, longName="Test", rxRssi=-50))

    assert srv.nodes[REMOTE]["last_seen"] >= first_seen
    assert srv.nodes[REMOTE]["rssi"] == "-50"


# --- apply_node_telemetry() ---------------------------------------------------


@pytest.fixture
def telemetry_env(server_module, monkeypatch):
    server_module.nodes.clear()
    monkeypatch.setattr(server_module.telemetry, "add_node_telemetry_record", lambda *a, **k: None)
    yield server_module
    server_module.nodes.clear()


def test_apply_node_telemetry_creates_a_node_and_stores_device_metrics(telemetry_env):
    updated = telemetry_env.apply_node_telemetry(REMOTE, {
        "battery_level": 80, "voltage": 3.9, "channel_utilization": 1.2, "air_util_tx": 0.5, "uptime_seconds": 1000,
    }, source="tcp")

    assert updated is True
    node = telemetry_env.nodes[REMOTE]
    assert node["device_metrics"] == {
        "battery_level": 80, "voltage": 3.9, "channel_utilization": 1.2, "air_util_tx": 0.5,
        "uptime_seconds": 1000, "updated": pytest.approx(node["device_metrics"]["updated"]), "source": "tcp",
    }
    assert node["telemetry_source"] == "tcp"
    assert node["battery_level"] == 80 and node["voltage"] == 3.9


def test_apply_node_telemetry_computes_power_from_voltage_and_current(telemetry_env):
    telemetry_env.apply_node_telemetry(REMOTE, {"voltage": 5.0, "current": 0.5}, source="tcp")

    assert telemetry_env.nodes[REMOTE]["power"] == pytest.approx(2.5)


def test_apply_node_telemetry_stores_environment_metrics(telemetry_env):
    telemetry_env.apply_node_telemetry(REMOTE, {"temperature": 21.5, "humidity": 40.0, "pressure": 1013.0}, source="tcp")

    env = telemetry_env.nodes[REMOTE]["environment_metrics"]
    assert (env["temperature"], env["humidity"], env["pressure"]) == (21.5, 40.0, 1013.0)
    assert env["source"] == "tcp"


def test_apply_node_telemetry_with_no_node_id_or_empty_values_does_nothing(telemetry_env):
    assert telemetry_env.apply_node_telemetry("", {"voltage": 1}, source="tcp") is False
    assert telemetry_env.apply_node_telemetry(REMOTE, {}, source="tcp") is False
    assert REMOTE not in telemetry_env.nodes


def test_apply_node_telemetry_only_writes_fields_that_are_present(telemetry_env):
    telemetry_env.apply_node_telemetry(REMOTE, {"voltage": 3.9}, source="tcp")
    telemetry_env.apply_node_telemetry(REMOTE, {"battery_level": 80}, source="tcp")

    node = telemetry_env.nodes[REMOTE]
    assert node["voltage"] == 3.9 and node["battery_level"] == 80


def test_apply_node_telemetry_records_history_for_a_remote_node_but_not_local(telemetry_env, monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry_env.telemetry, "add_node_telemetry_record", lambda node_id, values, source: calls.append(node_id))

    telemetry_env.apply_node_telemetry(REMOTE, {"voltage": 3.9}, source="tcp")
    telemetry_env.apply_node_telemetry(telemetry_env.LOCAL_NODE_ID, {"voltage": 3.9}, source="tcp")

    assert calls == [REMOTE]
