#!/usr/bin/env python3
"""
Parking Dashboard Web App
Self-contained HTTP server that:
  - Receives detection payloads from the parking postprocessor via POST /api/ingest
  - Manages parking space occupancy state, sessions, and cooldowns
  - Serves an interactive dark-theme dashboard at http://localhost:8114
"""
import os, sys, logging, logging.handlers, json, signal, time, uuid, argparse
from datetime import datetime
from threading import Thread, Lock, Event
from collections import defaultdict
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))

# ── Paths ──────────────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(script_location, "plugin.parking-dashboard-app.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - parking-dashboard-app - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=2),
    ]
)

DEFAULT_PORT   = 8114
NUM_SPACES     = 6
VACATE_TIMEOUT = 3.0    # seconds of no detection before marking space vacated
COOLDOWN_SECS  = 30.0   # cooldown period after vacating (30 seconds)

shutdown_event = Event()
web_server     = None
store          = None
logger         = None


# ── Parking State Store ────────────────────────────────────────────────────────

class ParkingStore:
    """
    Thread-safe store tracking:
    - spaces: current occupancy state for P1-P6
    - sessions: completed parking sessions (historical)
    - active_sessions: currently ongoing sessions per space
    - last_seen: last detection timestamp per space (for vacate timeout)
    """

    def __init__(self):
        self._lock = Lock()

        # spaces[space_id] = {
        #   occupied: bool,
        #   vehicle_class: str|None,
        #   entry_time: float|None,   # unix timestamp
        #   cooldown_until: float,    # unix timestamp, 0 if not in cooldown
        # }
        self.spaces = {
            f"P{i+1}": {
                'occupied':       False,
                'vehicle_class':  None,
                'entry_time':     None,
                'cooldown_until': 0.0,
            }
            for i in range(NUM_SPACES)
        }

        # completed sessions
        self.sessions = []          # list of dicts

        # active_sessions[space_id] = {id, space_id, vehicle_class, entry_time}
        self.active_sessions = {}

        # last_seen[space_id] = unix timestamp of last detection
        self.last_seen = {}

    # ── Ingest ─────────────────────────────────────────────────────────────────

    def ingest(self, payload):
        """Process one detection payload from the postprocessor."""
        ts         = float(payload.get('ts', time.time()))
        detections = payload.get('detections', [])

        # Build set of spaces seen in this frame
        seen_spaces = {}
        for det in detections:
            space = det.get('space')
            cls   = det.get('class', 'Car')
            if space and space in self.spaces:
                seen_spaces[space] = cls

        with self._lock:
            now = time.time()

            for space_id, cls in seen_spaces.items():
                self.last_seen[space_id] = now
                space = self.spaces[space_id]

                if not space['occupied']:
                    # Check if in cooldown — if so, skip
                    if space['cooldown_until'] > now:
                        logger.debug("Space %s in cooldown, ignoring detection", space_id)
                        continue

                    # Start new session
                    session_id = str(uuid.uuid4())
                    space['occupied']      = True
                    space['vehicle_class'] = cls
                    space['entry_time']    = now
                    space['cooldown_until'] = 0.0
                    self.active_sessions[space_id] = {
                        'id':            session_id,
                        'space_id':      space_id,
                        'vehicle_class': cls,
                        'entry_time':    now,
                    }
                    logger.info("Space %s OCCUPIED by %s (session %s)", space_id, cls, session_id)
                else:
                    # Update detection (vehicle class may change)
                    space['vehicle_class'] = cls
                    if space_id in self.active_sessions:
                        self.active_sessions[space_id]['vehicle_class'] = cls

    def vacate_space(self, space_id):
        """Mark a space as vacated (called from background watchdog thread)."""
        with self._lock:
            space = self.spaces.get(space_id)
            if space is None or not space['occupied']:
                return

            now = time.time()
            session = self.active_sessions.pop(space_id, None)
            if session:
                session['exit_time'] = now
                self.sessions.append(session)
                logger.info("Space %s VACATED  duration=%.1f min  session=%s",
                            space_id,
                            (now - session['entry_time']) / 60.0,
                            session['id'])

            space['occupied']       = False
            space['vehicle_class']  = None
            space['entry_time']     = None
            space['cooldown_until'] = now + COOLDOWN_SECS

    def release_space(self, space_id):
        """Immediately free a space, bypassing cooldown (manual override)."""
        with self._lock:
            space = self.spaces.get(space_id)
            if space is None:
                return False
            now = time.time()
            if space['occupied']:
                session = self.active_sessions.pop(space_id, None)
                if session:
                    session['exit_time'] = now
                    self.sessions.append(session)
                    logger.info("Space %s RELEASED manually  duration=%.1f min",
                                space_id, (now - session['entry_time']) / 60.0)
            else:
                logger.info("Space %s cooldown CLEARED manually", space_id)
            space['occupied']       = False
            space['vehicle_class']  = None
            space['entry_time']     = None
            space['cooldown_until'] = 0.0
            self.last_seen.pop(space_id, None)
            return True

    # ── Queries ────────────────────────────────────────────────────────────────

    def get_status(self):
        """Return full status dict for the UI polling endpoint."""
        now = time.time()
        with self._lock:
            spaces_out = {}
            for sid, s in self.spaces.items():
                duration_min = 0.0
                if s['occupied'] and s['entry_time']:
                    duration_min = (now - s['entry_time']) / 60.0

                cooldown = (not s['occupied']) and (s['cooldown_until'] > now)

                spaces_out[sid] = {
                    'occupied':      s['occupied'],
                    'vehicle_class': s['vehicle_class'],
                    'duration_min':  round(duration_min, 2),
                    'cooldown':      cooldown,
                    'entry_time':    s['entry_time'],
                }

            active_count = sum(1 for s in self.spaces.values() if s['occupied'])

            # Sessions today
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            sessions_today = [
                ses for ses in self.sessions
                if ses.get('entry_time', 0) >= today_start
            ]
            # Include active sessions that started today
            for ses in self.active_sessions.values():
                if ses.get('entry_time', 0) >= today_start:
                    sessions_today.append(ses)

            sessions_today_count = len(sessions_today)

            # Avg duration (completed sessions today only)
            completed_today = [
                ses for ses in self.sessions
                if ses.get('entry_time', 0) >= today_start and 'exit_time' in ses
            ]
            avg_duration_min = 0.0
            if completed_today:
                total_min = sum(
                    (ses['exit_time'] - ses['entry_time']) / 60.0
                    for ses in completed_today
                )
                avg_duration_min = round(total_min / len(completed_today), 2)

            # Most used space (all time)
            space_counts = defaultdict(int)
            for ses in self.sessions:
                space_counts[ses['space_id']] += 1
            for ses in self.active_sessions.values():
                space_counts[ses['space_id']] += 1
            most_used = max(space_counts, key=space_counts.get) if space_counts else "—"

        return {
            'spaces':          spaces_out,
            'active_count':    active_count,
            'sessions_today':  sessions_today_count,
            'avg_duration_min': avg_duration_min,
            'most_used':       most_used,
        }

    def get_stats(self, window='24h'):
        """Return per-space stats for the given time window."""
        now    = time.time()
        cutoff = _window_cutoff(now, window)

        with self._lock:
            sessions_in_window = [
                ses for ses in self.sessions
                if ses.get('entry_time', 0) >= cutoff
            ]
            # Include active sessions
            for ses in self.active_sessions.values():
                if ses.get('entry_time', 0) >= cutoff:
                    sessions_in_window.append(ses)

        counts   = defaultdict(int)
        duration = defaultdict(float)
        dur_cnt  = defaultdict(int)

        for ses in sessions_in_window:
            sid = ses['space_id']
            counts[sid] += 1
            if 'exit_time' in ses:
                duration[sid] += (ses['exit_time'] - ses['entry_time']) / 60.0
                dur_cnt[sid]  += 1

        result = []
        for i in range(NUM_SPACES):
            sid = f"P{i+1}"
            cnt = counts.get(sid, 0)
            avg = round(duration[sid] / dur_cnt[sid], 2) if dur_cnt.get(sid) else 0.0
            result.append({
                'space_id':         sid,
                'session_count':    cnt,
                'avg_duration_min': avg,
            })
        return result

    def get_duration_buckets(self, window='24h'):
        """Return session duration distribution in fixed buckets."""
        now    = time.time()
        cutoff = _window_cutoff(now, window)

        with self._lock:
            completed = [
                ses for ses in self.sessions
                if ses.get('entry_time', 0) >= cutoff and 'exit_time' in ses
            ]

        buckets = {'0-5': 0, '5-15': 0, '15-30': 0, '30-60': 0, '60+': 0}
        for ses in completed:
            mins = (ses['exit_time'] - ses['entry_time']) / 60.0
            if mins < 5:
                buckets['0-5'] += 1
            elif mins < 15:
                buckets['5-15'] += 1
            elif mins < 30:
                buckets['15-30'] += 1
            elif mins < 60:
                buckets['30-60'] += 1
            else:
                buckets['60+'] += 1
        return buckets


