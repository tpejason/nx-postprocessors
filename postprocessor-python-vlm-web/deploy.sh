#!/usr/bin/env bash
# Deploy VLM Web Post-Processor to NX AI Manager on the Parallels VM
# Usage: ./deploy.sh [user@host]
set -euo pipefail

TARGET="${1:-admin@<VM_IP>}"
REMOTE_DIR="/opt/networkoptix-metavms/mediaserver/bin/plugins/nxai_plugin/nxai_manager/postprocessors"

echo "Deploying to $TARGET:$REMOTE_DIR ..."

ssh "$TARGET" "echo <SSH_PASSWORD> | sudo -S mkdir -p $REMOTE_DIR/postprocessor-python-vlm-web"
ssh "$TARGET" "echo <SSH_PASSWORD> | sudo -S chown -R \$(whoami) $REMOTE_DIR/postprocessor-python-vlm-web"
rsync -avz --exclude '__pycache__' --exclude '*.pyc' \
  ./ "$TARGET:$REMOTE_DIR/postprocessor-python-vlm-web/"

ssh "$TARGET" "
  echo <SSH_PASSWORD> | sudo -S mkdir -p $REMOTE_DIR/../etc
  echo <SSH_PASSWORD> | sudo -S cp $REMOTE_DIR/postprocessor-python-vlm-web/plugin.vlm-web.ini.example \
     $REMOTE_DIR/../etc/plugin.vlm-web.ini 2>/dev/null || true
  echo <SSH_PASSWORD> | sudo -S chmod +x $REMOTE_DIR/postprocessor-python-vlm-web/postprocessor-python-vlm-web.py
  echo <SSH_PASSWORD> | sudo -S chmod +x $REMOTE_DIR/postprocessor-python-vlm-web/vlm_web_app.py
"

echo "Done! On the VM run:"
echo "  python3 vlm_web_app.py &"
echo "  # NX AI Manager will auto-launch the post-processor when enabled in the plugin settings"
