"""Docker support (DK1): the runtime/capability layer and the container files.

Two guarantees are pinned here:
  * with MESHCENTER_RUNTIME unset every capability keeps its native value and
    the native restart path (sudo systemctl) is unchanged;
  * with MESHCENTER_RUNTIME=container host-only operations refuse cleanly,
    restart is a SIGTERM to the worker, and /healthz works without a radio
    or a session.
No Docker is needed: image-level facts are asserted against the Dockerfile,
entrypoint, compose file and config template as text/exec.
"""
import json
import re
import runpy
import threading
import time
from pathlib import Path

import pytest
from flask import Flask
from werkzeug.security import generate_password_hash

from meshsrv import runtime_env

REPO = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO / "templates"


@pytest.fixture
def native(monkeypatch):
    for name in (runtime_env.RUNTIME_ENV, runtime_env.TRUST_HOST_CLOCK_ENV, runtime_env.VERSION_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def container(monkeypatch):
    monkeypatch.setenv(runtime_env.RUNTIME_ENV, "container")
    monkeypatch.delenv(runtime_env.TRUST_HOST_CLOCK_ENV, raising=False)


# --------------------------------------------------------------- capabilities

def test_native_defaults_when_variable_absent(native):
    caps = runtime_env.capabilities()
    assert runtime_env.is_container() is False
    assert caps["mode"] == "native"
    assert caps["restart_method"] == "systemctl"
    for flag in ("can_self_restart", "can_reboot_host", "can_manage_wifi", "can_self_update",
                 "can_edit_hardware_config", "has_host_metrics", "hardware_io"):
        assert caps[flag] is True, flag
    assert caps["trust_host_clock"] is False


@pytest.mark.parametrize("value", ["", "Container ", "docker", "1", "true", "/.dockerenv"])
def test_only_exact_container_value_switches_mode(monkeypatch, value):
    monkeypatch.setenv(runtime_env.RUNTIME_ENV, value)
    assert runtime_env.is_container() is (value.strip().lower() == "container")


def test_container_capabilities(container):
    caps = runtime_env.capabilities()
    assert caps["mode"] == "container"
    assert caps["restart_method"] == "signal"
    assert caps["can_self_restart"] is True
    for flag in ("can_reboot_host", "can_manage_wifi", "can_self_update",
                 "can_edit_hardware_config", "has_host_metrics", "hardware_io"):
        assert caps[flag] is False, flag


def test_trust_host_clock_default_off_and_container_only(monkeypatch, container):
    assert runtime_env.trust_host_clock() is False
    monkeypatch.setenv(runtime_env.TRUST_HOST_CLOCK_ENV, "1")
    assert runtime_env.trust_host_clock() is True
    monkeypatch.delenv(runtime_env.RUNTIME_ENV)
    assert runtime_env.trust_host_clock() is False  # ignored natively


def test_request_self_restart_sends_sigterm_to_own_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_env.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    runtime_env.request_self_restart()
    assert calls == [(runtime_env.os.getpid(), runtime_env.signal.SIGTERM)]


# ------------------------------------------------------------------ restart

def _csrf_client(server_module):
    client = server_module.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "test-csrf-token"
    return client


def _post(client, path, body):
    return client.post(path, json=body, headers={"X-CSRF-Token": "test-csrf-token"})


def _wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _no_sleep(monkeypatch, *modules):
    for mod in modules:
        monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)


