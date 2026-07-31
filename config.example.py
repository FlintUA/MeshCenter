#!/usr/bin/env python3
"""
Configuration file for MeshCenter
Copy this file to config.py and edit your settings
"""

from pathlib import Path

# Resolve installation-specific paths automatically. This keeps the example
# valid when MeshCenter is installed under a user other than "pi".
PROJECT_DIR = Path(__file__).resolve().parent

# ===== SERVER SETTINGS =====
APP_HOST = "0.0.0.0"
APP_PORT = 5000

# ===== MESHTASTIC SETTINGS =====
MESHTASTIC_CMD = str(PROJECT_DIR / "venv" / "bin" / "meshtastic")

# ===== SERIAL PORT =====
MESHTASTIC_PORT = "/dev/ttyACM0"  # or /dev/ttyUSB0

# ===== YOUR NODE SETTINGS =====
LOCAL_NODE_ID = "!xxxxxxxx"        # Your Meshtastic node ID
LOCAL_NODE_NAME = "My Meshtastic"  # Your node display name

# ===== DATA STORAGE =====
DATA_DIR = str(PROJECT_DIR / "data")

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

# ===== WEATHER / OPENWEATHER =====
# Keep the real API key in weather_secrets.py (ignored by Git).
try:
    from weather_secrets import OPENWEATHER_API_KEY
except ImportError:
    OPENWEATHER_API_KEY = ""

# Optional static fallback. Leave unset and choose a reference location in the
# web Settings, or enter fallback coordinates here.
WEATHER_LATITUDE = None
WEATHER_LONGITUDE = None
WEATHER_LOCATION_NAME = ""
WEATHER_LANGUAGE = "en"
WEATHER_CACHE_SECONDS = 600
