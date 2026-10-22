"""Pre-launch checklist for a gate PC. Prints PASS / WARN / FAIL for every check and starts GateCount only
when no mandatory check FAILs (a WARN never blocks).

  python tools/preflight.py                # check, then launch (entry PC: hub + counter; exit PC: counter)
  python tools/preflight.py --check-only   # only check; exit code 0 = ready, 1 = a mandatory check failed
  python tools/preflight.py --fullscreen   # launch with the live window full screen (gate monitor)
  python tools/preflight.py --headless     # launch the counter without the live window

Checks: Python and the pinned packages, config files and device tokens, the model (loads and runs), each camera
(connected, USB link speed, not held by another program, opens, delivers frames, fps, picture not black),
kernel USB errors (error -71 = cable/extension too weak), hub database and counter state files, disk space,
LAN address and hub reachability, clock sync, and that no counter or hub is already running.
Camera frames are only measured in memory: never saved, never shown (CLAUDE.md §9).

Launch: if the systemd user services are installed (deploy/install.sh) they are started; otherwise the hub
(if this PC hosts it) and the counter run from here until Ctrl-C.
"""

import argparse
import importlib.metadata as md
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LINUX = sys.platform.startswith("linux")
IST = timezone(timedelta(hours=5, minutes=30))
UNITS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
MIN_FPS, MIN_FREE_MB, LOW_FREE_MB = 8.0, 200, 2048

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
COLOR = {PASS: 32, WARN: 33, FAIL: 31}
results = []


def report(status, name, detail=""):
    results.append(status)
    tag = f"\033[{COLOR[status]}m{status}\033[0m" if sys.stdout.isatty() else status
    print(f"[{tag}] {name}{': ' + detail if detail else ''}", flush=True)


def section(title):
    print(f"\n== {title}", flush=True)


def read_env(path):
    """KEY=VALUE lines of a .env file (same rules as the counter and hub), without touching os.environ."""
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                env[key.strip()] = value.strip().strip("'\"")
    return env


def http_get(url, timeout=3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode(errors="replace")


def run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def processes():
    """{pid: command line} of this user's processes (Linux /proc)."""
    procs = {}
    for p in Path("/proc").glob("[0-9]*") if LINUX else []:
        try:
            procs[int(p.name)] = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            pass
    return procs


def holders(device):
    """Processes (pid, name) that have `device` open, other than this one."""
    real, found = os.path.realpath(device), []
    for p in Path("/proc").glob("[0-9]*") if LINUX else []:
        if int(p.name) == os.getpid():
            continue
        try:
            if any(os.readlink(fd) == real for fd in (p / "fd").iterdir()):
                found.append((int(p.name), (p / "comm").read_text().strip()))
        except OSError:
            pass
    return found


def local_ipv4():
    r = run(["hostname", "-I"]) if LINUX else None
    if r and r.returncode == 0:
        return [a for a in r.stdout.split() if "." in a and not a.startswith("127.")]
    try:
        return [a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("127.")]
    except OSError:
        return []


# ---------------------------------------------------------------- checks

def check_python():
    v = sys.version_info
    report(PASS if v >= (3, 11) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 11) else " (needs 3.11+)"))
    in_venv = sys.prefix != sys.base_prefix
    report(PASS if in_venv else WARN, "Virtual environment",
           sys.prefix if in_venv else "not running from .venv: use .venv/bin/python tools/preflight.py")


def check_packages():
    pins = {}
    for req in ("counter/requirements.txt", "hub/requirements.txt"):
        for line in (ROOT / req).read_text().splitlines():
            if m := re.match(r"\s*([A-Za-z0-9_.-]+)==([^\s#]+)", line):
                pins[m[1]] = m[2]
    missing, other = [], []
    for name, want in pins.items():
        try:
            have = md.version(name)
        except md.PackageNotFoundError:
            missing.append(name)
            continue
        if have.split("+")[0] != want:
            other.append(f"{name} {have} (pinned {want})")
    if missing:
        report(FAIL, "Python packages", f"missing {', '.join(missing)}: uv pip install -r counter/requirements.txt "
                                        "-r hub/requirements.txt --torch-backend=cpu")
    elif other:
        report(WARN, "Python packages", "versions differ from the pins: " + "; ".join(other))
    else:
        report(PASS, "Python packages", f"{len(pins)} pinned packages installed")
    tool = shutil.which("v4l2-ctl") if LINUX else "n/a"
    report(PASS if tool else WARN, "v4l2-ctl", tool or "missing (sudo apt install v4l-utils): 50 Hz anti-flicker "
                                                       "and fixed frame rate are not applied to the cameras")


