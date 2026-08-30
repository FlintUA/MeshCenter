"""Tests for meshsrv/serial_port_supervisor.py's SerialPortSupervisor -
extracted from adapters/meshtastic/serial_transport.py's SerialTransport
during the P0 #1 stabilization follow-up (independent audit). Covers the
listener-management + exclusive-access-claim surface
(run_listener()/get_listener_pid()/claim_exclusive_access()/the internal
stop_listener_process()/wait_serial_release() pair) moved 1:1 out of that
class - see meshsrv/serial_port_supervisor.py's own module docstring for
why this is a shared primitive, not Core-exclusive logic.

No server.py import needed - SerialPortSupervisor takes radio_lock/
pause_listen by DI and never imports server.py or meshtastic itself.
"""
import os
import subprocess
import threading
import time

import pytest

from meshsrv.radio_transport import TransportError, TransportErrorCode
from meshsrv.serial_port_supervisor import PortReleaseOutcome, SerialPortSupervisor


def _make_supervisor():
    return SerialPortSupervisor(
        cli_path="/does/not/matter/for/this/test",
        port="/dev/ttyFAKE",
        radio_lock=threading.RLock(),
        pause_listen=threading.Event(),
    )


# --- Task 49: get_listener_pid()'s bounded radio_lock acquire -------------
# (moved from tests/test_serial_transport_timeout.py - unchanged behavior,
# now exercised against SerialPortSupervisor directly rather than through
# SerialTransport)


def test_get_listener_pid_returns_none_on_a_busy_lock_within_its_short_budget():
    """Task 49 fix: once claim_exclusive_access() can hold this same
    radio_lock for a full IPC round-trip, get_listener_pid()'s old
    unconditional `with self._radio_lock:` could stall the whole
    dashboard page behind it. Now bounded by
    _LISTENER_PID_LOCK_TIMEOUT_S (3.0s, reusing meshsrv/transport_router.
    py's own _INFO_LOCK_TIMEOUT_S precedent) and, on a busy lock, returns
    None - a synthetic fallback matching is_connected()'s same fail-safe
    pattern, not a raised exception - a passive status field degrading
    gracefully, not an action failing loudly."""
    supervisor = _make_supervisor()
    supervisor._radio_lock.acquire()
    try:
        start = time.monotonic()
        result = supervisor.get_listener_pid()
        elapsed = time.monotonic() - start
    finally:
        supervisor._radio_lock.release()

    assert result is None
    assert elapsed < supervisor._LISTENER_PID_LOCK_TIMEOUT_S + 1.0, (
        f"took {elapsed:.2f}s - should give up within its own short budget, not block indefinitely"
    )


def test_get_listener_pid_still_works_normally_when_the_lock_is_free():
    """Sanity check alongside the busy-lock test above: the bounded
    acquire must not change behavior in the ordinary, uncontended case -
    still reports None when there's genuinely no listener process (this
    instance never runs run_listener() in these tests)."""
    supervisor = _make_supervisor()
    assert supervisor.get_listener_pid() is None


# --- claim_exclusive_access() - port-busy path -----------------------------


def test_claim_exclusive_access_raises_busy_when_the_port_never_frees():
    """claim_exclusive_access() (renamed from claim_for_external_command()/
    _claim_radio() during this stabilization follow-up) must still raise
    TransportError(BUSY) when the port is confirmed genuinely occupied -
    unchanged behavior from before the move, just relocated.
    _wait_for_release_outcome() is stubbed to always report a real
    PORT_BUSY so this doesn't depend on real `lsof`/OS state."""
    from meshsrv.radio_transport import TransportError, TransportErrorCode

    supervisor = _make_supervisor()
    supervisor._wait_for_release_outcome = lambda timeout=8: PortReleaseOutcome.PORT_BUSY

    try:
        with supervisor.claim_exclusive_access(timeout=0.2, cooldown=0):
            raise AssertionError("should never reach the yield - the port never freed")
    except TransportError as error:
        assert error.code == TransportErrorCode.BUSY
    else:
        raise AssertionError("claim_exclusive_access() did not raise on a busy port")


