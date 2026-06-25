#!/usr/bin/env python3
"""
VLM Web App — NX AI Manager + Gemma 4 Vision Integration

Feature 1: Live Q&A  — ask natural language questions about the current camera frame
Feature 2: History Search — search periodic Gemma 4 scene descriptions stored in SQLite

Receives inference events from the post-processor via POST /api/ingest.
Serves the web UI at http://0.0.0.0:8115/
"""
import os, sys, logging, logging.handlers, configparser, json, signal, sqlite3, time, base64
import urllib.request, urllib.error, ssl
from datetime import datetime

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE
from threading import Thread, Lock, Event
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from datetime import datetime

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))

# ── Paths ──────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(script_location, "..", "etc", "plugin.vlm-web.ini")
_etc        = os.path.join(script_location, "..", "etc")
_log_dir    = _etc if os.path.exists(_etc) else script_location
LOG_FILE    = os.path.join(_log_dir, "plugin.vlm-web-app.log")
DEFAULT_DB  = os.path.join(_log_dir, "plugin.vlm-web.db")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - vlm-web-app - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=3),
    ]
)

DEFAULT_PORT             = 8115
DEFAULT_OLLAMA_URL       = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL     = "gemma4:e4b"
DEFAULT_NX_URL           = "http://localhost:7001"
DEFAULT_NX_USER          = "admin"
DEFAULT_NX_PASS          = "<NX_PASSWORD>"
DEFAULT_INTERVAL_ACTIVE  = 20   # seconds when objects detected
DEFAULT_INTERVAL_IDLE    = 60   # seconds when scene is empty
DEFAULT_RETENTION_DAYS   = 30

shutdown_event = Event()
web_server     = None
store          = None
scanner        = None
logger         = None


# ── SQLite store ───────────────────────────────────────────────────────────────

