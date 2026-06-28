import os
import time

from config import DATA_DIR
from storage.json_store import safe_read_json, safe_write_json
from utils.helpers import now

TELEMETRY_FILE = os.path.join(DATA_DIR, "telemetry_history.json")

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

    if temp is None and humidity is None and pressure is None and current is None:
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
    }

    telemetry_history.append(record)

    max_records = 26000
    if len(telemetry_history) > max_records:
        telemetry_history = telemetry_history[-max_records:]

    telemetry_last_save_time = current_time
    save_telemetry()
    return True
