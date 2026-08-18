"""Gunicorn configuration for MeshCenter (deploy/meshcenter.service runs
`gunicorn -c gunicorn.conf.py wsgi:app`). Gunicorn loads this file
automatically from the current directory when -c points at it - see
WorkingDirectory in the systemd unit.

config.py stays the single source of truth for host/port, same as when
running `python server.py` directly - this file reads APP_HOST/APP_PORT
from it rather than hardcoding them here or in the systemd unit.
"""

from config import APP_HOST, APP_PORT

bind = f"{APP_HOST}:{APP_PORT}"

# Exactly one worker process, always. This is not a performance choice -
# MeshCenter's entire runtime state (nodes/chats/messages/settings) lives in
# one process's memory behind server.py's state_lock, and start_runtime()
# opens the Meshtastic radio's serial port exclusively. A second worker
# process would run its own start_runtime() and try to open the SAME serial
# port at the same time - a race for the device, not just wasted resources.
# server.py's own _runtime_started flag (PR #66) only guards against calling
# start_runtime() twice *within one process*; it can't see a second gunicorn
# worker process at all, which is why start_runtime() also takes an
# OS-level file lock (see server.py's RUNTIME_LOCK_FILE / _acquire_runtime_lock)
# as a second, independent line of defense in case `workers` is ever
# overridden by a stray CLI flag or a future config edit.
workers = 1

# gthread, not the sync default: /video_feed (api/api_camera.py) is a
# long-lived MJPEG stream (multipart/x-mixed-replace) that stays open for as
# long as someone's watching the camera. A sync worker handles exactly one
# connection at a time for the whole worker process, so one person watching
# the camera would block every other request - chat polling, node list,
# telemetry, everything - until they closed the tab. gthread runs each
# request in its own thread within the single worker process, so a live
# video stream and ordinary API traffic can proceed concurrently. Verified
# live on hardware, not just reasoned about - see the PR description.
worker_class = "gthread"

# MeshCenter is a LAN app for a handful of browser tabs, not an
# internet-facing service - this isn't sized for high concurrency. It's
# sized against chat.js's own polling behavior: a single open tab already
# keeps several endpoints in flight on a short cycle (system info,
# cpu-history, base_status, messages, chats, notifications, telemetry,
# radio_health, system log, ...), and one of those "requests" can be the
# long-lived /video_feed stream sitting in its own thread for minutes.  4
# threads would mean one video viewer alone leaves only 3 threads for every
# other request from every other tab; 8 gives real headroom for a couple of
# people using MeshCenter at once without costing much - threads share the
# single worker process's memory, they're not separate Python interpreters.
threads = 8

# Gunicorn's default (30s) is a heartbeat timeout for the whole worker
# process, not a per-request cap - but a gthread worker's heartbeat loop can
# still be starved if every thread is busy, and a slow/idle-but-open stream
# was worth erring generous on rather than assuming the default is fine.
# Bumped to 120s and verified live against a multi-minute real /video_feed
# stream on hardware (see PR description for what was actually observed) -
# adjust here if that ever changes.
timeout = 120

# Deliberately NOT set: max_requests / max_requests_jitter (gunicorn's
# worker-recycling). That would restart the single worker process
# periodically - including whatever the radio listener subprocess and
# serial port were doing at that moment - and graceful reopen-of-serial-port
# behavior across a recycle hasn't been tested. Revisit only as a deliberate
# decision, not a default to silently turn on.

# StandardOutput=journal / PYTHONUNBUFFERED=1 in the systemd unit already
# get server.py's own print() logging into journalctl - gunicorn's own
# access/error logs go to stderr by default, which the unit also captures,
# so no separate logging config is added here.
