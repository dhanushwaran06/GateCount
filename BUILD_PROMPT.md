# Build Prompt — Book Fair People Counter

Paste everything below this line into your Claude agent, with CLAUDE.md in the repo root.

---

Build the Book Fair People Counter described in `CLAUDE.md`, from an empty repo to a live deployment.
Read `CLAUDE.md` fully first; it is the source of truth. Keep the design minimal: exactly three parts
(counter, hub, dashboard). If you believe anything beyond `CLAUDE.md` is needed, ask me first.

Before writing code, ask me:
1. The OS of PC-A and PC-B (Windows or Linux).
2. The hub's domain name and how HTTPS is handled on my server (existing reverse proxy or not).
3. The event dates and daily opening hours.

Then work through the phases below in order. At the end of each phase, stop and give me:
the command(s) to run, what you verified, and anything I must do physically (cameras, routers).

## Target repo layout

```
book-fair-counter/
  CLAUDE.md
  README.md
  counter/
    main.py            # entry point: threads per camera, sender, state writer
    camera.py          # newest-frame grabber thread
    crossing.py        # pure line-crossing logic (no OpenCV/YOLO imports)
    sender.py          # POST on change (≤1/s) + 10 s heartbeat
    state.py           # atomic daily JSON state
    config.example.yaml
    requirements.txt
  hub/
    app.py             # FastAPI: API, SSE, SQLite minute rows, CSV export
    static/index.html  # dashboard, vanilla JS
    config.example.yaml
    Dockerfile
    docker-compose.yml
    requirements.txt
  tools/
    probe_cams.py      # list cameras, test MJPG 1280x720, print real fps
    export_model.py    # yolo26n.pt -> OpenVINO
    benchmark.py       # fps for N streams on this PC
  tests/
    test_crossing.py
    test_hub.py
  deploy/
    counter.service    # systemd (Linux)
    run_counter.bat    # Windows + Task Scheduler/NSSM notes in README
```

## Phase 0 — Scaffold

- Create the layout above, `.gitignore` (venv, `.env`, `state/`, `*.db`, model files), and `README.md`
  with setup steps for both PCs and the server.
- Pin dependencies in each `requirements.txt` (ultralytics, openvino, opencv-python, pyyaml, httpx;
  fastapi, uvicorn for the hub).

Done when: `pip install -r counter/requirements.txt` works in a fresh venv.

## Phase 1 — Cameras and model

- `tools/probe_cams.py`: list available cameras, open each at 1280×720 MJPG, report the real resolution
  and fps over 10 s. On Linux, print `/dev/v4l/by-id/` paths so config does not depend on USB order.
  On Windows, use the DirectShow backend.
- `tools/export_model.py`: download `yolo26n.pt` and export to OpenVINO at imgsz 640.
- `tools/benchmark.py`: run detection + tracking on 2 streams (cameras or video files) for 60 s and print
  fps per stream and CPU use. Target ≥ 8 fps per stream. If below, try imgsz 480 and report both.

Done when: probe lists all cameras and the benchmark shows ≥ 8 fps for 2 streams on one PC.

## Phase 2 — Crossing logic (pure, tested)

- `counter/crossing.py`: implement the rules in CLAUDE.md §7 as a small class that takes
  `(track_id, cx, cy, t)` updates and returns crossing events. No OpenCV or YOLO imports.
- `tests/test_crossing.py`: synthetic tracks covering: clean crossing each way, lingering on the line
  (no repeat counts), jitter around the line, crossing outside the x segment (ignored),
  track lost and reappearing, wrong-way crossing, stale track cleanup.

Done when: `pytest tests/test_crossing.py` passes.

## Phase 3 — Counter app

- `counter/camera.py`: a grabber thread per camera that always holds only the newest frame
  (buffer size 1, drop old frames). Reconnects automatically if the camera disconnects; reports
  `last_frame_at` and fps.
