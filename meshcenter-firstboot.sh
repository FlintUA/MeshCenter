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
REDIRECT_FILE = "/tmp/meshcenter-redirect.txt"
APP_PORT = 5000

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
            padding: 40px;
            max-width: 520px;
            width: 100%;
        }}
        .logo {{
            color: #2d7d46;
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
        }}
        .subtitle {{
            color: #718096;
            font-size: 14px;
            margin-bottom: 32px;
        }}
        .step {{
            background: #f0faf4;
            border: 1px solid #c6f6d5;
            border-left: 4px solid #2d7d46;
            border-radius: 8px;
            padding: 18px 20px;
            font-size: 17px;
            font-weight: 500;
            color: #22543d;
            line-height: 1.5;
            margin-bottom: 20px;
            min-height: 60px;
        }}
        .hint {{
            color: #a0aec0;
            font-size: 12px;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">🟢 MeshCenter</div>
        <div class="subtitle">Automatic installation in progress</div>
        <div class="step">{status}</div>
        <div class="hint">{hint}</div>
    </div>
</body>
</html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # If installation is done — redirect to :5000
        try:
            redirect_url = open(REDIRECT_FILE).read().strip()
            if redirect_url:
                self.send_response(302)
                self.send_header('Location', redirect_url)
                self.end_headers()
                return
        except FileNotFoundError:
            pass

        try:
            status = open(PROGRESS_FILE).read().strip()
        except Exception:
            status = "Starting..."

        status_html = status.replace('\n', '<br>')
        refresh_tag = '<meta http-equiv="refresh" content="3">'
        hint = "This page refreshes every 3 seconds."

        body = HTML_PAGE.format(
            refresh_tag=refresh_tag,
            status=status_html,
            hint=hint
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
    # Update the progress-page status. %b lets callers pass \n for a
    # second line, which the page's Python side turns into <br>.
    printf '%b\n' "$*" > "$PROGRESS_FILE"
    log "$*"
}

stop_progress_server() {
    if [[ -n "$PROGRESS_PID" ]]; then
        # Write the redirect URL — the page picks it up on its next refresh.
        local ip
        ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
        echo "http://${ip}:${APP_PORT}/" > /tmp/meshcenter-redirect.txt
        sleep 4  # give the browser a chance to catch the redirect
        kill "$PROGRESS_PID" 2>/dev/null || true
        PROGRESS_PID=""
    fi
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

# ---------------------------------------------------------------------------
# 1. Resolve the normal user created by Raspberry Pi Imager
# ---------------------------------------------------------------------------
update_progress "Step 1/7: Preparing system..."

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
update_progress "Step 2/7: Installing system packages..."

log "Installing MeshCenter system dependencies..."

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
    usbutils

systemctl enable avahi-daemon >/dev/null 2>&1 || true
systemctl start avahi-daemon || true

# Picamera2 is optional. Install it before creating the venv so
# --system-site-packages can expose it inside MeshCenter's environment.
if apt-cache show python3-picamera2 >/dev/null 2>&1; then
    log "Installing optional Raspberry Pi camera packages..."
    wait_for_apt_lock
    apt-get install -y --no-install-recommends \
        python3-picamera2 \
        rpicam-apps \
        || log "WARNING: camera packages could not be installed; continuing without camera support."
else
    log "WARNING: python3-picamera2 is not available in this image/repository."
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
update_progress "Step 3/7: Downloading MeshCenter..."

if [[ -e "$INSTALL_DIR" ]]; then
    fail "$INSTALL_DIR already exists. Refusing to overwrite an existing installation."
fi

log "Cloning MeshCenter..."
runuser -u "$TARGET_USER" -- \
    env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"

[[ -d "$INSTALL_DIR/.git" ]] || fail "Repository clone did not create $INSTALL_DIR/.git"
log "MeshCenter repository cloned."

# ---------------------------------------------------------------------------
# 6. Create Python virtual environment and install requirements
# ---------------------------------------------------------------------------
update_progress "Step 4/7: Installing Python dependencies...\n(longest step — up to 5 min on Pi Zero 2W)"

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
update_progress "Step 5/7: Waiting for Meshtastic radio...\n(waiting up to 3 min — plug in your device now if not connected)"

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

# Parse identity with Python.
# node_id is derived from myNodeNum because it is stable and avoids relying on
# the order/content of the node database printed by --info.
IDENTITY_JSON="$(
python3 - "$RADIO_INFO_FILE" "$RADIO_PORT" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
port = sys.argv[2]
text = path.read_text(errors="replace")

m_num = re.search(r'"myNodeNum"\s*:\s*(\d+)', text)
if not m_num:
    raise SystemExit("myNodeNum not found")

node_num = int(m_num.group(1))
if not 0 <= node_num <= 0xFFFFFFFF:
    raise SystemExit(f"myNodeNum is outside uint32 range: {node_num}")

node_id = f"!{node_num:08x}"

m_owner = re.search(r'^Owner:\s*(.*?)\s+\(([^()]*)\)\s*$', text, re.MULTILINE)
if m_owner:
    long_name = m_owner.group(1).strip()
    short_name = m_owner.group(2).strip()
else:
    m_owner = re.search(r'^Owner:\s*(.*?)\s*$', text, re.MULTILINE)
    if not m_owner:
        raise SystemExit("Owner line not found")
    long_name = m_owner.group(1).strip()
    short_name = ""

m_hw = re.search(r'"hwModel"\s*:\s*"([^"]+)"', text)
hardware = (m_hw.group(1) if m_hw else "").strip()

if not long_name:
    long_name = node_id
if not short_name:
    short_name = node_id[-4:].upper()

print(json.dumps({
    "node_id": node_id,
    "long_name": long_name,
    "short_name": short_name,
    "hardware": hardware,
    "port": port,
}, ensure_ascii=False))
PY
)" || fail "Could not parse Meshtastic identity."

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
update_progress "Step 6/7: Configuring MeshCenter..."

TEMPLATE_CONFIG="$INSTALL_DIR/config.example.py"
LIVE_CONFIG="$INSTALL_DIR/config.py"

[[ -f "$TEMPLATE_CONFIG" ]] || fail "Missing template: $TEMPLATE_CONFIG"
[[ ! -e "$LIVE_CONFIG" ]] || fail "$LIVE_CONFIG already exists unexpectedly."

log "Creating production config.py from config.example.py..."

python3 - \
    "$TEMPLATE_CONFIG" \
    "$LIVE_CONFIG" \
    "$NODE_ID" \
    "$LONG_NAME" \
    "$SHORT_NAME" \
    "$HW_MODEL" \
    "$RADIO_PORT" <<'PY'
import ast
import json
import re
import sys
from pathlib import Path

template = Path(sys.argv[1])
output = Path(sys.argv[2])
node_id, long_name, short_name, hw_model, radio_port = sys.argv[3:8]

text = template.read_text(encoding="utf-8")

def replace_simple_assignment(source: str, name: str, value) -> str:
    replacement = f"{name} = {value!r}"
    pattern = rf"(?m)^{re.escape(name)}\s*=.*$"
    new, count = re.subn(pattern, replacement, source, count=1)
    if count != 1:
        raise RuntimeError(f"Could not find assignment for {name}")
    return new

def replace_top_level_assignment_block(source: str, name: str, replacement: str) -> str:
    tree = ast.parse(source)
    target = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = []
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.append(t.id)
            elif isinstance(node.target, ast.Name):
                names.append(node.target.id)
            if name in names:
                target = node
                break
    if target is None or not hasattr(target, "end_lineno"):
        raise RuntimeError(f"Could not find top-level assignment for {name}")
    lines = source.splitlines()
    start = target.lineno - 1
    end = target.end_lineno
    lines[start:end] = replacement.splitlines()
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")

text = replace_simple_assignment(text, "MESHTASTIC_PORT", radio_port)
text = replace_simple_assignment(text, "LOCAL_NODE_ID", node_id)
text = replace_simple_assignment(text, "LOCAL_NODE_NAME", long_name)

known_nodes = (
    "KNOWN_NODES = {\n"
    f"    {node_id!r}: {long_name!r},\n"
    "}"
)
text = replace_top_level_assignment_block(text, "KNOWN_NODES", known_nodes)

known_node_info = (
    "KNOWN_NODE_INFO = {\n"
    f"    {node_id!r}: {{'short_name': {short_name!r}, 'hw_model': {hw_model!r}}},\n"
    "}"
)
text = replace_top_level_assignment_block(text, "KNOWN_NODE_INFO", known_node_info)

# Validate generated Python before writing.
ast.parse(text)

output.write_text(text, encoding="utf-8")
PY

chown "$TARGET_USER:$TARGET_USER" "$LIVE_CONFIG"
chmod 0644 "$LIVE_CONFIG"

# Validate the exact values that will be imported by server.py.
runuser -u "$TARGET_USER" -- \
    env HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
    "$INSTALL_DIR/venv/bin/python" - "$LIVE_CONFIG" "$NODE_ID" "$LONG_NAME" "$RADIO_PORT" <<'PY'
import importlib.util
import sys

path, expected_id, expected_name, expected_port = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("meshcenter_generated_config", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

checks = {
    "LOCAL_NODE_ID": (module.LOCAL_NODE_ID, expected_id),
    "LOCAL_NODE_NAME": (module.LOCAL_NODE_NAME, expected_name),
    "MESHTASTIC_PORT": (module.MESHTASTIC_PORT, expected_port),
}
bad = [f"{key}: {actual!r} != {expected!r}"
       for key, (actual, expected) in checks.items() if actual != expected]
if bad:
    raise SystemExit("; ".join(bad))
PY

log "Production config.py created and validated."

# Ensure clean first identity creation. A fresh install normally has no
# instance.json, but remove any incomplete bootstrap file defensively.
rm -f "$INSTALL_DIR/data/instance.json"
mkdir -p "$INSTALL_DIR/data"
chown -R "$TARGET_USER:$TARGET_USER" "$INSTALL_DIR/data"

# ---------------------------------------------------------------------------
# 9. Install sudoers files and systemd service from repository templates
# ---------------------------------------------------------------------------
update_progress "Step 7/7: Starting MeshCenter service..."

log "Installing MeshCenter system service..."

MESH_USER="$TARGET_USER"
MESH_HOME="$TARGET_HOME"

[[ -f "$INSTALL_DIR/deploy/meshcenter.service" ]] \
    || fail "Missing deploy/meshcenter.service"
[[ -f "$INSTALL_DIR/deploy/meshcenter.sudoers" ]] \
    || fail "Missing deploy/meshcenter.sudoers"
[[ -f "$INSTALL_DIR/deploy/meshcenter-wifi.sudoers" ]] \
    || fail "Missing deploy/meshcenter-wifi.sudoers"

sed -e "s|__MESH_USER__|${MESH_USER}|g" \
    -e "s|__MESH_HOME__|${MESH_HOME}|g" \
    "$INSTALL_DIR/deploy/meshcenter.service" \
    > /etc/systemd/system/meshcenter.service

sed "s|__MESH_USER__|${MESH_USER}|g" \
    "$INSTALL_DIR/deploy/meshcenter.sudoers" \
    > /etc/sudoers.d/meshcenter

sed "s|__MESH_USER__|${MESH_USER}|g" \
    "$INSTALL_DIR/deploy/meshcenter-wifi.sudoers" \
    > /etc/sudoers.d/meshcenter-wifi

chmod 0440 \
    /etc/sudoers.d/meshcenter \
    /etc/sudoers.d/meshcenter-wifi

visudo -cf /etc/sudoers.d/meshcenter >/dev/null \
    || fail "Generated meshcenter sudoers file is invalid."
visudo -cf /etc/sudoers.d/meshcenter-wifi >/dev/null \
    || fail "Generated meshcenter-wifi sudoers file is invalid."

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

update_progress "✅ Installation complete!\nOpening MeshCenter..."
stop_progress_server

exit 0
