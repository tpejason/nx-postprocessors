#!/usr/bin/env python3
"""
Gauge Dashboard Web App — NX AI Manager
Receives needle readings from the gauge postprocessor and serves a
real-time fuel gauge at http://localhost:<port>/.
Alert fires when needle reaches E (empty).
"""
import os, sys, logging, logging.handlers, json, signal, time, argparse
from threading import Lock, Event
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

script_location = os.path.dirname(os.path.realpath(sys.argv[0]))
LOG_FILE     = os.path.join(script_location, "plugin.gauge-dashboard-app.log")
DEFAULT_PORT = 8113
EMPTY_THRESHOLD = 0.12

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - gauge-web-app - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=2),
    ]
)
logger = logging.getLogger(__name__)
shutdown_event = Event()


class GaugeState:
    def __init__(self):
        self._lock      = Lock()
        self.fuel_level = 0.5
        self.e_cx       = None
        self.f_cx       = None
        self.last_ts    = 0.0
        self.alert      = False

    def update(self, reading):
        with self._lock:
            needle_cx = reading.get('needle_cx')
            if needle_cx is None:
                return
            if reading.get('e_cx') is not None:
                self.e_cx = reading['e_cx']
            if reading.get('f_cx') is not None:
                self.f_cx = reading['f_cx']

            if self.e_cx is not None and self.f_cx is not None and self.f_cx > self.e_cx:
                raw = (needle_cx - self.e_cx) / (self.f_cx - self.e_cx)
            elif reading.get('width', 0) > 0:
                raw = (needle_cx / reading['width'] - 0.25) / 0.5
            else:
                return

            self.fuel_level = max(0.0, min(1.0, round(raw, 3)))
            self.last_ts    = time.time()
            self.alert      = self.fuel_level < EMPTY_THRESHOLD

    def to_dict(self):
        with self._lock:
            return {
                'fuel_level':  self.fuel_level,
                'alert':       self.alert,
                'last_ts':     self.last_ts,
                'calibrated':  self.e_cx is not None and self.f_cx is not None,
            }


gauge = GaugeState()

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gauge Dashboard · NX AI</title>
<style>
:root{
  --bg:#0a0a0f;--card:#131320;--border:#1e1e2e;
  --text:#e0e0f0;--dim:#6b7280;
  --cyan:#00e5ff;--green:#00ff88;--yellow:#ffe600;
  --orange:#ff9500;--red:#ff1744;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace;
     min-height:100vh;display:flex;flex-direction:column;align-items:center}
header{width:100%;padding:14px 24px;border-bottom:1px solid var(--border);
       display:flex;align-items:center;gap:10px}
.h-title{font-size:17px;font-weight:700;color:var(--cyan);letter-spacing:1px}
.h-sub{font-size:10px;color:var(--dim);margin-top:2px}
.live-pill{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:11px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--red);flex-shrink:0}
.dot.live{background:var(--green);animation:blink 1s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
main{display:flex;flex-direction:column;align-items:center;gap:20px;
     padding:28px 16px;width:100%;max-width:440px}
.alert-banner{width:100%;background:rgba(255,23,68,.12);border:1px solid var(--red);
              border-radius:10px;padding:14px 18px;display:flex;align-items:center;gap:12px;
              animation:pulse-banner .8s ease-in-out infinite alternate}
.alert-banner.hidden{display:none;animation:none}
.alert-icon{font-size:22px;flex-shrink:0}
.alert-title{font-size:14px;font-weight:700;color:var(--red);letter-spacing:.5px}
.alert-sub{font-size:11px;color:#ff6b6b;margin-top:3px}
@keyframes pulse-banner{from{opacity:1}to{opacity:.55}}
.gauge-card{background:var(--card);border:1px solid var(--border);border-radius:16px;
            padding:28px 20px 20px;width:100%;display:flex;flex-direction:column;
            align-items:center;gap:14px;transition:border-color .3s,box-shadow .3s}
.gauge-card.alert{border-color:var(--red)!important;
                  animation:pulse-card 1s ease-in-out infinite alternate}
@keyframes pulse-card{
  from{box-shadow:0 0 15px rgba(255,23,68,.15)}
  to  {box-shadow:0 0 40px rgba(255,23,68,.45)}}
.gauge-svg{width:280px;height:240px;overflow:visible}
.level-row{display:flex;align-items:baseline;gap:6px}
.level-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--dim)}
.level-val{font-size:38px;font-weight:700;color:var(--cyan);transition:color .4s;
           font-variant-numeric:tabular-nums}
