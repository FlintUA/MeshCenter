#!/usr/bin/env python3
"""XSS remediation ratchet (H2-D, F1.3): no NEW inline event-handler attribute
built from a template-literal interpolation may land in `static/*.js`, and the
allowlist of pre-existing ones (seeded 2026-10-05 from the H2-D audit, PR #333)
can only shrink, never grow or go stale.

Background: `onclick="fn('${value}')"` built from a JS template literal is the
exact shape F1.1 (#311) found real bugs in (a value containing a stray quote
can break out of the attribute, or - the inverse case - break out of the
nested JS string once the HTML parser decodes the attribute first). H2-D's own
audit (see `docs/security/xss-sink-inventory.md`) found zero *exploitable*
sinks of this shape in the current codebase - every value reaching one today
is either escaped correctly or format-constrained server-side - but inline
handlers are also what blocks a real Content-Security-Policy (F1.4, not done
here) from ever dropping `unsafe-inline`. Per review decision (PR #333): ship
the ratchet now rather than converting all 65 remaining sites in one PR;
convert the rest opportunistically, whenever that render code is touched for
an unrelated reason, deleting that site's allowlist entry in the same PR.

Matching: `(?<![\\w-])on[a-z]+=["'][^"']*\\$\\{` - an attribute name starting
with "on" (word-boundary guarded so `data-contact="..."` doesn't spuriously
match the "on" inside "contact"), whose value contains a template-literal
`${`. Comments (`//...` to end of line, `/* ... */` blocks) are stripped from
each file's text before matching, since a few real sites in this codebase are
quoted *inside* a code comment explaining the escaping rules, not live markup.

Allowlist keying: by (file, exact matched snippet) in
`scripts/inline_handler_allowlist.json`, not by line number - line numbers
drift with every unrelated edit, which would make this check noisy rather
than useful. A snippet is the full matched text from `on` through the
attribute's closing quote. This is deliberately loose about exact occurrence
*counts* (if a snippet appears twice in a file and the allowlist has one
matching entry, both occurrences are treated as covered) - the ratchet's job
is to catch a genuinely NEW/different inline handler, not to count duplicates
precisely.

Two failure modes:
  - A real match in a scanned file that isn't covered by any allowlist entry
    for that file: a NEW inline handler - not allowed, convert it to
    data-chat-action (chat.js/chat-map.js/chat-updates-security.js/media.js)
    or data-files-action (files.js) + a delegated listener instead.
  - An allowlist entry whose snippet no longer appears in its file: STALE -
    that site was already converted (or the code changed) and nobody removed
    the entry. Remove it - the allowlist must only ever shrink.
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
ALLOWLIST_PATH = Path(__file__).parent / 'inline_handler_allowlist.json'

SCANNED_FILES = [
    'static/chat.js',
    'static/chat-map.js',
    'static/chat-updates-security.js',
    'static/files.js',
    'static/chat-telemetry.js',
    'static/media.js',
]

# Word-boundary guarded so "data-contact=" doesn't match "on" inside
# "contact" (a real false positive hit during the H2-D audit). Two separate
# alternatives, not one pattern with a shared negated class - a double-quoted
# attribute's value almost always contains single-quoted JS string literals
# (and vice versa for the one site using a single-quoted attribute), so the
# excluded-character class must match only the attribute's OWN delimiter,
# not both quote characters at once.
HANDLER_RE = re.compile(
    r'(?<![\w-])on[a-z]+="([^"]*\$\{[^"]*)"'
    r"|(?<![\w-])on[a-z]+='([^']*\$\{[^']*)'"
)

LINE_COMMENT_RE = re.compile(r'//.*$', re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def strip_comments(source: str) -> str:
    """Good enough for this check, not a real JS parser: a `//` or `/* */`
    inside a string literal would be mishandled, but none of the 6 scanned
    files use that inside a line containing an on*= handler - verified by
    hand during the H2-D audit, not algorithmically guaranteed."""
    return LINE_COMMENT_RE.sub('', BLOCK_COMMENT_RE.sub('', source))


def find_handlers(text: str) -> set[str]:
    stripped = strip_comments(text)
    return {m.group(0) for m in HANDLER_RE.finditer(stripped)}


def load_allowlist() -> list[dict]:
    if not ALLOWLIST_PATH.exists():
        return []
    return json.loads(ALLOWLIST_PATH.read_text(encoding='utf-8'))


def main() -> int:
    allowlist = load_allowlist()
    allowed_by_file: dict[str, set[str]] = {}
    for entry in allowlist:
        allowed_by_file.setdefault(entry['file'], set()).add(entry['snippet'])

    failures: list[str] = []

    for rel_path in SCANNED_FILES:
        path = REPO_ROOT / rel_path
        if not path.exists():
            continue
        text = path.read_text(encoding='utf-8')
        found = find_handlers(text)
        allowed = allowed_by_file.get(rel_path, set())

        for snippet in sorted(found - allowed):
            failures.append(
                f"{rel_path}: NEW inline handler not in the allowlist:\n"
                f"      {snippet[:140]}\n"
                f"    Convert it to data-chat-action/data-files-action + a delegated "
                f"listener instead of adding it to scripts/inline_handler_allowlist.json."
            )

        for snippet in sorted(allowed - found):
            failures.append(
                f"{rel_path}: STALE allowlist entry, no longer found in this file:\n"
                f"      {snippet[:140]}\n"
                f"    Remove it from scripts/inline_handler_allowlist.json - the "
                f"allowlist must only ever shrink."
            )

    if not failures:
        total = sum(len(v) for v in allowed_by_file.values())
        print(f'OK - no new inline handlers, allowlist ({total} entries) is current')
        return 0

    print(f'FAIL - {len(failures)} issue(s):\n', file=sys.stderr)
    for failure in failures:
        print(f'  - {failure}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
