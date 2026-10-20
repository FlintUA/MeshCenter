"""MeshCenter Update Service

Checks GitHub Releases for a newer version, caches the result (so the
browser never hits the GitHub API directly and a background poll never
runs more often than the user's configured interval), and applies an
update to exactly that release's TAG (UPD1) - never `origin/main`, never a
blind `git pull`, and never with any attempt to auto-resolve a dirty tree
or diverged history. See git_preflight()'s docstring for the rules.

Instance-scoped, like data/instance.json and data/settings.json - the
installed version and git state are properties of this machine, not of
whichever radio profile happens to be active.
"""

from __future__ import annotations

import json
import re
import shlex
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

# F4.1 PR 2: background poller, not an HTTP handler - check_worker()'s own
# try/except already keeps the loop alive on any exception, but
# safe_write_json() never raises (it returns False), so a persistent cache
# write failure here would otherwise fail completely silently. Logs once
# per failure streak instead, same shape as system_log.py's own
# _log_write_failing.
_cache_write_failing = False


def _save_cache(cache: dict[str, Any]) -> bool:
    global _cache_write_failing
    ok = safe_write_json(_cache_path, cache)
    if ok:
        _cache_write_failing = False
    elif not _cache_write_failing:
        _cache_write_failing = True
        print("[UPDATES] Could not save update-check cache (further failures suppressed until it recovers)", flush=True)
    return ok

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
    "previous_version": None,
    "previous_ref": None,
    "last_update_attempt_at": None,
    "last_update_ok": None,
    # UPD1: set by apply_update()/rollback() right before the restart, resolved by
    # reconcile_pending() in the NEW process at boot (the old one is gone by then).
    "pending_update": None,
    # {"kind": "update"|"rollback", "from": "1.12.0", "to": "1.12.1", "at": epoch, "ok": bool}
    "last_update": None,
}

TAG_RE = re.compile(r"v\d+\.\d+\.\d+")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def configure(cache_path: str) -> None:
    global _cache_path
    _cache_path = cache_path


def _load_cache() -> dict[str, Any]:
    return {**_DEFAULT_CACHE, **safe_read_json(_cache_path, {})}


def version_tuple(version: Any) -> tuple[int, int, int] | None:
    """(major, minor, patch) of an exact release number ("1.12.1" or "v1.12.1"), else None."""
    text = str(version or "").strip()
    if text.startswith("v"):
        text = text[1:]
    if not VERSION_RE.fullmatch(text):
        return None
    major, minor, patch = (int(part) for part in text.split("."))
    return (major, minor, patch)


def get_status(current_version: str) -> dict[str, Any]:
    with _lock:
        cache = _load_cache()
    cache["current_version"] = current_version
    cache["update_available"] = update_available(current_version, cache)
    cache["rollback_available"] = rollback_available(current_version, cache)
    return cache


def update_available(current_version: str, cache: dict[str, Any]) -> bool:
    """Whether current_version is genuinely BEHIND the cached latest release.

    A numeric comparison, never a string difference (UPD1): a checkout whose version is
    newer than the cached latest (a withdrawn release, a pre-release tag, a node ahead of
    the published release) must never be offered a downgrade. Only an exact release number
    can be behind one: resolve_app_version() returns the nearest reachable "vX.Y.Z" tag, so
    anything else ("dev", "dk2", a bare hash, "unknown") is not comparable (DK3) and is
    never offered an update.
    """
    if not cache.get("check_ok"):
        return False
    latest = version_tuple(cache.get("latest_version"))
    current = version_tuple(current_version)
    if latest is None or current is None:
        return False
    return latest > current


def rollback_available(current_version: str, cache: dict[str, Any]) -> bool:
    """A one-click rollback is offered only after a successful update this installation
    recorded, only to the recorded previous version, and only while the running version is
    the one that update produced - so a stale record can never roll an unrelated checkout."""
    last = cache.get("last_update") or {}
    if not (cache.get("previous_version_sha") and cache.get("previous_version") and last.get("ok")):
        return False
    return last.get("kind") == "update" and str(last.get("to")) == str(current_version)


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

        if payload.get("draft") or payload.get("prerelease"):
            raise ValueError("the latest release is a draft or pre-release")
        tag = str(payload.get("tag_name") or "").strip()
        if not TAG_RE.fullmatch(tag):
            raise ValueError(f"the latest release tag {tag!r} is not a vX.Y.Z version tag")
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
        _save_cache(cache)

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