class VLMStore:
    """Thread-safe SQLite store for inference events and scene descriptions."""

    def __init__(self, db_path):
        self._lock    = Lock()
        self._db_path = db_path
        self._init_db()

        # In-memory state for scanner
        self.camera_id    = None
        self.stream_name  = None
        self.last_ts      = 0.0
        self.last_counts  = {}  # {class: count}

    def _connect(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS inference_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       REAL    NOT NULL,
                    camera_id       TEXT    NOT NULL,
                    detected_classes TEXT   NOT NULL  -- JSON {"Person":2,"Car":1}
                );
                CREATE TABLE IF NOT EXISTS scene_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   REAL    NOT NULL,
                    camera_id   TEXT    NOT NULL,
                    description TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inf_ts     ON inference_log(timestamp);
                CREATE INDEX IF NOT EXISTS idx_inf_cam    ON inference_log(camera_id);
                CREATE INDEX IF NOT EXISTS idx_scene_ts   ON scene_log(timestamp);
                CREATE INDEX IF NOT EXISTS idx_scene_cam  ON scene_log(camera_id);
            """)

    def ingest(self, payload):
        camera_id   = str(payload.get('camera_id',   'unknown'))
        stream_name = str(payload.get('stream_name', camera_id))
        ts          = float(payload.get('ts', time.time()))
        counts      = payload.get('counts', {})

        with self._lock:
            self.camera_id   = camera_id
            self.stream_name = stream_name
            self.last_ts     = ts
            self.last_counts = counts

        if counts:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO inference_log (timestamp, camera_id, detected_classes) VALUES (?,?,?)",
                    (ts, camera_id, json.dumps(counts))
                )

    def add_scene(self, camera_id, description):
        ts = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO scene_log (timestamp, camera_id, description) VALUES (?,?,?)",
                (ts, camera_id, description)
            )
        logger.debug("Scene saved: camera=%s len=%d", camera_id, len(description))

    def search_scenes(self, start_ts, end_ts, query=None):
        params = [start_ts, end_ts]
        sql    = "SELECT timestamp, camera_id, description FROM scene_log WHERE timestamp >= ? AND timestamp <= ?"
        if query:
            sql    += " AND description LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY timestamp DESC LIMIT 100"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{'timestamp': r[0], 'camera_id': r[1], 'description': r[2]} for r in rows]

    def search_inference(self, start_ts, end_ts, query=None):
        params = [start_ts, end_ts]
        sql    = "SELECT timestamp, camera_id, detected_classes FROM inference_log WHERE timestamp >= ? AND timestamp <= ?"
        if query:
            sql    += " AND detected_classes LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY timestamp DESC LIMIT 100"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{'timestamp': r[0], 'camera_id': r[1], 'detected_classes': json.loads(r[2])} for r in rows]

    def purge_old(self, retention_days):
        cutoff = time.time() - retention_days * 86400
        with self._connect() as conn:
            conn.execute("DELETE FROM inference_log WHERE timestamp < ?", (cutoff,))
            conn.execute("DELETE FROM scene_log WHERE timestamp < ?", (cutoff,))

    def get_status(self):
        with self._lock:
            return {
                'camera_id':   self.camera_id,
                'stream_name': self.stream_name,
                'last_ts':     self.last_ts,
                'last_counts': self.last_counts,
            }


# ── NX API client ──────────────────────────────────────────────────────────────

class NXClient:
    def __init__(self, nx_url, nx_user, nx_pass):
        self._url   = nx_url.rstrip('/')
        self._user  = nx_user
        self._pass  = nx_pass
        self._token = None
        self._token_ts = 0
        self._lock  = Lock()

    def _authenticate(self):
        url  = f"{self._url}/rest/v1/login/sessions"
        body = json.dumps({'username': self._user, 'password': self._pass}).encode()
        req  = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
                data = json.loads(resp.read())
                self._token    = data.get('token')
                self._token_ts = time.time()
                logger.info("NX authenticated, token obtained")
                return True
        except Exception as e:
            logger.error("NX auth failed: %s", e)
            return False

    def _get_token(self):
        with self._lock:
            # Re-authenticate if token is older than 10 minutes
            if not self._token or (time.time() - self._token_ts) > 600:
                self._authenticate()
            return self._token

    def get_thumbnail(self, camera_id, timestamp_ms=None):
        """Fetch camera thumbnail. Returns JPEG bytes or None."""
        token = self._get_token()
        if not token:
            return None
        url = f"{self._url}/ec2/cameraThumbnail?cameraId={{{camera_id}}}&height=480"
        if timestamp_ms is not None:
            url += f"&time={int(timestamp_ms)}"
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        try:
            with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
                if resp.status == 200:
                    return resp.read()
        except Exception as e:
            logger.warning("Thumbnail fetch failed camera=%s: %s", camera_id, e)
        return None

    def list_cameras(self):
        """Return list of {id, name} dicts from NX."""
        token = self._get_token()
        if not token:
            return []
        req = urllib.request.Request(
            f"{self._url}/ec2/getCamerasEx",
            headers={'Authorization': f'Bearer {token}'}
        )
        try:
            with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
                cams = json.loads(resp.read())
                return [{'id': c.get('id','').strip('{}'), 'name': c.get('name', c.get('id',''))} for c in cams]
        except Exception as e:
            logger.error("list_cameras failed: %s", e)
            return []

    def create_bookmark(self, camera_id, name, start_ms, duration_ms, description):
        """Create an NX bookmark. Returns bookmark dict or None."""
        token = self._get_token()
        if not token:
            return None
        url  = f"{self._url}/rest/v3/devices/{camera_id}/bookmarks"
        body = json.dumps({
            'name':        name,
            'startTimeMs': int(start_ms),
            'durationMs':  int(duration_ms),
            'description': description,
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.error("Bookmark creation failed camera=%s: %s", camera_id, e)
            return None


# ── Ollama / Gemma 4 client ────────────────────────────────────────────────────

class OllamaClient:
    def __init__(self, ollama_url, model):
        self._url   = ollama_url.rstrip('/')
        self._model = model

    def ask(self, question, image_bytes=None):
        """Ask a question, optionally with an image. Returns answer string."""
        payload = {
            'model':  self._model,
            'prompt': question,
            'stream': False,
        }
        if image_bytes:
            payload['images'] = [base64.b64encode(image_bytes).decode()]

        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            self._url + '/api/generate',
            data=body,
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
                return result.get('response', '').strip()
        except Exception as e:
            logger.error("Ollama request failed: %s", e)
            return f"Error: {e}"

    def describe_scene(self, image_bytes):
        """Generate a scene description for storage."""
        prompt = (
            "Describe what you see in this camera frame in 2-3 sentences. "
            "Focus on: people, vehicles, objects, activities, and any unusual or notable events. "
            "Be specific and factual."
        )
        return self.ask(prompt, image_bytes)

    def semantic_search(self, query, scenes):
        """Use Gemma 4 to answer a question about scenes and return summary + relevant entries.
        Returns {'summary': str, 'scenes': list}."""
        if not scenes:
            return {'summary': 'No scene data available for the selected time range.', 'scenes': []}

        lines = []
        for i, s in enumerate(scenes):
            ts = datetime.fromtimestamp(s['timestamp']).strftime('%H:%M:%S')
            lines.append(f"[{i}] {ts} — {s['description']}")
        scene_block = "\n".join(lines)

        prompt = (
            f"You are analyzing security camera footage descriptions. "
            f"Answer the question based only on the scenes provided.\n\n"
            f"Scenes:\n{scene_block}\n\n"
            f"Question: {query}\n\n"
            f"Reply in EXACTLY this format:\n"
            f"SUMMARY: <2-3 sentence answer to the question based on all scenes>\n"
            f"---\n"
            f"ID:<number> | <one-sentence reason why this scene is relevant>\n"
            f"(repeat ID lines for each relevant scene, or omit if none are relevant)"
        )
        response = self.ask(prompt)
        logger.debug("semantic_search raw response: %s", response[:300])

        summary = ''
        matched = []

        for line in response.splitlines():
            line = line.strip()
            if line.startswith('SUMMARY:'):
                summary = line[len('SUMMARY:'):].strip()
            elif line.startswith('ID:'):
                try:
                    parts = line.split('|', 1)
                    idx   = int(parts[0].replace('ID:', '').strip())
                    reason = parts[1].strip() if len(parts) > 1 else ''
                    if 0 <= idx < len(scenes):
                        matched.append({**scenes[idx], 'ai_reason': reason})
                except (ValueError, IndexError):
                    continue

        if not summary:
            summary = response.split('---')[0].strip() or 'No summary available.'

        return {'summary': summary, 'scenes': matched}


# ── Background scanner ─────────────────────────────────────────────────────────

class SceneScanner:
    """Periodically fetches a camera frame and stores a Gemma 4 description."""

    def __init__(self, store, nx_client, ollama_client, interval_active, interval_idle, retention_days):
        self._store           = store
        self._nx              = nx_client
        self._ollama          = ollama_client
        self._interval_active = interval_active
        self._interval_idle   = interval_idle
        self._retention_days  = retention_days
        self._thread          = None
        self._stop            = Event()

    def start(self):
        self._thread = Thread(target=self._run, daemon=True, name="scanner")
        self._thread.start()
        logger.info("Scene scanner started (active=%ds idle=%ds)", self._interval_active, self._interval_idle)

    def stop(self):
        self._stop.set()

    def _run(self):
        last_purge  = time.time()
        last_scan   = 0.0

        while not self._stop.is_set():
            status = self._store.get_status()
            camera_id = status.get('camera_id')

            if not camera_id:
                self._stop.wait(timeout=5)
                continue

            has_objects = bool(status.get('last_counts'))
            interval    = self._interval_active if has_objects else self._interval_idle

            now = time.time()
            if (now - last_scan) >= interval:
                last_scan = now
                self._scan(camera_id)

            # Daily purge
            if (now - last_purge) >= 86400:
                last_purge = now
                try:
                    self._store.purge_old(self._retention_days)
                    logger.info("Purged records older than %d days", self._retention_days)
                except Exception as e:
                    logger.error("Purge error: %s", e)

            self._stop.wait(timeout=5)

    def _scan(self, camera_id):
        try:
            jpeg = self._nx.get_thumbnail(camera_id)
            if not jpeg:
                logger.debug("No thumbnail for camera %s, skipping scan", camera_id)
                return
            description = self._ollama.describe_scene(jpeg)
            if description and not description.startswith("Error:"):
                self._store.add_scene(camera_id, description)
        except Exception as e:
            logger.error("Scan error for camera %s: %s", camera_id, e)


# ── HTTP request handler ───────────────────────────────────────────────────────

class VLMHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        logger.info("HTTP %s", fmt % args)

    # ── GET ────────────────────────────────────────────────────────────────────

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)

        def qs1(k, default=None):
            v = qs.get(k)
            return v[0] if v else default

        path = p.path

        if path == '/':
            self._html(_build_html())
            return

        if path == '/api/status':
            self._json(store.get_status())
            return

        if path == '/api/thumbnail':
            camera_id    = qs1('camera_id')
            timestamp_ms = qs1('timestamp_ms')
            if not camera_id:
                self.send_error(400, 'camera_id required')
                return
            jpeg = nx_client.get_thumbnail(
                camera_id,
                timestamp_ms=int(timestamp_ms) if timestamp_ms else None
            )
            if jpeg:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', len(jpeg))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(jpeg)
            else:
                self.send_error(404, 'No thumbnail available')
            return

        if path == '/api/cameras':
            self._json(nx_client.list_cameras())
            return

        if path == '/api/ai_search':
            now   = time.time()
            start = float(qs1('start', now - 3600))
            end   = float(qs1('end',   now))
            query = qs1('query', '')
            if not query:
                self.send_error(400, 'query required for AI search')
                return
            scenes = store.search_scenes(start, end)  # all scenes, no keyword filter
            if not scenes:
                self._json([])
                return
            # Limit to 50 most recent to fit context
            scenes = scenes[:50]
            results = ollama_client.semantic_search(query, scenes)
            self._json(results)
            return

        if path == '/api/search':
            now   = time.time()
            start = float(qs1('start', now - 3600))
            end   = float(qs1('end',   now))
            query = qs1('query', '')
            scenes    = store.search_scenes(start, end, query or None)
            inference = store.search_inference(start, end, query or None)
            combined  = sorted(scenes + [
                {**r, 'description': ', '.join(f"{k}: {v}" for k, v in r['detected_classes'].items())}
                for r in inference
            ], key=lambda x: x['timestamp'], reverse=True)[:100]
            self._json(combined)
            return

        self.send_error(404)

    # ── POST ───────────────────────────────────────────────────────────────────

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        if self.path == '/api/select_camera':
            try:
                data = json.loads(body)
                cam_id   = str(data.get('camera_id', '')).strip()
                cam_name = str(data.get('name', cam_id)).strip()
                if not cam_id:
                    self.send_error(400, 'camera_id required')
                    return
                store.ingest({'camera_id': cam_id, 'stream_name': cam_name, 'ts': time.time(), 'counts': {}})
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)})
            return

        if self.path == '/api/ingest':
            try:
                data = json.loads(body)
                store.ingest(data)
                self._json({'ok': True})
            except Exception as e:
                logger.warning("Ingest error: %s", e)
                self.send_error(400, str(e))
            return

        if self.path == '/api/ask':
            try:
                data      = json.loads(body)
                question  = str(data.get('question', '')).strip()
                camera_id = str(data.get('camera_id', '')).strip() or store.get_status().get('camera_id')
                if not question:
                    self.send_error(400, 'question required')
                    return
                if not camera_id:
                    self._json({'answer': 'No camera connected yet. Please wait for the post-processor to receive a frame.'})
                    return

                jpeg   = nx_client.get_thumbnail(camera_id)
                answer = ollama_client.ask(question, jpeg)
                self._json({'answer': answer, 'camera_id': camera_id})
            except Exception as e:
                logger.error("Ask error: %s", e)
                self._json({'answer': f'Error: {e}'})
            return

        if self.path == '/api/bookmark':
            try:
                data        = json.loads(body)
                camera_id   = str(data.get('camera_id', ''))
                timestamp   = float(data.get('timestamp', 0))
                description = str(data.get('description', ''))
                if not camera_id or not timestamp:
                    self.send_error(400, 'camera_id and timestamp required')
                    return
                name   = f"VLM: {description[:60]}..." if len(description) > 60 else f"VLM: {description}"
                result = nx_client.create_bookmark(
                    camera_id   = camera_id,
                    name        = name,
                    start_ms    = timestamp * 1000,
                    duration_ms = 60000,
                    description = description,
                )
                if result:
                    self._json({'ok': True, 'bookmark': result})
                else:
                    self._json({'ok': False, 'error': 'Bookmark creation failed — check NX server logs'})
            except Exception as e:
                logger.error("Bookmark error: %s", e)
                self._json({'ok': False, 'error': str(e)})
            return

        self.send_error(404)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _html(self, content):
        b = content.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(b))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, data):
        b = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(b))
        self.end_headers()
        self.wfile.write(b)


class _ReusableHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        if issubclass(sys.exc_info()[0], BrokenPipeError):
            return
        logger.error("Unhandled HTTP error from %s", client_address, exc_info=True)


# ── HTML UI ────────────────────────────────────────────────────────────────────

def _build_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VLM Web — NX AI Manager + Gemma 4</title>
<style>
:root {
  --bg:       #0a0a0f;
  --card:     #12121a;
  --border:   #1e1e2e;
  --blue:     #00d4ff;
  --green:    #00ff88;
  --pink:     #ff0066;
  --yellow:   #ffdd00;
  --text:     #e0e0ff;
  --dim:      #666677;
}
*,*::before,*::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace;
       background: var(--bg); color: var(--text); min-height: 100vh; }
.page { max-width: 960px; margin: 0 auto; padding: 20px 16px; }

/* Header */
.header { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
          padding: 18px 24px; margin-bottom: 20px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.header h1 { font-size: 20px; font-weight: 700; color: var(--blue); letter-spacing: 1px;
             text-shadow: 0 0 20px rgba(0,212,255,.4); }
.header .sub { font-size: 12px; color: var(--dim); margin-top: 3px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); margin-left: auto; }
.status-dot.live { background: var(--green); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(.7)} }
.status-label { font-size: 11px; color: var(--dim); }
.cam-select { background: var(--card); border: 1px solid var(--border); border-radius: 6px;
              color: var(--text); padding: 6px 10px; font-size: 12px; outline: none; cursor: pointer; }
.cam-select:focus { border-color: var(--blue); }

/* Tabs */
.tabs { display: flex; gap: 4px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 0; }
.tab { padding: 10px 24px; cursor: pointer; font-size: 13px; font-weight: 700; letter-spacing: .5px;
       color: var(--dim); border-bottom: 2px solid transparent; transition: all .2s; }
.tab.active { color: var(--blue); border-bottom-color: var(--blue); }
.tab:hover { color: var(--text); }
.tab-pane { display: none; }
.tab-pane.active { display: block; }

/* Camera frame */
.frame-wrap { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
              overflow: hidden; margin-bottom: 16px; position: relative; }
.frame-wrap img { width: 100%; height: auto; display: block; max-height: 400px; object-fit: contain;
                  background: #0a0a0f; }
.frame-ts { position: absolute; bottom: 8px; right: 12px; font-size: 10px; color: var(--dim);
            background: rgba(0,0,0,.6); padding: 2px 8px; border-radius: 4px; }

/* Q&A input */
.qa-input-wrap { display: flex; gap: 8px; margin-bottom: 16px; }
.qa-input { flex: 1; background: var(--card); border: 1px solid var(--border); border-radius: 8px;
            color: var(--text); padding: 12px 16px; font-size: 14px; outline: none; transition: border-color .2s; }
.qa-input:focus { border-color: var(--blue); }
.qa-input::placeholder { color: var(--dim); }
.btn { padding: 12px 24px; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer;
       letter-spacing: .5px; transition: all .2s; border: none; }
.btn-primary { background: rgba(0,212,255,.15); border: 1px solid var(--blue); color: var(--blue); }
.btn-primary:hover { background: rgba(0,212,255,.25); box-shadow: 0 0 15px rgba(0,212,255,.2); }
.btn-primary:disabled { opacity: .4; cursor: not-allowed; }
.btn-green { background: rgba(0,255,136,.1); border: 1px solid var(--green); color: var(--green); }
.btn-green:hover { background: rgba(0,255,136,.2); }

/* Q&A history */
.qa-history { display: flex; flex-direction: column; gap: 12px; max-height: 500px; overflow-y: auto; }
.qa-entry { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
.qa-q { font-size: 12px; font-weight: 700; color: var(--blue); margin-bottom: 8px; letter-spacing: .3px; }
.qa-a { font-size: 14px; color: var(--text); line-height: 1.6; white-space: pre-wrap; }
.qa-thinking { color: var(--dim); font-style: italic; }

/* History search */
.search-bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
.search-input { flex: 1; min-width: 200px; background: var(--card); border: 1px solid var(--border);
                border-radius: 8px; color: var(--text); padding: 10px 14px; font-size: 13px; outline: none; }
.search-input:focus { border-color: var(--blue); }
input[type="datetime-local"] { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
                               color: var(--text); padding: 10px 12px; font-size: 12px; outline: none; }
input[type="datetime-local"]:focus { border-color: var(--blue); }

/* Results */
.results { display: flex; flex-direction: column; gap: 12px; }
.result-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
               overflow: hidden; display: flex; gap: 0; }
.result-thumb { width: 160px; min-width: 160px; background: #0a0a0f; display: flex;
                align-items: center; justify-content: center; overflow: hidden; }
.result-thumb img { width: 100%; height: 100%; object-fit: cover; }
.result-thumb .no-thumb { color: var(--dim); font-size: 11px; padding: 12px; text-align: center; }
.result-body { flex: 1; padding: 14px 16px; }
.result-ts { font-size: 11px; color: var(--dim); margin-bottom: 6px; }
.result-desc { font-size: 13px; color: var(--text); line-height: 1.6; margin-bottom: 10px; }
.result-actions { display: flex; gap: 8px; }
.btn-sm { padding: 5px 14px; border-radius: 6px; font-size: 11px; font-weight: 700; cursor: pointer;
          letter-spacing: .3px; transition: all .2s; }
.btn-mark { background: rgba(255,221,0,.08); border: 1px solid var(--yellow); color: var(--yellow); }
.btn-mark:hover { background: rgba(255,221,0,.18); }
.btn-mark:disabled { opacity: .4; cursor: not-allowed; }
.no-results { color: var(--dim); font-size: 13px; text-align: center; padding: 40px; }

/* AI summary */
.ai-summary { background: rgba(0,255,136,.06); border: 1px solid rgba(0,255,136,.25);
              border-radius: 10px; padding: 16px 20px; margin-bottom: 16px; }
.ai-summary-label { font-size: 11px; color: var(--green); font-weight: 700; letter-spacing: .5px; margin-bottom: 8px; }
.ai-summary-text { font-size: 14px; color: var(--text); line-height: 1.7; }

/* Accordion */
.accordion { display: flex; flex-direction: column; gap: 6px; }
.acc-item { background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.acc-header { display: flex; align-items: center; gap: 12px; padding: 12px 16px; cursor: pointer;
              user-select: none; transition: background .15s; }
.acc-header:hover { background: rgba(255,255,255,.03); }
.acc-arrow { font-size: 11px; color: var(--dim); transition: transform .2s; min-width: 12px; }
.acc-arrow.open { transform: rotate(90deg); }
.acc-ts { font-size: 12px; color: var(--dim); white-space: nowrap; }
.acc-reason { font-size: 12px; color: var(--green); flex: 1; }
.acc-body { display: none; padding: 0 16px 14px; border-top: 1px solid var(--border); }
.acc-body.open { display: block; }
.acc-thumb { width: 100%; max-height: 200px; object-fit: contain; background: #0a0a0f;
             border-radius: 6px; margin: 12px 0 10px; display: block; }
.acc-desc { font-size: 13px; color: var(--text); line-height: 1.6; margin-bottom: 10px; }

/* Scrollbar */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>
<div class="page">

<!-- Header -->
<div class="header">
  <div>
    <h1>VLM Web — NX AI Manager</h1>
    <div class="sub">Powered by Gemma 4 (gemma4:e4b) &middot; <span id="cam-label">Waiting for camera...</span></div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;margin-left:auto">
    <select class="cam-select" id="cam-select" onchange="selectCamera(this.value, this.options[this.selectedIndex].text)">
      <option value="">Loading cameras...</option>
    </select>
    <span class="status-label" id="status-label">OFFLINE</span>
    <div class="status-dot" id="status-dot"></div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab active" onclick="switchTab('live', this)">Live Q&amp;A</div>
  <div class="tab" onclick="switchTab('history', this)">History Search</div>
</div>

<!-- ── Tab 1: Live Q&A ──────────────────────────────────────────────────────── -->
<div class="tab-pane active" id="pane-live">

  <div class="frame-wrap">
    <img id="live-frame" src="" alt="Live camera frame" onerror="this.style.display='none'">
    <div class="frame-ts" id="frame-ts"></div>
    <button class="btn btn-primary" style="position:absolute;top:10px;right:10px;padding:6px 14px;font-size:12px" onclick="refreshFrame()">↻ Refresh</button>
  </div>

  <div class="qa-input-wrap">
    <input class="qa-input" id="qa-input" type="text"
           placeholder="Ask anything about the scene... e.g. Is there a car? How many people?"
">
    <button class="btn btn-primary" id="qa-btn" onclick="sendQuestion()">Ask</button>
  </div>

  <div class="qa-history" id="qa-history">
    <div style="color:var(--dim);font-size:12px;text-align:center;padding:20px">
      Ask a question about the live camera scene above.
    </div>
  </div>

</div>

<!-- ── Tab 2: History Search ────────────────────────────────────────────────── -->
<div class="tab-pane" id="pane-history">

  <div class="search-bar">
    <select class="cam-select" id="quick-range" onchange="applyQuickRange(this.value)" style="font-size:12px;padding:8px 12px">
      <option value="">Custom range</option>
      <option value="5">Last 5 min</option>
      <option value="15">Last 15 min</option>
      <option value="30">Last 30 min</option>
      <option value="60">Last 1 hr</option>
      <option value="180">Last 3 hr</option>
    </select>
    <input type="datetime-local" id="search-start">
    <input type="datetime-local" id="search-end">
  </div>
  <div class="search-bar">
    <input class="search-input" id="search-query" type="text"
           placeholder="Keyword search, or ask AI: 'Was there suspicious activity?'">
    <button class="btn btn-primary" onclick="doSearch()">Search</button>
    <button class="btn btn-green" id="ai-search-btn" onclick="doAISearch()">AI Search</button>
  </div>

  <div class="results" id="search-results">
    <div class="no-results">Set a time range and enter a search query, then click Search.</div>
  </div>

</div>

</div><!-- .page -->

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let currentCameraId = null;
let frameUpdated    = null;

// ── Tab switching ──────────────────────────────────────────────────────────────
function switchTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pane-' + name).classList.add('active');
}

// ── Status polling ─────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const r    = await fetch('/api/status');
    const data = await r.json();
    const dot  = document.getElementById('status-dot');
    const lbl  = document.getElementById('status-label');
    const cam  = document.getElementById('cam-label');

    if (data.camera_id && data.last_ts && (Date.now()/1000 - data.last_ts) < 30) {
      dot.classList.add('live');
      lbl.textContent = 'LIVE';
    } else {
      dot.classList.remove('live');
      lbl.textContent = data.camera_id ? 'IDLE' : 'OFFLINE';
    }
    if (data.camera_id) {
      currentCameraId = data.camera_id;
      cam.textContent = data.stream_name || data.camera_id;
    }
  } catch(e) {}
}

// ── Live frame ─────────────────────────────────────────────────────────────────
async function refreshFrame() {
  if (!currentCameraId) return;
  const img  = document.getElementById('live-frame');
  const ts   = document.getElementById('frame-ts');
  const url  = '/api/thumbnail?camera_id=' + encodeURIComponent(currentCameraId);
  const now  = new Date().toLocaleTimeString();
  img.style.display = 'block';
  img.src = url + '&_t=' + Date.now();
  ts.textContent = 'Last updated: ' + now;
}

// ── Live Q&A ───────────────────────────────────────────────────────────────────
async function sendQuestion() {
  const input = document.getElementById('qa-input');
  const btn   = document.getElementById('qa-btn');
  const q     = input.value.trim();
  if (!q) return;

  input.value = '';
  btn.disabled = true;

  // Refresh frame before sending
  await refreshFrame();

  const history = document.getElementById('qa-history');
  const entry   = document.createElement('div');
  entry.className = 'qa-entry';
  entry.innerHTML = '<div class="qa-q">Q: ' + escHtml(q) + '</div>' +
                    '<div class="qa-a qa-thinking">Asking Gemma 4...</div>';
  history.insertBefore(entry, history.firstChild);

  try {
    const r = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, camera_id: currentCameraId }),
    });
    const data = await r.json();
    entry.querySelector('.qa-a').textContent = data.answer || '(no answer)';
    entry.querySelector('.qa-a').classList.remove('qa-thinking');
  } catch(e) {
    entry.querySelector('.qa-a').textContent = 'Error: ' + e.message;
    entry.querySelector('.qa-a').classList.remove('qa-thinking');
  }
  btn.disabled = false;
}

// ── Quick range ────────────────────────────────────────────────────────────────
function applyQuickRange(minutes) {
  if (!minutes) return;
  const now  = new Date();
  const from = new Date(now - minutes * 60 * 1000);
  const fmt  = d => {
    const pad = n => String(n).padStart(2,'0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  document.getElementById('search-start').value = fmt(from);
  document.getElementById('search-end').value   = fmt(now);
}

// ── History Search ─────────────────────────────────────────────────────────────
function initDateInputs() {
  const now   = new Date();
  const ago24 = new Date(now - 24 * 3600 * 1000);
  const fmt   = d => {
    const pad = n => String(n).padStart(2,'0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  document.getElementById('search-start').value = fmt(ago24);
  document.getElementById('search-end').value   = '';  // blank = use current time at search
}

async function doSearch() {
  const startEl = document.getElementById('search-start');
  const endEl   = document.getElementById('search-end');
  const query   = document.getElementById('search-query').value.trim();
  const results = document.getElementById('search-results');

  const start = startEl.value ? new Date(startEl.value).getTime() / 1000 : (Date.now()/1000 - 3600);
  const end   = endEl.value   ? new Date(endEl.value).getTime() / 1000   : Date.now() / 1000;

  results.innerHTML = '<div class="no-results">Searching...</div>';

  try {
    const params = new URLSearchParams({ start, end });
    if (query) params.set('query', query);
    const r    = await fetch('/api/search?' + params);
    const data = await r.json();

    if (!data.length) {
      results.innerHTML = '<div class="no-results">No results found for the selected time range and query.</div>';
      return;
    }

    results.innerHTML = '';
    data.forEach(item => {
      const ts  = new Date(item.timestamp * 1000).toLocaleString();
      const tms = Math.round(item.timestamp * 1000);
      const camId = item.camera_id;

      const card = document.createElement('div');
      card.className = 'result-card';
      card.innerHTML =
        '<div class="result-thumb" id="thumb-' + tms + '">' +
          '<div class="no-thumb">Loading...</div>' +
        '</div>' +
        '<div class="result-body">' +
          '<div class="result-ts">' + escHtml(ts) + ' &middot; ' + escHtml(camId) + '</div>' +
          '<div class="result-desc">' + escHtml(item.description) + '</div>' +
          '<div class="result-actions">' +
            '<button class="btn-sm btn-mark" onclick="markNX(\\'' + escHtml(camId) + '\\',' + item.timestamp + ',\\'' + escHtml(item.description.slice(0,120)) + '\\',this)">Mark in NX</button>' +
          '</div>' +
        '</div>';
      results.appendChild(card);

      // Load thumbnail asynchronously
      loadThumb('thumb-' + tms, camId, tms);
    });
  } catch(e) {
    results.innerHTML = '<div class="no-results">Search error: ' + escHtml(e.message) + '</div>';
  }
}

async function doAISearch() {
  const startEl = document.getElementById('search-start');
  const endEl   = document.getElementById('search-end');
  const query   = document.getElementById('search-query').value.trim();
  const results = document.getElementById('search-results');
  const btn     = document.getElementById('ai-search-btn');

  if (!query) {
    results.innerHTML = '<div class="no-results">Enter a question for AI Search, e.g. "Summarize what happened" or "Was there suspicious activity?"</div>';
    return;
  }

  const start = startEl.value ? new Date(startEl.value).getTime() / 1000 : (Date.now()/1000 - 3600);
  const end   = endEl.value   ? new Date(endEl.value).getTime() / 1000   : Date.now() / 1000;

  results.innerHTML = '<div class="no-results">🤖 Asking Gemma 4... (5–20s)</div>';
  btn.disabled = true;

  try {
    const params = new URLSearchParams({ start, end, query });
    const r    = await fetch('/api/ai_search?' + params);
    const data = await r.json();

    results.innerHTML = '';

    // Summary block
    const summaryEl = document.createElement('div');
    summaryEl.className = 'ai-summary';
    summaryEl.innerHTML =
      '<div class="ai-summary-label">🤖 AI Summary</div>' +
      '<div class="ai-summary-text">' + escHtml(data.summary || 'No summary.') + '</div>';
    results.appendChild(summaryEl);

    if (!data.scenes || !data.scenes.length) {
      const none = document.createElement('div');
      none.className = 'no-results';
      none.style.marginTop = '12px';
      none.textContent = 'No specific scenes identified.';
      results.appendChild(none);
      btn.disabled = false;
      return;
    }

    // Accordion
    const acc = document.createElement('div');
    acc.className = 'accordion';

    data.scenes.forEach((item, idx) => {
      const ts    = new Date(item.timestamp * 1000).toLocaleString();
      const tms   = Math.round(item.timestamp * 1000);
      const camId = item.camera_id;
      const thumbId = 'ai-thumb-' + tms + '-' + idx;

      const itemEl = document.createElement('div');
      itemEl.className = 'acc-item';
      itemEl.innerHTML =
        '<div class="acc-header" onclick="toggleAcc(this)">' +
          '<span class="acc-arrow">▶</span>' +
          '<span class="acc-ts">' + escHtml(ts) + '</span>' +
          '<span class="acc-reason">' + escHtml(item.ai_reason || '') + '</span>' +
        '</div>' +
        '<div class="acc-body">' +
          '<img class="acc-thumb" id="' + thumbId + '" src="" style="display:none" alt="thumbnail">' +
          '<div class="acc-desc">' + escHtml(item.description) + '</div>' +
          '<div class="result-actions">' +
            '<button class="btn-sm btn-mark" onclick="markNX(\\'' + escHtml(camId) + '\\',' + item.timestamp + ',\\'' + escHtml(item.description.slice(0,120)) + '\\',this)">Mark in NX</button>' +
          '</div>' +
        '</div>';

      acc.appendChild(itemEl);
      loadThumbIntoImg(thumbId, camId, tms);
    });

    results.appendChild(acc);
  } catch(e) {
    results.innerHTML = '<div class="no-results">AI Search error: ' + escHtml(e.message) + '</div>';
  }
  btn.disabled = false;
}

function toggleAcc(header) {
  const arrow = header.querySelector('.acc-arrow');
  const body  = header.nextElementSibling;
  const open  = body.classList.toggle('open');
  arrow.classList.toggle('open', open);
}

async function loadThumbIntoImg(imgId, cameraId, timestampMs) {
  try {
    const url  = '/api/thumbnail?camera_id=' + encodeURIComponent(cameraId) + '&timestamp_ms=' + timestampMs;
    const resp = await fetch(url);
    if (resp.ok) {
      const blob   = await resp.blob();
      const img    = document.getElementById(imgId);
      if (img) { img.src = URL.createObjectURL(blob); img.style.display = 'block'; }
    }
  } catch(e) {}
}

async function loadThumb(containerId, cameraId, timestampMs) {
  const container = document.getElementById(containerId);
  if (!container) return;
  try {
    const url  = '/api/thumbnail?camera_id=' + encodeURIComponent(cameraId) + '&timestamp_ms=' + timestampMs;
    const resp = await fetch(url);
    if (resp.ok) {
      const blob   = await resp.blob();
      const imgUrl = URL.createObjectURL(blob);
      container.innerHTML = '<img src="' + imgUrl + '" alt="thumbnail">';
    } else {
      container.innerHTML = '<div class="no-thumb">No thumbnail</div>';
    }
  } catch(e) {
    container.innerHTML = '<div class="no-thumb">Error</div>';
  }
}

async function markNX(cameraId, timestamp, description, btn) {
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {
    const r = await fetch('/api/bookmark', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: cameraId, timestamp, description }),
    });
    const data = await r.json();
    if (data.ok) {
      btn.textContent = 'Marked!';
      btn.style.color = 'var(--green)';
      btn.style.borderColor = 'var(--green)';
    } else {
      btn.textContent = 'Failed';
      btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = 'Error';
    btn.disabled = false;
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────────
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Camera selector ────────────────────────────────────────────────────────────
async function loadCameras() {
  for (let attempt = 0; attempt < 10; attempt++) {
    try {
      const r    = await fetch('/api/cameras');
      const cams = await r.json();
      if (!cams.length) throw new Error('empty');
      const sel  = document.getElementById('cam-select');
      sel.innerHTML = '<option value="">— Select camera —</option>' +
        cams.map(c => `<option value="${escHtml(c.id)}">${escHtml(c.name)}</option>`).join('');
      return;
    } catch(e) {
      await new Promise(r => setTimeout(r, 2000));
    }
  }
  document.getElementById('cam-select').innerHTML = '<option value="">Failed to load cameras</option>';
}

async function selectCamera(id, name) {
  if (!id) return;
  await fetch('/api/select_camera', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ camera_id: id, name }),
  });
  currentCameraId = id;
  document.getElementById('cam-label').textContent = name;
  refreshFrame();
}

// ── Init ───────────────────────────────────────────────────────────────────────
initDateInputs();
loadCameras();
pollStatus();
setInterval(pollStatus, 5000);
setInterval(() => { if (currentCameraId) refreshFrame(); }, 15000);
</script>
</body>
</html>"""


