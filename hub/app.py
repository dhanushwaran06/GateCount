"""Hub: receives cumulative counts from the gate PCs, keeps the live state in memory, writes one SQLite row
per minute, and serves the dashboard plus a live SSE stream. One process, one file.

Run:  uvicorn app:create_app --factory --app-dir hub --host 0.0.0.0 --port 8000    (or: docker compose up)

Health is judged by the hub's own clock (receive time), so PCs with drifting clocks still show correctly.
"""

import asyncio
import csv
import hmac
import io
import ipaddress
import json
import logging
import math
import os
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

import yaml
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints, field_validator

log = logging.getLogger("hub")

IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata has no DST
HERE = Path(__file__).resolve().parent
STALE_CAMERA_S, OFFLINE_DEVICE_S, HEARTBEAT_S = 15, 30, 10
# Staff PIN for the group form, limited PER DEVICE (client address; IPv6 per /64): every check queues (one wrong
# guess per second), at most MAX_PIN_WAITING attempts may queue, and MAX_PIN_FAILS wrong PINs in a row lock that
# device out for PIN_LOCK_S. A guesser can only lock out themselves, never the staff's phones.
# Hub-wide ceiling: after GLOBAL_MAX_FAILS wrong PINs within PIN_LOCK_S, devices that have not entered the right
# PIN in the last TRUST_S are turned away unchecked; staff phones that already logged in keep working.
# Behind a reverse proxy all clients share the proxy's address: enable uvicorn --proxy-headers there.
MIN_PIN, MAX_PIN_WAITING, MAX_PIN_FAILS, PIN_LOCK_S = 8, 3, 10, 600
GLOBAL_MAX_FAILS, TRUST_S, MAX_PIN_CLIENTS = 100, 12 * 3600, 1000
WRONG_PIN_DELAY_S = 1.0
DATE = r"^\d{4}-\d{2}-\d{2}$"
# exit_enabled: false keeps the exit PC's code but switches it off: its messages are refused, OUT is 0.
# fair_start: first day of the fair; the cumulative total leaves out earlier days (office tests). None = all days.
# display_hold_s: the big-screen display holds its number this long on bulk or rapid updates, then flips.
DEFAULTS = {"entry_factor": 1.0, "exit_factor": 1.0, "open_hours": "09:00-21:00",
            "devices": {"pc-a": "entry", "pc-b": "exit"}, "exit_enabled": False, "fair_start": None,
            "display_hold_s": 10}


def today():
    return datetime.now(IST).date().isoformat()


