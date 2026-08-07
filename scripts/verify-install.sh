#!/usr/bin/env bash
#
# Self-check for a MeshCenter install. Read-only: does not restart the
# service, does not modify config.py/data/, does not require sudo unless
# checking sudoers rules (uses `sudo -n`, which never prompts).
#
# Usage: ./scripts/verify-install.sh
#
# See INSTALL.md for the full checklist this script covers.

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PASS=0
FAIL=0
WARN=0

ok()   { printf '  [OK]   %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf '  [WARN] %s\n' "$1"; WARN=$((WARN + 1)); }

echo "MeshCenter install check — $PROJECT_DIR"
echo

# --- 1. Python virtual environment -----------------------------------------
echo "Python environment"
if [ -x "venv/bin/python" ]; then
    ok "venv/bin/python exists"
    if venv/bin/python -c "import flask" >/dev/null 2>&1; then
        ok "flask importable inside venv"
    else
        bad "flask not importable inside venv — run: source venv/bin/activate && pip install -r requirements.txt"
    fi
    if venv/bin/python -c "import meshtastic" >/dev/null 2>&1; then
        ok "meshtastic package importable inside venv"
    else
        bad "meshtastic package not importable inside venv"
    fi
    if [ -x "venv/bin/meshtastic" ]; then
        ok "meshtastic CLI present at venv/bin/meshtastic"
    else
        warn "no venv/bin/meshtastic — CLI may resolve from elsewhere (MESHTASTIC_CMD in config.py)"
    fi
else
    bad "venv/bin/python missing — venv was not created (see INSTALL.md step 3)"
fi
echo

# --- 2. config.py ------------------------------------------------------------
echo "Configuration"
if [ -f "config.py" ]; then
    ok "config.py exists"
    if [ -x "venv/bin/python" ]; then
        MISSING_VARS=$(venv/bin/python - <<'PYEOF'
import sys
sys.path.insert(0, ".")
required = [
    "APP_HOST", "APP_PORT", "MESHTASTIC_CMD", "LOCAL_NODE_ID", "LOCAL_NODE_NAME",
    "DATA_DIR", "HISTORY_FILE", "NODES_FILE", "SENSORS_FILE", "CHATS_FILE",
    "MAX_HISTORY_MESSAGES", "CHANNEL_CHAT_ID", "CHANNEL_CHAT_NAME",
    "KNOWN_NODES", "KNOWN_NODE_INFO",
]
try:
    import config
except Exception as e:
    print(f"__IMPORT_ERROR__ {e}")
    sys.exit(0)
missing = [v for v in required if not hasattr(config, v)]
print(",".join(missing))
PYEOF
)
        if [[ "$MISSING_VARS" == __IMPORT_ERROR__* ]]; then
            bad "config.py failed to import: ${MISSING_VARS#__IMPORT_ERROR__ }"
        elif [ -z "$MISSING_VARS" ]; then
            ok "config.py defines all required variables"
        else
            bad "config.py is missing: $MISSING_VARS"
        fi

        LOCAL_NODE_ID=$(venv/bin/python -c "import sys; sys.path.insert(0,'.'); import config; print(getattr(config, 'LOCAL_NODE_ID', ''))" 2>/dev/null)
        if [ "$LOCAL_NODE_ID" = "!xxxxxxxx" ] || [ -z "$LOCAL_NODE_ID" ]; then
            warn "LOCAL_NODE_ID still looks like the placeholder from config.example.py"
        else
            ok "LOCAL_NODE_ID is set ($LOCAL_NODE_ID)"
        fi
    fi
else
    bad "config.py missing — run: cp config.example.py config.py (see INSTALL.md step 5)"
fi

if [ -d "data" ]; then
    ok "data/ directory exists"
else
    warn "data/ directory missing — run: mkdir -p data"
fi

if [ -f "weather_secrets.py" ]; then
    ok "weather_secrets.py present (Weather widget configured)"
else
    warn "weather_secrets.py absent — optional, Weather widget stays unconfigured"
fi
echo

# --- 3. systemd service -------------------------------------------------------
echo "systemd service"
if [ -f "/etc/systemd/system/meshcenter.service" ]; then
    ok "unit file installed at /etc/systemd/system/meshcenter.service"
else
    bad "unit file missing — see INSTALL.md step 8"
fi

if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled --quiet meshcenter.service 2>/dev/null; then
        ok "meshcenter.service is enabled (starts on boot)"
    else
        warn "meshcenter.service is not enabled — run: sudo systemctl enable meshcenter.service"
    fi
    if systemctl is-active --quiet meshcenter.service 2>/dev/null; then
        ok "meshcenter.service is active"
    else
        bad "meshcenter.service is not active — run: sudo systemctl status meshcenter.service"
    fi
else
    warn "systemctl not available — skipping service state checks"
fi
echo

# --- 4. sudoers (the step that's easy to forget) ------------------------------
echo "sudoers (system/Wi-Fi actions — commonly forgotten, see INSTALL.md 'Easy to forget')"
if [ -f "/etc/sudoers.d/meshcenter" ]; then
    ok "/etc/sudoers.d/meshcenter present"
else
    bad "/etc/sudoers.d/meshcenter missing — see INSTALL.md step 9"
fi
if [ -f "/etc/sudoers.d/meshcenter-wifi" ]; then
    ok "/etc/sudoers.d/meshcenter-wifi present"
else
    warn "/etc/sudoers.d/meshcenter-wifi missing — Wi-Fi actions in the UI will fail"
fi

# sudo -n never prompts; it just fails if a password would be required.
SUDO_LIST=$(sudo -n -l 2>/dev/null)
if [ -n "$SUDO_LIST" ]; then
    if echo "$SUDO_LIST" | grep -q "systemctl restart meshcenter.service"; then
        ok "NOPASSWD sudo confirmed for: systemctl restart meshcenter.service"
    else
        bad "systemctl restart meshcenter.service NOT in NOPASSWD sudo rules — UI restart action will hang waiting for a password"
    fi
    if echo "$SUDO_LIST" | grep -qE "nmcli|iw$|/usr/sbin/iw"; then
        ok "NOPASSWD sudo confirmed for Wi-Fi commands (nmcli/iw)"
    else
        warn "nmcli/iw not found in NOPASSWD sudo rules — Wi-Fi actions in the UI will fail"
    fi
else
    bad "sudo -n -l returned nothing (or would require a password) — NOPASSWD sudoers not effective for this user"
fi
echo

# --- 5. radio identity ---------------------------------------------------------
echo "Radio identity"
APP_PORT=5000
if [ -x "venv/bin/python" ] && [ -f "config.py" ]; then
    DETECTED_PORT=$(venv/bin/python -c "import sys; sys.path.insert(0,'.'); import config; print(getattr(config, 'APP_PORT', 5000))" 2>/dev/null)
    [ -n "$DETECTED_PORT" ] && APP_PORT="$DETECTED_PORT"
fi

if command -v curl >/dev/null 2>&1; then
    DASHBOARD=$(curl -s --max-time 5 "http://127.0.0.1:${APP_PORT}/api/node-manager/dashboard" 2>/dev/null)
    if [ -z "$DASHBOARD" ]; then
        warn "could not reach http://127.0.0.1:${APP_PORT}/api/node-manager/dashboard — is the service running?"
    elif echo "$DASHBOARD" | grep -q '"identity_status":"MATCH"'; then
        ok "radio identity: MATCH"
    elif echo "$DASHBOARD" | grep -q '"identity_status":"NOT_CHECKED"'; then
        warn "radio identity: NOT_CHECKED — radio may still be initializing, re-run in a few seconds"
    else
        IDENTITY=$(echo "$DASHBOARD" | grep -o '"identity_status":"[A-Z_]*"')
        bad "radio identity is not MATCH ($IDENTITY) — check Serial access is enabled on the radio and the correct port is in config.py"
    fi
else
    warn "curl not available — skipping radio identity check"
fi
echo

# --- Summary -------------------------------------------------------------------
echo "-------------------------------------------------"
echo "Summary: $PASS OK, $WARN warning(s), $FAIL failure(s)"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
