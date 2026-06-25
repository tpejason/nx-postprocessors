# NX Postprocessors

Custom postprocessors for NX AI Manager.

## Quick Start

Each postprocessor is a self-contained Python **external postprocessor** for NX AI
Manager. To run any of them after cloning:

```bash
git clone https://github.com/tpejason/nx-postprocessors.git
cd nx-postprocessors/postprocessor-python-<name>   # e.g. postprocessor-python-web-dashboard-advance
```

1. **Create your config from the template.** Every postprocessor ships a
   `plugin.*.ini.example`. Copy it and fill in **your own** values:
   ```bash
   cp plugin.<name>.ini.example plugin.<name>.ini
   ```
   Edit the `[nx]` section with your Nx Server URL, username, and password:
   ```ini
   [nx]
   url      = https://<YOUR_NX_SERVER>:7001
   username = <YOUR_USERNAME>
   password = <YOUR_PASSWORD>
   ```
   > The code reads these from the `.ini` at runtime (`cfg.get('nx', ...)`); the
   > `<...>` placeholders in the templates are **meant to be replaced** — nothing is
   > hard-coded to a specific server.

2. **Place the config where AI Manager expects it.** The web apps read the `.ini`
   from the AI Manager `etc/` directory (i.e. `../etc/plugin.<name>.ini` relative
   to the script). Copy your filled-in `.ini` there.

3. **Register the postprocessor** with AI Manager by adding it to
   `external_postprocessors.json` (Name, Command = the launcher script, SocketPath,
   Events), then restart the mediaserver so it spawns the feeder. See each
   postprocessor's own README for the exact entry.

4. **Open the dashboard** at `http://<your-server-ip>:<port>` (ports listed in the
   table below) and, in the Nx client, select the postprocessor on the target
   camera(s).

> **Deploy scripts** (`deploy.sh`, `scripts/dev_watch.py`) are convenience helpers
> that originally targeted the author's lab. Pass your own host/credentials as
> arguments, or edit the `<SERVER_IP>` / `<SSH_PASSWORD>` placeholders inside them
> before use. They are **not** required to run a postprocessor — only to automate
> remote deployment.

## Credentials & Security

> ⚠️ These postprocessors ship with **default placeholder credentials** for demo
> convenience (e.g. `admin` / `admin`, SSH `<SSH_PASSWORD>`). **Change them to your own
> credentials before any real deployment.**

- Set Nx Server URL, username, and password in each postprocessor's
  `plugin.*.ini` config file (see the `.ini.example` templates).
- Deploy scripts (`deploy.sh`) take the SSH user/password as arguments — do not
  hard-code production secrets.
- Never commit a populated `.ini`, `.db`, or `.log` file. These are excluded via
  `.gitignore`; keep runtime data out of the repository.

## Postprocessors

| Postprocessor | Port | Purpose |
|---------------|------|---------|
| [`web-dashboard-advance`](postprocessor-python-web-dashboard-advance) | 8112 | Advanced multi-camera real-time dashboard: position heatmap, object counts, timeline, filtering, CSV export |
| [`vlm-web`](postprocessor-python-vlm-web) | 8115 | Feeds detection metadata to a VLM (Ollama) for image description/analysis, served via a web app |
| [`stress-dashboard`](postprocessor-python-stress-dashboard) | 8120 | Stress-test dashboard (Nx Meta + Nx Witness): total/per-channel inference FPS and CPU/RAM/GPU/NPU load; named runs, HTML+CSV reports (pass-through) |
| [`gauge-dashboard`](postprocessor-python-gauge-dashboard) | 8082 | Reads numeric gauge values from a vision model; live analog gauge, trend chart, alert thresholds |
| [`dice-dashboard`](postprocessor-python-dice-dashboard) | 8081 | Classifies dice rolls (Big / Small / Triple / Unknown) with a live dashboard, history, and prize wheel |
| [`parking-dashboard`](postprocessor-python-parking-dashboard) | 8114 | Maps Car/Bus/Truck detections to 6 parking spaces (P1–P6); live occupancy map, per-space duration, Mark Free override, usage stats |

All postprocessors are Python **external postprocessors** for NX AI Manager. They
receive per-frame detection metadata from the AI pipeline; the AI model itself runs
inside NX AI Manager / Cloud Pipelines, not in the postprocessor.

> **Demo assets:** the demo `.onnx` models (and any sample clips) are large
> binaries and are **not tracked in git** — keep them locally (e.g. under an
> `aim-models/` directory) or distribute them via Git LFS / a separate share.

---

### postprocessor-python-web-dashboard-advance

Advanced real-time web dashboard for NX AI Manager.

- Multi-camera support
- Dark neon theme
- Per-camera position heatmap with RTSP thumbnail background
- Object count cards, pie chart, timeline
- Time + object type filtering
- CSV export

**Port:** 8112  
**Dashboard URL:** `http://<server-ip>:8112`

#### Setup

1. Build and install via CMake
2. Register in `external_postprocessors.json`
3. Open dashboard and set RTSP URL per camera (gear icon) to enable heatmap backgrounds

---

### postprocessor-python-gauge-dashboard

Reads numeric gauge values from a vision model and serves a real-time dashboard
with a live analog gauge, trend chart, and configurable Normal/High/Low alert
thresholds. See [its README](postprocessor-python-gauge-dashboard/README.md).

**Port:** 8082 · **Demo model:** `models/gauge.onnx` · **Demo clip:** `samples/gauge.mp4`

---

### postprocessor-python-dice-dashboard

Classifies dice rolls (Big / Small / Triple / Unknown) from a dice-pip vision
model and serves a live dashboard with roll history, cumulative tally, and a
prize wheel. See [its README](postprocessor-python-dice-dashboard/README.md).

**Port:** 8081 · **Demo model:** `models/dice.onnx`

---

### postprocessor-python-vlm-web

Forwards detection metadata to a VLM (via Ollama) for image description /
analysis and presents results in a web app.

**Port:** 8115

---

### postprocessor-python-stress-dashboard

Stress-testing dashboard for NX AI Manager (Nx Meta and Nx Witness). Reports
total and per-channel inference FPS plus CPU / RAM / GPU / NPU load, records
named stress runs, and exports HTML + CSV reports. The postprocessor is a
transparent pass-through (does not modify inference results).

**Port:** 8120

---

### postprocessor-python-parking-dashboard

Maps vehicle detections (Car / Bus / Truck) to six parking spaces (P1–P6) by
horizontal position and serves a live occupancy dashboard: per-space state and
duration, a manual **Mark Free** override, and usage statistics. Occupancy uses
a 3 s vacate timeout + 2 min cooldown to avoid flicker. See
[its README](postprocessor-python-parking-dashboard/README.md).

**Port:** 8114
