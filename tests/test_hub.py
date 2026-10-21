"""Hub API: seq ordering, stale messages, calibration, INSIDE >= 0, day boundary, auth, health, history."""

import asyncio
import json
import time
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from hub import app as hub_app

TOKENS = {"pc-a": "tok-a", "pc-b": "tok-b"}
PIN = "staff-123"
DAY = "2026-11-02"


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "today", lambda: DAY)

    def make(entry_factor=1.0, exit_factor=1.0, staff_pin=PIN):
        cfg = dict(hub_app.DEFAULTS, entry_factor=entry_factor, exit_factor=exit_factor)
        return TestClient(hub_app.create_app(cfg=cfg, tokens=TOKENS, db_path=tmp_path / "hub.db", staff_pin=staff_pin))
    return make


def msg(device="pc-a", gate="entry", seq=1, counts=(0,), date=DAY, sent_at=None, last_frame_at=None):
    ts = sent_at or datetime.now(hub_app.IST).isoformat(timespec="seconds")
    return {"device": device, "gate": gate, "date": date, "seq": seq, "sent_at": ts,
            "cameras": {f"c{i + 1}": {"count": c, "wrong_way": 0, "fps": 9.5, "last_frame_at": last_frame_at or ts}
                        for i, c in enumerate(counts)}}


def post(client, body, token=None):
    token = token or TOKENS[body["device"]]
    return client.post("/api/v1/counts", json=body, headers={"Authorization": f"Bearer {token}"})


def test_counts_are_summed_per_gate(make_client):
    c = make_client()
    assert post(c, msg(seq=1, counts=(10, 12))).json() == {"accepted": True}
    assert post(c, msg("pc-b", "exit", seq=1, counts=(3, 4))).json() == {"accepted": True}
    s = c.get("/api/v1/state").json()
    assert (s["in"], s["out"], s["inside"]) == (22, 7, 15)


def test_newer_seq_replaces_older(make_client):
    c = make_client()
    post(c, msg(seq=1, counts=(5,)))
    post(c, msg(seq=2, counts=(9,)))
    assert c.get("/api/v1/state").json()["in"] == 9


def test_older_or_repeated_seq_is_ignored(make_client):
    c = make_client()
    post(c, msg(seq=5, counts=(9,)))
    assert post(c, msg(seq=4, counts=(3,))).json() == {"accepted": False}  # late, out-of-order delivery
    assert post(c, msg(seq=5, counts=(3,))).json() == {"accepted": False}  # duplicate
    assert c.get("/api/v1/state").json()["in"] == 9


def test_seq_is_per_device_and_date(make_client):
    c = make_client()
    post(c, msg("pc-a", seq=100, counts=(5,)))
    assert post(c, msg("pc-b", "exit", seq=1, counts=(2,))).json()["accepted"]
    assert post(c, msg("pc-a", seq=1, counts=(1,), date="2026-11-03")).json()["accepted"]


@pytest.mark.parametrize("raw_in, raw_out, fin, fout, expected", [
    (100, 40, 1.1, 1.0, (110, 40, 70)),
    (5, 0, 0.5, 1.0, (3, 0, 3)),      # exactly 2.5 -> 3 (Python's banker's round() would give 2)
    (9, 3, 0.5, 0.5, (5, 2, 3)),      # 4.5 -> 5 and 1.5 -> 2
    (10, 10, 0.95, 1.05, (10, 11, 0)),
])
def test_calibration_math(make_client, raw_in, raw_out, fin, fout, expected):
    c = make_client(entry_factor=fin, exit_factor=fout)
    post(c, msg(counts=(raw_in,)))
    post(c, msg("pc-b", "exit", counts=(raw_out,)))
    s = c.get("/api/v1/state").json()
    assert (s["in"], s["out"], s["inside"]) == expected


def test_inside_never_below_zero(make_client):
    c = make_client()
    post(c, msg(counts=(3,)))
    post(c, msg("pc-b", "exit", counts=(10,)))
    assert c.get("/api/v1/state").json()["inside"] == 0


def test_day_boundary_only_counts_today(make_client, monkeypatch):
    c = make_client()
    post(c, msg(seq=1, counts=(50,), date="2026-11-01"))  # yesterday's totals
    post(c, msg(seq=2, counts=(7,), date=DAY))            # first message of the new day
    assert c.get("/api/v1/state").json()["in"] == 7
    monkeypatch.setattr(hub_app, "today", lambda: "2026-11-03")
    s = c.get("/api/v1/state").json()
    assert (s["date"], s["in"]) == ("2026-11-03", 0)


