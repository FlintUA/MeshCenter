"""Tests for meshsrv/subprocess_spawner.py (F15) - the shared "fork from
a long-lived thread" primitive generalizing the PR #314 review finding
(PR_SET_PDEATHSIG is per-thread, not per-process) to every caller in this
codebase.

Uses real subprocess.Popen() calls against a trivial Python one-liner
(no fixture binary needed - these tests exercise the spawner's own
threading/queue/future logic, not any particular child program's
behavior) rather than mocks, matching this project's usual "real OS
process, not a simulation" testing style for subprocess-lifecycle code.
"""

import concurrent.futures
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

import meshsrv.subprocess_spawner as spawner


@pytest.fixture(autouse=True)
def _reset_spawner_state(monkeypatch):
    """Each test gets a fresh queue/thread/pid record - the module-level
    spawner thread is deliberately process-lifetime-scoped in production,
    but tests must not leak state (or a leftover thread from a slow-
    exception test) into the next one."""
    monkeypatch.setattr(spawner, "_queue", queue.Queue())
    monkeypatch.setattr(spawner, "_thread", None)
    monkeypatch.setattr(spawner, "_thread_pid", None)
    yield


def _sleep_popen(seconds: float = 0.0):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _echo_popen(value: str):
    return subprocess.Popen(
        [sys.executable, "-c", f"print({value!r})"], stdout=subprocess.PIPE, text=True
    )


# ---------------------------------------------------------------------------
# Basic spawn behaviour
# ---------------------------------------------------------------------------

def test_spawn_returns_a_running_process():
    proc = spawner.spawn(lambda: _echo_popen("hello"))
    out, _ = proc.communicate(timeout=5)
    assert out.strip() == "hello"


def test_spawn_from_a_short_lived_thread_does_not_orphan_or_kill_the_child():
    """The actual F15/PR #314 regression shape, at the primitive level:
    a real, PDEATHSIG-armed-equivalent scenario is exercised by
    tests/test_usb_camera_driver.py and test_adapter_ipc_client.py's own
    short-lived-thread tests (they use the real preexec_fn); this proves
    the spawner mechanism itself introduces no issue when the calling
    thread is short-lived, on any platform."""
    result = {}

    def spawn_and_exit():
        result["proc"] = spawner.spawn(lambda: _sleep_popen(2.0))

    thread = threading.Thread(target=spawn_and_exit)
    thread.start()
    thread.join()

    proc = result["proc"]
    assert proc.poll() is None  # still running after its spawning thread ended
    proc.wait(timeout=5)


def test_multiple_spawns_use_the_same_persistent_thread():
    spawner.spawn(lambda: _echo_popen("a")).wait(timeout=5)
    thread_after_first = spawner._thread
    spawner.spawn(lambda: _echo_popen("b")).wait(timeout=5)
    assert spawner._thread is thread_after_first


# ---------------------------------------------------------------------------
# Spawner loop survives a failing popen_fn
# ---------------------------------------------------------------------------

def test_a_failing_popen_fn_raises_to_the_caller_without_killing_the_loop():
    def _broken():
        raise RuntimeError("simulated popen_fn failure")

    with pytest.raises(RuntimeError, match="simulated popen_fn failure"):
        spawner.spawn(_broken)

    # The spawner thread must still be alive and servicing new requests -
    # a naive `while True: process = popen_fn()` with no try/except
    # around the call would let this exception kill the loop thread
    # entirely, silently breaking every subsequent spawn() forever.
    assert spawner._thread.is_alive()
    proc = spawner.spawn(lambda: _echo_popen("still works"))
    out, _ = proc.communicate(timeout=5)
    assert out.strip() == "still works"


def test_missing_binary_raises_file_not_found_not_a_hang():
    with pytest.raises(FileNotFoundError):
        spawner.spawn(lambda: subprocess.Popen(["definitely-not-a-real-binary-xyz"]))


# ---------------------------------------------------------------------------
# Abandoned future - no orphan
# ---------------------------------------------------------------------------

