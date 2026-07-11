# MeshCenter

> This document was converted to GitHub Markdown format.
> Image links and emoji from the original README have been preserved where available.

# MeshCenter

<p align="center">
  <strong>Browser-based control center for a Meshtastic® base station running on Raspberry Pi</strong>
</p>

<p align="center">
  <img src="docs/images/meshcenter001.png" width="480" alt="MeshCenter logo">
</p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#installation">Installation</a> •
  <a href="#usage">Usage</a> •
  <a href="#api">API</a> •
  <a href="#development">Development</a> •
  <a href="#troubleshooting">Troubleshooting</a>
</p>

<p align="center">
  <img src="https://img.shields.io/github/v/release/FlintUA/MeshCenter" alt="Release">
  <img src="https://img.shields.io/github/license/FlintUA/MeshCenter" alt="License">
  <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/Raspberry%20Pi-Bookworm-C51A4A" alt="Platform">
  <img src="https://img.shields.io/badge/Meshtastic-Compatible-success" alt="Meshtastic">
  <img src="https://img.shields.io/badge/Status-Active%20Development-brightgreen" alt="Status">
</p>

> [!IMPORTANT]
> MeshCenter is under active development. It is intended for **trusted local networks** and currently has **no built‑in user authentication**. **Do not expose port `5000` directly to the Internet.**  
> If you need remote access, use a VPN (WireGuard, Tailscale) or an authenticated reverse proxy.

---

## Overview

MeshCenter is a lightweight Flask web application that turns a Raspberry Pi into a permanent, browser‑accessible control center for a USB‑connected Meshtastic node.

It complements the official Meshtastic applications – the official apps remain the recommended tools for firmware installation and full radio configuration. MeshCenter focuses on **day‑to‑day operation** of a fixed or portable station:

- Public‑channel and direct messaging
- Automatic node discovery and node management
- Device, environmental and power telemetry with historical charts and export
- Raspberry Pi Camera live view, photo capture and local gallery
- Raspberry Pi system and network information
- Wi‑Fi scanning, connection and saved‑profile management
- Radio health monitoring and persistent event logging
- Controlled listener, service, reboot and shutdown actions

The primary target is Raspberry Pi Zero 2W, but Raspberry Pi 3, 4 and 5 also work.

---

## Features

### 💬 Messaging
- LongFast/channel messaging on channel index `0`
- Direct messages to individual node IDs
- Local chat history with unread counters
- Emoji picker
- Separate channel and direct‑message conversations
- Ignored‑node handling
- Automatic refresh and local cache invalidation after sending

### 📡 Nodes
- Node discovery from Meshtastic listener output and `meshtastic --info`
- Long name, short name, node ID, hardware model and role
- Last‑seen time, RSSI, SNR, hop information and relay data when available
- Favourites and ignore list
- CSV and JSON import/export
- Duplicate detection and merge tools
- Rescan and cleanup actions

### 📊 Telemetry
- Temperature, relative humidity and barometric pressure
- Voltage, current and calculated power
- Battery estimate, channel utilisation, air utilisation and node uptime
- Configurable history interval (5 min to 1 hour)
- Historical charts for environment and power (with zoomable time ranges)
- CSV/JSON telemetry export
- Local history storage with bounded retention (up to 26 000 records)

Typical tested sensors include BME280‑class environmental telemetry and INA226‑class power telemetry when exposed by the Meshtastic node.

### 🎥 Camera and Gallery
- Raspberry Pi Camera support through Picamera2
- Browser‑compatible MJPEG live stream with adjustable resolution, FPS and JPEG quality
- Separate video and still‑photo settings
- High‑resolution photo capture (up to 3280×2464)
- Local gallery with individual and bulk deletion
- Dated screenshot directory structure (`YYYY/MM/DD/`)
- Automatic gallery cleanup (size and age limits)

Photos are stored locally on the Raspberry Pi. They are **not** transmitted over Meshtastic.

### 🖥️ System Tab (new)
- **System information**: hostname, uptime, CPU temperature, load average, RAM, disk usage, Raspberry Pi model, OS, kernel version
- **Network information**: current SSID, RSSI, signal percentage, RX/TX bitrate, IP, gateway, Internet reachability
- **Wi‑Fi Manager**:
  - Scan nearby networks (uses `iw` and `nmcli`)
  - Connect to a network with password (supports saved profiles)
  - Forget saved profiles
