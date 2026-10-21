"""Hub: receives cumulative counts from the gate PCs, keeps the live state in memory, writes one SQLite row
per minute, and serves the dashboard plus a live SSE stream. One process, one file.

Run:  uvicorn app:create_app --factory --app-dir hub --host 0.0.0.0 --port 8000    (or: docker compose up)

Health is judged by the hub's own clock (receive time), so PCs with drifting clocks still show correctly.
"""

import asyncio
import csv
import hmac
import io
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import yaml
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

log = logging.getLogger("hub")

IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata has no DST
HERE = Path(__file__).resolve().parent
STALE_CAMERA_S, OFFLINE_DEVICE_S, HEARTBEAT_S = 15, 30, 10
DATE = r"^\d{4}-\d{2}-\d{2}$"
DEFAULTS = {"entry_factor": 1.0, "exit_factor": 1.0, "open_hours": "09:00-21:00",
            "devices": {"pc-a": "entry", "pc-b": "exit"}}


def today():
    return datetime.now(IST).date().isoformat()


def epoch(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


def calibrate(raw, factor):
    """round(raw x factor), half-up (Python's round() is banker's rounding)."""
    return math.floor(raw * factor + 0.5)


class CameraIn(BaseModel):
    count: int = Field(ge=0)
    wrong_way: int = Field(0, ge=0)
    fps: float = 0.0
    last_frame_at: str | None = None


class CountsIn(BaseModel):
    device: str
    gate: Literal["entry", "exit"]
    date: str = Field(pattern=DATE)
    seq: int
    sent_at: str
    cameras: dict[str, CameraIn]


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
        """(in_shown, out_shown, inside, raw) for a date."""
        raw = {"entry": 0, "exit": 0}
        for (_, d), rec in self.latest.items():
            if d == date:
                raw[rec["gate"]] += sum(c["count"] for c in rec["cameras"].values())
        in_shown = calibrate(raw["entry"], self.cfg["entry_factor"])
        out_shown = calibrate(raw["exit"], self.cfg["exit_factor"])
        return in_shown, out_shown, max(0, in_shown - out_shown), raw

    def state(self, now=None):
        now = time.time() if now is None else now
        date = today()
        in_shown, out_shown, inside, raw = self.totals(date)
        devices = []
        for device, gate in self.cfg["devices"].items():
            counts = self.latest.get((device, date), {}).get("cameras", {})
            # health comes from the newest message of any date (a PC whose clock is off still shows online)
            recs = [r for (d, _), r in self.latest.items() if d == device]
            last = max(recs, key=lambda r: r["received_at"]) if recs else None
            age = now - last["received_at"] if last else None
            cameras = []
            for cid, cam in (last or {}).get("cameras", {}).items():
                frame_age = None
                if cam["last_frame_at"]:  # age at send time (PC clock) + time since we received it (hub clock)
                    frame_age = max(0.0, epoch(last["sent_at"]) - epoch(cam["last_frame_at"])) + (now - last["received_at"])
                cameras.append({"id": cid, "count": counts.get(cid, {}).get("count", 0),
                                "wrong_way": counts.get(cid, {}).get("wrong_way", 0), "fps": cam["fps"],
                                "frame_age_s": None if frame_age is None else round(frame_age, 1),
                                "stale": frame_age is None or frame_age > STALE_CAMERA_S})
            devices.append({"device": device, "gate": gate, "online": age is not None and age <= OFFLINE_DEVICE_S,
                            "last_seen_s": None if age is None else round(age, 1), "cameras": cameras})
        return {"date": date, "in": in_shown, "out": out_shown, "inside": inside,
                "in_raw": raw["entry"], "out_raw": raw["exit"], "entry_factor": self.cfg["entry_factor"],
                "exit_factor": self.cfg["exit_factor"], "open_hours": self.cfg["open_hours"],
                "stale_after_s": STALE_CAMERA_S, "offline_after_s": OFFLINE_DEVICE_S, "devices": devices}

    def write_minute(self, at: datetime) -> bool:
        """Write the row for the minute containing `at` (IST). Skipped until a counter has reported that day."""
        date = at.date().isoformat()
        if not any(d == date for _, d in self.latest):
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
    return cfg


def device_tokens(devices):
    """Token per device from env: pc-a -> TOKEN_PC_A."""
    tokens = {d: os.environ.get("TOKEN_" + d.upper().replace("-", "_"), "") for d in devices}
    for d, t in tokens.items():
        if not t:
            log.warning("no token for %s (set TOKEN_%s): its messages will be rejected", d, d.upper().replace("-", "_"))
    return tokens


async def minute_writer(hub):
    """At each IST minute boundary, record the minute that just ended."""
    while True:
        now = datetime.now(IST)
        await asyncio.sleep(60 - now.second - now.microsecond / 1e6 + 0.05)
        try:
            hub.write_minute(datetime.now(IST) - timedelta(seconds=30))
        except sqlite3.Error:
            log.exception("minute row write failed")


def create_app(cfg=None, tokens=None, db_path=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env(HERE / ".env")
    cfg = cfg or load_config(Path(os.environ.get("HUB_CONFIG", HERE / "config.yaml")))
    tokens = tokens if tokens is not None else device_tokens(cfg["devices"])
    hub = Hub(cfg, tokens, db_path or os.environ.get("HUB_DB", HERE / "data" / "counts.db"))

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(minute_writer(hub))
        log.info("hub ready: devices %s, factors entry %.3f exit %.3f",
                 cfg["devices"], cfg["entry_factor"], cfg["exit_factor"])
        yield
        task.cancel()

    app = FastAPI(title="Book fair people counter", lifespan=lifespan)
    app.state.hub = hub

    @app.post("/api/v1/counts")
    async def post_counts(msg: CountsIn, authorization: str = Header("")):
        token = hub.tokens.get(msg.device, "")
        if not token or not hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), token):
            raise HTTPException(401, "bad or missing device token")
        if msg.gate != cfg["devices"][msg.device]:
            raise HTTPException(409, f"{msg.device} is configured for gate '{cfg['devices'][msg.device]}'")
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

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def dashboard():
        return FileResponse(HERE / "static" / "index.html")

    return app