def epoch(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


def gate_enabled(cfg, gate):
    return gate != "exit" or bool(cfg["exit_enabled"])


def calibrate(raw, factor):
    """round(raw x factor), half-up (Python's round() is banker's rounding)."""
    return math.floor(raw * factor + 0.5)


def aware_iso(value):
    """Reject timestamps that do not parse or have no timezone: they are stored and parsed on every state()."""
    if value is not None and datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("timestamp needs a timezone offset, e.g. +05:30")
    return value


class CameraIn(BaseModel):
    count: int = Field(ge=0, le=10_000_000)
    wrong_way: int = Field(0, ge=0, le=10_000_000)
    fps: float = Field(0.0, ge=0, le=1000, allow_inf_nan=False)  # NaN would break the dashboard's JSON
    last_frame_at: str | None = None
    _check_ts = field_validator("last_frame_at")(aware_iso)


class CountsIn(BaseModel):
    device: str = Field(max_length=32)
    gate: Literal["entry", "exit"]
    date: str = Field(pattern=DATE)
    seq: int = Field(ge=0, le=2**62)
    sent_at: str
    cameras: dict[Annotated[str, StringConstraints(max_length=32)], CameraIn] = Field(max_length=16)
    _check_ts = field_validator("sent_at")(aware_iso)


class GroupIn(BaseModel):
    """A group (e.g. a school) that entered WITHOUT walking through the entry cameras; added to IN as-is."""
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    people: int = Field(ge=1, le=1000)


class Hub:
    """Latest message per (device, date), persisted so a hub restart shows the last totals immediately."""

    def __init__(self, cfg, tokens, db_path):
        self.cfg, self.tokens = cfg, tokens
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS minutes (date TEXT, minute TEXT, in_shown INTEGER, out_shown INTEGER,
                                                inside INTEGER, PRIMARY KEY (date, minute));
            CREATE TABLE IF NOT EXISTS device_state (device TEXT, date TEXT, record TEXT,
                                                     PRIMARY KEY (device, date));
            CREATE TABLE IF NOT EXISTS group_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, at TEXT,
                                                      name TEXT, people INTEGER);
        """)
        self.latest = {(d, dt): json.loads(r) for d, dt, r in self.db.execute("SELECT * FROM device_state")}
        self.version = 0
        self.changed = asyncio.Condition()

    def accept(self, msg: CountsIn, received_at: float) -> bool:
        """Store a message unless an equal or newer seq was already seen for this device + date."""
        key = (msg.device, msg.date)
        if key in self.latest and msg.seq <= self.latest[key]["seq"]:
            return False
        rec = {"seq": msg.seq, "gate": msg.gate, "sent_at": msg.sent_at, "received_at": received_at,
               "cameras": {cid: cam.model_dump() for cid, cam in msg.cameras.items()}}
        self.latest[key] = rec
        self.db.execute("INSERT OR REPLACE INTO device_state VALUES (?, ?, ?)", (*key, json.dumps(rec)))
        self.db.commit()
        return True

    def totals(self, date):
        """(in_shown, out_shown, inside, raw) for a date. Group entries are exact head counts: not calibrated."""
        raw = {"entry": 0, "exit": 0, "groups": self.group_total(date)}
        for (_, d), rec in self.latest.items():
            if d == date and gate_enabled(self.cfg, rec["gate"]):
                raw[rec["gate"]] += sum(c["count"] for c in rec["cameras"].values())
        in_shown = calibrate(raw["entry"], self.cfg["entry_factor"]) + raw["groups"]
        out_shown = calibrate(raw["exit"], self.cfg["exit_factor"])
        return in_shown, out_shown, max(0, in_shown - out_shown), raw

    def fair_total(self):
        """Visitors since the fair started: entry crossings of every day (calibrated) + every group entry.
        It never resets at midnight; it is rebuilt from saved data after any restart."""
        start = self.cfg["fair_start"] or ""
        raw = sum(sum(c["count"] for c in rec["cameras"].values())
                  for (_, d), rec in self.latest.items() if rec["gate"] == "entry" and d >= start)
        groups = self.db.execute("SELECT COALESCE(SUM(people), 0) FROM group_entries WHERE date >= ?",
                                 (start,)).fetchone()[0]
        return calibrate(raw, self.cfg["entry_factor"]) + groups

    def group_total(self, date):
        return self.db.execute("SELECT COALESCE(SUM(people), 0) FROM group_entries WHERE date = ?", (date,)).fetchone()[0]

    def groups(self, date):
        rows = self.db.execute("SELECT id, at, name, people FROM group_entries WHERE date = ? ORDER BY id", (date,))
        return [{"id": i, "at": at, "name": name, "people": n} for i, at, name, n in rows]

    def add_group(self, name, people):
        cur = self.db.execute("INSERT INTO group_entries (date, at, name, people) VALUES (?, ?, ?, ?)",
                              (today(), datetime.now(IST).strftime("%H:%M"), name, people))
        self.db.commit()
        return cur.lastrowid

    def delete_group(self, gid):
        """Undo a mistaken entry; only today's entries can be removed."""
        n = self.db.execute("DELETE FROM group_entries WHERE id = ? AND date = ?", (gid, today())).rowcount
        self.db.commit()
        return n > 0

    def state(self, now=None):
        now = time.time() if now is None else now
        date = today()
        in_shown, out_shown, inside, raw = self.totals(date)
        devices = []
        for device, gate in self.cfg["devices"].items():
            if not gate_enabled(self.cfg, gate):
                continue
            counts = self.latest.get((device, date), {}).get("cameras", {})
            # health comes from the newest message of any date (a PC whose clock is off still shows online)
            recs = [r for (d, _), r in self.latest.items() if d == device]
            last = max(recs, key=lambda r: r["received_at"]) if recs else None
            age = now - last["received_at"] if last else None
            cameras = []
            for cid, cam in (last or {}).get("cameras", {}).items():
                frame_age = None
                try:  # age at send time (PC clock) + time since we received it (hub clock)
                    frame_age = max(0.0, epoch(last["sent_at"]) - epoch(cam["last_frame_at"])) + (now - last["received_at"])
                except (TypeError, ValueError):  # missing/odd timestamp: show the camera as stale, keep serving
                    pass
                cameras.append({"id": cid, "count": counts.get(cid, {}).get("count", 0),
                                "wrong_way": counts.get(cid, {}).get("wrong_way", 0), "fps": cam["fps"],
                                "frame_age_s": None if frame_age is None else round(frame_age, 1),
                                "stale": frame_age is None or frame_age > STALE_CAMERA_S})
            devices.append({"device": device, "gate": gate, "online": age is not None and age <= OFFLINE_DEVICE_S,
                            "last_seen_s": None if age is None else round(age, 1), "cameras": cameras})
        return {"date": date, "total": self.fair_total(), "fair_start": self.cfg["fair_start"],
                "in": in_shown, "out": out_shown, "inside": inside,
                "in_raw": raw["entry"], "in_groups": raw["groups"], "out_raw": raw["exit"],
                "entry_factor": self.cfg["entry_factor"],
                "exit_factor": self.cfg["exit_factor"], "open_hours": self.cfg["open_hours"],
                "exit_enabled": bool(self.cfg["exit_enabled"]), "display_hold_s": self.cfg["display_hold_s"],
                "stale_after_s": STALE_CAMERA_S, "offline_after_s": OFFLINE_DEVICE_S, "devices": devices}

    def write_minute(self, at: datetime) -> bool:
        """Write the row for the minute containing `at` (IST). Skipped until a counter has reported that day."""
        date = at.date().isoformat()
        if not any(d == date for _, d in self.latest) and not self.group_total(date):
            return False
        self.db.execute("INSERT OR REPLACE INTO minutes VALUES (?, ?, ?, ?, ?)",
                        (date, at.strftime("%H:%M"), *self.totals(date)[:3]))
        self.db.commit()
        return True

    def rows(self, date):
        return self.db.execute("SELECT minute, in_shown, out_shown, inside FROM minutes WHERE date = ? "
                               "ORDER BY minute", (date,)).fetchall()

    async def notify(self):
        async with self.changed:
            self.version += 1
            self.changed.notify_all()


