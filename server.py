#!/usr/bin/env python3
"""
MeshCenter - Web Control Center for Meshtastic nodes on Raspberry Pi Zero 2W
"""

from flask import Flask, request, jsonify, render_template, Response, send_from_directory, make_response
from functools import wraps
import subprocess
import threading
import time
import re
import json
import os
import io
import csv
from collections import defaultdict, deque
from datetime import datetime
from camera import camera
from telemetry import telemetry
from meshsrv import meshsrv
from api.api_camera import register_camera_routes
from api.api_chat import register_chat_routes
from api.api_settings import register_settings_routes
from api.api_system import register_system_routes
from system_log import log_system_event
from api.api_node_tools import register_node_tools_routes

try:
    from config import *
except ImportError:
    print("=" * 60)
    print("❌ ERROR: config.py not found!")
    print("=" * 60)
    print("Please create config.py from config.example.py")
    print("=" * 60)
    exit(1)

required_vars = [
    "APP_HOST", "APP_PORT", "MESHTASTIC_CMD", "LOCAL_NODE_ID", "LOCAL_NODE_NAME",
    "DATA_DIR", "HISTORY_FILE", "NODES_FILE", "SENSORS_FILE", "CHATS_FILE",
    "MAX_HISTORY_MESSAGES", "CHANNEL_CHAT_ID", "CHANNEL_CHAT_NAME",
    "KNOWN_NODES", "KNOWN_NODE_INFO"
]

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

NODE_DEBUG_LOG = os.path.join(DATA_DIR, "nodes_debug.log")

try:
    MESHTASTIC_PORT
except NameError:
    MESHTASTIC_PORT = "/dev/ttyACM0"

missing_vars = []
for var in required_vars:
    if var not in dir():
        missing_vars.append(var)

if missing_vars:
    print("=" * 60)
    print("❌ ERROR: config.py is missing required variables!")
    print("Missing variables:", missing_vars)
    print("=" * 60)
    exit(1)

if not os.path.exists(MESHTASTIC_CMD):
    print(f"⚠️ WARNING: meshtastic not found at: {MESHTASTIC_CMD}")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# Папка для скриншотов
SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")
if not os.path.exists(SCREENSHOTS_DIR):
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

app = Flask(__name__)

