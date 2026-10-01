"""H1-A4 (security review, F7): /api/nodes_import used to trust its input
completely - no is_valid_node_id() check, no type/size checks, and it
replaced the whole node record (silently dropping any field outside its
own small whitelist, e.g. telemetry values set by TELEMETRY_APP ingestion
elsewhere). This file covers the hardening: a hostile/malformed node_id is
rejected and counted (not crashed on), an oversized list is refused
outright, wrong-typed fields don't poison the stored node, a NaN/Infinity
position is dropped rather than stored, and a merge import preserves
fields the import request never mentioned.

NaN/Infinity cases POST a raw JSON string via `data=`/`content_type=`
instead of the test client's `json=` convenience kwarg: Python's own
`json.loads` (what Flask's request.get_json() uses) accepts the bare
`NaN`/`Infinity`/`-Infinity` tokens as a non-standard extension - a real
attacker's hand-crafted request body can contain them - but the test
client's own JSON encoder serializing a Python dict with allow_nan=False
would refuse to produce that body in the first place, which would test
the wrong thing (the client encoder's behavior, not the server's).
"""

import json
import math

import pytest


def _csrf_client(server_module):
    client = server_module.app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf_token"] = "test-csrf-token"
    return client


def _import(client, nodes):
    return client.post(
        "/api/nodes_import",
        json={"nodes": nodes},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )


def _import_raw(client, nodes):
    """Like _import(), but serializes with allow_nan=True and posts the
    raw body - see the module docstring for why this is needed for the
    NaN/Infinity cases specifically."""
    body = json.dumps({"nodes": nodes}, allow_nan=True)
    return client.post(
        "/api/nodes_import",
        data=body,
        content_type="application/json",
        headers={"X-CSRF-Token": "test-csrf-token"},
    )


def test_hostile_node_id_is_rejected_and_counted_not_crashed(server_module):
    client = _csrf_client(server_module)

    resp = _import(client, [
        {"node_id": "'; DROP TABLE nodes; --", "name": "Hostile"},
        {"node_id": "../../../etc/passwd", "name": "Hostile2"},
        {"node_id": "!deadbeef", "name": "Real Node"},
    ])

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["imported_count"] == 1
    assert data["rejected_count"] == 2
    assert "!deadbeef" in server_module.nodes
    assert "'; DROP TABLE nodes; --" not in server_module.nodes


def test_oversized_list_is_rejected_with_413(server_module):
    client = _csrf_client(server_module)
    huge_list = [{"node_id": f"!{i:08x}", "name": "N"} for i in range(server_module.MAX_NODES_IMPORT_COUNT + 1)]

    resp = _import(client, huge_list)

    assert resp.status_code == 413
    assert resp.get_json()["error_code"] == "too_many_nodes"
    # Nothing from the oversized payload was imported.
    assert "!00000000" not in server_module.nodes


