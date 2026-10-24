#!/usr/bin/env python3
"""Repository guard: no large files and no archives in the repository.

Why: on 2026-10-24 a locally built Docker image archive (about 80 MB, it contains GPLv3 code) was committed by
a blanket `git add -A` and pushed to the PUBLIC repository. A built image must never be published (see
docs/Docker_Installation.md and THIRD_PARTY_NOTICES.md), and nothing that size belongs in git anyway.

Rejects, with file and reason:
  * any file larger than MAX_BYTES (5 MB),
  * any archive by name: *.tar, *.tar.gz, *.tgz (the usual shapes of an image or a release bundle).

Modes (stdlib + git only, like the other scripts/check_*.py):
  check_no_large_files.py            every tracked file (CI)
  check_no_large_files.py --staged   files staged for the next commit (the pre-commit hook)
  check_no_large_files.py --range A..B   files added or changed by the commits in a range (the pre-push hook)

The hooks live in scripts/git-hooks/ and are switched on once per clone with
`git config core.hooksPath scripts/git-hooks` (CLAUDE.md, "Repository guard"). Add to ALLOW_PATHS only for a
genuine, reviewed exception, with the reason next to the entry.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

MAX_BYTES = 5 * 1024 * 1024
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz")
ALLOW_PATHS: tuple[str, ...] = ()  # (path, reason) exceptions go here; none today


def _git(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=False)


def _names(result: subprocess.CompletedProcess) -> list[str]:
    return [n.decode("utf-8", "replace") for n in result.stdout.split(b"\0") if n]


def judge(path: str, size: int) -> str | None:
    """The reason a file is refused, or None."""
    if path in ALLOW_PATHS:
        return None
    lowered = path.lower()
    if lowered.endswith(ARCHIVE_SUFFIXES):
        return "archives (.tar, .tar.gz, .tgz) are not allowed in the repository (a built image must never be committed)"
    if size > MAX_BYTES:
        return f"{size / 1024 / 1024:.1f} MB is over the {MAX_BYTES // 1024 // 1024} MB limit"
    return None


def tracked(cwd: Path = REPO_ROOT) -> list[tuple[str, int]]:
    out = []
    for name in _names(_git("ls-files", "-z", cwd=cwd)):
        path = cwd / name
        try:
            out.append((name, path.stat().st_size if path.is_file() else 0))
        except OSError:
            continue
    return out


def staged(cwd: Path = REPO_ROOT) -> list[tuple[str, int]]:
    out = []
    for name in _names(_git("diff", "--cached", "--name-only", "--diff-filter=AM", "-z", cwd=cwd)):
        size = _git("cat-file", "-s", f":{name}", cwd=cwd).stdout.strip()
        out.append((name, int(size) if size.isdigit() else 0))
    return out


def in_range(spec: str, cwd: Path = REPO_ROOT) -> list[tuple[str, int]]:
    """Files added or changed by the commits of `spec` (A..B), sized as they are at B."""
    end = spec.split("..")[-1] or "HEAD"
    out = []
    for name in _names(_git("diff", "--name-only", "--diff-filter=AM", "-z", spec, cwd=cwd)):
        size = _git("cat-file", "-s", f"{end}:{name}", cwd=cwd).stdout.strip()
        out.append((name, int(size) if size.isdigit() else 0))
    return out


def check(files: list[tuple[str, int]]) -> list[str]:
    problems = []
    for name, size in files:
        reason = judge(name, size)
        if reason:
            problems.append(f"{name}: {reason}")
    return problems


def main(argv: list[str]) -> int:
    if "--staged" in argv:
        files, what = staged(), "staged files"
    elif "--range" in argv:
        spec = argv[argv.index("--range") + 1]
        files, what = in_range(spec), f"files in {spec}"
    else:
        files, what = tracked(), "tracked files"
    problems = check(files)
    if problems:
        print(f"FAIL - the repository guard refused {len(problems)} of the {what}:")
        for line in problems:
            print("  " + line)
        print("Keep build output (image archives, release bundles) outside the repository; stage files by explicit path.")
        return 1
    print(f"OK - no archives and no file over {MAX_BYTES // 1024 // 1024} MB among the {len(files)} {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
