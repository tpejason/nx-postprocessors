#!/usr/bin/env python3
"""ASUS-NUC CPU summary — 6 models across counts 4/8/16/24/32 (face only @4).
Reads per-run CSVs from RECORDS, writes SUMMARY.{html,md,csv} to OUTDIR."""
import csv, os, statistics, html

RECORDS = os.path.expanduser("~/Downloads/stress-reports/asus-nuc/record")
OUTDIR  = os.path.expanduser("~/Downloads/stress-reports/asus-nuc")
PREFIX, DEVICE = "ASUS-NUC-U5-125H", "cpu"
MODELS = [("demo","Demo Object Detection"),("face","Face Detection"),
          ("ppl-low","People Detection (Low)"),("ppl-veh-low","People + Vehicles (Low)"),
          ("ppl-high","People Detection (High)"),("ppl-veh-high","People + Vehicles (High)")]
COUNTS = [4,8,16,24,32]


def parse(p):
    with open(p) as f:
        meta=f.readline(); rows=list(csv.DictReader(f))
    if not rows: return None
    cam=[c for c in rows[0] if c.startswith("fps[")]
    col=lambda k:[float(r[k]) for r in rows if r.get(k) not in (None,"","None")]
    fps=col("fps_total")
    avg=statistics.mean(fps) if fps else 0
    return dict(eff=len(cam), fps=avg, perch=avg/len(cam) if cam else 0,
                cpu=statistics.mean(col("cpu_pct")) if col("cpu_pct") else 0,
                ram=statistics.mean(col("ram_pct")) if col("ram_pct") else 0,
                meta=meta.strip())

data={}; hw=""
for slug,_ in MODELS:
    for n in COUNTS:
        p=os.path.join(RECORDS,f"{PREFIX}-{n}CH-{slug}-{DEVICE}.csv")
        if os.path.exists(p):
            d=parse(p)
            if d: data[(slug,n)]=d; hw=hw or d["meta"]
import re
field=lambda k:(re.search(k+r"=([^,]+)",hw) or [None,"n/a"])[1] if hw else "n/a"
HW=f"{field('cpu')} | RAM {field('ram')}"

def rec(slug):  # recommended real-time cams = max sustained total fps / 7
    v=[data[(slug,n)]["fps"] for n in COUNTS if (slug,n) in data and data[(slug,n)]["fps"]>1]
    return round(max(v)/7) if v else None

# ---- CSV ----
with open(os.path.join(OUTDIR,"SUMMARY.csv"),"w",newline="") as f:
    w=csv.writer(f); w.writerow(["model","cameras","total_fps","per_ch_fps","cpu_%","ram_%"])
    for slug,name in MODELS:
        for n in COUNTS:
            d=data.get((slug,n))
            if d: w.writerow([name,n,f"{d['fps']:.1f}",f"{d['perch']:.2f}",f"{d['cpu']:.1f}",f"{d['ram']:.1f}"])

# ---- MD ----
md=[f"# ASUS-NUC — CPU Inference Stress Test\n","**Host:** "+HW+" · **CPU inference** · 180 s/step · secondary 640×360 @ 7 fps\n"]
md.append("\n## Recommended cameras (real-time, 7 fps/ch = max total FPS ÷ 7)\n")
md.append("| Model | Max total FPS | ~Cameras |\n|---|--:|--:|")
for slug,name in MODELS:
    v=[data[(slug,n)]["fps"] for n in COUNTS if (slug,n) in data]
    mx=max(v) if v else 0; r=rec(slug)
    md.append(f"| {name} | {mx:.1f} | {('~'+str(r)) if r else 'failed/0'} |")
for n in COUNTS:
    present=[s for s,_ in MODELS if (s,n) in data]
    if not present: continue
    md.append(f"\n## {n} cameras\n| Model | Total FPS | Per-ch FPS | CPU % | RAM % |\n|---|--:|--:|--:|--:|")
    for slug,name in MODELS:
        d=data.get((slug,n))
        if d: md.append(f"| {name} | {d['fps']:.1f} | {d['perch']:.2f} | {d['cpu']:.1f} | {d['ram']:.1f} |")
open(os.path.join(OUTDIR,"SUMMARY.md"),"w").write("\n".join(md))

# ---- HTML ----
h=lambda s:html.escape(str(s))
rows=['<h2>Recommended cameras — real-time (7 fps/ch)</h2>',
      '<table><tr><th>Model</th><th>Max total FPS</th><th>~Cameras @ full rate</th></tr>']
for slug,name in MODELS:
    v=[data[(slug,n)]["fps"] for n in COUNTS if (slug,n) in data]
    mx=max(v) if v else 0; r=rec(slug)
    rec_s=f"~{r}" if r else "<span class='bad'>failed / 0</span>"
    rows.append(f"<tr><td>{h(name)}</td><td>{mx:.1f}</td><td>{rec_s}</td></tr>")
rows.append("</table>")
for n in COUNTS:
    present=[s for s,_ in MODELS if (s,n) in data]
    if not present: continue
    rows.append(f'<h2>{n} cameras</h2><table><tr><th>Model</th><th>Total FPS</th><th>Per-ch FPS</th><th>CPU %</th><th>RAM %</th></tr>')
    for slug,name in MODELS:
        d=data.get((slug,n))
        if not d: continue
        bad=" class='bad'" if d['fps']<1 else ""
        rows.append(f"<tr{bad}><td>{h(name)}</td><td>{d['fps']:.1f}</td><td>{d['perch']:.2f}</td><td>{d['cpu']:.1f}%</td><td>{d['ram']:.1f}%</td></tr>")
    rows.append("</table>")
HTML=f"""<!doctype html><html><head><meta charset="utf-8"><title>ASUS-NUC CPU Stress Test</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6e6e6;max-width:900px;margin:24px auto;padding:0 16px}}
h1{{color:#4fc3f7}}h2{{color:#9ccc65;margin-top:26px;border-bottom:1px solid #2a2f36;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:14px}}th,td{{border:1px solid #2a2f36;padding:6px 10px;text-align:right}}
th{{background:#1a2027;color:#bbb}}td:first-child,th:first-child{{text-align:left}}.bad td{{color:#ff6b6b}}.sub{{color:#8b97a3;font-size:13px}}</style></head><body>
<h1>ASUS-NUC — CPU Inference Stress Test</h1>
<p class="sub"><b>Host:</b> {h(HW)} · CPU inference · 180 s/step · secondary 640×360 @ 7 fps</p>
{''.join(rows)}
<p class="sub">Recommended = max sustained total FPS ÷ 7 (cameras analyzed at full real-time frame rate).</p>
</body></html>"""
open(os.path.join(OUTDIR,"SUMMARY.html"),"w").write(HTML)
print("wrote SUMMARY.{html,md,csv} to",OUTDIR)
print("models with data:",sorted({s for s,_ in MODELS if any((s,n) in data for n in COUNTS)}))
