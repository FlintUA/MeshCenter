import atexit
import os
import threading
import time

from config import DATA_DIR
from storage.json_store import safe_read_json, safe_write_json
from utils.helpers import now

TELEMETRY_FILE = os.path.join(DATA_DIR, "telemetry_history.json")

# F4.1 PR 2: save_telemetry() took no lock of its own. The serial --listen
# parser thread and the TCP inbound worker thread could both call
# add_telemetry_record()/add_node_telemetry_record() concurrently with each
# other, and server.py's telemetry-export route read telemetry_history.json
# directly off disk with no lock at all - json.dump() iterating
# telemetry_history while another thread appends to it is a real race (a
# torn write, or in rare cases a RuntimeError from the list changing size
# mid-iteration). One reentrant lock (load_telemetry() calls
# save_telemetry() internally) held for every history mutation + save, and
# by get_history_snapshot() for its own read, closes this for every caller.
_telemetry_lock = threading.RLock()


def configure_storage(filepath):
    """Point telemetry persistence at the active radio profile before loading data."""
    global TELEMETRY_FILE
    TELEMETRY_FILE = str(filepath)


# H1-B2 review fix (F6): per-node caps ALONE still allowed an unbounded
# total file size - MAX_RECORDS_PER_NODE=10000 applied to every node
# meant dev's 505 known nodes could theoretically reach ~1.9 GB (505 *
# 10000 * ~374 bytes/record, measured via save_telemetry()'s own
# json.dumps(indent=2) shape), rewritten+fsynced on an SD card on every
# single telemetry save. Three independent limits now apply together:
#
# - MAX_LOCAL_RECORDS: local's own budget, unchanged from the original
#   H1-B2 sizing - 30 days of history at the DEFAULT 300s interval
#   (86400*30/300 = 8640 records) with headroom for a shorter configured
#   interval or a burst of merged device/environment/power samples.
# - MAX_RECORDS_PER_REMOTE_NODE: a much smaller per-remote-node budget -
#   1000 records at the default interval is ~3.5 days of per-node
#   history, plenty for "what has this specific remote node been doing
#   recently" (this feature's actual use case - long-term 30-day trend
#   analysis is a local-node feature, see MAX_LOCAL_RECORDS). Combined
#   with MAX_TOTAL_RECORDS below, one chatty remote node can never
#   consume more than 2.5% of the whole history file's budget by itself.
# - MAX_TOTAL_RECORDS: a hard ceiling on telemetry_history's TOTAL length
#   regardless of how many distinct nodes exist - the actual bound on
#   worst-case file size, since per-node caps alone can't bound a
#   mesh with hundreds of nodes. ~374 bytes/record * 40000 =~ 15 MB, a
#   number that holds no matter how many remote nodes a mesh has. When
#   exceeded, the OLDEST REMOTE records are evicted first (see
#   _enforce_global_ceiling()) - local history is never evicted by
#   remote traffic, matching MAX_LOCAL_RECORDS' own independent budget.
#
# A record is ~110-140 bytes of JSON for local (time, timestamp,
# temperature, humidity, pressure, voltage, current, power, source); a
# remote node's record adds node_id/battery_level/channel_utilization/
# air_util_tx/uptime_seconds, up to ~220 bytes - 374 bytes/record above
# is the actually-measured json.dumps(indent=2) figure including
# object/key overhead, not the raw field-byte estimate.
MAX_LOCAL_RECORDS = 10000
MAX_RECORDS_PER_REMOTE_NODE = 1000
MAX_TOTAL_RECORDS = 40000

# A per-key record count, maintained incrementally (incremented on
# append, decremented on eviction) instead of recomputed by scanning
# telemetry_history on every single call - the previous _trim_node_history()
# scanned+rebuilt the WHOLE list on every append regardless of whether
# anything was actually over its cap, exactly the "O(N) full-history scan
# on every append" the review flagged. Keyed the same way as every
# record's own "node_id" field: None for local. Rebuilt from scratch
# (_rebuild_record_counts()) at load time; any test that mutates
# telemetry_history directly (bypassing add_telemetry_record()/
# add_node_telemetry_record()) must call it too before relying on
# cap-enforcement behavior.
_record_counts = {}


def _rebuild_record_counts():
    """O(N) - meant to run once (at load time), not per-append."""
    global _record_counts
    counts = {}
    for record in telemetry_history:
        if not isinstance(record, dict):
            continue
        key = record.get("node_id")
        counts[key] = counts.get(key, 0) + 1
    _record_counts = counts


def _record_key_cap(key):
    return MAX_LOCAL_RECORDS if key is None else MAX_RECORDS_PER_REMOTE_NODE


