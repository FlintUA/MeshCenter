#!/usr/bin/env python3
"""
MeshCenter - Web Control Center for Meshtastic nodes on Raspberry Pi Zero 2W
"""

from flask import Flask, request, jsonify, render_template, Response, send_from_directory
from functools import wraps
import subprocess
import threading
import time
import re
import json
import os
import io
from collections import defaultdict
from datetime import datetime
from camera import camera
from telemetry import telemetry
from meshsrv import meshsrv
from api.api_camera import register_camera_routes
from api.api_chat import register_chat_routes


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

# ===== TELEMETRY BUFFER =====
# Состояние и история телеметрии вынесены в telemetry/telemetry.py.
# В server.py пока оставляем парсер и буфер, чтобы рефакторинг был безопасным.
telemetry_buffer_lock = threading.RLock()
telemetry_pending_values = {}
telemetry_pending_time = 0
TELEMETRY_DEBOUNCE_SECONDS = 1.5

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
    
    if node_id not in chats:
        name = node_name or get_node_name(node_id)
        chats[node_id] = {
            "id": node_id,
            "name": name,
            "type": "dm",
            "last_message": "",
            "last_time": "",
            "unread": 0
        }
        save_chats()

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
                nodes[node_id] = {
                    "name": long_name, "node_id": node_id,
                    "last_seen": last_heard or old.get("last_seen", 0),
                    "last_time": time.strftime("%H:%M:%S", time.localtime(last_heard)) if last_heard else old.get("last_time", "never"),
                    "rssi": old.get("rssi"), "snr": snr or old.get("snr"),
                    "hop_start": str(hops_away) if hops_away > 0 else old.get("hop_start", ""),
                    "relay_node": old.get("relay_node", ""), "last_text": old.get("last_text", ""),
                    "short_name": short_name or old.get("short_name", "") or node_id[-4:],
                    "hw_model": hw_model or old.get("hw_model", ""),
                    "role": role or old.get("role", "CLIENT"),
                    "ignored": old.get("ignored", False),
                    "favorite": old.get("favorite", False)
                }
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
            nodes[node_id] = {
                "name": name, "node_id": node_id,
                "last_seen": old.get("last_seen", 0),
                "last_time": old.get("last_time", "never"),
                "rssi": old.get("rssi"), "snr": old.get("snr"),
                "hop_start": old.get("hop_start", ""),
                "relay_node": old.get("relay_node", ""),
                "last_text": old.get("last_text", ""),
                "short_name": info.get("short_name", old.get("short_name", "")),
                "hw_model": info.get("hw_model", old.get("hw_model", "")),
                "role": old.get("role", "CLIENT"),
                "ignored": old.get("ignored", False),
                "favorite": old.get("favorite", False)
            }
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
    rssi = extract_rssi(line)
    snr = extract_snr(line)
    hop_start = extract_hop_start(line)
    relay_node = extract_relay_node(line)
    role = extract_field(line, ["role", "Role"])
    name = get_node_name(node_id)
    info = get_node_info(node_id)
    with state_lock:
        old = nodes.get(node_id, {})
        new_name = None
        long_name_match = re.search(r"'longName':\s*'([^']*)'", line) or re.search(r'"longName":\s*"([^"]*)"', line)
        if long_name_match:
            new_name = long_name_match.group(1).strip()
        if not new_name:
            if sender and sender != "RX" and not sender.startswith("!"):
                new_name = sender
        if new_name and old.get("name") != new_name:
            if node_id in chats:
                chats[node_id]["name"] = new_name
                save_chats()
        nodes[node_id] = {
            "name": new_name or name, "node_id": node_id,
            "last_seen": time.time(), "last_time": now(),
            "rssi": rssi or old.get("rssi"), "snr": snr or old.get("snr"),
            "hop_start": hop_start or old.get("hop_start", ""),
            "relay_node": relay_node or old.get("relay_node", ""),
            "last_text": text or old.get("last_text", ""),
            "short_name": info.get("short_name") or old.get("short_name", "") or node_id[-4:],
            "hw_model": info.get("hw_model") or old.get("hw_model", ""),
            "role": role or old.get("role", "CLIENT"),
            "ignored": old.get("ignored", False),
            "favorite": old.get("favorite", False)
        }
        if node_id.startswith("!"):
            ensure_chat(node_id, new_name or name, force=True)
        save_nodes()
    return node_id