SUPPORTED_BRANCH = "main"


def _git(project_dir: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=project_dir, capture_output=True, text=True, timeout=timeout)


def _checkout_state(project_dir: str) -> dict[str, Any]:
    """Where HEAD is: the branch name, or detached; plus the commit and the dirty files."""
    branch_proc = _git(project_dir, "symbolic-ref", "-q", "--short", "HEAD", timeout=10)
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else None
    head = _git(project_dir, "rev-parse", "HEAD", timeout=10).stdout.strip()
    status = _git(project_dir, "status", "--porcelain", timeout=10)
    dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    return {"branch": branch, "detached": branch is None, "head_sha": head, "dirty_files": dirty}


def _is_ancestor(project_dir: str, ancestor: str, descendant: str) -> bool:
    return _git(project_dir, "merge-base", "--is-ancestor", ancestor, descendant, timeout=10).returncode == 0


def git_preflight(project_dir: str, target_tag: str | None) -> dict[str, Any]:
    """Whether moving this checkout to the release tag `target_tag` is safe (UPD1).

    The target is the latest GitHub release's TAG (never origin/main). Allowed starting
    points: branch `main` or a detached HEAD (how the nodes sit after `git checkout vX.Y.Z`);
    any other branch is a development checkout and is left alone. Refused, with a reason and
    nothing touched, when: there is no valid target (`no_target`), the branch is not main
    (`unsupported_branch`), the tree is dirty (`dirty_tree`), the tag cannot be fetched from
    origin (`fetch_failed` / `tag_missing`), HEAD already is the tag (`up_to_date`), HEAD
    already contains the tag - newer than the release, a downgrade (`ahead`), or the history
    has diverged from the tag (`diverged`). Only "HEAD is a plain ancestor of the tag" is ok.
    Never auto-stashes, auto-discards or auto-merges."""
    result: dict[str, Any] = {
        "ok": False, "clean": False, "behind": 0, "ahead": 0,
        "dirty_files": [], "branch": None, "detached": False,
        "target_tag": target_tag, "target_sha": None, "head_sha": None,
        "reason": None, "error": None,
    }
    try:
        if not target_tag or not TAG_RE.fullmatch(str(target_tag)):
            result["reason"] = "no_target"
            return result

        state = _checkout_state(project_dir)
        result.update(branch=state["branch"], detached=state["detached"],
                      head_sha=state["head_sha"], dirty_files=state["dirty_files"],
                      clean=not state["dirty_files"])

        if not state["detached"] and state["branch"] != SUPPORTED_BRANCH:
            result["reason"] = "unsupported_branch"
            return result
        if state["dirty_files"]:
            result["reason"] = "dirty_tree"
            return result

        fetch = _git(project_dir, "fetch", "--no-tags", "origin", f"refs/tags/{target_tag}:refs/tags/{target_tag}", timeout=30)
        if fetch.returncode != 0:
            error = fetch.stderr.strip()
            result["error"] = error
            result["reason"] = "tag_missing" if "couldn't find remote ref" in error else "fetch_failed"
            return result

        target = _git(project_dir, "rev-parse", "--verify", "--quiet", f"refs/tags/{target_tag}^{{commit}}", timeout=10)
        if target.returncode != 0 or not target.stdout.strip():
            result["reason"] = "tag_missing"
            return result
        target_sha = target.stdout.strip()
        result["target_sha"] = target_sha

        if state["head_sha"] == target_sha:
            result["reason"] = "up_to_date"
        elif _is_ancestor(project_dir, target_sha, state["head_sha"]):
            result["reason"] = "ahead"
        elif _is_ancestor(project_dir, state["head_sha"], target_sha):
            behind = _git(project_dir, "rev-list", "--count", f"{state['head_sha']}..{target_sha}", timeout=10)
            result["behind"] = int(behind.stdout.strip() or 0)
            result["ok"] = True
        else:
            result["reason"] = "diverged"
    except Exception as error:
        result["error"] = str(error)
        if not result["reason"]:
            result["reason"] = "error"
    return result