def check_counter_config():
    import yaml
    from counter.crossing import LineCounter
    path = ROOT / "counter" / "config.yaml"
    if not path.exists():
        report(FAIL, "counter/config.yaml", "missing: cp counter/config.example.yaml counter/config.yaml and edit it")
        return None
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        report(FAIL, "counter/config.yaml", f"not valid YAML: {e}")
        return None
    problems = [f"missing '{k}'" for k in ("device", "gate", "hub_url", "cameras") if k not in cfg]
    if not problems:
        if cfg["gate"] not in ("entry", "exit"):
            problems.append("gate must be entry or exit")
        cams = cfg["cameras"] or []
        ids, sources = [c.get("id") for c in cams], [str(c.get("source")) for c in cams]
        if not cams:
            problems.append("no cameras")
        if len(set(ids)) != len(ids):
            problems.append(f"camera ids not unique: {ids}")
        if len(set(sources)) != len(sources):
            problems.append("two cameras use the same source")
        for c in cams:
            try:
                LineCounter.from_fractions(c.get("line_y", 0.5), c.get("x_min", 0.0), c.get("x_max", 1.0),
                                           c.get("direction", "down"), 1280, 720, c.get("margin", 0.04))
            except (ValueError, TypeError) as e:
                problems.append(f"{c.get('id')}: {e}")
    if problems:
        report(FAIL, "counter/config.yaml", "; ".join(problems))
        return None
    report(PASS, "counter/config.yaml", f"{cfg['device']} ({cfg['gate']} gate), {len(cfg['cameras'])} camera(s), "
                                        f"hub {cfg['hub_url']}")
    return cfg


def hub_is_local(cfg):
    host = urllib.parse.urlparse(cfg["hub_url"]).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host in local_ipv4() or host == socket.gethostname()


def check_tokens(cfg, hosts_hub, hub_cfg):
    env = {**read_env(ROOT / ".env"), **os.environ}
    name = cfg.get("token_env", "COUNTER_TOKEN")
    token = env.get(name, "")
    if not token or token == "change-me":
        report(FAIL, f"Device token ({name})", "not set in .env: the hub would reject every message")
    else:
        report(PASS, f"Device token ({name})", "set")
    if not hosts_hub:
        return
    hub_env = {**read_env(ROOT / "hub" / ".env"), **os.environ}
    if not (ROOT / "hub" / ".env").exists():
        report(FAIL, "hub/.env", "missing: copy hub/.env.example and set the tokens (see README)")
        return
    for device, gate in (hub_cfg or {}).get("devices", {}).items():
        if gate == "exit" and not hub_cfg.get("exit_enabled"):
            continue
        var = "TOKEN_" + device.upper().replace("-", "_")
        t = hub_env.get(var, "")
        if not t or t == "change-me":
            report(FAIL, f"hub/.env {var}", f"not set: {device}'s messages would be rejected")
        elif device == cfg["device"] and t != token:
            report(FAIL, f"hub/.env {var}", f"differs from this PC's {name}: the hub would answer 401")
        else:
            report(PASS, f"hub/.env {var}", "set" + (", matches this PC" if device == cfg["device"] else ""))
    pin = hub_env.get("STAFF_PIN", "")
    report(PASS if len(pin) >= 8 and pin != "change-me" else WARN, "Staff PIN (school-group form)",
           "set" if len(pin) >= 8 and pin != "change-me" else "missing or shorter than 8 characters: the group form stays off")


