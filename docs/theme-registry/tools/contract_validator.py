#!/usr/bin/env python3
"""Theme-registry contract validator (theme-registry tooling PR 1).

Checks a structural invariant of MeshCenter's family theme layer
(`html[data-theme-family="..."]` blocks in static/ui-kit.css): the
--mc-chat-*, --mc-tab-* and --mc-popover-* token groups are each meant to
be an all-or-nothing unit per family variant. This is exactly the bug
pattern found in Stage 1.1 - a family block redeclared some tokens in a
group but not others, so the family ended up with a mismatched mix of its
own leaf colors and the *other* mode's canonical value for the tokens it
forgot, because the missing tokens still cascade in from :root / the
canonical html[data-theme="dark"] block.

Three outcomes per (family variant, group):
  - family overrides ZERO tokens in the group -> informational only. This
    is the normal, expected shape for most families (they rely on the
    alias chain instead, e.g. --mc-chat-bg: var(--mc-bg-workspace)) - not
    a violation.
  - family overrides SOME but not ALL tokens in the group -> hard
    violation (exit 1). This is the Stage 1.1 pattern.
  - family overrides ALL tokens in the group -> clean, no flag.

Usage:
    python contract_validator.py [<path-to-ui-kit.css>]

Defaults to static/ui-kit.css relative to the repo root.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CSS = REPO_ROOT / "static" / "ui-kit.css"

GROUP_PREFIXES = ("--mc-chat-", "--mc-tab-", "--mc-popover-")

# Matches the family variant selector blocks. Each is a plain, flat
# custom-property declaration block (no nested rules), so a simple
# brace-balanced regex is sufficient - these files don't nest rules
# inside data-theme-family blocks.
FAMILY_SELECTOR_RE = re.compile(
    r'html\[data-theme-family="([a-z0-9_-]+)"\]'
    r'(\[data-theme="(dark|light)"\])?'
)
CUSTOM_PROP_RE = re.compile(r'(--[\w-]+)\s*:')


def strip_comment_spans(text: str) -> str:
    """Blank out /* ... */ contents, preserving newlines (shared logic
    with check_new_hex.py's stripper - keeps line numbers accurate)."""
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


def find_top_level_blocks(text: str):
    """Yield (selector_text, body_text, start_line) for every top-level
    `selector { ... }` block in text. Assumes no nested braces inside a
    block, true for every block this validator cares about. Line numbers
    are computed from character offsets (not incremental tracking) to
    avoid any risk of drift."""
    i = 0
    n = len(text)
    selector_start = 0
    while i < n:
        if text[i] == "{":
            selector = text[selector_start:i]
            depth = 1
            j = i + 1
            body_start = j
            while j < n and depth > 0:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            body = text[body_start:j - 1]
            stripped_selector = selector.strip()
            if stripped_selector:
                sel_offset = selector_start + selector.index(stripped_selector)
                selector_line = text.count("\n", 0, sel_offset) + 1
            yield stripped_selector, body, selector_line
            selector_start = j
            i = j
            continue
        i += 1


def canonical_group_tokens(root_body: str) -> dict:
    """token-name -> group-prefix for every --mc-chat-*/--mc-tab-*/
    --mc-popover-* token declared in the canonical :root block."""
    tokens = {}
    for m in CUSTOM_PROP_RE.finditer(root_body):
        name = m.group(1)
        for prefix in GROUP_PREFIXES:
            if name.startswith(prefix):
                tokens[name] = prefix
                break
    return tokens


def family_declared_tokens(body: str) -> set:
    return {m.group(1) for m in CUSTOM_PROP_RE.finditer(body)}


def main(argv: list[str]) -> int:
    css_path = Path(argv[0]) if argv else DEFAULT_CSS
    text = css_path.read_text(encoding="utf-8")
    clean = strip_comment_spans(text)

    root_body = None
    family_blocks = []  # (variant_label, body, line)
    for selector, body, line in find_top_level_blocks(clean):
        if selector == ":root":
            # First :root block is the canonical light block; later
            # ones (if any) are not - ui-kit.css only has one.
            if root_body is None:
                root_body = body
            continue
        m = FAMILY_SELECTOR_RE.fullmatch(selector)
        if m:
            family_id = m.group(1)
            mode = m.group(3)
            label = f'{family_id}[{mode}]' if mode else family_id
            family_blocks.append((label, body, line))

    if root_body is None:
        print(f"error: no :root block found in {css_path}", file=sys.stderr)
        return 2
    if not family_blocks:
        print(f"error: no html[data-theme-family=...] blocks found in {css_path}", file=sys.stderr)
        return 2

    group_tokens = canonical_group_tokens(root_body)
    groups = {}
    for name, prefix in group_tokens.items():
        groups.setdefault(prefix, set()).add(name)

    violations = []
    informational = []

    for label, body, line in family_blocks:
        declared = family_declared_tokens(body)
        for prefix, all_tokens in groups.items():
            overridden = declared & all_tokens
            if not overridden:
                informational.append((label, prefix, line))
            elif overridden != all_tokens:
                missing = sorted(all_tokens - overridden)
                violations.append((label, prefix, line, sorted(overridden), missing))

    print(f"Checked {len(family_blocks)} family variant(s) against {len(groups)} "
          f"token group(s) ({', '.join(sorted(p.strip('-') for p in groups))}) "
          f"from {css_path}.\n")

    if informational:
        print(f"Informational - zero overrides in group (expected/normal, relies on alias chain):")
        for label, prefix, line in informational:
            print(f"  {label} (line {line}): no {prefix}* overrides")
        print()

    if violations:
        print(f"VIOLATIONS - partial group override ({len(violations)}):\n")
        for label, prefix, line, overridden, missing in violations:
            print(f"  {label} (line {line}): partially overrides {prefix}*")
            print(f"    overridden ({len(overridden)}): {', '.join(overridden)}")
            print(f"    missing    ({len(missing)}): {', '.join(missing)}")
            print(f"    -> either override the rest of the group, or remove these overrides "
                  f"and rely on the alias chain like the family's other groups.")
        return 1

    print("OK: no family partially overrides a --mc-chat-*/--mc-tab-*/--mc-popover-* group.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
