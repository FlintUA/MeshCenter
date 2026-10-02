"""Tests for scripts/check_asset_cache_busting.py's git-diff-based drift
detector (H1-C review follow-up, PR #323).

The pre-existing checks in that script only ever verify INTERNAL
consistency (does static/i18n.js's CATALOG_VERSION agree with the
script's own hardcoded CURRENT_VERSION, does every entry already listed
in CURRENT_ASSETS carry that same token in templates/index.html) - they
cannot notice a changed file that nobody remembered to list anywhere at
all, which is exactly what happened in PR #323 (static/chat.js changed,
CURRENT_ASSETS was never updated to include it). `changed_static_files()`
and `_catalog_version_on_main()` close that by reading real git state
instead of trusting a human to have kept CURRENT_ASSETS/CURRENT_VERSION
in sync - these tests build a disposable, real git repo (subprocess, no
mocks) to exercise them against actual `git diff`/`git show` output.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_asset_cache_busting import changed_static_files, _catalog_version_on_main


def _git(repo, *args):
    result = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message, "--quiet")
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    """A real git repo with a `main` branch (immediately also registered
    as `refs/remotes/origin/main`, without a real remote - the standard
    trick for exercising `origin/...`-based tooling against a disposable
    local repo) and one feature branch checked out, ready for a test to
    make its own changes and commit them."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "i18n").mkdir()
    (tmp_path / "static" / "chat.js").write_text("console.log('v1');\n", encoding="utf-8")
    (tmp_path / "static" / "i18n" / "en.json").write_text('{"a": "1"}\n', encoding="utf-8")
    (tmp_path / "static" / "i18n.js").write_text(
        "var CATALOG_VERSION = 'v1';\n", encoding="utf-8"
    )
    main_sha = _commit_all(tmp_path, "initial")

    # Register this exact commit as the local stand-in for origin/main -
    # changed_static_files()/_catalog_version_on_main() only ever read
    # `origin/main`, never caring whether a real remote backs it.
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", main_sha)

    _git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_changed_static_files_lists_a_changed_static_path(repo):
    (repo / "static" / "chat.js").write_text("console.log('v2');\n", encoding="utf-8")
    _commit_all(repo, "change chat.js")

    assert changed_static_files(repo_root=repo) == ["static/chat.js"]


def test_changed_static_files_ignores_non_static_paths(repo):
    (repo / "server.py").write_text("print('hi')\n", encoding="utf-8")
    _commit_all(repo, "change server.py")

    assert changed_static_files(repo_root=repo) == []


def test_changed_static_files_is_none_on_main_itself(repo):
    """No-op on main: a checkout that IS already origin/main (a push-to-
    main CI run, or a plain local checkout of main) has nothing to diff
    against by definition - must never report an empty list masquerading
    as "nothing changed" when the real answer is "not applicable here"."""
    _git(repo, "checkout", "-q", "main")
    assert changed_static_files(repo_root=repo) is None


def test_changed_static_files_is_none_without_origin_main(tmp_path):
    """A repo that never had origin/main registered at all (e.g. a
    shallow/local clone) must fail quiet, not crash the whole check."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _commit_all(tmp_path, "initial")

    assert changed_static_files(repo_root=tmp_path) is None


def test_catalog_version_on_main_reads_the_baseline_value(repo):
    (repo / "static" / "i18n.js").write_text(
        "var CATALOG_VERSION = 'v2';\n", encoding="utf-8"
    )
    _commit_all(repo, "bump catalog version")

    # The feature branch now has 'v2', but main (the baseline) is still 'v1'.
    assert _catalog_version_on_main(repo_root=repo) == "v1"


def test_catalog_version_on_main_is_none_without_origin_main(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    _commit_all(tmp_path, "initial")

    assert _catalog_version_on_main(repo_root=tmp_path) is None


def test_the_pr_323_scenario_catalog_changed_but_version_not_bumped(repo):
    """Reproduces PR #323's actual miss: static/i18n/en.json's CONTENT
    changed, but static/i18n.js's CATALOG_VERSION was left exactly as it
    was on main - comparing against CURRENT_VERSION alone (the script's
    pre-existing check) would pass vacuously here if CURRENT_VERSION was
    ALSO never bumped (as it genuinely wasn't in the real PR); comparing
    against main's own prior CATALOG_VERSION value, as these two
    functions do together, catches it regardless."""
    (repo / "static" / "i18n" / "en.json").write_text('{"a": "2"}\n', encoding="utf-8")
    _commit_all(repo, "change catalog content only")

    changed = changed_static_files(repo_root=repo)
    assert "static/i18n/en.json" in changed

    baseline = _catalog_version_on_main(repo_root=repo)
    # i18n.js was never touched on this branch - current value == baseline.
    current = repo.joinpath("static", "i18n.js").read_text(encoding="utf-8")
    assert "CATALOG_VERSION = 'v1'" in current
    assert baseline == "v1"  # unchanged -> main()'s own check must flag this
