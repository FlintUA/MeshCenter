"""Tests for system/cpu_history.py - extracted from server.py's old "CPU
USAGE HISTORY" block (no server.py import needed; this module has no
hardware/CLI dependency of its own beyond /proc, which is real and present
on the Linux CI runner this suite actually executes on).

cpu_history/cpu_history_lock/_cpu_prev_total/_cpu_prev_idle/_cpu_current_usage
are shared module-level state (matching the original server.py globals this
was extracted from) - the autouse fixture below resets them around every
test so tests can't leak state into each other.
"""

import json
import time

import pytest

import system.cpu_history as cpu_history_module


@pytest.fixture(autouse=True)
def _reset_cpu_history_state():
    def _reset():
        with cpu_history_module.cpu_history_lock:
            cpu_history_module.cpu_history.clear()
        cpu_history_module._cpu_prev_total = None
        cpu_history_module._cpu_prev_idle = None
        cpu_history_module._cpu_current_usage = 0.0

    _reset()
    yield
    _reset()


# ---------------- downsample_cpu_records() ----------------

def test_downsample_empty_list_returns_empty():
    assert cpu_history_module.downsample_cpu_records([], 10) == []


def test_downsample_shorter_than_max_points_returned_unchanged():
    records = [{"timestamp": i, "usage": 10.0} for i in range(5)]
    result = cpu_history_module.downsample_cpu_records(records, 10)
    assert result == records


def test_downsample_longer_than_max_points_averages_within_buckets():
    # 10 records into 5 buckets -> 2 records per bucket.
    records = [{"timestamp": i, "usage": float(i)} for i in range(10)]
    result = cpu_history_module.downsample_cpu_records(records, 5)
    assert len(result) == 5
    # Bucket 0 = records[0:2] (usage 0.0, 1.0) -> average 0.5, timestamp of
    # the bucket's last record (see downsample_cpu_records()'s own
    # bucket[-1] choice).
    assert result[0]["usage"] == 0.5
    assert result[0]["timestamp"] == 1
    # Bucket 4 = records[8:10] (usage 8.0, 9.0) -> average 8.5.
    assert result[4]["usage"] == 8.5
    assert result[4]["timestamp"] == 9


# ---------------- load_cpu_history() / save_cpu_history() ----------------

def test_save_then_load_round_trips_records(tmp_path):
    history_file = str(tmp_path / "cpu_history.json")
    now = time.time()
    with cpu_history_module.cpu_history_lock:
        cpu_history_module.cpu_history.append({"timestamp": now, "usage": 42.5})
        cpu_history_module.cpu_history.append({"timestamp": now - 10, "usage": 10.0})

    cpu_history_module.save_cpu_history(history_file)

    with cpu_history_module.cpu_history_lock:
        cpu_history_module.cpu_history.clear()

    cpu_history_module.load_cpu_history(history_file)

    with cpu_history_module.cpu_history_lock:
        loaded = list(cpu_history_module.cpu_history)
    assert sorted(item["usage"] for item in loaded) == [10.0, 42.5]


def test_load_cpu_history_filters_out_records_older_than_retention(tmp_path):
    history_file = tmp_path / "cpu_history.json"
    now = time.time()
    stale_ts = now - cpu_history_module.CPU_HISTORY_RETENTION - 3600
    history_file.write_text(
        json.dumps({"cpu": [
            {"timestamp": stale_ts, "usage": 99.0},
            {"timestamp": now, "usage": 5.0},
        ]}),
        encoding="utf-8",
    )

    cpu_history_module.load_cpu_history(str(history_file))

    with cpu_history_module.cpu_history_lock:
        loaded = list(cpu_history_module.cpu_history)
    assert len(loaded) == 1
    assert loaded[0]["usage"] == 5.0


def test_load_cpu_history_missing_file_leaves_history_empty(tmp_path):
    history_file = str(tmp_path / "does_not_exist.json")
    cpu_history_module.load_cpu_history(history_file)
    with cpu_history_module.cpu_history_lock:
        assert list(cpu_history_module.cpu_history) == []


# ---------------- read_cpu_times() / read_memory_percent() / read_cpu_temperature() ----------------
# Real /proc access, not mocked - CI runs on Linux, so this exercises the
# actual code path. Only asserts on type/range, not exact values, since
# those are host-dependent.

def test_read_cpu_times_returns_a_valid_pair_or_both_none():
    total, idle = cpu_history_module.read_cpu_times()
    if total is None:
        assert idle is None
    else:
        assert isinstance(total, int) and total >= 0
        assert isinstance(idle, int) and idle >= 0


def test_read_memory_percent_returns_a_percentage_or_none():
    result = cpu_history_module.read_memory_percent()
    assert result is None or (isinstance(result, float) and 0.0 <= result <= 100.0)


def test_read_cpu_temperature_returns_a_number_or_none():
    result = cpu_history_module.read_cpu_temperature()
    assert result is None or isinstance(result, float)


# ---------------- get_current_usage() ----------------

def test_get_current_usage_reflects_current_module_state():
    assert cpu_history_module.get_current_usage() == 0.0
    cpu_history_module._cpu_current_usage = 37.5
    assert cpu_history_module.get_current_usage() == 37.5
