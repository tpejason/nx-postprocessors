#!/usr/bin/env python3
"""
HTML/JS templates for the Stress Dashboard web app.

Two self-contained pages, no external/CDN assets (works on an air-gapped
server and the report works offline via file://):

  * build_dashboard_html()                  -> live dashboard (polls /api/*)
  * build_report_html(session, summary, samples) -> downloadable stress report
"""
import json
import time
import html


# ── Shared CSS + canvas chart helper ────────────────────────────────────────

_CSS = """
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2a3340;
  --txt:#e6edf3; --muted:#8b949e; --accent:#2f81f7; --good:#3fb950;
  --warn:#d29922; --bad:#f85149; --fps:#a371f7; --cpu:#2f81f7;
  --igpu:#3fb950; --npu:#db61a2; --nv:#76e3ea; --ram:#d29922;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px}
h1,h2,h3{margin:0 0 .4em}
a{color:var(--accent);text-decoration:none}
header{padding:14px 20px;background:var(--panel);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header .title{font-size:18px;font-weight:600}
.badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--panel2);
  border:1px solid var(--border);color:var(--muted)}
.badge.on{color:var(--good);border-color:#1f5e2e}
.badge.off{color:var(--muted);opacity:.6}
.wrap{padding:18px 20px;max-width:1400px;margin:0 auto}
.grid{display:grid;gap:14px}
.cards{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
.gauge .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.gauge .val{font-size:30px;font-weight:700;line-height:1.1;margin-top:4px}
.gauge .val small{font-size:14px;font-weight:500;color:var(--muted)}
.gauge .sub{font-size:11px;color:var(--muted);margin-top:4px;min-height:14px}
.bar{height:6px;border-radius:3px;background:var(--panel2);margin-top:8px;overflow:hidden}
.bar > div{height:100%;background:var(--accent);width:0;transition:width .4s}
.fps .val{color:var(--fps)} .fps .bar>div{background:var(--fps)}
.cpu .bar>div{background:var(--cpu)} .ram .bar>div{background:var(--ram)}
.igpu .bar>div{background:var(--igpu)} .npu .bar>div{background:var(--npu)}
.nv .bar>div{background:var(--nv)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
input,textarea,button{font-family:inherit;font-size:13px}
input,textarea{background:var(--panel2);border:1px solid var(--border);color:var(--txt);
  border-radius:6px;padding:6px 8px}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 14px;
  cursor:pointer;font-weight:600}
button.stop{background:var(--bad)} button.ghost{background:var(--panel2);color:var(--txt);border:1px solid var(--border)}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.sec-title{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin:22px 0 8px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--muted)}
.dot.live{background:var(--bad);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:6px}
.legend span{display:flex;align-items:center;gap:5px}
.legend i{width:12px;height:3px;border-radius:2px;display:inline-block}
canvas{width:100%;display:block}
.mini{font-size:11px;color:var(--muted)}
"""

# Canvas multi-line chart. lines: [{key,color,axis:'left'|'right',label}]
# axisRight is fixed 0..100 (%) ; axisLeft auto-scales (FPS).
_CHART_JS = r"""
function fmt(n){ if(n==null) return '–'; return (Math.round(n*10)/10).toString(); }
function drawChart(cv, series, lines, opts){
  opts = opts||{};
  const dpr = window.devicePixelRatio||1;
  const W = cv.clientWidth, H = opts.height||220;
  cv.width = W*dpr; cv.height = H*dpr; cv.style.height = H+'px';
  const ctx = cv.getContext('2d'); ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,W,H);
  const padL=44, padR=44, padT=12, padB=22;
  const plotW=W-padL-padR, plotH=H-padT-padB;
  // axes ranges
  let leftMax=1;
  lines.filter(l=>l.axis==='left').forEach(l=>{
    series.forEach(s=>{ const v=s[l.key]; if(v!=null&&v>leftMax) leftMax=v; });
  });
  leftMax = Math.ceil(leftMax*1.15) || 1;
  const rightMax=100;
  const n=series.length;
  const x = i => padL + (n<=1?plotW/2:plotW*i/(n-1));
  const yL = v => padT + plotH - plotH*(v/leftMax);
  const yR = v => padT + plotH - plotH*(v/rightMax);
  // grid + right axis labels (%)
  ctx.strokeStyle='#2a3340'; ctx.fillStyle='#8b949e'; ctx.font='10px sans-serif';
  ctx.lineWidth=1;
  for(let g=0; g<=4; g++){
    const yy=padT+plotH*g/4; ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke();
    ctx.textAlign='right'; ctx.fillText(Math.round(rightMax*(1-g/4))+'%', W-6, yy+3);
    ctx.textAlign='left'; ctx.fillText(fmt(leftMax*(1-g/4)), 4, yy+3);
  }
  // lines
  lines.forEach(l=>{
    const Y = l.axis==='left'?yL:yR;
    ctx.beginPath(); ctx.strokeStyle=l.color; ctx.lineWidth=1.8;
    let started=false;
    series.forEach((s,i)=>{ const v=s[l.key]; if(v==null){return;}
      const px=x(i), py=Y(v);
      if(!started){ctx.moveTo(px,py);started=true;} else ctx.lineTo(px,py);
    });
    ctx.stroke();
  });
}
"""


