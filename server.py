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
import sys
import io
import csv
import uuid
import ast
import sqlite3
from contextlib import contextmanager
from collections import defaultdict, deque
from datetime import datetime
from camera import camera
from telemetry import telemetry
from meshsrv import meshsrv
from meshsrv.radio_manager import RadioConnectionManager
from meshsrv.runtime_identity import resolve_meshtastic_cli, resolve_serial_port, meshtastic_command, discover_serial_ports
from meshsrv.instance_manager import InstanceManager
from meshsrv.radio_identity import detect_radio_identity, detect_connected_radio, compare_radio_identity
from api.api_camera import register_camera_routes
from api.api_camera_manager import register_camera_manager_routes
from api.api_chat import register_chat_routes
from api.api_settings import register_settings_routes, normalize_settings, SUPPORTED_LANGUAGES
from api.api_system import register_system_routes
from system_log import log_system_event
from storage.waypoint_store import WaypointStore
from storage.profile_manager import ProfileManager
from storage.device_manager import DeviceManager
from api.api_node_tools import register_node_tools_routes
from api.api_node_icons import register_node_icon_routes
from api.api_weather import register_weather_routes
from weather.weather_manager import WeatherManager
from weather.providers.openweather import OpenWeatherProvider, WeatherConfig as OpenWeatherConfig
from weather.providers.weatherapi import WeatherApiProvider, WeatherApiConfig

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
# Radio-scoped paths are resolved after the accepted instance identity loads.
WAYPOINTS_DB_FILE = ""
NODE_DEBUG_LOG = ""
DELETED_DM_FILE = ""
PROFILE_DATA_DIR = ""
ACTIVE_PROFILE_ID = ""
PROFILE_CONTEXT = {}

try:
    MESHTASTIC_PORT
except NameError:
    MESHTASTIC_PORT = "/dev/ttyACM0"

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

def resolve_app_version(project_dir, fallback="dev"):
    """Return the app version from git: the exact tag when HEAD is tagged
    (e.g. "1.3.0" from "v1.3.0"), otherwise a tag+distance+hash description
    (e.g. "1.5.0-42-gd586650") so a checkout ahead of the latest reachable
    tag still reports something more useful than a bare "dev"."""
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        tag = result.stdout.strip()
        if result.returncode == 0 and tag:
            return tag[1:] if tag.startswith("v") else tag
    except Exception as error:
        print(f"[VERSION] git describe --abbrev=0 failed: {error}", flush=True)

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        described = result.stdout.strip()
        if result.returncode == 0 and described:
            return described[1:] if described.startswith("v") else described
    except Exception as error:
        print(f"[VERSION] git describe --always failed: {error}", flush=True)

    return fallback

APP_VERSION = resolve_app_version(PROJECT_DIR)
print(f"[VERSION] MeshCenter {APP_VERSION}", flush=True)

try:
    MESHTASTIC_CMD = resolve_meshtastic_cli(MESHTASTIC_CMD, PROJECT_DIR)
    MESHTASTIC_PORT = resolve_serial_port(MESHTASTIC_PORT)
    print(
        f"[INIT] Meshtastic CLI resolved: {MESHTASTIC_CMD}; "
        f"serial port: {MESHTASTIC_PORT}",
        flush=True,
    )
except RuntimeError as error:
    print("=" * 60, flush=True)
    print(f"❌ MESHTASTIC INITIALIZATION ERROR: {error}", flush=True)
    print("=" * 60, flush=True)
    raise SystemExit(1)

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

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# Persistent identity of this MeshCenter installation. This stage only
# normalizes and stores accepted identity data; it does not switch radios.
INSTANCE_FILE = os.path.join(DATA_DIR, "instance.json")
try:
    configured_instance_name = str(globals().get("INSTANCE_NAME", "") or "").strip()
    instance_manager = InstanceManager(INSTANCE_FILE)
    INSTANCE_IDENTITY = instance_manager.load_or_create({
        "instance_name": configured_instance_name,
        "hostname": os.uname().nodename if hasattr(os, "uname") else "",
        "active_profile_id": "",
        "radio": {
            "node_id": LOCAL_NODE_ID,
            "long_name": LOCAL_NODE_NAME,
            "short_name": "",
            "hardware": "",
            "role": "",
            "port": MESHTASTIC_PORT,
        },
        "runtime": {
            "cli_path": MESHTASTIC_CMD,
            "last_detected_at": None,
            "identity_status": "NOT_CHECKED",
            "last_error": None,
            "last_detected_radio": {},
        },
    })
    saved_radio = INSTANCE_IDENTITY.get("radio", {})
    saved_port = str(saved_radio.get("port") or "").strip()
    if saved_port and os.path.exists(saved_port):
        MESHTASTIC_PORT = saved_port

    # The accepted instance identity is the source of truth after a radio
    # profile switch. config.py values are only bootstrap fallbacks.
    saved_node_id = str(saved_radio.get("node_id") or "").strip()
    saved_long_name = str(saved_radio.get("long_name") or "").strip()
    if saved_node_id:
        LOCAL_NODE_ID = saved_node_id
    if saved_long_name:
        LOCAL_NODE_NAME = saved_long_name
    print(
        "[INSTANCE] "
        f"{INSTANCE_IDENTITY.get('instance_name', 'MeshCenter')} | "
        f"hostname={INSTANCE_IDENTITY.get('hostname', '')} | "
        f"radio={saved_radio.get('long_name') or 'Unknown'} "
        f"({saved_radio.get('node_id') or 'unknown'}) | "
        f"port={saved_radio.get('port') or MESHTASTIC_PORT}",
        flush=True,
    )
except Exception as error:
    print(f"[INSTANCE] Initialization failed: {error}", flush=True)
    raise SystemExit(1)

# Resolve the accepted radio profile before any radio-specific storage is opened.
try:
    profile_manager = ProfileManager(DATA_DIR)
    accepted_radio = dict(INSTANCE_IDENTITY.get("radio", {}))
    PROFILE_CONTEXT = profile_manager.ensure_profile(accepted_radio, migrate_legacy=True)
    ACTIVE_PROFILE_ID = PROFILE_CONTEXT["profile_id"]
    PROFILE_DATA_DIR = PROFILE_CONTEXT["profile_dir"]
    profile_paths = PROFILE_CONTEXT["paths"]

    HISTORY_FILE = profile_paths["messages"]
    NODES_FILE = profile_paths["nodes"]
    SENSORS_FILE = profile_paths["sensors"]
    CHATS_FILE = profile_paths["chats"]
    DELETED_DM_FILE = profile_paths["deleted_dm"]
    WAYPOINTS_DB_FILE = profile_paths["waypoints_db"]
    NODE_DEBUG_LOG = profile_paths["node_debug"]
    telemetry.configure_storage(profile_paths["telemetry_history"])

    updated_instance = dict(INSTANCE_IDENTITY)
    updated_instance["active_profile_id"] = ACTIVE_PROFILE_ID
    INSTANCE_IDENTITY = instance_manager.save(updated_instance)

    migration = PROFILE_CONTEXT.get("migration", {})
    if migration.get("performed"):
        if migration.get("errors"):
            print(f"[PROFILE] Migration completed with errors: {migration.get('errors')}", flush=True)
        else:
            print(
                f"[PROFILE] Legacy radio data migrated to {PROFILE_DATA_DIR}; "
                f"backups={len(migration.get('backups', []))}",
                flush=True,
            )
    print(
        f"[PROFILE] Active profile={ACTIVE_PROFILE_ID} | path={PROFILE_DATA_DIR}",
        flush=True,
    )
    device_manager = DeviceManager(PROFILE_DATA_DIR)
    PROFILE_DEVICES = device_manager.load_or_create()
except Exception as error:
    print(f"[PROFILE] Initialization failed: {error}", flush=True)
    raise SystemExit(1)

RADIO_IDENTITY_RESULT = {
    "status": "NOT_CHECKED",
    "checked_at": None,
    "configured": dict(INSTANCE_IDENTITY.get("radio", {})),
    "detected": {},
    "error": None,
}

def verify_radio_identity():
    """Probe the configured serial radio once and persist read-only verification state."""
    global INSTANCE_IDENTITY, RADIO_IDENTITY_RESULT
    result, output = detect_radio_identity(MESHTASTIC_CMD, MESHTASTIC_PORT, timeout=25)
    configured = dict(INSTANCE_IDENTITY.get("radio", {}))
    detected = dict(result.get("detected") or {})
    result["configured"] = configured
    result["status"] = compare_radio_identity(configured, detected) if detected.get("node_id") else result.get("status", "NOT_FOUND")
    RADIO_IDENTITY_RESULT = result

    updated = dict(INSTANCE_IDENTITY)
    runtime = dict(updated.get("runtime", {}))
    runtime.update({
        "cli_path": MESHTASTIC_CMD,
        "last_detected_at": result.get("checked_at"),
        "identity_status": result.get("status"),
        "last_error": result.get("error"),
        "last_detected_radio": detected,
    })
    updated["runtime"] = runtime
    INSTANCE_IDENTITY = instance_manager.save(updated)

    configured_label = configured.get("long_name") or "Unknown"
    configured_id = configured.get("node_id") or "unknown"
    detected_label = detected.get("long_name") or "Unknown"
    detected_id = detected.get("node_id") or "unknown"
    print(f"[IDENTITY] Configured: {configured_label} ({configured_id})", flush=True)
    print(f"[IDENTITY] Detected: {detected_label} ({detected_id})", flush=True)
    suffix = " - automatic replacement blocked" if result.get("status") == "MISMATCH" else ""
    print(f"[IDENTITY] Status: {result.get('status')}{suffix}", flush=True)
    if result.get("error"):
        print(f"[IDENTITY] Detail: {result.get('error')}", flush=True)
    return output

# Папка для скриншотов
SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")
if not os.path.exists(SCREENSHOTS_DIR):
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

app = Flask(__name__)
waypoint_store = WaypointStore(WAYPOINTS_DB_FILE)

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
# Separate from register_camera_routes()/api_camera.py above - see
# api/api_camera_manager.py's module docstring. Does not touch
# /video_feed or anything else camera.py already owns.
register_camera_manager_routes(app, device_manager, handle_errors)
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
radio_connection_manager = None

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
            radio_health["last_restart"] = now_ts

            if radio_connection_manager is not None:
                radio_connection_manager.listener_started()

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

            if radio_connection_manager is not None:
                radio_connection_manager.listener_stopped()

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
    value = str(node_id or "").strip()
    return re.fullmatch(r"![0-9a-fA-F]{8}", value) is not None

