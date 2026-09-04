"""Automatic budget allocation: the graph the loop actually runs.

`config.json` (plus `state/graph.json`) is the operator's **ceiling**, not the
plan. This module turns that ceiling into the graph in force right now, from
three budget buckets and how fast each one is being spent:

    fable       the per-model Fable window (`usage.extra["fable"]`), which the
                executive and the advisory lenses spend
    weekly      the seven-day window, every model together - the one the
                weekly goal and `state/stop.json` are measured on
    five_hour   the session window, the guard that already produces `blocked`

For each bucket it measures utilization now, the burn rate since the window
opened, the time to its reset and the goal it is paced to, and forecasts:

    expected_at_reset = utilization + rate * hours_to_reset
    ahead_by          = expected_at_reset - goal      (positive = will overshoot)

`ahead_by` is what the panel and `tracker.py alloc` print, but it is NOT what
the ladder is gated on. The endpoint reports utilization in whole percent, so
the smallest burn rate a span of `h` hours can even represent is `0.01 / h` -
and a week-long extrapolation multiplies that quantum by `hours_to_reset`,
which for a seven-day window turns one percent of measurement noise into more
than a whole utilization point of forecast. A dead band in utilization space is
therefore narrower than the measurement grid, and the ladder built on it can
only ever read "climb" or "give back". The gate lives in RATE space instead:

    required = (goal - utilization) / hours_to_reset      the pace to hold
    tolerance = max(|required| * slack, UTIL_QUANTUM / span_hours)

with `slack` being `allocation.ahead_step` / `allocation.behind_step` as
*fractions of the required pace*, and the floor being the quantum of the
estimate that produced the rate. The rate itself is measured from the window's
own opening rather than over a trailing hour, so its quantum (`0.01 /
hours_elapsed`) shrinks as the window runs instead of staying pinned at one
percent per hour. The trailing 60m/3h slopes are still measured, and still
written to state/allocation.json, but only for display.

A rate also has an AGE, and three rules follow from it. Each bucket derives its
window's start (`resets_at` minus the window's own length, learned from the
previous reset stamp in history and falling back to the nominal period), and:

    warm-up   below `allocation.warmup_hours` the ladder may give a rung back
              but may not climb, and the lanes may rise but not be cut. Fifty
              minutes of a busy morning cannot tell you the pace of a seven-day
              window, and throttling hardest when the budget is freshest is
              exactly backwards (2026-09-04 07:51 CT: 3% used, 3.92%/h
              measured, 657% "expected at reset", rung 5 of 5, lanes at 2)
    reset      when a window start moves, the new window starts from a clean
               slate - rung 0, counters cleared - rather than inheriting the
               rung the previous window ended on
    horizon    `allocation.max_horizon_hours` caps how far a rate is
               extrapolated, so one busy hour is not carried across a week.
               `required` still targets the REAL reset, so the long-run pace
               the ladder is gated on is unchanged

The FABLE bucket drives a six-rung ladder that protects the executive and gives
up the cheaper things first (`_build`); the WEEKLY bucket drives the worker lane
count between `allocation.min_workers` and the graph's own `surge_count`
(`worker_target`), which is the automatic surge that replaces reaching for the
button. Every rung keeps the superiority rule - executive >= advisory >=
workers by model capability - and the executive's model is never touched.

Two files, two speeds:

    `evaluate()`  runs once per poll, reads state/history.jsonl, moves the
                  ladder under hysteresis and writes state/allocation.json
    `allocate()`  is cheap and reads only state/allocation.json, so
                  `graph.apply_graph`, the fork prompt and every overlay
                  refresh can ask for the allocated graph without parsing
                  history

With no state/allocation.json - a fresh install, or a loop that has never
polled - `allocate()` returns the configured graph unchanged. That is the whole
safety property: nothing here can move the graph until the loop has actually
measured a burn rate.

FULL THROTTLE (`state/throttle.json`) stays a manual override: while it is on
the allocator's own numbers are still measured and logged, but the applied
graph is the configured one with the workers pinned to `surge_count`.

Nothing in here raises. It is called from inside the poll, from `load_config`
and from every overlay refresh, so a hostile history line, a half-written
allocation file and a hand-edited `allocation` block all have to degrade to
"hold the configured graph" rather than take the loop's next tick down.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .models import WEEK_HOURS, parse_iso, utcnow

FABLE = "fable"
WEEKLY = "weekly"
FIVE_HOUR = "five_hour"

# What each bucket is measured on. `window` is where the reading comes from in
# a UsageSnapshot, `models` which tier models spend it, `stop` the hard stop the
# loop already enforces for it (None = the bucket's goal is the stop).
FABLE_STOP = 0.97
BUCKETS: dict[str, dict[str, Any]] = {
    FABLE: {
        "window": "extra",
        "hints": ("fable",),
        "period_hours": WEEK_HOURS,
        "models": "fable",
        "stop": FABLE_STOP,
        "label": "fable",
    },
    WEEKLY: {
        "window": "seven_day",
        "hints": (),
        "period_hours": WEEK_HOURS,
        "models": "all",
        "stop": None,
        "label": "weekly",
    },
    FIVE_HOUR: {
        "window": "five_hour",
        "hints": (),
        "period_hours": 5.0,
        "models": "all",
        "stop": None,
        "label": "5h",
    },
}
BUCKET_ORDER = (FABLE, WEEKLY, FIVE_HOUR)

# The ladder. Cumulative: step N applies every rung up to N.
MAX_STEP = 5
STEP_LABELS = {
    0: "configured graph",
    1: "advisory count -1",
    2: "advisory effort medium",
    3: "fork cadence x2",
    4: "advisory on the workers' model, fork cadence x4",
    5: "advisory at minimum, fork cadence at maximum",
}
# The fork re-arm cooldown multiplier per rung; None means "the configured
# maximum", which is the executive-cadence-only rung.
COOLDOWN_FACTOR: dict[int, float | None] = {0: 1.0, 1: 1.0, 2: 1.0,
                                            3: 2.0, 4: 4.0, 5: None}
EFFORT_FULL = "high"
EFFORT_REDUCED = "medium"

# config.json's `allocation` block, and its defaults.
# `ahead_step`/`behind_step` are slack around the REQUIRED pace, as a fraction
# of it, floored at the measurement quantum (see the module docstring).
AHEAD_STEP = 0.03
BEHIND_STEP = 0.02
MIN_ADVISORY = 1
MIN_WORKERS = 2
MAX_FORK_COOLDOWN_SECONDS = 1800.0
# No more than one rung per this long, whatever the counters say. Hysteresis
# counts polls, and a signal saturated in one direction for ten polls running
# walks the whole ladder in minutes; the dwell is what bounds that in time.
MIN_DWELL_SECONDS = 1800.0
# How long a window must have run before its rate may cost a rung. The rate is
# anchored at the window's opening, so early in a window the whole estimate is
# whatever the last few minutes did: at 07:51 CT on 2026-09-04, 51 minutes into
# a fresh week, one screenshot render and one director run read as 3.92%/h and
# forecast 657% at reset. Below this the ladder may still give rungs BACK -
# holding a throttle nobody can justify is the failure being fixed.
WARMUP_HOURS = 2.0
# The furthest ahead a measured rate is carried. `expected_at_reset` is a rate
# multiplied by hours, and 167 of them turn a busy hour into a forecast that
# says nothing about the week. The pace the bucket is REQUIRED to hold still
# divides by the real time to reset, so the target itself is untouched.
MAX_HORIZON_HOURS = 48.0
# The dataclass default for `fork_cooldown_seconds`, repeated rather than
# imported from `cli` (which pulls in the dispatcher, and this module is
# reached from `load_config`).
FORK_COOLDOWN_DEFAULT = 120.0
ALLOC_DEFAULTS: dict[str, float] = {
    "ahead_step": AHEAD_STEP,
    "behind_step": BEHIND_STEP,
    "min_advisory": float(MIN_ADVISORY),
    "min_workers": float(MIN_WORKERS),
    "max_fork_cooldown_seconds": MAX_FORK_COOLDOWN_SECONDS,
    "min_dwell_seconds": MIN_DWELL_SECONDS,
    "warmup_hours": WARMUP_HOURS,
    "max_horizon_hours": MAX_HORIZON_HOURS,
}
# Hysteresis: a rung costs two polls of agreement to climb and three to give
# back, so one noisy reading never oscillates the graph.
UP_POLLS = 2
DOWN_POLLS = 3

# Rate measurement, in fractions of the bucket per hour. The 60m and 3h slopes
# are display only (`rate_1h`/`rate_3h`); every decision reads `rate`, which is
# anchored at the window's opening.
RATE_MINUTES_FAST = 60.0
RATE_MINUTES_SLOW = 180.0
MIN_SPAN_MINUTES = 5.0
# The endpoint reports utilization in whole percent. Every rate estimate is a
# difference of two such readings, so its own resolution is this over the span
# it was measured across - and no dead band may be narrower than that.
UTIL_QUANTUM = 0.01
# A drop this big between two readings is a window rollover, not negative burn.
RESET_DROP = 0.05
# A window length derived from two reset stamps is believed only this near the
# bucket's nominal period. The endpoint's reset time is fixed inside a window
# but nothing promises it: a replayed clock, a rolling session window or a
# single edited line can put two stamps minutes apart, and a length of minutes
# would make every poll look like the first minute of a fresh window.
WINDOW_LEN_MIN_FACTOR = 0.5
WINDOW_LEN_MAX_FACTOR = 2.0
# What counts as a window having ROLLED rather than drifted. A real rollover
# moves the start by a whole window; jitter in the reported reset time (and a
# test clock that walks forward under a fixed offset) moves it by minutes, and
# clearing the ladder on that would be its own oscillator.
RESET_SHIFT_FRACTION = 0.25
RESET_SHIFT_MIN_HOURS = 0.5
# The buckets whose rollover clears the ladder: the one that drives the rungs
# and the one that drives the lanes. The five-hour guard rolls several times a
# day and moves no rung, so it is not one of them.
RESET_WATCH = (FABLE, WEEKLY)
# Below this the rate says nothing: hold the configured graph rather than
# forecasting the whole week off rounding noise.
MIN_RATE = 0.0005
# Far enough back to reach the opening of the longest window; a rollover before
# that is trimmed by `_points` anyway, so nothing older can be relevant.
HISTORY_HOURS = WEEK_HOURS + 1.0

STATE_KEYS = ("buckets", "decision", "reasons", "notes", "generated_at")


# ------------------------------------------------------------- small helpers

def _finite(value: Any, fallback: float) -> float:
    """A finite float, or `fallback`. Never raises."""
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def alloc_setting(cfg: Any, key: str) -> float:
    """One `allocation.<key>` from config.json, with its built-in default.

    Tolerant on purpose: the block is hand-edited, and a nonsense value there
    must fall back to the default rather than reach the ladder.
    """
    block = getattr(cfg, "allocation", None)
    default = ALLOC_DEFAULTS.get(key, 0.0)
    if not isinstance(block, dict):
        return default
    return _finite(block.get(key, default), default)


def min_advisory(cfg: Any) -> int:
    return max(1, int(alloc_setting(cfg, "min_advisory")))


def min_workers(cfg: Any) -> int:
    return max(1, int(alloc_setting(cfg, "min_workers")))


def max_cooldown(cfg: Any) -> float:
    return max(0.0, alloc_setting(cfg, "max_fork_cooldown_seconds"))


def min_dwell(cfg: Any) -> float:
    """Seconds a rung must stand before the ladder may move again."""
    return max(0.0, alloc_setting(cfg, "min_dwell_seconds"))


def warmup_hours(cfg: Any) -> float:
    """Hours a window must have run before its rate may cost a rung.

    Zero turns the hold off, which is the pre-2026-09-04 behaviour and is left
    reachable for anyone who wants it back.
    """
    return max(0.0, alloc_setting(cfg, "warmup_hours"))


def max_horizon(cfg: Any) -> float:
    """Hours a measured rate may be extrapolated across. Zero = no cap."""
    return max(0.0, alloc_setting(cfg, "max_horizon_hours"))


def throttle_active(cfg: Config) -> bool:
    """The FULL THROTTLE manual override, read straight off state/throttle.json."""
    try:
        data = json.loads(cfg.throttle_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return bool(isinstance(data, dict) and data.get("active"))


def _write_atomic(path, body: str) -> None:
    # The same swap-in-whole dance control/goal/graph use: a torn read of this
    # file reads as "no allocation", which silently restores the full graph.
    tmp = path.parent / f"{path.name}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass


def reset_label(when: Any, cfg: Any = None) -> str:
    """"Fri 07:00" for a reset time, on the operator's clock. Never raises.

    Through `clock`, not `astimezone()`: the machine's own zone and the zone
    the operator asked for in config.json are not the same thing, and a reset
    time rendered in two zones on one panel is worse than either.
    """
    from .clock import fmt_local

    moment = when if isinstance(when, datetime) else parse_iso(when)
    if moment is None:
        return "?"
    return fmt_local(moment, "%a %H:%M", cfg, fallback="?")


def _zone(cfg: Any = None) -> str:
    """" CT", the zone label to hang off a rendered time. "" if unknown.

    A reset time on a line that is also read next to ISO UTC state files has to
    say which clock it is on; nothing here may raise to say it.
    """
    try:
        from .clock import label

        text = str(label(cfg)).strip()
    except Exception:  # pragma: no cover - a label is never worth a tick
        return ""
    return f" {text}" if text else ""


# ------------------------------------------------------------------ buckets

@dataclass
class Bucket:
    """One budget bucket, its burn rate and where that lands it at reset."""

    name: str
    utilization: float
    hours_to_reset: float
    goal: float
    stop: float
    resets_at: datetime | None = None
    rate_1h: float | None = None
    rate_3h: float | None = None
    rate_long: float | None = None
    span_hours: float | None = None
    # When this window OPENED, and how long it runs. Derived in `read_buckets`
    # from `resets_at` minus the window's own length (the gap to the previous
    # reset stamp in history, else the bucket's nominal period), because the
    # endpoint reports only where a window ENDS.
    window_start: datetime | None = None
    window_hours: float | None = None
    # How long it has been running at the moment this bucket was read. None
    # means the window's start could not be established, which reads as "no
    # opinion": nothing is gated on an age nobody knows.
    elapsed_hours: float | None = None
    warmup_hours: float = WARMUP_HOURS
    max_horizon_hours: float | None = None

    @property
    def fresh(self) -> bool:
        """True while the window is younger than its warm-up.

        The flag the ladder's climb is gated on. A rate anchored at a window
        that opened forty minutes ago is a measurement of forty minutes, and
        multiplying it by a week is arithmetic, not evidence.
        """
        elapsed = self.elapsed_hours
        if elapsed is None or not math.isfinite(elapsed):
            return False
        return elapsed < max(self.warmup_hours, 0.0)

    @property
    def age_minutes(self) -> float | None:
        """The window's age in minutes, for the line that says why it held."""
        elapsed = self.elapsed_hours
        if elapsed is None or not math.isfinite(elapsed):
            return None
        return elapsed * 60.0

    @property
    def horizon_hours(self) -> float:
        """How far this bucket's rate is carried: the reset, or the cap.

        `min(hours_to_reset, allocation.max_horizon_hours)`. Only the FORECAST
        is capped; `required` keeps dividing by the real time to reset, so the
        pace the bucket has to hold to land on its goal is untouched.
        """
        cap = self.max_horizon_hours
        if cap is None or not math.isfinite(cap) or cap <= 0:
            return self.hours_to_reset
        return min(self.hours_to_reset, cap)

    @property
    def capped(self) -> bool:
        """True when the forecast stops short of the reset."""
        return self.horizon_hours < self.hours_to_reset - 1e-9

    @property
    def rate(self) -> float | None:
        """Fraction of the bucket per hour, or None when nothing is measurable.

        The window-open baseline, because its measurement quantum is
        `UTIL_QUANTUM / hours_elapsed` and therefore shrinks as the window
        runs, where a trailing-hour slope is stuck at a whole percent per hour
        forever. The trailing slopes are the fallback for the shapes that
        cannot produce a long baseline at all - which in practice is only
        "fewer than two readings", since the hour window is a subset of it.
        """
        if self.rate_long is not None:
            return self.rate_long
        return self.rate_1h if self.rate_1h is not None else self.rate_3h

    @property
    def expected_at_reset(self) -> float | None:
        """utilization + rate x the HORIZON, which is at most the reset.

        Capped at `allocation.max_horizon_hours`: extrapolating a rate across
        167 hours multiplies its noise by 167 too, which is how 3% used at
        3.92%/h read as 657% at reset and pinned the ladder at its top rung.
        """
        rate = self.rate
        if rate is None:
            return None
        return self.utilization + rate * self.horizon_hours

    @property
    def ahead_by(self) -> float | None:
        """expected_at_reset - goal. Positive means it will overshoot.

        Display only. It is a forecast multiplied by the horizon, so a
        one-percent step in the reading moves it by a whole utilization point
        on a weekly window; `pace_state` is what any decision reads.
        """
        expected = self.expected_at_reset
        return None if expected is None else expected - self.goal

    @property
    def required(self) -> float:
        """The pace that lands exactly on the goal at reset, per hour.

        Negative when the bucket is already past its goal, which is the honest
        answer: no rate at all can bring it back inside.
        """
        return (self.goal - self.utilization) / max(self.hours_to_reset, 0.05)

    @property
    def rate_quantum(self) -> float:
        """The smallest non-zero rate this bucket's estimate can represent."""
        span = self.span_hours
        if span is None or not math.isfinite(span) or span <= 0:
            span = RATE_MINUTES_FAST / 60.0
        return UTIL_QUANTUM / span

    def tolerance(self, slack: float) -> float:
        """Dead band half-width in rate space, floored at the quantum.

        `slack` is a fraction of the required pace (`allocation.ahead_step` /
        `allocation.behind_step`); the floor is what stops the band being
        narrower than the grid the measurement lands on.
        """
        return max(abs(self.required) * max(slack, 0.0), self.rate_quantum)

    def pace_state(self, ahead_slack: float, behind_slack: float) -> str | None:
        """"ahead", "behind", "hold" - or None when there is no rate yet."""
        rate = self.rate
        if rate is None:
            return None
        required = self.required
        if rate > required + self.tolerance(ahead_slack):
            return "ahead"
        if rate < required - self.tolerance(behind_slack):
            return "behind"
        return "hold"

    def to_dict(self) -> dict[str, Any]:
        rate = self.rate
        return {
            "utilization": self.utilization,
            "rate_1h": self.rate_1h,
            "rate_3h": self.rate_3h,
            "rate_long": self.rate_long,
            "rate": rate,
            "span_hours": self.span_hours,
            "required_rate": self.required,
            "rate_quantum": self.rate_quantum,
            "pace_gap": None if rate is None else rate - self.required,
            "hours_to_reset": self.hours_to_reset,
            "horizon_hours": self.horizon_hours,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "window_start": (self.window_start.isoformat()
                             if self.window_start else None),
            "window_hours": self.window_hours,
            "elapsed_hours": self.elapsed_hours,
            "warmup_hours": self.warmup_hours,
            "fresh": self.fresh,
            "goal": self.goal,
            "stop": self.stop,
            "expected_at_reset": self.expected_at_reset,
            "ahead_by": self.ahead_by,
        }


