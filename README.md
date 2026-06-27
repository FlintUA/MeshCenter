# Flint Meshtastic Web UI

<p align="center">

<img src="https://github.com/user-attachments/assets/0847ebce-bf96-4b5d-8b3e-3b92bbd3d440" width="170">

</p>

<p align="center">

A lightweight and modern web interface for Meshtastic nodes running on Raspberry Pi.

</p>

<p align="center">

![Version](https://img.shields.io/badge/version-v1.1.0--dev-blue)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-success)
![Meshtastic](https://img.shields.io/badge/Meshtastic-Compatible-green)
![License](https://img.shields.io/badge/license-MIT-orange)

</p>

---

# Features

## Messaging

* Public LongFast chat
* Direct node-to-node messaging
* Automatic chat history
* Emoji picker
* Message timestamps
* Automatic updates

---

## Node Management

* Automatic node discovery
* Live node list
* RSSI / SNR display
* Last seen timer
* Hardware model detection
* Favorites
* Node search

---

## Telemetry

Supports both Meshtastic telemetry and external sensors.

### Meshtastic

* Battery level
* Voltage
* Channel utilization
* Air utilization
* Uptime

### External Sensors

* BME280

  * Temperature
  * Humidity
  * Pressure

* INA226

  * Voltage
  * Current
  * Power

Telemetry history is automatically stored and displayed.

---

# Camera Support

Built-in Raspberry Pi Camera support using **Picamera2**.

Features:

* Live MJPEG video
* Adjustable resolution
* Adjustable FPS
* JPEG quality control
* High-resolution photo capture
* Live preview
* Gallery
* Download captured images

The application automatically switches between optimized video mode and full-resolution photo mode.

---

# Screenshots

## Main Interface

![Main Interface](docs/images/main-ui.png)

---

# Tested Hardware

### Raspberry Pi

* Raspberry Pi Zero 2W

### Meshtastic Devices

* RAK4631
* RAK WisMesh TAP V2
* LILYGO T-Beam
* LILYGO T-Echo Plus

### Sensors

* BME280
* INA226

### Camera

* Raspberry Pi Camera OV5647

---

# Installation

```bash
git clone https://github.com/FlintUA/flint-meshtastic-web-ui.git

cd flint-meshtastic-web-ui

python3 -m venv venv

source venv/bin/activate

pip install -r requirements.txt
```

---

# Configuration

Edit:

```bash
config.py
```

Example:

```python
LOCAL_NODE_ID = "!067a40fa"

LOCAL_NODE_NAME = "Flint Base"

MESHTASTIC_CMD = "/home/flint/.local/bin/meshtastic"

DATA_DIR = "/home/flint/mesh_web/data"
```

Find your Node ID:

```bash
meshtastic --info
```

---

# Run

Development:

```bash
python3 server.py
```

Production:

```bash
sudo systemctl enable mesh-web

sudo systemctl start mesh-web
```

Restart:

```bash
sudo systemctl restart mesh-web
```

Status:

```bash
sudo systemctl status mesh-web
```

---

# Project Structure

```text
flint-meshtastic-web-ui/

├── server.py
├── config.py
├── requirements.txt
├── README.md
├── wsgi.py
│
├── templates/
│     └── index.html
│
├── static/
│     ├── chat.js
│     ├── style.css
│     └── chart.umd.min.js
│
├── docs/
│     └── images/
│
└── data/
      ├── chats.json
      ├── messages.json
      ├── nodes.json
      ├── sensors.json
      ├── telemetry_history.json
      └── screenshots/
```

---

# Architecture

```text
                    Browser
                       │
                       ▼
               Flask Web Server
                       │
      ┌────────────────┼────────────────┐
      │                │                │
      ▼                ▼                ▼
 Meshtastic CLI     Picamera2      JSON Storage
      │                │                │
      ▼                ▼                ▼
   RAK4631         OV5647 Camera     History
```

---

# Current Status

Implemented:

* Live chat
* Direct messages
* Sensor dashboard
* Node management
* Live camera
* Photo capture
* Gallery
* Persistent storage
* Automatic telemetry updates
* Responsive web interface

---

# Roadmap

### In Progress

* Improve camera stability
* Better telemetry synchronization
* Internal code refactoring
* Performance optimization

### Planned

* WebSocket updates
* File manager
* OTA configuration
* Plugin support
* Multiple camera support
* Weather dashboard
* Dark theme

---

# Requirements

* Raspberry Pi OS
* Python 3.11+
* Meshtastic CLI
* Flask
* Picamera2

---

# Contributing

Contributions, suggestions and pull requests are always welcome.

---

# License

MIT License

---

# Author

**Kostiantyn Vynohradov (FlintUA)**

GitHub

https://github.com/FlintUA

Project

https://github.com/FlintUA/flint-meshtastic-web-ui

---

# Support

If this project helps you, please consider giving it a ⭐ on GitHub.

Every star helps the project become more visible to the Meshtastic community.

---

Made with ❤️ for the Meshtastic community.
