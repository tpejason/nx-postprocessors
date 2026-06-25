#!/usr/bin/env python3
"""
Dice Dashboard Postprocessor for NX AI Manager.

Receives dice detection results (class labels "1"–"6"), classifies them as
Big / Small / Triple / Unknown, and serves a live web dashboard at http://HOST:PORT.

Data model:
  - latest:   the most recent parsed frame result (always overwritten).
  - rolls:    ring buffer of confirmed rolls (3-frame debounce, max N entries).
  - category_counts: cumulative Big / Small / Triple tallies.
"""
import base64
import csv
import io
import json
import logging
import msgpack
import os
import random
import signal
import sys
import time
import configparser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image as _PILImage
    _PILLOW = True
except ImportError:
    _PILLOW = False

try:
    import numpy as _np
    _NUMPY = True
except ImportError:
    _NUMPY = False

script_location = os.path.dirname(sys.argv[0])
sys.path.append(os.path.join(script_location, "../nxai-utilities/python-utilities"))
import nxai_communication_utils

# ── Paths ──────────────────────────────────────────────────────────────────────
_etc      = os.path.join(script_location, "..", "etc")
_log_dir  = _etc if os.path.exists(_etc) else script_location
CONFIG_FILE = os.path.join(_etc, "plugin.dice-dashboard.ini")
LOG_FILE    = os.path.join(_log_dir, "plugin.dice-dashboard.log")
CSV_FILE    = os.path.join(_log_dir, "dice-results.csv")
PID_FILE    = os.path.join(_log_dir, "dice-dashboard.pid")

import tempfile
Postprocessor_Socket_Path = os.path.join(
    tempfile.gettempdir(), "python-dice-dashboard-postprocessor.sock"
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - dice-dashboard - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="w"),
    ],
)

DEFAULT_PORT        = 8081
DEFAULT_CONFIDENCE  = 0.5
DEFAULT_NMS_IOU     = 0.5
DEFAULT_MAX_ROLLS   = 200
DEFAULT_DEBOUNCE    = 3

THUMB_MIN_INTERVAL = 1.0   # minimum seconds between thumbnail refreshes
THUMB_MAX_DIM      = 320   # longest edge of the thumbnail (px)

shutdown_event = Event()
web_server     = None
store          = None
logger         = None


# ── Classification ─────────────────────────────────────────────────────────────

def classify(dice_values):
    """
    Return (category, is_triple) given a list of dice face values.

    Rules (D1: always 3 dice):
      - len != 3                     → ("Unknown", False)
      - all three identical          → ("Triple",  True)
      - sum in [3, 9]                → ("Small",   False)
      - sum in [10, 18]              → ("Big",     False)
    """
    if len(dice_values) != 3:
        return "Unknown", False
    if len(set(dice_values)) == 1:
        return "Triple", True
    return ("Small" if sum(dice_values) <= 9 else "Big"), False


# ── NMS ─────────────────────────────────────────────────────────────────────────

def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    ua = max(0.0, a[2]-a[0]) * max(0.0, a[3]-a[1])
    ub = max(0.0, b[2]-b[0]) * max(0.0, b[3]-b[1])
    union = ua + ub - inter
    return inter / union if union > 0 else 0.0


def _nms(detections, iou_threshold):
    """Non-maximum suppression; input/output are lists of detection dicts."""
    if not detections:
        return []
    ordered = sorted(detections, key=lambda d: d["confidence"], reverse=True)
    keep = []
    while ordered:
        best = ordered.pop(0)
        keep.append(best)
        ordered = [d for d in ordered if _iou(best["bbox"], d["bbox"]) <= iou_threshold]
    return keep


# ── Label parsing ───────────────────────────────────────────────────────────────

def _label_to_value(label):
    """
    Map a model class label to a dice face integer 1–6.
    Handles "1"–"6", "dice_1"–"dice_6", "die_N", "pip_N", "face_N".
    Returns None if the label cannot be parsed.
    """
    s = str(label).strip().lower()
    for prefix in ("dice_", "die_", "pip_", "face_", "class_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    try:
        v = int(s)
        return v if 1 <= v <= 6 else None
    except ValueError:
        return None


# ── Detection parsing ───────────────────────────────────────────────────────────

def parse_dice_detections(msg, conf_threshold, nms_iou):
    """
    Extract dice detections from an NX AI Manager inference-results message.

    Returns a list of {"value": int, "confidence": float, "bbox": [x1,y1,x2,y2]}
    sorted left-to-right by x1 (natural reading order).

    Requires ReceiveConfidenceData: true in external_postprocessors.json so that
    ObjectsMetaData.<class>.Confidences is populated; falls back to 1.0 if absent.
    """
    bboxes = msg.get("BBoxes_xyxy", {})
    meta   = msg.get("ObjectsMetaData", {})

    raw = []
    for label, flat in bboxes.items():
        value = _label_to_value(label)
        if value is None:
            continue
        confs = []
        if label in meta and "Confidences" in meta[label]:
            confs = meta[label]["Confidences"]
        n = len(flat) // 4
        for i in range(n):
            x1, y1, x2, y2 = flat[i*4], flat[i*4+1], flat[i*4+2], flat[i*4+3]
            conf = float(confs[i]) if i < len(confs) else 1.0
            if conf < conf_threshold:
                continue
            raw.append({"value": value, "confidence": conf,
                         "bbox": [float(x1), float(y1), float(x2), float(y2)]})

    # Run NMS per dice-value group so adjacent dice with different values
    # whose boxes overlap do not suppress each other.
    groups = {}
    for d in raw:
        groups.setdefault(d["value"], []).append(d)
    kept = []
    for group in groups.values():
        kept.extend(_nms(group, nms_iou))
    kept.sort(key=lambda d: d["bbox"][0])   # left-to-right
    return kept


# ── CSV helpers ─────────────────────────────────────────────────────────────────

def _append_csv_row(result):
    try:
        new_file = not os.path.exists(CSV_FILE)
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["timestamp", "frame_id", "dice_count",
                             "dice_values", "total", "category"])
            w.writerow([
                result["timestamp"],
                result["frame_id"],
                result["dice_count"],
                "+".join(str(v) for v in result["dice_values"]),
                result["total"],
                result["category"],
            ])
    except Exception as e:
        logger.warning("CSV write error: %s", e)


# ── Frame helpers ───────────────────────────────────────────────────────────────

_shm_obj        = None   # reuse SharedMemory across frames (SHMKEY is typically stable)
_shm_key        = None
_last_frame_t   = 0.0   # timestamp of last thumbnail capture


def _read_shm(shmkey, width, height, channels):
    """Return raw bytes from NX shared memory, reusing the handle when key is stable."""
    global _shm_obj, _shm_key
    try:
        if _shm_obj is None or shmkey != _shm_key:
            _shm_obj = nxai_communication_utils.SharedMemory(key=shmkey)
            _shm_key = shmkey
        return _shm_obj.read()
    except Exception as e:
        logger.debug("SHM read error: %s", e)
        return None


