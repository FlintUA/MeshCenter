"""Shared "fork from a long-lived thread" primitive (F15, generalizing
the PR #314 review finding for camera/usb_driver.py's ffmpeg spawning to
every caller of PR_SET_PDEATHSIG in this codebase).

PR_SET_PDEATHSIG is per-THREAD, not per-process (`man 2 prctl`: "the
'parent' in this case is the thread that created this process... the
signal will be sent when that thread terminates... rather than after all
the threads in the parent process have terminated"). Any caller that
spawns a PDEATHSIG-armed `subprocess.Popen()` directly from a
short-lived thread - a Flask request thread under Werkzeug's threaded
dev server, `server.py`'s `_do_reconnect`/`_do_identity_retry` daemon
threads, or anything similar - silently kills that child the instant its
own spawning thread returns, independent of whether the process it
belongs to is still running. Confirmed live on camtest for
camera/usb_driver.py's ffmpeg spawns (PR #314); the identical bug exists
in meshsrv/adapter_ipc_client.py's `_spawn_locked()` (F15) - both now use
this one shared implementation instead of each rolling its own.

One process-wide spawner thread services every caller: `spawn()` hands a
zero-argument `popen_fn` (which must itself call `subprocess.Popen(...)`
and return the result) to the spawner thread via a queue/future handoff
and blocks the CALLING thread (safe to be short-lived) until the Popen
object comes back. "The thread that called fork()" is then a fixed,
permanent fact - this module's own spawner thread, which lives for the
process's lifetime - never an accident of which thread pool or
short-lived worker happened to service a given request.
"""

from __future__ import annotations

import concurrent.futures
import os
import queue
import subprocess
import threading
from typing import Callable

# How often the spawner thread wakes up with nothing queued - just often
# enough that the thread doesn't look permanently blocked to anything
# inspecting it, not a real polling interval for correctness (queue.get()
# with a timeout, not a real polling loop - the timeout only exists so
# _reset_after_fork() below can't ever be blocked out indefinitely by a
# Queue.get() call that started pre-fork).
_QUEUE_POLL_SECONDS = 0.5

# How long spawn() waits for an abandoned future's process to land, on
# the off chance the spawner thread already checked _SpawnRequest.abandoned
# (saw it False) and is mid-flight to deliver the result anyway - see
# spawn()'s own docstring for the exact race this closes.
_ABANDON_RACE_GRACE_SECONDS = 1.0

# Default per-call timeout for spawn() - overridable per call.
DEFAULT_SPAWN_TIMEOUT_SECONDS = 10.0


class _SpawnRequest:
    __slots__ = ("popen_fn", "future", "abandoned")

    def __init__(self, popen_fn: Callable[[], subprocess.Popen], future: "concurrent.futures.Future[subprocess.Popen]"):
        self.popen_fn = popen_fn
        self.future = future
        self.abandoned = threading.Event()


_lock = threading.Lock()
_queue: "queue.Queue[_SpawnRequest]" = queue.Queue()
_thread: threading.Thread | None = None
_thread_pid: int | None = None


def _safe_set_result(future: "concurrent.futures.Future[subprocess.Popen]", process: subprocess.Popen) -> bool:
    try:
        future.set_result(process)
        return True
    except Exception:
        # The future's own internal state rejected this (already
        # cancelled, or some other caller-side condition) - the spawner
        # loop itself must survive regardless, so this is swallowed, not
        # raised. Returning False tells the caller to treat `process` as
        # unclaimed (see _spawner_loop()).
        return False


def _safe_set_exception(future: "concurrent.futures.Future", error: BaseException) -> None:
    try:
        future.set_exception(error)
    except Exception:
        pass


def _reap_abandoned(process: subprocess.Popen) -> None:
    """A Popen nobody will ever call .result() to retrieve - terminate
    and reap it rather than leak it holding a device node or port
    indefinitely."""
    try:
        process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=5.0)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=5.0)
    except Exception:
        pass


def _spawner_loop() -> None:
    """Runs forever on the shared spawner thread. Never dies: any
    exception from `popen_fn()` itself, or from delivering the result,
    is caught and reported through the future instead of propagating and
    killing this loop - a spawner thread that silently stopped consuming
    the queue would be far worse than one failed spawn (every future
    caller would then block for its own full timeout and fail)."""
    while True:
        try:
            request = _queue.get(timeout=_QUEUE_POLL_SECONDS)
        except queue.Empty:
            continue

        try:
            process = request.popen_fn()
        except Exception as error:
            if not request.abandoned.is_set():
                _safe_set_exception(request.future, error)
            continue

        if request.abandoned.is_set():
            _reap_abandoned(process)
        else:
            if not _safe_set_result(request.future, process):
                _reap_abandoned(process)


