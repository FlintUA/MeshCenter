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
import subprocess
import threading
import time

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
    TransportError(BUSY) when prepare_command() reports the port never
    freed up - unchanged behavior from before the move, just relocated.
    wait_serial_release() is stubbed to always report "still busy" so
    this doesn't depend on real `lsof`/OS state."""
    from meshsrv.radio_transport import TransportError, TransportErrorCode

    supervisor = _make_supervisor()
    supervisor.wait_serial_release = lambda timeout=8: False

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


def test_only_the_final_layer_lsof_can_confirm_port_free():
    """Every intermediate PORT_FREE must fall through all the way to
    lsof - the last layer in the chain - before the result is trusted as
    a genuine "confirmed free". If lsof itself can't confirm either
    (timeout/failed/missing), that inconclusive outcome is the final
    answer - it must never be silently upgraded to PORT_FREE just
    because earlier layers guessed free."""
    supervisor = _make_supervisor()
    supervisor._port = "/dev/ttyACM0"
    supervisor._check_known_pid = lambda: None
    supervisor._check_proc_fd_scan = lambda: PortReleaseOutcome.PORT_FREE
    supervisor._check_fuser = lambda: PortReleaseOutcome.PORT_FREE

    supervisor._check_lsof = lambda: PortReleaseOutcome.PORT_FREE
    assert supervisor.check_port_release_once() == PortReleaseOutcome.PORT_FREE

    supervisor._check_lsof = lambda: PortReleaseOutcome.CHECK_TIMEOUT
    assert supervisor.check_port_release_once() == PortReleaseOutcome.CHECK_TIMEOUT
