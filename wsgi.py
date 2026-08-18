#!/usr/bin/env python3
"""
WSGI entry point for production servers (Gunicorn, uWSGI, etc.)
"""
from server import app, start_runtime

# `from server import app` alone only registers Flask routes - every
# background worker (radio listener, telemetry, radio health, CPU history,
# update checks, schedule engine, time service, ...) lives in start_runtime(),
# which normally only runs under `if __name__ == "__main__":` in server.py
# itself. A WSGI server imports this module without ever executing that
# block, so it has to be called explicitly here instead.
#
# If gunicorn (or uwsgi) is run with more than one worker process, each
# worker process runs this module-level code independently and would start
# its own radio listener - only one process may own the serial port at a
# time. Production deployment via gunicorn (not yet wired into
# deploy/meshcenter.service - the systemd unit still runs `python server.py`
# directly) must use exactly one worker: `gunicorn --workers 1 wsgi:app`.
start_runtime()

if __name__ == "__main__":
    app.run()

