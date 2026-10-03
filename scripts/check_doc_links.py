#!/usr/bin/env python3
"""CI check (H2-A4): every relative markdown link in the docs this project
actively maintains actually resolves to a real file AND, when it carries a
`#anchor`, to a real heading in that file - a cheap, dependency-free check
against the exact regression class a big docs restructure (like H2-A's own
README split into docs/User_Guide.md/docs/architecture/Architecture.md/
docs/BACKEND_API.md) can introduce: a link that pointed at a real file or
heading before the restructure and silently rots after it. H2-A's own PR
review caught exactly this - three `#N-section-name` anchors left pointing
at a heading number that had since shifted during the restructure - which is
why anchor validation exists here now, not just file-existence.

Scope is CHECKED_DOCS (an explicit list, same reasoning as
check_api_docs.py's DOCS_TO_CHECK - this project's docs/ tree also holds
ADRs and point-in-time audit/handoff snapshots that aren't actively
maintained, so a broad `docs/**` glob would flag pre-existing drift this
check was never meant to police) plus every top-level *.md file (README,
INSTALL, CLAUDE, THIRD_PARTY_NOTICES, KNOWN_ISSUES, ...), since those are
few enough, and important enough, to just always check.

Only relative links are checked - `[text](path)`, `[text](path#anchor)`, or
a same-page `[text](#anchor)` - where `path` doesn't start with a URL
scheme (`http://`, `mailto:`, ...). External links are deliberately out of
scope - that's a network check, not a repo-consistency one.

Anchor validation reimplements GitHub's own heading-to-anchor slug rule
(lowercase; drop anything that isn't a letter, digit, space, hyphen or
underscore - this also drops emoji and punctuation like the colon/apostrophe
in "License boundary: why there's a subprocess at all"; spaces become
hyphens; a slug that repeats later in the same file gets `-1`, `-2`, ...
appended for the 2nd, 3rd, ... occurrence) - not a general markdown-to-HTML
renderer, just this one well-documented, stable algorithm.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

CHECKED_DOCS = (
    "docs/User_Guide.md",
    "docs/BACKEND_API.md",
    "docs/API_ROUTES.md",
    "docs/architecture/Architecture.md",
    "docs/development/Roadmap.md",
    "adapters/meshtastic/README.md",
)

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
SLUG_STRIP_RE = re.compile(r"[^\w\s-]", re.UNICODE)
SLUG_WHITESPACE_RE = re.compile(r"\s+")

# A fenced code block's own '#' lines (shell comments, etc.) are not
# headings - track fence state so they're never mistaken for one.
FENCE_RE = re.compile(r"^\s*```")


def _top_level_markdown_files() -> list[Path]:
    return sorted(p for p in REPO_ROOT.glob("*.md") if p.is_file())


def _docs_to_check() -> list[Path]:
    explicit = [REPO_ROOT / rel for rel in CHECKED_DOCS if (REPO_ROOT / rel).exists()]
    return explicit + _top_level_markdown_files()


def _is_relative_link(target: str) -> bool:
    if not target:
        return False
    if URL_SCHEME_RE.match(target):
        return False
    return True


def _github_slug(heading_text: str) -> str:
    cleaned = SLUG_STRIP_RE.sub("", heading_text.strip().lower())
    return SLUG_WHITESPACE_RE.sub("-", cleaned.strip())


def _heading_slugs(text: str) -> set[str]:
    """Every heading in `text`, slugged exactly like GitHub renders them -
    including the `-1`/`-2`/... suffix GitHub appends to the 2nd, 3rd, ...
    heading that produces the same base slug (e.g. two "## License"
    headings in one file become #license and #license-1)."""
    seen_counts: dict[str, int] = {}
    slugs: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = ATX_HEADING_RE.match(line)
        if not match:
            continue
        base = _github_slug(match.group(2))
        if not base:
            continue
        count = seen_counts.get(base, 0)
        seen_counts[base] = count + 1
        slugs.add(base if count == 0 else f"{base}-{count}")
    return slugs


def check_links() -> list[str]:
    failures = []
    heading_cache: dict[Path, set[str]] = {}

    def slugs_for(path: Path) -> set[str]:
        if path not in heading_cache:
            try:
                heading_cache[path] = _heading_slugs(path.read_text(encoding="utf-8"))
            except OSError:
                heading_cache[path] = set()
        return heading_cache[path]

    for doc_path in _docs_to_check():
        rel_doc = doc_path.relative_to(REPO_ROOT)
        text = doc_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in MARKDOWN_LINK_RE.finditer(line):
                target = match.group(1).strip()
                if not _is_relative_link(target):
                    continue
                path_part, _, anchor = target.partition("#")
                path_part = path_part.strip()

                if path_part:
                    resolved = (doc_path.parent / path_part).resolve()
                    if not resolved.exists():
                        failures.append(f"{rel_doc}:{lineno}: broken relative link {target!r}")
                        continue
                else:
                    resolved = doc_path

                if anchor and resolved.suffix.lower() == ".md" and resolved.exists():
                    if anchor not in slugs_for(resolved):
                        rel_target = resolved.relative_to(REPO_ROOT) if resolved != doc_path else "this file"
                        failures.append(
                            f"{rel_doc}:{lineno}: anchor '#{anchor}' not found in {rel_target} "
                            f"(link {target!r})"
                        )
    return failures


def main() -> int:
    failures = check_links()
    if not failures:
        print("OK - no broken relative links or anchors found")
        return 0

    print(f"FAIL - {len(failures)} broken link/anchor issue(s):\n", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
