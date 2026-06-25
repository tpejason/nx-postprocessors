#!/usr/bin/env python3
"""
Web Dashboard Advance — standalone monitoring web application with multi-camera
support, time+class filtering, dark neon theme, heatmap, top-N, alerts, and
CSV export.

Receives detection data from the advance postprocessor via POST /api/ingest,
persists everything in SQLite, and serves an interactive dashboard at
http://localhost:<port>/.
"""
import os, sys, logging, logging.handlers, configparser, json, signal, random, sqlite3, time, argparse, io, csv
import subprocess, base64, tempfile, ssl, urllib.request
from datetime import datetime
from threading import Thread, Lock, Event
from collections import deque, defaultdict
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))

# ── Paths ─────────────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(script_location, "..", "etc", "plugin.web-dashboard-advance.ini")
_etc        = os.path.join(script_location, "..", "etc")
_log_dir    = _etc if os.path.exists(_etc) else script_location
LOG_FILE    = os.path.join(_log_dir, "plugin.web-dashboard-advance-app.log")
DEFAULT_DB  = os.path.join(_log_dir, "plugin.web-dashboard-advance.db")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - web-dashboard-advance-app - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=3),
    ]
)

DEFAULT_PORT          = 8112
DEFAULT_TIMELINE_CAP  = 50_000
DEFAULT_SCATTER_CAP   = 5_000
DEFAULT_FLUSH_SECS    = 10

shutdown_event = Event()
web_server     = None
store          = None
logger         = None

# Nx connection (for capturing camera thumbnails via the REST image endpoint;
# works even when the server has Basic/Digest auth disabled). Set in config().
NX_URL  = "https://127.0.0.1:7001"
NX_USER = "admin"
NX_PASS = ""


# ── Reservoir sampler ─────────────────────────────────────────────────────────

class ReservoirSampler:
    def __init__(self, capacity):
        self.capacity = capacity
        self.samples  = []
        self.n        = 0

    def restore(self, samples, n):
        self.samples = list(samples)
        # If the reservoir was not yet full when persisted, the stored total
        # count n can exceed the number of retained samples. Clamping n to the
        # sample count keeps the "not-yet-full" invariant so add() does not
        # always-accept new points and bias the reservoir.
        if len(self.samples) < self.capacity:
            self.n = min(n, len(self.samples))
        else:
            self.n = n

    def add(self, point):
        self.n += 1
        if len(self.samples) < self.capacity:
            idx = len(self.samples)
            self.samples.append(point)
            return idx
        idx = random.randint(0, self.n - 1)
        if idx < self.capacity:
            self.samples[idx] = point
            return idx
        return None

    def get(self):
        return list(self.samples)

    def clear(self):
        self.samples.clear()
        self.n = 0


# ── Detection store ───────────────────────────────────────────────────────────

