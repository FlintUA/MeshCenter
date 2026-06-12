from flask import Flask, request, jsonify, render_template_string
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
MAX_HISTORY_MESSAGES = 300
INFO_REFRESH_SECS = 120


KNOWN_NODES = {
    "!1fa065f0": "Elektroniker",
    "!067a40fa": "Flint Base",
    "!756f9960": "Flint TAP2",
    "!1300faf0": "Orion9 mobil",
    "!f68f9e94": "ThinkNode M5",
    "!04c67058": "HardTekkER",
}

KNOWN_NODE_INFO = {
    "!1fa065f0": {"short_name": "Elek", "hw_model": "TBEAM"},
    "!067a40fa": {"short_name": "FLTB", "hw_model": "RAK4631"},
    "!756f9960": {"short_name": "FLT2", "hw_model": "RAK3312"},
    "!1300faf0": {"short_name": "ori9", "hw_model": "T_DECK"},
    "!f68f9e94": {"short_name": "AB4", "hw_model": "THINKNODE_M5"},
    "!04c67058": {"short_name": "TeKK", "hw_model": "HELTEC_V4"},
}

app = Flask(__name__)

messages = []
seen_ids = set()
seen_recent_texts = {}
nodes = {}

listen_process = None
radio_lock = threading.Lock()
pause_listen = threading.Event()

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Meshnode Web Chat</title>
<style>
html, body {
    height: 100%;
    margin: 0;
    overflow: hidden;
}
body {
    font-family: Arial, sans-serif;
    background: #eeeeee;
    display: flex;
    flex-direction: column;
}
.header {
    flex: 0 0 auto;
    padding: 12px 18px;
    background: white;
    font-size: 24px;
    font-weight: bold;
    border-bottom: 1px solid #ddd;
}
.status {
    flex: 0 0 auto;
    padding: 4px 18px;
    background: white;
    color: #777;
    font-size: 12px;
}
.filterbar {
    flex: 0 0 auto;
    padding: 6px 18px;
    background: #f7f7f7;
    border-bottom: 1px solid #ddd;
    font-size: 13px;
    display: none;
    align-items: center;
    gap: 10px;
}
.filterbar button {
    font-size: 12px;
    padding: 3px 10px;
}
.main {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
}
#chat {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    padding: 12px;
    background: #eeeeee;
}
#nodes {
    width: 320px;
    flex: 0 0 320px;
    overflow-y: auto;
    background: #f8f8f8;
    border-left: 1px solid #ccc;
    padding: 10px;
    box-sizing: border-box;
}
.nodes-title {
    font-weight: bold;
    margin-bottom: 8px;
}
.node-card {
    background: white;
    border: 1px solid #ddd;
    border-radius: 10px;
    padding: 8px;
    margin-bottom: 8px;
    cursor: pointer;
}
.node-card.selected {
    border: 2px solid #4caf50;
}
.node-details {
    background: #ffffff;
    border: 1px solid #bbb;
    border-radius: 10px;
    padding: 8px;
    margin-bottom: 10px;
    font-size: 12px;
}
.node-details-title {
    font-weight: bold;
    font-size: 14px;
    margin-bottom: 5px;
}
.node-name {
    font-weight: bold;
    font-size: 14px;
}
.node-id {
    color: #777;
    font-size: 11px;
}
.node-meta {
    color: #555;
    font-size: 12px;
    margin-top: 4px;
}
.row {
    display: flex;
    margin-bottom: 8px;
}
.row.me {
    justify-content: flex-end;
}
.row.rx {
    justify-content: flex-start;
}
.bubble {
    max-width: 70%;
    padding: 7px 11px;
    border-radius: 12px;
    background: white;
    border: 1px solid #ddd;
}
.row.me .bubble {
    background: #dcf8c6;
}
.sender {
    font-size: 12px;
    color: #555;
    font-weight: bold;
    margin-bottom: 2px;
}
.text {
    font-size: 18px;
    white-space: pre-wrap;
    word-break: break-word;
}
.time {
    font-size: 11px;
    color: #777;
    text-align: right;
    margin-top: 3px;
}
form {
    flex: 0 0 auto;
    height: 54px;
    display: flex;
    gap: 8px;
    padding: 8px;
    background: white;
    border-top: 1px solid #ddd;
    box-sizing: border-box;
}
input {
    flex: 1;
    padding: 8px 10px;
    font-size: 17px;
}
button {
    padding: 8px 22px;
    font-size: 17px;
}
@media (max-width: 900px) {
    #nodes {
        display: none;
    }
}
</style>
</head>
<body>
<div class="header">Meshnode Web Chat - LongFast Channel 0</div>
<div class="status" id="status">loading...</div>

