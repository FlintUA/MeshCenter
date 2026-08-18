"""Tests for server.py's OS-level runtime lock (_acquire_runtime_lock()) -
the second, cross-process line of defense against two MeshCenter processes
both calling start_runtime() at once (e.g. gunicorn misconfigured with more
than one worker - see gunicorn.conf.py's own comment on why workers=1 is
mandatory). The in-process `_runtime_started` guard added in PR #66 only
stops a second call within the same process; it starts False again in every
new process, so it can't see a second one.

Skipped entirely where fcntl (POSIX flock) isn't available (e.g. a Windows
dev box running the suite) - server.py itself degrades
_acquire_runtime_lock() to a no-op there rather than crashing the import
(this application only ever runs on Linux/the Pi in production).
"""

import os

import pytest

try:
    import fcntl
except ImportError:
    fcntl = None


pytestmark = pytest.mark.skipif(
    fcntl is None, reason="fcntl (POSIX flock) is not available on this platform"
)


def test_acquire_runtime_lock_fails_loudly_when_already_held(server_module, capsys):
    # Simulate a second MeshCenter process already holding the lock by
    # locking the same file ourselves first, through a separate open file
    # description. flock() (unlike fcntl's POSIX record locks, confusingly
    # also reachable through the `fcntl` module) conflicts across distinct
    # open file descriptions even within a single process - so this
    # genuinely exercises the same contention a second real OS process
    # would hit, not a fake stand-in for it.
    os.makedirs(os.path.dirname(server_module.RUNTIME_LOCK_FILE), exist_ok=True)
    blocker = open(server_module.RUNTIME_LOCK_FILE, "w")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        with pytest.raises(SystemExit) as exc_info:
            server_module._acquire_runtime_lock()
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "FATAL" in captured.out
        assert "Another MeshCenter process" in captured.out
    finally:
        fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        blocker.close()


def test_acquire_runtime_lock_succeeds_when_free(server_module):
    server_module._runtime_lock_handle = None
    try:
        server_module._acquire_runtime_lock()
        assert server_module._runtime_lock_handle is not None
        # Confirm it's a *real* held lock, not just a set variable - a
        # second acquire attempt (simulating another process) must now
        # fail exactly like the test above.
        second_handle = open(server_module.RUNTIME_LOCK_FILE, "w")
        try:
            with pytest.raises(OSError):
                fcntl.flock(second_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            second_handle.close()
    finally:
        if server_module._runtime_lock_handle:
            server_module._runtime_lock_handle.close()
            server_module._runtime_lock_handle = None