def _raw_to_jpeg(data, width, height, channels, quality=65, max_dim=THUMB_MAX_DIM):
    """
    Convert raw tensor bytes (BGR, uint8) to a thumbnail JPEG.
    Resizes so the longest edge <= max_dim before encoding.
    Returns None if Pillow is unavailable or conversion fails.
    """
    if not _PILLOW or not data or width <= 0 or height <= 0:
        return None
    try:
        if _NUMPY and channels == 3:
            arr = _np.frombuffer(data, dtype=_np.uint8).reshape(height, width, 3)
            arr = arr[:, :, ::-1]          # BGR → RGB
            img = _PILImage.fromarray(arr)
        elif channels == 3:
            img = _PILImage.frombytes("RGB", (width, height), data)
        elif channels == 1:
            img = _PILImage.frombytes("L", (width, height), data)
        else:
            return None
        if max_dim and max(img.width, img.height) > max_dim:
            img.thumbnail((max_dim, max_dim), _PILImage.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception as e:
        logger.debug("Frame convert error: %s", e)
        return None


# ── DiceStore ───────────────────────────────────────────────────────────────────

class DiceStore:
    """
    Thread-safe state store for the dice dashboard.

    Warning debounce  — after DEFAULT_DEBOUNCE consecutive non-3-dice frames,
                        warning_active is set to True.
    Roll debounce     — a roll is only committed after DEFAULT_DEBOUNCE
                        consecutive frames showing the same (values, category).
    """

    def __init__(self, max_rolls, debounce, conf_threshold, nms_iou):
        self._lock           = Lock()
        self.start_time      = datetime.now()
        self.max_rolls       = max_rolls
        self.debounce        = debounce
        self.conf_threshold  = conf_threshold
        self.nms_iou         = nms_iou

        self.latest          = None
        self._frame_b64      = None
        self._rolls          = deque(maxlen=max_rolls)
        self.category_counts = {"Big": 0, "Small": 0, "Triple": 0}

        self._non3_streak    = 0
        self.warning_active  = False

        self._roll_key       = None
        self._roll_streak    = 0
        self.frame_count     = 0

    # ── write ──────────────────────────────────────────────────────────────────

    def update(self, msg, frame_id):
        with self._lock:
            self.frame_count += 1
            w = msg.get("Width",  0)
            h = msg.get("Height", 0)

            detections  = parse_dice_detections(msg, self.conf_threshold, self.nms_iou)
            dice_count  = len(detections)
            dice_values = [d["value"] for d in detections]
            total       = sum(dice_values) if dice_count == 3 else None
            category, is_triple = classify(dice_values)

            # Warning debounce
            if dice_count != 3:
                self._non3_streak += 1
                if self._non3_streak >= self.debounce:
                    self.warning_active = True
            else:
                self._non3_streak   = 0
                self.warning_active = False

            result = {
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "frame_id":    frame_id,
                "dice_count":  dice_count,
                "dice_values": dice_values,
                "total":       total,
                "category":    category,
                "is_triple":   is_triple,
                "detections":  detections,
                "warning":     self.warning_active,
                "width":       w,
                "height":      h,
            }
            self.latest = result

            # Roll debounce: commit only after `debounce` identical consecutive frames
            if dice_count == 3:
                key = (tuple(dice_values), category)
                if key == self._roll_key:
                    self._roll_streak += 1
                else:
                    self._roll_key    = key
                    self._roll_streak = 1
                if self._roll_streak == self.debounce:
                    self._rolls.appendleft(dict(result))
                    self.category_counts[category] = \
                        self.category_counts.get(category, 0) + 1
                    _append_csv_row(result)
            else:
                self._roll_key    = None
                self._roll_streak = 0

    def set_frame(self, jpeg_bytes):
        with self._lock:
            self._frame_b64 = (
                base64.b64encode(jpeg_bytes).decode("ascii") if jpeg_bytes else None
            )

    def set_conf_threshold(self, v):
        with self._lock:
            self.conf_threshold = max(0.0, min(1.0, float(v)))

    def clear(self):
        with self._lock:
            self._rolls.clear()
            self.category_counts = {"Big": 0, "Small": 0, "Triple": 0}
            self.frame_count     = 0
            self._roll_key       = None
            self._roll_streak    = 0
            self._non3_streak    = 0
            self.warning_active  = False
            self.latest          = None

    # ── read ───────────────────────────────────────────────────────────────────

    def get_latest(self):
        with self._lock:
            if not self.latest:
                return None
            result = dict(self.latest)
            result["frame_b64"] = self._frame_b64
            return result

    def get_rolls(self, limit=50):
        with self._lock:
            return list(self._rolls)[:limit]

    def get_stats(self):
        with self._lock:
            return {
                "frame_count":     self.frame_count,
                "uptime":          (datetime.now() - self.start_time).total_seconds(),
                "category_counts": dict(self.category_counts),
                "roll_count":      len(self._rolls),
                "conf_threshold":  self.conf_threshold,
            }

    def export_csv_bytes(self):
        with self._lock:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["timestamp", "frame_id", "dice_count",
                         "dice_values", "total", "category"])
            for r in self._rolls:
                w.writerow([
                    r["timestamp"], r["frame_id"], r["dice_count"],
                    "+".join(str(v) for v in r["dice_values"]),
                    r["total"], r["category"],
                ])
            return buf.getvalue().encode("utf-8")


# ── HTTP handler ────────────────────────────────────────────────────────────────

class _ReuseHTTPServer(HTTPServer):
    """HTTPServer with SO_REUSEADDR so rapid restarts don't hit EADDRINUSE."""
    allow_reuse_address = True


class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)
        try:
            limit = int(qs.get("limit", [50])[0])
        except (ValueError, TypeError):
            limit = 50
        limit = max(1, min(1000, limit))
        routes = {
            "/":           lambda: self._html(get_dashboard_html()),
            "/api/latest": lambda: self._json(store.get_latest() or {}),
            "/api/rolls":  lambda: self._json(
                               store.get_rolls(limit)),
            "/api/stats":  lambda: self._json(store.get_stats()),
            "/api/export": lambda: self._csv(store.export_csv_bytes()),
            "/healthz":    lambda: self._json({"ok": True}),
        }
        h = routes.get(p.path)
        if h:
            h()
        else:
            self.send_error(404)

    def do_POST(self):
        p      = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

        if p.path == "/api/clear":
            store.clear()
            self._json({"ok": True})
        elif p.path == "/api/config":
            if "confidence_threshold" in data:
                store.set_conf_threshold(data["confidence_threshold"])
            self._json({"ok": True, "conf_threshold": store.conf_threshold})
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── response helpers ───────────────────────────────────────────────────────

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _html(self, content):
        b = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", len(b))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj):
        b = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type",   "application/json")
        self._cors()
        self.send_header("Content-Length", len(b))
        self.end_headers()
        self.wfile.write(b)

    def _csv(self, data):
        self.send_response(200)
        self.send_header("Content-Type",        "text/csv")
        self.send_header("Content-Disposition", 'attachment; filename="dice-results.csv"')
        self.send_header("Content-Length",      len(data))
        self.end_headers()
        self.wfile.write(data)


# ── Web server lifecycle ────────────────────────────────────────────────────────

def start_web_server(port):
    def _run():
        global web_server
        try:
            web_server = _ReuseHTTPServer(("0.0.0.0", port), DashboardHandler)
            logger.info("Dashboard at http://localhost:%d", port)
            while not shutdown_event.is_set():
                web_server.handle_request()
        except Exception as e:
            logger.error("Web server error: %s", e, exc_info=True)
    Thread(target=_run, daemon=True).start()


def signal_handler(signum, _):
    logger.info("Signal %s received, shutting down.", signal.Signals(signum).name)
    shutdown_event.set()
    if web_server:
        web_server.server_close()   # close socket immediately; don't use shutdown()
                                    # which blocks forever when not using serve_forever()


# ── Config ──────────────────────────────────────────────────────────────────────

def load_config():
    port        = DEFAULT_PORT
    conf        = DEFAULT_CONFIDENCE
    nms         = DEFAULT_NMS_IOU
    maxr        = DEFAULT_MAX_ROLLS
    dbnc        = DEFAULT_DEBOUNCE
    recv_tensor = False
    logger.info("Reading config from: %s", CONFIG_FILE)
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        if "common" in cfg:
            _set_log_level(cfg.get("common", "log_level", fallback="INFO"))
        if "detection" in cfg:
            conf = cfg.getfloat("detection", "confidence_threshold", fallback=DEFAULT_CONFIDENCE)
            nms  = cfg.getfloat("detection", "nms_iou_threshold",    fallback=DEFAULT_NMS_IOU)
        if "dashboard" in cfg:
            port        = cfg.getint    ("dashboard", "port",                 fallback=DEFAULT_PORT)
            maxr        = cfg.getint    ("dashboard", "max_rolls",            fallback=DEFAULT_MAX_ROLLS)
            dbnc        = cfg.getint    ("dashboard", "debounce_frames",      fallback=DEFAULT_DEBOUNCE)
            recv_tensor = cfg.getboolean("dashboard", "receive_input_tensor", fallback=False)
    except Exception as e:
        logger.error("Config error: %s", e, exc_info=True)
    return port, conf, nms, maxr, dbnc, recv_tensor


def _set_log_level(level):
    try:
        logger.setLevel(getattr(logging, level.upper()))
    except Exception as e:
        logger.error("Log level error: %s", e, exc_info=True)


# ── Main loop ───────────────────────────────────────────────────────────────────

def _acquire_single_instance():
    """Kill any previous instance of this postprocessor, then register our PID."""
    my_pid = os.getpid()
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid != my_pid:
                logger.info("Terminating previous instance (PID %d)", old_pid)
                try:
                    os.kill(old_pid, signal.SIGTERM)
                    for _ in range(30):          # wait up to 1.5 s
                        time.sleep(0.05)
                        try:
                            os.kill(old_pid, 0)  # still alive?
                        except ProcessLookupError:
                            break
                    else:
                        logger.warning("PID %d did not exit; sending SIGKILL", old_pid)
                        os.kill(old_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except (ValueError, OSError):
            pass
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(my_pid))
    except OSError as e:
        logger.warning("Could not write PID file: %s", e)


