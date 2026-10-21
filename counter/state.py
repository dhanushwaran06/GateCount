"""Daily totals kept in memory, saved atomically to state/<date>.json by a writer thread."""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata has no DST


def today():
    return datetime.now(IST).date().isoformat()


def iso(ts):
    """Epoch seconds -> ISO 8601 in IST (None stays None)."""
    return datetime.fromtimestamp(ts, IST).isoformat(timespec="seconds") if ts else None


class Totals:
    """Thread-safe cumulative counts for today. A new IST date starts again at 0.

    `version` increases on every change; the sender and writer use it to know when there is news.
    """

    def __init__(self, camera_ids, state_dir):
        self.camera_ids = list(camera_ids)
        self.state_dir = Path(state_dir)
        self.changed = threading.Condition()
        self.version = 0
        self._seq = 0
        self._load(today())

    def _path(self, date):
        return self.state_dir / f"{date}.json"

    def _load(self, date):
        self.date = date
        self.cams = {c: {"count": 0, "wrong_way": 0} for c in self.camera_ids}
        path = self._path(date)
        if path.exists():
            saved = json.loads(path.read_text())
            for cam, vals in saved.get("cameras", {}).items():
                if cam in self.cams:
                    self.cams[cam].update({k: int(vals.get(k, 0)) for k in ("count", "wrong_way")})
            self._seq = int(saved.get("seq", 0))
            log.info("resumed %s: %s", path, self.cams)
        else:
            log.info("new day %s: starting at 0", date)

    def _rollover(self):
        """Caller holds the lock. Switch to a new date at IST midnight."""
        if today() != self.date:
            self._load(today())
            self.version += 1
            self.changed.notify_all()

    def add(self, cam, kind):
        """kind is "count" or "wrong_way"."""
        with self.changed:
            self._rollover()
            self.cams[cam][kind] += 1
            self.version += 1
            self.changed.notify_all()

    def snapshot(self):
        """(version, date, {cam: {count, wrong_way}}) — a consistent copy."""
        with self.changed:
            self._rollover()
            return self.version, self.date, {c: dict(v) for c, v in self.cams.items()}

    def next_seq(self):
        """Strictly increasing, also across restarts and state resets: max(previous + 1, epoch ms)."""
        with self.changed:
            self._seq = max(self._seq + 1, int(time.time() * 1000))
            return self._seq

    def save(self):
        """Atomic write: temp file + fsync + os.replace, so a crash never leaves a half-written file."""
        with self.changed:
            data = {"date": self.date, "seq": self._seq, "cameras": {c: dict(v) for c, v in self.cams.items()}}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(data["date"])
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


class StateWriter(threading.Thread):
    """Saves totals every `interval` s when they changed, so disk I/O never blocks counting."""

    def __init__(self, totals, stop, interval=2.0):
        super().__init__(daemon=True, name="state-writer")
        self.totals, self.stop_evt, self.interval = totals, stop, interval

    def run(self):
        saved_version = -1
        while True:
            stopping = self.stop_evt.wait(self.interval)
            version = self.totals.version
            if version != saved_version or stopping:
                try:
                    self.totals.save()
                    saved_version = version
                except OSError:
                    log.exception("state save failed")
            if stopping:
                return
