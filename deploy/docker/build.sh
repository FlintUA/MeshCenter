#!/bin/sh
# Build the MeshCenter image with a REAL version label (DK3).
#   deploy/docker/build.sh            ->  meshcenter:<git describe>
#   deploy/docker/build.sh dk3        ->  also tags meshcenter:dk3
# `git describe` gives "1.10.0" on a release tag and "1.10.0-12-gabc1234" ahead
# of it; either way MeshCenter will not claim a newer release exists. A label
# like "dk2" is not a version and used to trigger a false "update available".
set -eu
cd "$(dirname "$0")/../.."
VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"
VERSION="${VERSION#v}"
# The exact commit goes into the image labels (org.opencontainers.image.revision); "-dirty"
# marks a build from a tree with uncommitted changes.
REVISION="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
if [ "${REVISION}" != "unknown" ] && [ -n "$(git status --porcelain 2>/dev/null)" ]; then REVISION="${REVISION}-dirty"; fi
echo "Building meshcenter:${VERSION} (linux/amd64, revision ${REVISION})"
docker build --platform linux/amd64 --build-arg "MESHCENTER_VERSION=${VERSION}" --build-arg "MESHCENTER_REVISION=${REVISION}" -t "meshcenter:${VERSION}" .
if [ "${1:-}" != "" ]; then docker tag "meshcenter:${VERSION}" "meshcenter:$1"; echo "also tagged meshcenter:$1"; fi
