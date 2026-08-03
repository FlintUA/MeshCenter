import os
import time

from config import DATA_DIR
from storage.json_store import safe_read_json, safe_write_json
from utils.helpers import now

TELEMETRY_FILE = os.path.join(DATA_DIR, "telemetry_history.json")


def configure_storage(filepath):
    """Point telemetry persistence at the active radio profile before loading data."""
    global TELEMETRY_FILE
    TELEMETRY_FILE = str(filepath)


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

    data = safe_read_json(TELEMETRY_FILE, {})
    if data:
        telemetry_history = data.get("history", [])
        telemetry_config = data.get("config", {"interval": 300, "enabled": True})

        max_records = 26000
        if len(telemetry_history) > max_records:
            telemetry_history = telemetry_history[-max_records:]
            save_telemetry()
    else:
        save_telemetry()


def save_telemetry():
    data = {
        "config": telemetry_config,
        "history": telemetry_history,
    }
    safe_write_json(TELEMETRY_FILE, data)


def add_telemetry_record(temp, humidity, pressure, voltage, current):
    global telemetry_history, telemetry_last_save_time

    current_time = time.time()
    interval = telemetry_config.get("interval", 300)

    # Voltage is valid telemetry too. Many Meshtastic nodes expose only
    # device voltage, without current or environmental sensors. Do not drop
    # those samples, otherwise local-node Power History stays empty.
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

    max_records = 26000
    if len(telemetry_history) > max_records:
        telemetry_history = telemetry_history[-max_records:]

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
    # packets.  When they arrive inside one history interval, merge them into
    # the latest record instead of discarding the later packets.  This keeps a
    # single complete sample per node and interval.
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

    max_records = 26000
    if len(telemetry_history) > max_records:
        telemetry_history = telemetry_history[-max_records:]

    save_telemetry()
    return True
