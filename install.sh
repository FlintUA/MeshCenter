#!/usr/bin/env bash
# MeshCenter Installer
# https://github.com/FlintUA/MeshCenter
#
# Usage (automatic):
#   Placed on SD card as firstrun.sh — runs on first Pi boot
#
# Usage (manual):
#   curl -sSL https://raw.githubusercontent.com/FlintUA/MeshCenter/main/install.sh | bash
#   or: bash install.sh
#
# Supports: Raspberry Pi Zero 2W, Pi 3B/3B+, Pi 4B, Pi 5
# OS:       Raspberry Pi OS Lite or Desktop 64-bit (Debian Bullseye/Bookworm)
#           Ubuntu Server 22.04/24.04 LTS

set -euo pipefail

# ─── Constants ────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/FlintUA/MeshCenter"
INSTALL_DIR="${HOME}/meshcenter"
SERVICE_NAME="meshcenter"
APP_PORT=5000
PROGRESS_PORT=80
LOG_FILE="/tmp/meshcenter-install.log"
PROGRESS_FILE="/tmp/meshcenter-progress.txt"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=9
MIN_FREE_MB=500
MIN_RAM_MB=512

# Swap settings for low-RAM devices
SWAP_INSTALL_MB=512
ORIGINAL_SWAP_MB=100
NEEDS_SWAP=false
DEVICE_NAME="Unknown"
PROGRESS_PID=""
RADIO_PORT=""

# ─── Colors ───────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'
    GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; BLUE=''; NC=''
fi

# ─── Helpers ──────────────────────────────────────────────────────────────────
log()      { echo -e "${GREEN}[MeshCenter]${NC} $*"; }
warn()     { echo -e "${YELLOW}[WARNING]${NC} $*" >&2; }
progress() { echo "$*" > "$PROGRESS_FILE"; log "$*"; }

fail() {
    echo -e "\n${RED}[ERROR]${NC} $*" | tee -a "$LOG_FILE" >&2
    echo "" | tee -a "$LOG_FILE"
    echo "Installation failed. Full log: $LOG_FILE" | tee -a "$LOG_FILE"
    echo "To retry: bash ${INSTALL_DIR}/install.sh" | tee -a "$LOG_FILE"
    [[ -n "$PROGRESS_PID" ]] && kill "$PROGRESS_PID" 2>/dev/null || true
    exit 1
}

trap 'fail "Unexpected error at line $LINENO (exit code: $?)"' ERR

