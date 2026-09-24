"""Tests for server.py's restore_active_transport() and
verify_radio_identity()'s transport-aware branch (Radio TCP Transport,
part 2).

restore_active_transport() is start_runtime()'s own TRANSPORT RESTORE
step, extracted into a standalone top-level function specifically so it's
testable without invoking the rest of start_runtime()'s heavier startup
sequence (background worker threads, GitHub update checks, etc. - none of
which any existing test in this suite invokes either, since start_runtime()
itself is never called by the test suite, only by wsgi.py/__main__ - see
tests/conftest.py's server_module fixture docstring).

Uses the server_module fixture (server.py already imported, start_runtime()
never called) and monkeypatches module-level globals for the duration of
each test - the same technique tests/test_verify_radio_identity_logging.py
already uses for detect_radio_identity().
"""
import threading

import pytest

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
from meshsrv.transport_router import TransportRouter


class _FakeTransport:
    """Minimal RadioTransport stand-in - connects successfully unless
    `fail` is set, records connect() calls. `initial_descriptor`
    (default None, preserving every existing test's behavior) lets a
    test simulate "already connected to a specific endpoint" WITHOUT a
    connect() call ever happening - the double-connect-fix regression
    tests below need this to prove restore_active_transport() reuses an
    already-live connection instead of tearing it down and redoing the
    handshake."""

    def __init__(self, fail=False, node_id="!1fa065f0", nodes=None, initial_descriptor=None, initial_state=None):
        self.fail = fail
        self.node_id = node_id
        self.connect_calls = []
        self._nodes = nodes or []
        self._descriptor = initial_descriptor
        # Defaults to CONNECTED (preserving every existing test's
        # behavior) unless a test explicitly wants to simulate a link
        # that dropped between verify_radio_identity()'s own connect and
        # restore_active_transport()'s reuse check - see the "connection
        # disappeared between checks" regression test below.
        self._state = initial_state if initial_state is not None else ConnectionState.CONNECTED

    def connect(self, descriptor, *, force=False, timeout=30.0):
        self.connect_calls.append(descriptor)
        if self.fail:
            raise TransportError(TransportErrorCode.CONNECT_FAILED, "simulated failure")
        self._descriptor = descriptor
        self._state = ConnectionState.CONNECTED
        return self.get_connection_info()

    def disconnect(self, *, timeout=15.0):
        pass

    def get_connection_info(self):
        return ConnectionInfo(state=self._state, descriptor=self._descriptor, node_id=self.node_id)

    def get_nodes(self, *, timeout=15.0):
        return self._nodes


@pytest.fixture
def _preserve_transport_router_state(server_module):
    """restore_active_transport()/verify_radio_identity() mutate module-
    level globals (transport_router._active, nodes, chats, INSTANCE_IDENTITY,
    RADIO_IDENTITY_RESULT) not covered by conftest.py's autouse reset -
    restore them explicitly so this file's tests never leak state into
    others sharing the session-scoped server_module fixture."""
    original_active = server_module.transport_router._active
    original_nodes = dict(server_module.nodes)
    original_chats = dict(server_module.chats)
    original_identity = server_module.instance_manager.get()
    original_radio_result = dict(server_module.RADIO_IDENTITY_RESULT)
    yield
    server_module.transport_router._active = original_active
    server_module.nodes.clear()
    server_module.nodes.update(original_nodes)
    server_module.chats.clear()
    server_module.chats.update(original_chats)
    server_module.instance_manager.save(original_identity)
    server_module.INSTANCE_IDENTITY = original_identity
    server_module.RADIO_IDENTITY_RESULT = original_radio_result


# ---------------------------------------------------------------------------
# restore_active_transport()
# ---------------------------------------------------------------------------

