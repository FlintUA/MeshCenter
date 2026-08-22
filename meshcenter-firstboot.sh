#!/usr/bin/env bash
# MeshCenter automatic first-boot installer for Raspberry Pi OS (Debian Trixie)
#
# Purpose:
#   - keep Raspberry Pi Imager/cloud-init responsible for hostname, user,
#     password, Wi-Fi and SSH;
#   - install MeshCenter automatically on first boot;
#   - discover the physically connected Meshtastic radio BEFORE first start;
#   - create a real config.py from config.example.py using detected radio data;
#   - start MeshCenter only after the production config is valid.
#
# Copy this file to the bootfs partition as:
#   meshcenter-firstboot.sh
#
# Then add the runcmd line shown in the installation instructions to user-data.
#
# Tested design target:
#   Raspberry Pi OS Lite 64-bit (Debian 13 / Trixie)
#   Raspberry Pi Zero 2 W and newer
#
# Repository:
#   https://github.com/FlintUA/MeshCenter

set -Eeuo pipefail

REPO_URL="https://github.com/FlintUA/MeshCenter"
INSTALL_DIR_NAME="meshcenter"
LOG_FILE="/var/log/meshcenter-firstboot.log"
DONE_FILE="/var/lib/meshcenter-firstboot.done"
APP_PORT=5000

NETWORK_WAIT_SECONDS=300
RADIO_WAIT_SECONDS=180
RADIO_INFO_TIMEOUT=45
SERVICE_WAIT_SECONDS=30

# ─── Camera support ───────────────────────────────────────────────────────────
# Camera packages (python3-picamera2 + rpicam-apps) pull 200+ dependencies
# and can add up to 90 minutes of install time on Pi Zero 2W.
#
# Detection priority:
#   1. meshcenter-options file on bootfs (explicit user choice)
#   2. vcgencmd get_camera (auto-detect connected camera)
#   3. Default: no camera (fastest installation)
#
# To force camera installation, create meshcenter-options on bootfs:
#   echo "INSTALL_CAMERA=yes" > /boot/firmware/meshcenter-options
INSTALL_CAMERA=no

# ─── e-Paper display support ──────────────────────────────────────────────────
# python3-gpiozero and python3-spidev back the e-Paper display driver
# (modules/display/) - off by default, matching EPAPER_ENABLED=False in
# config.example.py (this hardware is opt-in, not every install has a HAT
# wired up). No auto-detection exists for it the way vcgencmd detects a
# camera, so this is manual-only.
#
# To install e-Paper support, create meshcenter-options on bootfs:
#   echo "INSTALL_EPAPER=yes" > /boot/firmware/meshcenter-options
# (remember to also set EPAPER_ENABLED=True in config.py afterwards)
INSTALL_EPAPER=no

PROGRESS_PORT=80
PROGRESS_FILE="/tmp/meshcenter-progress.txt"
PROGRESS_PID=""

mkdir -p "$(dirname "$DONE_FILE")"
touch "$LOG_FILE"
chmod 0644 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    log "ERROR: $*"
    log "Installation stopped. Full log: $LOG_FILE"
    exit 1
}

wait_for_apt_lock() {
    local waited=0
    while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || \
          fuser /var/lib/dpkg/lock >/dev/null 2>&1 || \
          fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
        if (( waited == 0 )); then
            log "Waiting for apt lock to be released..."
        fi
        sleep 3
        (( waited += 3 ))
        if (( waited > 120 )); then
            fail "apt lock was not released within 120 seconds."
        fi
    done
}