class DetectionStore:
    """
    Thread-safe detection store backed by SQLite.
    Extended with multi-camera support, filtered queries, heatmap, top-N,
    and CSV export.
    """

    def __init__(self, db_path, timeline_cap, scatter_cap):
        self._lock         = Lock()
        self._db_path      = db_path
        self._timeline_cap = timeline_cap
        self._scatter_cap  = scatter_cap
        self._dirty        = False

        self._frame_ts = deque()  # timestamps of recent frames for FPS calc

        self._init_db()
        self._load()

    # ── DB helpers ─────────────────────────────────────────────────────────────

    def _connect(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # Now that the HTTP server is multi-threaded, concurrent requests may
        # write at the same time; wait for the write lock instead of failing
        # immediately with "database is locked".
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    ts        INTEGER NOT NULL,
                    camera_id TEXT    NOT NULL DEFAULT 'unknown',
                    total     REAL    NOT NULL,
                    pc        TEXT    NOT NULL,
                    PRIMARY KEY (ts, camera_id)
                );
                CREATE TABLE IF NOT EXISTS size_samples (
                    idx INTEGER PRIMARY KEY,
                    x   REAL NOT NULL,
                    y   REAL NOT NULL,
                    c   TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_samples (
                    idx       INTEGER PRIMARY KEY,
                    x         REAL NOT NULL,
                    y         REAL NOT NULL,
                    c         TEXT NOT NULL,
                    camera_id TEXT NOT NULL DEFAULT 'unknown',
                    ts        REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS cameras (
                    camera_id   TEXT PRIMARY KEY,
                    stream_name TEXT NOT NULL,
                    first_seen  REAL NOT NULL,
                    last_seen   REAL NOT NULL,
                    total_frames INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_timeline_ts        ON timeline(ts);
                CREATE INDEX IF NOT EXISTS idx_timeline_camera    ON timeline(camera_id);
                CREATE INDEX IF NOT EXISTS idx_position_camera    ON position_samples(camera_id);
                CREATE INDEX IF NOT EXISTS idx_position_ts        ON position_samples(ts);
            """)
            # Add thumbnail and rtsp_url columns if they don't exist
            try:
                conn.execute("ALTER TABLE cameras ADD COLUMN thumbnail TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE cameras ADD COLUMN rtsp_url TEXT")
            except Exception:
                pass

    def _load(self):
        with self._connect() as conn:
            def meta(key, default=None):
                row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return row[0] if row else default

            self.frame_count          = int(meta('frame_count', 0))
            self.total_objects        = int(meta('total_objects', 0))
            self.cumulative_per_class = defaultdict(int, json.loads(meta('cumulative_per_class', '{}')))
            self.current_per_class    = {}
            self._frame_ts            = deque()
            self.frame_width          = int(meta('frame_width', 0))
            self.frame_height         = int(meta('frame_height', 0))

            raw_start = meta('start_time')
            self.start_time = datetime.fromisoformat(raw_start) if raw_start else datetime.now()
            if not raw_start:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('start_time', ?)",
                             (self.start_time.isoformat(),))

            # In-memory timeline: keyed by (ts_bucket, camera_id)
            rows = conn.execute(
                "SELECT ts, camera_id, total, pc FROM timeline ORDER BY ts"
            ).fetchall()
            self._timeline = deque(
                [{'ts': r[0], 'camera_id': r[1], 'total': r[2], 'pc': json.loads(r[3])} for r in rows],
                maxlen=self._timeline_cap,
            )
            # Per-camera accumulation state: camera_id -> {cur_sec, cur_total, cur_pc}
            self._cam_state = {}

            def load_sampler(table, n_key):
                rows2 = conn.execute(
                    f"SELECT idx, x, y, c FROM {table} ORDER BY idx"
                ).fetchall()
                samples = [{'x': r[1], 'y': r[2], 'c': r[3]} for r in rows2]
                n       = int(meta(n_key, len(samples)))
                s       = ReservoirSampler(self._scatter_cap)
                s.restore(samples, n)
                return s

            self._sizes     = load_sampler('size_samples',     'size_sampler_n')
            self._positions = load_sampler('position_samples', 'position_sampler_n')

        logger.info("Loaded from DB: %d timeline buckets, %d size samples, %d position samples",
                    len(self._timeline), len(self._sizes.samples), len(self._positions.samples))

    # ── Write ──────────────────────────────────────────────────────────────────

    def add_frame(self, payload):
        """
        Process one detection frame from the advance postprocessor.
        Extracts camera_id from payload and routes to update().
        """
        camera_id   = str(payload.get('camera_id',   'unknown'))
        stream_name = str(payload.get('stream_name', camera_id))
        self.update(
            ts          = float(payload['ts']),
            counts      = payload.get('counts', {}),
            sizes       = payload.get('sizes', []),
            positions   = payload.get('positions', []),
            width       = int(payload.get('width', 0)),
            height      = int(payload.get('height', 0)),
            camera_id   = camera_id,
            stream_name = stream_name,
        )

    def update(self, ts, counts, sizes, positions, width=0, height=0,
               camera_id='unknown', stream_name=None):
        if stream_name is None:
            stream_name = camera_id

        buckets_to_write = []
        sz_updates       = []
        ps_updates       = []
        now_ts           = ts

        with self._lock:
            self.frame_count  += 1
            frame_total        = sum(counts.values())
            self.total_objects += frame_total
            self.current_per_class[camera_id] = dict(counts)
            now = time.time()
            self._frame_ts.append(now)
            cutoff = now - 10.0
            while self._frame_ts and self._frame_ts[0] < cutoff:
                self._frame_ts.popleft()

            for cls, cnt in counts.items():
                self.cumulative_per_class[cls] += cnt

            if width > 0 and height > 0:
                self.frame_width  = width
                self.frame_height = height

            # Per-camera per-second bucket accumulation.
            # cur_sec == None means the camera's last second was already settled
            # (flushed) below; the next frame re-opens a fresh bucket.
            sec = int(ts)
            cam = self._cam_state.get(camera_id)
            if cam is None or cam['cur_sec'] is None:
                self._cam_state[camera_id] = {
                    'cur_sec':   sec,
                    'cur_total': frame_total,
                    'cur_pc':    defaultdict(int, counts),
                }
            else:
                if cam['cur_sec'] != sec:
                    bucket = {
                        'ts':        cam['cur_sec'],
                        'camera_id': camera_id,
                        'total':     cam['cur_total'],
                        'pc':        dict(cam['cur_pc']),
                    }
                    buckets_to_write.append(bucket)
                    self._timeline.append(bucket)
                    cam['cur_sec']   = sec
                    cam['cur_total'] = frame_total
                    cam['cur_pc']    = defaultdict(int, counts)
                else:
                    cam['cur_total'] += frame_total
                    for cls, cnt in counts.items():
                        cam['cur_pc'][cls] += cnt

            # Settle OTHER cameras whose current second has definitively passed
            # (1s grace for late/out-of-order frames). Without this, a camera's
            # last second only flushes when that same camera ticks over, so a
            # slow/idle camera lags behind on the shared per-second grid and the
            # merged ("all") timeline under-reports / looks jagged. Flushing here
            # aligns every camera's completed seconds and persists idle cameras'
            # final second. get_timeline skips cur_sec==None, so no double-count.
            for cid, st in self._cam_state.items():
                if cid == camera_id or st['cur_sec'] is None:
                    continue
                if st['cur_sec'] < sec - 1:
                    settled = {
                        'ts':        st['cur_sec'],
                        'camera_id': cid,
                        'total':     st['cur_total'],
                        'pc':        dict(st['cur_pc']),
                    }
                    buckets_to_write.append(settled)
                    self._timeline.append(settled)
                    st['cur_sec'] = None

            for p in sizes:
                idx = self._sizes.add(p)
                if idx is not None:
                    sz_updates.append((idx, p))
            for p in positions:
                pp = dict(p)
                pp['camera_id'] = camera_id
                pp['ts']        = now_ts
                idx = self._positions.add(pp)
                if idx is not None:
                    ps_updates.append((idx, pp))

            self._dirty = True

        if buckets_to_write or sz_updates or ps_updates:
            self._write_incremental(buckets_to_write, sz_updates, ps_updates)

        # Update cameras table
        self._upsert_camera(camera_id, stream_name, ts)

    def _upsert_camera(self, camera_id, stream_name, ts):
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO cameras (camera_id, stream_name, first_seen, last_seen, total_frames)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(camera_id) DO UPDATE SET
                    stream_name  = excluded.stream_name,
                    last_seen    = excluded.last_seen,
                    total_frames = total_frames + 1
            """, (camera_id, stream_name, ts, ts))

    def _write_incremental(self, buckets, sz_updates, ps_updates):
        with self._connect() as conn:
            for b in buckets:
                conn.execute(
                    "INSERT OR REPLACE INTO timeline (ts, camera_id, total, pc) VALUES (?,?,?,?)",
                    (b['ts'], b['camera_id'], b['total'], json.dumps(b['pc'])),
                )
            if buckets:
                conn.execute(
                    "DELETE FROM timeline WHERE ts NOT IN "
                    "(SELECT ts FROM (SELECT DISTINCT ts FROM timeline ORDER BY ts DESC LIMIT ?))",
                    (self._timeline_cap,),
                )
            for idx, pt in sz_updates:
                conn.execute(
                    "INSERT OR REPLACE INTO size_samples (idx,x,y,c) VALUES (?,?,?,?)",
                    (idx, pt['x'], pt['y'], pt['c']),
                )
            for idx, pt in ps_updates:
                conn.execute(
                    "INSERT OR REPLACE INTO position_samples (idx,x,y,c,camera_id,ts) VALUES (?,?,?,?,?,?)",
                    (idx, pt['x'], pt['y'], pt['c'], pt.get('camera_id','unknown'), pt.get('ts',0)),
                )

    def flush_meta(self):
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            pairs = [
                ('frame_count',          str(self.frame_count)),
                ('total_objects',        str(self.total_objects)),
                ('cumulative_per_class', json.dumps(dict(self.cumulative_per_class))),
                ('frame_width',          str(self.frame_width)),
                ('frame_height',         str(self.frame_height)),
                ('size_sampler_n',       str(self._sizes.n)),
                ('position_sampler_n',   str(self._positions.n)),
            ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", pairs
            )

    def clear(self):
        with self._lock:
            self.frame_count          = 0
            self.total_objects        = 0
            self.current_per_class    = {}
            self.cumulative_per_class = defaultdict(int)
            self._frame_ts.clear()
            self._timeline.clear()
            self._cam_state.clear()
            self._sizes.clear()
            self._positions.clear()
            self.start_time = datetime.now()
            self._dirty     = False

        with self._connect() as conn:
            conn.executescript("""
                DELETE FROM timeline;
                DELETE FROM size_samples;
                DELETE FROM position_samples;
                DELETE FROM cameras;
                DELETE FROM meta;
            """)
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('start_time', ?)",
                         (self.start_time.isoformat(),))

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_stats(self):
        with self._lock:
            now = time.time()
            cutoff = now - 5.0
            fps = sum(1 for t in self._frame_ts if t >= cutoff) / 5.0
            return {
                'frame_count':   self.frame_count,
                'total_objects': self.total_objects,
                'fps':           round(fps, 1),
                'uptime':        (datetime.now() - self.start_time).total_seconds(),
            }

    def get_timeline(self, max_pts=500, camera_id=None):
        with self._lock:
            data = list(self._timeline)
            # Add in-progress buckets from cam_state
            for cid, cam in self._cam_state.items():
                if cam['cur_sec'] is not None:
                    data.append({
                        'ts':        cam['cur_sec'],
                        'camera_id': cid,
                        'total':     cam['cur_total'],
                        'pc':        dict(cam['cur_pc']),
                    })
        if camera_id and camera_id != 'all':
            data = [d for d in data if d.get('camera_id') == camera_id]
        # Merge same-ts entries across cameras if all cameras selected
        merged = {}
        for d in data:
            key = d['ts']
            if key not in merged:
                merged[key] = {'ts': d['ts'], 'total': 0, 'pc': defaultdict(float)}
            merged[key]['total'] += d['total']
            for cls, cnt in d['pc'].items():
                merged[key]['pc'][cls] += cnt
        result = sorted([
            {'ts': v['ts'], 'total': v['total'], 'pc': dict(v['pc'])}
            for v in merged.values()
        ], key=lambda x: x['ts'])
        return _downsample(result, max_pts)

    def get_scatter(self):
        with self._lock:
            return {
                'sizes':     self._sizes.get(),
                'positions': [{'x': p['x'], 'y': p['y'], 'c': p['c']} for p in self._positions.get()],
                'fw':        self.frame_width,
                'fh':        self.frame_height,
            }

    def get_distribution(self):
        with self._lock:
            return dict(self.cumulative_per_class)

    def get_cameras(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT camera_id, stream_name, last_seen, total_frames FROM cameras ORDER BY last_seen DESC"
            ).fetchall()
        return [
            {'camera_id': r[0], 'stream_name': r[1], 'last_seen': r[2], 'total_frames': r[3]}
            for r in rows
        ]

    def get_camera_thumbnail(self, camera_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT thumbnail FROM cameras WHERE camera_id = ?", (camera_id,)
            ).fetchone()
        if row and row[0]:
            return row[0]
        return None

    def set_camera_rtsp(self, camera_id, rtsp_url):
        with self._connect() as conn:
            conn.execute(
                "UPDATE cameras SET rtsp_url = ? WHERE camera_id = ?",
                (rtsp_url, camera_id)
            )

    def set_camera_thumbnail(self, camera_id, thumbnail_data):
        with self._connect() as conn:
            conn.execute(
                "UPDATE cameras SET thumbnail = ? WHERE camera_id = ?",
                (thumbnail_data, camera_id)
            )

    def get_filtered_timeline(self, start, end, classes=None, camera_id=None):
        """
        Return per-minute aggregated counts per class between start and end.
        Returns {labels: ['HH:MM', ...], datasets: [{label, data}, ...]}
        """
        params = [int(start), int(end)]
        where  = "WHERE ts >= ? AND ts <= ?"

        if camera_id and camera_id != 'all':
            where  += " AND camera_id = ?"
            params.append(camera_id)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT ts, pc FROM timeline {where} ORDER BY ts",
                params
            ).fetchall()

        # Aggregate per minute
        minute_data = defaultdict(lambda: defaultdict(float))
        all_classes = set()
        for ts_val, pc_json in rows:
            pc = json.loads(pc_json)
            minute_key = (ts_val // 60) * 60
            for cls, cnt in pc.items():
                if classes and cls not in classes:
                    continue
                minute_data[minute_key][cls] += cnt
                all_classes.add(cls)

        if not minute_data:
            return {'labels': [], 'datasets': []}

        sorted_minutes = sorted(minute_data.keys())
        labels   = [datetime.fromtimestamp(m).strftime('%H:%M') for m in sorted_minutes]
        datasets = []
        for cls in sorted(all_classes):
            datasets.append({
                'label': cls,
                'data':  [round(minute_data[m].get(cls, 0), 1) for m in sorted_minutes],
            })
        return {'labels': labels, 'datasets': datasets}

    def get_filtered_stats(self, start, end, classes=None, camera_id=None):
        """Return total count per class for the period."""
        params = [int(start), int(end)]
        where  = "WHERE ts >= ? AND ts <= ?"

        if camera_id and camera_id != 'all':
            where  += " AND camera_id = ?"
            params.append(camera_id)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT pc FROM timeline {where}",
                params
            ).fetchall()

        totals = defaultdict(float)
        for (pc_json,) in rows:
            pc = json.loads(pc_json)
            for cls, cnt in pc.items():
                if classes and cls not in classes:
                    continue
                totals[cls] += cnt

        return {cls: round(v) for cls, v in totals.items()}

    def get_heatmap(self, camera_id=None, start=None, end=None):
        """Return list of {x, y, c} from position_samples filtered by camera and time."""
        params = []
        clauses = []

        if camera_id and camera_id != 'all':
            clauses.append("camera_id = ?")
            params.append(camera_id)
        if start is not None:
            clauses.append("ts >= ?")
            params.append(float(start))
        if end is not None:
            clauses.append("ts <= ?")
            params.append(float(end))

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT x, y, c FROM position_samples {where} LIMIT 20000",
                params
            ).fetchall()

        with self._lock:
            fw = self.frame_width  or 1920
            fh = self.frame_height or 1080

        return {
            'points': [{'x': r[0], 'y': r[1], 'c': r[2]} for r in rows],
            'fw':     fw,
            'fh':     fh,
        }

    def get_all_heatmaps(self, start=None, end=None):
        """Return per-camera heatmap data with thumbnails, keyed by camera_id."""
        with self._connect() as conn:
            cam_rows = conn.execute(
                "SELECT camera_id, stream_name, thumbnail FROM cameras ORDER BY first_seen"
            ).fetchall()

        result = {}
        for (cam_id, stream_name, thumbnail) in cam_rows:
            data = self.get_heatmap(camera_id=cam_id, start=start, end=end)
            data['stream_name'] = stream_name or cam_id
            data['thumbnail']   = thumbnail
            result[cam_id] = data
        return result

    def delete_camera(self, camera_id):
        """Remove a camera and all its data from the dashboard (in-memory + DB).
        If the camera is still selected as a postprocessor in Nx and keeps sending
        frames, it will re-appear on the next ingest (the user must remove it in Nx)."""
        with self._lock:
            self._cam_state.pop(camera_id, None)
            self.current_per_class.pop(camera_id, None)
            self._timeline = deque(
                (b for b in self._timeline if b.get('camera_id') != camera_id),
                maxlen=self._timeline_cap,
            )
            self._positions.samples = [
                p for p in self._positions.samples if p.get('camera_id') != camera_id
            ]
            self._dirty = True
        with self._connect() as conn:
            conn.execute("DELETE FROM timeline WHERE camera_id = ?",         (camera_id,))
            conn.execute("DELETE FROM position_samples WHERE camera_id = ?", (camera_id,))
            conn.execute("DELETE FROM cameras WHERE camera_id = ?",          (camera_id,))
        logger.info("Deleted camera %s from dashboard", camera_id)

    def get_top_n(self, n=5, start=None, end=None, camera_id=None, classes=None):
        """Return top N classes by total count for the given period."""
        now = time.time()
        if start is None:
            start = now - 86400
        if end is None:
            end = now

        params = [int(start), int(end)]
        where  = "WHERE ts >= ? AND ts <= ?"
        if camera_id and camera_id != 'all':
            where += " AND camera_id = ?"
            params.append(camera_id)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT pc FROM timeline {where}", params
            ).fetchall()

        totals = defaultdict(float)
        for (pc_json,) in rows:
            pc = json.loads(pc_json)
            for cls, cnt in pc.items():
                if classes and cls not in classes:
                    continue
                totals[cls] += cnt

        sorted_items = sorted(totals.items(), key=lambda x: x[1], reverse=True)
        return [{'class': cls, 'count': round(cnt)} for cls, cnt in sorted_items[:n]]

    def export_csv(self, start, end, classes=None, camera_id=None):
        """Return CSV string of timeline data."""
        params = [int(start), int(end)]
        where  = "WHERE ts >= ? AND ts <= ?"

        if camera_id and camera_id != 'all':
            where  += " AND camera_id = ?"
            params.append(camera_id)

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT ts, camera_id, pc FROM timeline {where} ORDER BY ts",
                params
            ).fetchall()

        buf = io.StringIO()
        # Use csv.writer so camera_id / class names containing commas, quotes,
        # or newlines are properly quoted instead of corrupting the output.
        # lineterminator='\n' preserves the original response bytes/encoding.
        writer = csv.writer(buf, lineterminator='\n')
        writer.writerow(["timestamp", "camera_id", "class", "count"])
        for ts_val, cam_id, pc_json in rows:
            dt_str = datetime.fromtimestamp(ts_val).strftime('%Y-%m-%d %H:%M:%S')
            pc = json.loads(pc_json)
            for cls, cnt in pc.items():
                if classes and cls not in classes:
                    continue
                writer.writerow([dt_str, cam_id, cls, round(cnt)])
        return buf.getvalue()


