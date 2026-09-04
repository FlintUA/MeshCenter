#!/usr/bin/env python3
"""Flag raw 6-digit HEX literals introduced into MeshCenter's theming CSS
that aren't already accounted for in docs/theme-registry/raw-hex-audit.md's
registry (hex_registry.json, generated alongside it) - and, separately (as
of theme-registry tooling PR 1), flag raw HEX used directly as an ordinary
property VALUE instead of going through a --mc-* token's var() reference,
regardless of whether that HEX value happens to already be registered.

Meant to gate theme-registry stage PRs: once later stages start migrating
raw HEX to `--mc-*` tokens, any *new* raw HEX literal showing up in a diff is
either a missed token opportunity or an undocumented one-off that the
registry needs to know about.

Two independent checks are run, and reported separately:

1. UNREGISTERED HEX - any raw HEX literal (wherever it appears - inside a
   `--mc-*: #value;` token declaration or an ordinary property value) whose
   value isn't already in hex_registry.json. This is the original check.

2. RAW HEX IN PROPERTY VALUE - any raw HEX literal that appears in an
   *ordinary* property's value (i.e. the declaration's property name does
   not start with `--`) *outside* of a var(...) call - e.g.
   `.video-frame-wrap { border-color: #445164; }` instead of
   `border-color: var(--mc-workspace-border);`. This fires regardless of
   whether the HEX value is already registered, because the bug this
   catches (Stage 1.4) is "value bypasses the token system entirely", not
   "value is unknown". A HEX literal used only as a var() fallback (e.g.
   `var(--mc-x, #445164)`) does not count - the token is still being
   consulted, so that's a different (usually deliberate) pattern.

Two modes:

1. Check specific file(s) as they stand on disk:
     python check_new_hex.py static/style-part4.css static/ui-kit.css

2. Check a git diff (unstaged, or against a ref) for newly *added* HEX
   literals only - pass --diff [<git-diff-args...>]. With no extra args this
   is `git diff` (unstaged changes); pass e.g. --diff HEAD~1 to check a
   specific range, or --diff --staged for the index. Only lines actually
   added by the diff are scanned (removed/untouched HEX is ignored) - but
   unlike the original implementation, comment-awareness is computed from
   the *whole* new-side file (not line-by-line under --unified=0), so a
   multi-line /* ... */ comment whose opening `/*` sits outside the diffed
   hunk no longer produces a false positive on a HEX literal inside it.

Exit status is 1 if either check finds something (for CI use), 0 otherwise.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "hex_registry.json"

HEX_RE = re.compile(r"#([0-9a-fA-F]{6})\b")
VAR_CALL_RE = re.compile(r"var\([^()]*\)")
# One CSS custom-property or ordinary declaration: `name: value;`. These
# theming files don't nest declarations inside declarations, so a
# non-brace value up to the next `;` is always a full declaration.
DECL_RE = re.compile(r"([\w-]+)\s*:\s*([^;{}]*);")

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


def hex_outside_var_calls(value: str):
    """Yield HEX matches in `value` that are not inside any var(...) call
    (i.e. a fallback like var(--mc-x, #445164) is excluded)."""
    var_spans = [m.span() for m in VAR_CALL_RE.finditer(value)]
    for m in HEX_RE.finditer(value):
        if not any(start <= m.start() < end for start, end in var_spans):
            yield m


def scan_text(text: str, known: set[str]):
    """Scan a whole (already comment-stripped) file's text for both finding
    categories. Returns (unregistered, raw_in_value), each a list of
    (lineno, ...) tuples."""
    unregistered = []
    raw_in_value = []
    for dm in DECL_RE.finditer(text):
        prop, value = dm.group(1), dm.group(2)
        value_start = dm.start(2)
        is_token_decl = prop.startswith("--")
        for hm in HEX_RE.finditer(value):
            val = hm.group(1).upper()
            if val not in known:
                lineno = text.count("\n", 0, value_start + hm.start()) + 1
                unregistered.append((lineno, val))
        if not is_token_decl:
            for hm in hex_outside_var_calls(value):
                val = hm.group(1).upper()
                lineno = text.count("\n", 0, value_start + hm.start()) + 1
                raw_in_value.append((lineno, prop, val, value.strip()[:100]))
    return unregistered, raw_in_value


def check_files(paths: list[Path], known: set[str]):
    unregistered_all = []
    raw_in_value_all = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        clean = strip_comment_spans(text)
        unregistered, raw_in_value = scan_text(clean, known)
        for lineno, val in unregistered:
            unregistered_all.append((str(path), lineno, val))
        for lineno, prop, val, snippet in raw_in_value:
            raw_in_value_all.append((str(path), lineno, prop, val, snippet))
    return unregistered_all, raw_in_value_all


def parse_added_line_ranges(diff_stdout: str) -> dict[str, set[int]]:
    """Parse `git diff --unified=0` output into {file: {added line numbers}},
    using only the hunk headers (@@ -a,b +c,d @@) - not line content - so
    this doesn't need per-line comment handling at all."""
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    added: dict[str, set[int]] = {}
    current_file = None
    for line in diff_stdout.splitlines():
        if line.startswith("+++ "):
            fname = line[4:].strip()
            if fname.startswith("b/"):
                fname = fname[2:]
            current_file = fname
            continue
        m = hunk_re.match(line)
        if m and current_file is not None:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                continue
            added.setdefault(current_file.replace("\\", "/"), set()).update(
                range(start, start + count)
            )
    return added


def determine_new_side_ref(diff_args: list[str]):
    """Returns None for 'working tree', 'INDEX' for --staged/--cached, or a
    git ref string (the second positional ref, for a two-ref diff)."""
    refs = []
    for a in diff_args:
        if a == "--":
            break
        if a in ("--staged", "--cached"):
            return "INDEX"
        if not a.startswith("-"):
            refs.append(a)
    if len(refs) >= 2:
        return refs[1]
    return None


def read_new_side(path_str: str, new_side_ref) -> str | None:
    try:
        if new_side_ref is None:
            return (REPO_ROOT / path_str).read_text(encoding="utf-8")
        if new_side_ref == "INDEX":
            cmd = ["git", "show", f":{path_str}"]
        else:
            cmd = ["git", "show", f"{new_side_ref}:{path_str}"]
        result = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True,
            check=True, encoding="utf-8", errors="replace",
        )
        return result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return None


def check_diff(diff_args: list[str], known: set[str]):
    cmd = ["git", "diff", "--unified=0"] + diff_args
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    )
    added_by_file = parse_added_line_ranges(result.stdout)
    new_side_ref = determine_new_side_ref(diff_args)

    unregistered_all = []
    raw_in_value_all = []
    for fname, added_lines in added_by_file.items():
        if fname not in THEME_CSS_FILES:
            continue
        text = read_new_side(fname, new_side_ref)
        if text is None:
            print(
                f"warning: could not read new-side content for {fname} "
                f"(deleted, or an unreadable ref) - skipping.",
                file=sys.stderr,
            )
            continue
        clean = strip_comment_spans(text)
        unregistered, raw_in_value = scan_text(clean, known)
        for lineno, val in unregistered:
            if lineno in added_lines:
                unregistered_all.append((fname, lineno, val))
        for lineno, prop, val, snippet in raw_in_value:
            if lineno in added_lines:
                raw_in_value_all.append((fname, lineno, prop, val, snippet))
    return unregistered_all, raw_in_value_all


