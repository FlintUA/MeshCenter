"""MeshCenter Update Service

Checks GitHub Releases for a newer version, caches the result (so the
browser never hits the GitHub API directly and a background poll never
runs more often than the user's configured interval), and applies an
update via a plain `git merge --ff-only` - never a blind `git pull`, and
never with any attempt to auto-resolve a dirty tree or diverged history.
See git_preflight()'s docstring for why.

Instance-scoped, like data/instance.json and data/settings.json - the
installed version and git state are properties of this machine, not of
whichever radio profile happens to be active.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.request import Request, urlopen

from storage.json_store import safe_read_json, safe_write_json

GITHUB_REPO = "FlintUA/MeshCenter"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

_lock = threading.Lock()
_cache_path: str | None = None

_DEFAULT_CACHE: dict[str, Any] = {
    "last_checked_at": None,
    "latest_version": None,
    "latest_tag": None,
    "release_name": None,
    "release_notes": None,
    "release_url": None,
    "check_ok": None,
    "check_error": None,
    "previous_version_sha": None,
    "last_update_attempt_at": None,
    "last_update_ok": None,
}


def configure(cache_path: str) -> None:
    global _cache_path
    _cache_path = cache_path


def _load_cache() -> dict[str, Any]:
    return {**_DEFAULT_CACHE, **safe_read_json(_cache_path, {})}


def get_status(current_version: str) -> dict[str, Any]:
    with _lock:
        cache = _load_cache()
    cache["current_version"] = current_version
    cache["update_available"] = update_available(current_version, cache)
    return cache


def update_available(current_version: str, cache: dict[str, Any]) -> bool:
    """Whether current_version is genuinely behind the cached latest release.

    Deliberately conservative: resolve_app_version() returns a bare
    "X.Y.Z" only when HEAD is exactly tagged; a checkout with untagged
    commits on top returns "X.Y.Z-N-ghash" (see server.py's
    resolve_app_version()). That "-N-g" form means this checkout is
    already ahead of *some* tag, so it must never be flagged as behind
    just because it doesn't string-match the latest release tag - that
    would misreport an ahead-of-release dev checkout as needing an
    update. Only an exact "X.Y.Z" that differs from the cached latest
    tag counts.
    """
    latest = cache.get("latest_version")
    if not latest or not cache.get("check_ok"):
        return False
    if "-" in str(current_version):
        return False
    return str(current_version) != str(latest)


def check_now(current_version: str) -> dict[str, Any]:
    """Live GitHub API call - not itself rate-limited. Callers decide when
    this runs: the background worker on its own interval, or a direct
    "Check now" click. GitHub's unauthenticated rate limit is 60
    requests/hour/IP; a single Pi polling at most once a day (default) or
    on an occasional manual click stays trivially within that."""
    with _lock:
        cache = _load_cache()

    try:
        request = Request(
            GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "MeshCenter-update-check",
            },
        )
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))

        tag = str(payload.get("tag_name") or "").strip()
        version = tag[1:] if tag.startswith("v") else tag

        cache.update({
            "last_checked_at": int(time.time()),
            "latest_version": version,
            "latest_tag": tag,
            "release_name": payload.get("name") or tag,
            "release_notes": payload.get("body") or "",
            "release_url": payload.get("html_url")
                or f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}",
            "check_ok": True,
            "check_error": None,
        })
    except Exception as error:
        cache.update({
            "last_checked_at": int(time.time()),
            "check_ok": False,
            "check_error": str(error),
        })

    with _lock:
        safe_write_json(_cache_path, cache)

    return get_status(current_version)


def check_worker(
    current_version: str,
    get_settings: Callable[[], dict],
    on_new_version: Callable[[dict], None],
) -> None:
    """Background poller. Re-reads settings.updates on every wake (not
    just once at thread start) so a live toggle/interval change in
    Settings takes effect on the next cycle without a service restart.
    Fires on_new_version() at most once per distinct version - a
    persistently-ignored update notice doesn't get resent every cycle."""
    last_notified_version = None
    while True:
        settings = get_settings() or {}
        updates_settings = settings.get("updates", {}) if isinstance(settings, dict) else {}
        interval = int(updates_settings.get("interval", 86400) or 86400)
        auto_check = updates_settings.get("auto_check", True)

        if auto_check:
            try:
                status = check_now(current_version)
                if status.get("update_available") and status.get("latest_version") != last_notified_version:
                    on_new_version(status)
                    last_notified_version = status.get("latest_version")
            except Exception as error:
                print(f"[UPDATES] Background check failed: {error}", flush=True)

        time.sleep(max(300, interval))


