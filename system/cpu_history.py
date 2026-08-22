"""CPU/RAM usage sampling, on-disk history, and the /api/system/cpu-history
route.

Extracted 1:1 from server.py's old "CPU USAGE HISTORY" block - no logic
changed, only decoupled from server.py itself: the history file path
(previously the module-level CPU_HISTORY_FILE constant, derived from
server.py's DATA_DIR) is now an explicit parameter on every function that
needs it, so this module has no dependency on server.py or config.py.
Registers its own Flask route via register_cpu_history_routes(), following
the same register_<area>_routes(app, ...) dependency-injection pattern as
api/*.py (see CLAUDE.md's Architecture section) rather than a Blueprint.
"""

import threading
import time
from collections import deque
from pathlib import Path

from flask import jsonify, request

from storage.json_store import safe_read_json, safe_write_json

CPU_SAMPLE_INTERVAL = 2.0
CPU_HISTORY_RETENTION = 24 * 60 * 60

cpu_history = deque()
cpu_history_lock = threading.RLock()
_cpu_prev_total = None
_cpu_prev_idle = None
_cpu_current_usage = 0.0


def get_current_usage():
    """Accessor for _cpu_current_usage - lets external code (e.g. server.py's
    e-Paper callbacks) read the latest sample without reaching into this
    module's private state directly."""
    return _cpu_current_usage


def read_cpu_times():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            parts = fh.readline().split()
        if not parts or parts[0] != "cpu":
            return None, None
        values = [int(value) for value in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total, idle
    except Exception:
        return None, None


def read_cpu_temperature():
    for path in (
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
    ):
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
            return round(float(raw) / 1000.0, 1)
        except Exception:
            continue
    return None


def read_memory_percent():
    try:
        values = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total <= 0:
            return None
        return round((total - available) * 100.0 / total, 1)
    except Exception:
        return None


def read_uptime_seconds():
    """Seconds since the kernel booted - the first number in /proc/uptime
    (the second is the sum of all cores' idle time, unused here). Distinct
    from a Meshtastic node's own uptime_seconds telemetry (that's the
    radio's own reported uptime, not this Raspberry Pi's - see the e-Paper
    System Screen, task 37, which needed the host's uptime and found
    nothing already reading it)."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return float(fh.readline().split()[0])
    except Exception:
        return None


def load_cpu_history(cpu_history_file):
    data = safe_read_json(cpu_history_file, {"cpu": []})
    records = data.get("cpu", []) if isinstance(data, dict) else []
    cutoff = time.time() - CPU_HISTORY_RETENTION
    with cpu_history_lock:
        cpu_history.clear()
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                ts = float(item.get("timestamp", 0))
                usage = float(item.get("usage", 0))
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                cpu_history.append({"timestamp": ts, "usage": round(max(0.0, min(100.0, usage)), 1)})


def save_cpu_history(cpu_history_file):
    with cpu_history_lock:
        payload = {"cpu": list(cpu_history)}
    safe_write_json(cpu_history_file, payload)


def cpu_history_worker(cpu_history_file):
    global _cpu_prev_total, _cpu_prev_idle, _cpu_current_usage
    _cpu_prev_total, _cpu_prev_idle = read_cpu_times()
    last_save = 0.0
    while True:
        time.sleep(CPU_SAMPLE_INTERVAL)
        total, idle = read_cpu_times()
        if total is None or idle is None:
            continue
        if _cpu_prev_total is not None and total > _cpu_prev_total:
            delta_total = total - _cpu_prev_total
            delta_idle = idle - _cpu_prev_idle
            usage = 100.0 * (delta_total - delta_idle) / delta_total
            _cpu_current_usage = round(max(0.0, min(100.0, usage)), 1)
            now = time.time()
            cutoff = now - CPU_HISTORY_RETENTION
            with cpu_history_lock:
                cpu_history.append({"timestamp": now, "usage": _cpu_current_usage})
                while cpu_history and cpu_history[0]["timestamp"] < cutoff:
                    cpu_history.popleft()
            if now - last_save >= 60:
                try:
                    save_cpu_history(cpu_history_file)
                    last_save = now
                except Exception as exc:
                    print(f"[CPU] History save error: {exc}", flush=True)
        _cpu_prev_total, _cpu_prev_idle = total, idle


def downsample_cpu_records(records, max_points):
    if len(records) <= max_points:
        return records
    bucket_size = len(records) / max_points
    result = []
    for index in range(max_points):
        start = int(index * bucket_size)
        end = max(start + 1, int((index + 1) * bucket_size))
        bucket = records[start:end]
        if not bucket:
            continue
        result.append({
            "timestamp": bucket[-1]["timestamp"],
            "usage": round(sum(item["usage"] for item in bucket) / len(bucket), 1),
        })
    return result


def register_cpu_history_routes(app, cpu_history_file):
    @app.route("/api/system/cpu-history")
    def api_system_cpu_history():
        range_key = str(request.args.get("range", "30m")).lower()
        ranges = {"30m": 1800, "1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400}
        seconds = ranges.get(range_key, 1800)
        cutoff = time.time() - seconds
        with cpu_history_lock:
            records = [dict(item) for item in cpu_history if item["timestamp"] >= cutoff]
        max_points = 900 if range_key == "30m" else 720
        records = downsample_cpu_records(records, max_points)
        return jsonify({
            "ok": True,
            "range": range_key,
            "current": _cpu_current_usage,
            "temperature": read_cpu_temperature(),
            "ram_percent": read_memory_percent(),
            "records": records,
        })