def process_nodeinfo(block):
    if ("NODEINFO_APP" not in block and "longName" not in block and "long_name" not in block and
        "shortName" not in block and "short_name" not in block and "hwModel" not in block and "hw_model" not in block):
        return False
    node_id = extract_node_id(block)
    if not node_id: return False
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
                "name": icon + " " + n["name"], "clean_name": n["name"],
                "node_id": n["node_id"], "meta": " | ".join(meta_parts),
                "last_text": last_text, "short_name": short_name,
                "hw_model": hw_model, "role": role,
                "rssi": rssi, "snr": snr,
                "hop_start": hop_start, "relay_node": relay_node,
                "signal_quality": quality, "age": age_display,
                "ignored": ignored, "favorite": favorite
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
                consecutive_errors = 0

            for line in listen_process.stdout:
                if pause_listen.is_set():
                    break

                line = line.strip()

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
                            with state_lock:
                                process_telemetry_line(line)
                        except Exception as e:
                            print(f"[TELEMETRY] Parse error: {e}", flush=True)

                    if "TEXT_MESSAGE_APP" in line or "'text':" in line or '"text":' in line:
                        print(f"[RAW] {line[:200]}...", flush=True)

                    if "NODEINFO_APP" in line or collecting_nodeinfo:
                        collecting_nodeinfo = True
                        nodeinfo_buffer.append(line)
                        block = "\n".join(nodeinfo_buffer)

                        if (
                            "fromId" in block
                            and (
                                "longName" in block
                                or "long_name" in block
                                or "shortName" in block
                                or "short_name" in block
                                or "hwModel" in block
                                or "hw_model" in block
                            )
                        ):
                            with state_lock:
                                process_nodeinfo(block)
                            nodeinfo_buffer = []
                            collecting_nodeinfo = False
                            continue

                        if len(nodeinfo_buffer) > 80:
                            with state_lock:
                                process_nodeinfo(block)
                            nodeinfo_buffer = []
                            collecting_nodeinfo = False

                        continue

                    text = extract_text_message(line)

                    if not text:
                        continue

                    pid = extract_packet_id(line)

                    if pid:
                        if pid in seen_ids:
                            continue
                        seen_ids.add(pid)

                    sender = extract_sender(line)
                    node_id = update_node(line, sender, text)

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

                    if chat_id.startswith("!") and chat_id != LOCAL_NODE_ID:
                        with state_lock:
                            ensure_chat(chat_id, sender, force=True)

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
                listen_process = None
                time.sleep(0.5)
                continue

            if return_code is not None and return_code != 0:
                print(f"[WARN] Listener process ended with code: {return_code}", flush=True)
                consecutive_errors += 1
            else:
                consecutive_errors = 0

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
    get_node_name,
    ensure_chat,
    add_message,
    reset_unread,
    get_node_info,
    save_nodes,
    now,
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

@app.route("/api/rescan_nodes", methods=["POST"])
@handle_errors
def api_rescan_nodes():
    global listen_process
    if listen_process is not None:
        try:
            listen_process.terminate()
            time.sleep(1)
            if listen_process.poll() is None:
                listen_process.kill()
            listen_process = None
        except Exception as e:
            print(f"[WARN] Error stopping listener: {e}")
    parse_nodes_from_info()
    threading.Thread(target=listen_meshtastic, daemon=True).start()
    return jsonify({"ok": True, "message": "Network rescan started"})

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
                    ensure_chat(node_id, nodes[node_id].get("name"), force=True)
            save_chats()
        return jsonify({"ok": True, "message": "Restored deleted DM chats"})
    return jsonify({"ok": True, "message": "No deleted chats to restore"})

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
    
    try:
        update_base_status_from_info()
    except Exception as e:
        print(f"[WARN] Base status update failed: {e}")
    
    telemetry.load_telemetry()
    camera.load_camera_settings()
    
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
    camera.init_camera()
    
    # Запуск потоков
    threading.Thread(target=listen_meshtastic, daemon=True).start()
    threading.Thread(target=cleanup_seen_ids, daemon=True).start()
    threading.Thread(target=telemetry_worker, daemon=True).start()
    threading.Thread(target=telemetry_buffer_worker, daemon=True).start()
    
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