def _embed_json(obj):
    return json.dumps(obj).replace("</", "<\\/")


# ════════════════════════════════════════════════════════════════════════════
#  Dashboard
# ════════════════════════════════════════════════════════════════════════════

def build_dashboard_html():
    return r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nx AI Manager — Stress Dashboard</title>
<style>__CSS__</style></head><body>
<header>
  <div class="title">Nx&nbsp;AI&nbsp;Manager · Stress&nbsp;Dashboard</div>
  <div class="badges" id="badges"></div>
  <div style="flex:1"></div>
  <div class="mini" id="host"></div>
</header>
<div class="wrap">

  <!-- Session control -->
  <div class="panel">
    <div class="row" style="justify-content:space-between">
      <h3 style="margin:0"><span class="dot" id="runDot"></span><span id="runState">No active run</span></h3>
      <div class="mini" id="runMeta"></div>
    </div>
    <div class="row" style="margin-top:10px" id="startForm">
      <input id="f_name" placeholder="Run name (e.g. yolov8n test)" style="min-width:200px">
      <input id="f_model" placeholder="Model" style="min-width:160px">
      <input id="f_res" placeholder="Resolution (e.g. 1080p)" style="width:150px">
      <input id="f_notes" placeholder="Notes" style="flex:1;min-width:160px">
      <button id="btnStart" onclick="startRun()">▶ Start run</button>
      <button id="btnStop" class="stop" onclick="stopRun()" style="display:none">■ Stop run</button>
    </div>
  </div>

  <!-- Gauges -->
  <div class="grid cards" id="gauges" style="margin-top:14px"></div>

  <!-- Server spec -->
  <div class="sec-title">Server spec</div>
  <div class="panel"><table class="kv" id="specTbl"><tbody>
    <tr><td>CPU</td><td id="sp_cpu" class="mini">…</td></tr>
    <tr><td>GPU</td><td id="sp_gpu" class="mini">…</td></tr>
    <tr><td>NPU</td><td id="sp_npu" class="mini">…</td></tr>
    <tr><td>RAM</td><td id="sp_ram" class="mini">…</td></tr>
  </tbody></table></div>

  <!-- Chart -->
  <div class="sec-title">Live timeline</div>
  <div class="panel">
    <div class="legend">
      <span><i style="background:var(--fps)"></i>Total FPS</span>
      <span><i style="background:var(--cpu)"></i>CPU %</span>
      <span><i style="background:var(--igpu)"></i>iGPU %</span>
      <span><i style="background:var(--npu)"></i>NPU %</span>
      <span><i style="background:var(--nv)"></i>NVIDIA %</span>
    </div>
    <canvas id="chart"></canvas>
  </div>

  <!-- Per-camera -->
  <div class="sec-title">Per-camera inference FPS</div>
  <div class="panel">
    <table><thead><tr>
      <th>Camera</th><th>Channel ID</th><th>Model</th><th>Primary stream</th><th>Secondary stream</th><th>Inference on</th><th>Device</th><th class="num">FPS</th><th style="width:18%"></th>
    </tr></thead><tbody id="camRows"><tr><td colspan="9" class="mini">Waiting for frames…</td></tr></tbody></table>
  </div>

  <!-- Sessions -->
  <div class="sec-title">Saved runs</div>
  <div class="panel">
    <div id="sessBar" style="display:flex;gap:10px;align-items:center;margin-bottom:8px">
      <button class="ghost" onclick="toggleAllSess()" id="selAllBtn">Select all</button>
      <button class="ghost" onclick="delSelected()" id="delSelBtn" disabled>Delete selected</button>
      <span class="mini" id="selCount"></span>
    </div>
    <table><thead><tr>
      <th style="width:24px"><input type="checkbox" id="sessAll" onclick="toggleAllSess(this.checked)"></th>
      <th>#</th><th>Name</th><th>Model</th><th class="num">Cams</th><th class="num">Dur</th>
      <th class="num">Samples</th><th>Status</th><th>Report</th><th></th>
    </tr></thead><tbody id="sessRows"></tbody></table>
  </div>
