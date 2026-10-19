"""DK2/DK3 - TCP link supervisor: unbounded retry with capped backoff, one
reconnect path, a session only counts once it has stayed up, contention notice,
health reflecting reality, no serial release for TCP, serial untouched.

The schedule/state-machine tests use a fake clock (a 25+ minute outage is
simulated, not waited for)."""
import threading

import pytest

from meshsrv import tcp_link_supervisor as tls
from meshsrv.radio_transport import ConnectionDescriptor, ConnectionType
from meshsrv.tcp_link_supervisor import (
    BACKOFF_DELAYS_S,
    HOLD_OFF_S,
    MAX_DELAY_S,
    OUTCOME_CONNECTED,
    OUTCOME_RETRY,
    OUTCOME_STOP,
    UP_THRESHOLD_S,
    TcpLinkSupervisor,
    backoff_delay,
)


class World:
    """A fake radio link + clock the supervisor's callables close over."""

    def __init__(self):
        self.now = 1000.0
        self.tcp_profile = True
        self.refused = False
        self.auto = True
        self.connected = False
        self.radio_reachable = False
        self.radio_keeps_session_for = None   # None = forever, else seconds a fresh session lives
        self.session_started = None
        self.attempts = []
        self.events = []
        self.outcome_override = None

    def clock(self):
        return self.now

    def attempt(self):
        self.attempts.append(self.now)
        if self.outcome_override:
            return self.outcome_override
        if self.radio_reachable:
            self.connected = True
            self.session_started = self.now
            return OUTCOME_CONNECTED
        return OUTCOME_RETRY

    def advance(self, seconds, supervisor, step=1.0):
        end = self.now + seconds
        while self.now < end:
            if self.connected and self.radio_keeps_session_for is not None \
                    and self.now - self.session_started >= self.radio_keeps_session_for:
                self.connected = False  # the radio closed the session
            supervisor.tick()
            self.now += step

    def supervisor(self):
        return TcpLinkSupervisor(
            is_tcp_profile=lambda: self.tcp_profile,
            identity_refused=lambda: self.refused,
            link_connected=lambda: self.connected,
            attempt=self.attempt,
            auto_enabled=lambda: self.auto,
            clock=self.clock,
            rng=lambda: 0.5,  # zero jitter
            on_event=lambda *event: self.events.append(event),
        )


def test_backoff_schedule_is_capped_at_120_and_never_ends():
    delays = [backoff_delay(i, rng=lambda: 0.5) for i in range(1, 12)]
    assert delays[:4] == [30, 60, 90, 120]
    assert delays[4:] == [120] * 7  # capped, not exhausted
    assert MAX_DELAY_S == 120


def test_jitter_stays_within_ten_percent():
    low = backoff_delay(3, rng=lambda: 0.0)
    high = backoff_delay(3, rng=lambda: 1.0)
    assert 81 <= low < 90 < high <= 99


def test_unbounded_retry_over_a_long_outage_then_recovery():
    """Radio away for 45 simulated minutes (far past the old ~18.5 min / 6-attempt
    and 3-per-30-min limits): attempts never stop; it reconnects the moment the
    radio is back and is marked stable after 20 s - no restart involved."""
    world = World()
    supervisor = world.supervisor()
    world.advance(45 * 60, supervisor)

    assert len(world.attempts) > 20, "must keep trying well past any old attempt limit"
    gaps = [b - a for a, b in zip(world.attempts, world.attempts[1:])]
    assert max(gaps) <= MAX_DELAY_S + 6, "delay is capped at 2 minutes (+ tick granularity)"
    assert supervisor.is_recovering()

    world.radio_reachable = True
    world.advance(MAX_DELAY_S + 10, supervisor)
    assert world.connected
    world.advance(UP_THRESHOLD_S + 2, supervisor)
    assert supervisor.status()["state"] == tls.STATE_IDLE
    assert supervisor.status()["failed_attempts"] == 0
    assert any(title == "TCP link restored" for title, _, _ in world.events)


