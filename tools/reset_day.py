"""Reset a day's counts to 0 on THIS PC (after morning test walks). Run it on every PC.

  python tools/reset_day.py              # today (IST)
  python tools/reset_day.py 2026-10-22   # a given date

Stops the GateCount services if they run, removes the counter's state/<date>.json (if this PC counts) and the
hub's rows for that date (if this PC hosts the hub), then starts the services again. The order between PCs
does not matter: counters send cumulative totals with an ever-increasing seq, so the newest total always wins.

The public "Total Visitors" adds up every day of the fair, so this also takes that day's camera counts out of
it. Run it only BEFORE the doors open (then the day holds test walks only); earlier days are never touched.
School-group entries are kept.
"""

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
SERVICES = ["gatecount-counter", "gatecount-hub"]


def user_services(action):
    subprocess.run(["systemctl", "--user", action, *SERVICES], capture_output=True)


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.now(IST).date().isoformat()
    datetime.strptime(date, "%Y-%m-%d")  # validate
    running = [s for s in SERVICES if subprocess.run(["systemctl", "--user", "is-active", "--quiet", s]).returncode == 0]
    if running:
        print(f"stopping {', '.join(running)}")
        user_services("stop")

    state = ROOT / "state" / f"{date}.json"
    if state.exists():
        state.unlink()
        print(f"counter: removed {state}")
    else:
        print(f"counter: no state for {date} on this PC")

    db = ROOT / "hub" / "data" / "counts.db"
    if db.exists():
        con = sqlite3.connect(db)
        rows = con.execute("DELETE FROM minutes WHERE date = ?", (date,)).rowcount
        devs = con.execute("DELETE FROM device_state WHERE date = ?", (date,)).rowcount
        con.commit()
        print(f"hub: removed {rows} minute rows and {devs} device totals for {date}")
    else:
        print("hub: not on this PC")

    if running:
        user_services("start")
        print("services started again")
    print("done: this PC starts the day at 0")


if __name__ == "__main__":
    main()
