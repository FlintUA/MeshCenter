"""RR1 - "Release radio" for TCP (and a duration for serial).

Covers: the manager (duration, absolute end time, persistence across a restart, hooks),
the server wiring (endpoints, fast "radio released" failures, no probe of the released
endpoint, boot without dialling, timer resume, explicit connect = Reconnect) and an
end-to-end run against the fake radio proving ZERO new connections while released."""
import json
import os
import threading
import time

import pytest

from meshsrv.radio_manager import RadioConnectionManager
from meshsrv.radio_release_store import RadioReleaseStore


# ---------------------------------------------------------------- manager (unit)

class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _manager(tmp_path, transport="tcp", clock=None, **hooks):
    clock = clock or Clock()
    calls = {"release": 0, "resume": 0, "stop": 0, "wait": 0, "reconnect": 0, "pause": []}

    class Pause:
        def set(self):
            calls["pause"].append("set")

        def clear(self):
            calls["pause"].append("clear")

    def tcp_release():
        calls["release"] += 1
        if hooks.get("release_error"):
            raise RuntimeError(hooks["release_error"])

    def tcp_resume():
        calls["resume"] += 1

    def stop():
        calls["stop"] += 1
        return True

    def wait(device=None, timeout=0):
        calls["wait"] += 1
        return True

    store = RadioReleaseStore(lambda: str(tmp_path))
    manager = RadioConnectionManager(
        pause_event=Pause(),
        stop_listener=stop,
        wait_serial_release=wait,
        serial_port="/dev/ttyACM0",
        on_reconnect=lambda: calls.__setitem__("reconnect", calls["reconnect"] + 1),
        transport_provider=lambda: transport,
        tcp_release=tcp_release,
        tcp_resume=tcp_resume,
        store=store,
        clock=clock,
    )
    return manager, calls, store, clock


def test_tcp_release_until_reconnect_persists_and_gates(tmp_path):
    manager, calls, store, _ = _manager(tmp_path)
    ok, status = manager.release()
    assert ok and calls["release"] == 1
    assert manager.is_released() and not manager.commands_allowed()
    assert status["until"] is None and status["release_kind"] == "tcp" and status["remaining_s"] is None
    record = json.load(open(os.path.join(tmp_path, "radio_release.json")))
    assert record["released"] is True and record["until"] is None and record["transport"] == "tcp"
    assert calls["stop"] == 0, "a TCP release never touches the serial listener"


def test_tcp_timed_release_keeps_an_absolute_end_time_and_expires(tmp_path):
    manager, calls, store, clock = _manager(tmp_path)
    ok, status = manager.release(duration_s=900)
    assert ok and status["until"] == clock.now + 900 and status["remaining_s"] == 900
    assert manager.expired() is False
    clock.now += 899
    assert manager.expired() is False
    clock.now += 2
    assert manager.expired() is True
    ok, status = manager.reconnect()                       # what the timer does
    assert ok and calls["resume"] == 1
    assert not manager.is_released() and manager.commands_allowed()
    assert store.load() is None, "the persisted record is gone once the release ended"


def test_a_restart_while_released_stays_released_and_does_not_touch_the_radio(tmp_path):
    manager, calls, store, clock = _manager(tmp_path)
    manager.release()
    # the process restarts: a fresh manager over the same store
    fresh, fresh_calls, _, _ = _manager(tmp_path, clock=clock)
    record = fresh.load_persisted_release()
    assert record is not None and record[0] is None
    fresh.restore_released(*record, kind="tcp")
    assert fresh.is_released() and not fresh.commands_allowed()
    assert fresh_calls["release"] == 0 and fresh_calls["resume"] == 0 and fresh_calls["stop"] == 0


def test_a_timed_release_survives_a_restart_with_its_end_time_and_an_expired_one_does_not(tmp_path):
    manager, _, store, clock = _manager(tmp_path)
    manager.release(duration_s=3600)
    clock.now += 600
    fresh, _, _, _ = _manager(tmp_path, clock=clock)
    until, _since = fresh.load_persisted_release()
    assert until == manager._until, "absolute end time, not 'one hour from the restart'"
    clock.now += 3600                                      # now past the end
    again, _, _, _ = _manager(tmp_path, clock=clock)
    assert again.load_persisted_release() is None
    assert store.load() is None, "an expired record is removed: connect normally"


