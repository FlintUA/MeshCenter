<p align="center">
  <img src="https://github.com/user-attachments/assets/0847ebce-bf96-4b5d-8b3e-3b92bbd3d440" width="180" alt="Meshtastic Powered">
</p>

<h1 align="center">MeshCenter</h1>

<p align="center">
Modern Web Control Center for Meshtastic nodes running on Raspberry Pi.
</p>

<p align="center">

![Version](https://img.shields.io/badge/version-v1.1.0--dev-blue)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-success)
![Meshtastic](https://img.shields.io/badge/Meshtastic-Compatible-green)
![License](https://img.shields.io/badge/license-MIT-orange)

</p>

---
## 🚀 What can it do?

✔ Public and Private Chat

✔ Live Camera Streaming

✔ High Resolution Photo Capture

✔ BME280 & INA226 Telemetry

✔ Node Management

✔ Raspberry Pi Dashboard

✔ Optimized for Raspberry Pi Zero 2W

# Overview

MeshCenter is a lightweight web interface designed for **Meshtastic** nodes running on **Raspberry Pi**.

It provides messaging, telemetry, camera streaming, photo gallery, sensor monitoring and node management through a modern browser interface.

The project provides a modern browser-based dashboard for messaging, node management, telemetry monitoring, live camera streaming and photo capture while remaining optimized for low-power hardware such as the Raspberry Pi Zero 2W.

Unlike the official Meshtastic applications, this project focuses on providing a permanent web dashboard that can be accessed from any device on the local network without requiring additional software.

---

# ✨ Highlights

- ✅ Public LongFast messaging
- ✅ Direct node-to-node messaging
- ✅ Live node discovery
- ✅ Device dashboard
- ✅ BME280 environmental telemetry
- ✅ INA226 power monitoring
- ✅ Live MJPEG video streaming
- ✅ High-resolution photo capture
- ✅ Raspberry Pi Camera support (Picamera2)
- ✅ Telemetry history
- ✅ Responsive web interface
- ✅ Optimized for Raspberry Pi Zero 2W

---

# 🚀 Features

## 💬 Messaging

The messaging system supports both public and private communication.

Features include:

- Public LongFast channel messaging
- Direct messages between nodes
- Automatic chat history
- Message timestamps
- Emoji picker
- Automatic message refresh
- Local JSON storage
- System message support

---

## 📡 Node Management

The application automatically discovers nearby nodes and maintains a local database.

Features:

- Automatic node discovery
- Live node list
- RSSI display
- SNR display
- Hardware model detection
- Last seen timer
- Favorite nodes
- Ignore list
- Search and filtering
- Import and export node database

---

## 📊 Telemetry Dashboard

Supports telemetry received from Meshtastic devices together with additional sensor information.

### Device Telemetry

- Battery level
- Voltage
- Channel utilization
- Air utilization
- Uptime
- Last update time

### Environmental Sensors

Supported sensors:

- 🌡️ Temperature
- 💧 Humidity
- 🌍 Atmospheric pressure

Supported hardware:

- BME280

### Power Monitoring

Supported values:

- ⚡ Voltage
- 🔋 Current
- 🔥 Power

Supported hardware:

- INA226

Telemetry history is stored locally and can be displayed as charts for long-term monitoring.

---

## 📷 Camera Support

The integrated camera module is based on **Picamera2**.

Current functionality:

- 🎥 Live MJPEG video stream
- 📸 High-resolution photo capture
- 🖼️ Live preview
- ⚙️ Adjustable video resolution
- ⚙️ Adjustable photo resolution
- 🎚️ Adjustable FPS
- 🎨 Adjustable JPEG quality
- 💾 Local image storage
- 📂 Screenshot gallery

The application automatically switches between optimized video mode and full-resolution photo mode to reduce system load on Raspberry Pi Zero 2W.

---

## 🖥️ User Interface

The interface is designed to be simple, responsive and lightweight.

Features:

- Modern layout
- Desktop optimized
- Mobile friendly
- Sidebar node management
- Chats / Video / Photo tabs
- Telemetry cards
- Fast page updates
- Lightweight design

---

# 📸 Screenshots

## Main Interface

![Main Interface](docs/images/main-ui.png)

---

# 🧪 Tested Hardware

## Raspberry Pi

- ✅ Raspberry Pi Zero 2W

## Meshtastic Devices

- ✅ RAK4631 (Flint Base)
- ✅ RAK WisMesh TAP V2
- ✅ LILYGO T-Beam
- ✅ LILYGO T-Echo Plus

## Sensors

- ✅ BME280
- ✅ INA226

## Camera

- ✅ OV5647 Camera Zero V2.2
  
# 📦 Installation

## Clone the Repository

```bash
git clone https://github.com/FlintUA/meshcenter.git
cd meshcenter
```

## Create a Python Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

## Install Python Dependencies

```bash
pip install -r requirements.txt
```

---

## Install Camera Support

For Raspberry Pi Camera support install:

```bash
sudo apt update
sudo apt install python3-picamera2 python3-pil
```

Verify that the camera is working:

```bash
rpicam-hello
```

If the preview starts correctly, the camera is ready.

---

# ⚙️ Configuration

Before starting the application, edit:

```bash
nano config.py
```

---

## Local Node Settings

Specify your local Meshtastic node.

Example:

```python
LOCAL_NODE_ID = "!067a40fa"
LOCAL_NODE_NAME = "Flint Base"
```

To find your Node ID:

```bash
meshtastic --info
```

Look for something similar to:

```text
"!067a40fa"
```

---

## Meshtastic CLI

Set the path to your Meshtastic CLI executable.

Example:

```python
MESHTASTIC_CMD = "/home/flint/.local/bin/meshtastic"
```

Verify:

```bash
which meshtastic
```

---

## USB Serial Port

Specify the serial device connected to your Meshtastic node.

Example:

```python
MESHTASTIC_PORT = "/dev/ttyACM0"
```

Useful commands:

```bash
lsusb
```

```bash
ls -l /dev/ttyACM*
```

Typical output:

```text
Bus 001 Device XXX:
Adafruit WisCore RAK4631 Board
```

---

## Data Directory

Application data are stored locally.

Example:

```python
DATA_DIR = "/home/flint/mesh_web/data"
```

Files created automatically:

```text
messages.json
nodes.json
chats.json
sensors.json
telemetry_history.json
deleted_dm.json
screenshots/
```

---

## Known Nodes (Optional)

You can assign friendly names to frequently used nodes.

Example:

```python
KNOWN_NODES = {
    "!067a40fa": "Flint Base",
    "!b0f14d2a": "Flint Echo",
    "!756f9960": "Flint TAP2",
    "!1fa065f0": "Elektroniker"
}
```

Additional information may also be defined:

```python
KNOWN_NODE_INFO = {
    "!067a40fa": {
        "short_name": "FLTB",
        "hw_model": "RAK4631"
    }
}
```

This section is optional but greatly improves the user interface.

---

# 🚀 Running

## Development Mode

Activate the virtual environment:

```bash
source venv/bin/activate
```

Run the server:

```bash
python3 server.py
```

---

## Automatic Startup (systemd)

Enable service:

```bash
sudo systemctl enable mesh-web.service
```

Start service:

```bash
sudo systemctl start mesh-web.service
```

Restart service:

```bash
sudo systemctl restart mesh-web.service
```

Check service status:

```bash
sudo systemctl status mesh-web.service
```

View live logs:

```bash
journalctl -u mesh-web -f
```

---

# 🌐 Web Interface

Open your browser and navigate to:

```text
http://RASPBERRY_IP:5000
```

Example:

```text
http://192.168.2.103:5000
```

---

# 📁 Project Structure

```text
meshcenter/

│
├── server.py
├── config.py
├── config.example.py
├── wsgi.py
├── requirements.txt
├── README.md
├── LICENSE
│
├── templates/
│   └── index.html
│
├── static/
│   ├── chat.js
│   ├── style.css
│   ├── chart.umd.min.js
│   └── favicon.png
│
├── docs/
│   └── images/
│
└── data/
    ├── chats.json
    ├── messages.json
    ├── nodes.json
    ├── sensors.json
    ├── telemetry_history.json
    ├── deleted_dm.json
    └── screenshots/
```

---

# 🏗️ Architecture

```
                 Browser
                     │
                     ▼
             Flask Web Server
                     │
      ┌──────────────┼──────────────┐
      │              │              │
      ▼              ▼              ▼
 Meshtastic CLI   Picamera2    JSON Storage
      │              │              │
      ▼              ▼              ▼
  RAK4631 Node   OV5647 Camera   Local Database
```

The application communicates with the Meshtastic node through the official Meshtastic CLI, while Picamera2 provides camera functionality. Persistent data are stored locally in JSON files.

---

# 🚧 Roadmap

The project is under active development.

## ✅ Completed

- Public LongFast messaging
- Direct node-to-node messaging
- Live node discovery
- Device dashboard
- Battery monitoring
- BME280 telemetry
- INA226 telemetry
- Telemetry history
- Live MJPEG video streaming
- High-resolution photo capture
- Screenshot gallery
- Responsive web interface
- Raspberry Pi Zero 2W optimization

---

## 🔄 In Progress

Current development focuses on improving reliability and architecture.

- Improve camera stability
- Improve telemetry synchronization
- Improve Meshtastic communication
- Reduce CPU usage
- Internal code refactoring
- Better error handling
- Faster page loading

---

## 💡 Planned Features

Future development ideas include:

- 🌤️ Weather dashboard
- 🌙 Dark mode
- 🔌 Plugin system
- 📁 File manager
- 📹 Video recording
- 📡 WebSocket communication
- 📲 Progressive Web App (PWA)
- 🌍 Multi-language interface
- 📈 Advanced telemetry graphs
- 🔔 Notification system

---

# ⚠️ Current Limitations

This project is still under active development.

Known limitations include:

- Communication currently relies on the Meshtastic CLI.
- Raspberry Pi Zero 2W has limited CPU and memory resources.
- High-resolution photo capture temporarily increases system load.
- MJPEG provides excellent browser compatibility but is not the most bandwidth-efficient streaming format.
- Camera, telemetry and serial communication share limited system resources.

For best performance on Raspberry Pi Zero 2W the recommended camera settings are:

| Setting | Recommended |
|----------|-------------|
| Video Resolution | 640 × 480 |
| FPS | 8–12 |
| JPEG Quality | 70–80 |
| Photo Preview | 640 × 480 |
| Photo Capture | 2592 × 1944 |

---

# 🛠 Useful Commands

### Activate Virtual Environment

```bash
source venv/bin/activate
```

---

### Start Development Server

```bash
python3 server.py
```

---

### Restart Service

```bash
sudo systemctl restart mesh-web
```

---

### View Service Status

```bash
sudo systemctl status mesh-web
```

---

### View Live Logs

```bash
journalctl -u mesh-web -f
```

---

### Check Connected Meshtastic Device

```bash
lsusb
```

```bash
ls -l /dev/ttyACM*
```

---

### Check Meshtastic Information

```bash
meshtastic --port /dev/ttyACM0 --info
```

---

### Test Raspberry Pi Camera

```bash
rpicam-hello
```

---

# 🤝 Contributing

Contributions are welcome.

If you would like to improve the project:

1. Fork the repository.
2. Create a feature branch.
3. Commit your changes.
4. Push your branch.
5. Open a Pull Request.

Bug reports, feature requests and suggestions are always appreciated.

---

# 📄 License

This project is released under the **MIT License**.

You are free to use, modify and distribute it in accordance with the license terms.

---

# 👨‍💻 Author

**Kostiantyn Vynohradov (FlintUA aka Elektroniker)**

Electronics engineer, embedded systems enthusiast and Meshtastic hobbyist.

More Meshtastic projects, electronics articles, Raspberry Pi experiments, 3D printing designs and repair tutorials:

🌐 **Website:** https://elektroniker.help

🔗 **GitHub:** https://github.com/FlintUA

📦 **Project:** https://github.com/FlintUA/meshcenter

---

# ❤️ Acknowledgements

This project would not be possible without the amazing work of:

- The Meshtastic Team
- Raspberry Pi Foundation
- Picamera2 Developers
- The Open Source Community

Thank you for creating such great tools.

---

# ⭐ Support

If you find this project useful, please consider giving it a **Star ⭐** on GitHub.

It helps other Meshtastic users discover the project and motivates further development.

If you have ideas or suggestions, feel free to open an Issue or start a Discussion.

---

<p align="center">

**Made with ❤️ for the Meshtastic community**

</p>
