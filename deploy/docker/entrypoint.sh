#!/bin/sh
# MeshCenter container entrypoint. Never chowns, never overwrites existing files.
set -eu

DATA_DIR="${MESHCENTER_DATA_DIR:-/data}"
CONFIG_DIR="${MESHCENTER_CONFIG_DIR:-/config}"

for dir in "$DATA_DIR" "$CONFIG_DIR"; do
    if [ ! -d "$dir" ] || [ ! -w "$dir" ]; then
        echo "[entrypoint] FATAL: $dir is missing or not writable by uid $(id -u):$(id -g)." >&2
        echo "[entrypoint] Make the host folder owned by that user (PUID/PGID in .env) and restart." >&2
        exit 78
    fi
done

mkdir -p "$DATA_DIR/.home"

# First start: seed the config only if absent.
if [ ! -e "$CONFIG_DIR/config.py" ]; then
    cp /app/deploy/docker/config.container.py "$CONFIG_DIR/config.py"
    echo "[entrypoint] Created $CONFIG_DIR/config.py from the container template."
fi

# Extra arguments and GUNICORN_CMD_ARGS are deliberately ignored: workers must
# stay 1 (gunicorn.conf.py), CLI flags would override it.
unset GUNICORN_CMD_ARGS
cd /app
exec gunicorn -c /app/gunicorn.conf.py wsgi:app