_REQUIREMENTS_FILES = ("requirements.txt", "adapters/meshtastic/requirements.txt")


def _changed_requirements(project_dir: str, target: str) -> list[str]:
    diff = _git(project_dir, "diff", "--name-only", "HEAD", target, "--", *_REQUIREMENTS_FILES, timeout=10)
    return [line for line in diff.stdout.strip().splitlines() if line]


# v1.12.2: host files an update never installs by itself (INSTALL.md, "Easy to forget"): the systemd
# unit, the sudoers templates, the udev rule and the two privileged helper scripts. A change in one
# of them does NOT block an update (only dependency files do) - the card shows a "manual step
# needed" notice with the exact command instead. Keys are repo paths; deploy/docker/ is container-only.
_MANUAL_STEP_FILES = (
    "deploy/meshcenter.service",
    "deploy/meshcenter.sudoers",
    "deploy/meshcenter-wifi.sudoers",
    "deploy/meshcenter-hw.sudoers",
    "deploy/99-meshcenter-rtc.rules",
    "scripts/meshcenter-network-helper",
    "scripts/meshcenter-hw-config",
)


def _sudoers_command(src: str, name: str) -> str:
    # rendered and validated BEFORE it replaces the live file (a broken sudoers file locks sudo out)
    return (
        f'sed "s|__MESH_USER__|$USER|g" {src} > /tmp/{name}.sudoers && sudo visudo -cf /tmp/{name}.sudoers '
        f'&& sudo install -o root -g root -m 0440 /tmp/{name}.sudoers /etc/sudoers.d/{name}'
    )


def manual_step_commands(project_dir: str, files: list[str]) -> list[dict[str, str]]:
    """The exact commands (same as install.sh) for each changed host file, in a stable order."""
    base = str(project_dir).rstrip("/")

    def path(rel: str) -> str:
        return shlex.quote(f"{base}/{rel}")

    out = []
    for rel in _MANUAL_STEP_FILES:
        if rel not in files:
            continue
        if rel == "deploy/meshcenter.service":
            command = (
                f'sed -e "s|__MESH_USER__|$USER|g" -e "s|__MESH_HOME__|$HOME|g" {path(rel)} '
                "| sudo tee /etc/systemd/system/meshcenter.service > /dev/null "
                "&& sudo systemctl daemon-reload && sudo systemctl restart meshcenter.service"
            )
        elif rel == "deploy/meshcenter.sudoers":
            command = _sudoers_command(path(rel), "meshcenter")
        elif rel == "deploy/meshcenter-wifi.sudoers":
            command = _sudoers_command(path(rel), "meshcenter-wifi")
        elif rel == "deploy/meshcenter-hw.sudoers":
            command = _sudoers_command(path(rel), "meshcenter-hw")
        elif rel == "deploy/99-meshcenter-rtc.rules":
            command = (
                f"sudo install -o root -g root -m 0644 {path(rel)} /etc/udev/rules.d/99-meshcenter-rtc.rules "
                "&& sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=rtc"
            )
        else:
            name = rel.rsplit("/", 1)[-1]
            command = f"sudo install -o root -g root -m 0755 {path(rel)} /usr/local/sbin/{name}"
        out.append({"file": rel, "command": command})
    return out


def manual_steps_between(project_dir: str, target: str) -> list[dict[str, str]]:
    """Manual steps needed to go from HEAD to `target` (commit-ish): the host files that differ."""
    diff = _git(project_dir, "diff", "--name-only", "HEAD", target, "--", *_MANUAL_STEP_FILES, timeout=10)
    files = [line for line in diff.stdout.strip().splitlines() if line]
    return manual_step_commands(project_dir, files)


