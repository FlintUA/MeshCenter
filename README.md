<h1 align="center">MeshCenter - Meshtastic Control Center</h1>

<p align="center">
A complete browser-based control center for Meshtastic® base stations running on Raspberry Pi.
</p>

<p align="center">
  <img src="docs/images/meshcenter001.png" width="480" alt="MeshCenter Logo">
</p>

<p align="center">💬 Messaging · 🗺 Interactive Map · 📊 Telemetry · 📷 Camera</p>

<p align="center">📡 Node Management · ⚙ Raspberry Pi · 🌦 Weather · 📶 Wi-Fi · 🔋 Power Monitoring</p>

<h1 align="center">Meshtastic Powered</h1>

<p align="center">
  <img width="256" height="256" alt="meshtastic-powered" src="https://github.com/user-attachments/assets/42b4c3fe-396f-489e-82cf-fd710b235361" />
</p>

<p align="center">
  <img src="https://img.shields.io/github/v/release/FlintUA/MeshCenter" alt="Release">
  <img src="https://img.shields.io/github/license/FlintUA/MeshCenter" alt="License">
  <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python">
  <img src="https://img.shields.io/badge/Raspberry%20Pi-Bookworm-C51A4A" alt="Platform">
  <img src="https://img.shields.io/badge/Meshtastic-Compatible-success" alt="Meshtastic">
  <img src="https://img.shields.io/badge/Status-Active%20Development-brightgreen" alt="Status">
</p>

---

## Overview

**MeshCenter** is an open-source browser-based control and monitoring platform for Meshtastic nodes running on Raspberry Pi.

Unlike traditional clients, MeshCenter combines messaging, interactive maps, telemetry, camera support, media management and system monitoring into a single responsive web interface.

## 🌐 Live Demo

Explore MeshCenter in your browser:
https://meshcenter.elektroniker.help/preview/

Instead of depending solely on a mobile application, MeshCenter provides a permanent web interface that is available from any device connected to your local network. It combines messaging (with Meshtastic reply support), an interactive network map, intelligent node management, telemetry dashboards, camera streaming, media management, Wi‑Fi administration, weather information, notifications, system monitoring and documentation into a single lightweight application.

The project is optimized for Raspberry Pi Zero 2W while remaining fully compatible with more powerful Raspberry Pi models.

MeshCenter is designed as a complete control center that continuously runs alongside a Meshtastic node, providing real-time monitoring and convenient management through any modern web browser.

---

## Why MeshCenter?

The official Meshtastic applications are excellent for configuration, mobile operation and everyday communication.

MeshCenter is **not intended to replace them**. It complements the official ecosystem by providing a permanent browser-based control center for fixed stations, gateways and Raspberry Pi based installations.
MeshCenter relies on the official Meshtastic configuration stored on the connected radio. Channel management and radio configuration are performed using the official Meshtastic applications, while MeshCenter automatically detects, synchronizes and works with the configured channels.

Typical use cases include:

- Home base stations
- Portable field communication servers
- Emergency communication nodes
- Raspberry Pi gateways
- Weather monitoring stations
- Remote telemetry systems
- Educational and experimental projects

---

## 📸 Screenshots

<details>
<summary>🗺️ Map — Light theme</summary>

![MeshCenter map view light theme](docs/images/MeshCenter_map_light_theme.png)

</details>

<details>
<summary>🗺️ Map — Dark theme</summary>

![MeshCenter map view dark theme](docs/images/MeshCenter_map_dark_theme.png)

</details>

<details>
<summary>🗺️ Map + Nodes panel — Light theme</summary>

![MeshCenter map split light theme](docs/images/MeshCenter_map_split_light_theme.png)

</details>

<details>
<summary>🗺️ Map + Nodes panel — Dark theme</summary>

![MeshCenter map split dark theme](docs/images/MeshCenter_map_split_dark_theme.png)

</details>

<details>
<summary>💬 Chats — Light theme</summary>

![MeshCenter chats light theme](docs/images/MeshCenter_chats_light_theme.png)

</details>

<details>
<summary>💬 Chats + Nodes panel — Dark theme</summary>

![MeshCenter chats split dark theme](docs/images/MeshCenter_chats_split_dark_theme.png)

</details>

<details>
<summary>🖼️ Media — Light theme</summary>

![MeshCenter media light theme](docs/images/MeshCenter_media_light_theme.png)

</details>

<details>
<summary>🖼️ Media + Nodes panel — Light theme</summary>

![MeshCenter media split light theme](docs/images/MeshCenter_media_split_light_theme.png)

</details>

<details>
<summary>📷 Camera — Light theme</summary>

![MeshCenter camera light theme](docs/images/MeshCenter_camera_light_theme.png)

</details>

<details>
<summary>📟 Devices — Light theme</summary>

![MeshCenter devices light theme](docs/images/MeshCenter_devices_light_theme.png)

</details>

<details>
<summary>ℹ️ About — Dark theme</summary>

![MeshCenter about dark theme](docs/images/MeshCenter_about_dark_theme.png)

</details>

---

## ⚡ Install