def test_a_failed_tcp_release_is_an_error_not_a_release(tmp_path):
    manager, calls, store, _ = _manager(tmp_path, release_error="could not close")
    ok, status = manager.release(duration_s=900)
    assert ok is False and status["mode"] == "error" and "could not close" in status["last_error"]
    assert status["until"] is None and store.load() is None
    assert manager.commands_allowed() is False or manager.status()["mode"] == "error"


def test_end_release_is_the_explicit_reconnect_without_the_resume_hook(tmp_path):
    manager, calls, store, _ = _manager(tmp_path)
    manager.release()
    assert manager.end_release() is True
    assert calls["resume"] == 0 and store.load() is None and manager.commands_allowed()
    assert manager.end_release() is False, "nothing left to end"


def test_serial_release_gets_the_same_duration_and_stays_in_memory(tmp_path):
    manager, calls, store, clock = _manager(tmp_path, transport="serial")
    ok, status = manager.release(timeout=1, duration_s=3600)
    assert ok and status["until"] == clock.now + 3600 and status["release_kind"] == "serial"
    assert calls["stop"] == 1 and calls["wait"] == 1 and calls["release"] == 0
    assert store.load() is None, "a serial release is deliberately not persisted"
    clock.now += 3601
    assert manager.expired() is True
    ok, status = manager.reconnect()
    assert ok and calls["reconnect"] == 1 and calls["pause"][-1] == "clear"
    assert status["until"] is None


def test_serial_release_until_reconnect_never_expires(tmp_path):
    manager, _, _, clock = _manager(tmp_path, transport="serial")
    manager.release(timeout=1)
    clock.now += 10 * 24 * 3600
    assert manager.expired() is False and manager.is_released()


# ---------------------------------------------------------------- server wiring

@pytest.fixture
def rr1(server_module):
    manager = server_module.radio_connection_manager
    saved = (manager._mode, manager._until, manager._kind, manager._released_at, manager._clock)
    original_identity = server_module.instance_manager.get()
    original_result = server_module.RADIO_IDENTITY_RESULT
    server_module.radio_release_store.clear()
    yield server_module
    manager._mode, manager._until, manager._kind, manager._released_at, manager._clock = saved
    server_module.radio_release_store.clear()
    server_module.instance_manager.save(original_identity)
    server_module.INSTANCE_IDENTITY = original_identity
    server_module.RADIO_IDENTITY_RESULT = original_result


def _accept(server_module, transport="tcp"):
    radio = {
        "tcp": {"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}},
        "bluetooth": {"node_id": "!756f9960", "transport": "bluetooth", "endpoint": {"address": "3C:DC:75:6F:99:61"}},
        "serial": {"node_id": "!067a40fa", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}},
    }[transport]
    updated = dict(server_module.instance_manager.get())
    updated["radio"] = radio
    server_module.INSTANCE_IDENTITY = server_module.instance_manager.save(updated)
    server_module.RADIO_IDENTITY_RESULT = {"status": "MATCH", "detected": {}, "error": None}


def _call(server_module, view, path, body=None):
    with server_module.app.test_request_context(path, method="POST", json=body if body is not None else {}):
        result = view()
    response, status = result if isinstance(result, tuple) else (result, 200)
    return response.get_json(), status


def test_release_api_for_tcp_closes_the_session_persists_and_gates_everything(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    closed = []
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: closed.append(timeout))

    data, status = _call(server, server.api_radio_connection_release, "/api/radio_connection/release",
                         {"duration_s": 900})

    assert status == 200 and data["ok"] is True and closed, "the TCP session was closed"
    assert data["radio"]["mode"] == "released" and data["radio"]["release_kind"] == "tcp"
    assert 890 <= data["radio"]["remaining_s"] <= 900
    assert server.radio_release_store.load()["until"] == data["radio"]["until"], "persisted per profile"
    assert server.is_radio_available() is False, "sends / Node Tools / MCA / schedules fail fast"
    assert server.inbound_eligibility() == "released"
    assert server.tcp_link_supervisor._is_released() is True
    health = server.compute_radio_health_status(
        active_transport="tcp", is_released=True, is_paused=False, listener_running=False,
        packet_age=None, transport_state={"state": "disconnected"},
    )
    assert health[0] == "RELEASED" and health[1] == "INFO"
    assert server._release_status_payload()["until"] == data["radio"]["until"]


def test_release_api_validates_the_duration_and_refuses_bluetooth(rr1):
    server = rr1
    _accept(server, "tcp")
    for bad in (5, 100000, "soon"):
        data, status = _call(server, server.api_radio_connection_release, "/api/radio_connection/release",
                             {"duration_s": bad})
        assert status == 400 and data["error_code"] == "invalid_duration"
    _accept(server, "bluetooth")
    data, status = _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {})
    assert status == 409 and data["error_code"] == "listener_not_applicable"


