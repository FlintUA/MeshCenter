#!/usr/bin/env python3
"""Check that every browser asset changed in the current release is served from
`templates/index.html` with the current cache-busting `?v=` token - a static
text check against the exact regression class that hit PR 6: `style-part2.css`
was reworked, but `index.html` still pointed at it with the previous release's
`?v=20260912-files-pr5-corr`, so browsers kept serving the old stylesheet from
cache and none of the CSS changes ever reached the user.

Not a substitute for real tests - just a fast, dependency-free check, same
class as `check_startup_calls.py` / `check-i18n.py`. It closes the loop on the
manual convention documented in CLAUDE.md (bump `?v=` per changed
`<script>`/`<link>` AND `static/i18n.js`'s `CATALOG_VERSION` in the same
commit): the single canonical token lives here, and this script fails unless
`index.html` and `i18n.js` both agree with it. When the next release changes a
set of assets, bump `CURRENT_VERSION` and update `CURRENT_ASSETS` in the same
commit as the `index.html`/`i18n.js` bumps - that's a feature, not a gap: the
point is nobody can change a browser asset without a visible, deliberate
cache-busting signal.
"""
import re
import sys
from pathlib import Path

INDEX_HTML = Path(__file__).parent.parent / 'templates' / 'index.html'
I18N_JS = Path(__file__).parent.parent / 'static' / 'i18n.js'

# The single canonical cache-busting token for the current frontend release.
CURRENT_VERSION = '20260914-auto-key-request'

# Browser assets changed in the current release. Every entry must be referenced
# in index.html with CURRENT_VERSION (and, for i18n.js, its CATALOG_VERSION must
# match too). These are the files that a browser would otherwise keep serving
# stale from cache. Keys are the `static/`-relative path.
CURRENT_ASSETS = {
    'static/chat.js',
    'static/files.js',
    'static/i18n.js',
    'static/style-part1.css',
    'static/style-part2.css',
    'static/style-part4.css',
}

# `<link rel="stylesheet" href="{{ url_for('static', filename='X') }}?v=TOKEN">`
LINK_RE = re.compile(
    r"url_for\('static',\s*filename='([^']+)'\)\s*\}\}\?v=([A-Za-z0-9_-]+)"
)
# `<script src="/static/X?v=TOKEN"></script>`
SCRIPT_RE = re.compile(r'src="/static/([^"?]+)\?v=([A-Za-z0-9_-]+)"')
# `var CATALOG_VERSION = 'TOKEN';` in static/i18n.js
CATALOG_RE = re.compile(r"CATALOG_VERSION\s*=\s*'([^']+)'")


def main() -> int:
    index_text = INDEX_HTML.read_text(encoding='utf-8')
    i18n_text = I18N_JS.read_text(encoding='utf-8')

    # Map `static/<file>` -> token as actually served by index.html.
    served: dict[str, str] = {}
    for match in LINK_RE.finditer(index_text):
        served[f'static/{match.group(1)}'] = match.group(2)
    for match in SCRIPT_RE.finditer(index_text):
        served[f'static/{match.group(1)}'] = match.group(2)

    failures: list[str] = []

    catalog = CATALOG_RE.search(i18n_text)
    catalog_version = catalog.group(1) if catalog else None
    if catalog_version != CURRENT_VERSION:
        failures.append(
            f"static/i18n.js CATALOG_VERSION is {catalog_version!r}, "
            f"expected {CURRENT_VERSION!r}"
        )

    for asset in sorted(CURRENT_ASSETS):
        if asset not in served:
            failures.append(
                f"{asset} is not referenced in templates/index.html at all"
            )
            continue
        if served[asset] != CURRENT_VERSION:
            failures.append(
                f"{asset} is served with stale ?v={served[asset]!r}, "
                f"expected ?v={CURRENT_VERSION!r}"
            )

    if not failures:
        print(
            f'OK - all {len(CURRENT_ASSETS)} current-release assets carry '
            f'?v={CURRENT_VERSION!r} and CATALOG_VERSION matches'
        )
        return 0

    print(
        f'FAIL - cache-busting drift across {len(failures)} check(s):\n',
        file=sys.stderr,
    )
    for failure in failures:
        print(f'  - {failure}', file=sys.stderr)
    print(
        "\nIf this release intentionally changed a different set of assets, "
        "update CURRENT_VERSION and CURRENT_ASSETS in this script in the same "
        "commit as the index.html/i18n.js bumps. Otherwise this is exactly the "
        "stale-cache regression this script exists to catch - see the module "
        "docstring.",
        file=sys.stderr,
    )
    return 1


if __name__ == '__main__':
    sys.exit(main())
