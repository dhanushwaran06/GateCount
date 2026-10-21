# Book Fair People Counter — Project Context

This file is the source of truth for the project. Read it before every task.
If an instruction elsewhere conflicts with this file, ask before acting.

## 1. What we are building

An AI people counter for a book fair (expo hall) with **one big entry gate and one big exit gate**, far apart.

- Entry gate counts **IN**. Exit gate counts **OUT**.
- Dashboard shows **IN, OUT, INSIDE** (INSIDE = IN − OUT, never below 0) and a **smoothed occupancy trend**.
- Goal: a **slow, steady, believable occupancy trend through the day**. Exact counts are NOT required.
  Target accuracy: each gate within ±10% of a manual clicker count after calibration.
- Latency target: a crossing shows on the dashboard within **2 s**.

## 2. Design principle — minimal, reliable, fast

- Minimal: do not add a component unless it removes a real failure. No message broker, no database
  server, no Kubernetes, no microservices, no ORM, no frontend framework, no build step.
- Reliable: a lost network message must never lose counts. A crashed process must resume from saved state.
- Fast: event-driven updates, not polling. Disk writes never block the counting loop.
- If you think something more complex is needed, STOP and explain why before building it.

## 3. Hardware (fixed — design around this)

| Item | Detail |
|---|---|
| PCs | 2 PCs, 16 GB RAM each, CPU inference (assume no GPU; support GPU if present) |
| Webcams | 4 USB webcams: 2 × 1080p, 2 × 720p |
| Network | Each gate has its own 4G modem/router. PC connects to it by LAN cable. Venue Wi-Fi is NOT used. |
| Server | An existing Linux server with Docker, reachable over the internet (hosts the hub) |

Camera allocation:

| Gate | PC | Cameras | Role |
|---|---|---|---|
| Entry | PC-A | C1, C2 (the two 1080p) | counts IN |
| Exit | PC-B | C3, C4 (the two 720p) | counts OUT |

All cameras are captured at **1280×720 MJPG** (the 1080p cameras are downscaled at capture) and inferred at
**imgsz 640**, so both gates behave the same.

## 4. Physical installation (affects the code)

- Cameras are mounted **overhead at 3–4 m, pointing straight down**, side by side across the gate width,
  with a slight overlap between the two views.
- Each frame has a **counting line across the walkway, in the middle of the frame**.
- Each camera only counts crossings inside **its own horizontal segment** of the line (x_min..x_max as
  fractions of frame width), so the overlap area is counted by exactly one camera.
- Mounting is rigid; line positions are set once per site in config.

## 5. Architecture (exactly three parts — do not add more)

```
Entry: C1,C2 ─USB─► PC-A  counter ─HTTPS POST─┐
                                              ├─► hub (Docker on server): API + SQLite + dashboard
Exit:  C3,C4 ─USB─► PC-B  counter ─HTTPS POST─┘                │
                                                     dashboard opened from any phone/laptop
```

**Current decision (2026-10-17): run fully local.** The hub runs on a machine on a LAN shared by both gates
(plain HTTP, no internet at runtime). Moving the hub to the internet server later is a `hub_url` change only,
so code must not assume either. Hub judges offline/stale by its own receive time (PC clocks may drift offline).

1. **counter** — the SAME Python program on both PCs; only `config.yaml` differs.
   One thread per camera + one sender thread + one state-writer thread.
2. **hub** — one FastAPI app in one Docker container: receives counts, keeps latest state in memory,
   writes one SQLite row per minute, serves the dashboard and a live SSE stream.
3. **dashboard** — one static HTML file served by the hub. Plain JS, no build step.

## 6. Tech stack (use these, do not swap without asking)

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Capture | OpenCV (`opencv-python`), grabber thread that keeps only the newest frame |
| Detection | Ultralytics **YOLO26n** (`yolo26n.pt`), person class only, exported to **OpenVINO** for CPU |
| Tracking | ByteTrack via `model.track(persist=True, tracker="bytetrack.yaml")` |
| Sender | `httpx` (or `requests`) with short timeouts |
| Config | YAML (`pyyaml`) |
| Hub | FastAPI + uvicorn, SSE for live push, SQLite via stdlib `sqlite3` |
| Dashboard | Single `index.html`, vanilla JS, `EventSource`, a small inline SVG/canvas trend chart |
| Hub deploy | Docker + docker compose, behind the server's existing HTTPS reverse proxy |
| Counter deploy | Python venv on each PC; systemd (Linux) or Task Scheduler/NSSM (Windows) for auto-start + restart |
| Timezone | Asia/Kolkata for all dates and day boundaries |

