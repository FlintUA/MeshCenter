#!/usr/bin/env python3
"""CI check (H2-A4): docs/API_ROUTES.md stays in sync with the real code,
and the project's own living documentation never claims an `/api/...`
path that doesn't actually exist.

Two independent checks:

1. Regenerates the route inventory (gen_api_inventory.py's own
   collect_routes(), imported directly rather than reimplemented, so
   these two scripts can never silently drift against each other) and
   fails if docs/API_ROUTES.md doesn't match - same "regenerate and
   diff" shape as check_asset_cache_busting.py's CATALOG_VERSION check.

2. Scans DOCS_TO_CHECK for any `/api/...`-shaped substring and fails if
   it doesn't match a real route. An explicit file list, not a `docs/**`
   glob: this project's `docs/` tree also holds ADRs
   (docs/architecture/ADR-*.md) and point-in-time audit/handoff
   snapshots (docs/architecture/*_HANDOFF.md, D0-summary.md,
   bootstrap-runtime-audit.md, ...) that are deliberately historical
   records, not living references kept in sync with the code - a real,
   intentional mention of a since-renamed/removed endpoint there is not
   a documentation bug. DOCS_TO_CHECK is exactly H2-A's own target list
   (the docs this project actually maintains as "describes current
   behavior"), not an attempt to auto-discover that distinction.

A route with a dynamic segment (`<node_id>`, `<id>`, `<page>`, ...) is
matched structurally: each real route becomes a regex with `<...>`
segments replaced by `[^/]+`, so a doc's own literal example
(`/api/waypoints/42`) or placeholder spelling (`/api/waypoints/<id>`)
both match the real `/api/waypoints/<id>` route without needing the two
spellings to agree on a placeholder name.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))

from gen_api_inventory import OUTPUT_PATH, collect_routes, render_markdown  # noqa: E402

# H2-A's own explicit target list - see module docstring for why this is
# an explicit list, not a glob.
DOCS_TO_CHECK = (
    "README.md",
    "INSTALL.md",
    "KNOWN_ISSUES.md",
    "docs/User_Guide.md",
    "docs/BACKEND_API.md",
    "docs/architecture/Architecture.md",
    "docs/development/Roadmap.md",
)

MENTION_RE = re.compile(r"/api/[A-Za-z0-9_\-./<>!]+")
TRAILING_PUNCTUATION = ".,;:)'\"`"


def _route_pattern(path: str) -> "re.Pattern[str]":
    segments = path.split("/")
    pattern_segments = [r"[^/]+" if seg.startswith("<") and seg.endswith(">") else re.escape(seg) for seg in segments]
    return re.compile("^" + "/".join(pattern_segments) + "$")


def check_route_inventory_is_fresh() -> list[str]:
    rendered = render_markdown(collect_routes())
    current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else None
    if current == rendered:
        return []
    return [
        f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is stale - "
        f"regenerate with `python scripts/gen_api_inventory.py` and commit it."
    ]


def check_docs_reference_real_routes(routes: list[dict]) -> list[str]:
    patterns = [_route_pattern(r["path"]) for r in routes]
    failures = []
    for doc_rel_path in DOCS_TO_CHECK:
        doc_path = REPO_ROOT / doc_rel_path
        if not doc_path.exists():
            continue
        text = doc_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in MENTION_RE.finditer(line):
                mentioned = match.group(0).rstrip(TRAILING_PUNCTUATION)
                if not mentioned or mentioned == "/api/":
                    continue
                if not any(p.match(mentioned) for p in patterns):
                    failures.append(f"{doc_rel_path}:{lineno}: mentions {mentioned!r} - no such route exists")
    return failures


def main() -> int:
    routes = collect_routes()
    failures = check_route_inventory_is_fresh()
    failures += check_docs_reference_real_routes(routes)

    if not failures:
        print(f"OK - docs/API_ROUTES.md is fresh and every documented /api/ path in {len(DOCS_TO_CHECK)} checked docs is real")
        return 0

    print(f"FAIL - {len(failures)} API documentation issue(s):\n", file=sys.stderr)
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