.level-val.c-red   {color:var(--red)!important}
.level-val.c-orange{color:var(--orange)!important}
.level-val.c-yellow{color:var(--yellow)!important}
.level-val.c-green {color:var(--green)!important}
.status-row{display:flex;gap:10px;width:100%}
.sc{flex:1;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px 14px}
.sc-label{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.8px;margin-bottom:3px}
.sc-val{font-size:13px;font-weight:700}
</style>
</head>
<body>
<header>
  <div>
    <div class="h-title">NX AI · Gauge Dashboard</div>
    <div class="h-sub">Real-time fuel gauge monitoring · NX AI Manager</div>
  </div>
  <div class="live-pill">
    <span class="dot" id="dot"></span>
    <span id="live-lbl">Connecting…</span>
  </div>
</header>
<main>
  <div class="alert-banner hidden" id="alert-banner">
    <span class="alert-icon">⚠</span>
    <div>
      <div class="alert-title">FUEL LOW — EMPTY WARNING</div>
      <div class="alert-sub">Gauge needle is near E (Empty). Refuel immediately.</div>
    </div>
  </div>
  <div class="gauge-card" id="gauge-card">
    <svg class="gauge-svg" viewBox="0 0 280 240" id="svg">
      <defs>
        <filter id="glow" x="-30%" y="-30%" width="160%" height="160%">
          <feGaussianBlur stdDeviation="3" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      <circle cx="140" cy="148" r="118" fill="#0a0a14" stroke="#2a2a4a" stroke-width="3"/>
      <g id="arc-zones"></g>
      <path id="arc-track" fill="none" stroke="#1e1e3a" stroke-width="16" stroke-linecap="round"/>
      <path id="arc-fill"  fill="none" stroke="#00e5ff" stroke-width="16" stroke-linecap="round" opacity="0"/>
      <g id="ticks"></g>
      <text id="lbl-e"    text-anchor="middle" font-family="'Courier New',monospace"
            font-size="15" font-weight="700" fill="#ff1744">E</text>
      <text id="lbl-f"    text-anchor="middle" font-family="'Courier New',monospace"
            font-size="15" font-weight="700" fill="#00ff88">F</text>
      <text id="lbl-half" text-anchor="middle" font-family="'Courier New',monospace"
            font-size="11" fill="#ffe600">1/2</text>
      <text x="140" y="180" text-anchor="middle" font-family="'Courier New',monospace"
            font-size="10" fill="#4a4a6a" letter-spacing="4">FUEL</text>
      <g id="needle" transform="rotate(0,140,148)">
        <polygon points="140,50 136,148 144,148"
                 fill="white" filter="url(#glow)" id="needle-poly"/>
        <circle cx="140" cy="148" r="10" fill="#1e1e3a" stroke="#3a3a5a" stroke-width="2"/>
        <circle cx="140" cy="148" r="4"  fill="var(--cyan)" id="needle-dot"/>
      </g>
    </svg>
    <div class="level-row">
      <div class="level-label">Fuel Level</div>
      <div class="level-val" id="level-val">--</div>
    </div>
  </div>
  <div class="status-row">
    <div class="sc"><div class="sc-label">Status</div><div class="sc-val" id="s-status">--</div></div>
    <div class="sc"><div class="sc-label">Calibration</div><div class="sc-val" id="s-cal">--</div></div>
    <div class="sc"><div class="sc-label">Last Update</div><div class="sc-val" id="s-time">--</div></div>
  </div>
