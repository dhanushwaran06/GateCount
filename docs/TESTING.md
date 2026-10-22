# Office test plan

Goal: prove, before the expo, that each gate counts people reliably and the dashboard shows it live.
Work through the tests in order and fill in the results table at the end. Each test says what **pass** means.

Read first: `README.md` (setup and daily operation). AI agents: `CLAUDE.md` is the source of truth,
§12–13 for the current status and the rules on the gate PCs.

## What you need

- The gate PCs (Linux) with a desktop login, their USB cameras, and the USB extension cables you will use at the venue.
- A way to mount a camera high (2.5–4 m), tilted about **70° from the floor**, looking at people's backs.
- 2–3 people to walk, and a hand clicker (or a phone counter app) for each gate.
- Access to the private GitHub repo `dhanushwaran06/GateCount` (ask Dhanushwaran to add you).

## Set up a PC (about 10 minutes, needs internet)

```bash
git clone https://github.com/dhanushwaran06/GateCount ~/GateCount && cd ~/GateCount
wget -qO- https://astral.sh/uv/install.sh | sh         # if uv is missing (curl is not installed on i45g-06)
uv venv --python 3.12 .venv
uv pip install -r counter/requirements.txt -r hub/requirements.txt pytest --torch-backend=cpu
.venv/bin/python tools/export_model.py                  # downloads YOLO26n, exports OpenVINO (models/)
.venv/bin/python tools/probe_cams.py                    # note each camera's /dev/v4l/by-id/... path
```

Not in git (secrets and per-PC settings), so create them:

```bash
cp counter/config.example.yaml counter/config.yaml      # set device, gate, hub_url, camera sources
cp hub/config.example.yaml hub/config.yaml              # entry PC only
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # run twice: one token per device
```

- Entry PC `hub/.env`: `TOKEN_PC_A=<token A>`, `TOKEN_PC_B=<token B>` and `STAFF_PIN=<8+ random characters>` (group form).
- Entry PC `.env` (repo root): `COUNTER_TOKEN=<token A>`. Exit PC `.env`: `COUNTER_TOKEN=<token B>`.
- `counter/config.yaml`: entry PC `device: pc-a`, `gate: entry`, `hub_url: http://127.0.0.1:8000`.
  Exit PC `device: pc-b`, `gate: exit`, `hub_url: http://<entry PC address>:8000` (`hostname -I` on the entry PC).
- Two cameras per PC: `x_min: 0.0, x_max: 0.55` for the first, `x_min: 0.45, x_max: 1.0` for the second.
- Settings → Power → **Screen Blank: Never**, and Settings → Users → **Automatic Login** on (both PCs).

**Only one PC available?** Configure it as the entry PC. Everything except the two-PC tests (T9d) works.

## Tests

### T1. Unit tests

`.venv/bin/pytest` → **pass:** `86 passed`.

### T2. Cameras and cables

Run T2 and T3 **before** T4. A camera can be opened by only one program: if the counter service runs,
stop it first with `systemctl --user stop gatecount-counter`.

1. `.venv/bin/python tools/probe_cams.py --seconds 30` with the venue cables plugged in.
   **Pass:** every camera listed, `1280x720 MJPG`, 12 fps or more, no "did not decode".
2. Long-cable soak, per camera: `.venv/bin/python tools/probe_cams.py --seconds 300 /dev/v4l/by-id/<camera>`.
   **Pass:** fps stays the same as in step 1 for the full 5 minutes.
3. `journalctl -k --since "-15min" | grep -iE "error -71|disconnect|over-current"`.
   **Pass:** no output. `error -71` means the cable cannot carry the signal or power (see Known issues).

### T3. CPU speed with both cameras

`.venv/bin/python tools/benchmark.py --seconds 60 <camera 1 path> <camera 2 path>`
**Pass:** `RESULT: PASS`, 8 fps or more per stream. Laptops must be on mains power.

### T4. Start the services

Entry PC: `deploy/install.sh entry`. Exit PC: `deploy/install.sh exit`.
Open `http://localhost:8000` on the entry PC (and on a phone on the same network: `http://<entry PC>:8000`).
**Pass:** within 15 s each PC shows **Online** and each camera **Live**. The live window is on screen.

### T5. Line, split and direction (70° mounting)

In the live window (mouse over the camera you are setting):
1. Left-click across the middle of the walkway: that is the counting line.
2. Right-click to move this camera's segment end to where the two camera views overlap.
3. Walk through in the gate's correct direction (into the hall at the entry, out at the exit).
   The line must flash **green**. Red: press `d` to flip direction, walk again.
4. Press `s` to save. **Pass:** `counter/config.yaml` shows the new `line_y`, `x_min`/`x_max`, `direction`.

### T6. Walk test (accuracy)

Per camera, with a hand clicker, then compare with the camera's COUNT in the live window:

| Scenario | Repeats | People |
| --- | --- | --- |
| One person, normal pace | 8 | 8 |
| Two people side by side | 3 | 6 |
| Short queue, 3 people close behind each other | 1 | 3 |
| Person stops on the line for 5 s, then continues | 1 | 1 |
| Fast walk / almost running | 2 | 2 |
| **Total correct-direction crossings** | | **20** |
| Wrong direction (walking against the flow) | 3 | 3 |