def _window_of(snap: Any, name: str):
    """The WindowUsage a bucket reads, or None when the snapshot has not got it."""
    spec = BUCKETS.get(name, {})
    which = spec.get("window")
    if which == "seven_day":
        return getattr(snap, "seven_day", None)
    if which == "five_hour":
        return getattr(snap, "five_hour", None)
    extra = getattr(snap, "extra", None)
    if not isinstance(extra, dict):
        return None
    hints = spec.get("hints") or ()
    for key, window in extra.items():
        if any(hint in str(key).lower() for hint in hints):
            return window
    return None


def _points(snaps: list[Any], name: str,
            now: datetime | None = None) -> list[tuple[datetime, float]]:
    """(time, utilization) for one bucket, oldest first, resets trimmed off.

    Readings later than `now` are dropped, so replaying a poll at an earlier
    clock sees only what that poll could have seen.
    """
    pairs: list[tuple[datetime, float]] = []
    for snap in snaps:
        window = _window_of(snap, name)
        if window is None:
            continue
        util = _finite(getattr(window, "utilization", None), math.nan)
        stamp = getattr(snap, "fetched_at", None)
        if not math.isfinite(util) or not isinstance(stamp, datetime):
            continue
        if now is not None and stamp > now:
            continue
        pairs.append((stamp, util))
    pairs.sort(key=lambda p: p[0])
    # Everything before the last rollover is a different window's spending.
    last_drop = 0
    for i in range(1, len(pairs)):
        if pairs[i][1] < pairs[i - 1][1] - RESET_DROP:
            last_drop = i
    return pairs[last_drop:]


