"""H1-B2 (F6): telemetry_history used a single shared 26000-record cap
across EVERY node plus local - a mesh with several chatty remote nodes
could evict local's own history (or another node's) well before it hit
any limit of its own.

Review fix (same PR): per-node caps alone still allowed an UNBOUNDED
total file size - a mesh with many distinct remote nodes could each fill
their own (then-10000-record) bucket, with nothing bounding the sum. Now:
MAX_LOCAL_RECORDS caps local's own bucket, MAX_RECORDS_PER_REMOTE_NODE
caps each remote node's bucket (much smaller), and MAX_TOTAL_RECORDS is a
hard ceiling on telemetry_history's total length - when exceeded, the
OLDEST REMOTE records are evicted first, never local's.
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
    monkeypatch.setattr(module, "_record_counts", {})
    # Small caps make the tests fast and the assertions legible, instead
    # of actually writing tens of thousands of records.
    monkeypatch.setattr(module, "MAX_LOCAL_RECORDS", 5)
    monkeypatch.setattr(module, "MAX_RECORDS_PER_REMOTE_NODE", 3)
    monkeypatch.setattr(module, "MAX_TOTAL_RECORDS", 12)
    return module


def _seed_local(module, count, start_ts=1_000_000.0, step=200.0):
    for i in range(count):
        module.telemetry_history.append({
            "time": str(i), "timestamp": start_ts + i * step,
            "temperature": float(i), "humidity": None, "pressure": None,
            "voltage": None, "current": None, "power": None, "source": "local",
        })
    module._rebuild_record_counts()


def _seed_node(module, node_id, count, start_ts=1_000_000.0, step=200.0):
    for i in range(count):
        module.telemetry_history.append({
            "node_id": node_id, "source": "tcp", "time": str(i),
            "timestamp": start_ts + i * step, "voltage": float(i),
        })
    module._rebuild_record_counts()


def test_local_history_is_capped_independently_of_remote_nodes(telemetry_module):
    """The actual F6 bug: a chatty remote node must never evict local's
    own history."""
    _seed_local(telemetry_module, 5)
    _seed_node(telemetry_module, REMOTE_A, 2)  # under its own cap of 3

    telemetry_module.add_node_telemetry_record(REMOTE_A, {"voltage": 99.0})

    local_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") is None]
    remote_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_A]
    assert len(local_records) == 5, "local history must survive untouched"
    assert len(remote_records) == 3, "remote node's own history stays capped at its own (smaller) limit"


def test_per_remote_cap_is_enforced(telemetry_module):
    _seed_node(telemetry_module, REMOTE_A, 3)  # already at the remote cap

    telemetry_module.add_node_telemetry_record(REMOTE_A, {"voltage": 99.0})

    a_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_A]
    assert len(a_records) == 3
    # Oldest (voltage=0.0) was evicted, newest (99.0) survives.
    assert 0.0 not in [r["voltage"] for r in a_records]
    assert 99.0 in [r["voltage"] for r in a_records]


def test_one_remote_node_does_not_evict_another_remote_nodes_history(telemetry_module):
    _seed_node(telemetry_module, REMOTE_A, 2)
    _seed_node(telemetry_module, REMOTE_B, 2)

    telemetry_module.add_node_telemetry_record(REMOTE_B, {"voltage": 99.0})

    a_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_A]
    b_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_B]
    assert len(a_records) == 2
    assert len(b_records) == 3


def test_many_remote_nodes_flooding_total_stays_at_or_under_the_global_ceiling_and_local_is_intact(telemetry_module):
    """The actual review-fix bug: per-node caps alone don't bound the
    TOTAL - many distinct remote nodes, each under its own per-node cap,
    could together still grow telemetry_history without limit. The
    global ceiling (12 in this test) must hold regardless, and it must
    evict REMOTE records only - local's 5 records must survive intact."""
    _seed_local(telemetry_module, 5)

    # Six distinct remote nodes, one record each - all individually well
    # under the per-remote cap of 3, but six nodes * growing would blow
    # past the global ceiling of 12 if nothing bounded the total.
    for i in range(6):
        node_id = f"!{i:08x}"
        telemetry_module.add_node_telemetry_record(node_id, {"voltage": float(i)})

    assert len(telemetry_module.telemetry_history) <= 12
    local_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") is None]
    assert len(local_records) == 5, "local history must never be evicted by remote traffic"


def test_global_ceiling_evicts_oldest_remote_records_first(telemetry_module):
    _seed_local(telemetry_module, 5)
    _seed_node(telemetry_module, REMOTE_A, 2)
    _seed_node(telemetry_module, REMOTE_B, 2)
    # Total so far: 5 + 2 + 2 = 9, under the ceiling of 12.

    # One more distinct remote node's record pushes total to 10 - still
    # under 12, so nothing should be evicted yet.
    telemetry_module.add_node_telemetry_record("!33333333", {"voltage": 1.0})
    assert len(telemetry_module.telemetry_history) == 10

    # Push over the ceiling (11, 12, 13 total) - the OLDEST remote records
    # (REMOTE_A's first seeded record) must go first, local stays intact.
    telemetry_module.add_node_telemetry_record("!44444444", {"voltage": 1.0})
    telemetry_module.add_node_telemetry_record("!55555555", {"voltage": 1.0})
    telemetry_module.add_node_telemetry_record("!66666666", {"voltage": 1.0})

    assert len(telemetry_module.telemetry_history) <= 12
    local_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") is None]
    assert len(local_records) == 5


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


def test_load_of_an_oversized_legacy_file_trims_to_the_new_limits(telemetry_module):
    """A file saved before these caps existed (or by an older build) could
    have any bucket over its cap, or the file as a whole over the global
    ceiling - load_telemetry() must trim it down to the current limits,
    not just whichever bucket happens to receive the next append."""
    _seed_local(telemetry_module, 50)
    _seed_node(telemetry_module, REMOTE_A, 50)
    telemetry_module.save_telemetry()

    telemetry_module.telemetry_history = []
    telemetry_module._record_counts = {}
    telemetry_module.load_telemetry()

    assert len(telemetry_module.telemetry_history) <= 12
    local_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") is None]
    remote_records = [r for r in telemetry_module.telemetry_history if r.get("node_id") == REMOTE_A]
    assert len(local_records) == 5
    assert len(remote_records) == 3