def test_reconnect_ends_a_tcp_release_and_hands_over_to_the_supervisor(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    triggered = []
    monkeypatch.setattr(server.tcp_link_supervisor, "trigger_now", lambda: triggered.append(1) or {})
    monkeypatch.setattr(server, "radio_event", lambda *a, **k: (_ for _ in ()).throw(AssertionError("serial hook")))

    _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {})
    data, status = _call(server, server.api_radio_connection_reconnect, "/api/radio_connection/reconnect")

    assert status == 200 and data["ok"] is True and triggered == [1]
    assert server.radio_release_store.load() is None
    assert server.radio_connection_manager.commands_allowed() is True


def test_reconnect_now_while_released_is_the_reconnect(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    monkeypatch.setattr(server.tcp_link_supervisor, "trigger_now", lambda: {})
    _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {})

    data, status = _call(server, server.api_meshtastic_reconnect_now, "/api/meshtastic/reconnect_now")

    assert status == 200 and data["ok"] is True and not server.radio_connection_manager.is_released()


def test_detect_and_accept_on_the_released_endpoint_are_refused_without_dialling(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {})

    def dial(*a, **k):
        raise AssertionError("a probe must never dial the released radio")

    monkeypatch.setattr(server, "probe_tcp_radio_identity", dial)
    body = {"host": "192.168.2.34", "port": 4403}
    data, status = _call(server, server.api_detect_new_radio, "/api/node-manager/radio/detect", body)
    assert status == 409 and data["error_code"] == "radio_released" and "Reconnect" in data["error"]
    data, status = _call(server, server.api_accept_detected_radio, "/api/node-manager/radio/accept", body)
    assert status == 409 and data["error_code"] == "radio_released"
    # another endpoint is not the released radio
    assert server._released_endpoint_refusal("192.168.2.99", 4403) is None


def test_explicit_tcp_connect_ends_the_release_without_a_second_attempt(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    triggered = []
    monkeypatch.setattr(server.tcp_link_supervisor, "trigger_now", lambda: triggered.append(1) or {})
    _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {})

    assert server._end_tcp_release_for_explicit_connect() is True
    assert triggered == [] and server.radio_connection_manager.commands_allowed()
    assert server._end_tcp_release_for_explicit_connect() is False


def test_a_sent_message_while_released_fails_fast_with_a_clear_code(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {})
    started = time.monotonic()
    client = server.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf-token"
    response = client.post("/api/send", json={"text": "hi", "chat_id": "channel"},
                           headers={"X-CSRF-Token": "test-csrf-token"})
    assert time.monotonic() - started < 2.0, "not a 25 s timeout"
    assert response.status_code == 409 and response.get_json()["error_code"] == "radio_released"


def test_boot_with_a_persisted_release_adopts_it_and_skips_the_probe(rr1):
    server = rr1
    _accept(server, "tcp")
    until = time.time() + 1800
    server.radio_release_store.save({"released": True, "until": until, "since": time.time(), "transport": "tcp"})
    server.radio_connection_manager._mode = "connected"

    assert server._restore_persisted_tcp_release() is True
    status = server.radio_connection_manager.status(False)
    assert status["mode"] == "released" and status["until"] == until and status["release_kind"] == "tcp"
    assert server.RADIO_IDENTITY_RESULT["status"] == "NOT_CHECKED"
    assert server.inbound_eligibility() == "released"

    # the boot sequence reads the release BEFORE the identity probe (which would dial)
    import inspect
    source = inspect.getsource(server.start_runtime)
    assert source.index("_restore_persisted_tcp_release()") < source.index("verify_radio_identity()")


def test_boot_with_an_expired_release_connects_normally_and_ignores_serial_profiles(rr1):
    server = rr1
    _accept(server, "tcp")
    server.radio_release_store.save({"released": True, "until": time.time() - 5, "since": 1.0, "transport": "tcp"})
    assert server._restore_persisted_tcp_release() is False
    assert server.radio_release_store.load() is None
    _accept(server, "serial")
    server.radio_release_store.save({"released": True, "until": None, "since": 1.0, "transport": "tcp"})
    assert server._restore_persisted_tcp_release() is False, "a serial profile never boots released"


