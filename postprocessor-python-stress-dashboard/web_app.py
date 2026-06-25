#!/usr/bin/env python3
"""
Stress Dashboard Web App
========================
Companion to postprocessor-python-stress-dashboard.

  * Receives per-camera frame counts from the postprocessor (POST /api/fps) and
    turns them into total + per-channel inference FPS.
  * Samples whole-machine CPU / RAM / GPU / NPU load once per second
    (see metrics.py — all sources auto-detected, graceful degradation).
  * Serves a live dashboard at http://<server>:8120.
  * Lets you run a named stress test (Start/Stop) and export the captured
    window as a self-contained HTML report and/or CSV.

Run separately from the postprocessor. For accurate Intel iGPU engine
utilisation (read from the mediaserver's DRM fdinfo) run this as root:

    sudo python3 web_app.py --port 8120
"""
import os, sys, json, time, signal, socket, sqlite3, logging, logging.handlers
import argparse, configparser, threading, statistics, html
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))
sys.path.append(script_location)
from metrics import MetricsManager

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_PORT = 8120
SAMPLE_INTERVAL = 1.0          # seconds between metric samples
FPS_IDLE_TIMEOUT = 3.0         # seconds without a count -> camera FPS decays to 0
CAM_DROP_TIMEOUT = 15.0        # seconds without a count -> camera dropped from the list
                               # (so only cameras currently running the postprocessor show)
LIVE_BUFFER = 600              # live ring-buffer samples kept in memory (~10 min)

_etc = os.path.join(script_location, "..", "etc")
CONFIG_FILE = os.path.join(_etc, "plugin.stress-dashboard.ini")
DEFAULT_DB = os.path.join(_etc if os.path.isdir(_etc) else script_location,
                          "plugin.stress-dashboard.db")
LOG_FILE = os.path.join(_etc if os.path.isdir(_etc) else script_location,
                        "plugin.stress-dashboard-app.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - stress-dashboard-app - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=3),
    ],
)
logger = logging.getLogger("stress-dashboard-app")

shutdown_event = threading.Event()


# ════════════════════════════════════════════════════════════════════════════
#  FPS tracking (fed by the postprocessor)
# ════════════════════════════════════════════════════════════════════════════

class FpsTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._fps = {}        # device_id -> instantaneous fps
        self._meta = {}       # device_id -> {"w","h","name"}
        self._last = {}       # device_id -> monotonic time of last update

    def ingest(self, payload):
        interval = float(payload.get("interval") or SAMPLE_INTERVAL)
        if interval <= 0:
            interval = SAMPLE_INTERVAL
        frames = payload.get("frames") or {}
        meta = payload.get("meta") or {}
        now = time.monotonic()
        with self._lock:
            for dev, count in frames.items():
                self._fps[dev] = round(float(count) / interval, 2)
                self._last[dev] = now
                if dev in meta:
                    self._meta[dev] = meta[dev]

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            cams = []
            total = 0.0
            for dev in list(self._fps.keys()):
                idle = now - self._last.get(dev, 0)
                # Drop cameras that haven't reported in a while — they no longer
                # have the stress postprocessor assigned (or stopped inferencing),
                # so the table only lists currently-active assigned cameras.
                if idle > CAM_DROP_TIMEOUT:
                    self._fps.pop(dev, None)
                    self._meta.pop(dev, None)
                    self._last.pop(dev, None)
                    continue
                fps = self._fps[dev] if idle <= FPS_IDLE_TIMEOUT else 0.0
                self._fps[dev] = fps
                m = self._meta.get(dev, {})
                cams.append({"id": dev, "fps": fps, "w": m.get("w", 0),
                             "h": m.get("h", 0), "name": m.get("name", dev)})
                total += fps
            cams.sort(key=lambda c: c["id"])
            return cams, round(total, 2)


# ════════════════════════════════════════════════════════════════════════════
#  SQLite store
# ════════════════════════════════════════════════════════════════════════════

