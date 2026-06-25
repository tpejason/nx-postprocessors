#!/usr/bin/env python3
"""Merge the Stress Dashboard entry into external_postprocessors.json without
clobbering other registered postprocessors. Run on the Nx server by deploy.sh.

Usage: deploy_merge.py <json_path> <command_path> <socket_path>
"""
import json, sys, os

path, cmd, sock = sys.argv[1], sys.argv[2], sys.argv[3]

data = {"externalPostprocessors": []}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        pass
data.setdefault("externalPostprocessors", [])

entry = {
    "Name": "Stress Dashboard",
    "Command": cmd,
    "SocketPath": sock,
    "ReceiveInputTensor": False,
    # An external postprocessor receives inference only if it declares Objects OR
    # Events. We use Events (a unique, never-emitted trigger) on purpose: declaring
    # Objects would re-declare the model's own object types and produce a
    # "Multiple Object Types have the same id" manifest error that breaks the whole
    # analytics pipeline. Events enables routing with no object-id collision.
    "Events": [
        {"ID": "stress.dashboard.tick", "Name": "Stress Dashboard FPS"}
    ],
}
# Replace any existing entry with the same Name, preserve the rest.
data["externalPostprocessors"] = [
    e for e in data["externalPostprocessors"] if e.get("Name") != "Stress Dashboard"
] + [entry]

with open(path, "w") as f:
    json.dump(data, f, indent=4)
print("registered:", cmd)
