# Flint Meshtastic Web UI

<img src="https://github.com/user-attachments/assets/0847ebce-bf96-4b5d-8b3e-3b92bbd3d440" width="170">`{=html}
A lightweight and modern web interface for Meshtastic nodes running on
Raspberry Pi.
![Version](https://img.shields.io/badge/version-v1.1.0--dev-blue)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-success)
![Meshtastic](https://img.shields.io/badge/Meshtastic-Compatible-green)
![License](https://img.shields.io/badge/license-MIT-orange)
------------------------------------------------------------------------

# ✨ Highlights

-   ✅ Public & Direct Chats
-   ✅ Live MJPEG Video Streaming
-   ✅ High Resolution Photo Capture
-   ✅ Raspberry Pi Camera Support (Picamera2)
-   ✅ Sensor Dashboard
-   ✅ BME280 Support
-   ✅ INA226 Support
-   ✅ Telemetry History
-   ✅ Node Discovery & Monitoring
-   ✅ Responsive Web Interface
-   ✅ Optimized for Raspberry Pi Zero 2W

------------------------------------------------------------------------

# 💬 Messaging

-   Public LongFast channel
-   Direct node-to-node messages
-   Emoji picker
-   Chat history
-   Automatic refresh
-   Message timestamps

# 📡 Node Management

-   Automatic node discovery
-   RSSI / SNR indicators
-   Hardware detection
-   Favorites
-   Node search
-   Last seen timer

# 📊 Telemetry

### Meshtastic

-   Battery level
-   Voltage
-   Channel utilization
-   Air utilization
-   Uptime

### External Sensors

-   🌡️ Temperature
-   💧 Humidity
-   🌍 Pressure
-   ⚡ Voltage
-   🔋 Current
-   🔥 Power

Telemetry history is automatically stored and displayed.

# 📷 Camera

Powered by **Picamera2**.

Features:

-   🎥 Live MJPEG stream
-   📸 High resolution photo capture
-   🖼️ Live preview
-   🗂️ Gallery
-   ⬇️ Download images
-   ⚙️ Adjustable resolution
-   🎚️ Adjustable FPS
-   🎨 JPEG quality control

The application automatically switches between optimized video and photo
modes.

# 🧪 Tested Hardware

## Raspberry Pi

-   Raspberry Pi Zero 2W

## Meshtastic

-   RAK4631
-   RAK WisMesh TAP V2
-   LILYGO T-Beam
-   LILYGO T-Echo Plus

## Sensors

-   BME280
-   INA226

## Camera

-   OV5647

# 📦 Installation

``` bash
git clone https://github.com/FlintUA/flint-meshtastic-web-ui.git
cd flint-meshtastic-web-ui
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

# ⚙️ Configuration

Edit `config.py`

``` python
LOCAL_NODE_ID="!067a40fa"
LOCAL_NODE_NAME="Flint Base"
MESHTASTIC_CMD="/home/flint/.local/bin/meshtastic"
```

Find your node ID:

``` bash
meshtastic --info
```

# 🚀 Run

Development:

``` bash
python3 server.py
```

Systemd:

``` bash
sudo systemctl enable mesh-web
sudo systemctl start mesh-web
sudo systemctl restart mesh-web
```

# 🗂️ Project Structure

``` text
server.py
config.py
templates/
static/
docs/
data/
```

# 🏗️ Architecture

``` text
Browser
    │
    ▼
 Flask Web Server
    │
 ┌──┼──────────────┐
 ▼  ▼              ▼
Meshtastic CLI  Picamera2  JSON Storage
```

# 🚧 Roadmap

### Completed

-   ✅ Chat
-   ✅ Direct Messages
-   ✅ Sensor Dashboard
-   ✅ Live Video
-   ✅ Photo Capture
-   ✅ Gallery

### In Progress

-   🚧 Camera stability improvements
-   🚧 Telemetry synchronization
-   🚧 Code refactoring
-   🚧 Performance optimization

### Planned

-   🌤️ Weather dashboard
-   🔌 Plugin support
-   🌙 Dark theme
-   📡 WebSocket updates
-   📁 File manager

# 🤝 Contributing

Contributions, bug reports and pull requests are welcome.

# 📄 License

MIT License

# 👨‍💻 Author

**Kostiantyn Vynohradov (FlintUA)**

GitHub: https://github.com/FlintUA

Project: https://github.com/FlintUA/flint-meshtastic-web-ui

# ⭐ Support

If this project is useful for you, please consider giving it a **⭐
Star** on GitHub.

It helps the project become more visible to the Meshtastic community.

------------------------------------------------------------------------

Made with ❤️ for the Meshtastic community.