<div class="filterbar" id="filterbar">
    <span id="filterText"></span>
    <button type="button" onclick="clearFilter()">Show all</button>
</div>

<div class="main">
    <div id="chat"></div>
    <div id="nodes">
        <div class="nodes-title" id="nodesTitle">Nodes</div>
        <div id="nodeDetails"></div>
        <div id="nodesList"></div>
    </div>
</div>

<form id="sendForm">
<input id="text" autocomplete="off" placeholder="Введите сообщение..." />
<button type="submit">Send</button>
</form>

<script>
let selectedNodeId = null;
let selectedNodeName = null;

function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

function clearFilter() {
    selectedNodeId = null;
    selectedNodeName = null;
    renderNodeDetails(null);
    loadMessages();
}

function updateFilterBar() {
    const bar = document.getElementById('filterbar');
    const text = document.getElementById('filterText');

    if (!selectedNodeId) {
        bar.style.display = 'none';
        text.textContent = '';
        return;
    }

    bar.style.display = 'flex';
    text.textContent = 'Filter: ' + selectedNodeName + ' (' + selectedNodeId + ')';
}

function renderNodeDetails(node) {
    const details = document.getElementById('nodeDetails');

    if (!node) {
        details.innerHTML = '';
        updateFilterBar();
        return;
    }

    details.innerHTML =
        '<div class="node-details">' +
        '<div class="node-details-title">' + esc(node.clean_name) + '</div>' +
        '<div>ID: ' + esc(node.node_id) + '</div>' +
        '<div>Short: ' + esc(node.short_name || '-') + '</div>' +
        '<div>Hardware: ' + esc(node.hw_model || '-') + '</div>' +
        '<div>Last seen: ' + esc(node.age) + '</div>' +
        '<div>Signal: ' + esc(node.signal_quality || '-') + '</div>' +
        '<div>RSSI: ' + esc(node.rssi || '-') + '</div>' +
        '<div>SNR: ' + esc(node.snr || '-') + '</div>' +
        '<div>Hops: ' + esc(node.hop_start || '-') + '</div>' +
        '<div>Relay: ' + esc(node.relay_node || '-') + '</div>' +
        '<div>Last message: ' + esc(node.last_text || '-') + '</div>' +
        '<hr>' +
        '<div><b>Power</b></div>' +
        '<div>Battery: ' + esc(node.battery_level ? node.battery_level + ' %' : '-') + '</div>' +
        '<div>Voltage: ' + esc(node.voltage ? node.voltage + ' V' : '-') + '</div>' +
        '<div>Channel: ' + esc(node.channel_utilization ? node.channel_utilization + ' %' : '-') + '</div>' +
        '<div>Air TX: ' + esc(node.air_util_tx ? node.air_util_tx + ' %' : '-') + '</div>' +
        '<div>Uptime: ' + esc(node.uptime_text || '-') + '</div>' +
        '<div>INA voltage: ' + esc(node.ina_voltage ? node.ina_voltage + ' V' : '-') + '</div>' +
        '<div>INA current: ' + esc(node.ina_current ? node.ina_current + ' mA' : '-') + '</div>' +
        '</div>';

    updateFilterBar();
}

