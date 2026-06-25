#!/bin/bash
# Deploy the Stress Dashboard postprocessor + web app to an Nx AI Manager server.
#
# Usage:   ./deploy.sh <server_ip> <ssh_user> <ssh_password> [port] [--start]
# Example: ./deploy.sh <SERVER_IP> nx <NX_PASSWORD> 8120 --start
#
# Works for both Nx Meta (/opt/networkoptix-metavms) and Nx Witness
# (/opt/networkoptix); the install root and service user are auto-detected on
# the remote host. Requires: sshpass (brew install hudochenkov/sshpass/sshpass)
#
# --start : also (re)start web_app.py on the remote as root (needed for true
#           Intel iGPU engine utilisation). Omit to start it yourself later.

set -e

SERVER_IP="${1:?Usage: $0 <server_ip> <ssh_user> <ssh_password> [port] [--start]}"
SSH_USER="${2:?}"
SSH_PASS="${3:?}"
PORT="${4:-8120}"
START_WEBAPP=0
for a in "$@"; do [ "$a" = "--start" ] && START_WEBAPP=1; done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SSH="sshpass -p $SSH_PASS ssh -o StrictHostKeyChecking=no $SSH_USER@$SERVER_IP"
SCP="sshpass -p $SSH_PASS scp -o StrictHostKeyChecking=no"
SUDO="echo $SSH_PASS | sudo -S -p ''"

echo "=== Stress Dashboard deploy → $SERVER_IP (port $PORT) ==="

# 1. Detect Nx install root + service user on the remote.
echo "[1/7] Detecting Nx install..."
read NX_ROOT OWNER <<< "$($SSH bash <<'EOF'
if [ -d /opt/networkoptix-metavms ]; then echo "/opt/networkoptix-metavms networkoptix-metavms";
elif [ -d /opt/networkoptix ]; then echo "/opt/networkoptix networkoptix";
else echo "NONE NONE"; fi
EOF
)"
if [ "$NX_ROOT" = "NONE" ]; then echo "ERROR: no Nx install found on $SERVER_IP"; exit 1; fi
PP_DIR="$NX_ROOT/mediaserver/var/nx_ai_manager/nxai_manager/postprocessors"
ETC_DIR="$NX_ROOT/mediaserver/var/nx_ai_manager/nxai_manager/etc"
echo "      root=$NX_ROOT  user=$OWNER"
echo "      postprocessors=$PP_DIR"

# 2. Python deps (postprocessor: msgpack; web app: psutil, nvidia-ml-py optional).
echo "[2/7] Installing Python deps (msgpack, psutil, nvidia-ml-py)..."
$SSH "$SUDO pip3 install --break-system-packages msgpack psutil nvidia-ml-py 2>&1 | tail -2"

# 3. Upload all files.
echo "[3/7] Uploading files..."
for f in nxai_communication_utils.py metrics.py ui_templates.py web_app.py \
         postprocessor-python-stress-dashboard.py; do
    $SCP "$SCRIPT_DIR/$f" "$SSH_USER@$SERVER_IP:/tmp/$f"
done
$SSH "$SUDO mkdir -p '$PP_DIR' '$ETC_DIR'"
$SSH "$SUDO bash -c 'for f in nxai_communication_utils.py metrics.py ui_templates.py web_app.py postprocessor-python-stress-dashboard.py; do cp /tmp/\$f \"$PP_DIR/\$f\"; chown $OWNER:$OWNER \"$PP_DIR/\$f\"; done; chmod 755 \"$PP_DIR/postprocessor-python-stress-dashboard.py\" \"$PP_DIR/web_app.py\"'"

# 3b. Install the NX C utilities shared library that the official comm utils
#     (ctypes) loads. The postprocessor will NOT receive any inference data
#     without it (the pure-python stub does not speak the real ready-signal/SHM
#     protocol). Prefer a native .so already on the target (correct arch); fall
#     back to the x86-64 copy bundled in this repo.
echo "[3b/7] Installing nxai C utilities shared library..."
SOLIB="libnxai-c-utilities-shared.so"
NATIVE_SO=$($SSH "find /opt /home -name '$SOLIB' 2>/dev/null | head -1" | tr -d '\r')
if [ -n "$NATIVE_SO" ]; then
    echo "      using target-native lib: $NATIVE_SO"
    $SSH "$SUDO cp '$NATIVE_SO' '$PP_DIR/$SOLIB'"
