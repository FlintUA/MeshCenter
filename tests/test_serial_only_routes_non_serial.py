"""/api/restart_listener and /api/rescan_nodes drive Core's serial `--listen`
subprocess and the serial `--info` CLI. Under a TCP/Bluetooth radio they used
to run anyway: restart_listener cleared pause_listen (which start_runtime()
sets on purpose for non-serial) and reported success; rescan_nodes ran a
serial CLI against a port that doesn't exist and answered `ok: false` with no
`error`, which the UI rendered as "Error: Unknown error". Now a clear 409
before any side effect. /api/radio_health also reports the transport so the
UI stops treating the always-False listener_running as a fault."""
import pytest


@pytest.fixture
def _preserve(server_module):
    original_identity = server_module.instance_manager.get()
    original_result = server_module.RADIO_IDENTITY_RESULT
    original_pause = server_module.pause_listen.is_set()
    yield
    server_module.instance_manager.save(original_identity)
    server_module.INSTANCE_IDENTITY = original_identity
    server_module.RADIO_IDENTITY_RESULT = original_result
    if original_pause:
        server_module.pause_listen.set()
    else:
        server_module.pause_listen.clear()


def _accept(server_module, transport):
    radio = {
        "tcp": {"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}},
        "bluetooth": {"node_id": "!756f9960", "transport": "bluetooth", "endpoint": {"address": "3C:DC:75:6F:99:61"}},
        "serial": {"node_id": "!067a40fa", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}},
    }[transport]
    updated = dict(server_module.instance_manager.get())
    updated["radio"] = radio
    server_module.INSTANCE_IDENTITY = server_module.instance_manager.save(updated)
    server_module.RADIO_IDENTITY_RESULT = {"status": "MATCH", "detected": {}, "error": None}


def _post(server_module, view, path):
    with server_module.app.test_request_context(path, method="POST", json={}):
        result = view()
    response, status = result if isinstance(result, tuple) else (result, 200)
    return response.get_json(), status


def _boom(*a, **k):
    raise AssertionError("serial-only machinery must not run for a non-serial radio")


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_restart_listener_is_refused_without_side_effects(server_module, _preserve, monkeypatch, transport):
    _accept(server_module, transport)
    server_module.pause_listen.set()  # start_runtime() leaves it set for non-serial
    monkeypatch.setattr(server_module, "stop_listener", _boom)
    monkeypatch.setattr(server_module, "radio_event", _boom)

    data, status = _post(server_module, server_module.api_restart_listener, "/api/restart_listener")

    assert status == 409
    assert data["ok"] is False
    assert data["error_code"] == "listener_not_applicable"
    assert transport.replace("bluetooth", "Bluetooth").replace("tcp", "TCP") in data["error"]
    assert data["transport"] == transport
    assert server_module.pause_listen.is_set(), "the deliberate non-serial pause must not be cleared"


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_rescan_nodes_is_refused_without_running_the_serial_cli(server_module, _preserve, monkeypatch, transport):
    _accept(server_module, transport)
    monkeypatch.setattr(server_module, "radio_session", _boom)
    monkeypatch.setattr(server_module.meshtastic_transport, "get_info", _boom)

    data, status = _post(server_module, server_module.api_rescan_nodes, "/api/rescan_nodes")

    assert status == 409
    assert data["ok"] is False and data["error"]  # never an empty error again
    assert data["error_code"] == "listener_not_applicable"


def test_refusal_wins_over_identity_gate_message(server_module, _preserve, monkeypatch):
    """A TCP radio in DETECTION_ERROR must be told the action is not
    applicable, not 'identity mismatch'."""
    _accept(server_module, "tcp")
    server_module.RADIO_IDENTITY_RESULT = {"status": "DETECTION_ERROR", "detected": {}, "error": "x"}

    data, status = _post(server_module, server_module.api_restart_listener, "/api/restart_listener")

    assert status == 409 and data["error_code"] == "listener_not_applicable"


def test_restart_listener_on_serial_is_unchanged(server_module, _preserve, monkeypatch):
    _accept(server_module, "serial")
    calls = []
    monkeypatch.setattr(server_module, "stop_listener", lambda: calls.append("stop") or True)
    monkeypatch.setattr(server_module, "radio_event", lambda name: calls.append(name))
    monkeypatch.setattr(server_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(server_module.radio_connection_manager, "is_released", lambda: False)
    server_module.pause_listen.set()

    data, status = _post(server_module, server_module.api_restart_listener, "/api/restart_listener")

    assert status == 200 and data["ok"] is True
    assert calls == ["stop", "restart"]
    assert not server_module.pause_listen.is_set()


@pytest.mark.parametrize("transport", ["serial", "tcp", "bluetooth"])
def test_radio_health_reports_the_active_transport(server_module, _preserve, transport):
    _accept(server_module, transport)

    with server_module.app.test_request_context("/api/radio_health"):
        data = server_module.api_radio_health().get_json()

    assert data["transport"] == transport
