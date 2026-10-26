"""BUSY1: the stored radio health is only refreshed every 30 s, so right after a command the chip kept saying a
plain "Busy" (PAUSED) for up to ~14 s although the listener was back. /api/radio_health now recomputes from the
live state when the stored status is a stale PAUSED/STARTING."""
import pytest


def _override(srv, **kw):
    args = dict(stored_status="PAUSED", transport="serial", released=False, busy_reason="", is_paused=False,
                listener_running=True, packet_age=5, planned_restart_active=False)
    args.update(kw)
    return srv.live_serial_health_override(**args)


def test_a_stale_paused_with_a_running_listener_becomes_ok(server_module):
    status, level, _reason, _rec = _override(server_module)
    assert (status, level) == ("OK", "OK")


def test_a_stale_starting_with_a_recent_packet_becomes_ok(server_module):
    assert _override(server_module, stored_status="STARTING")[0] == "OK"


def test_no_packet_yet_stays_starting(server_module):
    assert _override(server_module, stored_status="STARTING", packet_age=None)[0] == "STARTING"


@pytest.mark.parametrize("kw", [
    {"stored_status": "OK"}, {"stored_status": "LISTENER_DOWN"}, {"transport": "tcp"}, {"released": True},
    {"busy_reason": "traceroute"}, {"is_paused": True}, {"listener_running": False}, {"planned_restart_active": True},
])
def test_everything_else_is_left_alone(server_module, kw):
    assert _override(server_module, **kw) is None


def test_the_endpoint_does_not_wait_for_the_health_tick(server_module, monkeypatch):
    srv = server_module
    monkeypatch.setattr(srv, "current_radio_transport", lambda: "serial")
    with srv.state_lock:
        saved = {k: srv.radio_health.get(k) for k in ("status", "level", "listener_running", "last_packet")}
        srv.radio_health.update(status="PAUSED", level="WARNING", listener_running=True, last_packet=srv.time.time() - 3)
    srv.pause_listen.clear()
    try:
        with srv.app.test_request_context("/api/radio_health"):
            payload = srv.api_radio_health().get_json()
    finally:
        with srv.state_lock:
            srv.radio_health.update(saved)
    assert payload["status"] == "OK" and payload["busy_reason"] == ""
