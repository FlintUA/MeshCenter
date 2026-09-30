"""Tests for meshsrv/radio_identity.py's probe_tcp_radio_identity() - the
ephemeral TCP identity probe (TCP lifecycle P0, PR-B). Everything is DI'd
fakes: what matters here is WHICH transport gets touched and in what order.

The invariants: (1) the production ("live") transport is never connected,
disconnected or mutated by a probe; (2) a probe against the endpoint the
production session already holds opens NO second connection; (3) every
probe that started a process shuts it down, on success and on every failure
path; (4) probes are serialized, and the loser gets DETECTION_IN_PROGRESS.
"""
import threading

from meshsrv.radio_identity import DETECTION_IN_PROGRESS, probe_tcp_radio_identity
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    NodeInfo,
    NodeUser,
    TransportError,
    TransportErrorCode,
)

HOST, PORT = "192.168.2.34", 4403
ADDRESS = f"{HOST}:{PORT}"


class _Recorder:
    def __init__(self):
        self.events = []


class _FakeLive:
    """Stands in for the production tcp_ipc_transport."""

    def __init__(self, *, state=ConnectionState.DISCONNECTED, address=None, node_id="!1fa065f0", read_error=None):
        self._state = state
        self._address = address
        self._node_id = node_id
        self._read_error = read_error
        self.calls = []

    def get_connection_info(self):
        self.calls.append("get_connection_info")
        descriptor = (
            ConnectionDescriptor(type=ConnectionType.TCP, address=self._address) if self._address else None
        )
        return ConnectionInfo(state=self._state, descriptor=descriptor, node_id=self._node_id)

    def connect(self, *a, **k):
        self.calls.append("connect")

    def disconnect(self, *a, **k):
        self.calls.append("disconnect")

    def get_local_node(self, *, timeout):
        self.calls.append("get_local_node")
        if self._read_error is not None:
            raise self._read_error
        user = NodeUser(id=self._node_id, long_name="T-Beam", short_name="TBM", hw_model="TBEAM")
        return NodeInfo(node_id=self._node_id, num=1, user=user)

    def get_metadata(self, *, timeout):
        self.calls.append("get_metadata")
        return {"metadata_json": '{"firmwareVersion": "2.7.15.567b8ea"}'}


class _FakeProbe:
    """Stands in for tcp_probe_ipc_transport (its own adapter process)."""

    def __init__(self, recorder, *, node_id="!1fa065f0", warmup_error=None, connect_error=None, read_error=None):
        self._rec = recorder
        self._node_id = node_id
        self._warmup_error = warmup_error
        self._connect_error = connect_error
        self._read_error = read_error

    def disconnect(self, *, timeout=15.0):
        self._rec.events.append("warmup")
        if self._warmup_error is not None:
            raise self._warmup_error

    def connect(self, descriptor, *, force=False, timeout=30.0):
        self._rec.events.append(f"connect:{descriptor.address}")
        if self._connect_error is not None:
            raise self._connect_error

    def get_local_node(self, *, timeout):
        self._rec.events.append("read")
        if self._read_error is not None:
            raise self._read_error
        user = NodeUser(id=self._node_id, long_name="T-Beam", short_name="TBM", hw_model="TBEAM")
        return NodeInfo(node_id=self._node_id, num=1, user=user)

    def get_metadata(self, *, timeout):
        return {"metadata_json": "{}"}


def _probe(live, probe, recorder, lock=None, **kw):
    return probe_tcp_radio_identity(
        HOST,
        PORT,
        live_transport=live,
        probe_transport=probe,
        probe_shutdown=lambda: recorder.events.append("shutdown"),
        probe_lock=lock or threading.Lock(),
        **kw,
    )


# --- same endpoint as the live production session ----------------------


def test_same_endpoint_live_reads_from_the_live_session_and_opens_no_second_connection():
    rec = _Recorder()
    live = _FakeLive(state=ConnectionState.CONNECTED, address=ADDRESS)
    probe = _FakeProbe(rec)

    result, _ = _probe(live, probe, rec)

    assert result["status"] == "MATCH"
    assert result["detected"]["node_id"] == "!1fa065f0"
    assert rec.events == [], "no probe process, no warm-up, no connect, no shutdown"
    assert "connect" not in live.calls and "disconnect" not in live.calls


def test_same_endpoint_match_is_case_insensitive_on_the_address():
    rec = _Recorder()
    live = _FakeLive(state=ConnectionState.CONNECTED, address="Radio.Local:4403")
    probe = _FakeProbe(rec)

    result, _ = probe_tcp_radio_identity(
        "radio.local", 4403, live_transport=live, probe_transport=probe,
        probe_shutdown=lambda: rec.events.append("shutdown"), probe_lock=threading.Lock(),
    )

    assert result["status"] == "MATCH"
    assert rec.events == []


def test_stale_live_session_is_an_honest_error_never_a_fallback_second_connect():
    """The cached state says CONNECTED but the read fails (link actually
    dead). A fallback probe connect here would be exactly the duplicate
    connection this whole change exists to remove."""
    rec = _Recorder()
    live = _FakeLive(
        state=ConnectionState.CONNECTED,
        address=ADDRESS,
        read_error=TransportError(TransportErrorCode.NOT_CONNECTED, "TCPTransport is not connected"),
    )
    probe = _FakeProbe(rec)

    result, _ = _probe(live, probe, rec)

    assert result["status"] == "DETECTION_ERROR"
    assert "not responding" in result["error"]
    assert result["error_code"] == "not_connected"
    assert rec.events == [], "must not open any second connection"
    assert "connect" not in live.calls


