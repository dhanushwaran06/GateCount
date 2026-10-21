"""Fake counters for testing the hub: post cumulative totals like real PCs (change -> post, 10 s heartbeat).

Usage:
  python tools/simulate.py --hub http://127.0.0.1:8000 --device pc-a --gate entry --token tok-a
  python tools/simulate.py --hub http://127.0.0.1:8000 --device pc-b --gate exit  --token tok-b --rate 0.6

Stop one with Ctrl-C: the dashboard should show that device offline within 30 s.
"""

import argparse
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

IST = timezone(timedelta(hours=5, minutes=30))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", default="http://127.0.0.1:8000")
    ap.add_argument("--device", required=True)
    ap.add_argument("--gate", required=True, choices=["entry", "exit"])
    ap.add_argument("--token", required=True)
    ap.add_argument("--cameras", default="c1,c2")
    ap.add_argument("--rate", type=float, default=1.0, help="mean people per second across the gate")
    args = ap.parse_args()

    cams = {c: 0 for c in args.cameras.split(",")}
    client = httpx.Client(timeout=3, headers={"Authorization": f"Bearer {args.token}"})
    last_sent = 0.0
    while True:
        changed = False
        for c in cams:
            if random.random() < args.rate / len(cams):
                cams[c] += 1
                changed = True
        if changed or time.time() - last_sent >= 10:
            now = datetime.now(IST).isoformat(timespec="seconds")
            body = {"device": args.device, "gate": args.gate, "date": datetime.now(IST).date().isoformat(),
                    "seq": int(time.time() * 1000), "sent_at": now,
                    "cameras": {c: {"count": n, "wrong_way": 0, "fps": 9.8, "last_frame_at": now} for c, n in cams.items()}}
            try:
                r = client.post(args.hub.rstrip("/") + "/api/v1/counts", json=body)
                print(f"{now} {args.device} {sum(cams.values())} -> {r.status_code} {r.text}")
            except httpx.HTTPError as e:
                print(f"{now} {args.device} post failed: {e}")
            last_sent = time.time()
        time.sleep(1)


if __name__ == "__main__":
    main()