def _evict_oldest_for_key(key, cap):
    """Removes the OLDEST records for one key until its count is back at
    `cap` - O(N) (a flat shared list has no cheaper way to drop a
    specific key's oldest entries), but only runs when that key is
    actually over its own cap, not on every append; every other key's
    records are left untouched. Must be called with _telemetry_lock held."""
    global telemetry_history
    excess = _record_counts.get(key, 0) - cap
    if excess <= 0:
        return
    removed = 0
    kept = []
    for record in telemetry_history:
        if removed < excess and isinstance(record, dict) and record.get("node_id") == key:
            removed += 1
            continue
        kept.append(record)
    telemetry_history = kept
    _record_counts[key] = _record_counts.get(key, 0) - removed


def _enforce_global_ceiling():
    """Hard-bounds telemetry_history's total length at MAX_TOTAL_RECORDS by
    evicting the OLDEST REMOTE records first - local history (key=None)
    is never touched here, matching MAX_LOCAL_RECORDS' own independent
    budget. The length check is O(1), so this is a no-op on every append
    that doesn't cross the ceiling; the eviction itself is O(N) same as
    _evict_oldest_for_key() above, for the same reason. Must be called
    with _telemetry_lock held."""
    global telemetry_history
    excess = len(telemetry_history) - MAX_TOTAL_RECORDS
    if excess <= 0:
        return
    removed = 0
    kept = []
    for record in telemetry_history:
        if removed < excess and isinstance(record, dict) and record.get("node_id") is not None:
            key = record.get("node_id")
            _record_counts[key] = _record_counts.get(key, 1) - 1
            removed += 1
            continue
        kept.append(record)
    telemetry_history = kept


def _note_appended(key):
    """Call immediately after appending one record for `key` - O(1)
    bookkeeping, then enforces that key's own cap and the global ceiling
    (both no-ops, O(1) to check, unless actually exceeded). Must be
    called with _telemetry_lock held."""
    _record_counts[key] = _record_counts.get(key, 0) + 1
    _evict_oldest_for_key(key, _record_key_cap(key))
    _enforce_global_ceiling()


def _enforce_all_caps_after_load():
    """Every distinct key's own cap, then the global ceiling - used once
    at load time, since a file saved before these limits existed (or by
    an older build, or hand-edited) could have any key over its cap, or
    the file as a whole over the global ceiling, not just whichever key
    happens to receive the next append. Must be called with
    _telemetry_lock held. Returns True if anything was removed."""
    before = len(telemetry_history)
    for key in list(_record_counts.keys()):
        _evict_oldest_for_key(key, _record_key_cap(key))
    _enforce_global_ceiling()
    return len(telemetry_history) != before


telemetry_history = []
telemetry_config = {"interval": 300, "enabled": True}
telemetry_current = {
    "temperature": None,
    "humidity": None,
    "pressure": None,
    "voltage": None,
    "current": None,
    "power": None,
    "last_update": None,
    "timestamp": 0,
}
telemetry_last_save_time = 0


def load_telemetry():
    global telemetry_history, telemetry_config

    with _telemetry_lock:
        data = safe_read_json(TELEMETRY_FILE, {})
        if data:
            telemetry_history = data.get("history", [])
            telemetry_config = data.get("config", {"interval": 300, "enabled": True})
            _rebuild_record_counts()
            if _enforce_all_caps_after_load():
                save_telemetry()
        else:
            _rebuild_record_counts()
            save_telemetry()


# H1-C5: save_telemetry() used to run synchronously on whichever thread
# accepted a record (the serial `--listen` parser or the TCP inbound
# worker) - rewriting the ENTIRE history (indent=2, fsync'd) under
# _telemetry_lock on every single accepted record. Measured on dev (Pi
# Zero 2W) at a 40k-record history: ~5s per save, during which that
# ingest thread (and anything else waiting on _telemetry_lock) was
# blocked. Fixed by debouncing: add_telemetry_record()/
# add_node_telemetry_record() now only flip a dirty flag (_mark_dirty()),
# and telemetry_flush_worker() - a background thread started once from
# server.py's start_runtime(), for every transport - writes at most once
# every TELEMETRY_FLUSH_INTERVAL_S while dirty. The write itself always
# happens OUTSIDE _telemetry_lock (see flush_telemetry()), so a slow SD-
# card write can never block an ingest thread that only needs the lock
# briefly to append one record.
#
# Trade-off, accepted and documented (see README's telemetry section): a
# crash (not a clean shutdown - see shutdown_telemetry() below) can lose
# up to TELEMETRY_FLUSH_INTERVAL_S seconds of telemetry HISTORY. Nothing
# else is affected - live values (telemetry_current, node device/
# environment/power metrics) are updated directly by server.py's
# apply_telemetry_values()/apply_node_telemetry() independently of this
# buffer, and every other JSON-backed store in this project still saves
# synchronously, unchanged.
TELEMETRY_FLUSH_INTERVAL_S = 60