def test_unreachable_at_boot_for_more_than_20_minutes_recovers_when_it_appears():
    world = World()  # never connected since boot (DETECTION_ERROR case)
    supervisor = world.supervisor()
    world.advance(25 * 60, supervisor)
    assert not world.connected and len(world.attempts) >= 12

    world.radio_reachable = True
    world.advance(MAX_DELAY_S + 10, supervisor)
    assert world.connected


def test_first_attempt_for_a_radio_never_reached_is_immediate():
    world = World()
    supervisor = world.supervisor()
    supervisor.tick()
    assert len(world.attempts) == 1


def test_a_link_that_was_up_waits_the_hold_off_before_the_first_attempt():
    """The radio needs ~25 s to release its slot after it closes a session."""
    world = World()
    world.connected = True
    supervisor = world.supervisor()
    world.advance(30, supervisor)             # healthy and stable
    world.radio_reachable = True
    world.connected = False                   # the radio closed the session
    t_down = world.now
    world.advance(60, supervisor)
    assert world.attempts, "it does reconnect"
    assert world.attempts[0] - t_down >= HOLD_OFF_S - 1, "but not before the hold-off"
    assert world.attempts[0] - t_down <= HOLD_OFF_S + 6


def test_status_reports_attempt_and_countdown_while_waiting():
    world = World()
    supervisor = world.supervisor()
    supervisor.tick()  # attempt 1 fails -> waits ~30 s
    status = supervisor.status()
    assert status["state"] == tls.STATE_WAITING
    assert status["failed_attempts"] == 1 and status["attempt"] == 2, "the next attempt is the 2nd"
    assert status["next_in_s"] == 30
    world.now += 10
    assert supervisor.status()["next_in_s"] == 20


def test_a_session_that_dies_within_20_seconds_is_not_a_recovery():
    """DK2 NAS log: 'attempt 6' right after a success. The failure counter must
    survive a session the radio drops a second after accepting it."""
    world = World()
    supervisor = world.supervisor()
    world.advance(5 * 60, supervisor)                 # a few failed attempts
    failed_before = supervisor.status()["failed_attempts"]
    assert failed_before >= 4

    world.radio_reachable = True
    world.radio_keeps_session_for = 3                 # the radio closes it after 3 s
    world.advance(MAX_DELAY_S + 10, supervisor)
    assert world.session_started is not None
    world.advance(8, supervisor)
    status = supervisor.status()
    assert status["failed_attempts"] > failed_before, "an unstable session counts as one more failure"
    assert status["state"] == tls.STATE_WAITING


def test_stability_resets_the_counter_only_after_the_threshold():
    world = World()
    supervisor = world.supervisor()
    world.advance(4 * 60, supervisor)
    world.radio_reachable = True
    while not world.connected:
        world.advance(1, supervisor)
    world.advance(2, supervisor)
    assert supervisor.status()["failed_attempts"] > 0, "not yet proven"
    world.advance(UP_THRESHOLD_S + 2, supervisor)
    assert supervisor.status()["failed_attempts"] == 0


def test_repeated_quick_closes_flag_contention_and_back_off_hard():
    world = World()
    supervisor = world.supervisor()
    world.radio_reachable = True
    world.radio_keeps_session_for = 2
    world.advance(10 * 60, supervisor, step=1.0)
    status = supervisor.status()
    assert status["contention"] is True
    assert status["link_losses_1h"] >= 3
    # once contention is flagged the delay floor is CONTENTION_FLOOR_S (5 min)
    assert [b - a for a, b in zip(world.attempts, world.attempts[1:])][-1] >= tls.CONTENTION_FLOOR_S - 1
    # a user Reconnect tries at once but does NOT clear the flag (RR1: only a
    # stable session does)
    world.radio_keeps_session_for = None
    supervisor.trigger_now()
    n = len(world.attempts)
    supervisor.tick()
    assert len(world.attempts) == n + 1 and supervisor.status()["contention"] is True


def test_long_sessions_do_not_flag_contention():
    world = World()
    world.radio_reachable = True
    supervisor = world.supervisor()
    world.advance(30, supervisor)
    world.radio_keeps_session_for = 40 * 60    # sessions of 40 min: a drop now and then
    world.advance(3 * 3600, supervisor)
    status = supervisor.status()
    assert status["contention"] is False
    assert status["link_losses_1h"] <= 2


