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

H1-C review follow-up: the check above only verifies INTERNAL consistency
(do index.html/i18n.js/CURRENT_ASSETS all agree on one token) - it never
asked "did every file that actually changed on this branch get a mention
anywhere in that agreement at all?" PR #323 hit exactly that gap: chat.js
and all four static/i18n/*.json catalogs changed, but nobody added chat.js
to CURRENT_ASSETS or bumped CATALOG_VERSION, and the check above passed
anyway - it was never told to look at those files. `_changed_static_files()`
below closes that by diffing this branch against its merge-base with
origin/main (dependency-free: shells out to the `git` already on every CI
runner and every contributor's machine, not a pip package) and failing if
a changed static/ file isn't covered: a `static/i18n/*.json` catalog needs
CATALOG_VERSION bumped, anything else already referenced in index.html via
a versioned `?v=` tag needs to be in CURRENT_ASSETS with the current token.
A changed static/ file that was never referenced with a `?v=` tag at all is
out of scope here - there is no stale-cache regression this check can even
describe for it. Resolves to "nothing to check" (never a failure) when
origin/main can't be resolved (a shallow/local clone without it fetched)
or when this checkout already IS origin/main (a push-to-main CI run, or a
plain local checkout of main) - a no-op on main itself, per its own review
request, not a silent pass dressed up as one: see `main()`'s own handling
of `_changed_static_files()` returning `None`.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INDEX_HTML = REPO_ROOT / 'templates' / 'index.html'
I18N_JS = REPO_ROOT / 'static' / 'i18n.js'

# The single canonical cache-busting token for the current frontend release.
CURRENT_VERSION = '20261010-u2-csrf-click-msg'

# Browser assets changed in the current release. Every entry must be referenced
# in index.html with CURRENT_VERSION (and, for i18n.js, its CATALOG_VERSION must
# match too). These are the files that a browser would otherwise keep serving
# stale from cache. Keys are the `static/`-relative path.
CURRENT_ASSETS = {
    'static/i18n.js',
    'static/chat.js',
    'static/chat-views.js',
    'static/csrf.js',
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


def _run_git(args: list[str], repo_root: Path) -> str | None:
    """None on any failure (git missing, not a repo, ref doesn't resolve,
    timeout) - every caller treats that as "skip this part of the check",
    never as a reason to crash the whole script over an environment
    limitation this check doesn't own (e.g. a shallow clone that was
    never given an `origin/main` ref to diff against)."""
    try:
        result = subprocess.run(
            ['git', *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def changed_static_files(repo_root: Path = REPO_ROOT) -> list[str] | None:
    """Tracked `static/`-relative paths changed on this branch versus its
    merge-base with origin/main (git's own `a...b` diff syntax resolves
    the merge-base itself - no manual computation needed), or None when
    that comparison isn't meaningful here: origin/main can't be resolved
    (a shallow/local clone that never fetched it) or this checkout IS
    already origin/main (a push-to-main CI run, or a plain local checkout
    of main) - nothing to diff against by definition, a deliberate no-op
    per this check's own review request, not a silent "nothing changed"."""
    head = _run_git(['rev-parse', 'HEAD'], repo_root)
    main_sha = _run_git(['rev-parse', 'origin/main'], repo_root)
    if head is None or main_sha is None:
        return None
    if head.strip() == main_sha.strip():
        return None
    diff = _run_git(['diff', '--name-only', 'origin/main...HEAD'], repo_root)
    if diff is None:
        return None
    return [line.strip() for line in diff.splitlines() if line.strip().startswith('static/')]


def _catalog_version_on_main(repo_root: Path = REPO_ROOT) -> str | None:
    """CATALOG_VERSION as it reads on origin/main itself - the actual
    baseline a changed static/i18n/*.json catalog needs to have moved
    away from. Deliberately NOT compared against CURRENT_VERSION (the
    script's own hardcoded literal): PR #323's real catalog miss had
    CATALOG_VERSION and CURRENT_VERSION both stale AT THE SAME value
    (neither bumped), so a check against CURRENT_VERSION alone passes
    vacuously in exactly the failure case this exists to catch. Comparing
    against main's own prior value instead needs no human to have
    remembered to bump CURRENT_VERSION at all - only that CATALOG_VERSION
    itself actually moved. None when origin/main or its i18n.js can't be
    read (mirrors changed_static_files()'s own fail-quiet contract)."""
    source = _run_git(['show', 'origin/main:static/i18n.js'], repo_root)
    if source is None:
        return None
    match = CATALOG_RE.search(source)
    return match.group(1) if match else None


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

    # H1-C review follow-up: the loop above only ever checks assets someone
    # already remembered to list in CURRENT_ASSETS - it structurally cannot
    # notice a changed file that was never added there at all (PR #323's
    # own miss: chat.js changed, nobody added it). This discovers that case
    # directly from git instead of relying on CURRENT_ASSETS being complete.
    changed = changed_static_files()
    if changed is not None:
        catalog_changed = any(
            path.startswith('static/i18n/') and path.endswith('.json') for path in changed
        )
        baseline_catalog_version = _catalog_version_on_main() if catalog_changed else None
        for path in changed:
            if path.startswith('static/i18n/') and path.endswith('.json'):
                # Compared against main's own prior CATALOG_VERSION, not
                # CURRENT_VERSION - see _catalog_version_on_main()'s own
                # docstring for why (a CURRENT_VERSION that was also never
                # bumped would otherwise hide this exact miss).
                if baseline_catalog_version is not None and catalog_version == baseline_catalog_version:
                    failures.append(
                        f"{path} changed on this branch but static/i18n.js's "
                        f"CATALOG_VERSION ({catalog_version!r}) was not bumped "
                        "away from origin/main's value - browsers would keep "
                        "serving the old catalog from cache."
                    )
                continue
            if path in CURRENT_ASSETS:
                continue  # already checked above
            if path not in served:
                continue  # never referenced with a ?v= tag - out of scope
            failures.append(
                f"{path} changed on this branch but is not in CURRENT_ASSETS "
                f"- add it here and bump its ?v= tag in templates/index.html "
                f"to {CURRENT_VERSION!r} in the same commit."
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