- **Radio Health**:
  - Listener running state, last packet/telemetry/send age
  - Restart counter, diagnostic level (OK/WARNING/ERROR)
  - Recommendation text for troubleshooting
  - **System Log**: persistent JSONL event log (visible in the UI) with date, time, level, source, event and details
- **System Actions**:
  - Restart Meshtastic listener
  - Restart MeshCenter service
  - Reboot Raspberry Pi
  - Shutdown Raspberry Pi

All system actions are protected by narrowly scoped `sudo` permissions and require explicit confirmation.

---

## Screenshots

> Click to enlarge. The interface is continuously improving.

| Chat & Nodes | Telemetry | Camera | System |
|--------------|-----------|--------|--------|
| <img width="300" src="https://github.com/user-attachments/assets/55839039-c167-489e-8122-616cfa56f1af" alt="Chat"> | <img width="300" src="https://github.com/user-attachments/assets/d3922ae4-0bb3-4a37-9757-af0a5572cf45" alt="Telemetry"> | <img width="300" src="https://github.com/user-attachments/assets/7d9dedd0-9013-45c6-9aaa-fe3a59eef8ab" alt="Camera"> | <img width="300" src="https://github.com/user-attachments/assets/92e09683-3cd7-4dad-9af5-edfa25a050ca" alt="System"> |

