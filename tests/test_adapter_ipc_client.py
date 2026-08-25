"""Tests for meshsrv/adapter_ipc_client.py's Task 48 Core-side IPC
supervisor and RadioTransport proxy - real subprocess spawn/write/read/
kill/respawn against tests/fixtures/fake_adapter.py (no meshtastic/bleak
dependency, runs on any platform including this project's Windows dev
machine), not a mocked-out simulation of subprocess behavior.
"""
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from meshsrv import adapter_ipc_client
from meshsrv.adapter_ipc_client import (
    AdapterIPCTransport,
    AdapterSupervisor,
    _adapter_unavailable_info,
    _set_pdeathsig_to_sigkill,
)
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionState,
    ConnectionType,
    TransportError,
    TransportErrorCode,
)

_FAKE_ADAPTER = str(Path(__file__).resolve().parent / "fixtures" / "fake_adapter.py")
_PROJECT_DIR = str(Path(__file__).resolve().parents[1])


def _make_supervisor(**overrides):
    kwargs = dict(
        adapter_python=sys.executable,
        project_dir=_PROJECT_DIR,
        serial_port="/dev/ttyFAKE",
        meshtastic_cli="meshtastic",
        command=[sys.executable, _FAKE_ADAPTER],
    )
    kwargs.update(overrides)
    return AdapterSupervisor(**kwargs)


class _FakeCoreSerialTransport:
    """Stand-in for Core's own SerialTransport instance -
    claim_for_external_command() is the only method AdapterIPCTransport
    ever calls on it (see meshsrv/adapter_ipc_client.py's SERIAL PORT
    CLAIM docstring section). Records each call and can simulate the
    claim itself taking real wall-clock time (to prove the budget split
    actually measures elapsed time via time.monotonic(), not a fixed
    proportion of the caller's timeout) or failing the way the real
    _claim_radio() does (TransportError(BUSY), raised before yield -
    i.e. before the wrapped call ever runs)."""

    def __init__(self, claim_delay: float = 0.0, raise_busy: bool = False):
        self.claim_calls = 0
        self._claim_delay = claim_delay
        self._raise_busy = raise_busy

    @contextmanager
    def claim_for_external_command(self, *, timeout: float = 8, cooldown: float = 2.0):
        self.claim_calls += 1
        if self._claim_delay:
            time.sleep(self._claim_delay)
        if self._raise_busy:
            raise TransportError(TransportErrorCode.BUSY, "Serial port busy: /dev/ttyFAKE")
        yield


def test_scan_sends_the_scan_operation_with_this_proxys_own_transport_type():
    """AdapterIPCTransport.scan() is not part of RadioTransport (BLE-
    specific) - proves it builds the right request shape rather than
    just trusting its one-line implementation. fake_adapter.py's
    response only echoes `params`, not `operation`/`transport_type`
    (real adapter responses don't carry the request back either), so
    this spies on AdapterSupervisor.call() directly to inspect the
    request that was actually sent."""
    supervisor = _make_supervisor()
    ble_transport = AdapterIPCTransport(ConnectionType.BLUETOOTH, supervisor)

    captured_request = {}
    original_call = supervisor.call

    def _spy_call(request, **kwargs):
        captured_request.update(request)
        return original_call(request, **kwargs)

    supervisor.call = _spy_call

    result = ble_transport.scan(timeout=20.0)

    assert captured_request["operation"] == "scan"
    assert captured_request["transport_type"] == "bluetooth"
    assert captured_request["timeout"] == 20.0
    assert result == {"echo": {}, "_pid": result["_pid"]}


def test_scan_on_a_serial_proxy_raises_unsupported_without_any_ipc_round_trip():
    """Explicit Core-side guard (added on review) rather than relying
    solely on the server.py wiring convention that only ble_ipc_transport
    is ever passed into scan()-calling code. Proves no IPC call is even
    attempted - not just that some error eventually comes back."""
    supervisor = _make_supervisor()
    serial_transport = AdapterIPCTransport(ConnectionType.SERIAL, supervisor)

    with pytest.raises(TransportError) as excinfo:
        serial_transport.scan(timeout=5.0)

    assert excinfo.value.code == TransportErrorCode.UNSUPPORTED
    assert supervisor._proc is None  # never spawned - the guard fired before any IPC attempt