def load_env(path):
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(yaml.safe_load(path.read_text()) or {})
    else:
        log.warning("%s not found, using defaults", path)
    return check_config(cfg)


def check_config(cfg):
    """Normalise and validate the settings added for the fair, so a typo stops the hub at start, not mid-day."""
    if cfg["fair_start"]:  # YAML reads an unquoted 2026-10-22 as a date: keep it as an ISO string
        cfg["fair_start"] = str(cfg["fair_start"])
        datetime.strptime(cfg["fair_start"], "%Y-%m-%d")
    hold = cfg["display_hold_s"]
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0 <= hold <= 60:
        raise SystemExit(f"display_hold_s must be a number of seconds from 0 to 60, got {hold!r}")
    return cfg


def device_tokens(devices, optional=()):
    """Token per device from env: pc-a -> TOKEN_PC_A. No warning for `optional` (switched-off) devices."""
    tokens = {d: os.environ.get("TOKEN_" + d.upper().replace("-", "_"), "") for d in devices}
    for d, t in tokens.items():
        if not t and d not in optional:
            log.warning("no token for %s (set TOKEN_%s): its messages will be rejected", d, d.upper().replace("-", "_"))
    return tokens


async def minute_writer(hub):
    """At each IST minute boundary, record the minute that just ended."""
    while True:
        now = datetime.now(IST)
        await asyncio.sleep(60 - now.second - now.microsecond / 1e6 + 0.05)
        try:
            hub.write_minute(datetime.now(IST) - timedelta(seconds=30))
        except Exception:  # never let one bad minute stop the trend for the rest of the day
            log.exception("minute row write failed")


