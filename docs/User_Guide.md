# MeshCenter User Guide

This guide covers a clean installation, first start, everyday operation, backup, update and basic troubleshooting for MeshCenter.

MeshCenter is designed for a Raspberry Pi connected by USB to a standard Meshtastic-compatible radio. The radio does not require hardware modification and does not need its own Wi-Fi connection. The Raspberry Pi provides the web interface and network access, while the radio continues to communicate with the mesh over LoRa.

## 1. Before you start

### Required hardware

- Raspberry Pi Zero 2 W, Raspberry Pi 3, 4 or 5
- Raspberry Pi OS Bookworm or newer
- Reliable microSD card, 16 GB minimum and 32 GB recommended
- Stable power supply
- Local network connection by Wi-Fi or Ethernet
- Meshtastic-compatible radio connected to the Raspberry Pi by a data-capable USB cable

The reference installation uses a Raspberry Pi Zero 2 W and a RAK4631-based node. Other standard Meshtastic radios that expose a supported USB serial connection can also be used.

### Optional hardware and services

- Raspberry Pi Camera supported by Picamera2
- Meshtastic environmental or power telemetry sensors
- API key for a weather provider (OpenWeather or WeatherAPI) for the weather card
- Internet access for weather, map tiles and software updates

### Configure the radio first

Use an official Meshtastic application to configure the connected radio before installing MeshCenter:

1. Set the correct LoRa region.
2. Configure the required channels and channel keys.
3. Set the node long name and short name.
4. Confirm that the radio can exchange messages with another node.

MeshCenter reads and uses the channels already stored on the radio. Channel creation, region settings, modem presets and keys remain under the official Meshtastic applications.

## 2. Clean installation

### Install Raspberry Pi OS and enable SSH

MeshCenter normally runs as a headless system, without a dedicated monitor, keyboard or mouse. The Raspberry Pi can be prepared and accessed remotely from another computer on the same local network.