def test_minute_rows_and_midnight(make_client):
    c = make_client()
    hub = c.app.state.hub
    assert not hub.write_minute(datetime(2026, 11, 2, 10, 0, tzinfo=hub_app.IST))  # no data yet: no zero row
    post(c, msg(counts=(12,)))
    post(c, msg("pc-b", "exit", counts=(2,)))
    assert hub.write_minute(datetime(2026, 11, 2, 10, 1, 30, tzinfo=hub_app.IST))
    assert hub.write_minute(datetime(2026, 11, 2, 23, 59, 30, tzinfo=hub_app.IST))  # written just after midnight
    rows = c.get(f"/api/v1/history?date={DAY}").json()["rows"]
    assert rows == [{"minute": "10:01", "in": 12, "out": 2, "inside": 10},
                    {"minute": "23:59", "in": 12, "out": 2, "inside": 10}]
    csv = c.get(f"/api/v1/export.csv?date={DAY}").text.splitlines()
    assert csv[0] == "date,minute,in_shown,out_shown,inside" and csv[1] == f"{DAY},10:01,12,2,10"


def test_bad_token_unknown_device_and_wrong_gate(make_client):
    c = make_client()
    assert post(c, msg(), token="nope").status_code == 401
    assert c.post("/api/v1/counts", json=msg()).status_code == 401
    assert post(c, msg(device="pc-z"), token="tok-a").status_code == 401
    assert post(c, msg(gate="exit")).status_code == 409
    assert post(c, {"device": "pc-a"}).status_code == 422


@pytest.mark.parametrize("bad", [
    {"sent_at": "not a time"},
    {"sent_at": "2026-11-02T11:00:00"},                    # no timezone
    {"cameras": {"c1": {"count": 1, "last_frame_at": "yesterday"}}},
    {"cameras": {f"c{i}": {"count": 1} for i in range(17)}},
    {"cameras": {"c1": {"count": 10**12}}},
])
def test_malformed_messages_are_rejected_and_hub_keeps_serving(make_client, bad):
    c = make_client()
    post(c, msg(seq=1, counts=(4,)))
    assert post(c, {**msg(seq=2), **bad}).status_code == 422
    s = c.get("/api/v1/state")
    assert s.status_code == 200 and s.json()["in"] == 4


def test_nan_fps_is_rejected(make_client):
    c = make_client()
    body = json.dumps({**msg(seq=2), "cameras": {"c1": {"count": 1, "fps": float("nan")}}})  # raw "NaN" literal
    r = c.post("/api/v1/counts", content=body, headers={"Authorization": f"Bearer {TOKENS['pc-a']}",
                                                        "Content-Type": "application/json"})
    assert r.status_code == 422 and c.get("/api/v1/state").status_code == 200


def test_health_online_offline_and_stale(make_client):
    c = make_client()
    now = datetime.now(hub_app.IST)
    post(c, msg(counts=(1, 2), sent_at=now.isoformat(), last_frame_at=now.isoformat()))
    hub = c.app.state.hub
    t = hub.latest[("pc-a", DAY)]["received_at"]
    a, b = hub.state(now=t + 5)["devices"]
    assert a["online"] and not a["cameras"][0]["stale"] and a["last_seen_s"] == 5
    assert not b["online"] and b["last_seen_s"] is None  # pc-b never reported
    a = hub.state(now=t + 20)["devices"][0]
    assert a["online"] and a["cameras"][0]["stale"]       # frame age > 15 s
    assert not hub.state(now=t + 31)["devices"][0]["online"]  # no message for > 30 s


def test_health_uses_hub_clock_not_pc_clock(make_client):
    c = make_client()
    skewed = "2026-11-02T08:00:00+05:30"  # PC clock hours behind; frames were current when sent
    post(c, msg(counts=(1,), sent_at=skewed, last_frame_at=skewed))
    hub = c.app.state.hub
    t = hub.latest[("pc-a", DAY)]["received_at"]
    a = hub.state(now=t + 2)["devices"][0]
    assert a["online"] and not a["cameras"][0]["stale"]


