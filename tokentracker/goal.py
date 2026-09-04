"""Weekly goal target: the weekly utilization the operator wants to stop at.

The goal is a fraction of the weekly budget (0.05 - 1.0). `config.json` carries
the default; `state/goal.json` is the per-user override the overlay writes on a
tap, so a click never has to rewrite the checked-in config.

Every poll the loop compares the weekly window against the goal. On crossing it
drops `state/stop.json` - the file the main session watches as its stop point -
and, when nothing has parked dispatch already, flips the switch to stopped
through the control file with reason `weekly_goal` and `until` set to the
weekly window's own reset. A stop already standing is left alone, reason and
all: an operator STOP means the loop holds until a person lifts it, and
restamping it here would give it a reset to resume at.
Falling back below the goal (the weekly window rolled over) only clears the
file; the switch itself is flipped back by `control.maybe_resume` on the poll
that reaches that `until`, which is what the operator asked for: the weekly
budget, not a person, decides when the loop starts again.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from .config import Config
from .control import STOPPED, WEEKLY_GOAL, read_record, write_control
from .models import utcnow

GOAL_MIN = 0.05
GOAL_MAX = 1.0
GOAL_STEP = 0.05
GOAL_FALLBACK = 0.90
STOP_REASON = "weekly goal reached"
SOURCE_CONFIG = "config.json"
SOURCE_OVERRIDE = "state/goal.json"
# The main session parses stop.json: exactly these keys, no more, no fewer.
STOP_KEYS = ("reason", "goal", "weekly", "at")


def clamp_goal(value: float) -> float:
    """Keep the goal inside 5% - 100%; 0% would stop the loop the instant it starts.

    NaN and the infinities are refused rather than clamped: NaN survives every
    comparison as False, so a NaN goal would silently switch the stop point off
    (weekly >= nan is False at 100% too) with nothing on screen to say so.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"goal must be a finite fraction, got {value!r}")
    return min(max(number, GOAL_MIN), GOAL_MAX)


def parse_goal(text: Any) -> float:
    """Parse an operator-typed goal: 0.85, .85, 85 and "85%" all mean 85%.

    Raises ValueError on anything unparseable - "abc" and "nan" alike - so the
    CLI can complain instead of silently writing a nonsense target.
    """
    raw = str(text).strip()
    had_pct = raw.endswith("%")
    value = float(raw.rstrip("%").strip())
    if had_pct or value > 1.0:
        value /= 100.0
    return clamp_goal(value)


def _write_atomic(path, body: str) -> None:
    # Same swap-in-whole dance as control.write_control: a torn read of either
    # file would be read as "no goal" / "no stop", the wrong direction for both.
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


def _load_json(path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_goal_source(cfg: Config) -> tuple[float, str]:
    """(goal, where it came from). The override file wins when it is readable.

    Never raises. It is called once per poll from inside the run loop, from
    `status` and from every overlay refresh, so a hand-edited `config.json`
    (`"weekly_goal": null`, `"85%"`, `NaN`) has to degrade to the built-in
    default instead of taking the daemon down on its next tick.
    """
    data = _load_json(cfg.goal_file)
    if data is not None:
        raw = data.get("weekly_goal")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            # write_goal only ever stores a clamped fraction here, so a number
            # is taken at face value (2.5 is a broken 100%, not 2.5%). NaN and
            # the infinities make clamp_goal raise; that is junk, not an override.
            try:
                return clamp_goal(raw), SOURCE_OVERRIDE
            except (TypeError, ValueError):
                pass
        elif isinstance(raw, str):
            try:
                return parse_goal(raw), SOURCE_OVERRIDE
            except (TypeError, ValueError):
                pass
    # Missing, malformed, or holding junk: fall back to the configured default,
    # read the same tolerant way the CLI reads a typed goal, so config.json's
    # 90 and "85%" mean what `tracker.py goal 90` and `goal 85%` mean.
    try:
        return parse_goal(getattr(cfg, "weekly_goal", GOAL_FALLBACK)), SOURCE_CONFIG
    except (TypeError, ValueError):
        return GOAL_FALLBACK, SOURCE_CONFIG


def read_goal(cfg: Config) -> float:
    return read_goal_source(cfg)[0]


def write_goal(cfg: Config, value: float) -> float:
    """Persist the per-user override; returns the clamped goal actually written.

    Raises ValueError rather than storing a goal that is not a finite fraction
    (`tracker.py goal nan` is refused by parse_goal before it ever gets here).
    """
    goal = clamp_goal(value)
    _write_atomic(cfg.goal_file, json.dumps(
        {"weekly_goal": goal, "set_at": utcnow().isoformat()}))
    return goal


def read_stop(cfg: Config) -> dict | None:
    """The active stop record, or None when the goal has not been hit."""
    return _load_json(cfg.stop_file)


def valid_stop(record: Any) -> bool:
    """True when the record is one the main session can act on.

    The contract is the four keys and nothing else. A half-written or
    hand-edited file that merely happens to be a JSON object (`{}`) must not
    count as a stop already in place, or the loop would keep quiet about a goal
    it never actually enforced.
    """
    if not isinstance(record, dict) or set(record) != set(STOP_KEYS):
        return False
    if not isinstance(record["reason"], str) or not isinstance(record["at"], str):
        return False
    for key in ("goal", "weekly"):
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value):
            return False
    return True