def _rate(points: list[tuple[datetime, float]], minutes: float,
          now: datetime) -> float | None:
    """Fraction of the bucket burned per hour over the last `minutes`.

    None when it cannot be measured: fewer than two readings, or a span too
    short to divide by. A negative slope (a rollover the drop test missed)
    comes back as 0.0 rather than as a forecast that the bucket refills.
    """
    cutoff = now - timedelta(minutes=minutes)
    window = [p for p in points if cutoff <= p[0] <= now]
    if len(window) < 2:
        return None
    span_h = (window[-1][0] - window[0][0]).total_seconds() / 3600.0
    if span_h * 60.0 < MIN_SPAN_MINUTES:
        return None
    return max(0.0, (window[-1][1] - window[0][1]) / span_h)


def _rate_long(points: list[tuple[datetime, float]],
               now: datetime) -> tuple[float | None, float | None]:
    """Burn rate since the window opened, and the hours it is measured across.

    `points` is already trimmed at the last rollover, so `points[0]` is this
    window's own opening. Anchoring there rather than an hour back is what
    makes the estimate's quantum (`UTIL_QUANTUM / span`) shrink as the window
    runs, and what damps it: one percent appearing in the last five minutes
    moves a week-old baseline by almost nothing.
    """
    window = [p for p in points if p[0] <= now]
    if len(window) < 2:
        return None, None
    span_h = (window[-1][0] - window[0][0]).total_seconds() / 3600.0
    if span_h * 60.0 < MIN_SPAN_MINUTES:
        return None, None
    return max(0.0, (window[-1][1] - window[0][1]) / span_h), span_h


def _reset_stamps(snaps: list[Any], name: str,
                  now: datetime | None = None) -> list[datetime]:
    """Every distinct `resets_at` this bucket has reported, oldest first.

    The endpoint says where a window ENDS and never where it began, so the only
    record of a window's length is the distance between two consecutive reset
    stamps in state/history.jsonl. Readings later than `now` are dropped, so a
    replayed poll sees only what that poll could have seen.
    """
    pairs: list[tuple[datetime, datetime]] = []
    for snap in snaps:
        window = _window_of(snap, name)
        stamp = getattr(window, "resets_at", None)
        fetched = getattr(snap, "fetched_at", None)
        if not isinstance(stamp, datetime) or not isinstance(fetched, datetime):
            continue
        if now is not None and fetched > now:
            continue
        pairs.append((fetched, stamp))
    pairs.sort(key=lambda p: p[0])
    out: list[datetime] = []
    for _fetched, stamp in pairs:
        if not out or stamp != out[-1]:
            out.append(stamp)
    return out


def _window_length(stamps: list[datetime], period_hours: float) -> float:
    """The window's own length in hours: this reset minus the one before it.

    Falls back to the bucket's nominal period - seven days for the weekly and
    Fable windows, five hours for the session guard - and only believes a
    derived length within `WINDOW_LEN_MIN/MAX_FACTOR` of it, because two stamps
    minutes apart would otherwise make every window look brand new.
    """
    if len(stamps) >= 2:
        try:
            hours = (stamps[-1] - stamps[-2]).total_seconds() / 3600.0
        except (TypeError, OverflowError, ValueError):
            hours = math.nan
        if (math.isfinite(hours)
                and period_hours * WINDOW_LEN_MIN_FACTOR
                <= hours <= period_hours * WINDOW_LEN_MAX_FACTOR):
            return hours
    return period_hours


