#!/bin/bash
# Clean-install the dice / gauge / web-dashboard-advance postprocessors onto an
# Nx AI Manager server, WITHOUT touching the existing stress-dashboard.
#
# Reuses the nxai_communication_utils.py + libnxai-c-utilities-shared.so already
# present in the postprocessors dir (the real ctypes build), so we don't disturb
# whatever stress-dashboard depends on.
#
# Usage: ./deploy_dashboards.sh <server_ip> <ssh_user> <ssh_password>
# NOTE: no `set -e` on purpose — benign non-zero exits (e.g. pkill with no match)
# must not abort the deploy. Each remote command ends defensively with exit 0.

SERVER_IP="${1:?Usage: $0 <server_ip> <ssh_user> <ssh_password>}"
SSH_USER="${2:?}"
SSH_PASS="${3:?}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SSH="sshpass -p $SSH_PASS ssh -o StrictHostKeyChecking=no $SSH_USER@$SERVER_IP"
SCP="sshpass -p $SSH_PASS scp -o StrictHostKeyChecking=no"
SUDO="echo $SSH_PASS | sudo -S -p ''"

echo "=== Deploy dice/gauge/web dashboards -> $SERVER_IP ==="

# 1. Detect Nx install root + service user.
echo "[1/8] Detecting Nx install..."
read NX_ROOT OWNER <<< "$($SSH bash <<'EOF'
if [ -d /opt/networkoptix-metavms ]; then echo "/opt/networkoptix-metavms networkoptix-metavms";
elif [ -d /opt/networkoptix ]; then echo "/opt/networkoptix networkoptix";
else echo "NONE NONE"; fi
EOF
)"
[ "$NX_ROOT" = "NONE" ] && { echo "ERROR: no Nx install on $SERVER_IP"; exit 1; }
PP_DIR="$NX_ROOT/mediaserver/var/nx_ai_manager/nxai_manager/postprocessors"
ETC_DIR="$NX_ROOT/mediaserver/var/nx_ai_manager/nxai_manager/etc"
SVC=networkoptix-mediaserver
[ "$OWNER" = "networkoptix-metavms" ] && SVC=networkoptix-metavms-mediaserver
echo "      root=$NX_ROOT user=$OWNER svc=$SVC"
$SSH "test -f '$PP_DIR/nxai_communication_utils.py' && echo '      OK: shared nxai_communication_utils.py present' || echo '      WARNING: shared comm utils MISSING'"
$SSH "test -f '$PP_DIR/libnxai-c-utilities-shared.so' && echo '      OK: .so present' || echo '      WARNING: .so MISSING (postprocessors will get 0 frames)'"

# 2. Clean any prior install of THESE three (files, sockets, pycache, procs) in a
#    single remote script that always exits 0. Leaves stress-dashboard (incl. its
#    own web_app.py) and the shared comm utils / .so untouched.
echo "[2/8] Cleaning prior dice/gauge/web install (if any)..."
$SSH "$SUDO bash -c 'cd \"$PP_DIR\" 2>/dev/null && rm -f postprocessor-python-dice-dashboard postprocessor-python-dice-dashboard.py postprocessor-python-gauge-dashboard postprocessor-python-gauge-dashboard.py postprocessor-python-web-dashboard-advance postprocessor-python-web-dashboard-advance.py web-dashboard-advance-app.py 2>/dev/null; rm -rf \"$PP_DIR/__pycache__\" 2>/dev/null; pkill -f postprocessor-python-dice-dashboard.py 2>/dev/null; pkill -f postprocessor-python-gauge-dashboard.py 2>/dev/null; pkill -f postprocessor-python-web-dashboard-advance.py 2>/dev/null; pkill -f web-dashboard-advance-app.py 2>/dev/null; rm -f /tmp/python-dice-dashboard-postprocessor.sock /tmp/python-gauge-dashboard-postprocessor.sock /tmp/python-web-dashboard-advance-postprocessor.sock 2>/dev/null; exit 0'"

# 3. Python deps.
echo "[3/8] Installing Python deps (msgpack, Pillow, numpy)..."
$SSH "$SUDO pip3 install --break-system-packages msgpack Pillow numpy 2>&1 | tail -1"

# 4. Upload the unique files for each postprocessor (NOT comm utils / .so).
echo "[4/8] Uploading postprocessor files..."
$SCP "$SCRIPT_DIR/postprocessor-python-dice-dashboard/postprocessor-python-dice-dashboard.py"   "$SSH_USER@$SERVER_IP:/tmp/dice.py"
$SCP "$SCRIPT_DIR/postprocessor-python-gauge-dashboard/postprocessor-python-gauge-dashboard.py" "$SSH_USER@$SERVER_IP:/tmp/gauge.py"
$SCP "$SCRIPT_DIR/postprocessor-python-web-dashboard-advance/postprocessor-python-web-dashboard-advance.py" "$SSH_USER@$SERVER_IP:/tmp/web.py"
# web-dashboard-advance's web server is renamed to a unique filename so it does
# NOT collide with the stress-dashboard's own web_app.py in this shared dir.
$SCP "$SCRIPT_DIR/postprocessor-python-web-dashboard-advance/web_app.py" "$SSH_USER@$SERVER_IP:/tmp/wda_app.py"

