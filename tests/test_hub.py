"""Hub API: seq ordering, stale messages, calibration, INSIDE >= 0, day boundary, auth, health, history,
cumulative fair total, exit gate switch, display settings."""

import asyncio
import json
import re
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

    def make(entry_factor=1.0, exit_factor=1.0, staff_pin=PIN, exit_enabled=True, **extra):
        cfg = dict(hub_app.DEFAULTS, entry_factor=entry_factor, exit_factor=exit_factor, exit_enabled=exit_enabled, **extra)
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
    assert s["total"] == 57  # the public total does not reset at midnight


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


def test_pin_checks_are_throttled_even_for_parallel_guesses(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "today", lambda: DAY)
    monkeypatch.setattr(hub_app, "WRONG_PIN_DELAY_S", 0.3)
    app = hub_app.create_app(cfg=dict(hub_app.DEFAULTS), tokens=TOKENS, db_path=tmp_path / "hub.db", staff_pin=PIN)

    async def burst():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as client:
            t0 = time.monotonic()
            pins = [f"guess{i}" for i in range(6)] + [PIN]   # the right PIN hidden in a parallel burst
            replies = await asyncio.gather(*[client.post("/api/v1/groups", json={"name": "x", "people": 1},
                                                         headers={"Authorization": f"Bearer {p}"}) for p in pins])
            return time.monotonic() - t0, [r.status_code for r in replies]

    elapsed, codes = asyncio.run(burst())
    assert codes.count(401) == 3 and codes.count(429) == 4  # only 3 may queue; the rest are never checked
    assert codes[-1] == 429 and elapsed >= 0.85             # the hidden right PIN was refused, checks were serial


def test_ten_wrong_pins_lock_the_form(make_client, monkeypatch):
    monkeypatch.setattr(hub_app, "WRONG_PIN_DELAY_S", 0)
    c = make_client()
    for _ in range(hub_app.MAX_PIN_FAILS):
        assert add_group(c, 1, pin="wrong").status_code == 401
    assert add_group(c, 1).status_code == 429               # even the right PIN waits out the lock
    assert c.get("/api/v1/state").json()["in"] == 0


def test_a_guesser_cannot_lock_out_other_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "today", lambda: DAY)
    monkeypatch.setattr(hub_app, "WRONG_PIN_DELAY_S", 0)
    app = hub_app.create_app(cfg=dict(hub_app.DEFAULTS), tokens=TOKENS, db_path=tmp_path / "hub.db", staff_pin=PIN)

    async def scenario():
        def device(ip):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://hub")
        body = {"name": "x", "people": 3}
        async with device("10.0.0.66") as guesser, device("10.0.0.20") as staff:
            for i in range(hub_app.MAX_PIN_FAILS):
                await guesser.post("/api/v1/groups", json=body, headers={"Authorization": f"Bearer guess{i}"})
            locked = await guesser.post("/api/v1/groups", json=body, headers={"Authorization": f"Bearer {PIN}"})
            ok = await staff.post("/api/v1/groups", json=body, headers={"Authorization": f"Bearer {PIN}"})
            return locked.status_code, ok.status_code

    assert asyncio.run(scenario()) == (429, 200)  # the guesser is locked out; the staff phone is not


def _devices(app):
    def device(ip):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://hub")
    return device


async def _try(client, pin):
    r = await client.post("/api/v1/groups", json={"name": "x", "people": 1}, headers={"Authorization": f"Bearer {pin}"})
    return r.status_code


