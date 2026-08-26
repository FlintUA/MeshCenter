#!/usr/bin/env bash
#
# smoke_test_adapter.sh — extract -> clean venv -> install -> import ->
# health-check for a built meshcenter-meshtastic-adapter-<version>.tar.gz
# archive. Called by build-release.sh right after building the Adapter
# archive; also runnable standalone against any already-built archive.
#
# Usage:
#   scripts/smoke_test_adapter.sh <path-to-meshcenter-meshtastic-adapter-*.tar.gz>
#
# Fails loud (set -e, no `|| true` anywhere here) - a broken Adapter
# archive must stop the build, not produce a green exit code.

set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <path-to-meshcenter-meshtastic-adapter-*.tar.gz>" >&2
    exit 2
fi

ARCHIVE_PATH="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> [adapter smoke test] extracting $(basename "$ARCHIVE_PATH")"
tar -xzf "$ARCHIVE_PATH" -C "$WORK_DIR"
EXTRACTED_DIR="$WORK_DIR/$(ls "$WORK_DIR")"

echo "==> [adapter smoke test] creating clean venv"
python3 -m venv "$WORK_DIR/venv"
"$WORK_DIR/venv/bin/pip" install --quiet --upgrade pip
"$WORK_DIR/venv/bin/pip" install --quiet -r "$EXTRACTED_DIR/adapters/meshtastic/requirements.txt"

echo "==> [adapter smoke test] import + health check"
"$WORK_DIR/venv/bin/python" "$SCRIPT_DIR/_smoke_test_adapter_harness.py" "$EXTRACTED_DIR"

echo "==> [adapter smoke test] PASSED"
