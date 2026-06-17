from flask import Flask, request, jsonify, render_template
import subprocess
import threading
import time
import re
import json
import os

APP_HOST = "0.0.0.0"
APP_PORT = 5000
MESHTASTIC_CMD = "/home/flint/.local/bin/meshtastic"

LOCAL_NODE_ID = "!067a40fa"
LOCAL_NODE_NAME = "Flint Base"

HISTORY_FILE = "/home/flint/mesh_web/messages.json"
NODES_FILE = "/home/flint/mesh_web/nodes.json"
SENSORS_FILE = "/home/flint/mesh_web/sensors.json"
MAX_HISTORY_MESSAGES = 300

KNOWN_NODES = {
    "!1fa065f0": "Elektroniker",
    "!067a40fa": "Flint Base",
    "!756f9960": "Flint TAP2",
    "!1300faf0": "Orion9 mobil",
    "!f68f9e94": "ThinkNode M5",
    "!04c67058": "HardTekkER",
    "!f6cd2588": "Meshtastic 2588",
}

KNOWN_NODE_INFO = {
    "!1fa065f0": {"short_name": "Elek", "hw_model": "TBEAM"},
    "!067a40fa": {"short_name": "FLTB", "hw_model": "RAK4631"},
    "!756f9960": {"short_name": "FLT2", "hw_model": "RAK3312"},
    "!b0f14d2a": {"short_name": "FLTE", "hw_model": "T-Echo Plus"},
    "!1300faf0": {"short_name": "ori9", "hw_model": "T_DECK"},
    "!f68f9e94": {"short_name": "AB4", "hw_model": "THINKNODE_M5"},
    "!04c67058": {"short_name": "TeKK", "hw_model": "HELTEC_V4"},
    "!f6cd2588": {"short_name": "2588", "hw_model": "HELTEC_V4"},
}

app = Flask(__name__)

messages = []
seen_ids = set()
seen_recent_texts = {}
nodes = {}

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


def now():
    return time.strftime("%H:%M:%S")

def voltage_to_percent(voltage):
    try:
        v = float(voltage)

        if v >= 4.20:
            return 100
        elif v >= 4.15:
            return 95
        elif v >= 4.10:
            return 90
        elif v >= 4.05:
            return 85
        elif v >= 4.00:
            return 80
        elif v >= 3.95:
            return 70
        elif v >= 3.90:
            return 60
        elif v >= 3.85:
            return 50
        elif v >= 3.80:
            return 40
        elif v >= 3.75:
            return 30
        elif v >= 3.70:
            return 20
        elif v >= 3.60:
            return 10
        else:
            return 0

    except Exception:
        return None

def node_num_to_id(num):
    try:
        return "!" + format(int(num) & 0xFFFFFFFF, "08x")
    except Exception:
        return ""


def friendly_unknown_node_name(node_id):
    if node_id and node_id.startswith("!") and len(node_id) >= 5:
        return "Meshtastic " + node_id[-4:]
    return node_id or "Unknown"


def fixed_short_name(node_id, fallback=""):
    return KNOWN_NODE_INFO.get(node_id, {}).get("short_name") or fallback or ""


def fixed_hw_model(node_id, fallback=""):
    return KNOWN_NODE_INFO.get(node_id, {}).get("hw_model") or fallback or ""


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
        sensor_data = {
            "temperature": None, "humidity": None, "pressure": None,
            "voltage": None, "current": None, "power": None,
            "battery_percent": None, "air_quality": None, "last_update": None
        }


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


def ensure_known_nodes():
    for node_id, name in KNOWN_NODES.items():
        old = nodes.get(node_id, {})
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
            "short_name": fixed_short_name(node_id, old.get("short_name", "")),
            "hw_model": fixed_hw_model(node_id, old.get("hw_model", ""))
        }
    save_nodes()


def normalize_unknown_nodes():
    changed = False
    for node_id, node in nodes.items():
        name = node.get("name", "")
        if not name or name == node_id or name.startswith("node "):
            node["name"] = friendly_unknown_node_name(node_id)
            changed = True
        if not node.get("short_name") and node_id.startswith("!"):
            node["short_name"] = node_id[-4:]
            changed = True
    if changed:
        save_nodes()


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