- `counter/main.py`: per camera, run `model.track(..., persist=True, classes=[0])` on the newest frame,
  feed centres to the crossing logic, update totals in memory. Use one model instance per camera thread.
- `counter/state.py`: atomic `state/<date>.json` every 2 s; load on start; new date starts at 0.
- `counter/sender.py`: POST the CLAUDE.md §8 message within 1 s of a change (max 1/s) and every 10 s
  as a heartbeat. 3 s timeout, no retry queue needed (next message is cumulative). Log failures only.
- `--debug` flag: local window with boxes, track ids, the counting line, the x segment, live counts and fps.
  Mouse click sets the line y; keys save line settings back to config. Never write frames to disk.
- `config.example.yaml`: device id, gate (entry/exit), hub URL, token env var name, and per camera:
  id, source (index, path, or video file), line_y (fraction), x_min, x_max (fractions), correct direction
  (`down`/`up`), margin, confidence.

Done when: running on a recorded video file produces correct counts that match a manual count of that
video within ±10%, and killing and restarting the process resumes the same totals.

## Phase 4 — Hub and dashboard

- `hub/app.py` with the endpoints in CLAUDE.md §8. Latest state in memory; seq check per device+date;
  bearer token per device from env; calibration factors from `hub/config.yaml`; one SQLite row per
  minute from a background task; SSE pushes on every change and every 10 s.
- `hub/static/index.html`: big IN / OUT / INSIDE numbers, a trend chart of INSIDE (5-minute rolling
  average) for today, and a health row per camera and device ("last seen N s ago", red when stale or
  offline). Readable on a phone. Live via `EventSource`, reconnects automatically.
- `tests/test_hub.py`: seq ordering, stale messages ignored, calibration math, INSIDE never below 0,
  day boundary.
- Dockerfile + docker-compose.yml with the SQLite file on a volume and a `/healthz` check.

Done when: `docker compose up` runs locally, two simulated counters (a small script posting fake
cumulative totals) drive the dashboard live, and stopping one shows it as offline within 30 s.

## Phase 5 — End-to-end test

- Run both counters on video files (or cameras) against the local hub.
- Measure: crossing-to-dashboard latency (target < 2 s), memory and CPU per PC over 1 hour.
- Fault tests: unplug a camera (only it goes stale, it recovers on replug); cut the network for
  2 minutes (counts catch up on reconnect, nothing lost); kill a counter (auto-restart, totals resume);
  restart the hub (dashboard reconnects, history intact).

Done when: all fault tests pass and you give me a short test report.

## Phase 6 — Deploy

- Hub: deploy with docker compose on my server behind HTTPS. Generate one token per device.
- Counters: install on PC-A and PC-B in a venv, export the model once per PC, set up auto-start on boot
  and auto-restart on crash (systemd or Task Scheduler/NSSM, depending on the OS I gave you).
- Write a one-page `README` section: how to start, stop, check logs, reset a day, and set
  calibration factors.

Done when: both PCs post to the live hub over their 4G routers and the dashboard works from a phone.

## Phase 7 — On-site setup and calibration (I do this; you give me the checklist and tools)

Give me a checklist covering:
- Mount cameras overhead at 3–4 m, straight down; 1080p pair at entry, 720p pair at exit.
- Run `--debug` on each PC to position the counting line and set x segments so the two views split
  the gate with no gap and no double coverage.
- Walk test: 20 crossings per camera, including pairs walking side by side.
- 10-minute hand-clicker count at each gate in real traffic; set each gate's factor =
  clicker count ÷ system count in `hub/config.yaml`.
- Check 4G signal at both gates during a crowded period.
- Morning checklist for each event day (all cameras live, day starts at 0, dashboard open).

Only if the walk test is below 85% accurate: propose fine-tuning YOLO26n on 300–500 labelled overhead
frames from our own cameras, and wait for my go-ahead before doing it.