# --- P0 stabilization follow-up (Droidian-caught stdout-corruption cascade) ---
# adapters/meshtastic/ipc_server.py's main() runs a SerialTransport that
# composes ITS OWN SerialPortSupervisor instance (not Core's separate one) -
# on that instance, this module's stdout IS the JSON-RPC protocol channel
# back to Core. wait_serial_release() hitting an `lsof` subprocess timeout
# used to `print(..., flush=True)` with no `file=` argument, defaulting to
# stdout - corrupting the response Core was about to parse. This is
# platform-independent (nothing here depends on real `lsof`/OS timing,
# unlike the live Droidian report that first caught it) and does not touch
# any live node.

def test_lsof_timeout_never_reaches_stdout_only_stderr(monkeypatch, capsys):
    """THE regression test for the reported corruption mechanism: force
    subprocess.run (used by both the fuser and lsof fallback layers) to
    raise TimeoutExpired - exactly what was observed, unreliably, on
    Droidian - and confirm nothing lands on stdout, only stderr. The
    known-PID and /proc/*/fd layers are forced inconclusive (return None)
    so this test deterministically reaches the external-tool fallback
    layers regardless of the platform running it (a real /proc scan on
    Linux CI could otherwise short-circuit to PORT_FREE before ever
    calling subprocess.run, silently skipping the actual case under
    test)."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    monkeypatch.setattr(supervisor, "_check_known_pid", lambda: None)
    monkeypatch.setattr(supervisor, "_check_proc_fd_scan", lambda: None)

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0][0] if args else "lsof", timeout=2)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    result = supervisor.wait_serial_release(timeout=0.5)

    assert result is False
    captured = capsys.readouterr()
    assert captured.out == "", f"a subprocess timeout must never print to stdout, got: {captured.out!r}"
    assert "Serial port not confirmed free" in captured.err
    assert "check_timeout" in captured.err


def test_check_port_release_once_distinguishes_timeout_from_busy_from_missing():
    """P0.3: an lsof/fuser timeout, a genuinely busy port, and a missing
    utility must be distinguishable outcomes, not all collapsed into the
    same "busy" signal - the actual root cause of the observed corruption
    (a timeout was silently treated as proof of a busy port, prolonging
    the exclusive-access claim window unnecessarily)."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    supervisor._check_known_pid = lambda: None
    supervisor._check_proc_fd_scan = lambda: None

    supervisor._check_fuser = lambda: PortReleaseOutcome.CHECK_TIMEOUT
    supervisor._check_lsof = lambda: PortReleaseOutcome.CHECK_TIMEOUT
    assert supervisor.check_port_release_once() == PortReleaseOutcome.CHECK_TIMEOUT

    supervisor._check_fuser = lambda: PortReleaseOutcome.UTILITY_MISSING
    supervisor._check_lsof = lambda: PortReleaseOutcome.UTILITY_MISSING
    assert supervisor.check_port_release_once() == PortReleaseOutcome.UTILITY_MISSING

    supervisor._check_fuser = lambda: PortReleaseOutcome.CHECK_FAILED
    supervisor._check_lsof = lambda: PortReleaseOutcome.PORT_BUSY
    assert supervisor.check_port_release_once() == PortReleaseOutcome.PORT_BUSY


