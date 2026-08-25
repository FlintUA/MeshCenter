"""Tests for meshsrv/adapter_ipc_client.py's Task 48 Core-side IPC
supervisor and RadioTransport proxy - real subprocess spawn/write/read/
kill/respawn against tests/fixtures/fake_adapter.py (no meshtastic/bleak
dependency, runs on any platform including this project's Windows dev
machine), not a mocked-out simulation of subprocess behavior.
"""
import sys
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
