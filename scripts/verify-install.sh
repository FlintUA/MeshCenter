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
info() { printf '  [INFO] %s\n' "$1"; }

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
    # Task 48: meshtastic (GPLv3) no longer belongs in Core's own venv -
    # Core never imports it directly. Its presence here isn't wrong (a
    # pre-split install or a leftover from before the split still has it,
    # harmlessly), but it's no longer required, so this is informational
    # only, never `bad`.
    if venv/bin/python -c "import meshtastic" >/dev/null 2>&1; then
        warn "meshtastic package is importable inside Core's venv — harmless leftover from a pre-split install, no longer required here (see adapters/meshtastic/venv below)"
    fi
else
    bad "venv/bin/python missing — venv was not created (see INSTALL.md step 3)"
fi
echo

# --- 1b. Meshtastic adapter virtual environment (Task 48 venv-split) --------
echo "Meshtastic adapter environment (Task 48 venv-split)"
ADAPTER_VENV="adapters/meshtastic/venv"
if [ -x "${ADAPTER_VENV}/bin/python" ]; then
    ok "${ADAPTER_VENV}/bin/python exists"
    if "${ADAPTER_VENV}/bin/python" -c "import meshtastic" >/dev/null 2>&1; then
        ok "meshtastic package importable inside the adapter venv"
    else
        bad "meshtastic package not importable inside ${ADAPTER_VENV} — run: ${ADAPTER_VENV}/bin/pip install -r adapters/meshtastic/requirements.txt"
    fi
    if "${ADAPTER_VENV}/bin/python" -c "from meshtastic.ble_interface import BLEInterface" >/dev/null 2>&1; then
        ok "BLE support (bleak, a base meshtastic dependency) importable inside the adapter venv"
    else
        bad "BLE support not importable inside ${ADAPTER_VENV} — meshtastic itself may be missing/broken (bleak is a base dependency, not an extra - see adapters/meshtastic/requirements.txt); run: ${ADAPTER_VENV}/bin/pip install -r adapters/meshtastic/requirements.txt"
    fi
    if [ -x "${ADAPTER_VENV}/bin/meshtastic" ]; then
        ok "meshtastic CLI present at ${ADAPTER_VENV}/bin/meshtastic"
    else
        warn "no ${ADAPTER_VENV}/bin/meshtastic — CLI may resolve from elsewhere (MESHTASTIC_CMD in config.py, see resolve_meshtastic_cli())"
    fi
else
    bad "${ADAPTER_VENV}/bin/python missing — the adapter venv was not created. Core will still start (transport status ADAPTER_UNAVAILABLE, by design) but the radio will not connect. Run: python3 -m venv ${ADAPTER_VENV} && ${ADAPTER_VENV}/bin/pip install -r adapters/meshtastic/requirements.txt (see install.sh's step_adapter_venv — a plain 'git pull' update never runs this step on an existing install, same class of gap as task 39's sudoers checks above)"
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
# `sudo -n -l` (never prompts) is the only reliable check here: it reports
# what this user could actually run as root, whether that comes from
# deploy/meshcenter.sudoers or from a broader NOPASSWD rule set some other
# way. Testing for /etc/sudoers.d/meshcenter with `[ -f ... ]` looks
# appealing but is unreliable — that directory is typically mode 750
# root:root, so a non-root user gets "Permission denied" on stat(), which
# `[ -f ]` can't distinguish from "file doesn't exist".
SUDO_LIST=$(sudo -n -l 2>/dev/null)
if [ -z "$SUDO_LIST" ]; then
    bad "sudo -n -l returned nothing (or would require a password) — this user has no passwordless sudo at all; UI restart/reboot/Wi-Fi actions will hang waiting for a password"
elif echo "$SUDO_LIST" | grep -qE "NOPASSWD:\s*ALL|systemctl restart meshcenter\.service"; then
    ok "NOPASSWD sudo confirmed for: systemctl restart meshcenter.service"
else
    bad "systemctl restart meshcenter.service not covered by passwordless sudo — UI restart action will hang waiting for a password (see deploy/meshcenter.sudoers)"
fi
if [ -n "$SUDO_LIST" ] && echo "$SUDO_LIST" | grep -qE "NOPASSWD:\s*ALL|nmcli|/usr/sbin/iw"; then
    ok "NOPASSWD sudo confirmed for Wi-Fi commands (nmcli/iw)"
else
    warn "nmcli/iw not covered by passwordless sudo — Wi-Fi actions in the UI will fail (see deploy/meshcenter-wifi.sudoers)"
fi
# Not `bad` - this only affects the Devices tab's optional I2C/RTC card, not
# core MeshCenter functionality. An install predating task 23 (PR #84) that
# has only ever been updated via `git pull` + service restart will land here
# silently: update_service.py deliberately never touches system files, so
# deploy/meshcenter-hw.sudoers only gets installed by install.sh/
# meshcenter-firstboot.sh themselves, never by an update - confirmed live on
# a real node (067A40FA, task 39) whose "Enable I2C & configure RTC" button
# failed with exactly this gap.
if [ -n "$SUDO_LIST" ] && echo "$SUDO_LIST" | grep -qE "NOPASSWD:\s*ALL|meshcenter-hw-config"; then
    ok "NOPASSWD sudo confirmed for meshcenter-hw-config (I2C/RTC hardware setup)"
else
    warn "meshcenter-hw-config not covered by passwordless sudo — the Devices tab's 'Enable I2C & configure RTC' button will fail (see deploy/meshcenter-hw.sudoers; on an install that predates this feature, updating via git pull alone will NOT add it — install the sudoers file manually or re-run install.sh/meshcenter-firstboot.sh's relevant step)"
