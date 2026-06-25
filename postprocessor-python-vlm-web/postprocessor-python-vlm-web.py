#!/usr/bin/env python3
"""
VLM Web Post-Processor
Receives inference results from NX AI Manager via Unix socket and forwards
detection metadata to the VLM web app.  The web app handles all Ollama /
Gemma 4 communication and the HTTP UI.
"""
import os, sys, logging, logging.handlers, configparser, json, signal, time, urllib.request, struct, tempfile
from threading import Event, Thread

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))
sys.path.append(os.path.join(script_location, "../nxai-utilities/python-utilities"))
import nxai_communication_utils

# ── Paths ──────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(script_location, "..", "etc", "plugin.vlm-web.ini")
_etc = os.path.join(script_location, "..", "etc")
LOG_FILE = os.path.join(
    _etc if os.path.exists(_etc) else script_location,
    "plugin.vlm-web.log"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - vlm-web - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=3),
    ]
)

Postprocessor_Name        = "External - Python-VLM-Web-Postprocessor"
Postprocessor_Socket_Path = os.path.join(
    tempfile.gettempdir(), "python-vlm-web-postprocessor.sock"
)

DEFAULT_WEBAPP_URL = "http://localhost:8115"

shutdown_event = Event()
logger         = None


# ── Tensor parsing (same as web-dashboard-advance) ─────────────────────────────

def parse_bbox_tensors(tensors):
    bboxes = {}
    for tensor_key, tensor_bytes in tensors.items():
        if not isinstance(tensor_key, str) or 'bboxes-format:xyxysc' not in tensor_key:
            continue
        if not isinstance(tensor_bytes, (bytes, bytearray)):
            continue
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
            class_id  = int(round(class_idx_f))
            class_name = class_names.get(class_id, f'Class{class_id}')
            bboxes.setdefault(class_name, []).extend([x1, y1, x2, y2])
    return bboxes


# ── Message helpers ────────────────────────────────────────────────────────────

def extract_camera_info(obj):
    camera_id   = obj.get('DeviceID') or obj.get('StreamID') or obj.get('CameraID') or 'unknown'
    stream_name = obj.get('DeviceName') or obj.get('StreamName') or camera_id
    return str(camera_id), str(stream_name)


def extract_timestamp(obj):
    ts = obj.get('Timestamp')
    if ts is None:
        return time.time()
    return ts / 1_000_000 if ts > 1e12 else float(ts)


def count_objects(obj):
    if 'BBoxes_xyxy' not in obj:
        return {}
    return {cls: len(coords) // 4 for cls, coords in obj['BBoxes_xyxy'].items() if len(coords) >= 4}


# ── Web app communication ──────────────────────────────────────────────────────

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


# ── Lifecycle ──────────────────────────────────────────────────────────────────

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
        except OSError as e:
            logger.warning("Could not remove stale socket %s: %s", Postprocessor_Socket_Path, e)

    srv = nxai_communication_utils.SocketListener(Postprocessor_Socket_Path)

    while not shutdown_event.is_set():
        logger.debug("Waiting for message")
        conn = None
        try:
            conn, msg = srv.accept()
        except nxai_communication_utils.SocketTimeout:
            continue
        except nxai_communication_utils.SocketError as e:
            logger.warning("Socket error: %s", e)
            continue
        except Exception as e:
            logger.error("Unexpected error: %s", e, exc_info=True)
            continue

        try:
            obj = nxai_communication_utils.parseInferenceResults(msg)
            if isinstance(obj, nxai_communication_utils.ExitSignal):
                logger.info("Exit signal received.")
                break
            if not isinstance(obj, dict):
                logger.warning("Unexpected message type: %s", type(obj).__name__)
                continue

            if 'Tensors' in obj and 'BBoxes_xyxy' not in obj:
                parsed = parse_bbox_tensors(obj['Tensors'])
                if parsed:
                    obj['BBoxes_xyxy'] = parsed

            counts      = count_objects(obj)
            ts          = extract_timestamp(obj)
            camera_id, stream_name = extract_camera_info(obj)

            payload = {
                'ts':          ts,
                'camera_id':   camera_id,
                'stream_name': stream_name,
                'counts':      counts,
                'width':       obj.get('Width', 0),
                'height':      obj.get('Height', 0),
            }
            Thread(target=post_to_webapp, args=(webapp_url, payload), daemon=True).start()
            logger.debug("Forwarded ts=%.3f camera=%s counts=%s", ts, camera_id, counts)

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

    logger.info("VLM Web Post-Processor starting")
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
