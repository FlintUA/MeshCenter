"""tests/test_planned_restart.py - UPD1: a planned self-restart is not a fault.

`systemctl restart meshcenter.service` kills the process that runs it: the helper dies with
SIGTERM (used to be an ERROR "Restart after update failed ... died with SIGTERM") and the
--listen child goes with it (used to be LISTENER_DOWN + "unexpected exit"). A 60 s window
(meshsrv/planned_restart.py) marks that as expected, for every self-restart.
"""
import subprocess
import threading
from functools import wraps
from unittest.mock import patch

import pytest
from flask import Flask

from api.api_updates import register_updates_routes
from meshsrv import planned_restart, serial_port_supervisor as spv, update_service


@pytest.fixture(autouse=True)
def _clean_window():
    planned_restart.clear()
    yield
    planned_restart.clear()


def _sigterm_error():
    return subprocess.CalledProcessError(-15, ["sudo", "systemctl", "restart", "meshcenter.service"])


def _runner_raising(error):
    def run(*args, **kwargs):
        raise error
    return run


# ------------------------------------------------------------------ the window
def test_window_opens_and_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(planned_restart.time, "monotonic", lambda: now[0])
    assert planned_restart.active() is False
    planned_restart.begin("update")
    assert planned_restart.active() is True and planned_restart.reason() == "update"
    now[0] += planned_restart.WINDOW_S - 1
    assert planned_restart.active() is True
    now[0] += 2
    assert planned_restart.active() is False and planned_restart.reason() == ""


def test_window_is_sixty_seconds():
    assert planned_restart.WINDOW_S == 60.0


