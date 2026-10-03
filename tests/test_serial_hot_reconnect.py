"""Tests for SerialPortSupervisor's H2-C Phase 2 behavior: disconnect
detection (the stdout marker and the device-identity check),
by-id-based path re-resolution, and identity re-verification before a
restart. See meshsrv/serial_reconnect.py's module docstring for the three
failure modes these close, and that phase's live investigation
(2026-10-03) for how each was found.

Uses a fake Popen-shaped process object, not a real subprocess or shell
script - _listener_cycle() is the directly-testable one-iteration method
(see its own docstring for why run_listener() was split this way), so a
real `while True` loop or a POSIX fake-CLI script is never needed here.
"""
import threading
import time

import pytest

from meshsrv.serial_port_supervisor import SerialPortSupervisor


class _FakeListenProcess:
    """Minimal stand-in for subprocess.Popen - supports exactly what
    _listener_cycle() touches: .stdout (an iterable of raw lines),
    .poll(), .terminate(), .wait(), .kill().

    `exit_code=None` means "still running" (poll() returns None) until
    terminate()/kill() is called, after which poll() reports 0 - the
    hung-but-alive shape. `exit_code=<int>` means the process already
    exited with that code by the time its stdout iterator is exhausted -
    the ordinary crash shape."""

    def __init__(self, lines, exit_code=1):
        self.stdout = iter(lines)
        self.pid = 4242
        self._exit_code = exit_code
        self.terminated = False

    def poll(self):
        if self.terminated:
            return self._exit_code if self._exit_code is not None else 0
        return self._exit_code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.poll()

    def kill(self):
        self.terminated = True


def _make_supervisor(**kwargs):
    defaults = dict(
        cli_path="/does/not/matter",
        port="/dev/ttyACM0",
        radio_lock=threading.RLock(),
        pause_listen=threading.Event(),
    )
    defaults.update(kwargs)
    return SerialPortSupervisor(**defaults)


def _run_one_cycle(supervisor, monkeypatch, fake_process, *, sleep_patch=True):
    """Runs exactly one _listener_cycle() against `fake_process`, with
    subprocess.Popen and time.sleep stubbed so the test is fast and
    deterministic (identity-retry backoff can be up to 60s for real)."""
    import meshsrv.serial_port_supervisor as spv_module

    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: fake_process)
    if sleep_patch:
        monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)
    supervisor._listener_cycle()


# --- Case 5: hung-but-alive - the fake CLI prints the disconnect line and
# never exits - terminated within one tick. -------------------------------


def test_disconnect_line_terminates_a_hung_but_alive_process(monkeypatch):
    raw_lines = [
        "WARNING file:stream_interface.py __reader line:233 Meshtastic "
        "serial port disconnected, disconnecting... device reports "
        "readiness to read but returned no data (device disconnected or "
        "multiple access on port?)\n",
        "this line must never be processed - the loop breaks before it\n",
    ]
    seen_lines = []
    supervisor = _make_supervisor(on_raw_line=seen_lines.append)
    fake_process = _FakeListenProcess(raw_lines, exit_code=None)

    _run_one_cycle(supervisor, monkeypatch, fake_process)

    assert fake_process.terminated is True, "a hung-but-alive process must be terminated, not left running"
    assert seen_lines == [raw_lines[0].strip()], "must stop at the disconnect line, never read past it"
    assert supervisor._disconnect_detected.is_set()


def test_ordinary_crash_without_a_disconnect_line_does_not_set_disconnect_detected(monkeypatch):
    """A plain crash (exit code 1, no disconnect marker in its output)
    must keep going through the existing fast crash-and-retry path,
    unaffected by the new disconnect-detection logic - identity
    verification must never be triggered for a crash unrelated to the
    device itself."""
    supervisor = _make_supervisor()
    fake_process = _FakeListenProcess(["some unrelated crash output\n"], exit_code=1)

    _run_one_cycle(supervisor, monkeypatch, fake_process)

    assert supervisor._disconnect_detected.is_set() is False


# --- Cases 1/2: device vanishes -> terminated -> reappears (same path, or
# a different one resolved via by-id) -> MATCH -> resumed. ----------------


def test_disconnect_then_match_on_the_same_path_resumes(monkeypatch):
    calls = []
    supervisor = _make_supervisor(
        resolve_port=lambda: "/dev/ttyACM0",
        verify_identity=lambda port: calls.append(port) or "MATCH",
    )
    supervisor._disconnect_detected.set()
    import meshsrv.serial_port_supervisor as spv_module
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)

    fake_process = _FakeListenProcess(["some line\n"], exit_code=0)
    _run_one_cycle(supervisor, monkeypatch, fake_process, sleep_patch=False)

    assert calls == ["/dev/ttyACM0"]
    assert supervisor._disconnect_detected.is_set() is False
    assert supervisor._port == "/dev/ttyACM0"


