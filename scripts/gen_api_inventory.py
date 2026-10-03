#!/usr/bin/env python3
"""Generates docs/API_ROUTES.md from a static scan of every Flask route
registered in server.py/api/*.py/system/*.py (H2-A3).

Static regex parsing of the `@app.route(...)`/`@app.get/post/put/delete/
patch(...)` decorators, not `app.url_map` via a full Flask app import -
same style and reasoning as every other `check_*.py` script in this repo
(`check_startup_calls.py`, `check_asset_cache_busting.py`, `check-i18n.py`):
fast, dependency-free, and doesn't need to construct a full app (with its
synthetic test config, fake hardware, DI-wired 30+ shared globals - see
tests/conftest.py) just to answer "what routes exist." The decorator
syntax here is simple and uniform enough (always a single line, always a
plain string-literal path, never built from a variable or an f-string -
confirmed by direct read before writing this, not assumed) that a regex
scan is exact, not an approximation - CLAUDE.md's "api/*.py are NOT Flask
Blueprints, just register_*_routes(app, ...) functions" is exactly why
this works: every route decorator in this codebase closes over the one
`app` parameter name, consistently, everywhere.

Auth/CSRF columns mirror api/api_auth.py's own `_enforce_auth()`/
`_enforce_csrf()` `@app.before_request` hooks exactly - there is no
separate per-route exemption list anywhere to read instead. If those
hooks' own predicates ever change, update AUTH_EXEMPT_PATHS/
AUTH_EXEMPT_PREFIXES/CSRF_SAFE_METHODS below to match.

Usage:
    python scripts/gen_api_inventory.py             # writes docs/API_ROUTES.md
    python scripts/gen_api_inventory.py --check      # exit 1 if stale, don't write (used by check_api_docs.py)
"""
import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "API_ROUTES.md"

# server.py registers routes directly; api/*.py and system/cpu_history.py
# via the register_*_routes(app, ...) DI pattern (CLAUDE.md).
ROUTE_SOURCE_GLOBS = ("server.py", "api/*.py", "system/*.py")

ROUTE_DECORATOR_RE = re.compile(
    r'@app\.(route|get|post|put|delete|patch)\(\s*["\']([^"\']+)["\'](?P<rest>.*)\)\s*$'
)
METHODS_KW_RE = re.compile(r'methods\s*=\s*\[([^\]]*)\]')
DEF_RE = re.compile(r'^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(')
DECORATOR_LINE_RE = re.compile(r'^\s*@')

AUTH_EXEMPT_PATHS = {"/login", "/setup"}
AUTH_EXEMPT_PREFIXES = ("/static/",)
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_SHORTHAND_METHODS = {
    "get": ["GET"],
    "post": ["POST"],
    "put": ["PUT"],
    "delete": ["DELETE"],
    "patch": ["PATCH"],
}


def _iter_source_files():
    for pattern in ROUTE_SOURCE_GLOBS:
        yield from sorted(REPO_ROOT.glob(pattern))


def _methods_for(decorator_name: str, rest: str) -> list[str]:
    methods_match = METHODS_KW_RE.search(rest)
    if methods_match:
        return sorted({m.strip(" \"'") for m in methods_match.group(1).split(",") if m.strip(" \"'")})
    # @app.route() with no methods= defaults to GET (+ Flask's own
    # automatic HEAD/OPTIONS, which this inventory doesn't list - they
    # aren't meaningfully distinct endpoints for a human reading this doc).
    return _SHORTHAND_METHODS.get(decorator_name, ["GET"])


def _auth_exempt(path: str) -> bool:
    return path in AUTH_EXEMPT_PATHS or any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES)


def _csrf_applies(path: str, methods: list[str]) -> bool:
    if not path.startswith("/api/"):
        return False
    return any(m not in CSRF_SAFE_METHODS for m in methods)


def _find_function_name(lines: list[str], start_index: int) -> str:
    """Walks forward from just after a route decorator, past any stacked
    decorators (@handle_errors, a second @app.route(...) mapping another
    path to the same function, blank lines) to the def line itself."""
    j = start_index
    while j < len(lines):
        def_match = DEF_RE.match(lines[j])
        if def_match:
            return def_match.group(1)
        if DECORATOR_LINE_RE.match(lines[j]) or not lines[j].strip():
            j += 1
            continue
        break
    return "?"


def collect_routes() -> list[dict]:
    routes = []
    for source_path in _iter_source_files():
        lines = source_path.read_text(encoding="utf-8").splitlines()
        rel_path = source_path.relative_to(REPO_ROOT).as_posix()
        for i, line in enumerate(lines):
            match = ROUTE_DECORATOR_RE.search(line)
            if not match:
                continue
            decorator_name, path, rest = match.group(1), match.group(2), match.group("rest")
            methods = _methods_for(decorator_name, rest)
            func_name = _find_function_name(lines, i + 1)
            routes.append({
                "path": path,
                "methods": methods,
                "file": rel_path,
                "line": i + 1,
                "function": func_name,
                "auth_exempt": _auth_exempt(path),
                "csrf_exempt": not _csrf_applies(path, methods),
            })
    routes.sort(key=lambda r: (r["path"], r["methods"]))
    return routes


def render_markdown(routes: list[dict]) -> str:
    lines = [
        "# API Routes",
        "",
        "Auto-generated by `scripts/gen_api_inventory.py` from every Flask route "
        "registered in `server.py`/`api/*.py`/`system/*.py` - **do not hand-edit**; "
        "regenerate instead (`python scripts/gen_api_inventory.py`) and commit the "
        "result. `scripts/check_api_docs.py` fails CI if this file is stale.",
        "",
        "Auth/CSRF columns mirror `api/api_auth.py`'s own `_enforce_auth()`/"
        "`_enforce_csrf()` predicates exactly (there is no separate per-route "
        "exemption list) - \"exempt\" means that guard never applies to this "
        "route, not that the route is otherwise unprotected. Every non-exempt "
        "route still requires auth only when protection is enabled "
        "(`is_protected()`); CSRF applies to every non-GET/HEAD `/api/*` route.",
        "",
        f"{len(routes)} routes total.",
        "",
        "| Path | Methods | Source | Auth | CSRF |",
        "|---|---|---|---|---|",
    ]
    for r in routes:
        source = f"`{r['file']}:{r['line']}` (`{r['function']}`)"
        auth = "exempt" if r["auth_exempt"] else "required"
        csrf = "exempt" if r["csrf_exempt"] else "required"
        lines.append(f"| `{r['path']}` | {', '.join(r['methods'])} | {source} | {auth} | {csrf} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="exit 1 if docs/API_ROUTES.md is stale relative to the current code, without writing it",
    )
    args = parser.parse_args()

    routes = collect_routes()
    rendered = render_markdown(routes)

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else None
        if current != rendered:
            print(
                f"FAIL - {OUTPUT_PATH.relative_to(REPO_ROOT)} is stale - "
                f"regenerate with `python scripts/gen_api_inventory.py` and commit it.",
                file=sys.stderr,
            )
            return 1
        print(f"OK - {OUTPUT_PATH.relative_to(REPO_ROOT)} is up to date ({len(routes)} routes)")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} ({len(routes)} routes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
