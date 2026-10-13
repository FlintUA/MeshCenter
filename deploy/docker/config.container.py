"""MeshCenter configuration for the Docker image.

Copied to /config/config.py on first start ONLY if that file does not exist -
afterwards it is yours; the image never overwrites it. Based on
config.example.py, but paths come from the environment (the image sets
MESHCENTER_DATA_DIR=/data) and password protection is switched on explicitly.
"""
import os

APP_HOST = "0.0.0.0"          # required inside a container; publish via compose `ports`
APP_PORT = 5000

# The radio is TCP-only in the container and is configured in the web UI
# (Node Manager); these are only the bootstrap placeholders the app expects.
MESHTASTIC_CMD = ""            # empty = use the adapter venv's CLI
MESHTASTIC_PORT = "/dev/ttyACM0"
LOCAL_NODE_ID = "!xxxxxxxx"
LOCAL_NODE_NAME = "My Meshtastic"
INSTANCE_NAME = os.environ.get("MESHCENTER_INSTANCE_NAME", "")

DATA_DIR = os.environ.get("MESHCENTER_DATA_DIR", "/data")
HISTORY_FILE = f"{DATA_DIR}/messages.json"
NODES_FILE = f"{DATA_DIR}/nodes.json"
SENSORS_FILE = f"{DATA_DIR}/sensors.json"
CHATS_FILE = f"{DATA_DIR}/chats.json"

MAX_HISTORY_MESSAGES = 1000
CHANNEL_CHAT_ID = "channel"
CHANNEL_CHAT_NAME = "LongFast"
MCA_CONTROL_CHANNEL_INDEX = None

KNOWN_NODES = {"!xxxxxxxx": "My Node"}
KNOWN_NODE_INFO = {"!xxxxxxxx": {"short_name": "MYND", "hw_model": "RAK4631"}}

# API keys live in /config/weather_secrets.py (written by the web UI).
try:
    from weather_secrets import OPENWEATHER_API_KEY
except ImportError:
    OPENWEATHER_API_KEY = ""
try:
    from weather_secrets import WEATHERAPI_API_KEY
except ImportError:
    WEATHERAPI_API_KEY = ""

WEATHER_PROVIDER = "openweather"
WEATHER_LATITUDE = None
WEATHER_LONGITUDE = None
WEATHER_LOCATION_NAME = ""
WEATHER_LANGUAGE = "en"
WEATHER_CACHE_SECONDS = 600

EPAPER_ENABLED = False         # no GPIO/SPI in a container

# Password protection ON: the first visit lands on /setup to choose a password.
# Complete that step immediately - /setup is open to anyone who can reach the port.
AUTH_ENABLED = True
AUTH_PASSWORD_HASH = ""
SESSION_COOKIE_SECURE = False  # plain HTTP on a LAN; set True only behind HTTPS
