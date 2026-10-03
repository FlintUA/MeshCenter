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


# --- H2-C Phase 2: restart_listener() used to refuse outright (409)
# unless identity was already a confirmed MATCH - exactly the states a
# user would actually reach for this button (DETECTION_ERROR after a
# replug, MISMATCH after a radio swap). Now proceeds regardless, and
# forces a fresh identity re-check via clear_mismatch() rather than
# assuming the click fixed anything. ---------------------------------


@pytest.mark.parametrize("status", ["DETECTION_ERROR", "MISMATCH", "NOT_FOUND"])
def test_restart_listener_on_serial_no_longer_blocked_by_bad_identity(server_module, _preserve, monkeypatch, status):
    _accept(server_module, "serial")
    server_module.RADIO_IDENTITY_RESULT = {"status": status, "detected": {}, "error": "x"}
    calls = []
    monkeypatch.setattr(server_module, "stop_listener", lambda: calls.append("stop") or True)
    monkeypatch.setattr(server_module, "radio_event", lambda name: calls.append(name))
    monkeypatch.setattr(server_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(server_module.radio_connection_manager, "is_released", lambda: False)

    data, status_code = _post(server_module, server_module.api_restart_listener, "/api/restart_listener")

    assert status_code == 200
    assert data["ok"] is True
    assert calls == ["stop", "restart"]


def test_restart_listener_never_popens_without_a_fresh_match_even_on_a_persisting_mismatch(server_module, _preserve, monkeypatch):
    """The actual safety property reviewers need proof of, not just the
    route's own synchronous behavior: clicking Restart Listener after a
    radio swap must NEVER start the listener against the wrong radio,
    even though the route itself no longer returns 409. The route only
    sets the disconnect-recovery flag and clears pause_listen - it is
    listener_supervisor's own background cycle (run_listener(), a real
    persistent thread in production, driven by hand here since no such
    thread runs in this test environment) that actually performs the
    re-verification before ever touching subprocess.Popen. This test
    drives that cycle for real after the route call, with the swapped
    radio still answering MISMATCH, and proves Popen is never reached -
    not just that the route returned 200."""
    import meshsrv.serial_port_supervisor as spv_module

    _accept(server_module, "serial")
    server_module.RADIO_IDENTITY_RESULT = {"status": "MISMATCH", "detected": {}, "error": None}
    monkeypatch.setattr(server_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(server_module.radio_connection_manager, "is_released", lambda: False)

    data, status_code = _post(server_module, server_module.api_restart_listener, "/api/restart_listener")
    assert status_code == 200 and data["ok"] is True

    # The real persistent listener thread would pick this up on its own
    # next cycle in production - driven by hand here. The swapped radio
    # is still there and still wrong: verify_identity must be consulted
    # again (not skipped), and it says MISMATCH again.
    popen_calls = []
    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    server_module.listener_supervisor._resolve_port = lambda: "/dev/ttyACM0"
    server_module.listener_supervisor._verify_identity = lambda port: "MISMATCH"

    server_module.listener_supervisor._listener_cycle()

    assert popen_calls == [], "must never start the listener against a radio that hasn't been re-proven to be the accepted one"
    assert server_module.listener_supervisor.is_mismatch_active() is True


def test_restart_listener_resumes_once_the_re_check_actually_confirms_match(server_module, _preserve, monkeypatch):
    """The other half of the same property: once the re-check genuinely
    confirms MATCH (the same radio, or the user physically restored it),
    the listener resumes normally - clear_mismatch() forces a real
    re-check, not a permanent lockout."""
    import meshsrv.serial_port_supervisor as spv_module

    _accept(server_module, "serial")
    server_module.RADIO_IDENTITY_RESULT = {"status": "MISMATCH", "detected": {}, "error": None}
    monkeypatch.setattr(server_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(server_module.radio_connection_manager, "is_released", lambda: False)

    _post(server_module, server_module.api_restart_listener, "/api/restart_listener")

    popen_calls = []
    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: popen_calls.append(a) or _FakeProc())
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    server_module.listener_supervisor._resolve_port = lambda: "/dev/ttyACM0"
    server_module.listener_supervisor._verify_identity = lambda port: "MATCH"

    server_module.listener_supervisor._listener_cycle()

    assert len(popen_calls) == 1, "a genuine MATCH must actually resume the listener, not stay halted forever"
    assert server_module.listener_supervisor.is_mismatch_active() is False


class _FakeProc:
    def __init__(self):
        self.stdout = iter(["line\n"])

    def poll(self):
        return 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_reconnect_route_never_popens_without_a_fresh_match_even_on_a_persisting_mismatch(server_module, _preserve, monkeypatch):
    """Same safety property as restart_listener's sibling test above,
    through the Release/Reconnect entry point instead: a radio swapped or
    reconfigured while released must never be silently resumed just
    because Reconnect was clicked. /api/radio_connection/reconnect used
    to check RADIO_IDENTITY_RESULT - the STALE pre-release status, since
    nothing re-checks identity while released - which would have waved
    through exactly this case if it happened to read MATCH from before
    the release. radio_connection_manager's on_reconnect hook (wired to
    listener_supervisor.clear_mismatch() at construction) is what
    actually protects this now."""
    import meshsrv.serial_port_supervisor as spv_module

    _accept(server_module, "serial")
    server_module.RADIO_IDENTITY_RESULT = {"status": "MATCH", "detected": {}, "error": None}  # stale: true before release
    server_module.radio_connection_manager._mode = "released"
    monkeypatch.setattr(spv_module.time, "sleep", lambda *_a, **_k: None)

    data, status_code = _post(server_module, server_module.api_radio_connection_reconnect, "/api/radio_connection/reconnect")
    assert status_code == 200 and data["ok"] is True

    popen_calls = []
    monkeypatch.setattr(spv_module.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))
    monkeypatch.setattr(spv_module.os.path, "exists", lambda p: True)
    server_module.listener_supervisor._resolve_port = lambda: "/dev/ttyACM0"
    server_module.listener_supervisor._verify_identity = lambda port: "MISMATCH"  # the radio was swapped during release

    server_module.listener_supervisor._listener_cycle()

    assert popen_calls == [], "Reconnect must never resume against a radio that hasn't been re-proven to be the accepted one"
    assert server_module.listener_supervisor.is_mismatch_active() is True


def test_restart_listener_on_serial_clears_a_mismatch(server_module, _preserve, monkeypatch):
    _accept(server_module, "serial")
    server_module.listener_supervisor._mismatch_active.set()
    server_module.listener_supervisor._mismatch_port = "/dev/ttyACM0"
    monkeypatch.setattr(server_module, "stop_listener", lambda: True)
    monkeypatch.setattr(server_module, "radio_event", lambda name: None)
    monkeypatch.setattr(server_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(server_module.radio_connection_manager, "is_released", lambda: False)

    _post(server_module, server_module.api_restart_listener, "/api/restart_listener")

    assert server_module.listener_supervisor.is_mismatch_active() is False


@pytest.mark.parametrize("transport", ["serial", "tcp", "bluetooth"])
def test_radio_health_reports_the_active_transport(server_module, _preserve, transport):
    _accept(server_module, transport)

    with server_module.app.test_request_context("/api/radio_health"):
        data = server_module.api_radio_health().get_json()

    assert data["transport"] == transport
