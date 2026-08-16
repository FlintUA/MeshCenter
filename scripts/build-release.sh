#!/usr/bin/env bash
#
# build-release.sh — package a public MeshCenter release archive.
#
# Copies an explicit whitelist of paths into a clean staging directory and
# tars it up. Never archives the working tree wholesale: local dev artifacts
# (config.py, weather_secrets.py, data/, deploy.sh, .git/, venv/, this
# session's editor/agent directories, ad-hoc debug files, ...) must never
# reach a public release by accident. If a new top-level file or directory
# needs to ship, add it to WHITELIST explicitly — don't fall back to `cp -r .`.
#
# Usage:
#   scripts/build-release.sh [version]
#
#   version   Optional label used in the archive filename (e.g. v1.6.0).
#             Defaults to `git describe --tags` if available, else "dev".
#
# Output:
#   dist/meshcenter-<version>.tar.gz

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"
fi

DIST_DIR="$REPO_ROOT/dist"
STAGE_NAME="meshcenter-${VERSION}"
STAGE_DIR="$DIST_DIR/$STAGE_NAME"
ARCHIVE_PATH="$DIST_DIR/${STAGE_NAME}.tar.gz"

# Explicit whitelist. Directories are copied recursively (minus the
# exclusions applied below); files are copied as-is. This is the only
# place that decides what ships in a public release — keep it exhaustive
# and reviewed, never opportunistically widened.
WHITELIST=(
    api
    camera
    deploy
    devices
    docs
    meshsrv
    static
    storage
    telemetry
    templates
    utils
    weather
    server.py
    wsgi.py
    system_log.py
    config.example.py
    weather_secrets.example.py
    requirements.txt
    README.md
    LICENSE
)

# Never allowed into a release archive, even if accidentally added to
# WHITELIST above or present inside a whitelisted directory (e.g. a stray
# __pycache__ under api/). Belt-and-suspenders on top of the whitelist,
# not a substitute for it.
DENYLIST_PATTERNS=(
    "config.py"
    "weather_secrets.py"
    "*.pyc"
    "__pycache__"
    ".DS_Store"
    "Thumbs.db"
    "*.log"
    "*.swp"
)

echo "==> Building release: $STAGE_NAME"

if [ -e "$STAGE_DIR" ]; then
    echo "==> Removing stale staging directory: $STAGE_DIR"
    rm -rf "$STAGE_DIR"
fi
mkdir -p "$STAGE_DIR"

MISSING=0
for entry in "${WHITELIST[@]}"; do
    src="$REPO_ROOT/$entry"
    if [ ! -e "$src" ]; then
        echo "!! Missing whitelisted path: $entry" >&2
        MISSING=1
        continue
    fi

    dest="$STAGE_DIR/$entry"
    mkdir -p "$(dirname "$dest")"

    if [ -d "$src" ]; then
        cp -R "$src" "$dest"
    else
        cp "$src" "$dest"
    fi
done

if [ "$MISSING" -ne 0 ]; then
    echo "!! One or more whitelisted paths were missing from the repo (see above)." >&2
    echo "!! A release with a missing required path is broken, not just incomplete -" >&2
    echo "!! e.g. dropping weather/ or devices/ silently ships an archive whose" >&2
    echo "!! imports fail at runtime. Fix WHITELIST or restore the missing path" >&2
    echo "!! before publishing. Refusing to build." >&2
    rm -rf "$STAGE_DIR"
    exit 1
fi

echo "==> Applying denylist cleanup inside staged copy"
for pattern in "${DENYLIST_PATTERNS[@]}"; do
    find "$STAGE_DIR" -depth -iname "$pattern" -exec rm -rf {} + 2>/dev/null || true
done

# Defense in depth: fail loudly if anything denylisted survived, rather
# than silently shipping it.
LEAKED=0
for pattern in "config.py" "weather_secrets.py" "*.pyc"; do
    if find "$STAGE_DIR" -iname "$pattern" | grep -q .; then
        echo "!! Denylisted file survived staging: $pattern" >&2
        find "$STAGE_DIR" -iname "$pattern" >&2
        LEAKED=1
    fi
done
if [ "$LEAKED" -ne 0 ]; then
    echo "!! Refusing to archive: denylisted files present in staging directory." >&2
    exit 1
fi

echo "==> Archiving to $ARCHIVE_PATH"
tar -czf "$ARCHIVE_PATH" -C "$DIST_DIR" "$STAGE_NAME"

echo "==> Cleaning up staging directory"
rm -rf "$STAGE_DIR"

echo "==> Done: $ARCHIVE_PATH"
tar -tzf "$ARCHIVE_PATH" | sort
