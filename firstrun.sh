#!/usr/bin/env bash
# MeshCenter First Run Hook
#
# INSTALLATION:
#   1. Flash Raspberry Pi OS Lite 64-bit with Raspberry Pi Imager
#      (set hostname, username, password, WiFi in Imager settings)
#   2. Copy THIS FILE to the SD card boot partition:
#        Windows: copy to D:\firstrun.sh  (D: = SD card boot drive)
#        Mac:     copy to /Volumes/bootfs/firstrun.sh
#   3. Insert SD card, power on Pi
#   4. Wait 5-10 minutes
#   5. Open: http://meshcenter.local:5000
#
# Watch installation progress at: http://meshcenter.local (port 80)
#
# DO NOT RENAME this file — Pi OS looks for it by exact name.

LOG_FILE="/var/log/meshcenter-firstrun.log"

# Wait for network to come up (up to 120 seconds)
wait_for_network() {
    echo "[$(date)] Waiting for network..." >> "$LOG_FILE"
    for i in $(seq 1 24); do
        if ping -c1 -W3 github.com &>/dev/null 2>&1; then
            echo "[$(date)] Network available ✓" >> "$LOG_FILE"
            return 0
        fi
        sleep 5
    done
    echo "[$(date)] ERROR: No network after 120s" >> "$LOG_FILE"
    return 1
}

main() {
    echo "[$(date)] MeshCenter firstrun started" >> "$LOG_FILE"
    echo "[$(date)] Device: $(cat /proc/device-tree/model 2>/dev/null || echo unknown)" \
        >> "$LOG_FILE"

    # Wait for network
    if ! wait_for_network; then
        echo "[$(date)] Installation skipped — no internet." >> "$LOG_FILE"
        echo "Please reboot and ensure internet is available." >> "$LOG_FILE"
        exit 1
    fi

    # Download and run install.sh
    echo "[$(date)] Downloading and running install.sh..." >> "$LOG_FILE"

    curl -sSL \
        https://raw.githubusercontent.com/FlintUA/MeshCenter/main/install.sh \
        | bash >> "$LOG_FILE" 2>&1

    INSTALL_EXIT=$?

    if [[ $INSTALL_EXIT -eq 0 ]]; then
        echo "[$(date)] Installation completed successfully ✓" >> "$LOG_FILE"
    else
        echo "[$(date)] Installation failed (exit code: $INSTALL_EXIT)" >> "$LOG_FILE"
        echo "[$(date)] Log: $LOG_FILE" >> "$LOG_FILE"
        exit 1
    fi

    # Self-delete after successful installation
    rm -f /boot/firmware/firstrun.sh 2>/dev/null || true
    rm -f /boot/firstrun.sh          2>/dev/null || true
}

main