start_progress_server() {
    echo "Starting MeshCenter installation..." > "$PROGRESS_FILE"

    python3 - <<'PYEOF' &
import http.server, socketserver, os

PORT = 80
PROGRESS_FILE = "/tmp/meshcenter-progress.txt"
APP_PORT = 5000

STEPS = [
    (1, "Preparing system"),
    (2, "Installing system packages"),
    (3, "Downloading MeshCenter"),
    (4, "Installing Python dependencies"),
    (5, "Waiting for Meshtastic radio"),
    (6, "Configuring MeshCenter"),
    (7, "Starting service"),
]

STEP_HINTS = {
    2: "~2 min on Pi 4B / ~5 min on Pi Zero 2W",
    4: "longest step — up to 5 min on Pi 4B, up to 15 min on Pi Zero 2W",
    5: "up to 3 min — plug in your Meshtastic device now if not connected",
}

CAMERA_HINT = {
    2: "~20 min on Pi Zero 2W (camera packages included)",
}

HTML_PAGE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    {refresh_tag}
    <title>MeshCenter — Installing</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f0f4f8;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .card {{
            background: #fff;
            border-radius: 12px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            padding: 32px 36px;
            max-width: 480px;
            width: 100%;
        }}
        .logo {{
            color: #2d7d46;
            font-size: 22px;
            font-weight: 700;
            margin-bottom: 4px;
        }}
        .subtitle {{
            color: #718096;
            font-size: 13px;
            margin-bottom: 24px;
        }}
        .steps {{
            list-style: none;
            margin-bottom: 20px;
        }}
        .step {{
            display: flex;
            align-items: flex-start;
            padding: 7px 0;
            font-size: 14px;
            border-bottom: 1px solid #f0f4f8;
            gap: 10px;
        }}
        .step:last-child {{ border-bottom: none; }}
        .step-icon {{ width: 20px; flex-shrink: 0; text-align: center; }}
        .step-body {{ flex: 1; }}
        .step-label {{ font-weight: 500; }}
        .step-label.done {{ color: #718096; }}
        .step-label.active {{ color: #22543d; }}
        .step-label.pending {{ color: #a0aec0; }}
        .step-hint {{
            font-size: 12px;
            color: #e07b00;
            margin-top: 3px;
            font-style: italic;
        }}
        .footer {{
            color: #a0aec0;
            font-size: 11px;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">🟢 MeshCenter</div>
        <div class="subtitle">Automatic installation in progress</div>
        <ul class="steps">
{steps_html}
        </ul>
        <div class="footer">{footer}</div>
    </div>
</body>
</html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            status = open(PROGRESS_FILE).read().strip()
        except Exception:
            status = "1"

        # Determine the current step from the status file
        current_step = 0
        camera_mode = False
        for line in status.splitlines():
            if line.startswith("STEP:"):
                try:
                    current_step = int(line.split(":")[1])
                except Exception:
                    pass
            if line == "CAMERA:yes":
                camera_mode = True

        steps_html = ""
        for num, label in STEPS:
            if num < current_step:
                icon = "✅"
                cls = "done"
                hint = ""
            elif num == current_step:
                icon = "⏳"
                cls = "active"
                hints = CAMERA_HINT if camera_mode else STEP_HINTS
                hint_text = hints.get(num, "")
                hint = f'<div class="step-hint">⏱ {hint_text}</div>' if hint_text else ""
            else:
                icon = "⬜"
                cls = "pending"
                hint = ""

            steps_html += f'''            <li class="step">
                <span class="step-icon">{icon}</span>
                <div class="step-body">
                    <div class="step-label {cls}">{num}/7 &nbsp; {label}</div>
                    {hint}
                </div>
            </li>\n'''

        if current_step == 0:
            footer = "Starting..."
            refresh_tag = '<meta http-equiv="refresh" content="3">'
        elif current_step >= 7:
            footer = "Finishing up — the Pi will reboot shortly. Reopen this page after ~30s."
            refresh_tag = '<meta http-equiv="refresh" content="2">'
        else:
            footer = "Page refreshes every 3 seconds."
            refresh_tag = '<meta http-equiv="refresh" content="3">'

        body = HTML_PAGE.format(
            refresh_tag=refresh_tag,
            steps_html=steps_html,
            footer=footer
        ).encode('utf-8')

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet — don't clutter the install log

try:
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()
except Exception:
    pass  # port busy — not critical
PYEOF

    PROGRESS_PID=$!
    sleep 1
    if kill -0 "$PROGRESS_PID" 2>/dev/null; then
        log "Progress server started on port $PROGRESS_PORT"
        log "Watch installation at: http://$(hostname).local or http://$(hostname -I | awk '{print $1}')"
    else
        PROGRESS_PID=""
        log "Progress server could not start on port $PROGRESS_PORT (port busy — OK)"
    fi
}

update_progress() {
    local step="$1"
    local message="$2"
    {
        echo "STEP:${step}"
        [[ "$INSTALL_CAMERA" == "yes" ]] && echo "CAMERA:yes" || true
    } > "$PROGRESS_FILE"
    log "Step ${step}/7: ${message}"
}

trap 'rc=$?; log "ERROR: unexpected failure at line $LINENO (exit code $rc)"; exit $rc' ERR

[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root by cloud-init."

if [[ -f "$DONE_FILE" ]]; then
    log "MeshCenter first-boot installation has already completed."
    exit 0
fi

# Start the progress server as early as possible so it's already reachable
# while apt/git/pip work is still running.
start_progress_server

log "============================================================"
log "MeshCenter automatic first-boot installation started"
log "============================================================"
log "Device: $(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
log "OS: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"

# Read user options from bootfs if present
for OPTIONS_PATH in /boot/firmware/meshcenter-options /boot/meshcenter-options; do
    if [[ -f "$OPTIONS_PATH" ]]; then
        log "Reading options from $OPTIONS_PATH"
        # shellcheck source=/dev/null
        source "$OPTIONS_PATH"
        break
    fi
done

# Auto-detect camera if not explicitly set
if [[ "$INSTALL_CAMERA" == "no" ]]; then
    if vcgencmd get_camera 2>/dev/null | grep -q "detected=1"; then
        log "Camera detected via vcgencmd — enabling camera support"
        INSTALL_CAMERA=yes
    fi
fi

log "Camera support: $INSTALL_CAMERA"
log "e-Paper support: $INSTALL_EPAPER"

# ---------------------------------------------------------------------------
# 1. Resolve the normal user created by Raspberry Pi Imager
# ---------------------------------------------------------------------------
update_progress 1 "Preparing system"

TARGET_USER=""
TARGET_HOME=""

for _ in $(seq 1 30); do
    ENTRY="$(getent passwd 1000 || true)"
    if [[ -n "$ENTRY" ]]; then
        TARGET_USER="$(cut -d: -f1 <<<"$ENTRY")"
        TARGET_HOME="$(cut -d: -f6 <<<"$ENTRY")"
        break
    fi
    log "Waiting for the first normal user (UID 1000)..."
    sleep 2
done

[[ -n "$TARGET_USER" ]] || fail "No normal user with UID 1000 was found."
[[ "$TARGET_USER" != "root" ]] || fail "UID 1000 unexpectedly belongs to root."
[[ -d "$TARGET_HOME" ]] || fail "Home directory not found: $TARGET_HOME"

INSTALL_DIR="${TARGET_HOME}/${INSTALL_DIR_NAME}"

log "Target user: $TARGET_USER"
log "Target home: $TARGET_HOME"
log "Install directory: $INSTALL_DIR"

# ---------------------------------------------------------------------------
# 2. Ensure recovery access over SSH
# ---------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive

wait_for_apt_lock
apt-get update -qq
wait_for_apt_lock
apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    openssh-server \
    sudo

systemctl enable ssh.service >/dev/null 2>&1 || true
systemctl start ssh.service || true
log "SSH service: $(systemctl is-active ssh.service || true)"

# ---------------------------------------------------------------------------
# 3. Wait for Internet access using DNS + TCP/443, not ICMP ping
# ---------------------------------------------------------------------------
log "Waiting for Internet access..."

NETWORK_OK=0
for _ in $(seq 1 $((NETWORK_WAIT_SECONDS / 5))); do
    if getent ahosts github.com >/dev/null 2>&1; then
        if timeout 5 bash -c 'exec 3<>/dev/tcp/github.com/443' >/dev/null 2>&1; then
            NETWORK_OK=1
            break
        fi
    fi
    sleep 5
done

[[ "$NETWORK_OK" -eq 1 ]] || fail "Internet did not become available within ${NETWORK_WAIT_SECONDS}s."
log "Internet access is available."

# ---------------------------------------------------------------------------
# 4. Install system packages required by MeshCenter
# ---------------------------------------------------------------------------
update_progress 2 "Installing system packages"

log "Installing MeshCenter system dependencies..."

wait_for_apt_lock
apt-get update -qq
wait_for_apt_lock
apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-venv \
    python3-pip \
    avahi-daemon \
    network-manager \
    iw \
    usbutils \
    lsof \
    i2c-tools \
    util-linux-extra

systemctl enable avahi-daemon >/dev/null 2>&1 || true
systemctl start avahi-daemon || true

# Picamera2 is optional. Install it before creating the venv so
# --system-site-packages can expose it inside MeshCenter's environment.
if [[ "$INSTALL_CAMERA" == "yes" ]]; then
    log "Installing camera packages (python3-picamera2 + rpicam-apps)..."
    log "Note: this step can take up to 90 min on Pi Zero 2W"
    if apt-cache show python3-picamera2 >/dev/null 2>&1; then
        wait_for_apt_lock
        apt-get install -y --no-install-recommends \
            python3-picamera2 \
            rpicam-apps \
            || log "WARNING: camera packages could not be installed; continuing without camera support."
    else
        log "WARNING: python3-picamera2 not available in this repository."
    fi
else
    log "Camera support skipped (INSTALL_CAMERA=no)"
    log "To enable: create meshcenter-options on bootfs with INSTALL_CAMERA=yes"
fi

# gpiozero/spidev back the e-Paper display driver - optional, off by default
# (see EPAPER_ENABLED in config.example.py).
if [[ "$INSTALL_EPAPER" == "yes" ]]; then
    log "Installing e-Paper display packages (python3-gpiozero + python3-spidev)..."
    wait_for_apt_lock
    apt-get install -y --no-install-recommends \
        python3-gpiozero \
        python3-spidev \
        || log "WARNING: e-Paper packages could not be installed; continuing without e-Paper support."
else
    log "e-Paper support skipped (INSTALL_EPAPER=no)"
    log "To enable: create meshcenter-options on bootfs with INSTALL_EPAPER=yes"
fi

# Add the normal user to useful hardware groups that actually exist.
for group in dialout video render gpio i2c spi; do
    if getent group "$group" >/dev/null 2>&1; then
        usermod -a -G "$group" "$TARGET_USER"
    fi
done

log "System dependencies installed."

# ---------------------------------------------------------------------------
# 5. Clone MeshCenter
# ---------------------------------------------------------------------------
update_progress 3 "Downloading MeshCenter"

if [[ -e "$INSTALL_DIR" ]]; then
    fail "$INSTALL_DIR already exists. Refusing to overwrite an existing installation."
fi

log "Cloning MeshCenter..."
runuser -u "$TARGET_USER" -- \
    env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"

[[ -d "$INSTALL_DIR/.git" ]] || fail "Repository clone did not create $INSTALL_DIR/.git"
log "MeshCenter repository cloned."

# Shared radio-identity-parsing/config-generation logic (also used by
# install.sh) - only available now that the repo has actually been cloned.
# shellcheck source=installer/common.sh
source "$INSTALL_DIR/installer/common.sh"

# ---------------------------------------------------------------------------
# 6. Create Python virtual environment and install requirements
# ---------------------------------------------------------------------------
update_progress 4 "Installing Python dependencies"

log "Creating Python virtual environment..."

runuser -u "$TARGET_USER" -- \
    env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
    python3 -m venv --system-site-packages "$INSTALL_DIR/venv"

log "Installing Python dependencies..."

runuser -u "$TARGET_USER" -- \
    env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
    "$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip --quiet

runuser -u "$TARGET_USER" -- \
    env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
    "$INSTALL_DIR/venv/bin/python" -m pip install \
        -r "$INSTALL_DIR/requirements.txt" --quiet

MESHTASTIC_BIN="$INSTALL_DIR/venv/bin/meshtastic"
[[ -x "$MESHTASTIC_BIN" ]] || fail "Meshtastic CLI was not installed in the MeshCenter venv."

log "Python environment ready."

log "USB devices detected before Meshtastic probing:"
lsusb || true

# ---------------------------------------------------------------------------
# 7. Detect the physically connected Meshtastic radio
#
# Important:
#   We do NOT create config.py before a valid radio identity is obtained.
#   config.example.py remains only a template.
#
#   Do not blindly assume /dev/ttyACM0. Probe every ACM/USB serial candidate
#   and accept it only if Meshtastic CLI returns a valid local identity.
# ---------------------------------------------------------------------------
update_progress 5 "Waiting for Meshtastic radio"

log "Waiting for a Meshtastic serial device..."

VALID_RADIOS_FILE="/tmp/meshcenter-valid-radios.txt"
rm -f "$VALID_RADIOS_FILE"
touch "$VALID_RADIOS_FILE"

deadline=$((SECONDS + RADIO_WAIT_SECONDS))

while (( SECONDS < deadline )); do
    mapfile -t SERIAL_CANDIDATES < <(
        find /dev -maxdepth 1 \( -name 'ttyACM*' -o -name 'ttyUSB*' \) \
            -type c -print 2>/dev/null | sort
    )

    if (( ${#SERIAL_CANDIDATES[@]} == 0 )); then
        sleep 3
        continue
    fi

    : > "$VALID_RADIOS_FILE"

    for dev in "${SERIAL_CANDIDATES[@]}"; do
        safe_name="${dev#/dev/}"
        info_file="/tmp/meshcenter-radio-${safe_name}.txt"
        rm -f "$info_file"

        log "Probing serial candidate: $dev"

        if timeout "$RADIO_INFO_TIMEOUT" \
            runuser -u "$TARGET_USER" -- \
            env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
            "$MESHTASTIC_BIN" --port "$dev" --info \
            >"$info_file" 2>&1; then

            if grep -q '"myNodeNum"' "$info_file" && grep -q '^Owner:' "$info_file"; then
                printf '%s|%s\n' "$dev" "$info_file" >> "$VALID_RADIOS_FILE"
                log "Valid Meshtastic radio found on $dev"
            else
                log "$dev responded, but did not return a valid Meshtastic identity."
            fi
        else
            log "$dev is not a usable Meshtastic radio."
        fi
    done

    VALID_COUNT="$(wc -l < "$VALID_RADIOS_FILE" | tr -d ' ')"

    if [[ "$VALID_COUNT" -eq 1 ]]; then
        break
    elif [[ "$VALID_COUNT" -gt 1 ]]; then
        log "Multiple Meshtastic radios were detected:"
        cut -d'|' -f1 "$VALID_RADIOS_FILE" | sed 's/^/  - /'
        fail "Connect exactly one Meshtastic radio for automatic first-boot provisioning."
    fi

    sleep 5
done

VALID_COUNT="$(wc -l < "$VALID_RADIOS_FILE" | tr -d ' ')"
[[ "$VALID_COUNT" -eq 1 ]] \
    || fail "No valid Meshtastic radio was detected within ${RADIO_WAIT_SECONDS}s."

RADIO_PORT="$(cut -d'|' -f1 "$VALID_RADIOS_FILE")"
RADIO_INFO_FILE="$(cut -d'|' -f2- "$VALID_RADIOS_FILE")"

log "Using Meshtastic radio: $RADIO_PORT"

# Parse identity (shared with install.sh - see installer/common.sh).
# node_id is derived from myNodeNum because it is stable and avoids relying on
# the order/content of the node database printed by --info.
IDENTITY_JSON="$(parse_meshtastic_identity_from_info "$RADIO_INFO_FILE" "$RADIO_PORT")" \
    || fail "Could not parse Meshtastic identity."

NODE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["node_id"])' "$IDENTITY_JSON")"
LONG_NAME="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["long_name"])' "$IDENTITY_JSON")"
SHORT_NAME="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["short_name"])' "$IDENTITY_JSON")"
HW_MODEL="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["hardware"])' "$IDENTITY_JSON")"

[[ "$NODE_ID" =~ ^![0-9a-fA-F]{8}$ ]] || fail "Detected node ID is invalid: $NODE_ID"

log "Meshtastic radio detected:"
log "  Port:       $RADIO_PORT"
log "  Node ID:    $NODE_ID"
log "  Long name:  $LONG_NAME"
log "  Short name: $SHORT_NAME"
log "  Hardware:   ${HW_MODEL:-unknown}"

# ---------------------------------------------------------------------------
# 8. Create production config.py from config.example.py
#
# The repository file stays untouched and generic.
# Only the newly created config.py receives installation-specific values.
# ---------------------------------------------------------------------------
update_progress 6 "Configuring MeshCenter"

TEMPLATE_CONFIG="$INSTALL_DIR/config.example.py"
LIVE_CONFIG="$INSTALL_DIR/config.py"

[[ -f "$TEMPLATE_CONFIG" ]] || fail "Missing template: $TEMPLATE_CONFIG"
[[ ! -e "$LIVE_CONFIG" ]] || fail "$LIVE_CONFIG already exists unexpectedly."

log "Creating production config.py from config.example.py..."

# Shared with install.sh - see installer/common.sh. The write step always
# runs as plain `python3` (stdlib-only, no venv needed); only the final
# re-import validation uses the target user's venv python via runuser,
# same as before this was extracted.
run_as_target() {
    runuser -u "$TARGET_USER" -- \
        env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
        "$@"
}

generate_config_from_radio \
    "$TEMPLATE_CONFIG" "$LIVE_CONFIG" \
    "$NODE_ID" "$LONG_NAME" "$SHORT_NAME" "$HW_MODEL" "$RADIO_PORT" \
    "$TARGET_USER:$TARGET_USER" \
    "$INSTALL_DIR/venv/bin/python" run_as_target \
    || fail "Could not generate production config.py."

log "Production config.py created and validated."

# Ensure clean first identity creation. A fresh install normally has no
# instance.json, but remove any incomplete bootstrap file defensively.
rm -f "$INSTALL_DIR/data/instance.json"
mkdir -p "$INSTALL_DIR/data"
chown -R "$TARGET_USER:$TARGET_USER" "$INSTALL_DIR/data"

# ---------------------------------------------------------------------------
# 9. Install sudoers files and systemd service from repository templates
# ---------------------------------------------------------------------------
update_progress 7 "Starting service"

log "Installing MeshCenter system service..."

MESH_USER="$TARGET_USER"
MESH_HOME="$TARGET_HOME"

[[ -f "$INSTALL_DIR/deploy/meshcenter.service" ]] \
    || fail "Missing deploy/meshcenter.service"
[[ -f "$INSTALL_DIR/deploy/meshcenter.sudoers" ]] \
    || fail "Missing deploy/meshcenter.sudoers"
[[ -f "$INSTALL_DIR/deploy/meshcenter-wifi.sudoers" ]] \
    || fail "Missing deploy/meshcenter-wifi.sudoers"
[[ -f "$INSTALL_DIR/deploy/meshcenter-hw.sudoers" ]] \
    || fail "Missing deploy/meshcenter-hw.sudoers"
[[ -f "$INSTALL_DIR/scripts/meshcenter-hw-config" ]] \
    || fail "Missing scripts/meshcenter-hw-config"
[[ -f "$INSTALL_DIR/deploy/99-meshcenter-rtc.rules" ]] \
    || fail "Missing deploy/99-meshcenter-rtc.rules"

sed -e "s|__MESH_USER__|${MESH_USER}|g" \
    -e "s|__MESH_HOME__|${MESH_HOME}|g" \
    "$INSTALL_DIR/deploy/meshcenter.service" \
    > /etc/systemd/system/meshcenter.service

# Narrow privileged helper the I2C/RTC hardware card uses to edit
# /boot/firmware/config.txt (hardware/hardware_config.py calls it via
# `sudo -n` - see scripts/meshcenter-hw-config's own docstring).
install -o root -g root -m 0755 \
    "$INSTALL_DIR/scripts/meshcenter-hw-config" /usr/local/sbin/meshcenter-hw-config

# Grants the `i2c` group (already assigned to $TARGET_USER above) read
# access to /dev/rtcN - without it, hardware/rtc_service.py's readable-stage
# check can never succeed even once the RTC is detected and configured
# (task 35). Declarative, not a sudo rule - unlike the helper above, this
# needs no privileged runtime call.
install -o root -g root -m 0644 \
    "$INSTALL_DIR/deploy/99-meshcenter-rtc.rules" /etc/udev/rules.d/99-meshcenter-rtc.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=rtc

sed "s|__MESH_USER__|${MESH_USER}|g" \
    "$INSTALL_DIR/deploy/meshcenter.sudoers" \
    > /etc/sudoers.d/meshcenter

sed "s|__MESH_USER__|${MESH_USER}|g" \
    "$INSTALL_DIR/deploy/meshcenter-wifi.sudoers" \
    > /etc/sudoers.d/meshcenter-wifi

sed "s|__MESH_USER__|${MESH_USER}|g" \
    "$INSTALL_DIR/deploy/meshcenter-hw.sudoers" \
    > /etc/sudoers.d/meshcenter-hw

chmod 0440 \
    /etc/sudoers.d/meshcenter \
    /etc/sudoers.d/meshcenter-wifi \
    /etc/sudoers.d/meshcenter-hw

visudo -cf /etc/sudoers.d/meshcenter >/dev/null \
    || fail "Generated meshcenter sudoers file is invalid."
visudo -cf /etc/sudoers.d/meshcenter-wifi >/dev/null \
    || fail "Generated meshcenter-wifi sudoers file is invalid."
visudo -cf /etc/sudoers.d/meshcenter-hw >/dev/null \
    || fail "Generated meshcenter-hw sudoers file is invalid."

systemctl daemon-reload
systemctl enable meshcenter.service >/dev/null
systemctl start meshcenter.service

# ---------------------------------------------------------------------------
# 10. Verify stable service startup and HTTP response
# ---------------------------------------------------------------------------
log "Waiting for meshcenter.service..."

SERVICE_OK=0
for _ in $(seq 1 "$SERVICE_WAIT_SECONDS"); do
    if systemctl is-active meshcenter.service >/dev/null 2>&1; then
        # Make sure it stays up long enough to get through radio/profile init.
        sleep 1
        if systemctl is-active meshcenter.service >/dev/null 2>&1; then
            SERVICE_OK=1
            break
        fi
    fi
    sleep 1
done

if [[ "$SERVICE_OK" -ne 1 ]]; then
    journalctl -u meshcenter.service -n 120 --no-pager || true
    fail "meshcenter.service did not become stable."
fi

log "MeshCenter service is active."

HTTP_OK=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 "http://127.0.0.1:${APP_PORT}/" >/dev/null 2>&1; then
        HTTP_OK=1
        break
    fi
    sleep 1
done

if [[ "$HTTP_OK" -ne 1 ]]; then
    journalctl -u meshcenter.service -n 120 --no-pager || true
    fail "MeshCenter service is running, but HTTP port ${APP_PORT} did not respond."
fi

# ---------------------------------------------------------------------------
# 11. Final verification and completion marker
# ---------------------------------------------------------------------------
# Confirm MeshCenter created an instance identity containing the detected node.
INSTANCE_FILE="$INSTALL_DIR/data/instance.json"
[[ -f "$INSTANCE_FILE" ]] || fail "MeshCenter did not create data/instance.json."

python3 - "$INSTANCE_FILE" "$NODE_ID" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = sys.argv[2]
data = json.loads(path.read_text(encoding="utf-8"))
actual = str((data.get("radio") or {}).get("node_id") or "").strip()
if actual != expected:
    raise SystemExit(f"instance.json node_id {actual!r} != detected {expected!r}")
PY

touch "$DONE_FILE"
chmod 0644 "$DONE_FILE"

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_NAME="$(hostname)"

log "============================================================"
log "MeshCenter installation completed successfully"
log "============================================================"
log "User:       $TARGET_USER"
log "Directory:  $INSTALL_DIR"
log "Radio:      $LONG_NAME ($NODE_ID)"
log "Radio port: $RADIO_PORT"
log "SSH:        ssh ${TARGET_USER}@${IP_ADDR:-<IP-address>}"
log "Web:        http://${IP_ADDR:-<IP-address>}:${APP_PORT}"
log "mDNS:       http://${HOST_NAME}.local:${APP_PORT}"
log "Log:        $LOG_FILE"
log "============================================================"

update_progress 7 "Installation complete"

# Group membership changes (dialout, gpio, i2c, ...) applied earlier via
# usermod only take effect for new sessions — meshcenter.service was started
# in this same first-boot session, so the radio device may not be usable
# until the user's group list is re-resolved. Reboot to guarantee the
# service comes back up with correct permissions instead of leaving the
# user to debug a working install that can't see its own radio.
log "Rebooting to apply group membership changes..."
log "MeshCenter will be available at http://${HOST_NAME}.local:${APP_PORT} after reboot"
log "============================================================"

# Schedule the reboot a few seconds out so this log line has time to flush.
( sleep 5 && reboot ) &

exit 0