def git_preflight(project_dir: str) -> dict[str, Any]:
    """Checks whether pulling is a safe, plain fast-forward: clean working
    tree, a real upstream, and no diverged/ahead local history. Never
    attempts to auto-stash, auto-discard or auto-merge - only reports
    *why* it isn't safe, so the caller can show an honest error instead of
    silently resolving something it shouldn't. Matches the discipline this
    project has followed by hand all session: verify with `git status`/
    `diff -b -w` before touching anything, never blind-discard.
    """
    result: dict[str, Any] = {
        "ok": False, "clean": False, "behind": 0, "ahead": 0,
        "dirty_files": [], "branch": None, "upstream": None,
        "reason": None, "error": None,
    }

    def run(args, timeout=10):
        return subprocess.run(
            args, cwd=project_dir, capture_output=True, text=True, timeout=timeout,
        )

    try:
        branch_proc = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        result["branch"] = branch_proc.stdout.strip()

        upstream_proc = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if upstream_proc.returncode != 0:
            result["reason"] = "no_upstream"
            result["error"] = upstream_proc.stderr.strip()
            return result
        upstream = upstream_proc.stdout.strip()
        result["upstream"] = upstream

        status_proc = run(["git", "status", "--porcelain"])
        dirty = [line[3:] for line in status_proc.stdout.splitlines() if line.strip()]
        result["dirty_files"] = dirty
        result["clean"] = len(dirty) == 0

        fetch_proc = run(["git", "fetch"], timeout=20)
        if fetch_proc.returncode != 0:
            result["reason"] = "fetch_failed"
            result["error"] = fetch_proc.stderr.strip()
            return result

        behind = run(["git", "rev-list", "--count", f"HEAD..{upstream}"])
        ahead = run(["git", "rev-list", "--count", f"{upstream}..HEAD"])
        result["behind"] = int(behind.stdout.strip() or 0)
        result["ahead"] = int(ahead.stdout.strip() or 0)

        if not result["clean"]:
            result["reason"] = "dirty_tree"
        elif result["ahead"] > 0:
            result["reason"] = "diverged"
        elif result["behind"] == 0:
            result["reason"] = "up_to_date"
        else:
            result["ok"] = True
    except Exception as error:
        result["error"] = str(error)
        if not result["reason"]:
            result["reason"] = "error"

    return result


def apply_update(project_dir: str, upstream: str) -> dict[str, Any]:
    """Only call this after git_preflight() reported ok=True for this same
    upstream - it does not re-verify safety itself, so the preflight
    snapshot the user actually confirmed against is exactly what gets
    applied, not a second, silently different check a few seconds later."""
    def run(args, timeout=30):
        return subprocess.run(
            args, cwd=project_dir, capture_output=True, text=True, timeout=timeout,
        )

    previous_sha = run(["git", "rev-parse", "HEAD"], timeout=10).stdout.strip()
    pull = run(["git", "merge", "--ff-only", upstream])

    with _lock:
        cache = _load_cache()
        cache["previous_version_sha"] = previous_sha
        cache["last_update_attempt_at"] = int(time.time())
        cache["last_update_ok"] = pull.returncode == 0
        safe_write_json(_cache_path, cache)

    return {
        "ok": pull.returncode == 0,
        "previous_sha": previous_sha,
        "output": (pull.stdout + pull.stderr).strip(),
    }

# INTENTIONAL SYNTAX ERROR for live restart-timeout testing - reverted immediately after
def broken(