def _window_cutoff(now, window):
    windows = {'1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800}
    return now - windows.get(window, 86400)


# ── Background watchdog ────────────────────────────────────────────────────────

def start_watchdog(s):
    """Background thread: vacate spaces not seen for VACATE_TIMEOUT seconds."""
    def run():
        while not shutdown_event.wait(timeout=1.0):
            try:
                now = time.time()
                to_vacate = []
                with s._lock:
                    for space_id, last in s.last_seen.items():
                        if s.spaces[space_id]['occupied'] and (now - last) > VACATE_TIMEOUT:
                            to_vacate.append(space_id)
                for space_id in to_vacate:
                    s.vacate_space(space_id)
            except Exception as e:
                logger.error("Watchdog error: %s", e, exc_info=True)
    Thread(target=run, daemon=True, name="watchdog").start()


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class ParkingHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        logger.debug("HTTP %s", fmt % args)

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

        if path == '/api/stats':
            window = qs1('window', '24h')
            self._json(store.get_stats(window))
            return

        if path == '/api/history/durations':
            window = qs1('window', '24h')
            self._json(store.get_duration_buckets(window))
            return

        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        if self.path == '/api/ingest':
            try:
                data = json.loads(body)
                store.ingest(data)
                self._json({'ok': True})
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Bad ingest payload: %s", e)
                self.send_error(400, str(e))
        elif self.path.startswith('/api/release/'):
            space_id = self.path[len('/api/release/'):]
            if space_id in store.spaces:
                store.release_space(space_id)
                self._json({'ok': True})
            else:
                self.send_error(404, f'Unknown space: {space_id}')
        else:
            self.send_error(404)

    def _html(self, content):
        b = content.encode()
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
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


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        if issubclass(sys.exc_info()[0], BrokenPipeError):
            return
        logger.error("Unhandled error for request from %s", client_address, exc_info=True)


# ── Web server ─────────────────────────────────────────────────────────────────

def start_web_server(port):
    global web_server
    try:
        web_server = _ReusableHTTPServer(('0.0.0.0', port), ParkingHandler)
    except Exception as e:
        logger.error("Could not bind to port %d: %s", port, e)
        raise

    def run():
        try:
            web_server.serve_forever()
        except Exception as e:
            logger.error("Web server error: %s", e, exc_info=True)

    Thread(target=run, daemon=True, name="http").start()
    logger.info("Parking Dashboard running at http://localhost:%d", port)


# ── Signal handler ─────────────────────────────────────────────────────────────

def signal_handler(signum, _):
    logger.info("Signal %s received, shutting down.", signal.Signals(signum).name)
    shutdown_event.set()
    if web_server:
        web_server.shutdown()


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

def _build_html():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NX AI · Parking Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:        #0f1117;
  --surface:   #1a1d27;
  --border:    #2a2d3e;
  --accent:    #3b82f6;
  --occupied:  #ef4444;
  --available: #22c55e;
  --cooldown:  #f59e0b;
  --text:      #e2e8f0;
  --text-dim:  #64748b;
  --surface2:  #1e2130;
}
*,*::before,*::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  padding: 16px;
}
.page { max-width: 1400px; margin: 0 auto; }

