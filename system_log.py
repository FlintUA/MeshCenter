import json
import os
import sys
import threading
from datetime import datetime

from config import DATA_DIR

SYSTEM_LOG_FILE = os.path.join(DATA_DIR, "system_events.jsonl")
MAX_SYSTEM_LOG_EVENTS = 1000
_MAX_FILE_SIZE = 1024 * 1024
_log_lock = threading.RLock()

# F4.1 PR 2: log_system_event() is called from everywhere - request
# handlers, background workers, error-handling paths themselves - with no
# try/except at most call sites. On a full disk, an uncaught write failure
# here would turn every single log call anywhere in the app into an
# unhandled exception. Tracks whether the last attempt failed, so a
# failure streak reports once (to stderr, since the thing that's broken
# is persistence itself) and stays quiet after that, instead of either
# raising or spamming one print per call.
_log_write_failing = False


def _normalize_level(level):
    value = str(level or "INFO").upper()
    return value if value in {"INFO", "WARNING", "ERROR", "ACTION", "OK"} else "INFO"


def log_system_event(title, level="INFO", details="", source="system"):
    global _log_write_failing

    now = datetime.now().astimezone()
    event = {
        "timestamp": now.timestamp(),
        "datetime": now.isoformat(timespec="seconds"),
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M:%S"),
        "level": _normalize_level(level),
        "source": str(source or "system")[:40],
        "event": str(title or "Event")[:160],
        "details": str(details or "")[:1000],
    }

    with _log_lock:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SYSTEM_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            if _log_write_failing:
                _log_write_failing = False  # recovered - the next failure reports again

            try:
                if os.path.getsize(SYSTEM_LOG_FILE) > _MAX_FILE_SIZE:
                    _trim_log_locked()
            except OSError:
                pass
        except OSError as error:
            if not _log_write_failing:
                _log_write_failing = True
                print(f"[SYSLOG] Could not persist event (further failures suppressed until it recovers): {error}", file=sys.stderr, flush=True)

    return event


def _trim_log_locked():
    events = _read_events_locked(MAX_SYSTEM_LOG_EVENTS)
    temp_file = SYSTEM_LOG_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(temp_file, SYSTEM_LOG_FILE)


def _read_events_locked(limit):
    if not os.path.exists(SYSTEM_LOG_FILE):
        return []

    events = []
    try:
        with open(SYSTEM_LOG_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        events.append(item)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    return events[-max(1, int(limit)):]


def get_system_events(limit=100, level=None, source=None):
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100

    with _log_lock:
        events = _read_events_locked(MAX_SYSTEM_LOG_EVENTS)

    if level:
        wanted_level = str(level).upper()
        events = [event for event in events if str(event.get("level", "")).upper() == wanted_level]

    if source:
        wanted_source = str(source).lower()
        events = [event for event in events if str(event.get("source", "")).lower() == wanted_source]

    return events[-limit:]