def print_report(unregistered, raw_in_value, known_count: int, mode_label: str) -> int:
    if not unregistered and not raw_in_value:
        print(f"OK: no unregistered HEX literals and no raw-HEX-in-value found {mode_label} "
              f"(checked against {known_count} known values).")
        return 0

    if unregistered:
        print(f"Found {len(unregistered)} unregistered HEX literal(s) {mode_label}:\n")
        for row in unregistered:
            if len(row) == 3:
                fname, lineno, val = row
                print(f"  {fname}:{lineno}  #{val}")
        print(
            "\nEach of these is either a new theme color that should become a "
            "--mc-* token instead of raw HEX, or a legitimate new one-off that "
            "needs to be added to docs/theme-registry/raw-hex-audit.md's "
            "registry. Re-run the audit generator (.theme_stage0_scratch/"
            "classify_hex.py + render_audit_md.py, or its successor) to update "
            "hex_registry.json once triaged.\n"
        )

    if raw_in_value:
        print(f"Found {len(raw_in_value)} raw HEX literal(s) in property values (not via var()) {mode_label}:\n")
        for fname, lineno, prop, val, snippet in raw_in_value:
            print(f"  {fname}:{lineno}  {prop}: {snippet}")
        print(
            "\nEach of these bypasses the --mc-* token system in an ordinary "
            "property value - even if #value is already a known/registered "
            "color, it should most likely be var(--mc-<token>) instead. This "
            "is the Stage 1.4 bug pattern. If it's genuinely a one-off with "
            "no matching token, leave a comment saying so.\n"
        )

    return 1


def main(argv: list[str]) -> int:
    known = load_known_hex()

    if argv and argv[0] == "--diff":
        diff_args = argv[1:]
        unregistered, raw_in_value = check_diff(diff_args, known)
        return print_report(unregistered, raw_in_value, len(known), "in the diff")

    if not argv:
        print(__doc__)
        return 1

    paths = [Path(p) for p in argv]
    unregistered, raw_in_value = check_files(paths, known)
    return print_report(unregistered, raw_in_value, len(known), f"in {len(paths)} file(s)")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
