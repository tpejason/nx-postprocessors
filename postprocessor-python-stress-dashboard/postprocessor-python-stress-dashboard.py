#!/usr/bin/env python3
"""
Stress Dashboard Postprocessor
===============================
A transparent (pass-through) postprocessor for the NX AI Manager whose only job
is to measure inference throughput. For every inference result it receives it:

  1. counts the frame against its camera channel (DeviceID),
  2. echoes the message back to the AI Manager *unchanged* (so the pipeline is
     unaffected and it can be chained with any other postprocessor),
  3. flushes per-camera frame counts to the web app once per second over HTTP.

The web app (web_app.py) turns those counts into FPS and pairs them with
whole-machine CPU/RAM/GPU/NPU load to produce a live dashboard and a per-run
stress-test report.

All system-metric collection lives in the web app, NOT here — this stays a
lightweight, config-free, low-overhead counter so it does not perturb the very
performance it is measuring.
"""
import os, sys, logging, logging.handlers, configparser, json, signal, time, threading, urllib.request
import tempfile
from collections import defaultdict

import msgpack

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))
sys.path.append(os.path.join(script_location, "../nxai-utilities/python-utilities"))
sys.path.append(script_location)
import nxai_communication_utils

# ── Paths ─────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(script_location, "..", "etc", "plugin.stress-dashboard.ini")
_etc = os.path.join(script_location, "..", "etc")
LOG_FILE = os.path.join(
    _etc if os.path.exists(_etc) else script_location,
    "plugin.stress-dashboard.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - stress-dashboard - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=3),
    ]
)

Postprocessor_Name = "External - Python-Stress-Dashboard-Postprocessor"
Postprocessor_Socket_Path = os.path.join(
    tempfile.gettempdir(), "python-stress-dashboard-postprocessor.sock"
)

DEFAULT_WEBAPP_URL = "http://localhost:8120"
FLUSH_INTERVAL = 1.0  # seconds between count flushes to the web app

shutdown_event = threading.Event()
logger = None

# ── Frame counting ─────────────────────────────────────────────────────────────
# Guarded by _counter_lock. Reset on every flush.
_counter_lock = threading.Lock()
_frame_counts = defaultdict(int)        # device_id -> frames since last flush
_device_meta = {}                       # device_id -> {"w":, "h":, "name":}


def _record_frame(device_id, width, height, name):
    with _counter_lock:
        _frame_counts[device_id] += 1
        meta = _device_meta.get(device_id)
        if meta is None or meta.get("w") != width or meta.get("h") != height or (name and meta.get("name") != name):
            _device_meta[device_id] = {"w": width, "h": height, "name": name or device_id}


def _drain_counts():
    """Atomically take the accumulated counts and reset the accumulator."""
    with _counter_lock:
        if not _frame_counts:
            return {}, dict(_device_meta)
        counts = dict(_frame_counts)
        _frame_counts.clear()
        return counts, dict(_device_meta)


# ── Web app communication ──────────────────────────────────────────────────────

def post_to_webapp(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url + "/api/fps",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception as e:
        logger.debug("Could not reach web app at %s: %s", url, e)
        return False


def flusher(webapp_url):
    """Background thread: every FLUSH_INTERVAL seconds, post per-camera counts."""
    last = time.monotonic()
    while not shutdown_event.wait(FLUSH_INTERVAL):
        now = time.monotonic()
        interval = now - last
        last = now
        counts, meta = _drain_counts()
        payload = {
            "ts": time.time(),
            "interval": round(interval, 4),
            "frames": counts,                       # device_id -> frame count
            "meta": {d: meta[d] for d in counts} if counts else {},
        }
        # Always post (even empty) so the web app can decay idle cameras to 0 FPS.
        post_to_webapp(webapp_url, payload)
        if counts:
            logger.debug("Flushed counts over %.2fs: %s", interval, counts)


# ── Message field extraction ────────────────────────────────────────────────────

def _device_id(obj):
    return str(obj.get("DeviceID") or obj.get("StreamID") or obj.get("CameraID") or "unknown")


def _device_name(obj):
    name = obj.get("DeviceName") or obj.get("StreamName")
    return str(name) if name else None


# ── Lifecycle ───────────────────────────────────────────────────────────────────

def signal_handler(signum, _):
    logger.info("Signal %s received, shutting down.", signal.Signals(signum).name)
    shutdown_event.set()


def set_log_level(level):
    try:
        lvl = getattr(logging, level.upper())
        logger.setLevel(lvl)
        logging.getLogger().setLevel(lvl)
    except Exception as e:
        logger.error("Log level error: %s", e, exc_info=True)


def config():
    logger.info("Reading config from: %s", CONFIG_FILE)
    webapp_url = DEFAULT_WEBAPP_URL
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        set_log_level(cfg.get("common", "log_level", fallback="INFO"))
        webapp_url = cfg.get("web_app", "url", fallback=DEFAULT_WEBAPP_URL)
    except Exception as e:
        logger.error("Config error: %s", e, exc_info=True)
    return webapp_url


def main(webapp_url):
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    flush_thread = threading.Thread(target=flusher, args=(webapp_url,), daemon=True)
    flush_thread.start()

    if os.path.exists(Postprocessor_Socket_Path):
        try:
            os.unlink(Postprocessor_Socket_Path)
            logger.info("Removed stale socket file: %s", Postprocessor_Socket_Path)
        except OSError as e:
            logger.warning("Could not remove stale socket %s: %s", Postprocessor_Socket_Path, e)

    srv = nxai_communication_utils.SocketListener(Postprocessor_Socket_Path)

    while not shutdown_event.is_set():
        conn = None
        try:
            conn, msg = srv.accept()
        except nxai_communication_utils.SocketTimeout:
            continue
        except nxai_communication_utils.SocketError as e:
            logger.warning("Socket error on accept: %s", e)
            continue
        except Exception as e:
            logger.error("Unexpected error on accept: %s", e, exc_info=True)
            continue

        try:
            # Unpack raw to read only the fields we need; do NOT transform bbox
            # floats so we can echo the exact original bytes back, keeping this
            # postprocessor perfectly transparent in the pipeline.
            try:
                obj = msgpack.unpackb(msg)
            except Exception as e:
                logger.warning("Could not unpack message, skipping: %s", e)
                obj = None

            if isinstance(obj, dict):
                if "EXIT" in obj:
                    logger.info("Exit signal received.")
                    break
                _record_frame(
                    _device_id(obj),
                    int(obj.get("Width", 0) or 0),
                    int(obj.get("Height", 0) or 0),
                    _device_name(obj),
                )

            # Echo the message back unchanged.
            conn.send(msg)
        except Exception as e:
            logger.warning("Error processing message, skipping: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    logger.info("Main loop exited.")


# ── Entry point ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    webapp_url = config()

    logger.info("Stress Dashboard Postprocessor starting")
    if len(sys.argv) > 1:
        Postprocessor_Socket_Path = sys.argv[1]
    logger.info("Socket: %s | Web App: %s", Postprocessor_Socket_Path, webapp_url)

    try:
        main(webapp_url)
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)

    try:
        os.unlink(Postprocessor_Socket_Path)
    except OSError:
        if os.path.exists(Postprocessor_Socket_Path):
            logger.error("Could not remove socket: %s", Postprocessor_Socket_Path)