class StressStore:
    def __init__(self, db_path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = self._connect()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT, model TEXT, camera_count INTEGER,
                    resolution TEXT, notes TEXT,
                    start_ts REAL, end_ts REAL,
                    status TEXT, hostname TEXT, sources_json TEXT
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER, ts REAL,
                    cpu_pct REAL, ram_pct REAL, ram_used_mb REAL, ram_total_mb REAL,
                    igpu_pct REAL, igpu_mode TEXT,
                    npu_pct REAL, npu_mem_mb REAL, npu_freq_mhz REAL,
                    nvidia_pct REAL,
                    fps_total REAL, n_cameras INTEGER,
                    fps_json TEXT, metrics_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id, ts);
                CREATE TABLE IF NOT EXISTS cameras (
                    device_id TEXT PRIMARY KEY, name TEXT
                );
                """
            )
            self._conn.commit()

    # ── sessions ──────────────────────────────────────────────────────────
    def start_session(self, meta, sources):
        with self._lock:
            # Close any session still marked running (e.g. after a crash).
            self._conn.execute(
                "UPDATE sessions SET status='aborted', end_ts=? WHERE status='running'",
                (time.time(),))
            cur = self._conn.execute(
                """INSERT INTO sessions
                   (name, model, camera_count, resolution, notes,
                    start_ts, end_ts, status, hostname, sources_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (meta.get("name", ""), meta.get("model", ""),
                 int(meta.get("camera_count") or 0), meta.get("resolution", ""),
                 meta.get("notes", ""), time.time(), None, "running",
                 socket.gethostname(), json.dumps(sources)))
            self._conn.commit()
            return cur.lastrowid

    def stop_session(self, session_id):
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='completed', end_ts=? WHERE id=? AND status='running'",
                (time.time(), session_id))
            self._conn.commit()

    def active_session(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def list_sessions(self):
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.*, (SELECT COUNT(*) FROM samples WHERE session_id=s.id) AS n_samples
                   FROM sessions s ORDER BY s.id DESC""").fetchall()
            return [dict(r) for r in rows]

    def get_session(self, session_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def delete_session(self, session_id):
        with self._lock:
            self._conn.execute("DELETE FROM samples WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            self._conn.commit()

    def delete_sessions(self, ids):
        """Bulk-delete; never removes a still-running session. Returns count deleted."""
        ids = [int(i) for i in ids if i]
        if not ids:
            return 0
        with self._lock:
            qs = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT id FROM sessions WHERE id IN ({qs}) AND status!='running'", ids
            ).fetchall()
            kill = [r[0] for r in rows]
            if not kill:
                return 0
            qk = ",".join("?" * len(kill))
            self._conn.execute(f"DELETE FROM samples WHERE session_id IN ({qk})", kill)
            self._conn.execute(f"DELETE FROM sessions WHERE id IN ({qk})", kill)
            self._conn.commit()
            return len(kill)

    def add_sample(self, session_id, rec):
        with self._lock:
            self._conn.execute(
                """INSERT INTO samples
                   (session_id, ts, cpu_pct, ram_pct, ram_used_mb, ram_total_mb,
                    igpu_pct, igpu_mode, npu_pct, npu_mem_mb, npu_freq_mhz,
                    nvidia_pct, fps_total, n_cameras, fps_json, metrics_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, rec["ts"], rec.get("cpu_pct"), rec.get("ram_pct"),
                 rec.get("ram_used_mb"), rec.get("ram_total_mb"),
                 rec.get("igpu_pct"), rec.get("igpu_mode"),
                 rec.get("npu_pct"), rec.get("npu_mem_mb"), rec.get("npu_freq_mhz"),
                 rec.get("nvidia_pct"), rec.get("fps_total"), rec.get("n_cameras"),
                 json.dumps(rec.get("fps_map", {})), json.dumps(rec.get("metrics", {}))))
            self._conn.commit()

    def get_samples(self, session_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM samples WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()
            return [dict(r) for r in rows]

    # ── cameras ───────────────────────────────────────────────────────────
    def get_camera_names(self):
        with self._lock:
            rows = self._conn.execute("SELECT device_id, name FROM cameras").fetchall()
            return {r["device_id"]: r["name"] for r in rows}

    def set_camera_name(self, device_id, name):
        with self._lock:
            self._conn.execute(
                "INSERT INTO cameras(device_id, name) VALUES(?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET name=excluded.name",
                (device_id, name))
            self._conn.commit()


# ════════════════════════════════════════════════════════════════════════════
#  Statistics helpers
# ════════════════════════════════════════════════════════════════════════════

def _stats(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"avg": None, "peak": None, "min": None, "p95": None}
    s = sorted(vals)
    idx = max(0, min(len(s) - 1, int(round(0.95 * (len(s) - 1)))))
    return {
        "avg": round(statistics.fmean(vals), 2),
        "peak": round(max(vals), 2),
        "min": round(min(vals), 2),
        "p95": round(s[idx], 2),
    }


def build_summary(session, samples):
    cols = {k: [s.get(k) for s in samples] for k in
            ("cpu_pct", "ram_pct", "igpu_pct", "npu_pct", "nvidia_pct",
             "fps_total", "ram_used_mb", "npu_mem_mb")}
    summary = {k: _stats(v) for k, v in cols.items()}
    # Per-camera FPS aggregation.
    per_cam = {}
    names = {}
    dims = {}                       # device_id -> (w, h) of the inferenced frame
    for s in samples:
        try:
            fm = json.loads(s.get("fps_json") or "{}")
        except Exception:
            fm = {}
        for dev, info in fm.items():
            fps = info.get("fps") if isinstance(info, dict) else info
            per_cam.setdefault(dev, []).append(fps)
            if isinstance(info, dict):
                if info.get("name"):
                    names[dev] = info["name"]
                if info.get("w"):       # keep the latest non-zero inference dims
                    dims[dev] = (info.get("w"), info.get("h"))
    cam_summary = []
    for dev, vals in per_cam.items():
        st = _stats(vals)
        st["device_id"] = dev
        st["name"] = names.get(dev, dev)
        st["inf_w"], st["inf_h"] = dims.get(dev, (0, 0))
        cam_summary.append(st)
    cam_summary.sort(key=lambda c: c["device_id"])
    duration = None
    if session.get("start_ts"):
        end = session.get("end_ts") or (samples[-1]["ts"] if samples else session["start_ts"])
        duration = round(end - session["start_ts"], 1)
    return {"metrics": summary, "cameras": cam_summary,
            "duration_s": duration, "n_samples": len(samples)}


# ════════════════════════════════════════════════════════════════════════════
#  Sampler thread
# ════════════════════════════════════════════════════════════════════════════

class Sampler(threading.Thread):
    def __init__(self, store, fps_tracker, metrics_mgr):
        super().__init__(daemon=True)
        self.store = store
        self.fps = fps_tracker
        self.mm = metrics_mgr
        self.live = deque(maxlen=LIVE_BUFFER)
        self._live_lock = threading.Lock()

    def latest(self):
        with self._live_lock:
            return self.live[-1] if self.live else None

    def live_series(self, max_pts=300):
        with self._live_lock:
            data = list(self.live)
        if len(data) > max_pts:
            step = len(data) / max_pts
            data = [data[int(i * step)] for i in range(max_pts)]
        return data

    def run(self):
        names = self.store.get_camera_names()
        names_ts = time.monotonic()
        while not shutdown_event.wait(SAMPLE_INTERVAL):
            try:
                if time.monotonic() - names_ts > 5:
                    names = self.store.get_camera_names()
                    names_ts = time.monotonic()
                metrics = self.mm.sample()
                cams, total = self.fps.snapshot()
                for c in cams:
                    if c["id"] in names:
                        c["name"] = names[c["id"]]

                igpu = metrics.get("igpu") or {}
                npu = metrics.get("npu") or {}
                nv = metrics.get("nvidia") or {}
                cpu = metrics.get("cpu") or {}

                rec = {
                    "ts": time.time(),
                    "cpu_pct": cpu.get("cpu_pct"),
                    "ram_pct": cpu.get("ram_pct"),
                    "ram_used_mb": cpu.get("ram_used_mb"),
                    "ram_total_mb": cpu.get("ram_total_mb"),
                    "igpu_pct": igpu.get("util_pct"),
                    "igpu_mode": igpu.get("util_mode"),
                    "npu_pct": npu.get("util_pct"),
                    "npu_mem_mb": npu.get("mem_used_mb"),
                    "npu_freq_mhz": npu.get("freq_mhz"),
                    "nvidia_pct": nv.get("util_pct"),
                    "fps_total": total,
                    "n_cameras": len(cams),
                    "cameras": cams,
                    "metrics": metrics,
                }
                with self._live_lock:
                    self.live.append(rec)

                active = self.store.active_session()
                if active:
                    rec_db = dict(rec)
                    rec_db["fps_map"] = {c["id"]: {"fps": c["fps"], "name": c["name"],
                                                   "w": c["w"], "h": c["h"]} for c in cams}
                    self.store.add_sample(active["id"], rec_db)
            except Exception as e:
                logger.error("Sampler error: %s", e, exc_info=True)


# ════════════════════════════════════════════════════════════════════════════
#  HTTP handler
# ════════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    # Injected by main()
    store = None
    sampler = None
    fps = None
    metrics_mgr = None

    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ---- helpers ----
    def _send(self, code, body, ctype="application/json", download=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data), "application/json")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            return self._send(200, build_dashboard_html(), "text/html; charset=utf-8")

        if path == "/api/sources":
            src = self.metrics_mgr.sources()
            src["active_session"] = self.store.active_session()
            try:
                import metrics
                src["specs"] = metrics.get_hardware_specs()
                src["device"] = metrics.get_inference_device()
            except Exception:
                src["specs"] = {}
                src["device"] = "n/a"
            return self._json(src)

        if path == "/api/streams":
            try:
                import metrics
                return self._json(metrics.get_camera_streams())
            except Exception:
                return self._json({})

        if path == "/api/live":
            rec = self.sampler.latest() or {}
            return self._json({
                "now": rec,
                "series": self.sampler.live_series(int(qs.get("pts", ["300"])[0])),
                "active_session": self.store.active_session(),
            })

        if path == "/api/sessions":
            return self._json({"sessions": self.store.list_sessions()})

        if path == "/api/session":
            sid = int(qs.get("id", ["0"])[0])
            session = self.store.get_session(sid)
            if not session:
                return self._json({"error": "not found"}, 404)
            samples = self.store.get_samples(sid)
            return self._json({"session": session, "summary": build_summary(session, samples),
                               "samples": _downsample_samples(samples, 600)})

        if path == "/api/report.html":
            sid = int(qs.get("id", ["0"])[0])
            session = self.store.get_session(sid)
            if not session:
                return self._send(404, "session not found", "text/plain")
            samples = self.store.get_samples(sid)
            summary = build_summary(session, samples)
            fname = f"stress-report-{sid}-{_safe(session.get('name'))}.html"
            return self._send(200, build_report_html(session, summary, samples),
                              "text/html; charset=utf-8", download=fname)

        if path == "/api/report.csv":
            sid = int(qs.get("id", ["0"])[0])
            session = self.store.get_session(sid)
            if not session:
                return self._send(404, "session not found", "text/plain")
            samples = self.store.get_samples(sid)
            fname = f"stress-report-{sid}-{_safe(session.get('name'))}.csv"
            return self._send(200, build_csv(session, samples), "text/csv", download=fname)

        return self._send(404, "not found", "text/plain")

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/fps":
            self.fps.ingest(body)
            return self._json({"ok": True})

        if path == "/api/session/start":
            sources = self.metrics_mgr.sources()
            try:
                import metrics
                sources["device"] = metrics.get_inference_device()   # persist device used for this run
            except Exception:
                pass
            sid = self.store.start_session(body, sources)
            logger.info("Started session %d: %s", sid, body.get("name"))
            return self._json({"ok": True, "id": sid})

        if path == "/api/session/stop":
            active = self.store.active_session()
            if active:
                self.store.stop_session(active["id"])
                logger.info("Stopped session %d", active["id"])
                return self._json({"ok": True, "id": active["id"]})
            return self._json({"ok": False, "error": "no active session"})

        if path == "/api/session/delete":
            if isinstance(body.get("ids"), list):
                n = self.store.delete_sessions(body.get("ids"))
                logger.info("Deleted %d session(s)", n)
                return self._json({"ok": True, "deleted": n})
            sid = int(body.get("id") or 0)
            self.store.delete_session(sid)
            return self._json({"ok": True, "deleted": 1})

        if path == "/api/camera/name":
            self.store.set_camera_name(body.get("device_id", ""), body.get("name", ""))
            return self._json({"ok": True})

        return self._send(404, "not found", "text/plain")


