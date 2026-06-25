#!/usr/bin/env python3
"""Aggregate the AAEON-NX iGPU stress-run CSVs into a 4-model comparison (md+html+csv).
iGPU sweep, YOLO11s-based People/Vehicle models only (no Demo/Face — they have no GPU build).
Output: SUMMARY-igpu.{md,html,csv} in /home/aaeon/stress-reports (does NOT touch the CPU SUMMARY)."""
import csv, glob, os, statistics, html, re

DIR = "/home/aaeon/stress-reports"
PREFIX = "AAEON-PTL-U5-336H"
DEVICE = "gpu"
MODELS = [("ppl-veh-high", "People + Vehicles (High)"),
          ("ppl-high", "People Detection (High)"),
          ("ppl-low", "People Detection (Low)"),
          ("ppl-veh-low", "People + Vehicles (Low)")]
COUNTS = [8, 16, 24, 32, 64]


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def parse(path):
    with open(path) as f:
        meta = f.readline()
        rows = list(csv.DictReader(f))
    cam_cols = [c for c in rows[0].keys() if c.startswith("fps[")] if rows else []
    def col(name):
        return [float(r[name]) for r in rows if r.get(name) not in (None, "", "None")]
    fps = col("fps_total"); ncam = col("n_cameras")
    cpu = col("cpu_pct"); ram = col("ram_pct"); rammb = col("ram_used_mb")
    igpu = col("igpu_pct")
    eff = len(cam_cols)
    active = statistics.mean(ncam) if ncam else 0
    fps_avg = statistics.mean(fps) if fps else 0
    per_ch = fps_avg / active if active else 0
    return dict(eff=eff, active=active, samples=len(rows),
                fps_avg=fps_avg, fps_p95=pct(fps, 95), fps_min=min(fps) if fps else 0,
                fps_peak=max(fps) if fps else 0, per_ch=per_ch,
                cpu_avg=statistics.mean(cpu) if cpu else 0, cpu_peak=max(cpu) if cpu else 0,
                igpu_avg=statistics.mean(igpu) if igpu else 0,
                igpu_peak=max(igpu) if igpu else 0,
                igpu_n=len(igpu),
                ram_avg=statistics.mean(ram) if ram else 0,
                rammb_avg=statistics.mean(rammb) if rammb else 0,
                meta=meta.strip())


data = {}; hw = ""
for slug, _ in MODELS:
    for n in COUNTS:
        p = os.path.join(DIR, f"{PREFIX}-{n}CH-{slug}-{DEVICE}.csv")
        if os.path.exists(p):
            data[(slug, n)] = parse(p)
            if not hw:
                hw = data[(slug, n)]["meta"]

def field(k):
    m = re.search(k + r"=([^,]+)", hw); return m.group(1) if m else "n/a"
HW = f"{field('cpu')} | {field('gpu')} | RAM {field('ram')}"

def ig(d):  # iGPU avg display: 'n/a' if the collector never returned a value
    return f"{d['igpu_avg']:.0f}" if d.get("igpu_n") else "n/a"

# ---- CSV ----
with open(os.path.join(DIR, "SUMMARY-igpu.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["model", "requested_CH", "effective_cams", "avg_active_cams",
                "total_fps_avg", "total_fps_p95", "per_channel_fps",
                "igpu_avg_%", "igpu_peak_%", "cpu_avg_%", "ram_avg_%", "ram_used_MB"])
    for slug, name in MODELS:
        for n in COUNTS:
            d = data.get((slug, n))
            if not d: continue
            w.writerow([name, n, d["eff"], f"{d['active']:.1f}", f"{d['fps_avg']:.1f}",
                        f"{d['fps_p95']:.1f}", f"{d['per_ch']:.2f}", ig(d), f"{d['igpu_peak']:.0f}",
                        f"{d['cpu_avg']:.1f}", f"{d['ram_avg']:.1f}", f"{d['rammb_avg']:.0f}"])

# ---- Markdown ----
md = []
md.append("# Nx AI Manager Stress Test — AAEON-NX iGPU 4-Model Comparison\n")
md.append(f"**Host:** {HW}  ")
md.append(f"**Inference device:** Intel iGPU · **Per step:** 180 s · **Streams:** secondary 640×360 @ 7 fps\n")
md.append("> YOLO11s-based People/Vehicle models only (Demo + Face excluded — no GPU build). "
          "Counts 8/16/24/32/64; each AI-enables exactly that many channels.\n")