def test_known_pid_busy_short_circuits_before_any_external_tool():
    """The known-PID layer answering PORT_BUSY must skip every layer
    below it entirely (cheapest check first) - proven by making every
    other layer raise if reached, not just by checking the return value."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    with supervisor._radio_lock:
        supervisor._listen_process = _AlwaysRunningProcess()

    def _fail_if_called(*a, **k):
        raise AssertionError("should never be reached - known-PID layer already answered BUSY")

    supervisor._check_proc_fd_scan = _fail_if_called
    supervisor._check_fuser = _fail_if_called
    supervisor._check_lsof = _fail_if_called

    assert supervisor.check_port_release_once() == PortReleaseOutcome.PORT_BUSY


def test_known_pid_free_is_not_treated_as_port_confirmed_free():
    """Explicit test for the review's own caveat: "our listener isn't
    holding it" must NOT short-circuit straight to PORT_FREE - only to
    "inconclusive, ask the next layer" (None). An orphaned process from a
    previous crash, or an unrelated process on the device, would be
    invisible to the known-PID check alone."""
    supervisor = _make_supervisor()
    # No listener process at all - the known-PID layer's "not held by
    # OUR process" case.
    assert supervisor._check_known_pid() is None

    # Confirm check_port_release_once() actually consults the next layer
    # in this case, rather than treating None as a final PORT_FREE answer.
    supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.PORT_BUSY
    assert supervisor.check_port_release_once() == PortReleaseOutcome.PORT_BUSY


class _AlwaysRunningProcess:
    def poll(self):
        return None  # None = still running, matching subprocess.Popen.poll()'s own contract


# --- Review follow-up: asymmetric BUSY/FREE trust between layers -------------
# A PORT_FREE from an intermediate layer (proc_fd_scan, fuser) is NOT the
# same strength of evidence as its own PORT_BUSY - a cross-user process
# holding the port (e.g. a root-owned debugging `screen /dev/ttyACM0`
# session) is invisible to a scan/tool this process lacks permission to
# see into, so it would silently report PORT_FREE while the port is
# genuinely busy. Only PORT_BUSY short-circuits early; PORT_FREE from
# anything but the LAST layer (lsof) must fall through for confirmation.

def test_intermediate_layer_port_free_is_not_trusted_falls_through_to_next_layer():
    """proc_fd_scan saying PORT_FREE must not end the check - fuser is
    still consulted, and its PORT_BUSY answer wins over the earlier
    PORT_FREE."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    supervisor._check_known_pid = lambda: None
    supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.PORT_FREE
    supervisor._check_fuser = lambda: PortReleaseOutcome.PORT_BUSY

    def _fail_if_called():
        raise AssertionError("lsof should never be reached - fuser already answered BUSY")

    supervisor._check_lsof = _fail_if_called

    assert supervisor.check_port_release_once() == PortReleaseOutcome.PORT_BUSY


def test_lsof_confirming_free_is_still_trusted_on_its_own():
    """The baseline case, unchanged: every intermediate layer saying
    PORT_FREE and lsof itself also confirming PORT_FREE is a genuine
    confirmed-free result."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    supervisor._check_known_pid = lambda: None
    supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.PORT_FREE
    supervisor._check_fuser = lambda: PortReleaseOutcome.PORT_FREE
    supervisor._check_lsof = lambda: PortReleaseOutcome.PORT_FREE

    assert supervisor.check_port_release_once() == PortReleaseOutcome.PORT_FREE


# --- Droidian follow-up: fd_scan + fuser both PORT_FREE outweighs a
# merely-inconclusive lsof (live-measured on Droidian: lsof's own 2s
# busy_timeout is regularly too short there, 1.7-2.8s observed, turning a
# genuinely free port into a false "busy" on every send attempt) - but
# ONLY when both independent layers actually agree; any layer that
# didn't cleanly confirm PORT_FREE itself must not be papered over.


def test_proc_and_fuser_both_free_outweighs_an_inconclusive_lsof():
    """Case 4 from the task's required test list: /proc and fuser both
    confirm no owner, lsof times out - the new safe policy treats this as
    PORT_FREE rather than leaving it CHECK_TIMEOUT (which used to
    surface to the user as a false "Serial port busy")."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    supervisor._check_known_pid = lambda: None
    supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.PORT_FREE
    supervisor._check_fuser = lambda: PortReleaseOutcome.PORT_FREE

    for lsof_outcome in (
        PortReleaseOutcome.CHECK_TIMEOUT,
        PortReleaseOutcome.CHECK_FAILED,
        PortReleaseOutcome.UTILITY_MISSING,
    ):
        supervisor._check_lsof = lambda outcome=lsof_outcome: outcome
        assert supervisor.check_port_release_once() == PortReleaseOutcome.PORT_FREE


