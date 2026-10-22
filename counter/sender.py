"""Posts cumulative daily totals to the hub: within 1 s of a change (max 1/s) and a heartbeat every 10 s.

No retry queue: every message is cumulative, so the next successful one repairs any lost message.
"""

import logging
import threading
import time

import httpx

from counter.state import iso

log = logging.getLogger(__name__)


class Sender(threading.Thread):
    def __init__(self, cfg, totals, cameras, stop, min_interval=1.0, heartbeat=10.0, timeout=3.0):
        """cameras: {cam_id: object with .fps and .last_frame_at (epoch s)}."""
        super().__init__(daemon=True, name="sender")
        self.cfg, self.totals, self.cameras, self.stop_evt = cfg, totals, cameras, stop
        self.min_interval, self.heartbeat = min_interval, heartbeat
        self.url = cfg["hub_url"].rstrip("/") + "/api/v1/counts"
        self.client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {cfg['token']}"})
        self.ok = None  # last outcome, to log only on transitions

    def message(self):
        version, date, counts = self.totals.snapshot()
        cams = {
            cid: {**counts[cid], "fps": round(cam.fps, 1), "last_frame_at": iso(cam.last_frame_at)}
            for cid, cam in self.cameras.items()
        }
        return version, {
            "device": self.cfg["device"], "gate": self.cfg["gate"], "date": date,
            "seq": self.totals.next_seq(), "sent_at": iso(time.time()), "cameras": cams,
        }

    def send(self):
        """Returns the totals version that was delivered, or None on failure."""
        version, msg = self.message()
        try:
            r = self.client.post(self.url, json=msg)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:  # reachable, but refused: say why (bad token, gate disabled, ...)
            if self.ok is not False:
                log.warning("hub refused the message (%d): %s — will keep retrying; counts are kept here",
                            e.response.status_code, e.response.text[:200])
            self.ok = False
            return None
        except httpx.HTTPError as e:
            if self.ok is not False:
                log.warning("hub unreachable (%s): %s — will keep retrying", self.url, e)
            self.ok = False
            return None
        if self.ok is not True:
            log.info("hub OK (%s), seq %d, %s", self.url, msg["seq"], {c: v["count"] for c, v in msg["cameras"].items()})
        self.ok = True
        return version

    def run(self):
        sent_version, last_try = -1, 0.0
        while not self.stop_evt.is_set():
            with self.totals.changed:  # wake on a change or at the next heartbeat
                self.totals.changed.wait_for(
                    lambda: self.totals.version != sent_version or self.stop_evt.is_set(),
                    timeout=max(0.0, last_try + self.heartbeat - time.monotonic()))
            if self.stop_evt.is_set():
                break
            wait = last_try + self.min_interval - time.monotonic()  # at most 1 message/s
            if wait > 0 and self.stop_evt.wait(wait):
                break
            last_try = time.monotonic()
            delivered = self.send()
            if delivered is not None:
                sent_version = delivered
        self.send()  # final totals on shutdown (best effort)
        self.client.close()