def test_body_not_a_list_is_rejected(server_module):
    client = _csrf_client(server_module)
    resp = client.post(
        "/api/nodes_import",
        json={"nodes": "not-a-list"},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == "invalid_body"


def test_non_dict_entry_in_list_is_rejected_and_counted(server_module):
    client = _csrf_client(server_module)
    resp = _import(client, ["not-a-dict", 12345, None, {"node_id": "!cafef00d", "name": "Real"}])
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["imported_count"] == 1
    assert data["rejected_count"] == 3


def test_wrong_typed_fields_do_not_poison_the_stored_node(server_module):
    client = _csrf_client(server_module)

    resp = _import(client, [{
        "node_id": "!11223344",
        "name": {"not": "a string"},
        "rssi": "not-a-number",
        "snr": ["also", "not", "a", "number"],
        "short_name": 12345,
    }])

    assert resp.status_code == 200
    node = server_module.nodes["!11223344"]
    # Garbage values never get stored - they fall back to defaults instead
    # of a dict/list/etc. ending up in rssi/snr/name.
    assert isinstance(node["name"], str)
    assert node["rssi"] is None
    assert node["snr"] is None
    assert isinstance(node["short_name"], str)


def test_oversized_name_is_truncated_to_the_meshtastic_limit(server_module):
    client = _csrf_client(server_module)
    long_name = "A" * 500
    long_short = "B" * 500

    resp = _import(client, [{"node_id": "!11223344", "name": long_name, "short_name": long_short}])

    assert resp.status_code == 200
    node = server_module.nodes["!11223344"]
    assert len(node["name"]) == server_module.MAX_IMPORT_LONG_NAME_LEN
    assert len(node["short_name"]) == server_module.MAX_IMPORT_SHORT_NAME_LEN


@pytest.mark.parametrize("bad_position", [
    {"latitude": math.nan, "longitude": 10.0},
    {"latitude": 10.0, "longitude": math.inf},
    {"latitude": -math.inf, "longitude": 10.0},
    {"latitude": 91.0, "longitude": 10.0},  # out of range
    {"latitude": 10.0, "longitude": 181.0},  # out of range
    {"latitude": "not-a-number", "longitude": 10.0},
    {"longitude": 10.0},  # missing latitude
    "not-a-dict",
    None,
])
def test_nan_infinity_or_out_of_range_position_is_dropped_not_stored(server_module, bad_position):
    client = _csrf_client(server_module)

    resp = _import_raw(client, [{"node_id": "!11223344", "name": "N", "position": bad_position}])

    assert resp.status_code == 200
    node = server_module.nodes["!11223344"]
    # Never a non-finite/out-of-range value - either falls back to None or
    # whatever was already stored (there was nothing stored here yet).
    assert node["position"] is None


def test_nan_infinity_rssi_and_snr_are_dropped_not_stored(server_module):
    """Unlike position, rssi/snr have no range check of their own to
    incidentally catch a non-finite value - this exercises
    _finite_float_or_none() directly."""
    client = _csrf_client(server_module)

    resp = _import_raw(client, [{
        "node_id": "!11223344", "name": "N",
        "rssi": float("nan"), "snr": float("inf"),
    }])

    assert resp.status_code == 200
    node = server_module.nodes["!11223344"]
    assert node["rssi"] is None
    assert node["snr"] is None


def test_nan_position_does_not_overwrite_a_previously_good_position(server_module):
    client = _csrf_client(server_module)
    with server_module.state_lock:
        server_module.nodes["!11223344"] = {
            "node_id": "!11223344", "name": "N",
            "position": {"latitude": 50.0, "longitude": 10.0},
        }

    resp = _import_raw(client, [{"node_id": "!11223344", "name": "N", "position": {"latitude": math.nan, "longitude": 10.0}}])

    assert resp.status_code == 200
    node = server_module.nodes["!11223344"]
    # The bad incoming position is dropped - the good stored one survives.
    assert node["position"] == {"latitude": 50.0, "longitude": 10.0}


def test_valid_position_is_stored(server_module):
    client = _csrf_client(server_module)
    resp = _import(client, [{"node_id": "!11223344", "name": "N", "position": {"latitude": 50.5, "longitude": 10.5}}])
    assert resp.status_code == 200
    assert server_module.nodes["!11223344"]["position"] == {"latitude": 50.5, "longitude": 10.5}


def test_merge_preserves_fields_outside_the_import_whitelist(server_module):
    """F7: the old handler replaced the whole node record, silently
    dropping any field not in its own whitelist - e.g. telemetry values
    set directly on the node dict by TELEMETRY_APP ingestion. An import
    that only updates the name must not erase those."""
    client = _csrf_client(server_module)
    with server_module.state_lock:
        server_module.nodes["!11223344"] = {
            "node_id": "!11223344",
            "name": "Old Name",
            "battery_level": 87,
            "voltage": 4.05,
            "channel_utilization": 3.2,
            "air_util_tx": 1.1,
        }

    resp = _import(client, [{"node_id": "!11223344", "name": "New Name"}])

    assert resp.status_code == 200
    node = server_module.nodes["!11223344"]
    assert node["name"] == "New Name"
    assert node["battery_level"] == 87
    assert node["voltage"] == 4.05
    assert node["channel_utilization"] == 3.2
    assert node["air_util_tx"] == 1.1
