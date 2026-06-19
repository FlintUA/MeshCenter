# Meshtastic Web Chat

Simple web chat interface for Meshtastic nodes.

Designed and tested on Raspberry Pi Zero 2W.

## Features

* Send messages to LongFast channel 0
* Direct messages to specific nodes
* Receive messages in real time
* Node discovery and status monitoring
* Mobile-friendly responsive interface
* Lightweight and suitable for Raspberry Pi Zero 2W
* Message history with persistent storage
* Emoji support with category picker (384 emojis)
* Sensor data display (temperature, humidity, pressure, battery)
* GitHub version control support
* Works through Meshtastic CLI


## Requirements

* Raspberry Pi OS
* Python 3
* Meshtastic CLI
* Flask

## Installation

```bash
git clone https://github.com/FlintUA/meshtastic-web-ui.git
cd meshtastic-web-ui
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

## Open browser:

```text
http://RASPBERRY_IP:5000
```

Example:

```text
http://192.168.2.103:5000
```

## Tested Hardware

* Raspberry Pi Zero 2W
* RAK4631 (Flint Base)
* LILYGO T-Beam
* LILYGO T-Echo Plus
* RAK WisMesh TAP V2

## Current Status

Project is under active development.

Planned features:

* Node name detection
* Private messages
* Known nodes list
* Message history
* Better mobile interface
* Telemetry support

## Author

Kostiantyn Vynohradov (FlintUA)

GitHub:
https://github.com/FlintUA
