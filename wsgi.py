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
# time. deploy/meshcenter.service now runs this under gunicorn with
# `workers = 1` hardcoded in gunicorn.conf.py - never override that. As a
# second, independent line of defense in case it ever is,
# server.py's start_runtime() also takes an OS-level file lock
# (_acquire_runtime_lock()) that makes a second worker process fail loudly
# instead of silently racing the first one for the serial port.
start_runtime()

if __name__ == "__main__":
    app.run()