elif [ -f "$SCRIPT_DIR/$SOLIB" ]; then
    echo "      shipping bundled x86-64 lib (no native lib found on target)"
    $SCP "$SCRIPT_DIR/$SOLIB" "$SSH_USER@$SERVER_IP:/tmp/$SOLIB"
    $SSH "$SUDO cp /tmp/$SOLIB '$PP_DIR/$SOLIB'"
else
    echo "      WARNING: no $SOLIB on target or in repo — postprocessor will get 0 frames!"
fi
$SSH "$SUDO bash -c 'test -f \"$PP_DIR/$SOLIB\" && chown $OWNER:$OWNER \"$PP_DIR/$SOLIB\" || true'"

# 4. Bash wrapper the AI Manager launches (must be on PATH-style exec).
echo "[4/7] Creating launcher wrapper..."
$SSH "$SUDO bash -c 'printf \"#!/bin/bash\nexec python3 \\\"\\\$(dirname \\\"\\\$0\\\")/postprocessor-python-stress-dashboard.py\\\" \\\"\\\$@\\\"\n\" > \"$PP_DIR/postprocessor-python-stress-dashboard\"; chown $OWNER:$OWNER \"$PP_DIR/postprocessor-python-stress-dashboard\"; chmod 755 \"$PP_DIR/postprocessor-python-stress-dashboard\"'"

# 5. Config (.ini) — only create if absent, so we don't clobber local edits.
#    [nx] holds the local Nx REST API creds used to look up per-camera stream
#    res/fps for the report. Default password = this deploy's SSH password (on
#    these demo boxes the Nx admin password usually matches); edit if different.
echo "[5/7] Writing config (if absent)..."
$SSH "$SUDO bash -c 'test -f \"$ETC_DIR/plugin.stress-dashboard.ini\" || printf \"[common]\nlog_level = INFO\n\n[web_app]\nurl = http://localhost:$PORT\n\n[web_server]\nport = $PORT\n\n[nx]\nurl = https://127.0.0.1:7001\nuser = admin\npassword = $SSH_PASS\n\" > \"$ETC_DIR/plugin.stress-dashboard.ini\"; chown $OWNER:$OWNER \"$ETC_DIR/plugin.stress-dashboard.ini\"'"

# 6. Register in external_postprocessors.json (merge — preserve existing entries).
echo "[6/7] Registering postprocessor (merge)..."
SOCK="/tmp/python-stress-dashboard-postprocessor.sock"
$SCP "$SCRIPT_DIR/deploy_merge.py" "$SSH_USER@$SERVER_IP:/tmp/deploy_merge.py"
$SSH "$SUDO python3 /tmp/deploy_merge.py '$PP_DIR/external_postprocessors.json' '$PP_DIR/postprocessor-python-stress-dashboard' '$SOCK' && $SUDO chown $OWNER:$OWNER '$PP_DIR/external_postprocessors.json'"

# 7. Restart the FULL mediaserver so it reloads external_postprocessors.json and
#    rebuilds the analytics routing manifest. NOTE: `pkill sclblmod` is NOT enough
#    — it only bounces the inference module; the mediaserver is what reads the JSON
#    and decides to route inference to external postprocessors.
echo "[7/7] Restarting mediaserver (loads postprocessor routing config)..."
SVC=networkoptix-mediaserver
[ "$OWNER" = "networkoptix-metavms" ] && SVC=networkoptix-metavms-mediaserver
$SSH "$SUDO service $SVC restart 2>&1; echo restarted" || true
sleep 8

if [ "$START_WEBAPP" = "1" ]; then
    echo ""
    echo "=== Starting web_app.py as root (for true iGPU utilisation) ==="
    $SSH "$SUDO pkill -f 'web_app.py' 2>/dev/null; sleep 1; $SUDO bash -c 'cd \"$PP_DIR\"; nohup python3 web_app.py --port $PORT >/tmp/stress-webapp.out 2>&1 &'; echo started"
fi

echo ""
echo "=== Done ==="
echo "Dashboard: http://$SERVER_IP:$PORT"
if [ "$START_WEBAPP" != "1" ]; then
  echo ""
  echo "Start the web app (run as root for accurate Intel iGPU utilisation):"
  echo "  ssh $SSH_USER@$SERVER_IP"
  echo "  sudo python3 $PP_DIR/web_app.py --port $PORT"
fi
echo ""
echo "REQUIRED manual step (cannot be automated): in Nx AI Manager, open each camera"
echo "you want to benchmark, and SELECT 'Stress Dashboard' in the Postprocessor"
echo "dropdown. External postprocessors are NOT global — they receive inference only"
echo "when explicitly selected per camera. Then view/record the camera so inference"
echo "runs, open the dashboard, and Start a run."