def main(port, receive_input_tensor=False):
    global _last_frame_t
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)
    start_web_server(port)

    srv      = nxai_communication_utils.SocketListener(Postprocessor_Socket_Path)
    frame_id = 0

    while not shutdown_event.is_set():
        logger.debug("Waiting for message")
        try:
            conn, msg = srv.accept()
        except nxai_communication_utils.SocketTimeout:
            continue
        except nxai_communication_utils.SocketError as e:
            logger.warning("Socket error: %s", e)
            continue

        obj = nxai_communication_utils.parseInferenceResults(msg)
        if isinstance(obj, nxai_communication_utils.ExitSignal):
            logger.info("Exit signal received.")
            conn.close()
            break

        frame_id += 1

        # Allow live tuning via NX UI settings
        settings = obj.get("ExternalProcessorSettings", {})
        if "externalprocessor.confidence_threshold" in settings:
            try:
                store.set_conf_threshold(settings["externalprocessor.confidence_threshold"])
            except Exception:
                pass

        # Always drain the image header message from the socket when ReceiveInputTensor
        # is enabled — must happen before conn.send() regardless of whether we use it.
        img_raw = None
        if receive_input_tensor:
            try:
                img_raw = conn.receive()
            except nxai_communication_utils.SocketTimeout:
                logger.debug("Image header not received — check ReceiveInputTensor setting")
            except Exception as e:
                logger.debug("Image header receive error: %s", e)

        # Update store first so we know the confirmed dice count for this frame.
        store.update(obj, frame_id)

        # Capture thumbnail only when exactly 3 dice are confirmed, at most once
        # per THUMB_MIN_INTERVAL seconds (SHM read + PIL only run when needed).
        if img_raw:
            latest = store.get_latest()
            now    = time.time()
            if (latest and latest.get("dice_count") == 3
                    and now - _last_frame_t >= THUMB_MIN_INTERVAL):
                try:
                    hdr      = msgpack.unpackb(img_raw, raw=False)
                    shmkey   = hdr.get("SHMKEY")
                    width    = hdr.get("Width",    obj.get("Width",    0))
                    height   = hdr.get("Height",   obj.get("Height",   0))
                    channels = hdr.get("Channels", 3)
                    if shmkey is not None:
                        raw  = _read_shm(shmkey, width, height, channels)
                        jpeg = _raw_to_jpeg(raw, width, height, channels)
                        if jpeg:
                            store.set_frame(jpeg)
                            _last_frame_t = now
                except Exception as e:
                    logger.debug("Thumbnail capture error: %s", e)

        if frame_id <= 5 or frame_id % 50 == 0:
            bboxes = obj.get("BBoxes_xyxy", {})
            logger.info("Frame %d | classes: %s | latest category: %s",
                        frame_id, list(bboxes.keys()),
                        store.get_latest().get("category") if store.get_latest() else "none")

        conn.send(nxai_communication_utils.writeInferenceResults(obj))
        conn.close()

    logger.info("Main loop exited.")


# ── Dashboard HTML ──────────────────────────────────────────────────────────────