def check_hub_config():
    from hub import app as hub_app
    path = ROOT / "hub" / "config.yaml"
    try:
        cfg = hub_app.load_config(path)
    except SystemExit as e:
        report(FAIL, "hub/config.yaml", str(e))
        return None
    except Exception as e:  # noqa: BLE001 - any parse error is a FAIL with its message
        report(FAIL, "hub/config.yaml", f"{type(e).__name__}: {e}")
        return None
    report(PASS if path.exists() else WARN, "hub/config.yaml",
           ("" if path.exists() else "missing, using defaults; ") +
           f"entry x{cfg['entry_factor']}, exit gate {'ENABLED' if cfg['exit_enabled'] else 'disabled'}, "
           f"display hold {cfg['display_hold_s']} s")
    today = datetime.now(IST).date().isoformat()
    start = cfg["fair_start"]
    if start and today < start:
        report(WARN, "Fair start", f"{start} is after today ({today}): Total Visitors shows only days from {start}")
    else:
        report(PASS, "Fair start", f"Total Visitors adds up every day since {start or 'the first saved day'}")
    return cfg


def check_model(cfg):
    model = ROOT / cfg.get("model", "models/yolo26n_640_openvino_model")
    if not model.exists() or not list(model.glob("*.xml")):
        report(FAIL, "Model", f"{model.relative_to(ROOT)} not found: run .venv/bin/python tools/export_model.py")
        return
    # Load and run once in a child process, so the model's memory is not kept by the launcher.
    code = (
        "import os,sys,time,yaml,numpy as np;os.environ['YOLO_OFFLINE']='True';os.environ['YOLO_AUTOINSTALL']='False'\n"
        "from pathlib import Path;from ultralytics import YOLO;p=Path(sys.argv[1]);m=p/'metadata.yaml'\n"
        "imgsz=yaml.safe_load(m.read_text())['imgsz'] if m.exists() else 640;y=YOLO(str(p),task='detect')\n"
        "f=np.zeros((720,1280,3),np.uint8);y.predict(f,imgsz=imgsz,verbose=False);t=time.perf_counter()\n"
        "for _ in range(5): y.predict(f,imgsz=imgsz,verbose=False)\n"
        "print('MS', (time.perf_counter()-t)/5*1000)")
    r = run([sys.executable, "-c", code, str(model)], timeout=180)
    ms = re.search(r"^MS ([\d.]+)", r.stdout, re.M) if r else None
    if not ms:
        err = (r.stderr.strip().splitlines() or ["no output"])[-1] if r else "timed out"
        report(FAIL, "Model", f"does not load/run: {err}")
        return
    ms = float(ms[1])
    report(PASS if ms < 120 else WARN, "Model (OpenVINO CPU)",
           f"{model.name} loads and runs, {ms:.0f} ms per frame" + ("" if ms < 120 else " (slow: expect low fps)"))


def usb_info(device):
    """(port like '1-7', product, speed Mbit/s) of the USB camera behind /dev/videoN, or None."""
    try:
        iface = Path(f"/sys/class/video4linux/{Path(os.path.realpath(device)).name}/device").resolve()
        usb = iface.parent
        read = lambda f: (usb / f).read_text().strip() if (usb / f).exists() else ""  # noqa: E731
        return usb.name, read("product"), read("speed")
    except OSError:
        return None


def camera_ports(cfg):
    """{USB port: camera id} for the configured cameras that are plugged in, to name them in kernel errors."""
    from counter.camera import parse_source
    ports = {}
    for cam in cfg["cameras"] if LINUX else []:
        src, is_device = parse_source(cam["source"])
        dev = f"/dev/video{src}" if isinstance(src, int) else src
        if is_device and os.path.exists(dev) and (info := usb_info(dev)):
            ports[info[0]] = cam["id"]
    return ports