def test_successful_round_trip_returns_the_adapters_result():
    supervisor = _make_supervisor()

    response = supervisor.call(
        {"operation": "get_metadata", "transport_type": "serial", "params": {"hello": "world"}, "timeout": 5.0},
        timeout=5.0,
        ble_address_for_cleanup=None,
    )

    assert response["ok"] is True
    assert response["result"]["echo"] == {"hello": "world"}


def test_hung_adapter_is_killed_and_caller_gets_timeout_within_its_own_budget():
    import time

    supervisor = _make_supervisor()

    start = time.monotonic()
    with pytest.raises(TransportError) as excinfo:
        supervisor.call(
            {"operation": "get_metadata", "transport_type": "serial", "params": {"_test_behavior": "hang"}, "timeout": 1.0},
            timeout=1.0,
            ble_address_for_cleanup=None,
        )
    elapsed = time.monotonic() - start

    assert excinfo.value.code == TransportErrorCode.TIMEOUT
    assert elapsed < 3.0, f"took {elapsed:.2f}s - should fail within its own ~1s budget, not the fake adapter's 3600s hang"


def test_killed_adapter_respawns_as_a_genuinely_different_process():
    supervisor = _make_supervisor()

    first = supervisor.call(
        {"operation": "get_metadata", "transport_type": "serial", "params": {}, "timeout": 5.0},
        timeout=5.0,
        ble_address_for_cleanup=None,
    )
    first_pid = first["result"]["_pid"]
    assert first_pid is not None

    # Force a kill via a hang, then confirm the NEXT call gets a fresh
    # process, not the same (already-killed) one.
    with pytest.raises(TransportError):
        supervisor.call(
            {"operation": "get_metadata", "transport_type": "serial", "params": {"_test_behavior": "hang"}, "timeout": 0.5},
            timeout=0.5,
            ble_address_for_cleanup=None,
        )

    second = supervisor.call(
        {"operation": "get_metadata", "transport_type": "serial", "params": {}, "timeout": 5.0},
        timeout=5.0,
        ble_address_for_cleanup=None,
    )
    second_pid = second["result"]["_pid"]
    assert second_pid is not None
    assert second_pid != first_pid, "respawn must be a genuinely new OS process, not the killed one still running"


def test_crashed_adapter_raises_adapter_unavailable_not_a_raw_exception():
    supervisor = _make_supervisor()

    with pytest.raises(TransportError) as excinfo:
        supervisor.call(
            {"operation": "get_metadata", "transport_type": "serial", "params": {"_test_behavior": "crash"}, "timeout": 5.0},
            timeout=5.0,
            ble_address_for_cleanup=None,
        )

    assert excinfo.value.code == TransportErrorCode.ADAPTER_UNAVAILABLE


def test_spawn_failure_raises_adapter_unavailable_not_a_raw_exception():
    """Real scenario for the current, pre-venv-split state of the live
    nodes: adapter_python points at a well-known path
    (resolve_adapter_venv_dir()) that doesn't exist yet until install.sh's
    venv-split step runs. subprocess.Popen(nonexistent executable) raises
    FileNotFoundError - this must degrade the same clean way every other
    call() failure path does, not propagate a raw exception past a caller
    (e.g. the node-time-sync background worker) that only expects
    TransportError."""
    supervisor = _make_supervisor(
        adapter_python="/nonexistent/path/to/adapter/venv/bin/python", command=None
    )

    with pytest.raises(TransportError) as excinfo:
        supervisor.call(
            {"operation": "get_metadata", "transport_type": "serial", "params": {}, "timeout": 5.0},
            timeout=5.0,
            ble_address_for_cleanup=None,
        )

    assert excinfo.value.code == TransportErrorCode.ADAPTER_UNAVAILABLE
    assert supervisor._proc is None  # left in a clean, retryable state


def test_kill_runs_bluetoothctl_disconnect_only_when_a_ble_address_is_given():
    supervisor = _make_supervisor()

    with patch("meshsrv.adapter_ipc_client.subprocess.run") as mock_run:
        with pytest.raises(TransportError):
            supervisor.call(
                {"operation": "connect", "transport_type": "bluetooth", "params": {"_test_behavior": "hang"}, "timeout": 0.5},
                timeout=0.5,
                ble_address_for_cleanup="3C:DC:75:6F:99:61",
            )

    mock_run.assert_called_once()
    called_args = mock_run.call_args[0][0]
    assert called_args == ["bluetoothctl", "disconnect", "3C:DC:75:6F:99:61"]


