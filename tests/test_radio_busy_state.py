"""SIG4b: while a command holds the radio the status chip says "Busy: <reason>", never "Offline"."""
import threading

from meshsrv.serial_port_supervisor import SerialPortSupervisor


def _supervisor():
    return SerialPortSupervisor(
        cli_path="/does/not/matter/for/this/test",
        port="/dev/ttyFAKE",
        radio_lock=threading.RLock(),
        pause_listen=threading.Event(),
    )


def test_not_busy_by_default():
    assert _supervisor().busy_reason(listener_running=True) == ""
    assert _supervisor().busy_reason(listener_running=False) == ""


def test_busy_with_the_given_reason_during_a_command():
    sup = _supervisor()
    sup.begin_busy("traceroute")
    assert sup.busy_reason(listener_running=False) == "traceroute"
    assert sup.busy_reason(listener_running=True) == "traceroute"


def test_still_busy_while_the_listener_comes_back_then_free():
    sup = _supervisor()
    sup.begin_busy("traceroute")
    sup.end_busy()
    assert sup.busy_reason(listener_running=False) == "traceroute", "the listener is not back yet"
    assert sup.busy_reason(listener_running=True) == "", "the listener is back: not busy"


def test_the_post_command_window_ends():
    sup = _supervisor()
    sup.begin_busy("node_scan")
    sup.end_busy()
    sup._claim_released_at -= 60
    assert sup.busy_reason(listener_running=False) == ""


def test_an_empty_reason_is_a_generic_radio_command():
    sup = _supervisor()
    sup.begin_busy("")
    assert sup.busy_reason(listener_running=True) == "radio_command"


def test_a_claim_marks_the_radio_busy_for_its_whole_span(monkeypatch):
    sup = _supervisor()
    from meshsrv.serial_port_supervisor import PortReleaseOutcome
    monkeypatch.setattr(sup, "_prepare_command", lambda timeout=8: PortReleaseOutcome.PORT_FREE)
    seen = []
    with sup.claim_exclusive_access(timeout=1, cooldown=0, reason="traceroute"):
        seen.append(sup.busy_reason(listener_running=False))
    assert seen == ["traceroute"]
    assert sup.busy_reason(listener_running=False) == "traceroute", "the listener has not come back yet"
    assert sup.busy_reason(listener_running=True) == ""


def test_the_health_endpoint_says_busy_at_once(server_module, monkeypatch):
    srv = server_module
    monkeypatch.setattr(srv, "current_radio_transport", lambda: "serial")
    srv.listener_supervisor.begin_busy("traceroute")
    try:
        with srv.app.test_request_context("/api/radio_health"):
            payload = srv.api_radio_health().get_json()
    finally:
        srv.listener_supervisor.end_busy()
        srv.listener_supervisor._claim_released_at = float("-inf")
    assert payload["busy_reason"] == "traceroute"
    assert payload["status"] == "PAUSED" and payload["level"] == "WARNING"
    with srv.app.test_request_context("/api/radio_health"):
        quiet = srv.api_radio_health().get_json()
    assert quiet["busy_reason"] == ""


def test_every_locale_words_the_busy_chip():
    import json, os
    for lang in ("en", "de", "ru", "uk"):
        data = json.load(open(os.path.join("static", "i18n", f"{lang}.json"), encoding="utf-8"))["node_manager"]
        for key in ("header_busy", "header_busy_reason", "busy_reason_traceroute", "busy_reason_node_scan",
                    "busy_reason_request_telemetry", "busy_reason_request_position", "busy_reason_radio_command"):
            assert data[key].strip(), (lang, key)
        assert "{reason}" in data["header_busy_reason"], lang