def is_valid_chat_id(chat_id):
    value = str(chat_id or "").strip()
    return (
        value == CHANNEL_CHAT_ID
        or re.fullmatch(r"channel:[0-7]", value) is not None
        or is_valid_node_id(value)
    )

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
    changed = False

    with state_lock:
        messages.clear()

        if data:
            loaded_messages = data[-MAX_HISTORY_MESSAGES:]

            # Older MeshCenter history entries did not have a stable ID.
            # Add one once during loading so message actions can safely
            # address a single message even while new packets are arriving.
            for message in loaded_messages:
                if not message.get("id"):
                    message["id"] = uuid.uuid4().hex
                    changed = True

                # Preserve which local radio originally transmitted a message.
                # Older records only stored kind="me"/"tx" and node_id.  Without
                # this ownership field, switching radio profiles could make a
                # message from another local radio appear as if it was sent by
                # the currently active one.
                if message.get("kind") in ("me", "tx"):
                    owner_node_id = str(
                        message.get("owner_node_id")
                        or message.get("node_id")
                        or ""
                    ).strip()
                    if owner_node_id and not message.get("owner_node_id"):
                        message["owner_node_id"] = owner_node_id
                        changed = True

                    owner_profile_id = str(
                        message.get("owner_profile_id") or ""
                    ).strip().lower()
                    if not owner_profile_id and owner_node_id:
                        owner_profile_id = owner_node_id.lstrip("!").lower()
                        message["owner_profile_id"] = owner_profile_id
                        changed = True

            messages.extend(loaded_messages)

    if changed:
        save_messages()

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

        if (
            not changed
            and not extra
            and not raw
            and event not in ("SKIP_LOCAL_NODE", "ERROR")
        ):
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
        },
        "power": {
            "battery_capacity_mah": 3000
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

    power = data.get("power", {})
    if not isinstance(power, dict):
        power = {}
    try:
        battery_capacity_mah = int(power.get("battery_capacity_mah", 3000))
    except (TypeError, ValueError):
        battery_capacity_mah = 3000
    normalized_settings["power"] = {
        "battery_capacity_mah": max(100, min(50000, battery_capacity_mah))
    }

    settings.clear()
    settings.update(normalized_settings)

    save_settings()

def ensure_chat(node_id, node_name=None, force=False):
    if node_id == CHANNEL_CHAT_ID or not node_id or not node_id.startswith("!"):
        return

    deleted_file = DELETED_DM_FILE

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

def _telemetry_sender_node_id(line):
    """Resolve the sender of a Meshtastic telemetry listener line."""
    try:
        from_id = extract_field(line, ["fromId", "from_id"])
        if from_id:
            normalized = normalize_node_id(from_id)
            if normalized:
                return normalized

        match = re.search(r"['\"]from['\"]:\s*(\d+)", line)
        if match:
            return node_num_to_id(match.group(1))

        match = re.search(r"['\"]fromId['\"]:\s*['\"](![0-9a-fA-F]+)['\"]", line)
        if match:
            return normalize_node_id(match.group(1))
    except Exception:
        pass

    # Do not infer the sender from an arbitrary occurrence of LOCAL_NODE_ID.
    # It may appear as toId, relay metadata, or nested packet data belonging
    # to another node. Unresolved telemetry is dropped rather than attributed
    # to the wrong node.
    return None


def _telemetry_from_local_node(line):
    return _telemetry_sender_node_id(line) == LOCAL_NODE_ID



def _decode_waypoint_text(value):
    """Decode only explicit Python-style Unicode escapes from CLI output."""
    text = str(value or "")
    def replace_escape(match):
        try:
            return chr(int(match.group(1), 16))
        except (TypeError, ValueError, OverflowError):
            return match.group(0)
    text = re.sub(r"\\U([0-9a-fA-F]{8})", replace_escape, text)
    text = re.sub(r"\\u([0-9a-fA-F]{4})", replace_escape, text)
    return text


def _waypoint_string(line, keys):
    for key in keys:
        patterns = (
            rf"['\"]{re.escape(key)}['\"]\s*:\s*['\"]([^'\"]*)['\"]",
            rf"\b{re.escape(key)}\s*:\s*['\"]([^'\"]*)['\"]",
        )
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                return _decode_waypoint_text(match.group(1))
    return ""


def _waypoint_number(line, keys):
    for key in keys:
        value = _regex_number(line, (
            rf"['\"]{re.escape(key)}['\"]\s*:\s*(-?\d+(?:\.\d+)?)",
            rf"\b{re.escape(key)}\s*:\s*(-?\d+(?:\.\d+)?)",
        ))
        if value is not None:
            return value
    return None


def parse_waypoint_from_listen_line(line):
    """Parse a decoded WAYPOINT_APP listener line.

    Meshtastic CLI output differs slightly between releases.  The parser
    accepts both Python-dict output and protobuf-style field names.
    """
    if "WAYPOINT_APP" not in line and "'waypoint':" not in line and '"waypoint":' not in line:
        return None

    sender_id = _telemetry_sender_node_id(line) or ""
    marker_positions = [
        position for position in (
            line.find("'waypoint':"),
            line.find('"waypoint":'),
            line.find("WAYPOINT_APP"),
        ) if position >= 0
    ]
    waypoint_text = line[min(marker_positions):] if marker_positions else line

    waypoint_id = _waypoint_number(waypoint_text, ("id", "waypointId", "waypoint_id"))
    latitude_i = _waypoint_number(waypoint_text, ("latitudeI", "latitude_i"))
    longitude_i = _waypoint_number(waypoint_text, ("longitudeI", "longitude_i"))
    latitude = _waypoint_number(waypoint_text, ("latitude",))
    longitude = _waypoint_number(waypoint_text, ("longitude",))

    if latitude_i is not None:
        latitude = latitude_i / 1e7
    if longitude_i is not None:
        longitude = longitude_i / 1e7

    if waypoint_id is None:
        packet_id = extract_packet_id(line)
        waypoint_id = packet_id if packet_id is not None else 0

    if waypoint_id is None or latitude is None or longitude is None:
        # Meshtastic CLI emits short intermediary lines such as
        # "portnum: WAYPOINT_APP" before the decoded object. They are not errors.
        return None

    channel_index = extract_channel_index(line)
    expire_at = _waypoint_number(waypoint_text, ("expire", "expireAt", "expire_at"))
    icon = _waypoint_number(waypoint_text, ("icon",))

    return {
        "waypoint_id": int(waypoint_id),
        "sender_id": sender_id,
        "name": _waypoint_string(waypoint_text, ("name",)),
        "description": _waypoint_string(waypoint_text, ("description",)),
        "latitude": float(latitude),
        "longitude": float(longitude),
        "icon": int(icon) if icon is not None else None,
        "expire_at": int(expire_at) if expire_at is not None else None,
        "channel_index": int(channel_index) if channel_index is not None else None,
        "received_at": time.time(),
        "raw_packet": line,
    }


def process_waypoint_line(line):
    waypoint = parse_waypoint_from_listen_line(line)
    if not waypoint:
        return False

    saved = waypoint_store.upsert(waypoint)
    event = saved.pop("_event", "created")
    if event == "duplicate":
        return True

    sender_name = get_node_name(saved.get("sender_id")) if saved.get("sender_id") else "Unknown"
    name = saved.get("name") or f"Waypoint {saved.get('waypoint_id')}"
    action = "Updated" if event == "updated" else "Received"
    print(
        f"[WAYPOINT] {action}: {name}; sender={sender_name} "
        f"({saved.get('sender_id') or 'unknown'}); "
        f"lat={saved.get('latitude')}; lon={saved.get('longitude')}; "
        f"channel={saved.get('channel_index')}",
        flush=True,
    )
    log_system_event(
        title=f"Waypoint {event}",
        level="INFO",
        details=f"{name}; sender: {sender_name}; "
        f"coordinates: {saved.get('latitude')}, {saved.get('longitude')}",
        source="waypoint",
    )
    return True

def _parse_power_channels_from_line(line):
    """Return normalized PowerMetrics channels 1..3 from listener output."""
    channels = {}

    for channel_number in (1, 2, 3):
        voltage = _regex_number(line, [
            rf"['\"]ch{channel_number}Voltage['\"]:\s*(-?\d+(?:\.\d+)?)",
            rf"ch{channel_number}_voltage:\s*(-?\d+(?:\.\d+)?)",
            rf"ch{channel_number}Voltage:\s*(-?\d+(?:\.\d+)?)",
        ])
        current = _regex_number(line, [
            rf"['\"]ch{channel_number}Current['\"]:\s*(-?\d+(?:\.\d+)?)",
            rf"ch{channel_number}_current:\s*(-?\d+(?:\.\d+)?)",
            rf"ch{channel_number}Current:\s*(-?\d+(?:\.\d+)?)",
        ])

        if voltage is None and current is None:
            continue

        channel = {
            "voltage": voltage,
            "current": current,
        }

        if voltage is not None and current is not None:
            try:
                channel["power"] = float(voltage) * float(current)
            except (TypeError, ValueError):
                channel["power"] = None

        channels[str(channel_number)] = channel

    return channels


def parse_telemetry_from_listen_line(line):
    """Parse passive Meshtastic telemetry for local or remote nodes."""
    if (
        "TELEMETRY_APP" not in line
        and "environmentMetrics" not in line
        and "powerMetrics" not in line
        and "deviceMetrics" not in line
    ):
        return None

    node_id = _telemetry_sender_node_id(line)
    if not node_id:
        print(f"[TELEMETRY RAW] Sender not resolved: {line[:500]}", flush=True)
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
    power_channels = _parse_power_channels_from_line(line)
    channel_1 = power_channels.get("1", {})

    voltage = channel_1.get("voltage")
    if voltage is None:
        voltage = _regex_number(line, [
            r"['\"]voltage['\"]:\s*(-?\d+(?:\.\d+)?)",
            r"voltage:\s*(-?\d+(?:\.\d+)?)"
        ])

    current = channel_1.get("current")
    if current is None:
        current = _regex_number(line, [
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
        "battery_level": 100.0 if battery_level and battery_level > 100 else battery_level,
        "channel_utilization": channel_utilization,
        "air_util_tx": air_util_tx,
        "uptime_seconds": int(uptime_seconds) if uptime_seconds is not None else None,
        "power_channels": power_channels or None,
    }
    if all(value is None for value in values.values()):
        print(f"[TELEMETRY RAW] Metrics not parsed for {node_id}: {line[:500]}", flush=True)
        return None

    return {"node_id": node_id, "values": values}


def apply_node_telemetry(node_id, values, source="passive"):
    """Merge normalized telemetry into the persistent node model."""
    if not node_id or not values:
        return False

    timestamp = time.time()
    with state_lock:
        node = nodes.setdefault(node_id, {
            "node_id": node_id,
            "name": get_node_name(node_id),
        })

        for key, value in values.items():
            if value is not None and key != "power_channels":
                node[key] = value

        voltage = values.get("voltage")
        current_ma = values.get("current")
        if voltage is not None and current_ma is not None:
            node["power"] = float(voltage) * float(current_ma)

        device = node.setdefault("device_metrics", {})
        for key in ("battery_level", "voltage", "channel_utilization", "air_util_tx", "uptime_seconds"):
            if values.get(key) is not None:
                device[key] = values[key]
        if any(values.get(key) is not None for key in ("battery_level", "voltage", "channel_utilization", "air_util_tx", "uptime_seconds")):
            device.update({"updated": timestamp, "source": source})

        environment = node.setdefault("environment_metrics", {})
        for key in ("temperature", "humidity", "pressure"):
            if values.get(key) is not None:
                environment[key] = values[key]
        if any(values.get(key) is not None for key in ("temperature", "humidity", "pressure")):
            environment.update({"updated": timestamp, "source": source})

        power = node.setdefault("power_metrics", {})
        for key in ("voltage", "current", "power"):
            value = node.get(key) if key == "power" else values.get(key)
            if value is not None:
                power[key] = value

        incoming_channels = values.get("power_channels")
        if isinstance(incoming_channels, dict) and incoming_channels:
            stored_channels = power.setdefault("channels", {})
            for channel_id, channel_values in incoming_channels.items():
                if not isinstance(channel_values, dict):
                    continue

                stored_channel = stored_channels.setdefault(str(channel_id), {})
                for metric_name in ("voltage", "current", "power"):
                    metric_value = channel_values.get(metric_name)
                    if metric_value is not None:
                        stored_channel[metric_name] = metric_value

                stored_channel.update({
                    "updated": timestamp,
                    "source": source,
                })

        if (
            any(values.get(key) is not None for key in ("voltage", "current"))
            or bool(incoming_channels)
        ):
            power.update({"updated": timestamp, "source": source})

        node["last_telemetry_time"] = timestamp
        node["last_telemetry_time_text"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
        node["telemetry_source"] = source

    if node_id != LOCAL_NODE_ID:
        history_values = dict(values)
        # Never copy an old calculated power value into a new Device-only
        # sample. Power packets and Device packets arrive independently; stale
        # carry-over would create a false continuous power line in History.
        if values.get("voltage") is not None and values.get("current") is not None:
            try:
                history_values["power"] = float(values["voltage"]) * float(values["current"])
            except (TypeError, ValueError):
                history_values["power"] = None
        else:
            history_values["power"] = values.get("power")
        try:
            telemetry.add_node_telemetry_record(node_id, history_values, source=source)
        except Exception as error:
            print(f"[TELEMETRY HISTORY] Could not save {node_id}: {error}", flush=True)

    save_nodes()
    print(f"[TELEMETRY NODE] {source} update for {node_id}: "
          f"{', '.join(k for k, v in values.items() if v is not None)}", flush=True)
    return True



def _normalize_nodeinfo_position(position):
    if not isinstance(position, dict):
        return None

    lat = position.get("latitude")
    lon = position.get("longitude")
    if lat is None and position.get("latitudeI") is not None:
        lat = float(position.get("latitudeI")) / 1e7
    if lon is None and position.get("longitudeI") is not None:
        lon = float(position.get("longitudeI")) / 1e7

    if lat is None or lon is None:
        return None

    result = {
        "latitude": lat,
        "longitude": lon,
    }
    for source_key, target_key in (
        ("altitude", "altitude"),
        ("time", "time"),
        ("locationSource", "source"),
        ("precisionBits", "precision_bits"),
    ):
        if position.get(source_key) is not None:
            result[target_key] = position.get(source_key)
    return result


def process_received_nodeinfo_line(line):
    """Parse Meshtastic's complete 'Received nodeinfo' dictionary safely."""
    marker = "Received nodeinfo:"
    if marker not in line:
        return False

    payload = line.split(marker, 1)[1].strip()
    if not payload:
        return False

    try:
        info = ast.literal_eval(payload)
    except (ValueError, SyntaxError):
        print(f"[NODEINFO RAW] Could not parse nodeinfo: {payload[:500]}", flush=True)
        return False

    if not isinstance(info, dict):
        return False

    user = info.get("user") if isinstance(info.get("user"), dict) else {}
    node_id = user.get("id")
    if not node_id:
        num = info.get("num")
        if isinstance(num, int):
            node_id = f"!{num:08x}"

    if not node_id:
        print(f"[NODEINFO RAW] Node id missing: {payload[:500]}", flush=True)
        return False

    device_metrics = info.get("deviceMetrics")
    environment_metrics = info.get("environmentMetrics")
    power_metrics = info.get("powerMetrics")
    if not isinstance(device_metrics, dict):
        device_metrics = {}
    if not isinstance(environment_metrics, dict):
        environment_metrics = {}
    if not isinstance(power_metrics, dict):
        power_metrics = {}

    power_channels = {}
    for channel_number in (1, 2, 3):
        voltage_key = f"ch{channel_number}Voltage"
        current_key = f"ch{channel_number}Current"
        channel_voltage = power_metrics.get(voltage_key)
        channel_current = power_metrics.get(current_key)

        if channel_number == 1:
            if channel_voltage is None:
                channel_voltage = power_metrics.get("voltage")
            if channel_current is None:
                channel_current = power_metrics.get("current")

        if channel_voltage is None and channel_current is None:
            continue

        channel = {
            "voltage": channel_voltage,
            "current": channel_current,
        }

        if channel_voltage is not None and channel_current is not None:
            try:
                channel["power"] = (
                    float(channel_voltage) * float(channel_current)
                )
            except (TypeError, ValueError):
                channel["power"] = None

        power_channels[str(channel_number)] = channel

    channel_1 = power_channels.get("1", {})
    values = {
        "battery_level": device_metrics.get("batteryLevel"),
        "voltage": (
            device_metrics.get("voltage")
            if device_metrics.get("voltage") is not None
            else channel_1.get("voltage")
        ),
        "channel_utilization": device_metrics.get("channelUtilization"),
        "air_util_tx": device_metrics.get("airUtilTx"),
        "uptime_seconds": device_metrics.get("uptimeSeconds"),
        "temperature": environment_metrics.get("temperature"),
        "humidity": environment_metrics.get("relativeHumidity"),
        "pressure": environment_metrics.get("barometricPressure"),
        "current": channel_1.get("current"),
        "power_channels": power_channels or None,
    }

    position = _normalize_nodeinfo_position(info.get("position"))
    last_heard = info.get("lastHeard")
    long_name = (user.get("longName") or "").strip()
    short_name = (user.get("shortName") or "").strip()
    hw_model = user.get("hwModel") or ""
    role = user.get("role") or ""
    snr = info.get("snr")
    hops_away = info.get("hopsAway")

    with state_lock:
        old = nodes.get(node_id, {})
        node = dict(old)
        node.update({
            "node_id": node_id,
            "name": KNOWN_NODES.get(node_id) or long_name or old.get("name") or short_name or friendly_unknown_node_name(node_id),
            "short_name": short_name or old.get("short_name", "") or node_id[-4:],
            "hw_model": hw_model or old.get("hw_model", ""),
            "role": role or old.get("role", "CLIENT"),
            "snr": snr if snr is not None else old.get("snr"),
            "hop_start": str(hops_away) if hops_away is not None else old.get("hop_start", ""),
            "last_seen": last_heard if last_heard is not None else old.get("last_seen", time.time()),
            "last_time": (
                time.strftime("%H:%M:%S", time.localtime(last_heard))
                if isinstance(last_heard, (int, float)) and last_heard > 0
                else old.get("last_time", now())
            ),
            "ignored": old.get("ignored", False),
            "favorite": old.get("favorite", False),
            "last_text": old.get("last_text", ""),
            "relay_node": old.get("relay_node", ""),
        })
        if position is not None:
            old_position = old.get("position") if isinstance(old.get("position"), dict) else {}
            node["position"] = {**old_position, **position}
        elif "position" in old:
            node["position"] = old.get("position")
        nodes[node_id] = node
        if node_id.startswith("!"):
            ensure_chat(node_id, node["name"], force=True)

    metric_keys = [key for key, value in values.items() if value is not None]
    if metric_keys:
        apply_node_telemetry(node_id, values, source="nodeinfo")
    else:
        save_nodes()

    save_chats()
    print(
        f"[NODEINFO] updated {node_id}: "
        f"name={nodes.get(node_id, {}).get('name')}, "
        f"metrics={','.join(metric_keys) if metric_keys else 'none'}",
        flush=True,
    )
    return True

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
    parsed = parse_telemetry_from_listen_line(line)
    if not parsed:
        return False

    node_id = parsed["node_id"]
    values = parsed["values"]
    updated = apply_node_telemetry(node_id, values, source="passive")
    if updated:
        radio_event("telemetry")

    # Local radio telemetry also feeds the existing left-side sensor cards
    # and telemetry history. Remote-node telemetry must not overwrite them.
    if node_id == LOCAL_NODE_ID:
        queue_telemetry_values(values)

    return updated

def get_telemetry_from_info(info_output=None):
    global base_status

    try:
        if info_output is None:
            result = meshsrv.get_info(MESHTASTIC_CMD, serial_port=MESHTASTIC_PORT, timeout=15)
            output = result.stdout + result.stderr
        else:
            output = str(info_output)

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
    series="",
    node_id=""
    ):
    data = safe_read_json(telemetry.TELEMETRY_FILE, {})
    records = data.get("history", [])

    if not isinstance(records, list):
        records = []

    node_id = str(node_id or "").strip()
    if node_id:
        if node_id == LOCAL_NODE_ID:
            records = [
                record for record in records
                if isinstance(record, dict)
                and record.get("node_id") in (None, "", LOCAL_NODE_ID)
            ]
        else:
            records = [
                record for record in records
                if isinstance(record, dict)
                and record.get("node_id") == node_id
            ]

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

def parse_nodes_from_info(info_output=None):
    global nodes
    try:
        if info_output is None:
            result = subprocess.run(meshtastic_command(MESHTASTIC_CMD, MESHTASTIC_PORT, "--info"), capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
        else:
            output = str(info_output)
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
    if m:
        return int(m.group(1))
    m = re.search(r'"id":\s*(\d+)', line)
    if m:
        return int(m.group(1))
    m = re.search(r"\bid:\s*(\d+)", line)
    if m:
        return int(m.group(1))
    return None


def extract_channel_index(line):
    patterns = (
        r"['\"]channel['\"]\s*:\s*(\d+)",
        r"['\"]channelIndex['\"]\s*:\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            try:
                return max(0, min(7, int(match.group(1))))
            except (TypeError, ValueError):
                pass
    return 0

def channel_chat_id(index):
    return CHANNEL_CHAT_ID if int(index or 0) == 0 else f"channel:{int(index)}"

def extract_reply_id(line):
    """Return the Meshtastic packet ID referenced by an incoming reply."""
    patterns = [
        r"'replyId':\s*(\d+)",
        r'"replyId":\s*(\d+)',
        r"'reply_id':\s*(\d+)",
        r'"reply_id":\s*(\d+)',
        r"\breplyId:\s*(\d+)",
        r"\breply_id:\s*(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            value = int(match.group(1))
            return value if value > 0 else None
    return None


def extract_request_id(line):
    """Return the packet id a ROUTING_APP (ACK/NAK) line is responding to."""
    patterns = [
        r"'requestId':\s*(\d+)",
        r'"requestId":\s*(\d+)',
        r"'request_id':\s*(\d+)",
        r'"request_id":\s*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return int(match.group(1))
    return None


def extract_routing_error_reason(line):
    """Return the Routing.errorReason enum name from a ROUTING_APP line.

    "NONE" (or absent) means the mesh acknowledged the packet; anything
    else is a NAK with that reason (e.g. "NO_ROUTE", "MAX_RETRANSMIT").
    """
    patterns = [
        r"'errorReason':\s*'([A-Z_]+)'",
        r'"errorReason":\s*"([A-Z_]+)"',
        r"'error_reason':\s*'([A-Z_]+)'",
        r'"error_reason":\s*"([A-Z_]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return match.group(1)
    return None


def process_routing_ack_line(line):
    """Resolve a pending DM delivery ACK from a ROUTING_APP line in the
    --listen output (see the module docstring / CLAUDE.md for why this is
    text-parsed rather than using the meshtastic library's onResponse
    callback: the SerialInterface that sent the message is already closed
    by the time any ACK comes back over the mesh, so the callback would
    never fire - only the long-lived --listen process is still around to
    see it).
    """
    request_id = extract_request_id(line)
    if request_id is None:
        return

    error_reason = extract_routing_error_reason(line)
    is_ack = error_reason is None or error_reason == "NONE"

    with state_lock:
        target = find_message_by_packet_id(request_id)
        if target is None or not target.get("ack_requested") or target.get("status") != "sent":
            return

        target["status"] = "delivered" if is_ack else "unconfirmed"
        target.pop("ack_deadline", None)
        if is_ack:
            target.pop("error", None)
        else:
            target["error"] = error_reason or "NO_ACK"
        save_messages()

    print(
        f"[ACK] requestId={request_id} -> "
        f"{'delivered' if is_ack else 'unconfirmed'} ({error_reason})",
        flush=True,
    )


def ack_timeout_worker():
    """Flip DM sends that requested a delivery ACK to "unconfirmed" once
    their ack_deadline has passed without process_routing_ack_line() seeing
    a response. Kept separate from radio_health_worker so that one stays
    read-only (see CLAUDE.md's background-thread inventory).
    """
    while True:
        time.sleep(5)
        try:
            now_ts = time.time()
            changed = False
            with state_lock:
                for message in messages:
                    if (
                        message.get("status") == "sent"
                        and message.get("ack_requested")
                        and message.get("ack_deadline")
                        and now_ts >= message["ack_deadline"]
                    ):
                        message["status"] = "unconfirmed"
                        message["error"] = "No delivery ACK received in time"
                        message.pop("ack_deadline", None)
                        changed = True
                if changed:
                    save_messages()
        except Exception as e:
            print(f"[ACK] ack_timeout_worker error: {e}", flush=True)


def find_message_by_packet_id(packet_id, chat_id=None):
    if packet_id is None:
        return None

    try:
        wanted = int(packet_id)
    except (TypeError, ValueError):
        return None

    for message in reversed(messages):
        try:
            current = int(message.get("packet_id"))
        except (TypeError, ValueError):
            continue

        if current != wanted:
            continue
        if chat_id and message.get("chat_id") != chat_id:
            continue
        return message

    return None


def build_reply_reference(message):
    if not isinstance(message, dict):
        return None

    return {
        "id": str(message.get("id", ""))[:128],
        "packet_id": message.get("packet_id"),
        "sender": str(message.get("sender", "Unknown"))[:160],
        "node_id": str(message.get("node_id", ""))[:32],
        "text": str(message.get("text", ""))[:1000],
        "time": str(message.get("time", ""))[:32],
        "chat_id": str(message.get("chat_id", ""))[:32],
        "chat_name": str(message.get("chat_name", ""))[:160],
    }

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

        node = dict(old)
        node.update({
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
            "favorite": old.get("favorite", False),
            # Keep the last known position. Text packets do not contain
            # coordinates and must not erase a position obtained earlier.
            "position": old.get("position"),
        })
        nodes[node_id] = node

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
        node = dict(old)
        node.update({
            "name": name,
            "node_id": node_id,
            "last_seen": time.time(),
            "last_time": now(),
            "rssi": rssi or old.get("rssi"),
            "snr": snr or old.get("snr"),
            "hop_start": hop_start or old.get("hop_start", ""),
            "relay_node": relay_node or old.get("relay_node", ""),
            "last_text": old.get("last_text", ""),
            "short_name": info.get("short_name") or short_name or old.get("short_name", "") or node_id[-4:],
            "hw_model": info.get("hw_model") or hw_model or old.get("hw_model", ""),
            "role": role or old.get("role", "CLIENT"),
            "ignored": old.get("ignored", False),
            "favorite": old.get("favorite", False),
            # NODEINFO refreshes identity/radio metadata only. Preserve the
            # latest known coordinates across NODEINFO broadcasts/restarts.
            "position": old.get("position"),
        })
        nodes[node_id] = node
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

def add_message(kind, sender, text, node_id="", chat_id=None, chat_name=None, reply_to=None, packet_id=None,
                 status=None, client_id=None):
    # Store all locally transmitted messages under one canonical direction.
    # Older waypoint notifications used "tx", while the rest of the chat
    # subsystem and UI use "me" for outgoing messages.
    if kind == "tx":
        kind = "me"

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
            if chat_type == "dm":
                chat_name = get_node_name(chat_id)
            else:
                chat_name = chats.get(chat_id, {}).get("name")
                if not chat_name:
                    if chat_id == CHANNEL_CHAT_ID:
                        chat_name = CHANNEL_CHAT_NAME
                    else:
                        channel_index = chat_id.split(":", 1)[-1] if ":" in chat_id else chat_id
                        chat_name = f"Channel {channel_index}"
        if chat_type == "dm" and chat_id not in chats:
            ensure_chat(chat_id, chat_name, force=True)
        msg = {
            "id": uuid.uuid4().hex,
            "kind": kind, "sender": sender, "node_id": node_id,
            "text": text, "time": now(),
            "chat_id": chat_id, "chat_type": chat_type, "chat_name": chat_name,
            # "pending" -> "sent" / "failed" is used by the async /api/send flow so the
            # frontend can render an optimistic bubble and reconcile it once the
            # background send worker actually talks to the radio. Messages created
            # through any other path (rx, system, waypoint notifications) are
            # considered final immediately.
            "status": status or "sent"
        }
        if client_id:
            # Echoed back to the frontend so an optimistic local bubble can be
            # matched to the authoritative server copy and removed once it
            # shows up in a poll response.
            msg["client_id"] = str(client_id)[:64]

        # Record the real local-radio owner of every transmitted message.
        # The value remains stable when another saved radio profile becomes
        # active, so chat direction is never reassigned retroactively.
        if kind == "me":
            owner_node_id = str(node_id or LOCAL_NODE_ID or "").strip()
            owner_profile_id = str(ACTIVE_PROFILE_ID or "").strip().lower()
            if not owner_profile_id and owner_node_id:
                owner_profile_id = owner_node_id.lstrip("!").lower()

            if owner_node_id:
                msg["owner_node_id"] = owner_node_id
            if owner_profile_id:
                msg["owner_profile_id"] = owner_profile_id
        if packet_id is not None:
            try:
                msg["packet_id"] = int(packet_id)
            except (TypeError, ValueError):
                pass
        if isinstance(reply_to, dict):
            msg["reply_to"] = {
                "id": str(reply_to.get("id", ""))[:128],
                "packet_id": reply_to.get("packet_id"),
                "sender": str(reply_to.get("sender", "Unknown"))[:160],
                "node_id": str(reply_to.get("node_id", ""))[:32],
                "text": str(reply_to.get("text", ""))[:1000],
                "time": str(reply_to.get("time", ""))[:32],
                "chat_id": str(reply_to.get("chat_id", chat_id))[:32],
                "chat_name": str(reply_to.get("chat_name", chat_name))[:160]
            }
        messages.append(msg)
        messages[:] = messages[-MAX_HISTORY_MESSAGES:]
        update_chat_last_message(chat_id, text, msg["time"])
        if kind == "rx" and chat_id in chats:
            chats[chat_id]["unread"] = chats[chat_id].get("unread", 0) + 1
            save_chats()
        save_messages()
    return msg

# How long a DM that requested a delivery ACK waits for the mesh to confirm
# it before ack_timeout_worker() gives up and marks it "unconfirmed". Started
# at 30s (the meshtastic library's own Timeout(maxSecs=20) default from
# util.py, plus headroom) but live testing between two real nodes ~2.53km
# apart showed the text consistently arriving while the routing ACK
# consistently failed to return within 30s - LoRa is half-duplex, so the ACK
# has to make the same (possibly multi-hop) trip back, doubling the loss
# chance. Bumped to 60s. Broadcast/channel sends never request an ACK -
# there is no per-recipient acknowledgment for broadcast traffic in the
# Meshtastic protocol itself, so their "sent" status is already the
# terminal, honest state.
ACK_TIMEOUT_SECONDS = 60


def update_message_status(message_id, chat_id, status, packet_id=None, error=None, ack_requested=None):
    """Update a message created with status="pending" once the background
    send worker (api/api_chat.py) has actually talked to the radio.

    This lets /api/send return immediately after queueing the transmission
    instead of blocking the HTTP request for the full serial round-trip.

    ``ack_requested=True`` marks a DM send that asked the mesh for a
    delivery ACK (see api/api_chat.py: wantAck=True for chat_type="dm"):
    it stamps an ack_deadline so ack_timeout_worker() can flip it to
    "unconfirmed" if process_routing_ack_line() never sees a response.
    """
    with state_lock:
        for message in reversed(messages):
            if message.get("id") == message_id and message.get("chat_id") == chat_id:
                message["status"] = status
                if packet_id is not None:
                    try:
                        message["packet_id"] = int(packet_id)
                    except (TypeError, ValueError):
                        pass
                if ack_requested:
                    message["ack_requested"] = True
                    message["ack_deadline"] = time.time() + ACK_TIMEOUT_SECONDS
                if error:
                    message["error"] = str(error)[:300]
                elif "error" in message and status in ("sent", "delivered"):
                    message.pop("error", None)
                save_messages()
                return message
    return None

def reconcile_interrupted_sends():
    """Fail out any message left in status="pending" from a previous run.

    The outgoing send queue (api/api_chat.py: send_queue) lives only in
    process memory. If the process restarts (deploy, crash, manual
    restart) while a message is still "pending", the job that would have
    sent it is gone - there is no queue to resume from - but the message
    itself survives in messages.json and would otherwise sit forever with
    a spinner that never resolves, since nothing re-queues it. Whether the
    previous process actually reached the radio before dying is unknown,
    so silently retrying could double-send; marking it failed lets the
    user retry deliberately from the UI instead.

    Must run after load_messages() (this file's __main__ startup
    sequence) and before send_queue starts accepting new jobs.
    """
    changed = False
    with state_lock:
        for message in messages:
            if message.get("status") == "pending":
                message["status"] = "failed"
                message["error"] = "Interrupted by server restart"
                message["error_code"] = "interrupted_by_restart"
                changed = True
        if changed:
            save_messages()
    if changed:
        print("[STARTUP] Reconciled pending messages left over from a previous run", flush=True)

def is_duplicate_text(sender, text, node_id=""):
    cleaned_text = str(text or "").strip()
    if not cleaned_text:
        return True

    sender_value = str(sender or "").strip()
    node_value = str(node_id or "").strip()
    key = (
        f"{sender_value}|{node_value}|{cleaned_text}"
        if node_value
        else f"{sender_value}|{cleaned_text}"
    )
    current_time = time.time()

    with state_lock:
        expired_keys = [
            old_key
            for old_key, timestamp in seen_recent_texts.items()
            if current_time - timestamp > 15
        ]
        for old_key in expired_keys:
            seen_recent_texts.pop(old_key, None)

        previous_time = seen_recent_texts.get(key)
        if previous_time is not None and current_time - previous_time < 15:
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
            if favorite: meta_parts.append("⚑ favorite")
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

                # Normalized telemetry used by the node detail panes.
                "battery_level": n.get("battery_level"),
                "voltage": n.get("voltage"),
                "channel_utilization": n.get("channel_utilization"),
                "air_util_tx": n.get("air_util_tx"),
                "uptime_seconds": n.get("uptime_seconds"),
                "temperature": n.get("temperature"),
                "humidity": n.get("humidity"),
                "pressure": n.get("pressure"),
                "current": n.get("current"),
                "power": n.get("power"),
                "last_telemetry_time": n.get("last_telemetry_time"),
                "device_metrics": n.get("device_metrics", {}),
                "environment_metrics": n.get("environment_metrics", {}),
                "power_metrics": n.get("power_metrics", {}),

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
            if (chat_id == CHANNEL_CHAT_ID or chat_id.startswith("channel:")) and last_sender:
                if last_sender_id:
                    sender_display = f"{last_sender} [{last_sender_id}]"
                else:
                    sender_display = last_sender
            chat_list.append({
                "id": chat_id, "name": chat.get("name", chat_id),
                "type": chat.get("type", "dm"), "last_message": last_msg,
                "last_time": chat.get("last_time", ""), "unread": unread,
                "is_channel": chat_id == CHANNEL_CHAT_ID or chat_id.startswith("channel:"),
                "ignored": chat_id.startswith("!") and nodes.get(chat_id, {}).get("ignored", False),
                "favorite": is_favorite, "last_sender": sender_display
            })
        def sort_key(c):
            if c["is_channel"]: return (0, c.get("id", ""), "")
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

    # listen_meshtastic() only assigns listen_process while holding
    # radio_lock (when it creates the new Popen). Reading the global here
    # without the same lock left a narrow window where this could observe
    # a stale None right as the listener commits to starting a fresh
    # process, wrongly reporting "already stopped" with nothing to kill.
    with radio_lock:
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
        with radio_lock:
            listen_process = None
        time.sleep(1.0)

def wait_serial_release(device=None, timeout=8):
    # When no explicit device is supplied, Meshtastic CLI will auto-detect it
    # after the listener has stopped. There is no fixed path to inspect with lsof.
    if not device:
        return True

    start = time.time()

    while time.time() - start < timeout:
        try:
            result = subprocess.run(
                ["lsof", "-t", device],
                capture_output=True,
                text=True,
                timeout=2
            )

            if not result.stdout.strip():
                return True

        except Exception as e:
            print(f"[WARN] wait_serial_release error: {e}", flush=True)

        time.sleep(0.2)

    print(f"[WARN] Serial port still busy after {timeout}s: {device}", flush=True)
    return False


def prepare_radio_command(device=None, timeout=8):
    if (
        radio_connection_manager is not None
        and not radio_connection_manager.commands_allowed()
    ):
        print("[RADIO] Command rejected while radio is released", flush=True)
        return False

    pause_listen.set()
    stop_listener()

    if not wait_serial_release(device=device, timeout=timeout):
        return False

    return True


radio_connection_manager = RadioConnectionManager(
    pause_event=pause_listen,
    stop_listener=stop_listener,
    wait_serial_release=wait_serial_release,
    serial_port=MESHTASTIC_PORT,
    log_event=log_system_event,
)


def is_radio_available():
    identity_ok = RADIO_IDENTITY_RESULT.get("status") in {"MATCH", "NOT_CHECKED"}
    return identity_ok and radio_connection_manager.commands_allowed()


class RadioBusyError(RuntimeError):
    """Raised by radio_session() when the serial port could not be claimed."""


@contextmanager
def radio_session(device=None, timeout=8, cooldown=2.0, extra_release_wait=None):
    """Claim exclusive access to the radio for the duration of the block.

    Centralizes the pause-listener / wait-for-port / hold-radio_lock /
    resume-listener dance shared by the send worker, channel discovery, and
    node tools. ``cooldown`` is the pause before the listener resumes
    (matches the delay send already needed to avoid "Timed out waiting for
    connection completion" from bouncing --listen too fast); pass 0 to skip
    it. ``extra_release_wait`` adds a second wait_serial_release() pass after
    the block exits, for callers whose CLI subprocess can close its serial
    descriptor slightly after returning (see node tools).
    """
    prepared = prepare_radio_command(device=device, timeout=timeout)
    try:
        if not prepared:
            raise RadioBusyError(f"Serial port busy: {device or 'auto-detect'}")
        with radio_lock:
            yield
    finally:
        if extra_release_wait is not None:
            wait_serial_release(device=device, timeout=extra_release_wait)
            time.sleep(0.4)
        if cooldown:
            time.sleep(cooldown)
        if is_radio_available():
            pause_listen.clear()


def update_base_status_from_info(info_output=None):
    global base_status
    try:
        if info_output is None:
            result = meshsrv.get_info(MESHTASTIC_CMD, serial_port=MESHTASTIC_PORT, timeout=15)
            output = result.stdout + result.stderr
        else:
            output = str(info_output)
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
    global seen_ids

    while True:
        time.sleep(300)
        current_time = time.time()

        with state_lock:
            if len(seen_ids) > 1000:
                seen_ids = set(list(seen_ids)[-500:])

            expired_keys = [
                key
                for key, timestamp in seen_recent_texts.items()
                if current_time - timestamp > 60
            ]
            for key in expired_keys:
                seen_recent_texts.pop(key, None)

def listen_meshtastic():
    global listen_process, base_status

    if RADIO_IDENTITY_RESULT.get("status") != "MATCH":
        print(
            f"[IDENTITY] Listener start blocked: status={RADIO_IDENTITY_RESULT.get('status')}",
            flush=True,
        )
        return

    nodeinfo_buffer = []
    collecting_nodeinfo = False
    consecutive_errors = 0
    max_consecutive_errors = 10

    while True:
        if pause_listen.is_set():
            time.sleep(0.5)
            continue

        with radio_lock:
            listen_process = None

        try:
            time.sleep(0.5)

            print("[DEBUG] Starting listener...")

            with radio_lock:
                if pause_listen.is_set():
                    continue

                listener_cmd = meshtastic_command(
                    MESHTASTIC_CMD, MESHTASTIC_PORT, "--listen"
                )
                print(f"[DEBUG] Listener command: {listener_cmd}", flush=True)
                listen_process = subprocess.Popen(
                    listener_cmd,
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

                    if "Received nodeinfo:" in line:
                        try:
                            if process_received_nodeinfo_line(line):
                                continue
                        except Exception as e:
                            print(f"[NODEINFO] Parse error: {e}", flush=True)

                    if "WAYPOINT_APP" in line or "'waypoint':" in line or '"waypoint":' in line:
                        try:
                            process_waypoint_line(line)
                        except Exception as e:
                            print(f"[WAYPOINT] Parse error: {e}", flush=True)

                    if "Publishing meshtastic.receive.routing:" in line:
                        try:
                            process_routing_ack_line(line)
                        except Exception as e:
                            print(f"[ACK] Parse error: {e}", flush=True)

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

                    if not text:
                        continue

                    radio_event("text")

                    pid = extract_packet_id(line)

                    if pid:
                        with state_lock:
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
                    elif "'to': '!" in line or '"to": "!' in line:
                        is_channel = False
                    elif re.search(r"'to':\s*[0-9]+,", line) or re.search(r'"to":\s*[0-9]+,', line):
                        if "4294967295" not in line:
                            is_channel = False
                    else:
                        is_channel = True

                    if is_channel:
                        incoming_channel_index = extract_channel_index(line)
                        chat_id = channel_chat_id(incoming_channel_index)
                        if chat_id not in chats:
                            with state_lock:
                                chats[chat_id] = {
                                    "id": chat_id,
                                    "name": CHANNEL_CHAT_NAME if incoming_channel_index == 0 else f"Channel {incoming_channel_index}",
                                    "type": "channel",
                                    "last_message": "",
                                    "last_time": "",
                                    "unread": 0,
                                }
                                save_chats()
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

                    reply_to = None
                    incoming_reply_id = extract_reply_id(line)
                    if incoming_reply_id:
                        with state_lock:
                            original = (
                                find_message_by_packet_id(incoming_reply_id, chat_id)
                                or find_message_by_packet_id(incoming_reply_id)
                            )
                            reply_to = build_reply_reference(original)

                    with state_lock:
                        add_message(
                            "rx",
                            sender,
                            text,
                            node_id,
                            chat_id,
                            reply_to=reply_to,
                            packet_id=pid,
                        )

                except Exception as e:
                    print(f"[LISTEN] Error processing line: {e}", flush=True)
                    continue

            # stop_listener() can null out the shared listen_process global
            # from another thread at any time (e.g. via radio_session()).
            # Snapshot it under the same radio_lock used for that write so
            # this doesn't poll/terminate a None that just got swapped in -
            # that used to crash this loop with "'NoneType' object has no
            # attribute 'poll'", caught only by the generic retry handler
            # below.
            with radio_lock:
                proc = listen_process
            return_code = proc.poll() if proc is not None else None

            if pause_listen.is_set():
                print("[DEBUG] Listener paused, terminating process...", flush=True)
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

                radio_event("listener_stop")

                with radio_lock:
                    listen_process = None
                time.sleep(0.5)
                continue

            if return_code is not None and return_code != 0:
                print(f"[WARN] Listener process ended with code: {return_code}", flush=True)
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            radio_event("listener_stop")

            with radio_lock:
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
                title="Listener Auto Recovery enabled",
                level="INFO",
                details=f"Recovery delay: {delay} seconds",
                source="recovery",
            )

    elif state["last_enabled"] != enabled:
        state["last_enabled"] = enabled

        log_system_event(
            title="Listener Auto Recovery enabled"
                if enabled
                else "Listener Auto Recovery disabled",
            level="INFO",
            details=f"Recovery delay: {delay} seconds"
                if enabled
                else "Automatic listener restart is disabled",
            source="recovery",
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
                title="Listener recovered successfully",
                level="OK",
                details="Automatic listener restart completed",
                source="recovery",
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
                title="Automatic listener recovery failed",
                level="WARNING",
                details="Listener is still unavailable "
                    f"{LISTENER_RECOVERY_RESULT_TIMEOUT} seconds "
                    "after restart",
                source="recovery",
            )

            state["restart_pending"] = False
            state["restart_requested_at"] = None
            state["down_since"] = now_ts

        return

    # Only a real listener process failure triggers recovery.
    if status != "LISTENER_DOWN":
        if state["down_since"] is not None:
            log_system_event(
                title="Automatic recovery cancelled",
                level="INFO",
                details="Listener recovered before automatic restart",
                source="recovery",
            )

        state["down_since"] = None
        return

    # --------------------------------------------------------
    # START CONFIRMATION TIMER
    # --------------------------------------------------------

    if state["down_since"] is None:
        state["down_since"] = now_ts

        log_system_event(
            title="Listener failure detected",
            level="WARNING",
            details=f"Waiting {delay} seconds before "
                "automatic recovery",
            source="recovery",
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
                title="Automatic recovery limit reached",
                level="ERROR",
                details=f"{LISTENER_RECOVERY_MAX_ATTEMPTS} attempts "
                    "within 30 minutes. Manual action required.",
                source="recovery",
            )
            state["limit_logged"] = True

        return

    # --------------------------------------------------------
    # RESTART LISTENER
    # --------------------------------------------------------

    attempt_number = len(state["attempts"]) + 1

    log_system_event(
        title="Automatic listener restart requested",
        level="ACTION",
        details=f"Attempt {attempt_number} of "
            f"{LISTENER_RECOVERY_MAX_ATTEMPTS}",
        source="recovery",
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
            title="Automatic listener restart failed",
            level="ERROR",
            details=str(error),
            source="recovery",
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

                if radio_connection_manager.is_released():
                    status = "RELEASED"
                    reason = "Radio released for external configuration"
                    level = "WARNING"
                    recommendation = "Reconnect the radio from Settings when configuration is complete"

                elif pause_listen.is_set():
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
    save_messages,
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
    lambda: MESHTASTIC_PORT,
    LOCAL_NODE_ID,
    LOCAL_NODE_NAME,
    radio_lock,
    radio_session,
    RadioBusyError,
    get_node_name,
    ensure_chat,
    add_message,
    reset_unread,
    get_node_info,
    save_nodes,
    now,
    radio_event,
    is_radio_available,
    update_message_status,
)

def _coordinate(value, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def resolve_weather_location():
    """Resolve Weather coordinates from the shared Reference Location."""
    with state_lock:
        reference = settings.get("reference_location", {})
        if not isinstance(reference, dict):
            reference = {}

        mode = str(reference.get("mode", "disabled")).strip().lower()

        if mode == "manual":
            manual = reference.get("manual", {})
            if not isinstance(manual, dict):
                manual = {}
            latitude = _coordinate(manual.get("latitude"), -90, 90)
            longitude = _coordinate(manual.get("longitude"), -180, 180)
            if latitude is not None and longitude is not None:
                return {
                    "latitude": latitude,
                    "longitude": longitude,
                    "name": "Manual reference",
                    "source": "manual",
                }

        if mode == "node":
            node_id = str(reference.get("node_id", "")).strip()
            node = nodes.get(node_id) or {}
            position = node.get("position") if isinstance(node.get("position"), dict) else {}
            latitude_raw = position.get(
                "latitude",
                position.get("latitude_i", node.get("latitude")),
            )
            longitude_raw = position.get(
                "longitude",
                position.get("longitude_i", node.get("longitude")),
            )
            try:
                latitude_raw = float(latitude_raw)
                longitude_raw = float(longitude_raw)
            except (TypeError, ValueError):
                latitude_raw = longitude_raw = None

            # Meshtastic integer coordinates are scaled by 1e-7.
            if latitude_raw is not None and abs(latitude_raw) > 90:
                latitude_raw /= 10_000_000
            if longitude_raw is not None and abs(longitude_raw) > 180:
                longitude_raw /= 10_000_000

            latitude = _coordinate(latitude_raw, -90, 90)
            longitude = _coordinate(longitude_raw, -180, 180)
            if latitude is not None and longitude is not None:
                return {
                    "latitude": latitude,
                    "longitude": longitude,
                    "name": node.get("name") or node.get("long_name") or node_id,
                    "source": "node",
                }

    return {
        "latitude": WEATHER_LATITUDE,
        "longitude": WEATHER_LONGITUDE,
        "name": WEATHER_LOCATION_NAME,
        "source": "configured",
    }


_weather_providers = {
    "openweather": OpenWeatherProvider(OpenWeatherConfig(
        api_key=OPENWEATHER_API_KEY,
        latitude=WEATHER_LATITUDE,
        longitude=WEATHER_LONGITUDE,
        location_name=WEATHER_LOCATION_NAME,
        language=WEATHER_LANGUAGE,
        cache_seconds=WEATHER_CACHE_SECONDS,
    )),
    "weatherapi": WeatherApiProvider(WeatherApiConfig(
        api_key=WEATHERAPI_API_KEY,
        latitude=WEATHER_LATITUDE,
        longitude=WEATHER_LONGITUDE,
        location_name=WEATHER_LOCATION_NAME,
        language=WEATHER_LANGUAGE,
        cache_seconds=WEATHER_CACHE_SECONDS,
    )),
}
weather_manager = WeatherManager(
    _weather_providers,
    active_id=WEATHER_PROVIDER if WEATHER_PROVIDER in _weather_providers else "openweather",
)
register_weather_routes(
    app,
    weather_manager,
    resolve_weather_location,
    secrets_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_secrets.py"),
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
    radio_session=radio_session,
    RadioBusyError=RadioBusyError,
    log_system_event=log_system_event,
    is_radio_available=is_radio_available,
)
register_node_icon_routes(app, PROFILE_DATA_DIR, LOCAL_NODE_ID, is_valid_node_id)



def _format_bytes(value):
    size = float(value or 0)
    units = ("B", "KB", "MB", "GB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0


def _json_item_count(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            for key in ("messages", "nodes", "chats", "items", "history"):
                value = data.get(key)
                if isinstance(value, (list, dict)):
                    return len(value)
            return len(data)
    except Exception:
        return 0
    return 0


def _waypoint_count(path):
    if not path or not os.path.exists(path):
        return 0
    try:
        connection = sqlite3.connect(path, timeout=2)
        try:
            row = connection.execute("SELECT COUNT(*) FROM waypoints").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            connection.close()
    except Exception:
        return 0


def _profile_storage_summary(profile_dir):
    summary = {
        "total_bytes": 0,
        "messages_bytes": 0,
        "telemetry_bytes": 0,
        "waypoints_bytes": 0,
        "icons_bytes": 0,
    }
    if not profile_dir or not os.path.isdir(profile_dir):
        return summary

    for current_root, _, filenames in os.walk(profile_dir):
        for filename in filenames:
            path = os.path.join(current_root, filename)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            summary["total_bytes"] += size
            relative = os.path.relpath(path, profile_dir)
            if relative == "messages.json":
                summary["messages_bytes"] += size
            elif relative == "telemetry_history.json":
                summary["telemetry_bytes"] += size
            elif relative == "waypoints.db":
                summary["waypoints_bytes"] += size
            elif relative.startswith("node_icons" + os.sep):
                summary["icons_bytes"] += size
    return summary


# ============================================================
# API ROUTES
# ============================================================

def resolve_ui_language():
    with state_lock:
        language_setting = normalize_settings(settings).get("language", "auto")

    if language_setting != "auto":
        return language_setting

    supported = [lang for lang in SUPPORTED_LANGUAGES if lang != "auto"]
    return request.accept_languages.best_match(supported, default="en")

# Each provider owns its own ui.language -> provider-language-code mapping
# (see WeatherProvider.LANGUAGE_MAP in weather/providers/base.py) since that
# mapping is provider-specific - e.g. OpenWeather uses "ua" for Ukrainian,
# WeatherAPI uses "uk" directly. This just delegates to whichever provider is
# currently active.
def resolve_weather_language(ui_language):
    # Weather data is one cache shared by every connected client (see
    # weather/providers/*.py) - there's no single request to resolve "auto"
    # against, so fall back to the static config.py default instead of
    # guessing from whichever browser happened to trigger this call.
    if ui_language == "auto":
        ui_language = WEATHER_LANGUAGE
    return weather_manager.active().resolve_language(ui_language)

# Registered here (rather than alongside the other register_*_routes calls
# above) because it needs weather_manager/resolve_weather_language, both
# defined above this point.
register_settings_routes(
    app,
    state_lock,
    settings,
    save_settings,
    handle_errors,
    weather_manager=weather_manager,
    resolve_weather_language=resolve_weather_language,
)

@app.route("/")
def index():
    ui_language = resolve_ui_language()

    # set_language() only otherwise runs at startup and on
    # settings save (see resolve_weather_language()) - neither has a browser
    # to resolve "auto" against, so it fell back to the static config.py
    # default (English) and stuck there. Every page load does have a
    # browser, so when the stored preference is "auto", sync the shared
    # weather cache to whatever resolve_ui_language() just resolved for
    # this request.
    with state_lock:
        language_setting = normalize_settings(settings).get("language", "auto")
    if language_setting == "auto":
        resolved_weather_language = resolve_weather_language(ui_language)
        # set_language() unconditionally invalidates the shared weather
        # cache, so only call it when the resolved language actually
        # changed - otherwise every page load would defeat
        # WEATHER_CACHE_SECONDS and re-hit the provider's API for nothing.
        active_provider = weather_manager.active()
        if resolved_weather_language != active_provider.config.language:
            active_provider.set_language(resolved_weather_language)

    return render_template(
        "index.html",
        app_version=APP_VERSION,
        ui_language=ui_language,
    )

@app.route("/api/sensors")
def api_sensors():
    return jsonify(sensor_data)

@app.route("/api/instance")
def api_instance_identity():
    identity = instance_manager.get()
    result = dict(RADIO_IDENTITY_RESULT)
    return jsonify({
        "ok": True,
        "instance_name": identity.get("instance_name", "MeshCenter"),
        "hostname": identity.get("hostname", ""),
        "active_profile_id": identity.get("active_profile_id", ""),
        "profile_path": PROFILE_DATA_DIR,
        "configured": dict(identity.get("radio", {})),
        "detected": dict(result.get("detected") or identity.get("runtime", {}).get("last_detected_radio", {})),
        "status": result.get("status") or identity.get("runtime", {}).get("identity_status", "NOT_CHECKED"),
        "checked_at": result.get("checked_at") or identity.get("runtime", {}).get("last_detected_at"),
        "error": result.get("error") or identity.get("runtime", {}).get("last_error"),
    })

@app.route("/api/node-manager/dashboard")
@app.route("/api/devices/dashboard")
def api_devices_dashboard():
    identity = instance_manager.get()
    configured = dict(identity.get("radio", {}))
    runtime = dict(identity.get("runtime", {}))
    detected = dict(
        RADIO_IDENTITY_RESULT.get("detected")
        or runtime.get("last_detected_radio", {})
        or {}
    )

    with state_lock:
        listener_running = bool(radio_health.get("listener_running", False))
        last_restart = float(radio_health.get("last_restart", 0) or 0)
        listener_pid = (
            int(listen_process.pid)
            if listen_process is not None and listen_process.poll() is None
            else None
        )

    connection = radio_connection_manager.status(listener_running)
    profile_metadata = {}
    profile_json = os.path.join(PROFILE_DATA_DIR, "profile.json")
    try:
        with open(profile_json, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            profile_metadata = loaded
    except Exception:
        profile_metadata = {}

    paths = PROFILE_CONTEXT.get("paths", {}) if isinstance(PROFILE_CONTEXT, dict) else {}
    storage = _profile_storage_summary(PROFILE_DATA_DIR)
    counts = {
        "messages": _json_item_count(paths.get("messages", "")),
        "nodes": _json_item_count(paths.get("nodes", "")),
        "chats": _json_item_count(paths.get("chats", "")),
        "telemetry_records": _json_item_count(paths.get("telemetry_history", "")),
        "waypoints": _waypoint_count(paths.get("waypoints_db", "")),
    }

    connected_since = None
    if listener_running and last_restart:
        connected_since = datetime.fromtimestamp(last_restart).astimezone().isoformat(timespec="seconds")

    profiles = []
    profiles_root = os.path.join(DATA_DIR, "profiles")
    try:
        for entry in sorted(os.scandir(profiles_root), key=lambda item: item.name.lower()):
            if not entry.is_dir():
                continue
            metadata = {}
            try:
                with open(os.path.join(entry.path, "profile.json"), "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    metadata = loaded
            except Exception:
                metadata = {}
            profile_radio = dict(metadata.get("radio", {}))
            profile_id = metadata.get("profile_id") or entry.name
            active = profile_id == identity.get("active_profile_id", "")
            profiles.append({
                "profile_id": profile_id,
                "active": active,
                "radio": {
                    "node_id": profile_radio.get("node_id", ""),
                    "long_name": profile_radio.get("long_name", profile_id),
                    "short_name": profile_radio.get("short_name", ""),
                    "hardware": profile_radio.get("hardware", ""),
                    "role": profile_radio.get("role", ""),
                    "port": profile_radio.get("port", ""),
                    "identity_status": (RADIO_IDENTITY_RESULT.get("status") or runtime.get("identity_status", "NOT_CHECKED")) if active else "NOT_CHECKED",
                },
                "connection": {
                    "mode": connection.get("mode", "unknown") if active else "offline",
                    "listener_running": listener_running if active else False,
                },
            })
    except FileNotFoundError:
        profiles = []

    return jsonify({
        "ok": True,
        "instance": {
            "name": identity.get("instance_name", "MeshCenter"),
            "hostname": identity.get("hostname", ""),
        },
        "radio": {
            "node_id": detected.get("node_id") or configured.get("node_id", ""),
            "long_name": detected.get("long_name") or configured.get("long_name", ""),
            "short_name": detected.get("short_name") or configured.get("short_name", ""),
            "hardware": detected.get("hardware") or configured.get("hardware", ""),
            "firmware_version": detected.get("firmware_version") or configured.get("firmware_version", ""),
            "role": detected.get("role") or configured.get("role", ""),
            "port": connection.get("serial_port") or configured.get("port", ""),
            "identity_status": RADIO_IDENTITY_RESULT.get("status")
                or runtime.get("identity_status", "NOT_CHECKED"),
            "identity_checked_at": RADIO_IDENTITY_RESULT.get("checked_at")
                or runtime.get("last_detected_at"),
        },
        "connection": {
            **connection,
            "listener_pid": listener_pid,
            "connected_since": connected_since,
        },
        "profiles": profiles,
        "profile": {
            "profile_id": identity.get("active_profile_id", ""),
            "path": PROFILE_DATA_DIR,
            "created_at": profile_metadata.get("created_at"),
            "last_used_at": profile_metadata.get("last_used_at"),
            "counts": counts,
            "storage": {
                **storage,
                "total": _format_bytes(storage["total_bytes"]),
                "messages": _format_bytes(storage["messages_bytes"]),
                "telemetry": _format_bytes(storage["telemetry_bytes"]),
                "waypoints": _format_bytes(storage["waypoints_bytes"]),
                "icons": _format_bytes(storage["icons_bytes"]),
            },
        },
    })



def _restart_meshcenter_after_profile_switch():
    """Restart the service after the activation response reaches the browser."""
    time.sleep(1.2)
    try:
        subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "restart", "meshcenter.service"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as error:
        log_system_event(
            "Radio profile restart failed",
            "ERROR",
            str(error),
            source="radio",
        )



def _save_detected_radio_runtime(detection, status=None, error=None):
    """Persist the latest read-only detection result."""
    global INSTANCE_IDENTITY, RADIO_IDENTITY_RESULT

    detected = dict(detection.get("detected") or {})
    checked_at = detection.get("checked_at")
    configured = dict(INSTANCE_IDENTITY.get("radio", {}))
    resolved_status = status or (
        compare_radio_identity(configured, detected)
        if detected.get("node_id")
        else "NOT_FOUND"
    )

    RADIO_IDENTITY_RESULT = {
        "status": resolved_status,
        "checked_at": checked_at,
        "configured": configured,
        "detected": detected,
        "error": error or detection.get("error"),
    }

    updated = dict(INSTANCE_IDENTITY)
    runtime = dict(updated.get("runtime") or {})
    runtime.update({
        "cli_path": MESHTASTIC_CMD,
        "last_detected_at": checked_at,
        "identity_status": resolved_status,
        "last_error": error or detection.get("error"),
        "last_detected_radio": detected,
    })
    updated["runtime"] = runtime
    INSTANCE_IDENTITY = instance_manager.save(updated)


@app.route("/api/node-manager/radio/detect", methods=["POST"])
def api_detect_new_radio():
    """Release the listener and inspect any currently connected serial radio."""
    with state_lock:
        listener_running = bool(radio_health.get("listener_running", False))

    connection = radio_connection_manager.status(listener_running)
    if connection.get("mode") != "released":
        released, connection = radio_connection_manager.release(timeout=18)
        if not released:
            return jsonify({
                "ok": False,
                "error": connection.get("last_error") or "The radio could not be released.",
                "connection": connection,
            }), 409

    detection = detect_connected_radio(
        MESHTASTIC_CMD,
        preferred_port=MESHTASTIC_PORT,
        timeout_per_port=35,
        settle_seconds=2.5,
    )

    if not detection.get("ok"):
        log_system_event(
            "Radio detection failed",
            "WARNING",
            detection.get("error") or "No Meshtastic radio identity could be read.",
            source="radio",
        )
        return jsonify({
            "ok": False,
            "code": "RADIO_NOT_FOUND",
            "error": detection.get("error"),
            "attempts": detection.get("attempts", []),
            "candidates": detection.get("candidates", []),
            "connection": radio_connection_manager.status(False),
        }), 409

    detected = dict(detection.get("detected") or {})
    profile_id = profile_manager.profile_id_from_node_id(detected.get("node_id"))
    profile_exists = True
    try:
        existing = profile_manager.get_profile(profile_id)
        profile = {
            "profile_id": existing["profile_id"],
            "metadata": existing["metadata"],
        }
    except FileNotFoundError:
        profile_exists = False
        profile = None

    configured = dict(INSTANCE_IDENTITY.get("radio", {}))
    identity_status = compare_radio_identity(configured, detected)

    # Detection is provisional until the user confirms it. Keep the active
    # profile identity untouched so the UI never mixes old profile storage
    # with the newly detected physical radio.
    return jsonify({
        "ok": True,
        "detected": detected,
        "profile_id": profile_id,
        "profile_exists": profile_exists,
        "profile": profile,
        "identity_status": identity_status,
        "connection": radio_connection_manager.status(False),
        "message": (
            "Known radio detected."
            if profile_exists else
            "New radio detected. A clean profile can now be created."
        ),
    })


@app.route("/api/node-manager/radio/accept", methods=["POST"])
def api_accept_detected_radio():
    """Verify the connected radio again, create/use its profile and restart."""
    global INSTANCE_IDENTITY

    data = request.get_json(silent=True) or {}
    requested_node_id = str(data.get("node_id") or "").strip().lower()
    requested_port = str(data.get("port") or "").strip()

    detection = detect_connected_radio(
        MESHTASTIC_CMD,
        preferred_port=requested_port or MESHTASTIC_PORT,
        timeout_per_port=35,
        settle_seconds=1.5,
    )
    if not detection.get("ok"):
        log_system_event(
            "Radio confirmation failed",
            "WARNING",
            detection.get("error") or "The radio could not be verified.",
            source="radio",
        )
        return jsonify({
            "ok": False,
            "code": "RADIO_NOT_FOUND",
            "error": detection.get("error"),
            "attempts": detection.get("attempts", []),
            "candidates": detection.get("candidates", []),
        }), 409

    detected = dict(detection.get("detected") or {})
    detected_node_id = str(detected.get("node_id") or "").strip().lower()
    if requested_node_id and requested_node_id != detected_node_id:
        return jsonify({
            "ok": False,
            "code": "RADIO_CHANGED",
            "error": (
                "The connected radio changed during confirmation. "
                f"Expected {requested_node_id}, detected {detected_node_id}."
            ),
            "detected": detected,
        }), 409

    profile_id = profile_manager.profile_id_from_node_id(detected_node_id)
    created = False
    try:
        profile = profile_manager.get_profile(profile_id)
    except FileNotFoundError:
        profile = profile_manager.create_clean_profile(detected)
        created = True

    identity = instance_manager.get()
    updated = dict(identity)
    updated["active_profile_id"] = profile_id
    updated["radio"] = {
        "node_id": detected.get("node_id", ""),
        "long_name": detected.get("long_name", ""),
        "short_name": detected.get("short_name", ""),
        "hardware": detected.get("hardware", ""),
        "role": detected.get("role", ""),
        "port": detected.get("port") or requested_port or MESHTASTIC_PORT,
    }
    runtime = dict(updated.get("runtime") or {})
    runtime.update({
        "last_detected_at": detection.get("checked_at"),
        "identity_status": "MATCH",
        "last_error": None,
        "last_detected_radio": dict(updated["radio"]),
    })
    updated["runtime"] = runtime
    INSTANCE_IDENTITY = instance_manager.save(updated)
    _save_detected_radio_runtime(detection, status="MATCH")

    log_system_event(
        "New radio profile created" if created else "Radio profile selected",
        "ACTION",
        (
            f"{updated['radio'].get('long_name') or detected_node_id} "
            f"({detected_node_id}) on {updated['radio'].get('port')}"
        ),
        source="radio",
    )

    threading.Thread(
        target=_restart_meshcenter_after_profile_switch,
        daemon=True,
    ).start()

    return jsonify({
        "ok": True,
        "created": created,
        "profile_id": profile_id,
        "radio": updated["radio"],
        "restart_required": True,
        "message": (
            "A clean radio profile was created. MeshCenter is restarting."
            if created else
            "The saved radio profile was selected. MeshCenter is restarting."
        ),
    }), 202


@app.route("/api/node-manager/profiles/<profile_id>/activate", methods=["POST"])
def api_activate_radio_profile(profile_id):
    """Activate a saved radio profile only when the connected USB radio matches it."""
    global INSTANCE_IDENTITY

    try:
        profile = profile_manager.get_profile(profile_id)
    except ValueError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
    except FileNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except Exception as error:
        return jsonify({"ok": False, "error": f"Could not read radio profile: {error}"}), 500

    identity = instance_manager.get()
    current_profile_id = str(identity.get("active_profile_id") or "").strip().lower()
    selected_profile_id = str(profile.get("profile_id") or "").strip().lower()

    if selected_profile_id == current_profile_id:
        return jsonify({
            "ok": True,
            "already_active": True,
            "profile_id": selected_profile_id,
            "message": "This radio profile is already active.",
        })

    metadata = dict(profile.get("metadata") or {})
    expected_radio = dict(metadata.get("radio") or {})
    expected_node_id = str(expected_radio.get("node_id") or "").strip().lower()
    if not expected_node_id:
        return jsonify({"ok": False, "error": "The selected profile has no Meshtastic node ID."}), 409

    # Stop the listener first so the CLI can safely probe the USB serial radio.
    with state_lock:
        listener_running = bool(radio_health.get("listener_running", False))

    connection = radio_connection_manager.status(listener_running)
    if connection.get("mode") != "released":
        released, connection = radio_connection_manager.release(timeout=15)
        if not released:
            return jsonify({
                "ok": False,
                "error": connection.get("last_error") or "The active radio could not be released.",
                "connection": connection,
            }), 409

    detection = detect_connected_radio(
        MESHTASTIC_CMD,
        preferred_port=expected_radio.get("port") or MESHTASTIC_PORT,
        timeout_per_port=35,
        settle_seconds=2.0,
    )
    detected_radio = dict(detection.get("detected") or {})
    detected_node_id = str(detected_radio.get("node_id") or "").strip().lower()

    if not detected_node_id:
        return jsonify({
            "ok": False,
            "code": "RADIO_NOT_FOUND",
            "error": (
                "No Meshtastic radio was detected. Connect the radio assigned to "
                f"{expected_radio.get('long_name') or selected_profile_id} and try again."
            ),
            "expected": expected_radio,
            "detected": detected_radio,
            "attempts": detection.get("attempts", []),
            "candidates": detection.get("candidates", []),
            "connection": radio_connection_manager.status(False),
        }), 409

    if detected_node_id != expected_node_id:
        return jsonify({
            "ok": False,
            "code": "RADIO_MISMATCH",
            "error": (
                "The connected radio does not match the selected profile. "
                f"Expected {expected_radio.get('long_name') or expected_node_id} "
                f"({expected_node_id}), detected "
                f"{detected_radio.get('long_name') or detected_node_id} ({detected_node_id})."
            ),
            "expected": expected_radio,
            "detected": detected_radio,
            "connection": radio_connection_manager.status(False),
        }), 409

    updated = dict(identity)
    updated["active_profile_id"] = selected_profile_id
    updated["radio"] = {
        "node_id": detected_radio.get("node_id") or expected_radio.get("node_id", ""),
        "long_name": detected_radio.get("long_name") or expected_radio.get("long_name", ""),
        "short_name": detected_radio.get("short_name") or expected_radio.get("short_name", ""),
        "hardware": detected_radio.get("hardware") or expected_radio.get("hardware", ""),
        "role": detected_radio.get("role") or expected_radio.get("role", ""),
        "port": detected_radio.get("port") or expected_radio.get("port") or MESHTASTIC_PORT,
    }
    runtime = dict(updated.get("runtime") or {})
    runtime.update({
        "last_detected_at": detection.get("checked_at"),
        "identity_status": "MATCH",
        "last_error": None,
        "last_detected_radio": dict(updated["radio"]),
    })
    updated["runtime"] = runtime

    try:
        INSTANCE_IDENTITY = instance_manager.save(updated)
    except Exception as error:
        return jsonify({"ok": False, "error": f"Could not save active radio profile: {error}"}), 500

    log_system_event(
        "Radio profile activated",
        "ACTION",
        (
            f"Profile {selected_profile_id} selected for "
            f"{updated['radio'].get('long_name') or updated['radio'].get('node_id')}"
        ),
        source="radio",
    )

    threading.Thread(
        target=_restart_meshcenter_after_profile_switch,
        daemon=True,
    ).start()

    return jsonify({
        "ok": True,
        "accepted": True,
        "restart_required": True,
        "profile_id": selected_profile_id,
        "radio": updated["radio"],
        "message": "Radio profile activated. MeshCenter is restarting.",
    }), 202


@app.route("/api/devices")
def api_profile_devices():
    assignments = device_manager.load_or_create()
    configured_devices = assignments.get("devices", {})

    try:
        camera_status = camera.get_camera_status()
    except Exception as error:
        camera_status = {"ok": False, "started": False, "error": str(error)}

    env_fields = ("temperature", "humidity", "pressure")
    power_fields = ("voltage", "current", "power")
    environment_values = {key: sensor_data.get(key) for key in env_fields}
    power_values = {key: sensor_data.get(key) for key in power_fields}
    environment_detected = any(value is not None for value in environment_values.values())
    power_detected = any(value is not None for value in power_values.values())

    # devices.json schema v2: a single "camera" object became "cameras",
    # keyed by CameraDriver id (see camera/camera_manager.py). This route
    # still only knows about the CSI camera directly (camera_manager isn't
    # wired into it yet - see the project's usb-camera-plan notes), so it
    # reads the "csi" entry specifically rather than the whole dict.
    camera_cfg = dict(configured_devices.get("cameras", {}).get("csi", {}))
    environment_cfg = dict(configured_devices.get("environment", {}))
    power_cfg = dict(configured_devices.get("power", {}))

    return jsonify({
        "ok": True,
        "profile_id": ACTIVE_PROFILE_ID,
        "devices_file": device_manager.path,
        "devices": [
            {
                "id": "camera",
                "name": "Camera",
                "kind": "camera",
                "assigned": bool(camera_cfg.get("assigned", True)),
                "enabled": bool(camera_cfg.get("enabled", True)),
                "detected": bool(camera_status.get("ok", False)),
                "active": bool(camera_status.get("started", False)),
                "source": camera_cfg.get("source", "csi"),
                # Real sensor model from Picamera2 (see camera.init_camera())
                # wins over a manually configured label, which itself wins
                # over a generic placeholder when neither is available (e.g.
                # camera not detected yet).
                "model": camera_status.get("model") or camera_cfg.get("model") or "Raspberry Pi Camera",
                "status": "active" if camera_status.get("started") else ("available" if camera_status.get("ok") else "unavailable"),
                "action": {"label": "Open Camera", "tab": "video"},
            },
            {
                "id": "environment",
                "name": "Environmental sensor",
                "kind": "sensor",
                "assigned": bool(environment_cfg.get("assigned", True)),
                "enabled": bool(environment_cfg.get("enabled", True)),
                "detected": environment_detected,
                "driver": environment_cfg.get("driver") or "Environmental telemetry",
                "status": "data" if environment_detected else "no_data",
                "values": environment_values,
                "last_update": sensor_data.get("last_update"),
            },
            {
                "id": "power",
                "name": "Power monitor",
                "kind": "sensor",
                "assigned": bool(power_cfg.get("assigned", True)),
                "enabled": bool(power_cfg.get("enabled", True)),
                "detected": power_detected,
                "driver": power_cfg.get("driver") or "Power telemetry",
                "status": "data" if power_detected else "no_data",
                "values": power_values,
                "last_update": sensor_data.get("last_update"),
            },
        ],
    })


@app.route("/api/base_status")
def api_base_status():
    identity = instance_manager.get()
    radio = dict(identity.get("radio") or {})
    status = base_status.copy()
    status["node_name"] = radio.get("long_name") or LOCAL_NODE_NAME
    status["node_id"] = radio.get("node_id") or LOCAL_NODE_ID
    status["profile_id"] = identity.get("active_profile_id", "")
    response = jsonify(status)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

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

@app.route("/api/radio_connection/status")
@handle_errors
def api_radio_connection_status():
    with state_lock:
        listener_running = bool(radio_health.get("listener_running", False))
    return jsonify({
        "ok": True,
        "radio": radio_connection_manager.status(listener_running)
    })


@app.route("/api/radio_connection/release", methods=["POST"])
@handle_errors
def api_radio_connection_release():
    ok, status = radio_connection_manager.release(timeout=12)
    return jsonify({
        "ok": ok,
        "radio": status,
        "message": status.get("message", "")
    }), (200 if ok else 409)


@app.route("/api/radio_connection/reconnect", methods=["POST"])
@handle_errors
def api_radio_connection_reconnect():
    if RADIO_IDENTITY_RESULT.get("status") != "MATCH":
        return jsonify({
            "ok": False,
            "error": "Radio identity mismatch - reconnect is blocked for the active profile"
        }), 409
    ok, status = radio_connection_manager.reconnect()
    if ok:
        radio_event("restart")
    return jsonify({
        "ok": ok,
        "radio": status,
        "message": status.get("message", "")
    }), (200 if ok else 409)


@app.route("/api/restart_listener", methods=["POST"])
@handle_errors
def api_restart_listener():
    global listen_process

    if RADIO_IDENTITY_RESULT.get("status") != "MATCH":
        return jsonify({
            "ok": False,
            "error": "Radio identity mismatch - listener restart is blocked"
        }), 409

    if radio_connection_manager.is_released():
        return jsonify({
            "ok": False,
            "error": "The radio is released for external configuration"
        }), 409

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
    if RADIO_IDENTITY_RESULT.get("status") != "MATCH":
        return jsonify({
            "ok": False,
            "error": "Radio identity mismatch - network rescan is blocked"
        }), 409
    if radio_connection_manager.is_released():
        return jsonify({
            "ok": False,
            "error": "Reconnect the radio before rescanning the network"
        }), 409

    try:
        with radio_session(device=MESHTASTIC_PORT, timeout=10, cooldown=2.0):
            # Fetch --info while still holding radio_lock, so this doesn't
            # race the listener (or any other radio_session caller) the way
            # a bare parse_nodes_from_info() call used to: that helper runs
            # its own unlocked subprocess when given no info_output.
            result = subprocess.run(
                meshtastic_command(MESHTASTIC_CMD, MESHTASTIC_PORT, "--info"),
                capture_output=True,
                text=True,
                timeout=30,
            )
            info_output = result.stdout + result.stderr

        success = parse_nodes_from_info(info_output=info_output)

        return jsonify({
            "ok": bool(success),
            "message": (
                "Network rescan completed"
                if success
                else "Network rescan completed, no changes found"
            )
        })

    except RadioBusyError:
        return jsonify({
            "ok": False,
            "error": "Meshtastic serial port is busy",
            "error_code": "radio_busy"
        }), 503

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500

@app.route("/api/clear_chat", methods=["POST"])
@handle_errors
def api_clear_chat():
    data = request.get_json(force=True)
    chat_id = data.get("chat_id", "").strip()
    if not chat_id or not is_valid_chat_id(chat_id):
        return jsonify({"ok": False, "error": "Invalid chat_id", "error_code": "invalid_chat_id"}), 400
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
        return jsonify({"ok": False, "error": "Invalid chat", "error_code": "invalid_chat"}), 400
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

@app.route("/api/waypoints", methods=["GET"])
@handle_errors
def api_waypoints():
    include_expired = request.args.get("include_expired", "0").lower() in ("1", "true", "yes")
    include_hidden = request.args.get("include_hidden", "0").lower() in ("1", "true", "yes")
    include_raw = request.args.get("include_raw", "0").lower() in ("1", "true", "yes")
    waypoints = waypoint_store.list(
        include_expired=include_expired,
        include_hidden=include_hidden,
    )
    for waypoint in waypoints:
        sender_id = waypoint.get("sender_id") or ""
        waypoint["sender_name"] = get_node_name(sender_id) if sender_id else "Unknown"
        if not include_raw:
            waypoint.pop("raw_packet", None)
    return jsonify({
        "ok": True,
        "waypoints": waypoints,
        "total": len(waypoints),
    })


@app.route("/api/waypoints/<int:waypoint_id>", methods=["GET"])
@handle_errors
def api_waypoint_detail(waypoint_id):
    waypoint = waypoint_store.get(waypoint_id)
    if not waypoint:
        return jsonify({"ok": False, "error": "Waypoint not found", "error_code": "waypoint_not_found"}), 404

    sender_id = waypoint.get("sender_id") or ""
    waypoint["sender_name"] = get_node_name(sender_id) if sender_id else "Unknown"
    include_raw = request.args.get("include_raw", "0").lower() in ("1", "true", "yes")
    if not include_raw:
        waypoint.pop("raw_packet", None)
    return jsonify({"ok": True, "waypoint": waypoint})


@app.route("/api/waypoints/<int:waypoint_id>/hidden", methods=["POST"])
@handle_errors
def api_waypoint_hidden(waypoint_id):
    data = request.get_json(silent=True) or {}
    hidden = bool(data.get("hidden", True))
    waypoint = waypoint_store.set_hidden(waypoint_id, hidden)
    if not waypoint:
        return jsonify({"ok": False, "error": "Waypoint not found", "error_code": "waypoint_not_found"}), 404
    sender_id = waypoint.get("sender_id") or ""
    waypoint["sender_name"] = get_node_name(sender_id) if sender_id else "Unknown"
    waypoint.pop("raw_packet", None)
    return jsonify({"ok": True, "waypoint": waypoint})


@app.route("/api/waypoints/<int:waypoint_id>", methods=["DELETE"])
@handle_errors
def api_waypoint_delete(waypoint_id):
    deleted = waypoint_store.delete(waypoint_id)
    if not deleted:
        return jsonify({"ok": False, "error": "Waypoint not found", "error_code": "waypoint_not_found"}), 404
    return jsonify({"ok": True, "deleted": 1, "waypoint_id": waypoint_id})


@app.route("/api/waypoints/delete", methods=["POST"])
@handle_errors
def api_waypoints_delete_many():
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("waypoint_ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "waypoint_ids must be a list", "error_code": "waypoint_ids_not_a_list"}), 400
    try:
        waypoint_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid waypoint ID", "error_code": "invalid_waypoint_id"}), 400
    deleted = waypoint_store.delete_many(waypoint_ids)
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/waypoints", methods=["DELETE"])
@handle_errors
def api_waypoints_delete_all():
    deleted = waypoint_store.delete_all()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/waypoints/send", methods=["POST"])
@handle_errors
def api_waypoint_send():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    description = str(data.get("description") or "").strip()
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    channel_index = data.get("channel_index", 0)
    icon = data.get("icon", 128205)
    expire_at = data.get("expire_at")
    post_notification = bool(data.get("post_notification", True))

    if not name or len(name) > 30:
        return jsonify({"ok": False, "error": "Name is required and must be at most 30 characters", "error_code": "waypoint_invalid_name"}), 400
    if len(description) > 100:
        return jsonify({"ok": False, "error": "Description must be at most 100 characters", "error_code": "waypoint_invalid_description"}), 400
    try:
        latitude = float(latitude)
        longitude = float(longitude)
        channel_index = int(channel_index)
        icon = int(icon)
        expire_at = int(expire_at)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid waypoint data", "error_code": "waypoint_invalid_data"}), 400
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return jsonify({"ok": False, "error": "Coordinates are outside the valid range", "error_code": "waypoint_invalid_coordinates"}), 400
    if not (0 <= channel_index <= 7):
        return jsonify({"ok": False, "error": "Channel index must be between 0 and 7", "error_code": "waypoint_invalid_channel_index"}), 400
    if expire_at <= int(time.time()) + 30:
        return jsonify({"ok": False, "error": "Expiration must be in the future", "error_code": "waypoint_expiration_in_past"}), 400
    if not is_radio_available():
        return jsonify({"ok": False, "error": "Meshtastic radio is currently unavailable", "error_code": "radio_released"}), 503
    if not prepare_radio_command(MESHTASTIC_PORT, timeout=10):
        return jsonify({"ok": False, "error": "Meshtastic serial port is busy", "error_code": "radio_busy"}), 503

    cli_path = str(MESHTASTIC_CMD or "")
    python_path = os.path.join(os.path.dirname(cli_path), "python3")
    if not os.path.exists(python_path):
        python_path = sys.executable
    sender_script = os.path.join(PROJECT_DIR, "storage", "waypoint_sender.py")

    expires_text = datetime.fromtimestamp(expire_at).strftime("%d.%m.%Y %H:%M")
    notification_text = f"📍 Waypoint: {name}"
    if description:
        notification_text += f"\n{description}"
    notification_text += f"\n{latitude:.6f}, {longitude:.6f}\nExpires: {expires_text}"

    payload = {
        "port": MESHTASTIC_PORT,
        "name": name,
        "description": description,
        "latitude": latitude,
        "longitude": longitude,
        "channel_index": channel_index,
        "icon": icon,
        "expire_at": expire_at,
        "post_notification": post_notification,
        "notification_text": notification_text,
    }

    try:
        with radio_lock:
            result = subprocess.run(
                [python_path, sender_script],
                input=json.dumps(payload, ensure_ascii=False),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=35,
            )
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            print(f"[WAYPOINT SEND] Failed: {output}", flush=True)
            return jsonify({"ok": False, "error": output[-1000:] or "Waypoint send failed", "error_code": "waypoint_send_failed"}), 500

        try:
            sender_result = json.loads(output.splitlines()[-1])
            waypoint_id = int(sender_result["waypoint_id"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Waypoint sender returned invalid result: {output[-1000:]}") from error

        saved = waypoint_store.upsert({
            "waypoint_id": waypoint_id,
            "sender_id": LOCAL_NODE_ID,
            "name": name,
            "description": description,
            "latitude": latitude,
            "longitude": longitude,
            "icon": icon,
            "expire_at": expire_at,
            "channel_index": channel_index,
            "received_at": time.time(),
            "raw_packet": json.dumps({"source": "local-send", **sender_result}, ensure_ascii=False),
        })
        saved.pop("_event", None)
        saved.pop("raw_packet", None)
        saved["sender_name"] = LOCAL_NODE_NAME

        if post_notification:
            channel_id = channel_chat_id(channel_index)
            channel_name = CHANNEL_CHAT_NAME if channel_index == 0 else f"Channel {channel_index}"
            add_message(
                "me",
                LOCAL_NODE_NAME,
                notification_text,
                node_id=LOCAL_NODE_ID,
                chat_id=channel_id,
                chat_name=channel_name,
                packet_id=sender_result.get("notification_packet_id"),
            )

        print(
            f"[WAYPOINT SEND] Sent and saved: {name}; waypoint_id={waypoint_id}; "
            f"lat={latitude}; lon={longitude}; channel={channel_index}",
            flush=True,
        )
        log_system_event(
            title="Waypoint sent",
            level="OK",
            details=f"{name}; id {waypoint_id}; {latitude:.6f}, {longitude:.6f}; channel {channel_index}",
            source="waypoint",
        )
        return jsonify({
            "ok": True,
            "message": "Waypoint sent",
            "waypoint": saved,
            "notification_posted": post_notification,
            "sender_result": sender_result,
        })
    finally:
        if is_radio_available():
            pause_listen.clear()


@app.route("/api/telemetry/history")
def api_telemetry_history():
    limit = request.args.get("limit", 100, type=int)
    node_id = request.args.get("node_id", "").strip()

    if node_id and not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node_id"}), 400

    with state_lock:
        all_history = [
            record for record in telemetry.telemetry_history
            if isinstance(record, dict)
        ]

        if node_id:
            if node_id == LOCAL_NODE_ID:
                filtered = [
                    record for record in all_history
                    if record.get("node_id") in (None, "", LOCAL_NODE_ID)
                ]
            else:
                filtered = [
                    record for record in all_history
                    if record.get("node_id") == node_id
                ]
        else:
            # The main telemetry cards and charts remain local-node only.
            filtered = [
                record for record in all_history
                if record.get("node_id") in (None, "", LOCAL_NODE_ID)
            ]

        history = filtered[-limit:] if limit > 0 else filtered

    return jsonify({
        "history": history,
        "total": len(filtered),
        "node_id": node_id or LOCAL_NODE_ID,
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
    node_id = request.args.get("node_id", "").strip()

    if node_id and not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node_id"}), 400

    if data_type not in ("environment", "power", "all"):
        return jsonify({"ok": False, "error": "Invalid type"}), 400

    if export_format not in ("csv", "json"):
        return jsonify({"ok": False, "error": "Invalid format"}), 400

    records = get_telemetry_export_records(
        data_type=data_type,
        range_minutes=range_minutes,
        start_ts=start_ts,
        end_ts=end_ts,
        series=series,
        node_id=node_id
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
        allowed = [120, 300, 600, 900, 1800]
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
                "hw_model": node.get("hw_model", ""),
                # Keep coordinates in backups/exports as durable node data.
                "position": node.get("position")
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
                "favorite": old.get("favorite", False),
                # Importing metadata must not discard a stored position.
                "position": node_data.get("position", old.get("position"))
            }
            ensure_chat(node_id, name, force=True)
            imported_count += 1
        save_nodes()
        save_chats()
    return jsonify({"ok": True, "imported_count": imported_count})


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
            deleted_file = DELETED_DM_FILE
            try:
                with open(deleted_file, "w") as f:
                    json.dump({"deleted": dm_chat_ids}, f)
            except Exception as e:
                print(f"[WARN] Could not write deleted_dm.json: {e}")
            messages = [
                m for m in messages
                if m.get("chat_id") == CHANNEL_CHAT_ID or str(m.get("chat_id", "")).startswith("channel:")
            ]
            save_chats()
            save_messages()
        return jsonify({"ok": True, "deleted_count": deleted_count, "message": f"Deleted {deleted_count} DM chats"})
    except Exception as e:
        print(f"[ERROR] Delete all DM: {e}")
        return jsonify({"ok": False, "error": str(e), "error_code": "generic"}), 500

@app.route("/api/restore_deleted_dm", methods=["POST"])
@handle_errors
def api_restore_deleted_dm():
    deleted_file = DELETED_DM_FILE
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
    # Verify the physical radio before loading or mutating radio-profile data.
    startup_info_output = verify_radio_identity()
    identity_status = RADIO_IDENTITY_RESULT.get("status", "NOT_CHECKED")
    identity_match = identity_status == "MATCH"

    # Load the accepted profile regardless of radio availability so history
    # remains visible.  A mismatched radio is never allowed to write into it.
    load_messages()
    reconcile_interrupted_sends()
    load_nodes()
    load_sensors_data()
    load_chats()
    ensure_known_nodes()
    normalize_unknown_nodes()
    if identity_match:
        parse_nodes_from_info(startup_info_output)
    else:
        print(
            f"[PROFILE] Radio writes blocked for profile {ACTIVE_PROFILE_ID}: identity={identity_status}",
            flush=True,
        )
    load_settings()
    # weather_manager's providers were constructed before settings.json was
    # loaded (they're module-level singletons, built long before this
    # __main__ block runs), so sync the active provider and its language now
    # in case a choice other than the config.py defaults was saved in a
    # previous run.
    weather_manager.set_active(
        normalize_settings(settings).get("weather", {}).get("provider", "openweather")
    )
    weather_manager.active().set_language(resolve_weather_language(settings.get("language", "auto")))
    _load_cpu_history()

    if identity_match:
        try:
            update_base_status_from_info(startup_info_output)
        except Exception as e:
            print(f"[WARN] Base status update failed: {e}")
    
    telemetry.load_telemetry()
    camera.load_camera_settings()    # <--- вызов через модуль
    
    for node_id in KNOWN_NODES:
        if node_id not in chats:
            ensure_chat(node_id, KNOWN_NODES[node_id], force=True)
    save_chats()
    
    if identity_match:
        try:
            print("[INIT] Initial telemetry fetch...")
            get_telemetry_from_info(startup_info_output)
        except Exception as e:
            print(f"[INIT] Telemetry fetch error: {e}")
    
    # Инициализация камеры
    #
    # CUTOVER TODO (see camera/camera_manager.py and the project's
    # usb-camera-plan notes): this unconditional camera.init_camera() call
    # is the CSI-only startup path that predates camera_manager.py. Once
    # /video_feed and api_camera.py are switched to go through
    # camera_manager instead of the `camera` module directly, this call
    # must be REMOVED (not just left alongside build_camera_manager()) -
    # build_camera_manager() already calls CsiCameraDriver.detect(), which
    # itself calls camera.init_camera() internally. Calling init_camera()
    # here AND again via camera_manager would open a second Picamera2()
    # while the first is still live - confirmed live on camtest that even
    # calling it once already races real USB cameras there for the device
    # (see the module docstring in camera_manager.py's build_camera_manager()
    # for why - libcamera's uvcvideo pipeline handler makes Picamera2 claim
    # some USB webcams too, not just genuine CSI sensors). Replace this
    # block with `camera_manager = build_camera_manager(persisted_active_id=...)`
    # at cutover time, sourcing persisted_active_id from devices.json.
    print("[CAMERA] 🔍 Initializing...", flush=True)
    camera.init_camera()   # <--- вызов через модуль

    # Keep devices.json in sync with whatever sensor Picamera2 actually
    # found - it used to only ever hold "" or whatever model was recorded
    # the first time the file was created, so swapping the physical camera
    # module (e.g. imx219 -> ov5647) left a stale value on disk even though
    # /api/devices was already showing the live-detected one.
    detected_camera_model = str(getattr(camera, "CAMERA_MODEL", "") or "").strip()
    if detected_camera_model:
        try:
            devices_data = device_manager.load_or_create()
            # devices.json schema v2: "cameras" is keyed by CameraDriver id
            # (see camera/camera_manager.py) - this startup path only knows
            # about the CSI camera directly, so it writes the "csi" entry.
            cameras = devices_data.setdefault("devices", {}).setdefault("cameras", {})
            stored_camera = cameras.setdefault("csi", {})
            if stored_camera.get("model") != detected_camera_model:
                stored_camera["model"] = detected_camera_model
                device_manager.save(devices_data)
                print(
                    f"[CAMERA] Recorded detected model in devices.json: {detected_camera_model}",
                    flush=True,
                )
        except Exception as error:
            print(f"[CAMERA] Failed to persist detected model: {error}", flush=True)

    # Start radio workers only for the accepted physical radio.  This prevents
    # another USB node from contaminating the active profile.
    if identity_match:
        threading.Thread(target=listen_meshtastic, daemon=True).start()
        threading.Thread(target=cleanup_seen_ids, daemon=True).start()
        threading.Thread(target=telemetry_worker, daemon=True).start()
        threading.Thread(target=telemetry_buffer_worker, daemon=True).start()
        threading.Thread(target=radio_health_worker, daemon=True).start()
        threading.Thread(target=ack_timeout_worker, daemon=True).start()
    else:
        pause_listen.set()
        print(f"[IDENTITY] Listener not started because status={identity_status}", flush=True)
    threading.Thread(target=cpu_history_worker, daemon=True).start()
    
    identity_status = RADIO_IDENTITY_RESULT.get("status", "NOT_CHECKED")
    detected_radio = RADIO_IDENTITY_RESULT.get("detected") or {}
    banner_radio_name = detected_radio.get("long_name") or LOCAL_NODE_NAME
    banner_instance_name = INSTANCE_IDENTITY.get("instance_name", "MeshCenter")
    banner_hostname = INSTANCE_IDENTITY.get("hostname", "")
    print(
        "\n"
        f"    {banner_instance_name}\n"
        f"    Host: {banner_hostname}\n"
        f"    URL: http://{APP_HOST}:{APP_PORT}\n"
        f"    Radio: {banner_radio_name}\n"
        f"    Port: {MESHTASTIC_PORT}\n"
        f"    Identity: {identity_status}\n"
        f"    Camera: {'Available' if camera.CAMERA_AVAILABLE else 'Unavailable'}\n"
        f"    Video: {camera.VIDEO_CONFIG['resolution']} @ {camera.VIDEO_CONFIG['fps']}fps {camera.VIDEO_CONFIG['quality']}%\n"
        f"    Photo: {camera.PHOTO_CONFIG['resolution']} preview, {camera.PHOTO_SAVE_CONFIG['resolution']} save\n"
    )

    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)

