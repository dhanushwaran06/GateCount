"""Counter state: resume after restart, IST day rollover, seq monotonic across restarts and resets."""

import json

from counter import state
from counter.state import Totals


def test_new_day_starts_at_zero(tmp_path):
    t = Totals(["c1", "c2"], tmp_path)
    assert t.snapshot()[2] == {"c1": {"count": 0, "wrong_way": 0}, "c2": {"count": 0, "wrong_way": 0}}


def test_save_and_resume(tmp_path):
    t = Totals(["c1", "c2"], tmp_path)
    for cam, kind in (("c1", "count"), ("c1", "count"), ("c2", "wrong_way")):
        t.add(cam, kind)
    seq = t.next_seq()
    t.save()
    assert not list(tmp_path.glob("*.tmp"))  # atomic write leaves no temp file
    resumed = Totals(["c1", "c2"], tmp_path)
    assert resumed.snapshot()[2] == {"c1": {"count": 2, "wrong_way": 0}, "c2": {"count": 0, "wrong_way": 1}}
    assert resumed.next_seq() > seq


def test_resume_ignores_removed_cameras_and_adds_new_ones(tmp_path):
    (tmp_path / f"{state.today()}.json").write_text(json.dumps({"seq": 5, "cameras": {"old": {"count": 9}}}))
    assert Totals(["c1"], tmp_path).snapshot()[2] == {"c1": {"count": 0, "wrong_way": 0}}


def test_rollover_at_ist_midnight(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "today", lambda: "2026-11-02")
    t = Totals(["c1"], tmp_path)
    t.add("c1", "count")
    t.save()
    monkeypatch.setattr(state, "today", lambda: "2026-11-03")
    _, date, counts = t.snapshot()
    assert date == "2026-11-03" and counts == {"c1": {"count": 0, "wrong_way": 0}}
    t.add("c1", "count")
    t.save()
    assert json.loads((tmp_path / "2026-11-02.json").read_text())["cameras"]["c1"]["count"] == 1
    assert json.loads((tmp_path / "2026-11-03.json").read_text())["cameras"]["c1"]["count"] == 1


def test_seq_stays_monotonic_when_state_file_is_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 1_000.0)
    t = Totals(["c1"], tmp_path)
    first = t.next_seq()
    t.save()
    for f in tmp_path.iterdir():
        f.unlink()  # "reset the day" by deleting the file
    monkeypatch.setattr(state.time, "time", lambda: 1_005.0)  # restart a few seconds later
    assert Totals(["c1"], tmp_path).next_seq() > first  # epoch-ms based, so the hub still accepts it


def test_seq_is_strictly_increasing_within_a_process(tmp_path, monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 1_000.0)  # frozen clock
    t = Totals(["c1"], tmp_path)
    seqs = [t.next_seq() for _ in range(5)]
    assert all(b > a for a, b in zip(seqs, seqs[1:]))