/* Header */
.header {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 24px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}
.header h1 {
  font-size: 20px;
  font-weight: 700;
  color: var(--accent);
  letter-spacing: 0.5px;
}
.header p {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 2px;
}
.live-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 1px;
  background: rgba(34,197,94,0.08);
  color: var(--available);
  border: 1px solid rgba(34,197,94,0.3);
}
.pulse-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--available);
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(.7)} }

/* KPI row */
.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}
@media(max-width:700px) { .kpi-row { grid-template-columns: repeat(2,1fr); } }
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 18px 20px;
}
.kpi-label {
  font-size: 10px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  margin-bottom: 8px;
}
.kpi-value {
  font-size: 30px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  font-family: 'SF Mono', 'Fira Code', monospace;
}
.kpi-value.blue   { color: var(--accent); }
.kpi-value.green  { color: var(--available); }
.kpi-value.yellow { color: var(--cooldown); }
.kpi-value.red    { color: var(--occupied); }

/* Panel */
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px;
  margin-bottom: 16px;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
  flex-wrap: wrap;
  gap: 8px;
}
.panel-title {
  font-size: 11px;
  font-weight: 700;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.8px;
}

/* Lot SVG wrapper */
.lot-wrap {
  position: relative;
  background: var(--bg);
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.lot-wrap svg {
  width: 100%;
  height: auto;
  display: block;
}

/* Availability badge */
.avail-badge {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  font-family: 'SF Mono', 'Fira Code', monospace;
}
.avail-badge .count-green { color: var(--available); }

/* Toggle button */
.toggle-btn {
  padding: 6px 16px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  transition: all 0.2s;
}
.toggle-btn:hover,
.toggle-btn.active {
  border-color: var(--accent);
  color: var(--accent);
  background: rgba(59,130,246,0.08);
}

/* Two-column row */
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
@media(max-width:900px) { .two-col { grid-template-columns: 1fr; } }
.two-col .panel { margin-bottom: 0; }

/* Sessions table */
.sessions-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.sessions-table th {
  text-align: left;
  padding: 8px 10px;
  font-size: 10px;
  font-weight: 700;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
.sessions-table th:hover { color: var(--accent); }
.sessions-table td {
  padding: 9px 10px;
  border-bottom: 1px solid rgba(42,45,62,0.5);
  font-variant-numeric: tabular-nums;
}
.sessions-table tr:last-child td { border-bottom: none; }
.sessions-table tbody tr { transition: background 0.15s; }
.sessions-table tbody tr:hover { background: rgba(59,130,246,0.06); }
.sessions-table tbody tr.highlighted { background: rgba(59,130,246,0.12); }
.space-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px;
  font-weight: 700;
  background: rgba(59,130,246,0.15);
  color: var(--accent);
}
.cls-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 4px;
  background: rgba(255,255,255,0.06);
  color: var(--text-dim);
}
.duration-cell {
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 13px;
  color: var(--available);
}
.no-data {
  text-align: center;
  padding: 24px;
  color: var(--text-dim);
  font-size: 13px;
}

/* Window selector */
.window-btns {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.win-btn {
  padding: 4px 12px;
  border-radius: 14px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
  transition: all 0.15s;
}
.win-btn.active,
.win-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: rgba(59,130,246,0.08);
}

/* Chart wrapper */
.chart-wrap { position: relative; height: 220px; }