Download and install the official [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on a Windows, macOS or Linux computer.

Insert the microSD card into the computer and open Raspberry Pi Imager.

Configure the image as follows:

1. Under **Device**, select your Raspberry Pi model.
2. Under **Operating System**, select **Raspberry Pi OS Lite (64-bit)**. Raspberry Pi OS with Desktop can also be used, but the desktop environment is not required for MeshCenter.
3. Under **Storage**, select the correct microSD card.
4. Open **OS Customisation** and configure the following items:

   - **Hostname** – choose a unique network name, for example `meshcenter`.
   - **Localisation** – select the correct time zone, country and keyboard layout.
   - **User** – create a Linux username and a strong password. Remember these credentials because they are required for SSH and `sudo` commands.
   - **Wi-Fi** – enter the Wi-Fi network name and password. For Raspberry Pi Zero 2 W, use a 2.4 GHz Wi-Fi network.
   - **Remote Access** – enable **SSH** and select **Use password authentication**.

The examples below assume:

```text
Hostname: meshcenter
Username: flint
```

Use the hostname and username that you actually entered in Raspberry Pi Imager.

Review the settings, select **Write**, and wait until writing and verification are complete. Writing the image erases all existing data on the selected microSD card.

For additional information, see the official [Raspberry Pi headless setup documentation](https://www.raspberrypi.com/documentation/computers/remote-access.html).

#### First boot and SSH connection

1. Remove the microSD card from the computer.
2. Insert the microSD card into a **fully powered-off** Raspberry Pi. Do not connect power yet.
3. On a Raspberry Pi Zero 2 W, leave additional expansion boards or HATs (for example TAP2) disconnected until the first boot and SSH access are confirmed.
4. Connect power through the **PWR IN** connector (or the normal power input for your Pi model).
5. Wait **3–5 minutes**. The first boot applies the Imager settings, expands the filesystem and joins the configured network; it often takes longer than later startups.

Make sure that the computer and Raspberry Pi are connected to the same local network.

On Windows, open PowerShell or Windows Terminal. On Linux or macOS, open a terminal.

First check that the Raspberry Pi is reachable on the network:

```bash
ping <hostname>.local
```

Example:

```bash
ping meshcenter.local
```

If the Pi responds to ping, connect with SSH:

```bash
ssh <username>@<hostname>.local
```

Example:

```bash
ssh flint@meshcenter.local
```

At the first connection, SSH displays a host authenticity warning similar to:

```text
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

Enter:

```text
yes
```

Then enter the password created in Raspberry Pi Imager. The password is not displayed while typing — this is normal.

A successful connection shows a prompt similar to:

```text
flint@meshcenter:~ $
```

You are now working in the Raspberry Pi terminal and can continue with the MeshCenter installation.

#### If the hostname does not work

Some local networks do not resolve `.local` hostnames correctly. Find the Raspberry Pi IP address in the router's list of connected devices and connect directly to that address:

```bash
ssh <username>@<raspberry-pi-ip>
```

Example:

```bash
ssh flint@192.168.1.50
```

If a monitor and keyboard are connected to the Raspberry Pi, its IP address can also be displayed locally with:

```bash
hostname -I
```

After connecting, verify the system identity and network address:

```bash
whoami
hostname
hostname -I
```

Do not expose SSH port 22 directly to the public internet. The installation procedure assumes access from a trusted local network.

### Prepare Raspberry Pi OS

Install the required system packages:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip network-manager iw lsof
```

For Raspberry Pi Camera support, also install:

```bash
sudo apt install -y python3-picamera2 rpicam-apps
```

Add the current user to the serial-port group:

```bash
sudo usermod -aG dialout "$USER"
sudo reboot
```

Reconnect after the reboot.

### Clone MeshCenter

The guide uses one consistent installation path: `~/meshcenter`.

```bash
cd ~
git clone https://github.com/FlintUA/MeshCenter.git meshcenter
cd ~/meshcenter
```

### Create the Python environment

`--system-site-packages` allows the virtual environment to use Picamera2 and other Raspberry Pi system packages.

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

Confirm that the Meshtastic CLI was installed inside the virtual environment:

```bash
which meshtastic
meshtastic --version
```

`which meshtastic` should report a path under `~/meshcenter/venv/bin/`.

### Find and test the USB radio

Connect the Meshtastic radio and list available serial devices:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

Most RAK4631-based USB installations appear as `/dev/ttyACM0`. Some radios use `/dev/ttyUSB0`.

Test communication before continuing:

```bash
source ~/meshcenter/venv/bin/activate
meshtastic --port /dev/ttyACM0 --info
```

The command must display the local node information without permission, connection or serial-port errors. Record the local node ID, long name and short name from the output.

### Create the local configuration

```bash
cd ~/meshcenter
cp config.example.py config.py
mkdir -p data
```

Open `config.py` in your preferred editor and set at least:

```python
MESHTASTIC_PORT = "/dev/ttyACM0"

LOCAL_NODE_ID = "!xxxxxxxx"
LOCAL_NODE_NAME = "My Base Station"

KNOWN_NODES = {
    "!xxxxxxxx": "My Base Station",
}

KNOWN_NODE_INFO = {
    "!xxxxxxxx": {"short_name": "BASE", "hw_model": "RAK4631"},
}
```

Replace the example node ID, name, short name and hardware model with the values reported by your radio. The current `config.example.py` automatically resolves the paths to the virtual environment and `data` directory, so installation-specific home-directory paths do not need to be entered manually.

`config.py`, `weather_secrets.py` and `data/` are local installation files and are intentionally excluded from Git.

### Optional weather setup

MeshCenter supports two weather providers, OpenWeather and WeatherAPI. Only one is active at a time, chosen in `Workspace > Settings > Weather Provider`, but a key for either (or both) can be kept in `weather_secrets.py`:

```bash
cd ~/meshcenter
cp weather_secrets.example.py weather_secrets.py
```

Insert the API key(s) into `weather_secrets.py`:

```python
OPENWEATHER_API_KEY = "your-openweather-api-key"
WEATHERAPI_API_KEY = "your-weatherapi-api-key"
```

The active provider and the reference location can later be selected in `Workspace > Settings > Weather Provider` and `Workspace > Settings > Reference location`.

### First manual start

```bash
cd ~/meshcenter
source venv/bin/activate
python server.py
```

Find the Raspberry Pi IP address in another terminal:

```bash
hostname -I
```

Open the interface from another computer on the same local network:

```text
http://<raspberry-pi-ip>:5000
```

Stop the manual server with `Ctrl+C` before installing the system service.

## 3. Run MeshCenter as a system service

The supplied deployment templates contain placeholders for the current Linux user and home directory. Render and install the service with:

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

The expected service state is `active (running)`.

### Enable System and Wi-Fi actions

MeshCenter can restart its service, reboot or shut down the Raspberry Pi, scan Wi-Fi networks and manage NetworkManager connections. These functions require narrowly scoped sudo permissions.

```bash
cd ~/meshcenter
MESH_USER="$(id -un)"

sed "s|__MESH_USER__|$MESH_USER|g" deploy/meshcenter.sudoers \
    | sudo tee /etc/sudoers.d/meshcenter >/dev/null
sed "s|__MESH_USER__|$MESH_USER|g" deploy/meshcenter-wifi.sudoers \
    | sudo tee /etc/sudoers.d/meshcenter-wifi >/dev/null

sudo chmod 440 /etc/sudoers.d/meshcenter /etc/sudoers.d/meshcenter-wifi
sudo visudo -cf /etc/sudoers.d/meshcenter
sudo visudo -cf /etc/sudoers.d/meshcenter-wifi
```

Do not add broad password-free sudo access. Only the commands listed in the supplied templates are required.

## 4. First-run checklist

Check the following items in order:

1. `meshtastic --port /dev/ttyACM0 --info` can read the radio.
2. `systemctl is-active meshcenter.service` reports `active`.
3. The web interface opens at `http://<raspberry-pi-ip>:5000`.
4. The bottom-right status indicator changes from `Checking` to an online state.
5. The configured channels appear in Chats.
6. The connected radio appears as the local base node.
7. A test channel message can be sent and received.
8. A direct message can be exchanged with a reachable node.
9. The System page shows Raspberry Pi, network and Radio Health information.
10. Camera live view and capture work if a camera is installed.

If the interface still shows an older version after an update, use `Ctrl+F5` in the browser.

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

## 5. Interface overview

MeshCenter uses four main areas:

- **Base panel on the left** - local-node status, sensor values, battery estimate, telemetry charts and weather.
- **Main workspace in the center** - Chats, Camera, Media and Devices.
- **Nodes and Tools panel on the right** - discovered nodes, node details and database tools.
- **Status dock at the bottom** - panel controls, Map, Workspace, notifications and radio status.

The **Devices** workspace is currently a prepared placeholder for future sensors, relays and GPIO integrations. Current BME280, INA226 and Meshtastic telemetry values are shown in the Base panel and telemetry charts.

## 6. Messaging

### Channel messages

Select a channel under `Chats > Channels`, enter the text and press Send. MeshCenter detects the channels stored on the connected radio and supports Meshtastic channel indexes 0 through 7.

If a channel is missing or marked as unavailable, configure it on the radio with an official Meshtastic application and restart the listener or MeshCenter.

### Direct messages

Select a conversation under `Direct Messages`, or select a node in the right panel and open its chat. Delivery depends on radio reachability and the mesh route.

### Replies and message actions

Use the action button on a message to:

- Reply with Meshtastic reply metadata
- Copy the message
- View message information
- Delete the message from local MeshCenter history

Deleting or clearing a message removes only the local stored copy. It cannot recall a LoRa message that was already transmitted.

### Favorites and ignored nodes

Favorites make important contacts easier to find. Ignored nodes are hidden from the normal list but can be displayed from the Tools filters. Ignoring a node changes local presentation and does not reconfigure the remote radio.

## 7. Nodes and node tools

Select a node in the right panel to inspect available information such as node ID, hardware model, role, last-heard time, RSSI, SNR, hops, position and telemetry.

Use `Rescan Network` when recently heard nodes do not appear. The Tools tab also provides:

- Export node database as CSV or JSON
- Import a previously exported node database
- Merge duplicate entries
- Display duplicates
- Clean up locally stored nodes
- Restart the Meshtastic listener

Remote node actions can request telemetry, position or traceroute information. A request may fail when the node is offline, sleeping, out of range or not supported by its firmware configuration.

## 8. Map and reference location

Open the Map menu in the bottom dock and choose:

- Hide map
- Full map
- Split view with the map above or below the active workspace

Only nodes with known coordinates appear on the map. `Fit nodes` adjusts the view to all positioned nodes.

To calculate distance and bearing, open `Workspace > Settings > Reference location` and choose either:

- Manual coordinates
- A Meshtastic node with a known position

The external map-provider option controls links that open a saved location. The integrated map uses its own Leaflet view. Internet access is normally required to load external map tiles.

## 9. Telemetry, weather and battery estimate

The Base panel shows the most recent available environmental, power and battery values. Open the Environment or Power chart to select a range from one hour to 30 days.

Telemetry only appears when the connected or remote node actually transmits the corresponding Meshtastic telemetry. Empty fields do not necessarily indicate a MeshCenter fault.

The battery-capacity value under `Workspace > Settings` is used only for an approximate runtime estimate. It is not a battery calibration value.

Weather requires an API key for the active provider (`Workspace > Settings > Weather Provider`) and a valid reference location. Click the weather status badge to request a refresh.

## 10. Camera and Media

The Camera workspace provides live view, video settings, photo settings, image controls and Screenshot capture. On Raspberry Pi Zero 2 W, begin with conservative settings such as 640 x 480 or 800 x 600 at 8 to 15 FPS.

Captured images are stored locally under `data/screenshots/` and appear in the Media gallery. They are not transmitted through Meshtastic. Media can be viewed, downloaded or deleted from the browser.

Turning the camera off releases resources when it is not required.

## 11. System, Wi-Fi and Workspace

Open `Workspace > System` to inspect:

- Hostname, uptime, CPU load and temperature
- RAM and disk use
- Wi-Fi, IP address, gateway and Internet state
- Meshtastic listener and Radio Health state
- Recent system events

`Restart Listener` restarts only radio listening. `Restart MeshCenter` restarts the application service. Raspberry Pi reboot and shutdown affect the whole host and should be used only when intended.

The Wi-Fi Manager can change the Raspberry Pi network connection. Changing to another network can immediately disconnect the current browser session. Reopen MeshCenter at the new IP address after the connection changes.

The Workspace menu also controls Base and Nodes panel visibility, theme and compact mode. These visual preferences are stored in the current browser, not globally on the Raspberry Pi.

## 12. Back up MeshCenter

Persistent data is stored in `data/`. The most important items are `config.py`, the optional `weather_secrets.py` and the complete `data/` directory.

Create a consistent backup:

```bash
cd ~/meshcenter
sudo systemctl stop meshcenter.service
tar --ignore-failed-read -czf "$HOME/meshcenter-backup-$(date +%F-%H%M).tar.gz" \
    config.py weather_secrets.py data
sudo systemctl start meshcenter.service
sudo systemctl is-active meshcenter.service
```

Store the archive on another device. It can contain message history, node positions, images, settings and the private weather API key.

## 13. Update MeshCenter safely

The following procedure was verified on a second Raspberry Pi installation.

**Quick update** (recommended for most users):

```bash
cd ~/meshcenter
git pull
git fetch --tags
sudo -n /usr/bin/systemctl restart meshcenter.service
```

Reload the browser with **Ctrl+F5** after updating.

**Safe update** (if you have local changes or want extra caution):

First check for unexpected local changes:

```bash
cd ~/meshcenter
git status --short --branch
```

If modified tracked files are listed, stop and review them before updating.
Do not discard local changes blindly.

Back up `config.py` and `data/`, then update:

```bash
cd ~/meshcenter
git pull --ff-only origin main
git fetch --tags
source venv/bin/activate
python -m pip install -r requirements.txt
python -m compileall -q server.py api camera meshsrv storage telemetry utils
sudo systemctl restart meshcenter.service
sudo systemctl is-active meshcenter.service
git status --short --branch
git log -3 --oneline --decorate
```

Expected results:

- `git pull` completes with a fast-forward or reports `Already up to date`
- The service reports `active`
- The local `main` branch matches `origin/main`
- `config.py`, `weather_secrets.py` and `data/` remain unchanged
- Version in the status bar shows the new version (e.g. `v1.7.0`)

> **Note:** `git fetch --tags` is required for the version shown in the
> status bar to update correctly.
>
> An `Author identity unknown` message matters only when creating a new
> Git commit on that Raspberry Pi. It does not prevent normal `git pull` updates.

## 14. Troubleshooting

### Service does not start

```bash
sudo systemctl status meshcenter.service --no-pager -l
journalctl -u meshcenter.service -n 100 --no-pager
```

Check the paths in the installed service and the values in `config.py`.

### Radio is not detected

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
id
source ~/meshcenter/venv/bin/activate
meshtastic --port /dev/ttyACM0 --info
```

Confirm that the user belongs to `dialout`, the USB cable carries data and `MESHTASTIC_PORT` matches the actual device.

### Serial port is busy

Only one active process should control the same radio connection. Close official CLI listeners and other serial programs, then restart MeshCenter:

```bash
sudo systemctl restart meshcenter.service
```

### Messages are not delivered

Verify the LoRa region, channel index, channel key, radio reachability and firmware compatibility with an official Meshtastic application. Then confirm that the CLI can communicate with the connected radio outside MeshCenter.

### Camera is unavailable

```bash
rpicam-hello --list-cameras
source ~/meshcenter/venv/bin/activate
python -c "from picamera2 import Picamera2; print('Picamera2 OK')"
```

The virtual environment must have been created with `--system-site-packages`.

### Wi-Fi scan or system actions fail

```bash
systemctl is-active NetworkManager
sudo visudo -cf /etc/sudoers.d/meshcenter
sudo visudo -cf /etc/sudoers.d/meshcenter-wifi
```

Confirm that the rendered sudoers files contain the actual service username.

### Browser shows stale interface files

Use `Ctrl+F5`, clear the browser cache or open MeshCenter in a private browser window.

## 15. Security notes

MeshCenter is intended for a trusted local network. The current interface has no built-in user authentication and uses unencrypted HTTP by default. Anyone who can reach the service can potentially send messages, manage Wi-Fi or invoke enabled system actions.

- Do not forward port 5000 directly to the Internet.
- Use a VPN or authenticated reverse proxy for remote access.
- Keep the sudoers rules limited to the supplied commands.
- Back up local data before updates or hardware migration.
- Remove Wi-Fi credentials, channel keys and personal message content before sharing logs or backups.

## 16. Getting help

When reporting an issue, include:

- Raspberry Pi model and OS version
- Meshtastic radio model and firmware version
- Python and Meshtastic CLI versions
- Serial device path
- Browser name
- Exact steps to reproduce the issue
- Relevant `journalctl` output and System Log entries

Project repository: <https://github.com/FlintUA/MeshCenter>

Live demo: <https://meshcenter.elektroniker.help/preview/>
