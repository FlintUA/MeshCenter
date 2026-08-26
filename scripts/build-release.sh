#!/usr/bin/env bash
#
# build-release.sh — package MeshCenter's two independent public release
# archives (P0 #2/#3 stabilization follow-up, "Model B": separate Core and
# Adapter archives, laying groundwork for future alternative radio/protocol
# backends beyond Meshtastic - APRS/LoRaWAN/MQTT/etc - not just this one).
#
# Copies explicit whitelists of paths into clean staging directories and
# tars each one up separately. Never archives the working tree wholesale:
# local dev artifacts (config.py, weather_secrets.py, data/, deploy.sh,
# .git/, venv/, this session's editor/agent directories, ad-hoc debug
# files, ...) must never reach a public release by accident. If a new
# top-level file or directory needs to ship, add it to the relevant
# whitelist explicitly - don't fall back to `cp -r .`.
#
# Two archives, two licenses, two dependency sets:
#
#   meshcenter-core-<version>.tar.gz
#     MIT. Everything Core needs to run WITHOUT the Meshtastic adapter
#     present at all - not just with an unconfigured adapter venv, but
#     with the adapters/ directory entirely absent from disk. Core already
#     degrades to a synthetic ADAPTER_UNAVAILABLE transport status in that
#     case (Task 48's design, see meshsrv/adapter_ipc_client.py) - this
#     archive proves it holds at the packaging boundary too, via
#     smoke_test_core.sh below.
#
#   meshcenter-meshtastic-adapter-<version>.tar.gz
#     GPLv3 (meshtastic itself; the adapter's own code is still MIT, see
#     adapters/meshtastic/LICENSE + THIRD_PARTY_NOTICES.md for the
#     detail). NOT just adapters/meshtastic/ in isolation - it has a real
#     transitive import dependency on a specific slice of meshsrv/ and
#     hardware/ (see ADAPTER_WHITELIST below for the exact list and why).
#     An archive containing only adapters/meshtastic/ would fail to
#     import at all - that's the bug this whitelist and its smoke test
#     exist to catch, not a hypothetical.
#
# Both archives are built from the SAME working tree on every run - this
# is deliberate duplication of a handful of small MIT files' *bytes* into
# both tarballs, not duplication of code on disk or a second copy to keep
# in sync. There is exactly one copy of each file in the repo; both
# archives are just two different tar'd-up views of it, rebuilt fresh
# every time. A future third "protocol" archive (splitting out just the
# genuinely-neutral meshsrv/radio_transport.py + meshsrv/ipc_protocol.py
# from the Core-infrastructure files currently riding along with them) is
# a reasonable idea once a second real adapter (non-Meshtastic) actually
# exists to justify it - premature before that, same principle applied
# elsewhere in this stabilization pass to versioning and to the
# separate-git-repo question.
#
# Usage:
#   scripts/build-release.sh [version]
#
#   version   Optional label used in both archive filenames (e.g. v1.6.0).
#             Defaults to `git describe --tags` if available, else "dev".
#             Both archives share the same version/git tag for now -
#             independent Core/Adapter versioning is a future, non-
#             blocking follow-up.
#
# Output:
#   dist/meshcenter-core-<version>.tar.gz
#   dist/meshcenter-meshtastic-adapter-<version>.tar.gz
#
# Each archive is smoke-tested (extract -> clean venv -> install -> import
# -> health-check, see scripts/smoke_test_core.sh / smoke_test_adapter.sh)
# immediately after being built. A failed smoke test stops the whole
# build (set -e) - a broken archive must never produce a green exit code.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"
fi

DIST_DIR="$REPO_ROOT/dist"

# Core: everything Core needs to run, MIT, with zero dependency on
# adapters/. gunicorn.conf.py is required here (not in the original
# audit's list) - deploy/meshcenter.service's ExecStart references it by
# absolute path (`gunicorn -c .../gunicorn.conf.py wsgi:app`); an archive
# without it can't actually run in production under the documented
# deployment.
CORE_WHITELIST=(
    api camera deploy devices docs hardware meshsrv modules static
    storage system telemetry templates utils weather
    server.py wsgi.py system_log.py gunicorn.conf.py
    config.example.py weather_secrets.example.py requirements.txt
    README.md LICENSE THIRD_PARTY_NOTICES.md
)

# Adapter: adapters/meshtastic/ itself (GPLv3 meshtastic dependency, its
# own LICENSE + requirements.txt) PLUS the real transitive import closure
# it needs to actually import - confirmed by grep, not assumed:
#   adapters.meshtastic.serial_transport / .ble_transport / ._timeout_support
#     -> meshsrv.radio_transport, meshsrv.ipc_protocol,
#        meshsrv.meshtastic_transport, meshsrv.node_time_sync,
#        meshsrv.serial_port_supervisor
#   meshsrv.node_time_sync -> meshsrv.time_service -> hardware.rtc_service
#     -> hardware.i2c_service
# All of the above are MIT/stdlib-only (no meshtastic import, no native/
# GPL dependency at module level - hardware/i2c_service.py and
# rtc_service.py only shell out via subprocess) - confirmed by direct
# read, safe to duplicate into this archive.
#
# Known coupling, candidate for future cleanup (not fixed here): the
# hardware/rtc_service.py + hardware/i2c_service.py dependency only
# exists because meshsrv/node_time_sync.py's is_trusted() check reads
# RTC status - that's Core-infrastructure plumbing for "is the Pi's clock
# trustworthy", conceptually unrelated to both the Meshtastic wire
# protocol and to any future non-Meshtastic adapter. Whether
# set_device_time() could instead take a trusted/untrusted flag as a
# plain parameter from Core, instead of the adapter code importing
# hardware/rtc_service.py to determine it itself, is a legitimate
# follow-up - out of scope for this packaging fix.
ADAPTER_WHITELIST=(
    adapters/meshtastic
    meshsrv/__init__.py
    meshsrv/radio_transport.py
    meshsrv/ipc_protocol.py
    meshsrv/meshtastic_transport.py
    meshsrv/node_time_sync.py
    meshsrv/serial_port_supervisor.py
    meshsrv/time_service.py
    hardware/__init__.py
    hardware/rtc_service.py
    hardware/i2c_service.py
    THIRD_PARTY_NOTICES.md
)