def test_trigger_now_resets_backoff_and_attempts_on_the_next_tick():
    world = World()
    supervisor = world.supervisor()
    world.advance(10 * 60, supervisor)
    assert supervisor.status()["failed_attempts"] >= 4
    before = len(world.attempts)

    supervisor.trigger_now()
    assert supervisor.status()["failed_attempts"] == 0
    supervisor.tick()
    assert len(world.attempts) == before + 1, "immediate attempt, no waiting for next_at"
    assert supervisor.status()["next_in_s"] == BACKOFF_DELAYS_S[0], "backoff restarted at 30 s"


def test_trigger_now_forces_a_fresh_attempt_even_if_the_link_looks_healthy():
    world = World()
    world.connected = True
    world.radio_reachable = True
    supervisor = world.supervisor()
    supervisor.tick()
    assert world.attempts == []
    supervisor.trigger_now()
    supervisor.tick()
    assert len(world.attempts) == 1


def test_mismatch_or_not_found_stops_the_loop_fail_closed():
    world = World()
    supervisor = world.supervisor()
    world.refused = True
    world.advance(10 * 60, supervisor)
    assert world.attempts == []
    assert not supervisor.is_recovering()

    world.refused = False
    world.outcome_override = OUTCOME_STOP  # attempt itself discovers a different radio
    supervisor.trigger_now()
    supervisor.tick()
    assert len(world.attempts) == 1
    assert supervisor.status()["state"] == tls.STATE_IDLE
    world.attempts.clear()
    world.refused = True  # identity layer now records MISMATCH
    world.advance(10 * 60, supervisor)
    assert world.attempts == [], "no further attempts while identity is refused"


def test_idle_for_non_tcp_profiles_and_when_auto_reconnect_is_off():
    world = World()
    supervisor = world.supervisor()
    world.tcp_profile = False
    world.advance(5 * 60, supervisor)
    assert world.attempts == [] and not supervisor.is_recovering()

    world.tcp_profile = True
    world.auto = False
    world.advance(5 * 60, supervisor)
    assert world.attempts == []
    supervisor.trigger_now()  # explicit user action still works
    supervisor.tick()
    assert len(world.attempts) == 1


def test_an_attempt_that_raises_is_just_a_failed_attempt():
    world = World()
    supervisor = world.supervisor()

    def attempt():
        raise RuntimeError("socket on fire")

    supervisor._attempt = attempt
    supervisor.tick()
    status = supervisor.status()
    assert status["state"] == tls.STATE_WAITING and "socket on fire" in status["last_error"]


def test_attempts_are_single_flight_via_the_shared_lock():
    """No two connects at once: a second tick while an attempt is still running
    blocks on the (shared, re-entrant) lock instead of overlapping it."""
    lock = threading.RLock()
    inside = []
    overlap = []
    release = threading.Event()
    started = threading.Event()

    def attempt():
        inside.append(1)
        if len(inside) > 1:
            overlap.append(True)
        started.set()
        release.wait(5)
        inside.pop()
        return OUTCOME_RETRY

    world = World()
    supervisor = TcpLinkSupervisor(
        is_tcp_profile=lambda: True, identity_refused=lambda: False,
        link_connected=lambda: False, attempt=attempt, lock=lock,
        clock=world.clock, rng=lambda: 0.5,
    )
    t1 = threading.Thread(target=supervisor.tick)
    t1.start()
    assert started.wait(2)
    # Someone else (boot probe / manual flow) needing the same lock must wait.
    got = []
    t2 = threading.Thread(target=lambda: (lock.acquire(), got.append(True), lock.release()))
    t2.start()
    t2.join(0.3)
    assert got == [], "lock is held for the whole attempt"
    release.set()
    t1.join(2)
    t2.join(2)
    assert got == [True] and overlap == []


# ---------------------------------------------------------------------------
# server.py wiring
# ---------------------------------------------------------------------------


