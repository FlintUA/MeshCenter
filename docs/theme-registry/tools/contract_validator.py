#!/usr/bin/env python3
"""Theme-registry contract validator (theme-registry tooling PR 1; group
membership made mode-aware and alias-exclusion-aware in PR 2/Stage 9.1 -
see the note at the bottom of this docstring).

Checks a structural invariant of MeshCenter's family theme layer
(`html[data-theme-family="..."]` blocks in static/ui-kit.css): for each of
the --mc-chat-*, --mc-tab-* and --mc-popover-* token groups, a family
should never redeclare *some* of a group's still-literal (non-aliased)
tokens while leaving others of them on the canonical literal - that
mismatch is the real bug shape (a family's own leaf color sitting next to
whatever the *other* mode's canonical value happened to be, because the
forgotten token was never migrated to a var() alias and never got its own
family override either).

A token that the canonical block already resolves via `var(--mc-<base>)`
doesn't belong in that completeness check at all: it automatically follows
whichever base token the family overrides (or doesn't), with no risk of
the mismatch above - a family is free to add its own override for an
already-aliased token (to deliberately diverge from the alias) without
that counting toward "did you cover the whole group."

Three outcomes per (family variant, group):
  - family overrides ZERO of the group's still-literal tokens ->
    informational only. Normal/expected when a family relies entirely on
    the alias chain for that group (e.g. --mc-chat-bg: var(--mc-bg-
    workspace)) - not a violation.
  - family overrides SOME but not ALL of the group's still-literal tokens
    -> hard violation (exit 1). This is the bug pattern.
  - family overrides ALL of the group's still-literal tokens (or the group
    has zero still-literal tokens for that family's mode) -> clean, no flag.

Mode-aware: --mc-tab-line-active, for example, is a var() alias in the
canonical *light* block but a literal in the canonical *dark* block (an
intentional asymmetry - see static/ui-kit.css's own Stage 9.1 comments).
So the "must be all-or-nothing" token set for a light-mode family (Sharp,
Teal Light) is computed against :root; for a dark-mode family (Gunmetal,
Alpine, Teal Dark), against html[data-theme="dark"]. Family-to-mode
mapping mirrors static/chat.js's WORKSPACE_THEME_FAMILIES (Sharp: light,
Gunmetal/Alpine: dark, Teal: whichever half its own selector already
names).

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

# Tokens deliberately excluded from the "must be all-or-nothing" contract
# even though their name matches a group prefix and they're still a
# canonical literal. --mc-popover-shadow (theme-registry Stage 9.1): a
# shadow's blur/spread/alpha reads as elevation, not family hue - decided
# to stay a shared literal in both canonical blocks with no family needing
# its own value, the same kind of documented, deliberate exception as
# --mc-chat-unread-* (which doesn't need listing here - it's simply absent
# from the dark canonical block entirely, and no family currently
# overrides any of it in light, so it never triggers a violation).
EXCLUDED_FROM_GROUP_CONTRACT = {"--mc-popover-shadow"}

# Fixed-mode families that don't carry [data-theme="..."] in their own
# selector (see static/ui-kit.css's own family-block comments for why:
# WORKSPACE_THEME_FAMILIES gives each a fixed mode instead).
FIXED_FAMILY_MODE = {"sharp": "light", "gunmetal": "dark", "alpine": "dark"}

FAMILY_SELECTOR_RE = re.compile(
    r'html\[data-theme-family="([a-z0-9_-]+)"\]'
    r'(\[data-theme="(dark|light)"\])?'
)
DECL_RE = re.compile(r'(--[\w-]+)\s*:\s*([^;]+);')


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


def canonical_literal_group_tokens(canonical_body: str) -> dict:
    """token-name -> group-prefix, for every --mc-chat-*/--mc-tab-*/
    --mc-popover-* token declared in a canonical block (:root or
    html[data-theme="dark"]) whose OWN value is not itself a var() alias -
    i.e. the tokens that still carry their own literal and therefore still
    need every family (of the matching mode) to either fully cover the
    group or rely on the alias chain, group by group."""
    tokens = {}
    for m in DECL_RE.finditer(canonical_body):
        name, value = m.group(1), m.group(2).strip()
        if name in EXCLUDED_FROM_GROUP_CONTRACT:
            continue
        for prefix in GROUP_PREFIXES:
            if name.startswith(prefix) and not value.startswith("var("):
                tokens[name] = prefix
                break
    return tokens


def family_declared_tokens(body: str) -> set:
    return {m.group(1) for m in DECL_RE.finditer(body)}


def main(argv: list[str]) -> int:
    css_path = Path(argv[0]) if argv else DEFAULT_CSS
    text = css_path.read_text(encoding="utf-8")
    clean = strip_comment_spans(text)

    light_body = None
    dark_body = None
    family_blocks = []  # (variant_label, mode, body, line)
    for selector, body, line in find_top_level_blocks(clean):
        if selector == ":root":
            if light_body is None:
                light_body = body
            continue
        if selector == 'html[data-theme="dark"]':
            if dark_body is None:
                dark_body = body
            continue
        m = FAMILY_SELECTOR_RE.fullmatch(selector)
        if m:
            family_id = m.group(1)
            mode = m.group(3) or FIXED_FAMILY_MODE.get(family_id)
            label = f'{family_id}[{mode}]' if m.group(3) else family_id
            family_blocks.append((label, mode, body, line))

    if light_body is None:
        print(f"error: no :root block found in {css_path}", file=sys.stderr)
        return 2
    if dark_body is None:
        print(f'error: no html[data-theme="dark"] block found in {css_path}', file=sys.stderr)
        return 2
    if not family_blocks:
        print(f"error: no html[data-theme-family=...] blocks found in {css_path}", file=sys.stderr)
        return 2

    light_group_tokens = canonical_literal_group_tokens(light_body)
    dark_group_tokens = canonical_literal_group_tokens(dark_body)

    def groups_for(mode):
        source = light_group_tokens if mode == "light" else dark_group_tokens
        groups = {}
        for name, prefix in source.items():
            groups.setdefault(prefix, set()).add(name)
        return groups

    violations = []
    informational = []
    all_prefixes_seen = set()

    for label, mode, body, line in family_blocks:
        groups = groups_for(mode)
        all_prefixes_seen.update(groups.keys())
        declared = family_declared_tokens(body)
        for prefix, all_tokens in groups.items():
            overridden = declared & all_tokens
            if not overridden:
                informational.append((label, prefix, line))
            elif overridden != all_tokens:
                missing = sorted(all_tokens - overridden)
                violations.append((label, prefix, line, sorted(overridden), missing))

    print(f"Checked {len(family_blocks)} family variant(s) against "
          f"{', '.join(sorted(p.strip('-') for p in all_prefixes_seen)) or '(no still-literal groups)'} "
          f"token group(s) (mode-aware, alias-excluded) from {css_path}.\n")

    if informational:
        print("Informational - zero overrides of the group's still-literal tokens "
              "(expected/normal, relies on the alias chain and/or the group has no "
              "still-literal tokens left for this family's mode):")
        for label, prefix, line in informational:
            print(f"  {label} (line {line}): no {prefix}* overrides")
        print()

    if violations:
        print(f"VIOLATIONS - partial override of a group's still-literal tokens ({len(violations)}):\n")
        for label, prefix, line, overridden, missing in violations:
            print(f"  {label} (line {line}): partially overrides {prefix}* (still-literal tokens only)")
            print(f"    overridden ({len(overridden)}): {', '.join(overridden)}")
            print(f"    missing    ({len(missing)}): {', '.join(missing)}")
            print(f"    -> either override the rest of the group's still-literal tokens, or "
                  f"remove these overrides and rely on the alias chain like the family's other groups.")
        return 1

    print("OK: no family partially overrides a --mc-chat-*/--mc-tab-*/--mc-popover-* "
          "group's still-literal tokens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