def _blocked_by_requirements(project_dir: str, files: list[str], previous_sha: str, *, manual_command: str) -> dict[str, Any]:
    with _lock:
        cache = _load_cache()
        cache["last_update_attempt_at"] = int(time.time())
        cache["last_update_ok"] = False
        _save_cache(cache)
    return {
        "ok": False,
        "blocked": True,
        "reason": "requirements_changed",
        "previous_sha": previous_sha,
        "requirements_changed": True,
        "changed_requirements_files": files,
        "output": "",
        "instructions": (
            "This change touches dependency file(s): {files}. It was NOT applied - "
            "HEAD and the working tree are unchanged. To do it manually: "
            "1) {command}  "
            "2) source venv/bin/activate && pip install -r requirements.txt "
            "(and, if adapters/meshtastic/requirements.txt is listed above, "
            "adapters/meshtastic/venv/bin/pip install -r adapters/meshtastic/requirements.txt)  "
            "3) sudo systemctl restart meshcenter.service"
        ).format(files=", ".join(files), command=manual_command),
    }


def apply_update(project_dir: str, preflight: dict[str, Any], current_version: str | None = None) -> dict[str, Any]:
    """Move this checkout to the preflight's target tag. Only call after git_preflight()
    reported ok=True; the commit that preflight resolved (`target_sha`) is exactly what is
    applied, not a second, silently different lookup.

    Dependency-file changes are detected BEFORE anything moves (a plain `git diff HEAD
    <target>` touches neither HEAD nor the tree); if requirements.txt or the adapter's
    requirements.txt differ, nothing is applied and the manual steps are returned. Never runs
    pip (shelling out to pip against the venv the live process is running out of is its own
    hazard). On branch `main` the branch fast-forwards to the TAG's commit (`git merge
    --ff-only`), on a detached HEAD it is re-detached at the tag (`git checkout --detach`)."""
    target_tag = preflight.get("target_tag")
    target_sha = preflight.get("target_sha")
    previous_sha = _git(project_dir, "rev-parse", "HEAD", timeout=10).stdout.strip()
    detached = bool(preflight.get("detached"))
    previous_ref = "detached" if detached else str(preflight.get("branch") or SUPPORTED_BRANCH)

    changed = _changed_requirements(project_dir, target_sha)
    if changed:
        command = f"git checkout --detach {target_tag}" if detached else f"git merge --ff-only {target_tag}"
        return _blocked_by_requirements(project_dir, changed, previous_sha, manual_command=command)

    manual_steps = manual_steps_between(project_dir, target_sha)

    if detached:
        moved = _git(project_dir, "checkout", "--quiet", "--detach", target_sha)
    else:
        moved = _git(project_dir, "merge", "--ff-only", target_sha)
    ok = moved.returncode == 0
    to_version = str(target_tag)[1:]

    with _lock:
        cache = _load_cache()
        cache["last_update_attempt_at"] = int(time.time())
        cache["last_update_ok"] = ok
        if ok:
            cache["previous_version_sha"] = previous_sha
            cache["previous_version"] = current_version
            cache["previous_ref"] = previous_ref
            cache["pending_update"] = {
                "kind": "update", "from": current_version, "to": to_version, "tag": target_tag,
                "from_sha": previous_sha, "to_sha": target_sha, "started_at": int(time.time()),
                "manual_steps": manual_steps,
            }
        _save_cache(cache)

    return {
        "ok": ok,
        "blocked": False,
        "manual_steps": manual_steps,
        "previous_sha": previous_sha,
        "previous_version": current_version,
        "to_version": to_version,
        "target_tag": target_tag,
        "target_sha": target_sha,
        "output": (moved.stdout + moved.stderr).strip(),
        "requirements_changed": False,
        "changed_requirements_files": [],
    }


def rollback_preflight(project_dir: str, current_version: str) -> dict[str, Any]:
    """Whether going back to the recorded previous version is allowed. Same checkout rules
    as an update (main or detached, clean tree); the recorded commit must still be in the
    repository and an ancestor of HEAD (a rollback is a step back along this history)."""
    with _lock:
        cache = _load_cache()
    result: dict[str, Any] = {
        "ok": False, "reason": None, "error": None, "branch": None, "detached": False,
        "dirty_files": [], "previous_sha": cache.get("previous_version_sha"),
        "previous_version": cache.get("previous_version"), "head_sha": None,
    }
    try:
        if not rollback_available(current_version, cache):
            result["reason"] = "rollback_unavailable"
            return result
        state = _checkout_state(project_dir)
        result.update(branch=state["branch"], detached=state["detached"],
                      head_sha=state["head_sha"], dirty_files=state["dirty_files"])
        if not state["detached"] and state["branch"] != SUPPORTED_BRANCH:
            result["reason"] = "unsupported_branch"
        elif state["dirty_files"]:
            result["reason"] = "dirty_tree"
        else:
            previous = str(cache.get("previous_version_sha"))
            known = _git(project_dir, "cat-file", "-e", f"{previous}^{{commit}}", timeout=10).returncode == 0
            if not known or not _is_ancestor(project_dir, previous, state["head_sha"]) or previous == state["head_sha"]:
                result["reason"] = "rollback_unavailable"
            else:
                result["ok"] = True
    except Exception as error:
        result["error"] = str(error)
        result["reason"] = result["reason"] or "error"
    return result