async function loadMessages() {
    let url = '/api/messages';

    if (selectedNodeId) {
        url += '?node_id=' + encodeURIComponent(selectedNodeId);
    }

    const r = await fetch(url);
    const data = await r.json();

    document.getElementById('status').textContent = data.status;

    const chat = document.getElementById('chat');
    const nearBottom = chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 80;

    chat.innerHTML = '';

    data.messages.forEach(m => {
        const row = document.createElement('div');
        row.className = 'row ' + m.kind;

        const bubble = document.createElement('div');
        bubble.className = 'bubble';

        const sender = document.createElement('div');
        sender.className = 'sender';
        sender.textContent = m.sender;

        const text = document.createElement('div');
        text.className = 'text';
        text.textContent = m.text;

        const time = document.createElement('div');
        time.className = 'time';
        time.textContent = m.time;

        bubble.appendChild(sender);
        bubble.appendChild(text);
        bubble.appendChild(time);
        row.appendChild(bubble);
        chat.appendChild(row);
    });

    if (nearBottom) {
        chat.scrollTop = chat.scrollHeight;
    }

    const nodesList = document.getElementById('nodesList');
    nodesList.innerHTML = '';

    document.getElementById('nodesTitle').textContent =
        'Nodes (' + data.nodes.length + ')';

    data.nodes.forEach(n => {
        const card = document.createElement('div');
        card.className = selectedNodeId === n.node_id ? 'node-card selected' : 'node-card';

        card.onclick = () => {
            if (selectedNodeId === n.node_id) {
                clearFilter();
                return;
            }

            selectedNodeId = n.node_id;
            selectedNodeName = n.clean_name;
            renderNodeDetails(n);
            loadMessages();
        };

        const name = document.createElement('div');
        name.className = 'node-name';
        name.textContent = n.name;

        const id = document.createElement('div');
        id.className = 'node-id';
        id.textContent = n.node_id;

        const meta = document.createElement('div');
        meta.className = 'node-meta';
        meta.textContent = n.meta;

        const lastText = document.createElement('div');
        lastText.className = 'node-meta';
        lastText.textContent = n.last_text ? "Msg: " + n.last_text : "";

        card.appendChild(name);
        card.appendChild(id);
        card.appendChild(meta);
        card.appendChild(lastText);
        nodesList.appendChild(card);
    });

    const selectedNode = data.nodes.find(n => n.node_id === selectedNodeId);

    if (selectedNode) {
        selectedNodeName = selectedNode.clean_name;
    }

    renderNodeDetails(selectedNode);
}

document.getElementById('sendForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const input = document.getElementById('text');
    const text = input.value.trim();
    if (!text) return;

    input.disabled = true;

    await fetch('/api/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text})
    });

    input.value = '';
    input.disabled = false;
    input.focus();
    loadMessages();
});