def _safe(s):
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in (s or "run"))[:40]


def _downsample_samples(samples, max_pts):
    if len(samples) <= max_pts:
        return samples
    step = len(samples) / max_pts
    return [samples[int(i * step)] for i in range(max_pts)]


# ════════════════════════════════════════════════════════════════════════════
#  CSV export
# ════════════════════════════════════════════════════════════════════════════

def build_csv(session, samples):
    import io, csv
    # Collect the union of camera ids/names across the run for stable columns.
    cam_names = {}
    for s in samples:
        try:
            fm = json.loads(s.get("fps_json") or "{}")
        except Exception:
            fm = {}
        for dev, info in fm.items():
            cam_names[dev] = (info.get("name") if isinstance(info, dict) else None) or cam_names.get(dev, dev)
    cam_ids = sorted(cam_names)

    try:
        import metrics
        specs = metrics.get_hardware_specs()
    except Exception:
        specs = {}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# Stress report: {session.get('name')}",
                f"model={session.get('model')}",
                f"cameras={session.get('camera_count')}",
                f"resolution={session.get('resolution')}",
                f"host={session.get('hostname')}",
                f"cpu={specs.get('cpu') or 'n/a'}",
                f"gpu={specs.get('gpu') or 'n/a'}",
                f"npu={specs.get('npu') or 'n/a'}",
                f"ram={specs.get('ram') or 'n/a'}"])
    header = ["ts_iso", "ts_unix", "fps_total", "n_cameras",
              "cpu_pct", "ram_pct", "ram_used_mb", "ram_total_mb",
              "igpu_pct", "igpu_mode", "npu_pct", "npu_mem_mb", "npu_freq_mhz", "nvidia_pct"]
    header += [f"fps[{cam_names[c]}]" for c in cam_ids]
    w.writerow(header)
    for s in samples:
        try:
            fm = json.loads(s.get("fps_json") or "{}")
        except Exception:
            fm = {}
        iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["ts"]))
        row = [iso, round(s["ts"], 3), s.get("fps_total"), s.get("n_cameras"),
               s.get("cpu_pct"), s.get("ram_pct"), s.get("ram_used_mb"), s.get("ram_total_mb"),
               s.get("igpu_pct"), s.get("igpu_mode"), s.get("npu_pct"),
               s.get("npu_mem_mb"), s.get("npu_freq_mhz"), s.get("nvidia_pct")]
        for c in cam_ids:
            info = fm.get(c)
            row.append(info.get("fps") if isinstance(info, dict) else info)
        w.writerow(row)
    return buf.getvalue()


