#!/usr/bin/env python3
"""
Configuration file for Meshtastic Web UI
"""

# ===== SERVER SETTINGS =====
APP_HOST = "0.0.0.0"
APP_PORT = 5000

# ===== MESHTASTIC SETTINGS =====
MESHTASTIC_CMD = "/home/user/.local/bin/meshtastic"

# ===== YOUR NODE SETTINGS =====
LOCAL_NODE_ID = "!your_node_id"
LOCAL_NODE_NAME = "Your Node Name"

# ===== DATA STORAGE =====
DATA_DIR = "/home/flint/mesh_web/data"

# ===== FILE PATHS =====
HISTORY_FILE = f"{DATA_DIR}/messages.json"
NODES_FILE = f"{DATA_DIR}/nodes.json"
SENSORS_FILE = f"{DATA_DIR}/sensors.json"
CHATS_FILE = f"{DATA_DIR}/chats.json"

MAX_HISTORY_MESSAGES = 1000
CHANNEL_CHAT_ID = "channel"
CHANNEL_CHAT_NAME = "LongFast Channel 0"

# ===== ИЗВЕСТНЫЕ УЗЛЫ =====
# Optional: preconfigured known nodes
KNOWN_NODES = {
    "!your_node_id": "Your Node"
}

KNOWN_NODE_INFO = {
    "!your_node_id": {
        "short_name": "NODE",
        "hw_model": "RAK4631"
    }
}