def test_abandoned_future_reaps_the_process_instead_of_orphaning_it():
    """spawn()'s own timeout fires before a deliberately slow popen_fn
    finishes; once it does finish (after the caller has already given up
    and raised TimeoutError), the resulting process must be terminated,
    not left running with nothing ever calling .wait() on it."""
    started = threading.Event()
    process_holder = {}

    def _slow_popen():
        started.set()
        time.sleep(0.6)  # longer than the spawn() timeout below
        proc = _sleep_popen(30.0)
        process_holder["proc"] = proc
        return proc

    with pytest.raises(concurrent.futures.TimeoutError):
        spawner.spawn(_slow_popen, timeout=0.2)

    assert started.wait(timeout=2.0)
    # Give the spawner thread time to finish _slow_popen() and notice
    # the request was abandoned.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and "proc" not in process_holder:
        time.sleep(0.05)

    assert "proc" in process_holder, "popen_fn never completed"
    proc = process_holder["proc"]
    # Reaped (terminated), not left running for 30s with nobody watching it.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    assert proc.poll() is not None, "abandoned process was not reaped - orphan"


def test_spawner_loop_itself_reaps_a_request_marked_abandoned_before_processing():
    """Isolates the SPAWNER-side reaping specifically, bypassing
    spawn()'s own wrapper entirely: spawn() has a second, redundant
    caller-side grace-check for the same race (see its docstring) that
    would otherwise mask a regression in the spawner loop's own
    abandoned-check - confirmed by mutation testing (removing the
    spawner-side check alone did NOT fail test_abandoned_future_reaps_
    the_process_instead_of_orphaning_it, precisely because that test
    goes through spawn() and its redundant safety net silently covered
    for it). This test constructs the request directly and never calls
    spawn(), so only the spawner loop's own logic can make it pass."""
    process_holder = {}

    def _make_process():
        proc = _sleep_popen(30.0)
        process_holder["proc"] = proc
        return proc

    request = spawner._SpawnRequest(_make_process, concurrent.futures.Future())
    request.abandoned.set()
    spawner._ensure_spawner_thread()
    spawner._queue.put(request)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and "proc" not in process_holder:
        time.sleep(0.05)
    assert "proc" in process_holder, "spawner loop never called popen_fn for the queued request"

    proc = process_holder["proc"]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    assert proc.poll() is not None, "spawner loop did not reap a request marked abandoned - orphan"


def test_normal_spawn_after_an_abandoned_one_still_works():
    def _slow_popen():
        time.sleep(0.5)
        return _echo_popen("after abandon")

    with pytest.raises(Exception):
        spawner.spawn(_slow_popen, timeout=0.1)

    time.sleep(1.0)  # let the abandoned request fully drain
    proc = spawner.spawn(lambda: _echo_popen("normal"))
    out, _ = proc.communicate(timeout=5)
    assert out.strip() == "normal"


# ---------------------------------------------------------------------------
# pid-awareness (fork simulation)
# ---------------------------------------------------------------------------

def test_pid_mismatch_starts_a_fresh_thread(monkeypatch):
    """Simulates the gunicorn preload_app=True fork scenario without an
    actual fork: the recorded _thread_pid is made to look like it came
    from a different process, and spawn() must start a brand-new spawner
    thread rather than trying to use (or wait forever on) the old one."""
    spawner.spawn(lambda: _echo_popen("first")).wait(timeout=5)
    first_thread = spawner._thread
    assert first_thread is not None

    monkeypatch.setattr(spawner, "_thread_pid", spawner._thread_pid - 1 if spawner._thread_pid else -1)

    proc = spawner.spawn(lambda: _echo_popen("second"))
    out, _ = proc.communicate(timeout=5)
    assert out.strip() == "second"
    assert spawner._thread is not first_thread
    assert spawner._thread_pid == os.getpid()


def test_reset_after_fork_clears_state_and_drains_stale_requests():
    """Simulates os.register_at_fork()'s after_in_child callback firing
    directly (can't actually fork() this test process) - proves it
    resets the lock/queue/thread bookkeeping and fails any request that
    was still queued at "fork" time rather than silently discarding it."""
    def _never_runs():
        raise AssertionError("must never be called - the queue should be drained, not processed")

    future = concurrent.futures.Future()
    request = spawner._SpawnRequest(_never_runs, future)
    spawner._queue.put(request)

    spawner._reset_after_fork()

    with pytest.raises(RuntimeError, match="did not survive fork"):
        future.result(timeout=1.0)

    # Post-reset state is clean and still usable.
    proc = spawner.spawn(lambda: _echo_popen("after reset"))
    out, _ = proc.communicate(timeout=5)
    assert out.strip() == "after reset"
