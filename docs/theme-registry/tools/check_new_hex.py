#!/usr/bin/env python3
"""Flag raw 6-digit HEX literals introduced into MeshCenter's theming CSS
that aren't already accounted for in docs/theme-registry/raw-hex-audit.md's
registry (hex_registry.json, generated alongside it).

Meant to gate theme-registry stage PRs: once later stages start migrating
raw HEX to `--mc-*` tokens, any *new* raw HEX literal showing up in a diff is
either a missed token opportunity or an undocumented one-off that the
registry needs to know about.

Two modes:

1. Check specific file(s) as they stand on disk:
     python check_new_hex.py static/style-part4.css static/ui-kit.css

2. Check a git diff (unstaged, or against a ref) for newly *added* HEX
   literals only - pass --diff [<git-diff-args...>]. With no extra args this
   is `git diff` (unstaged changes); pass e.g. --diff HEAD~1 to check a
   specific range. Only lines beginning with '+' (added lines, not the
   '+++' file header) are scanned, so removed/untouched HEX is ignored.

Exit status is 1 if any un-registered HEX literal is found (for CI use),
0 otherwise.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "hex_registry.json"

HEX_RE = re.compile(r"#([0-9a-fA-F]{6})\b")

THEME_CSS_FILES = {
    "static/style-part1.css",
    "static/style-part2.css",
    "static/style-part3.css",
    "static/style-part4.css",
    "static/ui-kit.css",
}


def load_known_hex() -> set[str]:
    if not REGISTRY_PATH.exists():
        print(f"warning: registry not found at {REGISTRY_PATH}; treating all HEX as new", file=sys.stderr)
        return set()
    rows = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {row["value"].upper() for row in rows}


def strip_comment_spans(text: str) -> str:
    """Blank out /* ... */ comment contents so HEX inside design-note comments
    doesn't get flagged as a new live declaration.

    Preserves embedded newlines as newlines (not spaces) so line numbers
    for anything after a multi-line comment stay accurate in whole-file
    mode's check_files() - blanking them out entirely used to collapse a
    multi-line comment into effectively zero newlines once the caller
    splitlines()'d the result, silently shifting every reported line
    number after it by the comment's own line count. Confirmed live on
    two real PRs (theme-registry Stage 4.2/#188, Stage 5.1/#190) before
    being traced to this function specifically in Stage 5.2.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            end = j + 2 if j != -1 else n
            span = text[i:end]
            out.append("".join("\n" if ch == "\n" else " " for ch in span))
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def check_files(paths: list[Path], known: set[str]) -> list[tuple[str, int, str]]:
    findings = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        clean = strip_comment_spans(text)
        for lineno, line in enumerate(clean.splitlines(), start=1):
            for m in HEX_RE.finditer(line):
                val = m.group(1).upper()
                if val not in known:
                    findings.append((str(path), lineno, val))
    return findings


def check_diff(diff_args: list[str], known: set[str]) -> list[tuple[str, str, str]]:
    cmd = ["git", "diff", "--unified=0"] + diff_args
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    )
    findings = []
    current_file = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            fname = line[4:].strip()
            if fname.startswith("b/"):
                fname = fname[2:]
            current_file = fname
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if current_file is None or current_file.replace("\\", "/") not in THEME_CSS_FILES:
            continue
        added_line = strip_comment_spans(line[1:])
        for m in HEX_RE.finditer(added_line):
            val = m.group(1).upper()
            if val not in known:
                findings.append((current_file, added_line.strip()[:120], val))
    return findings


def main(argv: list[str]) -> int:
    known = load_known_hex()

    if argv and argv[0] == "--diff":
        diff_args = argv[1:]
        findings = check_diff(diff_args, known)
        if not findings:
            print(f"OK: no unregistered HEX literals added (checked against {len(known)} known values).")
            return 0
        print(f"Found {len(findings)} unregistered HEX literal(s) in the diff:\n")
        for fname, context, val in findings:
            print(f"  {fname}: #{val}\n    {context}")
        print(
            "\nEach of these is either a new theme color that should become a "
            "--mc-* token instead of raw HEX, or a legitimate new one-off that "
            "needs to be added to docs/theme-registry/raw-hex-audit.md's "
            "registry. Re-run the audit generator (.theme_stage0_scratch/"
            "classify_hex.py + render_audit_md.py, or its successor) to update "
            "hex_registry.json once triaged."
        )
        return 1

    if not argv:
        print(__doc__)
        return 1

    paths = [Path(p) for p in argv]
    findings = check_files(paths, known)
    if not findings:
        print(f"OK: no unregistered HEX literals found in {len(paths)} file(s) (checked against {len(known)} known values).")
        return 0
    print(f"Found {len(findings)} unregistered HEX literal(s):\n")
    for fname, lineno, val in findings:
        print(f"  {fname}:{lineno}  #{val}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