# ─── Step 0: Preflight checks ────────────────────────────────────────────────
step_preflight() {
    progress "1/9 Checking system requirements..."

    # Linux only
    [[ "$(uname -s)" == "Linux" ]] || \
        fail "MeshCenter requires Linux. Windows and macOS are not supported."

    # apt-get (Debian/Ubuntu)
    command -v apt-get &>/dev/null || \
        fail "apt-get not found. MeshCenter requires Debian 11+ or Raspberry Pi OS."

    # OS version
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        DISTRO_ID="${ID:-unknown}"
        DISTRO_VERSION="${VERSION_ID:-0}"
        DISTRO_NAME="${PRETTY_NAME:-Unknown OS}"

        case "$DISTRO_ID" in
            debian|raspbian|ubuntu) ;;
            *) warn "Untested OS: $DISTRO_NAME. Proceeding anyway." ;;
        esac

        if [[ "$DISTRO_ID" =~ ^(debian|raspbian)$ ]]; then
            if [[ "${DISTRO_VERSION%%.*}" -lt 11 ]] 2>/dev/null; then
                fail "Debian 11 (Bullseye) or newer required.
       Found: $DISTRO_NAME
       Please upgrade: https://www.raspberrypi.com/software/"
            fi
        fi

        if [[ "$DISTRO_ID" == "ubuntu" ]]; then
            if [[ "${DISTRO_VERSION%%.*}" -lt 22 ]] 2>/dev/null; then
                fail "Ubuntu 22.04 LTS or newer required. Found: $DISTRO_NAME"
            fi
        fi

        log "OS: $DISTRO_NAME ✓"
    else
        warn "Cannot detect OS version. Proceeding anyway."
    fi

    # Architecture
    ARCH=$(uname -m)
    case "$ARCH" in
        aarch64|x86_64|arm64) log "Architecture: $ARCH ✓" ;;
        armv7l|armv6l)
            warn "32-bit ARM detected ($ARCH). 64-bit OS is strongly recommended."
            warn "Some packages (lgpio, cryptography) may have issues on 32-bit."
            read -rp "Continue anyway? [y/N] " REPLY
            [[ "$REPLY" =~ ^[Yy]$ ]] || exit 0
            ;;
        *) warn "Unknown architecture: $ARCH. Proceeding anyway." ;;
    esac

    # Python >= 3.9
    command -v python3 &>/dev/null || \
        fail "python3 not found. Install Python 3.9+ first."

    PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
    PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
    PY_VER="${PY_MAJOR}.${PY_MINOR}"

    if [[ "$PY_MAJOR" -lt "$MIN_PYTHON_MAJOR" ]] || \
       [[ "$PY_MAJOR" -eq "$MIN_PYTHON_MAJOR" && \
          "$PY_MINOR" -lt "$MIN_PYTHON_MINOR" ]]; then
        fail "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ required.
       Found: Python $PY_VER
       Run: sudo apt-get install python3"
    fi
    log "Python $PY_VER ✓"

    # systemd
    command -v systemctl &>/dev/null || \
        fail "systemd not found. MeshCenter requires systemd to run as a service."
    log "systemd ✓"

    # Internet
    log "Checking internet connection..."
    ping -c1 -W5 github.com &>/dev/null 2>&1 || \
        fail "No internet connection. Check your WiFi/Ethernet and try again."
    log "Internet ✓"

    # Disk space
    FREE_MB=$(df -m "$HOME" | awk 'NR==2 {print $4}')
    [[ "$FREE_MB" -ge "$MIN_FREE_MB" ]] || \
        fail "Not enough disk space. Need ${MIN_FREE_MB}MB, have ${FREE_MB}MB free."
    log "Disk space: ${FREE_MB}MB free ✓"

    # RAM — detect device and swap need
    RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    log "RAM: ${RAM_MB}MB"

    if [[ "$RAM_MB" -lt "$MIN_RAM_MB" ]]; then
        warn "Low RAM (${RAM_MB}MB). Installation may be slow."
    fi

    # Detect Pi model from /proc/cpuinfo
    if grep -q "Raspberry Pi Zero 2" /proc/cpuinfo 2>/dev/null; then
        DEVICE_NAME="Raspberry Pi Zero 2W"
        NEEDS_SWAP=true
    elif grep -q "Raspberry Pi 3" /proc/cpuinfo 2>/dev/null; then
        DEVICE_NAME="Raspberry Pi 3"
        NEEDS_SWAP=false
    elif grep -q "Raspberry Pi 4" /proc/cpuinfo 2>/dev/null; then
        DEVICE_NAME="Raspberry Pi 4"
        NEEDS_SWAP=false
    elif grep -q "Raspberry Pi 5" /proc/cpuinfo 2>/dev/null; then
        DEVICE_NAME="Raspberry Pi 5"
        NEEDS_SWAP=false
    else
        DEVICE_NAME="Linux device"
        [[ "$RAM_MB" -lt 1024 ]] && NEEDS_SWAP=true || NEEDS_SWAP=false
    fi

    log "Device: $DEVICE_NAME ✓"
    log "All preflight checks passed ✓"
}