def rollback(project_dir: str, preflight: dict[str, Any], current_version: str) -> dict[str, Any]:
    """Go back to the version recorded by the last update. Call only after
    rollback_preflight() reported ok. Blocked (nothing moved, manual command returned) when the
    dependency files differ between here and there - same rule as an update."""
    previous_sha = str(preflight.get("previous_sha"))
    previous_version = preflight.get("previous_version")
    detached = bool(preflight.get("detached"))
    head_sha = _git(project_dir, "rev-parse", "HEAD", timeout=10).stdout.strip()

    changed = _changed_requirements(project_dir, previous_sha)
    if changed:
        command = f"git checkout --detach {previous_sha}" if detached else f"git reset --keep {previous_sha}"
        return _blocked_by_requirements(project_dir, changed, head_sha, manual_command=command)

    manual_steps = manual_steps_between(project_dir, previous_sha)

    if detached:
        moved = _git(project_dir, "checkout", "--quiet", "--detach", previous_sha)
    else:
        moved = _git(project_dir, "reset", "--keep", previous_sha)
    ok = moved.returncode == 0

    with _lock:
        cache = _load_cache()
        cache["last_update_attempt_at"] = int(time.time())
        cache["last_update_ok"] = ok
        if ok:
            cache["previous_version_sha"] = None
            cache["previous_version"] = None
            cache["previous_ref"] = None
            cache["pending_update"] = {
                "kind": "rollback", "from": current_version, "to": previous_version, "tag": None,
                "from_sha": head_sha, "to_sha": previous_sha, "started_at": int(time.time()),
                "manual_steps": manual_steps,
            }
        _save_cache(cache)

    return {
        "ok": ok, "blocked": False, "manual_steps": manual_steps,
        "previous_sha": head_sha, "to_version": previous_version,
        "output": (moved.stdout + moved.stderr).strip(),
        "requirements_changed": False, "changed_requirements_files": [],
    }


def reconcile_pending(current_version: str, log: Callable[..., None]) -> dict[str, Any] | None:
    """Called once at boot by the NEW process: resolve an update/rollback the previous process
    started. Records `last_update` ({kind, from, to, at, ok}) and logs "Update finished" or
    "Update failed" (the same for rollbacks) to the system log via `log(title, level, details,
    source=...)`. A running version that is not the target is a failure (the new code did not
    come up as the version that was applied)."""
    with _lock:
        cache = _load_cache()
        pending = cache.get("pending_update")
        if not pending:
            return None
        kind = pending.get("kind") or "update"
        ok = str(current_version) == str(pending.get("to"))
        cache["pending_update"] = None
        cache["last_update_ok"] = ok
        cache["last_update"] = {
            "kind": kind, "from": pending.get("from"), "to": pending.get("to"),
            "at": int(time.time()), "ok": ok,
            "manual_steps": list(pending.get("manual_steps") or []),
        }
        _save_cache(cache)
    noun = "Rollback" if kind == "rollback" else "Update"
    if ok:
        steps = pending.get("manual_steps") or []
        extra = f"; manual step needed: {', '.join(step['file'] for step in steps)}" if steps else ""
        log(f"{noun} finished", "OK", f"MeshCenter {pending.get('from')} -> {pending.get('to')}{extra}", source="update")
    else:
        log(f"{noun} failed", "ERROR",
            f"{pending.get('from')} -> {pending.get('to')} was applied but {current_version} is running", source="update")
    return cache["last_update"]
