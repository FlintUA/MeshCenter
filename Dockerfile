# MeshCenter - single-container image (Core + Meshtastic adapter), linux/amd64.
#
# Build decisions (see docs/Docker_Installation.md and
# docs/architecture/docker-blockers.md):
#  * python:3.11-slim - no Raspberry Pi OS base needed: the camera/libcamera
#    import is already guarded, and this image ships no camera, GPIO, I2C,
#    BLE or serial support. TCP radios only.
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
#    GPLv3 code. Build locally; do not publish it before the D0 section 5
#    documentation gap is closed.
FROM python:3.11-slim

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

# Debian point-release pins break rebuilds; only tini and tzdata are installed.
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

USER ${PUID}:${PGID}
EXPOSE 5000
VOLUME ["/data", "/config"]

# start-period covers start_runtime()'s synchronous TCP identity probe against
# an unreachable radio (up to ~80s) before gunicorn begins serving.
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/docker/entrypoint.sh"]
