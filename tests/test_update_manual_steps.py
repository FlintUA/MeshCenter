"""tests/test_update_manual_steps.py - v1.12.2: host-file changes do not block an update, they ask for a manual step.

An update never installs the systemd unit, the sudoers templates, the udev rule or the privileged
helper scripts (INSTALL.md, "Easy to forget"). Before v1.12.2 nothing told the user when a release
changed one of them. Now such a change is NOT a block (only requirements*.txt blocks): the update is
applied, the card shows a "manual step needed" notice with the exact command for each file, and the
"Update started" / "Update finished" events mention it. Real git against a bare origin, like
test_update_service_tags.py.
"""
from __future__ import annotations

import shlex
import subprocess
from functools import wraps
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from api.api_updates import register_updates_routes
from meshsrv import planned_restart, update_service

REPO = Path(__file__).resolve().parent.parent


def _run(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"{args} failed: {result.stderr}"
    return result


def _identity(path):
    _run(["git", "config", "user.email", "test@example.invalid"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)


@pytest.fixture
def env(tmp_path):
    update_service.configure(str(tmp_path / "update_check.json"))
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run(["git", "init", "--bare"], cwd=origin)
    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], cwd=work)
    _identity(work)
    for rel, content in {
        "requirements.txt": "Flask>=3.0.0\n",
        "server.py": "# 1.0.0\n",
        "deploy/meshcenter.service": "[Service]\nUser=__MESH_USER__\n",
        "deploy/meshcenter.sudoers": "__MESH_USER__ ALL=(root) NOPASSWD: /usr/bin/systemctl restart meshcenter.service\n",
        "deploy/meshcenter-wifi.sudoers": "__MESH_USER__ ALL=(root) NOPASSWD: /usr/local/sbin/meshcenter-network-helper\n",
        "deploy/99-meshcenter-rtc.rules": "# rtc\n",
        "deploy/docker/compose.yaml": "name: meshcenter\n",
        "scripts/meshcenter-network-helper": "#!/bin/sh\necho 1\n",
        "scripts/meshcenter-hw-config": "#!/bin/sh\necho 1\n",
        "install.sh": "echo install 1\n",
    }.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _run(["git", "add", "."], cwd=work)
    _run(["git", "commit", "-m", "1.0.0"], cwd=work)
    _run(["git", "remote", "add", "origin", str(origin)], cwd=work)
    _run(["git", "push", "-u", "origin", "main"], cwd=work)
    _run(["git", "tag", "v1.0.0"], cwd=work)
    _run(["git", "push", "origin", "v1.0.0"], cwd=work)
    dev = tmp_path / "dev"
    _run(["git", "clone", "--branch", "main", str(origin), str(dev)], cwd=tmp_path)
    _identity(dev)

    class Env:
        pass

    e = Env()
    e.work, e.dev = work, dev

    def publish(tag, files):
        for rel, content in files.items():
            target = dev / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _run(["git", "add", "."], cwd=dev)
        _run(["git", "commit", "-m", tag], cwd=dev)
        _run(["git", "push", "origin", "main"], cwd=dev)
        _run(["git", "tag", tag], cwd=dev)
        _run(["git", "push", "origin", tag], cwd=dev)

    e.publish = publish
    e.head = lambda: _run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
    return e


def _apply(env, tag="v1.0.1"):
    pre = update_service.git_preflight(str(env.work), tag)
    assert pre["ok"] is True, pre
    return update_service.apply_update(str(env.work), pre, "1.0.0")


# ------------------------------------------------------------------ not a block
def test_host_file_changes_are_applied_not_blocked_and_listed_in_a_stable_order(env):
    env.publish("v1.0.1", {
        "scripts/meshcenter-network-helper": "#!/bin/sh\necho 2\n",
        "deploy/meshcenter.sudoers": "__MESH_USER__ ALL=(root) NOPASSWD: /usr/bin/systemctl reboot\n",
        "deploy/meshcenter.service": "[Service]\nUser=__MESH_USER__\nRestart=always\n",
        "deploy/99-meshcenter-rtc.rules": "# rtc 2\n",
    })
    start = env.head()
    result = _apply(env)
    assert result["ok"] is True and result["blocked"] is False
    assert env.head() != start, "the update itself was applied"
    assert [step["file"] for step in result["manual_steps"]] == [
        "deploy/meshcenter.service",
        "deploy/meshcenter.sudoers",
        "deploy/99-meshcenter-rtc.rules",
        "scripts/meshcenter-network-helper",
    ], "stable order, only the files that changed"