def test_health_is_reconnecting_not_connected_while_the_supervisor_works(server_module):
    status, level, reason, _ = server_module.compute_radio_health_status(
        active_transport="tcp", is_released=False, is_paused=False, listener_running=False,
        packet_age=None,
        transport_state={"state": "error", "type": "tcp", "address": "10.0.0.5:4403"},
        tcp_link={"state": "waiting", "attempt": 3, "next_in_s": 42},
    )
    assert (status, level) == ("RECONNECTING", "WARNING")
    assert "attempt 3" in reason and "42" in reason


def test_health_is_disconnected_when_the_transport_is_not_connected(server_module):
    status, level, _, _ = server_module.compute_radio_health_status(
        active_transport="tcp", is_released=False, is_paused=False, listener_running=False,
        packet_age=None,
        transport_state={"state": "disconnected", "type": "tcp"},
        tcp_link={"state": "idle", "attempt": 0, "next_in_s": None},
    )
    assert (status, level) == ("DISCONNECTED", "ERROR")


def test_a_router_still_on_serial_never_reads_as_a_connected_tcp_link(server_module):
    status, _, _, _ = server_module.compute_radio_health_status(
        active_transport="tcp", is_released=False, is_paused=False, listener_running=False,
        packet_age=None, transport_state={"state": "connected", "type": "serial"},
    )
    assert status == "DISCONNECTED"


def test_quiet_mesh_does_not_flip_a_connected_tcp_link(server_module):
    """packet_age is informational for TCP: a connected link with no mesh packets
    for hours stays CONNECTED/OK (liveness comes from the transport itself)."""
    status, level, _, _ = server_module.compute_radio_health_status(
        active_transport="tcp", is_released=False, is_paused=False, listener_running=False,
        packet_age=11186, transport_state={"state": "connected", "type": "tcp"},
    )
    assert (status, level) == ("CONNECTED", "OK")


def test_serial_health_is_unchanged(server_module):
    assert server_module.compute_radio_health_status(
        active_transport="serial", is_released=False, is_paused=False, listener_running=True,
        packet_age=10, transport_state={"state": "connected", "type": "serial"},
        tcp_link={"state": "waiting", "attempt": 1, "next_in_s": 5},
    )[:2] == ("OK", "OK")


def test_listener_autorecovery_is_a_noop_for_tcp(server_module, monkeypatch):
    called = []
    monkeypatch.setattr(server_module, "log_system_event", lambda *a, **k: called.append(a))
    state_before = dict(server_module.listener_recovery_state)
    server_module.settings["listener_autorecovery"] = {"enabled": True, "delay": 30}
    try:
        server_module.process_listener_autorecovery(
            status="LISTENER_DOWN", listener_running=False, now_ts=10_000, active_transport="tcp",
        )
    finally:
        server_module.settings.pop("listener_autorecovery", None)
    assert called == []
    assert dict(server_module.listener_recovery_state) == state_before


