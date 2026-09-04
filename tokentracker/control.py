"""The dispatch switch: who stopped it, why, and when it lifts by itself.

The state lives in a single small file so the overlay process and the run loop
stay decoupled: the overlay writes it on click, the loop reads it once per poll.
Stopping is a launch gate only - workers already running are left alone and are
still reaped/adopted normally.

`state/control.json` carries three things:

    dispatch    "running" | "stopped"
    reason      who parked it: "operator", "fable_bucket", "weekly_goal",
                "five_hour", "snapshot" - or "auto_resume:<reason>" on the
                record a resume writes
    until       ISO UTC of the bucket reset that lifts it; null for an
                operator stop, which nothing lifts but a person, and null
                again when the reset offered was not still in the future

Only `operator` is permanent. Every other reason names a budget bucket (or a
task) that ends on its own, and `maybe_resume` - called once per poll from
`cli._tick`, before anything is launched - flips the switch back the moment it
does. That is the whole point: the operator sets the goal, the buckets stop the
loop when they are spent, and nobody has to press START at 07:00 because a
window rolled over at 06:59.

A stop with no reason at all (hand-written, or left by a version of this file
that predates the field) reads as `operator`: the reading that never resumes
behind the user's back is the only safe default.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .models import Decision, parse_iso, utcnow

RUNNING = "running"
STOPPED = "stopped"
CONTROL_MODES = (RUNNING, STOPPED)

# Why dispatch is stopped.
OPERATOR = "operator"
FABLE_BUCKET = "fable_bucket"
WEEKLY_GOAL = "weekly_goal"
FIVE_HOUR = "five_hour"
SNAPSHOT = "snapshot"
REASONS = (OPERATOR, FABLE_BUCKET, WEEKLY_GOAL, FIVE_HOUR, SNAPSHOT)
# The reasons that lift by themselves. `operator` is deliberately not one.
AUTO_REASONS = (FABLE_BUCKET, WEEKLY_GOAL, FIVE_HOUR, SNAPSHOT)
# The reason written on the record a resume leaves behind, so the log and the
# next reader can see which stop it lifted.
RESUME_PREFIX = "auto_resume:"
# The control file's contract: exactly these keys.
CONTROL_KEYS = ("dispatch", "reason", "until", "changed_at")

# How far under its own threshold a bucket has to fall before a reading counts
# as "the window rolled" rather than as one percent of measurement noise.
RESUME_MARGIN = 0.05
# reason -> the bucket it was stopped for, as the panel says it.
BUCKET_LABEL = {FABLE_BUCKET: "fable", WEEKLY_GOAL: "weekly", FIVE_HOUR: "5h"}
# reason -> how the gated decision names it, so the "last decision" line in
# `status` and in state.json agrees with the red band right above it.
GATE_LABEL = {
    OPERATOR: "by operator",
    FABLE_BUCKET: "on the fable bucket",
    WEEKLY_GOAL: "on the weekly goal",
    FIVE_HOUR: "on the 5h bucket",
    SNAPSHOT: "for a snapshot pass",
}


# ------------------------------------------------------------- small helpers

def _load(path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def _utc(value: Any) -> datetime | None:
    """`value` as an aware UTC datetime, or None when it is not a usable time."""
    moment = value if isinstance(value, datetime) else parse_iso(value)
    if not isinstance(moment, datetime):
        return None
    try:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    """`value` as an ISO UTC string, or None when it is not a usable time."""
    moment = _utc(value)
    return moment.isoformat() if moment is not None else None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# -------------------------------------------------------------- read / write

def read_control(cfg: Config) -> str:
    """Current dispatch mode; "running" when the file is missing or malformed.

    Defaulting to running matters: a corrupt or half-written file must never
    silently park the whole loop.
    """
    return read_record(cfg)["dispatch"]


def read_record(cfg: Config) -> dict[str, Any]:
    """{dispatch, reason, until, changed_at} - the whole switch. Never raises.

    `until` and `changed_at` come back as datetimes (None when absent or
    unparseable), and a stop carrying no readable reason reads as `operator`.
    """
    data = _load(cfg.control_file) or {}
    mode = STOPPED if data.get("dispatch") == STOPPED else RUNNING
    raw = data.get("reason")
    reason = raw.strip() if isinstance(raw, str) and raw.strip() else None
    if mode == STOPPED and reason is None:
        reason = OPERATOR
    return {
        "dispatch": mode,
        "reason": reason,
        "until": parse_iso(data.get("until")),
        "changed_at": parse_iso(data.get("changed_at")),
    }


def write_control(cfg: Config, mode: str, reason: str | None = None,
                  until: Any = None, now: datetime | None = None) -> str:
    """Persist the dispatch mode, why, and when it lifts. Returns the mode.

    Every caller names its reason: the overlay's buttons and a hand stop are
    `operator` (which never auto-resumes), `goal.apply_goal_stop` is
    `weekly_goal`, `allocator.apply_bucket_stops` is `fable_bucket`. A stop
    with no reason given is taken as the operator's, because that is the only
    reading that cannot resume work behind the user's back.

    An `until` that is not strictly in the future is dropped rather than
    stored. `resume_due`'s clock branch fires the moment `now` reaches it, so a
    stale reset - one taken off a state/allocation.json the loop has not
    refreshed, typed before the loop was ever started, or read on a machine
    that slept through the window - would lift the stop on the very next poll
    with nobody told. Without one the stop stands until the payload shows that
    bucket really rolled, or a person lifts it, which is the safe direction.
    """
    mode = STOPPED if mode == STOPPED else RUNNING
    if mode == STOPPED:
        reason = reason.strip() if isinstance(reason, str) and reason.strip() \
            else OPERATOR
        # Null for an operator stop, always: an `until` on that record would
        # read as a promise to resume, and nothing but a person lifts it.
        moment = None if reason == OPERATOR else _utc(until)
        if moment is not None and moment <= (_utc(now) or utcnow()):
            moment = None
        until_txt = moment.isoformat() if moment is not None else None
    else:
        # A running switch has nothing to wait for; the reason is kept only so
        # "who started it" is readable ("auto_resume:weekly_goal").
        reason = reason.strip() if isinstance(reason, str) and reason.strip() \
            else None
        until_txt = None
    body = json.dumps({"dispatch": mode, "reason": reason, "until": until_txt,
                       "changed_at": utcnow().isoformat()})
    path = cfg.control_file
    tmp = path.parent / f"{path.name}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Swap the file in whole: a truncated read would fall back to RUNNING,
        # which is the wrong direction for a STOP click.
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # os.replace can lose to a reader holding the file open on Windows.
        # Dropping the click silently is worse than one racy read, so fall back
        # to a direct write rather than swallowing the failure here.
        try:
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
    return mode


def gate_decision(decision: Decision, mode: str,
                  reason: str | None = None) -> Decision:
    """Decision the dispatcher may act on, given the switch.

    Stopped drops every launch budget (cloud and local) to zero, which is what
    keeps `Dispatcher.apply` from starting anything new; apply still reaps and
    finalizes adopted work first, so nothing running is disturbed.

    `reason` names who parked it, and the sentence carries it: this line is
    what `status` prints as "last decision" and what state.json stores, and
    while a bucket stop stands it sat directly under a band reading "fable
    bucket 97%" while itself saying "by operator". No reason given still reads
    as the operator's, the same default `read_record` applies.
    """
    if mode != STOPPED:
        return decision
    who = reason.strip() if isinstance(reason, str) and reason.strip() \
        else OPERATOR
    return Decision(
        STOPPED, 0, False,
        f"Dispatch stopped {GATE_LABEL.get(who, f'({who})')} "
        f"(would be {decision.mode}); no new tasks will launch.",
        local_concurrency=0,
    )


# ------------------------------------------------------------ the thresholds

def threshold(cfg: Config, reason: str) -> float | None:
    """The utilization each stop reason was armed at. None when there is none.

    Read live rather than stored with the stop: the Fable hard stop is a
    constant, and the weekly one is the goal, which the operator may move while
    the stop stands - and the number on the panel should be the one in force.
    """
    try:
        if reason == FABLE_BUCKET:
            from .allocator import FABLE, bucket_goal, bucket_stop

            return bucket_stop(cfg, FABLE, bucket_goal(cfg, FABLE))
        if reason == WEEKLY_GOAL:
            from .goal import read_goal

            return read_goal(cfg)
        if reason == FIVE_HOUR:
            from .allocator import FIVE_HOUR as FIVE_HOUR_BUCKET
            from .allocator import bucket_goal

            return bucket_goal(cfg, FIVE_HOUR_BUCKET)
    except Exception:
        return None
    return None


def _window(snap: Any, reason: str) -> Any:
    """The usage window a stop reason is measured on, or None."""
    if snap is None:
        return None
    if reason == WEEKLY_GOAL:
        return getattr(snap, "seven_day", None)
    if reason == FIVE_HOUR:
        return getattr(snap, "five_hour", None)
    if reason == FABLE_BUCKET:
        extra = getattr(snap, "extra", None)
        if isinstance(extra, dict):
            for key, window in extra.items():
                if "fable" in str(key).lower():
                    return window
    return None


def _clear_own_stop(cfg: Config, reason: str) -> bool:
    """Delete state/stop.json, but only when it belongs to `reason`.

    The weekly goal is the one stop that writes that file. A Fable resume must
    leave a standing weekly-goal stop exactly where it is, or the main session
    would be told the week is open again on the strength of a different bucket.
    """
    if reason != WEEKLY_GOAL:
        return False
    try:
        from .goal import STOP_REASON, clear_stop, read_stop

        record = read_stop(cfg)
        if isinstance(record, dict) and record.get("reason") == STOP_REASON:
            return clear_stop(cfg)
    except Exception:
        return False
    return False


# ------------------------------------------------------------- auto-resume

def resume_due(cfg: Config, record: dict[str, Any], snap: Any = None,
               now: datetime | None = None) -> bool:
    """Whether the standing stop has lifted. Pure; never raises.

    Two ways, and either is enough:

      the clock    `now` has reached the `until` the stop was written with,
                   i.e. the bucket's own reset has passed.
      the payload  this poll's reading of that bucket has fallen more than
                   `RESUME_MARGIN` under the threshold it was stopped at AND
                   its `resets_at` has moved past the stored one - the window
                   rolled early, or the stop was written without an `until`.
    """
    now = now or utcnow()
    if record.get("dispatch") != STOPPED:
        return False
    reason = record.get("reason")
    if reason not in AUTO_REASONS or reason == SNAPSHOT:
        return False
    until = record.get("until")
    if isinstance(until, datetime) and now >= until:
        return True
    anchor = until if isinstance(until, datetime) else record.get("changed_at")
    window = _window(snap, str(reason))
    if window is None or not isinstance(anchor, datetime):
        return False
    limit = threshold(cfg, str(reason))
    util = _finite(getattr(window, "utilization", None))
    resets = getattr(window, "resets_at", None)
    return (limit is not None and util is not None
            and util < limit - RESUME_MARGIN
            and isinstance(resets, datetime) and resets > anchor)


def maybe_resume(cfg: Config, snap: Any = None, now: datetime | None = None,
                 ) -> str | None:
    """Lift a non-operator stop whose bucket has reset. The line to log, or None.

    Called once per poll from `cli._tick`, before the decision is made and
    before anything is launched, so the tick that resumes is also the tick that
    re-arms the director fork (its own cooldown still applies).

    Never raises: it runs inside the tick, and a hand-edited control file must
    not cost the loop its next poll.
    """
    now = now or utcnow()
    try:
        record = read_record(cfg)
        if record["dispatch"] != STOPPED:
            return None
        reason = record["reason"]
        if reason not in AUTO_REASONS:
            # `operator`, and anything unrecognized: a person presses START.
            return None
        if reason == SNAPSHOT:
            from .snapshot import queued_state

            if queued_state(cfg) in ("pending", "running"):
                return None
            write_control(cfg, RUNNING, reason=f"{RESUME_PREFIX}{reason}")
            return "dispatch resumed: snapshot pass finished"
        if not resume_due(cfg, record, snap, now):
            return None
        write_control(cfg, RUNNING, reason=f"{RESUME_PREFIX}{reason}")
        _clear_own_stop(cfg, str(reason))
        from .clock import fmt_local

        until = record["until"]
        when = fmt_local(until if isinstance(until, datetime) else now,
                         "%a %H:%M", cfg, with_label=True)
        return f"dispatch resumed: {BUCKET_LABEL.get(reason, reason)} reset at {when}"
    except Exception:  # pragma: no cover - the poll must survive anything
        return None


# ------------------------------------------------------------ the said line

def _stop_weekly(stop: Any) -> float | None:
    """The weekly reading state/stop.json recorded, when it has a usable one."""
    if not isinstance(stop, dict):
        return None
    return _finite(stop.get("weekly"))


def _owns_goal_stop(cfg: Config, stop: Any) -> bool:
    try:
        from .goal import STOP_REASON

        return isinstance(stop, dict) and stop.get("reason") == STOP_REASON
    except Exception:
        return False


def stopped_text(cfg: Config, record: dict[str, Any] | None = None,
                 short: bool = False, stop: Any = None) -> str | None:
    """The one line the panel's red band and `tracker.py status` both print.

    None while dispatch is running. `short` is the collapsed bar's form, which
    shares its row with the mode word and so drops the prefix.

    A standing state/stop.json outranks the switch's own reason: whoever last
    pressed STOP, the weekly goal is what dispatch is actually parked on until
    that record clears, and pressing START would not get past it.
    """
    try:
        record = read_record(cfg) if record is None else record
        if record.get("dispatch") != STOPPED:
            return None
        reason = record.get("reason") or OPERATOR
        until = record.get("until")
        if _owns_goal_stop(cfg, stop) and reason != WEEKLY_GOAL:
            reason, until = WEEKLY_GOAL, None
        tail = ""
        if isinstance(until, datetime):
            from .clock import fmt_local

            tail = (" - " + fmt_local(until, "%a %H:%M", cfg) if short
                    else " - resumes " + fmt_local(until, "%a %H:%M", cfg,
                                                   with_label=True))
        if reason == SNAPSHOT:
            return ("SNAPSHOT PASS" if short
                    else "STOPPED: snapshot pass holding the lane")
        if reason not in AUTO_REASONS:
            return "STOPPED (operator)" if short else "STOPPED by operator"
        limit = threshold(cfg, str(reason))
        pct = f"{limit:.0%}" if limit is not None else "?"
        if reason == WEEKLY_GOAL:
            weekly = _stop_weekly(stop)
            seen = f" ({weekly:.0%})" if weekly is not None else ""
            if short:
                return f"GOAL {pct} HIT{seen}"
            return f"STOPPED: weekly goal {pct} reached{seen}{tail}"
        if reason == FABLE_BUCKET:
            return (f"FABLE {pct}{tail}" if short
                    else f"STOPPED: fable bucket {pct}{tail}")
        return (f"5H {pct}{tail}" if short
                else f"STOPPED: 5h bucket {pct}{tail}")
    except Exception:  # pragma: no cover - drawn every overlay frame
        return None
