#!/usr/bin/env python3
"""
Configuration file for MeshCenter
Copy this file to config.py and edit your settings
"""

# ===== SERVER SETTINGS =====
APP_HOST = "0.0.0.0"
APP_PORT = 5000

# ===== MESHTASTIC SETTINGS =====
MESHTASTIC_CMD = "/home/pi/.local/bin/meshtastic"  # Find with: which meshtastic

# ===== YOUR NODE SETTINGS =====
LOCAL_NODE_ID = "!xxxxxxxx"        # Your Meshtastic node ID
LOCAL_NODE_NAME = "My Meshtastic"  # Your node display name

# ===== DATA STORAGE =====
DATA_DIR = "/home/pi/mesh_web/data"

# ===== FILE PATHS (auto-generated from DATA_DIR) =====
HISTORY_FILE = f"{DATA_DIR}/messages.json"
NODES_FILE = f"{DATA_DIR}/nodes.json"
SENSORS_FILE = f"{DATA_DIR}/sensors.json"
CHATS_FILE = f"{DATA_DIR}/chats.json"

# ===== MESSAGE SETTINGS =====
MAX_HISTORY_MESSAGES = 1000
CHANNEL_CHAT_ID = "channel"
CHANNEL_CHAT_NAME = "LongFast Channel 0"

# ===== KNOWN NODES (pre-populated with your mesh) =====
KNOWN_NODES = {
    "!xxxxxxxx": "My Node",
}

KNOWN_NODE_INFO = {
    "!xxxxxxxx": {"short_name": "MYND", "hw_model": "RAK4631"},
}