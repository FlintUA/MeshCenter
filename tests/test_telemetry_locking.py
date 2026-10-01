"""Tests for telemetry/telemetry.py's own lock (F4.1 PR 2).

F4.0 investigation: save_telemetry() took no lock of its own. Two ingest
threads (the serial --listen parser, the TCP inbound worker) could call
add_telemetry_record()/add_node_telemetry_record() - and therefore
save_telemetry() - concurrently with each other AND with
server.py's get_telemetry_export_records(), which read telemetry_history.json
directly off disk with no lock at all. json.dump() iterating telemetry_history
while another thread appends to it is a real race (a torn write, or in rare
cases a RuntimeError from the list changing size mid-iteration); on top of
that, a read landing mid-write could delete the writer's own temp file under
the old json_store design (fixed in PR 1).

Fix: one module-level RLock (reentrant - load_telemetry() calls
save_telemetry() internally) held for every history mutation + save, and a
new get_history_snapshot() that returns an isolated, point-in-time copy of
telemetry_history under that same lock - server.py's export route now calls
this instead of re-reading the file, so it can never observe a half-written
file OR a list being mutated out from under it mid-copy.
"""

import os
import threading
import time

import pytest


@pytest.fixture
def telemetry_module(server_module, tmp_path, monkeypatch):
    """telemetry/telemetry.py does `from config import DATA_DIR` at module
    level, same reason as system_log.py's own fixture - needs
    server_module's synthetic config.py on sys.path first."""
    import telemetry.telemetry as module

    module.configure_storage(str(tmp_path / "telemetry_history.json"))
    monkeypatch.setattr(module, "telemetry_history", [])
    monkeypatch.setattr(module, "telemetry_config", {"interval": 300, "enabled": True})
    monkeypatch.setattr(module, "telemetry_last_save_time", 0)
    # _telemetry_lock is a module-level singleton shared by every test file
    # in the whole session that touches telemetry.telemetry (several do,
    # via the session-scoped server_module fixture). A fresh lock per test
    # avoids any cross-test contamination - this module's own code only
    # ever reaches it via the module attribute, never a captured
    # reference, so swapping it here is enough.
    monkeypatch.setattr(module, "_telemetry_lock", threading.RLock())
    return module


def test_load_telemetry_does_not_deadlock_on_its_own_lock(telemetry_module):
    # load_telemetry() calls save_telemetry() internally on a missing/empty
    # file - both must be able to run under the same RLock without
    # self-deadlocking.
    telemetry_module.load_telemetry()
    assert telemetry_module.telemetry_history == []


def test_concurrent_save_blocks_a_concurrent_append_until_it_finishes(telemetry_module, monkeypatch):
    order = []
    save_started = threading.Event()
    release_save = threading.Event()
    first_call = threading.Event()
    first_call.set()  # only the FIRST call pauses - later ones (including
    # the second thread's own eventual save, once it gets the lock) must
    # run for real, or this test would just be measuring the mock's own
    # blocking instead of the module's lock.

    real_write = telemetry_module.safe_write_json

    def _pausing_write(path, data):
        if first_call.is_set():
            first_call.clear()
            order.append("save_started")
            save_started.set()
            release_save.wait(timeout=5)
            order.append("save_finished")
        return real_write(path, data)

    monkeypatch.setattr(telemetry_module, "safe_write_json", _pausing_write)

    def _trigger_save():
        telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})

    saver_thread = threading.Thread(target=_trigger_save)
    saver_thread.start()
    try:
        assert save_started.wait(timeout=5), "save never started"

        def _second_append():
            telemetry_module.add_node_telemetry_record("!bbbbbbbb", {"voltage": 3.9})
            order.append("append_done")

        appender_thread = threading.Thread(target=_second_append)
        appender_thread.start()
        try:
            # Poll rather than a single fixed sleep - robust under a loaded
            # CI/full-suite run where scheduling can be delayed well past
            # any one short sleep. If the lock is doing its job this NEVER
            # becomes true no matter how long we wait, so a generous
            # bounded poll is exactly as safe as a shorter one, just less
            # prone to a false failure under load.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and "append_done" not in order:
                time.sleep(0.02)
            assert "append_done" not in order, "a concurrent append must block while a save holds the lock"
        finally:
            # Always unblock the saver and join both threads, even if the
            # assertion above failed - otherwise a failed run here leaves
            # both threads alive, still holding/contending for the module-
            # level lock, which then poisons every later test in this file.
            release_save.set()
            appender_thread.join(timeout=5)
    finally:
        release_save.set()
        saver_thread.join(timeout=5)

    assert order.index("save_finished") < order.index("append_done")
    # Both records landed - the lock serialized them, it didn't drop one.
    node_ids = {r.get("node_id") for r in telemetry_module.telemetry_history}
    assert node_ids == {"!aaaaaaaa", "!bbbbbbbb"}