def test_inconclusive_lsof_is_not_upgraded_unless_both_proc_and_fuser_agree():
    """The narrower guarantee that survives from the old test: an
    inconclusive lsof is NOT silently upgraded to PORT_FREE unless BOTH
    fd_scan and fuser affirmatively agree - a single clean layer (or a
    layer that was itself inconclusive) must not trigger the fallback,
    since the whole point is trusting two independent confirmations, not
    just one guess."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    supervisor._check_known_pid = lambda: None
    supervisor._check_lsof = lambda: PortReleaseOutcome.CHECK_TIMEOUT

    # Only fd_scan clean, fuser itself inconclusive - must not upgrade.
    supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.PORT_FREE
    supervisor._check_fuser = lambda: PortReleaseOutcome.CHECK_TIMEOUT
    assert supervisor.check_port_release_once() == PortReleaseOutcome.CHECK_TIMEOUT

    # Only fuser clean, fd_scan itself inconclusive - must not upgrade.
    supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.CHECK_FAILED
    supervisor._check_fuser = lambda: PortReleaseOutcome.PORT_FREE
    assert supervisor.check_port_release_once() == PortReleaseOutcome.CHECK_TIMEOUT


# --- Real /proc fd scan and real fuser/lsof-shaped subprocess results
# (not stubbed at the check_port_release_once() layer) - cases 2/3/6/7
# from the task's required test list.


def test_proc_fd_scan_detects_a_real_owner_via_matching_realpath(monkeypatch, tmp_path):
    """Case 2: /proc detects an owner -> PORT_BUSY. Exercises
    _check_proc_fd_scan() itself, not a stub of its return value - builds
    a synthetic /proc/<pid>/fd/<n> entry and makes os.path.realpath()
    resolve it to the target port, without needing a real symlink (which
    can fail without extra privilege on Windows) - matching the actual
    check's own logic (os.path.realpath(fd_entry) == target)."""
    import meshsrv.serial_port_supervisor as spv_module

    proc_dir = tmp_path / "proc"
    fd_dir = proc_dir / "4242" / "fd"
    fd_dir.mkdir(parents=True)
    fake_fd_entry = fd_dir / "3"
    fake_fd_entry.write_text("")

    target_port = "/dev/ttyACM0"
    real_realpath = os.path.realpath

    def fake_realpath(path):
        path_str = str(path)
        if path_str == target_port:
            return target_port
        if path_str == str(fake_fd_entry):
            return target_port
        return real_realpath(path)

    real_path_ctor = spv_module.Path

    def fake_path_ctor(path, *args, **kwargs):
        if str(path) == "/proc":
            return real_path_ctor(proc_dir)
        return real_path_ctor(path, *args, **kwargs)

    monkeypatch.setattr(spv_module, "Path", fake_path_ctor)
    monkeypatch.setattr(spv_module.os.path, "realpath", fake_realpath)

    supervisor = _make_supervisor()
    supervisor._port = target_port

    assert supervisor._check_proc_fd_scan() == PortReleaseOutcome.PORT_BUSY


