"""Tests for telemetry/telemetry.py's H1-C5 debounced flush.

Before this change, add_telemetry_record()/add_node_telemetry_record()
called save_telemetry() synchronously on every accepted record - rewriting
the ENTIRE history (indent=2, fsync'd) under _telemetry_lock every time.
Measured on dev (a Pi Zero 2W) at ~5s per save with a 40k-record history,
blocking whichever ingest thread (serial parser or TCP inbound worker)
triggered it for the whole duration.

Fix: ingest now only flips a dirty flag (mark_dirty(), via the internal
_mark_dirty()); a background thread (telemetry_flush_worker(), started
once from server.py's start_runtime()) writes at most once every
TELEMETRY_FLUSH_INTERVAL_S while dirty. flush_telemetry() takes its
snapshot and clears the dirty flag under the lock, then does the actual
json/disk write OUTSIDE it, so a slow write never blocks a concurrent
ingest call. A clean shutdown (shutdown_telemetry(), registered via
atexit) performs one final flush so a graceful restart never loses the
debounce window's worth of pending history the way a crash would.

Accepted trade-off, documented in README: a crash (not a clean shutdown)
can lose up to TELEMETRY_FLUSH_INTERVAL_S seconds of telemetry HISTORY.
Live values (telemetry_current, node metrics) are unaffected - they are
updated independently of this history buffer.
"""

import threading
import time

import pytest


@pytest.fixture
def telemetry_module(server_module, tmp_path, monkeypatch):
    """Same isolation pattern as test_telemetry_locking.py's own fixture:
    a fresh file path, fresh history/config, and fresh lock/event objects
    per test so nothing leaks across tests sharing this session-scoped
    module."""
    import telemetry.telemetry as module

    module.configure_storage(str(tmp_path / "telemetry_history.json"))
    monkeypatch.setattr(module, "telemetry_history", [])
    monkeypatch.setattr(module, "telemetry_config", {"interval": 300, "enabled": True})
    monkeypatch.setattr(module, "telemetry_last_save_time", 0)
    monkeypatch.setattr(module, "_telemetry_lock", threading.RLock())
    monkeypatch.setattr(module, "_telemetry_dirty", False)
    monkeypatch.setattr(module, "_flush_now_event", threading.Event())
    monkeypatch.setattr(module, "_flush_worker_stop", threading.Event())
    return module


def _spy_on_writes(telemetry_module, monkeypatch):
    """Replaces safe_write_json with a call-counting wrapper that still
    does the real write, so tests can assert "0 writes" / "1 write"
    without caring about the file's actual on-disk content."""
    calls = []
    real_write = telemetry_module.safe_write_json

    def _spy(path, data, indent=2):
        calls.append((path, data, indent))
        return real_write(path, data, indent=indent)

    monkeypatch.setattr(telemetry_module, "safe_write_json", _spy)
    return calls


# ---------------------------------------------------------------------------
# N adds -> 0 writes until the tick
# ---------------------------------------------------------------------------

