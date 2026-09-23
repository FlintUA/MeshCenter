"""Tests for meshsrv/radio_connections.py - the pure read/write domain API
for a radio record's `connections` dict (Radio Profiles & Connections
Model, PR 1). No `meshtastic` import needed, no file I/O - every function
is `radio: Mapping in, dict out`.
"""
from meshsrv.radio_connections import (
    connection_descriptor,
    get_connection,
    get_connections,
    record_success,
    remember_connection,
    set_preferred_transport,
)
from meshsrv.radio_transport import ConnectionType


# ---------------------------------------------------------------------------
# get_connections() / get_connection()
# ---------------------------------------------------------------------------

def test_get_connections_synthesizes_one_entry_from_a_legacy_singular_record():
    legacy = {"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}

    connections = get_connections(legacy)

    assert connections == {"tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}}}


def test_get_connections_returns_an_already_multi_transport_dict_as_is():
    radio = {
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {
            "tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}, "last_successful_at": "2026-09-23T12:00:00+00:00"},
            "serial": {"endpoint": {"port": "/dev/ttyACM0"}},
        },
    }

    connections = get_connections(radio)

    assert set(connections.keys()) == {"tcp", "serial"}
    assert connections["tcp"]["last_successful_at"] == "2026-09-23T12:00:00+00:00"


def test_get_connection_returns_none_for_a_never_remembered_transport():
    radio = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}

    assert get_connection(radio, "bluetooth") is None


def test_get_connection_is_case_insensitive_on_transport_name():
    radio = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}

    assert get_connection(radio, "TCP") == {"endpoint": {"host": "192.168.2.34", "port": 4403}}


# ---------------------------------------------------------------------------
# remember_connection()
# ---------------------------------------------------------------------------

def test_remember_connection_adds_a_new_transport_without_dropping_the_existing_one():
    radio = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}

    updated = remember_connection(radio, "serial", {"port": "/dev/ttyACM0"})

    assert updated["connections"]["tcp"] == {"endpoint": {"host": "192.168.2.34", "port": 4403}}
    assert updated["connections"]["serial"] == {"endpoint": {"port": "/dev/ttyACM0"}}


def test_remember_connection_does_not_touch_preferred_or_last_successful_transport():
    radio = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}

    updated = remember_connection(radio, "serial", {"port": "/dev/ttyACM0"})

    # normalize_radio_record()'s own defaulting still applies (both
    # default to the legacy singular transport, "tcp" here) - the point
    # is remember_connection() didn't change them to "serial".
    assert updated["preferred_transport"] == "tcp"
    assert updated["last_successful_transport"] == "tcp"


def test_remember_connection_with_an_unchanged_endpoint_preserves_last_successful_at():
    radio = {
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {
            "tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}, "last_successful_at": "2026-09-23T12:00:00+00:00"}
        },
    }

    updated = remember_connection(radio, "tcp", {"host": "192.168.2.34", "port": 4403})

    assert updated["connections"]["tcp"]["last_successful_at"] == "2026-09-23T12:00:00+00:00"


def test_remember_connection_with_a_changed_endpoint_clears_the_stale_last_successful_at():
    radio = {
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {
            "tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}, "last_successful_at": "2026-09-23T12:00:00+00:00"}
        },
    }

    # Radio moved to a new IP - the old success timestamp no longer
    # means anything for this (different) address.
    updated = remember_connection(radio, "tcp", {"host": "192.168.2.99", "port": 4403})

    assert "last_successful_at" not in updated["connections"]["tcp"]


def test_remember_connection_does_not_mutate_its_input():
    radio = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}
    original = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}

    remember_connection(radio, "serial", {"port": "/dev/ttyACM0"})

    assert radio == original


# ---------------------------------------------------------------------------
# set_preferred_transport()
# ---------------------------------------------------------------------------

def test_set_preferred_transport_mirrors_the_remembered_endpoint_into_legacy_fields():
    radio = {
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {
            "tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}},
            "serial": {"endpoint": {"port": "/dev/ttyACM0"}},
        },
    }

    updated = set_preferred_transport(radio, "serial")

    assert updated["preferred_transport"] == "serial"
    assert updated["transport"] == "serial"
    assert updated["endpoint"] == {"port": "/dev/ttyACM0"}
    # The connections dict itself is untouched.
    assert updated["connections"]["tcp"] == {"endpoint": {"host": "192.168.2.34", "port": 4403}}


def test_set_preferred_transport_for_a_never_remembered_transport_uses_a_default_endpoint_shape():
    radio = {"transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}}

    updated = set_preferred_transport(radio, "tcp")

    assert updated["preferred_transport"] == "tcp"
    assert updated["transport"] == "tcp"
    assert updated["endpoint"] == {"host": "", "port": 4403}


# ---------------------------------------------------------------------------
# record_success()
# ---------------------------------------------------------------------------

def test_record_success_sets_last_successful_at_and_top_level_transport():
    radio = {
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {"tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}}},
    }

    updated = record_success(radio, "tcp", when="2026-09-23T13:00:00+00:00")

    assert updated["connections"]["tcp"]["last_successful_at"] == "2026-09-23T13:00:00+00:00"
    assert updated["last_successful_transport"] == "tcp"


def test_record_success_creates_a_missing_connection_entry_instead_of_dropping_data():
    radio = {"transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}

    # Success reported for "bluetooth" even though nothing ever called
    # remember_connection() for it - must not silently no-op.
    updated = record_success(radio, "bluetooth", when="2026-09-23T13:00:00+00:00")

    assert updated["connections"]["bluetooth"]["last_successful_at"] == "2026-09-23T13:00:00+00:00"
    assert updated["last_successful_transport"] == "bluetooth"
    # The pre-existing tcp connection is still there.
    assert updated["connections"]["tcp"] == {"endpoint": {"host": "192.168.2.34", "port": 4403}}


def test_record_success_without_an_explicit_when_uses_a_real_timestamp():
    radio = {"transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}}

    updated = record_success(radio, "serial")

    stamp = updated["connections"]["serial"]["last_successful_at"]
    assert isinstance(stamp, str) and len(stamp) > 0


# ---------------------------------------------------------------------------
# connection_descriptor()
# ---------------------------------------------------------------------------

def test_connection_descriptor_for_tcp():
    radio = {"connections": {"tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}}}}

    descriptor = connection_descriptor(radio, "tcp")

    assert descriptor.type == ConnectionType.TCP
    assert descriptor.address == "192.168.2.34:4403"


def test_connection_descriptor_for_serial():
    radio = {"connections": {"serial": {"endpoint": {"port": "/dev/ttyACM0"}}}}

    descriptor = connection_descriptor(radio, "serial")

    assert descriptor.type == ConnectionType.SERIAL
    assert descriptor.address == "/dev/ttyACM0"


def test_connection_descriptor_for_bluetooth():
    radio = {"connections": {"bluetooth": {"endpoint": {"address": "3C:DC:75:6F:99:61", "label": "FLT2_9960"}}}}

    descriptor = connection_descriptor(radio, "bluetooth")

    assert descriptor.type == ConnectionType.BLUETOOTH
    assert descriptor.address == "3C:DC:75:6F:99:61"
    assert descriptor.label == "FLT2_9960"


def test_connection_descriptor_for_a_never_remembered_transport_uses_default_endpoint_shape():
    radio = {"connections": {"serial": {"endpoint": {"port": "/dev/ttyACM0"}}}}

    descriptor = connection_descriptor(radio, "tcp")

    assert descriptor.type == ConnectionType.TCP
    assert descriptor.address == ":4403"
