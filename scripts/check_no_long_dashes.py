#!/usr/bin/env python3
"""Style guard (DASH1): no em dash (U+2014) and no en dash (U+2013) in the project's own texts.

The owner's rule: use a plain hyphen-minus "-" in docs, release notes, UI strings, server
messages and code comments. The characters are written as \\u escapes in this file so the
guard does not trip over itself.

Scans every tracked UTF-8 text file. Skipped on purpose (see ALLOW_PATHS / ALLOW_LINES):
licence texts, vendored/minified third-party files, test fixtures that are real data
(CLI/radio output) and the few lines where the dash is the data under test. Anything else
fails with file:line and the rule. Dependency-free (git + stdlib), same class as
check_i18n.py / check_doc_links.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DASHES = ("\u2014", "\u2013")
NAMES = {"\u2014": "em dash U+2014", "\u2013": "en dash U+2013"}

# Path prefixes whose files are never checked (third-party text, fixtures, binaries).
ALLOW_PATHS = (
    "licenses/",                          # licence texts of vendored libraries
    "tests/fixtures/",                    # real CLI/radio output used as test data
    "docs/theme-registry/tools/fonts/",   # font licence
    "docs/images/",
)
# File names (any directory) that are licence texts or vendored/minified bundles.
ALLOW_NAMES_PREFIX = ("LICENSE",)
ALLOW_NAME_PARTS = (".min.",)

# (path, exact line substring): a dash that is the data under test and must stay.
ALLOW_LINES = (
    ("tests/test_mime_allowlist.py", "caf\u00e9 \u2014 \u65e5\u672c\u8a9e"),
)

RULE = "use a plain hyphen-minus '-' (see CLAUDE.md, Writing style)"


def tracked_files(root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=str(root), capture_output=True, text=True, check=True).stdout
    return [line for line in out.split("\n") if line]


def is_allowed_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith(ALLOW_PATHS)
        or name.startswith(ALLOW_NAMES_PREFIX)
        or any(part in name for part in ALLOW_NAME_PARTS)
    )


def find_violations(root: Path = REPO_ROOT, files: list[str] | None = None) -> list[tuple[str, int, str, str]]:
    """(path, line number, dash name, line text) for every disallowed dash."""
    found = []
    for path in files if files is not None else tracked_files(root):
        if is_allowed_path(path):
            continue
        try:
            raw = (root / path).read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue  # binary
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not any(d in text for d in DASHES):
            continue
        allowed = [needle for p, needle in ALLOW_LINES if p == path]
        for number, line in enumerate(text.split("\n"), start=1):
            for dash in DASHES:
                if dash in line and not any(needle in line for needle in allowed):
                    found.append((path, number, NAMES[dash], line.strip()[:120]))
                    break
    return found


def main() -> int:
    violations = find_violations()
    if not violations:
        print("OK - no em/en dashes in the project's own texts")
        return 0
    print(f"FAIL - {len(violations)} line(s) contain an em or en dash; {RULE}:\n")
    for path, number, name, text in violations[:80]:
        print(f"  {path}:{number}: {name}: {text}")
    if len(violations) > 80:
        print(f"  ... and {len(violations) - 80} more")
    print("\nIf a dash is genuinely data (a fixture, a licence text), add it to the allowlist in "
          "scripts/check_no_long_dashes.py with a reason in the PR.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
