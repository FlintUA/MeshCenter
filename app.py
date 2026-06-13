from flask import Flask, request, jsonify, render_template
import subprocess
import threading
import time
import re
import json
import os
from datetime import datetime

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
    "voltage": None,
    "channel_utilization": None,
    "air_util_tx": None,
    "uptime_seconds": None,
    "last_update": None
}

listen_process = None
radio_lock = threading.Lock()
pause_listen = threading.Event()

HTML = """
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Meshtastic Mesh Network</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            height: 100vh;
            overflow: hidden;
            color: #333;
        }

        ::-webkit-scrollbar {
            width: 8px;
            height: 8px;
        }

        ::-webkit-scrollbar-track {
            background: #f1f1f1;
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb {
            background: #888;
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: #555;
        }

        .app-container {
            height: 100vh;
            display: flex;
            flex-direction: column;
            background: rgba(255, 255, 255, 0.95);
        }

        .header {
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: white;
            padding: 16px 24px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }

        .header h1 {
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 4px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .header-sub {
            font-size: 12px;
            opacity: 0.9;
        }

        .status-bar {
            background: rgba(0,0,0,0.1);
            padding: 6px 24px;
            font-size: 12px;
            color: white;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .main-layout {
            display: flex;
            flex: 1;
            overflow: hidden;
        }

        .chat-area {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: #f5f5f5;
        }

        .messages-container {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
        }

        .message {
            margin-bottom: 16px;
            display: flex;
            animation: fadeIn 0.3s ease-in;
        }

        @keyframes fadeIn {
            from {
                opacity: 0;
                transform: translateY(10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .message.me {
            justify-content: flex-end;
        }

        .message.rx {
            justify-content: flex-start;
        }

        .bubble {
            max-width: 70%;
            padding: 10px 14px;
            border-radius: 18px;
            position: relative;
            box-shadow: 0 1px 2px rgba(0,0,0,0.1);
        }

        .message.me .bubble {
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: white;
        }

        .message.rx .bubble {
            background: white;
            color: #333;
        }

        .sender {
            font-size: 11px;
            font-weight: 600;
            margin-bottom: 4px;
            opacity: 0.8;
        }

        .text {
            font-size: 14px;
            word-wrap: break-word;
            line-height: 1.4;
        }

        .time {
            font-size: 10px;
            margin-top: 4px;
            text-align: right;
            opacity: 0.7;
        }

        .input-area {
            background: white;
            padding: 16px 20px;
            border-top: 1px solid #e0e0e0;
        }

        .input-form {
            display: flex;
            gap: 12px;
            align-items: center;
        }

        .input-form input {
            flex: 1;
            padding: 12px 16px;
            border: 2px solid #e0e0e0;
            border-radius: 25px;
            font-size: 14px;
            transition: all 0.3s;
            outline: none;
        }

        .input-form input:focus {
            border-color: #1e3c72;
        }

        .input-form button {
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: white;
            border: none;
            padding: 12px 28px;
            border-radius: 25px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }

        .input-form button:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(30, 60, 114, 0.4);
        }

        .sidebar {
            width: 380px;
            background: white;
            border-left: 1px solid #e0e0e0;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
            box-shadow: -2px 0 10px rgba(0,0,0,0.05);
        }

        .sensors-card {
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: white;
            padding: 20px;
            margin: 16px;
            border-radius: 16px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }

        .sensors-title {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .sensors-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
        }

        .sensor-item {
            background: rgba(255,255,255,0.2);
            padding: 10px;
            border-radius: 12px;
            text-align: center;
            transition: transform 0.2s;
        }

        .sensor-item:hover {
            transform: scale(1.05);
            background: rgba(255,255,255,0.3);
        }

        .sensor-label {
            font-size: 11px;
            opacity: 0.9;
            margin-bottom: 4px;
        }

        .sensor-value {
            font-size: 20px;
            font-weight: 700;
        }

        .sensor-unit {
            font-size: 12px;
            opacity: 0.8;
        }

        .sensor-update {
            font-size: 10px;
            text-align: center;
            margin-top: 12px;
            opacity: 0.8;
        }

        .battery-indicator {
            margin-top: 10px;
            background: rgba(255,255,255,0.2);
            border-radius: 10px;
            padding: 8px;
            text-align: center;
        }

        .battery-bar {
            height: 8px;
            background: rgba(255,255,255,0.3);
            border-radius: 4px;
            overflow: hidden;
            margin-top: 5px;
        }

        .battery-fill {
            height: 100%;
            background: #4caf50;
            transition: width 0.3s;
            border-radius: 4px;
        }

        .nodes-section {
            flex: 1;
            padding: 0 16px 16px 16px;
        }

        .nodes-header {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 12px;
            color: #1e3c72;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .node-card {
            background: #f8f9fa;
            border: 1px solid #e0e0e0;
            border-radius: 12px;
            padding: 12px;
            margin-bottom: 8px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .node-card:hover {
            transform: translateX(4px);
            border-color: #1e3c72;
            box-shadow: 0 2px 8px rgba(30, 60, 114, 0.2);
        }

        .node-card.selected {
            background: linear-gradient(135deg, #1e3c7215 0%, #2a529815 100%);
            border-color: #1e3c72;
            border-width: 2px;
        }

        .node-name {
            font-weight: 600;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 6px;
            margin-bottom: 4px;
        }

        .node-id {
            font-size: 10px;
            color: #999;
            font-family: monospace;
            margin-bottom: 6px;
        }

        .node-meta {
            font-size: 10px;
            color: #666;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 6px;
        }

        .badge {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 6px;
            font-size: 9px;
            font-weight: 600;
        }

        .badge-online {
            background: #4caf50;
            color: white;
        }

        .badge-offline {
            background: #9e9e9e;
            color: white;
        }

        .filter-bar {
            background: #fff3e0;
            padding: 8px 20px;
            display: none;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid #ffe0b2;
        }

        .filter-bar.show {
            display: flex;
        }

        .filter-text {
            font-size: 13px;
            color: #e65100;
        }

        .clear-filter {
            background: #ff9800;
            color: white;
            border: none;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 11px;
            cursor: pointer;
        }

        @media (max-width: 768px) {
            .sidebar {
                display: none;
            }
            .bubble {
                max-width: 85%;
            }
        }
    </style>
</head>
<body>
    <div class="app-container">
        <div class="header">
            <h1>
                <span>📡</span> Meshtastic Mesh Network
            </h1>
            <div class="header-sub">LongFast Channel 0 | RAK4631 Base Station</div>
        </div>
        <div class="status-bar">
            <span id="statusText">🟢 Connecting...</span>
            <span id="nodeCount"></span>
        </div>
        <div class="filter-bar" id="filterBar">
            <span class="filter-text" id="filterText"></span>
            <button class="clear-filter" onclick="clearFilter()">✕ Clear filter</button>
        </div>
        <div class="main-layout">
            <div class="chat-area">
                <div class="messages-container" id="messagesContainer">
                    <div class="loading">Loading messages...</div>
                </div>
                <div class="input-area">
                    <form class="input-form" id="sendForm">
                        <input type="text" id="messageInput" placeholder="Type your message..." autocomplete="off">
                        <button type="submit">Send 📡</button>
                    </form>
                </div>
            </div>
            <div class="sidebar">
                <div class="sensors-card" id="sensorsCard">
                    <div class="sensors-title">
                        <span>🌡️</span> Environment Sensors
                    </div>
                    <div class="sensors-grid">
                        <div class="sensor-item">
                            <div class="sensor-label">Temperature</div>
                            <div class="sensor-value" id="tempValue">--</div>
                            <div class="sensor-unit">°C</div>
                        </div>
                        <div class="sensor-item">
                            <div class="sensor-label">Humidity</div>
                            <div class="sensor-value" id="humValue">--</div>
                            <div class="sensor-unit">%</div>
                        </div>
                        <div class="sensor-item">
                            <div class="sensor-label">Pressure</div>
                            <div class="sensor-value" id="presValue">--</div>
                            <div class="sensor-unit">hPa</div>
                        </div>
                        <div class="sensor-item">
                            <div class="sensor-label">Voltage</div>
                            <div class="sensor-value" id="voltValue">--</div>
                            <div class="sensor-unit">V</div>
                        </div>
                        <div class="sensor-item">
                            <div class="sensor-label">Current</div>
                            <div class="sensor-value" id="currValue">--</div>
                            <div class="sensor-unit">mA</div>
                        </div>
                        <div class="sensor-item">
                            <div class="sensor-label">Power</div>
                            <div class="sensor-value" id="powValue">--</div>
                            <div class="sensor-unit">mW</div>
                        </div>
                    </div>
                    <div class="battery-indicator" id="batteryIndicator" style="display: none;">
                        <div>🔋 Battery Level</div>
                        <div class="battery-bar">
                            <div class="battery-fill" id="batteryFill" style="width: 0%"></div>
                        </div>
                        <div id="batteryPercent">0%</div>
                    </div>
                    <div class="sensor-update" id="sensorUpdate">Last update: --</div>
                </div>
                <div class="nodes-section">
                    <div class="nodes-header">
                        <span>🖥️</span> Network Nodes
                        <span id="nodesCountBadge" style="font-size: 12px; color: #999;"></span>
                    </div>
                    <div id="nodesList"></div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let selectedNodeId = null;
        let selectedNodeName = null;

        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function clearFilter() {
            selectedNodeId = null;
            selectedNodeName = null;
            document.getElementById('filterBar').classList.remove('show');
            loadMessages();
        }

        function updateFilterBar() {
            const bar = document.getElementById('filterBar');
            const text = document.getElementById('filterText');
            
            if (!selectedNodeId) {
                bar.classList.remove('show');
                return;
            }
            
            bar.classList.add('show');
            text.textContent = `💬 Filtered: ${selectedNodeName}`;
        }

        async function loadSensors() {
            try {
                const response = await fetch('/api/sensors');
                const data = await response.json();
                
                document.getElementById('tempValue').textContent = data.temperature !== null ? data.temperature.toFixed(1) : '--';
                document.getElementById('humValue').textContent = data.humidity !== null ? data.humidity.toFixed(1) : '--';
                document.getElementById('presValue').textContent = data.pressure !== null ? Math.round(data.pressure) : '--';
                document.getElementById('voltValue').textContent = data.voltage !== null ? data.voltage.toFixed(2) : '--';
                document.getElementById('currValue').textContent = data.current !== null ? Math.round(data.current) : '--';
                document.getElementById('powValue').textContent = data.power !== null ? Math.round(data.power) : '--';
                
                if (data.battery_percent !== null) {
                    const batteryIndicator = document.getElementById('batteryIndicator');
                    batteryIndicator.style.display = 'block';
                    const batteryFill = document.getElementById('batteryFill');
                    const batteryPercent = document.getElementById('batteryPercent');
                    batteryFill.style.width = data.battery_percent + '%';
                    batteryPercent.textContent = data.battery_percent + '%';
                    
                    // Change color based on battery level
                    if (data.battery_percent < 20) {
                        batteryFill.style.background = '#f44336';
                    } else if (data.battery_percent < 50) {
                        batteryFill.style.background = '#ff9800';
                    } else {
                        batteryFill.style.background = '#4caf50';
                    }
                }
                
                if (data.last_update) {
                    document.getElementById('sensorUpdate').textContent = `Last update: ${data.last_update}`;
                }
            } catch (error) {
                console.error('Error loading sensors:', error);
            }
        }

        async function loadMessages() {
            let url = '/api/messages';
            if (selectedNodeId) {
                url += '?node_id=' + encodeURIComponent(selectedNodeId);
            }
            
            try {
                const response = await fetch(url);
                const data = await response.json();
                
                document.getElementById('statusText').innerHTML = data.status === 'radio: listening' ? '🟢 Radio active' : '🟡 Sending...';
                document.getElementById('nodeCount').innerHTML = `📡 ${data.nodes.length} nodes online`;
                document.getElementById('nodesCountBadge').textContent = `(${data.nodes.length})`;
                
                const container = document.getElementById('messagesContainer');
                const shouldScroll = container.scrollTop + container.clientHeight >= container.scrollHeight - 100;
                
                if (data.messages.length === 0) {
                    container.innerHTML = '<div class="loading">💬 No messages yet. Be the first to send one!</div>';
                } else {
                    container.innerHTML = data.messages.map(msg => `
                        <div class="message ${msg.kind}">
                            <div class="bubble">
                                <div class="sender">${escapeHtml(msg.sender)}</div>
                                <div class="text">${escapeHtml(msg.text)}</div>
                                <div class="time">${escapeHtml(msg.time)}</div>
                            </div>
                        </div>
                    `).join('');
                }
                
                if (shouldScroll) {
                    container.scrollTop = container.scrollHeight;
                }
                
                const nodesList = document.getElementById('nodesList');
                nodesList.innerHTML = data.nodes.map(node => `
                    <div class="node-card ${selectedNodeId === node.node_id ? 'selected' : ''}" onclick="selectNode('${escapeHtml(node.node_id)}', '${escapeHtml(node.clean_name)}')">
                        <div class="node-name">
                            ${node.name}
                            <span class="badge ${node.signal_quality === 'good' ? 'badge-online' : 'badge-offline'}">${node.signal_quality === 'good' ? '●' : '○'}</span>
                        </div>
                        <div class="node-id">${escapeHtml(node.node_id)}</div>
                        <div class="node-meta">${escapeHtml(node.meta)}</div>
                        ${node.last_text ? `<div class="node-last-text" style="font-size: 11px; color: #1e3c72; margin-top: 6px; font-style: italic;">📝 ${escapeHtml(node.last_text.substring(0, 50))}${node.last_text.length > 50 ? '...' : ''}</div>` : ''}
                    </div>
                `).join('');
                
                updateFilterBar();
            } catch (error) {
                console.error('Error loading messages:', error);
            }
        }
        
        function selectNode(nodeId, nodeName) {
            if (selectedNodeId === nodeId) {
                clearFilter();
            } else {
                selectedNodeId = nodeId;
                selectedNodeName = nodeName;
                loadMessages();
            }
        }
        
        document.getElementById('sendForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const input = document.getElementById('messageInput');
            const text = input.value.trim();
            
            if (!text) return;
            
            const button = e.target.querySelector('button');
            button.disabled = true;
            button.textContent = 'Sending...';
            
            try {
                const response = await fetch('/api/send', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text})
                });
                
                if (response.ok) {
                    input.value = '';
                    loadMessages();
                }
            } catch (error) {
                console.error('Error sending message:', error);
            } finally {
                button.disabled = false;
                button.textContent = 'Send 📡';
                input.focus();
            }
        });
        
        // Auto-refresh every 2 seconds for messages, 10 seconds for sensors
        setInterval(() => {
            loadMessages();
        }, 2000);
        
        setInterval(() => {
            loadSensors();
        }, 10000);
        
        // Initial load
        loadMessages();
        loadSensors();
        
        // Focus input on load
        setTimeout(() => {
            document.getElementById('messageInput').focus();
        }, 100);
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
            "voltage": None,
            "channel_utilization": None,
            "air_util_tx": None,
            "uptime_seconds": None,
            "last_update": None
        }

def read_sensors_from_meshtastic():
    """Read sensor data from RAK4631 via Meshtastic telemetry"""
    try:
        # Try to get telemetry data
        result = subprocess.run(
            [MESHTASTIC_CMD, "--get", "telemetry"],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        output = result.stdout + result.stderr
        print(f"Telemetry output: {output[:500]}")  # Debug output
        
        # Parse environment metrics (BME280)
        # Look for various formats
        temp_match = re.search(r'(?:temperature|temp)[:\s=]+(-?\d+\.?\d*)', output, re.IGNORECASE)
        hum_match = re.search(r'(?:humidity|hum)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        press_match = re.search(r'(?:pressure)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        
        # Parse power metrics (INA226)
        volt_match = re.search(r'(?:voltage)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        curr_match = re.search(r'(?:current)[:\s=]+(\d+\.?\d*)', output, re.IGNORECASE)
        
        # Parse battery percentage if available
        batt_match = re.search(r'(?:battery|bat)[:\s=]+(\d+\.?\d*)%?', output, re.IGNORECASE)
        
        if temp_match:
            sensor_data["temperature"] = float(temp_match.group(1))
            print(f"Temperature: {sensor_data['temperature']}°C")
        if hum_match:
            sensor_data["humidity"] = float(hum_match.group(1))
            print(f"Humidity: {sensor_data['humidity']}%")
        if press_match:
            sensor_data["pressure"] = float(press_match.group(1))
            print(f"Pressure: {sensor_data['pressure']} hPa")
        if volt_match:
            sensor_data["voltage"] = float(volt_match.group(1))
            print(f"Voltage: {sensor_data['voltage']}V")
        if curr_match:
            sensor_data["current"] = float(curr_match.group(1))
            print(f"Current: {sensor_data['current']}mA")
        if batt_match:
            sensor_data["battery_percent"] = float(batt_match.group(1))
            print(f"Battery: {sensor_data['battery_percent']}%")
        
        # Calculate power (mW)
        if sensor_data["voltage"] is not None and sensor_data["current"] is not None:
            sensor_data["power"] = sensor_data["voltage"] * sensor_data["current"]
        
        # If we got any data, update the timestamp
        if any([sensor_data["temperature"], sensor_data["humidity"], sensor_data["pressure"], 
                sensor_data["voltage"], sensor_data["current"]]):
            sensor_data["last_update"] = now()
            save_sensors()
        
    except subprocess.TimeoutExpired:
        print("Sensor read timeout")
    except Exception as e:
        print(f"Error reading sensors: {e}")

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
            "hw_model": fixed_hw_model(node_id, old.get("hw_model", ""))
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
        "hw_model": fixed_hw_model(node_id, old.get("hw_model", ""))
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
            time.sleep(1)

            if listen_process.poll() is None:
                listen_process.kill()
                time.sleep(1)
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
            timeout=30
        )

        output = result.stdout + result.stderr

        metrics_pos = output.find('"deviceMetrics"')
        if metrics_pos < 0:
            print("Base status: deviceMetrics not found")
            return

        block_start = output.find("{", metrics_pos)
        block_end = output.find("}", block_start)

        if block_start < 0 or block_end < 0:
            print("Base status: deviceMetrics block not found")
            return

        metrics_text = output[block_start:block_end + 1]
        metrics = json.loads(metrics_text)

        base_status = {
            "battery_level": metrics.get("batteryLevel"),
            "voltage": metrics.get("voltage"),
            "channel_utilization": metrics.get("channelUtilization"),
            "air_util_tx": metrics.get("airUtilTx"),
            "uptime_seconds": metrics.get("uptimeSeconds"),
            "last_update": now()
        }

        print("Base status updated:", base_status)

    except Exception as e:
        print("Base status update error:", e)
def base_status_worker():
    while True:
        update_base_status_from_info()
        time.sleep(120)        

def listen_meshtastic():
    global listen_process

    nodeinfo_buffer = []
    collecting_nodeinfo = False
    
    # Start periodic sensor reading every 10 seconds
    last_sensor_read = 0

    while True:
        if pause_listen.is_set():
            time.sleep(0.5)
            continue
            
        # Read sensors every 10 seconds
        current_time = time.time()
        if current_time - last_sensor_read >= 10:
            read_sensors_from_meshtastic()
            last_sensor_read = current_time

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
                    "hw_model": fixed_hw_model(LOCAL_NODE_ID, old.get("hw_model", ""))
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
    load_sensors_data()
    ensure_known_nodes()
    update_base_status_from_info()
    
    # Start background sensor reading thread
    sensor_thread = threading.Thread(target=read_sensors_from_meshtastic, daemon=True)
    sensor_thread.start()

    base_status_thread = threading.Thread(target=base_status_worker, daemon=True)
    base_status_thread.start()

    t = threading.Thread(target=listen_meshtastic, daemon=True)
    t.start()

    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