def _downsample(data, max_pts):
    if len(data) <= max_pts:
        return data
    bsz = len(data) / max_pts
    out = []
    for i in range(max_pts):
        bucket = data[int(i * bsz): int((i + 1) * bsz)]
        if not bucket:
            continue
        classes = {c for b in bucket for c in b['pc']}
        out.append({
            'ts':    bucket[len(bucket) // 2]['ts'],
            'total': round(sum(b['total'] for b in bucket) / len(bucket), 1),
            'pc':    {c: round(sum(b['pc'].get(c, 0) for b in bucket) / len(bucket), 1)
                      for c in classes},
        })
    return out


# ── HTTP server ────────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)

        def qs1(k, default=None):
            v = qs.get(k)
            return v[0] if v else default

        def qs_list(k):
            v = qs.get(k)
            if not v:
                return None
            items = []
            for item in v:
                items.extend(x.strip() for x in item.split(',') if x.strip())
            return items if items else None

        path = p.path

        # ── Static page ───────────────────────────────────────────────────────
        if path == '/':
            self._html(_build_html())
            return

        # ── Basic API (backward compat + camera filter) ───────────────────────
        if path == '/api/stats':
            self._json(store.get_stats())
            return

        if path == '/api/timeline':
            cam = qs1('camera')
            pts = max(1, min(int(qs1('points', '500')), 5000))
            self._json(store.get_timeline(pts, camera_id=cam))
            return

        if path == '/api/scatter':
            self._json(store.get_scatter())
            return

        if path == '/api/distribution':
            self._json(store.get_distribution())
            return

        # ── New API ───────────────────────────────────────────────────────────
        if path == '/api/cameras':
            self._json(store.get_cameras())
            return

        if path == '/api/camera_thumbnail':
            cam_id = qs1('camera_id')
            if not cam_id:
                self._json({'data': None})
                return
            thumbnail = store.get_camera_thumbnail(cam_id)
            self._json({'data': thumbnail})
            return

        if path == '/api/filtered/timeline':
            now   = time.time()
            start = float(qs1('start', now - 3600))
            end   = float(qs1('end',   now))
            cls   = qs_list('classes')
            cam   = qs1('camera')
            self._json(store.get_filtered_timeline(start, end, classes=cls, camera_id=cam))
            return

        if path == '/api/filtered/stats':
            now   = time.time()
            start = float(qs1('start', now - 3600))
            end   = float(qs1('end',   now))
            cls   = qs_list('classes')
            cam   = qs1('camera')
            self._json(store.get_filtered_stats(start, end, classes=cls, camera_id=cam))
            return

        if path == '/api/filtered/donut':
            now   = time.time()
            start = float(qs1('start', now - 3600))
            end   = float(qs1('end',   now))
            cls   = qs_list('classes')
            cam   = qs1('camera')
            stats = store.get_filtered_stats(start, end, classes=cls, camera_id=cam)
            sorted_items = sorted(stats.items(), key=lambda x: x[1], reverse=True)
            self._json({
                'labels': [x[0] for x in sorted_items],
                'data':   [x[1] for x in sorted_items],
            })
            return

        if path == '/api/heatmap':
            cam   = qs1('camera')
            start = qs1('start')
            end   = qs1('end')
            self._json(store.get_heatmap(
                camera_id = cam,
                start     = float(start) if start else None,
                end       = float(end)   if end   else None,
            ))
            return

        if path == '/api/heatmap_all':
            start = qs1('start')
            end   = qs1('end')
            self._json(store.get_all_heatmaps(
                start = float(start) if start else None,
                end   = float(end)   if end   else None,
            ))
            return

        if path == '/api/top':
            n         = int(qs1('n', '5'))
            start_str = qs1('start')
            end_str   = qs1('end')
            cam       = qs1('camera')
            cls       = qs_list('classes')
            self._json(store.get_top_n(
                n         = n,
                start     = float(start_str) if start_str else None,
                end       = float(end_str)   if end_str   else None,
                camera_id = cam if cam else None,
                classes   = cls if cls else None,
            ))
            return

        if path == '/api/export.csv':
            now   = time.time()
            start = float(qs1('start', now - 3600))
            end   = float(qs1('end',   now))
            cls   = qs_list('classes')
            cam   = qs1('camera')
            csv_data = store.export_csv(start, end, classes=cls, camera_id=cam)
            b = csv_data.encode()
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="detections.csv"')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', len(b))
            self.end_headers()
            self.wfile.write(b)
            return

        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        if self.path == '/api/ingest':
            try:
                data = json.loads(body)
                if 'camera_id' in data:
                    store.add_frame(data)
                else:
                    store.update(
                        ts        = float(data['ts']),
                        counts    = data.get('counts', {}),
                        sizes     = data.get('sizes', []),
                        positions = data.get('positions', []),
                        width     = int(data.get('width', 0)),
                        height    = int(data.get('height', 0)),
                    )
                self._json({'ok': True})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                logger.warning("Bad ingest payload: %s", e)
                self.send_error(400, str(e))

        elif self.path == '/api/clear':
            store.clear()
            self._json({'ok': True})

        elif self.path == '/api/camera_rtsp':
            try:
                data     = json.loads(body)
                cam_id   = str(data.get('camera_id', ''))
                rtsp_url = str(data.get('rtsp_url', ''))
                if not cam_id or not rtsp_url:
                    self.send_error(400, 'camera_id and rtsp_url required')
                    return
                # Reject anything that is not an rtsp:// URL to avoid ffmpeg
                # option-injection via a leading "-" being treated as a flag.
                if not rtsp_url.startswith('rtsp://'):
                    self.send_error(400, 'rtsp_url must start with rtsp://')
                    return
                store.set_camera_rtsp(cam_id, rtsp_url)
                has_thumbnail = False
                try:
                    tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
                    tmp.close()
                    result = subprocess.run(
                        ['ffmpeg', '-rtsp_transport', 'tcp', '-i', rtsp_url,
                         '-vframes', '1', '-q:v', '2', '-y', tmp.name],
                        capture_output=True, timeout=15
                    )
                    if result.returncode == 0:
                        with open(tmp.name, 'rb') as f:
                            thumb_data = 'data:image/jpeg;base64,' + base64.b64encode(f.read()).decode()
                        store.set_camera_thumbnail(cam_id, thumb_data)
                        has_thumbnail = True
                    try:
                        os.unlink(tmp.name)
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning("Thumbnail capture failed for %s: %s", cam_id, e)
                self._json({'ok': True, 'has_thumbnail': has_thumbnail})
            except (json.JSONDecodeError, ValueError) as e:
                self.send_error(400, str(e))

        elif self.path == '/api/camera_snapshot':
            # Capture a thumbnail directly from Nx by camera_id (no RTSP URL
            # needed). Uses the REST image endpoint with a bearer token, so it
            # works even when the Nx server has Basic/Digest auth disabled.
            try:
                data   = json.loads(body)
                cam_id = str(data.get('camera_id', ''))
                if not cam_id:
                    self.send_error(400, 'camera_id required')
                    return
                ok = _capture_nx_thumbnail(cam_id)
                self._json({'ok': True, 'has_thumbnail': ok})
            except (json.JSONDecodeError, ValueError) as e:
                self.send_error(400, str(e))

        elif self.path == '/api/camera_delete':
            try:
                data   = json.loads(body)
                cam_id = str(data.get('camera_id', ''))
                if not cam_id:
                    self.send_error(400, 'camera_id required')
                    return
                store.delete_camera(cam_id)
                self._json({'ok': True})
            except (json.JSONDecodeError, ValueError) as e:
                self.send_error(400, str(e))

        else:
            self.send_error(404)

    def _html(self, content):
        b = content.encode()
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.send_header('Content-Length', len(b))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, data):
        b = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(b))
        self.end_headers()
        self.wfile.write(b)


class _ReusableHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads      = True

    def handle_error(self, request, client_address):
        if issubclass(sys.exc_info()[0], BrokenPipeError):
            return
        logger.error("Unhandled error for request from %s", client_address, exc_info=True)


# ── Web server ─────────────────────────────────────────────────────────────────

def start_web_server(port):
    global web_server
    try:
        web_server = _ReusableHTTPServer(('0.0.0.0', port), DashboardHandler)
    except Exception as e:
        logger.error("Could not bind to port %d: %s", port, e)
        raise
    def run():
        try:
            web_server.serve_forever()
        except Exception as e:
            logger.error("Web server error: %s", e, exc_info=True)
    Thread(target=run, daemon=True, name="http").start()
    logger.info("Dashboard running at http://localhost:%d", port)


# ── Background flush thread ────────────────────────────────────────────────────

def start_flush_thread(interval_secs):
    def run():
        while not shutdown_event.wait(timeout=interval_secs):
            try:
                store.flush_meta()
            except Exception as e:
                logger.error("Flush error: %s", e, exc_info=True)
        try:
            store.flush_meta()
        except Exception as e:
            logger.error("Final flush error: %s", e, exc_info=True)
    Thread(target=run, daemon=True, name="flush").start()