def test_the_timer_resumes_a_timed_release_through_the_normal_supervisor_path(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    triggered = []
    monkeypatch.setattr(server.tcp_link_supervisor, "trigger_now", lambda: triggered.append(1) or {})
    _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {"duration_s": 900})
    manager = server.radio_connection_manager
    assert manager.expired() is False
    until = manager._until
    manager._clock = lambda: until + 1
    assert manager.expired() is True
    server._auto_resume_released_radio()
    assert triggered == [1] and not manager.is_released()
    assert server.radio_release_store.load() is None


def test_the_release_hook_waits_for_an_inflight_attempt_and_closes_the_session(rr1, monkeypatch):
    server = rr1
    closed = []
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: closed.append(1))
    release_started = threading.Event()
    done = []

    def release():
        release_started.set()
        server._tcp_release_hook()
        done.append(1)

    with server.tcp_link_lock:                     # an attempt is running
        thread = threading.Thread(target=release)
        thread.start()
        release_started.wait(2)
        time.sleep(0.2)
        assert done == [] and closed == [], "waits for the in-flight attempt"
    thread.join(5)
    assert done == [1] and closed == [1]


def test_the_old_tcp_autorecovery_setting_no_longer_controls_tcp(server_module):
    """tcp_ble_autorecovery.enabled had no UI control and did not stop boot/explicit dials
    (pixel-111). The TCP supervisor no longer reads it: Release radio is the control."""
    with server_module.state_lock:
        original = dict(server_module.settings.get("tcp_ble_autorecovery") or {})
        server_module.settings["tcp_ble_autorecovery"] = {"enabled": False}
    try:
        assert server_module.tcp_link_supervisor._auto_enabled() is True
        assert not hasattr(server_module, "_tcp_auto_reconnect_enabled")
    finally:
        with server_module.state_lock:
            server_module.settings["tcp_ble_autorecovery"] = original


# ---------------------------------------------------------------- end to end (fake radio)

def test_e2e_no_connection_attempts_while_released_timed_resume_and_restart(tmp_path):
    pytest.importorskip("meshtastic")
    from adapters.meshtastic.fake_radio_server import FakeMeshtasticTcpServer
    from adapters.meshtastic.tcp_transport import TCPTransport
    from meshsrv.radio_transport import ConnectionDescriptor, ConnectionType
    from meshsrv.tcp_link_supervisor import OUTCOME_CONNECTED, OUTCOME_RETRY, TcpLinkSupervisor

    radio = FakeMeshtasticTcpServer(complete_handshake=True, node_num=0x1FA065F0)
    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address=f"127.0.0.1:{radio.port}")
    transport = TCPTransport(host="127.0.0.1", port=radio.port)
    clock = Clock(2_000_000.0)
    monotonic = {"t": 0.0}
    try:
        def build(store_dir):
            supervisor_ref = {}
            manager = RadioConnectionManager(
                pause_event=threading.Event(), stop_listener=lambda: True,
                wait_serial_release=lambda **k: True, serial_port="x",
                transport_provider=lambda: "tcp",
                tcp_release=lambda: transport.disconnect(timeout=10),
                tcp_resume=lambda: supervisor_ref["s"].trigger_now(),
                store=RadioReleaseStore(lambda: str(store_dir)), clock=clock,
            )

            def attempt():
                try:
                    transport.connect(descriptor, force=True, timeout=20)
                    return OUTCOME_CONNECTED
                except Exception:
                    return OUTCOME_RETRY

            supervisor = TcpLinkSupervisor(
                is_tcp_profile=lambda: True, identity_refused=lambda: False,
                link_connected=transport.is_connected, attempt=attempt,
                is_released=manager.is_released, clock=lambda: monotonic["t"], rng=lambda: 0.5,
            )
            supervisor_ref["s"] = supervisor
            return manager, supervisor

        def advance(supervisor, seconds, step=5.0):
            end = monotonic["t"] + seconds
            while monotonic["t"] < end:
                supervisor.tick()
                monotonic["t"] += step
                clock.now += step

        manager, supervisor = build(tmp_path)
        advance(supervisor, 10)
        assert transport.is_connected()
        connections_before = radio.accepted_connections
        assert connections_before >= 1

        ok, _ = manager.release(duration_s=900)                      # Release radio, 15 min
        assert ok and not transport.is_connected()
        time.sleep(0.3)
        settled = radio.accepted_connections
        advance(supervisor, 13 * 60)                                  # 13 minutes released
        assert radio.accepted_connections == settled, "ZERO connection attempts while released"

        # the service restarts while released: nothing dials
        manager2, supervisor2 = build(tmp_path)
        record = manager2.load_persisted_release()
        assert record is not None
        manager2.restore_released(*record, kind="tcp")
        advance(supervisor2, 60)
        assert radio.accepted_connections == settled, "a restart while released does not dial"
        assert manager2.expired() is False

        advance(supervisor2, 2 * 60)                                  # past the 15 minutes
        assert manager2.expired() is True
        manager2.reconnect()                                          # what the timer does
        advance(supervisor2, 15)
        assert transport.is_connected(), "the timed release resumed through the supervisor"
        assert radio.accepted_connections > settled
    finally:
        try:
            transport.close()
        except Exception:
            pass
        radio.shutdown()


