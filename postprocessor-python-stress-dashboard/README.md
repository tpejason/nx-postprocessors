Postprocessor Python Stress Dashboard
=====================================

A stress-testing dashboard for the **NX AI Manager** (Nx Meta **and** Nx Witness).
It answers the question: *"For this model and this number of cameras, what
total / per-channel inference FPS do I get, and how loaded are CPU / RAM / GPU /
NPU?"*

| Component | File | Role |
|-----------|------|------|
| **Postprocessor** | `postprocessor-python-stress-dashboard.py` | Transparent pass-through; counts frames per camera channel (`DeviceID`) and flushes per-second counts to the web app. Does not modify the inference results. |
| **Web App** | `web_app.py` | Samples whole-machine CPU/RAM/GPU/NPU once per second, turns frame counts into FPS, serves the live dashboard, records named stress runs, and exports HTML + CSV reports. |
| **Metrics** | `metrics.py` | Auto-detecting system-metric collectors (imported by the web app). |
| **UI** | `ui_templates.py` | Self-contained dashboard + report HTML (no external/CDN assets). |

## Architecture

```
NX AI Manager
    │  Unix socket (length-prefixed MessagePack), one message per frame per camera
    ▼
Postprocessor  ── counts frames per DeviceID, echoes message back unchanged
    │  HTTP POST /api/fps  (per-second counts)
    ▼
Web App  ── + 1 Hz CPU/RAM/GPU/NPU sampler ──►  SQLite (sessions + samples)
    │
    ▼
Browser dashboard  http://<server>:8120   →  Start/Stop run  →  HTML + CSV report
```

The postprocessor only counts and forwards — all system-metric collection lives
in the web app, so it never perturbs the performance being measured.

## What it shows

1. **Live CPU, RAM, GPU, NPU load** (whole machine).
2. **Total concurrent inference FPS** across every camera channel that has this
   postprocessor enabled.
3. **Per-camera-channel FPS** (channels identified by `DeviceID`; rename them in
   the UI).
4. **Stress-test report** — start a named run (model, camera count, resolution,
   notes), stop it, then export a self-contained **HTML** report (charts +
   avg/peak/min/p95 summary) and/or **CSV** (raw per-second time series).

## Metric sources (Linux)

All sources are auto-detected; absent ones are skipped and shown as *N/A*.

| Resource | Source |
|----------|--------|
| CPU / RAM | `psutil` (whole machine) |
| Intel NPU | `intel_vpu` sysfs `/sys/class/accel/accel*/device/` — `npu_busy_time_us` (→ utilisation %), `npu_memory_utilization`, frequency. Readable without root. |
| Intel iGPU | `xe`/`i915` DRM. **True engine utilisation** from per-client `drm-engine-*` fdinfo counters (**needs root**); falls back to the act/max GT-frequency ratio (no root) labelled `freq-proxy`. |
| NVIDIA GPU | `pynvml` (NVML) — utilisation, memory, power, temperature. |

> **Run the web app as root** (`sudo python3 web_app.py`) for accurate Intel
> iGPU engine utilisation. CPU, RAM and NPU work fine without root. iGPU/NPU
> load reads ~0 until inference is actually running on that device — which is
> the point of the stress test.

## Quick deploy (recommended)

From this directory, using `sshpass`:

```shell
./deploy.sh <server_ip> <ssh_user> <ssh_password> 8120 --start
# e.g.
./deploy.sh <SERVER_IP> nx <NX_PASSWORD> 8120 --start
```

`deploy.sh` auto-detects the Nx install (Meta `/opt/networkoptix-metavms` or
Witness `/opt/networkoptix`) and service user, installs deps
(`msgpack psutil nvidia-ml-py`), uploads all files, registers the postprocessor
in `external_postprocessors.json` (**merging**, not overwriting), restarts the
AI Manager runtime, and (with `--start`) launches the web app as root.

Then open `http://<server_ip>:8120`, enable **"Stress Dashboard"** in the
camera's Cloud pipeline, and run inference.

## Manual build & install (CMake / Nuitka)

```shell
mkdir -p build && cd build
python3 -m venv integrationsdk && source integrationsdk/bin/activate
cmake ..
cmake --build . --target postprocessor-python-stress-dashboard
cmake --install . --component postprocessor-python-stress-dashboard
```

Configuration (copy and edit): place `plugin.stress-dashboard.ini` in
`.../nx_ai_manager/nxai_manager/etc/`.

Register in `external_postprocessors.json`:

```json
{
    "externalPostprocessors": [
        {
            "Name": "Stress Dashboard",
            "Command": ".../nxai_manager/postprocessors/postprocessor-python-stress-dashboard",
            "SocketPath": "/tmp/python-stress-dashboard-postprocessor.sock",
            "ReceiveInputTensor": false
        }
    ]
}
```

Restart the server, then start the web app:

```shell
sudo python3 .../nxai_manager/postprocessors/web_app.py --port 8120
```

## Running a stress test

1. Open `http://<server>:8120`.
2. Enable the **Stress Dashboard** postprocessor on the cameras under test and
   start inference with your target model.
3. Watch the live gauges; per-camera channels appear as frames arrive (rename
   them inline for readable reports).
4. Fill in the run name / model / resolution and click **▶ Start run**.
5. Let it soak, then **■ Stop run**.
6. Under **Saved runs**, download the **HTML** or **CSV** report.

## Ports & files

| Item | Value |
|------|-------|
| Dashboard port | `8120` (configurable in the `.ini` or `--port`) |
| Postprocessor socket | `/tmp/python-stress-dashboard-postprocessor.sock` |
| Database | `.../nxai_manager/etc/plugin.stress-dashboard.db` (or `--db`) |
| Postprocessor log | `plugin.stress-dashboard.log` |
| Web app log | `plugin.stress-dashboard-app.log` |

## Troubleshooting

- **No FPS / no cameras** — confirm the postprocessor is enabled in the
  pipeline and inference is running; check `plugin.stress-dashboard.log` for
  `Could not reach web app` (web app not started or wrong port).
- **iGPU shows `freq-proxy` / 0%** — run the web app as root for true engine
  utilisation; the proxy only reflects clock, not load.
- **iGPU/NPU at 0** — nothing is running on that device yet, or the model runs
  on a different device. Pick the target device in the AI Manager.
- **Dashboard unreachable** — verify port 8120 is free and the web app process
  is running.

## Security

The web app binds to `0.0.0.0` with no authentication — intended for a lab /
test network. Put it behind an authenticated reverse proxy for anything else.

## Licence

Copyright 2025, Network Optix, All rights reserved.