# ── Server lifecycle ───────────────────────────────────────────────────────────

def signal_handler(signum, _):
    logger.info("Signal %s received, shutting down.", signal.Signals(signum).name)
    shutdown_event.set()
    if web_server:
        web_server.shutdown()


def set_log_level(level):
    try:
        logger.setLevel(getattr(logging, level.upper()))
    except Exception as e:
        logger.error("Log level error: %s", e, exc_info=True)


def config():
    logger.info("Reading config from: %s", CONFIG_FILE)
    port, tc, sc, db_path = DEFAULT_PORT, DEFAULT_TIMELINE_CAP, DEFAULT_SCATTER_CAP, DEFAULT_DB
    try:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        set_log_level(cfg.get('common', 'log_level', fallback='INFO'))
        if 'web_server' in cfg:
            port    = cfg.getint('web_server', 'port',              fallback=DEFAULT_PORT)
            tc      = cfg.getint('web_server', 'timeline_capacity', fallback=DEFAULT_TIMELINE_CAP)
            sc      = cfg.getint('web_server', 'scatter_capacity',  fallback=DEFAULT_SCATTER_CAP)
            db_path = cfg.get   ('web_server', 'db_path',           fallback=DEFAULT_DB)
        if 'nx' in cfg:
            global NX_URL, NX_USER, NX_PASS
            NX_URL  = cfg.get('nx', 'url',      fallback=NX_URL).rstrip('/')
            NX_USER = cfg.get('nx', 'user',     fallback=NX_USER)
            NX_PASS = cfg.get('nx', 'password', fallback=NX_PASS)
    except Exception as e:
        logger.error("Config error: %s", e, exc_info=True)

    if not (1 <= port <= 65535):
        logger.warning("Invalid port %d, using default %d", port, DEFAULT_PORT)
        port = DEFAULT_PORT
    if not (1 <= tc <= 1_000_000):
        logger.warning("Invalid timeline_capacity %d, using default %d", tc, DEFAULT_TIMELINE_CAP)
        tc = DEFAULT_TIMELINE_CAP
    if not (1 <= sc <= 1_000_000):
        logger.warning("Invalid scatter_capacity %d, using default %d", sc, DEFAULT_SCATTER_CAP)
        sc = DEFAULT_SCATTER_CAP
    return port, tc, sc, db_path


# ── Nx thumbnail capture (REST image endpoint, bearer-token auth) ───────────────

def _nx_token():
    """Log in to Nx and return a bearer token. Works when Basic/Digest auth is
    disabled on the server (that path is what breaks the ffmpeg rtsp:// URL)."""
    ctx  = ssl._create_unverified_context()
    body = json.dumps({"username": NX_USER, "password": NX_PASS, "setCookie": False}).encode()
    req  = urllib.request.Request(NX_URL + "/rest/v3/login/sessions", data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
        return json.load(r)["token"]


def _capture_nx_thumbnail(camera_id):
    """Fetch one JPEG for camera_id from Nx (GET /rest/v3/devices/{id}/image) and
    store it as a data URL in cameras.thumbnail. Returns True on success, False if
    Nx returns no frame (HTTP 204 — e.g. a stream with no decodable still) or on error."""
    if not NX_PASS:
        logger.warning("Nx snapshot: no [nx] password configured")
        return False
    try:
        ctx = ssl._create_unverified_context()
        tok = _nx_token()
        req = urllib.request.Request(f"{NX_URL}/rest/v3/devices/{camera_id}/image",
                                     headers={"Authorization": "Bearer " + tok})
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            jpeg = r.read()
        if not jpeg:
            logger.info("Nx snapshot: no frame available for %s (HTTP %s)", camera_id, r.status)
            return False
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        store.set_camera_thumbnail(camera_id, data_url)
        logger.info("Nx snapshot: stored %d bytes for %s", len(jpeg), camera_id)
        return True
    except Exception as e:
        logger.warning("Nx snapshot failed for %s: %s", camera_id, e)
        return False


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

def _build_html():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NX AI Vision Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<style>
:root {
  --bg: #0a0a0f;
  --card: #12121a;
  --border: #1e1e2e;
  --neon-blue: #00d4ff;
  --neon-green: #00ff88;
  --neon-pink: #ff0066;
  --neon-yellow: #ffdd00;
  --neon-orange: #ff8800;
  --text: #e0e0ff;
  --text-dim: #666677;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;background:var(--bg);color:var(--text);min-height:100vh;padding:16px}
.page{max-width:1440px;margin:0 auto}

/* Header */
.header{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 24px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
.header h1{font-size:22px;font-weight:700;color:var(--neon-blue);letter-spacing:1px;text-shadow:0 0 20px rgba(0,212,255,.4)}
.header p{font-size:12px;color:var(--text-dim);margin-top:3px;letter-spacing:.5px}

/* Live badge */
.badge{display:inline-flex;align-items:center;gap:6px;padding:7px 16px;border-radius:20px;font-size:13px;font-weight:700;letter-spacing:1px}
.badge-live{background:rgba(0,255,136,.08);color:var(--neon-green);border:1px solid rgba(0,255,136,.3)}
.badge-filtered{background:rgba(0,212,255,.08);color:var(--neon-blue);border:1px solid rgba(0,212,255,.3)}
.badge-offline{background:rgba(102,102,119,.1);color:var(--text-dim);border:1px solid var(--border)}
.pulse-dot{width:8px;height:8px;border-radius:50%;background:var(--neon-green)}
.badge-live .pulse-dot{animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.7)}}
.offline-dot{width:8px;height:8px;border-radius:50%;background:var(--text-dim)}
.filter-dot{width:8px;height:8px;border-radius:50%;background:var(--neon-blue)}

/* Alert banner */
#alert-banner{display:none;background:rgba(255,0,102,.12);border:1px solid var(--neon-pink);border-radius:8px;padding:10px 16px;margin-bottom:12px;color:var(--neon-pink);font-size:13px;font-weight:600}

/* Camera selector */
.cam-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:16px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px}
.cam-label{font-size:11px;color:var(--text-dim);letter-spacing:.5px;text-transform:uppercase;margin-right:4px}
.cam-btn{padding:5px 14px;border-radius:20px;border:1px solid var(--border);background:transparent;color:var(--text-dim);cursor:pointer;font-size:12px;font-weight:600;transition:all .2s;letter-spacing:.5px}
.cam-btn:hover{border-color:var(--neon-blue);color:var(--neon-blue)}
.cam-btn.active{border-color:var(--neon-blue);color:var(--neon-blue);background:rgba(0,212,255,.08);box-shadow:0 0 10px rgba(0,212,255,.2)}
.cam-gear{font-size:10px;opacity:.5;margin-left:4px;cursor:pointer;vertical-align:middle}
.cam-gear:hover{opacity:1}

/* RTSP inline form */
.rtsp-form{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:12px;display:none;gap:8px;align-items:center;flex-wrap:wrap}
.rtsp-form.open{display:flex}
.rtsp-input{background:#0d0d15;border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 10px;font-size:12px;outline:none;flex:1;min-width:220px}
.rtsp-input:focus{border-color:var(--neon-blue)}
.btn-rtsp{padding:6px 16px;border-radius:6px;border:1px solid var(--neon-blue);background:rgba(0,212,255,.1);color:var(--neon-blue);cursor:pointer;font-size:12px;font-weight:700}
.rtsp-msg{font-size:11px;color:var(--text-dim)}

/* Filter panel */
.filter-panel{background:var(--card);border:1px solid var(--neon-blue);border-radius:10px;padding:20px;margin-bottom:16px;box-shadow:0 0 30px rgba(0,212,255,.05)}
.filter-panel .panel-title{color:var(--neon-blue)}
.filter-row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.filter-label{font-size:11px;color:var(--text-dim);letter-spacing:.5px;min-width:50px}
input[type=datetime-local],select{background:#0d0d15;border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 10px;font-size:12px;outline:none;transition:border-color .2s}
input[type=datetime-local]:focus,select:focus{border-color:var(--neon-blue)}
.checkbox-group{display:flex;gap:8px;flex-wrap:wrap}
.checkbox-group label{display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer;color:var(--text-dim)}
.checkbox-group input[type=checkbox]{accent-color:var(--neon-blue);cursor:pointer}
.btn-apply{padding:8px 22px;border-radius:6px;border:1px solid var(--neon-blue);background:rgba(0,212,255,.1);color:var(--neon-blue);cursor:pointer;font-size:13px;font-weight:700;letter-spacing:.5px;transition:all .2s}
.btn-apply:hover{background:rgba(0,212,255,.2);box-shadow:0 0 15px rgba(0,212,255,.2)}
.btn-export{padding:8px 22px;border-radius:6px;border:1px solid var(--neon-green);background:rgba(0,255,136,.08);color:var(--neon-green);cursor:pointer;font-size:13px;font-weight:700;letter-spacing:.5px;transition:all .2s}
.btn-export:hover{background:rgba(0,255,136,.18);box-shadow:0 0 15px rgba(0,255,136,.2)}
.btn-clear{padding:8px 22px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text-dim);cursor:pointer;font-size:13px;font-weight:600;transition:all .2s}
.btn-clear:hover{border-color:var(--text-dim);color:var(--text)}
.btn-clear-filter{padding:8px 22px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text-dim);cursor:pointer;font-size:13px;font-weight:600;transition:all .2s}
.btn-clear-filter:hover{border-color:var(--neon-pink);color:var(--neon-pink)}

