# postprocessor-python-parking-dashboard

An NX AI Manager external post-processor that turns vehicle detections into a
live **parking-occupancy dashboard**. It maps each detected vehicle to one of
six parking spaces (P1–P6) by horizontal position and serves a real-time
dark-theme web dashboard.

## What it does

1. Receives per-frame detection results from a vehicle vision model
   (classes **Car**, **Bus**, **Truck**; other classes are ignored).
2. For each vehicle, computes the bounding-box center and maps it to a space
   **P1–P6** by horizontal position (`floor(center_x / frame_width * 6)`,
   left → right).
3. POSTs the per-frame occupancy snapshot to the dashboard web app
   (`POST /api/ingest`).
4. The web app maintains occupancy state (sessions + cooldowns) and serves a
   live dashboard at `http://HOST:8114` showing which spaces are occupied/free,
   how long each has been occupied, and usage statistics.

The post-processor is a transparent **pass-through** — it forwards the original
inference result unchanged after extracting occupancy data, so it can be chained
with other post-processors.

---

## Occupancy logic

| Constant         | Value | Meaning                                                        |
|------------------|-------|---------------------------------------------------------------|
| `NUM_SPACES`     | 6     | Parking spaces P1–P6 (split frame width into 6 equal columns)  |
| `VACATE_TIMEOUT` | 3 s   | No detection in a space for this long → space marked **vacated** |
| `COOLDOWN_SECS`  | 120 s | After a space vacates, new detections are ignored for 2 min    |

- A space becomes **OCCUPIED** when a vehicle is detected in its column; a
  session (with a unique id and start time) is opened.
- When no vehicle is seen in that column for `VACATE_TIMEOUT` seconds, the space
  is marked **vacated** and enters a **cooldown**.
- During cooldown, detections in that column are ignored — this prevents a
  parked car that flickers in/out of detection from being counted as repeated
  park events.
- **Mark Free** (manual override) immediately frees a space and clears its
  cooldown, bypassing the timers.

> The number of spaces and the left-to-right mapping are fixed (`NUM_SPACES = 6`,
> equal-width columns). For a different layout, adjust `NUM_SPACES` /
> `assign_space()` in `postprocessor-python-parking-dashboard.py`.

---

## Dashboard

Open `http://SERVER_IP:8114` in a browser after the post-processor is running.

| Panel            | Content                                                           |
|------------------|------------------------------------------------------------------|
| Parking map      | Six spaces (P1–P6); green = free, red = occupied, amber = cooldown |
| Per-space card   | Current state, occupied duration, vehicle class, **Mark Free** button |
| Statistics       | Per-space session count & occupancy over a selectable time window |
| Duration history | Occupancy-duration history chart over a selectable time window     |

The dashboard polls the web app for live state; "Mark Free" appears on a space
only when it is occupied or in cooldown.

---

## HTTP API (web app)

| Method | Path                              | Purpose                                  |
|--------|-----------------------------------|------------------------------------------|
| POST   | `/api/ingest`                     | Receive a frame's detections (from feeder) |
| GET    | `/api/status`                     | Current state of all six spaces          |
| GET    | `/api/stats?window=24h`           | Per-space session/occupancy stats        |
| GET    | `/api/history/durations?window=24h` | Occupancy-duration history             |
| POST   | `/api/release/<space_id>`         | Manually free a space (Mark Free)        |

The web app uses a threaded HTTP server (`ThreadingHTTPServer`) so concurrent
feeder POSTs and browser polling don't block each other.

---

## Files

| File                                          | Role                                              |
|-----------------------------------------------|---------------------------------------------------|
| `postprocessor-python-parking-dashboard.py`   | The AI Manager feeder (socket listener → `/api/ingest`) |
| `parking_web_app.py`                          | Self-contained dashboard web server (port 8114)   |
| `plugin.parking-dashboard.ini.example`        | Config template (copy to `plugin.parking-dashboard.ini`) |

---

## Setup

1. **Configure.** Copy the template and (optionally) point the feeder at the web
   app URL:
   ```bash
   cp plugin.parking-dashboard.ini.example plugin.parking-dashboard.ini
   ```
   ```ini
   [common]
   log_level = INFO

   [web_app]
   url = http://localhost:8114
   ```

2. **Start the web app** (typically as a systemd service):
   ```bash
   python3 parking_web_app.py --port 8114
   ```

3. **Register the feeder** with NX AI Manager by adding it to
   `external_postprocessors.json` in the AI Manager `postprocessors/` directory:
   ```json
   {
     "Name": "Parking Dashboard",
     "Command": "/path/to/postprocessor-python-parking-dashboard.py",
     "SocketPath": "/tmp/python-parking-dashboard-postprocessor.sock",
     "Events": ["parking.dashboard.tick"]
   }
   ```
   Then restart the mediaserver so it spawns the feeder.

4. In the **Nx client**, enable the "Parking Dashboard" post-processor on the
   target camera(s). The camera's AI pipeline must output **Car / Bus / Truck**
   detections (e.g. a People & Vehicles model).

5. Open `http://SERVER_IP:8114`.

---

## Notes

- Runtime files (`plugin.parking-dashboard.log`, `plugin.parking-dashboard-app.log`)
  are written next to the scripts and are git-ignored.
- The feeder parses `bboxes-format:xyxysc` tensors directly, so no
  `ReceiveConfidenceData` flag is required in the registration.