def _reset_after_fork() -> None:
    """Registered via os.register_at_fork() below - runs in the CHILD
    immediately after fork(). Only the forking thread survives fork();
    every other thread's OS-level state (including the spawner thread's)
    is simply gone, even though the Python-level Thread object may still
    report is_alive()==True (CPython doesn't update that bookkeeping on
    fork). A Lock held by some other thread at fork time would otherwise
    stay locked forever in the child (a well-known fork+threading
    hazard) - replacing it with a fresh, guaranteed-unlocked Lock
    sidesteps that entirely rather than trying to detect/recover a
    possibly-stuck one. Any request already queued targeted the PARENT
    process's now-gone spawner thread - draining it (with a clear
    exception on each abandoned future) instead of letting a fresh
    thread process stale pre-fork entries."""
    global _lock, _queue, _thread, _thread_pid
    stale_queue = _queue
    _lock = threading.Lock()
    _queue = queue.Queue()
    _thread = None
    _thread_pid = None
    while True:
        try:
            request = stale_queue.get_nowait()
        except queue.Empty:
            break
        _safe_set_exception(request.future, RuntimeError("subprocess spawner thread did not survive fork()"))


if hasattr(os, "register_at_fork"):  # POSIX only - this project's actual deployment target
    os.register_at_fork(after_in_child=_reset_after_fork)


def _ensure_spawner_thread() -> None:
    """Starts the spawner thread lazily (on first spawn() call, not at
    import time) and pid-aware: if the current process's pid doesn't
    match the pid that started the thread we have on record, a fresh one
    is started. This is the second, redundant layer behind
    os.register_at_fork() above (belt and suspenders - covers any gap
    between a fork happening and register_at_fork's callback having run,
    and doubles as the only protection on a platform where
    register_at_fork doesn't exist) - without EITHER layer, a
    preload_app=True-style fork (not this project's current gunicorn
    config, but a plausible future one) would leave the child with a
    Thread object that looks alive but whose actual OS thread doesn't
    exist, so every spawn() call would wait out its full timeout and
    fail."""
    global _thread, _thread_pid
    with _lock:
        current_pid = os.getpid()
        if _thread is not None and _thread.is_alive() and _thread_pid == current_pid:
            return
        _thread = threading.Thread(target=_spawner_loop, name="subprocess-spawner", daemon=True)
        _thread_pid = current_pid
        _thread.start()


def spawn(
    popen_fn: Callable[[], subprocess.Popen],
    timeout: float = DEFAULT_SPAWN_TIMEOUT_SECONDS,
) -> subprocess.Popen:
    """Runs `popen_fn()` (a zero-argument callable that itself calls
    `subprocess.Popen(...)`, typically with `preexec_fn=`/PDEATHSIG set,
    and returns the resulting Popen) on the shared, persistent spawner
    thread - never on the caller's own thread. Blocks the calling thread
    (safe to be short-lived) until the Popen comes back, or raises
    `concurrent.futures.TimeoutError` after `timeout` seconds.

    Abandoned-future handling: if this call's own wait times out, the
    request is marked abandoned rather than cancelled outright (the
    spawner thread may already be mid-`popen_fn()`, with no way to abort
    that call once started) - if the spawner subsequently completes it,
    the process is reaped immediately instead of orphaned holding a
    device node or port with nothing left to ever call .wait() on it.
    One race remains irreducible without a shared lock spanning both
    sides for the whole operation (not worth the added contention for
    what CAM-1's own live testing showed is a rare, narrow window): the
    spawner thread might check `abandoned` (see it False) and be
    mid-flight to call `future.set_result()` at the exact moment this
    timeout fires. The grace check below closes that specific window
    from the caller's side too, so an orphan requires both sides to lose
    the race simultaneously, not just one.
    """
    _ensure_spawner_thread()
    future: "concurrent.futures.Future[subprocess.Popen]" = concurrent.futures.Future()
    request = _SpawnRequest(popen_fn, future)
    _queue.put(request)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        request.abandoned.set()
        try:
            process = future.result(timeout=_ABANDON_RACE_GRACE_SECONDS)
        except concurrent.futures.TimeoutError:
            pass
        else:
            _reap_abandoned(process)
        raise
