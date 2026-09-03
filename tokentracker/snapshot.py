"""Keep the rendered gallery fresh: a screenshot pass the loop schedules itself.

The operator's ask: "make sure the render screenshot is up-to-date at the end
of the day or right before the token limit is exhausted as TD identifies."
Both halves are budget decisions, so they belong beside the allocator rather
than in a cron job that cannot see how much week is left.

Two triggers, evaluated every poll from the allocator's own bucket forecasts:

  end of day     the first poll at or after `snapshot.eod_local` on each local
                 day. `next_eod` in state/snapshot.json is what makes it fire
                 once: it is advanced past `now` the moment the trigger reads
                 it, so a five-minute poll cannot fire twelve times at 23:00.

  pre-exhaustion any bucket's forecast reaching its own stop threshold within
                 `snapshot.lead_minutes` - fable >= 0.97, weekly >= the goal,
                 five_hour >= its guard - or the poll that writes the weekly
                 goal stop.

`min_gap_minutes` suppresses either one when a snapshot already ran that
recently, and the end-of-day slot is still consumed when it does: a gallery
refreshed forty minutes ago does not need refreshing again at 23:00, and
holding the slot open would only fire it at 23:05 instead.

The lead time is the point of the pre-exhaustion trigger, and it is why the
stop-write trigger is the weaker of the two. Once state/stop.json is written
the control switch is `stopped`, `gate_decision` zeroes both launch budgets and
nothing starts - so a snapshot enqueued at that moment sits `pending` until the
operator presses START or the week rolls. The forecast is what gets the work
done while there is still budget and dispatch to do it with, which is why
`reserve_fraction` of the weekly bucket is held back from the pacer's target
(`scheduler.pacing`): the room the snapshot needs is set aside *before* the
goal, not begged for after it.

The task runs on the WORKERS tier's model - never Fable. A screenshot pass is
hands-on work (wait on a process, run a script, convert files, commit), which
is exactly what the worker tier is for, and the Fable bucket is the one the
allocator is usually protecting when this fires.

While a snapshot task is pending or running the director fork is not re-armed
(`cli._fork_wanted`), so the engine and the budget are the snapshot's.

Nothing here raises: it is called from inside the poll and from status lines.
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .models import parse_iso, utcnow

PREFIX = "snapshot-"
# Above the director fork's 100, so the launch batch (sorted by -priority)
# starts the snapshot first when both are pending.
PRIORITY = 200
MAX_MINUTES = 240
# state/snapshot.json's contract.
STATE_KEYS = ("last_run", "last_reason", "last_commit", "next_eod",
              "forecast_trigger_at", "commits")
# A git hash as the worker will report it.
COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "eod_local": "23:00",
    "lead_minutes": 45.0,
    "reserve_fraction": 0.02,
    "repo": "C:/Users/chenw/StarGTA",
    "min_gap_minutes": 120.0,
}
# A reserve larger than this is not a reserve, it is a second goal.
RESERVE_MAX = 0.25

REASON_EOD = "end of day"
REASON_FORECAST = "pre-exhaustion forecast"
REASON_STOP = "weekly goal stop"

# The brief is fixed text, not a template the operator edits: it is the one
# task in the system whose value depends on being reproducible run after run.
PROMPT = (
    "In {repo}: wait until no UnrealEditor*/UnrealBuildTool process is alive "
    "(never kill one). Run Tools/verify.sh shots (Sable gallery). If "
    "D:/UE/StarGTA-beta/Saved/beta-warm.ok exists, run Tools/verify.sh "
    "beta-shots in D:/UE/StarGTA-beta and copy the frames into "
    "{repo}/docs/screenshots/beta/. Convert new PNGs to JPEG q86 beside the "
    "existing gallery convention. Refresh the README gallery sections (Sable "
    "and, when present, 'Beta on the City Sample') with the fresh frames and "
    "today's date; commit by explicit paths (docs/screenshots/*.jpg, "
    "docs/screenshots/beta/*.jpg, README.md) with the trailer "
    "'Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>' and push origin. "
    "Report the frame count and the commit hash."
)


# ------------------------------------------------------------------ settings

def _finite(value: Any, fallback: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def block(cfg: Any) -> dict[str, Any]:
    raw = getattr(cfg, "snapshot", None)
    return raw if isinstance(raw, dict) else {}


def setting(cfg: Any, key: str) -> Any:
    """One `snapshot.<key>`, with its built-in default. Never raises."""
    return block(cfg).get(key, DEFAULTS.get(key))


def enabled(cfg: Any) -> bool:
    return bool(setting(cfg, "enabled"))


def repo(cfg: Any) -> str:
    """The repo the gallery lives in; `report_repo` when the block omits it."""
    value = setting(cfg, "repo")
    text = str(value).strip() if isinstance(value, str) else ""
    if text:
        return text
    fallback = str(getattr(cfg, "report_repo", "") or "").strip()
    return fallback or str(DEFAULTS["repo"])


def lead_minutes(cfg: Any) -> float:
    return max(0.0, _finite(setting(cfg, "lead_minutes"),
                            float(DEFAULTS["lead_minutes"])))


def min_gap_minutes(cfg: Any) -> float:
    return max(0.0, _finite(setting(cfg, "min_gap_minutes"),
                            float(DEFAULTS["min_gap_minutes"])))


def reserve_fraction(cfg: Any) -> float:
    """Weekly budget held back for the snapshot; 0 when the policy is off.

    Subtracted from the pacer's target in `scheduler.pacing`, so the loop aims
    to land the week that much short of the goal and the screenshot pass has
    somewhere to run from.
    """
    if not enabled(cfg):
        return 0.0
    return min(max(_finite(setting(cfg, "reserve_fraction"),
                           float(DEFAULTS["reserve_fraction"])), 0.0),
               RESERVE_MAX)


def parse_hhmm(text: Any) -> tuple[int, int]:
    """"23:00" -> (23, 0). Junk falls back to the default rather than raising."""
    default = str(DEFAULTS["eod_local"])
    raw = str(text if text is not None else default).strip() or default
    parts = raw.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        hour, minute = 23, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour, minute = 23, 0
    return hour, minute


# ------------------------------------------------------- state/snapshot.json

def _write_atomic(path, body: str) -> None:
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


def read_state(cfg: Config) -> dict[str, Any]:
    """state/snapshot.json as a dict; {} when there is none. Never raises."""
    try:
        data = json.loads(cfg.snapshot_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(cfg: Config, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into state/snapshot.json; returns what is now on disk."""
    state = read_state(cfg)
    state.update(patch)
    payload = {key: state.get(key) for key in STATE_KEYS if key in state}
    _write_atomic(cfg.snapshot_file, json.dumps(payload, indent=2))
    return payload