def _window_start(resets_at: Any, length_hours: float) -> datetime | None:
    """When the window opened: its reset, less its own length. Never raises."""
    if not isinstance(resets_at, datetime):
        return None
    if not math.isfinite(length_hours) or length_hours <= 0:
        return None
    try:
        return resets_at - timedelta(hours=length_hours)
    except (OverflowError, ValueError, OSError):
        return None


def _rolled_over(buckets: dict[str, Bucket],
                 seen: dict[str, datetime]) -> list[tuple[str, datetime]]:
    """The watched buckets whose window START moved since the last poll.

    A rollover moves the start by a whole window; the reported reset time
    drifting by minutes does not, and clearing the ladder on that would be its
    own oscillator. Nothing is reported for a bucket with no stored start,
    which is what keeps the first poll after an upgrade quiet.
    """
    rolled: list[tuple[str, datetime]] = []
    for name in RESET_WATCH:
        bucket = buckets.get(name)
        previous = seen.get(name)
        if bucket is None or not isinstance(previous, datetime):
            continue
        start = bucket.window_start
        if not isinstance(start, datetime):
            continue
        try:
            shift = (start - previous).total_seconds() / 3600.0
        except (TypeError, OverflowError, ValueError):
            continue
        length = _finite(bucket.window_hours,
                         float(BUCKETS.get(name, {}).get("period_hours",
                                                         WEEK_HOURS)))
        if shift >= max(length * RESET_SHIFT_FRACTION, RESET_SHIFT_MIN_HOURS):
            rolled.append((name, start))
    return rolled


def _hours_to_reset(resets_at: Any, period_hours: float, now: datetime) -> float:
    if not isinstance(resets_at, datetime):
        return period_hours / 2
    try:
        hours = (resets_at - now).total_seconds() / 3600.0
    except TypeError:
        return period_hours / 2
    return min(max(hours, 0.05), period_hours)


def bucket_goal(cfg: Config, name: str) -> float:
    """The utilization each bucket is paced to land on.

    weekly      the weekly goal (state/goal.json, else config.json)
    fable       `fable_goal`, defaulting to the weekly goal - the Fable window
                is the executive's own budget and the operator sets one number
                unless they say otherwise
    five_hour   the idle five-hour guard, i.e. the ceiling the scheduler
                already refuses to launch past
    """
    from .goal import GOAL_FALLBACK, clamp_goal, read_goal

    try:
        weekly = read_goal(cfg)
    except (TypeError, ValueError):
        weekly = GOAL_FALLBACK
    if name == WEEKLY:
        return weekly
    if name == FABLE:
        raw = getattr(cfg, "fable_goal", None)
        if raw is None:
            return weekly
        try:
            return clamp_goal(_finite(raw, weekly))
        except (TypeError, ValueError):
            return weekly
    guard = _finite(getattr(cfg, "five_hour_guard_idle", 0.95), 0.95)
    return min(max(guard, 0.05), 1.0)


def bucket_stop(cfg: Config, name: str, goal: float) -> float:
    """The hard stop for a bucket: fable 97%, the others their own goal/guard."""
    stop = BUCKETS.get(name, {}).get("stop")
    return float(stop) if isinstance(stop, (int, float)) else goal


def _row(bucket: Any, key: str) -> Any:
    """One field off a Bucket or off the dict `Bucket.to_dict` wrote."""
    if isinstance(bucket, dict):
        value = bucket.get(key)
        return parse_iso(value) if key == "resets_at" else value
    return getattr(bucket, key, None)


def apply_bucket_stops(cfg: Config, buckets: Any,
                       now: datetime | None = None) -> str | None:
    """The Fable hard stop: at 97% the allocator parks dispatch itself.

    Until today this was a rule the director applied by hand - the loop knew
    the threshold (`FABLE_STOP`, reported in every bucket row) and stopped
    nothing. It writes the switch now, with reason `fable_bucket` and `until`
    set to the Fable window's own reset, which is what lets
    `control.maybe_resume` start the loop again at that reset with nobody at
    the keyboard.

    Returns the line the loop logs, or None. Two things hold it back: dispatch
    already being stopped (whatever parked it, this must not overwrite the
    reason), and a reading whose window has already expired - a Fable
    utilization from a window that reset ten minutes ago says nothing about the
    one running now, and stopping on it would park the loop for a whole week.

    Never raises: it is called from inside the tick.
    """
    now = now or utcnow()
    try:
        from .control import FABLE_BUCKET, STOPPED, read_record, write_control

        row = (buckets or {}).get(FABLE)
        if row is None:
            return None
        util = _finite(_row(row, "utilization"), math.nan)
        stop = _finite(_row(row, "stop"), math.nan)
        resets = _row(row, "resets_at")
        if not math.isfinite(util) or not math.isfinite(stop) or util < stop:
            return None
        if isinstance(resets, datetime) and resets <= now:
            return None
        if read_record(cfg)["dispatch"] == STOPPED:
            return None
        write_control(cfg, STOPPED, reason=FABLE_BUCKET, until=resets, now=now)
        tail = (f"; resumes {reset_label(resets, cfg)}"
                if isinstance(resets, datetime) else "")
        return (f"fable bucket {util:.0%} >= hard stop {stop:.0%}; "
                f"dispatch stopped{tail}")
    except Exception:  # pragma: no cover - the poll must survive anything
        return None


def read_buckets(cfg: Config, snap: Any = None, now: datetime | None = None,
                 history: Any = None) -> dict[str, Bucket]:
    """Every bucket's utilization, burn rate, time to reset and pace. Never raises.

    `snap` is this poll's reading when there is one; the history file supplies
    the rates either way, and the newest history point stands in for a poll
    that could not fetch.
    """
    now = now or utcnow()
    try:
        from .usage import UsageHistory

        snaps = (history or UsageHistory(cfg)).load_recent(hours=HISTORY_HOURS)
    except (OSError, ValueError, TypeError, AttributeError):
        snaps = []
    if snap is not None:
        # This poll's own reading may not be in the file yet (the loop appends
        # before the tick, but a caller with a snapshot in hand is authoritative).
        stamp = getattr(snap, "fetched_at", None)
        if not any(getattr(s, "fetched_at", None) == stamp for s in snaps):
            snaps = list(snaps) + [snap]
    out: dict[str, Bucket] = {}
    warmup = warmup_hours(cfg)
    horizon = max_horizon(cfg)
    for name in BUCKET_ORDER:
        spec = BUCKETS[name]
        period = float(spec.get("period_hours", WEEK_HOURS))
        points = _points(snaps, name, now)
        window = _window_of(snap, name) if snap is not None else None
        if window is None and points:
            # No live reading for this bucket (an account with no Fable window,
            # or a failed fetch): the newest history point is the truth.
            latest = [s for s in snaps
                      if _window_of(s, name) is not None
                      and getattr(s, "fetched_at", now) <= now]
            window = _window_of(latest[-1], name) if latest else None
        util = _finite(getattr(window, "utilization", None), 0.0)
        resets_at = getattr(window, "resets_at", None)
        goal = bucket_goal(cfg, name)
        long_rate, span = _rate_long(points, now)
        # Where this window OPENED: its reset less its own length, the length
        # taken from the previous reset stamp this bucket has shown.
        length = _window_length(_reset_stamps(snaps, name, now), period)
        start = _window_start(resets_at, length)
        elapsed = (None if start is None
                   else max(0.0, (now - start).total_seconds() / 3600.0))
        out[name] = Bucket(
            name=name,
            utilization=min(max(util, 0.0), 1.5),
            hours_to_reset=_hours_to_reset(resets_at, period, now),
            goal=goal,
            stop=bucket_stop(cfg, name, goal),
            resets_at=resets_at if isinstance(resets_at, datetime) else None,
            rate_1h=_rate(points, RATE_MINUTES_FAST, now),
            rate_3h=_rate(points, RATE_MINUTES_SLOW, now),
            rate_long=long_rate,
            span_hours=span,
            window_start=start,
            window_hours=length,
            elapsed_hours=elapsed,
            warmup_hours=warmup,
            max_horizon_hours=horizon if horizon > 0 else None,
        )
    return out