# ════════════════════════════════════════════════════════════════════════════
#  HTML (dashboard + report) — imported from a sibling module to keep this file
#  focused; falls back to inline minimal page if the template module is missing.
# ════════════════════════════════════════════════════════════════════════════

from ui_templates import build_dashboard_html, build_report_html  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
#  Lifecycle
# ════════════════════════════════════════════════════════════════════════════

def signal_handler(signum, _):
    logger.info("Signal %s received, shutting down.", signal.Signals(signum).name)
    shutdown_event.set()


def read_config():
    port = DEFAULT_PORT
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        lvl = cfg.get("common", "log_level", fallback="INFO")
        logging.getLogger().setLevel(getattr(logging, lvl.upper(), logging.INFO))
        port = cfg.getint("web_server", "port", fallback=DEFAULT_PORT)
        # Nx REST API creds for per-camera stream info (primary/secondary res+fps).
        # Defaults to the local server; override per-box in the [nx] section.
        import metrics
        metrics.set_nx_credentials(
            url=cfg.get("nx", "url", fallback="https://127.0.0.1:7001"),
            user=cfg.get("nx", "user", fallback="admin"),
            password=cfg.get("nx", "password", fallback="admin"),
        )
    except Exception as e:
        logger.warning("Config read error: %s", e)
    return port


def main():
    ap = argparse.ArgumentParser(description="Stress Dashboard web app")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    cfg_port = read_config()
    port = args.port or cfg_port

    store = StressStore(args.db)
    fps = FpsTracker()
    mm = MetricsManager()
    sampler = Sampler(store, fps, mm)
    sampler.start()

    Handler.store = store
    Handler.sampler = sampler
    Handler.fps = fps
    Handler.metrics_mgr = mm

    srcs = mm.sources()
    logger.info("Metric sources: %s", json.dumps(srcs))
    if srcs.get("intel_igpu") and srcs.get("igpu_mode") == "freq-proxy" and not srcs.get("root"):
        logger.warning("Intel iGPU utilisation is using the frequency proxy. "
                       "Run as root (sudo) for true engine utilisation via DRM fdinfo.")

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    logger.info("Stress Dashboard web app listening on http://0.0.0.0:%d (db=%s)", port, args.db)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    try:
        while not shutdown_event.wait(0.5):
            pass
    finally:
        logger.info("Shutting down HTTP server.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
