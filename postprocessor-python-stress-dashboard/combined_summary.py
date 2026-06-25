#!/usr/bin/env python3
"""Combined CPU vs iGPU comparison across DELL + AAEON-NX → one summary.html.
Drop every per-run CSV (DELL/AAEON, cpu/gpu) into one --dir; filenames carry box/count/model/device:
  <label>-<count>CH-<model>-<device>.csv   e.g. DELL-CU7-265-16CH-ppl-veh-high-gpu.csv
Run: python3 combined_summary.py [dir]   (default ~/Downloads/stress-combined/records)
Writes <dir>/../summary.html (+ .md)."""
import csv, glob, os, re, statistics, html, sys

RECORDS = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/Downloads/stress-combined/records")
OUT = os.path.join(os.path.dirname(RECORDS.rstrip("/")), "summary.html")
OUTMD = OUT[:-5] + ".md"

BOX = {"DELL-CU7-265": "DELL — Core Ultra 7 265 (20 threads)",
       "AAEON-PTL-U5-336H": "AAEON-NX — Core Ultra 5 336H (12 threads)",
       "ASUS-NUC-U5-125H": "ASUS-NUC — Core Ultra 5 125H (18 threads)"}
BOX_ORDER = ["DELL-CU7-265", "AAEON-PTL-U5-336H", "ASUS-NUC-U5-125H"]
DEV = {"cpu": "CPU", "gpu": "iGPU"}
MODELNAME = {"demo": "Demo Object Detection", "face": "Face Detection",
             "ppl-low": "People Detection (Low)", "ppl-high": "People Detection (High)",
             "ppl-veh-low": "People + Vehicles (Low)", "ppl-veh-high": "People + Vehicles (High)"}
MODEL_ORDER = ["demo", "face", "ppl-low", "ppl-veh-low", "ppl-high", "ppl-veh-high"]
FNAME = re.compile(r"^(?P<label>.+)-(?P<count>\d+)CH-(?P<model>.+)-(?P<device>cpu|gpu)\.csv$")


def parse(path):
    with open(path) as f:
        f.readline()
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    cam_cols = [c for c in rows[0].keys() if c.startswith("fps[")]
    def col(n):
        return [float(r[n]) for r in rows if r.get(n) not in (None, "", "None")]
    fps = col("fps_total"); cpu = col("cpu_pct"); igpu = col("igpu_pct")
    eff = len(cam_cols)
    avg = statistics.mean(fps) if fps else 0
    fmin = min(fps) if fps else 0
    # stalled = inference collapsed: whole window dead (~0), or a mid-run crash (min->0 while avg>2)
    stalled = (avg < 2.0) or (fmin < 1.0 and avg > 2.0)
    return dict(eff=eff, fps=avg, fmin=fmin, stalled=stalled,
                perch=avg / eff if eff else 0,
                cpu=statistics.mean(cpu) if cpu else 0,
                igpu=statistics.mean(igpu) if igpu else None)


data = {}                       # (label, device, model, count) -> stats
counts = {}                     # (label, device) -> sorted counts
for p in glob.glob(os.path.join(RECORDS, "*CH-*-*.csv")):
    m = FNAME.match(os.path.basename(p))
    if not m:
        continue
    d = parse(p)
    if not d:
        continue
    label, count, model, dev = m["label"], int(m["count"]), m["model"], m["device"]
    data[(label, dev, model, count)] = d
    counts.setdefault((label, dev), set()).add(count)

present = [(lb, dv) for lb in BOX_ORDER for dv in ("cpu", "gpu") if (lb, dv) in counts]


def models_for(lb, dv):
    ms = {k[2] for k in data if k[0] == lb and k[1] == dv}
    return [m for m in MODEL_ORDER if m in ms]


def cmax(lb, dv, model):
    vals = [data[(lb, dv, model, c)]["fps"] for c in counts.get((lb, dv), []) if (lb, dv, model, c) in data]
    return max(vals) if vals else None