def test_state_survives_hub_restart(make_client):
    c = make_client()
    post(c, msg(seq=3, counts=(40,)))
    restarted = make_client()  # same db file
    assert restarted.get("/api/v1/state").json()["in"] == 40
    assert post(restarted, msg(seq=2, counts=(1,))).json() == {"accepted": False}  # seq survives too


def test_dashboard_and_healthz(make_client):
    c = make_client()
    assert c.get("/healthz").json() == {"ok": True}
    r = c.get("/")
    assert r.status_code == 200 and "EventSource" in r.text


def add_group(client, people, name="Govt. School", pin=PIN):
    return client.post("/api/v1/groups", json={"name": name, "people": people}, headers={"Authorization": f"Bearer {pin}"})


def test_group_entries_add_to_in_and_inside_without_calibration(make_client):
    c = make_client(entry_factor=2.0)
    post(c, msg(counts=(10,)))                      # cameras: 10 raw x 2.0 = 20
    post(c, msg("pc-b", "exit", counts=(5,)))
    assert add_group(c, 30).status_code == 200      # +30 exactly
    s = c.get("/api/v1/state").json()
    assert (s["in"], s["in_groups"], s["out"], s["inside"]) == (50, 30, 5, 45)
    listed = c.get("/api/v1/groups").json()
    assert listed["total"] == 30 and listed["groups"][0]["name"] == "Govt. School"


def test_group_entry_can_be_undone(make_client):
    c = make_client()
    gid = add_group(c, 40).json()["id"]
    assert c.get("/api/v1/state").json()["in"] == 40
    assert c.delete(f"/api/v1/groups/{gid}", headers={"Authorization": f"Bearer {PIN}"}).status_code == 200
    assert c.get("/api/v1/state").json()["in"] == 0
    assert c.delete(f"/api/v1/groups/{gid}", headers={"Authorization": f"Bearer {PIN}"}).status_code == 404


def test_short_staff_pin_keeps_the_form_off(make_client):
    c = make_client(staff_pin="1234")
    assert add_group(c, 10, pin="1234").status_code == 403


def test_wrong_pins_are_serialised_across_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "today", lambda: DAY)
    app = hub_app.create_app(cfg=dict(hub_app.DEFAULTS), tokens=TOKENS, db_path=tmp_path / "hub.db", staff_pin=PIN)

    async def guess_in_parallel():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as client:
            t0 = time.monotonic()
            replies = await asyncio.gather(*[client.post("/api/v1/groups", json={"name": "x", "people": 1},
                                                         headers={"Authorization": f"Bearer guess{i}"}) for i in range(3)])
            ok = await client.post("/api/v1/groups", json={"name": "x", "people": 1}, headers={"Authorization": f"Bearer {PIN}"})
            return time.monotonic() - t0, [r.status_code for r in replies], ok.status_code

    elapsed, codes, ok = asyncio.run(guess_in_parallel())
    assert codes == [401, 401, 401] and elapsed >= 2.9  # 3 parallel guesses still take 3 s
    assert ok == 200                                     # the right PIN is never held up


def test_group_entries_need_the_staff_pin(make_client):
    c = make_client()
    assert add_group(c, 10, pin="wrong").status_code == 401
    assert c.post("/api/v1/groups", json={"name": "x", "people": 1}).status_code == 401
    assert make_client(staff_pin="").post("/api/v1/groups", json={"name": "x", "people": 1}).status_code == 403
    assert c.get("/api/v1/state").json()["in"] == 0


@pytest.mark.parametrize("body", [{"name": "x", "people": 0}, {"name": "x", "people": 1001},
                                  {"name": "   ", "people": 5}, {"name": "x" * 81, "people": 5}])
def test_bad_group_entries_are_rejected(make_client, body):
    c = make_client()
    assert c.post("/api/v1/groups", json=body, headers={"Authorization": f"Bearer {PIN}"}).status_code == 422


def test_minute_rows_include_groups(make_client):
    c = make_client()
    add_group(c, 25)                                 # no counter has reported yet: groups alone still write a row
    assert c.app.state.hub.write_minute(datetime(2026, 11, 2, 10, 0, 30, tzinfo=hub_app.IST))
    assert c.get(f"/api/v1/history?date={DAY}").json()["rows"] == [{"minute": "10:00", "in": 25, "out": 0, "inside": 25}]


def test_display_and_group_pages_are_served(make_client):
    c = make_client()
    for path in ("/display", "/groups", "/static/fonts/orbitron.woff2"):
        assert c.get(path).status_code == 200, path