def test_exact_commands_match_what_install_sh_does(env):
    env.publish("v1.0.1", {
        "deploy/meshcenter.service": "[Service]\nUser=__MESH_USER__\nRestart=always\n",
        "deploy/meshcenter.sudoers": "x __MESH_USER__\n",
        "deploy/meshcenter-wifi.sudoers": "y __MESH_USER__\n",
        "deploy/99-meshcenter-rtc.rules": "# rtc 2\n",
        "scripts/meshcenter-network-helper": "#!/bin/sh\necho 2\n",
        "scripts/meshcenter-hw-config": "#!/bin/sh\necho 2\n",
    })
    commands = {step["file"]: step["command"] for step in _apply(env)["manual_steps"]}
    def q(rel):
        return shlex.quote(f"{env.work}/{rel}")

    install_sh = (REPO / "install.sh").read_text(encoding="utf-8")

    unit = commands["deploy/meshcenter.service"]
    assert q("deploy/meshcenter.service") in unit
    assert "__MESH_USER__|$USER|g" in unit and "__MESH_HOME__|$HOME|g" in unit
    assert "| sudo tee /etc/systemd/system/meshcenter.service" in unit
    assert unit.endswith("sudo systemctl daemon-reload && sudo systemctl restart meshcenter.service")

    for name, rel in (("meshcenter", "deploy/meshcenter.sudoers"), ("meshcenter-wifi", "deploy/meshcenter-wifi.sudoers")):
        cmd = commands[rel]
        assert q(rel) in cmd and "__MESH_USER__|$USER|g" in cmd
        # validated BEFORE it replaces the live file
        assert cmd.index(f"visudo -cf /tmp/{name}.sudoers") < cmd.index(f"/etc/sudoers.d/{name}")
        assert f"sudo install -o root -g root -m 0440 /tmp/{name}.sudoers /etc/sudoers.d/{name}" in cmd

    assert commands["deploy/99-meshcenter-rtc.rules"] == (
        f"sudo install -o root -g root -m 0644 {q('deploy/99-meshcenter-rtc.rules')} /etc/udev/rules.d/99-meshcenter-rtc.rules "
        "&& sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=rtc"
    )
    for helper in ("meshcenter-network-helper", "meshcenter-hw-config"):
        assert commands[f"scripts/{helper}"] == f"sudo install -o root -g root -m 0755 {q('scripts/' + helper)} /usr/local/sbin/{helper}"
        assert f"/usr/local/sbin/{helper}" in install_sh

    # the destinations really are the ones install.sh uses
    for fragment in ("/etc/systemd/system/", "/etc/sudoers.d/", "/etc/udev/rules.d/99-meshcenter-rtc.rules",
                     "udevadm control --reload-rules", "systemctl daemon-reload", "__MESH_USER__|${MESH_USER}|g"):
        assert fragment in install_sh, fragment


def test_docker_and_installer_changes_need_no_manual_step(env):
    env.publish("v1.0.1", {
        "deploy/docker/compose.yaml": "name: meshcenter\nservices: {}\n",
        "install.sh": "echo install 2\n",
        "server.py": "# 1.0.1\n",
    })
    result = _apply(env)
    assert result["ok"] is True and result["manual_steps"] == []


def test_a_dependency_change_still_blocks_and_records_no_manual_steps(env):
    env.publish("v1.0.1", {
        "requirements.txt": "Flask>=3.1.0\n",
        "deploy/meshcenter.service": "[Service]\nUser=__MESH_USER__\nRestart=always\n",
    })
    start = env.head()
    result = _apply(env)
    assert result["blocked"] is True and result["changed_requirements_files"] == ["requirements.txt"]
    assert env.head() == start, "blocked updates move nothing"
    assert update_service.get_status("1.0.0")["pending_update"] is None