</main>
<script>
const CX=140,CY=148,R=102,E_ANG=-105,F_ANG=105,EMPTY_T=0.12;
const ZONES=[
  {a1:-105,a2:-63,color:'#ff1744'},
  {a1: -63,a2:-21,color:'#ff9500'},
  {a1: -21,a2: 21,color:'#ffe600'},
  {a1:  21,a2: 63,color:'#8bc34a'},
  {a1:  63,a2:105,color:'#00ff88'},
];
const TICKS=[
  {a:-105,len:16,w:2.5},{a:-63,len:10,w:1.5},{a:-21,len:10,w:1.5},
  {a:0,len:16,w:2.5},{a:21,len:10,w:1.5},{a:63,len:10,w:1.5},{a:105,len:16,w:2.5},
];
function polar(a,r){const rad=(a-90)*Math.PI/180;return{x:CX+r*Math.cos(rad),y:CY+r*Math.sin(rad)}}
function arcD(a1,a2,r){const p1=polar(a1,r),p2=polar(a2,r);return`M${p1.x.toFixed(2)},${p1.y.toFixed(2)} A${r},${r} 0 0,1 ${p2.x.toFixed(2)},${p2.y.toFixed(2)}`}
function fuelColor(f){return f<EMPTY_T?'var(--red)':f<0.25?'var(--orange)':f<0.55?'var(--yellow)':'var(--green)'}
function fuelClass(f){return f<EMPTY_T?'c-red':f<0.25?'c-orange':f<0.55?'c-yellow':'c-green'}
function buildGauge(){
  const NS='http://www.w3.org/2000/svg';
  document.getElementById('arc-track').setAttribute('d',arcD(E_ANG,F_ANG,R));
  const zg=document.getElementById('arc-zones');
  ZONES.forEach(z=>{const p=document.createElementNS(NS,'path');p.setAttribute('d',arcD(z.a1,z.a2,R));p.setAttribute('fill','none');p.setAttribute('stroke',z.color);p.setAttribute('stroke-width','14');p.setAttribute('stroke-linecap','butt');p.setAttribute('opacity','0.25');zg.appendChild(p)});
  const tg=document.getElementById('ticks');
  TICKS.forEach(t=>{const i=polar(t.a,R-7),o=polar(t.a,R-7-t.len);const ln=document.createElementNS(NS,'line');ln.setAttribute('x1',i.x.toFixed(2));ln.setAttribute('y1',i.y.toFixed(2));ln.setAttribute('x2',o.x.toFixed(2));ln.setAttribute('y2',o.y.toFixed(2));ln.setAttribute('stroke','#3a3a5a');ln.setAttribute('stroke-width',t.w);tg.appendChild(ln)});
  const ep=polar(E_ANG,R+20),fp=polar(F_ANG,R+20),hp=polar(0,R+22);
  function setXY(id,x,y){const el=document.getElementById(id);el.setAttribute('x',x.toFixed(1));el.setAttribute('y',y.toFixed(1))}
  setXY('lbl-e',ep.x,ep.y+5);setXY('lbl-f',fp.x,fp.y+5);setXY('lbl-half',hp.x,hp.y+4);
}
function setNeedle(fuel){
  const angle=E_ANG+fuel*(F_ANG-E_ANG);
  document.getElementById('needle').setAttribute('transform',`rotate(${angle.toFixed(2)},${CX},${CY})`);
  const col=fuelColor(fuel);
  document.getElementById('needle-poly').setAttribute('fill',col);
  document.getElementById('needle-dot').style.fill=col;
  const fa=document.getElementById('arc-fill');
  if(fuel>0.01){fa.setAttribute('d',arcD(E_ANG,E_ANG+fuel*(F_ANG-E_ANG),R));fa.setAttribute('stroke',col);fa.setAttribute('opacity','0.55')}
  else fa.setAttribute('opacity','0');
}
function updateUI(data){
  const fuel=data.fuel_level,isAlert=data.alert,pct=Math.round(fuel*100);
  setNeedle(fuel);
  const lv=document.getElementById('level-val');
  lv.textContent=pct+'%';lv.className='level-val '+fuelClass(fuel);
  document.getElementById('gauge-card').classList.toggle('alert',isAlert);
  document.getElementById('alert-banner').classList.toggle('hidden',!isAlert);
  const sEl=document.getElementById('s-status');
  sEl.textContent=isAlert?'EMPTY':fuel<0.25?'LOW':fuel<0.75?'NORMAL':'FULL';
  sEl.style.color=fuelColor(fuel);
  const cal=document.getElementById('s-cal');
  cal.textContent=data.calibrated?'LOCK':'EST';
  cal.style.color=data.calibrated?'var(--green)':'var(--yellow)';
  const age=Date.now()/1000-data.last_ts,live=age<4;
  document.getElementById('dot').className='dot'+(live?' live':'');
  document.getElementById('live-lbl').textContent=live?'LIVE':'No signal';
  document.getElementById('s-time').textContent=live?'LIVE':Math.round(age)+'s ago';
}
async function poll(){try{const r=await fetch('/api/status');updateUI(await r.json())}catch(e){}}
buildGauge();setNeedle(0.5);setInterval(poll,400);poll();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/status':
            self._json(gauge.to_dict())
        elif path in ('/', '/index.html'):
            body = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path == '/api/reading':
            length = int(self.headers.get('Content-Length', 0))
            try:
                gauge.update(json.loads(self.rfile.read(length)))
                self._json({'ok': True})
            except Exception as e:
                self._json({'error': str(e)}, 400)
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.end_headers()


def signal_handler(signum, _):
    logger.info("Signal %s — shutting down.", signal.Signals(signum).name)
    shutdown_event.set()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT,  signal_handler)

    # ThreadingHTTPServer: handle each request in its own thread so concurrent
    # browser polling + feeder POSTs never block each other (a single-threaded
    # HTTPServer deadlocks under the Nx client's embedded-view polling).
    server = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    server.daemon_threads = True
    from threading import Thread
    Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Gauge dashboard running at http://localhost:%d", args.port)

    shutdown_event.wait()
    server.shutdown()
