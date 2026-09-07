"""tests/test_update_service_requirements_changed.py

PR #231 review (3rd pass): meshsrv.update_service.apply_update() must
detect a requirements.txt/adapters/meshtastic/requirements.txt change
BEFORE running `git merge --ff-only` - not merge first and only then
notice. When dependencies changed, apply_update() must: not merge; leave
HEAD and the working tree completely unchanged; never run pip; never
report ok=True (so the API layer never schedules a restart). Also covers
the untouched-requirements happy path and confirms no pip invocation
happens anywhere in this module regardless of outcome.

Uses two real local git repositories (a bare "origin" and a working
clone) rather than mocking subprocess - apply_update() itself only ever
calls `git`, so exercising the real command against real repos is more
representative than stubbing its return values by hand, and git
operations against a local tmp_path repo are fast and require no
network access.
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


def _head_sha(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _working_tree_is_clean(repo: Path) -> bool:
    status = _run(["git", "status", "--porcelain"], cwd=repo).stdout.strip()
    return status == ""


def _git_repo(tmp_path: Path) -> Path:
    """A bare 'origin' plus a working clone with a committed requirements.txt,
    a configured remote-tracking branch, and identity set locally (never
    touching the user's real global git config)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run(["git", "init", "--bare"], cwd=origin)

    work = tmp_path / "work"
    work.mkdir()
    _run(["git", "init"], cwd=work)
    _run(["git", "config", "user.email", "test@example.invalid"], cwd=work)
    _run(["git", "config", "user.name", "Test"], cwd=work)
    (work / "requirements.txt").write_text("Flask>=3.0.0\n", encoding="utf-8")
    (work / "server.py").write_text("# placeholder\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=work)
    _run(["git", "commit", "-m", "initial"], cwd=work)
    _run(["git", "branch", "-M", "main"], cwd=work)
    _run(["git", "remote", "add", "origin", str(origin)], cwd=work)
    _run(["git", "push", "-u", "origin", "main"], cwd=work)
    return work


@pytest.fixture
def repo(tmp_path):
    update_service.configure(str(tmp_path / "update_check_cache.json"))
    return _git_repo(tmp_path)


def _push_from_a_second_clone(repo: Path, *, mutate) -> None:
    """Simulates 'someone else pushed an update' - a second clone makes a
    commit and pushes it to the same origin repo's own fixture uses, then
    repo's own working tree fetches/merges it via apply_update() itself,
    exactly like a real `git pull` against a real upstream."""
    origin_url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    second_clone = repo.parent / "second_clone"
    _run(["git", "clone", "--branch", "main", origin_url, str(second_clone)], cwd=repo.parent)
    _run(["git", "config", "user.email", "test@example.invalid"], cwd=second_clone)
    _run(["git", "config", "user.name", "Test"], cwd=second_clone)
    mutate(second_clone)
    _run(["git", "add", "."], cwd=second_clone)
    _run(["git", "commit", "-m", "update"], cwd=second_clone)
    _run(["git", "push", "origin", "main"], cwd=second_clone)


def test_apply_update_blocks_and_leaves_head_unchanged_when_requirements_txt_changed(repo):
    original_head = _head_sha(repo)
    _push_from_a_second_clone(
        repo, mutate=lambda clone: (clone / "requirements.txt").write_text("Flask>=3.1.0\ncbor2>=6.1.0\n", encoding="utf-8")
    )

    preflight = update_service.git_preflight(str(repo))
    assert preflight["ok"] is True

    result = update_service.apply_update(str(repo), preflight["upstream"])

    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["reason"] == "requirements_changed"
    assert result["requirements_changed"] is True
    assert result["changed_requirements_files"] == ["requirements.txt"]
    assert "requirements.txt" in result["instructions"]
    assert "pip install" in result["instructions"]
    assert "systemctl restart" in result["instructions"]

    # The whole point: never merged. HEAD must be exactly what it was
    # before apply_update() was ever called, and the working tree clean -
    # not "merged then somehow reverted", genuinely never touched.
    assert _head_sha(repo) == original_head
    assert _working_tree_is_clean(repo)


def test_apply_update_blocks_for_the_adapter_requirements_file_too(repo):
    original_head = _head_sha(repo)

    def mutate(clone: Path) -> None:
        (clone / "adapters").mkdir()
        (clone / "adapters" / "meshtastic").mkdir()
        (clone / "adapters" / "meshtastic" / "requirements.txt").write_text("meshtastic>=2.5.0\n", encoding="utf-8")

    _push_from_a_second_clone(repo, mutate=mutate)

    preflight = update_service.git_preflight(str(repo))
    assert preflight["ok"] is True

    result = update_service.apply_update(str(repo), preflight["upstream"])

    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["changed_requirements_files"] == ["adapters/meshtastic/requirements.txt"]
    assert _head_sha(repo) == original_head
    assert _working_tree_is_clean(repo)


def test_apply_update_blocks_when_both_requirements_files_changed_in_one_update(repo):
    original_head = _head_sha(repo)

    def mutate(clone: Path) -> None:
        (clone / "requirements.txt").write_text("Flask>=3.1.0\n", encoding="utf-8")
        (clone / "adapters").mkdir()
        (clone / "adapters" / "meshtastic").mkdir()
        (clone / "adapters" / "meshtastic" / "requirements.txt").write_text("meshtastic>=2.5.0\n", encoding="utf-8")

    _push_from_a_second_clone(repo, mutate=mutate)
    preflight = update_service.git_preflight(str(repo))
    result = update_service.apply_update(str(repo), preflight["upstream"])

    assert result["blocked"] is True
    assert sorted(result["changed_requirements_files"]) == [
        "adapters/meshtastic/requirements.txt", "requirements.txt",
    ]
    assert _head_sha(repo) == original_head


def test_apply_update_proceeds_normally_when_requirements_txt_is_untouched(repo):
    original_head = _head_sha(repo)
    _push_from_a_second_clone(
        repo, mutate=lambda clone: (clone / "server.py").write_text("# placeholder\n# a real change\n", encoding="utf-8")
    )

    preflight = update_service.git_preflight(str(repo))
    assert preflight["ok"] is True

    result = update_service.apply_update(str(repo), preflight["upstream"])

    assert result["ok"] is True
    assert result["blocked"] is False
    assert result["requirements_changed"] is False
    assert result["changed_requirements_files"] == []
    # This time it really did merge - HEAD moved forward.
    assert _head_sha(repo) != original_head


def test_apply_update_never_runs_pip_when_blocked(repo, monkeypatch):
    """The whole point of this fix: detection only, never an automatic
    install, whether or not the update is blocked. Pins that no `pip`
    invocation happens anywhere inside apply_update() by making one
    raise loudly if it ever were called."""
    real_run = subprocess.run

    def _guarded_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and any("pip" in str(part).lower() for part in args):
            raise AssertionError(f"apply_update() must never invoke pip, got: {args}")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(update_service.subprocess, "run", _guarded_run)

    _push_from_a_second_clone(
        repo, mutate=lambda clone: (clone / "requirements.txt").write_text("Flask>=3.1.0\n", encoding="utf-8")
    )
    preflight = update_service.git_preflight(str(repo))
    result = update_service.apply_update(str(repo), preflight["upstream"])

    assert result["blocked"] is True


def test_apply_update_never_runs_pip_when_not_blocked(repo, monkeypatch):
    real_run = subprocess.run

    def _guarded_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and any("pip" in str(part).lower() for part in args):
            raise AssertionError(f"apply_update() must never invoke pip, got: {args}")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(update_service.subprocess, "run", _guarded_run)

    _push_from_a_second_clone(
        repo, mutate=lambda clone: (clone / "server.py").write_text("# placeholder\n# a real change\n", encoding="utf-8")
    )
    preflight = update_service.git_preflight(str(repo))
    result = update_service.apply_update(str(repo), preflight["upstream"])

    assert result["ok"] is True
    assert result["blocked"] is False