def test_get_history_snapshot_returns_an_isolated_copy(telemetry_module):
    telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})

    snapshot = telemetry_module.get_history_snapshot()
    assert len(snapshot) == 1

    # Mutating the live history afterward must not retroactively change the
    # snapshot already handed out.
    telemetry_module.telemetry_history[0]["voltage"] = 9.9
    telemetry_module.telemetry_history.append({"node_id": "!cccccccc"})

    assert snapshot[0]["voltage"] == 4.0
    assert len(snapshot) == 1


def test_get_history_snapshot_waits_for_an_in_progress_save(telemetry_module, monkeypatch):
    save_started = threading.Event()
    release_save = threading.Event()
    real_write = telemetry_module.safe_write_json

    def _pausing_write(path, data):
        save_started.set()
        release_save.wait(timeout=5)
        return real_write(path, data)

    monkeypatch.setattr(telemetry_module, "safe_write_json", _pausing_write)

    result = {}

    def _trigger_save():
        telemetry_module.add_node_telemetry_record("!aaaaaaaa", {"voltage": 4.0})

    saver_thread = threading.Thread(target=_trigger_save)
    saver_thread.start()
    try:
        assert save_started.wait(timeout=5)

        def _snapshot():
            result["snapshot"] = telemetry_module.get_history_snapshot()

        snapshot_thread = threading.Thread(target=_snapshot)
        snapshot_thread.start()
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and "snapshot" not in result:
                time.sleep(0.02)
            assert "snapshot" not in result, "snapshot must wait for the in-progress save's lock hold"
        finally:
            release_save.set()
            snapshot_thread.join(timeout=5)
    finally:
        release_save.set()
        saver_thread.join(timeout=5)

    assert len(result["snapshot"]) == 1


def test_get_telemetry_export_records_reads_the_in_memory_snapshot_not_the_file(server_module, telemetry_module):
    """server.py's get_telemetry_export_records() used to call
    safe_read_json(telemetry.TELEMETRY_FILE, {}) directly - re-reading
    whatever is on disk, independent of (and racing) telemetry.py's own
    in-memory state. It must now go through get_history_snapshot()
    instead: proven here by adding a record that hasn't been (and, in this
    test, never will be) flushed to disk, and confirming export sees it
    anyway."""
    telemetry_module.add_node_telemetry_record("!756f9960", {"voltage": 4.1})

    # The file on disk is irrelevant now - delete it outright and confirm
    # export still sees the in-memory record. get_telemetry_export_records()
    # reshapes each record (no node_id in its output - see its own "item"
    # dict), so check the one field this record actually carries through.
    if os.path.exists(telemetry_module.TELEMETRY_FILE):
        os.remove(telemetry_module.TELEMETRY_FILE)

    records = server_module.get_telemetry_export_records(node_id="!756f9960")
    assert any(r.get("voltage_v") == 4.1 for r in records)