def get_node_name(node_id):
    if node_id in KNOWN_NODES:
        return KNOWN_NODES[node_id]
    if node_id in nodes:
        name = nodes[node_id].get("name", "")
        if name and name != node_id and not name.startswith("node "):
            return name
    return friendly_unknown_node_name(node_id)


def resolve_sender_name(sender):
    if sender.startswith("!"):
        return get_node_name(sender)
    return sender


def add_message(kind, sender, text, node_id=""):
    if not node_id:
        node_id = infer_node_id_from_sender(sender)
    messages.append({
        "kind": kind,
        "sender": sender,
        "node_id": node_id,
        "text": text,
        "time": now()
    })
    messages[:] = messages[-MAX_HISTORY_MESSAGES:]
    save_messages()


def extract_packet_id(line):
    m = re.search(r"'id':\s*(\d+)", line)
    if m:
        return m.group(1)
    m = re.search(r"\bid:\s*(\d+)", line)
    if m:
        return m.group(1)
    return None


def extract_node_id(line):
    patterns = [
        r"'fromId':\s*'([^']+)'",
        r'"fromId":\s*"([^"]+)"',
        r"'id':\s*'(![0-9a-fA-F]+)'",
        r'"id":\s*"(![0-9a-fA-F]+)"',
        r"\bid:\s*\"(![0-9a-fA-F]+)\"",
        r"\bid:\s*(![0-9a-fA-F]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, line)
        if m:
            return m.group(1)
    m = re.search(r"'from':\s*(\d+)", line)
    if m:
        return node_num_to_id(m.group(1))
    m = re.search(r"\bfrom:\s*(\d+)", line)
    if m:
        return node_num_to_id(m.group(1))
    return None


def extract_sender(line):
    node_id = extract_node_id(line)
    if node_id:
        return get_node_name(node_id)
    m = re.search(r"'from':\s*(\d+)", line)
    if m:
        return friendly_unknown_node_name(node_num_to_id(m.group(1)))
    m = re.search(r"\bfrom:\s*(\d+)", line)
    if m:
        return friendly_unknown_node_name(node_num_to_id(m.group(1)))
    return "RX"


def extract_text_message(line):
    if "TEXT_MESSAGE_APP" not in line and "'text':" not in line and '"text":' not in line:
        return None
    m = re.search(r"'text':\s*'([^']*)'", line)
    if m:
        return m.group(1).strip()
    m = re.search(r'"text":\s*"([^"]*)"', line)
    if m:
        return m.group(1).strip()
    return None


def extract_rssi(line):
    m = re.search(r"'rxRssi':\s*(-?\d+)", line)
    if m:
        return m.group(1)
    m = re.search(r"\brx_rssi:\s*(-?\d+)", line)
    if m:
        return m.group(1)
    return None


def extract_snr(line):
    m = re.search(r"'rxSnr':\s*(-?\d+(?:\.\d+)?)", line)
    if m:
        return m.group(1)
    m = re.search(r"\brx_snr:\s*(-?\d+(?:\.\d+)?)", line)
    if m:
        return m.group(1)
    return None


def extract_hop_start(line):
    m = re.search(r"'hopStart':\s*(\d+)", line)
    if m:
        return m.group(1)
    m = re.search(r"\bhop_start:\s*(\d+)", line)
    if m:
        return m.group(1)
    return None


def extract_relay_node(line):
    m = re.search(r"'relayNode':\s*(\d+)", line)
    if m:
        return m.group(1)
    m = re.search(r"\brelay_node:\s*(\d+)", line)
    if m:
        return m.group(1)
    return None


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
    rssi = extract_rssi(block)
    snr = extract_snr(block)
    hop_start = extract_hop_start(block)
    relay_node = extract_relay_node(block)
    name = KNOWN_NODES.get(node_id) or long_name or short_name or friendly_unknown_node_name(node_id)
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
        "last_text": old.get("last_text", ""),
        "short_name": fixed_short_name(node_id, short_name or old.get("short_name", "") or node_id[-4:]),
        "hw_model": fixed_hw_model(node_id, hw_model or old.get("hw_model", ""))
    }
    save_nodes()
    return True


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


