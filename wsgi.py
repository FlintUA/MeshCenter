#!/usr/bin/env python3
"""
WSGI entry point for production servers (Gunicorn, uWSGI, etc.)
"""
import logging

# server.py itself still logs via print() (unchanged here), but modules such
# as meshsrv/attachments/service.py use the standard `logging` module
# (logger = logging.getLogger(__name__)) and emit nothing without a
# configured handler - Python's logging falls back to a "handler of last
# resort" that only surfaces WARNING and above, silently dropping the
# INFO-level structured MCA observability records
# (_log_inbound_channel_observability()). `python server.py` never hit this
# in practice because nothing called basicConfig() there either; it's only
# being fixed here, at the actual production entry point gunicorn imports.
# No timestamp/PID in the format - journald (StandardOutput=journal in
# deploy/meshcenter.service) already stamps every line, so repeating that
# here would just duplicate it in `journalctl` output.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

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