# ---------- HTML ----------
def h(s): return html.escape(str(s))
S = []
S.append("<h1>Nx AI Manager Stress Test — CPU vs iGPU (DELL, AAEON-NX &amp; ASUS-NUC)</h1>")
S.append('<p class="sub">Analytics on secondary stream 640×360 @ 7 fps · 180 s per measurement · '
         'iGPU via Intel OpenVINO runtime. Cell = <b>total FPS</b> (small = per-channel fps). '
         '⚠ = per-channel &lt; 7 fps (channels below real-time).</p>')

# headline: CPU vs iGPU max sustained FPS per model, per box + iGPU stall point
def gpu_stable_to(lb, model):
    """Largest camera count with a non-stalled iGPU result; note the first stalled count."""
    cs = sorted(counts.get((lb, "gpu"), []))
    ok, stall = None, None
    for c in cs:
        d = data.get((lb, "gpu", model, c))
        if not d:
            continue
        if d["stalled"]:
            stall = stall or c
        else:
            ok = c
    return ok, stall

S.append("<h2>Headline — CPU vs iGPU, and where the iGPU stalls</h2>")
S.append('<table><tr><th>Box</th><th>Model</th><th>CPU max FPS</th><th>iGPU max FPS</th>'
         '<th>iGPU vs CPU</th><th>iGPU stable to</th></tr>')
for lb in BOX_ORDER:
    ms = [m for m in MODEL_ORDER if any(k[0] == lb and k[2] == m for k in data)]
    for i, m in enumerate(ms):
        c = cmax(lb, "cpu", m)
        # iGPU "max" = best NON-stalled result (so a stall doesn't inflate it)
        gvals = [data[(lb, "gpu", m, cc)]["fps"] for cc in counts.get((lb, "gpu"), [])
                 if (lb, "gpu", m, cc) in data and not data[(lb, "gpu", m, cc)]["stalled"]]
        g = max(gvals) if gvals else None
        ok, stall = gpu_stable_to(lb, m)
        has_gpu = any(k[0] == lb and k[1] == "gpu" and k[2] == m for k in data)
        ratio = f"{g/c:.1f}×" if (c and g) else "—"
        gp = (f"{g:.0f}" if g is not None else "0") if has_gpu else "<span class='na'>no GPU build</span>"
        cp = f"{c:.0f}" if c is not None else "—"
        if not has_gpu:
            stable = "<span class='na'>—</span>"
        elif stall:
            stable = f"<span class='dead'>≤{ok} cams (STALL ≥{stall})</span>" if ok else f"<span class='dead'>STALL ≥{stall}</span>"
        else:
            stable = f"{ok}+ cams ✅" if ok else "—"
        boxcell = f'<td rowspan="{len(ms)}">{h(BOX[lb])}</td>' if i == 0 else ""
        S.append(f"<tr>{boxcell}<td>{h(MODELNAME.get(m,m))}</td><td>{cp}</td><td>{gp}</td><td>{ratio}</td><td>{stable}</td></tr>")
S.append("</table>")

# per box/device grids
for lb in BOX_ORDER:
    if not any(k[0] == lb for k in data):
        continue
    S.append(f"<h2>{h(BOX[lb])}</h2>")
    for dv in ("cpu", "gpu"):
        if (lb, dv) not in counts:
            continue
        cs = sorted(counts[(lb, dv)])
        ms = models_for(lb, dv)
        S.append(f"<h3>{DEV[dv]} inference</h3>")
        S.append("<table><tr><th>Model \\ cameras</th>" + "".join(f"<th>{c}</th>" for c in cs) + "</tr>")
        for m in ms:
            cells = []
            for c in cs:
                d = data.get((lb, dv, m, c))
                if not d:
                    cells.append("<td>—</td>"); continue
                if d["stalled"]:
                    cells.append('<td class="dead">stall&nbsp;☠</td>'); continue
                warn = " ⚠" if d["perch"] < 6.5 else ""
                extra = f' · gpu&nbsp;{d["igpu"]:.0f}%' if (dv == "gpu" and d["igpu"] is not None) else ""
                cells.append(f'<td>{d["fps"]:.0f}<span class="pc"> ({d["perch"]:.1f}{warn}{extra})</span></td>')
            S.append(f"<tr><td>{h(MODELNAME.get(m,m))}</td>" + "".join(cells) + "</tr>")
        S.append("</table>")
        if dv == "cpu":
            S.append('<p class="sub">CPU-bound: CPU plateaus ~65% even when saturated (runtime is thread-limited). No crashes.</p>')
        else:
            igvals = [data[k]["igpu"] for k in data if k[0] == lb and k[1] == "gpu" and data[k]["igpu"] is not None]
            anystall = any(data[k]["stalled"] for k in data if k[0] == lb and k[1] == "gpu")
            ig = (f"iGPU util readable here — reaches {max(igvals):.0f}% peak. " if igvals
                  else "iGPU %% util not readable on this driver (offload shown by low CPU). ")
            st = ("<b>STALL:</b> inference collapses to 0 at higher counts once the iGPU saturates (~90%) — "
                  "matches the field report; recovers only with CPU-switch + mediaserver restart."
                  if anystall else "No stall — scales cleanly to the max count tested.")
            S.append(f'<p class="sub">{ig}{st}</p>')

