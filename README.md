# <img width="128" height="128" alt="meshnode_light_full_2 копия" src="https://github.com/user-attachments/assets/ce14d260-51b2-414a-9354-56e01f40e959" />
# Meshtastic Web UI v1.0.0

## First Stable Release

A modern web interface for Meshtastic nodes designed for Raspberry Pi Zero 2W and other Linux-based systems.

### ✨ Highlights

- Real-time Meshtastic chat interface
- Public channel messaging (LongFast)
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
- Chat history persistence
- Message timestamps
- Emoji support with popup picker

### Node Management
- Automatic node discovery
- Live node list
- Signal quality indicators
- RSSI and SNR display
- Hardware identification
- Last seen tracking
- Node filtering and search

### Device Dashboard
- Voltage monitoring
- Battery level estimation
- Channel utilization
- Air utilization statistics
- Uptime display

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
git clone https://github.com/FlintUA/meshtastic-web-ui.git
cd meshtastic-web-ui

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 🚀 Start

### Development

```bash
python3 app.py
```

### Production (systemd)

```bash
sudo systemctl enable mesh-web.service
sudo systemctl start mesh-web.service
```

Open:

```text
http://RASPBERRY_IP:5000
```

---

## 👨‍💻 Author

**Kostiantyn Vynohradov (FlintUA)**

GitHub: https://github.com/FlintUA

Project: https://github.com/FlintUA/meshtastic-web-ui

---

## ❤️ Support

If you find this project useful, please give it a ⭐ on GitHub.

**Made for the Meshtastic community**
