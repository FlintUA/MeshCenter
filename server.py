from flask import Flask, request, jsonify, render_template
import subprocess
import threading
import time
import re
import json
import os
from collections import defaultdict

# ===== КОНФИГУРАЦИЯ =====
APP_HOST = "0.0.0.0"
APP_PORT = 5000
MESHTASTIC_CMD = "/home/flint/.local/bin/meshtastic"

LOCAL_NODE_ID = "!067a40fa"
LOCAL_NODE_NAME = "Flint Base"

# Создаем папку для данных
DATA_DIR = "/home/flint/mesh_web/data"

HISTORY_FILE = os.path.join(DATA_DIR, "messages.json")
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
SENSORS_FILE = os.path.join(DATA_DIR, "sensors.json")
CHATS_FILE = os.path.join(DATA_DIR, "chats.json")

MAX_HISTORY_MESSAGES = 1000

CHANNEL_CHAT_ID = "channel"
CHANNEL_CHAT_NAME = "LongFast Channel 0"

# ===== ИЗВЕСТНЫЕ УЗЛЫ =====
KNOWN_NODES = {
    "!067a40fa": "Flint Base",
    "!b0f14d2a": "Flint_Echo",
    "!756f9960": "Flint TAP2",
    "!1fa065f0": "Elektroniker",
    "!1300faf0": "Orion9 mobil",
    "!0e8b3cf6": "StS_Erl_fix",
    "!51fbf9c": "Uttenreuth-MGS13-B",
    "!1paa51c": "Meshtastic a51c",
    "!0a809218": "RetroMobil",
    "!9ea0c0fc": "BirgitsPaperMesh",
    "!7e9f4f33": "Meshstatic 4f33",
    "!19ee6b8fc": "Erlangen WOK1",
    "!f68f9e94": "ThinkNode M5",
    "!04c67058": "HardTekkER",
    "!f6cd2588": "Meshtastic 2588",
    "!1dd2a0bc": "daa792-a0bc",
}

KNOWN_NODE_INFO = {
    "!067a40fa": {"short_name": "FLTB", "hw_model": "RAK4631"},
    "!b0f14d2a": {"short_name": "FLIE", "hw_model": "T-Echo Plus"},
    "!756f9960": {"short_name": "FLT2", "hw_model": "RAK3312"},
    "!1fa065f0": {"short_name": "Elek", "hw_model": "TBEAM"},
    "!1300faf0": {"short_name": "ori9", "hw_model": "T_DECK"},
    "!0e8b3cf6": {"short_name": "3cf6", "hw_model": "RAK4631"},
    "!51fbf9c": {"short_name": "AR76", "hw_model": "TLORA_V2_1_1P6"},
    "!1paa51c": {"short_name": "a51c", "hw_model": "UNSET"},
    "!0a809218": {"short_name": "RKM", "hw_model": "TLORA_T3_S3"},
    "!9ea0c0fc": {"short_name": "BPM", "hw_model": "HELTEC_WIRELESS_PAPER"},
    "!7e9f4f33": {"short_name": "4f33", "hw_model": "RAK4631"},
    "!19ee6b8fc": {"short_name": "WOK1", "hw_model": "HELTEC_V3"},
    "!f68f9e94": {"short_name": "AB4", "hw_model": "THINKNODE_M5"},
    "!04c67058": {"short_name": "TeKK", "hw_model": "HELTEC_V4"},
    "!f6cd2588": {"short_name": "2588", "hw_model": "HELTEC_V4"},
    "!1dd2a0bc": {"short_name": "a0bc", "hw_model": "SEEED_XIAO_S3"},
}

# ===== ИНИЦИАЛИЗАЦИЯ FLASK =====
app = Flask(__name__)

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====
messages = []
seen_ids = set()
seen_recent_texts = {}
nodes = {}
chats = {}

sensor_data = {
    "temperature": None,
    "humidity": None,
    "pressure": None,
    "voltage": None,
    "current": None,
    "power": None,
    "battery_percent": None,
    "air_quality": None,
    "last_update": None
}

base_status = {
    "battery_level": None,
    "real_battery": None,
    "voltage": None,
    "channel_utilization": None,
    "air_util_tx": None,
    "uptime_seconds": None,
    "last_update": None
}

listen_process = None
radio_lock = threading.Lock()
pause_listen = threading.Event()
current_active_chat = CHANNEL_CHAT_ID

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def now():
    return time.strftime("%H:%M:%S")

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
    if not node_id:
        return None
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
    if not node_id:
        return None
    return normalize_node_id(node_id)