def pace_phrase(bucket: Bucket | None) -> str:
    """"fable 6% ahead of pace at reset Fri 07:00", the head of every reason.

    When the forecast stopped short of the reset it says which horizon it used
    instead - "over the next 48h (reset Thu 07:00)" - because "at reset" would
    otherwise name a moment the number was never carried to.
    """
    if bucket is None:
        return "no reading"
    label = BUCKETS.get(bucket.name, {}).get("label", bucket.name)
    ahead = bucket.ahead_by
    if ahead is None:
        return f"{label} pace unknown (no burn rate yet)"
    where = "ahead of" if ahead >= 0 else "behind"
    if bucket.capped:
        return (f"{label} {abs(ahead):.0%} {where} pace over the next "
                f"{bucket.horizon_hours:.0f}h "
                f"(reset {reset_label(bucket.resets_at)})")
    return (f"{label} {abs(ahead):.0%} {where} pace at reset "
            f"{reset_label(bucket.resets_at)}")


def fable_tiers(graph: Any) -> list[str]:
    """The tiers whose model is a Fable model, i.e. the ones on that bucket."""
    from .graph import TIERS, tiers_of

    out: list[str] = []
    for tier, block in zip(TIERS, tiers_of(graph)):
        if "fable" in str(block.get("model") or "").lower():
            out.append(tier)
    return out


# ------------------------------------------------------ the allocated graph

@dataclass
class Allocation:
    """The graph in force, beside the ceiling it was allocated from."""

    graph: dict[str, dict[str, Any]]
    configured: dict[str, dict[str, Any]]
    step: int = 0
    advisory_effort: str = EFFORT_FULL
    fork_cooldown_seconds: float = 0.0
    worker_count: int = 0
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    override: bool = False
    buckets: dict[str, Any] = field(default_factory=dict)
    generated_at: str | None = None

    @property
    def differs(self) -> bool:
        """True when the allocated graph is not the configured one."""
        return self.graph != self.configured

    def counts(self) -> dict[str, tuple[int, int]]:
        """{tier: (allocated count, configured count)} for the panel's columns."""
        from .graph import TIERS

        out: dict[str, tuple[int, int]] = {}
        for tier in TIERS:
            alloc = self.graph.get(tier) or {}
            conf = self.configured.get(tier) or {}
            out[tier] = (int(alloc.get("count", 0) or 0),
                         int(conf.get("count", 0) or 0))
        return out

    def top_reason(self) -> str:
        if self.override:
            return "FULL THROTTLE (manual override)"
        return self.reasons[0] if self.reasons else ""

    def label(self) -> str:
        """"step 1 - advisory x2 (cfg x3)", the short form for a status line."""
        from .graph import TIERS

        if self.override:
            return f"manual override (workers x{self.worker_count})"
        parts = [f"step {self.step}"]
        counts = self.counts()
        for tier in TIERS:
            allocated, configured = counts[tier]
            if allocated != configured:
                parts.append(f"{tier} x{allocated} (cfg x{configured})")
        if self.advisory_effort != EFFORT_FULL:
            parts.append(f"effort {self.advisory_effort}")
        return " - ".join(parts)

    def line(self) -> str:
        """The one line the panel and `tracker.py status` print.

        The rung and the top reason, and not the count deltas: those are drawn
        on the rungs themselves as "cfg xN", and repeating them here only cost
        the reason its room on a 300px panel.
        """
        if self.override:
            return f"ALLOCATION {self.label()}"
        reason = self.top_reason()
        return (f"ALLOCATION step {self.step}: {reason}" if reason
                else f"ALLOCATION {self.label()}")

    def to_dict(self) -> dict[str, Any]:
        from .graph import ADVISORY

        advisory = self.graph.get(ADVISORY) or {}
        return {
            "step": self.step,
            "worker_count": self.worker_count,
            "advisory_count": int(advisory.get("count", 0) or 0),
            "advisory_effort": self.advisory_effort,
            "advisory_model": advisory.get("model"),
            "fork_cooldown_seconds": self.fork_cooldown_seconds,
            "override": self.override,
        }


def base_cooldown(cfg: Any) -> float:
    """The configured fork re-arm cooldown, before any ladder multiplier.

    `cli.FORK_COOLDOWN_SECONDS` is the same number; it is not imported here
    because `cli` pulls in the dispatcher, and this module is reached from
    `load_config`.
    """
    return max(0.0, _finite(getattr(cfg, "fork_cooldown_seconds",
                                    FORK_COOLDOWN_DEFAULT),
                            float(FORK_COOLDOWN_DEFAULT)))


def step_cooldown(cfg: Any, step: int) -> float:
    """The fork re-arm cooldown at a rung: base x factor, capped at the ceiling.

    The top rung is the executive-cadence-only one, so it sits at the ceiling
    outright rather than at a multiple of the base.
    """
    base = base_cooldown(cfg)
    ceiling = max_cooldown(cfg)
    factor = COOLDOWN_FACTOR.get(clamp_step(step), 1.0)
    if factor is None:
        return ceiling if ceiling > 0 else base
    wanted = base * factor
    return min(wanted, ceiling) if ceiling > 0 else wanted


def clamp_step(value: Any) -> int:
    try:
        step = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(max(step, 0), MAX_STEP)


def _build(cfg: Config, graph: dict[str, dict[str, Any]], step: int,
           worker_count: int | None, override: bool) -> Allocation:
    """Apply the ladder to the configured graph. Pure, and it never raises.

    Cumulative rungs, cheapest given up first, and the executive's model is
    never one of them:

        1  advisory count -1 (floor: allocation.min_advisory)
        2  advisory review effort high -> medium
        3  fork re-arm cooldown x2
        4  advisory onto the workers' primary model, cooldown x4
        5  advisory at the floor, cooldown at allocation.max_fork_cooldown_seconds

    Rung 4 is the only one that touches a model, and it is checked against the
    superiority rule before it is applied: advisory takes the WORKERS' model,
    which is at or below it by definition, so `executive >= advisory >= workers`
    still holds - unless the configured graph already broke it, in which case
    the rung is skipped rather than made worse.
    """
    from .graph import (
        ADVISORY,
        EXECUTIVE,
        SURGE_MAX,
        TIERS,
        WORKERS,
        may_fall_back,
        model_rank,
        tiers_of,
    )

    blocks = {tier: dict(block) for tier, block in zip(TIERS, tiers_of(graph))}
    configured = {tier: dict(block) for tier, block in blocks.items()}
    step = clamp_step(step)
    effort = EFFORT_FULL
    floor_adv = min_advisory(cfg)

    if override:
        # The manual override wins whole: the configured graph, with the lanes
        # pinned to its own surge budget. The ladder keeps tracking underneath
        # (evaluate still measures and logs) but nothing here is applied.
        pinned = int(blocks[WORKERS].get("surge_count",
                                        blocks[WORKERS].get("count", 1)) or 1)
        blocks[WORKERS]["count"] = pinned
        return Allocation(graph=blocks, configured=configured, step=step,
                          advisory_effort=EFFORT_FULL,
                          fork_cooldown_seconds=base_cooldown(cfg),
                          worker_count=pinned, override=True)

    if step >= 1:
        blocks[ADVISORY]["count"] = max(
            floor_adv, int(blocks[ADVISORY].get("count", 1) or 1) - 1)
    if step >= 2:
        effort = EFFORT_REDUCED
    if step >= 4:
        worker_model = str(blocks[WORKERS].get("model") or "")
        exec_model = blocks[EXECUTIVE].get("model")
        # Never a promotion, and never a step that breaks the order rule: only
        # onto a ranked worker model that the executive still outranks.
        if (worker_model
                and model_rank(worker_model, cfg) != 0
                and model_rank(exec_model, cfg) >= model_rank(worker_model, cfg)):
            blocks[ADVISORY]["model"] = worker_model
            if not may_fall_back(worker_model,
                                 blocks[ADVISORY].get("fallback"), cfg):
                # The advisory fallback would now outrank its own primary; take
                # the workers' fallback, which by construction does not.
                blocks[ADVISORY]["fallback"] = blocks[WORKERS].get("fallback")
    if step >= 5:
        blocks[ADVISORY]["count"] = floor_adv

    cooldown = step_cooldown(cfg, step)

    lanes = int(blocks[WORKERS].get("count", 1) or 1)
    if worker_count is not None:
        surge = int(blocks[WORKERS].get("surge_count", lanes) or lanes)
        lanes = min(max(int(worker_count), 1), max(surge, 1), SURGE_MAX)
    blocks[WORKERS]["count"] = lanes
    blocks[WORKERS]["surge_count"] = max(
        int(blocks[WORKERS].get("surge_count", lanes) or lanes), lanes)
    return Allocation(graph=blocks, configured=configured, step=step,
                      advisory_effort=effort, fork_cooldown_seconds=cooldown,
                      worker_count=lanes, override=False)