def check_cameras(cfg, seconds):
    import cv2
    import numpy as np
    from counter.camera import WIDTH, HEIGHT, open_capture, parse_source, quiet_libjpeg_warnings, to_frame
    restore = quiet_libjpeg_warnings()
    try:
        for cam in cfg["cameras"]:
            cid, source = cam["id"], cam["source"]
            src, is_device = parse_source(source)
            name = f"Camera {cid}"
            if not is_device and isinstance(src, str):
                path = Path(src) if Path(src).is_absolute() else ROOT / src
                report(PASS if path.exists() else FAIL, name, f"video file {src}" + ("" if path.exists() else " not found"))
                continue
            dev = f"/dev/video{src}" if isinstance(src, int) else src
            if LINUX and not os.path.exists(dev):
                report(FAIL, name, f"{dev} not connected: check the USB cable / extension and replug")
                continue
            if LINUX:
                info = usb_info(dev)
                if info:
                    port, product, speed = info
                    slow = speed and float(speed) < 480
                    report(WARN if slow else PASS, f"{name} USB link",
                           f"{product or 'camera'} on USB port {port}, {speed or '?'} Mbit/s" +
                           (" (USB 1.1 speed: use another port or cable)" if slow else ""))
                busy = holders(dev)
                if busy:
                    who = ", ".join(f"{n} (pid {p})" for p, n in busy)
                    report(FAIL, name, f"in use by {who}: close it (a camera opens in one program at a time)")
                    continue
            cap = open_capture(source)
            if not cap.isOpened():
                cap.release()
                report(FAIL, name, f"cannot open {source}")
                continue
            t_open = time.monotonic()
            ok, last = cap.read()  # some webcams take ~3 s for the first frame: fps is measured after it
            first_ms, t0, frames = (time.monotonic() - t_open) * 1000, time.monotonic(), 0
            while ok and time.monotonic() - t0 < seconds:
                ok, data = cap.read()
                if ok:
                    frames, last = frames + 1, data
            cap.release()
            img = to_frame(last) if last is not None else None
            if img is None:
                report(FAIL, name, "opens but delivers no usable frames (USB link too weak? see the kernel USB check)")
                continue
            fps = frames / max(1e-6, time.monotonic() - t0)
            h, w = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            mean, std = float(np.mean(gray)), float(np.std(gray))
            notes = []
            if (w, h) != (WIDTH, HEIGHT):
                notes.append(f"camera gives {w}x{h}, counter resizes to {WIDTH}x{HEIGHT}")
            if fps < MIN_FPS:
                notes.append(f"low fps (target {MIN_FPS:g}+): more light, mains power, shorter cable")
            if mean < 8 or std < 3:
                notes.append("picture is almost black or flat: lens covered, cap on, or far too dark")
            report(WARN if notes else PASS, name,
                   f"{w}x{h}, {fps:.1f} fps, first frame {first_ms:.0f} ms" + (" — " + "; ".join(notes) if notes else ""))
    finally:
        restore()


def check_usb_errors(camera_ports):
    if not LINUX:
        return
    r = run(["journalctl", "-k", "--since", "-10min", "--no-pager", "-o", "cat"])
    if not r or r.returncode != 0:
        report(WARN, "Kernel USB errors", "cannot read the kernel log (journalctl -k): check skipped")
        return
    bad = {}
    for line in r.stdout.splitlines():
        m = re.search(r"\b(\d+-[\d.]+)(?::[\d.]+)?:.*(error -71|: -71\b|over-current|unable to enumerate|"
                      r"device descriptor read|USB disconnect)", line)
        if m:
            bad[m[1]] = bad.get(m[1], 0) + 1
    if not bad:
        report(PASS, "Kernel USB errors", "none in the last 10 minutes")
        return
    for port, n in sorted(bad.items()):
        who = f"camera {camera_ports[port]}" if port in camera_ports else "a device"
        report(WARN, "Kernel USB errors", f"{n} on USB port {port} ({who}) in the last 10 minutes: cable or extension "
                                          "too weak — use a powered active extension, no chaining")


def check_state(cfg):
    state_dir = ROOT / cfg.get("state_dir", "state")
    parent = state_dir if state_dir.exists() else state_dir.parent
    if not os.access(parent, os.W_OK):
        report(FAIL, "Counter state", f"{state_dir} is not writable: counts could not be saved")
        return
    today = datetime.now(IST).date().isoformat()
    files, broken = sorted(state_dir.glob("*.json")) if state_dir.exists() else [], []
    for f in files:
        try:
            json.loads(f.read_text())
        except (OSError, ValueError):
            broken.append(f.name)
    if f"{today}.json" in broken:
        report(FAIL, "Counter state", f"state/{today}.json is corrupt: the counter cannot resume today's counts")
    elif broken:
        report(WARN, "Counter state", f"unreadable older files: {', '.join(broken)}")
    else:
        report(PASS, "Counter state", f"{len(files)} day file(s), writable" + (", today's will resume" if (state_dir / f"{today}.json").exists() else ""))