def test_paths_with_spaces_are_quoted():
    steps = update_service.manual_step_commands("/home/a b/mesh center", ["scripts/meshcenter-hw-config"])
    assert "'/home/a b/mesh center/scripts/meshcenter-hw-config'" in steps[0]["command"] and "/usr/local/sbin/meshcenter-hw-config" in steps[0]["command"]


def test_unknown_files_are_ignored():
    assert update_service.manual_step_commands("/x", ["server.py", "deploy/docker/compose.yaml"]) == []


# ------------------------------------------------------------------ across the restart
def test_the_steps_survive_the_restart_and_reach_the_finished_event(env):
    env.publish("v1.0.1", {"deploy/meshcenter.service": "[Service]\nUser=__MESH_USER__\nRestart=always\n"})
    _apply(env)
    pending = update_service.get_status("1.0.1")["pending_update"]
    assert [s["file"] for s in pending["manual_steps"]] == ["deploy/meshcenter.service"]

    events = []
    last = update_service.reconcile_pending("1.0.1", lambda *a, **k: events.append(a))
    assert last["ok"] is True and [s["file"] for s in last["manual_steps"]] == ["deploy/meshcenter.service"]
    title, level, details = events[0]
    assert title == "Update finished" and "manual step needed: deploy/meshcenter.service" in details
    status = update_service.get_status("1.0.1")
    assert status["last_update"]["manual_steps"][0]["command"].startswith("sed -e")


def test_finished_event_without_steps_is_unchanged(env):
    env.publish("v1.0.1", {"server.py": "# 1.0.1\n"})
    _apply(env)
    events = []
    last = update_service.reconcile_pending("1.0.1", lambda *a, **k: events.append(a))
    assert last["manual_steps"] == []
    assert events[0][2] == "MeshCenter 1.0.0 -> 1.0.1"


def test_rollback_reports_the_reverse_host_file_changes(env):
    env.publish("v1.0.1", {"deploy/meshcenter.sudoers": "changed __MESH_USER__\n"})
    _apply(env)
    update_service.reconcile_pending("1.0.1", lambda *a, **k: None)
    pre = update_service.rollback_preflight(str(env.work), "1.0.1")
    assert pre["ok"] is True
    result = update_service.rollback(str(env.work), pre, "1.0.1")
    assert result["ok"] is True
    assert [s["file"] for s in result["manual_steps"]] == ["deploy/meshcenter.sudoers"]


# ------------------------------------------------------------------ the API
def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


def test_apply_response_and_started_event_carry_the_manual_step(tmp_path):
    update_service.configure(str(tmp_path / "update_check.json"))
    events = []
    app = Flask(__name__)
    register_updates_routes(app, lambda: "1.0.0", str(tmp_path), _handle_errors,
                            log_event=lambda title, level="INFO", details="", source="system": events.append((title, level, details, source)))
    pre = {"ok": True, "target_tag": "v1.0.1", "target_sha": "b" * 40, "detached": False, "branch": "main"}
    applied = {"ok": True, "blocked": False, "previous_sha": "a" * 40, "previous_version": "1.0.0", "to_version": "1.0.1",
               "target_tag": "v1.0.1", "target_sha": "b" * 40, "output": "",
               "manual_steps": [{"file": "deploy/meshcenter.service", "command": "sed ..."},
                                {"file": "scripts/meshcenter-hw-config", "command": "sudo install ..."}]}
    planned_restart.clear()
    with patch("api.api_updates.update_service.git_preflight", return_value=pre), \
         patch("api.api_updates.update_service.apply_update", return_value=applied), \
         patch("api.api_updates.threading.Thread"):
        response = app.test_client().post("/api/updates/apply")
    planned_restart.clear()
    assert response.status_code == 202
    assert [s["file"] for s in response.get_json()["manual_steps"]] == ["deploy/meshcenter.service", "scripts/meshcenter-hw-config"]
    title, level, details, _ = events[0]
    assert (title, level) == ("Update started", "ACTION")
    assert "manual step needed after the restart: deploy/meshcenter.service, scripts/meshcenter-hw-config" in details
