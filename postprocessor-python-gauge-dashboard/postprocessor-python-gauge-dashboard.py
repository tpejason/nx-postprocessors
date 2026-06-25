#!/usr/bin/env python3
"""
Gauge Dashboard Postprocessor
Reads bounding boxes from NX AI Manager (classes: Needle, E, F, 1/2)
and forwards gauge readings to the web dashboard via HTTP.
"""
import os, sys, logging, logging.handlers, configparser, json, signal, time, urllib.request
from threading import Event
import tempfile

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))
sys.path.append(os.path.join(script_location, "../nxai-utilities/python-utilities"))
import nxai_communication_utils

_log_dir    = script_location
LOG_FILE    = os.path.join(_log_dir, "plugin.gauge-dashboard.log")
CONFIG_FILE = os.path.join(script_location, "plugin.gauge-dashboard.ini")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - gauge-postprocessor - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=2),
    ]
)

Postprocessor_Socket_Path = os.path.join(
    tempfile.gettempdir(), "python-gauge-dashboard-postprocessor.sock"
)
DEFAULT_WEBAPP_URL = "http://localhost:8113"

shutdown_event = Event()
logger         = None


def cx_of(boxes):
    if len(boxes) >= 4:
        return (boxes[0] + boxes[2]) / 2.0
    return None


def extract_reading(msg):
    bboxes = msg.get('BBoxes_xyxy', {})
    needle_cx = cx_of(bboxes.get('Needle', []))
    if needle_cx is None:
        return None

    e_raw = cx_of(bboxes.get('E', []))
    f_raw = cx_of(bboxes.get('F', []))

    return {
        'needle_cx': round(needle_cx, 2),
        'e_cx':      round(e_raw, 2) if e_raw is not None else None,
        'f_cx':      round(f_raw, 2) if f_raw is not None else None,
        'width':     msg.get('Width',  0),
        'height':    msg.get('Height', 0),
        'ts':        time.time(),
    }


def post_to_webapp(url, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url + '/api/reading',
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


def signal_handler(signum, _):
    logger.info("Signal %s received, shutting down.", signal.Signals(signum).name)
    shutdown_event.set()


def config():
    webapp_url = DEFAULT_WEBAPP_URL
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
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
        except OSError:
            pass

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
            logger.error("Unexpected error: %s", e, exc_info=True)
            continue

        try:
            obj = nxai_communication_utils.parseInferenceResults(msg)
            if isinstance(obj, nxai_communication_utils.ExitSignal):
                logger.info("Exit signal received.")
                break
            if isinstance(obj, dict):
                reading = extract_reading(obj)
                if reading:
                    post_to_webapp(webapp_url, reading)
                    logger.debug("Forwarded: needle_cx=%.1f  e=%s  f=%s",
                                 reading['needle_cx'], reading['e_cx'], reading['f_cx'])
            conn.send(nxai_communication_utils.writeInferenceResults(obj))
        except Exception as e:
            logger.warning("Error processing message: %s", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    logger.info("Main loop exited.")


if __name__ == '__main__':
    logger = logging.getLogger(__name__)
    webapp_url = config()

    if len(sys.argv) > 1:
        Postprocessor_Socket_Path = sys.argv[1]

    logger.info("Gauge Dashboard Postprocessor starting  socket=%s  webapp=%s",
                Postprocessor_Socket_Path, webapp_url)
    try:
        main(webapp_url)
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)

    try:
        os.unlink(Postprocessor_Socket_Path)
    except OSError:
        pass