/* KPI cards */
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
@media(max-width:700px){.kpi-row{grid-template-columns:repeat(2,1fr)}}
.kpi-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px 20px;transition:border-color .3s,box-shadow .3s}
.kpi-card.alert-flash{border-color:var(--neon-pink)!important;box-shadow:0 0 20px rgba(255,0,102,.3)!important;animation:flash-border 1s ease-in-out 3}
@keyframes flash-border{0%,100%{border-color:var(--neon-pink);box-shadow:0 0 20px rgba(255,0,102,.3)}50%{border-color:var(--border);box-shadow:none}}
.kpi-label{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.kpi-value{font-size:32px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.kpi-value.blue{color:var(--neon-blue);text-shadow:0 0 15px rgba(0,212,255,.3)}
.kpi-value.green{color:var(--neon-green);text-shadow:0 0 15px rgba(0,255,136,.3)}
.kpi-value.pink{color:var(--neon-pink);text-shadow:0 0 15px rgba(255,0,102,.3)}
.kpi-value.yellow{color:var(--neon-yellow);text-shadow:0 0 15px rgba(255,221,0,.3)}

/* Count cards row */
#count-cards-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.count-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 20px;min-width:120px;text-align:center;transition:border-color .3s}
.count-card:hover{border-color:var(--neon-blue)}
.count-card-label{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.count-card-value{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums}

/* Top 5 */
.top5-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px 20px;margin-bottom:16px}
.top5-title{font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px}
.top5-list{display:flex;flex-direction:column;gap:10px}
.top5-item{display:flex;align-items:center;gap:10px}
.top5-rank{font-size:11px;color:var(--text-dim);width:18px;text-align:right;font-weight:700}
.top5-name{font-size:13px;font-weight:600;width:100px;flex-shrink:0;color:var(--text)}
.top5-bar-wrap{flex:1;height:6px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden}
.top5-bar{height:100%;border-radius:3px;transition:width .6s ease}
.top5-count{font-size:13px;font-weight:700;width:60px;text-align:right;font-variant-numeric:tabular-nums}

/* Panels */
.panel{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px}
.panel-title{font-size:11px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px}
.chart-wrap{position:relative}

.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}
.two-col .panel{margin-bottom:0}

/* Heatmap */
.heatmap-filter-bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--border)}
.hm-time-btn{padding:4px 14px;border-radius:14px;border:1px solid var(--border);background:transparent;color:var(--text-dim);cursor:pointer;font-size:11px;font-weight:600;transition:all .2s}
.hm-time-btn.active{border-color:var(--neon-pink);color:var(--neon-pink);background:rgba(255,0,102,.08)}
.heatmap-cls-filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.heatmap-cls-btn{padding:4px 12px;border-radius:14px;border:1px solid var(--border);background:transparent;color:var(--text-dim);cursor:pointer;font-size:11px;font-weight:600;transition:all .2s}
.heatmap-cls-btn.active{border-color:var(--neon-blue);color:var(--neon-blue);background:rgba(0,212,255,.08)}
.heatmap-refresh-btn{padding:4px 14px;border-radius:14px;border:1px solid var(--neon-green);background:rgba(0,255,136,.06);color:var(--neon-green);cursor:pointer;font-size:11px;font-weight:700;margin-left:auto}
.heatmap-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;margin-top:4px}
@media(max-width:660px){.heatmap-grid{grid-template-columns:1fr}}
.heatmap-cam-card{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;padding:10px}
.heatmap-cam-card{position:relative}
.heatmap-cam-del{position:absolute;top:6px;right:6px;z-index:5;width:22px;height:22px;border-radius:50%;border:1px solid var(--border);background:rgba(10,10,15,.72);color:var(--text-dim);font-size:15px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0}
.heatmap-cam-del:hover{background:#c62828;color:#fff;border-color:#c62828}
.heatmap-cam-title{font-size:10px;font-weight:700;color:var(--text-dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.heatmap-cam-canvas{width:100%;border-radius:5px;display:block}

/* Controls bar */
.ctrl-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:16px}

/* Scrollbar */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>
<div class="page">

<!-- Alert banner -->
<div id="alert-banner"></div>

<!-- Header -->
<div class="header">
  <div>
    <h1>NX AI Vision Dashboard</h1>
    <p>Real-time multi-camera monitoring &nbsp;&middot;&nbsp; NX AI Manager</p>
  </div>
  <span class="badge badge-live" id="status-badge">
    <span class="pulse-dot" id="status-dot"></span>
    <span id="status-text">LIVE</span>
  </span>
</div>

<!-- Camera selector -->
<div class="cam-bar">
  <span class="cam-label">Camera</span>
  <button class="cam-btn active" data-cam="all" onclick="selectCamera('all', this)">ALL CAMERAS</button>
  <span id="cam-extra-btns"></span>
</div>

<!-- RTSP inline form -->
<div class="rtsp-form" id="rtsp-form">
  <span style="font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px">RTSP URL for</span>
  <span id="rtsp-cam-label" style="font-size:12px;color:var(--neon-blue);font-weight:700"></span>
  <input class="rtsp-input" id="rtsp-url-input" placeholder="rtsp://user:pass@192.168.1.x/stream">
  <button class="btn-rtsp" onclick="submitRtsp()">Set &amp; Capture</button>
  <span class="rtsp-msg" id="rtsp-msg"></span>
  <button class="btn-clear" style="padding:6px 14px;font-size:12px" onclick="closeRtspForm()">Close</button>
</div>

<!-- Filter panel (moved here, above KPI cards) -->
<div class="filter-panel">
  <div class="panel-title">Filter &amp; Analysis</div>
  <div class="filter-row">
    <span class="filter-label">From</span>
    <input type="datetime-local" id="f-start">
    <span class="filter-label">To</span>
    <input type="datetime-local" id="f-end">
  </div>
  <div class="filter-row">
    <span class="filter-label">Camera</span>
    <select id="f-camera">
      <option value="all">All Cameras</option>
    </select>
    <span class="filter-label" style="margin-left:12px">Classes</span>
    <div class="checkbox-group" id="f-classes"></div>
  </div>
  <div class="filter-row">
    <button class="btn-apply" onclick="applyFilter()">Apply Filter</button>
    <button class="btn-clear-filter" onclick="clearFilter()">Clear Filter</button>
    <button class="btn-export" onclick="exportCSV()">Export CSV</button>
  </div>
</div>

<!-- KPI row -->
<div class="kpi-row">
  <div class="kpi-card" id="kpi-frames">
    <div class="kpi-label">Frames Processed</div>
    <div class="kpi-value blue" id="v-frames">0</div>
  </div>
  <div class="kpi-card" id="kpi-objects">
    <div class="kpi-label">Total Objects</div>
    <div class="kpi-value green" id="v-objects">0</div>
  </div>
  <div class="kpi-card" id="kpi-current">
    <div class="kpi-label">Inference FPS</div>
    <div class="kpi-value pink" id="v-current">0.0</div>
  </div>
  <div class="kpi-card" id="kpi-uptime">
    <div class="kpi-label">Uptime</div>
    <div class="kpi-value yellow" id="v-uptime">0s</div>
  </div>
</div>

<!-- Count cards per class -->
<div id="count-cards-row"></div>

<!-- Top 5 -->
<div class="top5-card">
  <div class="top5-title" id="top5-title">Top Detected Classes &mdash; Last 24h</div>
  <div class="top5-list" id="top5-list">
    <div style="color:var(--text-dim);font-size:12px">No data yet</div>
  </div>
</div>

<!-- Controls -->
<div class="ctrl-bar">
  <button class="btn-clear" onclick="resetZoom()">Reset Zoom</button>
  <button class="btn-clear" id="btn-pause" onclick="togglePause()">Pause</button>
  <button class="btn-clear" onclick="clearData()">Clear Data</button>
</div>

<!-- Live timeline -->
<div class="panel">
  <div class="panel-title" id="timeline-panel-title">Live Object Timeline</div>
  <div class="chart-wrap" style="height:300px">
    <canvas id="timelineChart"></canvas>
  </div>
</div>

<!-- Distribution + Pie chart (replaced size scatter) -->
<div class="two-col">
  <div class="panel">
    <div class="panel-title" id="dist-panel-title">Cumulative Distribution</div>
    <div class="chart-wrap" id="distWrap" style="height:220px">
      <canvas id="distChart"></canvas>
    </div>
  </div>
  <div class="panel">
    <div class="panel-title" id="pie-panel-title">Class Distribution</div>
    <div class="chart-wrap" style="height:220px">
      <canvas id="pieChart"></canvas>
    </div>
  </div>
</div>

<!-- Per-camera heatmaps -->
<div class="panel">
  <div class="panel-title">Position Heatmap — Per Camera</div>
  <div class="heatmap-filter-bar" id="heatmap-time-bar">
    <button class="hm-time-btn active" data-range="all"  onclick="setHeatmapRange('all',this)">ALL</button>
    <button class="hm-time-btn"        data-range="week" onclick="setHeatmapRange('week',this)">Last Week</button>
    <button class="hm-time-btn"        data-range="day"  onclick="setHeatmapRange('day',this)">Last Day</button>
    <button class="hm-time-btn"        data-range="3h"   onclick="setHeatmapRange('3h',this)">Last 3 Hours</button>
    <button class="hm-time-btn"        data-range="1h"   onclick="setHeatmapRange('1h',this)">Last Hour</button>
    <button class="hm-time-btn"        data-range="1min" onclick="setHeatmapRange('1min',this)">Last 1 Min</button>
    <button class="hm-time-btn"        data-range="30s"  onclick="setHeatmapRange('30s',this)">Last 30 Sec</button>
  </div>
  <div class="heatmap-cls-filters" id="heatmap-cls-filters">
    <button class="heatmap-cls-btn active" data-cls="all" onclick="heatmapSelectClass('all',this)">ALL</button>
    <button class="heatmap-refresh-btn" onclick="manualRefreshHeatmap()">&#8635; Refresh</button>
  </div>
  <div class="heatmap-grid" id="heatmap-grid">
    <div style="color:var(--text-dim);font-size:12px;padding:20px 0">Waiting for camera data...</div>
  </div>
</div>

</div><!-- .page -->

<script>
// ── Constants ─────────────────────────────────────────────────────────────────
const NEON_COLORS = [
  '#00d4ff','#00ff88','#ff0066','#ffdd00','#ff8800',
  '#cc44ff','#00ffff','#ff4488','#88ff00','#ff6600'
];

// ── Color management ──────────────────────────────────────────────────────────
const clsColors = {};
function getColor(cls) {
  if (!clsColors[cls])
    clsColors[cls] = NEON_COLORS[Object.keys(clsColors).length % NEON_COLORS.length];
  return clsColors[cls];
}
function hex2rgba(hex, a) {
  const r = parseInt(hex.slice(1,3), 16);
  const g = parseInt(hex.slice(3,5), 16);
  const b = parseInt(hex.slice(5,7), 16);
  return `rgba(${r},${g},${b},${a})`;
}

// ── CounterAnimation ──────────────────────────────────────────────────────────
class CounterAnimation {
  constructor(el) {
    this.el      = el;
    this.current = 0;
    this.target  = 0;
    this.raf     = null;
  }
  set(val) {
    if (val === this.target) return;
    this.target = val;
    if (this.raf) cancelAnimationFrame(this.raf);
    const step = () => {
      const diff = this.target - this.current;
      if (Math.abs(diff) < 1) {
        this.current = this.target;
        this.el.textContent = this._fmt(this.target);
        return;
      }
      this.current += diff * 0.25;
      this.el.textContent = this._fmt(Math.round(this.current));
      this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }
  _fmt(n) { return typeof n === 'number' ? n.toLocaleString() : n; }
}

const counters = {
  frames:  new CounterAnimation(document.getElementById('v-frames')),
  objects: new CounterAnimation(document.getElementById('v-objects')),
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtTime(ts)  { return new Date(ts * 1000).toLocaleTimeString(); }
function fmtDur(s) {
  s = Math.floor(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return h > 0 ? `${h}h ${m}m ${sec}s` : m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}
function getHidden(chart) {
  const hidden = new Set();
  chart.data.datasets.forEach((ds, i) => {
    if (!chart.isDatasetVisible(i)) hidden.add(ds.label);
  });
  return hidden;
}
function restoreHidden(chart, hidden) {
  if (!hidden.size) return;
  chart.data.datasets.forEach((ds, i) => {
    if (hidden.has(ds.label)) chart.setDatasetVisibility(i, false);
  });
  chart.update('none');
}

// ── Global Filter State ───────────────────────────────────────────────────────
const filterState = {
  camera:  'all',
  start:   '',
  end:     '',
  classes: []
};

function filterQS() {
  const p = new URLSearchParams();
  if (filterState.camera && filterState.camera !== 'all') p.set('camera', filterState.camera);
  if (filterState.start) p.set('start', new Date(filterState.start).getTime() / 1000);
  if (filterState.end)   p.set('end',   new Date(filterState.end).getTime() / 1000);
  if (filterState.classes.length) p.set('classes', filterState.classes.join(','));
  return p.toString();
}

// ── Charts ────────────────────────────────────────────────────────────────────
const CHART_DEFAULTS = {
  color: '#666677',
  borderColor: '#1e1e2e',
  backgroundColor: 'transparent',
};
Chart.defaults.color       = CHART_DEFAULTS.color;
Chart.defaults.borderColor = CHART_DEFAULTS.borderColor;

let timelineChart, distChart, pieChart;

function initCharts() {
  const darkGrid = { color: 'rgba(255,255,255,.04)', tickColor: 'rgba(255,255,255,.04)' };

  timelineChart = new Chart(document.getElementById('timelineChart'), {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 11 }, color: '#666677' } },
        tooltip: { backgroundColor: '#0d0d18', borderColor: '#1e1e2e', borderWidth: 1, padding: 10, titleColor: '#e0e0ff', bodyColor: '#aaaacc' },
        zoom: { zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' }, pan: { enabled: true, mode: 'x' } }
      },
      scales: {
        x: { grid: darkGrid, ticks: { maxTicksLimit: 10, font: { size: 11 } }, title: { display: true, text: 'Time', color: '#666677' } },
        y: { beginAtZero: true, grid: darkGrid, title: { display: true, text: 'Count / second', color: '#666677' } }
      }
    }
  });

  distChart = new Chart(document.getElementById('distChart'), {
    type: 'bar',
    data: { labels: [], datasets: [{ data: [], backgroundColor: [], borderRadius: 3 }] },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { backgroundColor: '#0d0d18', borderColor: '#1e1e2e', borderWidth: 1, padding: 10,
          callbacks: { label: ctx => '  ' + ctx.parsed.x.toLocaleString() + ' total' } }
      },
      scales: {
        x: { type: 'logarithmic', grid: darkGrid, title: { display: true, text: 'Cumulative count (log)', color: '#666677' } },
        y: { grid: darkGrid, ticks: { font: { size: 11 } } }
      }
    }
  });

  pieChart = new Chart(document.getElementById('pieChart'), {
    type: 'doughnut',
    data: { labels: [], datasets: [{ data: [], backgroundColor: [], borderWidth: 2, borderColor: '#12121a' }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 11 }, color: '#666677' } },
        tooltip: { backgroundColor: '#0d0d18', borderColor: '#1e1e2e', borderWidth: 1, padding: 10 }
      }
    }
  });
}