</div>

<script>__CHART_JS__

let SOURCES={}, ACTIVE=null, STREAMS={};

function badge(on,label){return '<span class="badge '+(on?'on':'off')+'">'+label+'</span>';}

async function loadSources(){
  SOURCES = await (await fetch('/api/sources')).json();
  const b=[];
  b.push(badge(SOURCES.cpu_ram,'CPU/RAM'));
  b.push(badge(SOURCES.intel_igpu,'iGPU'+(SOURCES.igpu_mode?(' · '+SOURCES.igpu_mode):'')));
  b.push(badge(SOURCES.intel_npu,'NPU'));
  b.push(badge(SOURCES.nvidia,'NVIDIA'));
  b.push(badge(SOURCES.root,'root'));
  document.getElementById('badges').innerHTML=b.join('');
  const sp=SOURCES.specs||{};
  document.getElementById('sp_cpu').textContent=sp.cpu||'n/a';
  document.getElementById('sp_gpu').textContent=sp.gpu||'n/a';
  document.getElementById('sp_npu').textContent=sp.npu||'n/a';
  document.getElementById('sp_ram').textContent=sp.ram||'n/a';
}

function normId(id){return (id||'').replace(/[{}]/g,'').toLowerCase();}
function fmtStream(s){return s?(s.fps?(s.res+' @ '+s.fps+'fps'):(s.res||'n/a')):'n/a';}

async function loadStreams(){
  try{ STREAMS = await (await fetch('/api/streams')).json() || {}; }catch(e){ STREAMS={}; }
}

function gaugeCard(cls,lbl,val,unit,sub,pct){
  return '<div class="panel gauge '+cls+'">'+
    '<div class="lbl">'+lbl+'</div>'+
    '<div class="val">'+val+(unit?'<small> '+unit+'</small>':'')+'</div>'+
    '<div class="sub">'+(sub||'')+'</div>'+
    (pct==null?'':'<div class="bar"><div style="width:'+Math.min(100,pct)+'%"></div></div>')+
    '</div>';
}

function renderGauges(now){
  const g=[];
  g.push(gaugeCard('fps','Total inference FPS', fmt(now.fps_total||0),'', (now.n_cameras||0)+' channel(s)', null));
  if(SOURCES.cpu_ram){
    g.push(gaugeCard('cpu','CPU', fmt(now.cpu_pct),'%','', now.cpu_pct));
    const rt=now.ram_total_mb, ru=now.ram_used_mb;
    g.push(gaugeCard('ram','RAM', fmt(now.ram_pct),'%',
      (ru?fmt(ru/1024)+' / '+fmt(rt/1024)+' GB':''), now.ram_pct));
  }
  if(SOURCES.intel_igpu){
    g.push(gaugeCard('igpu','Intel iGPU', now.igpu_pct==null?'–':fmt(now.igpu_pct),'%',
      (now.igpu_mode||'')+(now.metrics&&now.metrics.igpu&&now.metrics.igpu.freq_mhz?(' · '+now.metrics.igpu.freq_mhz+' MHz'):''), now.igpu_pct));
  }
  if(SOURCES.intel_npu){
    g.push(gaugeCard('npu','Intel NPU', now.npu_pct==null?'–':fmt(now.npu_pct),'%',
      (now.npu_freq_mhz!=null?now.npu_freq_mhz+' MHz':'')+(now.npu_mem_mb?(' · '+fmt(now.npu_mem_mb)+' MB'):''), now.npu_pct));
  }
  if(SOURCES.nvidia && now.metrics && now.metrics.nvidia){
    const nv=now.metrics.nvidia, g0=(nv.gpus&&nv.gpus[0])||{};
    g.push(gaugeCard('nv','NVIDIA', now.nvidia_pct==null?'–':fmt(now.nvidia_pct),'%',
      (g0.mem_used_mb?fmt(g0.mem_used_mb/1024)+' GB':'')+(g0.temp_c?(' · '+g0.temp_c+'°C'):''), now.nvidia_pct));
  }
  document.getElementById('gauges').innerHTML=g.join('');
}

