# Book Fair People Counter — Project Context

This file is the source of truth for the project. Read it before every task.
If an instruction elsewhere conflicts with this file, ask before acting.

## 1. What we are building

An AI people counter for a book fair (expo hall) with **one big entry gate and one big exit gate**, far apart.

- Entry gate counts **IN**. Exit gate counts **OUT**.
- Dashboard shows **IN, OUT, INSIDE** (INSIDE = IN − OUT, never below 0) and a **smoothed occupancy trend**.
- A public **big-screen display** (`/display`) carries over the 2025 expo look (neon, split-flap total); staff can
  add **school groups** that did not walk through the entry cameras (`/groups`, staff PIN).
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
| Network | Original plan: a 4G router per gate. **Current: fully local** — both PCs on one LAN (router/switch or one direct cable), see §5. Venue Wi-Fi is NOT used. |
| Server | An existing Linux server with Docker, reachable over the internet (hosts the hub) |

Camera allocation:

| Gate | PC | Cameras | Role |
|---|---|---|---|
| Entry | PC-A | C1, C2 (the two 1080p) | counts IN |
| Exit | PC-B | C3, C4 (the two 720p) | counts OUT |

All cameras are captured at **1280×720 MJPG** (the 1080p cameras are downscaled at capture) and inferred at
**imgsz 640**, so both gates behave the same.

## 4. Physical installation (affects the code)

- Cameras are mounted high (3–4 m), side by side across the gate width, with a slight overlap between the
  two views. **Decision 2026-10-21: tilted ~70° from the floor, looking at the backs of people walking through**
  (people then move UP the image → `direction: up`). Straight-down mounting also works: only `direction` and
  the line position change, never the code.
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

**Current decision (2026-10-17): run fully local.** The hub runs on the **entry PC (PC-A)** as a systemd user
service on port 8000 (plain HTTP, no internet at runtime); the exit PC posts to it over the LAN. Moving the hub
to the internet server later is a `hub_url` change only, so code must not assume either. Hub judges
offline/stale by its own receive time (PC clocks may drift offline). The entry gate has two monitors:
the counter's live window full screen (`--debug --fullscreen`) and the big-screen display (`/display`).

1. **counter** — the SAME Python program on both PCs; only `config.yaml` differs.
   One thread per camera + one sender thread + one state-writer thread.
2. **hub** — one FastAPI app in one Docker container: receives counts, keeps latest state in memory,
   writes one SQLite row per minute, serves the dashboard and a live SSE stream.
3. **dashboard** — static HTML pages served by the hub, plain JS, no build step: staff dashboard `/`,
   big-screen display `/display`, school-group form `/groups`. Fonts are bundled (no internet at runtime).

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
| Hub deploy | Now: systemd user service on PC-A (`deploy/install.sh entry`). Later on a server: Docker + compose behind HTTPS |
| Counter deploy | Python venv on each PC; systemd **user** services via `deploy/install.sh` (desktop session + automatic login) |
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
- Hub calibration factors per gate live in hub config: `IN_shown = round(IN_raw × entry_factor) + groups`
  (school-group head counts are exact, never calibrated),
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
| GET | `/` | Staff dashboard |
| GET | `/display`, `/groups` | Big-screen display; school-group form |
| GET/POST/DELETE | `/api/v1/groups[/{id}]` | List / add / undo today's school groups (POST/DELETE need `STAFF_PIN`) |

Health rules shown on the dashboard: a camera is **stale** if its `last_frame_at` is > 15 s old;
a device is **offline** if no message for > 30 s. Show "last seen N s ago" for each.

## 9. Privacy (non-negotiable)

- Never save, upload or stream frames, crops or video. Only counts leave the PC.
- The local debug window (`--debug`) shows the live frame with boxes and lines on that PC's screen only
  (full screen on the gate monitor with `--fullscreen`). Agents must never screenshot or capture that screen.
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

## 12. Current status (2026-10-21, expo opens 2026-10-22)

- Phases 0–4 of `BUILD_PROMPT.md` are built and verified (49 tests). Phase 5–7 = office/venue testing now:
  follow `docs/TESTING.md`. **Do not rebuild from BUILD_PROMPT.md** — it is the original plan, kept for history.
- Exit PC `i45g-06` (Ubuntu, user `sysadmin`, Logitech C270 + C505e) benchmarked 15 fps per camera.
  Entry PC-A is not set up yet; the dev laptop simulated it. Known issues: `docs/TESTING.md` § Known issues.

## 13. Agents working on the gate PCs

- Services are systemd **user** units: `systemctl --user {status,restart,stop} gatecount-counter gatecount-hub`;
  logs via `journalctl --user -u gatecount-counter`. Never start a second counter while the service runs.
- Ask the human before: `sudo`, reboots, network/IP changes, deleting `state/` or `hub/data/`, editing `.env`.
- Over SSH, never `pkill -f <pattern>` that also matches your own command line (it kills your shell);
  prefer `systemctl --user`, or a bracket pattern like `pkill -f "[c]ounter[.]main"`.
- Run `.venv/bin/pytest` before claiming a change works; keep diffs small; never commit `.env` or `config.yaml`.
