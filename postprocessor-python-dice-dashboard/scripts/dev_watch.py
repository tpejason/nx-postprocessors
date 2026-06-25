#!/usr/bin/env python3
"""
dev_watch.py — Live development helper for postprocessor-python-dice-dashboard.

Watches the main Python file for changes, syncs it to the VM, and restarts
the postprocessor so changes are visible immediately at http://<SERVER_IP>:8081.

Usage:
    python3 scripts/dev_watch.py

Requirements:
    pip install sshpass   ← or set SSH_PASS env var and have sshpass in PATH
    sshpass must be installed: brew install hudochenkov/sshpass/sshpass

Environment variables (optional overrides):
    VM_HOST      VM hostname/IP   (default: <SERVER_IP>)
    VM_USER      SSH user         (default: parallels)
    VM_PASS      SSH password     (default: <SSH_PASSWORD>)
    DASHBOARD_URL Dashboard URL   (default: http://<SERVER_IP>:8081)
"""

import hashlib
import os
import subprocess
import sys
import time
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────
VM_HOST       = os.environ.get("VM_HOST",  "<SERVER_IP>")
VM_USER       = os.environ.get("VM_USER",  "parallels")
VM_PASS       = os.environ.get("VM_PASS",  "<SSH_PASSWORD>")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", f"http://{VM_HOST}:8081")

_here      = os.path.dirname(os.path.abspath(__file__))
WATCH_FILE = os.path.join(os.path.dirname(_here), "postprocessor-python-dice-dashboard.py")
REMOTE_PATH = (
    "/opt/networkoptix-metavms/mediaserver/var/nx_ai_manager"
    "/nxai_manager/postprocessors/postprocessor-python-dice-dashboard.py"
)

POLL_INTERVAL = 1.0  # seconds between file checks


# ── Helpers ───────────────────────────────────────────────────────────────────

def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ssh(cmd: str, capture=False) -> int:
    full = [
        "sshpass", f"-p{VM_PASS}",
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
        f"{VM_USER}@{VM_HOST}", cmd,
    ]
    if capture:
        r = subprocess.run(full, capture_output=True, text=True)
        return r.returncode, r.stdout.strip()
    return subprocess.run(full).returncode


def _scp(local: str, remote: str) -> int:
    return subprocess.run([
        "sshpass", f"-p{VM_PASS}",
        "scp", "-o", "StrictHostKeyChecking=no",
        local, f"{VM_USER}@{VM_HOST}:{remote}",
    ]).returncode


def _dashboard_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{DASHBOARD_URL}/healthz", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def deploy():
    print(f"\n{'─'*60}")
    print(f"  Change detected — deploying to {VM_HOST}…")
    print(f"{'─'*60}")

    # 1. SCP the file to a temp location
    if _scp(WATCH_FILE, "/tmp/_dice_update.py") != 0:
        print("  ✗ SCP failed")
        return False

    # 2. sudo-move into place + restart mediaserver
    rc = _ssh(
        f"echo {VM_PASS} | sudo -S bash -c "
        f"'cp /tmp/_dice_update.py {REMOTE_PATH} && "
        f"systemctl restart networkoptix-metavms-mediaserver'"
    )
    if rc != 0:
        print("  ✗ Remote install / restart failed")
        return False

    # 3. Wait for dashboard to come back (~15s for mediaserver + model load)
    print("  Waiting for mediaserver restart", end="", flush=True)
    for _ in range(40):
        time.sleep(0.5)
        print(".", end="", flush=True)
        if _dashboard_healthy():
            print(" ✓")
            print(f"  Dashboard live → {DASHBOARD_URL}")
            return True

    print("\n  ✗ Dashboard did not come back within 20s — check VM logs")
    return False


def main():
    if not os.path.exists(WATCH_FILE):
        print(f"ERROR: watch file not found: {WATCH_FILE}")
        sys.exit(1)

    # Quick connectivity check
    rc, _ = _ssh("echo ok", capture=True)
    if rc != 0:
        print(f"ERROR: Cannot reach {VM_USER}@{VM_HOST} via SSH.")
        print("Make sure sshpass is installed: brew install hudochenkov/sshpass/sshpass")
        sys.exit(1)

    print("━" * 60)
    print("  Dice Dashboard — Live Dev Watch")
    print("━" * 60)
    print(f"  Watching : {os.path.basename(WATCH_FILE)}")
    print(f"  VM       : {VM_USER}@{VM_HOST}")
    print(f"  Dashboard: {DASHBOARD_URL}")
    print("  Press Ctrl+C to stop.\n")

    last_md5 = _md5(WATCH_FILE)
    print(f"  Baseline checksum: {last_md5[:8]}…  (ready)")

    while True:
        try:
            time.sleep(POLL_INTERVAL)
            current = _md5(WATCH_FILE)
            if current != last_md5:
                last_md5 = current
                deploy()
        except KeyboardInterrupt:
            print("\n\nStopped.")
            break


if __name__ == "__main__":
    main()