fi
if [ -x "/usr/local/sbin/meshcenter-hw-config" ]; then
    ok "meshcenter-hw-config helper installed at /usr/local/sbin/meshcenter-hw-config"
else
    warn "/usr/local/sbin/meshcenter-hw-config missing or not executable — the I2C/RTC hardware card will fail even with correct sudoers (see scripts/meshcenter-hw-config, installed by install.sh/meshcenter-firstboot.sh)"
fi
echo

# --- 5. radio identity ---------------------------------------------------------
# Primary source: data/instance.json's runtime.identity_status, written
# synchronously by verify_radio_identity() at the very start of
# start_runtime() (server.py) - before the listener starts, before Flask
# serves a single request. Reading it directly off disk needs no HTTP call
# and therefore no auth at all, unlike the old curl-only check below, which
# started reporting a false FAIL on every fresh install once AUTH_ENABLED
# defaulted to True (see PR #131) - a protected /api/node-manager/dashboard
# correctly returns 401, but the old code only ever looked for
# "identity_status":"MATCH"/"NOT_CHECKED" substrings in the body and fell
# through to "not MATCH" for anything else, including an auth_required 401.
echo "Radio identity"
APP_PORT=5000
DATA_DIR="data"
if [ -x "venv/bin/python" ] && [ -f "config.py" ]; then
    CONFIG_VALUES=$(venv/bin/python -c "
import sys
sys.path.insert(0, '.')
import config
print(getattr(config, 'APP_PORT', 5000))
print(getattr(config, 'DATA_DIR', 'data'))
" 2>/dev/null)
    DETECTED_PORT=$(echo "$CONFIG_VALUES" | sed -n '1p')
    DETECTED_DATA_DIR=$(echo "$CONFIG_VALUES" | sed -n '2p')
    [ -n "$DETECTED_PORT" ] && APP_PORT="$DETECTED_PORT"
    [ -n "$DETECTED_DATA_DIR" ] && DATA_DIR="$DETECTED_DATA_DIR"
fi

INSTANCE_FILE="${DATA_DIR}/instance.json"
IDENTITY_STATUS=""
if [ -x "venv/bin/python" ] && [ -f "$INSTANCE_FILE" ]; then
    IDENTITY_STATUS=$(venv/bin/python -c "
import json
try:
    with open('${INSTANCE_FILE}', encoding='utf-8') as f:
        data = json.load(f)
    runtime = data.get('runtime') if isinstance(data.get('runtime'), dict) else {}
    print(runtime.get('identity_status') or 'NOT_CHECKED')
except Exception as e:
    print('__READ_ERROR__ ' + str(e))
" 2>/dev/null)
fi

if [ -n "$IDENTITY_STATUS" ] && [[ "$IDENTITY_STATUS" != __READ_ERROR__* ]]; then
    case "$IDENTITY_STATUS" in
        MATCH)
            ok "radio identity: MATCH (${INSTANCE_FILE})"
            ;;
        NOT_CHECKED)
            warn "radio identity: NOT_CHECKED (${INSTANCE_FILE}) — radio may still be initializing, re-run in a few seconds"
            ;;
        *)
            bad "radio identity is not MATCH (${IDENTITY_STATUS}, ${INSTANCE_FILE}) — check Serial access is enabled on the radio and the correct port is in config.py"
            ;;
    esac
else
    # Fallback only: instance.json missing/unreadable (e.g. first-ever
    # startup hasn't written it yet, or DATA_DIR resolves unexpectedly).
    # Never treat a 401 here as a real failure - it just means
    # AUTH_ENABLED=True (the default since PR #131) is doing its job, and
    # this script must never try to bypass it to get a definitive answer.
    if command -v curl >/dev/null 2>&1; then
        DASHBOARD_TMP=$(mktemp 2>/dev/null || echo "/tmp/mc-verify-dashboard.$$")
        HTTP_CODE=$(curl -s -o "$DASHBOARD_TMP" -w '%{http_code}' --max-time 5 "http://127.0.0.1:${APP_PORT}/api/node-manager/dashboard" 2>/dev/null)
        DASHBOARD=$(cat "$DASHBOARD_TMP" 2>/dev/null)
        rm -f "$DASHBOARD_TMP"
        case "$HTTP_CODE" in
            200)
                if echo "$DASHBOARD" | grep -q '"identity_status":"MATCH"'; then
                    ok "radio identity: MATCH (HTTP fallback — ${INSTANCE_FILE} was unavailable)"
                elif echo "$DASHBOARD" | grep -q '"identity_status":"NOT_CHECKED"'; then
                    warn "radio identity: NOT_CHECKED — radio may still be initializing, re-run in a few seconds"
                else
                    IDENTITY=$(echo "$DASHBOARD" | grep -o '"identity_status":"[A-Z_]*"')
                    bad "radio identity is not MATCH ($IDENTITY) — check Serial access is enabled on the radio and the correct port is in config.py"
                fi
                ;;
            401)
                info "Authentication enabled - protected API check skipped (${INSTANCE_FILE} was also unavailable; log in via the UI to verify radio identity manually)"
                ;;
            "" | 000)
                warn "could not reach http://127.0.0.1:${APP_PORT}/api/node-manager/dashboard — is the service running?"
                ;;
            *)
                warn "http://127.0.0.1:${APP_PORT}/api/node-manager/dashboard returned HTTP $HTTP_CODE — unexpected, skipping radio identity check"
                ;;
        esac
    else
        warn "curl not available and ${INSTANCE_FILE} unreadable — skipping radio identity check"
    fi
fi
echo

# --- Summary -------------------------------------------------------------------
echo "-------------------------------------------------"
echo "Summary: $PASS OK, $WARN warning(s), $FAIL failure(s)"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