def test_disconnect_then_match_on_a_different_path_resolved_via_by_id_resumes(monkeypatch):
    """Case 2: the device re-enumerated as ttyACM1 - resolve_port() (the
    by-id resolution server.py injects) returns the NEW path, and once
    identity confirms MATCH there, the listener adopts it."""
    calls = []
    supervisor = _make_supervisor(
        port="/dev/ttyACM0",
        resolve_port=lambda: "/dev/ttyACM1",
        verify_identity=lambda port: calls.append(port) or "MATCH",
    )
    supervisor._disconnect_detected.set()
    import meshsrv.serial_port_supervisor as spv_module
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)

    fake_process = _FakeListenProcess(["some line\n"], exit_code=0)
    _run_one_cycle(supervisor, monkeypatch, fake_process, sleep_patch=False)

    assert calls == ["/dev/ttyACM1"]
    assert supervisor._port == "/dev/ttyACM1", "must adopt the newly-resolved path, not keep the stale one"


def test_disconnect_with_no_device_present_backs_off_without_verifying_identity(monkeypatch):
    """Nothing to verify identity against yet - must back off (per
    identity_retry_delay()) rather than call verify_identity() against a
    path that doesn't exist, and must NOT attempt a Popen."""
    import meshsrv.serial_port_supervisor as spv_module

    verify_calls = []
    popen_calls = []
    supervisor = _make_supervisor(
        resolve_port=lambda: "/dev/ttyACM0",
        verify_identity=lambda port: verify_calls.append(port) or "MATCH",
    )
    supervisor._disconnect_detected.set()
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: False)
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: popen_calls.append(a) or _FakeListenProcess([]))

    supervisor._listener_cycle()

    assert verify_calls == []
    assert popen_calls == []
    assert supervisor._disconnect_detected.is_set(), "must stay in the disconnect-recovery state, not give up"
    assert supervisor._identity_retry_attempt == 1


# --- Case 3: reappears with a different node_id -> not resumed, mismatch
# state set and surfaced. --------------------------------------------------


def test_disconnect_then_mismatch_halts_and_surfaces_without_starting_the_listener(monkeypatch):
    """Popen-never-called is asserted via a call-counting stub, not a
    raising side_effect - _listener_cycle()'s own broad `except Exception`
    around the Popen block would otherwise silently swallow a raised
    AssertionError, letting this test pass even if Popen were (wrongly)
    reached (confirmed by mutation: returning True instead of False from
    _await_identity_before_restart() on MISMATCH still passed against a
    raising stub, because the exception never escaped _listener_cycle())."""
    import meshsrv.serial_port_supervisor as spv_module

    mismatch_calls = []
    popen_calls = []
    supervisor = _make_supervisor(
        resolve_port=lambda: "/dev/ttyACM0",
        verify_identity=lambda port: "MISMATCH",
        on_identity_mismatch=mismatch_calls.append,
    )
    supervisor._disconnect_detected.set()
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)

    supervisor._listener_cycle()

    assert popen_calls == [], "must never Popen on a mismatch"
    assert supervisor.is_mismatch_active() is True
    assert supervisor.mismatch_port == "/dev/ttyACM0"
    assert mismatch_calls == ["/dev/ttyACM0"]
    assert supervisor._disconnect_detected.is_set() is False, "mismatch owns the wait now, not the disconnect-retry path"


def test_mismatch_active_never_popens_on_subsequent_cycles(monkeypatch):
    popen_calls = []
    supervisor = _make_supervisor()
    supervisor._mismatch_active.set()
    import meshsrv.serial_port_supervisor as spv_module
    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)

    supervisor._listener_cycle()

    assert popen_calls == []


def test_clear_mismatch_re_enters_the_disconnect_recovery_path_not_a_blind_restart():
    """clear_mismatch() (the Reconnect-radio flow) must re-verify from
    scratch, not assume the mismatch is resolved just because the user
    clicked something."""
    supervisor = _make_supervisor()
    supervisor._mismatch_active.set()
    supervisor._mismatch_port = "/dev/ttyACM0"

    supervisor.clear_mismatch()

    assert supervisor.is_mismatch_active() is False
    assert supervisor.mismatch_port == ""
    assert supervisor._disconnect_detected.is_set() is True
    assert supervisor._identity_retry_attempt == 0


# --- Case 7: intentional release -> no re-detection until Reconnect. -----