def update_node(line, sender, text):
    node_id = extract_node_id(line) or infer_node_id_from_sender(sender)
    if not node_id:
        return ""
    rssi = extract_rssi(line)
    snr = extract_snr(line)
    hop_start = extract_hop_start(line)
    relay_node = extract_relay_node(line)
    name = get_node_name(node_id)
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
        "last_text": text or "",
        "short_name": fixed_short_name(node_id, old.get("short_name", "") or node_id[-4:]),
        "hw_model": fixed_hw_model(node_id, old.get("hw_model", ""))
    }
    save_nodes()
    return node_id


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
        quality = signal_quality(rssi)
        meta_parts = [age_text(last_seen)]
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
        result.append({
            "name": icon + " " + n["name"],
            "clean_name": n["name"],
            "node_id": n["node_id"],
            "meta": " | ".join(meta_parts),
            "last_text": last_text,
            "short_name": short_name,
            "hw_model": hw_model,
            "rssi": rssi,
            "snr": snr,
            "hop_start": hop_start,
            "relay_node": relay_node,
            "signal_quality": quality,
            "age": age_text(last_seen)
        })
    return result


def is_duplicate_text(sender, text):
    cleaned_text = text.strip()
    if not cleaned_text:
        return True
    current_time = time.time()
    old_keys = []
    for key, ts in seen_recent_texts.items():
        if current_time - ts > 60:
            old_keys.append(key)
    for key in old_keys:
        del seen_recent_texts[key]
    old_time = seen_recent_texts.get(cleaned_text)
    if old_time and current_time - old_time < 15:
        return True
    seen_recent_texts[cleaned_text] = current_time
    return False


def stop_listener():
    global listen_process
    if listen_process is not None:
        try:
            listen_process.terminate()
            time.sleep(0.5)
            if listen_process.poll() is None:
                listen_process.kill()
        except Exception:
            pass
        listen_process = None


def update_base_status_from_info():
    global base_status

    try:
        result = subprocess.run(
            [MESHTASTIC_CMD, "--info"],
            capture_output=True,
            text=True,
            timeout=15
        )

        output = result.stdout + result.stderr

        node_pos = output.find(f'"{LOCAL_NODE_ID}"')
        if node_pos < 0:
            print("Base status: local node id not found")
            return

        next_node_pos = output.find('\n  "!', node_pos + 1)
        if next_node_pos < 0:
            node_block = output[node_pos:]
        else:
            node_block = output[node_pos:next_node_pos]

        metrics_pos = node_block.find('"deviceMetrics"')
        if metrics_pos < 0:
            print("Base status: deviceMetrics not found")
            return

        block_start = node_block.find("{", metrics_pos)
        block_end = node_block.find("}", block_start)

        if block_start < 0 or block_end < 0:
            print("Base status: metrics block not found")
            return

        metrics_text = node_block[block_start:block_end + 1]
        metrics = json.loads(metrics_text)

        voltage = metrics.get("voltage")
        battery_level = metrics.get("batteryLevel")

        real_battery = voltage_to_percent(voltage)

        base_status = {
            "battery_level": battery_level,
            "real_battery": real_battery,
            "voltage": voltage,
            "channel_utilization": metrics.get("channelUtilization"),
            "air_util_tx": metrics.get("airUtilTx"),
            "uptime_seconds": metrics.get("uptimeSeconds"),
            "last_update": now()
        }

        print("Base status updated:", base_status)

    except Exception as e:
        print("Base status update error:", e)

