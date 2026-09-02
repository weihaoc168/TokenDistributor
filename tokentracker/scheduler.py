from __future__ import annotations

import math
from datetime import datetime, timedelta

from .config import Config
from .models import (
    ActivityState,
    BurnRates,
    Decision,
    QueueStats,
    UsageSnapshot,
    WindowUsage,
    WEEK_HOURS,
)

FIVE_HOUR_PERIOD = 5.0


def _roll_window(window: WindowUsage, hours: float, now: datetime) -> WindowUsage:
    if window.resets_at is None or now < window.resets_at:
        return window
    resets = window.resets_at
    while resets <= now:
        resets += timedelta(hours=hours)
    return WindowUsage(0.0, resets)


def normalize(snap: UsageSnapshot, now: datetime) -> UsageSnapshot:
    # A snapshot fetched before a window boundary is provably wrong once the
    # boundary passes: the window rolled to 0. Roll resets_at forward so pacing
    # math never sees an expired window.
    snap.five_hour = _roll_window(snap.five_hour, FIVE_HOUR_PERIOD, now)
    snap.seven_day = _roll_window(snap.seven_day, WEEK_HOURS, now)
    snap.extra = {k: _roll_window(w, WEEK_HOURS, now) for k, w in snap.extra.items()}
    return snap

MIN_CLASS_RATE = 0.05
EXHAUSTED_UTIL = 0.999
SURGE_FLOOR_REMAINING = 0.005


def _hours_until(resets_at: datetime | None, now: datetime) -> float:
    if resets_at is None:
        return WEEK_HOURS / 2
    hours = (resets_at - now).total_seconds() / 3600
    return min(max(hours, 0.25), WEEK_HOURS)


def pacing(snap: UsageSnapshot, cfg: Config, now: datetime) -> dict[str, float]:
    u = snap.seven_day.utilization
    time_left_h = _hours_until(snap.seven_day.resets_at, now)
    elapsed_frac = min(max(1 - time_left_h / WEEK_HOURS, 0.0), 1.0)
    if time_left_h <= cfg.endgame_hours:
        reserve = 0.0
    else:
        reserve = cfg.reserve_week_frac * (time_left_h / WEEK_HOURS)
    required = max(0.0, 1 - reserve - u) / time_left_h * 100
    return {
        "time_left_h": time_left_h,
        "elapsed_frac": elapsed_frac,
        "reserve": reserve,
        "required_total_pct_per_hr": required,
        "utilization": u,
    }


def decide(
    snap: UsageSnapshot,
    rates: BurnRates,
    activity: ActivityState,
    queue: QueueStats,
    cfg: Config,
    class_rates: tuple[float, float],
    now: datetime,
) -> Decision:
    u = snap.seven_day.utilization
    u5 = snap.five_hour.utilization
    p = pacing(snap, cfg, now)
    time_left_h = p["time_left_h"]

    if u >= EXHAUSTED_UTIL:
        return Decision(
            "blocked", 0, False,
            f"Weekly limit exhausted ({u:.1%}); resets in {time_left_h:.1f}h.",
        )

    user_recent = activity.recent_within(cfg.activity_cooldown_minutes, now)
    guard = cfg.five_hour_guard_active if user_recent else cfg.five_hour_guard_idle
    if u5 >= guard:
        reset5 = _hours_until(snap.five_hour.resets_at, now)
        return Decision(
            "blocked", 0, False,
            f"Five-hour window at {u5:.1%} >= guard {guard:.0%}; resets in {reset5:.1f}h.",
        )

    if time_left_h <= cfg.endgame_hours and u < 1 - SURGE_FLOOR_REMAINING:
        conc = cfg.surge_concurrency
        if activity.user_active:
            conc = max(1, cfg.max_concurrency - 1)
        return Decision(
            "surge", conc, True,
            f"Endgame: {1 - u:.1%} of weekly budget left with {time_left_h:.1f}h to reset; maximizing burn.",
        )

    if activity.user_active:
        n_sessions = len(activity.active_foreign_sessions)
        return Decision(
            "yield", cfg.yield_concurrency, False,
            f"User active ({n_sessions} foreign session(s) live); yielding budget to interactive work.",
        )

    own_required = max(0.0, p["required_total_pct_per_hr"] - rates.foreign_ema_pct_per_hr)
    heavy_rate = max(class_rates[0], MIN_CLASS_RATE)
    light_rate = max(class_rates[1], MIN_CLASS_RATE)

    if own_required <= 0.05:
        return Decision(
            "coast", 0, False,
            f"Ahead of pace: utilization {u:.1%} vs elapsed {p['elapsed_frac']:.1%}; nothing to burn yet.",
        )

    if own_required <= light_rate * 1.5:
        return Decision(
            "pace", 1, False,
            f"Small gap ({own_required:.2f}%/h needed); one light task covers it.",
        )

    n = min(max(math.ceil(own_required / heavy_rate), 1), cfg.max_concurrency)
    return Decision(
        "pace", n, True,
        f"Behind pace: need {own_required:.2f}%/h from background work; running {n} task(s).",
    )


def decide_local(
    decision: Decision, activity: ActivityState, cfg: Config, now: datetime,
) -> int:
    # The local FreeToken lane burns zero Claude budget, so it only makes sense
    # when Claude itself is unavailable ("blocked": weekly exhausted or the
    # five-hour guard tripped). It still yields the GPU to a present human
    # unless local_when_active says otherwise.
    if not cfg.local_enabled or decision.mode != "blocked":
        return 0
    if activity.user_active and not cfg.local_when_active:
        return 0
    return max(0, cfg.local_max_concurrency)