# ---------------------------------------------------------------- RR1 follow-up: instant released status, clear send reason

def test_radio_health_reports_released_at_once_not_the_stale_stored_status(rr1, monkeypatch):
    """The health worker refreshes `status` every 30 s; right after a release the stored value
    still read OK / listener stopped and the header chip painted a red "Offline"."""
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    with server.state_lock:
        saved = (server.radio_health.get("status"), server.radio_health.get("level"))
        server.radio_health["status"], server.radio_health["level"] = "OK", "OK"
    try:
        _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {"duration_s": 900})
        with server.app.test_request_context("/api/radio_health"):
            payload = server.api_radio_health().get_json()
        assert payload["status"] == "RELEASED" and payload["level"] == "INFO"
        assert payload["released"]["until"] is not None and payload["released"]["kind"] == "tcp"
    finally:
        with server.state_lock:
            server.radio_health["status"], server.radio_health["level"] = saved


def test_a_send_while_released_says_why(rr1, monkeypatch):
    server = rr1
    _accept(server, "tcp")
    monkeypatch.setattr(server.tcp_ipc_transport, "disconnect", lambda timeout=10: None)
    _call(server, server.api_radio_connection_release, "/api/radio_connection/release", {})
    client = server.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf-token"
    response = client.post("/api/send", json={"text": "hi", "chat_id": "channel"},
                           headers={"X-CSRF-Token": "test-csrf-token"})
    body = response.get_json()
    assert response.status_code == 409 and body["error_code"] == "radio_released"
    assert body["error"] == "Radio released - press Reconnect"


def test_every_locale_words_the_released_send_reason_and_the_composer_hint():
    import json
    for lang in ("en", "de", "ru", "uk"):
        data = json.load(open(os.path.join("static", "i18n", f"{lang}.json"), encoding="utf-8"))
        for key in ("radio_released_placeholder", "radio_released_hint"):
            assert data["chat"][key].strip(), (lang, key)
        assert data["errors"]["radio_released"].strip(), lang


# ---------------------------------------------------------------- RR1 PR nits

def test_node_manager_dashboard_shows_the_endpoint_after_a_restart_while_released(rr1, monkeypatch):
    """After a restart in Released the router never left its serial default (no TCP descriptor),
    so Host:Port read '-'. The accepted profile's own endpoint is shown instead."""
    server = rr1
    _accept(server, "tcp")
    server.radio_connection_manager.restore_released(None, None, kind="tcp")
    with server.app.test_request_context("/api/node-manager/dashboard"):
        payload = server.api_devices_dashboard().get_json()
    connection = payload["connection"]
    assert connection["mode"] == "released" and connection["type"] == "tcp"
    assert connection["address"] == "192.168.2.34:4403"
    assert connection["until"] is None


def test_the_node_manager_dashboard_never_calls_the_radio(rr1, monkeypatch):
    """The Node Manager now refreshes itself (3 s in a transition, 30 s otherwise): the endpoint it
    polls must read in-memory/cached status and local file sizes only - no radio call, no channel
    discovery, no adapter round trip."""
    server = rr1
    _accept(server, "tcp")

    def boom(*a, **k):
        raise AssertionError("the dashboard must not touch the radio")

    router = server.transport_router
    for name in ("get_channels", "get_nodes", "get_local_node", "get_metadata", "set_device_time",
                 "send_text", "send_messages", "connect", "reconnect", "drain_received",
                 "refresh_connection_info"):
        monkeypatch.setattr(router, name, boom)
    monkeypatch.setattr(server, "refresh_non_serial_connection_state", boom)
    monkeypatch.setattr(server, "_probe_tcp_identity", boom)
    monkeypatch.setattr(server, "verify_radio_identity", boom)
    for transport in (server.serial_ipc_transport, server.tcp_ipc_transport):
        monkeypatch.setattr(transport, "get_channels", boom)
        monkeypatch.setattr(transport, "connect", boom)
        monkeypatch.setattr(transport, "get_nodes", boom)
    with server.app.test_request_context("/api/node-manager/dashboard?refresh_channels=force"):
        assert server.api_devices_dashboard().get_json()["ok"] is True