**Pass:** system count ≥ 17 of 20 (85%), and the 3 wrong-direction walks show as wrong-way, not as counts.
Below 85% at both 70° and straight down: report it, do not tune the code (CLAUDE.md §7 decides the rules).

### T7. Two-camera seam

10 people walk through exactly where the two camera views overlap.
**Pass:** the two cameras' counts together go up by 10 (±1). More means the segments overlap: move an edge.

### T8. Latency

Watch the dashboard while someone crosses. **Pass:** IN (or OUT) goes up within 2 s of the line flashing green.

### T9. Faults (each must recover with nobody touching the PC)

| | Do | Pass |
| --- | --- | --- |
| a | Unplug one camera for 20 s, plug it back | Live window: NO SIGNAL within 2 s. Dashboard: that camera Stale within ~15 s, Live again ~10 s after replug. The other camera keeps counting. |
| b | `systemctl --user kill -s SIGKILL gatecount-counter` | Restarts by itself within ~15 s. Totals are the same as before (a crossing in the last 2 s may be lost). |
| c | `systemctl --user restart gatecount-hub` | Dashboard reconnects by itself. Totals and the trend are unchanged. |
| d | Two PCs: unplug the exit PC's network for 2 min while 5 people walk out | Dashboard: Exit gate Offline after ~30 s and the red "not reporting" note. Plug back in: OUT catches up within 2 s, all 5 counted. |
| e | Reboot a PC (automatic login on) | Everything comes back without touching the keyboard. |

### T10. Reset

`.venv/bin/python tools/reset_day.py` on every PC. **Pass:** staff dashboard IN today 0, gates Online;
Total Visitors keeps the earlier days of the fair (it never resets).

### T10b. Pre-launch checklist

`.venv/bin/python tools/preflight.py --check-only` with the services stopped. **Pass:** every line
PASS or WARN and `READY`. Unplug a camera (or open it in a browser tab) and run it again: **pass** = FAIL for
that camera and `Nothing was started`.

### T11. Big-screen display and school groups

1. On the entry PC open `http://localhost:8000/display`, press F11. **Pass:** only "AI-Powered Visitor Count",
   "Total Visitors" with six flip digits, and the "Developed by Center of Excellence, ABC Technologies." line.
   One person crossing flips the total within 2 s.
2. Stop the hub for 30 s (`systemctl --user stop gatecount-hub`, then `start`). **Pass:** the display recovers by
   itself and the total comes back unchanged.
3. Open `http://<entry PC>:8000/groups` on a phone: only School Name and Number of People. Add a group: the PIN is
   asked once. Wrong PIN: **pass** = "Wrong staff PIN", nothing added. Right PIN, 25 people: the display holds for
   `display_hold_s` (10 s), then shows the total + 25. Press Undo: it goes back down (again after the hold).
   Several people crossing within a few seconds also hold, then flip to the latest total.
4. Leave the display for 10 minutes. **Pass:** the screen does not blank (Screen Blank: Never).

### T12. One-hour run (optional, recommended)

Leave both PCs running for an hour with people passing now and then.
**Pass:** `systemctl --user status gatecount-counter` memory stays flat (about 0.5–1 GB),
`journalctl --user -u gatecount-counter --since "-1h" -p warning` shows no repeating errors.

## Results (copy into a message for Dhanushwaran)

| Test | PC | Result | Numbers / notes |
| --- | --- | --- | --- |
| T1 unit tests | | | |
| T2 cameras + cables | | | fps per camera, any `error -71` |
| T3 CPU | | | fps per stream |
| T4 services | | | |
| T5 setup | | | direction used |
| T6 walk test | | | clicker vs system, per camera |
| T7 seam | | | |
| T8 latency | | | |
| T9 a–e faults | | | which passed |
| T10 reset | | | |
| T11 display + groups | | | |

## Known issues (as of 2026-10-21)

- **Second USB camera on a long active extension did not work** on the dev laptop: kernel `error -71`,
  "unable to enumerate". Try the camera directly (no extension), power the extension's adapter if it has one,
  never chain extensions. Passive USB 2.0 cables stop working past ~5 m.
- **Laptop on battery / power-saver**: about 6 fps with two cameras. Use mains power.
- **Exit PC `i45g-06`** has Ethernet and a USB Wi-Fi adapter on the same network, both on DHCP, so its address
  changes after reboots. Find it with `hostname -I`. At the venue use one cable and a fixed address (README).
- **C505e** sometimes times out on its first frame; the counter reconnects it after ~1 s. Harmless.
- Log lines `Could not find the Qt platform plugin "wayland"` and `QFontDatabase` are harmless.
  `Corrupt JPEG data` warnings from Logitech cameras are suppressed on purpose.
- PC users are not in the `video` group: camera access comes from the desktop login, so the PCs need
  **automatic login**. Adding the group needs `sudo usermod -aG video $USER` (ask first).

## For AI agents helping with these tests

- Follow `CLAUDE.md` §13. Ask the human before `sudo`, reboots, network changes or deleting data.
- Never screenshot or capture a screen showing the live window (privacy, CLAUDE.md §9).
- Useful read-only checks: `systemctl --user status 'gatecount-*'`, `journalctl --user -u gatecount-counter -n 50`,
  `.venv/bin/python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/api/v1/state').read().decode())"`,
  `.venv/bin/python tools/probe_cams.py --seconds 5` (only while the counter service is stopped: a camera opens once).
- Report results as the table above. Do not change counting rules or thresholds to make a test pass.