def recorded_commits(cfg: Config) -> set[str]:
    """Every commit a snapshot run has reported, for the ledger's milestones."""
    raw = read_state(cfg).get("commits")
    if not isinstance(raw, list):
        return set()
    return {str(c).strip().lower() for c in raw if isinstance(c, str) and c.strip()}


# ------------------------------------------------------------------ triggers

def eod_on(cfg: Config, moment: datetime) -> datetime:
    """The `eod_local` instant on `moment`'s LOCAL day, back in UTC.

    Local, because "end of day" means the operator's day: at UTC this would
    fire at 18:00 CT in summer and 17:00 in winter.
    """
    from . import clock

    hour, minute = parse_hhmm(setting(cfg, "eod_local"))
    local = clock.to_local(moment, cfg)
    if local is None:
        return moment
    try:
        target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return target.astimezone(moment.tzinfo or target.tzinfo)
    except (ValueError, OSError, OverflowError):
        return moment


def next_eod_after(cfg: Config, moment: datetime) -> datetime:
    """The first end-of-day strictly after `moment`.

    A loop that was off for three days lands on tomorrow, not on the three
    slots it slept through: a snapshot is worth taking now, not four times.
    """
    candidate = eod_on(cfg, moment)
    guard = 0
    while candidate <= moment and guard < 400:
        candidate = eod_on(cfg, candidate + timedelta(days=1))
        guard += 1
    return candidate