def test_release_and_reconnect_are_never_counted_as_link_losses():
    from meshsrv.tcp_link_supervisor import OUTCOME_CONNECTED, TcpLinkSupervisor

    state = {"released": False, "connected": True, "now": 1000.0}
    supervisor = TcpLinkSupervisor(
        is_tcp_profile=lambda: True, identity_refused=lambda: False,
        link_connected=lambda: state["connected"],
        attempt=lambda: state.update(connected=True) or OUTCOME_CONNECTED,
        is_released=lambda: state["released"], clock=lambda: state["now"], rng=lambda: 0.5,
    )

    def run(seconds):
        end = state["now"] + seconds
        while state["now"] < end:
            supervisor.tick()
            state["now"] += 1.0

    run(60)                                    # healthy session
    state["released"] = True                   # Release: the manager is set BEFORE the session is closed
    run(2)
    state["connected"] = False                 # ... and then the session really closes
    run(120)
    state["released"] = False                  # Reconnect
    supervisor.trigger_now()
    run(60)
    status = supervisor.status()
    assert state["connected"] is True
    assert status["link_losses_1h"] == 0 and status["contention"] is False


def test_manager_log_entries_land_in_the_system_log_with_the_right_fields(tmp_path, monkeypatch):
    """Live on the NAS: entries came out shifted - event "ACTION", source = the truncated
    details text ("TCP connection closed - MeshCenter will "), details = the title. The manager
    called log_system_event(title, level, details, source) with (level, "radio", title, details)."""
    import system_log

    log_file = tmp_path / "system_events.jsonl"
    monkeypatch.setattr(system_log, "SYSTEM_LOG_FILE", str(log_file))
    monkeypatch.setattr(system_log, "DATA_DIR", str(tmp_path))

    class Pause:
        def set(self): pass
        def clear(self): pass

    store = RadioReleaseStore(lambda: str(tmp_path))
    manager = RadioConnectionManager(
        pause_event=Pause(), stop_listener=lambda: True, wait_serial_release=lambda **k: True,
        serial_port="x", log_event=system_log.log_system_event,
        transport_provider=lambda: "tcp", tcp_release=lambda: None, tcp_resume=lambda: None, store=store,
    )
    manager.release(duration_s=900)
    manager.reconnect()
    manager.release()
    manager.end_release()

    events = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    by_event = [(e["event"], e["source"], e["level"], e["details"]) for e in events]
    assert by_event[0] == (
        "Radio released", "radio", "ACTION",
        "TCP connection closed - MeshCenter will not reconnect until the chosen time has passed",
    )
    assert by_event[1] == (
        "Radio reconnect requested", "recovery", "ACTION", "TCP release ended - reconnecting to the radio",
    )
    assert by_event[2][0] == "Radio released" and by_event[2][3].endswith("until you press Reconnect")
    assert by_event[3] == (
        "Radio reconnect requested", "recovery", "ACTION", "TCP release ended by an explicit connect",
    )
    for event, source, level, details in by_event:
        assert event != "ACTION" and source in ("radio", "recovery") and len(details) > 20


def test_a_failed_release_is_logged_as_an_error_with_the_reason(tmp_path, monkeypatch):
    import system_log

    log_file = tmp_path / "system_events.jsonl"
    monkeypatch.setattr(system_log, "SYSTEM_LOG_FILE", str(log_file))
    monkeypatch.setattr(system_log, "DATA_DIR", str(tmp_path))

    def refuse():
        raise RuntimeError("A reconnect attempt is still running - try again in a moment")

    manager = RadioConnectionManager(
        pause_event=threading.Event(), stop_listener=lambda: True, wait_serial_release=lambda **k: True,
        serial_port="x", log_event=system_log.log_system_event,
        transport_provider=lambda: "tcp", tcp_release=refuse, store=RadioReleaseStore(lambda: str(tmp_path)),
    )
    ok, _ = manager.release()
    assert ok is False
    event = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert (event["event"], event["level"], event["source"]) == ("Radio release failed", "ERROR", "radio")
    assert "still running" in event["details"]
