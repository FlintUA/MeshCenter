"""tests/test_update_service_tags.py - UPD1: the updater moves to the latest release's TAG.

Before UPD1 it merged `origin/main` (whatever main happened to be) and needed an upstream, so
a node on a detached `vX.Y.Z` checkout could not update at all. These tests run real `git`
against a bare origin plus working clones (no mocking of git itself): the update lands on the
TAG's commit even when main has moved on, works from branch `main` and from a detached HEAD,
refuses a dirty tree / another branch / diverged history, never offers or performs a downgrade,
rolls back exactly, and records / resolves the pending update across the restart.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meshsrv import update_service


def _run(args, cwd):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"{args} failed: {result.stderr}"
    return result


def _git(cwd, *args):
    return _run(["git", *args], cwd=cwd).stdout.strip()


def _identity(path):
    _run(["git", "config", "user.email", "test@example.invalid"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)


@pytest.fixture
def env(tmp_path):
    """origin (bare) + `work` (the installation) at tagged v1.0.0 on main + `dev` (a second
    clone used to publish releases / extra commits)."""
    update_service.configure(str(tmp_path / "update_check.json"))
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run(["git", "init", "--bare"], cwd=origin)

    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], cwd=work)
    _identity(work)
    (work / "requirements.txt").write_text("Flask>=3.0.0\n", encoding="utf-8")
    (work / "server.py").write_text("# 1.0.0\n", encoding="utf-8")
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
    e.work, e.dev, e.origin, e.tmp = work, dev, origin, tmp_path

    def publish(tag, files=None, *, tag_it=True):
        """Commit on `dev`, push main, tag and push the tag. Returns the commit sha."""
        for name, content in (files or {"server.py": f"# {tag}\n"}).items():
            target = dev / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _run(["git", "add", "."], cwd=dev)
        _run(["git", "commit", "-m", tag], cwd=dev)
        _run(["git", "push", "origin", "main"], cwd=dev)
        sha = _git(dev, "rev-parse", "HEAD")
        if tag_it:
            _run(["git", "tag", tag], cwd=dev)
            _run(["git", "push", "origin", tag], cwd=dev)
        return sha

    e.publish = publish
    e.head = lambda: _git(work, "rev-parse", "HEAD")
    e.branch = lambda: _git(work, "branch", "--show-current")
    e.clean = lambda: _git(work, "status", "--porcelain") == ""
    return e


def _update(env, tag, current="1.0.0"):
    pre = update_service.git_preflight(str(env.work), tag)
    assert pre["ok"] is True, pre
    return pre, update_service.apply_update(str(env.work), pre, current)


# ------------------------------------------------------------------ the target is the TAG
def test_update_lands_on_the_tag_not_on_origin_main(env):
    tag_sha = env.publish("v1.0.1")
    env.publish("later", {"server.py": "# an unreleased commit after the tag\n"}, tag_it=False)  # main moved on
    pre, result = _update(env, "v1.0.1")
    assert result["ok"] and result["to_version"] == "1.0.1"
    assert env.head() == tag_sha, "must be the tag's commit, not origin/main"
    assert env.branch() == "main", "stays on the branch it was on"
    assert env.clean()


def test_update_from_a_detached_head_works_and_stays_detached(env):
    _run(["git", "checkout", "--quiet", "--detach", "v1.0.0"], cwd=env.work)
    tag_sha = env.publish("v1.0.1")
    pre = update_service.git_preflight(str(env.work), "v1.0.1")
    assert pre["ok"] is True and pre["detached"] is True and pre["branch"] is None
    result = update_service.apply_update(str(env.work), pre, "1.0.0")
    assert result["ok"] is True
    assert env.head() == tag_sha
    assert env.branch() == "", "still detached - no branch was created or moved"


def test_apply_records_what_a_rollback_needs(env):
    start = env.head()
    _update_sha = env.publish("v1.0.1")
    _update(env, "v1.0.1")
    cache = update_service.get_status("1.0.1")
    assert cache["previous_version_sha"] == start
    assert cache["previous_version"] == "1.0.0"
    assert cache["previous_ref"] == "main"
    assert cache["pending_update"]["to"] == "1.0.1" and cache["pending_update"]["kind"] == "update"
    assert cache["last_update_ok"] is True


# ------------------------------------------------------------------ refusals
def test_refuses_a_dirty_tree(env):
    env.publish("v1.0.1")
    (env.work / "server.py").write_text("# local edit\n", encoding="utf-8")
    before = env.head()
    pre = update_service.git_preflight(str(env.work), "v1.0.1")
    assert pre["ok"] is False and pre["reason"] == "dirty_tree" and pre["dirty_files"] == ["server.py"]
    assert env.head() == before


def test_refuses_any_branch_other_than_main(env):
    env.publish("v1.0.1")
    _run(["git", "checkout", "-q", "-b", "feature/x"], cwd=env.work)
    pre = update_service.git_preflight(str(env.work), "v1.0.1")
    assert pre["ok"] is False and pre["reason"] == "unsupported_branch" and pre["branch"] == "feature/x"


def test_refuses_diverged_history(env):
    env.publish("v1.0.1")
    (env.work / "local.txt").write_text("a local commit\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=env.work)
    _run(["git", "commit", "-m", "local"], cwd=env.work)
    pre = update_service.git_preflight(str(env.work), "v1.0.1")
    assert pre["ok"] is False and pre["reason"] == "diverged"


def test_never_downgrades_a_checkout_that_is_ahead_of_the_release(env):
    env.publish("v1.0.1")
    env.publish("v1.0.2")
    _run(["git", "fetch", "--tags", "origin"], cwd=env.work)
    _run(["git", "checkout", "-q", "--detach", "v1.0.2"], cwd=env.work)
    before = env.head()
    # the cached "latest release" is older than what is running (a withdrawn release, ...)
    pre = update_service.git_preflight(str(env.work), "v1.0.1")
    assert pre["ok"] is False and pre["reason"] == "ahead"
    assert env.head() == before


def test_up_to_date_when_head_is_the_tag(env):
    pre = update_service.git_preflight(str(env.work), "v1.0.0")
    assert pre["ok"] is False and pre["reason"] == "up_to_date"


def test_tag_that_cannot_be_fetched_is_refused(env):
    pre = update_service.git_preflight(str(env.work), "v9.9.9")
    assert pre["ok"] is False and pre["reason"] == "tag_missing"


def test_unreachable_origin_is_fetch_failed(env):
    env.publish("v1.0.1")
    _run(["git", "remote", "set-url", "origin", str(env.tmp / "nowhere.git")], cwd=env.work)
    pre = update_service.git_preflight(str(env.work), "v1.0.1")
    assert pre["ok"] is False and pre["reason"] == "fetch_failed"


@pytest.mark.parametrize("bad", [None, "", "main", "v1.0", "v1.0.1-rc1", "1.0.1"])
def test_no_valid_target_is_refused_before_touching_git(env, bad):
    pre = update_service.git_preflight(str(env.work), bad)
    assert pre["ok"] is False and pre["reason"] == "no_target"


def test_a_local_tag_that_disagrees_with_origin_is_not_overwritten(env):
    env.publish("v1.0.1")
    (env.work / "x.txt").write_text("x\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=env.work)
    _run(["git", "commit", "-m", "x"], cwd=env.work)
    _run(["git", "tag", "v1.0.1"], cwd=env.work)  # same name, other commit
    _run(["git", "reset", "--hard", "-q", "v1.0.0"], cwd=env.work)
    local = _git(env.work, "rev-parse", "v1.0.1")
    pre = update_service.git_preflight(str(env.work), "v1.0.1")
    assert pre["ok"] is False and pre["reason"] == "fetch_failed"
    assert _git(env.work, "rev-parse", "v1.0.1") == local


# ------------------------------------------------------------------ versions
@pytest.mark.parametrize("current,latest,expected", [
    ("1.12.0", "1.12.1", True),
    ("1.12.1", "1.12.1", False),
    ("1.12.2", "1.12.1", False),   # newer than the "latest": never a downgrade offer
    ("1.9.0", "1.12.0", True),     # numeric, not string order
    ("1.12.0", "1.9.0", False),
    ("2.0.0", "1.99.99", False),
    ("dev", "1.12.1", False),
    ("dk2", "1.12.1", False),
    ("1.12.0-3-gabc1234", "1.12.1", False),
    ("", "1.12.1", False),
])
def test_update_available_is_a_numeric_comparison(current, latest, expected):
    cache = {"check_ok": True, "latest_version": latest}
    assert update_service.update_available(current, cache) is expected


def test_no_update_without_a_successful_check():
    assert update_service.update_available("1.0.0", {"check_ok": False, "latest_version": "9.9.9"}) is False


@pytest.mark.parametrize("payload,fragment", [
    ({"tag_name": "v1.0.1", "draft": True}, "draft"),
    ({"tag_name": "v1.0.1", "prerelease": True}, "pre-release"),
    ({"tag_name": "nightly"}, "version tag"),
])
def test_check_now_ignores_drafts_prereleases_and_non_version_tags(env, monkeypatch, payload, fragment):
    import io, json as _json

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(update_service, "urlopen", lambda *a, **k: _Resp(_json.dumps(payload).encode()))
    status = update_service.check_now("1.0.0")
    assert status["check_ok"] is False and fragment in status["check_error"]
    assert status["update_available"] is False


# ------------------------------------------------------------------ rollback
def _updated_and_reconciled(env, tag="v1.0.1", files=None):
    env.publish(tag, files)
    _update(env, tag)
    update_service.reconcile_pending(tag[1:], lambda *a, **k: None)


def test_rollback_from_branch_main_goes_back_exactly(env):
    start = env.head()
    _updated_and_reconciled(env)
    status = update_service.get_status("1.0.1")
    assert status["rollback_available"] is True
    pre = update_service.rollback_preflight(str(env.work), "1.0.1")
    assert pre["ok"] is True
    result = update_service.rollback(str(env.work), pre, "1.0.1")
    assert result["ok"] is True and result["to_version"] == "1.0.0"
    assert env.head() == start and env.branch() == "main" and env.clean()
    # a single step back: the record is consumed, no second rollback
    assert update_service.get_status("1.0.0")["rollback_available"] is False
    assert update_service.rollback_preflight(str(env.work), "1.0.0")["reason"] == "rollback_unavailable"


def test_rollback_from_a_detached_head_stays_detached(env):
    _run(["git", "checkout", "--quiet", "--detach", "v1.0.0"], cwd=env.work)
    start = env.head()
    _updated_and_reconciled(env)
    pre = update_service.rollback_preflight(str(env.work), "1.0.1")
    assert pre["ok"] is True and pre["detached"] is True
    assert update_service.rollback(str(env.work), pre, "1.0.1")["ok"] is True
    assert env.head() == start and env.branch() == ""


def test_rollback_is_only_offered_for_the_version_the_update_produced(env):
    _updated_and_reconciled(env)
    assert update_service.get_status("1.0.1")["rollback_available"] is True
    assert update_service.get_status("1.0.0")["rollback_available"] is False
    assert update_service.get_status("1.2.0")["rollback_available"] is False


def test_rollback_needs_a_successful_recorded_update(env):
    env.publish("v1.0.1")
    _update(env, "v1.0.1")  # applied, but the new process has not confirmed it yet
    assert update_service.get_status("1.0.1")["rollback_available"] is False
    update_service.reconcile_pending("1.0.0", lambda *a, **k: None)  # the restart came up on the old version
    assert update_service.get_status("1.0.1")["rollback_available"] is False


def test_rollback_refuses_a_dirty_tree_and_other_branches(env):
    _updated_and_reconciled(env)
    (env.work / "server.py").write_text("# local edit\n", encoding="utf-8")
    assert update_service.rollback_preflight(str(env.work), "1.0.1")["reason"] == "dirty_tree"
    _run(["git", "checkout", "--", "server.py"], cwd=env.work)
    _run(["git", "checkout", "-q", "-b", "wip"], cwd=env.work)
    assert update_service.rollback_preflight(str(env.work), "1.0.1")["reason"] == "unsupported_branch"


def test_rollback_blocked_when_dependency_files_differ_and_nothing_moves(env):
    env.publish("v1.0.1", {"server.py": "# 1.0.1\n"})
    pre, result = _update(env, "v1.0.1")
    # simulate that the new release's own dependency file is what is installed now
    (env.work / "requirements.txt").write_text("Flask>=3.0.0\nnewdep>=1\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=env.work)
    _run(["git", "commit", "-m", "dep change inside the release checkout"], cwd=env.work)
    # (HEAD moved off the tag by one commit; the recorded previous commit is still an ancestor)
    update_service.reconcile_pending("1.0.1", lambda *a, **k: None)
    here = env.head()
    rpre = update_service.rollback_preflight(str(env.work), "1.0.1")
    assert rpre["ok"] is True
    result = update_service.rollback(str(env.work), rpre, "1.0.1")
    assert result["blocked"] is True and result["changed_requirements_files"] == ["requirements.txt"]
    assert "git reset --keep" in result["instructions"] and "pip install" in result["instructions"]
    assert env.head() == here and env.clean()


# ------------------------------------------------------------------ pending update across the restart
def test_reconcile_logs_finished_when_the_new_version_is_running(env):
    env.publish("v1.0.1")
    _update(env, "v1.0.1")
    events = []
    last = update_service.reconcile_pending("1.0.1", lambda *a, **k: events.append((a, k)))
    assert last["ok"] is True and last["from"] == "1.0.0" and last["to"] == "1.0.1" and last["kind"] == "update"
    (title, level, details), kwargs = events[0]
    assert title == "Update finished" and level == "OK" and "1.0.0 -> 1.0.1" in details and kwargs["source"] == "update"
    status = update_service.get_status("1.0.1")
    assert status["pending_update"] is None and status["last_update"]["ok"] is True
    assert update_service.reconcile_pending("1.0.1", lambda *a, **k: events.append(1)) is None  # once only
    assert len(events) == 1


def test_reconcile_logs_failed_when_another_version_is_running(env):
    env.publish("v1.0.1")
    _update(env, "v1.0.1")
    events = []
    last = update_service.reconcile_pending("1.0.0", lambda *a, **k: events.append((a, k)))
    assert last["ok"] is False
    (title, level, details), _ = events[0]
    assert title == "Update failed" and level == "ERROR" and "1.0.0 is running" in details
    assert update_service.get_status("1.0.0")["last_update_ok"] is False


def test_reconcile_names_a_rollback_as_one(env):
    _updated_and_reconciled(env)
    pre = update_service.rollback_preflight(str(env.work), "1.0.1")
    update_service.rollback(str(env.work), pre, "1.0.1")
    events = []
    last = update_service.reconcile_pending("1.0.0", lambda *a, **k: events.append(a))
    assert last["kind"] == "rollback" and last["ok"] is True
    assert events[0][0] == "Rollback finished"


def test_reconcile_without_a_pending_update_does_nothing(env):
    assert update_service.reconcile_pending("1.0.0", lambda *a, **k: pytest.fail("no event expected")) is None
