#!/usr/bin/env python3
"""
Parking Dashboard Postprocessor
Receives bounding boxes from NX AI Manager (classes: Car, Bus, Truck),
maps each detection to a parking space P1-P6 by horizontal position,
and forwards the result to the parking web dashboard via HTTP.
"""
import os, sys, logging, logging.handlers, configparser, json, signal, time, urllib.request, struct
from threading import Event
import tempfile

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))
sys.path.append(os.path.join(script_location, "../nxai-utilities/python-utilities"))
import nxai_communication_utils

# ── Paths ──────────────────────────────────────────────────────────────────────
_log_dir    = script_location
LOG_FILE    = os.path.join(_log_dir, "plugin.parking-dashboard.log")
CONFIG_FILE = os.path.join(script_location, "plugin.parking-dashboard.ini")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - parking-postprocessor - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=2),
    ]
)

Postprocessor_Socket_Path = os.path.join(
    tempfile.gettempdir(), "python-parking-dashboard-postprocessor.sock"
)
DEFAULT_WEBAPP_URL = "http://localhost:8114"

# Vehicle classes we care about
VEHICLE_CLASSES = {"Car", "Bus", "Truck"}
NUM_SPACES = 6

shutdown_event = Event()
logger         = None


# ── Tensor parsing (xyxysc format) ────────────────────────────────────────────

def parse_bbox_tensors(tensors):
    """Parse bboxes-format:xyxysc tensors into BBoxes_xyxy dict.

    The key format is: bboxes-format:xyxysc;0:ClassName;1:ClassName;...
    The value is raw little-endian float32 bytes, each row = [x1, y1, x2, y2, score, class_id].
    Rows with score <= 0 are padding and skipped.
    """
    bboxes = {}
    for tensor_key, tensor_bytes in tensors.items():
        if not isinstance(tensor_key, str) or 'bboxes-format:xyxysc' not in tensor_key:
            continue
        if not isinstance(tensor_bytes, (bytes, bytearray)):
            continue

        # Parse class names from format string: ";0:Car;1:Bus;..."
        class_names = {}
        for part in tensor_key.split(';')[1:]:
            if ':' in part:
                idx_str, name = part.split(':', 1)
                try:
                    class_names[int(idx_str)] = name
                except ValueError:
                    pass

        n_floats = len(tensor_bytes) // 4
        if n_floats < 6:
            continue
        values = struct.unpack(f'<{n_floats}f', tensor_bytes)
        n_rows = n_floats // 6

        for i in range(n_rows):
            x1, y1, x2, y2, score, class_idx_f = values[i*6:(i+1)*6]
            if score <= 0 or class_idx_f < 0:
                continue
            class_id = int(round(class_idx_f))
            if class_id < 0:
                continue
            class_name = class_names.get(class_id, f'Class{class_id}')
            bboxes.setdefault(class_name, []).extend([x1, y1, x2, y2])

    return bboxes


# ── Space assignment ──────────────────────────────────────────────────────────

def assign_space(cx, frame_width):
    """Map center-x pixel coordinate to parking space P1-P6."""
    if frame_width <= 0:
        frame_width = 1920
    space_idx = min(int((cx / frame_width) * NUM_SPACES), NUM_SPACES - 1)
    return f"P{space_idx + 1}"


# ── Detection extraction ──────────────────────────────────────────────────────

def extract_detections(obj):
    """Return list of detection dicts for vehicle classes, computing space assignment."""
    bboxes      = obj.get('BBoxes_xyxy', {})
    frame_width  = obj.get('Width', 0) or 1920
    frame_height = obj.get('Height', 0) or 1080
    detections   = []

    for cls, coords in bboxes.items():
        if cls not in VEHICLE_CLASSES:
            continue
        for i in range(0, len(coords) - 3, 4):
            x1, y1, x2, y2 = coords[i], coords[i+1], coords[i+2], coords[i+3]
            cx = (x1 + x2) / 2.0
            cy = (y1 + y2) / 2.0
            space = assign_space(cx, frame_width)
            detections.append({
                'space': space,
                'class': cls,
                'cx':    round(cx, 2),
                'cy':    round(cy, 2),
            })

    return detections, frame_width, frame_height