NOTE = ("<div class='note'><b>Key findings.</b> "
        "(1) <b>iGPU frees the CPU and boosts throughput</b> — it drops CPU from ~65% to ~10–20% and lets the Low models "
        "run far more channels (AAEON-NX: 192 FPS / ~24 cams at full 7 fps on iGPU vs 65 FPS / ~8 cams on CPU). "
        "(2) <b>DELL iGPU STILL STALLS</b> — once the iGPU saturates (~90%, which the Low models reach at 12 cams), "
        "inference <b>collapses to 0 FPS at 16+ cameras</b> and only recovers with a CPU-switch + mediaserver restart. "
        "This <b>reproduces the field report</b>. Only People+Vehicles (High) survived, because it's compute-heavy and "
        "drove the iGPU to only ~62% (never hit the wall). "
        "(3) <b>AAEON-NX iGPU does NOT stall</b> — the newer Core Ultra 5 (xe driver) scaled cleanly to 64 cameras. "
        "So the stall is specific to DELL's older Arrow Lake-U iGPU. "
        "(4) <b>Demo &amp; Face are CPU-only</b> (no GPU build). "
        "(5) Adding 'Vehicles' costs ~nothing; cost is driven by accuracy TIER (input resolution). "
        "<br><b>Practical guidance:</b> on DELL, keep iGPU use to ≤8–12 cams (or use CPU for higher counts); "
        "on AAEON-NX the iGPU is safe to scale and is the better choice for the Low models.</div>")

CSS = """<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6e6e6;max-width:1080px;margin:24px auto;padding:0 16px}
 h1{color:#4fc3f7} h2{color:#9ccc65;margin-top:30px;border-bottom:1px solid #2a2f36;padding-bottom:4px}
 h3{color:#ffb74d;margin:16px 0 4px}
 .sub{color:#8b97a3;font-size:13px} .pc{color:#8b97a3;font-size:12px} .na{color:#777;font-style:italic}
 .dead{color:#ff6b6b;font-weight:600}
 table{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}
 th,td{border:1px solid #2a2f36;padding:6px 10px;text-align:right} th{background:#1a2027;color:#bbb}
 td:first-child,th:first-child{text-align:left}
 .note{background:#1a2027;border-left:3px solid #ffb300;padding:12px 16px;margin:18px 0;font-size:14px;line-height:1.5}
</style>"""
htmlout = f"<!doctype html><html><head><meta charset='utf-8'><title>Nx AI Stress — CPU vs iGPU (DELL, AAEON-NX & ASUS-NUC)</title>{CSS}</head><body>{''.join(S)}{NOTE}<p class='sub'>Per-run reports in records/.</p></body></html>"
open(OUT, "w").write(htmlout)
# tiny md index
open(OUTMD, "w").write("# CPU vs iGPU — DELL & AAEON-NX\n\nDatasets present: " +
                       ", ".join(f"{lb} {DEV[dv]} ({sorted(counts[(lb,dv)])})" for lb, dv in present) + "\n")
print("wrote", OUT)
print("datasets:", [(lb, dv, sorted(counts[(lb,dv)])) for lb, dv in present])
