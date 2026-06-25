# VLM Web Post-Processor — Specification

> Vision-Language Model integration for NX AI Manager, powered by Gemma 4 via Ollama.

---

## Overview / 概覽

**Post-processor name**: `VLM Web Post-Processor`

A web-based post-processor that connects NX AI Manager camera streams with a local Vision-Language Model (Gemma 4) to enable natural language scene understanding. Users can ask questions about live camera footage in plain language, and search through historical scene descriptions using conversational queries.

本 Post-Processor 將 NX AI Manager 攝影機串流與本機視覺語言模型（Gemma 4）整合，讓使用者可以用自然語言詢問即時畫面狀況，以及搜尋歷史場景描述。

---

## Environment / 環境

| Component | Value |
|-----------|-------|
| **VLM Model** | `gemma4:e4b` (9.6 GB) |
| **Model Host** | Mac M5 Pro 48GB — Ollama |
| **Ollama URL** | `http://localhost:11434` (local) / `http://<VM_IP>:11434` (from Parallels VM) |
| **Post-processor Host** | Parallels VM — NX Meta (Ubuntu ARM64) |
| **NX Meta URL** | `http://localhost:7001` |
| **Web App Port** | `8115` |

---

## Features / 功能

### Feature 1 — Live Scene Q&A / 即時場景問答

Users type a natural language question about the current camera scene and receive a text answer powered by Gemma 4 vision.

使用者輸入自然語言問題，Gemma 4 分析當下攝影機畫面後回答。

**Flow / 流程:**
1. User selects camera and types a question / 使用者選擇攝影機並輸入問題
2. Backend fetches current frame from NX API: `GET /ec2/cameraThumbnail?cameraId={id}&height=720`
3. Frame is base64-encoded and sent to Ollama with the question / 圖片 base64 編碼後連同問題傳送給 Ollama
4. Gemma 4 returns a natural language answer / Gemma 4 回傳自然語言答案
5. Answer is displayed in the UI with Q&A history / 答案顯示在 UI 並記錄對話歷史

**Example questions / 範例問題:**
- "Is there a car in the scene?" / "場景中有車嗎？"
- "How many people are visible?" / "可以看到幾個人？"
- "Are there any dangerous objects or situations?" / "有沒有危險物品或情況？"
- "What is happening in this scene?" / "這個場景正在發生什麼事？"
- "What color is the car in P3?" / "P3 停車格的車是什麼顏色？"

**Latency / 延遲:** ~5–8 seconds per query (gemma4:e4b on M5 Pro)

---

### Feature 2 — Historical Event Search / 歷史事件搜尋

#### Data Source 1: Inference Metadata Log / 推論元數據記錄
- NX AI Manager inference results (detected class names + timestamps) stored in SQLite
- Fast queries (1–2s): e.g. "Was a knife detected between 13:00–15:00?"
- 快速查詢（1-2 秒）：例如「13:00–15:00 有偵測到刀嗎？」

#### Data Source 2: Periodic Scene Descriptions / 定期場景描述存檔
- Every N seconds, a camera frame is fetched, analyzed by Gemma 4 vision, and the **text description only** is stored in SQLite (no images saved)
- 每 N 秒抓一張 frame，由 Gemma 4 分析後，**只儲存文字描述**（不存圖片）
- Richer queries (3–5s): e.g. "Was there suspicious activity near the entrance?"
- 較豐富的查詢（3-5 秒）：例如「入口附近有沒有可疑行為？」

**SQLite schema / 資料庫結構:**
```
inference_log:   timestamp | camera_id | detected_classes
scene_log:       timestamp | camera_id | description (text)
```

**Snapshot interval (dynamic) / 快照間隔（動態）:**

| Condition | Interval |
|-----------|----------|
| Objects detected by NX AI Manager / 有偵測到物件 | Every **20 seconds** |
| No objects detected / 沒有物件 | Every **60 seconds** |