_telemetry_dirty = False
# Set to wake telemetry_flush_worker() before its normal tick - used by
# request_flush() (explicit config/enabled changes) and shutdown_telemetry()
# (clean shutdown), never by the two ingest functions themselves (they are
# exactly the hot path this debounce exists to keep off the write path).
_flush_now_event = threading.Event()
_flush_worker_stop = threading.Event()


def _mark_dirty():
    """Caller MUST already hold _telemetry_lock - same contract as
    _note_appended() and friends above."""
    global _telemetry_dirty
    _telemetry_dirty = True


def flush_telemetry(force=False):
    """Writes telemetry_history/telemetry_config to disk if dirty (or
    unconditionally when `force=True`). The snapshot (a plain dict - no
    deep copy needed, see get_history_snapshot()'s own comment on why a
    shallow list copy is enough once nothing mutates records in place
    after this point) is taken, and the dirty flag cleared, under
    _telemetry_lock; the actual json.dumps()+fsync - the slow part on an
    SD card - happens OUTSIDE it. If that write fails, the dirty flag is
    set again (regardless of what a concurrent append may have already
    set it to) so the NEXT tick retries rather than silently losing the
    pending write forever. Returns True if a write was attempted and
    succeeded, False if nothing was dirty (and not forced) or the write
    itself failed."""
    global _telemetry_dirty
    with _telemetry_lock:
        if not (_telemetry_dirty or force):
            return False
        data = {
            "config": telemetry_config,
            "history": telemetry_history,
        }
        _telemetry_dirty = False

    ok = safe_write_json(TELEMETRY_FILE, data, indent=None)
    if not ok:
        with _telemetry_lock:
            _telemetry_dirty = True
    return ok


def request_flush(wait=False):
    """For explicit, infrequent admin actions (interval/enabled config
    changes via /api/telemetry/config) that should not wait out the full
    debounce window - the user just changed a setting and expects it
    persisted now, not up to 60s later. Marks dirty and wakes the
    background worker immediately; `wait=True` additionally performs the
    flush on the CALLING thread right away (used by shutdown_telemetry(),
    where the process is exiting and there is no later tick to rely on
    the woken worker actually running before exit)."""
    global _telemetry_dirty
    with _telemetry_lock:
        _telemetry_dirty = True
    _flush_now_event.set()
    if wait:
        flush_telemetry()


def telemetry_flush_worker():
    """Background debounce loop - see the module-level comment above
    flush_telemetry() for the full rationale. Runs for the lifetime of
    the process, like every other background worker in server.py's
    "Background threads" group (CLAUDE.md), for every transport (unlike
    the serial-only group) since telemetry can arrive via TCP too."""
    while not _flush_worker_stop.is_set():
        _flush_now_event.wait(timeout=TELEMETRY_FLUSH_INTERVAL_S)
        _flush_now_event.clear()
        if _flush_worker_stop.is_set():
            break
        flush_telemetry()


def shutdown_telemetry():
    """Flush on clean shutdown: stops telemetry_flush_worker()'s loop and
    performs one final flush if anything is still dirty, so a graceful
    process exit (gunicorn worker restart/stop, a manual service restart)
    never loses the last few seconds of telemetry history the way a crash
    would. Registered with atexit at module import time - there is no
    other single "runtime stop path" in this codebase to hook into (see
    CLAUDE.md: server.py has no stop_runtime() counterpart to
    start_runtime()) - and also safe to call directly or more than once.

    Deliberately calls flush_telemetry() directly rather than
    request_flush(wait=True): the latter unconditionally marks dirty
    before flushing (correct for ITS OWN contract - an explicit "I just
    changed something, persist it now" caller), which would make every
    shutdown force a write even when nothing was pending, contradicting
    this function's own "if anything is still dirty" contract above and
    wasting a write (and, on a Pi, SD-card wear) for no reason."""
    _flush_worker_stop.set()
    _flush_now_event.set()
    flush_telemetry()


atexit.register(shutdown_telemetry)


def save_telemetry():
    """Immediate, synchronous, forced flush - unchanged name/contract for
    existing callers (load_telemetry()'s own startup saves below, and
    /api/telemetry/config's explicit interval/enabled changes in
    server.py, which want their change persisted right away, not
    debounced). Implemented via flush_telemetry(force=True) so it shares
    the same snapshot-outside-lock behavior and compact-JSON encoding as
    the debounced path, rather than a second, divergent write
    implementation."""
    flush_telemetry(force=True)