# ── Server lifecycle ───────────────────────────────────────────────────────────

def start_web_server(port):
    global web_server
    try:
        web_server = _ReusableHTTPServer(('0.0.0.0', port), VLMHandler)
    except Exception as e:
        logger.error("Could not bind to port %d: %s", port, e)
        raise
    def run():
        try:
            web_server.serve_forever()
        except Exception as e:
            logger.error("Web server error: %s", e, exc_info=True)
    Thread(target=run, daemon=True, name="http").start()
    logger.info("VLM Web App running at http://0.0.0.0:%d", port)


def signal_handler(signum, _):
    logger.info("Signal %s received, shutting down.", signal.Signals(signum).name)
    shutdown_event.set()
    if web_server:
        web_server.shutdown()
    if scanner:
        scanner.stop()


def set_log_level(level):
    try:
        logger.setLevel(getattr(logging, level.upper()))
    except Exception as e:
        logger.error("Log level error: %s", e)


def config():
    logger.info("Reading config from: %s", CONFIG_FILE)
    port             = DEFAULT_PORT
    ollama_url       = DEFAULT_OLLAMA_URL
    ollama_model     = DEFAULT_OLLAMA_MODEL
    nx_url           = DEFAULT_NX_URL
    nx_user          = DEFAULT_NX_USER
    nx_pass          = DEFAULT_NX_PASS
    interval_active  = DEFAULT_INTERVAL_ACTIVE
    interval_idle    = DEFAULT_INTERVAL_IDLE
    retention_days   = DEFAULT_RETENTION_DAYS
    db_path          = DEFAULT_DB

    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        set_log_level(cfg.get('common', 'log_level', fallback='INFO'))
        port           = cfg.getint('web_app',   'port',             fallback=DEFAULT_PORT)
        db_path        = cfg.get   ('web_app',   'db_path',          fallback=DEFAULT_DB)
        ollama_url     = cfg.get   ('ollama',    'url',              fallback=DEFAULT_OLLAMA_URL)
        ollama_model   = cfg.get   ('ollama',    'model',            fallback=DEFAULT_OLLAMA_MODEL)
        nx_url         = cfg.get   ('nx',        'url',              fallback=DEFAULT_NX_URL)
        nx_user        = cfg.get   ('nx',        'username',         fallback=DEFAULT_NX_USER)
        nx_pass        = cfg.get   ('nx',        'password',         fallback=DEFAULT_NX_PASS)
        interval_active = cfg.getint('scanner',  'interval_active',  fallback=DEFAULT_INTERVAL_ACTIVE)
        interval_idle   = cfg.getint('scanner',  'interval_idle',    fallback=DEFAULT_INTERVAL_IDLE)
        retention_days  = cfg.getint('retention','days',             fallback=DEFAULT_RETENTION_DAYS)
    except Exception as e:
        logger.error("Config error: %s", e, exc_info=True)

    return port, ollama_url, ollama_model, nx_url, nx_user, nx_pass, \
           interval_active, interval_idle, retention_days, db_path


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description='VLM Web App')
    parser.add_argument('--port',  type=int, help='Override web port')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    port, ollama_url, ollama_model, nx_url, nx_user, nx_pass, \
        interval_active, interval_idle, retention_days, db_path = config()

    if args.port:
        port = args.port
    if args.debug:
        set_log_level('DEBUG')

    logger.info("VLM Web App starting — port=%d ollama=%s model=%s nx=%s",
                port, ollama_url, ollama_model, nx_url)

    store         = VLMStore(db_path)
    nx_client     = NXClient(nx_url, nx_user, nx_pass)
    ollama_client = OllamaClient(ollama_url, ollama_model)
    scanner       = SceneScanner(store, nx_client, ollama_client,
                                 interval_active, interval_idle, retention_days)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    # Pre-authenticate so the first UI request is instant
    Thread(target=nx_client._get_token, daemon=True).start()

    start_web_server(port)
    scanner.start()

    logger.info("Ready. Open http://localhost:%d in your browser.", port)
    shutdown_event.wait()
    logger.info("VLM Web App stopped.")
