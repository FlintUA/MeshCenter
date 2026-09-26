"""End-to-end isolation test for the ephemeral TCP probe (TCP lifecycle P0,
PR-B): TWO real adapter subprocesses (production + probe), real
meshtastic.tcp_interface.TCPInterface, and FakeMeshtasticTcpServer radios
that count what they actually see.

Proves what the unit tests can only assume: probing another endpoint never
disturbs the production session, probing the production endpoint opens no
second connection at that radio, and the probe's socket is gone after every
probe (process death). Skipped where the real `meshtastic` package isn't
installed (Core's own CI venv), like tests/test_tcp_transport_integration.py.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("meshtastic")

from adapters.meshtastic.fake_radio_server import FakeMeshtasticTcpServer  # noqa: E402
from meshsrv.adapter_ipc_client import AdapterIPCTransport, AdapterSupervisor  # noqa: E402
from meshsrv.radio_identity import probe_tcp_radio_identity  # noqa: E402
from meshsrv.radio_transport import ConnectionDescriptor, ConnectionState, ConnectionType  # noqa: E402

_ROOT = str(Path(__file__).resolve().parents[1])


def _supervisor():
    return AdapterSupervisor(
        adapter_python=sys.executable,
        project_dir=_ROOT,
        serial_port="/dev/null",
        meshtastic_cli="meshtastic",
    )


def _wait_until(predicate, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def stack():
    production_radio = FakeMeshtasticTcpServer(complete_handshake=True, node_num=0x1FA065F0)
    other_radio = FakeMeshtasticTcpServer(complete_handshake=True, node_num=0x756F9960)
    production_supervisor = _supervisor()
    probe_supervisor = _supervisor()
    production = AdapterIPCTransport(ConnectionType.TCP, production_supervisor)
    probe = AdapterIPCTransport(ConnectionType.TCP, probe_supervisor)
    yield {
        "production_radio": production_radio,
        "other_radio": other_radio,
        "production": production,
        "probe": probe,
        "probe_supervisor": probe_supervisor,
        "production_supervisor": production_supervisor,
    }
    probe_supervisor.shutdown()
    production_supervisor.shutdown()
    production_radio.shutdown()
    other_radio.shutdown()


def _probe(stack, port):
    return probe_tcp_radio_identity(
        "127.0.0.1",
        port,
        live_transport=stack["production"],
        probe_transport=stack["probe"],
        probe_shutdown=stack["probe_supervisor"].shutdown,
        probe_lock=threading.Lock(),
        timeout=25,
        warmup_timeout=40,
    )


def _connect_production(stack):
    port = stack["production_radio"].port
    info = stack["production"].connect(
        ConnectionDescriptor(type=ConnectionType.TCP, address=f"127.0.0.1:{port}"), timeout=30
    )
    assert info.state == ConnectionState.CONNECTED
    time.sleep(0.2)  # the library's one-time post-config heartbeat write


def test_probing_another_radio_never_disturbs_the_production_session(stack):
    _connect_production(stack)
    production_radio, other_radio = stack["production_radio"], stack["other_radio"]
    assert production_radio.active_connections == 1

    result, _ = _probe(stack, other_radio.port)

    assert result["status"] == "MATCH"
    assert result["detected"]["node_id"] == "!756f9960"
    # The probe reached the OTHER radio, exactly once, and its socket is gone
    # (probe process killed) - while production never blinked.
    assert other_radio.real_connections == 1
    assert _wait_until(lambda: other_radio.active_connections == 0), "probe socket must close with its process"
    assert production_radio.real_connections == 1
    assert production_radio.max_concurrent_connections == 1
    assert production_radio.active_connections == 1
    assert stack["production"].get_connection_info().state == ConnectionState.CONNECTED
    assert stack["production"].get_local_node(timeout=15).node_id == "!1fa065f0"


def test_probing_the_production_endpoint_opens_no_second_connection_at_that_radio(stack):
    _connect_production(stack)
    production_radio = stack["production_radio"]

    result, _ = _probe(stack, production_radio.port)

    assert result["status"] == "MATCH"
    assert result["detected"]["node_id"] == "!1fa065f0"
    assert production_radio.real_connections == 1, "identity came from the live session, not a new connect"
    assert production_radio.max_concurrent_connections == 1
    assert stack["probe_supervisor"]._proc is None, "no probe process was even spawned"


def test_a_probe_that_fails_does_not_touch_production_and_still_cleans_up(stack):
    _connect_production(stack)
    production_radio = stack["production_radio"]

    result, _ = _probe(stack, 1)  # nothing listens on port 1

    assert result["status"] == "DETECTION_ERROR"
    assert stack["probe_supervisor"]._proc is None, "probe process killed after a failed probe too"
    assert production_radio.max_concurrent_connections == 1
    assert production_radio.active_connections == 1
    assert stack["production"].get_connection_info().state == ConnectionState.CONNECTED


def test_repeated_probes_never_accumulate_connections(stack):
    other_radio = stack["other_radio"]

    for _ in range(3):
        result, _ = _probe(stack, other_radio.port)
        assert result["status"] == "MATCH"
        assert _wait_until(lambda: other_radio.active_connections == 0)

    assert other_radio.real_connections == 3
    assert other_radio.max_concurrent_connections == 1