def get_history_snapshot():
    """An isolated, point-in-time copy of telemetry_history - each record
    dict is copied too (not just the outer list), since
    add_node_telemetry_record()'s merge path mutates a record in place, not
    just appends. Callers (server.py's telemetry-export route) read this
    instead of re-reading TELEMETRY_FILE off disk - it can never observe a
    half-written file, and the same lock as every writer means it can never
    observe the list mid-mutation either."""
    with _telemetry_lock:
        return [dict(record) if isinstance(record, dict) else record for record in telemetry_history]


def add_telemetry_record(temp, humidity, pressure, voltage, current):
    global telemetry_history, telemetry_last_save_time

    with _telemetry_lock:
        # H1-B1 (F5): telemetry_config["enabled"] used to be written by
        # /api/telemetry/config but never read anywhere - toggling it had
        # no effect at all. Per the agreed semantics, disabling only stops
        # WRITING HISTORY: live values (telemetry_current, sensor_data,
        # base_status, node device/environment/power_metrics) are updated
        # directly by server.py's apply_telemetry_values()/
        # apply_node_telemetry() regardless of this flag, independently of
        # the add_*_record() calls these two functions gate.
        if not telemetry_config.get("enabled", True):
            return False

        current_time = time.time()
        interval = telemetry_config.get("interval", 300)

        # Voltage is valid telemetry too. Many Meshtastic nodes expose only
        # device voltage, without current or environmental sensors. Do not
        # drop those samples, otherwise local-node Power History stays empty.
        if all(value is None for value in (temp, humidity, pressure, voltage, current)):
            return False

        if current_time - telemetry_last_save_time < interval:
            return False

        power = None
        try:
            if voltage is not None and current is not None:
                power = float(voltage) * float(current)
        except Exception:
            power = None

        record = {
            "time": now(),
            "timestamp": current_time,
            "temperature": temp,
            "humidity": humidity,
            "pressure": pressure,
            "voltage": voltage,
            "current": current,
            "power": power,
            "source": "local",
        }

        telemetry_history.append(record)
        _note_appended(None)

        telemetry_last_save_time = current_time
        _mark_dirty()
        return True


def add_node_telemetry_record(node_id, values, source="passive"):
    """Append telemetry history for one Meshtastic node.

    Records are rate-limited per node using the configured telemetry interval.
    Empty updates are ignored and never overwrite another node's history.
    """
    global telemetry_history

    if not node_id or not isinstance(values, dict):
        return False

    fields = {
        "temperature": values.get("temperature"),
        "humidity": values.get("humidity"),
        "pressure": values.get("pressure"),
        "voltage": values.get("voltage"),
        "current": values.get("current"),
        "power": values.get("power"),
        "battery_level": values.get("battery_level"),
        "channel_utilization": values.get("channel_utilization"),
        "air_util_tx": values.get("air_util_tx"),
        "uptime_seconds": values.get("uptime_seconds"),
    }

    if all(value is None for value in fields.values()):
        return False

    with _telemetry_lock:
        # H1-B1 (F5): see add_telemetry_record()'s identical comment above -
        # same gate, same semantics (live per-node fields are updated by
        # apply_node_telemetry() regardless, independently of this call).
        if not telemetry_config.get("enabled", True):
            return False

        timestamp = time.time()
        interval = max(30, int(telemetry_config.get("interval", 300) or 300))

        last_record = None
        last_timestamp = 0.0
        for record in reversed(telemetry_history):
            if isinstance(record, dict) and record.get("node_id") == node_id:
                last_record = record
                try:
                    last_timestamp = float(record.get("timestamp", 0) or 0)
                except (TypeError, ValueError):
                    last_timestamp = 0.0
                break

        if fields["power"] is None:
            try:
                if fields["voltage"] is not None and fields["current"] is not None:
                    fields["power"] = float(fields["voltage"]) * float(fields["current"])
            except (TypeError, ValueError):
                fields["power"] = None

        # Meshtastic sends Device, Environment and Power metrics as separate
        # packets.  When they arrive inside one history interval, merge them
        # into the latest record instead of discarding the later packets.
        # This keeps a single complete sample per node and interval.
        if last_record is not None and timestamp - last_timestamp < interval:
            changed = False
            for key, value in fields.items():
                if value is not None and last_record.get(key) != value:
                    last_record[key] = value
                    changed = True

            if changed:
                last_record["source"] = source
                last_record["updated_at"] = timestamp
                _mark_dirty()
            return changed

        record = {
            "node_id": node_id,
            "source": source,
            "time": now(),
            "timestamp": timestamp,
            **fields,
        }
        telemetry_history.append(record)
        _note_appended(node_id)

        _mark_dirty()
        return True
