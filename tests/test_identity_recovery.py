"""PR-2: transient-vs-definitive identity classification and the background
identity retry (process_identity_recovery). Motivation: a boot-race
DETECTION_ERROR on a TCP-accepted radio (pixel-111) was never re-checked, so
restore_active_transport() - which refuses an unverified TCP endpoint - never
ran. Fail-closed invariants are tested as hard as the recovery itself."""
import pytest

from meshsrv.radio_identity import (
    TRANSIENT_IDENTITY_ERROR_CODES,
    is_transient_identity_failure,
)
from meshsrv.radio_transport import TransportErrorCode


def _err(code, **extra):
    r = {"status": "DETECTION_ERROR", "detected": {}, "error": "boom", "error_code": code}
    r.update(extra)
    return r


# --- classification ---------------------------------------------------


@pytest.mark.parametrize("code", sorted(TRANSIENT_IDENTITY_ERROR_CODES))
def test_whitelisted_codes_are_transient(code):
    assert is_transient_identity_failure(_err(code)) is True


def test_whitelist_only_contains_real_transport_error_codes():
    valid = {c.value for c in TransportErrorCode}
    assert TRANSIENT_IDENTITY_ERROR_CODES <= valid


@pytest.mark.parametrize("code", [
    "identity_mismatch", "adapter_unavailable", "adapter_protocol_error", "unknown",
    "device_not_found", "unsupported", "busy", "port_check_inconclusive", "not_connected",
    "", None, "some_future_code",
])
def test_everything_else_is_not_transient(code):
    assert is_transient_identity_failure(_err(code)) is False


@pytest.mark.parametrize("result", [
    {"status": "MISMATCH", "detected": {"node_id": "!deadbeef"}, "error_code": "connect_failed"},
    {"status": "NOT_FOUND", "detected": {}, "error_code": "connect_failed"},
    {"status": "MATCH", "detected": {"node_id": "!1"}, "error_code": None},
    _err("connect_failed", detected={"node_id": "!1fa065f0"}),  # a radio identified itself
    None, "x",
])
def test_definitive_or_malformed_results_are_never_transient(result):
    assert is_transient_identity_failure(result) is False


# --- background retry -------------------------------------------------


@pytest.fixture
def env(server_module, monkeypatch):
    s = server_module
    saved_result = s.RADIO_IDENTITY_RESULT
    saved_state = dict(s.identity_recovery_state)
    s._reset_identity_recovery_state()
    s.identity_recovery_state["in_progress"] = False

    events, restores = [], []
    monkeypatch.setattr(
        s, "log_system_event",
        lambda title, level="INFO", details="", source="system": events.append((title, level)),
    )
    monkeypatch.setattr(s, "restore_active_transport", lambda radio, match: restores.append((radio, match)))

    class _Sync:
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            self._t()

    monkeypatch.setattr(s.threading, "Thread", _Sync)
    s.RADIO_IDENTITY_RESULT = _err("connect_failed")
    verify_calls = []

    def set_verify(*outcomes):
        seq = list(outcomes)

        def fake_verify():
            verify_calls.append(1)
            s.RADIO_IDENTITY_RESULT = seq.pop(0) if len(seq) > 1 else seq[0]
            return ""

        monkeypatch.setattr(s, "verify_radio_identity", fake_verify)

    yield s, events, restores, verify_calls, set_verify
    s.RADIO_IDENTITY_RESULT = saved_result
    s.identity_recovery_state.clear()
    s.identity_recovery_state.update(saved_state)


def test_first_tick_schedules_but_does_not_attempt(env):
    s, events, restores, verifies, set_verify = env
    set_verify({"status": "MATCH", "detected": {"node_id": "!1"}})
    s.process_identity_recovery("tcp", 1000.0)
    s.process_identity_recovery("tcp", 1029.0)

    assert verifies == []
    assert s.identity_recovery_state["next_attempt_at"] == 1030.0