def extract_timestamp(obj):
    """Return a Unix float (seconds) from the message Timestamp field."""
    ts = obj.get('Timestamp')
    if ts is None:
        return time.time()
    return ts / 1_000_000 if ts > 1e12 else float(ts)


def extract_camera_info(obj):
    """Extract camera_id and stream_name from inference message."""
    camera_id   = obj.get('DeviceID') or obj.get('StreamID') or obj.get('CameraID') or 'unknown'
    stream_name = obj.get('DeviceName') or obj.get('StreamName') or camera_id
    return str(camera_id), str(stream_name)


# ── Web app communication ─────────────────────────────────────────────────────

def post_to_webapp(url, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url + '/api/ingest',
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning("Could not reach web app at %s: %s", url, e)
        return False


# ── Lifecycle ─────────────────────────────────────────────────────────────────

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
    webapp_url = DEFAULT_WEBAPP_URL
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        set_log_level(cfg.get('common', 'log_level', fallback='INFO'))
        webapp_url = cfg.get('web_app', 'url', fallback=DEFAULT_WEBAPP_URL)
    except Exception as e:
        logger.error("Config error: %s", e, exc_info=True)
    return webapp_url


def main(webapp_url):
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    if os.path.exists(Postprocessor_Socket_Path):
        try:
            os.unlink(Postprocessor_Socket_Path)
            logger.info("Removed stale socket file: %s", Postprocessor_Socket_Path)
        except OSError as e:
            logger.warning("Could not remove stale socket %s: %s", Postprocessor_Socket_Path, e)

    srv = nxai_communication_utils.SocketListener(Postprocessor_Socket_Path)
    logger.info("Listening on %s  ->  web app at %s", Postprocessor_Socket_Path, webapp_url)

    while not shutdown_event.is_set():
        conn = None
        try:
            conn, msg = srv.accept()
        except nxai_communication_utils.SocketTimeout:
            continue
        except nxai_communication_utils.SocketError as e:
            logger.warning("Socket error: %s", e)
            continue
        except Exception as e:
            logger.error("Unexpected error on accept: %s", e, exc_info=True)
            continue

        try:
            obj = nxai_communication_utils.parseInferenceResults(msg)
            if isinstance(obj, nxai_communication_utils.ExitSignal):
                logger.info("Exit signal received.")
                break
            if not isinstance(obj, dict):
                logger.warning("Parsed message is not a dict (got %s), skipping",
                               type(obj).__name__)
                continue

            # If message contains raw tensors (xyxysc format), convert to BBoxes_xyxy
            if 'Tensors' in obj and 'BBoxes_xyxy' not in obj:
                parsed = parse_bbox_tensors(obj['Tensors'])
                if parsed:
                    obj['BBoxes_xyxy'] = parsed
                    logger.debug("Parsed tensors -> BBoxes_xyxy: %s",
                                 {k: len(v)//4 for k, v in parsed.items()})

            detections, frame_width, frame_height = extract_detections(obj)
            ts = extract_timestamp(obj)
            camera_id, stream_name = extract_camera_info(obj)

            payload = {
                'ts':           ts,
                'camera_id':    camera_id,
                'stream_name':  stream_name,
                'frame_width':  frame_width,
                'frame_height': frame_height,
                'detections':   detections,
            }
            post_to_webapp(webapp_url, payload)
            logger.debug("Forwarded ts=%.3f camera=%s detections=%d",
                         ts, camera_id, len(detections))

            conn.send(nxai_communication_utils.writeInferenceResults(obj))
        except Exception as e:
            logger.warning("Error processing message: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    logger.info("Main loop exited.")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logger = logging.getLogger(__name__)
    webapp_url = config()

    if len(sys.argv) > 1:
        Postprocessor_Socket_Path = sys.argv[1]

    logger.info("Parking Dashboard Postprocessor starting  socket=%s  webapp=%s",
                Postprocessor_Socket_Path, webapp_url)
    try:
        main(webapp_url)
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)

    try:
        os.unlink(Postprocessor_Socket_Path)
    except OSError:
        if os.path.exists(Postprocessor_Socket_Path):
            logger.error("Could not remove socket: %s", Postprocessor_Socket_Path)