# Never allowed into either release archive, even if accidentally added
# to a whitelist above or present inside a whitelisted directory (e.g. a
# stray __pycache__ under api/). Belt-and-suspenders on top of the
# whitelist, not a substitute for it.
DENYLIST_PATTERNS=(
    "config.py"
    "weather_secrets.py"
    "*.pyc"
    "__pycache__"
    ".DS_Store"
    "Thumbs.db"
    "*.log"
    "*.swp"
    "venv"
)

# build_archive <archive-basename-without-extension> <whitelist-array-name>
#
# Stages the given whitelist into dist/<name>/, applies the denylist,
# fails loud on any missing whitelisted path or any denylisted survivor,
# then tars it up to dist/<name>.tar.gz. Shared by both archives so the
# staging/denylist/archiving logic exists exactly once.
build_archive() {
    local stage_name="$1"
    local -n whitelist_ref="$2"

    local stage_dir="$DIST_DIR/$stage_name"
    local archive_path="$DIST_DIR/${stage_name}.tar.gz"

    echo "==> Building release: $stage_name"

    if [ -e "$stage_dir" ]; then
        echo "==> Removing stale staging directory: $stage_dir"
        rm -rf "$stage_dir"
    fi
    mkdir -p "$stage_dir"

    local missing=0
    local entry src dest
    for entry in "${whitelist_ref[@]}"; do
        src="$REPO_ROOT/$entry"
        if [ ! -e "$src" ]; then
            echo "!! Missing whitelisted path: $entry" >&2
            missing=1
            continue
        fi

        dest="$stage_dir/$entry"
        mkdir -p "$(dirname "$dest")"

        if [ -d "$src" ]; then
            cp -R "$src" "$dest"
        else
            cp "$src" "$dest"
        fi
    done

    if [ "$missing" -ne 0 ]; then
        echo "!! One or more whitelisted paths were missing from the repo (see above)." >&2
        echo "!! A release with a missing required path is broken, not just incomplete -" >&2
        echo "!! e.g. dropping hardware/ or gunicorn.conf.py silently ships an archive" >&2
        echo "!! whose imports fail at runtime or that can't be deployed at all. Fix the" >&2
        echo "!! whitelist or restore the missing path before publishing. Refusing to build." >&2
        rm -rf "$stage_dir"
        exit 1
    fi

    echo "==> Applying denylist cleanup inside staged copy"
    local pattern
    for pattern in "${DENYLIST_PATTERNS[@]}"; do
        find "$stage_dir" -depth -iname "$pattern" -exec rm -rf {} + 2>/dev/null || true
    done

    # Defense in depth: fail loudly if anything denylisted survived,
    # rather than silently shipping it.
    local leaked=0
    for pattern in "config.py" "weather_secrets.py" "*.pyc"; do
        if find "$stage_dir" -iname "$pattern" | grep -q .; then
            echo "!! Denylisted file survived staging: $pattern" >&2
            find "$stage_dir" -iname "$pattern" >&2
            leaked=1
        fi
    done
    if [ "$leaked" -ne 0 ]; then
        echo "!! Refusing to archive: denylisted files present in staging directory." >&2
        exit 1
    fi

    echo "==> Archiving to $archive_path"
    tar -czf "$archive_path" -C "$DIST_DIR" "$stage_name"

    echo "==> Cleaning up staging directory"
    rm -rf "$stage_dir"

    echo "==> Done: $archive_path"
    tar -tzf "$archive_path" | sort
}

mkdir -p "$DIST_DIR"

CORE_ARCHIVE_NAME="meshcenter-core-${VERSION}"
ADAPTER_ARCHIVE_NAME="meshcenter-meshtastic-adapter-${VERSION}"

build_archive "$CORE_ARCHIVE_NAME" CORE_WHITELIST
echo "==> Smoke-testing $CORE_ARCHIVE_NAME.tar.gz"
"$SCRIPT_DIR/smoke_test_core.sh" "$DIST_DIR/${CORE_ARCHIVE_NAME}.tar.gz"

build_archive "$ADAPTER_ARCHIVE_NAME" ADAPTER_WHITELIST
echo "==> Smoke-testing $ADAPTER_ARCHIVE_NAME.tar.gz"
"$SCRIPT_DIR/smoke_test_adapter.sh" "$DIST_DIR/${ADAPTER_ARCHIVE_NAME}.tar.gz"

echo "==> Both archives built and smoke-tested successfully:"
echo "    $DIST_DIR/${CORE_ARCHIVE_NAME}.tar.gz"
echo "    $DIST_DIR/${ADAPTER_ARCHIVE_NAME}.tar.gz"