def test_restore_active_transport_is_a_noop_for_serial(server_module, _preserve_transport_router_state, monkeypatch):
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport()
    fake_tcp = _FakeTransport()
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    server_module.restore_active_transport(
        {"transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}}, identity_match=True
    )

    assert fake_serial.connect_calls == []
    assert fake_ble.connect_calls == []
    assert fake_tcp.connect_calls == []


def test_restore_active_transport_switches_router_to_bluetooth(server_module, _preserve_transport_router_state, monkeypatch):
    """The dedicated regression check the task explicitly asked for:
    Bluetooth restore after restart actually reconnects and re-points
    transport_router, not merely "doesn't crash" as an incidental
    side-effect of the serial/tcp refactor."""
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport(node_id="!756f9960")
    fake_tcp = _FakeTransport()
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    server_module.restore_active_transport({
        "transport": "bluetooth",
        "endpoint": {"address": "3C:DC:75:6F:99:61", "label": "FLT2_9960"},
    }, identity_match=False)  # BLE has no identity check - always False, must still restore

    assert server_module.transport_router._active is fake_ble
    assert len(fake_ble.connect_calls) == 1
    assert fake_ble.connect_calls[0].address == "3C:DC:75:6F:99:61"
    assert fake_ble.connect_calls[0].label == "FLT2_9960"
    assert server_module.transport_router.get_connection_info().node_id == "!756f9960"


def test_restore_active_transport_switches_router_to_tcp_and_seeds_nodes(server_module, _preserve_transport_router_state, monkeypatch):
    # Deliberately NOT "!aabbccdd" - that's the synthetic test config's
    # own LOCAL_NODE_ID (tests/conftest.py), and seed_nodes_from_transport()
    # correctly skips the local node the same way parse_nodes_from_info()
    # does - using it here would test the skip-local-node behavior by
    # accident instead of the seed-a-remote-node behavior this test is for.
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport()
    remote_node = NodeInfo(
        node_id="!11223344",
        num=0x11223344,
        user=NodeUser(id="!11223344", long_name="Remote Node", short_name="RMT", hw_model="TBEAM"),
    )
    fake_tcp = _FakeTransport(node_id="!1fa065f0", nodes=[remote_node])
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    server_module.restore_active_transport({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, identity_match=True)

    assert server_module.transport_router._active is fake_tcp
    assert fake_tcp.connect_calls[0].address == "192.168.2.34:4403"
    # One-time node seed from the TCP transport's own initial NodeDB.
    assert "!11223344" in server_module.nodes
    assert server_module.nodes["!11223344"]["name"] == "Remote Node"


def test_restore_active_transport_reuses_an_already_connected_tcp_link_without_reconnecting(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """Double-connect fix (follow-up investigation after PR #279):
    verify_radio_identity() (called earlier in start_runtime(), moments
    before restore_active_transport()) already connected tcp_ipc_transport
    to this exact endpoint as part of its own identity probe -
    reconnecting from scratch here would tear down a perfectly good,
    seconds-old link and redo the entire handshake for no reason,
    doubling the number of TCP connect attempts on every single boot.
    A fake already reporting CONNECTED with a matching descriptor (no
    connect() call simulated at all) proves restore_active_transport()
    detects and reuses it - zero connect_calls, router still correctly
    points at it."""
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport()
    remote_node = NodeInfo(
        node_id="!11223344",
        num=0x11223344,
        user=NodeUser(id="!11223344", long_name="Remote Node", short_name="RMT", hw_model="TBEAM"),
    )
    fake_tcp = _FakeTransport(
        node_id="!1fa065f0",
        nodes=[remote_node],
        initial_descriptor=ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403"),
    )
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    server_module.restore_active_transport({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, identity_match=True)

    assert server_module.transport_router._active is fake_tcp
    assert fake_tcp.connect_calls == [], "must reuse the already-connected link, not call connect() again"
    # The one-time node seed must still happen even on the reuse path -
    # it's the whole reason a TCP restore needs to run at all.
    assert "!11223344" in server_module.nodes


def test_restore_active_transport_reconnects_when_the_cached_endpoint_does_not_match(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """Negative test guarding the reuse fast-path's own scope: a
    tcp_ipc_transport that's connected to a DIFFERENT endpoint (or has
    no descriptor at all) must not be trusted - falls through to a real
    connect() against the actual restore target, same as before this fix."""
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport()
    fake_tcp = _FakeTransport(
        node_id="!1fa065f0",
        initial_descriptor=ConnectionDescriptor(type=ConnectionType.TCP, address="10.0.0.5:4403"),
    )
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    server_module.restore_active_transport({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, identity_match=True)

    assert len(fake_tcp.connect_calls) == 1
    assert fake_tcp.connect_calls[0].address == "192.168.2.34:4403"


def test_restore_active_transport_reconnects_when_the_connection_no_longer_shows_connected(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """Negative test distinct from the wrong-endpoint one above: same
    target endpoint as verify_radio_identity() just probed, but the link
    no longer reports CONNECTED (e.g. it dropped in the moments between
    that probe and this restore step running) - must not be trusted as
    reusable either. Falls through to a real connect(), same as any other
    not-actually-connected case."""
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport()
    fake_tcp = _FakeTransport(
        node_id="!1fa065f0",
        initial_descriptor=ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403"),
        initial_state=ConnectionState.DISCONNECTED,
    )
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    server_module.restore_active_transport({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, identity_match=True)

    assert len(fake_tcp.connect_calls) == 1
    assert fake_tcp.connect_calls[0].address == "192.168.2.34:4403"


def test_restore_active_transport_skips_tcp_entirely_on_identity_mismatch(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """Identity-safety regression: a TCP identity probe that just found
    MISMATCH/NOT_FOUND at this endpoint must not be followed by this
    function connecting to it anyway and seeding its (unverified/wrong)
    nodes into the accepted profile - no connect attempt at all, router
    stays on whatever it already was, nodes/chats untouched."""
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport()
    remote_node = NodeInfo(
        node_id="!99999999",
        num=0x99999999,
        user=NodeUser(id="!99999999", long_name="Wrong Radio", short_name="WRG", hw_model="TBEAM"),
    )
    fake_tcp = _FakeTransport(node_id="!deadbeef", nodes=[remote_node])
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    server_module.restore_active_transport({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, identity_match=False)

    assert fake_tcp.connect_calls == []
    assert server_module.transport_router._active is fake_serial
    assert "!99999999" not in server_module.nodes


def test_restore_active_transport_failure_is_logged_not_raised(server_module, _preserve_transport_router_state, monkeypatch):
    """Best-effort per this function's own docstring - a failed restore
    (radio unreachable at boot) must never propagate out and take down
    start_runtime()/Core with it."""
    fake_serial = _FakeTransport()
    fake_ble = _FakeTransport()
    fake_tcp = _FakeTransport(fail=True)
    monkeypatch.setattr(server_module, "transport_router", TransportRouter(fake_serial))
    monkeypatch.setattr(server_module, "serial_ipc_transport", fake_serial)
    monkeypatch.setattr(server_module, "ble_ipc_transport", fake_ble)
    monkeypatch.setattr(server_module, "tcp_ipc_transport", fake_tcp)

    logged = []
    monkeypatch.setattr(
        server_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )

    server_module.restore_active_transport({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, identity_match=True)  # must not raise

    assert len(logged) == 1
    assert logged[0]["title"] == "Radio transport restore failed"
    assert logged[0]["level"] == "WARNING"
    # Router never reassigned to the transport that just failed to connect.
    assert server_module.transport_router._active is fake_serial


def test_restore_active_transport_against_the_real_unreachable_adapter_does_not_crash(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """Uses the REAL tcp_ipc_transport (an AdapterIPCTransport pointed at
    whatever adapter venv path this sandbox resolves to, which does not
    exist here - no hardware, no adapter subprocess in CI) rather than a
    fake - proves the actual production code path degrades gracefully
    against a genuinely-unreachable adapter, not just a controlled fake
    failure. Timeout patched down from the real 90s default as a safety
    net (bounds this test's own worst case); in practice the actual delay
    here comes from subprocess.Popen()'s own OS-level failure-to-spawn
    resolution against a nonexistent interpreter path (observed ~11s on
    this sandbox, independent of the connect timeout value), not from
    AdapterSupervisor's own timeout machinery being exercised."""
    monkeypatch.setattr(server_module, "SWITCH_CONNECT_TIMEOUT_S", 3.0)
    server_module.restore_active_transport({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }, identity_match=True)  # must not raise, must not exit the process

    info = server_module.transport_router.get_connection_info()
    assert info.state in (ConnectionState.ERROR, ConnectionState.CONNECTING, ConnectionState.DISCONNECTED)


# ---------------------------------------------------------------------------
# verify_radio_identity()'s transport-aware branch
# ---------------------------------------------------------------------------

def test_verify_radio_identity_uses_tcp_probe_for_a_tcp_configured_radio(server_module, _preserve_transport_router_state, monkeypatch):
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        return (
            {
                "status": "MATCH",
                "checked_at": "2026-09-22T00:00:00+00:00",
                "configured": {"host": host, "port": port},
                "detected": {"node_id": "!1fa065f0", "long_name": "T-Beam"},
                "error": None,
                "error_code": None,
            },
            "",
        )

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    def _fail_if_called_serial(*a, **k):
        raise AssertionError("serial CLI probe must not be used for a TCP-configured radio")

    monkeypatch.setattr(server_module, "detect_radio_identity", _fail_if_called_serial)

    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = {
        "node_id": "!1fa065f0",
        "long_name": "T-Beam",
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }
    server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = updated

    server_module.verify_radio_identity()

    assert calls == [("192.168.2.34", 4403)]
    assert server_module.RADIO_IDENTITY_RESULT["status"] == "MATCH"


def test_verify_radio_identity_skips_probe_entirely_for_bluetooth(server_module, _preserve_transport_router_state, monkeypatch):
    """No dedicated Bluetooth identity-verification mechanism exists
    (explicitly out of scope - see verify_radio_identity()'s own
    docstring) - must not call either CLI-based or TCP-based detection,
    and must land on NOT_CHECKED rather than crashing or false-MISMATCHing."""
    def _fail_if_called(*a, **k):
        raise AssertionError("no detection function should be called for bluetooth")

    monkeypatch.setattr(server_module, "detect_radio_identity", _fail_if_called)
    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fail_if_called)

    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = {
        "node_id": "!756f9960",
        "long_name": "Flint TAP2",
        "transport": "bluetooth",
        "endpoint": {"address": "3C:DC:75:6F:99:61", "label": "FLT2_9960"},
    }
    server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = updated

    server_module.verify_radio_identity()

    assert server_module.RADIO_IDENTITY_RESULT["status"] == "NOT_CHECKED"


# ---------------------------------------------------------------------------
# TCP boot-race retry (reliability follow-up): verify_radio_identity()'s
# TCP branch retries a transport-level failure (IDENTITY_DETECTION_ERROR)
# a bounded number of times with a short backoff, but a definitive
# MISMATCH/NOT_FOUND must never be retried - see server.py's own
# TCP_IDENTITY_BOOT_RETRY_DELAYS_S comment for the live-caught boot-race
# this addresses.
# ---------------------------------------------------------------------------

def test_verify_radio_identity_retries_a_transient_tcp_failure_and_recovers(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """Simulates the exact live-caught scenario: the network isn't up yet
    for the first couple of attempts (IDENTITY_DETECTION_ERROR, "Network
    is unreachable"-shaped), then the same radio answers successfully -
    must recover within the bounded retry budget instead of leaving
    identity_status stuck at DETECTION_ERROR for the rest of the process
    lifetime."""
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        if len(calls) < 3:
            return (
                {
                    "status": "DETECTION_ERROR",
                    "checked_at": "2026-09-24T14:42:16+00:00",
                    "configured": {"host": host, "port": port},
                    "detected": {},
                    "error": "connect_failed: could not open a TCP connection "
                             f"to {host}:{port}: [Errno 101] Network is unreachable",
                    "error_code": "connect_failed",
                },
                "",
            )
        return (
            {
                "status": "MATCH",
                "checked_at": "2026-09-24T14:42:20+00:00",
                "configured": {"host": host, "port": port},
                "detected": {"node_id": "!1fa065f0", "long_name": "T-Beam"},
                "error": None,
                "error_code": None,
            },
            "",
        )

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    slept = []
    monkeypatch.setattr(server_module.time, "sleep", lambda seconds: slept.append(seconds))

    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = {
        "node_id": "!1fa065f0",
        "long_name": "T-Beam",
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }
    server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = updated

    server_module.verify_radio_identity()

    assert len(calls) == 3, "must have retried exactly twice (3 attempts total) before recovering"
    assert slept == list(server_module.TCP_IDENTITY_BOOT_RETRY_DELAYS_S[:2]), (
        "must sleep only between attempts actually made, using the module's own bounded delay tuple"
    )
    assert server_module.RADIO_IDENTITY_RESULT["status"] == "MATCH"


def test_verify_radio_identity_never_retries_a_definitive_mismatch(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """A MISMATCH means we genuinely reached a radio and it was the wrong
    one (detected.node_id populated) - retrying this would mask a real
    identity mismatch behind transient-looking retry logic, which must
    never happen. Exactly one call, zero sleeps."""
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        # Matches detect_tcp_radio_identity()'s own real success-path
        # return shape (meshsrv/radio_identity.py): status="MATCH" is
        # just "we successfully reached *a* radio" - verify_radio_identity()'s
        # own compare_radio_identity() call (after this returns) is what
        # actually turns this into MISMATCH, based on detected.node_id
        # not matching configured. Never IDENTITY_DETECTION_ERROR here -
        # that value is reserved for a transport-level failure that never
        # reached any radio at all, which this scenario is not.
        return (
            {
                "status": "MATCH",
                "checked_at": "2026-09-24T14:42:16+00:00",
                "configured": {"host": host, "port": port},
                "detected": {"node_id": "!deadbeef", "long_name": "Someone Else's Radio"},
                "error": None,
                "error_code": None,
            },
            "",
        )

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    slept = []
    monkeypatch.setattr(server_module.time, "sleep", lambda seconds: slept.append(seconds))

    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = {
        "node_id": "!1fa065f0",
        "long_name": "T-Beam",
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }
    server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = updated

    server_module.verify_radio_identity()

    assert len(calls) == 1, "a definitive MISMATCH must never be retried"
    assert slept == []
    assert server_module.RADIO_IDENTITY_RESULT["status"] == "MISMATCH"


def test_verify_radio_identity_gives_up_after_exhausting_retries(
    server_module, _preserve_transport_router_state, monkeypatch
):
    """Every attempt keeps failing (radio genuinely never reachable) -
    must exhaust the bounded retry budget and land on DETECTION_ERROR,
    not retry forever."""
    calls = []

    def _fake_detect_tcp(transport, host, port, timeout=25):
        calls.append((host, port))
        return (
            {
                "status": "DETECTION_ERROR",
                "checked_at": "2026-09-24T14:42:16+00:00",
                "configured": {"host": host, "port": port},
                "detected": {},
                "error": "connect_failed: could not open a TCP connection "
                         f"to {host}:{port}: [Errno 101] Network is unreachable",
                "error_code": "connect_failed",
            },
            "",
        )

    monkeypatch.setattr(server_module, "detect_tcp_radio_identity", _fake_detect_tcp)

    slept = []
    monkeypatch.setattr(server_module.time, "sleep", lambda seconds: slept.append(seconds))

    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = {
        "node_id": "!1fa065f0",
        "long_name": "T-Beam",
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }
    server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = updated

    server_module.verify_radio_identity()

    expected_attempts = len(server_module.TCP_IDENTITY_BOOT_RETRY_DELAYS_S) + 1
    assert len(calls) == expected_attempts
    assert slept == list(server_module.TCP_IDENTITY_BOOT_RETRY_DELAYS_S)
    assert server_module.RADIO_IDENTITY_RESULT["status"] == "DETECTION_ERROR"


# ---------------------------------------------------------------------------
# refresh_identity_after_reconnect() - reliability follow-up: a successful
# transport_router.reconnect()/.switch() used to never touch
# RADIO_IDENTITY_RESULT/INSTANCE_IDENTITY.runtime.identity_status at all,
# leaving is_radio_available() gated on a stale pre-reconnect status
# forever - live-caught on pixel-111 with a fully live TCP connection
# sitting behind identity_status="DETECTION_ERROR" from an earlier
# boot-race failure.
# ---------------------------------------------------------------------------

def _set_accepted_radio(server_module, node_id="!1fa065f0", transport="tcp"):
    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = {
        "node_id": node_id,
        "long_name": "T-Beam",
        "transport": transport,
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }
    server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = updated


def _set_stale_identity_result(server_module, status="DETECTION_ERROR"):
    server_module.RADIO_IDENTITY_RESULT = {
        "status": status,
        "checked_at": "2026-09-24T14:42:16+00:00",
        "configured": {},
        "detected": {},
        "error": "connect_failed: could not open a TCP connection: [Errno 101] Network is unreachable",
    }


def test_refresh_identity_after_reconnect_clears_a_stale_status_on_match(
    server_module, _preserve_transport_router_state,
):
    _set_accepted_radio(server_module, node_id="!1fa065f0")
    _set_stale_identity_result(server_module, status="DETECTION_ERROR")

    server_module.refresh_identity_after_reconnect("tcp", {"node_id": "!1fa065f0"})

    assert server_module.RADIO_IDENTITY_RESULT["status"] == "MATCH"
    assert server_module.RADIO_IDENTITY_RESULT["error"] is None
    assert server_module.INSTANCE_IDENTITY["runtime"]["identity_status"] == "MATCH"
    # Fresh, not the old stale timestamp.
    assert server_module.RADIO_IDENTITY_RESULT["checked_at"] != "2026-09-24T14:42:16+00:00"


def test_refresh_identity_after_reconnect_reports_a_real_mismatch_not_silently(
    server_module, _preserve_transport_router_state,
):
    """The exact safety guarantee this fix must not weaken: a reconnect
    to a DIFFERENT radio than accepted must surface as MISMATCH, not get
    rubber-stamped as MATCH just because the reconnect itself succeeded."""
    _set_accepted_radio(server_module, node_id="!1fa065f0")
    _set_stale_identity_result(server_module, status="DETECTION_ERROR")

    server_module.refresh_identity_after_reconnect("tcp", {"node_id": "!deadbeef"})

    assert server_module.RADIO_IDENTITY_RESULT["status"] == "MISMATCH"
    assert server_module.INSTANCE_IDENTITY["runtime"]["identity_status"] == "MISMATCH"


def test_refresh_identity_after_reconnect_is_a_noop_for_bluetooth(
    server_module, _preserve_transport_router_state,
):
    """Bluetooth has no identity-verification mechanism at all
    (deliberate, pre-existing) - must not touch RADIO_IDENTITY_RESULT."""
    _set_accepted_radio(server_module, node_id="!756f9960", transport="bluetooth")
    _set_stale_identity_result(server_module, status="DETECTION_ERROR")
    before = dict(server_module.RADIO_IDENTITY_RESULT)

    server_module.refresh_identity_after_reconnect("bluetooth", {"node_id": "!756f9960"})

    assert server_module.RADIO_IDENTITY_RESULT == before


def test_refresh_identity_after_reconnect_is_a_noop_for_serial(
    server_module, _preserve_transport_router_state,
):
    _set_accepted_radio(server_module, node_id="!067a40fa", transport="serial")
    _set_stale_identity_result(server_module, status="DETECTION_ERROR")
    before = dict(server_module.RADIO_IDENTITY_RESULT)

    server_module.refresh_identity_after_reconnect("serial", {"node_id": "!067a40fa"})

    assert server_module.RADIO_IDENTITY_RESULT == before


def test_refresh_identity_after_reconnect_missing_node_id_reports_not_found(
    server_module, _preserve_transport_router_state,
):
    """A minimal {"node_id": None} (e.g. TransportRouter.get_connection_info()
    returned no node_id for some reason) must not be silently treated as
    a match - compare_radio_identity()'s own NOT_FOUND path, same as any
    other caller that never got a real node_id."""
    _set_accepted_radio(server_module, node_id="!1fa065f0")
    _set_stale_identity_result(server_module, status="DETECTION_ERROR")

    server_module.refresh_identity_after_reconnect("tcp", {"node_id": None})

    assert server_module.RADIO_IDENTITY_RESULT["status"] == "NOT_FOUND"
