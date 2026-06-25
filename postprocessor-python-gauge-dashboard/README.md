# postprocessor-python-gauge-dashboard

An NX AI Manager external post-processor that reads numeric gauge values from
a vision model and serves a real-time web dashboard.

## What it does

1. Receives per-frame detection results from a gauge reading model.
2. Filters detections by confidence threshold and applies NMS.
3. Classifies the reading as **Normal**, **High**, or **Low** based on configurable thresholds.
4. Serves a real-time dashboard at `http://HOST:PORT` showing a live analog gauge,
   trend chart, alert status, and reading history.

---

## Classification rules

| Condition                        | Status   |
|----------------------------------|----------|
| reading < `alert_low`            | Low      |
| reading > `alert_high`           | High     |
| `alert_low` ≤ reading ≤ `alert_high` | Normal |

---

## Dashboard

Open `http://SERVER_IP:8082` in a browser after the post-processor is running.

| Panel          | Content                                                    |
|----------------|------------------------------------------------------------|
| Analog Gauge   | Semicircular canvas gauge with colored zones and needle     |
| Reading Card   | Current value, status badge (Normal / High / Low), min/max/avg |
| Trend Chart    | Line chart of last N readings with threshold markers        |
| Recent Readings| Scrollable table — Time, Reading, Confidence, Status        |

Color coding: Normal = green, High = red, Low = blue.

---

## Model label format

The processor accepts class labels in any of these formats:

- `"45.5"` — plain float string
- `"reading_45.5"`, `"gauge_75"`, `"value_0.8"` — prefixed
- `"45%"` — percentage suffix (stripped before parsing)
- `"level_30.0"`, `"class_50"` — other common prefixes

Labels that cannot be parsed to a float are silently ignored.

---

## Integration with NX AI Manager

### 1. Register the post-processor

Add an entry to `external_postprocessors.json`:

**Linux (NX Meta)**
```
/opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_manager/postprocessors/external_postprocessors.json
```

```json
{
  "externalPostprocessors": [
    {
      "Name": "Gauge Dashboard",
      "Command": "/opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_manager/postprocessors/postprocessor-python-gauge-dashboard",
      "SocketPath": "/tmp/python-gauge-dashboard-postprocessor.sock",
      "ReceiveConfidenceData": true,
      "Settings": [
        {
          "type": "DoubleSpinBox",
          "name": "externalprocessor.confidence_threshold",
          "caption": "Confidence Threshold",
          "description": "Minimum confidence for a detection to be used (0.0 – 1.0).",
          "defaultValue": 0.5,
          "minValue": 0.0,
          "maxValue": 1.0
        }
      ]
    }
  ]
}
```

> **Important:** `ReceiveConfidenceData: true` is required so that per-detection
> confidence values are included in `ObjectsMetaData.<class>.Confidences`.

**Windows (NX Meta)**
```
C:\Windows\System32\config\systemprofile\AppData\Local\Network Optix\Network Optix MetaVMS Media Server\nx_ai_manager\nxai_manager\postprocessors\external_postprocessors.json
```

### 2. Set permissions and restart (Linux)

```bash
sudo chmod -R a+x /opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_manager/postprocessors/
sudo service networkoptix-metavms-mediaserver restart
```

### 3. Activate in NX Cloud Pipelines UI

1. Open NX Cloud Pipelines.
2. Select your gauge reading AI model.
3. In the postprocessor dropdown, choose **Gauge Dashboard**.
4. Open the web dashboard at `http://SERVER_IP:8082`.

---

## Configuration

Copy `plugin.gauge-dashboard.ini.example` to `../etc/plugin.gauge-dashboard.ini`
(relative to the installed binary) and edit as needed.

| Section     | Key                    | Default | Description                                       |
|-------------|------------------------|---------|---------------------------------------------------|
| `common`    | `log_level`            | `INFO`  | Logging level (DEBUG / INFO / WARNING / ERROR)    |
| `detection` | `confidence_threshold` | `0.5`   | Minimum detection confidence                      |
| `detection` | `nms_iou_threshold`    | `0.5`   | IoU threshold for NMS                             |
| `gauge`     | `min_value`            | `0.0`   | Minimum physical gauge value                      |
| `gauge`     | `max_value`            | `100.0` | Maximum physical gauge value                      |
| `gauge`     | `unit`                 | `%`     | Unit label shown on dashboard (%, PSI, °C, bar…)  |
| `gauge`     | `alert_low`            | `20.0`  | Low alert threshold                               |
| `gauge`     | `alert_high`           | `80.0`  | High alert threshold                              |
| `dashboard` | `port`                 | `8082`  | Dashboard TCP port                                |
| `dashboard` | `history_size`         | `200`   | Ring buffer size for reading history              |
| `dashboard` | `trend_size`           | `60`    | Number of readings shown in trend chart           |

---

## API endpoints

| Method | Path              | Description                                        |
|--------|-------------------|----------------------------------------------------|
| GET    | `/`               | Dashboard HTML                                     |
| GET    | `/api/latest`     | Latest frame result (JSON)                         |
| GET    | `/api/history`    | Recent readings (`?limit=N`, default 50)           |
| GET    | `/api/trend`      | Trend data points for chart (JSON)                 |
| GET    | `/api/stats`      | Uptime, frame count, status counts, min/max/avg    |
| GET    | `/api/export`     | Download reading history as CSV                    |
| GET    | `/healthz`        | Health check — returns `{"ok": true}`              |
| POST   | `/api/clear`      | Clear reading history                              |
| POST   | `/api/config`     | Update runtime config (`{"confidence_threshold": 0.7}`) |

---

## Build

From the SDK root:

```bash
mkdir build && cd build
cmake ..
cmake --build . --target postprocessor-python-gauge-dashboard
cmake --install . --component postprocessor-python-gauge-dashboard
```

Requires Python 3.10, Nuitka >= 2.0, and the SDK's `nxai-utilities` submodule.

---

## CSV audit log

Every reading is appended to `../etc/gauge-readings.csv` with columns:

```
timestamp, frame_id, reading, confidence, status
```

Download via the **Export CSV** button on the dashboard or the `/api/export` endpoint.