function renderCams(cams){
  const tb=document.getElementById('camRows');
  if(!cams||!cams.length){tb.innerHTML='<tr><td colspan="9" class="mini">Waiting for frames…</td></tr>';return;}
  const maxf=Math.max(1,...cams.map(c=>c.fps||0));
  const dev=(SOURCES.device&&SOURCES.device!=='n/a')?SOURCES.device:'–';
  tb.innerHTML=cams.map(c=>{
    const nm=(c.name&&c.name!==c.id)?c.name:'';
    const si=STREAMS[normId(c.id)]||{};
    const infRes=(c.w&&c.h)?(c.w+'x'+c.h):'';
    let which='';
    if(si.secondary&&si.secondary.res===infRes) which=' (secondary)';
    else if(si.primary&&si.primary.res===infRes) which=' (primary)';
    const infDisp=infRes?(infRes.replace('x','×')+which):'–';
    return '<tr>'+
      '<td><input value="'+(nm||shortId(c.id))+'" style="width:140px" onchange="renameCam(\''+c.id+'\',this.value)"></td>'+
      '<td class="mini">'+shortId(c.id)+'</td>'+
      '<td class="mini">'+(si.model||'n/a')+'</td>'+
      '<td class="mini">'+fmtStream(si.primary)+'</td>'+
      '<td class="mini">'+fmtStream(si.secondary)+'</td>'+
      '<td class="mini">'+infDisp+'</td>'+
      '<td>'+dev+'</td>'+
      '<td class="num"><b>'+fmt(c.fps)+'</b></td>'+
      '<td><div class="bar"><div style="width:'+(100*(c.fps||0)/maxf)+'%;background:var(--fps)"></div></div></td>'+
    '</tr>';
  }).join('');
}
function shortId(id){return id&&id.length>12?id.slice(0,8)+'…':id;}

async function renameCam(id,name){ await fetch('/api/camera/name',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id,name:name})}); }

function fmtDur(s){ if(s==null)return '–'; s=Math.round(s); const m=Math.floor(s/60),ss=s%60;
  return m?(m+'m'+ss+'s'):(ss+'s'); }