def test_tcp_attempt_never_touches_serial_release(server_module, monkeypatch):
    """The reconnect attempt must go identity probe -> restore only; any serial
    release / lsof / tty path would be a regression of the DK2 live finding."""
    accepted = {"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "10.0.0.5", "port": 4403}}
    original = server_module.instance_manager.get()
    updated = dict(original)
    updated["radio"] = accepted
    server_module.INSTANCE_IDENTITY = server_module.instance_manager.save(updated)

    calls = []
    forbidden = ("wait_serial_release", "stop_listener", "prepare_radio_command", "claim_exclusive_access")
    for name in forbidden:
        if hasattr(server_module, name):
            monkeypatch.setattr(server_module, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(server_module.listener_supervisor, "claim_exclusive_access",
                        lambda *a, **k: calls.append("claim_exclusive_access"), raising=False)
    monkeypatch.setattr(server_module.tcp_ipc_transport, "disconnect", lambda **k: calls.append("tcp_disconnect"))

    def fake_verify(retry=True):
        calls.append(("verify", retry))
        server_module.RADIO_IDENTITY_RESULT = {"status": "MATCH"}

    monkeypatch.setattr(server_module, "verify_radio_identity", fake_verify)
    monkeypatch.setattr(server_module, "restore_active_transport", lambda *a, **k: calls.append("restore"))
    monkeypatch.setattr(server_module, "_tcp_link_connected", lambda: True)
    try:
        outcome = server_module.tcp_link_attempt()
    finally:
        server_module.instance_manager.save(original)
        server_module.INSTANCE_IDENTITY = original

    assert outcome == OUTCOME_CONNECTED
    assert calls == ["tcp_disconnect", ("verify", False), "restore"], "old session closed BEFORE the probe"


def _call_reconnect_now(server_module):
    # Called inside a request context, below the auth/CSRF before_request gate
    # (that gate is covered by its own tests); this exercises the route's logic.
    with server_module.app.test_request_context("/api/meshtastic/reconnect_now", method="POST", json={}):
        result = server_module.api_meshtastic_reconnect_now()
    response, status = result if isinstance(result, tuple) else (result, 200)
    return status, response.get_json()


def test_reconnect_now_in_detection_error_triggers_a_reprobe(server_module, monkeypatch):
    """Reconnect in DETECTION_ERROR used to answer 'No previously-connected TCP
    endpoint'; now it triggers the supervisor (whose attempt re-probes)."""
    triggered = []
    monkeypatch.setattr(server_module, "current_radio_transport", lambda: "tcp")
    monkeypatch.setattr(server_module.tcp_link_supervisor, "trigger_now",
                        lambda: triggered.append(1) or {"state": "waiting", "attempt": 0})
    monkeypatch.setattr(server_module, "RADIO_IDENTITY_RESULT", {"status": "DETECTION_ERROR"})
    status, body = _call_reconnect_now(server_module)
    assert status == 200 and body["ok"] is True and triggered == [1]


def test_reconnect_now_is_refused_for_a_refused_identity(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "current_radio_transport", lambda: "tcp")
    monkeypatch.setattr(server_module, "RADIO_IDENTITY_RESULT", {"status": "MISMATCH"})
    status, body = _call_reconnect_now(server_module)
    assert status == 409 and body["error_code"] == "identity_mismatch"


def test_reconnect_now_is_refused_for_serial(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "current_radio_transport", lambda: "serial")
    status, body = _call_reconnect_now(server_module)
    assert status == 409 and body["error_code"] == "reconnect_not_applicable"


def test_disconnecting_others_skips_a_never_connected_serial_for_tcp():
    from meshsrv.radio_endpoint import build_transport_connect_new
    from meshsrv.radio_transport import ConnectionInfo, ConnectionState

    class T:
        def __init__(self, info):
            self.info = info
            self.disconnects = []
            self.connects = []

        def get_connection_info(self):
            return self.info

        def disconnect(self, **kw):
            self.disconnects.append(kw)

        def connect(self, descriptor, **kw):
            self.connects.append(descriptor)


# ---------------------------------------------------------------- RR1: contention fixtures

def _cycle_world(session_s):
    world = World()
    world.radio_reachable = True
    world.radio_keeps_session_for = session_s
    return world, world.supervisor()


def test_rr1_a_radio_closing_30s_sessions_is_flagged_within_five_minutes():
    """~15 radio closes in ~22 min with ~30 s sessions (the real alternation pattern)."""
    world, supervisor = _cycle_world(30)
    world.advance(30, supervisor)               # first session comes up
    start = world.now
    flagged_at = None
    for _ in range(22 * 60):
        world.advance(1, supervisor)
        if flagged_at is None and supervisor.status()["contention"]:
            flagged_at = world.now - start
    assert flagged_at is not None and flagged_at <= 5 * 60, flagged_at
    assert supervisor.status()["contention_since_s"] is not None
    # and it backs off harder afterwards instead of re-dialling every ~90 s
    assert [b - a for a, b in zip(world.attempts, world.attempts[1:])][-1] >= tls.CONTENTION_FLOOR_S - 1


def test_rr1_b_a_single_reboot_is_not_contention():
    world = World()
    world.radio_reachable = True
    supervisor = world.supervisor()
    world.advance(10 * 60, supervisor)           # stable
    world.connected = False                       # the radio reboots...
    world.radio_reachable = False
    world.advance(3 * 60, supervisor)            # ...and is away for 3 min
    world.radio_reachable = True
    world.advance(10 * 60, supervisor)
    assert supervisor.status()["contention"] is False
    assert world.connected


def test_rr1_b_a_wifi_blip_is_not_contention():
    world = World()
    world.radio_reachable = True
    supervisor = world.supervisor()
    world.advance(20 * 60, supervisor)
    world.connected = False
    world.advance(40, supervisor)
    world.advance(10 * 60, supervisor)
    assert supervisor.status()["contention"] is False


def test_rr1_b_a_25_minute_power_off_is_not_contention():
    world = World()
    world.radio_reachable = True
    supervisor = world.supervisor()
    world.advance(15 * 60, supervisor)
    world.connected = False
    world.radio_reachable = False
    world.advance(25 * 60, supervisor)            # unplugged: failed attempts are not closes
    assert supervisor.status()["failed_attempts"] >= 5
    world.radio_reachable = True
    world.advance(10 * 60, supervisor)
    assert supervisor.status()["contention"] is False


def test_rr1_c_the_flag_clears_only_after_a_stable_session():
    world, supervisor = _cycle_world(30)
    world.advance(10 * 60, supervisor)
    assert supervisor.status()["contention"] is True
    world.radio_keeps_session_for = None          # the other client went away
    world.advance(15 * 60, supervisor)            # up, but not yet 15 min
    assert supervisor.status()["contention"] is True or world.connected
    world.advance(16 * 60, supervisor)
    assert world.connected
    assert supervisor.status()["contention"] is False
    assert any("contention cleared" in event[0] for event in world.events)


def test_rr1_regular_alternation_every_12_minutes_is_flagged():
    world, supervisor = _cycle_world(12 * 60)
    world.advance(55 * 60, supervisor)
    assert supervisor.status()["contention"] is True


def test_rr1_contention_event_carries_the_release_hint_once():
    world, supervisor = _cycle_world(30)
    world.advance(15 * 60, supervisor)
    hints = [e for e in world.events if "in use by another client" in e[0]]
    assert len(hints) == 1 and "Release radio" in hints[0][2]


# ---------------------------------------------------------------- RR1: released = zero attempts

def test_rr1_released_means_no_attempts_and_nothing_counted():
    world = World()
    world.released = False
    supervisor = TcpLinkSupervisor(
        is_tcp_profile=lambda: True,
        identity_refused=lambda: False,
        link_connected=lambda: world.connected,
        attempt=world.attempt,
        is_released=lambda: world.released,
        clock=world.clock,
        rng=lambda: 0.5,
        on_event=lambda *event: world.events.append(event),
    )
    world.radio_reachable = True
    world.connected = True                         # a healthy session
    world.advance(60, supervisor)
    assert world.connected and world.attempts == []
    world.released = True
    world.connected = False                        # the release closed the session
    world.advance(30 * 60, supervisor)
    assert world.attempts == [], "not a single attempt while released"
    status = supervisor.status()
    assert status["state"] == tls.STATE_IDLE
    assert status["link_losses_1h"] == 0 and status["contention"] is False
    supervisor.trigger_now()                       # ignored while released
    world.advance(30, supervisor)
    assert world.attempts == []
    world.released = False                         # Reconnect / timer ended it
    supervisor.trigger_now()
    world.advance(10, supervisor)
    assert len(world.attempts) == 1 and world.connected


def test_rr1_release_landing_between_tick_and_attempt_does_not_dial():
    world = World()
    state = {"released": False}
    supervisor = TcpLinkSupervisor(
        is_tcp_profile=lambda: True,
        identity_refused=lambda: False,
        link_connected=lambda: False,
        attempt=world.attempt,
        is_released=lambda: state["released"],
        clock=world.clock,
        rng=lambda: 0.5,
    )
    # the tick decided to dial (state ATTEMPTING) ...
    supervisor._state = tls.STATE_ATTEMPTING
    # ... and the release lands before the dial
    state["released"] = True
    supervisor._run_attempt()
    assert world.attempts == []
    assert supervisor.status()["state"] == tls.STATE_IDLE