def clear_stop(cfg: Config) -> bool:
    try:
        cfg.stop_file.unlink()
        return True
    except OSError:
        return False


def apply_goal_stop(cfg: Config, weekly: float, now=None,
                    resets_at=None) -> tuple[dict | None, str | None]:
    """Compare the weekly window against the goal; returns (stop record, log line).

    Crossing the goal writes state/stop.json once and stops dispatch once - a
    stop record already on disk is never rewritten, so pressing START while the
    week is still over goal keeps running instead of being re-stopped every
    poll. Dropping back below the goal only deletes the record.

    `resets_at` is the weekly window's own reset, and it becomes the `until` on
    the control record: the moment `control.maybe_resume` lifts this stop
    without anyone pressing START. The switch is only written when dispatch is
    still running - a stop already standing keeps its own reason and its own
    `until`, so an operator STOP is never converted into one that resumes.

    Never raises, and never acts on a weekly value it cannot trust: the usage
    payload is remote JSON, so `utilization` can arrive null or NaN, and
    neither may clear a standing stop (that would resume the main session as if
    the week had rolled over) nor kill the loop that has to run every poll.
    """
    now = now or utcnow()
    goal = read_goal(cfg)
    try:
        weekly = float(weekly)
    except (TypeError, ValueError):
        weekly = math.nan
    existing = read_stop(cfg)
    salvaged = False
    if existing is not None and not valid_stop(existing):
        # Unreadable to the main session, and it would suppress the real stop
        # for the rest of the week: drop it and let this poll decide afresh.
        clear_stop(cfg)
        existing = None
        salvaged = True

    if not math.isfinite(weekly):
        # No usable reading: hold whatever the last good poll decided.
        return existing, (f"discarded unreadable {cfg.stop_file.name}"
                          if salvaged else None)

    if weekly >= goal:
        if existing is not None:
            return existing, None
        # Exactly these four keys: the main session parses this file.
        record = {
            "reason": STOP_REASON,
            "goal": goal,
            "weekly": weekly,
            "at": now.isoformat(),
        }
        _write_atomic(cfg.stop_file, json.dumps(record))
        # The switch is only flipped when nothing else has parked it, the way
        # allocator.apply_bucket_stops guards its own stop: whoever got there
        # first keeps the reason. The case that matters is the operator's own
        # STOP, which carries no `until` and is meant to hold. TD does not own
        # the weekly burn - the main session and the adopted workers spend it
        # too - so the goal can be crossed on a poll long after that STOP, and
        # restamping the record as weekly_goal would hand it the weekly reset
        # as an `until`. control.maybe_resume would then start the loop again
        # at that rollover, which is exactly the resume the operator's stop is
        # supposed to be immune to. state/stop.json is a separate signal (the
        # main session reads it, not this switch), so it is written either way.
        already = read_record(cfg)["dispatch"] == STOPPED
        if not already:
            write_control(cfg, STOPPED, reason=WEEKLY_GOAL, until=resets_at,
                          now=now)
        return record, (f"weekly goal {goal:.0%} reached (weekly {weekly:.0%}); "
                        f"dispatch {'already stopped' if already else 'stopped'}"
                        f", wrote {cfg.stop_file.name}")

    if existing is None:
        return None, (f"discarded unreadable {cfg.stop_file.name}"
                      if salvaged else None)
    clear_stop(cfg)
    # Deliberately no write_control(RUNNING) here: the switch is lifted by
    # `control.maybe_resume`, which runs earlier in the same poll and knows
    # which reason parked it. Clearing the record is this function's whole job.
    return None, (f"weekly {weekly:.0%} back under goal {goal:.0%}; "
                  f"cleared {cfg.stop_file.name}")