def get_dashboard_html():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dice Dashboard — NX AI Manager</title>
<style>
:root{
  --color-small:#2E7D32;--color-big:#C62828;--color-triple:#FFB300;
  --color-unknown:#757575;--color-warning:#FB8C00;
  --bg:#f0f2f5;--surface:#fff;--text:#1a1a2e;--text2:#666;
  --border:#e0e0e0;--shadow:0 1px 3px rgba(0,0,0,.08);
  --felt:#35654d;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;padding:16px}
.page{max-width:1400px;margin:0 auto}

/* Header */
.header{background:var(--text);color:#fff;border-radius:10px;padding:18px 24px;
  margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:12px}
.header h1{font-size:20px;font-weight:600}
.header p{font-size:13px;color:#aaa;margin-top:3px}
.header-right{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.last-update{font-size:12px;color:#aaa}
.status-badge{display:inline-flex;align-items:center;padding:6px 14px;
  border-radius:20px;font-size:13px;font-weight:600}
.status-badge.online{background:#064e3b;color:#34d399}
.status-badge.offline{background:#374151;color:#9ca3af}

/* Main grid */
.main-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:768px){.main-grid{grid-template-columns:1fr}}

/* Panel */
.panel{background:var(--surface);border-radius:10px;padding:20px;box-shadow:var(--shadow)}
.panel-title{font-size:11px;font-weight:600;color:var(--text2);text-transform:uppercase;
  letter-spacing:.6px;margin-bottom:16px}
.panel-title-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
.panel-title-row .panel-title{margin-bottom:0}

/* Preview — canvas gets the felt background via JS drawPreview() */
#preview-canvas{width:100%;height:auto;border-radius:6px;display:block}
.preview-meta{font-size:12px;color:var(--text2);margin-top:8px;font-variant-numeric:tabular-nums}

/* Warning banner */
.warn-banner{background:var(--color-warning);color:#fff;border-radius:8px;
  padding:16px;display:flex;align-items:flex-start;gap:12px;margin-bottom:16px}
.warn-banner.hidden,.triple-congrats.hidden{display:none}
.warn-icon{font-size:22px;flex-shrink:0;line-height:1.1}
.warn-body p{font-size:14px;font-weight:600;margin-bottom:4px}
.warn-body small{font-size:12px;opacity:.88}

/* Result card */
#result-content.dimmed{opacity:.3;pointer-events:none}
.total-row{display:flex;align-items:baseline;gap:12px;margin-bottom:16px}
.total-label{font-size:13px;color:var(--text2);text-transform:uppercase;letter-spacing:.4px}
.total-value{font-size:72px;font-weight:700;font-variant-numeric:tabular-nums;
  line-height:1;color:var(--text)}

/* Category badge */
.cat-badge{display:inline-flex;align-items:center;gap:8px;padding:10px 22px;
  border-radius:8px;font-size:22px;font-weight:700;letter-spacing:.5px;margin-bottom:20px}
.cat-badge.small{background:var(--color-small);color:#fff}
.cat-badge.big{background:var(--color-big);color:#fff}
.cat-badge.triple{background:var(--color-triple);color:#1a1a2e;
  animation:pulse 1.5s ease-in-out infinite}
.cat-badge.unknown{background:var(--color-unknown);color:#fff}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,179,0,.7)}
  50%{box-shadow:0 0 0 14px rgba(255,179,0,0)}}

/* Dice faces */
.dice-row{display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.dice-face{width:64px;height:64px;border:3px solid var(--border);border-radius:12px;
  display:flex;align-items:center;justify-content:center;font-size:30px;font-weight:700;
  font-variant-numeric:tabular-nums;background:var(--surface);color:var(--text);
  transition:border-color .2s,transform .15s;user-select:none}
.dice-face.active{border-color:var(--text);transform:scale(1.06)}
.dice-face:empty::before{content:"?";color:var(--border)}
.detected-at{font-size:12px;color:var(--text2)}

/* Controls */
.controls-bar{background:var(--surface);border-radius:10px;padding:12px 20px;
  margin-bottom:16px;box-shadow:var(--shadow);display:flex;align-items:center;
  gap:12px;flex-wrap:wrap}
.btn{padding:7px 16px;border:1px solid var(--border);border-radius:6px;
  background:var(--surface);color:var(--text);cursor:pointer;font-size:13px;
  transition:background .15s,opacity .15s;white-space:nowrap}
.btn:hover:not(:disabled){background:var(--bg)}
.btn.primary{background:var(--text);color:#fff;border-color:var(--text)}
.btn.primary:hover:not(:disabled){background:#2d2d4e}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn.sm{padding:4px 10px;font-size:12px}
.conf-ctrl{display:flex;align-items:center;gap:8px;font-size:13px}
.conf-ctrl label{color:var(--text2);white-space:nowrap}
.conf-ctrl input[type=range]{width:110px}
.conf-val{font-weight:600;font-variant-numeric:tabular-nums;min-width:3.2ch}
.cat-pills{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap;align-items:center}
.cat-pill{padding:4px 10px;border-radius:12px;font-size:12px;font-weight:600;white-space:nowrap}
.cat-pill.big{background:#fce4e4;color:var(--color-big)}
.cat-pill.small{background:#e8f5e9;color:var(--color-small)}
.cat-pill.triple{background:#fff8e1;color:#b47e00}

/* ── Mid grid: Tally + Wheel ──────────────────────────────────────────── */
.mid-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:1024px){.mid-grid{grid-template-columns:1fr 1fr}}
@media(max-width:768px){.mid-grid{grid-template-columns:1fr}}

/* Tally chart */
.tally-chart{display:flex;flex-direction:column;gap:14px}
.tally-row{display:flex;align-items:center;gap:10px}
.tally-label{width:52px;font-size:12px;font-weight:700;flex-shrink:0}
.tally-label.lbl-big{color:var(--color-big)}
.tally-label.lbl-small{color:var(--color-small)}
.tally-label.lbl-triple{color:#b47e00}
.tally-track{flex:1;background:#f0f2f5;border-radius:20px;height:22px;
  position:relative;overflow:visible;display:flex;align-items:center}
.tally-bar{height:100%;border-radius:20px;min-width:4px;
  transition:width .4s cubic-bezier(.4,0,.2,1);width:0%}
.tally-bar.bar-big{background:linear-gradient(90deg,#C62828,#ef5350)}
.tally-bar.bar-small{background:linear-gradient(90deg,#2E7D32,#43a047)}
.tally-bar.bar-triple{background:linear-gradient(90deg,#b47e00,#FFB300)}
.tally-count{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums;
  margin-left:8px;color:var(--text);min-width:2ch;flex-shrink:0}

/* Wheel panel */
.wheel-outer{display:flex;flex-direction:column;align-items:center;gap:14px}
.wheel-wrap{position:relative;display:inline-block}
.wheel-arrow{position:absolute;top:-2px;left:50%;transform:translateX(-50%);
  width:0;height:0;
  border-left:10px solid transparent;border-right:10px solid transparent;
  border-top:22px solid #1a1a2e;
  z-index:2;filter:drop-shadow(0 2px 3px rgba(0,0,0,.25))}
#wheel-canvas{display:block;max-width:100%;border-radius:50%}
.spin-btn{font-size:15px;padding:10px 32px;border-radius:24px}
.spin-hint{font-size:12px;color:var(--text2);text-align:center}

/* Triple marquee frame */
.triple-frame{display:inline-block;position:relative;border-radius:11px;
  padding:3px;margin-bottom:20px;overflow:hidden;vertical-align:top}
.triple-frame.marching::before{content:'';position:absolute;
  top:50%;left:50%;width:300%;height:300%;
  transform:translate(-50%,-50%) rotate(0deg);
  background:conic-gradient(#FFD700,#FF8800,#FF3300,#CC00FF,#0088FF,#00FFCC,#FFD700);
  animation:triple-run 0.55s linear infinite;z-index:0}
.triple-frame .cat-badge{position:relative;z-index:1;margin-bottom:0}
@keyframes triple-run{from{transform:translate(-50%,-50%) rotate(0deg)}
  to{transform:translate(-50%,-50%) rotate(360deg)}}

/* Triple congrats message */
.triple-congrats{margin-top:0;margin-bottom:16px;padding:12px 18px;
  background:linear-gradient(135deg,#fff9e0,#fffbe8);
  border:1.5px solid #FFD700;border-radius:8px;
  font-size:13px;font-weight:600;text-align:center;line-height:1.5;color:#7B5800}
.triple-congrats .congrats-word{
  display:inline-block;font-size:17px;font-weight:800;letter-spacing:.5px;
  background:linear-gradient(135deg,#FF8C00,#FFD700,#FF8C00);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;text-shadow:none;margin-right:2px}

/* Neon ring while wheel spins */
@keyframes neon-ring{
  0%  {box-shadow:0 0 0 3px #FF0000,0 0 14px 5px #FF0000}
  14% {box-shadow:0 0 0 3px #FF8800,0 0 14px 5px #FF8800}
  28% {box-shadow:0 0 0 3px #FFD700,0 0 14px 5px #FFD700}
  42% {box-shadow:0 0 0 3px #00FF88,0 0 14px 5px #00FF88}
  57% {box-shadow:0 0 0 3px #0088FF,0 0 14px 5px #0088FF}
  71% {box-shadow:0 0 0 3px #CC00FF,0 0 14px 5px #CC00FF}
  85% {box-shadow:0 0 0 3px #FF00AA,0 0 14px 5px #FF00AA}
  100%{box-shadow:0 0 0 3px #FF0000,0 0 14px 5px #FF0000}}
#wheel-canvas.spinning{animation:neon-ring 0.14s linear infinite}

/* Recent rolls */
.rolls-panel{margin-bottom:0}
.rolls-wrap{overflow-y:auto;max-height:380px}
table{width:100%;border-collapse:collapse}
thead th{position:sticky;top:0;background:var(--surface);border-bottom:2px solid var(--border);
  text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;
  color:var(--text2);padding:8px 12px;font-weight:600}
tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
tbody tr:hover{background:var(--bg)}
tbody td{padding:10px 12px;font-size:13px;vertical-align:middle}
.td-dice{font-variant-numeric:tabular-nums;letter-spacing:1px}
.td-total{font-weight:700;font-variant-numeric:tabular-nums}
.result-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;
  border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap}
.result-pill.Big{background:#fce4e4;color:var(--color-big)}
.result-pill.Small{background:#e8f5e9;color:var(--color-small)}
.result-pill.Triple{background:#fff8e1;color:#b47e00;font-weight:700}
.result-pill.Unknown{background:#f5f5f5;color:var(--color-unknown)}
.empty-state{text-align:center;padding:32px;color:var(--text2);font-size:14px}

/* MODE 1 CTA */
.mode1-cta{display:inline-block;margin-top:14px;padding:7px 18px;
  border-radius:20px;font-size:13px;font-weight:700;letter-spacing:.6px;
  background:linear-gradient(135deg,#fff8e1,#fff3cd);
  border:1.5px solid #FFB300;color:#7B5800;
  animation:cta-pulse 2.2s ease-in-out infinite}
.mode1-cta.hidden{display:none}
@keyframes cta-pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,179,0,.45)}
  50%{box-shadow:0 0 0 8px rgba(255,179,0,0)}}

/* Mode toggle */
.mode-toggle{display:flex;align-items:center;gap:6px;background:rgba(255,255,255,0.08);
  border-radius:8px;padding:4px}
.mode-btn{padding:5px 14px;border-radius:6px;border:none;cursor:pointer;
  font-size:12px;font-weight:700;letter-spacing:.4px;transition:background .18s,color .18s;
  background:transparent;color:rgba(255,255,255,0.55)}
.mode-btn.active{background:#fff;color:#1a1a2e}

/* Mode 2 manual input */
.mode2-panel{display:none;margin-top:16px;padding:14px 16px;
  background:#f8f9fa;border:1.5px dashed var(--border);border-radius:10px}
.mode2-panel.visible{display:block}
.mode2-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
  color:var(--text2);margin-bottom:8px}
.mode2-select-row{display:flex;align-items:center;gap:10px}
.mode2-select{flex:1;padding:8px 12px;border:1.5px solid var(--border);border-radius:8px;
  font-size:14px;font-weight:600;background:var(--surface);color:var(--text);
  cursor:pointer;appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23888' stroke-width='1.8' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 10px center;padding-right:30px}
.mode2-select:focus{outline:none;border-color:var(--text)}
.mode2-select:disabled{opacity:.45;cursor:not-allowed;background-color:#f0f2f5}
.mode2-desc{font-size:12px;margin-top:7px;min-height:16px;font-weight:700;letter-spacing:.3px;color:#555}
.mode2-desc.triple{color:#b47e00;font-size:13px}
.mode2-desc.big{color:#C62828}
.mode2-desc.small{color:#2E7D32}
.mode2-lock-hint{font-size:11px;margin-top:6px;min-height:16px;font-weight:600}
.mode2-lock-hint.locked{color:#B71C1C}
.mode2-lock-hint.open{color:#B71C1C}
.mode2-result-badge{display:none;padding:8px 18px;border-radius:8px;font-size:15px;
  font-weight:700;letter-spacing:.3px;white-space:nowrap}
.mode2-result-badge.big{display:inline-flex;background:var(--color-big);color:#fff}
.mode2-result-badge.small{display:inline-flex;background:var(--color-small);color:#fff}
.mode2-result-badge.triple{display:inline-flex;background:var(--color-triple);color:#1a1a2e}

/* Mode 2 WIN / LOSE verdict */
.mode2-verdict{display:none;margin-top:14px;padding:14px 12px;border-radius:10px;
  text-align:center;font-size:22px;font-weight:900;letter-spacing:.5px}
.mode2-verdict.show{display:block}
.mode2-verdict.win{background:linear-gradient(135deg,#e8f5e9,#c8e6c9);
  color:#1B5E20;border:2px solid #43a047;animation:verdict-pop .35s cubic-bezier(.34,1.56,.64,1)}
.mode2-verdict.lose{background:linear-gradient(135deg,#fce4e4,#ffcdd2);
  color:#B71C1C;border:2px solid #ef5350;animation:verdict-pop .35s cubic-bezier(.34,1.56,.64,1)}
.mode2-verdict .verdict-sub{display:block;font-size:12px;font-weight:500;
  margin-top:4px;opacity:.75}
@keyframes verdict-pop{from{transform:scale(0.75);opacity:0}to{transform:scale(1);opacity:1}}
</style>
</head>
<body>
<div class="page">

<!-- Header -->
<div class="header">
  <div>
    <h1>&#127922; Dice Dashboard</h1>
    <p>Real-time dice detection &middot; NX AI Manager</p>
  </div>
  <div class="header-right">
    <div class="mode-toggle">
      <button class="mode-btn active" id="mode-btn-1" onclick="setMode(1)">MODE 1</button>
      <button class="mode-btn"        id="mode-btn-2" onclick="setMode(2)">MODE 2</button>
    </div>
    <span id="status" class="status-badge online">&#9679; Online</span>
    <span class="last-update" id="last-update">Updating&hellip;</span>
  </div>
</div>

<!-- Two-column: Preview + Result -->
<div class="main-grid">

  <!-- Left: Live Preview -->
  <div class="panel">
    <div class="panel-title">Live Preview</div>
    <canvas id="preview-canvas" width="640" height="480"></canvas>
    <div class="preview-meta" id="preview-meta">Waiting for detections&hellip;</div>
  </div>

  <!-- Right: Result Card -->
  <div class="panel">
    <div class="panel-title">Current Result</div>

    <div id="warn-banner" class="warn-banner hidden">
      <span class="warn-icon">&#9888;</span>
      <div class="warn-body">
        <p>Please ensure exactly 3 dice are visible in the frame.</p>
        <small>Detected: <span id="warn-count">0</span> dice &mdash;
          Total and Result are hidden until 3 dice are detected.</small>
      </div>
    </div>

    <div id="result-content">
      <div class="total-row">
        <span class="total-label">Total</span>
        <span class="total-value" id="total-value">&mdash;</span>
      </div>
      <div id="triple-frame" class="triple-frame">
        <div id="cat-badge" class="cat-badge unknown">&mdash; UNKNOWN</div>
      </div>
      <div id="triple-congrats" class="triple-congrats hidden">
        <span class="congrats-word">Congratulations</span>, please spin lucky wheel once.
      </div>
      <div class="dice-row">
        <div class="dice-face" id="die-0"></div>
        <div class="dice-face" id="die-1"></div>
        <div class="dice-face" id="die-2"></div>
      </div>
      <div class="detected-at" id="detected-at">No data yet</div>

      <!-- MODE 1: CTA -->
      <div id="mode1-cta" class="mode1-cta">&#9733; Try a TRIPLE</div>

      <!-- MODE 2: manual input -->
      <div id="mode2-panel" class="mode2-panel">
        <div class="mode2-label">Manual Selection</div>
        <div class="mode2-select-row">
          <select class="mode2-select" id="mode2-select" onchange="onMode2Select(this.value)">
            <option value="">— Select result —</option>
            <option value="Big">Big</option>
            <option value="Small">Small</option>
            <option value="Triple">Triple</option>
          </select>
          <span id="mode2-badge" class="mode2-result-badge"></span>
        </div>
        <div id="mode2-desc" class="mode2-desc"></div>
        <div id="mode2-lock-hint" class="mode2-lock-hint"></div>
        <div id="mode2-verdict" class="mode2-verdict"></div>
      </div>
    </div>
  </div>
</div>

<!-- Controls -->
<div class="controls-bar">
  <button class="btn primary" onclick="clearData()">Clear History</button>
  <button class="btn" onclick="exportCSV()">Export CSV</button>
  <div class="conf-ctrl">
    <label for="conf-slider">Confidence:</label>
    <input type="range" id="conf-slider" min="0" max="1" step="0.05" value="0.5"
           oninput="onConfChange(this.value)">
    <span class="conf-val" id="conf-val">0.50</span>
  </div>
  <div class="cat-pills">
    <span class="cat-pill big"    id="cnt-big">Big: 0</span>
    <span class="cat-pill small"  id="cnt-small">Small: 0</span>
    <span class="cat-pill triple" id="cnt-triple">Triple: 0</span>
  </div>
</div>

<!-- Mid: Tally Chart + Spin Wheel -->
<div class="mid-grid">

  <!-- Tally bar chart -->
  <div class="panel">
    <div class="panel-title-row">
      <span class="panel-title">Cumulative Tally</span>
      <button class="btn sm" onclick="clearChart()">Clear Chart</button>
    </div>
    <div class="tally-chart">
      <div class="tally-row">
        <span class="tally-label lbl-big">Big</span>
        <div class="tally-track">
          <div class="tally-bar bar-big" id="bar-big"></div>
        </div>
        <span class="tally-count" id="bar-count-big">0</span>
      </div>
      <div class="tally-row">
        <span class="tally-label lbl-small">Small</span>
        <div class="tally-track">
          <div class="tally-bar bar-small" id="bar-small"></div>
        </div>
        <span class="tally-count" id="bar-count-small">0</span>
      </div>
      <div class="tally-row">
        <span class="tally-label lbl-triple">Triple</span>
        <div class="tally-track">
          <div class="tally-bar bar-triple" id="bar-triple"></div>
        </div>
        <span class="tally-count" id="bar-count-triple">0</span>
      </div>
    </div>
  </div>

  <!-- Spin wheel -->
  <div class="panel">
    <div class="panel-title">Prize Wheel</div>
    <div class="wheel-outer">
      <div class="wheel-wrap">
        <div class="wheel-arrow"></div>
        <canvas id="wheel-canvas" width="320" height="320"></canvas>
      </div>
      <button id="spin-btn" class="btn primary spin-btn" onclick="spinWheel()" disabled>
        &#127922; Spin
      </button>
      <div class="spin-hint" id="spin-hint">Roll Triple to enable spin</div>
    </div>
  </div>

</div>

<!-- Recent Rolls (last 10) -->
<div class="panel rolls-panel">
  <div class="panel-title">Recent Rolls</div>
  <div class="rolls-wrap">
    <table>
      <thead>
        <tr><th>Time</th><th>Dice</th><th>Total</th><th>Result</th></tr>
      </thead>
      <tbody id="rolls-tbody">
        <tr><td colspan="4" class="empty-state">No rolls recorded yet</td></tr>
      </tbody>
    </table>
  </div>
</div>

</div><!-- .page -->
<script>
// ── Constants ──────────────────────────────────────────────────────────────────
const ICONS = {Big:'&#9650;', Small:'&#9660;', Triple:'&#9733;', Unknown:'&mdash;'};
const CLS   = {Big:'big', Small:'small', Triple:'triple', Unknown:'unknown'};

// ── Canvas preview ─────────────────────────────────────────────────────────────
const canvas = document.getElementById('preview-canvas');
const ctx    = canvas.getContext('2d');

const PIPS = {
  1: [[.50,.50]],
  2: [[.25,.25],[.75,.75]],
  3: [[.25,.25],[.50,.50],[.75,.75]],
  4: [[.25,.25],[.75,.25],[.25,.75],[.75,.75]],
  5: [[.25,.25],[.75,.25],[.50,.50],[.25,.75],[.75,.75]],
  6: [[.25,.18],[.75,.18],[.25,.50],[.75,.50],[.25,.82],[.75,.82]]
};
const CAT_COLOR = {Big:'#C62828', Small:'#2E7D32', Triple:'#FFB300', Unknown:'#9e9e9e'};

function _rrect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x+r, y);
  ctx.lineTo(x+w-r, y);  ctx.arc(x+w-r, y+r,   r, -Math.PI/2, 0);
  ctx.lineTo(x+w, y+h-r); ctx.arc(x+w-r, y+h-r, r,  0,         Math.PI/2);
  ctx.lineTo(x+r, y+h);   ctx.arc(x+r,   y+h-r, r,  Math.PI/2, Math.PI);
  ctx.lineTo(x, y+r);     ctx.arc(x+r,   y+r,   r,  Math.PI,  -Math.PI/2);
  ctx.closePath();
}

function drawDiceFace(x, y, sz, value, col) {
  const r   = sz * 0.14;
  const lw  = Math.max(2, sz * 0.05);
  const pip = sz * 0.085;
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,.22)'; ctx.shadowBlur = sz*.12; ctx.shadowOffsetY = sz*.04;
  ctx.fillStyle = '#fff';
  _rrect(x, y, sz, sz, r); ctx.fill();
  ctx.restore();
  ctx.strokeStyle = col; ctx.lineWidth = lw;
  _rrect(x, y, sz, sz, r); ctx.stroke();
  ctx.fillStyle = '#1a1a2e';
  for (const [px, py] of (PIPS[value] || [])) {
    ctx.beginPath(); ctx.arc(x+px*sz, y+py*sz, pip, 0, Math.PI*2); ctx.fill();
  }
}

function drawFelt(W, H) {
  // Base felt green
  ctx.fillStyle = '#35654d';
  ctx.fillRect(0, 0, W, H);
  // Fine halftone dot texture
  ctx.fillStyle = 'rgba(0,0,0,0.055)';
  for (let y = 2; y < H; y += 4) {
    for (let x = (Math.floor(y/4) % 2 === 0 ? 2 : 0); x < W; x += 4) {
      ctx.fillRect(x, y, 1, 1);
    }
  }
  // Subtle radial vignette
  const grad = ctx.createRadialGradient(W/2, H/2, Math.min(W,H)*.25,
                                         W/2, H/2, Math.max(W,H)*.7);
  grad.addColorStop(0, 'rgba(255,255,255,0.03)');
  grad.addColorStop(1, 'rgba(0,0,0,0.18)');
  ctx.fillStyle = grad; ctx.fillRect(0, 0, W, H);
  // Corner arc ornaments
  const orn = Math.min(W, H) * 0.07;
  ctx.strokeStyle = 'rgba(255,255,255,0.13)'; ctx.lineWidth = 1.5;
  for (const [cx2, cy2, sa, ea] of [
    [orn*1.4, orn*1.4, Math.PI, -Math.PI/2],
    [W-orn*1.4, orn*1.4, -Math.PI/2, 0],
    [W-orn*1.4, H-orn*1.4, 0, Math.PI/2],
    [orn*1.4, H-orn*1.4, Math.PI/2, Math.PI],
  ]) {
    ctx.beginPath(); ctx.arc(cx2, cy2, orn, sa, ea); ctx.stroke();
  }
}

function drawPreview(d) {
  const W = d.width  || 640;
  const H = d.height || 480;
  canvas.width  = W;
  canvas.height = H;
  drawFelt(W, H);

  // ── Zone labels (background layer — semi-transparent) ──────────
  const lblSz = Math.max(12, Math.round(W * 0.042));
  ctx.font = `700 ${lblSz}px sans-serif`;
  ctx.textAlign = 'center';
  ctx.fillStyle = 'rgba(255,255,255,0.18)';
  ctx.fillText('BIG',   W / 4,     H - lblSz * 0.8);
  ctx.fillText('SMALL', W * 3 / 4, H - lblSz * 0.8);

  // ── Center divider ─────────────────────────────────────────────
  ctx.save();
  ctx.strokeStyle = 'rgba(255,255,255,0.5)';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([9, 7]);
  ctx.beginPath();
  ctx.moveTo(W / 2, 8); ctx.lineTo(W / 2, H - 8);
  ctx.stroke();
  ctx.restore();
  // ──────────────────────────────────────────────────────────────

  if (!d.detections || !d.detections.length) {
    ctx.fillStyle = 'rgba(255,255,255,0.65)';
    ctx.font = `${Math.round(W*.028)}px sans-serif`;
    ctx.textAlign = 'center';
    ctx.fillText('Waiting for dice…', W/2, H/2);
    // Border frame (no-detection state)
    ctx.strokeStyle = 'rgba(255,255,255,0.75)';
    ctx.lineWidth = 3;
    ctx.setLineDash([]);
    ctx.strokeRect(3, 3, W - 6, H - 6);
    return;
  }
  const col = CAT_COLOR[d.category] || CAT_COLOR.Unknown;
  for (const det of d.detections) {
    const [x1, y1, x2, y2] = det.bbox;
    const sz = Math.min(x2-x1, y2-y1) * 0.88;
    const dcx = (x1+x2)/2, dcy = (y1+y2)/2;
    drawDiceFace(dcx - sz/2, dcy - sz/2, sz, det.value, col);
  }
  // ── White border frame drawn last (on top of dice) ─────────────
  ctx.strokeStyle = 'rgba(255,255,255,0.75)';
  ctx.lineWidth = 3;
  ctx.setLineDash([]);
  ctx.strokeRect(3, 3, W - 6, H - 6);
}

// ── Tally chart ────────────────────────────────────────────────────────────────
let chartBaseline = {Big:0, Small:0, Triple:0};
let serverCounts  = {Big:0, Small:0, Triple:0};

function renderBarChart(counts) {
  const max = Math.max(1, counts.Big, counts.Small, counts.Triple);
  for (const [key, id] of [['Big','big'],['Small','small'],['Triple','triple']]) {
    const pct = Math.round((counts[key] / max) * 100);
    document.getElementById('bar-' + id).style.width = pct + '%';
    document.getElementById('bar-count-' + id).textContent = counts[key];
  }
}

function getChartCounts() {
  return {
    Big:    Math.max(0, serverCounts.Big    - chartBaseline.Big),
    Small:  Math.max(0, serverCounts.Small  - chartBaseline.Small),
    Triple: Math.max(0, serverCounts.Triple - chartBaseline.Triple),
  };
}

function clearChart() {
  chartBaseline = Object.assign({}, serverCounts);
  renderBarChart({Big:0, Small:0, Triple:0});
}

// ── Triple flash ───────────────────────────────────────────────────────────────
let _flashTimer = null;
let _flashStep  = 0;
let _lastCat    = null;

function tripleFlash() {
  if (_flashTimer) { clearTimeout(_flashTimer); }
  const badge = document.getElementById('cat-badge');
  _flashStep = 0;
  badge.style.animationPlayState = 'paused';

  function step() {
    if (_flashStep >= 10) {
      badge.style.background = '';
      badge.style.color = '';
      badge.style.animationPlayState = '';
      _flashTimer = null;
      return;
    }
    if (_flashStep % 2 === 0) {
      badge.style.background = '#fff5c2';
      badge.style.color = '#1a1a2e';
    } else {
      badge.style.background = 'var(--color-triple)';
      badge.style.color = '#1a1a2e';
    }
    _flashStep++;
    _flashTimer = setTimeout(step, 200);
  }
  step();
}

// ── Spin wheel ─────────────────────────────────────────────────────────────────
// Layout: Legendary×1, Golden×2, Lucky×7  (10 total)
// Index:  0=Legendary, 1=Golden, 2=Lucky, 3=Lucky, 4=Golden,
//         5=Lucky, 6=Lucky, 7=Lucky, 8=Lucky, 9=Lucky
// label: 'nx' = Nx brand text, 'emoji' = emoji char in `icon`
const WHEEL_SLICES = [
  {label:'nx',    color:'#6A0DAD', text:'#FFD700', icon:null},
  {label:'emoji', color:'#D4840A', text:'#fff5cc', icon:'😊'},
  {label:'emoji', color:'#C62828', text:'#fff',    icon:'💩'},
  {label:'emoji', color:'#1565C0', text:'#fff',    icon:'💩'},
  {label:'emoji', color:'#D4840A', text:'#fff5cc', icon:'😊'},
  {label:'emoji', color:'#00695C', text:'#fff',    icon:'💩'},
  {label:'emoji', color:'#4527A0', text:'#fff',    icon:'💩'},
  {label:'emoji', color:'#AD1457', text:'#fff',    icon:'💩'},
  {label:'emoji', color:'#558B2F', text:'#fff',    icon:'💩'},
  {label:'emoji', color:'#00838F', text:'#fff',    icon:'💩'},
];
let wheelAngle  = 0;
let isSpinning  = false;
let wheelWinner = -1;

function drawWheel(angle, highlight) {
  const wc = document.getElementById('wheel-canvas');
  if (!wc) return;
  const wctx = wc.getContext('2d');
  const cx   = wc.width / 2, cy = wc.height / 2;
  const R    = Math.min(cx, cy) - 4;
  const RIM  = R * 0.10;   // outer rim thickness
  const HUB  = R * 0.165;  // center hub radius
  const n    = WHEEL_SLICES.length;
  const step = (2 * Math.PI) / n;

  wctx.clearRect(0, 0, wc.width, wc.height);

  // ── Outer rim (dark, thick — like the reference) ──────────────
  wctx.beginPath();
  wctx.arc(cx, cy, R + 6, 0, 2 * Math.PI);
  wctx.fillStyle = '#111';
  wctx.fill();
  // Rim bevel highlight
  const rimG = wctx.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
  rimG.addColorStop(0, 'rgba(255,255,255,0.18)');
  rimG.addColorStop(0.5, 'rgba(0,0,0,0.0)');
  rimG.addColorStop(1, 'rgba(0,0,0,0.35)');
  wctx.beginPath();
  wctx.arc(cx, cy, R + 6, 0, 2 * Math.PI);
  wctx.fillStyle = rimG;
  wctx.fill();

  // ── Slices ─────────────────────────────────────────────────────
  for (let i = 0; i < n; i++) {
    const sa   = angle + i * step - Math.PI / 2;
    const ea   = sa + step;
    const s    = WHEEL_SLICES[i];
    const isHi = (highlight === i);

    // Slice path
    wctx.beginPath();
    wctx.moveTo(cx, cy);
    wctx.arc(cx, cy, R - RIM * 0.5, sa, ea);
    wctx.closePath();

    // Radial gradient per slice (lighter near outside edge)
    const mid = sa + step / 2;
    const slG = wctx.createRadialGradient(cx, cy, 0, cx, cy, R);
    const hiColor = lightenHex(s.color, 55);
    slG.addColorStop(0, isHi ? hiColor : lightenHex(s.color, 30));
    slG.addColorStop(1, isHi ? hiColor : s.color);
    wctx.fillStyle = slG;
    wctx.fill();

    // Thick black spoke border
    wctx.strokeStyle = '#111';
    wctx.lineWidth = isHi ? 2 : 3;
    wctx.stroke();

    // Winner glow
    if (isHi) {
      wctx.save();
      wctx.shadowColor = '#fff'; wctx.shadowBlur = 18;
      wctx.strokeStyle = '#fff'; wctx.lineWidth = 2.5;
      wctx.stroke();
      wctx.restore();
    }

    // ── Label / icon ──────────────────────────────────────────────
    const tx = cx + (R - RIM) * 0.60 * Math.cos(mid);
    const ty = cy + (R - RIM) * 0.60 * Math.sin(mid);
    wctx.save();
    wctx.translate(tx, ty);
    wctx.rotate(mid + Math.PI / 2);
    wctx.textAlign = 'center';
    wctx.textBaseline = 'middle';

    if (s.label === 'nx') {
      // ── Nx brand logo — two-layer text ───────────────────────
      const nxFs = Math.max(11, Math.round((R - RIM) * 0.155));
      wctx.shadowColor = 'rgba(0,0,0,0.85)';
      wctx.shadowBlur = 4;
      // Outline pass (dark)
      wctx.strokeStyle = '#1a0030';
      wctx.lineWidth = 3;
      wctx.font = `900 ${nxFs}px "Arial Black", sans-serif`;
      wctx.strokeText('Nx', 0, 0);
      // Fill pass (brand cyan over purple)
      wctx.fillStyle = isHi ? '#fff' : '#00D4FF';
      wctx.fillText('Nx', 0, 0);
    } else if (s.label === 'emoji') {
      // ── Emoji icon ───────────────────────────────────────────
      const emFs = Math.max(13, Math.round((R - RIM) * 0.175));
      wctx.shadowColor = 'rgba(0,0,0,0.5)';
      wctx.shadowBlur = 2;
      wctx.font = `${emFs}px sans-serif`;
      wctx.fillStyle = 'white'; // required but emoji ignores this
      wctx.fillText(s.icon, 0, 0);
    }
    wctx.restore();
  }

  // ── Rim inner edge line ────────────────────────────────────────
  wctx.beginPath();
  wctx.arc(cx, cy, R - RIM * 0.5, 0, 2 * Math.PI);
  wctx.strokeStyle = '#111';
  wctx.lineWidth = 3;
  wctx.stroke();

  // ── Center hub ────────────────────────────────────────────────
  // Shadow ring
  wctx.beginPath();
  wctx.arc(cx, cy, HUB + 5, 0, 2 * Math.PI);
  wctx.fillStyle = '#111';
  wctx.fill();
  // Hub body gradient (dark red, like reference)
  const hubG = wctx.createRadialGradient(cx - HUB * 0.3, cy - HUB * 0.3, 0, cx, cy, HUB);
  hubG.addColorStop(0, '#c0392b');
  hubG.addColorStop(0.6, '#7B0000');
  hubG.addColorStop(1, '#3d0000');
  wctx.beginPath();
  wctx.arc(cx, cy, HUB, 0, 2 * Math.PI);
  wctx.fillStyle = hubG;
  wctx.fill();
  // Hub text
  wctx.fillStyle = '#FFD700';
  wctx.shadowColor = 'rgba(0,0,0,0.7)';
  wctx.shadowBlur = 3;
  wctx.font = `bold ${Math.round(HUB * 0.52)}px sans-serif`;
  wctx.textAlign = 'center';
  wctx.textBaseline = 'middle';
  wctx.fillText('SPIN', cx, cy - HUB * 0.1);
  wctx.font = `bold ${Math.round(HUB * 0.38)}px sans-serif`;
  wctx.fillText('& WIN', cx, cy + HUB * 0.42);
  wctx.shadowBlur = 0;
}

// Lighten a hex color by `amount` (0–255 per channel)
function lightenHex(hex, amount) {
  const n = parseInt(hex.replace('#',''), 16);
  const r = Math.min(255, (n >> 16) + amount);
  const g = Math.min(255, ((n >> 8) & 0xff) + amount);
  const b = Math.min(255, (n & 0xff) + amount);
  return `rgb(${r},${g},${b})`;
}

function easeOut(t) { return 1 - Math.pow(1-t, 4); }

function spinWheel() {
  if (isSpinning) return;
  // Weighted random: Legendary 10%, Golden 20% (slices 1&4), Lucky 70%
  const r = Math.random();
  let target;
  if (r < 0.10)      target = 0;                               // Legendary
  else if (r < 0.30) target = (Math.random() < 0.5) ? 1 : 4;  // Golden
  else               target = [2,3,5,6,7,8,9][Math.floor(Math.random()*7)]; // Lucky

  wheelWinner = target;
  const n    = WHEEL_SLICES.length;
  const step = (2*Math.PI) / n;
  // Bring slice `target`'s center to the top (arrow at -π/2)
  const desiredEnd = -(target + 0.5)*step;
  // Add enough full rotations (current + at least 6 turns)
  const extraTurns = Math.floor((wheelAngle - desiredEnd) / (2*Math.PI)) + 6 + Math.floor(Math.random()*3);
  const finalAngle = desiredEnd + extraTurns * 2 * Math.PI;

  const startAngle = wheelAngle;
  const totalDelta = finalAngle - startAngle;
  const duration   = 3800 + Math.random()*600;
  const startTime  = performance.now();

  isSpinning = true;
  document.getElementById('spin-btn').disabled = true;
  document.getElementById('spin-hint').textContent = 'Spinning…';
  const wc = document.getElementById('wheel-canvas');
  if (wc) wc.classList.add('spinning');

  function animate(now) {
    const t = Math.min(1, (now - startTime) / duration);
    wheelAngle = startAngle + totalDelta * easeOut(t);
    drawWheel(wheelAngle, t >= 1 ? wheelWinner : -1);
    if (t < 1) {
      requestAnimationFrame(animate);
    } else {
      isSpinning = false;
      if (wc) wc.classList.remove('spinning');
      const winSlice = WHEEL_SLICES[wheelWinner];
      const winName = winSlice.label === 'nx' ? 'Legendary — Nx!' :
                      winSlice.icon === '😊'   ? 'Golden 😊!'      : 'Lucky 💩!';
      document.getElementById('spin-hint').textContent = winName;
    }
  }
  requestAnimationFrame(animate);
}

// ── Mode toggle ────────────────────────────────────────────────────────────────
let _currentMode    = 1;
let _mode2Selected  = '';   // value the user picked in mode2
let _mode2Winner    = null; // true=win, false=lose, null=undecided

function setMode(m) {
  _currentMode = m;
  document.getElementById('mode-btn-1').classList.toggle('active', m === 1);
  document.getElementById('mode-btn-2').classList.toggle('active', m === 2);
  const panel = document.getElementById('mode2-panel');
  const cta = document.getElementById('mode1-cta');
  if (m === 2) {
    panel.classList.add('visible');
    if (cta) cta.classList.add('hidden');
    // Remove dimmed so dropdown is always interactive in MODE 2
    const rc = document.getElementById('result-content');
    if (rc) rc.classList.remove('dimmed');
    checkMode2Verdict();
  } else {
    panel.classList.remove('visible');
    if (cta) cta.classList.remove('hidden');
    _mode2Selected = '';
    _mode2Winner   = null;
    document.getElementById('mode2-select').value = '';
    document.getElementById('mode2-badge').className = 'mode2-result-badge';
    document.getElementById('mode2-badge').textContent = '';
    _resetMode2Verdict();
  }
}

function onMode2Select(val) {
  _mode2Selected = val;
  const badge = document.getElementById('mode2-badge');
  const desc  = document.getElementById('mode2-desc');
  if (!val) {
    badge.className = 'mode2-result-badge';
    badge.textContent = '';
    if (desc) { desc.className = 'mode2-desc'; desc.textContent = ''; }
    _resetMode2Verdict();
    return;
  }
  const icons = {Big:'▲', Small:'▼', Triple:'★'};
  badge.className = 'mode2-result-badge ' + val.toLowerCase();
  badge.textContent = (icons[val] || '') + ' ' + val.toUpperCase();
  if (desc) {
    if (val === 'Triple') {
      desc.className = 'mode2-desc triple';
      desc.textContent = '★ Roll TRIPLE & WIN the Big Prize Directly!!!';
    } else {
      desc.className = 'mode2-desc ' + val.toLowerCase();
      desc.textContent = 'ROLL ' + val.toUpperCase() + ' to PLAY PRIZE WHEEL';
    }
  }
  checkMode2Verdict();
}

function _resetMode2Verdict() {
  _mode2Winner = null;
  const v = document.getElementById('mode2-verdict');
  if (v) { v.className = 'mode2-verdict'; v.innerHTML = ''; }
}

function checkMode2Verdict() {
  if (_currentMode !== 2 || !_mode2Selected || !_lastCat || _lastCat === 'Unknown') {
    _resetMode2Verdict();
    _updateSpinBtn();
    return;
  }
  const win = (_mode2Selected === _lastCat);
  _mode2Winner = win;

  const v = document.getElementById('mode2-verdict');
  if (win) {
    if (_mode2Selected === 'Triple') {
      v.className = 'mode2-verdict win show';
      v.innerHTML = '🏆 YOU WIN the BIG PRIZE!'
        + '<span class="verdict-sub">Congratulations — Triple is the Grand Prize!</span>';
    } else {
      v.className = 'mode2-verdict win show';
      v.innerHTML = '🎉 You WIN!'
        + '<span class="verdict-sub">Congratulations — spin the Prize Wheel!</span>';
    }
  } else {
    v.className = 'mode2-verdict lose show';
    v.innerHTML = '😢 You LOSE'
      + '<span class="verdict-sub">Better luck next roll!</span>';
  }
  _updateSpinBtn();
}

function _updateSpinBtn() {
  if (isSpinning) return;
  const spinBtn  = document.getElementById('spin-btn');
  const spinHint = document.getElementById('spin-hint');
  if (_currentMode === 2) {
    const isTripleWin = (_mode2Winner === true && _mode2Selected === 'Triple');
    const enabled = _mode2Winner === true && !isTripleWin;
    spinBtn.disabled = !enabled;
    spinHint.textContent = isTripleWin
      ? '🏆 BIG PRIZE — no spin needed!'
      : (enabled
          ? 'You WIN — ready to spin!'
          : (_mode2Winner === false ? 'You LOSE — no spin this round' : 'Make a selection to play'));
  } else {
    // MODE 1: original behaviour (Triple only)
    spinBtn.disabled = (_lastCat !== 'Triple');
    spinHint.textContent = _lastCat === 'Triple'
      ? 'Triple detected — ready to spin!'
      : 'Roll Triple to enable spin';
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────────
function fmtTime(iso) {
  try { return new Date(iso).toLocaleTimeString(); } catch { return iso; }
}

// ── Update functions ───────────────────────────────────────────────────────────
function updateLatest(d) {
  if (!d || !d.timestamp) return;

  drawPreview(d);
  document.getElementById('preview-meta').textContent =
    `Frame ${d.frame_id || '—'} · ${d.dice_count} dice · ${fmtTime(d.timestamp)}`;

  const warnEl   = document.getElementById('warn-banner');
  const resultEl = document.getElementById('result-content');
  if (d.warning) {
    warnEl.classList.remove('hidden');
    document.getElementById('warn-count').textContent = d.dice_count;
    // In MODE 2 don't dim — user needs to interact with the dropdown
    if (_currentMode !== 2) resultEl.classList.add('dimmed');
  } else {
    warnEl.classList.add('hidden');
    resultEl.classList.remove('dimmed');
  }

  document.getElementById('total-value').textContent =
    (d.dice_count === 3 && d.total !== null) ? d.total : '—';

  const badge = document.getElementById('cat-badge');
  const cat   = d.category || 'Unknown';
  badge.className = 'cat-badge ' + (CLS[cat] || 'unknown');
  badge.innerHTML = (ICONS[cat] || '—') + ' ' + cat.toUpperCase();

  // Triple flash on new Triple detection
  if (cat === 'Triple' && _lastCat !== 'Triple') tripleFlash();
  _lastCat = cat;

  // Marching-lights frame + congrats message (hide if warning or not Triple, or MODE 2)
  const isTripleActive = (cat === 'Triple') && !d.warning && (_currentMode === 1);
  const frameEl = document.getElementById('triple-frame');
  if (frameEl) frameEl.classList.toggle('marching', isTripleActive);
  const congratsEl = document.getElementById('triple-congrats');
  if (congratsEl) congratsEl.classList.toggle('hidden', !isTripleActive);

  // Spin button + MODE 2 verdict
  if (_currentMode === 2) {
    // Lock dropdown when dice are visible; unlock when frame is empty
    const dicePresent = (d.dice_count > 0);
    const sel  = document.getElementById('mode2-select');
    const hint = document.getElementById('mode2-lock-hint');
    if (sel) sel.disabled = dicePresent;
    if (hint) {
      if (dicePresent) {
        hint.className = 'mode2-lock-hint locked';
        hint.textContent = '🔒 Dice detected — selection locked';
      } else {
        hint.className = 'mode2-lock-hint open';
        hint.textContent = 'Make your prediction before rolling dices';
      }
    }
    checkMode2Verdict();
  } else {
    _updateSpinBtn();
  }

  for (let i = 0; i < 3; i++) {
    const el  = document.getElementById('die-' + i);
    const val = (d.dice_values && d.dice_values[i] !== undefined) ? d.dice_values[i] : '';
    el.textContent = val !== '' ? val : '';
    el.classList.toggle('active', val !== '');
  }

  document.getElementById('detected-at').textContent =
    d.timestamp ? 'Detected at ' + fmtTime(d.timestamp) : 'No data yet';
}

function updateStats(s) {
  if (!s) return;
  const cc = s.category_counts || {};
  serverCounts = {Big: cc.Big||0, Small: cc.Small||0, Triple: cc.Triple||0};
  document.getElementById('cnt-big').textContent    = `Big: ${cc.Big||0}`;
  document.getElementById('cnt-small').textContent  = `Small: ${cc.Small||0}`;
  document.getElementById('cnt-triple').textContent = `Triple: ${cc.Triple||0}`;
  renderBarChart(getChartCounts());
  if (typeof s.conf_threshold === 'number') {
    const v = s.conf_threshold.toFixed(2);
    document.getElementById('conf-slider').value = v;
    document.getElementById('conf-val').textContent = v;
  }
}

function updateRolls(rolls) {
  const tbody = document.getElementById('rolls-tbody');
  if (!rolls || !rolls.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No rolls recorded yet</td></tr>';
    return;
  }
  tbody.innerHTML = rolls.map(r => {
    const dice = (r.dice_values || []).join(' + ');
    const cat  = r.category || 'Unknown';
    return `<tr>
      <td>${fmtTime(r.timestamp)}</td>
      <td class="td-dice">${dice}</td>
      <td class="td-total">${r.total !== null ? r.total : '—'}</td>
      <td><span class="result-pill ${cat}">${ICONS[cat]||''} ${cat}</span></td>
    </tr>`;
  }).join('');
}

// ── Status ─────────────────────────────────────────────────────────────────────
let missCount = 0;
function setStatus(online) {
  const el = document.getElementById('status');
  el.className = 'status-badge ' + (online ? 'online' : 'offline');
  el.innerHTML = online ? '&#9679; Online' : '&#9679; Offline';
}

// ── Fetch loops ────────────────────────────────────────────────────────────────
async function fetchLatest() {
  try {
    const [lr, sr] = await Promise.all([fetch('/api/latest'), fetch('/api/stats')]);
    const [latest, stats] = await Promise.all([lr.json(), sr.json()]);
    updateLatest(latest);
    updateStats(stats);
    missCount = 0; setStatus(true);
    document.getElementById('last-update').textContent =
      'Last update: ' + new Date().toLocaleTimeString();
  } catch {
    missCount++;
    if (missCount >= 3) setStatus(false);
  }
}

async function fetchRolls() {
  try {
    const r = await fetch('/api/rolls?limit=10');
    updateRolls(await r.json());
  } catch { /* silent */ }
}

// ── Controls ───────────────────────────────────────────────────────────────────
async function clearData() {
  if (!confirm('Clear all history?')) return;
  await fetch('/api/clear', {method:'POST'});
  document.getElementById('rolls-tbody').innerHTML =
    '<tr><td colspan="4" class="empty-state">No rolls recorded yet</td></tr>';
  document.getElementById('cnt-big').textContent    = 'Big: 0';
  document.getElementById('cnt-small').textContent  = 'Small: 0';
  document.getElementById('cnt-triple').textContent = 'Triple: 0';
  chartBaseline = {Big:0, Small:0, Triple:0};
  serverCounts  = {Big:0, Small:0, Triple:0};
  renderBarChart({Big:0, Small:0, Triple:0});
}

function exportCSV() { window.location.href = '/api/export'; }

let confDebounce = null;
function onConfChange(v) {
  const val = parseFloat(v).toFixed(2);
  document.getElementById('conf-val').textContent = val;
  clearTimeout(confDebounce);
  confDebounce = setTimeout(() => {
    fetch('/api/config', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({confidence_threshold: parseFloat(val)}),
    }).catch(()=>{});
  }, 400);
}

// ── Boot ───────────────────────────────────────────────────────────────────────
drawWheel(wheelAngle, -1);
fetchLatest();
fetchRolls();
setInterval(fetchLatest, 1000);
setInterval(fetchRolls,  3000);
</script>
</body>
</html>"""


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    port, conf, nms_iou, max_rolls, debounce, recv_tensor = load_config()
    store = DiceStore(max_rolls, debounce, conf, nms_iou)

    logger.info("Dice Dashboard Postprocessor starting")
    if len(sys.argv) > 1:
        Postprocessor_Socket_Path = sys.argv[1]
    _acquire_single_instance()
    logger.info("Socket: %s | Port: %d | Conf: %.2f | NMS IoU: %.2f | "
                "Max rolls: %d | Debounce: %d frames | ReceiveInputTensor: %s",
                Postprocessor_Socket_Path, port, conf, nms_iou, max_rolls, debounce,
                recv_tensor)

    try:
        main(port, receive_input_tensor=recv_tensor)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)

    try:
        os.unlink(Postprocessor_Socket_Path)
    except OSError:
        if os.path.exists(Postprocessor_Socket_Path):
            logger.error("Could not remove socket: %s", Postprocessor_Socket_Path)
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                if int(f.read().strip()) == os.getpid():
                    os.unlink(PID_FILE)
    except OSError:
        pass
