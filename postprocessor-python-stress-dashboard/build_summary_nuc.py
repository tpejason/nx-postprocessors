#!/usr/bin/env python3
"""Aggregate the ASUS-NUC stress-run CSVs into a 5-model comparison (md + html + csv).
Runs ON the ASUS-NUC box right after the sweep. Output dir = /home/nx/stress-reports."""
import csv, glob, os, statistics, html, re

DIR = "/home/nx/stress-reports"
PREFIX = "ASUS-NUC-U5-125H"          # must match stress_harness --label
DEVICE = "cpu"
MODELS = [("demo", "Demo Object Detection"),
          ("ppl-veh-high", "People + Vehicles (High)"),
          ("ppl-high", "People Detection (High)"),
          ("ppl-low", "People Detection (Low)"),
          ("ppl-veh-low", "People + Vehicles (Low)")]
COUNTS = [8, 16, 24, 32]


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
    fps = col("fps_total")
    ncam = col("n_cameras")
    cpu = col("cpu_pct")
    ram = col("ram_pct")
    rammb = col("ram_used_mb")
    npu = col("npu_pct")
    igpu = col("igpu_pct")
    eff = len(cam_cols)                       # cameras that ever reported a frame
    active = statistics.mean(ncam) if ncam else 0
    fps_avg = statistics.mean(fps) if fps else 0
    per_ch = fps_avg / active if active else 0
    return dict(eff=eff, active=active, samples=len(rows),
                fps_avg=fps_avg, fps_p95=pct(fps, 95), fps_peak=max(fps) if fps else 0,
                per_ch=per_ch,
                cpu_avg=statistics.mean(cpu) if cpu else 0, cpu_p95=pct(cpu, 95),
                cpu_peak=max(cpu) if cpu else 0,
                ram_avg=statistics.mean(ram) if ram else 0,
                rammb_avg=statistics.mean(rammb) if rammb else 0,
                npu_avg=statistics.mean(npu) if npu else 0,
                igpu_avg=statistics.mean(igpu) if igpu else 0,
                meta=meta.strip())


data = {}   # (slug,count) -> stats
hw = ""
for slug, _ in MODELS:
    for n in COUNTS:
        p = os.path.join(DIR, f"{PREFIX}-{n}CH-{slug}-{DEVICE}.csv")
        if os.path.exists(p):
            data[(slug, n)] = parse(p)
            if not hw:
                hw = data[(slug, n)]["meta"]

# hardware string
def field(k):
    m = re.search(k + r"=([^,]+)", hw)
    return m.group(1) if m else "n/a"
HW = f"{field('cpu')} | {field('gpu')} | RAM {field('ram')}"

# ---- build CSV ----
with open(os.path.join(DIR, "SUMMARY.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["model", "requested_CH", "effective_cams", "avg_active_cams",
                "total_fps_avg", "total_fps_p95", "per_channel_fps",
                "cpu_avg_%", "cpu_p95_%", "cpu_peak_%", "ram_avg_%", "ram_used_MB"])
    for slug, name in MODELS:
        for n in COUNTS:
            d = data.get((slug, n))
            if not d:
                continue
            w.writerow([name, n, d["eff"], f"{d['active']:.1f}",
                        f"{d['fps_avg']:.1f}", f"{d['fps_p95']:.1f}", f"{d['per_ch']:.2f}",
                        f"{d['cpu_avg']:.1f}", f"{d['cpu_p95']:.1f}", f"{d['cpu_peak']:.1f}",
                        f"{d['ram_avg']:.1f}", f"{d['rammb_avg']:.0f}"])

# ---- build Markdown ----
md = []
md.append("# Nx AI Manager Stress Test — ASUS-NUC 5-Model Comparison\n")
md.append(f"**Host:** {HW}  ")
md.append(f"**Inference device:** CPU · **Per step:** 180 s · **Streams:** secondary 640×360 @ 7 fps\n")
md.append("> Camera source: 64 Nx Testcameras (from the Memryx box). Each requested count "
          "(8/16/32/64) AI-enables exactly that many channels.\n")