# ─── Step 1: Progress server ──────────────────────────────────────────────────
step_progress_server() {
    # Minimal HTTP server showing install status.
    # User opens http://meshcenter.local and sees progress.
    # Non-critical if it fails to start (port busy or no permission).

    echo "Starting..." > "$PROGRESS_FILE"

    python3 - <<'PYEOF' &
import http.server, socketserver, os, time

PROGRESS_FILE = "/tmp/meshcenter-progress.txt"
PORT = 80

HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="3">
    <title>MeshCenter Installing...</title>
    <style>
        body {{ font-family: sans-serif; max-width: 600px; margin: 80px auto;
                padding: 0 20px; background: #f8f9fa; color: #333; }}
        h1 {{ color: #2d7d46; }}
        .status {{ background: #fff; border: 1px solid #ddd; border-radius: 8px;
                   padding: 24px; margin-top: 24px; font-size: 18px; }}
        .footer {{ margin-top: 24px; color: #888; font-size: 13px; }}
    </style>
</head>
<body>
    <h1>🟢 MeshCenter</h1>
    <div class="status">{status}</div>
    <div class="footer">This page refreshes automatically every 3 seconds.</div>
</body>
</html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            status = open(PROGRESS_FILE).read().strip()
        except Exception:
            status = "Installing..."
        body = HTML.format(status=status).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass  # quiet

try:
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()
except Exception:
    pass  # port busy or no permission — just don't start
PYEOF

    PROGRESS_PID=$!
    # Give it a second to start
    sleep 1
    # Check it actually started
    if kill -0 "$PROGRESS_PID" 2>/dev/null; then
        log "Progress server started on port $PROGRESS_PORT (PID $PROGRESS_PID)"
        log "Watch progress at: http://$(hostname).local or http://$(hostname -I | awk '{print $1}')"
    else
        PROGRESS_PID=""
        log "Progress server not started (port $PROGRESS_PORT busy or no permission — OK)"
    fi
}

# ─── Step 2: System packages ─────────────────────────────────────────────────
step_system_packages() {
    progress "2/9 Installing system packages..."

    sudo apt-get update -qq
    sudo apt-get install -y \
        git \
        python3-venv \
        python3-pip \
        avahi-daemon \
        network-manager \
        iw \
        lsof \
        --no-install-recommends \
        -qq

    # avahi-daemon for meshcenter.local (mDNS)
    sudo systemctl enable avahi-daemon --quiet 2>/dev/null || true
    sudo systemctl start  avahi-daemon         2>/dev/null || true

    # Camera support (Picamera2) — optional, must be installed before the
    # venv is created so --system-site-packages can see it. Not fatal if
    # unavailable (e.g. non-Pi hardware or repo without picamera2 packages).
    if apt-cache show python3-picamera2 &>/dev/null; then
        sudo apt-get install -y python3-picamera2 rpicam-apps --no-install-recommends -qq \
            || warn "Camera packages failed to install — camera feature will be unavailable."
    else
        warn "python3-picamera2 not available on this system — camera feature will be unavailable."
    fi

    log "System packages installed ✓"
}

# ─── Step 3: Swap (for Pi Zero 2W and devices with < 1GB RAM) ────────────────
step_swap() {
    [[ "$NEEDS_SWAP" == "true" ]] || return 0

    progress "3/9 Configuring swap for ${DEVICE_NAME}..."

    if [[ -f /etc/dphys-swapfile ]]; then
        ORIGINAL_SWAP_MB=$(grep CONF_SWAPSIZE /etc/dphys-swapfile \
                           | cut -d= -f2 | tr -d ' ' || echo 100)
        sudo dphys-swapfile swapoff 2>/dev/null || true
        sudo sed -i "s/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${SWAP_INSTALL_MB}/" \
            /etc/dphys-swapfile
        sudo dphys-swapfile setup
        sudo dphys-swapfile swapon
        log "Swap increased to ${SWAP_INSTALL_MB}MB for installation ✓"
    else
        warn "dphys-swapfile not found — skipping swap setup"
    fi
}

# ─── Step 4: Clone / update repo ─────────────────────────────────────────────
step_clone() {
    progress "4/9 Downloading MeshCenter..."

    if [[ -d "$INSTALL_DIR/.git" ]]; then
        log "Existing installation found at $INSTALL_DIR"
        read -rp "Update existing installation? [Y/n] " REPLY
        if [[ ! "$REPLY" =~ ^[Nn]$ ]]; then
            git -C "$INSTALL_DIR" pull --quiet
            git -C "$INSTALL_DIR" fetch --tags --quiet
            log "Updated to $(git -C "$INSTALL_DIR" describe --tags) ✓"
        else
            log "Update skipped."
            exit 0
        fi
    else
        git clone "$REPO_URL" "$INSTALL_DIR" --quiet
        git -C "$INSTALL_DIR" fetch --tags --quiet
        log "MeshCenter $(git -C "$INSTALL_DIR" describe --tags) downloaded ✓"
    fi
}

# ─── Step 5: Python virtualenv + dependencies ────────────────────────────────
step_venv() {
    progress "5/9 Installing Python dependencies (may take several minutes on ${DEVICE_NAME})..."

    # --system-site-packages is required so Picamera2 (installed system-wide
    # in step 2) is visible inside the venv — see CLAUDE.md / INSTALL.md.
    python3 -m venv --system-site-packages "${INSTALL_DIR}/venv"
    # shellcheck source=/dev/null
    source "${INSTALL_DIR}/venv/bin/activate"

    pip install --upgrade pip --quiet
    pip install -r "${INSTALL_DIR}/requirements.txt" --quiet

    log "Python dependencies installed ✓"
}

# ─── Step 6: Config ───────────────────────────────────────────────────────────
step_config() {
    progress "6/9 Configuring MeshCenter..."

    mkdir -p "${INSTALL_DIR}/data"

    if [[ ! -f "${INSTALL_DIR}/config.py" ]]; then
        cp "${INSTALL_DIR}/config.example.py" "${INSTALL_DIR}/config.py"
        log "config.py created from template ✓"
    else
        log "config.py already exists — not overwritten ✓"
    fi
}

# ─── Step 7: Detect radio ─────────────────────────────────────────────────────
step_detect_radio() {
    progress "7/9 Detecting Meshtastic radio..."

    # Add user to dialout for serial access
    sudo usermod -a -G dialout "$USER" 2>/dev/null || true

    # Find device
    RADIO_PORT=""
    for DEV in /dev/ttyACM* /dev/ttyUSB*; do
        if [[ -e "$DEV" ]]; then
            RADIO_PORT="$DEV"
            break
        fi
    done

    if [[ -n "$RADIO_PORT" ]]; then
        log "Meshtastic device detected: $RADIO_PORT ✓"
    else
        log "No Meshtastic device detected. Connect it later via Settings."
    fi
}

# ─── Step 8: systemd service ──────────────────────────────────────────────────
step_systemd() {
    progress "8/9 Setting up system service..."

    # Render the repo's own systemd unit template rather than duplicating it.
    MESH_USER="$USER"
    MESH_HOME="$HOME"
    sed -e "s|__MESH_USER__|${MESH_USER}|g" \
        -e "s|__MESH_HOME__|${MESH_HOME}|g" \
        "${INSTALL_DIR}/deploy/meshcenter.service" \
        | sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null

    # Render the repo's sudoers templates (service restart/reboot/poweroff,
    # plus Wi-Fi management) instead of a partial hand-rolled set — the UI's
    # System and Wi-Fi actions depend on both being present.
    sed "s|__MESH_USER__|${MESH_USER}|g" "${INSTALL_DIR}/deploy/meshcenter.sudoers" \
        | sudo tee /etc/sudoers.d/${SERVICE_NAME} > /dev/null
    sed "s|__MESH_USER__|${MESH_USER}|g" "${INSTALL_DIR}/deploy/meshcenter-wifi.sudoers" \
        | sudo tee /etc/sudoers.d/${SERVICE_NAME}-wifi > /dev/null
    sudo chmod 0440 /etc/sudoers.d/${SERVICE_NAME} /etc/sudoers.d/${SERVICE_NAME}-wifi

    sudo visudo -cf /etc/sudoers.d/${SERVICE_NAME} \
        || fail "Generated sudoers file /etc/sudoers.d/${SERVICE_NAME} failed validation."
    sudo visudo -cf /etc/sudoers.d/${SERVICE_NAME}-wifi \
        || fail "Generated sudoers file /etc/sudoers.d/${SERVICE_NAME}-wifi failed validation."

    # Start the service
    sudo systemctl daemon-reload
    sudo systemctl enable  ${SERVICE_NAME} --quiet
    sudo systemctl restart ${SERVICE_NAME}

    # Wait for it to come up (up to 15 seconds)
    for i in $(seq 1 15); do
        if sudo systemctl is-active ${SERVICE_NAME} --quiet; then
            log "Service started ✓"
            return 0
        fi
        sleep 1
    done

    warn "Service may not have started yet. Check: sudo systemctl status ${SERVICE_NAME}"
}

# ─── Step 9: Cleanup + Done ───────────────────────────────────────────────────
step_cleanup() {
    # Restore swap
    if [[ "$NEEDS_SWAP" == "true" && -f /etc/dphys-swapfile ]]; then
        sudo dphys-swapfile swapoff 2>/dev/null || true
        sudo sed -i "s/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${ORIGINAL_SWAP_MB}/" \
            /etc/dphys-swapfile
        sudo dphys-swapfile setup
        sudo dphys-swapfile swapon 2>/dev/null || true
        log "Swap restored to ${ORIGINAL_SWAP_MB}MB ✓"
    fi

    # Remove trigger file if run via firstrun
    rm -f /boot/firmware/meshcenter-install 2>/dev/null || true
    rm -f /boot/meshcenter-install          2>/dev/null || true
}

step_done() {
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    VERSION=$(git -C "$INSTALL_DIR" describe --tags 2>/dev/null || echo "unknown")

    # Final message in progress file
    echo "Installation complete! Redirecting to MeshCenter..." > "$PROGRESS_FILE"

    # Stop progress server
    if [[ -n "$PROGRESS_PID" ]]; then
        kill "$PROGRESS_PID" 2>/dev/null || true
        PROGRESS_PID=""
    fi

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║   MeshCenter ${VERSION} — installed!          ${NC}"
    echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║   Open in browser:                           ${NC}"
    echo -e "${GREEN}║   http://$(hostname).local:${APP_PORT}        ${NC}"
    if [[ -n "$LOCAL_IP" ]]; then
    echo -e "${GREEN}║   http://${LOCAL_IP}:${APP_PORT}              ${NC}"
    fi
    echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
    if [[ -n "$RADIO_PORT" ]]; then
    echo -e "${GREEN}║   Radio:  ${RADIO_PORT} (auto-detected)       ${NC}"
    else
    echo -e "${YELLOW}║   Radio:  not detected — connect via Settings${NC}"
    fi
    echo -e "${GREEN}╠══════════════════════════════════════════════╣${NC}"
    echo -e "${GREEN}║   Log:    ${LOG_FILE}                         ${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

# ─── Main ─────────────────────────────────────────────────────────────────────
main() {
    echo ""
    echo -e "${BLUE}MeshCenter Installer${NC}"
    echo -e "${BLUE}════════════════════${NC}"
    echo ""

    # Log all output
    exec > >(tee -a "$LOG_FILE") 2>&1

    step_preflight
    step_progress_server
    step_system_packages
    step_swap
    step_clone
    step_venv
    step_config
    step_detect_radio
    step_systemd
    step_cleanup
    step_done
}

main "$@"
