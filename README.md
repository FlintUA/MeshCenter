# <img width="128" height="128" alt="meshnode_light_full_2 копия" src="https://github.com/user-attachments/assets/ce14d260-51b2-414a-9354-56e01f40e959" />

# Meshtastic Web UI v1.0.0

## First Stable Release

A modern web interface for Meshtastic nodes designed for Raspberry Pi Zero 2W and other Linux-based systems.

### ✨ Highlights

- Real-time Meshtastic chat interface
- Public channel messaging (LongFast)
- Direct node-to-node messaging
- Node discovery and monitoring
- Device status dashboard
- Sensor telemetry display
- Emoji picker support
- Responsive desktop and mobile layout
- Persistent JSON-based storage
- Systemd service support
- Optimized for low-power hardware

---

## 🎯 Features

### Messaging
- Send and receive messages in LongFast channel
- Direct messages between nodes
- Chat history persistence
- Message timestamps
- Emoji support with popup picker
- Automatic message updates

### Node Management
- Automatic node discovery
- Live node list
- Signal quality indicators
- RSSI and SNR display
- Hardware identification
- Last seen tracking
- Node filtering and search
- Favorite node support

### Device Dashboard
- Voltage monitoring
- Battery level estimation
- Channel utilization
- Air utilization statistics
- Uptime display
- Local node status monitoring

### Sensor Support
- Temperature
- Humidity
- Pressure
- Voltage
- Current
- Power

### User Interface
- Clean modern layout
- Desktop optimized
- Mobile friendly
- Fast updates
- Lightweight design
- Sidebar node management
- Responsive chat interface

---

## 🧪 Tested Hardware

- ✅ Raspberry Pi Zero 2W
- ✅ RAK4631 (Flint Base)
- ✅ LILYGO T-Beam
- ✅ LILYGO T-Echo Plus
- ✅ RAK WisMesh TAP V2

---

## 📦 Installation

```bash
git clone https://github.com/FlintUA/flint-meshtastic-web-ui.git
cd flint-meshtastic-web-ui

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## ⚙️ Configuration

Before starting the application, edit the configuration file:

```bash
nano config.py
```

### Local Node Settings

Set your local Meshtastic node ID and node name:

```python
LOCAL_NODE_ID = "!067a40fa"
LOCAL_NODE_NAME = "Flint Base"
```

Find your node ID using:

```bash
meshtastic --info
```

Look for an entry similar to:

```text
"!067a40fa"
```

and use that value as `LOCAL_NODE_ID`.

### Meshtastic CLI Path

If Meshtastic CLI is installed in a different location, update:

```python
MESHTASTIC_CMD = "/home/flint/.local/bin/meshtastic"
```

Verify the path:

```bash
which meshtastic
```

### Data Storage

Default data directory:

```python
DATA_DIR = "/home/flint/mesh_web/data"
```

Application files:

```text
messages.json   - chat history
nodes.json      - discovered mesh nodes
sensors.json    - sensor telemetry
chats.json      - direct message history
```

### Optional: Known Nodes

You can predefine frequently used nodes for friendly display names:

```python
KNOWN_NODES = {
    "!067a40fa": "Flint Base",
    "!b0f14d2a": "Flint_Echo",
    "!756f9960": "Flint TAP2",
    "!1fa065f0": "Elektroniker"
}
```

Additional node information:

```python
KNOWN_NODE_INFO = {
    "!067a40fa": {
        "short_name": "FLTB",
        "hw_model": "RAK4631"
    }
}
```

This is optional but improves node identification and display.

---

## 🚀 Start

### Development

```bash
python3 server.py
```

### Production (systemd)

Enable automatic startup:

```bash
sudo systemctl enable mesh-web.service
sudo systemctl start mesh-web.service
```

Check status:

```bash
sudo systemctl status mesh-web.service
```

Restart service:

```bash
sudo systemctl restart mesh-web.service
```

---

## 🌐 Access Web Interface

Open in browser:

```text
http://RASPBERRY_IP:5000
```

Example:

```text
http://192.168.2.103:5000
```

---

## 📂 Project Structure

```text
flint-meshtastic-web-ui/
│
├── server.py              # Flask server
├── config.py              # Configuration
├── wsgi.py                # WSGI entry point
├── requirements.txt
├── README.md
│
├── static/
│   ├── app.js             # Client-side logic
│   └── style.css          # Styles
│
├── templates/
│   └── index.html         # Main page
│
└── data/
    ├── messages.json
    ├── nodes.json
    ├── chats.json
    └── sensors.json
```

---

## 📋 Requirements

- Raspberry Pi OS (or any Linux)
- Python 3.8+
- Meshtastic CLI
- Flask

Install Meshtastic CLI:

```bash
pip install meshtastic
```

---

## 🤝 Contributing

Contributions are welcome.

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push your branch
5. Open a Pull Request

---

## 📄 License

MIT License

---

## 👨‍💻 Author

**Kostiantyn Vynohradov (FlintUA)**

GitHub:
https://github.com/FlintUA

Project:
https://github.com/FlintUA/flint-meshtastic-web-ui

---

## ❤️ Support

If you find this project useful, please give it a ⭐ on GitHub.

**Made with ❤️ for the Meshtastic community**