**Storage estimate / 儲存空間估算:**
- Text descriptions only: ~1–2 MB per camera per day
- Retention policy: auto-delete records older than 30 days
- 只存文字描述：每台攝影機每天約 1-2 MB，自動保留 30 天

---

### Feature 2 UX — Viewing Events in NX / 在 NX 中查看事件

After finding an interesting event in the search results, users have two options to view the footage in NX:

在搜尋結果中找到有興趣的事件後，使用者有兩種方式在 NX 中查看影像：

**Option 2 — On-demand Thumbnail Preview / 即時縮圖預覽**
- When results appear, the backend fetches the NX thumbnail at that exact timestamp on-demand
- No extra storage needed
- 搜尋結果出現時，後端即時從 NX 抓取那個時間點的縮圖
- `GET /ec2/cameraThumbnail?cameraId={uuid}&height=480&time={timestamp_ms}`
- Auth: Bearer token (obtained via `POST /rest/v1/login/sessions`)
- ✅ Verified: returns JPEG, HTTP 200

**Option 3 — "Mark in NX" Bookmark / 建立 NX Bookmark**
- One-click button creates an NX Bookmark at the event timestamp via NX API
- User opens NX Client → Bookmarks → jumps directly to footage
- 一鍵在 NX 建立 Bookmark，使用者在 NX Client → Bookmarks 直接跳到該片段
- ⚠️ Requires NX server to have storage/archive configured for the camera
```json
POST /rest/v3/devices/{cameraId}/bookmarks
Authorization: Bearer {token}

{
  "name": "VLM: <event summary>",
  "startTimeMs": 1748834525000,
  "durationMs": 60000,
  "description": "<full Gemma 4 description>"
}
```
- ✅ Verified: returns bookmark object with id, deviceId, startTimeMs

> **Design principle / 設計原則:** Web UI handles **search**; NX Client handles **playback**. Web UI 負責搜尋，NX Client 負責播放影片。

---

## UI Layout / 介面佈局

Tab-based layout / Tab 分頁佈局：

### Tab 1 — Live Q&A
```
┌─────────────────────────────────────────┐
│ [ Live Q&A ]  [ History Search ]        │
├─────────────────────────────────────────┤
│  ┌───────────────────────────────────┐  │
│  │   Live Camera Frame               │  │
│  │   (refreshes on each query)       │  │
│  │   Last updated: 13:42:05          │  │
│  └───────────────────────────────────┘  │
│                                         │
│  [Ask anything about the scene...     ] │
│  [                                    ] │
│  [                           ] [Send]   │
│                                         │
│  ── Q&A History ──────────────────────  │
│  Q: Is there a car in the scene?        │
│  A: Yes, a white sedan is parked...     │
│                                         │
│  Q: Any people visible?                 │
│  A: No people detected in the frame...  │
└─────────────────────────────────────────┘
```

### Tab 2 — History Search
```
┌─────────────────────────────────────────┐
│ [ Live Q&A ]  [ History Search ]        │
├─────────────────────────────────────────┤
│  From [13:00]  To [15:00]  [Today ▼]   │
│  Ask: [Was there a man with a knife? ]  │
│  [Search]                               │
│                                         │
│  ── Results ─────────────────────────── │
│  13:42:00  ┌──────┐  "A person near    │
│  [Mark NX] │ img  │   the entrance..."  │
│            └──────┘                     │
│  13:42:20  ┌──────┐  "Same person,     │
│  [Mark NX] │ img  │   still present"   │
│            └──────┘                     │
└─────────────────────────────────────────┘
```

---

## Configuration / 設定檔

`plugin.vlm-web.ini`:

```ini
[nx]
url      = http://localhost:7001
username = admin
password = <NX_PASSWORD>

[ollama]
url   = http://<VM_IP>:11434
model = gemma4:e4b

[scanner]
interval_active = 20    # seconds when objects detected / 有物件時的間隔（秒）
interval_idle   = 60    # seconds when scene is empty / 場景空閒時的間隔（秒）

[web_app]
port = 8115

[retention]
days = 30               # auto-delete descriptions older than N days / 自動刪除 N 天前的記錄
```