def test_hub_wide_ceiling_pauses_new_devices_but_not_logged_in_staff(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "today", lambda: DAY)
    monkeypatch.setattr(hub_app, "WRONG_PIN_DELAY_S", 0)
    monkeypatch.setattr(hub_app, "GLOBAL_MAX_FAILS", 5)
    app = hub_app.create_app(cfg=dict(hub_app.DEFAULTS), tokens=TOKENS, db_path=tmp_path / "hub.db", staff_pin=PIN)
    device = _devices(app)

    async def scenario():
        async with device("10.0.0.20") as staff:
            assert await _try(staff, PIN) == 200            # staff phone logs in before the attack
            for i in range(5):                               # 5 guessers, one wrong PIN each
                async with device(f"10.0.1.{i}") as g:
                    assert await _try(g, "wrong") == 401
            async with device("10.0.2.1") as newcomer:
                blocked = await _try(newcomer, PIN)          # even the right PIN from a new device is paused
            return blocked, await _try(staff, PIN)

    assert asyncio.run(scenario()) == (429, 200)


def test_device_table_cleanup_keeps_wrong_pin_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "today", lambda: DAY)
    monkeypatch.setattr(hub_app, "WRONG_PIN_DELAY_S", 0)
    monkeypatch.setattr(hub_app, "MAX_PIN_CLIENTS", 3)
    app = hub_app.create_app(cfg=dict(hub_app.DEFAULTS), tokens=TOKENS, db_path=tmp_path / "hub.db", staff_pin=PIN)
    device = _devices(app)

    async def scenario():
        async with device("10.0.0.66") as guesser:
            for _ in range(hub_app.MAX_PIN_FAILS - 1):
                await _try(guesser, "wrong")
            codes = []
            for i in range(3):                               # more devices than the table holds
                async with device(f"10.0.1.{i}") as other:
                    codes.append(await _try(other, "wrong"))
            assert await _try(guesser, "wrong") == 401        # 10th strike still counted...
            return codes, await _try(guesser, PIN)           # ...so the guesser is now locked out

    codes, last = asyncio.run(scenario())
    assert 429 in codes and last == 429                      # table full: new devices refused, counts kept


def test_client_key_groups_ipv6_per_64():
    class Req:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})()
    key = lambda host: hub_app.client_key(Req(host))  # noqa: E731
    assert key("192.168.1.7") == "192.168.1.7"
    assert key("2001:db8:1:2::a") == key("2001:db8:1:2:ffff::1") == "2001:db8:1:2::/64"
    assert key("testclient") == "testclient"


def test_right_pin_resets_the_wrong_count(make_client, monkeypatch):
    monkeypatch.setattr(hub_app, "WRONG_PIN_DELAY_S", 0)
    c = make_client()
    for _ in range(hub_app.MAX_PIN_FAILS - 1):
        add_group(c, 1, pin="wrong")
    assert add_group(c, 5).status_code == 200
    assert add_group(c, 1, pin="wrong").status_code == 401  # a staff typo after that is not a lockout


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


# ---------------------------------------------------------------- cumulative fair total

def test_fair_total_adds_up_every_day_and_every_group(make_client, monkeypatch):
    c = make_client(entry_factor=2.0)
    post(c, msg(seq=1, counts=(10, 5), date="2026-11-01"))   # day 1: 15 raw
    post(c, msg(seq=2, counts=(7,), date=DAY))               # day 2: 7 raw
    post(c, msg("pc-b", "exit", counts=(9,)))                # exits never lower the visitor total
    add_group(c, 40)                                         # exact head count, not calibrated
    s = c.get("/api/v1/state").json()
    assert s["total"] == (15 + 7) * 2 + 40 and s["in"] == 7 * 2 + 40
    monkeypatch.setattr(hub_app, "today", lambda: "2026-11-03")  # next day, before anyone walks in
    s = c.get("/api/v1/state").json()
    assert (s["in"], s["total"]) == (0, 84)


def test_fair_total_survives_hub_restart(make_client):
    c = make_client()
    post(c, msg(seq=1, counts=(30,), date="2026-11-01"))
    post(c, msg(seq=2, counts=(12,)))
    add_group(c, 25)
    assert make_client().get("/api/v1/state").json()["total"] == 67  # same db file, new process