def worker_target(cfg: Config, weekly: Bucket | None, five_hour: Bucket | None,
                  workers: dict[str, Any],
                  running: Any = None) -> tuple[int, str | None]:
    """Lane count for the weekly bucket: (count, why it moved).

    Between `allocation.min_workers` and the graph's own `surge_count`, chosen
    so `expected_at_reset` lands on the goal - which is what makes the surge
    automatic and the button unnecessary:

        required  = (goal - utilization) / hours_to_reset
        per_lane  = rate / lanes actually running
        lanes     = ceil(required / per_lane)

    `running` is the standing allocated count (`decision["worker_count"]`), NOT
    the configured ceiling, and that distinction is the whole of the loop's
    stability. The rate was produced by the lanes that were running; dividing
    it by the ceiling instead over-states the per-lane cost by exactly
    `running / configured` and understates the answer by its inverse, so every
    time the allocator moves off the ceiling - its entire purpose - the next
    poll is handed a wrong divisor and swings back. Divided by what actually
    ran, `ceil(required * running / rate)` is independent of `running`: the
    controller is deadbeat and lands on its fixed point in one poll.

    `configured` and `surge` survive only as the clamp and as the "cfg xN"
    wording. With no usable rate the configured count is held: a forecast built
    on rounding noise is worse than the number the operator set - and a rate
    whose window is still inside `allocation.warmup_hours` (`Bucket.fresh`) is
    unusable for the same reason, so inside the warm-up the lanes may still
    RISE but never fall below what is running or configured. That is the other
    half of the 2026-09-04 07:51 CT failure: the rung pinned at 5 and the lanes
    pinned at their floor of 2, both off fifty minutes of a fresh week.
    """
    configured = int(workers.get("count", 1) or 1)
    surge = max(int(workers.get("surge_count", configured) or configured),
                configured)
    low = min(min_workers(cfg), surge)
    standing = configured
    if running is not None and not isinstance(running, bool):
        try:
            candidate = int(running)
        except (TypeError, ValueError, OverflowError):
            candidate = configured
        standing = max(1, candidate)
    if weekly is None:
        return configured, None
    rate = weekly.rate
    if rate is None or rate < MIN_RATE:
        return configured, None
    required = weekly.required
    if required <= 0:
        want = low
    else:
        per_lane = rate / max(standing, 1)
        want = int(math.ceil(required / per_lane)) if per_lane > 0 else standing
    want = min(max(want, low), surge)
    # The window-age rule the ladder is under, applied to the lanes. The count
    # standing when a window opens is the PREVIOUS window's throttle, and the
    # rate that would cut it further is a measurement of the minutes since the
    # reset; cutting on it throttles hardest when the budget is freshest, which
    # is the whole defect. Raising is never the failure being guarded against,
    # so only the CUT is held - and the five-hour clamp below still overrides
    # this, because that guard is the loop's hard brake.
    floor_fresh = min(max(standing, configured), surge)
    warm = bool(getattr(weekly, "fresh", False)) and want < floor_fresh
    if warm:
        want = floor_fresh
    if five_hour is not None:
        # The five-hour guard is the loop's own hard brake (it produces
        # `blocked`); forecast to trip it, the allocator must not be the thing
        # raising lanes into it. Clamped to what is RUNNING, not to the
        # ceiling: standing at the floor of 2 with a ceiling of 10, a clamp at
        # the ceiling still authorises a five-fold raise into the window the
        # scheduler is about to answer with `blocked`.
        expected = five_hour.expected_at_reset
        if expected is not None and expected >= five_hour.goal:
            want = min(want, standing)
    if want == standing:
        return standing, None
    head = pace_phrase(weekly)
    tail = (f" [cfg x{configured}]" if standing != configured else "")
    if warm and want > standing:
        # Not an auto surge: the lanes the previous window ended throttled to,
        # handed back because nothing measured in this one can justify them.
        # `want` is the configured count here by construction, so the "cfg xN"
        # tail every other reason carries would only repeat the number.
        age = _finite(getattr(weekly, "age_minutes", None), 0.0)
        return want, (f"{head}; the weekly window is only {age:.0f}m old "
                      f"(warm-up {warmup_hours(cfg) * 60.0:.0f}m) -> workers "
                      f"{standing}->{want} (the configured count), too early "
                      "to read the window's pace to hold lanes down")
    if want > standing:
        return want, (f"{head} -> workers {standing}->{want} "
                      f"(auto surge, ceiling {surge}){tail}")
    return want, f"{head} -> workers {standing}->{want} (floor {low}){tail}"


# ------------------------------------------------------ state/allocation.json