def check_database():
    db = ROOT / "hub" / "data" / "counts.db"
    folder = db.parent if db.parent.exists() else db.parent.parent
    if not os.access(folder, os.W_OK):
        report(FAIL, "Hub database", f"{folder} is not writable")
        return
    if not db.exists():
        report(WARN, "Hub database", "not created yet: the hub creates hub/data/counts.db at first start")
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        days = con.execute("SELECT COUNT(DISTINCT date) FROM device_state").fetchone()[0] if "device_state" in tables else 0
        groups = con.execute("SELECT COUNT(*) FROM group_entries").fetchone()[0] if "group_entries" in tables else 0
        con.close()
    except sqlite3.Error as e:
        report(FAIL, "Hub database", f"cannot read {db}: {e}")
        return
    if ok != "ok":
        report(FAIL, "Hub database", f"integrity check failed: {ok}")
    else:
        report(PASS, "Hub database", f"integrity ok, {days} day(s) of totals, {groups} group entr{'y' if groups == 1 else 'ies'}")


def check_disk():
    free_mb = shutil.disk_usage(ROOT).free / 2**20
    status = FAIL if free_mb < MIN_FREE_MB else WARN if free_mb < LOW_FREE_MB else PASS
    report(status, "Disk space", f"{free_mb / 1024:.1f} GB free")


def check_network(cfg, hosts_hub, port):
    ips = local_ipv4()
    if ips:
        report(PASS, "LAN address", ", ".join(ips) + (f"  (dashboard: http://{ips[0]}:{port}/)" if hosts_hub else ""))
    else:
        report(WARN if hosts_hub else FAIL, "LAN address", "no network address: " +
               ("phones and the exit PC cannot reach the hub" if hosts_hub else "this PC cannot reach the hub"))
    if hosts_hub:
        return
    url = cfg["hub_url"].rstrip("/")
    try:
        http_get(url + "/healthz")
        state = json.loads(http_get(url + "/api/v1/state")[1])
    except Exception as e:  # noqa: BLE001 - any network error is the same answer for the operator
        report(WARN, "Hub reachable", f"{url} not reachable ({e}): the counter keeps counting and catches up later")
        return
    report(PASS, "Hub reachable", url)
    if cfg["gate"] == "exit" and not state.get("exit_enabled", True):
        report(FAIL, "Exit gate", "disabled on the hub (exit_enabled: false in hub/config.yaml on the entry PC): "
                                  "this PC's counts would be ignored. Set it to true there to count exits.")


def running_counter():
    me = os.getpid()
    return [p for p, cmd in processes().items() if p != me and re.search(r"-m counter\.main\b", cmd)]


def hub_running(port):
    try:
        return http_get(f"http://127.0.0.1:{port}/healthz", timeout=2)[0] == 200
    except Exception:  # noqa: BLE001
        return False


def port_in_use(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def check_clock():
    r = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) if LINUX else None
    if not r or r.returncode != 0:
        return
    synced = r.stdout.strip() == "yes"
    report(PASS if synced else WARN, "Clock", datetime.now(IST).strftime("%Y-%m-%d %H:%M IST") +
           ("" if synced else " (not NTP-synced: days roll over by this PC's clock, check it is right)"))


# ---------------------------------------------------------------- launch

def gui_env():
    """DISPLAY etc. for the live window; over SSH they come from the desktop session's systemd user manager."""
    env = dict(os.environ)
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        r = run(["systemctl", "--user", "show-environment"]) if LINUX else None
        for line in (r.stdout.splitlines() if r and r.returncode == 0 else []):
            key, _, value = line.partition("=")
            if key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR"):
                env.setdefault(key, value)
    return env if (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) else None