def test_fair_start_leaves_out_office_test_days(make_client):
    c = make_client(fair_start="2026-11-02")
    post(c, msg(seq=1, counts=(500,), date="2026-11-01"))   # test walks before the fair
    post(c, msg(seq=2, counts=(8,)))
    s = c.get("/api/v1/state").json()
    assert (s["total"], s["fair_start"]) == (8, "2026-11-02")


def test_fair_start_from_unquoted_yaml_date(tmp_path):
    (tmp_path / "config.yaml").write_text("fair_start: 2026-10-22\ndisplay_hold_s: 5\n")
    cfg = hub_app.load_config(tmp_path / "config.yaml")
    assert (cfg["fair_start"], cfg["display_hold_s"], cfg["exit_enabled"]) == ("2026-10-22", 5, False)


# ---------------------------------------------------------------- exit gate (pc-b) switched off

def test_exit_gate_disabled_refuses_pc_b_and_hides_it(make_client):
    c = make_client(exit_enabled=False)
    post(c, msg(counts=(20,)))
    r = post(c, msg("pc-b", "exit", counts=(5,)))
    assert r.status_code == 403 and "exit_enabled" in r.json()["detail"]
    s = c.get("/api/v1/state").json()
    assert (s["in"], s["out"], s["total"], s["exit_enabled"]) == (20, 0, 20, False)
    assert [d["device"] for d in s["devices"]] == ["pc-a"]


def test_exit_gate_can_be_enabled_again_without_losing_counts(make_client):
    c = make_client(exit_enabled=True)
    post(c, msg("pc-b", "exit", counts=(6,)))
    assert make_client(exit_enabled=False).get("/api/v1/state").json()["out"] == 0   # ignored while off
    s = make_client(exit_enabled=True).get("/api/v1/state").json()                   # back on: still there
    assert s["out"] == 6 and [d["device"] for d in s["devices"]] == ["pc-a", "pc-b"]


def test_disabled_exit_pc_is_told_why_not_bad_token(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "today", lambda: DAY)
    monkeypatch.setenv("TOKEN_PC_A", "tok-a")
    monkeypatch.setenv("TOKEN_PC_B", "tok-b")   # tokens read from the environment, as on the gate PC
    c = TestClient(hub_app.create_app(cfg=dict(hub_app.DEFAULTS), db_path=tmp_path / "hub.db", staff_pin=PIN))
    assert post(c, msg("pc-b", "exit", counts=(5,))).status_code == 403


def test_exit_gate_is_off_by_default():
    assert hub_app.DEFAULTS["exit_enabled"] is False


# ---------------------------------------------------------------- big-screen display and group form

@pytest.mark.parametrize("hold", [5, 10, 0])
def test_display_hold_is_configurable(make_client, hold):
    assert make_client(display_hold_s=hold).get("/api/v1/state").json()["display_hold_s"] == hold


@pytest.mark.parametrize("hold", [-1, 61, "10", True])
def test_bad_display_hold_stops_the_hub(make_client, hold):
    with pytest.raises(SystemExit):
        make_client(display_hold_s=hold)


def test_display_shows_only_title_total_and_credit(make_client):
    html = make_client().get("/display").text
    visible = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", visible).split())
    assert text == ("AI-Powered Visitor Count AI-Powered Visitor Count Total Visitors "
                    "Developed by Center of Excellence , ABC Technologies .")  # <title> + h1, label, footer
    assert "DIGITS = 6" in html and "s.total" in html                         # six-digit flip counter of the fair total
    for gone in ("INSIDE", "EXITED", "PEAK", "LAST HOUR", "/api/v1/history", "chart"):
        assert gone not in html, gone


def test_group_form_has_only_school_name_and_people(make_client):
    html = make_client().get("/groups").text
    form = re.search(r'<form id="form".*?</form>', html, re.S)[0]
    assert re.findall(r'<input id="(\w+)"', form) == ["name", "people"]
    assert "School Name" in form and "Number of People" in form
