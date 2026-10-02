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


# H1-B2 (F6): replaces the old single 26000-record cap SHARED across every
# node plus local - a mesh with several chatty remote nodes could evict
# local's own history (or another node's) well before it, with no
# per-node fairness at all. Each node (local counts as its own bucket,
# keyed by node_id=None - see _trim_node_history()) now gets its own
# independent cap instead.
#
# Sized to hold 30 days of history at the DEFAULT 300s interval
# (86400*30/300 = 8640 records) with headroom for a shorter configured
# interval or a burst of merged device/environment/power samples - not
# sized for the shortest allowed interval (120s would need 21600 for 30
# days), since the task's own instruction is "30 days at default
# interval". A local record is ~110-140 bytes of JSON (time, timestamp,
# temperature, humidity, pressure, voltage, current, power, source); a
# remote node's record adds node_id/battery_level/channel_utilization/
# air_util_tx/uptime_seconds, up to ~220 bytes.
#
# Worst-case file size, reported as asked rather than assumed: dev
# (!756f9960) has 507 entries in /api/nodes_export as of 2026-10-01 -
# most of those are only ever seen via NODEINFO/routing on the mesh and
# never send this node actual TELEMETRY_APP packets, so realistic usage
# is a small fraction of the theoretical cap. The true worst case if
# EVERY one of those 507 nodes filled its own 10000-record bucket is
# 507 * 10000 * ~220 bytes =~ 1.06 GB for telemetry_history.json - a real
# number worth flagging to the reviewer, not a size that happens in
# practice (it would require 507 distinct radios each sending telemetry
# at the default interval continuously for 30+ days), but large enough
# that a node seeing many hundreds of mesh-visible devices should budget
# SD card space accordingly, or the cap should be lowered/made
# configurable in a follow-up if that theoretical ceiling is a concern.
MAX_RECORDS_PER_NODE = 10000

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


def _trim_node_history(node_key):
    """Drop the OLDEST records for one node/local bucket if it exceeds
    MAX_RECORDS_PER_NODE - every other bucket (every other node_id, or
    local when trimming a remote node's bucket) is left completely
    untouched. `node_key` is the record's own `record.get("node_id")`
    value - None for a local record (see add_telemetry_record()'s record
    shape, which never sets "node_id" at all). Must be called with
    _telemetry_lock already held. Returns True if anything was removed."""
    global telemetry_history

    matching_indices = [
        i for i, record in enumerate(telemetry_history)
        if isinstance(record, dict) and record.get("node_id") == node_key
    ]
    excess = len(matching_indices) - MAX_RECORDS_PER_NODE
    if excess <= 0:
        return False

    remove_indices = set(matching_indices[:excess])
    telemetry_history = [
        record for i, record in enumerate(telemetry_history)
        if i not in remove_indices
    ]
    return True


def _trim_all_history_per_node():
    """Like _trim_node_history(), but for every distinct node_key present -
    used once at load time, since a file saved before MAX_RECORDS_PER_NODE
    existed (or saved by an older build) could have any node over the cap,
    not just whichever one happens to be written to next. Must be called
    with _telemetry_lock already held."""
    keys = {
        record.get("node_id") for record in telemetry_history
        if isinstance(record, dict)
    }
    trimmed = False
    for key in keys:
        if _trim_node_history(key):
            trimmed = True
    return trimmed


def load_telemetry():
    global telemetry_history, telemetry_config

    with _telemetry_lock:
        data = safe_read_json(TELEMETRY_FILE, {})
        if data:
            telemetry_history = data.get("history", [])
            telemetry_config = data.get("config", {"interval": 300, "enabled": True})

            if _trim_all_history_per_node():
                save_telemetry()
        else:
            save_telemetry()


def save_telemetry():
    with _telemetry_lock:
        data = {
            "config": telemetry_config,
            "history": telemetry_history,
        }
        safe_write_json(TELEMETRY_FILE, data)


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
        _trim_node_history(None)

        telemetry_last_save_time = current_time
        save_telemetry()
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
                save_telemetry()
            return changed

        record = {
            "node_id": node_id,
            "source": source,
            "time": now(),
            "timestamp": timestamp,
            **fields,
        }
        telemetry_history.append(record)
        _trim_node_history(node_id)

        save_telemetry()
        return True