def _bucket_rows(buckets: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(buckets, dict):
        return {}
    return {str(name): row for name, row in buckets.items()
            if isinstance(row, dict)}


def forecast_hits(cfg: Config, buckets: Any,
                  now: datetime | None = None) -> tuple[datetime | None, str]:
    """When the first bucket reaches its own stop, at the rate it is burning.

    Reads the dict form the allocator writes to state/allocation.json rather
    than re-deriving the buckets, so the panel and `status` get the same answer
    the poll did without parsing history a second time.

    (None, "") when no bucket has a usable rate, or none of them is heading
    anywhere. A bucket already past its stop returns `now`: it is not "about
    to" be exhausted, it is exhausted, and the snapshot is overdue.
    """
    now = now or utcnow()
    best: datetime | None = None
    which = ""
    for name, row in _bucket_rows(buckets).items():
        stop = _finite(row.get("stop"), math.nan)
        util = _finite(row.get("utilization"), math.nan)
        if not math.isfinite(stop) or not math.isfinite(util):
            continue
        if util >= stop:
            return now, name
        rate = row.get("rate")
        rate = _finite(rate, 0.0) if rate is not None else 0.0
        if rate <= 0:
            continue
        hours = (stop - util) / rate
        # Never past the window's own reset: the bucket empties there anyway,
        # and a forecast beyond it is about a week that has not started.
        left = _finite(row.get("hours_to_reset"), math.inf)
        if math.isfinite(left) and hours >= left:
            continue
        when = now + timedelta(hours=hours)
        if best is None or when < best:
            best, which = when, name
    return best, which


def _too_soon(cfg: Config, state: dict[str, Any], now: datetime) -> bool:
    """True while `min_gap_minutes` since the last run has not elapsed."""
    last = parse_iso(state.get("last_run"))
    if last is None:
        return False
    return (now - last).total_seconds() < min_gap_minutes(cfg) * 60.0


def due(cfg: Config, buckets: Any, now: datetime | None = None,
        stop_written: bool = False,
        state: dict[str, Any] | None = None,
        ) -> tuple[str | None, datetime, datetime | None, bool]:
    """(reason or None, next_eod, forecast_at, suppressed).

    Pure apart from reading state/snapshot.json: `maybe_enqueue` is what
    persists the advanced `next_eod`, so calling this from a status line cannot
    consume the day's end-of-day slot.
    """
    now = now or utcnow()
    state = read_state(cfg) if state is None else state
    forecast_at, bucket = forecast_hits(cfg, buckets, now)

    stored = parse_iso(state.get("next_eod"))
    if stored is None:
        # First sight: seed the next slot and do not fire. A fresh install at
        # 23:30 must not decide it missed today's 23:00 - it has no way to know
        # whether one already ran.
        return None, next_eod_after(cfg, now), forecast_at, False

    reason: str | None = None
    next_eod = stored
    if now >= stored:
        reason = REASON_EOD
        next_eod = next_eod_after(cfg, now)
    elif stop_written:
        reason = REASON_STOP
    elif forecast_at is not None:
        minutes = (forecast_at - now).total_seconds() / 60.0
        if minutes <= lead_minutes(cfg):
            reason = (f"{REASON_FORECAST}: {bucket} reaches its stop in "
                      f"{max(minutes, 0.0):.0f} min")

    if reason is not None and _too_soon(cfg, state, now):
        # Suppressed, and the end-of-day slot is still consumed: a gallery
        # refreshed inside the gap is fresh, and re-arming the slot would only
        # fire it again at the next poll.
        return None, next_eod, forecast_at, True
    return reason, next_eod, forecast_at, False


# --------------------------------------------------------------- the task

def task_id(now: datetime | None = None, cfg: Any = None) -> str:
    """"snapshot-20260903T2300", on the operator's clock.

    Local, because the id is read by a human beside an end-of-day trigger that
    is itself local; the row's timestamps stay UTC like every other task's.
    """
    from . import clock

    return PREFIX + clock.fmt_local(now or utcnow(), "%Y%m%dT%H%M", cfg,
                                    fallback="unknown")


def is_snapshot(task_id_value: Any) -> bool:
    return str(task_id_value or "").startswith(PREFIX)


def pending_or_running(dispatcher: Any) -> Any:
    """The snapshot task holding the lane right now, or None.

    While one stands the director fork is not re-armed, so this is the gate
    `cli._fork_wanted` asks.
    """
    try:
        tasks = dispatcher.tasks()
    except (AttributeError, TypeError):
        return None
    for task in tasks:
        if is_snapshot(getattr(task, "id", "")) and \
                getattr(task, "status", "") in ("pending", "running"):
            return task
    return None


def queued_state(cfg: Config) -> str | None:
    """"pending" / "running" when a pass is outstanding, else None.

    Read straight off tasks.json rather than through a Dispatcher, because the
    two callers that need it - the panel's line and `status` - have a Config
    and nothing else, and every other state in this project is read from its
    file the same way.
    """
    try:
        data = json.loads(cfg.tasks_file.read_text(encoding="utf-8"))
        rows = data.get("tasks") if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or not is_snapshot(row.get("id")):
            continue
        status = row.get("status")
        if status in ("pending", "running"):
            return str(status)
    return None


def worker_model(cfg: Config) -> str | None:
    """The ALLOCATED workers tier's model - never a Fable one.

    The allocated graph rather than config.json, because rung 4 of the ladder
    can move the advisory tier onto this model and the snapshot must run on
    what the workers are actually running on. A Fable id here would mean the
    graph puts Fable at the worker tier; the tier's fallback is tried, and
    failing that the row goes out on the account default rather than spending
    the bucket the allocator is protecting.
    """
    from .allocator import allocate
    from .graph import WORKERS, tiers_of

    try:
        workers = tiers_of(allocate(cfg).graph)[2]
    except Exception:
        workers = {}
    for key in ("model", "fallback"):
        model = str(workers.get(key) or "").strip()
        if model and "fable" not in model.lower():
            return model
    fallback = str(getattr(cfg, "worker_model", "") or "").strip()
    if fallback and "fable" not in fallback.lower():
        return fallback
    return None


def prompt(cfg: Config) -> str:
    return PROMPT.format(repo=repo(cfg))


def build_task(cfg: Config, now: datetime | None = None):
    """The TaskSpec a trigger enqueues."""
    from .models import TaskSpec

    return TaskSpec(
        id=task_id(now, cfg),
        prompt=prompt(cfg),
        cwd=repo(cfg),
        weight="heavy",
        model=worker_model(cfg),
        priority=PRIORITY,
        max_minutes=MAX_MINUTES,
    )


def maybe_enqueue(cfg: Config, dispatcher: Any, buckets: Any,
                  now: datetime | None = None, stop_written: bool = False,
                  ) -> str | None:
    """Evaluate both triggers and queue the pass when one fires.

    Returns the line the loop logs, or None. Never raises: it runs inside the
    poll, and a broken snapshot block must not cost the tick.
    """
    now = now or utcnow()
    try:
        if not enabled(cfg):
            return None
        state = read_state(cfg)
        reason, next_eod, forecast_at, suppressed = due(
            cfg, buckets, now, stop_written, state)
        patch: dict[str, Any] = {
            "next_eod": next_eod.isoformat(),
            "forecast_trigger_at": (forecast_at.isoformat()
                                    if forecast_at is not None else None),
        }
        if reason is None:
            # The advanced slot and the fresh forecast are persisted either
            # way: that is what makes the end-of-day trigger fire once even
            # when min_gap suppressed it.
            write_state(cfg, patch)
            if suppressed:
                return (f"snapshot suppressed: one ran inside the last "
                        f"{min_gap_minutes(cfg):.0f} min")
            return None
        if pending_or_running(dispatcher) is not None:
            write_state(cfg, patch)
            return None
        task = build_task(cfg, now)
        if dispatcher.get(task.id) is not None:
            # Same minute, or a row left from a previous run of this id.
            write_state(cfg, patch)
            return None
        dispatcher.add(task)
        write_state(cfg, {**patch, "last_run": now.isoformat(),
                          "last_reason": reason})
        return (f"snapshot queued ({reason}): {task.id} on "
                f"{task.model or '(account default)'} in {task.cwd}")
    except Exception:  # pragma: no cover - the poll must survive anything
        return None


def note_finished(cfg: Config, dispatcher: Any) -> str | None:
    """Record the commit a finished snapshot reported. Never raises.

    The hash is scraped from the task's own result file rather than from git,
    so the ledger can mark that milestone without a second repo read - and so
    `status` can name the commit the last gallery refresh landed on.
    """
    try:
        state = read_state(cfg)
        known = recorded_commits(cfg)
        for task in dispatcher.tasks():
            if not is_snapshot(getattr(task, "id", "")):
                continue
            if getattr(task, "status", "") != "done":
                continue
            commit = _commit_of(cfg, task.id)
            if not commit or commit in known:
                continue
            commits = [c for c in (state.get("commits") or [])
                       if isinstance(c, str)]
            commits.append(commit)
            write_state(cfg, {"last_commit": commit, "commits": commits[-20:]})
            return f"snapshot {task.id} committed {commit[:8]}"
    except Exception:  # pragma: no cover
        return None
    return None


def _commit_of(cfg: Config, ident: str) -> str | None:
    """The last git hash the run printed, from logs/<id>.out.json."""
    try:
        text = (cfg.logs_dir / f"{ident}.out.json").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The last hash wins: the worker reports it at the end, after any hash it
    # quoted from the log on the way.
    found = [m.group(0) for m in COMMIT_RE.finditer(text.lower())
             if len(m.group(0)) >= 7]
    return found[-1] if found else None


# ------------------------------------------------------------------ display

def age(cfg: Config, now: datetime | None = None) -> str:
    """"shots 3h ago", or "shots never" when none has run.

    `last_run` is when the pass was QUEUED, not when it finished, and a pass
    can sit pending behind operator STOP or run for hours - so while one is
    outstanding the line says so instead of reporting a freshness the gallery
    has not got yet.
    """
    outstanding = queued_state(cfg)
    if outstanding == "running":
        return "shots running"
    if outstanding == "pending":
        return "shots queued"
    last = parse_iso(read_state(cfg).get("last_run"))
    if last is None:
        return "shots never"
    seconds = max(0.0, ((now or utcnow()) - last).total_seconds())
    if seconds < 90:
        return "shots just now"
    if seconds < 3600:
        return f"shots {int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"shots {int(seconds // 3600)}h ago"
    return f"shots {int(seconds // 86400)}d ago"


def status_line(cfg: Config, buckets: Any = None,
                now: datetime | None = None) -> str:
    """"shots 3h ago - next: eod 23:00 CT / pre-limit in 38 min".

    `buckets` defaults to whatever the last poll wrote to
    state/allocation.json, so a status line costs one small file read.
    """
    from . import clock

    now = now or utcnow()
    if not enabled(cfg):
        return "shots: policy off (snapshot.enabled = false)"
    if buckets is None:
        from .allocator import read_state as read_alloc

        buckets = (read_alloc(cfg) or {}).get("buckets")
    state = read_state(cfg)
    _reason, next_eod, forecast_at, _suppressed = due(cfg, buckets, now,
                                                      state=state)
    parts = [f"eod {clock.fmt_local(next_eod, '%a %H:%M', cfg, with_label=True)}"]
    if forecast_at is None:
        parts.append("pre-limit not forecast")
    else:
        minutes = (forecast_at - now).total_seconds() / 60.0
        parts.append("pre-limit now" if minutes <= 0
                     else f"pre-limit in {minutes:.0f} min")
    tail = ""
    commit = state.get("last_commit")
    if isinstance(commit, str) and commit:
        tail = f" - last commit {commit[:8]}"
    return f"{age(cfg, now)} - next: {' / '.join(parts)}{tail}"