def handle_errors(f):
    """Декоратор для обработки ошибок в API"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"[ERROR] {f.__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            return jsonify({
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc() if app.debug else None
            }), 500
    return decorated_function

register_camera_routes(app, camera, handle_errors)
register_system_routes(app)

# ===== STATIC FILES =====
@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory('static', filename)

def safe_read_json(filepath, default=None):
    """Безопасное чтение JSON с проверкой временных файлов"""
    if default is None:
        default = {}
    
    tmp_file = filepath + ".tmp"
    if os.path.exists(tmp_file):
        try:
            os.remove(tmp_file)
            print(f"[JSON] Removed stale tmp file: {tmp_file}", flush=True)
        except Exception as e:
            print(f"[JSON] Could not remove tmp file: {e}", flush=True)
    
    if not os.path.exists(filepath):
        return default
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[JSON] Read error: {e}, using default", flush=True)
        return default

def safe_write_json(filepath, data):
    """Безопасная атомарная запись JSON"""
    tmp_file = filepath + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, filepath)
        return True
    except Exception as e:
        print(f"[JSON] Write error: {e}", flush=True)
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except:
            pass
        return False

def atomic_write_json(filepath, data):
    return safe_write_json(filepath, data)

def extract_json_block(text, start_pos):
    """Извлекает JSON блок из текста начиная с указанной позиции"""
    brace_start = text.find("{", start_pos)
    if brace_start < 0:
        return None
    brace_count = 0
    brace_end = -1
    for i in range(brace_start, len(text)):
        if text[i] == '{':
            brace_count += 1
        elif text[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                brace_end = i
                break
    if brace_end < 0:
        return None
    return text[brace_start:brace_end + 1]

# ============================================================
# ВСЕ ОСТАЛЬНЫЕ ФУНКЦИИ (Meshtastic, чаты, телеметрия и т.д.)
# ============================================================

# ===== ГЛОБАЛЬНЫЙ LOCK ДЛЯ ПОТОКОБЕЗОПАСНОСТИ =====
state_lock = threading.RLock()
radio_lock = threading.RLock()

messages = []
seen_ids = set()
seen_recent_texts = {}
nodes = {}
chats = {}
settings = {}

sensor_data = {
    "temperature": None, "humidity": None, "pressure": None,
    "voltage": None, "current": None, "power": None,
    "battery_percent": None, "air_quality": None, "last_update": None
}

base_status = {
    "battery_level": None, "real_battery": None, "voltage": None,
    "channel_utilization": None, "air_util_tx": None,
    "uptime_seconds": None, "last_update": None
}

listen_process = None
pause_listen = threading.Event()

radio_health = {
    "status": "STARTING",
    "status_reason": "Service is starting",

    "listener_running": False,

    "last_packet": 0,
    "last_text": 0,
    "last_telemetry": 0,
    "last_send": 0,

    "last_restart": 0,
    "restart_count": 0,

    "last_check": 0,
    "last_check_time": None,

    "last_ok": 0,
    "last_ok_time": None,

    "fail_count": 0,
    "last_error": "",
    "history": []
}

# ===== TELEMETRY BUFFER =====
# Состояние и история телеметрии вынесены в telemetry/telemetry.py.
# В server.py пока оставляем парсер и буфер, чтобы рефакторинг был безопасным.
telemetry_buffer_lock = threading.RLock()
telemetry_pending_values = {}
telemetry_pending_time = 0
TELEMETRY_DEBOUNCE_SECONDS = 1.5

def _radio_history_locked(event, level="INFO", details=""):
    """Добавляет событие Radio Health в память и постоянный системный журнал."""
    item = log_system_event(
        title=event,
        level=level,
        details=details,
        source="radio",
    )

    history = radio_health.setdefault("history", [])
    history.append(item)

    # Быстрый оперативный кэш. Полная история хранится в system_events.jsonl.
    if len(history) > 50:
        del history[:-50]


def radio_event(event, error=""):
    now_ts = time.time()

    with state_lock:
        if event == "listener_start":
            was_running = bool(
                radio_health.get("listener_running", False)
            )

            radio_health["listener_running"] = True

            # Не создаём повторные записи, если состояние уже было True
            if not was_running:
                _radio_history_locked(
                    "Listener started",
                    "INFO",
                    "Meshtastic listener is running"
                )

        elif event == "listener_stop":
            was_running = bool(
                radio_health.get("listener_running", False)
            )

            radio_health["listener_running"] = False

            if was_running:
                if pause_listen.is_set():
                    _radio_history_locked(
                        "Listener paused",
                        "INFO",
                        "Listener stopped temporarily for a radio command"
                    )
                else:
                    _radio_history_locked(
                        "Listener stopped",
                        "ERROR",
                        "Meshtastic listener exited unexpectedly"
                    )

        elif event == "packet":
            radio_health["last_packet"] = now_ts

        elif event == "telemetry":
            radio_health["last_packet"] = now_ts
            radio_health["last_telemetry"] = now_ts

        elif event == "text":
            radio_health["last_packet"] = now_ts
            radio_health["last_text"] = now_ts

        elif event == "send":
            radio_health["last_send"] = now_ts
            radio_health["last_error"] = ""

            _radio_history_locked(
                "Message sent",
                "INFO",
                "Meshtastic message sent successfully"
            )

        elif event == "send_error":
            error_text = str(error or "Unknown send error")[:300]

            radio_health["last_error"] = error_text
            radio_health["fail_count"] = (
                int(radio_health.get("fail_count", 0)) + 1
            )

            _radio_history_locked(
                "Send error",
                "ERROR",
                error_text
            )

        elif event == "restart":
            radio_health["last_restart"] = now_ts
            radio_health["restart_count"] = (
                int(radio_health.get("restart_count", 0)) + 1
            )

            _radio_history_locked(
                "Listener restart requested",
                "ACTION",
                "Manual listener restart"
            )

# ===== АТОМАРНАЯ ЗАПИСЬ JSON =====
# Используем safe_read_json и safe_write_json

def now():
    return time.strftime("%H:%M:%S")

def timestamp_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def voltage_to_percent(voltage):
    try:
        v = float(voltage)
        if v >= 4.20: return 100
        elif v >= 4.15: return 95
        elif v >= 4.10: return 90
        elif v >= 4.05: return 85
        elif v >= 4.00: return 80
        elif v >= 3.95: return 70
        elif v >= 3.90: return 60
        elif v >= 3.85: return 50
        elif v >= 3.80: return 40
        elif v >= 3.75: return 30
        elif v >= 3.70: return 20
        elif v >= 3.60: return 10
        else: return 0
    except Exception:
        return None

def node_num_to_id(num):
    try:
        hex_str = format(int(num) & 0xFFFFFFFF, "08x")
        return "!" + hex_str
    except Exception:
        return ""

def normalize_node_id(node_id):
    if not node_id: return None
    if node_id.startswith("!") and len(node_id) == 9:
        return node_id
    if node_id.startswith("!1p"):
        hex_part = node_id[3:]
        if len(hex_part) == 8:
            return "!" + hex_part
    if re.match(r'^[0-9a-fA-F]{8}$', node_id):
        return "!" + node_id
    if node_id.startswith("!") and len(node_id) != 9:
        hex_part = re.search(r'[0-9a-fA-F]{8}', node_id)
        if hex_part:
            return "!" + hex_part.group(0)
    return node_id

def normalize_node_id_with_aliases(node_id):
    if not node_id: return None
    return normalize_node_id(node_id)

def is_valid_node_id(node_id):
    if not node_id: return False
    if node_id == CHANNEL_CHAT_ID: return True
    return node_id.startswith("!") and len(node_id) >= 5

def sanitize_text(text):
    if not text: return ""
    if len(text) > 500: text = text[:500]
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return text

def friendly_unknown_node_name(node_id):
    if node_id and node_id.startswith("!") and len(node_id) >= 5:
        return "Meshtastic " + node_id[-4:]
    return node_id or "Unknown"

def get_node_name(node_id):
    if not node_id:
        return "Unknown"
    if node_id in KNOWN_NODES:
        return KNOWN_NODES[node_id]
    if node_id in nodes:
        name = nodes[node_id].get("name", "")
        if name and name != node_id and not name.startswith("node "):
            return name
    return friendly_unknown_node_name(node_id)

def get_node_info(node_id):
    return KNOWN_NODE_INFO.get(node_id, {"short_name": "", "hw_model": ""})

def save_messages():
    with state_lock:
        safe_write_json(HISTORY_FILE, messages[-MAX_HISTORY_MESSAGES:])

def load_messages():
    data = safe_read_json(HISTORY_FILE, [])
    with state_lock:
        messages.clear()
        if data:
            messages.extend(data[-MAX_HISTORY_MESSAGES:])

def save_chats():
    with state_lock:
        safe_write_json(CHATS_FILE, chats)

def load_chats():
    data = safe_read_json(CHATS_FILE, {})
    with state_lock:
        chats.clear()
        if data:
            chats.update(data)

        if CHANNEL_CHAT_ID not in chats:
            chats[CHANNEL_CHAT_ID] = {
                "id": CHANNEL_CHAT_ID,
                "name": CHANNEL_CHAT_NAME,
                "type": "channel",
                "last_message": "",
                "last_time": "",
                "unread": 0
            }
            save_chats()

def save_nodes():
    with state_lock:
        safe_write_json(NODES_FILE, nodes)

def load_nodes():
    data = safe_read_json(NODES_FILE, {})
    with state_lock:
        nodes.clear()
        if data:
            nodes.update(data)

def log_node_event(event, source, node_id, old=None, new=None, raw=None, extra=None):
    try:
        old = old or {}
        new = new or {}
        extra = extra or {}

        changed = {}
        keys = set(old.keys()) | set(new.keys())

        for key in keys:
            if old.get(key) != new.get(key):
                changed[key] = {
                    "old": old.get(key),
                    "new": new.get(key)
                }

        if not changed and event not in ("SKIP_LOCAL_NODE", "ERROR"):
            return

        lines = [
            "=" * 70,
            f"TIME: {now()}",
            f"EVENT: {event}",
            f"SOURCE: {source}",
            f"NODE_ID: {node_id}",
        ]

        if changed:
            lines.append("CHANGED:")
            for key, value in changed.items():
                lines.append(f"  {key}: {value['old']} -> {value['new']}")

        if extra:
            lines.append("EXTRA:")
            for key, value in extra.items():
                lines.append(f"  {key}: {value}")

        if raw:
            lines.append("RAW:")
            lines.append(str(raw)[:1200])

        lines.append("=" * 70)
        lines.append("")

        with open(NODE_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

    except Exception as e:
        print(f"[NODE_LOG] Error: {e}", flush=True)            

def save_sensors():
    with state_lock:
        safe_write_json(SENSORS_FILE, sensor_data)
        
def load_sensors_data():
    global sensor_data
    data = safe_read_json(SENSORS_FILE, {})
    if data:
        sensor_data = data
    else:
        save_sensors()

def default_settings():
    return {
        "units": {
            "temperature": "c",
            "pressure": "hpa",
            "wind": "ms"
        },
        "listener_autorecovery": {
            "enabled": False,
            "delay": 0
        }
    }

def save_settings():
    with state_lock:
        safe_write_json(SETTINGS_FILE, settings)

def load_settings():
    data = safe_read_json(SETTINGS_FILE, default_settings())

    if not isinstance(data, dict):
        data = default_settings()

    units = data.get("units", {})
    recovery = data.get("listener_autorecovery", {})
    if not isinstance(recovery, dict):
        recovery = {}

    if not isinstance(units, dict):
        units = {}

    normalized_settings = dict(data)

    normalized_settings["units"] = {
        "temperature": units.get("temperature", "c")
            if units.get("temperature", "c") in ("c", "f", "both")
            else "c",

        "pressure": units.get("pressure", "hpa")
            if units.get("pressure", "hpa") in ("hpa", "mmhg", "both")
            else "hpa",

        "wind": units.get("wind", "ms")
            if units.get("wind", "ms") in ("ms", "kmh", "mph")
            else "ms",
    }

    normalized_settings["listener_autorecovery"] = {
        "enabled": bool(recovery.get("enabled", False)),
        "delay": int(recovery.get("delay", 60))
    }

    settings.clear()
    settings.update(normalized_settings)

    save_settings()

def ensure_chat(node_id, node_name=None, force=False):
    if node_id == CHANNEL_CHAT_ID or not node_id or not node_id.startswith("!"):
        return

    deleted_file = os.path.join(DATA_DIR, "deleted_dm.json")

    if not force and os.path.exists(deleted_file):
        try:
            with open(deleted_file, "r") as f:
                deleted_data = json.load(f)
                if node_id in deleted_data.get("deleted", []):
                    return
        except (json.JSONDecodeError, IOError) as e:
            print(f"[WARN] Could not read deleted_dm.json: {e}")

    name = node_name or get_node_name(node_id)

    if node_id in chats:
        old_name = chats[node_id].get("name", "")

        if name and old_name != name:
            chats[node_id]["name"] = name
            save_chats()

            try:
                log_node_event(
                    "CHAT_RENAME",
                    "ENSURE_CHAT",
                    node_id,
                    old={"chat_name": old_name},
                    new={"chat_name": name}
                )
            except Exception:
                pass

        return

    chats[node_id] = {
        "id": node_id,
        "name": name,
        "type": "dm",
        "last_message": "",
        "last_time": "",
        "unread": 0
    }

    save_chats()

    try:
        log_node_event(
            "CHAT_CREATE",
            "ENSURE_CHAT",
            node_id,
            new={"chat_name": name}
        )
    except Exception:
        pass

def update_chat_last_message(chat_id, text, time_str):
    if chat_id in chats:
        chats[chat_id]["last_message"] = text[:100]
        chats[chat_id]["last_time"] = time_str
        save_chats()

def reset_unread(chat_id):
    if chat_id in chats:
        chats[chat_id]["unread"] = 0
        save_chats()

# ===== TELEMETRY FUNCTIONS =====
def _float_or_none(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None

def _regex_number(line, patterns):
    for pattern in patterns:
        m = re.search(pattern, line, re.IGNORECASE)
        if m:
            return _float_or_none(m.group(1))
    return None

def _telemetry_from_local_node(line):
    try:
        from_id = extract_field(line, ["fromId"])
        if from_id:
            return normalize_node_id(from_id) == LOCAL_NODE_ID

        m = re.search(r"['\"]from['\"]:\s*(\d+)", line)
        if m:
            return node_num_to_id(m.group(1)) == LOCAL_NODE_ID
    except Exception:
        pass
    return LOCAL_NODE_ID in line

def parse_telemetry_from_listen_line(line):
    if "TELEMETRY_APP" not in line and "environmentMetrics" not in line and "powerMetrics" not in line and "deviceMetrics" not in line:
        return None

    if not _telemetry_from_local_node(line):
        return None

    temp = _regex_number(line, [
        r"['\"]temperature['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"temperature:\s*(-?\d+(?:\.\d+)?)"
    ])
    humidity = _regex_number(line, [
        r"['\"]relativeHumidity['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"relative_humidity:\s*(-?\d+(?:\.\d+)?)",
        r"relativeHumidity:\s*(-?\d+(?:\.\d+)?)"
    ])
    pressure = _regex_number(line, [
        r"['\"]barometricPressure['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"barometric_pressure:\s*(-?\d+(?:\.\d+)?)",
        r"barometricPressure:\s*(-?\d+(?:\.\d+)?)"
    ])

    voltage = _regex_number(line, [
        r"['\"]ch1Voltage['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"ch1_voltage:\s*(-?\d+(?:\.\d+)?)",
        r"['\"]voltage['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"voltage:\s*(-?\d+(?:\.\d+)?)"
    ])
    current = _regex_number(line, [
        r"['\"]ch1Current['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"ch1_current:\s*(-?\d+(?:\.\d+)?)",
        r"['\"]current['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"current:\s*(-?\d+(?:\.\d+)?)"
    ])

    battery_level = _regex_number(line, [
        r"['\"]batteryLevel['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"battery_level:\s*(-?\d+(?:\.\d+)?)"
    ])
    channel_utilization = _regex_number(line, [
        r"['\"]channelUtilization['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"channel_utilization:\s*(-?\d+(?:\.\d+)?)"
    ])
    air_util_tx = _regex_number(line, [
        r"['\"]airUtilTx['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"air_util_tx:\s*(-?\d+(?:\.\d+)?)"
    ])
    uptime_seconds = _regex_number(line, [
        r"['\"]uptimeSeconds['\"]:\s*(-?\d+(?:\.\d+)?)",
        r"uptime_seconds:\s*(-?\d+(?:\.\d+)?)"
    ])

    values = {
        "temperature": temp,
        "humidity": humidity,
        "pressure": pressure,
        "voltage": voltage,
        "current": current,
        "battery_level": battery_level,
        "channel_utilization": channel_utilization,
        "air_util_tx": air_util_tx,
        "uptime_seconds": uptime_seconds
    }

    if all(v is None for v in values.values()):
        return None
    return values

def apply_telemetry_values(values, save_history=True):
    global sensor_data, base_status

    if not values:
        return False

    current = telemetry.telemetry_current

    temp = values.get("temperature") if values.get("temperature") is not None else current.get("temperature")
    humidity = values.get("humidity") if values.get("humidity") is not None else current.get("humidity")
    pressure = values.get("pressure") if values.get("pressure") is not None else current.get("pressure")
    voltage = values.get("voltage") if values.get("voltage") is not None else current.get("voltage")
    current_ma = values.get("current") if values.get("current") is not None else current.get("current")

    power = None
    try:
        if voltage is not None and current_ma is not None:
            power = float(voltage) * float(current_ma)
    except Exception:
        power = None

    current_time = time.time()

    telemetry.telemetry_current.update({
        "temperature": temp,
        "humidity": humidity,
        "pressure": pressure,
        "voltage": voltage,
        "current": current_ma,
        "power": power,
        "last_update": now(),
        "timestamp": current_time
    })

    sensor_data.update({
        "temperature": temp,
        "humidity": humidity,
        "pressure": pressure,
        "voltage": voltage,
        "current": current_ma,
        "power": power,
        "battery_percent": voltage_to_percent(voltage) if voltage is not None else sensor_data.get("battery_percent"),
        "last_update": now()
    })
    save_sensors()

    if voltage is not None:
        base_status["voltage"] = voltage
        base_status["real_battery"] = voltage_to_percent(voltage)
    if values.get("battery_level") is not None and values.get("battery_level") != 101:
        base_status["battery_level"] = values.get("battery_level")
    elif voltage is not None:
        base_status["battery_level"] = voltage_to_percent(voltage)
    if values.get("channel_utilization") is not None:
        base_status["channel_utilization"] = values.get("channel_utilization")
    if values.get("air_util_tx") is not None:
        base_status["air_util_tx"] = values.get("air_util_tx")
    if values.get("uptime_seconds") is not None:
        base_status["uptime_seconds"] = values.get("uptime_seconds")
    base_status["last_update"] = now()

    if save_history:
        saved = telemetry.add_telemetry_record(temp, humidity, pressure, voltage, current_ma)

        if saved:
            print(f"[TELEMETRY] history saved: T={temp}, H={humidity}, P={pressure}, V={voltage}, I={current_ma}, W={power}", flush=True)
        else:
            print(f"[TELEMETRY] current updated: T={temp}, H={humidity}, P={pressure}, V={voltage}, I={current_ma}, W={power}", flush=True)
    else:
        print(f"[TELEMETRY] current updated only: T={temp}, H={humidity}, P={pressure}, V={voltage}, I={current_ma}, W={power}", flush=True)

    return True


def queue_telemetry_values(values):
    global telemetry_pending_values, telemetry_pending_time

    if not values:
        return False

    with telemetry_buffer_lock:
        for key, value in values.items():
            if value is not None:
                telemetry_pending_values[key] = value

        telemetry_pending_time = time.time()

    return True


def telemetry_buffer_worker():
    global telemetry_pending_values, telemetry_pending_time

    print("[TELEMETRY] Buffer worker started", flush=True)

    while True:
        time.sleep(0.25)

        try:
            values_to_apply = None

            with telemetry_buffer_lock:
                if telemetry_pending_values:
                    age = time.time() - telemetry_pending_time

                    if age >= TELEMETRY_DEBOUNCE_SECONDS:
                        values_to_apply = dict(telemetry_pending_values)
                        telemetry_pending_values = {}
                        telemetry_pending_time = 0

            if values_to_apply:
                with state_lock:
                    apply_telemetry_values(values_to_apply)

        except Exception as e:
            print(f"[TELEMETRY] Buffer worker error: {e}", flush=True)

def process_telemetry_line(line):
    values = parse_telemetry_from_listen_line(line)
    if values:
        return queue_telemetry_values(values)
    return False

def get_telemetry_from_info():
    global base_status

    try:
        result = meshsrv.get_info(MESHTASTIC_CMD, timeout=15)
        output = result.stdout + result.stderr

        node_pos = output.find(f'"{LOCAL_NODE_ID}"')
        if node_pos < 0:
            return

        temp = humidity = pressure = voltage = current = None
        battery = None

        env_pos = output.find('"environmentMetrics"', node_pos)
        if env_pos >= 0:
            block = extract_json_block(output, env_pos)
            if block:
                try:
                    env = json.loads(block)
                    temp = env.get("temperature")
                    humidity = env.get("relativeHumidity")
                    pressure = env.get("barometricPressure")
                    print(f"[INFO_TELEMETRY] Environment: temp={temp}, humidity={humidity}, pressure={pressure}", flush=True)
                except Exception as e:
                    print(f"[INFO_TELEMETRY] Error parsing environment: {e}", flush=True)

        power_pos = output.find('"powerMetrics"', node_pos)
        if power_pos >= 0:
            block = extract_json_block(output, power_pos)
            if block:
                try:
                    power_data = json.loads(block)
                    current = power_data.get("current")
                    print(f"[INFO_TELEMETRY] Power: current={current}mA", flush=True)
                except Exception as e:
                    print(f"[INFO_TELEMETRY] Error parsing power: {e}", flush=True)

        metrics_pos = output.find('"deviceMetrics"', node_pos)
        if metrics_pos >= 0:
            block = extract_json_block(output, metrics_pos)
            if block:
                try:
                    metrics = json.loads(block)
                    voltage = metrics.get("voltage")
                    battery = metrics.get("batteryLevel")
                    print(f"[INFO_TELEMETRY] Device: voltage={voltage}V, battery={battery}%", flush=True)
                except Exception as e:
                    print(f"[INFO_TELEMETRY] Error parsing device: {e}", flush=True)

        if voltage is not None or temp is not None or humidity is not None or pressure is not None or current is not None:
            values = {
                "temperature": temp,
                "humidity": humidity,
                "pressure": pressure,
                "voltage": voltage,
                "current": current,
                "battery_level": battery
            }

            with state_lock:
                apply_telemetry_values(values, save_history=False)

            print("[INFO_TELEMETRY] Applied telemetry from --info", flush=True)

    except Exception as e:
        print(f"[INFO_TELEMETRY] Error: {e}", flush=True)

def get_telemetry_export_records(
    data_type="all",
    range_minutes="all",
    start_ts=None,
    end_ts=None,
    series=""
    ):
    data = safe_read_json(os.path.join(DATA_DIR, "telemetry_history.json"), {})
    records = data.get("history", [])

    if not isinstance(records, list):
        records = []

    now = time.time()

    #
    # Priority:
    # 1. Custom start/end timestamps
    # 2. Quick range buttons
    #

    if start_ts is not None and end_ts is not None:

        try:
            start_ts = float(start_ts)
            end_ts = float(end_ts)

            records = [
                r for r in records
                if isinstance(r, dict)
                and start_ts <= float(r.get("timestamp", 0)) <= end_ts
            ]

        except Exception:
            records = []

    elif range_minutes != "all":

        try:
            minutes = int(range_minutes)
            cutoff = now - minutes * 60

            records = [
                r for r in records
                if isinstance(r, dict)
                and float(r.get("timestamp", 0)) >= cutoff
            ]

        except Exception:
            records = []

    selected_series = set()

    if series:
        selected_series = {
            s.strip().lower()
            for s in series.split(",")
            if s.strip()
        }

    clean = []

    for r in records:
        if not isinstance(r, dict):
            continue

        item = {
            "timestamp": r.get("timestamp"),
            "datetime": datetime.fromtimestamp(float(r.get("timestamp", 0))).strftime("%Y-%m-%d %H:%M:%S") if r.get("timestamp") else "",
            "temperature_c": r.get("temperature"),
            "humidity_percent": r.get("humidity"),
            "pressure_hpa": r.get("pressure"),
            "voltage_v": r.get("voltage"),
            "current_ma": r.get("current"),
            "power_mw": r.get("power"),
        }

        if data_type == "environment":
            row = {
                "timestamp": item["timestamp"],
                "datetime": item["datetime"],
            }

            if not selected_series or "temperature" in selected_series:
                row["temperature_c"] = item["temperature_c"]

            if not selected_series or "humidity" in selected_series:
                row["humidity_percent"] = item["humidity_percent"]

            if not selected_series or "pressure" in selected_series:
                row["pressure_hpa"] = item["pressure_hpa"]

            clean.append(row)

        elif data_type == "power":
            row = {
                "timestamp": item["timestamp"],
                "datetime": item["datetime"],
            }

            if not selected_series or "voltage" in selected_series:
                row["voltage_v"] = item["voltage_v"]

            if not selected_series or "current" in selected_series:
                row["current_ma"] = item["current_ma"]

            if not selected_series or "power" in selected_series:
                row["power_w"] = (item["power_mw"] / 1000) if item["power_mw"] is not None else None

            clean.append(row)

        else:
            clean.append(item)

    return clean


def records_to_csv(records):
    output = io.StringIO()

    if not records:
        return ""

    fieldnames = list(records[0].keys())
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(records)

    return output.getvalue()

def parse_nodes_from_info():
    global nodes
    try:
        result = subprocess.run([MESHTASTIC_CMD, "--info"], capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
        mesh_pos = output.find("Nodes in mesh: {")
        if mesh_pos < 0:
            mesh_pos = output.find("Nodes in mesh:")
            if mesh_pos < 0:
                return False
        block = extract_json_block(output, mesh_pos)
        if not block:
            return False
        data = json.loads(block)
        imported = 0
        updated = 0
        for node_id, node_data in data.items():
            if node_id == LOCAL_NODE_ID: continue
            user = node_data.get("user", {})
            long_name = user.get("longName", "")
            short_name = user.get("shortName", "")
            hw_model = user.get("hwModel", "")
            role = user.get("role", "CLIENT")
            snr = node_data.get("snr")
            last_heard = node_data.get("lastHeard")
            hops_away = node_data.get("hopsAway", 0)
            if not long_name or long_name == "Unknown": continue

            with state_lock:
                old = nodes.get(node_id, {})
                old_name = old.get("name", "")

                node = dict(old)

                node.update({
                    "name": long_name,
                    "node_id": node_id,
                    "last_seen": last_heard or old.get("last_seen", 0),
                    "last_time": (
                        time.strftime("%H:%M:%S", time.localtime(last_heard))
                        if last_heard else old.get("last_time", "never")
                    ),
                    "rssi": old.get("rssi"),
                    "snr": snr or old.get("snr"),
                    "hop_start": (
                        str(hops_away)
                        if hops_away > 0 else old.get("hop_start", "")
                    ),
                    "relay_node": old.get("relay_node", ""),
                    "last_text": old.get("last_text", ""),
                    "short_name": (
                        short_name
                        or old.get("short_name", "")
                        or node_id[-4:]
                    ),
                    "hw_model": hw_model or old.get("hw_model", ""),
                    "role": role or old.get("role", "CLIENT"),
                    "ignored": old.get("ignored", False),
                    "favorite": old.get("favorite", False)
                })

                nodes[node_id] = node

                if old_name and old_name != long_name:
                    updated += 1
                else:
                    imported += 1
                if node_id not in chats:
                    ensure_chat(node_id, long_name, force=True)
        if imported > 0 or updated > 0:
            save_nodes()
            save_chats()
            print(f"[PARSE] Imported {imported} new nodes, updated {updated} existing nodes")
            return True
        return False
    except Exception as e:
        print(f"[PARSE] Error: {e}")
        return False

def ensure_known_nodes():
    for node_id, name in KNOWN_NODES.items():

        with state_lock:
            old = nodes.get(node_id, {})
            info = get_node_info(node_id)

            node_data = dict(old)

            node_data.update({
                "name": name,
                "node_id": node_id,
                "last_seen": old.get("last_seen", 0),
                "last_time": old.get("last_time", "never"),
                "rssi": old.get("rssi"),
                "snr": old.get("snr"),
                "hop_start": old.get("hop_start", ""),
                "relay_node": old.get("relay_node", ""),
                "last_text": old.get("last_text", ""),
                "short_name": info.get(
                    "short_name",
                    old.get("short_name", "")
                ),
                "hw_model": info.get(
                    "hw_model",
                    old.get("hw_model", "")
                ),
                "role": old.get("role", "CLIENT"),
                "ignored": old.get("ignored", False),
                "favorite": old.get("favorite", False)
            })

            nodes[node_id] = node_data

            ensure_chat(node_id, name, force=True)

    save_nodes()

def normalize_unknown_nodes():
    global nodes
    changed = False
    with state_lock:
        for node_id, node in nodes.items():
            name = node.get("name", "")
            if not name or name == node_id or name.startswith("node "):
                node["name"] = get_node_name(node_id)
                changed = True
            if not node.get("short_name") and node_id.startswith("!"):
                node["short_name"] = node_id[-4:]
                changed = True
            if not node.get("role"):
                node["role"] = "CLIENT"
                changed = True
            if "ignored" not in node:
                node["ignored"] = False
                changed = True
            if "favorite" not in node:
                node["favorite"] = False
                changed = True
            if node_id.startswith("!") and node_id not in chats:
                ensure_chat(node_id, node.get("name"), force=True)
    if changed:
        save_nodes()

def extract_node_id(line):
    patterns = [
        r"'fromId':\s*'([^']+)'", r'"fromId":\s*"([^"]+)"',
        r"'id':\s*'(![0-9a-fA-F]+)'", r'"id":\s*"(![0-9a-fA-F]+)"',
        r'\bid:\s*"(![0-9a-fA-F]+)"', r'\bid:\s*(![0-9a-fA-F]+)',
        r"'from':\s*'([^']*)'", r'"from":\s*"([^"]*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, line)
        if m:
            node_id = m.group(1)
            if not node_id: continue
            if node_id.isdigit():
                return normalize_node_id_with_aliases(node_num_to_id(node_id))
            if node_id.startswith("!"):
                return normalize_node_id_with_aliases(node_id)
            if re.match(r'^[0-9a-fA-F]{8}$', node_id):
                return "!" + node_id
    m = re.search(r"'from':\s*(\d+)", line)
    if m:
        return normalize_node_id_with_aliases(node_num_to_id(m.group(1)))
    return None

def extract_nodeinfo_user_id(block):
    patterns = [
        r"'user':\s*\{[^}]*'id':\s*'(![0-9a-fA-F]+)'",
        r'"user":\s*\{[^}]*"id":\s*"(![0-9a-fA-F]+)"',
        r"\buser\s*\{[^}]*\bid:\s*\"(![0-9a-fA-F]+)\"",
        r"\bid:\s*\"(![0-9a-fA-F]+)\"",
        r"'id':\s*'(![0-9a-fA-F]+)'",
        r'"id":\s*"(![0-9a-fA-F]+)"',
    ]

    for pattern in patterns:
        m = re.search(pattern, block, re.DOTALL)
        if m:
            return normalize_node_id_with_aliases(m.group(1))

    return None

def extract_sender(line):
    node_id = extract_node_id(line)
    if node_id:
        return get_node_name(node_id)
    m = re.search(r"'from':\s*'([^']*)'", line)
    if m:
        name = m.group(1).strip()
        if name:
            return name
    return "RX"

def infer_node_id_from_sender(sender):
    if not sender: return ""
    if sender.startswith("!"): return sender
    for node_id, name in KNOWN_NODES.items():
        if sender == name: return node_id
    for node_id, node in nodes.items():
        if sender == node.get("name"): return node_id
    return ""

def extract_field(line, names):
    for name in names:
        patterns = [
            rf"'{name}':\s*'([^']*)'", rf'"{name}":\s*"([^"]*)"',
            rf"\b{name}:\s*\"([^\"]*)\"", rf"\b{name}:\s*'([^']*)'",
            rf"\b{name}:\s*([^\s,}}]+)"
        ]
        for pattern in patterns:
            m = re.search(pattern, line)
            if m:
                return m.group(1).strip()
    return None

def extract_packet_id(line):
    m = re.search(r"'id':\s*(\d+)", line)
    if m: return m.group(1)
    m = re.search(r"\bid:\s*(\d+)", line)
    if m: return m.group(1)
    return None

def extract_text_message(line):
    if "TEXT_MESSAGE_APP" not in line and "'text':" not in line and '"text":' not in line:
        return None
    patterns = [
        r"'text':\s*'([^']*)'", r'"text":\s*"([^"]*)"',
        r"'text':\s*\"([^\"]*)\"", r'"text":\s*\'([^\']*)\'',
    ]
    for pattern in patterns:
        m = re.search(pattern, line)
        if m:
            text = m.group(1).strip()
            if text:
                return text
    return None

def extract_rssi(line):
    m = re.search(r"'rxRssi':\s*(-?\d+)", line)
    return m.group(1) if m else None

def extract_snr(line):
    m = re.search(r"'rxSnr':\s*(-?\d+(?:\.\d+)?)", line)
    return m.group(1) if m else None

def extract_hop_start(line):
    m = re.search(r"'hopStart':\s*(\d+)", line)
    return m.group(1) if m else None

def extract_relay_node(line):
    m = re.search(r"'relayNode':\s*(\d+)", line)
    return m.group(1) if m else None

def update_node(line, sender, text):
    node_id = extract_node_id(line) or infer_node_id_from_sender(sender)

    if not node_id:
        return ""

    if node_id == LOCAL_NODE_ID:
        log_node_event(
            "SKIP_LOCAL_NODE",
            "TEXT_MESSAGE",
            node_id,
            extra={
                "sender": sender,
                "text": text
            },
            raw=line
        )
        return node_id

    rssi = extract_rssi(line)
    snr = extract_snr(line)
    hop_start = extract_hop_start(line)
    relay_node = extract_relay_node(line)
    role = extract_field(line, ["role", "Role"])

    name = get_node_name(node_id)
    info = get_node_info(node_id)

    with state_lock:
        old = nodes.get(node_id, {})

        old_snapshot = {
            "name": old.get("name"),
            "short_name": old.get("short_name"),
            "hw_model": old.get("hw_model"),
            "role": old.get("role"),
            "rssi": old.get("rssi"),
            "snr": old.get("snr"),
            "hop_start": old.get("hop_start"),
            "relay_node": old.get("relay_node"),
            "last_text": old.get("last_text")
        }

        # ВАЖНО:
        # TEXT_MESSAGE больше НЕ переименовывает ноду.
        # Имя может менять только NODEINFO / parse_nodes_from_info.
        stable_name = old.get("name") or name

        nodes[node_id] = {
            "name": stable_name,
            "node_id": node_id,
            "last_seen": time.time(),
            "last_time": now(),
            "rssi": rssi or old.get("rssi"),
            "snr": snr or old.get("snr"),
            "hop_start": hop_start or old.get("hop_start", ""),
            "relay_node": relay_node or old.get("relay_node", ""),
            "last_text": text or old.get("last_text", ""),
            "short_name": info.get("short_name") or old.get("short_name", "") or node_id[-4:],
            "hw_model": info.get("hw_model") or old.get("hw_model", ""),
            "role": role or old.get("role", "CLIENT"),
            "ignored": old.get("ignored", False),
            "favorite": old.get("favorite", False)
        }

        new_snapshot = {
            "name": nodes[node_id].get("name"),
            "short_name": nodes[node_id].get("short_name"),
            "hw_model": nodes[node_id].get("hw_model"),
            "role": nodes[node_id].get("role"),
            "rssi": nodes[node_id].get("rssi"),
            "snr": nodes[node_id].get("snr"),
            "hop_start": nodes[node_id].get("hop_start"),
            "relay_node": nodes[node_id].get("relay_node"),
            "last_text": nodes[node_id].get("last_text")
        }

        log_node_event(
            "UPDATE_NODE",
            "TEXT_MESSAGE",
            node_id,
            old=old_snapshot,
            new=new_snapshot,
            extra={
                "sender": sender,
                "text": text,
                "line_has_longName": "longName" in line
            },
            raw=line
        )

        if node_id.startswith("!"):
            ensure_chat(node_id, nodes[node_id].get("name"), force=True)

        save_nodes()

    return node_id

def process_nodeinfo(block):
    if ("NODEINFO_APP" not in block and "longName" not in block and "long_name" not in block and
        "shortName" not in block and "short_name" not in block and "hwModel" not in block and "hw_model" not in block):
        return False
    node_id = extract_nodeinfo_user_id(block)

    if not node_id:
        node_id = extract_node_id(block)

    if not node_id:
        return False
    outer_node_id = extract_node_id(block)

    if outer_node_id and outer_node_id != node_id:
        log_node_event(
            "NODEINFO_ID_MISMATCH",
            "NODEINFO",
            node_id,
            extra={
                "outer_node_id": outer_node_id,
                "user_node_id": node_id
            },
            raw=block
        )
           
    if node_id == LOCAL_NODE_ID:
        log_node_event(
            "SKIP_LOCAL_NODE",
            "NODEINFO",
            node_id,
            raw=block
        )
        return True
        
    long_name = extract_field(block, ["longName", "long_name", "longname"])
    short_name = extract_field(block, ["shortName", "short_name", "shortname"])
    hw_model = extract_field(block, ["hwModel", "hw_model"])
    role = extract_field(block, ["role", "Role"])
    rssi = extract_rssi(block)
    snr = extract_snr(block)
    hop_start = extract_hop_start(block)
    relay_node = extract_relay_node(block)
    name = KNOWN_NODES.get(node_id) or long_name or short_name or friendly_unknown_node_name(node_id)
    with state_lock:
        old = nodes.get(node_id, {})
        old_snapshot = {
        "name": old.get("name"),
        "short_name": old.get("short_name"),
        "hw_model": old.get("hw_model"),
        "role": old.get("role"),
        "rssi": old.get("rssi"),
        "snr": old.get("snr")
        }
        info = get_node_info(node_id)
        nodes[node_id] = {
            "name": name, "node_id": node_id,
            "last_seen": time.time(), "last_time": now(),
            "rssi": rssi or old.get("rssi"), "snr": snr or old.get("snr"),
            "hop_start": hop_start or old.get("hop_start", ""),
            "relay_node": relay_node or old.get("relay_node", ""),
            "last_text": old.get("last_text", ""),
            "short_name": info.get("short_name") or short_name or old.get("short_name", "") or node_id[-4:],
            "hw_model": info.get("hw_model") or hw_model or old.get("hw_model", ""),
            "role": role or old.get("role", "CLIENT"),
            "ignored": old.get("ignored", False),
            "favorite": old.get("favorite", False)
        }
        if node_id.startswith("!"):
            ensure_chat(node_id, name, force=True)
            new_snapshot = {
            "name": nodes[node_id].get("name"),
            "short_name": nodes[node_id].get("short_name"),
            "hw_model": nodes[node_id].get("hw_model"),
            "role": nodes[node_id].get("role"),
            "rssi": nodes[node_id].get("rssi"),
            "snr": nodes[node_id].get("snr")
        }

        log_node_event(
            "UPDATE_NODE",
            "NODEINFO",
            node_id,
            old=old_snapshot,
            new=new_snapshot,
            extra={
                "long_name": long_name,
                "short_name": short_name,
                "hw_model": hw_model,
                "role": role
            },
            raw=block
        )
        save_nodes()
    return True

def add_message(kind, sender, text, node_id="", chat_id=None, chat_name=None):
    with state_lock:
        if not node_id:
            node_id = infer_node_id_from_sender(sender)
        if node_id and node_id.startswith("!") and node_id != LOCAL_NODE_ID:
            if node_id not in chats:
                ensure_chat(node_id, sender or get_node_name(node_id), force=True)
        if chat_id is None:
            if kind == "system" or "SYSTEM" in sender:
                chat_id = CHANNEL_CHAT_ID
                chat_type = "channel"
            else:
                if node_id and node_id.startswith("!") and node_id != LOCAL_NODE_ID:
                    chat_id = node_id
                    chat_type = "dm"
                else:
                    chat_id = CHANNEL_CHAT_ID
                    chat_type = "channel"
        else:
            chat_type = "dm" if chat_id.startswith("!") else "channel"
        if chat_id == LOCAL_NODE_ID:
            chat_id = CHANNEL_CHAT_ID
            chat_type = "channel"
        if chat_type == "dm" and not chat_id.startswith("!"):
            chat_id = CHANNEL_CHAT_ID
            chat_type = "channel"
        if chat_name is None:
            chat_name = get_node_name(chat_id) if chat_type == "dm" else CHANNEL_CHAT_NAME
        if chat_type == "dm" and chat_id not in chats:
            ensure_chat(chat_id, chat_name, force=True)
        msg = {
            "kind": kind, "sender": sender, "node_id": node_id,
            "text": text, "time": now(),
            "chat_id": chat_id, "chat_type": chat_type, "chat_name": chat_name
        }
        messages.append(msg)
        messages[:] = messages[-MAX_HISTORY_MESSAGES:]
        update_chat_last_message(chat_id, text, msg["time"])
        if kind == "rx" and chat_id in chats:
            chats[chat_id]["unread"] = chats[chat_id].get("unread", 0) + 1
            save_chats()
        save_messages()
    return msg

def is_duplicate_text(sender, text, node_id=""):
    cleaned_text = text.strip()
    if not cleaned_text:
        return True
    
    if node_id:
        key = f"{sender}|{node_id}|{cleaned_text}"
    else:
        key = f"{sender}|{cleaned_text}"
    
    current_time = time.time()
    old_keys = [k for k, ts in seen_recent_texts.items() if current_time - ts > 15]
    for key_old in old_keys:
        del seen_recent_texts[key_old]
    
    old_time = seen_recent_texts.get(key)
    if old_time and current_time - old_time < 15:
        return True
    
    seen_recent_texts[key] = current_time
    return False

def node_status_icon(last_seen):
    if not last_seen: return "⚪"
    age = time.time() - last_seen
    if age < 120: return "🟢"
    if age < 900: return "🟡"
    return "🔴"

def age_text(last_seen):
    if not last_seen: return "not heard yet"
    age = int(time.time() - last_seen)
    if age < 60: return f"seen {age} sec ago"
    if age < 3600: return f"seen {age // 60} min ago"
    if age < 86400: return f"seen {age // 3600} h ago"
    return f"seen {age // 86400} d ago"

def signal_quality(rssi):
    if rssi is None or rssi == "": return ""
    try:
        value = int(float(rssi))
    except ValueError:
        return ""
    if value >= -90: return "good"
    if value >= -105: return "medium"
    return "weak"

def get_nodes_list():
    with state_lock:
        sorted_nodes = sorted(nodes.values(), key=lambda n: n.get("last_seen", 0), reverse=True)
        result = []
        for n in sorted_nodes:
            last_seen = n.get("last_seen", 0)
            icon = node_status_icon(last_seen)
            rssi = n.get("rssi")
            snr = n.get("snr")
            hop_start = n.get("hop_start", "")
            relay_node = n.get("relay_node", "")
            last_text = n.get("last_text", "")
            short_name = n.get("short_name", "")
            hw_model = n.get("hw_model", "")
            role = n.get("role", "CLIENT")
            ignored = n.get("ignored", False)
            favorite = n.get("favorite", False)
            quality = signal_quality(rssi)
            age = age_text(last_seen)
            age_display = age[5:] if age.startswith("seen ") else age
            meta_parts = []
            if quality: meta_parts.append("signal: " + quality)
            if rssi: meta_parts.append("RSSI: " + str(rssi) + " dBm")
            if snr: meta_parts.append("SNR: " + str(snr) + " dB")
            if hop_start: meta_parts.append("hops: " + str(hop_start))
            if relay_node: meta_parts.append("relay: " + str(relay_node))
            if short_name: meta_parts.append("short: " + str(short_name))
            if hw_model: meta_parts.append("hw: " + str(hw_model))
            if role: meta_parts.append("role: " + str(role))
            if ignored: meta_parts.append("🚫 ignored")
            if favorite: meta_parts.append("⭐ favorite")
            result.append({
                "name": icon + " " + n["name"],
                "clean_name": n["name"],
                "node_id": n["node_id"],
                "meta": " | ".join(meta_parts),
                "last_text": last_text,
                "short_name": short_name,
                "hw_model": hw_model,
                "role": role,
                "rssi": rssi,
                "snr": snr,
                "hop_start": hop_start,
                "relay_node": relay_node,
                "signal_quality": quality,
                "age": age_display,
                "ignored": ignored,
                "favorite": favorite,
                # последняя сохранённая позиция
                "position": n.get("position")
            })
    return result

def get_chats_list():
    with state_lock:
        chat_list = []
        total_unread = 0
        for chat_id, chat in chats.items():
            if chat_id.startswith("!") and nodes.get(chat_id, {}).get("ignored", False):
                continue
            is_favorite = nodes.get(chat_id, {}).get("favorite", False) if chat_id.startswith("!") else False
            unread = chat.get("unread", 0)
            total_unread += unread
            last_msg = chat.get("last_message", "")
            last_sender = ""
            last_sender_id = ""
            sender_display = ""
            for msg in reversed(messages):
                if msg.get("chat_id") == chat_id:
                    last_sender = msg.get("sender", "")
                    last_sender_id = msg.get("node_id", "")
                    break
            if chat_id == CHANNEL_CHAT_ID and last_sender:
                if last_sender_id:
                    sender_display = f"{last_sender} [{last_sender_id}]"
                else:
                    sender_display = last_sender
            chat_list.append({
                "id": chat_id, "name": chat.get("name", chat_id),
                "type": chat.get("type", "dm"), "last_message": last_msg,
                "last_time": chat.get("last_time", ""), "unread": unread,
                "is_channel": chat_id == CHANNEL_CHAT_ID,
                "ignored": chat_id.startswith("!") and nodes.get(chat_id, {}).get("ignored", False),
                "favorite": is_favorite, "last_sender": sender_display
            })
        def sort_key(c):
            if c["is_channel"]: return (0, "", "")
            if c["favorite"]: return (1, "", c["last_time"] or "")
            if c["unread"] > 0: return (2, "", c["last_time"] or "")
            return (3, "", c["last_time"] or "")
        chat_list.sort(key=sort_key)
    return chat_list, total_unread

def get_chat_messages(chat_id):
    with state_lock:
        return [m for m in messages if m.get("chat_id") == chat_id]

def stop_listener():
    global listen_process

    print("[DEBUG] Stopping listener...", flush=True)
    pause_listen.set()
    time.sleep(1.5)

    proc = listen_process

    if proc is None:
        print("[DEBUG] Listener already stopped", flush=True)
        return True

    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

        print("[DEBUG] Listener stopped", flush=True)
        return True

    except Exception as e:
        print(f"[WARN] Error stopping listener: {e}", flush=True)
        return False

    finally:
        listen_process = None
        time.sleep(1.0)

def wait_serial_release(device="/dev/ttyACM0", timeout=8):
    start = time.time()

    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                ["lsof", device],
                capture_output=True,
                text=True,
                timeout=2
            )

            out = result.stdout or ""

            if "meshtastic" not in out:
                return True

        except Exception as e:
            print(f"[WARN] wait_serial_release error: {e}", flush=True)

        time.sleep(0.2)

    print(f"[WARN] Serial port still busy after {timeout}s: {device}", flush=True)
    return False


def prepare_radio_command(device="/dev/ttyACM0", timeout=8):
    pause_listen.set()
    stop_listener()

    if not wait_serial_release(device=device, timeout=timeout):
        return False

    return True

def update_base_status_from_info():
    global base_status
    try:
        result = meshsrv.get_info(MESHTASTIC_CMD, timeout=15)
        output = result.stdout + result.stderr
        node_pos = output.find(f'"{LOCAL_NODE_ID}"')
        if node_pos < 0:
            print("Base status: local node id not found")
            return
        block = extract_json_block(output, output.find('"deviceMetrics"', node_pos))
        if not block:
            print("Base status: deviceMetrics not found")
            return
        metrics = json.loads(block)
        voltage = metrics.get("voltage")
        battery_level = metrics.get("batteryLevel")
        if battery_level == 101:
            battery_level = 100
        real_battery = voltage_to_percent(voltage)
        with state_lock:
            base_status = {
                "battery_level": battery_level,
                "real_battery": real_battery if real_battery is not None else battery_level,
                "voltage": voltage,
                "channel_utilization": metrics.get("channelUtilization"),
                "air_util_tx": metrics.get("airUtilTx"),
                "uptime_seconds": metrics.get("uptimeSeconds"),
                "last_update": now()
            }
        print("Base status updated:", base_status)
    except Exception as e:
        print(f"Base status update error: {e}")

def read_sensors_from_meshtastic():
    return sensor_data

def cleanup_seen_ids():
    global seen_ids, seen_recent_texts
    while True:
        time.sleep(300)
        if len(seen_ids) > 1000:
            seen_ids = set(list(seen_ids)[-500:])
        current_time = time.time()
        old_keys = [k for k, ts in seen_recent_texts.items() if current_time - ts > 60]
        for key in old_keys:
            del seen_recent_texts[key]

def listen_meshtastic():
    global listen_process, base_status

    nodeinfo_buffer = []
    collecting_nodeinfo = False
    consecutive_errors = 0
    max_consecutive_errors = 10

    while True:
        if pause_listen.is_set():
            time.sleep(0.5)
            continue

        listen_process = None

        try:
            time.sleep(0.5)

            print("[DEBUG] Starting listener...")

            with radio_lock:
                if pause_listen.is_set():
                    continue

                listen_process = subprocess.Popen(
                    [MESHTASTIC_CMD, "--listen"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    errors="ignore"
                )

                print(f"[DEBUG] Listener started with PID: {listen_process.pid}")
                radio_event("listener_start")
                consecutive_errors = 0

            for line in listen_process.stdout:
                if pause_listen.is_set():
                    break

                line = line.strip()

                radio_event("packet")

                if not line:
                    continue

                try:
                    if (
                        "WARNING" in line
                        or "ERROR" in line
                        or "disconnected" in line.lower()
                        or "multiple access" in line.lower()
                    ):
                        print(f"[LISTEN WARN] {line}", flush=True)

                    if (
                        "TELEMETRY_APP" in line
                        or "environmentMetrics" in line
                        or "powerMetrics" in line
                        or "deviceMetrics" in line
                    ):
                        try:
                            process_telemetry_line(line)
                        except Exception as e:
                            print(f"[TELEMETRY] Parse error: {e}", flush=True)

                    if "TEXT_MESSAGE_APP" in line or "'text':" in line or '"text":' in line:
                        print(f"[RAW] {line[:200]}...", flush=True)

                    # ===== ИЗМЕНЕНИЕ №1: Новая логика сбора NODEINFO =====
                    if "NODEINFO_APP" in line or collecting_nodeinfo:
                        collecting_nodeinfo = True
                        nodeinfo_buffer.append(line)
                        block = "\n".join(nodeinfo_buffer)

                        has_nodeinfo = (
                            "longName" in block
                            or "long_name" in block
                            or "shortName" in block
                            or "short_name" in block
                            or "hwModel" in block
                            or "hw_model" in block
                            or "'user':" in block
                            or '"user":' in block
                        )

                        nodeinfo_id = extract_nodeinfo_user_id(block)

                        if has_nodeinfo and nodeinfo_id:
                            with state_lock:
                                process_nodeinfo(block)
                            nodeinfo_buffer = []
                            collecting_nodeinfo = False
                            continue

                        # ===== ИЗМЕНЕНИЕ №2: Не обрабатывать буфер при переполнении =====
                        if len(nodeinfo_buffer) > 80:
                            log_node_event(
                                "DROP_NODEINFO_BUFFER",
                                "LISTENER",
                                nodeinfo_id or "",
                                extra={
                                    "buffer_lines": len(nodeinfo_buffer),
                                    "has_nodeinfo": has_nodeinfo
                                },
                                raw=block
                            )
                            print(f"[NODEINFO] Dropping oversized buffer ({len(nodeinfo_buffer)} lines)", flush=True)
                            nodeinfo_buffer = []
                            collecting_nodeinfo = False
                            continue

                        continue

                    # Ignore duplicate onReceive() debug events.
                    if "onReceive()" in line:
                        continue

                    text = extract_text_message(line)

                    radio_event("telemetry")

                    if not text:
                        continue

                    pid = extract_packet_id(line)

                    if pid:
                        if pid in seen_ids:
                            continue
                        seen_ids.add(pid)

                    sender = extract_sender(line)
                    node_id = update_node(line, sender, text)

                    # ===== ИЗМЕНЕНИЕ №4: Обновить sender после update_node =====
                    if node_id:
                        sender = get_node_name(node_id)

                    if is_duplicate_text(sender, text, node_id):
                        continue

                    if node_id and nodes.get(node_id, {}).get("ignored", False):
                        continue

                    chat_id = CHANNEL_CHAT_ID
                    is_channel = False

                    if (
                        "'to': 4294967295" in line
                        or '"to": 4294967295' in line
                        or "'to': '^all'" in line
                        or '"to": "^all"' in line
                        or "'toId': '^all'" in line
                        or '"toId": "^all"' in line
                        or "broadcast" in line.lower()
                    ):
                        is_channel = True
                    elif "'dest'" in line.lower() or '"dest"' in line.lower():
                        is_channel = False
                    elif "'to': '!" in line or '"to": "!"' in line:
                        is_channel = False
                    elif re.search(r"'to':\s*[0-9]+,", line) or re.search(r'"to":\s*[0-9]+,', line):
                        if "4294967295" not in line:
                            is_channel = False
                    else:
                        is_channel = True

                    if is_channel:
                        chat_id = CHANNEL_CHAT_ID
                    else:
                        if node_id and node_id.startswith("!") and node_id != LOCAL_NODE_ID:
                            chat_id = node_id
                        else:
                            from_match = re.search(r"'from':\s*'(![0-9a-f]+)'", line)

                            if not from_match:
                                from_match = re.search(r'"from":\s*"(![0-9a-f]+)"', line)

                            if from_match:
                                chat_id = from_match.group(1)
                            else:
                                chat_id = CHANNEL_CHAT_ID

                    # ===== ИЗМЕНЕНИЕ №3: ensure_chat без force=True и с именем из базы =====
                    if chat_id.startswith("!") and chat_id != LOCAL_NODE_ID:
                        with state_lock:
                            ensure_chat(
                                chat_id,
                                get_node_name(chat_id),
                                force=False
                            )

                    with state_lock:
                        add_message("rx", sender, text, node_id, chat_id)

                except Exception as e:
                    print(f"[LISTEN] Error processing line: {e}", flush=True)
                    continue

            return_code = listen_process.poll()

            if pause_listen.is_set():
                print("[DEBUG] Listener paused, terminating process...", flush=True)
                try:
                    listen_process.terminate()
                    listen_process.wait(timeout=3)
                except Exception:
                    try:
                        listen_process.kill()
                    except Exception:
                        pass
                
                radio_event("listener_stop")
                
                listen_process = None
                time.sleep(0.5)
                continue

            if return_code is not None and return_code != 0:
                print(f"[WARN] Listener process ended with code: {return_code}", flush=True)
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            radio_event("listener_stop")

            listen_process = None

        except Exception as e:
            consecutive_errors += 1
            print(f"[ERROR] listen_meshtastic (attempt {consecutive_errors}): {e}", flush=True)
            delay = min(consecutive_errors * 2, 30)
            print(f"[ERROR] Waiting {delay}s before restart...", flush=True)
            time.sleep(delay)

        if consecutive_errors > max_consecutive_errors:
            print("[FATAL] Too many listener errors, restarting process...", flush=True)
            consecutive_errors = 0
            time.sleep(5)
        else:
            time.sleep(2)

def telemetry_worker():
    print("[TELEMETRY] Worker started - listen-only mode", flush=True)

    while True:
        time.sleep(60)

        try:
            now_time = time.time()
            last_ts = telemetry.telemetry_current.get("timestamp", 0)

            if last_ts:
                age = int(now_time - last_ts)
                print(f"[TELEMETRY] Last data age: {age}s", flush=True)
            else:
                print("[TELEMETRY] No telemetry yet - waiting for --listen", flush=True)

        except Exception as e:
            print(f"[TELEMETRY] Worker error: {e}", flush=True)

# ============================================================
# LISTENER AUTO RECOVERY
# ============================================================

LISTENER_RECOVERY_MAX_ATTEMPTS = 3
LISTENER_RECOVERY_WINDOW = 30 * 60
LISTENER_RECOVERY_RESULT_TIMEOUT = 60

listener_recovery_state = {
    "down_since": None,
    "attempts": [],
    "restart_pending": False,
    "restart_requested_at": None,
    "limit_logged": False,
    "last_enabled": None,
}


def process_listener_autorecovery(status, listener_running, now_ts):
    """
    Restart the Meshtastic listener after a persistent LISTENER_DOWN state.

    Safety limits:
    - maximum 3 attempts in 30 minutes;
    - PAUSED, STARTING, IDLE and NO_PACKETS never trigger recovery;
    - recovery is cancelled if the listener returns before the delay expires.
    """
    state = listener_recovery_state

    with state_lock:
        recovery_settings = settings.get(
            "listener_autorecovery",
            {}
        ).copy()

    enabled = bool(
        recovery_settings.get("enabled", False)
    )

    try:
        delay = int(
            recovery_settings.get("delay", 60)
        )
    except (TypeError, ValueError):
        delay = 60

    if delay not in (30, 60, 90, 120, 180, 300):
        delay = 60

    # --------------------------------------------------------
    # ENABLE / DISABLE
    # --------------------------------------------------------

    if state["last_enabled"] is None:
        state["last_enabled"] = enabled

        if enabled:
            log_system_event(
                "INFO",
                "recovery",
                "Listener Auto Recovery enabled",
                f"Recovery delay: {delay} seconds",
            )

    elif state["last_enabled"] != enabled:
        state["last_enabled"] = enabled

        log_system_event(
            "INFO",
            "recovery",
            (
                "Listener Auto Recovery enabled"
                if enabled
                else "Listener Auto Recovery disabled"
            ),
            (
                f"Recovery delay: {delay} seconds"
                if enabled
                else "Automatic listener restart is disabled"
            ),
        )

    if not enabled:
        state["down_since"] = None
        state["restart_pending"] = False
        state["restart_requested_at"] = None
        state["attempts"] = []
        state["limit_logged"] = False
        return

    # Keep only attempts made inside the current 30-minute window.
    state["attempts"] = [
        timestamp
        for timestamp in state["attempts"]
        if now_ts - timestamp < LISTENER_RECOVERY_WINDOW
    ]

    if len(state["attempts"]) < LISTENER_RECOVERY_MAX_ATTEMPTS:
        state["limit_logged"] = False

    # --------------------------------------------------------
    # CHECK RESULT OF A PREVIOUS AUTOMATIC RESTART
    # --------------------------------------------------------

    if state["restart_pending"]:
        requested_at = state["restart_requested_at"] or now_ts

        if listener_running and status != "LISTENER_DOWN":
            log_system_event(
                "OK",
                "recovery",
                "Listener recovered successfully",
                "Automatic listener restart completed",
            )

            state["restart_pending"] = False
            state["restart_requested_at"] = None
            state["down_since"] = None
            return

        if (
            now_ts - requested_at
            >= LISTENER_RECOVERY_RESULT_TIMEOUT
        ):
            log_system_event(
                "WARNING",
                "recovery",
                "Automatic listener recovery failed",
                (
                    "Listener is still unavailable "
                    f"{LISTENER_RECOVERY_RESULT_TIMEOUT} seconds "
                    "after restart"
                ),
            )

            state["restart_pending"] = False
            state["restart_requested_at"] = None
            state["down_since"] = now_ts

        return

    # Only a real listener process failure triggers recovery.
    if status != "LISTENER_DOWN":
        if state["down_since"] is not None:
            log_system_event(
                "INFO",
                "recovery",
                "Automatic recovery cancelled",
                "Listener recovered before automatic restart",
            )

        state["down_since"] = None
        return

    # --------------------------------------------------------
    # START CONFIRMATION TIMER
    # --------------------------------------------------------

    if state["down_since"] is None:
        state["down_since"] = now_ts

        log_system_event(
            "WARNING",
            "recovery",
            "Listener failure detected",
            (
                f"Waiting {delay} seconds before "
                "automatic recovery"
            ),
        )
        return

    if now_ts - state["down_since"] < delay:
        return

    # --------------------------------------------------------
    # SAFETY LIMIT
    # --------------------------------------------------------

    if (
        len(state["attempts"])
        >= LISTENER_RECOVERY_MAX_ATTEMPTS
    ):
        if not state["limit_logged"]:
            log_system_event(
                "ERROR",
                "recovery",
                "Automatic recovery limit reached",
                (
                    f"{LISTENER_RECOVERY_MAX_ATTEMPTS} attempts "
                    "within 30 minutes. Manual action required."
                ),
            )
            state["limit_logged"] = True

        return

    # --------------------------------------------------------
    # RESTART LISTENER
    # --------------------------------------------------------

    attempt_number = len(state["attempts"]) + 1

    log_system_event(
        "ACTION",
        "recovery",
        "Automatic listener restart requested",
        (
            f"Attempt {attempt_number} of "
            f"{LISTENER_RECOVERY_MAX_ATTEMPTS}"
        ),
    )

    state["attempts"].append(now_ts)
    state["restart_pending"] = True
    state["restart_requested_at"] = now_ts
    state["down_since"] = None

    try:
        stop_listener()
        time.sleep(1)
        pause_listen.clear()

        radio_event("restart")

        print(
            "[RECOVERY] Automatic listener restart requested "
            f"(attempt {attempt_number}/"
            f"{LISTENER_RECOVERY_MAX_ATTEMPTS})",
            flush=True,
        )

    except Exception as error:
        state["restart_pending"] = False
        state["restart_requested_at"] = None
        state["down_since"] = now_ts

        log_system_event(
            "ERROR",
            "recovery",
            "Automatic listener restart failed",
            str(error),
        )

        print(
            f"[RECOVERY] Restart error: {error}",
            flush=True,
        )

def radio_health_worker():
    print("[RADIO] Passive health worker started", flush=True)

    while True:
        time.sleep(30)

        try:
            now_ts = time.time()

            with state_lock:
                listener_running = bool(
                    radio_health.get("listener_running", False)
                )

                last_packet = float(
                    radio_health.get("last_packet") or 0
                )
                last_telemetry = float(
                    radio_health.get("last_telemetry") or 0
                )
                last_send = float(
                    radio_health.get("last_send") or 0
                )

                packet_age = (
                    max(0, int(now_ts - last_packet))
                    if last_packet else None
                )
                telemetry_age = (
                    max(0, int(now_ts - last_telemetry))
                    if last_telemetry else None
                )
                send_age = (
                    max(0, int(now_ts - last_send))
                    if last_send else None
                )

                if pause_listen.is_set():
                    status = "PAUSED"
                    reason = "Listener temporarily paused for a radio command"
                    level = "WARNING"
                    recommendation = "Wait until the radio command is completed"

                elif not listener_running:
                    status = "LISTENER_DOWN"
                    reason = "Meshtastic listener is not running"
                    level = "ERROR"
                    recommendation = "Restart the Meshtastic listener"

                elif packet_age is None:
                    status = "STARTING"
                    reason = "Listener is running, waiting for the first packet"
                    level = "WARNING"
                    recommendation = "Wait for the first radio packet"

                elif packet_age <= 180:
                    status = "OK"
                    reason = "Recent radio activity detected"
                    level = "OK"
                    recommendation = "No action required"

                elif packet_age <= 600:
                    status = "IDLE"
                    reason = f"No packets received for {packet_age} seconds"
                    level = "WARNING"
                    recommendation = "No action required if the mesh is quiet"

                else:
                    status = "NO_PACKETS"
                    reason = f"No packets received for {packet_age} seconds"
                    level = "ERROR"
                    recommendation = (
                        "Check radio reception and try restarting the listener"
                    )

                previous_status = radio_health.get("status")

                radio_health["status"] = status
                radio_health["level"] = level
                radio_health["status_reason"] = reason
                radio_health["recommendation"] = recommendation

                radio_health["last_check"] = now_ts
                radio_health["last_check_time"] = now()

                radio_health["packet_age"] = packet_age
                radio_health["telemetry_age"] = telemetry_age
                radio_health["send_age"] = send_age

                if status == "OK":
                    radio_health["last_ok"] = now_ts
                    radio_health["last_ok_time"] = now()
                    radio_health["fail_count"] = 0
                    radio_health["last_error"] = ""

                elif status in ("LISTENER_DOWN", "NO_PACKETS"):
                    radio_health["fail_count"] = (
                        int(radio_health.get("fail_count", 0)) + 1
                    )

                if previous_status != status:
                    _radio_history_locked(
                        f"Status changed: {previous_status or 'UNKNOWN'} -> {status}",
                        level,
                        reason
                    )

            print(
                "[RADIO] "
                f"status={status}, "
                f"level={level}, "
                f"listener={listener_running}, "
                f"packet_age={packet_age}, "
                f"telemetry_age={telemetry_age}, "
                f"send_age={send_age}",
                flush=True
            )

            process_listener_autorecovery(
                status=status,
                listener_running=listener_running,
                now_ts=now_ts,
            )

        except Exception as e:
            error_text = str(e)

            with state_lock:
                previous_status = radio_health.get("status")

                radio_health["status"] = "ERROR"
                radio_health["level"] = "ERROR"
                radio_health["status_reason"] = "Radio health worker failed"
                radio_health["recommendation"] = (
                    "Check the MeshCenter service log"
                )
                radio_health["last_error"] = error_text
                radio_health["last_check"] = time.time()
                radio_health["last_check_time"] = now()
                radio_health["fail_count"] = (
                    int(radio_health.get("fail_count", 0)) + 1
                )

                if previous_status != "ERROR":
                    _radio_history_locked(
                        f"Status changed: {previous_status or 'UNKNOWN'} -> ERROR",
                        "ERROR",
                        error_text
                    )

            print(
                f"[RADIO] Health worker error: {error_text}",
                flush=True
            )
            
register_chat_routes(
    app,
    state_lock,
    chats,
    nodes,
    messages,
    save_chats,
    get_chats_list,
    get_chat_messages,
    get_nodes_list,
    is_valid_node_id,
    handle_errors,
    sanitize_text,
    CHANNEL_CHAT_ID,
    CHANNEL_CHAT_NAME,
    MESHTASTIC_CMD,
    LOCAL_NODE_ID,
    LOCAL_NODE_NAME,
    pause_listen,
    radio_lock,
    stop_listener,
    prepare_radio_command,
    get_node_name,
    ensure_chat,
    add_message,
    reset_unread,
    get_node_info,
    save_nodes,
    now,
    radio_event,
)

register_settings_routes(
    app,
    state_lock,
    settings,
    save_settings,
    handle_errors,
)

register_node_tools_routes(
    app=app,
    handle_errors=handle_errors,
    is_valid_node_id=is_valid_node_id,
    nodes=nodes,
    state_lock=state_lock,
    save_nodes=save_nodes,
    MESHTASTIC_CMD=MESHTASTIC_CMD,
    MESHTASTIC_PORT=MESHTASTIC_PORT,
    radio_lock=radio_lock,
    pause_listen=pause_listen,
    prepare_radio_command=prepare_radio_command,
    log_system_event=log_system_event,
)

# ============================================================
# API ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/sensors")
def api_sensors():
    return jsonify(sensor_data)

@app.route("/api/base_status")
def api_base_status():
    status = base_status.copy()
    status["node_name"] = LOCAL_NODE_NAME
    status["node_id"] = LOCAL_NODE_ID
    return jsonify(status)

@app.route("/api/node_status")
def api_node_status():
    node_id = request.args.get("node_id", "").strip()
    if not node_id or not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node_id"}), 400
    with state_lock:
        node = nodes.get(node_id, {})
    return jsonify({"ok": True, "node_id": node_id, "ignored": node.get("ignored", False), "favorite": node.get("favorite", False), "name": node.get("name", "Unknown")})

@app.route("/api/toggle_ignore", methods=["POST"])
@handle_errors
def api_toggle_ignore():
    data = request.get_json(force=True)
    node_id = data.get("node_id", "").strip()
    if not node_id or node_id not in nodes or not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node"}), 400
    with state_lock:
        nodes[node_id]["ignored"] = not nodes[node_id].get("ignored", False)
        save_nodes()
    return jsonify({"ok": True, "ignored": nodes[node_id]["ignored"]})

@app.route("/api/toggle_favorite", methods=["POST"])
@handle_errors
def api_toggle_favorite():
    data = request.get_json(force=True)
    node_id = data.get("node_id", "").strip()
    if not node_id or node_id not in nodes or not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node"}), 400
    with state_lock:
        nodes[node_id]["favorite"] = not nodes[node_id].get("favorite", False)
        save_nodes()
    return jsonify({"ok": True, "favorite": nodes[node_id]["favorite"]})

@app.route("/api/cleanup_nodes", methods=["POST"])
@handle_errors
def api_cleanup_nodes():
    with state_lock:
        for node_id, node in nodes.items():
            if node_id.startswith("!") and node_id not in chats:
                ensure_chat(node_id, node.get("name"), force=True)
        save_chats()
    return jsonify({"ok": True, "message": "Nodes cleaned up", "node_count": len(nodes)})

@app.route("/api/restart_listener", methods=["POST"])
@handle_errors
def api_restart_listener():
    global listen_process

    try:
        stop_listener()
        time.sleep(1)
        pause_listen.clear()

        radio_event("restart")

        return jsonify({
            "ok": True,
            "message": "Meshtastic listener restart requested"
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.route("/api/rescan_nodes", methods=["POST"])
@handle_errors
def api_rescan_nodes():
    try:
        pause_listen.set()
        stop_listener()

        success = parse_nodes_from_info()

        pause_listen.clear()

        return jsonify({
            "ok": bool(success),
            "message": (
                "Network rescan completed"
                if success
                else "Network rescan completed, no changes found"
            )
        })

    except Exception as e:
        pause_listen.clear()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.route("/api/clear_chat", methods=["POST"])
@handle_errors
def api_clear_chat():
    data = request.get_json(force=True)
    chat_id = data.get("chat_id", "").strip()
    if not chat_id or not is_valid_node_id(chat_id):
        return jsonify({"ok": False, "error": "Invalid chat_id"}), 400
    global messages
    with state_lock:
        messages = [m for m in messages if m.get("chat_id") != chat_id]
        save_messages()
        if chat_id in chats:
            chats[chat_id]["last_message"] = ""
            chats[chat_id]["last_time"] = ""
            chats[chat_id]["unread"] = 0
            save_chats()
    return jsonify({"ok": True})

@app.route("/api/delete_chat", methods=["POST"])
@handle_errors
def api_delete_chat():
    data = request.get_json(force=True)
    chat_id = data.get("chat_id", "").strip()
    if not chat_id or chat_id == CHANNEL_CHAT_ID or not is_valid_node_id(chat_id):
        return jsonify({"ok": False, "error": "Invalid chat"}), 400
    with state_lock:
        if chat_id in chats:
            del chats[chat_id]
            save_chats()
        global messages
        messages = [m for m in messages if m.get("chat_id") != chat_id]
        save_messages()
    return jsonify({"ok": True})

# ===== TELEMETRY API =====
@app.route("/api/telemetry")
def api_telemetry():
    return jsonify(telemetry.telemetry_current)

@app.route("/api/telemetry/history")
def api_telemetry_history():
    limit = request.args.get("limit", 100, type=int)
    with state_lock:
        history = telemetry.telemetry_history[-limit:] if limit > 0 else telemetry.telemetry_history

    return jsonify({
        "history": history,
        "total": len(telemetry.telemetry_history),
        "config": telemetry.telemetry_config
    })

@app.route("/api/export/telemetry", methods=["GET"])
@handle_errors
def api_export_telemetry():
    data_type = request.args.get("type", "all").lower()
    export_format = request.args.get("format", "csv").lower()
    range_minutes = request.args.get("range", "all").lower()
    start_ts = request.args.get("start")
    end_ts = request.args.get("end")
    series = request.args.get("series", "")

    if data_type not in ("environment", "power", "all"):
        return jsonify({"ok": False, "error": "Invalid type"}), 400

    if export_format not in ("csv", "json"):
        return jsonify({"ok": False, "error": "Invalid format"}), 400

    records = get_telemetry_export_records(
        data_type=data_type,
        range_minutes=range_minutes,
        start_ts=start_ts,
        end_ts=end_ts,
        series=series
    )

    series_part = "-".join(
        s.strip().lower()
        for s in series.split(",")
        if s.strip()
    ) if series else "all"

    def export_range_label(range_value):
        labels = {
            "60": "last_1h",
            "360": "last_6h",
            "720": "last_12h",
            "1440": "last_24h",
            "10080": "last_7d",
            "43200": "last_30d",
            "all": "all"
        }
        return labels.get(str(range_value), f"last_{range_value}min")

    if start_ts and end_ts:
        try:
            dt1 = datetime.fromtimestamp(float(start_ts))
            dt2 = datetime.fromtimestamp(float(end_ts))

            if dt1.date() == dt2.date():
                range_part = f"{dt1.strftime('%Y-%m-%d')}_{dt1.strftime('%H-%M')}_to_{dt2.strftime('%H-%M')}"
            else:
                range_part = f"{dt1.strftime('%Y-%m-%d_%H-%M')}_to_{dt2.strftime('%Y-%m-%d_%H-%M')}"

        except Exception:
            range_part = "custom"
    else:
        range_part = export_range_label(range_minutes)

    filename = f"meshcenter_{data_type}_{series_part}_{range_part}.{export_format}"

    if export_format == "json":
        response = make_response(json.dumps(records, indent=2, ensure_ascii=False))
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response

    csv_data = records_to_csv(records)
    response = make_response(csv_data)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response

@app.route("/api/telemetry/config", methods=["POST"])
@handle_errors
def api_telemetry_config():
    data = request.get_json(force=True)
    interval = data.get("interval")
    enabled = data.get("enabled")

    if interval is not None:
        allowed = [300, 900, 1800, 3600]
        if interval in allowed:
            with state_lock:
                telemetry.telemetry_config["interval"] = interval
                telemetry.save_telemetry()
        else:
            return jsonify({"ok": False, "error": "Invalid interval"}), 400

    if enabled is not None:
        with state_lock:
            telemetry.telemetry_config["enabled"] = bool(enabled)
            telemetry.save_telemetry()

    return jsonify({"ok": True, "config": telemetry.telemetry_config})

# ===== NODE MANAGEMENT ROUTES =====
@app.route("/api/nodes_management", methods=["GET"])
def api_nodes_management():
    with state_lock:
        nodes_list = []
        for node_id, node in nodes.items():
            nodes_list.append({
                "name": node.get("name", "Unknown"), "node_id": node_id,
                "ignored": node.get("ignored", False),
                "favorite": node.get("favorite", False),
                "last_seen": node.get("last_seen", 0)
            })
        nodes_list.sort(key=lambda x: x.get("name", "").lower())
    return jsonify({"nodes": nodes_list, "total": len(nodes_list)})

@app.route("/api/cleanup_all_nodes", methods=["POST"])
@handle_errors
def api_cleanup_all_nodes():
    global nodes, chats
    try:
        with state_lock:
            deleted_count = len(nodes)
            dm_chat_ids = [c for c in chats.keys() if c != CHANNEL_CHAT_ID and c.startswith("!")]
            for chat_id in dm_chat_ids:
                if chat_id in chats:
                    del chats[chat_id]
            nodes = {}
            save_nodes()
            save_chats()
        return jsonify({"ok": True, "deleted_count": deleted_count})
    except Exception as e:
        print(f"[ERROR] Cleanup all nodes: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/nodes_export", methods=["GET"])
def api_nodes_export():
    with state_lock:
        nodes_list = []
        for node_id, node in nodes.items():
            nodes_list.append({
                "name": node.get("name", ""), "node_id": node_id,
                "last_time": node.get("last_time", ""),
                "rssi": node.get("rssi", ""), "snr": node.get("snr", ""),
                "role": node.get("role", "CLIENT"),
                "short_name": node.get("short_name", ""),
                "hw_model": node.get("hw_model", "")
            })
    return jsonify({"nodes": nodes_list})

@app.route("/api/nodes_import", methods=["POST"])
@handle_errors
def api_nodes_import():
    data = request.get_json()
    imported_nodes = data.get("nodes", [])
    imported_count = 0
    with state_lock:
        for node_data in imported_nodes:
            node_id = node_data.get("node_id")
            if not node_id:
                continue
            old = nodes.get(node_id, {})
            name = node_data.get("name") or old.get("name") or friendly_unknown_node_name(node_id)
            nodes[node_id] = {
                "name": name, "node_id": node_id,
                "last_seen": old.get("last_seen", time.time()),
                "last_time": node_data.get("last_time", old.get("last_time", now())),
                "rssi": node_data.get("rssi", old.get("rssi")),
                "snr": node_data.get("snr", old.get("snr")),
                "hop_start": old.get("hop_start", ""),
                "relay_node": old.get("relay_node", ""),
                "last_text": old.get("last_text", ""),
                "short_name": node_data.get("short_name", old.get("short_name", "") or node_id[-4:]),
                "hw_model": node_data.get("hw_model", old.get("hw_model", "")),
                "role": node_data.get("role", old.get("role", "CLIENT")),
                "ignored": old.get("ignored", False),
                "favorite": old.get("favorite", False)
            }
            ensure_chat(node_id, name, force=True)
            imported_count += 1
        save_nodes()
        save_chats()
    return jsonify({"ok": True, "imported_count": imported_count})

@app.route("/api/nodes_merge_duplicates", methods=["POST"])
@handle_errors
def api_nodes_merge_duplicates():
    merged = 0
    with state_lock:
        name_map = {}
        duplicates = []
        for node_id, node in nodes.items():
            name = node.get("name", "")
            if not name:
                continue
            if name in name_map:
                duplicates.append((name, node_id, name_map[name]))
            else:
                name_map[name] = node_id
        for name, dup_id, main_id in duplicates:
            dup = nodes.get(dup_id, {})
            main = nodes.get(main_id, {})
            if dup.get("last_seen", 0) > main.get("last_seen", 0):
                nodes[main_id] = dup
                nodes[main_id]["node_id"] = main_id
            if dup_id in chats:
                del chats[dup_id]
            del nodes[dup_id]
            merged += 1
        if merged:
            save_nodes()
            save_chats()
    return jsonify({"ok": True, "merged_count": merged})

@app.route("/api/delete_all_dm", methods=["POST"])
@handle_errors
def api_delete_all_dm():
    global messages, chats
    try:
        with state_lock:
            deleted_count = 0
            dm_chat_ids = []
            for chat_id in list(chats.keys()):
                if chat_id != CHANNEL_CHAT_ID and chat_id.startswith("!"):
                    dm_chat_ids.append(chat_id)
                    deleted_count += 1
            for chat_id in dm_chat_ids:
                if chat_id in chats:
                    del chats[chat_id]
            deleted_file = os.path.join(DATA_DIR, "deleted_dm.json")
            try:
                with open(deleted_file, "w") as f:
                    json.dump({"deleted": dm_chat_ids}, f)
            except Exception as e:
                print(f"[WARN] Could not write deleted_dm.json: {e}")
            messages = [m for m in messages if m.get("chat_id") == CHANNEL_CHAT_ID]
            save_chats()
            save_messages()
        return jsonify({"ok": True, "deleted_count": deleted_count, "message": f"Deleted {deleted_count} DM chats"})
    except Exception as e:
        print(f"[ERROR] Delete all DM: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/restore_deleted_dm", methods=["POST"])
@handle_errors
def api_restore_deleted_dm():
    deleted_file = os.path.join(DATA_DIR, "deleted_dm.json")
    if os.path.exists(deleted_file):
        os.remove(deleted_file)
        with state_lock:
            for node_id in nodes:
                if node_id.startswith("!"):
                    ensure_chat(node_id, nodes[node_id].get("name"), force=False)
            save_chats()
        return jsonify({"ok": True, "message": "Restored deleted DM chats"})
    return jsonify({"ok": True, "message": "No deleted chats to restore"})

@app.route("/api/radio_health")
def api_radio_health():
    now_ts = time.time()

    with state_lock:
        status = dict(radio_health)

    last_packet = float(status.get("last_packet") or 0)
    last_telemetry = float(status.get("last_telemetry") or 0)
    last_text = float(status.get("last_text") or 0)
    last_send = float(status.get("last_send") or 0)

    status["packet_age"] = (
        max(0, int(now_ts - last_packet))
        if last_packet else None
    )

    status["telemetry_age"] = (
        max(0, int(now_ts - last_telemetry))
        if last_telemetry else None
    )

    status["text_age"] = (
        max(0, int(now_ts - last_text))
        if last_text else None
    )

    status["send_age"] = (
        max(0, int(now_ts - last_send))
        if last_send else None
    )

    return jsonify(status)
    
# ============================================================
# CPU USAGE HISTORY
# ============================================================
CPU_HISTORY_FILE = os.path.join(DATA_DIR, "cpu_history.json")
CPU_SAMPLE_INTERVAL = 2.0
CPU_HISTORY_RETENTION = 24 * 60 * 60
cpu_history = deque()
cpu_history_lock = threading.RLock()
_cpu_prev_total = None
_cpu_prev_idle = None
_cpu_current_usage = 0.0

def _read_cpu_times():
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

def _read_cpu_temperature():
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

def _read_memory_percent():
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

def _load_cpu_history():
    data = safe_read_json(CPU_HISTORY_FILE, {"cpu": []})
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

def _save_cpu_history():
    with cpu_history_lock:
        payload = {"cpu": list(cpu_history)}
    safe_write_json(CPU_HISTORY_FILE, payload)

def cpu_history_worker():
    global _cpu_prev_total, _cpu_prev_idle, _cpu_current_usage
    _cpu_prev_total, _cpu_prev_idle = _read_cpu_times()
    last_save = 0.0
    while True:
        time.sleep(CPU_SAMPLE_INTERVAL)
        total, idle = _read_cpu_times()
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
                    _save_cpu_history()
                    last_save = now
                except Exception as exc:
                    print(f"[CPU] History save error: {exc}", flush=True)
        _cpu_prev_total, _cpu_prev_idle = total, idle

def _downsample_cpu_records(records, max_points):
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

@app.route("/api/system/cpu-history")
def api_system_cpu_history():
    range_key = str(request.args.get("range", "30m")).lower()
    ranges = {"30m": 1800, "1h": 3600, "6h": 21600, "12h": 43200, "24h": 86400}
    seconds = ranges.get(range_key, 1800)
    cutoff = time.time() - seconds
    with cpu_history_lock:
        records = [dict(item) for item in cpu_history if item["timestamp"] >= cutoff]
    max_points = 900 if range_key == "30m" else 720
    records = _downsample_cpu_records(records, max_points)
    return jsonify({
        "ok": True,
        "range": range_key,
        "current": _cpu_current_usage,
        "temperature": _read_cpu_temperature(),
        "ram_percent": _read_memory_percent(),
        "records": records,
    })

# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    # Загружаем данные
    load_messages()
    load_nodes()
    load_sensors_data()
    load_chats()
    ensure_known_nodes()
    normalize_unknown_nodes()
    parse_nodes_from_info()
    load_settings()
    _load_cpu_history()

    try:
        update_base_status_from_info()
    except Exception as e:
        print(f"[WARN] Base status update failed: {e}")
    
    telemetry.load_telemetry()
    camera.load_camera_settings()    # <--- вызов через модуль
    
    for node_id in KNOWN_NODES:
        if node_id not in chats:
            ensure_chat(node_id, KNOWN_NODES[node_id], force=True)
    save_chats()
    
    try:
        print("[INIT] Initial telemetry fetch...")
        get_telemetry_from_info()
    except Exception as e:
        print(f"[INIT] Telemetry fetch error: {e}")
    
    # Инициализация камеры
    print("[CAMERA] 🔍 Initializing...", flush=True)
    camera.init_camera()   # <--- вызов через модуль
    
    # Запуск потоков
    threading.Thread(target=listen_meshtastic, daemon=True).start()
    threading.Thread(target=cleanup_seen_ids, daemon=True).start()
    threading.Thread(target=telemetry_worker, daemon=True).start()
    threading.Thread(target=telemetry_buffer_worker, daemon=True).start()
    threading.Thread(target=radio_health_worker, daemon=True).start()
    threading.Thread(target=cpu_history_worker, daemon=True).start()
    
    print(f"""
    ╔══════════════════════════════════════════════╗
    ║   MeshCenter (Pi Zero 2W)                    ║
    ╠══════════════════════════════════════════════╣
    ║  URL: http://{APP_HOST}:{APP_PORT}       ║
    ║  Node: {LOCAL_NODE_NAME}                     ║
    ║  Port: {MESHTASTIC_PORT}                    ║
    ║  Camera: {'✅' if camera.CAMERA_AVAILABLE else '❌'} Available        ║
    ║  Video: {camera.VIDEO_CONFIG['resolution']} @ {camera.VIDEO_CONFIG['fps']}fps {camera.VIDEO_CONFIG['quality']}% ║
    ║  Photo: {camera.PHOTO_CONFIG['resolution']} preview, {camera.PHOTO_SAVE_CONFIG['resolution']} save ║
    ╚══════════════════════════════════════════════╝
    """)
    
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)

