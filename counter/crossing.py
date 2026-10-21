"""Pure line-crossing logic (CLAUDE.md §7). No OpenCV/YOLO imports: tested with synthetic tracks."""

from dataclasses import dataclass, field

DIRECTIONS = ("down", "up")  # direction of travel in the image: down = increasing y


@dataclass
class _Track:
    side: str | None  # "above"/"below" once the centre has been >= margin px from the line
    last_seen: float
    done: set[str] = field(default_factory=set)  # directions already reported for this id


class LineCounter:
    """Detects crossings of a horizontal line at `line_y`, inside the x segment [x_min, x_max).

    Hysteresis: a track's side only changes when its centre is at least `margin` px beyond the line,
    so lingering on the line or jitter around it never counts. A side change is a crossing; each track id
    reports each direction at most once. update() returns "count" for the gate's correct `direction`,
    "wrong_way" for the opposite, or None. The segment is checked where the crossing is confirmed; a side
    change outside it is tracked but not reported (the neighbouring camera owns it). The segment is
    half-open so a shared boundary belongs to exactly one camera. Ids unseen for `forget_after` s are dropped.
    """

    def __init__(self, line_y, x_min, x_max, direction, margin, forget_after=5.0):
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be 'down' or 'up', got {direction!r}")
        if not x_min < x_max:
            raise ValueError(f"x_min ({x_min}) must be < x_max ({x_max})")
        if margin <= 0:
            raise ValueError(f"margin must be > 0, got {margin}")
        self.line_y, self.x_min, self.x_max = line_y, x_min, x_max
        self.direction, self.margin, self.forget_after = direction, margin, forget_after
        self._tracks: dict[int, _Track] = {}
        self._last_prune = float("-inf")

    @classmethod
    def from_fractions(cls, line_y, x_min, x_max, direction, width, height, margin=0.04, forget_after=5.0):
        """Build from config fractions: line_y/margin of frame height, x_min/x_max of frame width."""
        return cls(round(line_y * height), round(x_min * width), round(x_max * width),
                   direction, margin * height, forget_after)

    def update(self, track_id, cx, cy, t):
        """Feed one tracked box centre at time t (seconds). Returns "count", "wrong_way" or None."""
        if t - self._last_prune >= 1.0:
            self.prune(t)
        if cy <= self.line_y - self.margin:
            side = "above"
        elif cy >= self.line_y + self.margin:
            side = "below"
        else:
            side = None  # inside the dead band: no change
        track = self._tracks.get(track_id)
        if track is None:
            self._tracks[track_id] = _Track(side, t)
            return None
        track.last_seen = t
        if side is None or side == track.side:
            return None
        previous, track.side = track.side, side
        if previous is None:  # first firm side for this track, nothing crossed yet
            return None
        moved = "down" if side == "below" else "up"
        if moved in track.done or not self.x_min <= cx < self.x_max:
            return None
        track.done.add(moved)
        return "count" if moved == self.direction else "wrong_way"

    def prune(self, now):
        """Forget tracks not seen for more than `forget_after` seconds."""
        self._last_prune = now
        for tid in [tid for tid, tr in self._tracks.items() if now - tr.last_seen > self.forget_after]:
            del self._tracks[tid]

    def track_ids(self):
        return set(self._tracks)
