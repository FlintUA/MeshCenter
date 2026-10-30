# MeshCenter - single-container image (Core + Meshtastic adapter), linux/amd64.
#
# Build decisions (see docs/Docker_Installation.md and
# docs/architecture/docker-blockers.md):
#  * python:3.11-slim-trixie (Debian 13, pinned) - no Raspberry Pi OS base needed: the camera/libcamera
#    import is already guarded, and this image ships no camera, GPIO, I2C,
#    BLE or serial support. TCP radios only (network cameras use the minimal LGPL ffmpeg built in the first stage).
#  * ONE container, TWO OS processes, TWO separate venvs - the GPLv3
#    `meshtastic` package lives only in the adapter venv (the same layout
#    install.sh builds on a Pi); Core's interpreter cannot import it. The
#    adapter venv must sit at adapters/meshtastic/venv, where
#    meshsrv/runtime_identity.py looks for the CLI and the adapter interpreter.
#  * Both venvs are installed from the repo's own requirement files - no
#    duplicated dependency lists.
#  * /config is appended to the END of sys.path of the Core venv ONLY, via a
#    .pth file. NOT PYTHONPATH: that would shadow the stdlib and leak into
#    the adapter subprocess and the meshtastic CLI.
#  * tini is PID 1 (signal forwarding, zombie reaping). gunicorn runs with the
#    repo's gunicorn.conf.py; the entrypoint ignores extra arguments so
#    `workers = 1` cannot be overridden from the command line.
#  * Runs as a non-root user; compose overrides it with PUID:PGID (the user
#    owning the bind mounts). No chown is ever attempted.
#  * Redistribution note: an image containing the adapter venv redistributes
#    GPLv3 code (and the LGPL ffmpeg below). Build locally; do not publish it
#    before the D0 section 5 documentation gap is closed.
# Pinned on purpose (DK5): python:3.11-slim silently moved from Debian 12 to 13 once. This is the system the
# running image already uses; change it only deliberately, with a release note.
# ---------------------------------------------------------------------------
# Stage 1: a minimal LGPL ffmpeg (CAM2 Phase 4). Debian's own ffmpeg package is GPL (x264/x265) and adds about
# 625 MB; this build has NO external libraries, NO --enable-gpl, NO --enable-nonfree, and only the components the
# camera code uses (every call site was listed in the Phase 4 PR): the concat demuxer fed from stdin, RTSP/RTP/SDP
# over tcp/udp (no TLS: RTSPS is not supported), the mov demuxer, native H.264 decoder, mjpeg encoder, the mp4,
# mjpeg and image2pipe muxers, the scale/format filters. Shared libraries (an rpath into /opt/ffmpeg/lib), so the
# LGPL relink right is trivial: the source tarball, the licence text and the configure line ship in the image under
# /usr/share/doc/meshcenter-ffmpeg. The version and checksum are pinned; the checksum is checked by BuildKit itself.
# Security point releases of the 7.1 line: rebuild the image (docs/development/RELEASING.md).
FROM python:3.11-slim-trixie AS ffmpeg-build
ARG FFMPEG_VERSION=7.1.5
ARG FFMPEG_SHA256=de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential nasm pkg-config xz-utils ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:${FFMPEG_SHA256} https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz /src/ffmpeg.tar.xz
RUN mkdir /src/ffmpeg && tar -xJf /src/ffmpeg.tar.xz -C /src/ffmpeg --strip-components=1 \
 && cd /src/ffmpeg \
 && ./configure \
      --prefix=/opt/ffmpeg --enable-rpath --enable-shared --disable-static \
      --disable-gpl --disable-nonfree --disable-version3 --disable-autodetect \
      --disable-everything --disable-doc --disable-debug --disable-ffplay --disable-ffprobe \
      --disable-avdevice --disable-postproc --enable-pthreads \
      --enable-protocol=pipe,fd,tcp,udp,rtp \
      --enable-demuxer=concat,rtsp,rtp,sdp,mov,mjpeg \
      --enable-muxer=mp4,mjpeg,image2pipe \
      --enable-decoder=h264,aac,pcm_alaw,pcm_mulaw \
      --enable-encoder=mjpeg \
      --enable-parser=h264,hevc,aac \
      --enable-bsf=extract_extradata,h264_mp4toannexb,hevc_mp4toannexb,null \
      --enable-filter=scale,format,null,buffer,buffersink \
      --enable-swscale \
 && make -j"$(nproc)" \
 && make install \
 && /opt/ffmpeg/bin/ffmpeg -buildconf > /opt/ffmpeg/buildconf.txt \
 && cp /src/ffmpeg/COPYING.LGPLv2.1 /src/ffmpeg/LICENSE.md /opt/ffmpeg/ \
 && /opt/ffmpeg/bin/ffmpeg -L | head -3

