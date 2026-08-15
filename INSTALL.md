# MeshCenter Installation Guide

## System Requirements

**Recommended:**
- Raspberry Pi Zero 2W, 3B/3B+, or 4B
- Raspberry Pi OS Lite 64-bit (Bookworm)
- 8GB+ microSD card
- Internet connection during installation

**Minimum:**
- Any Linux system with Python 3.9+
- Debian 11 (Bullseye) / Ubuntu 22.04 LTS or newer
- 500MB free disk space · 512MB RAM · systemd

**Not supported:**
- Windows / macOS
- Raspberry Pi OS 32-bit (lgpio compatibility issues)
- Debian 10 (Buster) or older (Python 3.7 — too old)
- Alpine Linux (musl libc — pip build failures)

---

## Quick Install (Recommended)

> **What you need:** Raspberry Pi, microSD card (8GB+), internet connection,
> [Raspberry Pi Imager](https://www.raspberrypi.com/software/)

### Step 1 — Flash the SD card

1. Open **Raspberry Pi Imager**
2. Choose OS: **Raspberry Pi OS Lite (64-bit)**
3. Click the ⚙️ gear icon → configure:
   - Hostname: `meshcenter` (gives you `http://meshcenter.local:5000`)
   - Username and password
   - WiFi network (if not using Ethernet)
   - Enable SSH *(optional, for advanced access)*
4. Flash to SD card

### Step 2 — Add MeshCenter installer

After flashing, the SD card appears as a drive called **bootfs**.

Download **[firstrun.sh](https://github.com/FlintUA/MeshCenter/releases/latest/download/firstrun.sh)**
and copy it to the **root** of the bootfs drive:

```
bootfs (SD card):
├── firstrun.sh   ← copy here
├── cmdline.txt
├── config.txt
└── ...
```

- **Windows:** SD card appears as `D:\` (or similar) — copy `firstrun.sh` there
- **Mac:** SD card appears as `/Volumes/bootfs/` — copy `firstrun.sh` there

### Step 3 — Boot and wait

1. Safely eject the SD card
2. Insert it into your Raspberry Pi
3. Connect power
4. Wait **5–10 minutes** *(Pi Zero 2W may take up to 10 minutes)*

> 💡 **Watch the progress:** open **http://meshcenter.local** (port 80)
> in your browser while it installs. The page updates every 3 seconds.

### Camera support

By default, camera packages are **not installed** to keep installation fast.
Camera support is enabled automatically if a camera is detected,
or you can force it by creating a file `meshcenter-options` on the bootfs drive:

```
INSTALL_CAMERA=yes
```

> ⚠️ **Installation time with camera support:**
> - Pi 4B / 3B+: ~20 minutes
> - Pi Zero 2W: up to 90 minutes
>
> Without camera support:
> - Pi 4B / 3B+: ~5 minutes
> - Pi Zero 2W: ~15 minutes

### Step 4 — Open MeshCenter

Once installation completes, open in your browser:

```
http://meshcenter.local:5000
```

or use the IP address shown on the progress page.

### Step 5 — Connect your radio

Plug in your Meshtastic device via USB.
MeshCenter will detect it automatically.

> ⚙️ Configure OpenWeather API key and other settings in
> **Settings → Weather** after installation.

---

## Updating

```bash
cd ~/meshcenter
git pull
git fetch --tags
sudo -n /usr/bin/systemctl restart meshcenter.service
```

Reload the browser with Ctrl+F5 after updating.
Note: `git fetch --tags` is required for the version shown in the status bar
to update correctly.

---

## Manual Install (Advanced)

A condensed, tick-off checklist for a first-time install on a Raspberry Pi. It
mirrors the same steps as **[README.md → Installation](README.md#installation)**
but in checklist form, and calls out the steps that are easy to skip and only
surface as a problem later.

For full explanations, hardware requirements, and extended troubleshooting,
see:

- **[README.md → Installation](README.md#installation)** — the detailed,
  narrated walkthrough this checklist is based on.
- **[docs/User_Guide.md](docs/User_Guide.md)** — first-run checks, backup,
  safe updates, and the System/Wi-Fi sudo actions section.

Run `./scripts/verify-install.sh` at the end (or at any point) to check most
of this automatically.

---

## Before you start

- [ ] Meshtastic radio already configured with the official app (LoRa region,
      channels, node long/short name) — MeshCenter reads existing radio
      configuration, it does not create channels itself.
- [ ] **Serial access enabled** on the radio: `Settings → Security → Serial
      enabled` in the Meshtastic app. If this is off, the Pi still sees the
      USB device but MeshCenter can't read the radio identity.
- [ ] Raspberry Pi OS Bookworm (or newer) flashed, SSH enabled, and you can
      log in: `ssh <user>@<hostname>.local`

## Checklist

1. [ ] **OS packages, dialout group, reboot**
   ```bash
   sudo apt update
   sudo apt install -y git python3 python3-venv python3-pip network-manager iw
   # optional camera support — install BEFORE creating the venv:
   sudo apt install -y python3-picamera2 rpicam-apps
   sudo usermod -aG dialout "$USER"
   sudo reboot
   ```

2. [ ] **Clone the repository**
   ```bash
   cd ~
   git clone https://github.com/FlintUA/MeshCenter.git meshcenter
   cd ~/meshcenter
   ```

3. [ ] **Python environment** (`--system-site-packages` is required so
   Picamera2 is visible inside the venv — if you created the venv before
   installing `python3-picamera2`, delete `venv/` and redo this step)
   ```bash
   python3 -m venv --system-site-packages venv
   source venv/bin/activate
   python -m pip install --upgrade pip setuptools wheel
   python -m pip install -r requirements.txt
   which meshtastic   # should print a path under ~/meshcenter/venv/bin/
   ```

4. [ ] **Test the radio before configuring MeshCenter**
   ```bash
   ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
   source ~/meshcenter/venv/bin/activate
   meshtastic --port /dev/ttyACM0 --info
   ```
   Do not continue until this prints node info with no permission/serial
   errors. Note the local node ID, long name and short name.

5. [ ] **`config.py`** — server.py exits at import time if this is missing.
   ```bash
   cp config.example.py config.py
   mkdir -p data
   ```
   Edit `config.py`: at minimum `MESHTASTIC_PORT`, `LOCAL_NODE_ID`,
   `LOCAL_NODE_NAME`. `KNOWN_NODES` / `KNOWN_NODE_INFO` are optional
   friendly-name maps for other nodes on your mesh.
   > ⚠ `config.py` is gitignored on purpose — it can end up holding real
   > node IDs and names. Never `git add -f` it or otherwise force it into a
   > commit.

6. [ ] **`weather_secrets.py`** (optional — only if you'll use the Weather
   widget)
   ```bash
   cp weather_secrets.example.py weather_secrets.py
   ```

7. [ ] **First manual run**
   ```bash
   source venv/bin/activate
   python server.py
   ```
   Look for `Identity: MATCH` in the startup banner, then open
   `http://<raspberry-pi-ip>:5000` from another device. `Ctrl+C` once
   confirmed — don't leave it running outside systemd.

8. [ ] **systemd service**
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
   sudo systemctl status meshcenter.service --no-pager -l   # expect: active (running)
   ```

9. [ ] **sudoers rules — see "Easy to forget" below, do this now, not later**
   ```bash
   cd ~/meshcenter
   MESH_USER="$(id -un)"
   sed "s|__MESH_USER__|$MESH_USER|g" deploy/meshcenter.sudoers \
       | sudo tee /etc/sudoers.d/meshcenter >/dev/null
   sed "s|__MESH_USER__|$MESH_USER|g" deploy/meshcenter-wifi.sudoers \
       | sudo tee /etc/sudoers.d/meshcenter-wifi >/dev/null
   sudo chmod 440 /etc/sudoers.d/meshcenter /etc/sudoers.d/meshcenter-wifi
   sudo visudo -cf /etc/sudoers.d/meshcenter        # expect: parsed OK
   sudo visudo -cf /etc/sudoers.d/meshcenter-wifi    # expect: parsed OK
   ```
   `scripts/verify-install.sh` checks this by asking `sudo -n -l` what the
   current user can actually run passwordlessly, not by looking for these
   exact files — a Pi where the user already has broader NOPASSWD sudo some
   other way also passes.

10. [ ] **Confirm radio identity** — open the UI and check there's no
    identity-mismatch banner, or:
    ```bash
    curl -s http://127.0.0.1:5000/api/node-manager/dashboard | grep -o '"identity_status":"[A-Z_]*"'
    # expect: "identity_status":"MATCH"
    ```

11. [ ] **Run the automated check**
    ```bash
    cd ~/meshcenter
    ./scripts/verify-install.sh
    ```

---

## Easy to forget

- **Step 9 (sudoers)** — the app runs and looks fully functional without it.
  It only breaks later, when someone clicks **Restart MeshCenter** or a
  Wi-Fi action in the UI and it silently fails because `sudo systemctl
  restart` has no tty to prompt for a password. This is exactly what
  happened on this project's own prod node: dev had it configured, prod
  didn't, and it only surfaced when a deploy needed a restart. Do this step
  during install, don't defer it.
- **`config.py` doesn't exist after `git clone`** — it's gitignored by
  design (step 5). If you skip `cp config.example.py config.py`, `python
  server.py` exits immediately with a missing-config error, not a vague one.
- **Camera packages must precede the venv** — `python3-picamera2` needs to
  already be installed system-wide before `python3 -m venv
  --system-site-packages venv` is created, or the venv won't see it. If you
  installed the camera packages after creating the venv, delete `venv/` and
  redo step 3.
- **`weather_secrets.py` is optional** — nothing breaks if you skip it, the
  Weather widget just stays unconfigured until you add a key later.