def read_state(cfg: Config) -> dict[str, Any] | None:
    """state/allocation.json as a dict, or None. Never raises."""
    try:
        data = json.loads(cfg.allocation_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def read_decision(cfg: Config) -> dict[str, Any]:
    """The standing decision: step, poll counters and the lane count."""
    state = read_state(cfg) or {}
    raw = state.get("decision")
    raw = raw if isinstance(raw, dict) else {}
    # None, not the configured count: "no decision yet" has to stay
    # distinguishable from "decided to run the configured count", and a
    # `worker_count` that is not a finite number is no decision at all - taking
    # it as 1 would park the loop on one lane because a file was mangled.
    lanes = raw.get("worker_count")
    if isinstance(lanes, bool) or not isinstance(lanes, (int, float)) \
            or not math.isfinite(lanes):
        lanes = None
    else:
        lanes = max(1, int(lanes))
    # The window openings this decision was made against, so the next poll can
    # see a bucket roll over. Anything unparseable is simply absent, which
    # reads as "no previous window" and reports no rollover.
    starts: dict[str, datetime] = {}
    raw_starts = raw.get("window_starts")
    if isinstance(raw_starts, dict):
        for key, value in raw_starts.items():
            moment = parse_iso(value)
            if moment is not None:
                starts[str(key)] = moment
    return {
        "step": clamp_step(raw.get("step", 0)),
        "up_polls": max(0, int(_finite(raw.get("up_polls", 0), 0))),
        "down_polls": max(0, int(_finite(raw.get("down_polls", 0), 0))),
        "worker_count": lanes,
        # When the rung last moved, for the dwell. None ("never") lets the
        # first move through, which is what a fresh install wants.
        "last_step_at": parse_iso(raw.get("last_step_at")),
        "window_starts": starts,
    }


def write_state(cfg: Config, allocation: Allocation,
                buckets: dict[str, Bucket], decision: dict[str, Any],
                now: datetime | None = None) -> dict[str, Any]:
    """Persist the poll's buckets, decision and reasons. Returns what was written."""
    now = now or utcnow()
    last_step = decision.get("last_step_at")
    starts = decision.get("window_starts")
    starts = starts if isinstance(starts, dict) else {}
    payload = {
        "buckets": {name: bucket.to_dict() for name, bucket in buckets.items()},
        "decision": {**allocation.to_dict(),
                     "up_polls": int(decision.get("up_polls", 0)),
                     "down_polls": int(decision.get("down_polls", 0)),
                     "last_step_at": (last_step.isoformat()
                                      if isinstance(last_step, datetime)
                                      else last_step),
                     "window_starts": {
                         str(name): (value.isoformat()
                                     if isinstance(value, datetime) else value)
                         for name, value in starts.items()}},
        "reasons": list(allocation.reasons),
        "notes": list(allocation.notes),
        "generated_at": now.isoformat(),
    }
    _write_atomic(cfg.allocation_file, json.dumps(payload, indent=2))
    return payload


def allocate(cfg: Config, graph: dict[str, dict[str, Any]] | None = None,
             ) -> Allocation:
    """The graph in force right now, from the standing decision. Cheap and safe.

    Reads only state/allocation.json, so `graph.apply_graph`, the fork prompt
    and every overlay refresh can call it without parsing history. With no
    decision on disk it returns the configured graph unchanged, which is what
    keeps a fresh install (and every caller that never polls) on the operator's
    own numbers.
    """
    from .graph import read_graph

    try:
        graph = read_graph(cfg) if graph is None else graph
        decision = read_decision(cfg)
        state = read_state(cfg) or {}
        allocation = _build(cfg, graph, decision["step"],
                            decision["worker_count"], throttle_active(cfg))
        reasons = state.get("reasons")
        notes = state.get("notes")
        allocation.reasons = [str(r) for r in reasons] if isinstance(reasons, list) else []
        allocation.notes = [str(n) for n in notes] if isinstance(notes, list) else []
        buckets = state.get("buckets")
        allocation.buckets = buckets if isinstance(buckets, dict) else {}
        stamp = state.get("generated_at")
        allocation.generated_at = str(stamp) if isinstance(stamp, str) else None
        return allocation
    except Exception:  # pragma: no cover - the last-resort guard
        # This runs inside load_config and every overlay frame; a graph that
        # cannot be allocated must still be a graph.
        from .graph import TIERS, tiers_of

        blocks = {t: dict(b) for t, b in zip(TIERS, tiers_of(graph))}
        return Allocation(graph=blocks,
                          configured={t: dict(b) for t, b in blocks.items()},
                          worker_count=int(blocks["workers"].get("count", 1) or 1))


def allocated_graph(cfg: Config,
                    graph: dict[str, dict[str, Any]] | None = None,
                    ) -> dict[str, dict[str, Any]]:
    return allocate(cfg, graph).graph


def evaluate(cfg: Config, snap: Any = None, now: datetime | None = None,
             history: Any = None) -> Allocation:
    """One poll of the allocator: measure, move the ladder, persist, return it.

    Four gates, and a CLIMB needs all four:

      pace       `Bucket.pace_state` in RATE space, with a dead band floored at
                 the measurement quantum. The utilization-space band this
                 replaced was narrower than the grid the readings land on, so
                 no reachable rate sat inside it and every poll read either
                 "climb" or "give back".
      hysteresis `UP_POLLS` consecutive polls ahead of pace to give something
                 up, `DOWN_POLLS` behind to take it back.
      dwell      `allocation.min_dwell_seconds` since the last move. Hysteresis
                 counts polls, not signal; a rate saturated in one direction
                 for ten polls running satisfies it ten times over, so the
                 ladder needs a bound in wall-clock time as well. The counter
                 stays armed while the dwell runs, so the rung moves on the
                 first poll after it and no agreement is thrown away.
      warm-up    `allocation.warmup_hours` of window age (and the same gate
                 holds the LANES up in `worker_target`, which is the other half
                 of the same throttle). A rate anchored at a
                 window that opened forty minutes ago measures forty minutes,
                 and the ladder must not throttle hardest when the budget is
                 freshest. Giving a rung BACK is never held back by it, and the
                 counter stays armed exactly as it does under the dwell.

    And ahead of all four, the reset drop: a window whose start has moved has
    ROLLED, so the new window begins at rung 0 with the counters cleared rather
    than inheriting whatever the last one ended on. A rung standing while the
    window is still inside its warm-up is dropped on the same grounds even when
    no poll saw the rollover (a restart, a fetch gap): under the warm-up rule
    such a rung cannot have been earned in the window it is standing in.

    Never raises: it is called from inside the tick, and a broken bucket has to
    degrade to "hold what stands".
    """
    from .graph import ADVISORY, read_graph, tiers_of

    now = now or utcnow()
    try:
        graph = read_graph(cfg)
        buckets = read_buckets(cfg, snap, now, history)
        decision = read_decision(cfg)
        override = throttle_active(cfg)
        notes: list[str] = []
        reasons: list[str] = []
        # The warm-up hold and the reset drop explain the RUNG itself rather
        # than a change to it, so they lead the reasons the panel and the
        # fork's brief are handed - they are the answer to "why is it there".
        gates: list[str] = []

        fable = buckets.get(FABLE)
        step = decision["step"]
        up, down = decision["up_polls"], decision["down_polls"]
        ahead_step = alloc_setting(cfg, "ahead_step")
        behind_step = alloc_setting(cfg, "behind_step")
        on_fable = fable_tiers(graph)
        state = (fable.pace_state(ahead_step, behind_step)
                 if fable is not None else None)

        # The dwell, measured from the last actual move. `None` (never moved)
        # is not "0 seconds ago": the first rung must not be held back.
        dwell = min_dwell(cfg)
        last_step_at = decision["last_step_at"]
        if isinstance(last_step_at, datetime):
            held_for = (now - last_step_at).total_seconds()
        else:
            held_for = None
        may_move = held_for is None or held_for >= dwell

        def _wait_note(direction: str) -> None:
            remaining = max(0.0, dwell - (held_for or 0.0))
            notes.append(f"{pace_phrase(fable)}; {direction} armed but the rung "
                         f"has stood {(held_for or 0.0) / 60.0:.0f}m of "
                         f"{dwell / 60.0:.0f}m - holding {remaining / 60.0:.0f}m "
                         "more")

        # A window that has ROLLED since the last poll: the rung the previous
        # window ended on says nothing about this one, and inheriting it is
        # what left the ladder at 5 of 5 across the 07:00 reset on 2026-09-04.
        # Checked before the pace, because a clean slate is not a pace verdict.
        rolled = _rolled_over(buckets, decision["window_starts"])
        # ... and the same conclusion reached from the rung itself, for the
        # reset no poll was there to see: a restart, a fetch gap or the upgrade
        # that added this rule. A rung can no longer be EARNED inside the
        # warm-up, so a rung standing while the window is still that young was
        # inherited from the window before it, whatever the file remembers.
        inherited = step > 0 and fable is not None and fable.fresh and not rolled

        if rolled or inherited:
            tail = (f"ladder back to rung 0 from {step}" if step > 0
                    else "ladder held at rung 0")
            for name, start in rolled:
                gates.append(
                    f"{BUCKETS.get(name, {}).get('label', name)} window reset "
                    f"at {reset_label(start, cfg)}{_zone(cfg)}; {tail} "
                    "(a new window starts on a clean slate)")
            if inherited:
                gates.append(
                    f"the fable window is only {fable.age_minutes or 0.0:.0f}m "
                    f"old (reset {reset_label(fable.window_start, cfg)}"
                    f"{_zone(cfg)}), so rung {step} was inherited from the "
                    f"window before it - {tail}")
            if step > 0:
                # The drop IS a move, so it stamps the dwell: the ladder does
                # not climb straight back out of the reset it just cleared.
                last_step_at = now
            step, up, down = 0, 0, 0
        elif state is None:
            notes.append("no Fable burn rate yet; holding the configured graph")
            up = down = 0
        elif not on_fable:
            notes.append("no tier runs a Fable model; the Fable ladder is idle")
            up = down = 0
        elif state == "ahead":
            # At the top of the ladder there is nothing left to give up, so the
            # counter is parked rather than left to climb forever.
            up, down = (0 if step >= MAX_STEP else up + 1), 0
            if fable is not None and fable.fresh:
                # The warm-up hold. The agreement is kept armed, exactly as it
                # is under the dwell, so the first poll past the warm-up moves.
                age = fable.age_minutes or 0.0
                up = min(up, UP_POLLS)
                gates.append(
                    f"{pace_phrase(fable)}; the fable window is only "
                    f"{age:.0f}m old (warm-up "
                    f"{warmup_hours(cfg) * 60.0:.0f}m) - holding rung {step}, "
                    "too early to read the window's pace")
            elif up >= UP_POLLS and step < MAX_STEP:
                if may_move:
                    step, up = step + 1, 0
                    last_step_at = now
                else:
                    up = UP_POLLS      # stay armed; the dwell is the only brake
                    _wait_note("climb")
        elif state == "behind":
            down, up = (0 if step <= 0 else down + 1), 0
            if down >= DOWN_POLLS and step > 0:
                if may_move:
                    step, down = step - 1, 0
                    last_step_at = now
                else:
                    down = DOWN_POLLS
                    _wait_note("give back")
        else:
            notes.append(f"{pace_phrase(fable)}; inside the band "
                         f"(+/-{fable.tolerance(ahead_step) * 100:.3f}%/h "
                         f"around {fable.required * 100:.3f}%/h), holding")
            up = down = 0

        workers = tiers_of(graph)[2]
        # The lanes that produced the rate, not the ceiling: see `worker_target`.
        lanes, lane_reason = worker_target(cfg, buckets.get(WEEKLY),
                                           buckets.get(FIVE_HOUR), workers,
                                           decision["worker_count"])

        allocation = _build(cfg, graph, step, lanes, override)
        allocation.buckets = {n: b.to_dict() for n, b in buckets.items()}
        allocation.generated_at = now.isoformat()

        # Reasons name the change and the pace that caused it; notes are the
        # diagnostics, and only the reasons ever reach the fork's brief.
        head = pace_phrase(fable)
        counts = allocation.counts()
        adv_alloc, adv_conf = counts[ADVISORY]
        if not override:
            # Why the rung is where it is, before what it changed.
            reasons.extend(gates)
            if adv_alloc != adv_conf:
                reasons.append(f"{head} -> advisory {adv_conf}->{adv_alloc}")
            if allocation.advisory_effort != EFFORT_FULL:
                reasons.append(f"{head} -> advisory effort "
                               f"{EFFORT_FULL}->{allocation.advisory_effort}")
            base = base_cooldown(cfg)
            if allocation.fork_cooldown_seconds > base:
                reasons.append(
                    f"{head} -> fork cadence {base:.0f}s->"
                    f"{allocation.fork_cooldown_seconds:.0f}s")
            allocated_model = allocation.graph[ADVISORY].get("model")
            if allocated_model != allocation.configured[ADVISORY].get("model"):
                reasons.append(
                    f"{head} -> advisory model "
                    f"{allocation.configured[ADVISORY].get('model')}->"
                    f"{allocated_model}")
            if lane_reason:
                reasons.append(lane_reason)
        else:
            notes.append("FULL THROTTLE (manual override) is on: the allocator "
                         f"is ignored, workers pinned to x{allocation.worker_count}")
        allocation.reasons = reasons
        allocation.notes = notes
        # The window openings this poll saw, over the ones it was handed: a
        # bucket that could not be read this poll keeps its remembered start
        # rather than losing it, so a fetch gap cannot swallow a rollover.
        starts = dict(decision["window_starts"])
        starts.update({name: bucket.window_start
                       for name, bucket in buckets.items()
                       if isinstance(bucket.window_start, datetime)})
        decision = {"step": step, "up_polls": up, "down_polls": down,
                    "worker_count": allocation.worker_count,
                    "last_step_at": last_step_at,
                    "window_starts": starts}
        write_state(cfg, allocation, buckets, decision, now)
        return allocation
    except Exception:  # pragma: no cover - the poll must survive anything
        return allocate(cfg)


def tick_notes(before: Allocation, after: Allocation) -> list[str]:
    """What the loop logs about this poll's allocation, and nothing more."""
    lines: list[str] = []
    if after.override and not before.override:
        lines.append("full throttle (manual override) on: the allocator is "
                     f"ignored, workers pinned to x{after.worker_count}")
    if after.step != before.step:
        direction = "up" if after.step > before.step else "down"
        lines.append(f"allocation step {before.step} -> {after.step} ({direction}: "
                     f"{STEP_LABELS.get(after.step, '?')})"
                     + (f" - {after.top_reason()}" if after.top_reason() else ""))
    elif after.worker_count != before.worker_count and not after.override:
        lines.append(f"allocation workers x{before.worker_count} -> "
                     f"x{after.worker_count}"
                     + (f" - {after.top_reason()}" if after.top_reason() else ""))
    return lines


# The bucket table's columns, so the header and the rows are laid out from one
# place and cannot drift apart.
BUCKET_COLUMNS = (("bucket", -10), ("now", 6), ("rate/h", 9), ("need/h", 9),
                  ("at reset", 10), ("goal", 7), ("pace", 10))


def _cell(text: str, width: int) -> str:
    return text.ljust(-width) if width < 0 else text.rjust(width)


def format_buckets(allocation: Allocation, cfg: Any = None) -> list[str]:
    """The bucket table `tracker.py alloc` prints: pace, forecast and reset.

    `rate/h` beside `need/h` and their difference, because the ladder is gated
    on exactly that comparison; `at reset` is the forecast the two of them
    imply, and it is the readable summary rather than the decision.

    A window whose reset is further off than `allocation.max_horizon_hours` is
    forecast only to that horizon, and the footer says so: "at reset" over a
    week of remaining budget would otherwise name a number no rate was carried
    to, and the age of each window is what says how much the rate is worth.
    """
    from .clock import label

    lines = ["  " + "".join(_cell(name, width)
                            for name, width in BUCKET_COLUMNS)
             + f"  resets at ({label(cfg)})"]
    capped: list[str] = []
    ages: list[str] = []
    cap_hours = math.nan
    for name in BUCKET_ORDER:
        row = allocation.buckets.get(name)
        if not isinstance(row, dict):
            continue
        rate = row.get("rate")
        expected = row.get("expected_at_reset")
        gap = row.get("pace_gap")
        cells = (
            name,
            f"{_finite(row.get('utilization'), 0.0):.0%}",
            "n/a" if rate is None else f"{_finite(rate, 0.0) * 100:.2f}%",
            f"{_finite(row.get('required_rate'), 0.0) * 100:.2f}%",
            "n/a" if expected is None else f"{_finite(expected, 0.0):.0%}",
            f"{_finite(row.get('goal'), 0.0):.0%}",
            "n/a" if gap is None else f"{_finite(gap, 0.0) * 100:+.3f}%",
        )
        lines.append("  " + "".join(
            _cell(text, width) for text, (_label, width)
            in zip(cells, BUCKET_COLUMNS))
            + f"  {reset_label(row.get('resets_at'), cfg)}")
        horizon = _finite(row.get("horizon_hours"), math.nan)
        left = _finite(row.get("hours_to_reset"), math.nan)
        if math.isfinite(horizon) and math.isfinite(left) and horizon < left:
            capped.append(f"{name} {left:.0f}h")
            cap_hours = horizon
        elapsed = _finite(row.get("elapsed_hours"), math.nan)
        if math.isfinite(elapsed):
            ages.append(f"{name} {elapsed:.1f}h"
                        + (" (warm-up)" if row.get("fresh") else ""))
    if capped and math.isfinite(cap_hours):
        lines.append(f"  at reset = now + rate x {cap_hours:.0f}h "
                     f"(allocation.max_horizon_hours; {', '.join(capped)} to "
                     "reset), need/h still targets the real reset")
    if ages:
        lines.append(f"  window age: {', '.join(ages)}")
    return lines


def status_line(cfg: Config, allocation: Allocation | None = None) -> str:
    """The single allocation line `tracker.py status` and the panel print."""
    allocation = allocate(cfg) if allocation is None else allocation
    return allocation.line()