def test_native_profile_switch_restart_still_uses_systemctl(server_module, native, monkeypatch):
    ran, restarted = [], []
    monkeypatch.setattr(server_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(server_module.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(runtime_env, "request_self_restart", lambda: restarted.append(1))
    server_module._restart_meshcenter_after_profile_switch()
    assert ran == [["sudo", "-n", "/usr/bin/systemctl", "restart", "meshcenter.service"]]
    assert restarted == []


def test_container_profile_switch_restart_signals_worker(server_module, container, monkeypatch):
    ran, restarted = [], []
    monkeypatch.setattr(server_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(server_module.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(runtime_env, "request_self_restart", lambda: restarted.append(1))
    server_module._restart_meshcenter_after_profile_switch()
    assert ran == []
    assert restarted == [1]


def test_native_ui_restart_action_uses_systemctl(server_module, native, monkeypatch):
    import api.api_system as api_system
    ran = []
    _no_sleep(monkeypatch, api_system)
    monkeypatch.setattr(api_system.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    client = _csrf_client(server_module)
    response = _post(client, "/api/system/action", {"action": "restart_meshcenter"})
    assert response.status_code == 202
    assert _wait_for(lambda: ran)
    assert ran[0] == ["sudo", "-n", "/usr/bin/systemctl", "restart", "meshcenter.service"]


def test_container_ui_restart_action_signals_worker(server_module, container, monkeypatch):
    import api.api_system as api_system
    ran, restarted = [], []
    _no_sleep(monkeypatch, api_system)
    monkeypatch.setattr(api_system.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(runtime_env, "request_self_restart", lambda: restarted.append(1))
    client = _csrf_client(server_module)
    assert _post(client, "/api/system/action", {"action": "restart_meshcenter"}).status_code == 202
    assert _wait_for(lambda: restarted)
    assert ran == []


# -------------------------------------------------------------- refusals

@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/system/action", {"action": "reboot"}),
    ("post", "/api/system/action", {"action": "shutdown"}),
    ("get", "/api/system/wifi/scan", None),
    ("post", "/api/system/wifi/connect", {"ssid": "x", "password": "y"}),
    ("post", "/api/system/wifi/forget", {"ssid": "x"}),
    ("get", "/api/system/network", None),
    ("get", "/api/system/top-processes", None),
    ("post", "/api/updates/apply", {}),
    ("get", "/api/updates/preflight", None),
    ("post", "/api/hardware/i2c/enable", {}),
    ("post", "/api/hardware/rtc/configure", {}),
])
def test_host_operations_refused_in_container(server_module, container, monkeypatch, method, path, body):
    import subprocess

    def boom(*_a, **_k):
        pytest.fail("a host command was executed")

    for name in ("run", "Popen", "check_output", "check_call"):
        monkeypatch.setattr(subprocess, name, boom)
    client = _csrf_client(server_module)
    kwargs = {"json": body} if body is not None else {}
    response = getattr(client, method)(path, headers={"X-CSRF-Token": "test-csrf-token"}, **kwargs)
    assert response.status_code == 403
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error_code"] == "not_available_in_container"


def test_saved_wifi_list_never_queried_in_container(server_module, container, monkeypatch):
    from meshsrv import network_config

    def boom(*_a, **_k):
        pytest.fail("network_config was called in container mode")

    monkeypatch.setattr(network_config, "list_wifi_connections", boom)
    monkeypatch.setattr(network_config, "scan", boom)
    client = _csrf_client(server_module)
    assert client.get("/api/system/wifi/scan").status_code == 403


def test_system_info_in_container_hides_container_identity(server_module, container):
    payload = server_module.app.test_client().get("/api/system/info").get_json()
    for key in ("hostname", "os", "model", "cpu_temp"):
        assert payload[key] is None, key
    assert payload["runtime"]["mode"] == "container"


def test_system_info_native_reports_runtime_native(server_module, native):
    payload = server_module.app.test_client().get("/api/system/info").get_json()
    assert payload["runtime"]["mode"] == "native"


# ------------------------------------------------------------------ healthz

def test_healthz_reachable_without_radio_and_leaks_nothing(server_module):
    response = server_module.app.test_client().get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert "Set-Cookie" not in response.headers


def test_healthz_is_get_only(server_module):
    assert server_module.app.test_client().post("/healthz").status_code == 405


def test_healthz_is_exempt_from_auth_even_when_protected():
    import api.api_auth as api_auth
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
    app.secret_key = "k"
    state = {"enabled": True, "password_hash": generate_password_hash("realpassword123")}
    api_auth.register_auth_routes(app, threading.RLock(), state, "unused.json", lambda f: f)

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.route("/api/private")
    def private():
        return {"secret": 1}

    client = app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/private").status_code == 401


def test_index_embeds_runtime_capabilities_meta(server_module, container):
    response = server_module.app.test_client().get("/")
    if response.status_code != 200:
        pytest.skip("index not directly renderable in this fixture (auth redirect)")
    match = re.search(r'name="runtime-capabilities" content="([^"]*)"', response.get_data(as_text=True))
    assert match
    import html
    assert json.loads(html.unescape(match.group(1)))["mode"] == "container"


# ------------------------------------------------------------------ version

def test_version_fallback_uses_baked_version_after_git(server_module, tmp_path, monkeypatch):
    monkeypatch.setenv(runtime_env.VERSION_ENV, "v1.10.0")
    assert server_module.resolve_app_version(str(tmp_path)) == "1.10.0"


def test_version_without_env_still_dev(server_module, tmp_path, native):
    assert server_module.resolve_app_version(str(tmp_path)) == "dev"


def test_version_git_wins_over_baked_value(server_module, monkeypatch):
    monkeypatch.setenv(runtime_env.VERSION_ENV, "9.9.9")
    version = server_module.resolve_app_version(str(REPO))
    if (REPO / ".git").exists():
        assert version != "9.9.9"


# --------------------------------------------------------------- schedules

def test_schedule_file_follows_data_dir_with_legacy_read_fallback(tmp_path, monkeypatch):
    from meshsrv import schedule_engine
    legacy = tmp_path / "legacy" / "schedules.json"
    legacy.parent.mkdir()
    legacy.write_text('[{"id": "abc", "name": "old"}]')
    monkeypatch.setattr(schedule_engine, "LEGACY_SCHEDULES_FILE", legacy)
    monkeypatch.setattr(schedule_engine, "SCHEDULES_FILE", schedule_engine.SCHEDULES_FILE)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    schedule_engine.configure_storage(data_dir)
    assert schedule_engine.SCHEDULES_FILE == data_dir / "schedules.json"

    rules = schedule_engine._load()
    assert [r["id"] for r in rules] == ["abc"]          # read from legacy
    assert not (data_dir / "schedules.json").exists()   # read-only fallback
    schedule_engine._save(rules)
    assert (data_dir / "schedules.json").exists()       # writes go to DATA_DIR

    legacy.write_text("[]")
    assert [r["id"] for r in schedule_engine._load()] == ["abc"]  # new file wins


def test_schedule_file_created_in_data_dir_when_nothing_exists(tmp_path, monkeypatch):
    from meshsrv import schedule_engine
    monkeypatch.setattr(schedule_engine, "LEGACY_SCHEDULES_FILE", tmp_path / "nope" / "schedules.json")
    monkeypatch.setattr(schedule_engine, "SCHEDULES_FILE", schedule_engine.SCHEDULES_FILE)
    schedule_engine.configure_storage(tmp_path / "d")
    assert schedule_engine._load() == []
    assert (tmp_path / "d" / "schedules.json").exists()


# -------------------------------------------------------------------- time

def test_ntp_probe_trusts_host_clock_only_in_container_with_opt_in(monkeypatch):
    from meshsrv import time_service

    def no_timedatectl(*_a, **_k):
        raise FileNotFoundError("timedatectl")

    monkeypatch.setattr(time_service.subprocess, "check_output", no_timedatectl)

    monkeypatch.delenv(runtime_env.RUNTIME_ENV, raising=False)
    monkeypatch.setenv(runtime_env.TRUST_HOST_CLOCK_ENV, "1")
    assert time_service._probe_ntp_status()["synchronized"] is False       # native: ignored

    monkeypatch.setenv(runtime_env.RUNTIME_ENV, "container")
    assert time_service._probe_ntp_status() == {"synchronized": True, "source": "host-clock"}

    monkeypatch.delenv(runtime_env.TRUST_HOST_CLOCK_ENV)
    assert time_service._probe_ntp_status()["synchronized"] is False       # default off


def test_timezone_from_env_only_in_container(monkeypatch):
    from meshsrv import time_service
    monkeypatch.setattr(time_service, "_timezone_from_timedatectl", lambda: None)
    monkeypatch.setattr(time_service, "_timezone_from_etc_timezone", lambda: "Europe/Paris")
    monkeypatch.setenv("TZ", "Europe/Berlin")

    monkeypatch.delenv(runtime_env.RUNTIME_ENV, raising=False)
    assert time_service.detect_system_timezone() == "Europe/Paris"
    monkeypatch.setenv(runtime_env.RUNTIME_ENV, "container")
    assert time_service.detect_system_timezone() == "Europe/Berlin"


# ----------------------------------------------------- container files (static)

def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def test_config_template_has_everything_server_requires_and_auth_on(monkeypatch, tmp_path):
    monkeypatch.setenv("MESHCENTER_DATA_DIR", str(tmp_path))
    ns = runpy.run_path(str(REPO / "deploy/docker/config.container.py"))
    for name in ("APP_HOST", "APP_PORT", "MESHTASTIC_CMD", "LOCAL_NODE_ID", "LOCAL_NODE_NAME", "DATA_DIR",
                 "HISTORY_FILE", "NODES_FILE", "SENSORS_FILE", "CHATS_FILE", "MAX_HISTORY_MESSAGES",
                 "CHANNEL_CHAT_ID", "CHANNEL_CHAT_NAME", "KNOWN_NODES", "KNOWN_NODE_INFO"):
        assert name in ns, name
    from storage.profile_manager import ProfileManager
    # The placeholder must be a valid profile node ID, or Core exits at import
    # (found live on the first container run).
    assert ProfileManager.profile_id_from_node_id(ns["LOCAL_NODE_ID"]) == "00000000"
    assert ns["AUTH_ENABLED"] is True
    assert ns["AUTH_PASSWORD_HASH"] == ""
    assert ns["APP_HOST"] == "0.0.0.0"
    assert ns["DATA_DIR"] == str(tmp_path)
    assert ns["EPAPER_ENABLED"] is False
    assert re.search(r"^AUTH_ENABLED\s*=\s*True\b", _read("deploy/docker/config.container.py"), re.M)


def test_dockerfile_isolation_and_runtime_settings():
    text = _read("Dockerfile")
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "FROM python:3.11-slim" in text
    assert "MESHCENTER_RUNTIME=container" in text
    assert "PYTHONPATH" not in code
    assert re.search(r'echo "/config" > .*meshcenter-config\.pth', text)
    assert "/opt/core-venv/bin/pip install -r /tmp/requirements.txt" in text
    assert "/app/adapters/meshtastic/venv/bin/pip install -r /tmp/adapter-requirements.txt" in text
    # the .pth is written through the Core venv only; the adapter interpreter is checked for absence
    assert code.count("meshcenter-config.pth") == 1
    assert "import sys; sys.exit(1 if '/config' in sys.path else 0)" in text
    assert '! /opt/core-venv/bin/python -c "import meshtastic"' in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text and "HOME=/data/.home" in text
    assert "tini" in text and "HEALTHCHECK" in text and "/healthz" in text
    assert "ENTRYPOINT" in text and "USER ${PUID}:${PGID}" in text
    for path in ("/app/data", "/app/deploy/docker/data", "/app/deploy/docker/config"):
        assert f"test ! -e {path}" in text, path


def test_entrypoint_cannot_override_workers_or_chown():
    text = _read("deploy/docker/entrypoint.sh")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "unset GUNICORN_CMD_ARGS" in code
    assert '"$@"' not in code and "$*" not in code
    assert "exec gunicorn -c /app/gunicorn.conf.py wsgi:app" in code
    assert "chown" not in code
    assert '[ ! -e "$CONFIG_DIR/config.py" ]' in code      # never overwrites
    assert _read("gunicorn.conf.py").count("workers = 1") == 1


def test_compose_is_locked_down():
    text = _read("deploy/docker/compose.yaml")
    assert "cap_drop:" in text and "- ALL" in text
    assert "no-new-privileges:true" in text
    for forbidden in ("privileged", "docker.sock", "network_mode", "devices:", "pid: host"):
        assert forbidden not in text, forbidden
    assert '"5000:5000"' in text
    assert "restart: unless-stopped" in text
    assert "./config:/config" in text and "./data:/data" in text
    assert "container_name: meshcenter" in text


def test_dockerignore_excludes_secrets_and_state_but_keeps_runtime():
    lines = {ln.strip() for ln in _read(".dockerignore").splitlines() if ln.strip() and not ln.startswith("#")}
    for needed in ("deploy/docker/data", "deploy/docker/config", "deploy/docker/.env"):
        assert needed in lines, needed
    gitignore = _read(".gitignore").splitlines()
    for needed in ("deploy/docker/data/", "deploy/docker/config/", "deploy/docker/.env"):
        assert needed in gitignore, needed
    for needed in (".git", "data", "config.py", "weather_secrets.py", ".env", "relay-server", "*.db", "**/venv"):
        assert needed in lines, needed
    for runtime_dir in ("static", "templates", "adapters", "meshsrv", "api", "storage", "deploy"):
        assert runtime_dir not in lines, runtime_dir
    assert (REPO / "deploy/docker/entrypoint.sh").exists()
    assert ".env" in _read(".gitignore").splitlines()


def test_env_example_has_no_secrets_and_documents_clock_trust():
    text = _read("deploy/docker/.env.example")
    assert "MESHCENTER_TRUST_HOST_CLOCK=1" in text
    assert not re.search(r"(?i)(password|secret|token|api_key)\s*=", text)


def test_adapter_subprocess_env_does_not_gain_config_dir():
    text = _read("meshsrv/adapter_ipc_client.py")
    assert '"PYTHONPATH": str(self._project_dir)' in text


# ---- ST1: a SIGTERM-killed restart helper is the expected outcome, not an error ----

def _logged(monkeypatch, server_module):
    events = []
    monkeypatch.setattr(server_module, "log_system_event",
                        lambda title, level="INFO", details="", source="system": events.append((title, level)))
    return events


def test_profile_restart_helper_dying_of_sigterm_is_not_logged_as_an_error(server_module, native, monkeypatch):
    import signal
    import subprocess

    events = _logged(monkeypatch, server_module)
    monkeypatch.setattr(server_module.time, "sleep", lambda *_a, **_k: None)

    def killed(cmd, **kw):
        raise subprocess.CalledProcessError(-signal.SIGTERM, cmd)

    monkeypatch.setattr(server_module.subprocess, "run", killed)
    server_module._restart_meshcenter_after_profile_switch()
    assert events == [("Radio profile restart", "INFO")]


def test_profile_restart_helper_exit_143_is_the_same_expected_outcome(server_module, native, monkeypatch):
    import subprocess

    events = _logged(monkeypatch, server_module)
    monkeypatch.setattr(server_module.time, "sleep", lambda *_a, **_k: None)

    def killed(cmd, **kw):
        raise subprocess.CalledProcessError(143, cmd)

    monkeypatch.setattr(server_module.subprocess, "run", killed)
    server_module._restart_meshcenter_after_profile_switch()
    assert [level for _, level in events] == ["INFO"]


def test_profile_restart_real_failures_still_log_an_error(server_module, native, monkeypatch):
    import subprocess

    monkeypatch.setattr(server_module.time, "sleep", lambda *_a, **_k: None)
    for failure in (
        subprocess.CalledProcessError(1, ["sudo"]),               # sudo refused / systemctl failed
        subprocess.TimeoutExpired(["sudo"], 20),                  # restart never answered
        FileNotFoundError("sudo"),
    ):
        events = _logged(monkeypatch, server_module)

        def boom(cmd, _f=failure, **kw):
            raise _f

        monkeypatch.setattr(server_module.subprocess, "run", boom)
        server_module._restart_meshcenter_after_profile_switch()
        assert [(t, l) for t, l in events] == [("Radio profile restart failed", "ERROR")], failure


def test_container_profile_restart_logs_nothing_and_never_calls_the_helper(server_module, container, monkeypatch):
    events = _logged(monkeypatch, server_module)
    restarted = []
    monkeypatch.setattr(server_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(server_module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no systemctl in a container")))
    monkeypatch.setattr(runtime_env, "request_self_restart", lambda: restarted.append(1))
    server_module._restart_meshcenter_after_profile_switch()
    assert restarted == [1] and events == []