def test_fuser_detects_a_real_owner(monkeypatch):
    """Case 3: fuser detects an owner -> PORT_BUSY. Mocks subprocess.run
    itself (not _check_fuser()'s return value) with the actual shape
    fuser produces for a held file: returncode 0, the PID on stdout."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "fuser"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="1234\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert supervisor._check_fuser() == PortReleaseOutcome.PORT_BUSY


def test_check_external_tool_reports_utility_missing_for_a_nonexistent_binary():
    """Case 6: the external tool binary itself is entirely absent -
    genuinely exercises FileNotFoundError from a real subprocess.run()
    call (no such binary exists on any platform), not a stubbed
    shortcut."""
    supervisor = _make_supervisor()

    outcome = supervisor._check_external_tool(
        "this-binary-does-not-exist-anywhere-on-any-platform", ["/dev/ttyACM0"]
    )

    assert outcome == PortReleaseOutcome.UTILITY_MISSING


def test_check_external_tool_reports_check_failed_on_permission_error(monkeypatch):
    """Case 7: the external tool errors out / access is refused ->
    CHECK_FAILED, distinct from both UTILITY_MISSING (tool absent) and
    CHECK_TIMEOUT (tool present but too slow)."""
    supervisor = _make_supervisor()

    def fake_run(cmd, **kwargs):
        raise PermissionError("Access denied")

    monkeypatch.setattr(subprocess, "run", fake_run)

    outcome = supervisor._check_external_tool("fuser", ["/dev/ttyACM0"])

    assert outcome == PortReleaseOutcome.CHECK_FAILED


# --- Listener-restart guarantees (requirement #4) - cases 8/9/10 -----------


def test_pause_listen_is_cleared_after_a_successful_claim():
    """Case 8: listener resumes after a successful send."""
    supervisor = _make_supervisor()
    supervisor._wait_for_release_outcome = lambda timeout=8: PortReleaseOutcome.PORT_FREE
    supervisor.stop_listener_process = lambda: True

    with supervisor.claim_exclusive_access(timeout=1, cooldown=0):
        pass

    assert not supervisor._pause_listen.is_set()


def test_pause_listen_is_cleared_after_a_send_error_inside_the_claim():
    """Case 9: listener resumes after a send error - an ordinary
    exception raised from inside the claimed block."""
    supervisor = _make_supervisor()
    supervisor._wait_for_release_outcome = lambda timeout=8: PortReleaseOutcome.PORT_FREE
    supervisor.stop_listener_process = lambda: True

    with pytest.raises(RuntimeError):
        with supervisor.claim_exclusive_access(timeout=1, cooldown=0):
            raise RuntimeError("send failed")

    assert not supervisor._pause_listen.is_set()


def test_pause_listen_is_cleared_after_an_adapter_timeout_inside_the_claim():
    """Case 10: listener resumes after an adapter-side timeout/IPC
    exception - claim_exclusive_access()'s finally must not care what
    kind of exception propagated through the yield, a TransportError(
    TIMEOUT) (the shape AdapterSupervisor.call() actually raises on a
    wedged adapter) included."""
    supervisor = _make_supervisor()
    supervisor._wait_for_release_outcome = lambda timeout=8: PortReleaseOutcome.PORT_FREE
    supervisor.stop_listener_process = lambda: True

    with pytest.raises(TransportError) as excinfo:
        with supervisor.claim_exclusive_access(timeout=1, cooldown=0):
            raise TransportError(TransportErrorCode.TIMEOUT, "adapter subprocess did not respond")

    assert excinfo.value.code == TransportErrorCode.TIMEOUT
    assert not supervisor._pause_listen.is_set()


def test_claim_exclusive_access_raises_port_check_inconclusive_not_busy_when_check_cannot_confirm():
    """The addendum's central point: a check that couldn't reach a
    definitive answer must raise a distinct, honest error - not the same
    BUSY a real owner would produce."""
    supervisor = _make_supervisor()
    supervisor._wait_for_release_outcome = lambda timeout=8: PortReleaseOutcome.CHECK_TIMEOUT

    with pytest.raises(TransportError) as excinfo:
        with supervisor.claim_exclusive_access(timeout=0.2, cooldown=0):
            raise AssertionError("should never reach the yield - the check never confirmed free")

    assert excinfo.value.code == TransportErrorCode.PORT_CHECK_INCONCLUSIVE
    assert "check_timeout" in excinfo.value.message


# --- Case 13: both Core's and the adapter's own instance are the SAME
# class, so this single fix covers both layers - proven directly with two
# independent instances, not asserted from the shared-code claim alone.


def test_two_independent_supervisor_instances_both_benefit_from_the_same_fallback():
    """Mirrors the real architecture: Core's own SerialPortSupervisor and
    the adapter's own composed instance are two separate instances of the
    same class, each with its own radio_lock/pause_listen (never shared
    across the process boundary - see this module's own docstring).
    Fixing check_port_release_once() once must resolve the false-BUSY
    for both layers independently, not just whichever one a test happens
    to construct."""
    core_side = _make_supervisor()
    adapter_side = _make_supervisor()

    for supervisor in (core_side, adapter_side):
        supervisor._port = "/dev/ttyACM0"
        supervisor._check_known_pid = lambda: None
        supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.PORT_FREE
        supervisor._check_fuser = lambda: PortReleaseOutcome.PORT_FREE
        supervisor._check_lsof = lambda: PortReleaseOutcome.CHECK_TIMEOUT

    assert core_side.check_port_release_once() == PortReleaseOutcome.PORT_FREE
    assert adapter_side.check_port_release_once() == PortReleaseOutcome.PORT_FREE