async function loadSessions(){
  const d=await (await fetch('/api/sessions')).json();
  document.getElementById('sessRows').innerHTML=(d.sessions||[]).map(s=>{
    const dur = s.end_ts? fmtDur(s.end_ts-s.start_ts) : (s.status==='running'?'live':'–');
    const running = s.status==='running';
    return '<tr>'+
      '<td><input type="checkbox" class="sessChk" value="'+s.id+'"'+(running?' disabled title="running"':'')+' onclick="updSel()"></td>'+
      '<td>'+s.id+'</td><td>'+(s.name||'–')+'</td><td>'+(s.model||'–')+'</td>'+
      '<td class="num">'+(s.camera_count||0)+'</td><td class="num">'+dur+'</td>'+
      '<td class="num">'+(s.n_samples||0)+'</td>'+
      '<td>'+(running?'<span class="dot live"></span>live':s.status)+'</td>'+
      '<td><a href="/api/report.html?id='+s.id+'">HTML</a> · <a href="/api/report.csv?id='+s.id+'">CSV</a></td>'+
      '<td><button class="ghost" onclick="delSession('+s.id+')">✕</button></td>'+
    '</tr>';
  }).join('');
  updSel();
}
function sessChks(){ return Array.from(document.querySelectorAll('.sessChk')); }
function selectedIds(){ return sessChks().filter(c=>c.checked).map(c=>parseInt(c.value)); }
function updSel(){
  const sel=selectedIds(), boxes=sessChks().filter(c=>!c.disabled);
  document.getElementById('selCount').textContent = sel.length? sel.length+' selected' : '';
  document.getElementById('delSelBtn').disabled = sel.length===0;
  const all=document.getElementById('sessAll');
  all.checked = boxes.length>0 && sel.length===boxes.length;
  all.indeterminate = sel.length>0 && sel.length<boxes.length;
}
function toggleAllSess(force){
  const want = (typeof force==='boolean')? force : !document.getElementById('sessAll').checked;
  sessChks().forEach(c=>{ if(!c.disabled) c.checked=want; });
  updSel();
}
async function delSelected(){
  const ids=selectedIds();
  if(!ids.length) return;
  if(!confirm('Delete '+ids.length+' run(s)? This cannot be undone.')) return;
  await fetch('/api/session/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:ids})}); loadSessions(); }
async function delSession(id){ if(!confirm('Delete run #'+id+'?'))return;
  await fetch('/api/session/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id})}); loadSessions(); }

function applyActive(active,now){
  ACTIVE=active;
  const dot=document.getElementById('runDot'), st=document.getElementById('runState'),
        meta=document.getElementById('runMeta');
  if(active){
    dot.className='dot live';
    st.textContent='Recording: '+(active.name||('run #'+active.id));
    meta.textContent=[active.model&&('model '+active.model),active.resolution,
      (now?('FPS '+fmt(now.fps_total)+' · '+(now.n_cameras||0)+' cams'):'')].filter(Boolean).join('  ·  ');
    document.getElementById('btnStart').style.display='none';
    document.getElementById('btnStop').style.display='';
  }else{
    dot.className='dot'; st.textContent='No active run'; meta.textContent='';
    document.getElementById('btnStart').style.display='';
    document.getElementById('btnStop').style.display='none';
  }
}

async function startRun(){
  const body={ name:document.getElementById('f_name').value,
    model:document.getElementById('f_model').value,
    resolution:document.getElementById('f_res').value,
    notes:document.getElementById('f_notes').value,
    camera_count:(window._lastCams||0) };
  await fetch('/api/session/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  tick(); loadSessions();
}
async function stopRun(){ await fetch('/api/session/stop',{method:'POST'}); tick(); loadSessions(); }

async function tick(){
  try{
    const d=await (await fetch('/api/live?pts=300')).json();
    const now=d.now||{};
    window._lastCams = now.n_cameras||0;
    renderGauges(now);
    renderCams(now.cameras||[]);
    applyActive(d.active_session,now);
    const cv=document.getElementById('chart');
    drawChart(cv, d.series||[], [
      {key:'fps_total',color:getCss('--fps'),axis:'left'},
      {key:'cpu_pct',color:getCss('--cpu'),axis:'right'},
      {key:'igpu_pct',color:getCss('--igpu'),axis:'right'},
      {key:'npu_pct',color:getCss('--npu'),axis:'right'},
      {key:'nvidia_pct',color:getCss('--nv'),axis:'right'},
    ],{height:240});
  }catch(e){ console.error(e); }
}
function getCss(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}

(async function init(){
  await loadSources();
  await loadStreams();
  await loadSessions();
  document.getElementById('host').textContent =
    (SOURCES.active_session&&SOURCES.active_session.hostname)||'';
  tick();
  setInterval(tick,1000);
  setInterval(loadSessions,5000);
  setInterval(loadStreams,30000);
})();
</script>
</body></html>""".replace("__CSS__", _CSS).replace("__CHART_JS__", _CHART_JS)


# ════════════════════════════════════════════════════════════════════════════
#  Report (downloadable, self-contained)
# ════════════════════════════════════════════════════════════════════════════

def _stat_row(label, st, unit=""):
    def c(v):
        return "–" if v is None else f"{v:g}{unit}"
    return (f"<tr><td>{html.escape(label)}</td>"
            f"<td class='num'>{c(st['avg'])}</td><td class='num'>{c(st['peak'])}</td>"
            f"<td class='num'>{c(st['min'])}</td><td class='num'>{c(st['p95'])}</td></tr>")


def build_report_html(session, summary, samples):
    m = summary["metrics"]
    gen = time.strftime("%Y-%m-%d %H:%M:%S")
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session["start_ts"])) if session.get("start_ts") else "–"
    try:
        sources = json.loads(session.get("sources_json") or "{}")
    except Exception:
        sources = {}

    rows = []
    rows.append(_stat_row("Total inference FPS", m["fps_total"]))
    rows.append(_stat_row("CPU load", m["cpu_pct"], "%"))
    rows.append(_stat_row("RAM used", m["ram_used_mb"], " MB"))
    rows.append(_stat_row("RAM load", m["ram_pct"], "%"))
    if sources.get("intel_igpu"):
        rows.append(_stat_row("Intel iGPU load", m["igpu_pct"], "%"))
    if sources.get("intel_npu"):
        rows.append(_stat_row("Intel NPU load", m["npu_pct"], "%"))
        rows.append(_stat_row("Intel NPU memory", m["npu_mem_mb"], " MB"))
    if sources.get("nvidia"):
        rows.append(_stat_row("NVIDIA GPU load", m["nvidia_pct"], "%"))
    stat_rows = "\n".join(rows)

    try:
        import metrics
        streams = metrics.get_camera_streams()
    except Exception:
        streams = {}

    def _fmt_stream(s):
        if not s:
            return "n/a"
        return f"{s['res']} @ {s['fps']}fps" if s.get("fps") else (s.get("res") or "n/a")

    def _cam_cells(c):
        si = streams.get(str(c["device_id"]).strip("{}").lower(), {}) or {}
        prim, sec = si.get("primary"), si.get("secondary")
        inf_res = f"{c.get('inf_w', 0)}x{c.get('inf_h', 0)}" if c.get("inf_w") else "?"
        which = ""
        if sec and sec.get("res") == inf_res:
            which = " (secondary)"
        elif prim and prim.get("res") == inf_res:
            which = " (primary)"
        inf_disp = (inf_res + which) if inf_res != "?" else "n/a"
        return _fmt_stream(prim), _fmt_stream(sec), inf_disp, (si.get("model") or "n/a")

    # Inference device for this run: prefer what was stored at run start, else current.
    device = sources.get("device")
    if not device:
        try:
            import metrics as _m
            device = _m.get_inference_device()
        except Exception:
            device = "n/a"

    def _cam_row(c):
        prim, sec, inf, model = _cam_cells(c)
        return (f"<tr><td>{html.escape(str(c['name']))}</td>"
                f"<td class='mini'>{html.escape(c['device_id'])}</td>"
                f"<td>{html.escape(model)}</td>"
                f"<td>{html.escape(prim)}</td><td>{html.escape(sec)}</td>"
                f"<td>{html.escape(inf)}</td>"
                f"<td>{html.escape(str(device))}</td>"
                f"<td class='num'>{c['avg']:g}</td><td class='num'>{c['peak']:g}</td>"
                f"<td class='num'>{c['min']:g}</td><td class='num'>{c['p95']:g}</td></tr>")

    cam_rows = "\n".join(_cam_row(c) for c in summary["cameras"]) \
        or "<tr><td colspan='11' class='mini'>No per-camera data</td></tr>"

    # Compact series for embedded charts (relative seconds + key metrics).
    t0 = samples[0]["ts"] if samples else 0
    series = [{
        "t": round(s["ts"] - t0, 1),
        "fps_total": s.get("fps_total"),
        "cpu_pct": s.get("cpu_pct"),
        "igpu_pct": s.get("igpu_pct"),
        "npu_pct": s.get("npu_pct"),
        "nvidia_pct": s.get("nvidia_pct"),
        "ram_pct": s.get("ram_pct"),
    } for s in samples]

    try:
        import metrics
        specs = metrics.get_hardware_specs()
    except Exception:
        specs = {}

    meta_tbl = "".join(
        f"<tr><td>{k}</td><td>{html.escape(str(v))}</td></tr>" for k, v in [
            ("Run name", session.get("name") or "–"),
            ("Model", session.get("model") or "–"),
            ("Cameras", session.get("camera_count") or 0),
            ("Resolution", session.get("resolution") or "–"),
            ("Notes", session.get("notes") or "–"),
            ("Host", session.get("hostname") or "–"),
            ("CPU", specs.get("cpu") or "n/a"),
            ("GPU", specs.get("gpu") or "n/a"),
            ("NPU", specs.get("npu") or "n/a"),
            ("RAM", specs.get("ram") or "n/a"),
            ("Started", start),
            ("Duration", f"{summary['duration_s']} s" if summary.get("duration_s") is not None else "–"),
            ("Samples", summary["n_samples"]),
        ])

    return r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stress Report — __NAME__</title>
<style>__CSS__
.report{max-width:1000px;margin:0 auto;padding:24px}
.kv td:first-child{color:var(--muted);width:160px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:780px){.two{grid-template-columns:1fr}}
h1{font-size:22px}
</style></head><body><div class="report">
<h1>Nx AI Manager · Stress-Test Report</h1>
<div class="mini">Generated __GEN__</div>

<div class="sec-title">Run configuration</div>
<div class="panel"><table class="kv">__META__</table></div>

<div class="sec-title">Summary (avg / peak / min / p95)</div>
<div class="panel"><table>
<thead><tr><th>Metric</th><th class="num">Avg</th><th class="num">Peak</th><th class="num">Min</th><th class="num">P95</th></tr></thead>
<tbody>__STATROWS__</tbody></table></div>

<div class="sec-title">Per-camera inference FPS (avg / peak / min / p95)</div>
<div class="panel"><table>
<thead><tr><th>Camera</th><th>Channel ID</th><th>Model</th><th>Primary stream</th><th>Secondary stream</th><th>Inference on</th><th>Device</th><th class="num">Avg</th><th class="num">Peak</th><th class="num">Min</th><th class="num">P95</th></tr></thead>
<tbody>__CAMROWS__</tbody></table></div>

<div class="sec-title">Inference FPS over time</div>
<div class="panel"><canvas id="cFps"></canvas></div>

<div class="sec-title">Resource load over time</div>
<div class="panel">
<div class="legend">
  <span><i style="background:var(--cpu)"></i>CPU %</span>
  <span><i style="background:var(--ram)"></i>RAM %</span>
  <span><i style="background:var(--igpu)"></i>iGPU %</span>
  <span><i style="background:var(--npu)"></i>NPU %</span>
  <span><i style="background:var(--nv)"></i>NVIDIA %</span>
</div>
<canvas id="cRes"></canvas></div>

<div class="mini" style="margin-top:24px">Network Optix · NX AI Manager stress dashboard</div>
</div>
<script>__CHART_JS__
const SERIES=__SERIES__;
function g(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}
function draw(){
  drawChart(document.getElementById('cFps'),SERIES,[
    {key:'fps_total',color:g('--fps'),axis:'left'}],{height:240});
  drawChart(document.getElementById('cRes'),SERIES,[
    {key:'cpu_pct',color:g('--cpu'),axis:'right'},
    {key:'ram_pct',color:g('--ram'),axis:'right'},
    {key:'igpu_pct',color:g('--igpu'),axis:'right'},
    {key:'npu_pct',color:g('--npu'),axis:'right'},
    {key:'nvidia_pct',color:g('--nv'),axis:'right'}],{height:240});
}
draw(); window.addEventListener('resize',draw);
</script>
</body></html>""" \
        .replace("__CSS__", _CSS).replace("__CHART_JS__", _CHART_JS) \
        .replace("__NAME__", html.escape(session.get("name") or "run")) \
        .replace("__GEN__", gen).replace("__META__", meta_tbl) \
        .replace("__STATROWS__", stat_rows).replace("__CAMROWS__", cam_rows) \
        .replace("__SERIES__", _embed_json(series))