// ── HeatmapRenderer ───────────────────────────────────────────────────────────
let heatmapActiveClass = 'all';
let heatmapRenderers   = {};         // camera_id -> HeatmapRenderer
let _heatmapAllClasses = new Set();

class HeatmapRenderer {
  constructor(canvas) {
    this.canvas   = canvas;
    this.ctx      = canvas.getContext('2d');
    this.data     = [];
    this.fw       = 1920;
    this.fh       = 1080;
    this._bgImage = null;
    this._resizeObserver = new ResizeObserver(() => this.render());
    this._resizeObserver.observe(canvas.parentElement);
  }

  setData(d) {
    this.data = d.points || [];
    this.fw   = d.fw   || 1920;
    this.fh   = d.fh   || 1080;
    this.render();
  }

  setBackground(dataUrl) {
    if (!dataUrl) { this._bgImage = null; this.render(); return; }
    const img = new Image();
    img.onload = () => { this._bgImage = img; this.render(); };
    img.src = dataUrl;
  }

  render() {
    const W = this.canvas.parentElement.clientWidth || 400;
    const H = Math.round(W * this.fh / this.fw);
    this.canvas.width  = W;
    this.canvas.height = H;

    const ctx  = this.ctx;
    const cols = 60;
    const rows = 34;
    const cw   = W / cols;
    const ch   = H / rows;

    const grid = new Float32Array(cols * rows);
    const pts  = heatmapActiveClass === 'all'
      ? this.data
      : this.data.filter(p => p.c === heatmapActiveClass);
    let maxVal = 0;
    for (const p of pts) {
      const col = Math.min(cols - 1, Math.floor(p.x / this.fw * cols));
      const row = Math.min(rows - 1, Math.floor(p.y / this.fh * rows));
      if (col >= 0 && row >= 0) {
        grid[row * cols + col] += 1;
        if (grid[row * cols + col] > maxVal) maxVal = grid[row * cols + col];
      }
    }

    ctx.clearRect(0, 0, W, H);

    if (this._bgImage) {
      ctx.drawImage(this._bgImage, 0, 0, W, H);
      ctx.fillStyle = 'rgba(0,0,0,0.45)';
      ctx.fillRect(0, 0, W, H);
    } else {
      ctx.fillStyle = '#0a0a0f';
      ctx.fillRect(0, 0, W, H);
    }

    if (maxVal === 0) {
      ctx.fillStyle = 'rgba(255,255,255,0.25)';
      ctx.font = '12px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('No position data', W / 2, H / 2);
      return;
    }

    const colorRamp = [
      [0,   0,   0],
      [0,   24,  51],
      [0,   51, 153],
      [0,  136, 255],
      [0,  255, 170],
      [255, 221,  0],
      [255,  68,  0],
      [255,   0,  0],
    ];
    function rampColor(t) {
      const n = colorRamp.length - 1;
      const i = Math.min(n - 1, Math.floor(t * n));
      const f = t * n - i;
      const a = colorRamp[i], b2 = colorRamp[i + 1];
      return `rgba(${Math.round(a[0]+(b2[0]-a[0])*f)},${Math.round(a[1]+(b2[1]-a[1])*f)},${Math.round(a[2]+(b2[2]-a[2])*f)},0.88)`;
    }

    for (let row = 0; row < rows; row++) {
      for (let col = 0; col < cols; col++) {
        const v = grid[row * cols + col];
        if (v === 0) continue;
        ctx.fillStyle = rampColor(Math.pow(v / maxVal, 0.5));
        ctx.fillRect(Math.floor(col * cw), Math.floor(row * ch), Math.ceil(cw) + 1, Math.ceil(ch) + 1);
      }
    }
  }
}

function heatmapSelectClass(cls, btn) {
  heatmapActiveClass = cls;
  document.querySelectorAll('#heatmap-cls-filters .heatmap-cls-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  Object.values(heatmapRenderers).forEach(r => r.render());
}

function _updateHeatmapClassButtons(classes) {
  const newCls = [...classes].filter(c => !_heatmapAllClasses.has(c)).sort();
  if (!newCls.length) return;
  newCls.forEach(c => _heatmapAllClasses.add(c));
  const bar        = document.getElementById('heatmap-cls-filters');
  const refreshBtn = bar.querySelector('.heatmap-refresh-btn');
  newCls.forEach(cls => {
    const btn = document.createElement('button');
    btn.className   = 'heatmap-cls-btn' + (heatmapActiveClass === cls ? ' active' : '');
    btn.dataset.cls = cls;
    btn.textContent = cls;
    btn.onclick     = () => heatmapSelectClass(cls, btn);
    bar.insertBefore(btn, refreshBtn);
  });
}

// ── Heatmap independent filter ────────────────────────────────────────────────
let heatmapTimeRange = 'all';

const HM_RANGE_SECS = { all: 0, week: 604800, day: 86400, '3h': 10800, '1h': 3600, '1min': 60, '30s': 30 };

function heatmapFilterQS() {
  if (heatmapTimeRange === 'all') return '';
  const now   = Date.now() / 1000;
  const secs  = HM_RANGE_SECS[heatmapTimeRange] || 0;
  const p     = new URLSearchParams();
  p.set('start', now - secs);
  p.set('end',   now);
  return p.toString();
}

function setHeatmapRange(range, btn) {
  heatmapTimeRange = range;
  document.querySelectorAll('#heatmap-time-bar .hm-time-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  updateHeatmap(heatmapFilterQS());
}

async function manualRefreshHeatmap() {
  await updateHeatmap(heatmapFilterQS());
}

// ── Camera selector ───────────────────────────────────────────────────────────
let selectedCamera = 'all';
let knownCameras   = [];
let rtspTargetCam  = null;

function selectCamera(cam, btn) {
  selectedCamera = cam;
  filterState.camera = cam;
  document.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  // Close rtsp form if switching cameras
  closeRtspForm();
  refreshAll();
}

function openRtspForm(camId, camName) {
  rtspTargetCam = camId;
  document.getElementById('rtsp-cam-label').textContent = camName || camId;
  document.getElementById('rtsp-url-input').value = '';
  document.getElementById('rtsp-msg').textContent = '';
  document.getElementById('rtsp-form').classList.add('open');
}

function closeRtspForm() {
  document.getElementById('rtsp-form').classList.remove('open');
  rtspTargetCam = null;
}

async function submitRtsp() {
  if (!rtspTargetCam) return;
  const url = document.getElementById('rtsp-url-input').value.trim();
  if (!url) { document.getElementById('rtsp-msg').textContent = 'Please enter a URL.'; return; }
  document.getElementById('rtsp-msg').textContent = 'Capturing...';
  try {
    const res  = await fetch('/api/camera_rtsp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: rtspTargetCam, rtsp_url: url })
    });
    const data = await res.json();
    if (data.ok) {
      document.getElementById('rtsp-msg').textContent = data.has_thumbnail ? 'Thumbnail captured!' : 'RTSP saved (no thumbnail — check ffmpeg/URL).';
      await updateHeatmap(heatmapFilterQS());
    } else {
      document.getElementById('rtsp-msg').textContent = 'Failed.';
    }
  } catch(e) {
    document.getElementById('rtsp-msg').textContent = 'Error: ' + e.message;
  }
}

async function captureNxSnapshot(camId) {
  try {
    const res  = await fetch('/api/camera_snapshot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: camId })
    });
    const data = await res.json();
    if (data.ok && data.has_thumbnail) {
      await updateHeatmap(heatmapFilterQS());
    } else {
      alert('No thumbnail from Nx for this camera (it may have no still frame available right now — open/play it in Nx, then try again).');
    }
  } catch(e) {
    alert('Snapshot error: ' + e.message);
  }
}