def test_match_on_retry_runs_shared_bootstrap_with_identity_true(env):
    s, events, restores, verifies, set_verify = env
    set_verify({"status": "MATCH", "detected": {"node_id": "!1"}})
    s.process_identity_recovery("tcp", 1000.0)
    s.process_identity_recovery("tcp", 1031.0)

    assert len(verifies) == 1
    assert len(restores) == 1 and restores[0][1] is True
    assert ("TCP identity confirmed by background retry", "OK") in events
    assert s.identity_recovery_state["attempts"] == 0  # reset after success


def test_mismatch_on_retry_stops_and_never_bootstraps(env):
    s, events, restores, verifies, set_verify = env
    set_verify({"status": "MISMATCH", "detected": {"node_id": "!other"}})
    s.process_identity_recovery("tcp", 1000.0)
    s.process_identity_recovery("tcp", 1031.0)
    for t in (1100.0, 2000.0, 5000.0):
        s.process_identity_recovery("tcp", t)

    assert restores == []
    assert len(verifies) == 1  # no further attempts once resolved to MISMATCH
    assert ("TCP identity retry stopped", "ERROR") in events


def test_not_found_on_retry_stops_and_never_bootstraps(env):
    s, events, restores, verifies, set_verify = env
    set_verify({"status": "NOT_FOUND", "detected": {}, "error_code": None})
    s.process_identity_recovery("tcp", 1000.0)
    s.process_identity_recovery("tcp", 1031.0)
    s.process_identity_recovery("tcp", 9999.0)

    assert restores == [] and len(verifies) == 1


def test_schedule_and_cap(env):
    s, events, restores, verifies, set_verify = env
    set_verify(_err("connect_failed"))  # stays transient forever
    offsets = [30, 60, 120, 300, 300, 300]  # first is from first_seen, rest from the previous attempt
    t0 = 1000.0
    s.process_identity_recovery("tcp", t0)
    last = t0
    for i, off in enumerate(offsets, start=1):
        s.process_identity_recovery("tcp", last + off - 1)   # too early
        assert len(verifies) == i - 1
        last = last + off + 1
        s.process_identity_recovery("tcp", last)             # due
        assert len(verifies) == i
    s.process_identity_recovery("tcp", last + 100000)
    assert len(verifies) == 6  # capped
    assert ("TCP identity retry limit reached", "ERROR") in events
    assert restores == []


def test_non_tcp_or_non_transient_state_is_inert(env):
    s, events, restores, verifies, set_verify = env
    set_verify({"status": "MATCH", "detected": {"node_id": "!1"}})
    s.process_identity_recovery("serial", 0.0)
    s.process_identity_recovery("bluetooth", 0.0)
    s.RADIO_IDENTITY_RESULT = _err("adapter_unavailable")
    s.process_identity_recovery("tcp", 0.0)
    s.process_identity_recovery("tcp", 10_000.0)
    s.RADIO_IDENTITY_RESULT = {"status": "MISMATCH", "detected": {"node_id": "!x"}}
    s.process_identity_recovery("tcp", 20_000.0)

    assert verifies == [] and restores == []


def test_exception_in_attempt_is_contained_and_rescheduled(env, monkeypatch):
    s, events, restores, verifies, set_verify = env

    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setattr(s, "verify_radio_identity", boom)
    s.process_identity_recovery("tcp", 1000.0)
    s.process_identity_recovery("tcp", 1031.0)

    assert s.identity_recovery_state["in_progress"] is False
    assert s.identity_recovery_state["next_attempt_at"] > 1031.0
    assert ("TCP identity retry failed", "WARNING") in events


# --- interaction with router auto-reconnect ---------------------------


def test_router_autoreconnect_is_skipped_while_identity_is_detection_error(env, monkeypatch):
    """Router is still on its serial default in this state - a reconnect()
    would reconnect SERIAL for a TCP-accepted radio."""
    s, events, restores, verifies, set_verify = env
    calls = []
    monkeypatch.setattr(s.transport_router, "reconnect", lambda **k: calls.append(k))
    with s.state_lock:
        s.settings["tcp_ble_autorecovery"] = {"enabled": True}
    s.RADIO_IDENTITY_RESULT = _err("connect_failed")
    for t in range(0, 600, 30):
        s.process_transport_autorecovery("DISCONNECTED", "tcp", float(t))

    assert calls == []
