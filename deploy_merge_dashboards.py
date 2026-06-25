#!/usr/bin/env python3
"""Merge the Dice / Gauge / Web-Dashboard-Advance entries into
external_postprocessors.json without clobbering other registered
postprocessors (e.g. the Stress Dashboard).

Each entry declares a unique, never-emitted Event as its routing trigger.
Declaring Objects instead would re-declare the model's own object-type ids and
raise a "Multiple Object Types have the same id" manifest error that breaks the
whole analytics pipeline; the dummy Event routes the full inference message
(BBoxes_xyxy etc.) to the postprocessor with no id collision.

Usage: deploy_merge_dashboards.py <json_path> <pp_dir>
"""
import json, sys, os

path, pp_dir = sys.argv[1], sys.argv[2]

DASHBOARDS = [
    {
        "Name": "Dice Dashboard",
        "wrapper": "postprocessor-python-dice-dashboard",
        "sock": "/tmp/python-dice-dashboard-postprocessor.sock",
        "event": ("dice.dashboard.tick", "Dice Dashboard"),
        "confidence": True,
    },
    {
        "Name": "Gauge Dashboard",
        "wrapper": "postprocessor-python-gauge-dashboard",
        "sock": "/tmp/python-gauge-dashboard-postprocessor.sock",
        "event": ("gauge.dashboard.tick", "Gauge Dashboard"),
        "confidence": True,
    },
    {
        "Name": "Web Dashboard Advance",
        "wrapper": "postprocessor-python-web-dashboard-advance",
        "sock": "/tmp/python-web-dashboard-advance-postprocessor.sock",
        "event": ("web.dashboard.tick", "Web Dashboard Advance"),
        "confidence": False,
    },
]

data = {"externalPostprocessors": []}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        pass
data.setdefault("externalPostprocessors", [])

names = {d["Name"] for d in DASHBOARDS}
# Drop any prior copies of our three entries; preserve everything else (stress…).
kept = [e for e in data["externalPostprocessors"] if e.get("Name") not in names]

for d in DASHBOARDS:
    entry = {
        "Name": d["Name"],
        "Command": os.path.join(pp_dir, d["wrapper"]),
        "SocketPath": d["sock"],
        "ReceiveInputTensor": False,
        "Events": [{"ID": d["event"][0], "Name": d["event"][1]}],
    }
    if d["confidence"]:
        entry["ReceiveConfidenceData"] = True
    kept.append(entry)

data["externalPostprocessors"] = kept

with open(path, "w") as f:
    json.dump(data, f, indent=4)

print("registered:", ", ".join(d["Name"] for d in DASHBOARDS))