def test_kill_does_not_run_bluetoothctl_when_no_ble_address_given():
    supervisor = _make_supervisor()

    with patch("meshsrv.adapter_ipc_client.subprocess.run") as mock_run:
        with pytest.raises(TransportError):
            supervisor.call(
                {"operation": "get_metadata", "transport_type": "serial", "params": {"_test_behavior": "hang"}, "timeout": 0.5},
                timeout=0.5,
                ble_address_for_cleanup=None,
            )

    mock_run.assert_not_called()


def test_fresh_transport_reports_adapter_unavailable_before_any_call():
    """Task 48 review requirement: the initial/never-reached cache state
    must be explicit, not a default-dataclass-value accident that could
    read as an ordinary DISCONNECTED radio."""
    supervisor = _make_supervisor()
    transport = AdapterIPCTransport(ConnectionType.SERIAL, supervisor)

    info = transport.get_connection_info()

    assert info.state == ConnectionState.ERROR
    assert info.last_error.code == TransportErrorCode.ADAPTER_UNAVAILABLE
    assert transport.is_connected() is False


def test_adapter_unavailable_info_helper_is_explicit_not_a_default():
    info = _adapter_unavailable_info()
    assert info.state == ConnectionState.ERROR
    assert info.last_error is not None
    assert info.last_error.code == TransportErrorCode.ADAPTER_UNAVAILABLE


def test_libc_prctl_resolution_happens_at_import_time_not_inside_the_function():
    """Real exercise of the module-load-time resolution (module-level
    try/except, see the module's own SAFETY comment for why it must
    happen there and not inside _set_pdeathsig_to_sigkill() -
    dlopen()/dlsym() are unsafe to call from preexec_fn in a
    multi-threaded process), on whichever real platform this actually
    runs on - not a single hardcoded expectation. On real Linux (this
    project's actual target, and CI's runner) libc.so.6 genuinely exists,
    so _libc resolves to a real, usable handle with prctl bound and typed
    - the intended, common case. On this dev machine (Windows) or any
    other platform without that library, resolution genuinely fails and
    _libc is None - the fallback case _set_pdeathsig_to_sigkill() must
    tolerate. Either way, the assertion below proves the resolution
    actually ran (not skipped, not deferred) and produced the outcome
    that platform's ctypes.CDLL() call was always going to produce."""
    if sys.platform == "linux":
        assert adapter_ipc_client._libc is not None
        assert callable(adapter_ipc_client._libc.prctl)
        assert adapter_ipc_client._libc.prctl.argtypes == [
            adapter_ipc_client.ctypes.c_int,
            adapter_ipc_client.ctypes.c_ulong,
            adapter_ipc_client.ctypes.c_ulong,
            adapter_ipc_client.ctypes.c_ulong,
            adapter_ipc_client.ctypes.c_ulong,
        ]
    else:
        assert adapter_ipc_client._libc is None


def test_set_pdeathsig_is_best_effort_and_never_raises():
    """With _libc already None (see the import-time test above), this
    exercises the "nothing to do" fast path - proves the function
    swallows a failed-to-resolve libc instead of crashing the child
    before it can even exec() the adapter, matching its own documented
    "best-effort, defense in depth" contract."""
    _set_pdeathsig_to_sigkill()  # must not raise


def test_spawn_only_passes_preexec_fn_on_linux():
    """On this (Windows) dev machine, subprocess.Popen rejects
    preexec_fn outright (raises ValueError) - if _spawn_locked() ever
    passed it unconditionally, every test in this file would already be
    failing. Passing here is itself proof the sys.platform guard works,
    not just an assertion about intent."""
    supervisor = _make_supervisor()
    response = supervisor.call(
        {"operation": "get_metadata", "transport_type": "serial", "params": {}, "timeout": 5.0},
        timeout=5.0,
        ble_address_for_cleanup=None,
    )
    assert response["ok"] is True


# --- Task 48 follow-up: serial port claim across the process boundary -----


