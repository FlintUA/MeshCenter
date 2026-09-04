#!/usr/bin/env python3
"""Full (not sampled) inventory of every selector that *consumes* a --mc-*
design token via var(--mc-*) - theme-registry tooling PR 1, deliverable 3.

Unlike a spot-check or grep-by-hand, this walks every declaration in the
five theming CSS files and records, per token, the complete list of
(file, line, selector) sites that reference it - meant to let a reviewer
answer questions like "what are all the roles --mc-bg-subtle plays?" or
"does --mc-border-focus get consumed anywhere inside a family block?"
without trusting a sampled/partial grep.

Usage:
    python token_inventory.py [--token <name>] [--out <path>]

--token <name>   only print/write the given token's consumer list (e.g.
                 --token mc-bg-subtle or --mc-bg-subtle, either form).
--out <path>     write the full inventory as Markdown to <path> instead of
                 (or in addition to, with --stdout) stdout. Defaults to
                 docs/theme-registry/token-inventory.md when no --token
                 filter is given and no --out is passed.
--stdout         also print to stdout when --out is used.

With no arguments, writes the full inventory to
docs/theme-registry/token-inventory.md and prints a short summary.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "docs" / "theme-registry" / "token-inventory.md"

THEME_CSS_FILES = [
    "static/ui-kit.css",
    "static/style-part1.css",
    "static/style-part2.css",
    "static/style-part3.css",
    "static/style-part4.css",
]

VAR_TOKEN_RE = re.compile(r"var\(\s*(--mc-[\w-]+)")
AT_RULE_RE = re.compile(r"^@[\w-]+")


def strip_comment_spans(text: str) -> str:
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


def iter_leaf_rules(text: str):
    """Yield (context_label, body_text, body_start_line) for every rule
    whose body is a flat declaration list (not itself containing nested
    rules) - i.e. every normal selector block, skipping @-rule wrappers
    (@media/@supports/...) as bodies themselves but still descending into
    them to find the real selector blocks they contain. `context_label`
    includes the enclosing @-rule prelude(s) for readability, e.g.
    '@media (max-width: 600px) > .foo'."""
    stack = []  # list of prelude strings (selectors or @-rule preludes)
    i = 0
    n = len(text)
    prelude_start = 0
    while i < n:
        ch = text[i]
        if ch == "{":
            prelude = text[prelude_start:i].strip()
            body_start = i + 1
            depth = 1
            j = body_start
            while j < n and depth > 0:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                j += 1
            body = text[body_start:j - 1]
            if AT_RULE_RE.match(prelude):
                # Wrapper: recurse into its body to find real rule blocks,
                # prefixing their context with this at-rule's prelude.
                stack.append(prelude)
                for label, inner_body, inner_line in iter_leaf_rules_with_offset(body, body_start, text):
                    yield f"{prelude} > {label}", inner_body, inner_line
                stack.pop()
            else:
                body_line = text.count("\n", 0, body_start) + 1
                yield prelude, body, body_line
            prelude_start = j
            i = j
            continue
        i += 1


def iter_leaf_rules_with_offset(body: str, offset: int, full_text: str):
    """Like iter_leaf_rules but for a nested body extracted from a larger
    string - line numbers are computed against the *original* full_text
    via the given character offset, so they stay accurate for @media etc."""
    stack_local = []
    i = 0
    n = len(body)
    prelude_start = 0
    while i < n:
        ch = body[i]
        if ch == "{":
            prelude = body[prelude_start:i].strip()
            body_start = i + 1
            depth = 1
            j = body_start
            while j < n and depth > 0:
                if body[j] == "{":
                    depth += 1
                elif body[j] == "}":
                    depth -= 1
                j += 1
            inner_body = body[body_start:j - 1]
            if AT_RULE_RE.match(prelude):
                stack_local.append(prelude)
                for label, ib, il in iter_leaf_rules_with_offset(inner_body, offset + body_start, full_text):
                    yield f"{prelude} > {label}", ib, il
                stack_local.pop()
            else:
                line = full_text.count("\n", 0, offset + body_start) + 1
                yield prelude, inner_body, line
            prelude_start = j
            i = j
            continue
        i += 1


def build_inventory():
    """Returns {token_name: [(file, line, selector), ...]} sorted."""
    inventory: dict[str, list[tuple[str, int, str]]] = {}
    for rel_path in THEME_CSS_FILES:
        path = REPO_ROOT / rel_path
        if not path.exists():
            print(f"warning: {rel_path} not found, skipping", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8")
        clean = strip_comment_spans(text)
        for selector, body, line in iter_leaf_rules(clean):
            tokens_seen_in_rule = set()
            for m in VAR_TOKEN_RE.finditer(body):
                tokens_seen_in_rule.add(m.group(1))
            for token in tokens_seen_in_rule:
                inventory.setdefault(token, []).append((rel_path, line, selector))
    for token in inventory:
        inventory[token].sort(key=lambda row: (row[0], row[1]))
    return dict(sorted(inventory.items()))


def render_markdown(inventory: dict) -> str:
    lines = [
        "# MeshCenter theme token consumer inventory",
        "",
        "Auto-generated by `docs/theme-registry/tools/token_inventory.py` "
        "(theme-registry tooling PR 1). Full (not sampled) list of every "
        "selector that consumes each `--mc-*` token via `var(--mc-*)` "
        "across the five theming CSS files "
        f"({', '.join(THEME_CSS_FILES)}). Regenerate after any CSS change "
        "that touches token usage - this file is a snapshot, not derived "
        "at build time.",
        "",
        f"Total tokens with at least one consumer: {len(inventory)}",
        "",
    ]
    for token, sites in inventory.items():
        lines.append(f"## `{token}` ({len(sites)} consumer{'s' if len(sites) != 1 else ''})")
        lines.append("")
        for fname, lineno, selector in sites:
            lines.append(f"- `{fname}:{lineno}` — `{selector}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    token_filter = None
    out_path = None
    also_stdout = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--token" and i + 1 < len(argv):
            token_filter = argv[i + 1]
            if not token_filter.startswith("--"):
                token_filter = "--" + token_filter.lstrip("-")
            i += 2
        elif a == "--out" and i + 1 < len(argv):
            out_path = Path(argv[i + 1])
            i += 2
        elif a == "--stdout":
            also_stdout = True
            i += 1
        else:
            print(f"unrecognized argument: {a}", file=sys.stderr)
            return 2

    inventory = build_inventory()

    if token_filter is not None:
        sites = inventory.get(token_filter, [])
        print(f"{token_filter}: {len(sites)} consumer(s)")
        for fname, lineno, selector in sites:
            print(f"  {fname}:{lineno}  {selector}")
        return 0

    if out_path is None:
        out_path = DEFAULT_OUT
    md = render_markdown(inventory)
    out_path.write_text(md, encoding="utf-8")
    total_sites = sum(len(v) for v in inventory.values())
    print(f"Wrote {out_path.relative_to(REPO_ROOT)}: {len(inventory)} tokens, "
          f"{total_sites} consumer sites total.")
    if also_stdout:
        print()
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