Licence note: Ultralytics is AGPL-3.0. Fine for this internal event deployment.

## 7. Counting rules

- Detect `person` only (class 0), confidence ≥ 0.35 (configurable).
- Use the **centre of each tracked box**.
- A crossing counts when a track moves from **before** the line to **after** the line with hysteresis:
  it must have been at least `margin` px before the line and then at least `margin` px after it
  (default margin = 4% of frame height). Each track id counts at most once per direction.
- Only count if the centre's x is inside this camera's segment [x_min, x_max].
- Direction: config says which direction (`down` or `up` in the image) is the gate's correct direction.
  Correct direction → `count`. Opposite direction → `wrong_way` (logged, never added to totals).
- Forget track state for ids not seen for 5 s.

## 8. Data rules (these make the system reliable)

- Counters send **cumulative daily totals**, never single events. A lost message is fixed by the next one.
- Every message carries a monotonically increasing `seq`; the hub ignores older seq for the same device+date.
- Counter sends **within 1 s of any change** (at most 1 message/s) plus a **heartbeat every 10 s**.
- Counter keeps totals in memory and writes `state/<date>.json` **atomically** (temp file + `os.replace`)
  every 2 s from a separate thread. On start, it loads today's file if present; a new date starts at 0.
- Hub calibration factors per gate live in hub config: `IN_shown = round(IN_raw × entry_factor)`,
  `OUT_shown = round(OUT_raw × exit_factor)`, `INSIDE = max(0, IN_shown − OUT_shown)`.
- Hub writes one row per minute to SQLite: date, minute, in_shown, out_shown, inside.
- Trend line = 5-minute rolling average of INSIDE, computed in the dashboard from the minute rows.

### Counter → hub message (POST /api/v1/counts, `Authorization: Bearer <device token>`)

```json
{
  "device": "pc-a",
  "gate": "entry",
  "date": "2026-11-02",
  "seq": 1842,
  "sent_at": "2026-11-02T11:42:07+05:30",
  "cameras": {
    "c1": {"count": 612, "wrong_way": 3, "fps": 9.6, "last_frame_at": "2026-11-02T11:42:07+05:30"},
    "c2": {"count": 598, "wrong_way": 1, "fps": 9.9, "last_frame_at": "2026-11-02T11:42:07+05:30"}
  }
}
```

### Hub endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/counts` | Counter updates (bearer token per device) |
| GET | `/api/v1/state` | Current IN, OUT, INSIDE, per-camera health |
| GET | `/api/v1/stream` | SSE: pushes state on every change + every 10 s |
| GET | `/api/v1/history?date=YYYY-MM-DD` | Minute rows for the trend |
| GET | `/api/v1/export.csv?date=YYYY-MM-DD` | Minute rows as CSV |
| GET | `/healthz` | Liveness |
| GET | `/` | Dashboard |

Health rules shown on the dashboard: a camera is **stale** if its `last_frame_at` is > 15 s old;
a device is **offline** if no message for > 30 s. Show "last seen N s ago" for each.

## 9. Privacy (non-negotiable)

- Never save, upload or stream frames, crops or video. Only counts leave the PC.
- The local debug window (`--debug`) shows the live frame with boxes and lines on that PC's screen only.
- No face detection or recognition of any kind.

## 10. Non-goals (do not build)

Crowd-density models, zone/stall counting, user accounts, multi-event support, mobile apps,
message brokers, cloud video, re-identification across cameras, alerting systems.

## 11. Working rules for the agent

- Build in the phases given in the build prompt. Finish and verify each phase before the next.
- Every phase ends with a runnable command and a short note of how it was verified.
- Write tests for the line-crossing logic with synthetic tracks (no camera needed).
- Make every camera source configurable as either a device (index or `/dev/v4l/by-id/...` path) or a
  **video file**, so everything can be tested without live cameras.
- Keep the whole repo small and readable. Prefer one clear file over many tiny ones.
- Never commit secrets; tokens come from environment variables or a git-ignored `.env`.
