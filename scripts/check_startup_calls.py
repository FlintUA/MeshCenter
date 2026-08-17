#!/usr/bin/env python3
"""Check that server.py still starts every background worker/route group it
should - a cheap, blunt safeguard against the exact class of regression that
prompted it: a whole-file deploy from a stale local branch silently deleted
several startup calls from server.py's __main__ block (Schedule Engine,
node-time-sync, e-paper time-format wiring) while a legitimate, narrower
change was being committed alongside them. Nothing raised an exception -
the deleted features just silently stopped running - so this needs to be a
static text check, not something that would show up in a test run.

Not a substitute for real tests (see GitHub issue #46, which this script
is a first concrete entry for) - just a fast, dependency-free check that
can run in CI or as a pre-commit hook: does server.py still literally
contain a call to each of these, by substring. A refactor that renames one
of these functions needs to update this list too - that's a feature, not
a gap: the point is nobody should be able to remove a startup call from
server.py without a visible, deliberate signal.
"""
import re
import sys
from pathlib import Path

SERVER_PY = Path(__file__).parent.parent / 'server.py'

# (substring to look for, human explanation of what silently breaks if it's
# missing - shown in the failure message so this doesn't require re-deriving
# the incident from scratch next time).
REQUIRED_CALLS = [
    ('start_time_service()',
     'Background thread that populates the time-sync status meshsrv/time_service.py '
     'caches (GET /api/time, the browser Time card widget, e-paper System Screen). '
     'Without this the cache never populates and every /api/time response silently '
     'falls back to a hardcoded {"timezone": "UTC", "synchronized": False} shape - '
     'no exception, no error log, just a wrong clock shown in the UI.'),
    ('start_schedule_engine(',
     'meshsrv/schedule_engine.py\'s background thread - without this, configured '
     'schedule rules (data/schedules.json) are silently never evaluated or fired.'),
    ('register_camera_routes(',
     '/video_feed and the rest of api/api_camera.py\'s live-camera routes.'),
    ('register_camera_manager_routes(',
     'Devices tab camera discovery/rescan/switch routes (api/api_camera_manager.py).'),
    ('register_chat_routes(',
     'Every chat/message send/receive route (api/api_chat.py) - /api/send, /api/messages, etc.'),
    ('register_settings_routes(',
     'The Settings page backend (api/api_settings.py).'),
    ('listen_meshtastic',
     'The long-lived `meshtastic --listen` subprocess this whole app is built '
     'around (see CLAUDE.md) - without starting this thread, no radio traffic '
     'is ever parsed at all.'),
]


def main() -> int:
    text = SERVER_PY.read_text(encoding='utf-8')
    missing = [(needle, why) for needle, why in REQUIRED_CALLS if needle not in text]

    if not missing:
        print(f'OK - all {len(REQUIRED_CALLS)} required startup calls found in server.py')
        return 0

    print(f'FAIL - server.py is missing {len(missing)} expected startup call(s):\n', file=sys.stderr)
    for needle, why in missing:
        print(f'  Missing: {needle}', file=sys.stderr)
        print(f'  Breaks:  {why}\n', file=sys.stderr)
    print(
        'If this is a deliberate removal (the feature was actually retired), '
        'update REQUIRED_CALLS in this script in the same commit. If it\'s not '
        'deliberate, this is exactly the regression class this script exists to '
        'catch - see the module docstring.',
        file=sys.stderr,
    )
    return 1


if __name__ == '__main__':
    sys.exit(main())