def test_many_adds_cause_zero_writes_before_a_flush(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    for i in range(50):
        telemetry_module.add_node_telemetry_record(f"!{i:08x}", {"voltage": 4.0})

    assert calls == []
    assert telemetry_module._telemetry_dirty is True
    assert len(telemetry_module.telemetry_history) == 50


def test_add_telemetry_record_also_only_marks_dirty(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    assert telemetry_module.add_telemetry_record(21.5, 40.0, 1013.0, None, None) is True

    assert calls == []
    assert telemetry_module._telemetry_dirty is True


# ---------------------------------------------------------------------------
# A flush ("the tick") writes the snapshot
# ---------------------------------------------------------------------------

def test_flush_writes_the_pending_snapshot(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})
    telemetry_module.add_node_telemetry_record("!bbbbbbbb", {"voltage": 3.9})
    assert calls == []

    result = telemetry_module.flush_telemetry()

    assert result is True
    assert len(calls) == 1
    written_data = calls[0][1]
    assert len(written_data["history"]) == 2
    assert telemetry_module._telemetry_dirty is False
    # Compact encoding, not the default pretty-print - see json_store's own test.
    assert calls[0][2] is None


def test_flush_is_a_noop_when_nothing_is_dirty(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    assert telemetry_module.flush_telemetry() is False
    assert calls == []


def test_flush_force_writes_even_when_not_dirty(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    assert telemetry_module.flush_telemetry(force=True) is True
    assert len(calls) == 1


def test_a_second_flush_with_nothing_new_is_a_noop(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})
    telemetry_module.flush_telemetry()
    assert len(calls) == 1

    assert telemetry_module.flush_telemetry() is False
    assert len(calls) == 1  # no new write - nothing was dirty


# ---------------------------------------------------------------------------
# Shutdown flush
# ---------------------------------------------------------------------------

def test_shutdown_telemetry_flushes_pending_data(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})
    assert calls == []

    telemetry_module.shutdown_telemetry()

    assert len(calls) == 1
    assert telemetry_module._telemetry_dirty is False


def test_shutdown_telemetry_stops_the_flush_worker_loop(telemetry_module):
    assert not telemetry_module._flush_worker_stop.is_set()
    telemetry_module.shutdown_telemetry()
    assert telemetry_module._flush_worker_stop.is_set()


def test_shutdown_telemetry_is_a_noop_on_nothing_pending(telemetry_module, monkeypatch):
    """Nothing dirty -> shutdown must not force a spurious write."""
    calls = _spy_on_writes(telemetry_module, monkeypatch)
    telemetry_module.shutdown_telemetry()
    assert calls == []


def test_telemetry_flush_worker_exits_promptly_on_stop(telemetry_module):
    """The worker loop must not be stuck waiting out the full
    TELEMETRY_FLUSH_INTERVAL_S once asked to stop - request_flush()/
    shutdown_telemetry() set _flush_now_event precisely to wake it early."""
    worker_thread = threading.Thread(target=telemetry_module.telemetry_flush_worker)
    worker_thread.start()
    try:
        telemetry_module.shutdown_telemetry()
        worker_thread.join(timeout=2)
        assert not worker_thread.is_alive(), "flush worker did not exit promptly on shutdown"
    finally:
        telemetry_module._flush_worker_stop.set()
        telemetry_module._flush_now_event.set()
        worker_thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Ingest is never blocked by a slow write
# ---------------------------------------------------------------------------

def test_ingest_is_not_blocked_by_a_slow_flush_write(telemetry_module, monkeypatch):
    """A monkeypatched safe_write_json that sleeps, per the task spec -
    add_node_telemetry_record() must return quickly even while another
    thread's flush is in the middle of a slow write."""
    real_write = telemetry_module.safe_write_json

    def _slow_write(path, data, indent=2):
        time.sleep(1.0)
        return real_write(path, data, indent=indent)

    monkeypatch.setattr(telemetry_module, "safe_write_json", _slow_write)

    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})

    flush_thread = threading.Thread(target=telemetry_module.flush_telemetry, kwargs={"force": True})
    flush_thread.start()
    try:
        time.sleep(0.1)  # let the flush thread get into its slow write

        started = time.monotonic()
        telemetry_module.add_node_telemetry_record("!bbbbbbbb", {"voltage": 3.9})
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"ingest took {elapsed:.2f}s - it must not wait on the slow write"
    finally:
        flush_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Dirty flag survives a failed write
# ---------------------------------------------------------------------------

def test_dirty_flag_survives_a_failed_write_for_the_next_tick_to_retry(telemetry_module, monkeypatch):
    monkeypatch.setattr(telemetry_module, "safe_write_json", lambda *a, **k: False)

    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})
    result = telemetry_module.flush_telemetry()

    assert result is False
    assert telemetry_module._telemetry_dirty is True, (
        "a failed write must leave the dirty flag set so the next tick retries"
    )


def test_a_retry_after_a_failed_write_succeeds_once_writes_work_again(telemetry_module, monkeypatch):
    attempts = {"n": 0}
    real_write = telemetry_module.safe_write_json

    def _fail_once(path, data, indent=2):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False
        return real_write(path, data, indent=indent)

    monkeypatch.setattr(telemetry_module, "safe_write_json", _fail_once)

    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})

    assert telemetry_module.flush_telemetry() is False
    assert telemetry_module._telemetry_dirty is True

    assert telemetry_module.flush_telemetry() is True
    assert telemetry_module._telemetry_dirty is False
    assert attempts["n"] == 2


# ---------------------------------------------------------------------------
# request_flush(): explicit admin actions (config/enabled changes)
# ---------------------------------------------------------------------------

def test_request_flush_wakes_the_event_without_writing_itself(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    telemetry_module.request_flush()

    assert calls == [], "request_flush() without wait=True must not write on the calling thread"
    assert telemetry_module._flush_now_event.is_set()


def test_request_flush_wait_true_flushes_synchronously(telemetry_module, monkeypatch):
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})
    assert calls == []

    telemetry_module.request_flush(wait=True)

    assert len(calls) == 1


def test_save_telemetry_is_an_immediate_forced_flush(telemetry_module, monkeypatch):
    """save_telemetry() keeps its old name/contract (used by
    /api/telemetry/config's explicit interval/enabled changes, and by
    load_telemetry()'s own startup saves) - an immediate, synchronous
    write regardless of the dirty flag."""
    calls = _spy_on_writes(telemetry_module, monkeypatch)

    telemetry_module.save_telemetry()

    assert len(calls) == 1
    assert calls[0][2] is None  # compact encoding, same as the debounced path