# ---------------------------------------------------------------------------
# Stage 2: the MeshCenter image
FROM python:3.11-slim-trixie

ARG MESHCENTER_VERSION=unknown
ARG PUID=1026
ARG PGID=100

ENV MESHCENTER_RUNTIME=container \
    MESHCENTER_VERSION=${MESHCENTER_VERSION} \
    MESHCENTER_DATA_DIR=/data \
    MESHCENTER_CONFIG_DIR=/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data/.home \
    TZ=UTC \
    PATH=/opt/core-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Debian point-release pins break rebuilds; only tini and tzdata are installed. ffmpeg is NOT taken from Debian
# (205 packages, about 625 MB, GPL): the minimal LGPL build of the first stage is copied in below.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && (getent group "${PGID}" >/dev/null || groupadd -g "${PGID}" meshcenter) \
 && useradd -l -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin meshcenter \
 && mkdir -p /data /config \
 && chown "${PUID}:${PGID}" /data /config

WORKDIR /app

# Dependencies first (layer cache). Core venv: no `meshtastic` in it.
# The minimal LGPL ffmpeg (stage 1) with its shared libraries, the licence text, the configure line and the exact
# source tarball it was built from (the written offer is this very file), and a PATH entry for the binary.
COPY --from=ffmpeg-build /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffmpeg
COPY --from=ffmpeg-build /opt/ffmpeg/lib/ /opt/ffmpeg/lib/
COPY --from=ffmpeg-build /opt/ffmpeg/buildconf.txt /opt/ffmpeg/COPYING.LGPLv2.1 /opt/ffmpeg/LICENSE.md /usr/share/doc/meshcenter-ffmpeg/
COPY --from=ffmpeg-build /src/ffmpeg.tar.xz /usr/share/doc/meshcenter-ffmpeg/ffmpeg-source.tar.xz
RUN ln -s /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg \
 && ffmpeg -version | head -1 \
 && ffmpeg -L | grep -q "GNU Lesser General Public" \
 && ! ffmpeg -buildconf | grep -Eq "enable-(gpl|nonfree|version3)" \
 && ! ldd /opt/ffmpeg/bin/ffmpeg | grep -q "not found"

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/core-venv \
 && /opt/core-venv/bin/pip install -r /tmp/requirements.txt \
 && echo "/config" > "$(/opt/core-venv/bin/python -c 'import site; print(site.getsitepackages()[0])')/meshcenter-config.pth"

# Adapter venv at the path Core expects.
COPY adapters/meshtastic/requirements.txt /tmp/adapter-requirements.txt
RUN python -m venv /app/adapters/meshtastic/venv \
 && /app/adapters/meshtastic/venv/bin/pip install -r /tmp/adapter-requirements.txt

COPY . /app

# Image-level isolation checks (fail the build, not the first request).
RUN ! /opt/core-venv/bin/python -c "import meshtastic" 2>/dev/null \
 && /app/adapters/meshtastic/venv/bin/python -c "import meshtastic" \
 && /app/adapters/meshtastic/venv/bin/python -c "import sys; sys.exit(1 if '/config' in sys.path else 0)" \
 && /opt/core-venv/bin/python -c "import sys; sys.exit(0 if sys.path[-1] == '/config' else 1)" \
 && test -x /app/adapters/meshtastic/venv/bin/meshtastic \
 && chmod 0755 /app/deploy/docker/entrypoint.sh \
 && test ! -e /app/data \
 && test ! -e /app/deploy/docker/data \
 && test ! -e /app/deploy/docker/config \
 && test ! -e /app/deploy/docker/.env \
 && test ! -e /app/config.py \
 && test ! -e /app/weather_secrets.py

# OCI image labels (v1.12.2): `docker image inspect` shows the version and the exact commit.
# Declared last so a new revision never invalidates the dependency layers above.
# The licence expression covers the whole image: MIT Core + the GPLv3 adapter environment + the LGPL ffmpeg.
ARG MESHCENTER_REVISION=unknown
LABEL org.opencontainers.image.title="MeshCenter"       org.opencontainers.image.version="${MESHCENTER_VERSION}"       org.opencontainers.image.revision="${MESHCENTER_REVISION}"       org.opencontainers.image.source="https://github.com/FlintUA/MeshCenter"       org.opencontainers.image.licenses="MIT AND GPL-3.0-only AND LGPL-2.1-or-later"

USER ${PUID}:${PGID}
EXPOSE 5000
VOLUME ["/data", "/config"]

# start-period covers start_runtime()'s synchronous TCP identity probe against
# an unreachable radio (up to ~80s) before gunicorn begins serving.
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/docker/entrypoint.sh"]