for n in COUNTS:
    md.append(f"\n## Requested {n} cameras  (effective {data.get(('ppl-low',n),{}).get('eff','?')})\n")
    md.append("| Model | Total FPS (avg) | Per-channel FPS | iGPU avg % | iGPU peak % | CPU avg % | RAM % |")
    md.append("|---|--:|--:|--:|--:|--:|--:|")
    for slug, name in MODELS:
        d = data.get((slug, n))
        if not d: md.append(f"| {name} | — | — | — | — | — | — |"); continue
        md.append(f"| {name} | {d['fps_avg']:.1f} | {d['per_ch']:.2f} | {ig(d)} | "
                  f"{d['igpu_peak']:.0f} | {d['cpu_avg']:.1f} | {d['ram_avg']:.1f} |")
md.append("\n## Saturation summary\n")
md.append("Ideal per-channel rate = 7 fps. A sudden drop in total FPS within a step can indicate an "
          "iGPU capacity stall (seen on similar Intel iGPUs above ~12 channels).\n")
md.append("| Model | Saturates at (eff cams) | Max sustained total FPS | iGPU @ max load |")
md.append("|---|--:|--:|--:|")
for slug, name in MODELS:
    sat = None; maxfps = 0; ig_at = "n/a"
    for n in COUNTS:
        d = data.get((slug, n))
        if not d: continue
        if d["fps_avg"] >= maxfps: maxfps = d["fps_avg"]; ig_at = ig(d)
        if sat is None and d["per_ch"] < 6.5: sat = d["eff"]
    md.append(f"| {name} | {sat if sat else '> 64 (none)'} | {maxfps:.1f} | {ig_at}% |")
open(os.path.join(DIR, "SUMMARY-igpu.md"), "w").write("\n".join(md))

# ---- HTML ----
def h(s): return html.escape(str(s))
rows = []
for n in COUNTS:
    eff = data.get(('ppl-low', n), {}).get('eff', '?')
    rows.append(f'<h2>Requested {n} cameras <span class="sub">(effective {eff})</span></h2>')
    rows.append('<table><tr><th>Model</th><th>Total FPS</th><th>Per-ch FPS</th>'
                '<th>iGPU avg</th><th>iGPU peak</th><th>CPU avg</th><th>RAM</th></tr>')
    for slug, name in MODELS:
        d = data.get((slug, n))
        if not d: continue
        sat = d['per_ch'] < 6.5
        rows.append(f'<tr><td>{h(name)}</td><td>{d["fps_avg"]:.1f}</td>'
                    f'<td>{d["per_ch"]:.2f}{" ⚠" if sat else ""}</td>'
                    f'<td>{ig(d)}%</td><td>{d["igpu_peak"]:.0f}%</td>'
                    f'<td>{d["cpu_avg"]:.1f}%</td><td>{d["ram_avg"]:.1f}%</td></tr>')
    rows.append('</table>')
HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Stress Test — AAEON-NX iGPU 4-Model</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6e6e6;max-width:980px;margin:24px auto;padding:0 16px}}
 h1{{color:#4fc3f7}} h2{{color:#9ccc65;margin-top:28px;border-bottom:1px solid #2a2f36;padding-bottom:4px}}
 .sub{{color:#888;font-size:13px;font-weight:normal}}
 table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}}
 th,td{{border:1px solid #2a2f36;padding:6px 10px;text-align:right}} th{{background:#1a2027;color:#bbb}}
 td:first-child,th:first-child{{text-align:left}}
 .note{{background:#1a2027;border-left:3px solid #ffb300;padding:10px 14px;margin:14px 0;font-size:14px;color:#ddd}}
</style></head><body>
<h1>Nx AI Manager Stress Test — AAEON-NX iGPU (4 YOLO11s models)</h1>
<p><b>Host:</b> {h(HW)}<br><b>Device:</b> Intel iGPU · <b>180 s/step</b> · <b>Streams:</b> secondary 640×360 @ 7 fps</p>
<div class="note">YOLO11s People/Vehicle models only (Demo + Face have no GPU build). ⚠ on per-channel = below 7 fps. Watch for sudden total-FPS drops = possible iGPU capacity stall.</div>
{''.join(rows)}
<p class="sub">Generated from the iGPU run · SUMMARY-igpu.csv has the full numeric table.</p>
</body></html>"""
open(os.path.join(DIR, "SUMMARY-igpu.html"), "w").write(HTML)
print("wrote SUMMARY-igpu.{md,html,csv} to", DIR)
