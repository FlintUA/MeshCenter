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
# Empty enables safe runtime discovery; an explicit absolute path is also accepted.
MESHTASTIC_CMD = ""

# ===== SERIAL PORT =====
MESHTASTIC_PORT = "/dev/ttyACM0"  # or /dev/ttyUSB0

# ===== YOUR NODE SETTINGS =====
LOCAL_NODE_ID = "!xxxxxxxx"        # Your Meshtastic node ID
LOCAL_NODE_NAME = "My Meshtastic"  # Your node display name
INSTANCE_NAME = ""                 # Optional MeshCenter installation name; defaults to hostname

# ===== DATA STORAGE =====
DATA_DIR = str(PROJECT_DIR / "data")

# ===== LEGACY / BOOTSTRAP FILE PATHS =====
# MeshCenter redirects radio-specific files into data/profiles/<node-id>/ at runtime.
HISTORY_FILE = f"{DATA_DIR}/messages.json"
NODES_FILE = f"{DATA_DIR}/nodes.json"
SENSORS_FILE = f"{DATA_DIR}/sensors.json"
CHATS_FILE = f"{DATA_DIR}/chats.json"

# ===== MESSAGE SETTINGS =====
MAX_HISTORY_MESSAGES = 1000
CHANNEL_CHAT_ID = "channel"
CHANNEL_CHAT_NAME = "LongFast"  # without index — the code appends [0] itself

# ===== MCAttach CONTROL CHANNEL =====
# The Meshtastic channel index MCAttach's DIRECT control traffic uses
# (KEY_REQUEST / KEY_ANNOUNCE / OFFER / ACK / CANCEL / REJECTED / EXPIRED).
# These are still DIRECT node-to-node messages (RouteType.DIRECT, a specific
# destination node, not a channel broadcast) - this only selects which
# channel *index* the radio transmits them on, exactly like a normal chat
# message's channel selection. Valid range is 0-7 (Meshtastic's 8 channels).
# 0 is the primary channel (usually LongFast) - a *public* channel, which is
# why MCA control traffic should be moved off it. Set this to a private
# channel's index (e.g. 1 for a "Flint-pvt" secondary channel) so MCA key/
# transfer control never transmits on the public channel. A value outside
# 0-7, or an index whose channel does not exist on the connected radio, is a
# hard error (transmission is blocked) - MCAttach never silently falls back
# to channel 0. This is a dedicated MCA setting, independent of
# CHANNEL_CHAT_ID/CHANNEL_CHAT_NAME (ordinary chat keeps its own channel).
MCA_CONTROL_CHANNEL_INDEX = 0

# ===== KNOWN NODES (pre-populated with your mesh) =====
KNOWN_NODES = {
    "!xxxxxxxx": "My Node",
}

KNOWN_NODE_INFO = {
    "!xxxxxxxx": {"short_name": "MYND", "hw_model": "RAK4631"},
}

# ===== WEATHER =====
# Keep the real API keys in weather_secrets.py (ignored by Git). Only the key
# for the provider chosen in web Settings -> Weather Provider is actually used.
try:
    from weather_secrets import OPENWEATHER_API_KEY
except ImportError:
    OPENWEATHER_API_KEY = ""

try:
    from weather_secrets import WEATHERAPI_API_KEY
except ImportError:
    WEATHERAPI_API_KEY = ""

# Which provider is active on first startup, before any choice is saved to
# settings.json. Must be "openweather" or "weatherapi".
WEATHER_PROVIDER = "openweather"

# Optional static fallback. Leave unset and choose a reference location in the
# web Settings, or enter fallback coordinates here.
WEATHER_LATITUDE = None
WEATHER_LONGITUDE = None
WEATHER_LOCATION_NAME = ""
WEATHER_LANGUAGE = "en"
WEATHER_CACHE_SECONDS = 600

# ===== E-PAPER DISPLAY (experimental, feature/epaper-display branch) =====
# Off by default for every install. Only set True on hardware with a
# Waveshare 2.13" e-Paper HAT (G) actually wired up - see the e-Paper Stage
# 1 plan and modules/display/ for details.
EPAPER_ENABLED = False

# ===== OPTIONAL PASSWORD PROTECTION =====
# On by default - MeshCenter is intended for a trusted local network, but a
# guest Wi-Fi/VPN/accidental port-forward can expose it, so a single shared
# password guards the whole app out of the box. With AUTH_ENABLED=True and
# no AUTH_PASSWORD_HASH set (the case here), MeshCenter generates a random
# one-time password the first time it starts with no data/auth.json yet -
# printed once to the console/log and saved to data/initial_password.txt
# (owner-only permissions) - log in with it and change it via
# Settings -> Security. AUTH_PASSWORD_HASH is only ever used to seed
# data/auth.json (it becomes the source of truth afterwards, editable from
# Settings -> Security) - once auth.json exists these two variables are
# ignored, including on every later restart.
# Set AUTH_ENABLED = False here to skip protection (and generation)
# entirely - the old, still-supported opt-out. Set AUTH_PASSWORD_HASH
# explicitly only if you want to pre-seed a specific password instead of
# getting a generated one; leaving it set on an existing install (one
# that already has data/auth.json) does nothing either way, per above.
# Generate a hash with:
#   python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your-password'))"
AUTH_ENABLED = True
AUTH_PASSWORD_HASH = ""

# ===== SESSION COOKIE SECURITY =====
# Controls the Secure flag on the session cookie. False (the default) lets
# MeshCenter run over plain HTTP on a trusted local network - the cookie is
# still HttpOnly and SameSite=Lax, so it is unreachable from JavaScript and
# restricted to same-site requests. Set True only when serving behind HTTPS:
# the browser then sends the cookie exclusively over encrypted connections.
# Do not set True on a plain-HTTP deployment, or login/session state breaks.
SESSION_COOKIE_SECURE = False