More screenshots:  
- [Main screen](https://github.com/user-attachments/assets/92e09683-3cd7-4dad-9af5-edfa25a050ca)  
- [Chat and nodes](https://github.com/user-attachments/assets/55839039-c167-489e-8122-616cfa56f1af)  
- [Sidebar](https://github.com/user-attachments/assets/84e56a8d-d65f-4877-a9e1-425c7da095cb)  
- [Environment telemetry](https://github.com/user-attachments/assets/d3922ae4-0bb3-4a37-9757-af0a5572cf45)  
- [Power telemetry](https://github.com/user-attachments/assets/ac1bb3ec-21a7-498b-8279-a5735cfe59c8)  
- [Live camera](<img width="1396" height="1295" alt="photo002" src="https://github.com/user-attachments/assets/de30ce3a-3c48-4c17-8d5c-515a4b50a087" /)
- [Photo gallery](https://github.com/user-attachments/assets/b129ac36-40eb-4ca5-883f-918760f7d5be)
<img width="1396" height="1293" alt="system001" src="https://github.com/user-attachments/assets/bfea66e7-7e27-425c-b5fe-05305a4d2431" />

---

## Installation

The instructions below describe a clean installation on **Raspberry Pi OS Bookworm** (32‑bit or 64‑bit). Commands use the example user `pi`. Replace `pi` and `/home/pi` with your actual Linux user and home directory.

### 1. Hardware requirements

**Required:**
- Raspberry Pi Zero 2W, 3, 4 or 5
- Reliable microSD card (16 GB minimum, 32 GB recommended)
- Stable power supply
- Wi‑Fi or Ethernet connection
- Meshtastic‑compatible device connected by USB serial (tested with `/dev/ttyACM0`)

**Optional:**
- Raspberry Pi Camera supported by Picamera2 (official or third‑party)
- Meshtastic environmental or power telemetry sensors

### 2. Prepare Raspberry Pi OS

Update the system:
```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
Install base packages:

bash
sudo apt install -y git python3 python3-venv python3-pip python3-pil iw wireless-tools network-manager
For camera support:

bash
sudo apt install -y python3-picamera2 rpicam-apps
(On older Bookworm images the package may be named libcamera-apps instead of rpicam-apps.)

3. Enable serial access for your user
Add your user to the dialout group:

bash
sudo usermod -aG dialout "$USER"
Log out and back in, or reboot, before continuing:

bash
sudo reboot
Connect the Meshtastic device and identify its serial port:

bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
Expected output: /dev/ttyACM0 (or similar). Update MESHTASTIC_PORT in config.py accordingly.

4. Clone MeshCenter
bash
cd ~
git clone https://github.com/FlintUA/MeshCenter.git
cd MeshCenter
5. Create a virtual environment
Use --system-site-packages to allow access to the system‑installed Picamera2:

bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
Install dependencies and the Meshtastic CLI:

bash
python -m pip install -r requirements.txt
python -m pip install --upgrade meshtastic
Check that the CLI is in the virtual environment:

bash
which meshtastic   # should point to .../MeshCenter/venv/bin/meshtastic
meshtastic --version
6. Test the radio
With the virtual environment active, test communication:

bash
meshtastic --port /dev/ttyACM0 --info
Record the local node ID (e.g. !067a40fa) and the long name from the output. Do not continue until this command works without permission or serial‑port errors.

7. Create config.py
bash
cp config.example.py config.py
nano config.py
A practical configuration looks like this (replace paths and IDs):

python
APP_HOST = "0.0.0.0"
APP_PORT = 5000

MESHTASTIC_CMD = "/home/pi/MeshCenter/venv/bin/meshtastic"
MESHTASTIC_PORT = "/dev/ttyACM0"

LOCAL_NODE_ID = "!xxxxxxxx"
LOCAL_NODE_NAME = "My Base Station"

DATA_DIR = "/home/pi/MeshCenter/data"

HISTORY_FILE = f"{DATA_DIR}/messages.json"
NODES_FILE = f"{DATA_DIR}/nodes.json"
SENSORS_FILE = f"{DATA_DIR}/sensors.json"
CHATS_FILE = f"{DATA_DIR}/chats.json"

MAX_HISTORY_MESSAGES = 1000
CHANNEL_CHAT_ID = "channel"
CHANNEL_CHAT_NAME = "LongFast Channel 0"

KNOWN_NODES = {
    "!xxxxxxxx": "My Base Station",
}

KNOWN_NODE_INFO = {
    "!xxxxxxxx": {
        "short_name": "BASE",
        "hw_model": "RAK4631",
    },
}
Create the data directory:

bash
mkdir -p /home/pi/MeshCenter/data
[!NOTE]
config.py contains installation‑specific paths and should never be committed to version control.

8. Optional camera test
Check that the camera is detected:

bash
rpicam-hello --list-cameras
Test imports from the virtual environment:

bash
source ~/MeshCenter/venv/bin/activate
python - <<'PY'
from PIL import Image
print("Pillow: OK")
try:
    from picamera2 import Picamera2
    print("Picamera2: OK")
except Exception as exc:
    print("Picamera2: ERROR:", exc)
PY
MeshCenter can run without a camera; camera endpoints will report that the camera is unavailable.

9. Run MeshCenter manually (first test)
bash
cd ~/MeshCenter
source venv/bin/activate
python server.py
Open from another device on the same network:

text
http://<raspberry-pi-ip>:5000
Find the IP with hostname -I. Stop the manual server with Ctrl+C before creating the systemd service.

Running as a systemd service (production daemon)
1. Create the service file
bash
sudo nano /etc/systemd/system/meshcenter.service
Paste the following (adjust paths and user):

ini
[Unit]
Description=MeshCenter
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/home/pi/MeshCenter
Environment="PATH=/home/pi/MeshCenter/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/pi/MeshCenter/venv/bin/python /home/pi/MeshCenter/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
Load and enable:

bash
sudo systemctl daemon-reload
sudo systemctl enable --now meshcenter.service
Check status and logs:

bash
systemctl status meshcenter.service --no-pager
journalctl -u meshcenter.service -n 100 --no-pager
2. Verify service user permissions
The service user (pi in the example) must:

Own (or be able to write to) the data directory

Belong to the dialout group for USB serial access

Have the sudo permissions described below for Wi‑Fi and system actions

Set ownership:

bash
sudo chown -R pi:pi /home/pi/MeshCenter
Check group membership:

bash
id pi
Production deployment (advanced)
While the systemd service described above runs MeshCenter directly with Python’s built‑in server.py, for higher load or multiple concurrent users you can use a production WSGI server.

Option A: Use Gunicorn (recommended)
Install Gunicorn in the virtual environment:

bash
source ~/MeshCenter/venv/bin/activate
pip install gunicorn
Create a WSGI entry point (e.g. wsgi.py – already present in the project root):

python
from server import app as application
Then run Gunicorn (adjust bind address and workers):

bash
gunicorn --bind 0.0.0.0:5000 --workers 3 wsgi:application
For systemd, create a separate service file that uses Gunicorn.

Option B: Nginx as reverse proxy (optional)
If you want to serve static files faster and add HTTPS, place Nginx in front:

Configure Nginx to proxy requests to http://127.0.0.1:5000

Serve static files directly from ~/MeshCenter/static/

Add SSL/TLS termination

Example Nginx snippet:

nginx
location / {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
location /static/ {
    alias /home/pi/MeshCenter/static/;
}
Wi‑Fi Manager permissions
The Wi‑Fi Manager calls iw and nmcli via sudo without a password. Add the following sudoers rule:

bash
sudo visudo -f /etc/sudoers.d/meshcenter-wifi
Add this line (replace pi with your service user):

text
pi ALL=(root) NOPASSWD: /usr/sbin/iw, /usr/bin/nmcli
Set permissions and validate:

bash
sudo chmod 440 /etc/sudoers.d/meshcenter-wifi
sudo visudo -c
Test the commands:

bash
sudo -n /usr/sbin/iw dev wlan0 link
sudo -n /usr/bin/nmcli connection show
[!NOTE]
The code expects the wireless interface to be wlan0 and uses NetworkManager. Adjust if your interface has a different name.

System action permissions
The following actions require sudo:

Restart MeshCenter service

Reboot Raspberry Pi

Shutdown Raspberry Pi

Create a separate sudoers file:

bash
sudo visudo -f /etc/sudoers.d/meshcenter
Add (replace pi with your user):

text
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart meshcenter.service
pi ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
pi ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
Validate:

bash
sudo chmod 440 /etc/sudoers.d/meshcenter
sudo visudo -c
sudo -n -l
Test only the restart command:

bash
sudo -n /usr/bin/systemctl restart meshcenter.service
systemctl status meshcenter.service --no-pager
First‑run verification checklist
After installation, verify these items in order:

meshtastic --port /dev/ttyACM0 --info reads the radio.

systemctl status meshcenter.service reports active (running).

The web interface loads at http://<raspberry-pi-ip>:5000.

Chats and nodes appear.

A channel test message can be sent and received.

Direct messages work with a reachable node.

Telemetry updates appear when the local node transmits telemetry.

The System tab shows Raspberry Pi and network information.

Wi‑Fi scan works without a password prompt.

Restart Listener creates events in the System Log.

Restart MeshCenter restarts the service and returns to active (running).

Camera live view and capture work when a camera is installed.

Quick API checks:

bash
curl -s http://127.0.0.1:5000/api/system/info | python3 -m json.tool
curl -s http://127.0.0.1:5000/api/system/network | python3 -m json.tool
curl -s http://127.0.0.1:5000/api/radio_health | python3 -m json.tool
curl -s http://127.0.0.1:5000/api/system/log | python3 -m json.tool
Data and backups
MeshCenter stores all persistent data under DATA_DIR (default /home/pi/MeshCenter/data). Important files:

text
data/
├── camera_config.json      # Camera settings
├── chats.json              # Chat metadata & unread counts
├── deleted_dm.json         # List of deleted DM chats (soft deletion)
├── messages.json           # Chat message history
├── nodes.json              # Discovered and imported nodes
├── sensors.json            # Latest sensor values
├── settings.json           # User preferences (units)
├── system_events.jsonl     # Persistent system log (JSONL)
├── telemetry_history.json  # Historical telemetry data
└── screenshots/            # Captured images (YYYY/MM/DD/...)
To back up:

bash
sudo systemctl stop meshcenter.service
tar -czf "$HOME/meshcenter-data-$(date +%F).tar.gz" -C "$HOME/MeshCenter" data config.py
sudo systemctl start meshcenter.service
Restore only onto a compatible version and verify ownership.

Updating MeshCenter
Back up first:

bash
cd ~/MeshCenter
cp config.py "$HOME/config.py.meshcenter.backup"
tar -czf "$HOME/meshcenter-data-$(date +%F-%H%M).tar.gz" data
Update:

bash
sudo systemctl stop meshcenter.service
cd ~/MeshCenter
git pull --ff-only
source venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --upgrade meshtastic
python -m py_compile server.py api/*.py system_log.py
sudo systemctl start meshcenter.service
If the browser shows old interface files, do a hard refresh (Ctrl+F5) or clear the cache.

Project structure
text
MeshCenter/
├── api/                    # Flask route modules
│   ├── api_camera.py       # Camera endpoints
│   ├── api_chat.py         # Chat & send endpoints
│   ├── api_settings.py     # Settings endpoints
│   └── api_system.py       # System, Wi-Fi, logs, actions
├── camera/                 # Picamera2, MJPEG, capture, gallery (if separate; here camera.py is in root)
├── meshsrv/                # Meshtastic CLI process helpers
├── storage/                # Atomic JSON storage helpers
├── telemetry/              # Telemetry state and history management
├── utils/                  # Shared helper functions (now helpers.py)
├── static/                 # JavaScript, CSS, icons, Chart.js
├── templates/              # HTML templates
├── docs/                   # Documentation images
├── data/                   # Runtime data (not committed)
├── config.example.py       # Configuration template
├── config.py               # Local configuration (create during installation)
├── server.py               # Main Flask application and listener coordination
├── system_log.py           # Persistent JSONL event log
├── helpers.py              # General helper functions
├── json_store.py           # JSON read/write helpers
├── meshsrv.py              # CLI command wrappers
├── telemetry.py            # Telemetry module
├── camera.py               # Camera management module
├── requirements.txt
└── README.md
The project is being refactored from a monolithic server.py into modular components.

API Reference
The web interface uses a REST‑like API. Endpoints are not considered stable yet and may change.

Chat & nodes
text
GET    /api/chats
GET    /api/messages
POST   /api/send
POST   /api/clear_chat
POST   /api/delete_chat
POST   /api/delete_all_dm
GET    /api/nodes_management
GET    /api/nodes_export
POST   /api/nodes_import
POST   /api/nodes_merge_duplicates
POST   /api/toggle_favorite
POST   /api/toggle_ignore
POST   /api/rescan_nodes
Telemetry
text
GET    /api/sensors
GET    /api/base_status
GET    /api/telemetry
GET    /api/telemetry/history
POST   /api/telemetry/config
GET    /api/export/telemetry
Camera
text
GET    /video_feed
GET    /api/camera/status
GET    /api/camera/settings
POST   /api/camera/settings
POST   /api/camera/stop
POST   /api/camera/switch_mode
POST   /api/camera/screenshot
GET    /api/camera/screenshots
DELETE /api/camera/screenshot/<filename>
DELETE /api/camera/screenshots
GET    /api/photo/settings
POST   /api/photo/settings
POST   /api/photo/capture
POST   /api/photo/save
System
text
GET    /api/system/info
GET    /api/system/network
GET    /api/system/wifi/scan
POST   /api/system/wifi/connect
POST   /api/system/wifi/forget
GET    /api/radio_health
POST   /api/restart_listener
GET    /api/system/log
POST   /api/system/action
Supported action values: restart_meshcenter, reboot, shutdown.

Troubleshooting
config.py not found
bash
cd ~/MeshCenter
cp config.example.py config.py
nano config.py
Meshtastic CLI not found in service
Set MESHTASTIC_CMD in config.py to the full path inside the virtual environment.

Permission denied on /dev/ttyACM0
bash
sudo usermod -aG dialout "$USER"
sudo reboot
Serial port busy
Stop other processes using the port and restart MeshCenter.

Service fails to start
Check logs: journalctl -u meshcenter.service -n 100 --no-pager. Verify paths in config.py and the service file.

Wi‑Fi scan fails
Check NetworkManager and sudo permissions:

bash
systemctl is-active NetworkManager
sudo -n /usr/sbin/iw dev wlan0 scan >/dev/null
sudo -n /usr/bin/nmcli connection show
sudo visudo -c
Camera unavailable
Ensure rpicam-hello --list-cameras shows a camera.

The virtual environment must be created with --system-site-packages.

High CPU or memory usage
Reduce camera resolution, FPS, or quality. On Raspberry Pi Zero 2W, avoid high resolutions.

No messages arrive
Verify radio connectivity, channel settings, and that meshtastic --info works. Check the System Log for listener errors.

Browser shows old interface
Use Ctrl+F5 or clear browser cache.

Security notes
MeshCenter is designed for trusted local networks.

No built‑in authentication – anyone who can reach the service can use it.

HTTP is unencrypted by default.

System action endpoints can reboot or shut down the host when sudoers rules are enabled.

Wi‑Fi management changes host network configuration.

API endpoints are accessible to any client that can reach the service.

Recommended practices:

Keep MeshCenter behind a firewall (home or field router).

Do not forward port 5000 directly to the Internet.

Use WireGuard, Tailscale, or an authenticated reverse proxy for remote access.

Grant only the exact sudo commands shown above.

Keep Raspberry Pi OS and Meshtastic CLI updated.

Back up data/ and config.py before upgrades.

Known limitations
English‑first interface (more languages planned).

No built‑in authentication.

Designed for one local USB‑connected Meshtastic radio.

Wi‑Fi management assumes NetworkManager and interface wlan0.

Serial handling primarily tested with /dev/ttyACM0.

Photos stay local and are not sent over LoRa.

REST API is internal and may change without notice.

Roadmap
Near‑term priorities:

Improve persistent system logging and filtering

Expand diagnostics and health monitoring

Better system action feedback and reconnect behaviour

Refine telemetry export and long‑term statistics

Improve installation automation and configuration validation

Add optional authentication

Continue modularising the server

Longer‑term ideas:

Map and position display

MQTT and Home Assistant integration

Notifications and automation rules

Additional sensors and GPIO modules

Plugin architecture

Multilingual interface

Multi‑radio support

Contributing
Contributions, documentation corrections and tested installation notes are welcome.

A useful issue report includes:

Raspberry Pi model and OS version

Python version and Meshtastic CLI version

Radio hardware and firmware version

Serial device path

Browser and exact steps to reproduce

Relevant journalctl output

Relevant entries from data/system_events.jsonl

Please remove private data, Wi‑Fi credentials and channel keys before posting logs.

License
MeshCenter is released under the MIT License. See LICENSE.

Acknowledgements
The Meshtastic team and community

The Raspberry Pi Foundation and Picamera2 maintainers

Open‑source contributors and testers

MeshCenter is an independent community project and is not affiliated with or endorsed by the official Meshtastic project. Meshtastic® is a trademark of its respective owners.

Author
Konstantin Vynohradov – FlintUA

GitHub: https://github.com/FlintUA

Project: https://github.com/FlintUA/MeshCenter

Website: https://elektroniker.help

<p align="center"><strong>Made for the Meshtastic community.</strong></p>
For Developers – Architecture and Extension Guide
This section is intended for contributors and developers who want to understand the codebase and add new features.

High‑level architecture
MeshCenter is a Flask web application that runs on a Raspberry Pi. It consists of several layers:

Web server – Flask serves the UI (static files + templates) and provides a REST API.

Background workers – Threads run continuously:

Meshtastic listener – reads from the serial port (meshtastic --listen), parses packets, updates nodes, messages, telemetry.

Telemetry buffer worker – debounces telemetry updates and saves them to disk.

Telemetry worker – periodically checks for fresh data.

Radio health worker – monitors listener status and packet ages.

Seen IDs cleanup – prevents duplicate messages.

Data storage – JSON files with atomic writes (via json_store.py).

System log – JSONL file for persistent events (system_log.py).

Camera – managed by camera.py, using Picamera2.

Telemetry – managed by telemetry.py, keeps history and configuration.

API modules – each set of endpoints is in api/*.py, registered in server.py.

Helpers – helpers.py provides common functions (normalise node IDs, format timestamps, etc.).

CLI wrapper – meshsrv.py wraps meshtastic subprocess calls.

Module overview
server.py
The main entry point. It:

Loads config.py and validates required variables.

Initialises Flask and registers API routes.

Loads persistent data (messages, nodes, chats, sensors, settings, telemetry, camera config).

Starts all background threads.

Runs the Flask development server (in production, use a WSGI server like Gunicorn with systemd).

config.py (user‑provided)
Contains all configuration: paths, node IDs, known nodes, etc. See config.example.py for a template.

helpers.py
Pure utility functions used across the project:

normalize_node_id(), is_valid_node_id(), sanitize_text()

voltage_to_percent(), node_num_to_id()

Timestamp helpers (now(), timestamp_iso())

json_store.py
Provides safe_read_json() and safe_write_json() for atomic file operations. Used by all modules that persist data.

system_log.py
Manages the persistent event log (system_events.jsonl). Exports:

log_system_event(title, level, details, source) – appends a JSON line.

get_system_events(limit, level, source) – reads and filters events.

meshsrv.py
Thin wrapper around meshtastic CLI commands:

get_info(meshtastic_cmd, timeout) → runs meshtastic --info

send_text(meshtastic_cmd, text, channel, dest, timeout) → sends a message

telemetry.py
Manages telemetry state and history. Provides:

telemetry_current – latest values.

telemetry_history – list of historical records.

add_telemetry_record() – appends a record if the interval has elapsed.

load_telemetry() / save_telemetry() – persist to telemetry_history.json.

camera.py
Handles all camera operations using Picamera2:

Initialisation, mode switching (video/photo), frame capture.

MJPEG stream generation.

Screenshot capture and gallery management (list, delete, cleanup).

Photo preview and high‑resolution save.

API modules
Each module registers its routes with the Flask app via a registration function:

api_chat.py – chats, messages, send, node operations.

api_camera.py – camera endpoints.

api_settings.py – user settings (units).

api_system.py – system info, network, Wi‑Fi, radio health, system log, system actions.

All API functions are decorated with handle_errors (from server.py) to catch exceptions and return JSON errors.

static/ and templates/
index.html – single‑page application UI.

style.css – all styles.

chat.js – main client‑side logic (chat, nodes, telemetry, camera, system, settings). Uses fetch() to call the API.

chart.umd.min.js – Chart.js for telemetry graphs.

Background threads
These are started in server.py after initialisation:

listen_meshtastic() – runs the listener and processes incoming packets.

cleanup_seen_ids() – periodically prunes duplicate‑detection caches.

telemetry_worker() – monitors telemetry freshness.

telemetry_buffer_worker() – debounces telemetry updates.

radio_health_worker() – updates radio health status every 30 seconds.

How to extend MeshCenter
Add a new API endpoint
Decide which module it belongs to (or create a new one in api/).

Define a route function with @app.route(...) and @handle_errors.

Register the route in server.py using the module’s registration function (or if it’s a simple route, you can add it directly to server.py).

Update the API reference in this README.

Add a new background task
Define a function that runs a loop (or uses threading.Timer).

Start it in server.py as a daemon thread: threading.Thread(target=my_function, daemon=True).start().

If it interacts with shared state, acquire state_lock (or a dedicated lock) to avoid race conditions.

Add a new configuration variable
Add it to config.example.py with a comment.

In server.py, include it in the required_vars list (or handle it with a default).

Document it in the “Installation → Create config.py” section.

Add a new UI tab or component
Edit templates/index.html – add a new tab button and the corresponding view container.

Add CSS rules in style.css.

In chat.js, implement the JavaScript logic: switchMainTab() handles tab switching, and you can add data‑loading functions.

Create API endpoints to serve the data needed by the new UI.

Improve telemetry or add new sensors
Extend telemetry.py to parse additional fields from the listener output.

Update the frontend to display them (telemetry cards, chart series).

Add to the export functionality (CSV/JSON) if appropriate.

Work with the system log
Use log_system_event() to record important events (errors, actions, state changes).

The UI fetches events via /api/system/log; you can filter by level and source.

The log is stored as JSONL – each line is a JSON object. Use get_system_events() to read it.

Debugging
Use print() statements with flush=True – they appear in the systemd journal (journalctl -u meshcenter.service -f).

For API debugging, inspect the response payloads in the browser’s DevTools (Network tab).

For frontend debugging, use console.log() and browser DevTools.

Coding style and conventions
Python: follow PEP 8. Use 4‑space indentation.

JavaScript: semi‑colons, 2‑space indentation, const/let, avoid var.

HTML/CSS: maintain consistency with existing classes and naming.

API responses: always include "ok": true/false and an "error" message on failure.

Use with state_lock: when accessing or modifying shared dictionaries (messages, nodes, chats, etc.).

Use atomic JSON writes (safe_write_json) for all data persistence.

Testing
Currently, there is no automated test suite. Manual testing is recommended:

Run the app, perform actions, check the UI and logs.

Use curl to test API endpoints.

Verify data persistence (restart the service and check that data is reloaded).

Contributions that include tests (unit or integration) are very welcome.

Last updated: July 2026 – reflects the current state of the main branch.