for n in COUNTS:
    md.append(f"\n## Requested {n} cameras  (effective {data.get(('demo',n),{}).get('eff','?')})\n")
    md.append("| Model | Total FPS (avg) | Per-channel FPS | CPU avg % | CPU P95 % | CPU peak % | RAM % |")
    md.append("|---|--:|--:|--:|--:|--:|--:|")
    for slug, name in MODELS:
        d = data.get((slug, n))
        if not d:
            md.append(f"| {name} | — | — | — | — | — | — |"); continue
        md.append(f"| {name} | {d['fps_avg']:.1f} | {d['per_ch']:.2f} | {d['cpu_avg']:.1f} | "
                  f"{d['cpu_p95']:.1f} | {d['cpu_peak']:.1f} | {d['ram_avg']:.1f} |")

md.append("\n## Saturation summary\n")
md.append("Ideal per-channel inference rate = 7 fps (the secondary-stream fps). "
          "When per-channel FPS falls below ~7, the CPU can no longer keep every channel at full rate.\n")
md.append("| Model | Saturates at (eff cams) | Max sustained total FPS | CPU @ max load |")
md.append("|---|--:|--:|--:|")
for slug, name in MODELS:
    sat = None
    maxfps = 0; cpu_at = 0
    for n in COUNTS:
        d = data.get((slug, n))
        if not d:
            continue
        maxfps = max(maxfps, d["fps_avg"])
        if d["fps_avg"] >= maxfps:
            cpu_at = d["cpu_avg"]
        if sat is None and d["per_ch"] < 6.5:
            sat = d["eff"]
    md.append(f"| {name} | {sat if sat else '> 64 (none)'} | {maxfps:.1f} | {cpu_at:.1f}% |")

open(os.path.join(DIR, "SUMMARY.md"), "w").write("\n".join(md))

# ---- build HTML ----
def h(s): return html.escape(str(s))
rows_html = []
for n in COUNTS:
    eff = data.get(('demo', n), {}).get('eff', '?')
    rows_html.append(f'<h2>Requested {n} cameras <span class="sub">(effective {eff})</span></h2>')
    rows_html.append('<table><tr><th>Model</th><th>Total FPS</th><th>Per-ch FPS</th>'
                     '<th>CPU avg</th><th>CPU P95</th><th>CPU peak</th><th>RAM</th></tr>')
    for slug, name in MODELS:
        d = data.get((slug, n))
        if not d:
            continue
        sat = d['per_ch'] < 6.5
        cls = ' class="hot"' if d['cpu_avg'] > 80 else ''
        rows_html.append(f'<tr{cls}><td>{h(name)}</td><td>{d["fps_avg"]:.1f}</td>'
                         f'<td>{d["per_ch"]:.2f}{" ⚠" if sat else ""}</td>'
                         f'<td>{d["cpu_avg"]:.1f}%</td><td>{d["cpu_p95"]:.1f}%</td>'
                         f'<td>{d["cpu_peak"]:.1f}%</td><td>{d["ram_avg"]:.1f}%</td></tr>')
    rows_html.append('</table>')

HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Stress Test — ASUS-NUC 5-Model Comparison</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6e6e6;max-width:980px;margin:24px auto;padding:0 16px}}
 h1{{color:#4fc3f7}} h2{{color:#9ccc65;margin-top:28px;border-bottom:1px solid #2a2f36;padding-bottom:4px}}
 .sub{{color:#888;font-size:13px;font-weight:normal}}
 table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}}
 th,td{{border:1px solid #2a2f36;padding:6px 10px;text-align:right}} th{{background:#1a2027;color:#bbb}}
 td:first-child,th:first-child{{text-align:left}}
 tr.hot td{{background:#3a1f1f}}
 .note{{background:#1a2027;border-left:3px solid #ffb300;padding:10px 14px;margin:14px 0;font-size:14px;color:#ddd}}
 code{{color:#ffb74d}}
</style></head><body>
<h1>Nx AI Manager Stress Test — ASUS-NUC 5-Model Comparison</h1>
<p><b>Host:</b> {h(HW)}<br><b>Device:</b> CPU · <b>180 s/step</b> · <b>Streams:</b> secondary 640×360 @ 7 fps</p>
<div class="note">Camera source: 64 Nx Testcameras (from the Memryx box). Counts 8/16/32/64 AI-enable exactly that many channels. Rows in red = CPU &gt; 80% (saturated). ⚠ on per-channel = below 7 fps (channels starved).</div>
{''.join(rows_html)}
<p class="sub">Generated from up to 20 runs · SUMMARY.csv has the full numeric table.</p>
</body></html>"""
open(os.path.join(DIR, "SUMMARY.html"), "w").write(HTML)
print("wrote SUMMARY.md, SUMMARY.html, SUMMARY.csv to", DIR)