async function deleteHeatmapCamera(camId, name) {
  if (!confirm('Remove camera "' + name + '" from the dashboard? '
             + 'This deletes its heatmap, timeline and history data. '
             + 'If the camera is still selected as a postprocessor in Nx, it will reappear on the next frame.')) {
    return;
  }
  try {
    const res  = await fetch('/api/camera_delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: camId })
    });
    const data = await res.json();
    if (data.ok) {
      // Remove the heatmap card + its renderer immediately.
      const card = document.querySelector(`#heatmap-grid [data-cam="${CSS.escape(camId)}"]`);
      if (card) card.remove();
      delete heatmapRenderers[camId];
      // If the deleted camera was the active selection, fall back to ALL.
      if (selectedCamera === camId) selectCamera('all', document.querySelector('.cam-btn[data-cam="all"]'));
      await refreshCameras();
      await updateHeatmap(heatmapFilterQS());
    } else {
      alert('Delete failed.');
    }
  } catch(e) {
    alert('Delete error: ' + e.message);
  }
}

async function refreshCameras() {
  try {
    const res  = await fetch('/api/cameras');
    const cams = await res.json();
    const container = document.getElementById('cam-extra-btns');
    const newIds    = cams.map(c => c.camera_id);

    // Remove buttons for cameras no longer present
    Array.from(container.querySelectorAll('.cam-btn')).forEach(b => {
      if (!newIds.includes(b.dataset.cam)) b.remove();
    });

    // Add new cameras
    cams.forEach(cam => {
      if (!container.querySelector(`[data-cam="${CSS.escape(cam.camera_id)}"]`)) {
        const btn = document.createElement('button');
        btn.className   = 'cam-btn' + (selectedCamera === cam.camera_id ? ' active' : '');
        btn.dataset.cam = cam.camera_id;
        const cidEsc    = cam.camera_id.replace(/'/g,"\\'");
        const snapSpan  = `<span class="cam-gear" title="Capture thumbnail from Nx" onclick="event.stopPropagation();captureNxSnapshot('${cidEsc}')">&#128247;</span>`;
        const gearSpan  = `<span class="cam-gear" title="Set RTSP thumbnail (manual)" onclick="event.stopPropagation();openRtspForm('${cidEsc}','${(cam.stream_name||cam.camera_id).replace(/'/g,"\\'")}')">&#9881;</span>`;
        btn.innerHTML   = (cam.stream_name || cam.camera_id) + snapSpan + gearSpan;
        btn.title       = `ID: ${cam.camera_id} | Frames: ${cam.total_frames.toLocaleString()}`;
        btn.onclick     = () => selectCamera(cam.camera_id, btn);
        container.appendChild(btn);
      }
    });

    // Populate filter camera select
    const sel = document.getElementById('f-camera');
    const existing = new Set(Array.from(sel.options).map(o => o.value));
    cams.forEach(cam => {
      if (!existing.has(cam.camera_id)) {
        const opt = document.createElement('option');
        opt.value       = cam.camera_id;
        opt.textContent = cam.stream_name || cam.camera_id;
        sel.appendChild(opt);
      }
    });

    knownCameras = cams;
  } catch(e) { /* silently skip */ }
}

// ── Update functions ──────────────────────────────────────────────────────────
function updateStats(s) {
  counters.frames.set(s.frame_count);
  counters.objects.set(s.total_objects);
  document.getElementById('v-current').textContent = (s.fps || 0).toFixed(1);
  document.getElementById('v-uptime').textContent = fmtDur(s.uptime);
}

function renderTimeline(data, isFiltered) {
  if (!data.length) return;
  const classes = new Set();
  data.forEach(pt => Object.keys(pt.pc).forEach(c => classes.add(c)));
  const hidden = getHidden(timelineChart);

  document.getElementById('timeline-panel-title').textContent =
    isFiltered ? 'Filtered Timeline (per minute)' : 'Live Object Timeline';

  timelineChart.data.labels   = data.map(pt => fmtTime(pt.ts));
  timelineChart.data.datasets = Array.from(classes).map((cls, i) => ({
    label:           cls,
    data:            data.map(pt => pt.pc[cls] || 0),
    borderColor:     getColor(cls),
    backgroundColor: hex2rgba(getColor(cls), 0.06),
    fill:            false,
    tension:         0.3,
    pointRadius:     0,
    pointHoverRadius:4,
    borderWidth:     1.5,
  }));
  timelineChart.update('none');
  restoreHidden(timelineChart, hidden);
  updateFilterClassCheckboxes(classes);
  // Update last per-class for alerts
  if (data.length > 0) _lastCurrentPerClass = data[data.length - 1].pc || {};
}

async function updateTimeline(qs, hasTimeFilter) {
  try {
    if (hasTimeFilter) {
      const res  = await fetch('/api/filtered/timeline?' + qs);
      const data = await res.json();
      // Convert filtered (label/datasets) format to timeline-point format for rendering
      if (!data.labels || !data.labels.length) return;
      const pts = data.labels.map((lbl, i) => {
        const pc = {};
        (data.datasets || []).forEach(ds => { pc[ds.label] = ds.data[i] || 0; });
        return { ts: 0, _label: lbl, pc };
      });
      // Use custom labels
      const hidden = getHidden(timelineChart);
      document.getElementById('timeline-panel-title').textContent = 'Filtered Timeline (per minute)';
      timelineChart.data.labels   = data.labels;
      timelineChart.data.datasets = (data.datasets || []).map((ds, i) => ({
        label:           ds.label,
        data:            ds.data,
        borderColor:     NEON_COLORS[i % NEON_COLORS.length],
        backgroundColor: hex2rgba(NEON_COLORS[i % NEON_COLORS.length], 0.06),
        fill: false, tension: 0.3, pointRadius: 0, pointHoverRadius: 4, borderWidth: 1.5,
      }));
      timelineChart.update('none');
      restoreHidden(timelineChart, hidden);
    } else {
      const cam = filterState.camera !== 'all' ? '?camera=' + encodeURIComponent(filterState.camera) : '';
      const res  = await fetch('/api/timeline' + cam);
      const data = await res.json();
      renderTimeline(data, false);
      updateFilterClassCheckboxes(new Set(data.flatMap(pt => Object.keys(pt.pc))));
    }
  } catch(e) { /* silently skip */ }
}

async function updateDistribution(qs) {
  try {
    const hasFilter = !!(filterState.start || filterState.end || filterState.classes.length || (filterState.camera && filterState.camera !== 'all'));
    let data;
    if (hasFilter && qs) {
      const res = await fetch('/api/filtered/stats?' + qs);
      data = await res.json();
    } else {
      const res = await fetch('/api/distribution');
      data = await res.json();
    }
    document.getElementById('dist-panel-title').textContent =
      hasFilter ? 'Filtered Distribution' : 'Cumulative Distribution';
    const sorted = Object.entries(data).sort((a, b) => b[1] - a[1]);
    if (!sorted.length) return;
    const h = Math.min(500, Math.max(120, sorted.length * 28));
    document.getElementById('distWrap').style.height = h + 'px';
    distChart.data.labels = sorted.map(([cls]) => cls);
    distChart.data.datasets[0].data            = sorted.map(([, cnt]) => cnt);
    distChart.data.datasets[0].backgroundColor = sorted.map(([cls]) => getColor(cls));
    distChart.update('none');
  } catch(e) { /* silently skip */ }
}

async function updatePieChart(qs) {
  try {
    const now   = Date.now() / 1000;
    const pqs   = qs || ('start=' + (now - 86400) + '&end=' + now);
    const res   = await fetch('/api/filtered/donut?' + pqs);
    const data  = await res.json();
    const hasFilter = filterState.start || filterState.end || filterState.classes.length;
    document.getElementById('pie-panel-title').textContent =
      hasFilter ? 'Filtered Class Distribution' : 'Class Distribution (Today)';
    pieChart.data.labels = data.labels || [];
    pieChart.data.datasets[0].data            = data.data || [];
    pieChart.data.datasets[0].backgroundColor = (data.labels || []).map((_, i) =>
      hex2rgba(NEON_COLORS[i % NEON_COLORS.length], 0.8));
    pieChart.update('none');
  } catch(e) { /* silently skip */ }
}

async function updateCountCards(qs) {
  try {
    const now   = Date.now() / 1000;
    const pqs   = qs || ('start=' + (now - 86400) + '&end=' + now);
    const res   = await fetch('/api/filtered/stats?' + pqs);
    const data  = await res.json();
    const row   = document.getElementById('count-cards-row');
    const entries = Object.entries(data).sort((a, b) => b[1] - a[1]).slice(0, 8);
    if (!entries.length) { row.innerHTML = ''; return; }
    row.innerHTML = entries.map(([cls, cnt], i) => {
      const color = NEON_COLORS[i % NEON_COLORS.length];
      return `<div class="count-card">
        <div class="count-card-label">${cls}</div>
        <div class="count-card-value" style="color:${color};text-shadow:0 0 15px ${hex2rgba(color,0.3)}">${cnt.toLocaleString()}</div>
      </div>`;
    }).join('');
  } catch(e) { /* silently skip */ }
}

async function updateHeatmap(qs) {
  try {
    // Strip camera param — heatmap always shows all cameras
    const hqs = qs.replace(/(?:^|&)camera=[^&]*/g, '').replace(/^&/, '');
    const res  = await fetch('/api/heatmap_all?' + hqs);
    const data = await res.json();  // { cam_id: { points, fw, fh, stream_name, thumbnail }, ... }
    const grid = document.getElementById('heatmap-grid');
    const camIds = Object.keys(data);

    if (!camIds.length) {
      grid.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:20px 0">No camera data yet</div>';
      return;
    }

    // Clear placeholder if present
    const placeholder = grid.querySelector('div[style]');
    if (placeholder) placeholder.remove();

    const allClasses = new Set();
    camIds.forEach((camId, idx) => {
      const camData = data[camId];
      (camData.points || []).forEach(p => allClasses.add(p.c));

      if (!heatmapRenderers[camId]) {
        const safeId = 'heatmap-c-' + idx;
        const card   = document.createElement('div');
        card.className = 'heatmap-cam-card';
        card.id        = 'heatmap-card-' + safeId;
        card.dataset.cam = camId;
        const camEsc   = camId.replace(/'/g,"\\'");
        const nameEsc  = (camData.stream_name || camId).replace(/'/g,"\\'");
        card.innerHTML = `<button class="heatmap-cam-del" title="Remove this camera from the dashboard" onclick="deleteHeatmapCamera('${camEsc}','${nameEsc}')">&times;</button><div class="heatmap-cam-title">${camData.stream_name || camId}</div><canvas class="heatmap-cam-canvas" id="${safeId}"></canvas>`;
        grid.appendChild(card);
        const canvas = document.getElementById(safeId);
        heatmapRenderers[camId] = new HeatmapRenderer(canvas);
      }

      const renderer = heatmapRenderers[camId];
      renderer.setData(camData);
      if (camData.thumbnail) renderer.setBackground(camData.thumbnail);
    });

    _updateHeatmapClassButtons(allClasses);
  } catch(e) { /* silently skip */ }
}

async function updateTopN(qs) {
  try {
    const query = qs ? 'n=5&' + qs : 'n=5';
    const res   = await fetch('/api/top?' + query);
    const data  = await res.json();
    const list  = document.getElementById('top5-list');
    const title = document.getElementById('top5-title');
    const hasFilter = !!(filterState.start || filterState.end || filterState.classes.length || (filterState.camera && filterState.camera !== 'all'));
    title.textContent = 'Top Detected Classes — ' + (hasFilter ? 'Filtered' : 'Last 24h');
    if (!data.length) {
      list.innerHTML = '<div style="color:var(--text-dim);font-size:12px">No data yet</div>';
      return;
    }
    const max = data[0].count || 1;
    list.innerHTML = data.map((item, idx) => {
      const color = NEON_COLORS[idx % NEON_COLORS.length];
      const pct   = Math.max(2, Math.round(item.count / max * 100));
      return `<div class="top5-item">
        <span class="top5-rank">${idx+1}</span>
        <span class="top5-name" style="color:${color}">${item.class}</span>
        <div class="top5-bar-wrap">
          <div class="top5-bar" style="width:${pct}%;background:${color}"></div>
        </div>
        <span class="top5-count" style="color:${color}">${item.count.toLocaleString()}</span>
      </div>`;
    }).join('');
  } catch(e) { /* silently skip */ }
}

function updateLiveBadge(hasTimeFilter) {
  const badge = document.getElementById('status-badge');
  const dot   = document.getElementById('status-dot');
  const txt   = document.getElementById('status-text');
  if (hasTimeFilter) {
    badge.className = 'badge badge-filtered';
    dot.className   = 'filter-dot';
    txt.textContent = 'FILTERED';
  } else {
    badge.className = 'badge badge-live';
    dot.className   = 'pulse-dot';
    txt.textContent = 'LIVE';
  }
}

// ── Master refresh ────────────────────────────────────────────────────────────
async function refreshAll() {
  const qs            = filterQS();
  const hasTimeFilter = !!(filterState.start || filterState.end);

  updateLiveBadge(hasTimeFilter);

  await Promise.all([
    updateTimeline(qs, hasTimeFilter),
    updateDistribution(qs),
    updatePieChart(qs),
    updateCountCards(qs),
    updateTopN(qs),
  ]);
}

// ── Filter class checkboxes ───────────────────────────────────────────────────
let knownClasses = new Set();
function updateFilterClassCheckboxes(classes) {
  const newCls = [...classes].filter(c => !knownClasses.has(c));
  if (!newCls.length) return;
  const container = document.getElementById('f-classes');
  newCls.forEach(cls => {
    knownClasses.add(cls);
    const lbl = document.createElement('label');
    lbl.innerHTML = `<input type="checkbox" value="${cls}" checked> ${cls}`;
    container.appendChild(lbl);
  });
}

// ── Fetch loop ────────────────────────────────────────────────────────────────
let isPaused   = false;
let lastDataTs = 0;

async function fetchFast() {
  if (isPaused) return;
  try {
    const res   = await fetch('/api/stats');
    const stats = await res.json();
    updateStats(stats);
    lastDataTs = Date.now();
    // Only update live badge if no time filter active
    if (!(filterState.start || filterState.end)) setStatus('live');
    // Update timeline live only if no time filter
    if (!(filterState.start || filterState.end)) {
      await updateTimeline('', false);
    }
  } catch { setStatus('offline'); }
}

async function fetchSlow() {
  if (isPaused) return;
  const qs = filterQS();
  await Promise.all([
    updateDistribution(qs),
    updatePieChart(qs),
    updateCountCards(qs),
    updateTopN(qs),
    updateHeatmap(heatmapFilterQS()),
  ]);
}

function setStatus(s) {
  const badge = document.getElementById('status-badge');
  const dot   = document.getElementById('status-dot');
  const txt   = document.getElementById('status-text');
  // Don't override filtered badge
  if (filterState.start || filterState.end) return;
  if (s === 'live') {
    badge.className = 'badge badge-live';
    dot.className   = 'pulse-dot';
    txt.textContent = 'LIVE';
  } else {
    badge.className = 'badge badge-offline';
    dot.className   = 'offline-dot';
    txt.textContent = 'OFFLINE';
  }
}

// Check if data has gone stale (>5s)
setInterval(() => {
  if (Date.now() - lastDataTs > 5000 && lastDataTs > 0) setStatus('offline');
}, 1000);

function resetZoom() {
  if (timelineChart && timelineChart.resetZoom) timelineChart.resetZoom();
}

function togglePause() {
  isPaused = !isPaused;
  document.getElementById('btn-pause').textContent = isPaused ? 'Resume' : 'Pause';
}

function clearData() {
  if (!confirm('Clear all data?')) return;
  fetch('/api/clear', { method: 'POST' }).then(res => {
    if (!res.ok) { alert('Clear failed'); return; }
    [timelineChart, distChart, pieChart].forEach(ch => {
      ch.data.labels = [];
      ch.data.datasets = ch === distChart
        ? [{ data: [], backgroundColor: [], borderRadius: 3 }]
        : ch === pieChart
          ? [{ data: [], backgroundColor: [], borderWidth: 2, borderColor: '#12121a' }]
          : [];
      ch.update();
    });
    document.getElementById('distWrap').style.height = '220px';
    document.getElementById('count-cards-row').innerHTML = '';
    Object.keys(clsColors).forEach(k => delete clsColors[k]);
    knownClasses.clear();
    document.getElementById('f-classes').innerHTML = '';
    document.getElementById('top5-list').innerHTML = '<div style="color:var(--text-dim);font-size:12px">No data yet</div>';
    Object.values(heatmapRenderers).forEach(r => r.setData({ points: [], fw: 1920, fh: 1080 }));
  }).catch(() => alert('Clear failed'));
}

// ── Filter panel ──────────────────────────────────────────────────────────────
function _toLocalDT(d) {
  const off = d.getTimezoneOffset() * 60000;
  return new Date(d - off).toISOString().slice(0, 16);
}

function _initFilterDefaults() {
  const now   = new Date();
  const start = new Date(now - 3600_000);
  document.getElementById('f-end').value   = _toLocalDT(now);
  document.getElementById('f-start').value = _toLocalDT(start);
}

function applyFilter() {
  filterState.start   = document.getElementById('f-start').value;
  filterState.end     = document.getElementById('f-end').value;
  filterState.camera  = document.getElementById('f-camera').value;
  const checked = [...document.querySelectorAll('#f-classes input:checked')].map(i => i.value);
  filterState.classes = checked;
  refreshAll();
}

function clearFilter() {
  filterState.start   = '';
  filterState.end     = '';
  filterState.classes = [];
  // Sync filter UI (keep camera in sync with cam bar)
  document.getElementById('f-start').value  = '';
  document.getElementById('f-end').value    = '';
  document.querySelectorAll('#f-classes input[type=checkbox]').forEach(cb => { cb.checked = true; });
  updateLiveBadge(false);
  refreshAll();
}

function exportCSV() {
  const start = new Date(document.getElementById('f-start').value).getTime() / 1000;
  const end   = new Date(document.getElementById('f-end').value).getTime()   / 1000;
  const cam   = document.getElementById('f-camera').value;
  const checked = [...document.querySelectorAll('#f-classes input:checked')].map(i => i.value);
  const p = [`start=${start}`, `end=${end}`];
  if (cam && cam !== 'all') p.push('camera=' + encodeURIComponent(cam));
  if (checked.length < knownClasses.size && checked.length > 0)
    p.push('classes=' + checked.map(encodeURIComponent).join(','));
  window.location = '/api/export.csv?' + p.join('&');
}

// ── Alert manager ─────────────────────────────────────────────────────────────
class AlertManager {
  constructor() {
    this.alerts = [];
  }
  check(currentPerClass) {
    const triggered = [];
    this.alerts.forEach(a => {
      const cnt = currentPerClass[a.class] || 0;
      if (cnt >= a.threshold) triggered.push(a.class);
    });
    if (triggered.length) {
      const b = document.getElementById('alert-banner');
      b.style.display = 'block';
      b.textContent   = 'ALERT: ' + triggered.join(', ') + ' exceeded threshold';
      const card = document.getElementById('kpi-current');
      card.classList.remove('alert-flash');
      void card.offsetWidth;
      card.classList.add('alert-flash');
    } else {
      document.getElementById('alert-banner').style.display = 'none';
    }
  }
}

const alertManager   = new AlertManager();
let _lastCurrentPerClass = {};

function checkAlerts(currentTotal) {
  alertManager.check(_lastCurrentPerClass);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
initCharts();
_initFilterDefaults();

fetchFast();
fetchSlow();
refreshCameras();

setInterval(fetchFast,      1000);
setInterval(fetchSlow,      5000);
setInterval(refreshCameras, 30000);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Web Dashboard Advance — NX AI Manager monitoring app")
    parser.add_argument('--port',      type=int,    help=f"HTTP port (default: {DEFAULT_PORT})")
    parser.add_argument('--db',        metavar='PATH', help="SQLite database path")
    parser.add_argument('--log-level', metavar='LEVEL', help="Log level: DEBUG|INFO|WARNING|ERROR")
    args = parser.parse_args()

    logger = logging.getLogger(__name__)
    port, tc, sc, db_path = config()

    if args.port:      port     = args.port
    if args.db:        db_path  = args.db
    if args.log_level: set_log_level(args.log_level)

    store = DetectionStore(db_path, tc, sc)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    start_flush_thread(DEFAULT_FLUSH_SECS)

    try:
        start_web_server(port)
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)

    logger.info("Web Dashboard Advance running — http://localhost:%d  db=%s", port, db_path)
    shutdown_event.wait()

    shutdown_event.set()
    logger.info("Web Dashboard Advance stopped.")