def launch(hosts_hub, port, args):
    services = [s for s in (["gatecount-hub"] if hosts_hub else []) + ["gatecount-counter"]
                if (UNITS / f"{s}.service").exists()]
    if LINUX and "gatecount-counter" in services:
        print(f"\nStarting systemd user services: {', '.join(services)}")
        r = run(["systemctl", "--user", "start", *services], timeout=60)
        if not r or r.returncode:
            print("could not start them:", r.stderr.strip() if r else "timeout")
            return 1
        run(["systemctl", "--user", "--no-pager", "--lines=0", "status", *services])
        print("Running. Logs: journalctl --user -u gatecount-counter -f")
        return 0

    env = None if args.headless else gui_env()
    counter_args = ["--debug"] + (["--fullscreen"] if args.fullscreen else []) if env else []
    if not args.headless and not env:
        print("No desktop session found: the live window is off (counting is not affected).")
    procs = []
    try:
        if hosts_hub and not hub_running(port):
            print(f"\nStarting the hub on port {port} ...")
            procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app:create_app", "--factory", "--app-dir",
                                           "hub", "--host", "0.0.0.0", "--port", str(port),
                                           "--timeout-graceful-shutdown", "3", "--no-access-log"], cwd=ROOT))
            deadline = time.monotonic() + 30
            while not hub_running(port):
                if procs[0].poll() is not None or time.monotonic() > deadline:
                    print("the hub did not start: see its output above")
                    return 1
                time.sleep(0.5)
        print("Starting the counter ... (Ctrl-C stops everything)")
        counter = subprocess.Popen([sys.executable, "-m", "counter.main", *counter_args], cwd=ROOT, env=env or os.environ)
        procs.append(counter)
        return counter.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        for p in reversed(procs):  # counter first: it sends its final totals to the hub before the hub stops
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    p.kill()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-only", action="store_true", help="run the checks, do not launch")
    ap.add_argument("--headless", action="store_true", help="launch the counter without the live window")
    ap.add_argument("--fullscreen", action="store_true", help="launch the live window full screen")
    ap.add_argument("--seconds", type=float, default=4.0, help="how long to measure each camera (default 4)")
    args = ap.parse_args()
    print(f"GateCount pre-launch checklist — {socket.gethostname()}, {datetime.now(IST):%Y-%m-%d %H:%M} IST")

    section("Software")
    check_python()
    check_packages()

    section("Configuration")
    cfg = check_counter_config()
    hosts_hub = bool(cfg) and hub_is_local(cfg)
    port = (urllib.parse.urlparse(cfg["hub_url"]).port or 8000) if hosts_hub else 8000
    hub_cfg = check_hub_config() if hosts_hub else None
    if cfg:
        check_tokens(cfg, hosts_hub, hub_cfg)
        check_model(cfg)

    section("Already running")
    counters = running_counter()
    if counters:
        report(FAIL, "Counter", f"already running (pid {', '.join(map(str, counters))}): it holds the cameras. "
                                "Stop it first: systemctl --user stop gatecount-counter (or pkill -f '[c]ounter[.]main')")
    else:
        report(PASS, "Counter", "not running yet")
    if hosts_hub:
        if hub_running(port):
            report(WARN, "Hub", f"already running on port {port}: it is kept, not started again")
        elif port_in_use(port):
            report(FAIL, "Hub", f"port {port} is used by another program")
        else:
            report(PASS, "Hub", f"port {port} free")

    section("Cameras and USB")
    if cfg and not counters:
        check_cameras(cfg, args.seconds)
    elif cfg:
        report(WARN, "Cameras", "not tested: the running counter holds them")
    check_usb_errors(camera_ports(cfg) if cfg else {})

    section("Data, disk, network, clock")
    if cfg:
        check_state(cfg)
    if hosts_hub:
        check_database()
    check_disk()
    if cfg:
        check_network(cfg, hosts_hub, port)
    check_clock()

    fails, warns = results.count(FAIL), results.count(WARN)
    print(f"\nResult: {results.count(PASS)} PASS, {warns} WARN, {fails} FAIL")
    if fails:
        print("NOT READY: fix every FAIL above, then run this again. Nothing was started.")
        return 1
    print("READY" + (" (check the WARN lines)" if warns else "") + ".")
    if args.check_only:
        return 0
    return launch(hosts_hub, port, args)


if __name__ == "__main__":
    sys.exit(main())
