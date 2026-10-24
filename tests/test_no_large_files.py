"""Repository guard: no archives and no file over 5 MB (scripts/check_no_large_files.py), and the hooks that run it."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_no_large_files.py"
sys.path.insert(0, str(REPO / "scripts"))
import check_no_large_files as guard  # noqa: E402


def test_the_repository_itself_is_clean():
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(REPO), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_judgement_rules():
    assert guard.judge("dist-nas/meshcenter-1.13.0.tar", 10) is not None
    assert guard.judge("backup.tar.gz", 10) is not None
    assert guard.judge("BUNDLE.TGZ", 10) is not None
    assert guard.judge("static/big.bin", guard.MAX_BYTES + 1) is not None
    assert guard.judge("static/ok.bin", guard.MAX_BYTES) is None
    assert guard.judge("docs/images/screenshot.png", 1_600_000) is None
    assert guard.judge("tests/fixtures/real.tar.gz.txt", 10) is None  # only the suffix counts


def _repo(tmp_path):
    def run(*args):
        return subprocess.run(["git", *args], cwd=str(tmp_path), capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    run("config", "commit.gpgsign", "false")
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "check_no_large_files.py")
    hooks = tmp_path / "scripts" / "git-hooks"
    hooks.mkdir()
    for name in ("pre-commit", "pre-push"):
        shutil.copy(REPO / "scripts" / "git-hooks" / name, hooks / name)
        os.chmod(hooks / name, 0o755)
    run("config", "core.hooksPath", "scripts/git-hooks")
    return run


def test_staged_mode_refuses_an_archive_and_an_oversized_file(tmp_path):
    run = _repo(tmp_path)
    (tmp_path / "image.tar").write_bytes(b"x" * 100)
    (tmp_path / "huge.bin").write_bytes(b"\0" * (guard.MAX_BYTES + 1))
    (tmp_path / "fine.txt").write_text("ok")
    run("add", "image.tar", "huge.bin", "fine.txt")
    result = subprocess.run([sys.executable, str(tmp_path / "scripts" / "check_no_large_files.py"), "--staged"], cwd=str(tmp_path), capture_output=True, text=True)
    assert result.returncode == 1
    assert "image.tar" in result.stdout and "huge.bin" in result.stdout and "fine.txt" not in result.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell for the hook")
def test_the_pre_commit_hook_blocks_the_commit(tmp_path):
    run = _repo(tmp_path)
    (tmp_path / "ok.txt").write_text("ok")
    run("add", "ok.txt")
    assert run("commit", "-q", "-m", "fine").returncode == 0
    (tmp_path / "image.tar").write_bytes(b"x" * 100)
    run("add", "-f", "image.tar")
    blocked = run("commit", "-q", "-m", "bad")
    assert blocked.returncode != 0 and "image.tar" in (blocked.stdout + blocked.stderr)
    assert run("rev-list", "--count", "HEAD").stdout.strip() == "1"  # the bad commit was never made


def test_range_mode_sees_the_files_of_the_pushed_commits(tmp_path):
    run = _repo(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    run("add", "a.txt")
    run("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "one")
    base = run("rev-parse", "HEAD").stdout.strip()
    (tmp_path / "bundle.tgz").write_bytes(b"x")
    run("add", "-f", "bundle.tgz")
    run("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "two")
    head = run("rev-parse", "HEAD").stdout.strip()
    result = subprocess.run([sys.executable, str(tmp_path / "scripts" / "check_no_large_files.py"), "--range", f"{base}..{head}"], cwd=str(tmp_path), capture_output=True, text=True)
    assert result.returncode == 1 and "bundle.tgz" in result.stdout