> **Requirements:** Raspberry Pi (Zero 2W / 3B+ / 4B), microSD card 8GB+,
> [Raspberry Pi Imager](https://www.raspberrypi.com/software/), internet connection.
> 💡 For manual installation or advanced options see [INSTALL.md](INSTALL.md).

---

## ✨ Highlights

***Key Features***

- Browser-based interface
- No additional software required on client devices
- Optimized for desktop web browsers
- Responsive desktop UI
- Open source
- Designed for Raspberry Pi
- Works with standard Meshtastic firmware

### 📚 Documentation

- [Practical User Guide](docs/User_Guide.md)
- [Quick Installation](#installation)
- [Architecture Overview](#application-architecture)
- [Development Roadmap](#roadmap)

## Switching to another Meshtastic radio

1. Open Node Manager.
2. Click Release Radio.
3. Disconnect the current radio.
4. Connect the replacement radio by USB.
5. Wait a few seconds for Linux to create the serial device.
6. Click Detect radio.
7. Confirm the detected radio.
8. For a new radio, MeshCenter creates a clean separate profile.
9. For a known radio, MeshCenter activates its saved profile.
10. MeshCenter restarts automatically.

Each radio profile keeps its own messages, nodes, telemetry, waypoints and icons.
Data from different radios is not merged.

### 💬 Messaging

- MeshCenter automatically detects and synchronizes all channels configured on the connected Meshtastic node. The interface supports Meshtastic channel indexes 0–7, allowing up to eight configured channels, including the primary channel. Channel creation and radio configuration are handled by the official Meshtastic applications.
- Public channel messaging
- Direct messages
- Compatible with the official Meshtastic reply feature.
- Reply composer and quoted replies
- Jump to original message
- Message actions (copy message with sender information)
- Automatic chat updates
- Chat history
- Favorite contacts
- Ignore list
- Message export

### 🧩 Workspace Map

MeshCenter includes a fully integrated interactive map directly inside the workspace.

***Features include:***

- Embedded map without leaving the main interface
- Hide, Split and Full Screen map layouts
- Split layout with top or bottom positioning
- Live node locations
- Quick actions directly from map popups
- Automatic synchronization with node details
- External map provider support when needed

### 📍 Waypoints

MeshCenter includes integrated waypoint management fully compatible with Meshtastic.

Waypoints can be created, stored, managed and transmitted directly from the browser interface without leaving MeshCenter.

- Create and edit waypoints
- Persistent waypoint storage
- Send waypoints to Meshtastic nodes
- Waypoint management workspace
- Local JSON-based storage
- Ready for future waypoint synchronization

### 🗺 Network Visualization

- Display all positioned nodes
- Selected node highlighting
- Node labels
- Node information popups
- Distance and bearing visualization
- Reference location marker
- Fit Nodes view
- Automatic scrolling to selected node
- Persistent node positions

### 📷 Camera & Media Gallery

- Live MJPEG video streaming
- High-resolution photo capture
- Integrated photo gallery with thumbnails, download and delete
- Adjustable image quality and FPS
- Raspberry Pi Camera support
- Media workspace to browse local captures

### 📶 Wi-Fi Manager

- View current Wi‑Fi connection (SSID, signal strength, IP, gateway)
- Scan nearby networks with signal percentage
- Connect to new networks with password prompt
- Automatically reuse saved credentials
- Forget saved profiles

### 📈 Telemetry

- Device telemetry (battery, voltage, channel utilisation, uptime)
- Environmental sensors (temperature, humidity, pressure)
- Power monitoring (voltage, current, power)
- Historical charts with selectable time ranges (1h to 30d)
- Export telemetry data as CSV or JSON

### 🖥 Modern Desktop Interface

- Professional three‑column layout
- Responsive workspace layout
- Workspace panel (show/hide columns, theme, compact mode)
- Notification Center
- Bottom status dock with system metrics
- Improved light and dark themes
- Unified component design
- Custom node icons
- Larger and clearer map markers
- Smoother map interaction

### 📡 Node Management

- Automatic node discovery
- Hardware information and role
- RSSI / SNR monitoring
- Favorites and ignore list
- Import / Export node database (CSV / JSON)
- Merge duplicates
- Synchronized selection with interactive map
- Interactive node popups with quick actions
- Locate nodes directly on the embedded map

### 🛠️ Node Tools

- Request telemetry from any visible node
- Request position (saves coordinates)
- Traceroute – show mesh route to a node

### 🩺 System & Radio Health

- Real‑time system info (hostname, uptime, CPU, RAM, disk, temperature)
- Radio listener status, packet age, telemetry age
- Automatic listener recovery with configurable delay
- CPU usage history chart (30m to 24h)
- System actions: restart MeshCenter, reboot, shutdown

### 🌦️ Weather Module

- Current weather and 3‑day forecast, from a selectable provider (OpenWeather or WeatherAPI)
- Location from manual coordinates or a reference node
- Units follow global preferences
- Auto‑refreshes every 10 minutes

### ⚙️ Web Settings Editor

- Units (temperature and pressure)
- Telemetry update interval
- Battery capacity for runtime estimation
- Listener auto‑recovery settings
- External map service
- Reference location (manual or node‑based)

### 🎨 Workspace & UI Preferences

- Persistent panel visibility, theme and compact mode per browser
- All preferences stored locally

### ⚡ Optimized for Raspberry Pi

MeshCenter has been developed with Raspberry Pi Zero 2W as the primary target platform.

Special attention has been paid to:

- Low memory usage
- Low CPU utilization
- Fast page loading
- Lightweight architecture
- Stable 24/7 operation

### 🧪 Tested Hardware Setup

# IMPORTANT

## Enable Serial Interface on your Meshtastic device

MeshCenter communicates with the radio over the USB serial interface.

Before connecting your Meshtastic node to Raspberry Pi, make sure the serial interface is enabled in the official Meshtastic application.

Without the USB serial interface MeshCenter will not be able to detect or communicate with the radio.

Official Meshtastic App:

Settings → Serial → Enabled

Tested with:

RAK4631
RAK WisMesh TAP v2 (RAK3312)
LilyGO T-Echo Plus

The USB serial interface must be enabled.

MeshCenter is primarily developed and tested on a Raspberry Pi Zero 2 W connected by USB to a standard Meshtastic radio node. The radio uses standard Meshtastic firmware and does not require hardware modification or its own Wi-Fi connection.

The current reference setup includes:

- Raspberry Pi Zero 2 W
- RAK4631-based Meshtastic node connected by USB
- Raspberry Pi Camera via CSI interface
- Supported cameras:
  - IMX219 8 MP (currently installed)
  - OV5647 5 MP (tested)
- INA226 power monitor
- BME280 environmental sensor

Other Raspberry Pi models and standard Meshtastic-compatible radios with a supported USB serial connection may also work. The radio must first be configured with an official Meshtastic application. MeshCenter then uses the region, channels and channel keys already stored on it.


---

## Design Philosophy

MeshCenter follows a few simple principles:

- Browser-first experience
- Fast and responsive interface
- Reliable long-term operation
- Low resource consumption
- Simple installation
- Easy maintenance
- Full compatibility with the official Meshtastic ecosystem

The goal is to provide a practical and reliable control center that can run continuously on a Raspberry Pi while giving convenient access to the most important functions of a Meshtastic base station from any web browser.

---

## What Makes MeshCenter Different?

Unlike traditional web interfaces, MeshCenter is designed as a complete operational environment for a Meshtastic base station.

It combines multiple independent subsystems into one application:

- Messaging (including Meshtastic reply support)
- Interactive Network Map
- Waypoints
- Camera
- Photo Gallery
- Telemetry
- Node Management
- Node Tools (telemetry request, position request, traceroute)
- System Monitoring & Radio Health
- Weather Integration
- Wi‑Fi Manager
- Local Data Storage
- Background Services
- REST API

This modular architecture makes it easy to extend the project while keeping the user interface simple and responsive.

## Working with Multiple Radios

MeshCenter automatically manages independent profiles for every Meshtastic radio connected to the Raspberry Pi.

Each radio receives its own dedicated profile containing:

- Messages
- Direct messages
- Node database
- Telemetry history
- Waypoints
- Device configuration
- Node icons
- Profile-specific settings

This means you can disconnect one Meshtastic device, connect another one and continue working with its own independent data without affecting the previous radio.

### Connecting a new radio

1. Disconnect the current radio.
2. Connect another supported Meshtastic device via USB.
3. Make sure **USB Serial** is enabled in the Meshtastic firmware.
4. Open **Node Manager**.
5. Press **Detect radio**.

If the radio has never been used before, MeshCenter will offer to create a new clean profile.

If the radio has been used previously, MeshCenter will automatically detect its existing profile and offer to activate it.

No existing profile data is overwritten.

### Switching between radios

After a profile has been created, switching is simple:

1. Open **Node Manager**.
2. Click the desired saved radio.
3. Connect that radio to the Raspberry Pi.
4. Confirm the activation.

MeshCenter will:

- release the current radio;
- verify the newly connected radio;
- activate its saved profile;
- restart the internal listener;
- continue using the selected profile.

The switch normally takes only a few seconds.

### Waypoints

Waypoints belong to the active radio profile.

Each Meshtastic device keeps its own independent waypoint database.

Switching between radios automatically switches to that radio's waypoint collection.

No waypoint data is shared or mixed between different radio profiles.

### Notes

- One USB radio can be active at a time.
- Profiles are never merged automatically.
- Existing data is preserved when switching between radios.
- Profiles can be backed up simply by copying the corresponding folder from `data/profiles/`.

---

## Installation

## ⚠ IMPORTANT: Enable Serial access on the Meshtastic radio

Before connecting a Meshtastic radio to MeshCenter, make sure that
**Serial access is enabled in the Meshtastic settings**.

Open the Meshtastic app and enable:

Settings → Security → Serial enabled

Depending on the app or firmware version, the option may also appear under:

Settings → Device → Serial enabled

Do not confuse this option with the Serial module.

If Serial access is disabled, Linux may still detect the USB device and create
`/dev/ttyACM0`, but MeshCenter and the Meshtastic CLI will not be able to read
the radio identity. Typical symptoms are:

- `Connection timed out`
- `No Meshtastic radio identity could be read`
- `Unable to detect radio`

This is the most common cause of USB detection failure.

> **Tick-off checklist:** the same steps below are also available as a
> condensed checklist in **[INSTALL.md](INSTALL.md)**, including the sudoers
> step that's easy to miss, plus `scripts/verify-install.sh` to check an
> install automatically.

### Installation Validation

The installation procedure described in this repository has been successfully validated on a clean Raspberry Pi Zero 2 W using a **RAK WisMesh TAP v2 (RAK3312)** with standard Meshtastic firmware.

The system was installed entirely from the documentation, confirming that no undocumented configuration steps are required.

MeshCenter runs on **Raspberry Pi OS Bookworm** (or newer). It is primarily tested on Raspberry Pi Zero 2W and also works on Raspberry Pi 3, 4 and 5.

This section is a complete beginner-friendly install path from a fresh Pi to a working web interface. For first-run checks, interface usage, backup, safe update details and extended troubleshooting, see the **[Practical User Guide](docs/User_Guide.md)**.

### Requirements

**Hardware**

- Raspberry Pi Zero 2W or newer
- microSD card (16 GB minimum, 32 GB recommended)
- Computer with a microSD card reader for preparing Raspberry Pi OS
- Stable power supply
- Meshtastic-compatible radio with a **data-capable** USB cable (serial connection)
- Wi-Fi or Ethernet (for access to the web interface)
- Raspberry Pi Camera (optional)

**Software**

- Raspberry Pi OS Bookworm (64-bit recommended)
- Raspberry Pi Imager
- Python 3.11 or newer
- Git

**Before installing MeshCenter**, configure the radio with an official Meshtastic app (Android or Desktop):

1. Set the correct LoRa region.
2. Configure channels and channel keys.
3. Set the node long name and short name.
4. Confirm that the radio can exchange messages with another node.

MeshCenter reads the channels already stored on the radio. It does not create or edit channels itself.

### 1. Write Raspberry Pi OS and enable SSH

MeshCenter can run headless, without a monitor or keyboard. Use the official [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to prepare the microSD card.

1. Select your Raspberry Pi model.
2. Select **Raspberry Pi OS Lite (64-bit)**.
3. Select the correct microSD card.
4. In **OS Customisation**, configure:

   - A unique hostname, for example `meshcenter`
   - Your time zone, country and keyboard layout
   - A Linux username and strong password
   - Wi-Fi SSID and password
   - **Remote Access > Enable SSH > Use password authentication**

Write and verify the image, insert the microSD card into the Raspberry Pi and connect the power supply. Allow several minutes for the first boot.

From a computer on the same local network, open PowerShell, Windows Terminal or another terminal and connect with:

```bash
ssh <username>@<hostname>.local
```

Example:

```bash
ssh flint@meshcenter.local
```

At the first connection, enter `yes`, then enter the password created in Raspberry Pi Imager. The password is not displayed while typing.

If the `.local` hostname does not work, find the Raspberry Pi address in the router's connected-device list:

```bash
ssh <username>@<raspberry-pi-ip>
```

After a successful connection, the Raspberry Pi terminal is ready for the commands below.

For more detailed instructions, see the [Practical User Guide](docs/User_Guide.md).

### 2. Prepare Raspberry Pi OS

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip network-manager iw lsof
```

Optional camera support (install **before** creating the virtual environment):

```bash
sudo apt install -y python3-picamera2 rpicam-apps
```

Allow the current user to access the USB serial port, then reboot:

```bash
sudo usermod -aG dialout "$USER"
sudo reboot
```

After reboot, reconnect (SSH or local terminal) and continue.

### 3. Clone the repository

```bash
cd ~
git clone https://github.com/FlintUA/MeshCenter.git meshcenter
cd ~/meshcenter
```

### 4. Create the Python environment

`--system-site-packages` is required so Picamera2 and other Raspberry Pi system packages are visible inside the virtual environment.

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

Confirm that the Meshtastic CLI is installed inside the venv:

```bash
which meshtastic
meshtastic --version
```

`which meshtastic` should show a path under `~/meshcenter/venv/bin/`.

### 5. Find and test the USB radio

Connect the Meshtastic radio and list serial devices:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

Most RAK4631-based nodes appear as `/dev/ttyACM0`. Some radios use `/dev/ttyUSB0`.

Test communication (replace the port if needed):

```bash
source ~/meshcenter/venv/bin/activate
meshtastic --port /dev/ttyACM0 --info
```

**Do not continue** until this command shows local node information without permission or serial-port errors. Note the local node ID, long name and short name from the output — you will need them in the next step.

### 6. Create the local configuration

```bash
cd ~/meshcenter
cp config.example.py config.py
mkdir -p data
```

Open `config.py` and set at least:

```python
MESHTASTIC_PORT = "/dev/ttyACM0"

LOCAL_NODE_ID = "!xxxxxxxx"
LOCAL_NODE_NAME = "My Base Station"
```

Replace the example values with those reported by `meshtastic --info`.  
`MESHTASTIC_CMD` and `DATA_DIR` are resolved automatically from the project directory.

`config.py`, optional `weather_secrets.py` and the `data/` folder are local files and are **not** overwritten by normal Git updates.

**Optional weather:** copy `weather_secrets.example.py` to `weather_secrets.py` and insert an API key for OpenWeather and/or WeatherAPI. Which provider is active is chosen in **Workspace → Settings → Weather Provider**; the location can later be chosen in **Workspace → Settings → Reference location**.

### 7. First manual start

```bash
cd ~/meshcenter
source venv/bin/activate
python server.py
```

On the Pi, find the IP address:

```bash
hostname -I
```

From another device on the same local network open:

```text
http://<raspberry-pi-ip>:5000
```

Stop the manual server with `Ctrl+C` before installing the systemd service.

### 8. Run as a system service (recommended)

Render and install the service template for the current user:

```bash
cd ~/meshcenter
MESH_USER="$(id -un)"
MESH_HOME="$HOME"
sed -e "s|__MESH_USER__|$MESH_USER|g" \
    -e "s|__MESH_HOME__|$MESH_HOME|g" \
    deploy/meshcenter.service \
    | sudo tee /etc/systemd/system/meshcenter.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now meshcenter.service
sudo systemctl status meshcenter.service --no-pager -l
```

Expected state: `active (running)`.

**System and Wi‑Fi actions** (restart MeshCenter, reboot/shutdown Pi, scan/connect Wi‑Fi) need extra narrowly scoped sudo rules. Full commands are in the [Practical User Guide → Enable System and Wi‑Fi actions](docs/User_Guide.md#enable-system-and-wi-fi-actions).

### Radio Configuration Mode (after install)

MeshCenter normally maintains an active USB connection to the Meshtastic radio in order to provide:

- Real-time messaging
- Node telemetry
- Node management
- Interactive maps
- Node Tools
- Automatic channel synchronization

Since only one application can access the radio at a time, the official Meshtastic mobile application cannot connect while MeshCenter is using the USB interface.

### Temporary Radio Release

To configure the radio using the official Meshtastic Android application:

1. Open **Settings → Meshtastic Radio**.
2. Click **Release Radio**.
3. Wait until the status changes to **Released**.
   - Releasing the USB connection may take up to **1–2 minutes**, depending on the current listener state.
4. Connect to the node using the official Meshtastic Android application via Bluetooth.
5. Make any required changes (channels, encryption keys, LoRa settings, etc.).
6. Disconnect from the Android application.
7. Return to MeshCenter and click **Reconnect Radio**.

MeshCenter automatically reconnects to the radio and resumes normal operation without restarting the service.

### Automatic Synchronization

After reconnecting, MeshCenter automatically:

- Detects newly added channels
- Removes deleted channels
- Updates channel names
- Restores messaging and telemetry
- Resumes node monitoring

No restart or manual refresh is required.

> **Note**
>
> While the radio is released, messaging, telemetry, node management and Node Tools are temporarily unavailable because the radio is intentionally disconnected from MeshCenter.

### Design Philosophy

MeshCenter intentionally does **not** modify the radio configuration itself.

Radio configuration is performed using the official Meshtastic application, while MeshCenter automatically synchronizes with the current radio configuration after reconnecting.

This approach provides several advantages:

- Full compatibility with standard Meshtastic firmware
- No custom firmware required
- Complete compatibility with the official Meshtastic applications
- Safe configuration using the official tools
- Automatic synchronization of channels and radio state

### Updating MeshCenter

Full safe-update procedure (backup, checks, verification): **[User Guide → Update MeshCenter safely](docs/User_Guide.md#13-update-meshcenter-safely)**.

Short version when the working tree is clean:

```bash
cd ~/meshcenter
git status --short --branch          # review any local changes first
# back up config.py and data/ if needed
git pull --ff-only origin main
source venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl restart meshcenter.service
```

Reload the browser with `Ctrl+F5` after a UI update.

---

## 🔄 Updating

```bash
cd ~/meshcenter
git pull
git fetch --tags
sudo systemctl restart meshcenter.service
```

After updating, reload the browser with **Ctrl+F5**.

> **Note:** `git fetch --tags` is required to pull the latest version tag.
> Without it, the version shown in the status bar may remain outdated.
>
> On some installations `sudo systemctl` requires the full path:
> ```bash
> sudo -n /usr/bin/systemctl restart meshcenter.service
> ```

---

## Project Structure

MeshCenter has been designed as a modular application. Each subsystem has its own responsibility, making the project easier to maintain, debug and extend.

A typical installation looks like this:

```
meshcenter/
│
├── api/                # REST API endpoints
├── camera/             # Camera subsystem
├── meshsrv/            # Meshtastic communication layer
├── storage/            # JSON storage helpers
├── telemetry/          # Telemetry processing
├── utils/              # Shared utility functions
│
├── static/             # CSS, JavaScript, icons
├── templates/          # HTML templates
├── data/               # Persistent application data (messages, nodes, photos, icons, settings)
├── docs/               # Documentation and images
│   └── User_Guide.md   # Installation and practical operation guide
│
├── venv/               # Python virtual environment
│
├── config.py           # Local configuration (not in git)
├── config.example.py   # Example configuration
├── requirements.txt
├── server.py           # Main application entry point
└── README.md
```

The `data` directory stores persistent information such as messages, telemetry history, node icons, camera photos and application settings.

---

## Tech Stack

MeshCenter is built with a lightweight and practical technology stack:

- Python
- Flask
- HTML5
- CSS3
- JavaScript
- Meshtastic Python API
- Raspberry Pi OS / Linux
- systemd
- Git and GitHub
- Leaflet
- OpenStreetMap

## Core Features

MeshCenter combines several independent subsystems into one control center. Each subsystem is designed to be useful on its own, but together they turn a Raspberry Pi into a practical Meshtastic base station.

### 📚 Documentation

- UI Guidelines
- Components
- Architecture
- Development Roadmap

### 💬 Messaging

MeshCenter provides a browser-based chat interface for Meshtastic communication.

The messaging system supports both public channel messages and direct node-to-node messages.

#### Public Channel

Public messages are sent to the configured Meshtastic channel, usually LongFast channel index 0.

Typical use cases:

- Local mesh chat
- Community messages
- Field communication
- Test messages
- Sensor status announcements

#### Direct Messages

MeshCenter also supports direct messages between nodes.

Direct messages are shown as separate chats, making it easier to work with multiple known nodes from a browser interface.

#### Messaging Features

- Public channel messaging
- Direct node-to-node messaging
- Native Meshtastic reply support
- Reply composer
- Quoted replies
- Jump to original message
- Message actions
- Copy including sender name
- Improved Direct Messages
- Chat history
- Automatic message refresh
- Message timestamps
- Favorite chats
- Ignore list
- Emoji picker
- System messages
- Local JSON-based storage

### 🗺 Interactive Network Map

MeshCenter includes an integrated Leaflet-based interactive map for visualizing the mesh network.

The map is built into the application and can be opened directly from the **Show on map** action. It displays all nodes that have a known position, along with a reference location marker used for distance and bearing calculations.

Node positions are stored locally on the Raspberry Pi and persist across restarts.

### 📍 Waypoints Features

MeshCenter includes integrated waypoint management fully compatible with Meshtastic.

Waypoints can be created, stored, managed and transmitted directly from the browser interface without leaving MeshCenter.

- Create and edit waypoints
- Persistent waypoint storage
- Send waypoints to Meshtastic nodes
- Waypoint management workspace
- Local JSON-based storage
- Ready for future waypoint synchronization

#### Map Features

- Integrated Leaflet-based map inside MeshCenter
- Open map directly from **Show on map**
- Display all positioned nodes
- Reference location marker
- Selected node highlighting
- Node labels
- Node information popups
- Distance and bearing visualization
- Fit Nodes view
- Synchronized node selection between map and node list
- Automatic scrolling to selected node
- Persistent node positions

#### Map UI and Performance

- Redesigned map panel
- Larger and clearer map markers
- Better popup positioning
- Improved selection colors and node highlighting
- Smoother map interaction
- Faster map updates and reduced unnecessary redraws
- Optimized map refresh logic and node synchronization

Node selection is synchronized between the map and the node list: selecting a node on the map highlights it in the list (and scrolls to it), and selecting a node in the list highlights it on the map.

> **Note:** The integrated map uses Leaflet with OpenStreetMap tiles. The separate **Map provider** setting (OpenStreetMap / Google Maps) controls only external “open in maps” links for individual node positions, not the built-in map.

### 🖥 Modern Desktop Interface

- Professional three-column layout
- Redesigned map panel
- Workspace panel
- Notification Center
- Bottom status dock
- Improved light and dark themes
- Better spacing and layout consistency
- Unified component design
- Custom node icons
- Responsive dashboard

### 📡 Node Management

MeshCenter automatically discovers nodes from the Meshtastic mesh and stores them locally.

The node list helps you understand what devices are visible in your area and when they were last heard.

#### Node Information

Depending on the available data, MeshCenter can display:

- Long name
- Short name
- Node ID
- Hardware model
- Role
- Last seen time
- RSSI
- SNR
- Hop distance
- Last message

#### Favorites

Frequently used nodes can be marked as favorites.

Favorites are useful for:

- Your own devices
- Family nodes
- Field team nodes
- Known repeaters
- Important contacts

#### Ignore List

Nodes that are not relevant can be ignored.

Ignored nodes remain in the database, but they can be hidden from the main view and filtered out from normal interaction.

This is useful in busy areas where many nodes are visible but only a few are important for your setup.

#### Import and Export

MeshCenter supports importing and exporting the local node database in CSV and JSON formats.

This is useful for:

- Backups
- Moving to another Raspberry Pi
- Keeping a known node list
- Sharing node information between installations

### 🛠️ Node Tools (Remote Commands)

MeshCenter can send commands to any visible node directly from the web interface:

- **Request Telemetry** – Ask the node to send its current sensor readings (temperature, humidity, pressure, voltage, current, power)
- **Request Position** – Request the node's GPS or fixed position; the result is saved and displayed
- **Traceroute** – Show the mesh route to the destination node, both forward and return paths

These tools are available from the node detail card and the Node Tools button.

### Custom Node Icons

You can upload a custom image (PNG, JPEG or WebP) for any node (including the local base node). The image is automatically centered and cropped into a 256×256 transparent square. Icons are stored locally on the Raspberry Pi and are served by MeshCenter.

## 🔔 Notifications

MeshCenter includes a notification system that helps operators stay informed without constantly watching the active chat.

Features include:

- Channel activity notifications
- Unread message indicators
- Conversation highlighting
- Improved notification synchronization

### 📈 Telemetry

MeshCenter displays telemetry received from Meshtastic devices and stores historical telemetry locally.

Telemetry is useful for monitoring both the radio node and connected sensors.

#### Device Telemetry

Supported device telemetry includes:

- Battery level
- Voltage
- Channel utilization
- Air utilization
- Uptime
- Last update time

#### Environmental Telemetry

Environmental telemetry can include:

- Temperature
- Humidity
- Atmospheric pressure

Typical sensor: **BME280**

#### Power Monitoring

Power telemetry can include:

- Voltage
- Current
- Power

Typical sensor: **INA226**

#### Telemetry History

Telemetry history is stored locally and can be displayed as charts with selectable time ranges (1 hour to 30 days). Data can be exported as CSV or JSON.

### 📷 Camera

MeshCenter includes camera support based on Raspberry Pi Camera and Picamera2.

The camera subsystem is designed for lightweight live viewing and photo capture on low-power hardware.

#### Live Video

Live video is provided as an MJPEG stream.

MJPEG is not the most bandwidth-efficient video format, but it has excellent browser compatibility and works reliably without additional client-side software.

#### Photo Capture

MeshCenter can capture high-resolution photos and store them locally.

The application can use different settings for:

- Live preview
- Video mode
- Photo capture

This allows the system to keep the live view lightweight while still supporting full-resolution image capture.

#### Camera Settings

Depending on the connected camera, MeshCenter can support:

- Video resolution
- Photo resolution
- FPS
- JPEG quality
- Preview size
- Save size

#### Gallery

Captured images are saved locally and can be viewed through the Media workspace. The gallery displays thumbnails, file size, capture time and provides download and delete actions.

### 🖼️ Media Gallery

MeshCenter stores captured photos inside the local data directory.

The gallery provides browser-based access to saved images without requiring SSH or file browser access to the Raspberry Pi.

Typical use cases:

- Checking recent camera captures
- Reviewing field images
- Downloading saved photos
- Keeping a visual project log

The gallery shows total image count, used space and free space. You can delete individual images or clear the entire gallery.

**Note:** The term “screenshots” in the storage directory and some internal references refers to photos captured by the Raspberry Pi Camera. The interface screenshots shown in this README are illustrative images of the web UI and are not stored by the application.

Photos are stored locally and are **not** transmitted through Meshtastic.

### ⚙️ Local Storage

MeshCenter stores application data locally using JSON files.

This keeps the project simple and easy to inspect, backup and repair.

Typical stored files include:

```
messages.json
nodes.json
chats.json
sensors.json
telemetry_history.json
deleted_dm.json
settings.json
camera_config.json
node_icons/
screenshots/          # camera photos
```

Local storage is useful because:

- No external database is required
- Backups are easy
- Files can be inspected manually
- The system remains lightweight
- The installation stays simple

### 🩺 System Monitor & Radio Health

MeshCenter provides a dedicated System workspace that gives you full visibility into your Raspberry Pi and Meshtastic radio status.

#### System Information

- Hostname, uptime, CPU load and temperature
- RAM usage (used / total)
- Disk usage (used / total)
- Raspberry Pi model and OS version

#### Radio Health Dashboard

- Listener status (running / stopped / paused)
- Packet age, telemetry age and send age
- Status level (OK, Warning, Error)
- Restart count and fail count
- A recommendation message for troubleshooting

#### CPU History Chart

- Live CPU usage graph with selectable ranges: 30m, 1h, 6h, 12h, 24h
- Current CPU usage, RAM usage and temperature are also shown in the status dock

#### System Log

- A detailed event log showing listener starts, stops, errors and system actions
- Logs are stored persistently and can be viewed directly in the System workspace

#### Automatic Listener Recovery

- If the Meshtastic listener stops, MeshCenter can automatically restart it after a configurable delay (30–300 seconds)
- The recovery mechanism respects a safety limit (max 3 attempts in 30 minutes) to avoid restart loops

### 🌦️ Weather Module

MeshCenter shows current weather conditions and a 3‑day forecast for your location, sourced from a pluggable weather provider (`weather/providers/`) - currently OpenWeather or WeatherAPI. The active provider is chosen in **Settings → Weather Provider**.

- **Server-side API key** - Each provider's API key is stored in the local `weather_secrets.py` file and never exposed to the browser
- **Caching** – Weather data is cached for 10 minutes to reduce API calls
- **Location sources**:
  - Manual coordinates (set in the web Settings)
  - Reference node position (if the node has GPS)
  - Static configured coordinates (fallback from `config.py`)
- **Units** – Follow global unit preferences (temperature, pressure, wind speed)
- **Forecast** – Shows the next three days with temperature ranges, weather condition and precipitation probability
- **Refresh** – Click the status badge to force an immediate update

The weather card is displayed in the Base panel and updates automatically every 10 minutes.

### 🎨 Workspace & UI Preferences

MeshCenter remembers your interface preferences per browser using local storage.

- **Panel visibility** – Show or hide the Base and Nodes panels
- **Theme** – System (follows OS), Light or Dark
- **Compact mode** – Reduced spacing and control sizes for smaller screens

All changes are saved automatically and applied on next visit.

Access the Workspace menu from the bottom‑left status dock.

### ⚙️ Web Settings Editor

MeshCenter provides a graphical settings interface accessible from the Workspace menu.

You can adjust the following without editing configuration files:

- **Units** - Temperature (°C/°F) and pressure (hPa/mmHg)
- **Telemetry interval** – How often sensor readings are stored (2, 5, 10, 15, 30 minutes)
- **Battery capacity** – Used for runtime estimation (100–50000 mAh)
- **Listener auto‑recovery** – Enable/disable, delay (30–300 seconds)
- **Map provider** – OpenStreetMap or Google Maps for external node-position links (does not affect the integrated Leaflet map)
- **Reference location** – Set a manual coordinate or select a node as the reference for distance/bearing calculations on the map

All settings are saved immediately and persist across restarts.

### 🌍 Localization (i18n)

MeshCenter includes the underlying infrastructure for a multi-language interface, selectable from **Settings → General → Language**.

**What works today:**

- Language switching (Auto / English / Deutsch / Русский / Українська) from the Settings panel
- The page's `<html lang>` attribute and the loaded translation catalog follow the selected language (or the browser's language when set to Auto)
- Part of the static interface text, a first slice of error and toast messages, and the Weather module's response language (mapped to whichever provider is active) all follow the selected language
- Verified end-to-end for all four locales (en/de/ru/uk)

**What's not translated yet:**

The German, Russian and Ukrainian catalogs currently contain the same English text as placeholders — the translation *infrastructure* is complete and tested, but the actual translated wording has not been written yet. Selecting German or Ukrainian today changes the mechanism (`<html lang>`, weather text, the wired error messages) while most of the visible interface text still reads in English until real translations are added in a future update.

Most of the chat interface (message bubbles, toasts and dialogs generated dynamically by JavaScript) is not yet wired into the translation system at all and remains English-only regardless of the selected language.

See `static/i18n/README.md` for translator notes, including terms that must never be translated (Meshtastic, LongFast, PSK, and similar).

### 🧩 Modular Architecture

MeshCenter is gradually moving from a single large server file to a modular architecture.

Current modules include:

```
api/
camera/
meshsrv/
storage/
telemetry/
utils/
```

This makes the project easier to maintain and extend.

The goal is to keep `server.py` as the central coordinator while moving specialized logic into dedicated modules.

## 🕐 Time System, Notifications & Automation

### 🕐 Time System

MeshCenter now maintains a single, authoritative source of time based on the Raspberry Pi. Every connected client (desktop browser, tablet, phone) sees the same device time instead of its own local browser clock.

- **"Time & Timers" card** – shows the current MeshCenter time, its timezone, and sync status
- **12h / 24h format** – switchable in Settings → Units, applied globally to every timestamp in the interface
- **Automatic time-source detection** – NTP (`systemd-timesyncd`), hardware RTC, or the system clock, in that order of preference
- **Meshtastic node time sync** – MeshCenter automatically sets the exact time on the connected radio node via the `setTime()` API on (re)connect
- **e-Paper display integration** – the current time is shown on all info screens of both supported displays (Waveshare 2.13" 4-color and WeAct 1.54"), without increasing the physical refresh rate

### 🔔 Notification Center

A new **"Notifications"** card sits between the Time card and the Weather card.

- Persistent event history – stays visible when you come back to the screen
- Unread-count badge in the card header
- Notifications from every source in one place: schedules, timers, system events
- Each notification can be dismissed individually, or cleared all at once
- Works alongside the existing toast popup system: toasts are for an immediate heads-up, the card is for history

### 📅 Schedule Engine

A scheduling system that runs actions automatically, on a time basis.

**Two trigger modes:**
- **At a specific time** – a fixed hour/minute, with day-of-week selection
- **Every N minutes** – a fixed interval, independent of time of day

**Three action types:**
- 📋 **System Log entry** – a quiet log record, no other side effects
- 📩 **Send a mesh message** – a static text message to a specific node or channel
- 📊 **Send a data report** – an automatic report built from current telemetry (voltage, temperature, humidity, pressure, battery, weather), with field selection, a compact/line-by-line format, and a stale-data policy

**Notifications on fire:**
- A short **signal** (up to 95 characters) sent over Meshtastic to a node or channel
- **Details** – the full text or task list, shown in the notification card and the System Log

Schedules persist across service restarts. If MeshCenter doesn't have a trusted, synchronized time, schedules do not run – to avoid firing on a wrong clock.

### ⏱ Timers

Two modes:

- **Stopwatch** – counts up from the moment it's started
- **Countdown** – a timer for a fixed duration (HH:MM:SS); on reaching zero it automatically creates a notification and, optionally, sends a signal over Meshtastic

Timers are in-memory (reset on service restart). On restart, a notification is recorded in the notification card for each timer that was reset this way.

### ⚙️ Under the hood

- **Centralized time formatter** (`TimeFormatter`) – a single formatting point for the whole interface, aware of the active UI language and the 12h/24h setting
- **i18n consistency check script** (`scripts/check-i18n.py`) – automatically verifies the EN/DE/RU/UK translation catalogs stay in sync
- **Form preference persistence** – selected channel/node and checkbox state in composer-style forms (Schedule, Timer) are saved server-side, scoped to the active radio profile

These new features (Time System, Notification Center, Schedule Engine, Timers) are fully translated for all four supported interface languages (English, Deutsch, Русский, Українська) – unlike some older parts of the interface noted in the Localization (i18n) section above.

## ⚙ Action Engine

MeshCenter now includes an internal Action Engine responsible for processing interactive operations between the user interface and backend services.

The Action Engine provides:

- Centralized action handling
- Extensible architecture
- Better separation between UI and backend
- Foundation for future automation features

## 🆔 Runtime Identity

MeshCenter automatically detects the connected local Meshtastic node and maintains a runtime identity.

This improves:

- Local node detection
- Backend synchronization
- Message handling
- Future multi-node support

---

## Camera Notes

Camera support depends on Raspberry Pi system packages.

The recommended way to create the virtual environment (with `--system-site-packages`) is already described in the installation section. Without this option, Python inside the virtual environment may not see system packages such as:

- Picamera2
- Pillow (system version)
- libcamera-related bindings

To test camera imports:

```bash
source venv/bin/activate
python - <<'PY'
try:
    from picamera2 import Picamera2
    print("Picamera2 OK")
except Exception as e:
    print("Picamera2 ERROR:", e)

try:
    from PIL import Image
    print("Pillow OK")
except Exception as e:
    print("Pillow ERROR:", e)
PY
```

### Recommended Camera Settings

For Raspberry Pi Zero 2W, conservative camera settings are recommended.

| Setting            | Recommended          |
|--------------------|----------------------|
| Video Resolution   | 640 × 480 or 800 × 600 |
| FPS                | 8–15                 |
| JPEG Quality       | 70–85                |
| Photo Preview      | 640 × 480            |
| Photo Capture      | 3280 × 2464          |

Higher settings may work, but they increase CPU usage, memory usage and heat.

### Recommended Raspberry Pi Zero 2W Usage

Raspberry Pi Zero 2W is powerful enough for MeshCenter, but it has limited resources.

For best stability:

- Avoid unnecessarily high video FPS
- Avoid very high JPEG quality for live video
- Keep browser polling intervals reasonable
- Use a reliable power supply
- Use a good microSD card
- Keep the enclosure ventilated
- Monitor CPU temperature during long camera sessions

MeshCenter is designed to remain lightweight, but camera streaming and photo capture can still temporarily increase system load.

---

## Application Architecture

MeshCenter consists of several independent modules that work together.

```
                    Web Browser
                          │
                          │ HTTP
                          ▼
                   Flask Application
                          │
     ┌──────────────┬──────────────┬──────────────┬──────────────┐
     │              │              │              │              │
     ▼              ▼              ▼              ▼              ▼
 Messaging      Camera        Telemetry      System/Health   REST API
     │              │              │              │
     └──────────────┼──────────────┴──────────────┘
                    ▼
             Meshtastic CLI
                    │
                    ▼
             LoRa Radio Device
```

The browser never communicates directly with the Meshtastic node. All communication is handled by the Flask application, which coordinates the different subsystems.

---

## Data Storage

MeshCenter intentionally avoids using an SQL database.

Instead, all application data is stored as JSON files.

**Advantages of this approach:**

- No database server required
- Easy backups
- Human-readable files
- Simple migration between Raspberry Pi devices
- Easy recovery after unexpected shutdowns

Typical stored files include:

```
messages.json
nodes.json
chats.json
telemetry_history.json
deleted_dm.json
sensors.json
settings.json
camera_config.json
node_icons/
screenshots/
```

Future versions may optionally support SQLite for installations with very large datasets, but JSON storage will remain the default.

---

## REST API

MeshCenter exposes a REST API used by the browser interface.

Examples of available endpoints include:

```
GET    /api/chats
GET    /api/messages
POST   /api/send

GET    /api/telemetry
GET    /api/telemetry/history
POST   /api/telemetry/config

GET    /api/sensors
GET    /api/base_status
GET    /api/radio_health

GET    /api/system/info
GET    /api/system/network
GET    /api/system/cpu-history
POST   /api/system/action

GET    /api/nodes
GET    /api/nodes_management
POST   /api/nodes_import
GET    /api/nodes_export

POST   /api/node_tools

GET    /api/camera/status
POST   /api/camera/settings
POST   /api/photo/capture
POST   /api/photo/save

GET    /api/weather/current
```

The API is primarily intended for the built-in web interface, but it also allows future integrations with third-party applications.

---

## Performance

MeshCenter has been optimized for Raspberry Pi Zero 2W.

Typical resource usage depends on the enabled features. Approximate values measured on a Raspberry Pi Zero 2W:

| Feature             | CPU (approx.)      | RAM (approx.) |
|---------------------|--------------------|---------------|
| Idle                | < 5 %              | 40–60 MB      |
| Messaging           | 5–15 %             | 50–80 MB      |
| Telemetry           | 5–10 %             | 50–70 MB      |
| Camera Preview      | 25–50 %            | 80–120 MB     |
| Photo Capture       | short peak 60–90 % | 90–130 MB     |
| System Monitoring   | 5–10 %             | 50–70 MB      |

Live MJPEG streaming is currently the most resource-intensive component. Values can vary depending on resolution, FPS, JPEG quality and the number of connected browser clients.

---

## Security

MeshCenter is intended for trusted local networks.

Current security model:

- Local network access
- No cloud dependency
- No external database
- Local JSON storage
- Local camera storage

If remote access is required, it is recommended to use a VPN or another secure tunnel instead of exposing the web interface directly to the Internet.

Future versions may include optional authentication.

---

## Major Features

- **Interactive Network Map** – Integrated Leaflet-based map with positioned nodes, reference marker, distance/bearing, synchronized selection with the node list, persistent positions, Fit Nodes view and node information popups
- **Messaging Improvements** – Native Meshtastic reply support, reply composer, quoted replies, jump to original message, message actions and improved Direct Messages
- **Map UI & Performance** – Redesigned map panel, larger markers, smoother interaction, faster updates, reduced redraws and optimized refresh logic
- **System & Radio Health** – Added system monitoring, radio health dashboard, CPU history chart and automatic listener recovery
- **Weather Module** – Pluggable weather provider (OpenWeather or WeatherAPI, selectable in Settings) with caching and location from reference node
- **Node Tools** – Added remote telemetry, position request and traceroute
- **Custom Node Icons** – Upload and manage icons for any node
- **Workspace & UI** – Persistent panel visibility, theme, compact mode; improved light/dark themes and layout consistency
- **Web Settings Editor** – Units, telemetry interval, battery capacity, listener recovery, external map-link provider, reference location
- **Media Gallery** – Thumbnails, download, delete, storage info
- **Export/Import** – Node database export/import in CSV and JSON
- **Wi‑Fi Manager** – Scan, connect, forget with saved networks indicator

---

## Known Limitations

| # | Description | Status |
|---|---|---|
| KI-001 | The e-Paper driver can hang on start with some HAT configurations | Investigating |
| KI-002 | The Meshtastic Python API (2.7.x) does not support reading a node's current time | Waiting on upstream |
| KI-003 | The field picker UI for the "Send data report" schedule action is still basic | Planned |
| KI-007 | Chat-list timestamps are formatted server-side and don't react to the 12h/24h toggle | Planned |

## Troubleshooting

### Meshtastic CLI not found

Check that the CLI is installed inside the virtual environment:

```bash
source venv/bin/activate
which meshtastic
```

Verify the configured path in `config.py`.

### Radio not detected

Verify that the device is connected:

```bash
ls /dev/ttyACM*
```

Test communication:

```bash
source venv/bin/activate
meshtastic --info
```

### Camera not working

Verify that Picamera2 is available:

```bash
source venv/bin/activate
python -c "from picamera2 import Picamera2"
```

If the import fails, recreate the virtual environment with system site packages:

```bash
deactivate
rm -rf venv
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements.txt
```

### Service does not start

Check the service status:

```bash
systemctl status meshcenter
```

View the logs:

```bash
journalctl -u meshcenter -f
```

### High CPU usage

Possible causes:

- High MJPEG frame rate
- Large preview resolution
- High JPEG quality
- Multiple browser clients
- Background image processing

Reducing camera settings usually has the greatest impact.

### Messages are not delivered

Verify:

- Same LoRa region
- Same channel
- Same PSK
- Compatible firmware versions
- Target node is reachable

Use the Meshtastic CLI to verify that communication works outside MeshCenter.

### Browser cannot connect

Verify that Flask is listening:

```bash
ss -tln
```

Default port: `5000`

Also check your firewall configuration.

### Weather data not showing

- Verify that the active provider's key (`OPENWEATHER_API_KEY` or `WEATHERAPI_API_KEY`) is set in `weather_secrets.py` - check which provider is active in Settings → Weather Provider
- Check that the reference location is configured (Settings → Reference location)
- Ensure the Raspberry Pi has internet access

### Node Tools commands fail

- Check that the target node is reachable (last heard time is recent)
- Ensure the serial port is not busy (wait a few seconds and retry)
- Check the System Log for detailed error messages

### Custom node icons not updating

- Clear your browser cache or reload the page with `Ctrl+F5`
- Verify that the uploaded image is a valid PNG, JPEG or WebP

### Wi‑Fi connection fails

- Check that the password is correct
- Ensure the network is visible in the scan list
- Verify that NetworkManager is installed and running

---

## Frequently Asked Questions

### Does MeshCenter support mobile devices?

MeshCenter is currently optimized for desktop web browsers. It is partially usable on many tablets, while full mobile optimization is planned for a future release.

### Does MeshCenter replace the official Meshtastic application?

No. MeshCenter complements the official applications by providing a permanent browser-based control center for Raspberry Pi installations.

### Does MeshCenter send photos over Meshtastic?

No. Photos are stored locally on the Raspberry Pi and viewed through the web interface.

### Can multiple browsers connect simultaneously?

Yes. Multiple users on the same local network can access the interface at the same time.

### Does MeshCenter require Internet access?

No. Internet access is not required for normal operation.

Some optional features, such as weather integration, external map tiles and software updates, require Internet connectivity.

### Which Raspberry Pi models are supported?

Recommended:

- Raspberry Pi Zero 2W
- Raspberry Pi 3
- Raspberry Pi 4
- Raspberry Pi 5

MeshCenter is primarily optimized for Raspberry Pi Zero 2W.

---

## Roadmap

MeshCenter is an actively developed project.

The primary goal is to provide a lightweight, reliable and feature-rich browser-based control center for Meshtastic base stations while keeping the installation simple and resource-efficient.

The roadmap is intentionally conservative. Features are added only after they have been tested and integrated without compromising stability.

### Current Development

#### 📈 Improved Telemetry

Future versions will extend telemetry visualization with:

- Better historical charts
- Long-term statistics
- Improved graph rendering
- Additional sensor support
- Improved data export

#### 🚀 Performance Improvements

Continuous optimization remains an important goal.

Future work includes:

- Faster page loading
- Lower memory usage
- Reduced CPU utilization
- Better responsiveness
- Improved camera performance

#### 🌍 Multi-language Interface

The i18n infrastructure is implemented and live (see "Localization (i18n)" under Core Features above) — language switching, `<html lang>`, part of the interface text, a first slice of error messages and the Weather module all work across English, German, Russian and Ukrainian.

What remains is writing the actual translations: German, Russian and Ukrainian currently ship as English placeholder text, and most of the JavaScript-rendered chat interface still needs to be wired into the translation system.

### Future Ideas

These ideas are being considered for future releases.  
Their implementation depends on project maturity and available development time.

#### 🧩 Plugin Support

A plugin architecture could allow optional modules without increasing the complexity of the core application.

Possible plugins:

- Weather services
- Telegram notifications
- MQTT integration
- Grafana / InfluxDB exporters
- APRS gateway
- Custom sensor modules

#### 🗺 Network Map Enhancements

Further map improvements under consideration:

- Signal quality overlays
- Routing / traceroute visualization on the map
- Favorite node emphasis
- Last-heard indicators

#### 📦 Additional Integrations

Possible future integrations include:

- Home Assistant
- Node-RED
- MQTT brokers
- REST integrations
- Additional environmental sensors

## Version History

| Version | Highlights |
|----------|------------|
| v1.4.0 | Localization (i18n) infrastructure — language switching, translation runtime, first slice of localized errors and weather; translations pending |
| v1.3.0 | Waypoints, Notifications, Action Engine |
| v1.2.0 | Interactive Map |
| v1.0.0 | First Stable Release |

---

## Contributing

Contributions are welcome.

If you find a bug, have an idea for an improvement or would like to contribute code, please open an Issue or submit a Pull Request.

Suggestions for improving the documentation are also greatly appreciated.

### Reporting Issues

When reporting a problem, please include as much information as possible.

Useful information includes:

- Raspberry Pi model
- Raspberry Pi OS version
- Python version
- Meshtastic firmware version
- Meshtastic CLI version
- Browser
- Relevant log messages
- Steps required to reproduce the issue

Providing detailed information helps identify and resolve problems more quickly.

---

## License

This project is released under the MIT License.

You are free to use, modify and distribute the software in accordance with the terms of the license.

See the [LICENSE](LICENSE) file for details.

---

## Acknowledgements

Special thanks to:

- The Meshtastic Team
- The Raspberry Pi Foundation
- The open-source community
- Everyone who tests MeshCenter and shares feedback

Their work and support make projects like this possible.

---

## Support

If you enjoy the project, consider supporting it by:

- ⭐ Starring the repository
- Reporting bugs
- Suggesting new features
- Sharing the project with other Meshtastic users
- Contributing improvements

Community feedback plays an important role in shaping future development.

---

## Author

**Kostiantyn Vynohradov (FlintUA)**  
Electronics engineer, embedded systems enthusiast and Meshtastic hobbyist.

- Interactive Live Demo MeshCenter - Meshtastic Control Center: https://meshcenter.elektroniker.help/preview/
- Information Center MeshCenter - Meshtastic Control Center: https://meshcenter.elektroniker.help/
- GitHub: [https://github.com/FlintUA](https://github.com/FlintUA)
- Project repository: [https://github.com/FlintUA/MeshCenter](https://github.com/FlintUA/MeshCenter)
- Website: [https://elektroniker.help](https://elektroniker.help)

The website contains additional articles, practical projects and experiments related to Meshtastic, Raspberry Pi, embedded systems, electronics and 3D printing.

---

## Disclaimer

MeshCenter is an independent open-source project created for the Meshtastic community.

It is not affiliated with or endorsed by the official Meshtastic project.

Meshtastic® is a trademark of its respective owners.

---

<p align="center">
Made with ❤️ for the Meshtastic community
</p>
