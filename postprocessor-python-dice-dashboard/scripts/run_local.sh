#!/usr/bin/env bash
# run_local.sh — Start the dice dashboard postprocessor with a mock detection feed.
#
# Usage:
#   ./scripts/run_local.sh
#
# Optional environment variables:
#   PORT              Dashboard port (default: 8081)
#   SOCKET_PATH       Unix socket path (default: /tmp/python-dice-dashboard-postprocessor.sock)
#   TRIPLE_PROB       Probability of a Triple roll per frame    (default: 0.15)
#   WRONG_COUNT_PROB  Probability of sending != 3 dice per frame (default: 0.05)
#   FRAME_INTERVAL    Seconds between frames                    (default: 0.5)
#
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROC_DIR="$(dirname "$SCRIPT_DIR")"
SDK_ROOT="$(dirname "$PROC_DIR")"
UTILS_DIR="$SDK_ROOT/nxai-utilities/python-utilities"

PORT="${PORT:-8081}"
SOCKET_PATH="${SOCKET_PATH:-/tmp/python-dice-dashboard-postprocessor.sock}"

export PYTHONPATH="$UTILS_DIR:$PROC_DIR:${PYTHONPATH:-}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Dice Dashboard — Local Development Mode"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Socket  : $SOCKET_PATH"
echo " Port    : $PORT"
echo " Dashboard: http://localhost:$PORT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Remove stale socket from a previous run
rm -f "$SOCKET_PATH"

# ── Start postprocessor ────────────────────────────────────────────────────────
python3 "$PROC_DIR/postprocessor-python-dice-dashboard.py" "$SOCKET_PATH" &
PROC_PID=$!
echo "Postprocessor started (PID $PROC_PID)"

# ── Cleanup on exit ────────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Stopping postprocessor (PID $PROC_PID)…"
    kill "$PROC_PID" 2>/dev/null || true
    rm -f "$SOCKET_PATH"
    exit 0
}
trap cleanup INT TERM

# Wait for socket to appear (up to 10 s)
echo -n "Waiting for socket"
for i in $(seq 1 20); do
    if [ -S "$SOCKET_PATH" ]; then
        echo " ready."
        break
    fi
    echo -n "."
    sleep 0.5
done

if [ ! -S "$SOCKET_PATH" ]; then
    echo ""
    echo "ERROR: Socket $SOCKET_PATH did not appear. Check postprocessor log."
    kill "$PROC_PID" 2>/dev/null || true
    exit 1
fi

# ── Start mock sender ──────────────────────────────────────────────────────────
echo "Starting mock sender (frames every ${FRAME_INTERVAL:-0.5}s)…"
python3 "$SCRIPT_DIR/mock_sender.py" "$SOCKET_PATH"