setInterval(loadMessages, 2000);
loadMessages();
</script>
</body>
</html>
"""

def now():
    return time.strftime("%H:%M:%S")

def fixed_short_name(node_id, fallback=""):
    return KNOWN_NODE_INFO.get(node_id, {}).get("short_name") or fallback or ""

def fixed_hw_model(node_id, fallback=""):
    return KNOWN_NODE_INFO.get(node_id, {}).get("hw_model") or fallback or ""

def fmt_float(value, digits=2):
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)

def fmt_int(value):
    if value is None or value == "":
        return ""
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)

def uptime_text(seconds):
    if seconds is None or seconds == "":
        return ""

    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds)

    if seconds < 60:
        return f"{seconds} sec"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"

    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"

    days = hours // 24
    return f"{days} d"

def extract_json_object(text, marker):
    start_marker = text.find(marker)
    if start_marker < 0:
        return ""

    start = text.find("{", start_marker)
    if start < 0:
        return ""

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape:
            escape = False
            continue

        if ch == "\\":
            escape = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return ""

def power_value(metrics, names):
    if not isinstance(metrics, dict):
        return ""

    for name in names:
        if name in metrics:
            return metrics.get(name)

    return ""

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
            "hw_model": fixed_hw_model(node_id, old.get("hw_model", "")),
            "battery_level": old.get("battery_level", ""),
            "voltage": old.get("voltage", ""),
            "channel_utilization": old.get("channel_utilization", ""),
            "air_util_tx": old.get("air_util_tx", ""),
            "uptime_seconds": old.get("uptime_seconds", ""),
            "ina_voltage": old.get("ina_voltage", ""),
            "ina_current": old.get("ina_current", ""),
            "hops_away": old.get("hops_away", "")
        }

    save_nodes()

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

    return None

def get_node_name(node_id):
    if node_id in KNOWN_NODES:
        return KNOWN_NODES[node_id]

    if node_id in nodes:
        return nodes[node_id].get("name", node_id)

    return node_id

def resolve_sender_name(sender):
    if sender.startswith("!"):
        return get_node_name(sender)

    return sender

def extract_sender(line):
    node_id = extract_node_id(line)
    if node_id:
        return get_node_name(node_id)

    m = re.search(r"'from':\s*(\d+)", line)
    if m:
        return "node " + m.group(1)

    m = re.search(r"\bfrom:\s*(\d+)", line)
    if m:
        return "node " + m.group(1)

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
    if (
        "NODEINFO_APP" not in block
        and "longName" not in block
        and "long_name" not in block
        and "shortName" not in block
        and "short_name" not in block
        and "hwModel" not in block
        and "hw_model" not in block
    ):
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

    name = KNOWN_NODES.get(node_id) or long_name or short_name or node_id
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
        "short_name": fixed_short_name(node_id, short_name or old.get("short_name", "")),
        "hw_model": fixed_hw_model(node_id, hw_model or old.get("hw_model", "")),
        "battery_level": old.get("battery_level", ""),
        "voltage": old.get("voltage", ""),
        "channel_utilization": old.get("channel_utilization", ""),
        "air_util_tx": old.get("air_util_tx", ""),
        "uptime_seconds": old.get("uptime_seconds", ""),
        "ina_voltage": old.get("ina_voltage", ""),
        "ina_current": old.get("ina_current", ""),
        "hops_away": old.get("hops_away", "")
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
    rssi = extract_rssi(line)
    snr = extract_snr(line)
    hop_start = extract_hop_start(line)
    relay_node = extract_relay_node(line)

    name = get_node_name(node_id) if node_id else sender
    old = nodes.get(node_id, {}) if node_id else {}

    if not node_id:
        return ""

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
        "short_name": fixed_short_name(node_id, old.get("short_name", "")),
        "hw_model": fixed_hw_model(node_id, old.get("hw_model", "")),
        "battery_level": old.get("battery_level", ""),
        "voltage": old.get("voltage", ""),
        "channel_utilization": old.get("channel_utilization", ""),
        "air_util_tx": old.get("air_util_tx", ""),
        "uptime_seconds": old.get("uptime_seconds", ""),
        "ina_voltage": old.get("ina_voltage", ""),
        "ina_current": old.get("ina_current", ""),
        "hops_away": old.get("hops_away", "")
    }

    save_nodes()
    return node_id

def get_nodes_list():
    sorted_nodes = sorted(
        nodes.values(),
        key=lambda n: n.get("last_seen", 0),
        reverse=True
    )

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
        battery_level = n.get("battery_level", "")
        voltage = n.get("voltage", "")
        channel_utilization = n.get("channel_utilization", "")
        air_util_tx = n.get("air_util_tx", "")
        uptime_seconds = n.get("uptime_seconds", "")
        ina_voltage = n.get("ina_voltage", "")
        ina_current = n.get("ina_current", "")
        hops_away = n.get("hops_away", "")

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
        if battery_level:
            meta_parts.append("bat: " + str(battery_level) + "%")
        if voltage:
            meta_parts.append("V: " + str(voltage))
        if hops_away != "":
            meta_parts.append("hopsAway: " + str(hops_away))

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
            "age": age_text(last_seen),
            "battery_level": battery_level,
            "voltage": voltage,
            "channel_utilization": channel_utilization,
            "air_util_tx": air_util_tx,
            "uptime_seconds": uptime_seconds,
            "uptime_text": uptime_text(uptime_seconds),
            "ina_voltage": ina_voltage,
            "ina_current": ina_current,
            "hops_away": hops_away
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

def update_nodes_from_info_text(info_text):
    nodes_json = extract_json_object(info_text, "Nodes in mesh:")
    if not nodes_json:
        return 0

    try:
        mesh_nodes = json.loads(nodes_json)
    except Exception as e:
        print("Info parse error:", e)
        return 0

    count = 0

    for node_id, data in mesh_nodes.items():
        if not isinstance(data, dict):
            continue

        user = data.get("user", {}) or {}
        device_metrics = data.get("deviceMetrics", {}) or {}
        power_metrics = data.get("powerMetrics", {}) or {}

        long_name = user.get("longName") or user.get("long_name") or node_id
        short_name = user.get("shortName") or user.get("short_name") or ""
        hw_model = user.get("hwModel") or user.get("hw_model") or ""

        old = nodes.get(node_id, {})

        last_seen = data.get("lastHeard") or old.get("last_seen", 0)
        try:
            last_seen = float(last_seen)
        except (TypeError, ValueError):
            last_seen = old.get("last_seen", 0)

        voltage = power_value(device_metrics, ["voltage", "batteryVoltage", "battery_voltage"])
        battery_level = power_value(device_metrics, ["batteryLevel", "battery_level"])
        channel_utilization = power_value(device_metrics, ["channelUtilization", "channel_utilization"])
        air_util_tx = power_value(device_metrics, ["airUtilTx", "air_util_tx"])
        uptime_seconds = power_value(device_metrics, ["uptimeSeconds", "uptime_seconds"])

        ina_voltage = power_value(
            power_metrics,
            ["voltage", "inaVoltage", "ina_voltage", "busVoltage", "bus_voltage", "ch1Voltage", "ch1_voltage"]
        )
        ina_current = power_value(
            power_metrics,
            ["current", "inaCurrent", "ina_current", "busCurrent", "bus_current", "ch1Current", "ch1_current"]
        )

        nodes[node_id] = {
            "name": KNOWN_NODES.get(node_id) or long_name,
            "node_id": node_id,
            "last_seen": last_seen,
            "last_time": old.get("last_time", now()),
            "rssi": old.get("rssi"),
            "snr": data.get("snr", old.get("snr")),
            "hop_start": old.get("hop_start", ""),
            "relay_node": old.get("relay_node", ""),
            "hops_away": data.get("hopsAway", old.get("hops_away", "")),
            "last_text": old.get("last_text", ""),
            "short_name": fixed_short_name(node_id, short_name or old.get("short_name", "")),
            "hw_model": fixed_hw_model(node_id, hw_model or old.get("hw_model", "")),
            "battery_level": fmt_int(battery_level) or old.get("battery_level", ""),
            "voltage": fmt_float(voltage, 3) or old.get("voltage", ""),
            "channel_utilization": fmt_float(channel_utilization, 2) or old.get("channel_utilization", ""),
            "air_util_tx": fmt_float(air_util_tx, 3) or old.get("air_util_tx", ""),
            "uptime_seconds": fmt_int(uptime_seconds) or old.get("uptime_seconds", ""),
            "ina_voltage": fmt_float(ina_voltage, 3) or old.get("ina_voltage", ""),
            "ina_current": fmt_float(ina_current, 2) or old.get("ina_current", "")
        }

        count += 1

    if count:
        save_nodes()

    return count

def refresh_nodes_from_info_once():
    pause_listen.set()

    with radio_lock:
        try:
            stop_listener()
            time.sleep(1)

            result = subprocess.run(
                [MESHTASTIC_CMD, "--info"],
                capture_output=True,
                text=True,
                timeout=90
            )

            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip()
                print("Info refresh error:", err)
                return

            count = update_nodes_from_info_text(result.stdout)
            print("Info refresh nodes:", count)

        except Exception as e:
            print("Info refresh exception:", e)

        finally:
            time.sleep(1)
            pause_listen.clear()

def info_refresh_worker():
    time.sleep(20)

    while True:
        refresh_nodes_from_info_once()
        time.sleep(INFO_REFRESH_SECS)

def stop_listener():
    global listen_process

    if listen_process is not None:
        try:
            listen_process.terminate()
            time.sleep(1)

            if listen_process.poll() is None:
                listen_process.kill()
                time.sleep(1)
        except Exception:
            pass

        listen_process = None

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
    return render_template_string(HTML)

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
            time.sleep(2)

            result = subprocess.run(
                [MESHTASTIC_CMD, "--ch-index", "0", "--sendtext", text],
                capture_output=True,
                text=True,
                timeout=60
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
                    "hw_model": fixed_hw_model(LOCAL_NODE_ID, old.get("hw_model", "")),
                    "battery_level": old.get("battery_level", ""),
                    "voltage": old.get("voltage", ""),
                    "channel_utilization": old.get("channel_utilization", ""),
                    "air_util_tx": old.get("air_util_tx", ""),
                    "uptime_seconds": old.get("uptime_seconds", ""),
                    "ina_voltage": old.get("ina_voltage", ""),
                    "ina_current": old.get("ina_current", ""),
                    "hops_away": old.get("hops_away", "")
                }

                save_nodes()

                return jsonify({"ok": True})

            err = result.stderr.strip() or result.stdout.strip() or "unknown send error"
            add_message("rx", "SYSTEM ERROR", "send: " + err, "")
            return jsonify({"ok": False, "error": err}), 500

        except Exception as e:
            add_message("rx", "SYSTEM ERROR", "send: " + str(e), "")
            return jsonify({"ok": False, "error": str(e)}), 500

        finally:
            time.sleep(2)
            pause_listen.clear()

if __name__ == "__main__":
    load_messages()
    load_nodes()
    ensure_known_nodes()

    t = threading.Thread(target=listen_meshtastic, daemon=True)
    t.start()

    info_t = threading.Thread(target=info_refresh_worker, daemon=True)
    info_t.start()

    app.run(host=APP_HOST, port=APP_PORT)
    