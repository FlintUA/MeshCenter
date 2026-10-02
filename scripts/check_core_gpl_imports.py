#!/usr/bin/env python3
"""GPLv3 license-boundary check (H1-D).

Three independent checks, each guarding a different way GPL-licensed code
could leak into Core's own MIT-licensed process (see CLAUDE.md's "GPLv3
process isolation" section and THIRD_PARTY_NOTICES.md for the full
reasoning):

1. `meshtastic` (GPLv3) imported anywhere outside adapters/meshtastic/ -
   the original check (Task 48's own process-isolation work). Previously
   an inline shell step in .github/workflows/ci.yml; moved here (H1-D)
   once a second GPL package needed the exact same shape, which would
   have made that shell step unwieldy.
2. `linuxpy`/`v4l2py` (GPL-3.0-or-later) imported anywhere in Core -
   CAM-1 (audit review finding F10) removed this dependency entirely in
   favor of driving `ffmpeg`/`v4l2-ctl` as external subprocesses; this
   check guards against it ever coming back.
3. `meshtastic`, `linuxpy` or `v4l2py` listed in the root
   requirements.txt - these must only ever appear in
   adapters/meshtastic/requirements.txt (meshtastic's own, separate
   venv/process) or not at all (linuxpy/v4l2py, post-CAM-1).

`git ls-files`, not a filesystem walk, for the import scans - scopes every
check to tracked sources only, so it can never be polluted by an untracked
local scratch/worktree directory (the original check's own long-standing
reasoning: this repo's own .claude/worktrees/ has held pre-Task-48 code
with real, now-historical `from meshtastic import SerialInterface` lines -
gitignored, so `git ls-files` never sees them, but a raw `grep -r .`
would have and failed this check on files that were never part of the
repo).

Import patterns are anchored to actual import statements (optional
leading whitespace, `\b` word boundary after the package name) - an
unanchored substring grep for "import meshtastic"/"from meshtastic" also
matches "import meshtastic_transport" (a first-party module name that
happens to start with "meshtastic", see meshsrv/meshtastic_transport.py)
and dozens of comments/docstrings/test mock class names across this
codebase that mention SerialInterface/BLEInterface in prose - confirmed
against the original check's own commit message: an unanchored version
produced ~45 false-positive hits, none of them a real violation.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"

# Directories allowed to reference these GPL packages directly - each
# one's own adapter/smoke-test code, never Core itself.
ALLOWED_PREFIXES = ("adapters/meshtastic/", "tests/", "scripts/_smoke_test_")

MESHTASTIC_IMPORT_RE = re.compile(r"^\s*(import meshtastic\b|from meshtastic\b)")
LINUXPY_V4L2PY_IMPORT_RE = re.compile(
    r"^\s*(import (?:linuxpy|v4l2py)\b|from (?:linuxpy|v4l2py)\b)"
)
# Matches a requirements.txt line naming one of these packages as the
# line's OWN requirement (not merely mentioned in a comment) - anchored
# at the start of the (already comment-stripped, already-stripped) line,
# followed by a version specifier character or end of line, so it can
# never match an unrelated package whose name happens to start the same
# way (there is none among PyPI's meshtastic/linuxpy/v4l2py today, but
# the anchor costs nothing and matches this script's own import-pattern
# discipline above).
REQUIREMENTS_FORBIDDEN_RE = re.compile(
    r"^(meshtastic|linuxpy|v4l2py)\s*([=<>!~;\[]|$)", re.IGNORECASE
)


def _is_allowed(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def _tracked_python_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in output.splitlines() if line.strip()]


def _scan_for_import(pattern: "re.Pattern[str]") -> list[str]:
    hits = []
    for path in _tracked_python_files():
        if _is_allowed(path):
            continue
        try:
            text = (REPO_ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.match(line):
                hits.append(f"{path}:{lineno}: {line.strip()}")
    return hits


def check_meshtastic_imports() -> list[str]:
    return _scan_for_import(MESHTASTIC_IMPORT_RE)


def check_linuxpy_v4l2py_imports() -> list[str]:
    return _scan_for_import(LINUXPY_V4L2PY_IMPORT_RE)


def check_requirements_txt() -> list[str]:
    if not REQUIREMENTS_TXT.exists():
        return []
    hits = []
    for lineno, raw_line in enumerate(REQUIREMENTS_TXT.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if REQUIREMENTS_FORBIDDEN_RE.match(stripped):
            hits.append(f"requirements.txt:{lineno}: {raw_line.strip()}")
    return hits


def main() -> int:
    failures: list[tuple[str, list[str]]] = []

    meshtastic_hits = check_meshtastic_imports()
    if meshtastic_hits:
        failures.append(("meshtastic imported outside adapters/meshtastic/", meshtastic_hits))

    linuxpy_hits = check_linuxpy_v4l2py_imports()
    if linuxpy_hits:
        failures.append(("linuxpy/v4l2py imported in Core (F10 - removed in CAM-1)", linuxpy_hits))

    requirements_hits = check_requirements_txt()
    if requirements_hits:
        failures.append(("GPL package listed in root requirements.txt", requirements_hits))

    if not failures:
        print("OK - no GPLv3 license-boundary violations found")
        return 0

    print("FAIL - GPLv3 license-boundary violation(s) found:\n", file=sys.stderr)
    for title, hits in failures:
        print(f"{title}:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        print(file=sys.stderr)
    print(
        "See CLAUDE.md's \"GPLv3 process isolation\" section and "
        "THIRD_PARTY_NOTICES.md for why this boundary exists.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
