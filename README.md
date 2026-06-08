# Meshtastic Web Chat

Simple web chat interface for Meshtastic nodes.

Designed and tested on Raspberry Pi Zero 2W.

## Features

* Send messages to LongFast channel 0
* Receive messages in real time
* Mobile-friendly interface
* Works through Meshtastic CLI
* Lightweight and suitable for Raspberry Pi Zero 2W
* GitHub version control support

## Requirements

* Raspberry Pi OS
* Python 3
* Meshtastic CLI
* Flask

## Installation

```bash
git clone https://github.com/FlintUA/meshnode-web-chat.git

cd meshnode-web-chat

python3 -m venv venv

source venv/bin/activate

pip install -r requirements.txt
```

## Run

```bash
python3 app.py
```

Open browser:

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
* Meshtastic EU868

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