def is_valid_node_id(node_id):
    if not node_id:
        return False
    if node_id == CHANNEL_CHAT_ID:
        return True
    return node_id.startswith("!") and len(node_id) >= 5

def sanitize_text(text):
    if not text:
        return ""
    if len(text) > 500:
        text = text[:500]
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

# ===== РАБОТА С ФАЙЛАМИ =====
def save_messages():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(messages[-MAX_HISTORY_MESSAGES:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("History save error:", e)

def load_messages():
    global messages
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            messages = json.load(f)
            messages[:] = messages[-MAX_HISTORY_MESSAGES:]
    except Exception as e:
        print("History load error:", e)
        messages = []

def save_chats():
    try:
        with open(CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(chats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Chats save error:", e)

def load_chats():
    global chats
    if not os.path.exists(CHATS_FILE):
        chats = {CHANNEL_CHAT_ID: {"id": CHANNEL_CHAT_ID, "name": CHANNEL_CHAT_NAME, "type": "channel", "last_message": "", "last_time": "", "unread": 0}}
        save_chats()
        return
    try:
        with open(CHATS_FILE, "r", encoding="utf-8") as f:
            content = f.read()
            if not content.strip():
                raise ValueError("Empty file")
            chats = json.loads(content)
            if CHANNEL_CHAT_ID not in chats:
                chats[CHANNEL_CHAT_ID] = {"id": CHANNEL_CHAT_ID, "name": CHANNEL_CHAT_NAME, "type": "channel", "last_message": "", "last_time": "", "unread": 0}
                save_chats()
    except (json.JSONDecodeError, ValueError, Exception) as e:
        print(f"Chats load error: {e}, creating new")
        chats = {CHANNEL_CHAT_ID: {"id": CHANNEL_CHAT_ID, "name": CHANNEL_CHAT_NAME, "type": "channel", "last_message": "", "last_time": "", "unread": 0}}
        save_chats()

def save_nodes():
    try:
        with open(NODES_FILE, "w", encoding="utf-8") as f:
            json.dump(nodes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Nodes save error:", e)

def load_nodes():
    global nodes
    if not os.path.exists(NODES_FILE):
        return
    try:
        with open(NODES_FILE, "r", encoding="utf-8") as f:
            nodes = json.load(f)
    except Exception as e:
        print("Nodes load error:", e)
        nodes = {}

def save_sensors():
    try:
        with open(SENSORS_FILE, "w", encoding="utf-8") as f:
            json.dump(sensor_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Sensors save error:", e)

def load_sensors_data():
    global sensor_data
    if not os.path.exists(SENSORS_FILE):
        return
    try:
        with open(SENSORS_FILE, "r", encoding="utf-8") as f:
            sensor_data = json.load(f)
    except Exception as e:
        print("Sensors load error:", e)

def ensure_chat(node_id, node_name=None):
    if node_id == CHANNEL_CHAT_ID or not node_id or not node_id.startswith("!"):
        return
    if node_id not in chats:
        name = node_name or get_node_name(node_id)
        chats[node_id] = {"id": node_id, "name": name, "type": "dm", "last_message": "", "last_time": "", "unread": 0}
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

# ===== УПРАВЛЕНИЕ НОДАМИ =====
def deduplicate_nodes():
    global nodes
    changed = False
    unique_nodes = {}
    duplicates = []
    
    for node_id, node in nodes.items():
        node_name = node.get("name", "")
        if not node_name:
            continue
        found = False
        for existing_id, existing_node in unique_nodes.items():
            if existing_node.get("name") == node_name:
                if node.get("last_seen", 0) > existing_node.get("last_seen", 0):
                    unique_nodes[existing_id] = node
                    duplicates.append(existing_id)
                else:
                    duplicates.append(node_id)
                found = True
                break
        if not found:
            unique_nodes[node_id] = node
    
    for dup_id in duplicates:
        if dup_id in nodes:
            if dup_id in chats:
                del chats[dup_id]
            del nodes[dup_id]
            changed = True
    
    if changed:
        save_nodes()
        save_chats()
        print(f"Deduplicated nodes: removed {len(duplicates)} duplicates")
    return changed

def ensure_known_nodes():
    for node_id, name in KNOWN_NODES.items():
        old = nodes.get(node_id, {})
        info = get_node_info(node_id)
        nodes[node_id] = {
            "name": name,
            "node_id": node_id,
            "last_seen": old.get("last_seen", 0),
            "last_time": old.get("last_time", "never"),
            "rssi": old.get("rssi"),
            "snr": old.get("snr"),
            "hop_start": old.get("hop_start", ""),
            "relay_node": old.get("relay_node", ""),
            "last_text": old.get("last_text", ""),
            "short_name": info.get("short_name", old.get("short_name", "")),
            "hw_model": info.get("hw_model", old.get("hw_model", "")),
            "role": old.get("role", "CLIENT"),
            "ignored": old.get("ignored", False),
            "favorite": old.get("favorite", False)
        }
        ensure_chat(node_id, name)
    deduplicate_nodes()
    save_nodes()

def normalize_unknown_nodes():
    global nodes
    changed = False
    deduplicate_nodes()
    
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
            ensure_chat(node_id, node.get("name"))
    
    if changed:
        save_nodes()

# ===== ПАРСИНГ СООБЩЕНИЙ =====
def extract_node_id(line):
    patterns = [
        r"'fromId':\s*'([^']+)'",
        r'"fromId":\s*"([^"]+)"',
        r"'id':\s*'(![0-9a-fA-F]+)'",
        r'"id":\s*"(![0-9a-fA-F]+)"',
        r'\bid:\s*"(![0-9a-fA-F]+)"',
        r'\bid:\s*(![0-9a-fA-F]+)',
        r"'from':\s*'([^']*)'",
        r'"from":\s*"([^"]*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, line)
        if m:
            node_id = m.group(1)
            if not node_id:
                continue
            if node_id.isdigit():
                return normalize_node_id_with_aliases(node_num_to_id(node_id))
            if node_id.startswith("!"):
                return normalize_node_id_with_aliases(node_id)
            if re.match(r'^[0-9a-fA-F]{8}$', node_id):
                return "!" + node_id
    m = re.search(r"'from':\s*(\d+)", line)
    if m:
        return normalize_node_id_with_aliases(node_num_to_id(m.group(1)))
    m = re.search(r'\bfrom:\s*(\d+)', line)
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
    if not sender:
        return ""
    if sender.startswith("!"):
        return sender
    for node_id, name in KNOWN_NODES.items():
        if sender == name:
            return node_id
    for node_id, node in nodes.items():
        if sender == node.get("name"):
            return node_id
    return ""

def extract_field(line, names):
    for name in names:
        patterns = [
            rf"'{name}':\s*'([^']*)'",
            rf'"{name}":\s*"([^"]*)"',
            rf"\b{name}:\s*\"([^\"]*)\"",
            rf"\b{name}:\s*'([^']*)'",
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
        return m.group(1)
    m = re.search(r"\bid:\s*(\d+)", line)
    if m:
        return m.group(1)
    return None

def extract_text_message(line):
    if "TEXT_MESSAGE_APP" not in line and "'text':" not in line and '"text":' not in line:
        return None
    patterns = [
        r"'text':\s*'([^']*)'",
        r'"text":\s*"([^"]*)"',
        r"'text':\s*\"([^\"]*)\"",
        r'"text":\s*\'([^\']*)\'',
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
    old = nodes.get(node_id, {})
    
    nodes[node_id] = {
        "name": name,
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
    
    if node_id.startswith("!"):
        ensure_chat(node_id, name)
    save_nodes()
    return node_id

def process_nodeinfo(block):
    if ("NODEINFO_APP" not in block and "longName" not in block and "long_name" not in block and
        "shortName" not in block and "short_name" not in block and "hwModel" not in block and "hw_model" not in block):
        return False
    
    node_id = extract_node_id(block)
    if not node_id:
        return False
    
    long_name = extract_field(block, ["longName", "long_name", "longname"])
    short_name = extract_field(block, ["shortName", "short_name", "shortname"])
    hw_model = extract_field(block, ["hwModel", "hw_model"])
    role = extract_field(block, ["role", "Role"])
    rssi = extract_rssi(block)
    snr = extract_snr(block)
    hop_start = extract_hop_start(block)
    relay_node = extract_relay_node(block)
    
    name = KNOWN_NODES.get(node_id) or long_name or short_name or friendly_unknown_node_name(node_id)
    old = nodes.get(node_id, {})
    info = get_node_info(node_id)
    
    nodes[node_id] = {
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
        "favorite": old.get("favorite", False)
    }
    
    if node_id.startswith("!"):
        ensure_chat(node_id, name)
    save_nodes()
    return True

def add_message(kind, sender, text, node_id="", chat_id=None, chat_name=None):
    global current_active_chat
    
    if not node_id:
        node_id = infer_node_id_from_sender(sender)
    
    if node_id and node_id.startswith("!") and node_id != LOCAL_NODE_ID:
        if node_id not in KNOWN_NODES:
            KNOWN_NODES[node_id] = sender
            print(f"[AUTO] Added new node: {node_id} -> {sender}")
        if node_id not in chats:
            ensure_chat(node_id, sender or get_node_name(node_id))
    
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
        ensure_chat(chat_id, chat_name)
    
    msg = {
        "kind": kind,
        "sender": sender,
        "node_id": node_id,
        "text": text,
        "time": now(),
        "chat_id": chat_id,
        "chat_type": chat_type,
        "chat_name": chat_name
    }
    messages.append(msg)
    messages[:] = messages[-MAX_HISTORY_MESSAGES:]
    
    update_chat_last_message(chat_id, text, msg["time"])
    
    if kind == "rx" and chat_id in chats and chat_id != current_active_chat:
        chats[chat_id]["unread"] = chats[chat_id].get("unread", 0) + 1
        save_chats()
    
    save_messages()
    return msg

def is_duplicate_text(sender, text):
    cleaned_text = text.strip()
    if not cleaned_text:
        return True
    current_time = time.time()
    old_keys = [k for k, ts in seen_recent_texts.items() if current_time - ts > 60]
    for key in old_keys:
        del seen_recent_texts[key]
    old_time = seen_recent_texts.get(cleaned_text)
    if old_time and current_time - old_time < 15:
        return True
    seen_recent_texts[cleaned_text] = current_time
    return False

# ===== СТАТУСЫ И МЕТРИКИ =====
def node_status_icon(last_seen):
    if not last_seen:
        return "⚪"
    age = time.time() - last_seen
    if age < 120:
        return "🟢"
    if age < 900:
        return "🟡"
    return "🔴"

def age_text(last_seen):
    if not last_seen:
        return "not heard yet"
    age = int(time.time() - last_seen)
    if age < 60:
        return f"seen {age} sec ago"
    if age < 3600:
        return f"seen {age // 60} min ago"
    if age < 86400:
        return f"seen {age // 3600} h ago"
    return f"seen {age // 86400} d ago"

def signal_quality(rssi):
    if rssi is None or rssi == "":
        return ""
    try:
        value = int(float(rssi))
    except ValueError:
        return ""
    if value >= -90:
        return "good"
    if value >= -105:
        return "medium"
    return "weak"

def get_nodes_list():
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
        if quality:
            meta_parts.append("signal: " + quality)
        if rssi:
            meta_parts.append("RSSI: " + str(rssi) + " dBm")
        if snr:
            meta_parts.append("SNR: " + str(snr) + " dB")
        if hop_start:
            meta_parts.append("hops: " + str(hop_start))
        if relay_node:
            meta_parts.append("relay: " + str(relay_node))
        if short_name:
            meta_parts.append("short: " + str(short_name))
        if hw_model:
            meta_parts.append("hw: " + str(hw_model))
        if role:
            meta_parts.append("role: " + str(role))
        if ignored:
            meta_parts.append("🚫 ignored")
        if favorite:
            meta_parts.append("⭐ favorite")
            
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
            "favorite": favorite
        })
    return result

def get_chats_list():
    chat_list = []
    total_unread = 0
    
    for chat_id, chat in chats.items():
        if chat_id.startswith("!") and nodes.get(chat_id, {}).get("ignored", False):
            continue
        
        is_favorite = nodes.get(chat_id, {}).get("favorite", False) if chat_id.startswith("!") else False
        unread = chat.get("unread", 0)
        total_unread += unread
        
        chat_list.append({
            "id": chat_id,
            "name": chat.get("name", chat_id),
            "type": chat.get("type", "dm"),
            "last_message": chat.get("last_message", ""),
            "last_time": chat.get("last_time", ""),
            "unread": unread,
            "is_channel": chat_id == CHANNEL_CHAT_ID,
            "ignored": chat_id.startswith("!") and nodes.get(chat_id, {}).get("ignored", False),
            "favorite": is_favorite
        })
    
    def sort_key(c):
        if c["is_channel"]:
            return (0, "", "")
        if c["favorite"]:
            return (1, "", c["last_time"] or "")
        if c["unread"] > 0:
            return (2, "", c["last_time"] or "")
        return (3, "", c["last_time"] or "")
    
    chat_list.sort(key=sort_key)
    return chat_list, total_unread

def get_chat_messages(chat_id):
    return [m for m in messages if m.get("chat_id") == chat_id]

# ===== УПРАВЛЕНИЕ ПРОЦЕССАМИ =====
def stop_listener():
    global listen_process
    if listen_process is not None:
        try:
            listen_process.terminate()
            for _ in range(30):
                if listen_process.poll() is not None:
                    break
                time.sleep(0.1)
            
            if listen_process.poll() is None:
                print("[WARN] Listener didn't stop gracefully, killing...")
                listen_process.kill()
                time.sleep(0.5)
            
            print(f"[DEBUG] Listener stopped (exit code: {listen_process.returncode})")
            listen_process = None
        except Exception as e:
            print(f"[WARN] Error stopping listener: {e}")
            listen_process = None

def update_base_status_from_info():
    global base_status
    try:
        result = subprocess.run([MESHTASTIC_CMD, "--info"], capture_output=True, text=True, timeout=15)
        output = result.stdout + result.stderr
        node_pos = output.find(f'"{LOCAL_NODE_ID}"')
        if node_pos < 0:
            return
        next_node_pos = output.find('\n  "!', node_pos + 1)
        node_block = output[node_pos:next_node_pos] if next_node_pos >= 0 else output[node_pos:]
        
        metrics_pos = node_block.find('"deviceMetrics"')
        if metrics_pos < 0:
            return
        block_start = node_block.find("{", metrics_pos)
        block_end = node_block.find("}", block_start)
        if block_start < 0 or block_end < 0:
            return
        
        metrics = json.loads(node_block[block_start:block_end + 1])
        voltage = metrics.get("voltage")
        base_status = {
            "battery_level": metrics.get("batteryLevel"),
            "real_battery": voltage_to_percent(voltage),
            "voltage": voltage,
            "channel_utilization": metrics.get("channelUtilization"),
            "air_util_tx": metrics.get("airUtilTx"),
            "uptime_seconds": metrics.get("uptimeSeconds"),
            "last_update": now()
        }
    except Exception as e:
        print("Base status update error:", e)

def read_sensors_from_meshtastic():
    global sensor_data
    try:
        result = subprocess.run([MESHTASTIC_CMD, "--get", "telemetry"], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        
        temp_match = re.search(r'(?:temperature|temp)[:\s=]+(-?\d+\.?\d*)', output, re.IGNORECASE)
        hum_match = re.search(r'(?:humidity|hum)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        press_match = re.search(r'(?:pressure)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        volt_match = re.search(r'(?:voltage|batteryVoltage)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        curr_match = re.search(r'(?:current)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        batt_match = re.search(r'(?:battery)[:\s=]+(\d+\.?\d*)%?', output, re.IGNORECASE)
        
        if temp_match:
            sensor_data["temperature"] = float(temp_match.group(1))
        if hum_match:
            sensor_data["humidity"] = float(hum_match.group(1))
        if press_match:
            sensor_data["pressure"] = float(press_match.group(1))
        if volt_match:
            sensor_data["voltage"] = float(volt_match.group(1))
        if curr_match:
            sensor_data["current"] = float(curr_match.group(1))
        if batt_match:
            sensor_data["battery_percent"] = float(batt_match.group(1))
        if sensor_data["voltage"] is not None and sensor_data["current"] is not None:
            sensor_data["power"] = sensor_data["voltage"] * sensor_data["current"]
        if any([sensor_data["temperature"], sensor_data["humidity"], sensor_data["pressure"], 
                sensor_data["voltage"], sensor_data["current"]]):
            sensor_data["last_update"] = now()
            save_sensors()
    except Exception as e:
        print(f"Error reading sensors: {e}")

# ===== ФОНОВЫЕ ПОТОКИ =====
def base_status_worker():
    while True:
        update_base_status_from_info()
        time.sleep(120)

def sensor_reader_worker():
    while True:
        read_sensors_from_meshtastic()
        time.sleep(10)

def cleanup_seen_ids():
    global seen_ids, seen_recent_texts
    while True:
        time.sleep(300)
        if len(seen_ids) > 1000:
            seen_ids = set(list(seen_ids)[-500:])
            print(f"[DEBUG] Cleaned seen_ids, new size: {len(seen_ids)}")
        
        current_time = time.time()
        old_keys = [k for k, ts in seen_recent_texts.items() if current_time - ts > 60]
        for key in old_keys:
            del seen_recent_texts[key]
        if old_keys:
            print(f"[DEBUG] Cleaned {len(old_keys)} old texts")

def listen_meshtastic():
    global listen_process, current_active_chat
    nodeinfo_buffer = []
    collecting_nodeinfo = False
    consecutive_errors = 0
    
    while True:
        if pause_listen.is_set():
            time.sleep(0.5)
            continue
            
        try:
            time.sleep(0.5)
            with radio_lock:
                if pause_listen.is_set():
                    continue
                print("[DEBUG] Starting listener...")
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
                
                if "TEXT_MESSAGE_APP" in line or "'text':" in line or '"text":' in line:
                    print(f"[RAW] {line[:200]}...")
                
                if "NODEINFO_APP" in line or collecting_nodeinfo:
                    collecting_nodeinfo = True
                    nodeinfo_buffer.append(line)
                    block = "\n".join(nodeinfo_buffer)
                    if ("fromId" in block and ("longName" in block or "long_name" in block or
                        "shortName" in block or "short_name" in block or "hwModel" in block or "hw_model" in block)):
                        process_nodeinfo(block)
                        nodeinfo_buffer = []
                        collecting_nodeinfo = False
                        continue
                    if len(nodeinfo_buffer) > 80:
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
                if is_duplicate_text(sender, text):
                    continue
                    
                node_id = update_node(line, sender, text)
                if node_id and nodes.get(node_id, {}).get("ignored", False):
                    continue
                
                # ========== ОПРЕДЕЛЯЕМ ТИП СООБЩЕНИЯ ==========
                chat_id = CHANNEL_CHAT_ID
                is_channel = False
                
                # 1. Проверяем broadcast (канал)
                if ("'to': 4294967295" in line or '"to": 4294967295' in line or
                    "'to': '^all'" in line or '"to": "^all"' in line or
                    "'toId': '^all'" in line or '"toId": "^all"' in line or
                    'broadcast' in line.lower()):
                    is_channel = True
                    print(f"[DEBUG] CHANNEL (broadcast): from={sender}, text={text[:30]}")
                
                # 2. Проверяем наличие 'dest' - это всегда DM
                elif "'dest'" in line.lower() or '"dest"' in line.lower():
                    is_channel = False
                    print(f"[DEBUG] DM (dest field): from={sender}, text={text[:30]}")
                
                # 3. Проверяем 'to' с конкретным ID - это DM
                elif ("'to': '!" in line or '"to": "!"' in line):
                    is_channel = False
                    print(f"[DEBUG] DM (to with !): from={sender}, text={text[:30]}")
                
                # 4. Если есть 'to' с числовым ID, но не 4294967295 - это DM
                elif re.search(r"'to':\s*[0-9]+,", line) or re.search(r'"to":\s*[0-9]+,', line):
                    if "4294967295" not in line:
                        is_channel = False
                        print(f"[DEBUG] DM (numeric to): from={sender}, text={text[:30]}")
                
                # 5. Если ничего не определили - считаем каналом
                else:
                    is_channel = True
                    print(f"[DEBUG] CHANNEL (default): from={sender}, text={text[:30]}")
                
                # Определяем chat_id
                if is_channel:
                    chat_id = CHANNEL_CHAT_ID
                else:
                    # Для DM используем node_id отправителя
                    if node_id and node_id.startswith("!") and node_id != LOCAL_NODE_ID:
                        chat_id = node_id
                    else:
                        # Если node_id не определен, пытаемся извлечь из 'from'
                        from_match = re.search(r"'from':\s*'(![0-9a-f]+)'", line)
                        if not from_match:
                            from_match = re.search(r'"from":\s*"(![0-9a-f]+)"', line)
                        if from_match:
                            chat_id = from_match.group(1)
                        else:
                            chat_id = CHANNEL_CHAT_ID
                
                # Создаем чат если нужно
                if chat_id.startswith("!") and chat_id != LOCAL_NODE_ID:
                    ensure_chat(chat_id, sender)
                
                print(f"[DEBUG] FINAL: chat_id={chat_id}, is_channel={is_channel}")
                add_message("rx", sender, text, node_id, chat_id)
                
        except Exception as e:
            consecutive_errors += 1
            print(f"[ERROR] listen_meshtastic (attempt {consecutive_errors}): {e}")
            add_message("rx", "SYSTEM ERROR", f"listen: {str(e)}", "", CHANNEL_CHAT_ID)
            if consecutive_errors > 5:
                print("[FATAL] Too many errors, restarting listener thread")
                consecutive_errors = 0
                time.sleep(5)
                continue
                
        if pause_listen.is_set():
            time.sleep(0.5)
            continue
            
        if listen_process and listen_process.poll() is not None:
            print("[WARN] Listener process died, restarting...")
            listen_process = None
            time.sleep(2)
            continue
            
        time.sleep(2)

# ===== API МАРШРУТЫ =====
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/chats")
def api_chats():
    chat_list, total_unread = get_chats_list()
    return jsonify({"chats": chat_list, "total_unread": total_unread})

@app.route("/api/messages")
def api_messages():
    global current_active_chat
    chat_id = request.args.get("chat_id", "").strip()
    
    if chat_id and not is_valid_node_id(chat_id):
        return jsonify({"ok": False, "error": "Invalid chat_id"}), 400
    
    if chat_id:
        chat_messages = get_chat_messages(chat_id)
        if chat_id.startswith("!") and nodes.get(chat_id, {}).get("ignored", False):
            chat_messages = [m for m in chat_messages if m.get("kind") == "me" or "SYSTEM" in m.get("sender", "")]
        
        if chat_id in chats:
            chats[chat_id]["unread"] = 0
            save_chats()
        
        current_active_chat = chat_id
        return jsonify({
            "chat_id": chat_id,
            "messages": chat_messages,
            "chat_info": chats.get(chat_id, {})
        })
    else:
        return jsonify({"messages": messages, "nodes": get_nodes_list()})

@app.route("/api/sensors")
def api_sensors():
    return jsonify(sensor_data)

@app.route("/api/base_status")
def api_base_status():
    return jsonify(base_status)

@app.route("/api/node_status")
def api_node_status():
    node_id = request.args.get("node_id", "").strip()
    if not node_id or not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node_id"}), 400
    node = nodes.get(node_id, {})
    return jsonify({
        "ok": True,
        "node_id": node_id,
        "ignored": node.get("ignored", False),
        "favorite": node.get("favorite", False),
        "name": node.get("name", "Unknown")
    })

@app.route("/api/toggle_ignore", methods=["POST"])
def api_toggle_ignore():
    data = request.get_json(force=True)
    node_id = data.get("node_id", "").strip()
    if not node_id or node_id not in nodes or not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node"}), 400
    nodes[node_id]["ignored"] = not nodes[node_id].get("ignored", False)
    save_nodes()
    return jsonify({"ok": True, "ignored": nodes[node_id]["ignored"]})

@app.route("/api/toggle_favorite", methods=["POST"])
def api_toggle_favorite():
    data = request.get_json(force=True)
    node_id = data.get("node_id", "").strip()
    if not node_id or node_id not in nodes or not is_valid_node_id(node_id):
        return jsonify({"ok": False, "error": "Invalid node"}), 400
    nodes[node_id]["favorite"] = not nodes[node_id].get("favorite", False)
    save_nodes()
    return jsonify({"ok": True, "favorite": nodes[node_id]["favorite"]})

@app.route("/api/cleanup_nodes", methods=["POST"])
def api_cleanup_nodes():
    try:
        deduplicate_nodes()
        for node_id, node in nodes.items():
            if node_id.startswith("!") and node_id not in chats:
                ensure_chat(node_id, node.get("name"))
        return jsonify({"ok": True, "message": "Nodes cleaned up", "node_count": len(nodes)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/clear_chat", methods=["POST"])
def api_clear_chat():
    data = request.get_json(force=True)
    chat_id = data.get("chat_id", "").strip()
    if not chat_id or not is_valid_node_id(chat_id):
        return jsonify({"ok": False, "error": "Invalid chat_id"}), 400
    
    global messages
    messages = [m for m in messages if m.get("chat_id") != chat_id]
    save_messages()
    if chat_id in chats:
        chats[chat_id]["last_message"] = ""
        chats[chat_id]["last_time"] = ""
        chats[chat_id]["unread"] = 0
        save_chats()
    return jsonify({"ok": True})

@app.route("/api/send", methods=["POST"])
def api_send():
    global current_active_chat
    
    data = request.get_json(force=True)
    text = sanitize_text(data.get("text", "").strip())
    target_node = data.get("target_node", "")
    chat_id = data.get("chat_id", "")
    
    if not text:
        return jsonify({"ok": False, "error": "empty or invalid message"}), 400
    
    if chat_id and chat_id != CHANNEL_CHAT_ID and not is_valid_node_id(chat_id):
        return jsonify({"ok": False, "error": "Invalid chat_id"}), 400
    if target_node and not is_valid_node_id(target_node):
        return jsonify({"ok": False, "error": "Invalid target_node"}), 400
    if target_node and target_node not in nodes:
        return jsonify({"ok": False, "error": "Target node not found"}), 404
    
    pause_listen.set()
    time.sleep(0.5)
    
    with radio_lock:
        try:
            stop_listener()
            time.sleep(1.5)
            
            # Проверяем порт
            try:
                result = subprocess.run(["lsof", "/dev/ttyACM0"], capture_output=True, text=True, timeout=2)
                if result.stdout.strip():
                    print(f"[WARN] Port in use: {result.stdout.strip()}")
                    subprocess.run(["fuser", "-k", "/dev/ttyACM0"], capture_output=True, timeout=2)
                    time.sleep(1)
            except:
                pass
            
            cmd = [MESHTASTIC_CMD, "--ch-index", "0"]
            
            if chat_id and chat_id != CHANNEL_CHAT_ID and chat_id.startswith("!"):
                cmd.extend(["--dest", chat_id])
                receiver_name = get_node_name(chat_id)
                chat_name = receiver_name
                chat_type = "dm"
                final_chat_id = chat_id
            elif target_node and target_node.startswith("!"):
                cmd.extend(["--dest", target_node])
                receiver_name = get_node_name(target_node)
                chat_name = receiver_name
                chat_type = "dm"
                final_chat_id = target_node
            else:
                receiver_name = "Broadcast"
                chat_name = CHANNEL_CHAT_NAME
                chat_type = "channel"
                final_chat_id = CHANNEL_CHAT_ID
            
            cmd.extend(["--sendtext", text])
            print(f"[DEBUG] Sending: {' '.join(cmd)}")
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                if chat_type == "dm" and final_chat_id not in chats:
                    ensure_chat(final_chat_id, chat_name)
                
                sender_name = f"{LOCAL_NODE_NAME} → {receiver_name}" if chat_type == "dm" else LOCAL_NODE_NAME
                add_message("me", sender_name, text, LOCAL_NODE_ID, final_chat_id, chat_name)
                
                current_active_chat = final_chat_id
                if final_chat_id in chats:
                    reset_unread(final_chat_id)
                
                old = nodes.get(LOCAL_NODE_ID, {})
                info = get_node_info(LOCAL_NODE_ID)
                nodes[LOCAL_NODE_ID] = {
                    "name": LOCAL_NODE_NAME,
                    "node_id": LOCAL_NODE_ID,
                    "last_seen": time.time(),
                    "last_time": now(),
                    "rssi": old.get("rssi"),
                    "snr": old.get("snr"),
                    "hop_start": old.get("hop_start", ""),
                    "relay_node": old.get("relay_node", ""),
                    "last_text": f"sent to {receiver_name}: {text}" if chat_type == "dm" else f"sent: {text}",
                    "short_name": info.get("short_name", old.get("short_name", "")),
                    "hw_model": info.get("hw_model", old.get("hw_model", "")),
                    "role": old.get("role", "CLIENT_BASE"),
                    "ignored": old.get("ignored", False),
                    "favorite": old.get("favorite", False)
                }
                save_nodes()
                return jsonify({"ok": True, "chat_id": final_chat_id})
            
            err = result.stderr.strip() or result.stdout.strip() or "unknown send error"
            add_message("rx", "SYSTEM ERROR", f"send: {err}", "", CHANNEL_CHAT_ID)
            return jsonify({"ok": False, "error": err}), 500
            
        except subprocess.TimeoutExpired:
            add_message("rx", "SYSTEM ERROR", "send timeout", "", CHANNEL_CHAT_ID)
            return jsonify({"ok": False, "error": "timeout"}), 500
        except Exception as e:
            add_message("rx", "SYSTEM ERROR", f"send: {str(e)}", "", CHANNEL_CHAT_ID)
            return jsonify({"ok": False, "error": str(e)}), 500
        finally:
            time.sleep(1)
            pause_listen.clear()
            if listen_process is None:
                threading.Thread(target=listen_meshtastic, daemon=True).start()

@app.route("/api/delete_chat", methods=["POST"])
def api_delete_chat():
    global current_active_chat
    data = request.get_json(force=True)
    chat_id = data.get("chat_id", "").strip()
    
    if not chat_id or chat_id == CHANNEL_CHAT_ID or not is_valid_node_id(chat_id):
        return jsonify({"ok": False, "error": "Invalid chat"}), 400
    
    if chat_id in chats:
        del chats[chat_id]
        save_chats()
    
    global messages
    messages = [m for m in messages if m.get("chat_id") != chat_id]
    save_messages()
    
    if current_active_chat == chat_id:
        current_active_chat = CHANNEL_CHAT_ID
    
    return jsonify({"ok": True})

# ===== ЗАПУСК =====
if __name__ == "__main__":
    load_messages()
    load_nodes()
    load_sensors_data()
    load_chats()
    ensure_known_nodes()
    normalize_unknown_nodes()
    update_base_status_from_info()
    
    for node_id in KNOWN_NODES:
        if node_id not in chats:
            ensure_chat(node_id, KNOWN_NODES[node_id])
    save_chats()
    
    threading.Thread(target=sensor_reader_worker, daemon=True).start()
    threading.Thread(target=base_status_worker, daemon=True).start()
    threading.Thread(target=listen_meshtastic, daemon=True).start()
    threading.Thread(target=cleanup_seen_ids, daemon=True).start()
    
    print(f"""
    ╔══════════════════════════════════════════╗
    ║     Meshtastic Web Interface Started     ║
    ╠══════════════════════════════════════════╣
    ║  URL: http://{APP_HOST}:{APP_PORT}    ║
    ╚══════════════════════════════════════════╝
    """)
    
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)