def test_intentional_pause_suppresses_disconnect_recovery_entirely(monkeypatch):
    """pause_listen (an intentional claim - Release radio, Node Tools,
    any claim_exclusive_access() caller) must win over disconnect-
    recovery/mismatch state - no --info probe, no Popen, while a claim is
    in progress, regardless of what the supervisor was doing before it.

    resolve_port()/os.path.exists() are both forced to say the device IS
    present - without this, the test would pass for the wrong reason (no
    real /dev/ttyACM0 on the machine running the suite short-circuits
    _await_identity_before_restart() before verify_identity() is ever
    reached, regardless of whether the pause_listen check is correct)."""
    import meshsrv.serial_port_supervisor as spv_module

    verify_calls = []
    popen_calls = []
    pause_listen = threading.Event()
    pause_listen.set()
    supervisor = _make_supervisor(
        pause_listen=pause_listen,
        resolve_port=lambda: "/dev/ttyACM0",
        verify_identity=lambda port: verify_calls.append(port) or "MATCH",
    )
    supervisor._disconnect_detected.set()  # a prior outage was mid-recovery when the claim started
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)

    supervisor._listener_cycle()

    assert verify_calls == [], "must not probe identity while an intentional claim is in progress"
    assert popen_calls == []
    assert supervisor._disconnect_detected.is_set(), "the recovery state must survive the claim, not be lost"


def test_terminate_if_device_changed_does_nothing_during_an_intentional_pause():
    pause_listen = threading.Event()
    pause_listen.set()
    supervisor = _make_supervisor(pause_listen=pause_listen)
    with supervisor._radio_lock:
        supervisor._listen_process = _FakeListenProcess([], exit_code=None)

    assert supervisor.terminate_if_device_changed() is False


# --- Case 6: quiet mesh (no lines for a long time, device present) ->
# listener NOT killed - terminate_if_device_changed() only ever reacts to
# an actual device-identity change, never to elapsed silence. -------------


def test_terminate_if_device_changed_false_when_device_is_unchanged(tmp_path):
    from meshsrv.serial_reconnect import capture_device_identity

    port_file = tmp_path / "ttyACM0"
    port_file.write_text("")
    supervisor = _make_supervisor(port=str(port_file))
    with supervisor._radio_lock:
        supervisor._listen_process = _FakeListenProcess([], exit_code=None)
        supervisor._device_identity_snapshot = capture_device_identity(str(port_file))

    assert supervisor.terminate_if_device_changed() is False
    assert supervisor._listen_process is not None


def test_terminate_if_device_changed_true_and_kills_when_the_device_node_is_gone(tmp_path):
    from meshsrv.serial_reconnect import capture_device_identity

    port_file = tmp_path / "ttyACM0"
    port_file.write_text("")
    supervisor = _make_supervisor(port=str(port_file))
    fake_process = _FakeListenProcess([], exit_code=None)
    with supervisor._radio_lock:
        supervisor._listen_process = fake_process
        supervisor._device_identity_snapshot = capture_device_identity(str(port_file))
    port_file.unlink()

    result = supervisor.terminate_if_device_changed()

    assert result is True
    assert fake_process.terminated is True
    assert supervisor._disconnect_detected.is_set() is True


def test_terminate_if_device_changed_false_when_nothing_is_running():
    supervisor = _make_supervisor()
    assert supervisor.terminate_if_device_changed() is False


# --- Case 4: boot without a radio -> health worker running -> radio
# appears -> MATCH -> listener started. -----------------------------------


def test_start_in_recovery_state_detection_error_enters_wait_for_device():
    """Boot-time DETECTION_ERROR/NOT_FOUND/NOT_CHECKED: enters the same
    wait-for-device-then-verify loop a live disconnect would, instead of
    refusing to ever start the listener thread."""
    supervisor = _make_supervisor()

    supervisor.start_in_recovery_state(mismatch=False)

    assert supervisor._disconnect_detected.is_set() is True
    assert supervisor.is_mismatch_active() is False


def test_start_in_recovery_state_mismatch_halts_immediately():
    """Boot-time MISMATCH: halts with no Popen, same as a live mismatch -
    a known-wrong radio needs no "wait for it" phase."""
    supervisor = _make_supervisor()

    supervisor.start_in_recovery_state(mismatch=True, port="/dev/ttyACM0")

    assert supervisor.is_mismatch_active() is True
    assert supervisor.mismatch_port == "/dev/ttyACM0"
    assert supervisor._disconnect_detected.is_set() is False


def test_boot_in_recovery_state_then_device_appears_and_matches_starts_the_listener(monkeypatch):
    """End-to-end for case 4: seed the DETECTION_ERROR boot state, then
    simulate the radio appearing and matching - the listener actually
    starts (a real Popen call happens)."""
    import meshsrv.serial_port_supervisor as spv_module

    popen_calls = []
    supervisor = _make_supervisor(
        resolve_port=lambda: "/dev/ttyACM0",
        verify_identity=lambda port: "MATCH",
    )
    supervisor.start_in_recovery_state(mismatch=False)

    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        spv_module.subprocess, "Popen",
        lambda *a, **k: popen_calls.append(a) or _FakeListenProcess(["line\n"], exit_code=0),
    )

    supervisor._listener_cycle()

    assert len(popen_calls) == 1
    assert supervisor._disconnect_detected.is_set() is False
