#!/usr/bin/env python3
"""
Configuration file for Meshtastic Web UI
"""

# ===== SERVER SETTINGS =====
APP_HOST = "0.0.0.0"
APP_PORT = 5000

# ===== MESHTASTIC SETTINGS =====
MESHTASTIC_CMD = "/home/flint/.local/bin/meshtastic"

# ===== YOUR NODE SETTINGS =====
LOCAL_NODE_ID = "!067a40fa"
LOCAL_NODE_NAME = "Flint Base"

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