$SSH "$SUDO bash -c 'cp /tmp/dice.py \"$PP_DIR/postprocessor-python-dice-dashboard.py\"; cp /tmp/gauge.py \"$PP_DIR/postprocessor-python-gauge-dashboard.py\"; cp /tmp/web.py \"$PP_DIR/postprocessor-python-web-dashboard-advance.py\"; cp /tmp/wda_app.py \"$PP_DIR/web-dashboard-advance-app.py\"; cd \"$PP_DIR\"; chown $OWNER:$OWNER postprocessor-python-dice-dashboard.py postprocessor-python-gauge-dashboard.py postprocessor-python-web-dashboard-advance.py web-dashboard-advance-app.py; chmod 755 postprocessor-python-dice-dashboard.py postprocessor-python-gauge-dashboard.py postprocessor-python-web-dashboard-advance.py web-dashboard-advance-app.py; rm -f /tmp/dice.py /tmp/gauge.py /tmp/web.py /tmp/wda_app.py; exit 0'"

# 5. Bash launcher wrappers (the Command the mediaserver execs).
echo "[5/8] Creating launcher wrappers..."
for base in dice-dashboard gauge-dashboard web-dashboard-advance; do
  WRAP="postprocessor-python-$base"
  $SSH "$SUDO bash -c 'printf \"#!/bin/bash\nexec python3 \\\"\\\$(dirname \\\"\\\$0\\\")/$WRAP.py\\\" \\\"\\\$@\\\"\n\" > \"$PP_DIR/$WRAP\"; chown $OWNER:$OWNER \"$PP_DIR/$WRAP\"; chmod 755 \"$PP_DIR/$WRAP\"'"
done

# 6. Config .ini files (only if absent — don't clobber local edits).
echo "[6/8] Writing config files (if absent)..."
$SSH "$SUDO mkdir -p '$ETC_DIR'"
$SSH "$SUDO bash -c 'test -f \"$ETC_DIR/plugin.dice-dashboard.ini\"  || printf \"[common]\nlog_level = INFO\n\n[detection]\nconfidence_threshold = 0.5\nnms_iou_threshold = 0.5\n\n[dashboard]\nport = 8081\nmax_rolls = 200\ndebounce_frames = 3\n\" > \"$ETC_DIR/plugin.dice-dashboard.ini\"; chown $OWNER:$OWNER \"$ETC_DIR/plugin.dice-dashboard.ini\"'"
$SSH "$SUDO bash -c 'test -f \"$ETC_DIR/plugin.gauge-dashboard.ini\" || printf \"[common]\nlog_level = INFO\n\n[detection]\nconfidence_threshold = 0.5\nnms_iou_threshold = 0.5\n\n[gauge]\nmin_value = 0.0\nmax_value = 100.0\nunit = %%\nalert_low = 20.0\nalert_high = 80.0\n\n[dashboard]\nport = 8082\nhistory_size = 200\ntrend_size = 60\n\" > \"$ETC_DIR/plugin.gauge-dashboard.ini\"; chown $OWNER:$OWNER \"$ETC_DIR/plugin.gauge-dashboard.ini\"'"
$SSH "$SUDO bash -c 'test -f \"$ETC_DIR/plugin.web-dashboard-advance.ini\" || printf \"[common]\nlog_level = INFO\n\n[web_app]\nurl = http://localhost:8112\n\n[web_server]\nport = 8112\ntimeline_capacity = 50000\nscatter_capacity = 5000\n\" > \"$ETC_DIR/plugin.web-dashboard-advance.ini\"; chown $OWNER:$OWNER \"$ETC_DIR/plugin.web-dashboard-advance.ini\"'"

# 7. Register all three in external_postprocessors.json (merge; preserve stress + others).
echo "[7/8] Registering postprocessors (merge)..."
$SCP "$SCRIPT_DIR/deploy_merge_dashboards.py" "$SSH_USER@$SERVER_IP:/tmp/deploy_merge_dashboards.py"
$SSH "$SUDO python3 /tmp/deploy_merge_dashboards.py '$PP_DIR/external_postprocessors.json' '$PP_DIR' && $SUDO chown $OWNER:$OWNER '$PP_DIR/external_postprocessors.json'"
echo "      --- external_postprocessors.json now: ---"
$SSH "cat '$PP_DIR/external_postprocessors.json'"

# 8. Restart mediaserver, then start web_app.py for web-dashboard-advance.
echo "[8/8] Restarting mediaserver + starting web_app.py (port 8112)..."
$SSH "$SUDO service $SVC restart 2>&1; echo restarted"
sleep 8
$SSH "$SUDO bash -c 'cd \"$PP_DIR\"; nohup python3 web-dashboard-advance-app.py --port 8112 >/tmp/web-dashboard-advance.out 2>&1 & exit 0'; echo 'web_app started'"
sleep 2

echo ""
echo "=== Done ==="
echo "Dashboards (after you SELECT each postprocessor per-camera in Nx):"
echo "  Dice  : http://$SERVER_IP:8081"
echo "  Gauge : http://$SERVER_IP:8082"
echo "  Web   : http://$SERVER_IP:8112"
echo ""
echo "REQUIRED manual step: in Nx, open each camera and SELECT the postprocessor"
echo "(Dice / Gauge / Web Dashboard Advance) in the Postprocessor dropdown."
echo "External postprocessors receive inference only when selected per-camera."