def test_a_second_begin_never_shortens_the_window(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(planned_restart.time, "monotonic", lambda: now[0])
    planned_restart.begin("a", window_s=60)
    planned_restart.begin("b", window_s=5)
    now[0] = 30
    assert planned_restart.active() is True


@pytest.mark.parametrize("code,expected", [(-15, True), (143, True), (1, False), (0, False), (None, False)])
def test_helper_killed_detection(code, expected):
    error = subprocess.CalledProcessError(code if code is not None else 0, ["x"])
    if code is None:
        error = RuntimeError("no returncode")
    assert planned_restart.helper_killed_by_the_restart(error) is expected


# ------------------------------------------------------------------ the helper command
def test_sigterm_of_the_helper_is_info_not_error():
    events = []
    planned_restart.begin("restart")
    ok = planned_restart.run_restart_command(
        ["sudo", "-n", "systemctl", "restart", "meshcenter.service"], label="Update restart",
        log=lambda *a, **k: events.append((a, k)), source="update", runner=_runner_raising(_sigterm_error()))
    assert ok is True
    assert events == [(("Update restart", "INFO", "MeshCenter is restarting"), {"source": "update"})]
    assert planned_restart.active() is True, "the window stays open: the restart is under way"


def test_a_real_failure_is_an_error_and_closes_the_window():
    events = []
    planned_restart.begin("restart")
    error = subprocess.CalledProcessError(1, ["sudo"], stderr="a password is required")
    ok = planned_restart.run_restart_command(
        ["sudo"], label="Update restart", log=lambda *a, **k: events.append(a), source="update",
        runner=_runner_raising(error))
    assert ok is False
    assert events[0][0] == "Update restart failed" and events[0][1] == "ERROR"
    assert planned_restart.active() is False, "nothing is restarting, so nothing is excused"


def test_a_missing_sudo_is_an_error_too():
    events = []
    planned_restart.begin("restart")
    ok = planned_restart.run_restart_command(
        ["sudo"], label="Device reboot", log=lambda *a, **k: events.append(a),
        runner=_runner_raising(FileNotFoundError("sudo")))
    assert ok is False and events[0][0] == "Device reboot failed" and planned_restart.active() is False


def test_a_helper_that_returns_normally_is_success():
    events = []
    ok = planned_restart.run_restart_command(["true"], label="x", log=lambda *a, **k: events.append(a), runner=lambda *a, **k: None)
    assert ok is True and events == []


# ------------------------------------------------------------------ listener + health
def _supervisor(planned):
    return spv.SerialPortSupervisor(
        cli_path="x", port="/dev/ttyACM0", radio_lock=threading.RLock(), pause_listen=threading.Event(),
        is_planned_restart=planned)


def test_listener_exit_during_a_planned_restart_is_intentional():
    assert _supervisor(lambda: False)._stop_is_intentional() is False, "a real crash is still a crash"
    assert _supervisor(lambda: True)._stop_is_intentional() is True
    assert _supervisor(planned_restart.active)._stop_is_intentional() is False
    planned_restart.begin("update")
    assert _supervisor(planned_restart.active)._stop_is_intentional() is True


def test_listener_exit_after_the_window_is_unexpected_again(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(planned_restart.time, "monotonic", lambda: now[0])
    supervisor = _supervisor(planned_restart.active)
    planned_restart.begin("update")
    assert supervisor._stop_is_intentional() is True
    now[0] = planned_restart.WINDOW_S + 1
    # (spv uses its own time module reference; the window itself is what expired)
    assert planned_restart.active() is False


def test_the_default_supervisor_has_no_planned_restart():
    supervisor = spv.SerialPortSupervisor(
        cli_path="x", port="/dev/ttyACM0", radio_lock=threading.RLock(), pause_listen=threading.Event())
    assert supervisor._stop_is_intentional() is False


def test_health_says_restarting_not_listener_down(server_module):
    base = dict(active_transport="serial", is_released=False, is_paused=False, listener_running=False,
                packet_age=10, transport_state={"state": "connected"})
    during = server_module.compute_radio_health_status(**base, planned_restart=True)
    assert during[0] == "PAUSED" and during[1] == "INFO" and "restarting" in during[2].lower()
    assert server_module.compute_radio_health_status(**base)[0] == "LISTENER_DOWN", "a real outage is still reported"


def test_health_restarting_for_tcp_too(server_module):
    status = server_module.compute_radio_health_status(
        active_transport="tcp", is_released=False, is_paused=False, listener_running=False, packet_age=None,
        transport_state={"state": "disconnected"}, planned_restart=True)
    assert status[0] == "PAUSED" and status[1] == "INFO"


def test_a_released_radio_still_reads_released_during_a_restart(server_module):
    status = server_module.compute_radio_health_status(
        active_transport="tcp", is_released=True, is_paused=False, listener_running=False, packet_age=None,
        transport_state={"state": "disconnected"}, planned_restart=True)
    assert status[0] == "RELEASED"


# ------------------------------------------------------------------ every self-restart path
def test_profile_switch_restart_uses_the_shared_helper_and_opens_the_window(server_module, monkeypatch):
    srv = server_module
    logged = []
    monkeypatch.setattr(srv, "log_system_event", lambda *a, **k: logged.append((a, k)))
    monkeypatch.setattr(srv.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(srv.runtime_env, "is_container", lambda: False)
    with patch("meshsrv.planned_restart.subprocess.run", side_effect=_sigterm_error()):
        srv._restart_meshcenter_after_profile_switch()
    assert planned_restart.active() is True
    assert logged[0][0][:2] == ("Radio profile restart", "INFO")
    assert "selected radio profile" in logged[0][0][2]
    assert not any(a[0][1] == "ERROR" for a in logged)


def test_system_restart_action_sigterm_is_info(monkeypatch):
    from api import api_system

    logged = []
    monkeypatch.setattr(api_system, "log_system_event", lambda *a, **k: logged.append((a, k)))
    monkeypatch.setattr(api_system.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(api_system.runtime_env, "is_container", lambda: False)
    app = Flask(__name__)
    api_system.register_system_routes(app)

    started = []

    class _ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self.target, self.args = target, args

        def start(self):
            started.append(1)
            self.target(*self.args)

    with patch("api.api_system.threading.Thread", _ImmediateThread), \
         patch("meshsrv.planned_restart.subprocess.run", side_effect=_sigterm_error()):
        response = app.test_client().post("/api/system/action", json={"action": "restart_meshcenter"})

    assert response.status_code == 202 and started
    assert planned_restart.active() is True
    titles = [(a[0], a[1]) for a, _ in logged]
    assert ("MeshCenter restart", "INFO") in titles, titles
    assert not any(level == "ERROR" for _, level in titles), "no false 'restart failed' ERROR"


def test_refused_system_actions_do_not_open_the_window():
    from api import api_system

    app = Flask(__name__)
    api_system.register_system_routes(app)
    response = app.test_client().post("/api/system/action", json={"action": "bogus"})
    assert response.status_code == 400
    assert planned_restart.active() is False


# ------------------------------------------------------------------ the updater routes
def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


@pytest.fixture
def api(tmp_path):
    update_service.configure(str(tmp_path / "update_check.json"))
    events = []
    app = Flask(__name__)
    register_updates_routes(
        app, lambda: "1.0.0", str(tmp_path), _handle_errors,
        log_event=lambda title, level="INFO", details="", source="system": events.append((title, level, details, source)))
    return app.test_client(), events


PRE_OK = {"ok": True, "target_tag": "v1.0.1", "target_sha": "b" * 40, "detached": False, "branch": "main"}
APPLY_OK = {"ok": True, "blocked": False, "previous_sha": "a" * 40, "previous_version": "1.0.0", "to_version": "1.0.1",
            "target_tag": "v1.0.1", "target_sha": "b" * 40, "output": "", "requirements_changed": False,
            "changed_requirements_files": []}


def test_apply_logs_started_and_opens_the_window(api):
    client, events = api
    with patch("api.api_updates.update_service.git_preflight", return_value=PRE_OK) as pre, \
         patch("api.api_updates.update_service.apply_update", return_value=APPLY_OK), \
         patch("api.api_updates.threading.Thread"):
        response = client.post("/api/updates/apply")
    assert response.status_code == 202
    assert response.get_json()["to_version"] == "1.0.1" and response.get_json()["previous_version"] == "1.0.0"
    assert planned_restart.active() is True
    title, level, details, source = events[0]
    assert (title, level, source) == ("Update started", "ACTION", "update")
    assert "1.0.0 -> 1.0.1" in details and "v1.0.1" in details and "bbbbbbb" in details
    assert pre.call_args.args[1] is None, "no cached latest tag -> no target"


def test_apply_passes_the_cached_latest_tag_as_the_target(api, tmp_path):
    client, _ = api
    update_service._save_cache({**update_service._DEFAULT_CACHE, "latest_tag": "v1.0.1", "latest_version": "1.0.1", "check_ok": True})
    with patch("api.api_updates.update_service.git_preflight", return_value={"ok": False, "reason": "up_to_date"}) as pre:
        client.post("/api/updates/apply")
    assert pre.call_args.args[1] == "v1.0.1"


def test_refused_update_is_logged_and_does_not_restart(api):
    client, events = api
    refusal = {"ok": False, "reason": "unsupported_branch", "branch": "feature/x"}
    with patch("api.api_updates.update_service.git_preflight", return_value=refusal), \
         patch("api.api_updates.threading.Thread") as thread:
        response = client.post("/api/updates/apply")
    assert response.status_code == 409 and response.get_json()["preflight"]["branch"] == "feature/x"
    thread.assert_not_called()
    assert planned_restart.active() is False
    assert events[0][:2] == ("Update failed", "WARNING") and "unsupported_branch" in events[0][2] and "feature/x" in events[0][2]


def test_git_failure_is_an_error_event(api):
    client, events = api
    failed = {"ok": False, "blocked": False, "output": "fatal: boom"}
    with patch("api.api_updates.update_service.git_preflight", return_value=PRE_OK), \
         patch("api.api_updates.update_service.apply_update", return_value=failed), \
         patch("api.api_updates.threading.Thread") as thread:
        response = client.post("/api/updates/apply")
    assert response.status_code == 500
    thread.assert_not_called()
    assert planned_restart.active() is False
    assert events[0][:2] == ("Update failed", "ERROR") and "boom" in events[0][2]


def test_blocked_update_is_a_warning_event_and_never_restarts(api):
    client, events = api
    blocked = {"ok": False, "blocked": True, "changed_requirements_files": ["requirements.txt"], "instructions": "do it by hand"}
    with patch("api.api_updates.update_service.git_preflight", return_value=PRE_OK), \
         patch("api.api_updates.update_service.apply_update", return_value=blocked), \
         patch("api.api_updates.threading.Thread") as thread:
        response = client.post("/api/updates/apply")
    assert response.status_code == 409 and response.get_json()["blocked"] is True
    thread.assert_not_called()
    assert planned_restart.active() is False
    assert events[0][:2] == ("Update failed", "WARNING") and "requirements.txt" in events[0][2]


def test_update_restart_thread_treats_sigterm_as_info(api):
    client, events = api
    with patch("api.api_updates.update_service.git_preflight", return_value=PRE_OK), \
         patch("api.api_updates.update_service.apply_update", return_value=APPLY_OK), \
         patch("api.api_updates.time.sleep"), \
         patch("meshsrv.planned_restart.subprocess.run", side_effect=_sigterm_error()), \
         patch("api.api_updates.threading.Thread") as thread:
        client.post("/api/updates/apply")
        target = thread.call_args.kwargs["target"]
        args = thread.call_args.kwargs["args"]
        target(*args)
    titles = [(e[0], e[1]) for e in events]
    assert ("Update restart", "INFO") in titles
    assert not any(level == "ERROR" for _, level in titles)


ROLLBACK_PRE = {"ok": True, "previous_sha": "a" * 40, "previous_version": "1.0.0", "detached": False, "branch": "main"}
ROLLBACK_OK = {"ok": True, "blocked": False, "to_version": "1.0.0", "previous_sha": "b" * 40, "output": ""}


def test_rollback_route_logs_and_restarts(api):
    client, events = api
    with patch("api.api_updates.update_service.rollback_preflight", return_value=ROLLBACK_PRE), \
         patch("api.api_updates.update_service.rollback", return_value=ROLLBACK_OK), \
         patch("api.api_updates.threading.Thread") as thread:
        response = client.post("/api/updates/rollback")
    assert response.status_code == 202 and response.get_json()["to_version"] == "1.0.0"
    thread.assert_called_once()
    assert planned_restart.active() is True
    assert events[0][:2] == ("Rollback started", "ACTION")
    assert "1.0.0 -> 1.0.0" in events[0][2] and "aaaaaaa" in events[0][2]


def test_rollback_route_refusal_blocked_and_failure(api):
    client, events = api
    with patch("api.api_updates.update_service.rollback_preflight", return_value={"ok": False, "reason": "rollback_unavailable"}), \
         patch("api.api_updates.threading.Thread") as thread:
        response = client.post("/api/updates/rollback")
    assert response.status_code == 409 and response.get_json()["preflight"]["reason"] == "rollback_unavailable"
    assert events[-1][:2] == ("Rollback failed", "WARNING")

    blocked = {"ok": False, "blocked": True, "changed_requirements_files": ["requirements.txt"], "instructions": "by hand"}
    with patch("api.api_updates.update_service.rollback_preflight", return_value=ROLLBACK_PRE), \
         patch("api.api_updates.update_service.rollback", return_value=blocked), \
         patch("api.api_updates.threading.Thread") as thread:
        response = client.post("/api/updates/rollback")
    assert response.status_code == 409 and response.get_json()["blocked"] is True
    assert response.get_json()["instructions"] == "by hand"
    thread.assert_not_called()

    with patch("api.api_updates.update_service.rollback_preflight", return_value=ROLLBACK_PRE), \
         patch("api.api_updates.update_service.rollback", return_value={"ok": False, "output": "fatal"}), \
         patch("api.api_updates.threading.Thread") as thread:
        response = client.post("/api/updates/rollback")
    assert response.status_code == 500
    thread.assert_not_called()
    assert planned_restart.active() is False
    assert events[-1][:2] == ("Rollback failed", "ERROR")


def test_updates_are_refused_in_a_container(api, monkeypatch):
    client, _ = api
    from meshsrv import runtime_env

    monkeypatch.setenv(runtime_env.RUNTIME_ENV if hasattr(runtime_env, "RUNTIME_ENV") else "MESHCENTER_RUNTIME", "container")
    for path, method in (("/api/updates/preflight", "get"), ("/api/updates/apply", "post"), ("/api/updates/rollback", "post")):
        response = getattr(client, method)(path)
        assert response.status_code == 403, path