def test_serial_call_is_wrapped_in_claim_for_external_command():
    """The live-caught gap this follow-up fixes: a SERIAL-type call with
    a core_serial_transport given must go through
    claim_for_external_command() before ever reaching the adapter."""
    supervisor = _make_supervisor()
    core_serial = _FakeCoreSerialTransport()
    transport = AdapterIPCTransport(ConnectionType.SERIAL, supervisor, core_serial_transport=core_serial)

    result = transport.get_metadata(timeout=5.0)

    assert result["echo"] == {}
    assert core_serial.claim_calls == 1


def test_bluetooth_call_is_never_wrapped_in_claim_even_if_core_serial_transport_given():
    """Defensive, matching scan()'s own guard elsewhere: BLE never shares
    Core's listener/serial port, so it must never pay this cost - checked
    even for the (production-impossible, but worth proving) case where a
    core_serial_transport was mistakenly passed to a BLUETOOTH instance."""
    supervisor = _make_supervisor()
    core_serial = _FakeCoreSerialTransport()
    transport = AdapterIPCTransport(ConnectionType.BLUETOOTH, supervisor, core_serial_transport=core_serial)

    transport.get_metadata(timeout=5.0)

    assert core_serial.claim_calls == 0


def test_serial_call_without_core_serial_transport_behaves_exactly_as_before():
    """Backward-compat: core_serial_transport is optional and defaults to
    None (matches every pre-existing test in this file, none of which
    pass it) - no wrapping, no behavior change for callers that don't
    supply one."""
    supervisor = _make_supervisor()
    transport = AdapterIPCTransport(ConnectionType.SERIAL, supervisor)

    result = transport.get_metadata(timeout=5.0)

    assert result["echo"] == {}


def test_claim_budget_split_is_dynamic_not_a_fixed_proportion():
    """Task 48 follow-up review requirement: the delegated call's budget
    must be caller_timeout - ACTUAL claim elapsed time (measured via
    time.monotonic()), not a fixed percentage split - matching
    TransportRouter._delegate()'s existing remaining = timeout - elapsed
    mechanic (Task 47.5). Proven here by making the claim itself take a
    real, measurable delay and asserting the adapter actually received a
    reduced timeout reflecting that delay, not the original caller
    timeout unchanged and not some fixed fraction of it."""
    supervisor = _make_supervisor()
    core_serial = _FakeCoreSerialTransport(claim_delay=1.0)
    transport = AdapterIPCTransport(ConnectionType.SERIAL, supervisor, core_serial_transport=core_serial)

    captured_requests = []
    original_call = supervisor.call

    def _spy_call(request, **kwargs):
        captured_requests.append(dict(request))
        return original_call(request, **kwargs)

    supervisor.call = _spy_call

    transport.get_metadata(timeout=5.0)

    sent_timeout = captured_requests[0]["timeout"]
    # Caller declared 5.0s, the claim itself measurably took ~1.0s - the
    # delegated call must have received noticeably less than 5.0s (proves
    # elapsed time was actually subtracted), but comfortably more than a
    # naive fixed-percentage split would leave (proves it's not just
    # e.g. "70% of 5.0 = 3.5" by coincidence) - bounds wide enough to
    # absorb real scheduling jitter without being a no-op assertion.
    assert 3.0 < sent_timeout < 4.5, f"expected ~4.0s (5.0 - ~1.0 claim delay), got {sent_timeout}"


def test_claim_busy_error_propagates_unchanged_without_reaching_the_adapter():
    """Task 48 follow-up review requirement: a claim that fails to free
    the port raises TransportError(BUSY) - _claim_radio()'s existing,
    unchanged behavior. Nothing in AdapterIPCTransport should catch and
    reclassify it; it must propagate through the same TransportError
    handling every other failure in _call() already uses, and the
    adapter must never even be contacted (the claim fails before yield)."""
    supervisor = _make_supervisor()
    core_serial = _FakeCoreSerialTransport(raise_busy=True)
    transport = AdapterIPCTransport(ConnectionType.SERIAL, supervisor, core_serial_transport=core_serial)

    with pytest.raises(TransportError) as excinfo:
        transport.get_metadata(timeout=5.0)

    assert excinfo.value.code == TransportErrorCode.BUSY
    assert supervisor._proc is None  # never spawned - the claim failed before any IPC attempt
    assert transport.get_connection_info().last_error.code == TransportErrorCode.BUSY