def client_key(request):
    """Who is guessing: the client address, with IPv6 grouped per /64 so address rotation does not help."""
    host = request.client.host if request.client else "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else host


def same_secret(given, expected):
    """Constant-time compare; bytes so a non-ASCII header cannot raise."""
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def create_app(cfg=None, tokens=None, db_path=None, staff_pin=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env(HERE / ".env")
    cfg = check_config(dict(DEFAULTS, **cfg)) if cfg else load_config(Path(os.environ.get("HUB_CONFIG", HERE / "config.yaml")))
    tokens = tokens if tokens is not None else device_tokens(
        cfg["devices"], optional=[d for d, gate in cfg["devices"].items() if not gate_enabled(cfg, gate)])
    hub = Hub(cfg, tokens, db_path or os.environ.get("HUB_DB", HERE / "data" / "counts.db"))
    staff_pin = staff_pin if staff_pin is not None else os.environ.get("STAFF_PIN", "")
    if 0 < len(staff_pin) < MIN_PIN:
        log.warning("STAFF_PIN is shorter than %d characters: the group form stays switched off", MIN_PIN)
        staff_pin = ""
    pin_clients = {}  # client key -> {"lock", "waiting", "fails", "locked_until", "seen", "ok_at"}
    recent_fails = deque(maxlen=GLOBAL_MAX_FAILS)  # monotonic times of the latest wrong PINs, hub-wide

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(minute_writer(hub))
        log.info("hub ready: devices %s, factors entry %.3f exit %.3f, exit gate %s, fair total since %s",
                 cfg["devices"], cfg["entry_factor"], cfg["exit_factor"],
                 "enabled" if cfg["exit_enabled"] else "DISABLED", cfg["fair_start"] or "the first saved day")
        yield
        task.cancel()

    app = FastAPI(title="Book fair people counter", lifespan=lifespan)
    app.state.hub = hub

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc):
        # name the bad field but do not echo the input: a NaN in it would make the error reply itself fail
        return JSONResponse({"detail": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]}, status_code=422)

    @app.post("/api/v1/counts")
    async def post_counts(msg: CountsIn, authorization: str = Header("")):
        token = hub.tokens.get(msg.device, "")
        if not same_secret(authorization.removeprefix("Bearer ").strip(), token):
            raise HTTPException(401, "bad or missing device token")
        if msg.gate != cfg["devices"][msg.device]:
            raise HTTPException(409, f"{msg.device} is configured for gate '{cfg['devices'][msg.device]}'")
        if not gate_enabled(cfg, msg.gate):
            raise HTTPException(403, "the exit gate is disabled on the hub (exit_enabled: false in hub/config.yaml)")
        accepted = hub.accept(msg, time.time())
        if accepted:
            await hub.notify()
        return {"accepted": accepted}

    @app.get("/api/v1/state")
    async def get_state():
        return hub.state()

    @app.get("/api/v1/stream")
    async def stream(request: Request):
        async def events():
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                seen = hub.version
                yield f"data: {json.dumps(hub.state())}\n\n"
                async with hub.changed:  # wake on a change, or after HEARTBEAT_S to refresh "last seen"
                    try:
                        await asyncio.wait_for(hub.changed.wait_for(lambda: hub.version != seen), HEARTBEAT_S)
                    except TimeoutError:
                        pass
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/history")
    async def history(date: str | None = Query(None, pattern=DATE)):
        date = date or today()
        return {"date": date, "rows": [{"minute": m, "in": i, "out": o, "inside": x} for m, i, o, x in hub.rows(date)]}

    @app.get("/api/v1/export.csv")
    async def export_csv(date: str | None = Query(None, pattern=DATE)):
        date = date or today()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["date", "minute", "in_shown", "out_shown", "inside"])
        writer.writerows((date, *row) for row in hub.rows(date))
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="counts-{date}.csv"'})

    async def require_staff(request, authorization):
        if not staff_pin:
            raise HTTPException(403, f"group entries are switched off: set an {MIN_PIN}+ character STAFF_PIN in hub/.env")
        now = time.monotonic()
        key = client_key(request)
        if key not in pin_clients and len(pin_clients) >= MAX_PIN_CLIENTS:
            # forget only devices idle for a whole lock period, so their wrong-PIN counts cannot be reset early
            for k in [k for k, c in pin_clients.items() if not c["waiting"] and now - c["seen"] > PIN_LOCK_S]:
                del pin_clients[k]
            if len(pin_clients) >= MAX_PIN_CLIENTS:
                raise HTTPException(429, "too many devices are trying the PIN: try again in a few minutes")
        pin = pin_clients.setdefault(key, {"lock": asyncio.Lock(), "waiting": 0, "fails": 0, "locked_until": 0.0,
                                           "seen": now, "ok_at": None})
        pin["seen"] = now
        trusted = pin["ok_at"] is not None and now - pin["ok_at"] < TRUST_S
        if not trusted and len(recent_fails) == GLOBAL_MAX_FAILS and now - recent_fails[0] < PIN_LOCK_S:
            raise HTTPException(429, "too many wrong PINs on the network: new devices are paused for a few minutes")

        def locked():
            left = pin["locked_until"] - time.monotonic()
            if left > 0:
                raise HTTPException(429, f"too many wrong PINs from this device: locked for {max(1, round(left / 60))} more minutes")

        locked()
        if pin["waiting"] >= MAX_PIN_WAITING:  # do not even check: a queue of guesses is an attack, not staff
            raise HTTPException(429, "too many attempts at once, try again in a few seconds")
        pin["waiting"] += 1
        try:
            async with pin["lock"]:  # the throttle comes BEFORE the check, so parallel guesses cannot skip it
                locked()
                if same_secret(authorization.removeprefix("Bearer ").strip(), staff_pin):
                    pin["fails"], pin["ok_at"] = 0, time.monotonic()
                    return
                pin["fails"] += 1
                recent_fails.append(time.monotonic())
                if len(recent_fails) == GLOBAL_MAX_FAILS and recent_fails[-1] - recent_fails[0] < PIN_LOCK_S:
                    log.error("%d wrong staff PINs hub-wide in %d minutes: new devices paused",
                              GLOBAL_MAX_FAILS, PIN_LOCK_S // 60)
                log.warning("wrong staff PIN from %s (%d in a row)", key, pin["fails"])
                if pin["fails"] >= MAX_PIN_FAILS:
                    pin["fails"], pin["locked_until"] = 0, time.monotonic() + PIN_LOCK_S
                    log.error("%d wrong staff PINs in a row from %s: that device is locked out for %d minutes",
                              MAX_PIN_FAILS, key, PIN_LOCK_S // 60)
                await asyncio.sleep(WRONG_PIN_DELAY_S)
        finally:
            pin["waiting"] -= 1
        raise HTTPException(401, "wrong staff PIN")

    @app.get("/api/v1/groups")
    async def list_groups(date: str | None = Query(None, pattern=DATE)):
        date = date or today()
        groups = hub.groups(date)
        return {"date": date, "total": sum(g["people"] for g in groups), "groups": groups}

    @app.post("/api/v1/groups")
    async def add_group(group: GroupIn, request: Request, authorization: str = Header("")):
        await require_staff(request, authorization)
        gid = hub.add_group(group.name, group.people)
        await hub.notify()
        return {"id": gid}

    @app.delete("/api/v1/groups/{gid}")
    async def delete_group(gid: int, request: Request, authorization: str = Header("")):
        await require_staff(request, authorization)
        if not hub.delete_group(gid):
            raise HTTPException(404, "no such group entry today")
        await hub.notify()
        return {"deleted": gid}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def dashboard():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/display")
    async def display():
        return FileResponse(HERE / "static" / "display.html")

    @app.get("/groups")
    async def groups_page():
        return FileResponse(HERE / "static" / "groups.html")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")  # fonts
    return app