def read_sensors_from_meshtastic():
    """Read sensor data from RAK4631 via Meshtastic telemetry"""
    global sensor_data
    try:
        result = subprocess.run(
            [MESHTASTIC_CMD, "--get", "telemetry"],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout + result.stderr
        
        # Parse environment metrics (BME280)
        temp_match = re.search(r'(?:temperature|temp)[:\s=]+(-?\d+\.?\d*)', output, re.IGNORECASE)
        hum_match = re.search(r'(?:humidity|hum)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        press_match = re.search(r'(?:pressure)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        
        # Parse power metrics (INA226)
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
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        print(f"Error reading sensors: {e}")


def base_status_worker():
    while True:
        update_base_status_from_info()
        time.sleep(120)


def sensor_reader_worker():
    while True:
        read_sensors_from_meshtastic()
        time.sleep(10)


def listen_meshtastic():
    global listen_process
    nodeinfo_buffer = []
    collecting_nodeinfo = False
    while True:
        if pause_listen.is_set():
            time.sleep(0.5)
            continue
        try:
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
            for line in listen_process.stdout:
                if pause_listen.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
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
                add_message("rx", sender, text, node_id)
        except Exception as e:
            add_message("rx", "SYSTEM ERROR", "listen: " + str(e), "")
        time.sleep(2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/messages")
def api_messages():
    status = "radio: sending..." if pause_listen.is_set() else "radio: listening"
    filter_node_id = request.args.get("node_id", "").strip()
    visible_messages = []
    for m in messages:
        msg = dict(m)
        sender = msg.get("sender", "")
        node_id = msg.get("node_id", "")
        if not node_id:
            node_id = infer_node_id_from_sender(sender)
            msg["node_id"] = node_id
        msg["sender"] = resolve_sender_name(sender)
        if filter_node_id and node_id != filter_node_id:
            continue
        visible_messages.append(msg)
    return jsonify({
        "status": status,
        "messages": visible_messages,
        "nodes": get_nodes_list()
    })


@app.route("/api/sensors")
def api_sensors():
    return jsonify(sensor_data)


@app.route("/api/base_status")
def api_base_status():
    return jsonify(base_status)


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty message"}), 400
    pause_listen.set()
    with radio_lock:
        try:
            stop_listener()
            time.sleep(1)
            # Уменьшаем таймаут до 30 секунд
            result = subprocess.run(
                [MESHTASTIC_CMD, "--ch-index", "0", "--sendtext", text],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                add_message("me", LOCAL_NODE_NAME, text, LOCAL_NODE_ID)
                old = nodes.get(LOCAL_NODE_ID, {})
                nodes[LOCAL_NODE_ID] = {
                    "name": LOCAL_NODE_NAME,
                    "node_id": LOCAL_NODE_ID,
                    "last_seen": time.time(),
                    "last_time": now(),
                    "rssi": old.get("rssi"),
                    "snr": old.get("snr"),
                    "hop_start": old.get("hop_start", ""),
                    "relay_node": old.get("relay_node", ""),
                    "last_text": "sent: " + text,
                    "short_name": fixed_short_name(LOCAL_NODE_ID, old.get("short_name", "")),
                    "hw_model": fixed_hw_model(LOCAL_NODE_ID, old.get("hw_model", ""))
                }
                save_nodes()
                return jsonify({"ok": True})
            err = result.stderr.strip() or result.stdout.strip() or "unknown send error"
            add_message("rx", "SYSTEM ERROR", "send: " + err, "")
            return jsonify({"ok": False, "error": err}), 500
        except subprocess.TimeoutExpired:
            add_message("rx", "SYSTEM ERROR", "send timeout after 30 seconds", "")
            return jsonify({"ok": False, "error": "timeout"}), 500
        except Exception as e:
            add_message("rx", "SYSTEM ERROR", "send: " + str(e), "")
            return jsonify({"ok": False, "error": str(e)}), 500
        finally:
            time.sleep(1)
            pause_listen.clear()


if __name__ == "__main__":
    load_messages()
    load_nodes()
    load_sensors_data()
    ensure_known_nodes()
    normalize_unknown_nodes()
    update_base_status_from_info()
    
    sensor_thread = threading.Thread(target=sensor_reader_worker, daemon=True)
    sensor_thread.start()
    
    base_status_thread = threading.Thread(target=base_status_worker, daemon=True)
    base_status_thread.start()
    
    listener_thread = threading.Thread(target=listen_meshtastic, daemon=True)
    listener_thread.start()
    
    print("""
    ╔══════════════════════════════════════════╗
    ║     Meshtastic Web Interface Started     ║
    ╠══════════════════════════════════════════╣
    ║  URL: http://{}:{}    ║
    ╚══════════════════════════════════════════╝
    """.format(APP_HOST, APP_PORT))
    
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)