---

## NX API Reference / NX API 參考（已驗證 2026-05-31）

> All endpoints require Bearer token authentication. Basic Auth is disabled.
> 所有 endpoint 需要 Bearer token 認證，Basic Auth 已停用。

### Authentication / 認證
```
POST /rest/v1/login/sessions
Body: { "username": "admin", "password": "<NX_PASSWORD>" }
→ Returns: { "token": "vms-xxxxx..." }
→ Use as: Authorization: Bearer {token}
```

### API Endpoints

| # | Purpose / 用途 | Method | Endpoint | Status |
|---|----------------|--------|----------|--------|
| 1 | Get auth token / 取得 token | `POST` | `/rest/v1/login/sessions` | ✅ Verified |
| 2 | List cameras / 列出攝影機 | `GET` | `/ec2/getCamerasEx` | ✅ Verified |
| 3 | Live thumbnail / 即時縮圖 | `GET` | `/ec2/cameraThumbnail?cameraId={uuid}&height=480` | ✅ Verified, returns JPEG |
| 4 | Historical thumbnail / 歷史縮圖 | `GET` | `/ec2/cameraThumbnail?cameraId={uuid}&height=480&time={ms}` | ✅ Endpoint confirmed |
| 5 | Create bookmark / 建立 Bookmark | `POST` | `/rest/v3/devices/{cameraId}/bookmarks` | ✅ Verified, visible in NX Client |
| 6 | List bookmarks / 查詢 Bookmark | `GET` | `/ec2/bookmarks` | ✅ Verified |

### Notes / 注意事項
- **Camera ID format**: `{uuid-with-braces}` in query params; without braces in URL path
- **Camera ID 格式**：query 參數帶大括號 `{uuid}`；URL path 不帶括號
- **Bookmark** requires camera to have recording/archive enabled / 需要攝影機已啟用錄影
- **`time` parameter**: Unix timestamp in milliseconds / Unix 時間戳記（毫秒）

### Create Bookmark Payload
```json
POST /rest/v3/devices/{cameraId}/bookmarks
Authorization: Bearer {token}

{
  "name": "VLM: <event summary>",
  "startTimeMs": 1748834525000,
  "durationMs": 60000,
  "description": "<full Gemma 4 description>"
}
```

---

## Tech Stack / 技術堆疊

| Component | Technology |
|-----------|------------|
| Post-processor | Python 3 |
| Web framework | Built-in `http.server` (no dependencies) |
| VLM | Gemma 4 `e4b` via Ollama REST API |
| Database | SQLite (built-in) |
| Frame source | NX REST API (`/ec2/cameraThumbnail`) |
| Bookmark creation | NX REST API (`/rest/v3/devices/{id}/bookmarks`) |
| Auth | Bearer token (`/rest/v1/login/sessions`) |
| Communication with NX AI Manager | Unix socket (`nxai_communication_utils`) |

---

## File Structure / 檔案結構

```
postprocessor-python-vlm-web/
├── SPEC.md                              # This file / 本文件
├── plugin.vlm-web.ini.example           # Example config / 設定範例
├── postprocessor-python-vlm-web.py      # Post-processor (socket listener)
└── vlm_web_app.py                       # Web app (HTTP server + Ollama integration)
```

---

## Development Status / 開發狀態

| Phase | Status |
|-------|--------|
| Brainstorming & spec | ✅ Complete |
| Environment setup (Ollama + gemma4:e4b) | ✅ Complete |
| NX API verification | ✅ Complete |
| Post-processor implementation | ✅ Complete |
| Web app — Feature 1 (Live Q&A) | ✅ Complete |
| Web app — Feature 2 (History Search) | ✅ Complete |
| NX Bookmark integration | ✅ Complete |
| Testing on Parallels VM | ✅ Complete |

---

_Last updated: 2026-06-01_