# --- everything else uses the ephemeral probe process -------------------


def test_probe_runs_warmup_connect_read_then_always_shuts_down():
    rec = _Recorder()
    live = _FakeLive()  # production not connected to anything
    probe = _FakeProbe(rec)

    result, _ = _probe(live, probe, rec)

    assert result["status"] == "MATCH"
    assert rec.events == ["warmup", f"connect:{ADDRESS}", "read", "shutdown"]


def test_probe_of_a_different_endpoint_never_touches_the_live_production_transport():
    rec = _Recorder()
    live = _FakeLive(state=ConnectionState.CONNECTED, address="192.168.2.99:4403")
    probe = _FakeProbe(rec)

    result, _ = _probe(live, probe, rec)

    assert result["status"] == "MATCH"
    assert live.calls == ["get_connection_info"], "only the cached-info read, nothing that mutates"
    assert rec.events[0] == "warmup" and rec.events[-1] == "shutdown"


def test_production_in_error_state_on_the_same_endpoint_still_uses_the_probe_process():
    """Only a CONNECTED production session owns the endpoint."""
    rec = _Recorder()
    live = _FakeLive(state=ConnectionState.ERROR, address=ADDRESS)
    probe = _FakeProbe(rec)

    _probe(live, probe, rec)

    assert "get_local_node" not in live.calls
    assert f"connect:{ADDRESS}" in rec.events


def test_connect_failure_returns_the_specific_error_and_still_shuts_down():
    rec = _Recorder()
    probe = _FakeProbe(
        rec, connect_error=TransportError(TransportErrorCode.CONNECT_REFUSED, "192.168.2.34:4403 refused")
    )

    result, _ = _probe(_FakeLive(), probe, rec)

    assert result["status"] == "DETECTION_ERROR"
    assert result["error_code"] == "connect_refused"
    assert rec.events[-1] == "shutdown"


def test_post_connect_read_failure_still_shuts_down():
    rec = _Recorder()
    probe = _FakeProbe(rec, read_error=TransportError(TransportErrorCode.TIMEOUT, "identity read timed out"))

    result, _ = _probe(_FakeLive(), probe, rec)

    assert result["status"] == "DETECTION_ERROR"
    assert result["error_code"] == "timeout"
    assert rec.events[-1] == "shutdown"


def test_warmup_failure_is_reported_skips_connect_and_still_shuts_down():
    rec = _Recorder()
    probe = _FakeProbe(
        rec, warmup_error=TransportError(TransportErrorCode.ADAPTER_UNAVAILABLE, "failed to launch adapter")
    )

    result, _ = _probe(_FakeLive(), probe, rec)

    assert result["status"] == "DETECTION_ERROR"
    assert result["error_code"] == "adapter_unavailable"
    assert "could not be started" in result["error"]
    assert not any(e.startswith("connect:") for e in rec.events)
    assert rec.events[-1] == "shutdown"


def test_lock_is_released_after_every_outcome():
    rec = _Recorder()
    lock = threading.Lock()

    _probe(_FakeLive(), _FakeProbe(rec), rec, lock=lock)
    assert lock.acquire(blocking=False), "released after success"
    lock.release()

    _probe(_FakeLive(), _FakeProbe(rec, connect_error=TransportError(TransportErrorCode.TIMEOUT, "x")), rec, lock=lock)
    assert lock.acquire(blocking=False), "released after failure"
    lock.release()


# --- concurrency ---------------------------------------------------------


def test_a_second_concurrent_probe_gets_detection_in_progress_and_touches_nothing():
    rec = _Recorder()
    lock = threading.Lock()
    lock.acquire()  # another detection is running
    try:
        result, _ = _probe(_FakeLive(), _FakeProbe(rec), rec, lock=lock, lock_timeout=0.2)
    finally:
        lock.release()

    assert result["status"] == "DETECTION_ERROR"
    assert result["error_code"] == DETECTION_IN_PROGRESS
    assert rec.events == [], "the loser must not spawn, connect or shut down the winner's process"


def test_same_endpoint_live_read_does_not_need_the_probe_lock():
    rec = _Recorder()
    lock = threading.Lock()
    lock.acquire()
    try:
        result, _ = _probe(
            _FakeLive(state=ConnectionState.CONNECTED, address=ADDRESS), _FakeProbe(rec), rec,
            lock=lock, lock_timeout=0.2,
        )
    finally:
        lock.release()

    assert result["status"] == "MATCH"


def test_no_host_short_circuits():
    rec = _Recorder()
    result, _ = probe_tcp_radio_identity(
        "", PORT, live_transport=_FakeLive(), probe_transport=_FakeProbe(rec),
        probe_shutdown=lambda: rec.events.append("shutdown"), probe_lock=threading.Lock(),
    )

    assert result["status"] == "DETECTION_ERROR"
    assert result["error"] == "No TCP host configured"
    assert rec.events == []