/* Heatmap legend */
.heat-legend {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  font-size: 10px;
  color: var(--text-dim);
}
.heat-gradient {
  flex: 1;
  height: 8px;
  border-radius: 4px;
  background: linear-gradient(to right, #1d4ed8, #fbbf24, #dc2626);
}

/* Bay release button (SVG) */
.bay-btn { cursor: pointer; }
.bay-btn rect { transition: fill 0.15s; }
.bay-btn:hover rect { fill: rgba(34,197,94,0.32) !important; }

/* Scrollbar */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>
<div class="page">

<!-- Header -->
<div class="header">
  <div>
    <h1>NX AI &middot; Parking Dashboard</h1>
    <p>Real-time parking space monitoring &nbsp;&middot;&nbsp; NX AI Manager</p>
  </div>
  <span class="live-badge">
    <span class="pulse-dot"></span>
    LIVE
  </span>
</div>

<!-- KPI Cards -->
<div class="kpi-row">
  <div class="kpi-card">
    <div class="kpi-label">Currently Occupied</div>
    <div class="kpi-value blue" id="kpi-occupied">0 / 6</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Sessions Today</div>
    <div class="kpi-value green" id="kpi-sessions">0</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Avg Duration</div>
    <div class="kpi-value yellow" id="kpi-avg-dur">— min</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">Most Used Space</div>
    <div class="kpi-value red" id="kpi-most-used">—</div>
  </div>
</div>

<!-- Lot Diagram -->
<div class="panel">
  <div class="panel-header">
    <span class="panel-title">Lot Diagram</span>
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
      <span class="avail-badge" id="avail-badge">
        <span id="avail-count" class="count-green">6</span> / 6 Available
      </span>
      <button class="toggle-btn active" id="toggle-view-btn" onclick="toggleView()">Live View</button>
    </div>
  </div>
  <div class="lot-wrap">
    <svg id="lot-svg" viewBox="0 0 720 200" xmlns="http://www.w3.org/2000/svg">
      <!-- Background -->
      <rect width="720" height="200" fill="#0f1117"/>
      <!-- Subtle grid lines -->
      <line x1="0" y1="100" x2="720" y2="100" stroke="#1a1d27" stroke-width="1"/>
      <line x1="360" y1="0" x2="360" y2="200" stroke="#1a1d27" stroke-width="1"/>
      <!-- Floor label -->
      <text x="360" y="195" text-anchor="middle" fill="#2a2d3e" font-size="9" font-family="monospace">PARKING LOT — CAMERA VIEW</text>

      <!-- Bays: 6 spaces, each 108px wide, 8px gap, starting at x=6 -->
      <!-- P1 -->
      <g id="bay-P1" class="bay" data-space="P1" style="cursor:pointer" onclick="highlightSpace('P1')">
        <rect id="rect-P1" x="6" y="16" width="108" height="166" rx="6" ry="6"
              fill="rgba(34,197,94,0.07)" stroke="#22c55e" stroke-width="2.5"/>
        <text id="sid-P1" x="60" y="40" text-anchor="middle" fill="#22c55e"
              font-size="13" font-weight="700" font-family="monospace">P1</text>
        <text id="cls-P1" x="60" y="90" text-anchor="middle" fill="#94a3b8"
              font-size="11" font-family="sans-serif"></text>
        <text id="dur-P1" x="60" y="128" text-anchor="middle" fill="#64748b"
              font-size="10" font-family="monospace"></text>
        <g id="btn-P1" class="bay-btn" style="display:none" onclick="releaseSpace('P1',event)">
          <rect x="16" y="143" width="88" height="22" rx="4" ry="4"
                fill="rgba(34,197,94,0.15)" stroke="#22c55e" stroke-width="1.5"/>
          <text x="60" y="158" text-anchor="middle" fill="#22c55e"
                font-size="9" font-family="sans-serif" font-weight="700">Mark Free</text>
        </g>
        <!-- Heat overlay -->
        <rect id="heat-P1" x="6" y="16" width="108" height="166" rx="6" ry="6"
              fill="transparent" stroke="none" opacity="0.65" style="display:none"/>
      </g>
      <!-- P2 -->
      <g id="bay-P2" class="bay" data-space="P2" style="cursor:pointer" onclick="highlightSpace('P2')">
        <rect id="rect-P2" x="120" y="16" width="108" height="166" rx="6" ry="6"
              fill="rgba(34,197,94,0.07)" stroke="#22c55e" stroke-width="2.5"/>
        <text id="sid-P2" x="174" y="40" text-anchor="middle" fill="#22c55e"
              font-size="13" font-weight="700" font-family="monospace">P2</text>
        <text id="cls-P2" x="174" y="90" text-anchor="middle" fill="#94a3b8"
              font-size="11" font-family="sans-serif"></text>
        <text id="dur-P2" x="174" y="128" text-anchor="middle" fill="#64748b"
              font-size="10" font-family="monospace"></text>
        <g id="btn-P2" class="bay-btn" style="display:none" onclick="releaseSpace('P2',event)">
          <rect x="130" y="143" width="88" height="22" rx="4" ry="4"
                fill="rgba(34,197,94,0.15)" stroke="#22c55e" stroke-width="1.5"/>
          <text x="174" y="158" text-anchor="middle" fill="#22c55e"
                font-size="9" font-family="sans-serif" font-weight="700">Mark Free</text>
        </g>
        <rect id="heat-P2" x="120" y="16" width="108" height="166" rx="6" ry="6"
              fill="transparent" stroke="none" opacity="0.65" style="display:none"/>
      </g>
      <!-- P3 -->
      <g id="bay-P3" class="bay" data-space="P3" style="cursor:pointer" onclick="highlightSpace('P3')">
        <rect id="rect-P3" x="234" y="16" width="108" height="166" rx="6" ry="6"
              fill="rgba(34,197,94,0.07)" stroke="#22c55e" stroke-width="2.5"/>
        <text id="sid-P3" x="288" y="40" text-anchor="middle" fill="#22c55e"
              font-size="13" font-weight="700" font-family="monospace">P3</text>
        <text id="cls-P3" x="288" y="90" text-anchor="middle" fill="#94a3b8"
              font-size="11" font-family="sans-serif"></text>
        <text id="dur-P3" x="288" y="128" text-anchor="middle" fill="#64748b"
              font-size="10" font-family="monospace"></text>
        <g id="btn-P3" class="bay-btn" style="display:none" onclick="releaseSpace('P3',event)">
          <rect x="244" y="143" width="88" height="22" rx="4" ry="4"
                fill="rgba(34,197,94,0.15)" stroke="#22c55e" stroke-width="1.5"/>
          <text x="288" y="158" text-anchor="middle" fill="#22c55e"
                font-size="9" font-family="sans-serif" font-weight="700">Mark Free</text>
        </g>
        <rect id="heat-P3" x="234" y="16" width="108" height="166" rx="6" ry="6"
              fill="transparent" stroke="none" opacity="0.65" style="display:none"/>
      </g>
      <!-- P4 -->
      <g id="bay-P4" class="bay" data-space="P4" style="cursor:pointer" onclick="highlightSpace('P4')">
        <rect id="rect-P4" x="348" y="16" width="108" height="166" rx="6" ry="6"
              fill="rgba(34,197,94,0.07)" stroke="#22c55e" stroke-width="2.5"/>
        <text id="sid-P4" x="402" y="40" text-anchor="middle" fill="#22c55e"
              font-size="13" font-weight="700" font-family="monospace">P4</text>
        <text id="cls-P4" x="402" y="90" text-anchor="middle" fill="#94a3b8"
              font-size="11" font-family="sans-serif"></text>
        <text id="dur-P4" x="402" y="128" text-anchor="middle" fill="#64748b"
              font-size="10" font-family="monospace"></text>
        <g id="btn-P4" class="bay-btn" style="display:none" onclick="releaseSpace('P4',event)">
          <rect x="358" y="143" width="88" height="22" rx="4" ry="4"
                fill="rgba(34,197,94,0.15)" stroke="#22c55e" stroke-width="1.5"/>
          <text x="402" y="158" text-anchor="middle" fill="#22c55e"
                font-size="9" font-family="sans-serif" font-weight="700">Mark Free</text>
        </g>
        <rect id="heat-P4" x="348" y="16" width="108" height="166" rx="6" ry="6"
              fill="transparent" stroke="none" opacity="0.65" style="display:none"/>
      </g>
      <!-- P5 -->
      <g id="bay-P5" class="bay" data-space="P5" style="cursor:pointer" onclick="highlightSpace('P5')">
        <rect id="rect-P5" x="462" y="16" width="108" height="166" rx="6" ry="6"
              fill="rgba(34,197,94,0.07)" stroke="#22c55e" stroke-width="2.5"/>
        <text id="sid-P5" x="516" y="40" text-anchor="middle" fill="#22c55e"
              font-size="13" font-weight="700" font-family="monospace">P5</text>
        <text id="cls-P5" x="516" y="90" text-anchor="middle" fill="#94a3b8"
              font-size="11" font-family="sans-serif"></text>
        <text id="dur-P5" x="516" y="128" text-anchor="middle" fill="#64748b"
              font-size="10" font-family="monospace"></text>
        <g id="btn-P5" class="bay-btn" style="display:none" onclick="releaseSpace('P5',event)">
          <rect x="472" y="143" width="88" height="22" rx="4" ry="4"
                fill="rgba(34,197,94,0.15)" stroke="#22c55e" stroke-width="1.5"/>
          <text x="516" y="158" text-anchor="middle" fill="#22c55e"
                font-size="9" font-family="sans-serif" font-weight="700">Mark Free</text>
        </g>
        <rect id="heat-P5" x="462" y="16" width="108" height="166" rx="6" ry="6"
              fill="transparent" stroke="none" opacity="0.65" style="display:none"/>
      </g>
      <!-- P6 -->
      <g id="bay-P6" class="bay" data-space="P6" style="cursor:pointer" onclick="highlightSpace('P6')">
        <rect id="rect-P6" x="576" y="16" width="138" height="166" rx="6" ry="6"
              fill="rgba(34,197,94,0.07)" stroke="#22c55e" stroke-width="2.5"/>
        <text id="sid-P6" x="645" y="40" text-anchor="middle" fill="#22c55e"
              font-size="13" font-weight="700" font-family="monospace">P6</text>
        <text id="cls-P6" x="645" y="90" text-anchor="middle" fill="#94a3b8"
              font-size="11" font-family="sans-serif"></text>
        <text id="dur-P6" x="645" y="128" text-anchor="middle" fill="#64748b"
              font-size="10" font-family="monospace"></text>
        <g id="btn-P6" class="bay-btn" style="display:none" onclick="releaseSpace('P6',event)">
          <rect x="590" y="143" width="110" height="22" rx="4" ry="4"
                fill="rgba(34,197,94,0.15)" stroke="#22c55e" stroke-width="1.5"/>
          <text x="645" y="158" text-anchor="middle" fill="#22c55e"
                font-size="9" font-family="sans-serif" font-weight="700">Mark Free</text>
        </g>
        <rect id="heat-P6" x="576" y="16" width="138" height="166" rx="6" ry="6"
              fill="transparent" stroke="none" opacity="0.65" style="display:none"/>
      </g>
    </svg>
  </div>
  <!-- Heatmap legend (hidden in live mode) -->
  <div class="heat-legend" id="heat-legend" style="display:none">
    <span>Low</span>
    <div class="heat-gradient"></div>
    <span>High</span>
  </div>
</div>

<!-- Active Sessions + Frequency Chart -->
<div class="two-col">
  <!-- Active Sessions Table -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Active Sessions</span>
      <span style="font-size:11px;color:var(--text-dim)" id="active-count-label">0 active</span>
    </div>
    <table class="sessions-table">
      <thead>
        <tr>
          <th onclick="sortTable('space')">Space <span id="sort-space"></span></th>
          <th onclick="sortTable('cls')">Type</th>
          <th onclick="sortTable('entry')">Entry Time</th>
          <th onclick="sortTable('duration')">Duration <span id="sort-duration">&#8597;</span></th>
        </tr>
      </thead>
      <tbody id="sessions-tbody">
        <tr><td colspan="4" class="no-data">No active sessions</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Frequency Bar Chart -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Space Utilization</span>
      <div class="window-btns">
        <button class="win-btn" onclick="setStatsWindow('1h',this)">1h</button>
        <button class="win-btn" onclick="setStatsWindow('6h',this)">6h</button>
        <button class="win-btn active" onclick="setStatsWindow('24h',this)">24h</button>
        <button class="win-btn" onclick="setStatsWindow('7d',this)">7d</button>
      </div>
    </div>
    <div class="chart-wrap">
      <canvas id="freqChart"></canvas>
    </div>
  </div>
</div>

<!-- Duration Histogram -->
<div class="panel">
  <div class="panel-header">
    <span class="panel-title">Session Duration Distribution</span>
    <div class="window-btns">
      <button class="win-btn" onclick="setHistWindow('1h',this)">1h</button>
      <button class="win-btn" onclick="setHistWindow('6h',this)">6h</button>
      <button class="win-btn active" onclick="setHistWindow('24h',this)">24h</button>
      <button class="win-btn" onclick="setHistWindow('7d',this)">7d</button>
    </div>
  </div>
  <div class="chart-wrap" style="height:200px">
    <canvas id="histChart"></canvas>
  </div>
</div>

</div><!-- .page -->

<script>
// ── State ──────────────────────────────────────────────────────────────────────
let currentStatus   = null;
let statsWindow     = '24h';
let histWindow      = '24h';
let heatmapMode     = false;
let selectedSpace   = null;
let sortKey         = 'duration';
let sortDesc        = true;
let sessionEntryTimes = {}; // space_id -> entry_time (unix float) for live timers

// Bay center-X for SVG (used for highlight animation)
const BAY_CX = { P1: 60, P2: 174, P3: 288, P4: 402, P5: 516, P6: 645 };
const SPACES  = ['P1','P2','P3','P4','P5','P6'];

// ── Chart.js setup ─────────────────────────────────────────────────────────────
Chart.defaults.color       = '#64748b';
Chart.defaults.borderColor = '#2a2d3e';

const DARK_GRID = { color: 'rgba(255,255,255,0.04)' };

let freqChart, histChart;

function initCharts() {
  freqChart = new Chart(document.getElementById('freqChart'), {
    type: 'bar',
    data: {
      labels: SPACES,
      datasets: [{
        label: 'Sessions',
        data:  [0,0,0,0,0,0],
        backgroundColor: SPACES.map(() => '#3b82f6'),
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1d27',
          borderColor: '#2a2d3e',
          borderWidth: 1,
          padding: 10,
          titleColor: '#e2e8f0',
          bodyColor: '#94a3b8',
        }
      },
      scales: {
        x: { grid: DARK_GRID, ticks: { color: '#64748b', font: { size: 11 } } },
        y: {
          beginAtZero: true,
          grid: DARK_GRID,
          ticks: {
            color: '#64748b',
            font: { size: 11 },
            stepSize: 1,
            callback: v => Number.isInteger(v) ? v : null
          }
        }
      }
    }
  });

  histChart = new Chart(document.getElementById('histChart'), {
    type: 'bar',
    data: {
      labels: ['0–5 min', '5–15 min', '15–30 min', '30–60 min', '60+ min'],
      datasets: [{
        label: 'Sessions',
        data:  [0,0,0,0,0],
        backgroundColor: ['#1d4ed8','#2563eb','#f59e0b','#ef4444','#dc2626'],
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1d27',
          borderColor: '#2a2d3e',
          borderWidth: 1,
          padding: 10,
          titleColor: '#e2e8f0',
          bodyColor: '#94a3b8',
        }
      },
      scales: {
        x: { grid: DARK_GRID, ticks: { color: '#64748b', font: { size: 11 } } },
        y: {
          beginAtZero: true,
          grid: DARK_GRID,
          ticks: {
            color: '#64748b',
            font: { size: 11 },
            stepSize: 1,
            callback: v => Number.isInteger(v) ? v : null
          }
        }
      }
    }
  });
}

// ── Heatmap heat color ─────────────────────────────────────────────────────────
function heatColor(t) {
  // t in [0,1]: blue(0) -> yellow(0.5) -> red(1)
  if (t <= 0.5) {
    const f = t * 2;
    const r = Math.round(29  + (251-29)  * f);
    const g = Math.round(78  + (191-78)  * f);
    const b = Math.round(216 + (36-216)  * f);
    return `rgb(${r},${g},${b})`;
  } else {
    const f = (t - 0.5) * 2;
    const r = Math.round(251 + (220-251) * f);
    const g = Math.round(191 + (38-191)  * f);
    const b = Math.round(36  + (38-36)   * f);
    return `rgb(${r},${g},${b})`;
  }
}

// ── Update lot diagram ─────────────────────────────────────────────────────────
function updateLot(spaces) {
  if (heatmapMode) return; // don't override heatmap
  let availCount = 0;
  for (const sid of SPACES) {
    const s    = spaces[sid];
    const rect = document.getElementById('rect-' + sid);
    const sidEl = document.getElementById('sid-' + sid);
    const clsEl = document.getElementById('cls-' + sid);
    const durEl = document.getElementById('dur-' + sid);

    if (!s) continue;

    if (s.occupied) {
      // Red — occupied
      rect.setAttribute('fill',   'rgba(239,68,68,0.12)');
      rect.setAttribute('stroke', '#ef4444');
      sidEl.setAttribute('fill',  '#ef4444');
      clsEl.textContent = s.vehicle_class || '';
      clsEl.setAttribute('fill', '#e2e8f0');
      // Duration updated by live timer
      if (s.entry_time) {
        sessionEntryTimes[sid] = s.entry_time;
      }
    } else if (s.cooldown) {
      // Yellow — cooldown
      rect.setAttribute('fill',   'rgba(245,158,11,0.10)');
      rect.setAttribute('stroke', '#f59e0b');
      sidEl.setAttribute('fill',  '#f59e0b');
      clsEl.textContent = 'Cooldown';
      clsEl.setAttribute('fill', '#f59e0b');
      durEl.textContent = '';
      delete sessionEntryTimes[sid];
      availCount++;
    } else {
      // Green — available
      rect.setAttribute('fill',   'rgba(34,197,94,0.07)');
      rect.setAttribute('stroke', '#22c55e');
      sidEl.setAttribute('fill',  '#22c55e');
      clsEl.textContent = '';
      durEl.textContent = '';
      delete sessionEntryTimes[sid];
      availCount++;
    }

    // Show/hide Mark Free button
    const btnEl = document.getElementById('btn-' + sid);
    if (btnEl) btnEl.style.display = (s.occupied || s.cooldown) ? '' : 'none';

    // Highlight selected
    if (sid === selectedSpace) {
      rect.setAttribute('stroke-width', '4');
    } else {
      rect.setAttribute('stroke-width', '2.5');
    }
  }
  document.getElementById('avail-count').textContent = availCount;
}

// ── Live duration timers ───────────────────────────────────────────────────────
function fmtDuration(secs) {
  secs = Math.floor(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function tickTimers() {
  const now = Date.now() / 1000;
  for (const [sid, entryTime] of Object.entries(sessionEntryTimes)) {
    const elapsed = now - entryTime;
    const durEl   = document.getElementById('dur-' + sid);
    if (durEl) durEl.textContent = fmtDuration(elapsed);
  }
}

setInterval(tickTimers, 1000);

// ── KPI updates ────────────────────────────────────────────────────────────────
function updateKpi(status) {
  document.getElementById('kpi-occupied').textContent =
    status.active_count + ' / 6';
  document.getElementById('kpi-sessions').textContent =
    status.sessions_today;
  const avg = status.avg_duration_min;
  document.getElementById('kpi-avg-dur').textContent =
    avg > 0 ? avg.toFixed(1) + ' min' : '— min';
  document.getElementById('kpi-most-used').textContent =
    status.most_used || '—';
}

// ── Sessions table ─────────────────────────────────────────────────────────────
let activeSessionsData = [];

function updateSessionsTable(spaces) {
  const now = Date.now() / 1000;
  activeSessionsData = [];

  for (const sid of SPACES) {
    const s = spaces[sid];
    if (!s || !s.occupied) continue;
    activeSessionsData.push({
      space:    sid,
      cls:      s.vehicle_class || 'Unknown',
      entry:    s.entry_time || 0,
      duration: s.entry_time ? (now - s.entry_time) / 60.0 : 0,
    });
  }

  renderSessionsTable();
}

function renderSessionsTable() {
  const now    = Date.now() / 1000;
  let data     = [...activeSessionsData];

  // Sort
  data.sort((a, b) => {
    let va = a[sortKey], vb = b[sortKey];
    if (typeof va === 'string') va = va.toLowerCase();
    if (typeof vb === 'string') vb = vb.toLowerCase();
    return sortDesc ? (vb > va ? 1 : -1) : (va > vb ? 1 : -1);
  });

  const tbody = document.getElementById('sessions-tbody');
  if (data.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="no-data">No active sessions</td></tr>';
    document.getElementById('active-count-label').textContent = '0 active';
    return;
  }

  document.getElementById('active-count-label').textContent = data.length + ' active';

  tbody.innerHTML = data.map(row => {
    const entryStr  = row.entry
      ? new Date(row.entry * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
      : '—';
    const durStr    = fmtDuration((now - row.entry) * (row.entry > 0 ? 1 : 0));
    const highlight = row.space === selectedSpace ? ' highlighted' : '';
    return `<tr class="${highlight}" onclick="highlightSpace('${row.space}')">
      <td><span class="space-badge">${row.space}</span></td>
      <td><span class="cls-badge">${row.cls}</span></td>
      <td style="font-size:12px;color:var(--text-dim)">${entryStr}</td>
      <td class="duration-cell">${durStr}</td>
    </tr>`;
  }).join('');
}

// Refresh table durations every second
setInterval(renderSessionsTable, 1000);

function sortTable(key) {
  if (sortKey === key) { sortDesc = !sortDesc; }
  else { sortKey = key; sortDesc = true; }
  renderSessionsTable();
}

// ── Manual release ─────────────────────────────────────────────────────────────
async function releaseSpace(sid, evt) {
  if (evt) evt.stopPropagation();
  try {
    await fetch('/api/release/' + sid, { method: 'POST' });
    await pollStatus();
  } catch(e) {
    console.warn('Release error:', e);
  }
}

// ── Space highlight ────────────────────────────────────────────────────────────
function highlightSpace(sid) {
  selectedSpace = (selectedSpace === sid) ? null : sid;
  if (currentStatus) updateLot(currentStatus.spaces);
  renderSessionsTable();
}

// ── Status polling ─────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const res    = await fetch('/api/status');
    const status = await res.json();
    currentStatus = status;
    updateKpi(status);
    updateLot(status.spaces);
    updateSessionsTable(status.spaces);
    const availCount = 6 - status.active_count;
    document.getElementById('avail-badge').innerHTML =
      `<span class="count-green">${availCount}</span> / 6 Available`;
  } catch(e) {
    console.warn('Status poll error:', e);
  }
}

setInterval(pollStatus, 2000);

// ── Heatmap toggle ─────────────────────────────────────────────────────────────
async function toggleView() {
  heatmapMode = !heatmapMode;
  const btn = document.getElementById('toggle-view-btn');
  btn.textContent = heatmapMode ? 'Heatmap View' : 'Live View';
  btn.classList.toggle('active', !heatmapMode);
  document.getElementById('heat-legend').style.display = heatmapMode ? 'flex' : 'none';

  if (heatmapMode) {
    await applyHeatmap();
  } else {
    // Restore live view colors
    for (const el of document.querySelectorAll('[id^="heat-"]')) {
      el.style.display = 'none';
    }
    if (currentStatus) updateLot(currentStatus.spaces);
  }
}

async function applyHeatmap() {
  try {
    const res   = await fetch('/api/stats?window=' + statsWindow);
    const stats = await res.json();
    const maxCount = Math.max(...stats.map(s => s.session_count), 1);

    for (const s of stats) {
      const t      = s.session_count / maxCount;
      const color  = heatColor(t);
      const heatEl = document.getElementById('heat-' + s.space_id);
      const rectEl = document.getElementById('rect-' + s.space_id);
      if (!heatEl || !rectEl) continue;

      heatEl.setAttribute('fill', color);
      heatEl.style.display = '';
      rectEl.setAttribute('stroke', color);
      rectEl.setAttribute('fill',   color.replace('rgb', 'rgba').replace(')', ',0.12)'));

      // Reset text to space ID color
      const sidEl = document.getElementById('sid-' + s.space_id);
      if (sidEl) sidEl.setAttribute('fill', color);
    }
  } catch(e) {
    console.warn('Heatmap fetch error:', e);
  }
}

// ── Stats chart ────────────────────────────────────────────────────────────────
async function fetchStats() {
  try {
    const res   = await fetch('/api/stats?window=' + statsWindow);
    const stats = await res.json();
    const counts   = stats.map(s => s.session_count);
    const maxCount = Math.max(...counts, 1);

    // Color bars by relative session count (blue -> red)
    const colors = counts.map(c => heatColor(c / maxCount));

    freqChart.data.datasets[0].data            = counts;
    freqChart.data.datasets[0].backgroundColor = colors;
    freqChart.update();

    if (heatmapMode) await applyHeatmap();
  } catch(e) {
    console.warn('Stats fetch error:', e);
  }
}

function setStatsWindow(w, btn) {
  statsWindow = w;
  document.querySelectorAll('.panel:nth-of-type(3) .win-btn, ' +
    '.two-col .panel:last-child .win-btn').forEach(b => b.classList.remove('active'));
  // Mark correct button active
  document.querySelectorAll('[onclick^="setStatsWindow"]').forEach(b => {
    b.classList.toggle('active', b.textContent.trim() === w);
  });
  fetchStats();
}

// ── Duration histogram ─────────────────────────────────────────────────────────
async function fetchDurations() {
  try {
    const res     = await fetch('/api/history/durations?window=' + histWindow);
    const buckets = await res.json();
    histChart.data.datasets[0].data = [
      buckets['0-5']  || 0,
      buckets['5-15'] || 0,
      buckets['15-30']|| 0,
      buckets['30-60']|| 0,
      buckets['60+']  || 0,
    ];
    histChart.update();
  } catch(e) {
    console.warn('Duration fetch error:', e);
  }
}

function setHistWindow(w, btn) {
  histWindow = w;
  document.querySelectorAll('[onclick^="setHistWindow"]').forEach(b => {
    b.classList.toggle('active', b.textContent.trim() === w);
  });
  fetchDurations();
}

// ── Init ───────────────────────────────────────────────────────────────────────
(function init() {
  initCharts();
  pollStatus();
  fetchStats();
  fetchDurations();
  // Periodic refresh of charts (less frequent than status)
  setInterval(fetchStats,     30000);
  setInterval(fetchDurations, 30000);
})();
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Parking Dashboard Web App')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'HTTP port (default: {DEFAULT_PORT})')
    args = parser.parse_args()

    logger = logging.getLogger(__name__)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    store = ParkingStore()
    start_watchdog(store)
    start_web_server(args.port)

    logger.info("Parking Dashboard Web App started on port %d", args.port)
    logger.info("Dashboard: http://localhost:%d", args.port)

    try:
        shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt.")

    logger.info("Shutdown complete.")
