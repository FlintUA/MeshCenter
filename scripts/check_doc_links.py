#!/usr/bin/env python3
"""CI check (H2-A4): every relative markdown link in the docs this project
actively maintains actually resolves to a real file - a cheap, dependency-
free check against the exact regression class a big docs restructure (like
H2-A's own README split into docs/User_Guide.md/docs/architecture/
Architecture.md/docs/BACKEND_API.md) can introduce: a link that pointed at
a real heading/file before the restructure and silently rots after it.

Scope is CHECKED_DOCS (an explicit list, same reasoning as
check_api_docs.py's DOCS_TO_CHECK - this project's docs/ tree also holds
ADRs and point-in-time audit/handoff snapshots that aren't actively
maintained, so a broad `docs/**` glob would flag pre-existing drift this
check was never meant to police) plus every top-level *.md file (README,
INSTALL, CLAUDE, THIRD_PARTY_NOTICES, KNOWN_ISSUES, ...), since those are
few enough, and important enough, to just always check.

Only relative links are checked - `[text](path)` or `[text](path#anchor)`
where `path` doesn't start with a URL scheme (`http://`, `mailto:`, ...)
and isn't a bare `#anchor`-only same-page link (anchor-only intra-page
links aren't checked - verifying every heading-derived GitHub anchor slug
exists is a different, fussier problem this check doesn't attempt).
External links are deliberately out of scope - that's a network check, not
a repo-consistency one.
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


def _top_level_markdown_files() -> list[Path]:
    return sorted(p for p in REPO_ROOT.glob("*.md") if p.is_file())


def _docs_to_check() -> list[Path]:
    explicit = [REPO_ROOT / rel for rel in CHECKED_DOCS if (REPO_ROOT / rel).exists()]
    return explicit + _top_level_markdown_files()


def _is_relative_link(target: str) -> bool:
    if not target or target.startswith("#"):
        return False
    if URL_SCHEME_RE.match(target):
        return False
    return True


def check_links() -> list[str]:
    failures = []
    for doc_path in _docs_to_check():
        rel_doc = doc_path.relative_to(REPO_ROOT)
        text = doc_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in MARKDOWN_LINK_RE.finditer(line):
                target = match.group(1).strip()
                if not _is_relative_link(target):
                    continue
                path_part = target.split("#", 1)[0].strip()
                if not path_part:
                    continue
                resolved = (doc_path.parent / path_part).resolve()
                if not resolved.exists():
                    failures.append(f"{rel_doc}:{lineno}: broken relative link {target!r}")
    return failures


def main() -> int:
    failures = check_links()
    if not failures:
        print("OK - no broken relative links found")
        return 0

    print(f"FAIL - {len(failures)} broken relative link(s):\n", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
