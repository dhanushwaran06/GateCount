"""Line-crossing rules (CLAUDE.md §7) with synthetic tracks — no camera or model needed."""

import pytest

from counter.crossing import LineCounter

# 1280x720 frame, line at y=360, margin 30 px -> "above" is y <= 330, "below" is y >= 390.
W, H, LINE, MARGIN = 1280, 720, 360, 30


def make(direction="down", x_min=0, x_max=W):
    return LineCounter(line_y=LINE, x_min=x_min, x_max=x_max, direction=direction, margin=MARGIN)


def walk(lc, tid, ys, x=640, t0=0.0, dt=0.1):
    """Feed a track through the given y positions; return the non-None events."""
    events = [lc.update(tid, x, y, t0 + i * dt) for i, y in enumerate(ys)]
    return [e for e in events if e]


def test_clean_crossing_down_counts_once():
    assert walk(make("down"), 1, range(200, 560, 20)) == ["count"]


def test_clean_crossing_up_counts_once():
    assert walk(make("up"), 1, range(560, 200, -20)) == ["count"]


def test_wrong_way_crossing_is_reported_not_counted():
    assert walk(make("down"), 1, range(560, 200, -20)) == ["wrong_way"]


def test_lingering_on_the_line_counts_once():
    ys = [250, 300] + [345, 360, 375, 362, 350, 370] * 20 + [420, 500]
    assert walk(make(), 1, ys) == ["count"]


def test_small_jitter_inside_dead_band_never_counts():
    assert walk(make(), 1, [340, 380, 335, 385, 345, 375] * 50) == []


def test_large_oscillation_counts_each_direction_at_most_once():
    ys = [300, 420] * 10  # repeatedly crosses both thresholds
    assert walk(make(), 1, ys) == ["count", "wrong_way"]


def test_crossing_outside_x_segment_is_ignored():
    lc = make(x_min=0, x_max=640)
    assert walk(lc, 1, range(200, 560, 20), x=900) == []


def test_crossing_outside_segment_then_drifting_inside_does_not_count():
    lc = make(x_min=0, x_max=640)
    walk(lc, 1, range(200, 560, 20), x=900)  # crossed in the other camera's segment
    assert walk(lc, 1, [500, 510, 520], x=300, t0=5.0) == []  # same side, now inside our segment


def test_segment_is_half_open_so_boundary_belongs_to_one_camera():
    left, right = make(x_min=0, x_max=640), make(x_min=640, x_max=W)
    ys = range(200, 560, 20)
    assert walk(left, 1, ys, x=640) == [] and walk(right, 1, ys, x=640) == ["count"]


def test_track_appearing_already_past_the_line_is_not_counted():
    assert walk(make(), 1, [450, 500, 560]) == []


def test_track_starting_inside_dead_band_is_not_counted():
    assert walk(make(), 1, [350, 370, 400, 450]) == []


def test_track_lost_briefly_and_reappearing_past_line_counts():
    lc = make()
    assert walk(lc, 1, [200, 250, 300], t0=0.0) == []
    assert walk(lc, 1, [420, 460], t0=3.0) == ["count"]  # gap of ~2.8 s < 5 s


def test_track_forgotten_after_5s_is_not_counted_on_reappearance():
    lc = make()
    walk(lc, 1, [200, 250, 300], t0=0.0)
    lc.prune(now=6.0)
    assert walk(lc, 1, [420, 460], t0=6.0) == []


def test_stale_track_cleanup():
    lc = make()
    walk(lc, 1, [200], t0=0.0)
    walk(lc, 2, [200], t0=4.0)
    lc.prune(now=6.0)
    assert lc.track_ids() == {2}
    lc.prune(now=9.5)
    assert lc.track_ids() == set()


def test_update_prunes_automatically():
    lc = make()
    walk(lc, 1, [200], t0=0.0)
    walk(lc, 2, [200], t0=10.0)
    assert lc.track_ids() == {2}


def test_tracks_are_independent():
    lc = make()
    events = []
    for i, y in enumerate(range(200, 560, 20)):
        events += [lc.update(1, 300, y, i * 0.1), lc.update(2, 900, y, i * 0.1), lc.update(3, 600, 760 - y, i * 0.1)]
    assert sorted(e for e in events if e) == ["count", "count", "wrong_way"]


def test_fast_walker_jumping_over_dead_band_counts():
    assert walk(make(), 1, [250, 450]) == ["count"]


def test_from_fractions_uses_frame_size_and_default_margin():
    lc = LineCounter.from_fractions(line_y=0.5, x_min=0.0, x_max=0.55, direction="up", width=W, height=H)
    assert (lc.line_y, lc.x_min, lc.x_max, lc.direction) == (360, 0, 704, "up")
    assert lc.margin == pytest.approx(0.04 * H)


@pytest.mark.parametrize("kwargs", [
    {"direction": "left"},
    {"x_min": 700, "x_max": 600},
    {"margin": 0},
])
def test_invalid_config_is_rejected(kwargs):
    args = dict(line_y=LINE, x_min=0, x_max=W, direction="down", margin=MARGIN) | kwargs
    with pytest.raises(ValueError):
        LineCounter(**args)
