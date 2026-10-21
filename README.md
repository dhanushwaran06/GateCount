# Book Fair People Counter

Counts people at one entry gate (IN) and one exit gate (OUT) with webcams and YOLO26n, and shows
IN / OUT / INSIDE plus an occupancy trend on a live dashboard. `CLAUDE.md` is the design source of truth.

```text
counter/   runs on PC-A (entry) and PC-B (exit): same code, different counter/config.yaml
hub/       FastAPI + SQLite + dashboard (runs on the entry PC; Docker files for a server later)
tools/     probe_cams, export_model, benchmark, simulate (fake counters), reset_day
deploy/    systemd user services + install.sh
tests/     pytest suite (no camera needed)
```

Privacy: frames never leave the PC and are never written to disk. Only counts are sent.

**Start here:** testing on the office PCs → [`docs/TESTING.md`](docs/TESTING.md).
AI agents → [`AGENTS.md`](AGENTS.md) / [`CLAUDE.md`](CLAUDE.md).

## What runs where (fully local)

| PC | Runs | Screen |
| --- | --- | --- |
| **PC-A, entry** | counter (`pc-a`, counts IN) + hub on port 8000 | Monitor 1: live camera window (full screen). Monitor 2: big-screen display `http://localhost:8000/display` |
| **PC-B, exit** | counter (`pc-b`, counts OUT) → posts to PC-A's hub | Live camera window |

Pages on the hub (any phone or laptop on the same network, `http://<PC-A address>:8000/...`):

- `/display`: the public big-screen display (2025 expo look: neon, split-flap total, trend). Press F11 for full screen.
- `/`: staff dashboard with gate and camera health.
- `/groups`: staff form for **school groups that entered without walking through the entry cameras**.
  Needs the `STAFF_PIN` from `hub/.env`; the head count is added to IN as typed, and can be undone.

## One-time setup per PC (needs internet for ~2 minutes)

```bash
git clone https://github.com/dhanushwaran06/GateCount ~/GateCount && cd ~/GateCount   # private repo: ask for access
uv venv --python 3.12 .venv
uv pip install -r counter/requirements.txt -r hub/requirements.txt --torch-backend=cpu
.venv/bin/python tools/probe_cams.py      # lists cameras: copy their /dev/v4l/by-id/... paths into config
ls models/yolo26n_640_openvino_model || .venv/bin/python tools/export_model.py
```

- `counter/config.yaml`: `device`, `gate`, `hub_url`, and per camera `source`, line, segment, `direction`.
- Tokens: the entry PC's `hub/.env` holds `TOKEN_PC_A`, `TOKEN_PC_B` and `STAFF_PIN` (group form). Each PC's `~/GateCount/.env` holds
  `COUNTER_TOKEN=` that PC's token. New tokens: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
- Turn on **automatic login** for the user (Settings → Users), so everything starts after a power cut.
- Set **Screen Blank: Never** (Settings → Power), or the gate monitors go black after 5 minutes.

## Start, stop, logs

```bash
deploy/install.sh entry     # PC-A: counter (full-screen live window) + hub; starts now and at every login
deploy/install.sh exit      # PC-B: counter only
deploy/install.sh remove    # uninstall

systemctl --user restart gatecount-counter        # also: stop / start / status
systemctl --user restart gatecount-hub
journalctl --user -u gatecount-counter -f         # live log: COUNT / wrong way / camera problems
```

Both services restart by themselves 3 s after a crash. Counts are saved every 2 s and survive restarts.

## Live camera window

| Do | Effect |
| --- | --- |
| Left-click | Counting line here |
| Right-click | Move the nearest end of this camera's segment here |
| `d` | Flip the direction of the camera under the mouse (correct walk must flash **green**) |
| `s` | Save line, segment and direction to `counter/config.yaml` |
| `q` | Quit (the service restarts it) |

Green flash = counted, red flash = wrong way. **NO SIGNAL** = no frames for 2 s: check that camera's cable.

## Reset the day (after morning test walks)

Run on **each** PC (any order; the newest total always wins):

```bash
.venv/bin/python tools/reset_day.py
```

## Calibration

After a 10-minute hand-clicker count at a gate: factor = clicker count ÷ system count for that gate.
Edit `hub/config.yaml` (`entry_factor`, `exit_factor`) on PC-A, then `systemctl --user restart gatecount-hub`.

## Network between the gates

The exit PC only needs to reach `http://<PC-A>:8000`. Set that once in the exit PC's `counter/config.yaml`
(`hub_url:`), then `systemctl --user restart gatecount-counter`.

- **Both PCs on one router or switch:** find PC-A's address with `hostname -I` on PC-A.
  A DHCP reservation on the router keeps it from changing.
- **One cable straight between the two PCs (no router):** give both fixed addresses, once:
  `sudo nmcli con add type ethernet ifname <eth> con-name gatecount ipv4.method manual ipv4.addresses 10.10.10.1/24`
  on PC-A (`10.10.10.2/24` on PC-B). Then `hub_url: http://10.10.10.1:8000`. `<eth>` is from `nmcli dev`.
- **No link yet:** the exit PC keeps counting and saving. The dashboard shows "Exit gate not reporting".
  When the link comes up, the exit total catches up within a second.

## Event-day checklist

### Setup (once, at the venue)

1. Mount cameras rigidly, as high as possible, about 70° from the floor, looking at people's backs.
2. Plug in and run `tools/probe_cams.py`: every camera listed, `1280x720 MJPG`, about 15 fps or more.
3. Put the live window on monitor 1, the big-screen display on monitor 2 (`http://localhost:8000/display`, F11).
4. Live window: set each line across the walkway and split the two segments where the views overlap
   (a person of average height walking along the seam must be counted once). Press `s`.
5. Walk test: 20 crossings per camera with a clicker, including pairs and a short queue. Wrong way must flash red.
6. Exit PC: set `hub_url`, check the dashboard shows the exit gate online.

### Every morning before opening

1. PCs on (automatic login starts everything). Staff dashboard (`/`): both gates **Online**, all cameras **Live**.
2. Walk through once at each gate: counts go up on the dashboard within 2 s.
3. `tools/reset_day.py` on both PCs. Display shows 000 000, staff dashboard 0 / 0 / 0. Groups start at 0 too.

## Troubleshooting

- **Camera missing / NO SIGNAL with a long USB cable:** USB 2.0 passive cables stop working past ~5 m.
  Use one *powered* active extension (plug in its power adapter), do not chain extensions, and check
  `journalctl -k | grep -i usb` for `error -71` (signal or power too weak).
- **Low fps:** laptops on battery or power-saver mode throttle hard. Use mains power. Target is 8 fps or more.
- **Gate offline on the dashboard:** check that PC's counter log, its `hub_url`, and the network cable.

## Hub on an internet server (later)

`hub/` has a Dockerfile and `docker-compose.yml`: `cp config.example.yaml config.yaml && cp .env.example .env
&& docker compose up -d`, behind the server's HTTPS proxy (turn off response buffering for `/api/v1/stream`).
Then point both counters' `hub_url` at it. Nothing else changes.

## Development

```bash
.venv/bin/pytest                                   # 49 tests, no camera needed
.venv/bin/python tools/simulate.py --device pc-b --gate exit --token <TOKEN_PC_B>   # fake exit counter
```
