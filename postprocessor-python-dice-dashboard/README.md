# postprocessor-python-dice-dashboard

An NX AI Manager external post-processor that classifies dice roll results
(Big / Small / Triple / Unknown) and serves a live web dashboard.

## What it does

1. Receives per-frame detection results from a dice-pip vision model.
2. Filters detections by confidence threshold and applies NMS.
3. Classifies the result (always expects exactly 3 dice — see §Classification).
4. Serves a real-time dashboard at `http://HOST:PORT` showing the current
   result, individual die values, a live bbox preview, and a roll history table.

---

## Classification rules (3 dice assumed — D1)

| Condition                          | Category |
|------------------------------------|----------|
| Detected dice != 3                 | Unknown  |
| All three values identical         | Triple   |
| Sum in [3, 9]                      | Small    |
| Sum in [10, 18]                    | Big      |

When dice count != 3 for three or more consecutive frames, the dashboard shows
a warning banner: *"Please ensure exactly 3 dice are visible in the frame."*
Rolls are only committed to history after three consecutive frames show the
same values (debounce — prevents momentary misreads from flooding history).

---

## Dashboard

Open `http://SERVER_IP:8081` in a browser after the post-processor is running.

| Panel          | Content                                                    |
|----------------|------------------------------------------------------------|
| Live Preview   | Canvas with detected bounding boxes and pip values         |
| Result Card    | Total, category badge (color + icon), three dice slots     |
| Controls       | Clear history, Export CSV, live confidence threshold slider |
| Recent Rolls   | Scrollable table — Time, Dice, Total, Result               |

Color coding: Big = red, Small = green, Triple = gold (+ pulse animation),
Unknown = gray. Icons: Big = ▲, Small = ▼, Triple = ★.

---

## Model label format

The processor accepts class labels in any of these formats:

- `"1"` – `"6"` (plain integer strings)
- `"dice_1"` – `"dice_6"`
- `"die_N"`, `"pip_N"`, `"face_N"` (case-insensitive)

Labels that cannot be parsed to an integer 1–6 are silently ignored.

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
      "Name": "Dice Dashboard",
      "Command": "/opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_manager/postprocessors/postprocessor-python-dice-dashboard",
      "SocketPath": "/tmp/python-dice-dashboard-postprocessor.sock",
      "ReceiveConfidenceData": true,
      "Settings": [
        {
          "type": "DoubleSpinBox",
          "name": "externalprocessor.confidence_threshold",
          "caption": "Confidence Threshold",
          "description": "Minimum confidence for a detection to be counted (0.0 – 1.0).",
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

### 2. Set directory permissions and restart

```bash
sudo chmod -R a+x /opt/networkoptix-metavms/mediaserver/var/nx_ai_manager/nxai_manager/postprocessors/
sudo service networkoptix-metavms-mediaserver restart
```

### 3. Activate in NX Cloud Pipelines UI

1. Open NX Cloud Pipelines.
2. Select a dice-detection AI model.
3. In the postprocessor dropdown, choose **Dice Dashboard**.
4. Open the web dashboard at `http://SERVER_IP:8081`.

---

## Configuration

Copy `plugin.dice-dashboard.ini.example` to `../etc/plugin.dice-dashboard.ini`
(relative to the installed binary) and edit as needed.

| Section     | Key                    | Default | Description                                  |
|-------------|------------------------|---------|----------------------------------------------|
| `common`    | `log_level`            | `INFO`  | Logging level (DEBUG / INFO / WARNING / ERROR) |
| `detection` | `confidence_threshold` | `0.5`   | Minimum detection confidence                 |
| `detection` | `nms_iou_threshold`    | `0.5`   | IoU threshold for NMS                        |
| `dashboard` | `port`                 | `8081`  | Dashboard TCP port                           |
| `dashboard` | `max_rolls`            | `200`   | Ring buffer size for roll history            |
| `dashboard` | `debounce_frames`      | `3`     | Frames required to confirm a roll / warning  |

The confidence threshold can also be tuned live via the NX plugin settings UI
(mapped to `externalprocessor.confidence_threshold`) or via the slider on the
dashboard itself.

---

## API endpoints

| Method | Path            | Description                                      |
|--------|-----------------|--------------------------------------------------|
| GET    | `/`             | Dashboard HTML                                   |
| GET    | `/api/latest`   | Latest frame result (JSON)                       |
| GET    | `/api/rolls`    | Recent confirmed rolls (`?limit=N`, default 50)  |
| GET    | `/api/stats`    | Uptime, frame count, category counts (JSON)      |
| GET    | `/api/export`   | Download roll history as CSV                     |
| GET    | `/healthz`      | Health check — returns `{"ok": true}`            |
| POST   | `/api/clear`    | Clear roll history                               |
| POST   | `/api/config`   | Update runtime config (body: `{"confidence_threshold": 0.7}`) |

---

## Build

From the SDK root:

```bash
mkdir build && cd build
cmake ..
cmake --build . --target postprocessor-python-dice-dashboard
cmake --install . --component postprocessor-python-dice-dashboard
```

Requires Python 3.10, Nuitka >= 2.0, and the SDK's `nxai-utilities` submodule.

---

## Local development (no camera)

```bash
./scripts/run_local.sh
```

This starts the postprocessor and a mock detection sender that generates random
dice rolls. Open `http://localhost:8081` to see the dashboard.

Environment variables accepted by `run_local.sh`:

| Variable          | Default   | Description                                    |
|-------------------|-----------|------------------------------------------------|
| `PORT`            | `8081`    | Dashboard port                                 |
| `SOCKET_PATH`     | `/tmp/…`  | Unix socket path                               |
| `TRIPLE_PROB`     | `0.15`    | Probability of a Triple roll per frame         |
| `WRONG_COUNT_PROB`| `0.05`    | Probability of sending != 3 dice per frame     |
| `FRAME_INTERVAL`  | `0.5`     | Seconds between mock frames                    |

---

## Tests

```bash
cd postprocessor-python-dice-dashboard
pip install pytest
pytest tests/ -v
```

Tests cover all §5.2 edge cases: boundary totals (3, 9, 10, 18), Triple
detection ([1,1,1], [6,6,6]), non-Triple two-same ([2,2,5]), wrong dice count
(0–2 and 4+), NMS suppression, confidence filtering, and label parsing.

---

## CSV audit log

Every confirmed roll is appended to `../etc/dice-results.csv` with columns:

```
timestamp, frame_id, dice_count, dice_values, total, category
```

The in-memory history can also be downloaded via the **Export CSV** button on
the dashboard or the `/api/export` endpoint.

---

## Defaults used for open questions (§6 of spec)

- **Q2 Label format:** `"1"`–`"6"` (overridable by changing `_label_to_value`).
- **Q3 Multi-camera:** Single stream; `?source=` query param reserved for future use.
- **Q4 Frame rate:** Dashboard polls at 1 Hz regardless of model frame rate.
- **Q7 Roll debounce:** K = 3 consecutive identical frames (same as warning debounce).
- **Q8 Auth:** No authentication; trusted intranet deployment assumed.
- **Q9 Persistence:** CSV file only; no database.
- **Q10 Color-blind:** Category also distinguished by icon (▲ ▼ ★).
- **Q15 Python version:** 3.10 (matches SDK baseline).
