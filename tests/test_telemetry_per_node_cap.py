"""H1-B2 (F6): telemetry_history used a single shared 26000-record cap
across EVERY node plus local - a mesh with several chatty remote nodes
could evict local's own history (or another node's) well before it hit
any limit of its own. MAX_RECORDS_PER_NODE now caps each node/local
bucket independently.
"""

import threading

import pytest


REMOTE_A = "!11111111"
REMOTE_B = "!22222222"


@pytest.fixture
def telemetry_module(server_module, tmp_path, monkeypatch):
    import telemetry.telemetry as module

    module.configure_storage(str(tmp_path / "telemetry_history.json"))
    monkeypatch.setattr(module, "telemetry_history", [])
    monkeypatch.setattr(module, "telemetry_config", {"interval": 120, "enabled": True})
    monkeypatch.setattr(module, "telemetry_last_save_time", 0)
    monkeypatch.setattr(module, "_telemetry_lock", threading.RLock())
    # A small cap makes the test fast and the assertions legible, instead
    # of actually writing MAX_RECORDS_PER_NODE (10000) records.
    monkeypatch.setattr(module, "MAX_RECORDS_PER_NODE", 5)
    return module


def _seed_local(module, count, start_ts=1_000_000.0, step=200.0):
    for i in range(count):
        module.telemetry_history.append({
            "time": str(i), "timestamp": start_ts + i * step,
            "temperature": float(i), "humidity": None, "pressure": None,
            "voltage": None, "current": None, "power": None, "source": "local",
        })


def _seed_node(module, node_id, count, start_ts=1_000_000.0, step=200.0):
    for i in range(count):
        module.telemetry_history.append({
            "node_id": node_id, "source": "tcp", "time": str(i),
            "timestamp": start_ts + i * step, "voltage": float(i),
        })


def test_local_history_is_capped_independently_of_remote_nodes(telemetry_module):
    """The actual F6 bug: a chatty remote node must never evict local's
    own history."""
    _seed_local(telemetry_module, 5)
    _seed_node(telemetry_module, REMOTE_A, 50)  # way over the test cap of 5

    telemetry_module.add_node_telemetry_record(REMOTE_A, {"voltage": 99.0})

    local_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") is None]
    remote_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_A]
    assert len(local_records) == 5, "local history must survive untouched"
    assert len(remote_records) == 5, "remote node's own history stays capped at its own limit"


def test_one_remote_node_does_not_evict_another_remote_nodes_history(telemetry_module):
    _seed_node(telemetry_module, REMOTE_A, 5)
    _seed_node(telemetry_module, REMOTE_B, 50)

    telemetry_module.add_node_telemetry_record(REMOTE_B, {"voltage": 99.0})

    a_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_A]
    b_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_B]
    assert len(a_records) == 5
    assert len(b_records) == 5


def test_trim_removes_the_oldest_records_for_that_node_only(telemetry_module):
    _seed_local(telemetry_module, 5, start_ts=1_000_000.0, step=200.0)

    # A 6th, newest local record pushes the bucket over its cap of 5.
    saved = telemetry_module.add_telemetry_record(temp=99.0, humidity=None, pressure=None, voltage=None, current=None)

    assert saved is True
    local_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") is None]
    assert len(local_records) == 5
    # The OLDEST (temperature=0.0, the very first seeded record) is gone;
    # everything newer, including the brand-new one, survives.
    temps = sorted(r["temperature"] for r in local_records)
    assert temps[0] == 1.0  # temperature=0.0 was evicted
    assert temps[-1] == 99.0


def test_load_telemetry_trims_every_oversized_bucket_from_disk(telemetry_module):
    _seed_local(telemetry_module, 50)
    _seed_node(telemetry_module, REMOTE_A, 50)
    telemetry_module.save_telemetry()

    # Reload from disk - load_telemetry() must trim EVERY bucket that's
    # over the cap, not just whichever one happens to get a new record
    # next (the old code only trimmed the single shared list on load too,
    # but by total count, not per bucket).
    telemetry_module.telemetry_history = []
    telemetry_module.load_telemetry()

    local_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") is None]
    remote_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_A]
    assert len(local_records) == 5
    assert len(remote_records) == 5
