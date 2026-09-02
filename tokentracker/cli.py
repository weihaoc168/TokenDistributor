from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load_config
from .dispatch import Dispatcher, DispatchError
from .models import Decision, TaskSpec, utcnow

ROOT = Path(__file__).resolve().parent.parent
BAR_WIDTH = 30
THROTTLE_TASK_ID = "throttle-main-continue"
THROTTLE_PROMPT = (
    "FULL THROTTLE: the operator pressed the TokenDistributor full-throttle "
    "button. The goal is to exhaust the remaining weekly Claude token budget on "
    "useful work for this project before it resets. Continue the most valuable "
    "unfinished work from this conversation autonomously: pick the "
    "highest-impact pending items, work in long thorough passes, verify results "
    "as you go, and keep working until no genuinely useful work remains."
)


def _throttle_active(cfg: Config) -> bool:
    try:
        data = json.loads(cfg.throttle_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("active"))


def _throttle_sig(cfg: Config) -> float | None:
    try:
        return cfg.throttle_file.stat().st_mtime
    except OSError:
        return None


def _ensure_throttle_task(cfg: Config, dispatcher: Dispatcher) -> None:
    if not cfg.main_session_ids:
        return
    task = dispatcher.get(THROTTLE_TASK_ID)
    if task is None:
        dispatcher.add(TaskSpec(
            id=THROTTLE_TASK_ID,
            prompt=THROTTLE_PROMPT,
            cwd=str(Path.home()),
            weight="heavy",
            model=cfg.throttle_model or None,
            priority=100,
            max_minutes=240,
            resume_session=cfg.main_session_ids[0],
        ))
    elif task.status in ("done", "failed", "killed"):
        # The stored spec may predate a config change (main session handover,
        # executive model directive); refresh it before every relaunch.
        task.model = cfg.throttle_model or None
        task.resume_session = cfg.main_session_ids[0]
        dispatcher.set_status(THROTTLE_TASK_ID, "pending")


def _bar(frac: float) -> str:
    frac = min(max(frac, 0.0), 1.0)
    filled = round(frac * BAR_WIDTH)
    return f"[{'#' * filled}{'.' * (BAR_WIDTH - filled)}] {frac:.1%}"


def _write_state(cfg: Config, payload: dict) -> None:
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    import os
    os.replace(tmp, cfg.state_file)


def _distribution(cfg: Config, own_dirs: set[str], now) -> dict:
    from datetime import timedelta

    from .activity import project_dir_name
    from .usage import MAIN_SESSION_KEY, scan_local_tokens

    since = now - timedelta(minutes=cfg.slope_window_minutes)
    tokens = scan_local_tokens(cfg, since, now)
    home_prefix = project_dir_name(str(Path.home()))

    main_tokens = tokens.get(MAIN_SESSION_KEY, 0)
    own_tokens = sum(v for k, v in tokens.items() if k in own_dirs and k != MAIN_SESSION_KEY)
    foreign = sorted(
        ((k, v) for k, v in tokens.items()
         if k not in own_dirs and k != MAIN_SESSION_KEY),
        key=lambda kv: -kv[1],
    )
    shares: list[dict] = []
    if main_tokens:
        shares.append({"label": "main", "kind": "main", "tokens": main_tokens})
    if own_tokens:
        shares.append({"label": "tracker", "kind": "own", "tokens": own_tokens})
    for name, count in foreign[:4]:
        if name == home_prefix:
            label = "interactive"
        elif name.startswith(f"{home_prefix}-"):
            label = name[len(home_prefix) + 1:]
        else:
            label = name
        shares.append({"label": label, "kind": "foreign", "tokens": count})
    rest = sum(v for _, v in foreign[4:])
    if rest:
        shares.append({"label": "other", "kind": "foreign", "tokens": rest})
    return {
        "window_minutes": cfg.slope_window_minutes,
        "total_tokens": sum(tokens.values()),
        "shares": shares,
    }


def _tick(
    cfg: Config, dispatcher: Dispatcher, history, do_fetch: bool = True,
) -> tuple[Decision, list[str], Exception | None, "object | None", bool]:
    from .activity import detect_activity
    from .scheduler import decide, decide_local, normalize, pacing
    from .usage import (
        UsageFetchError,
        compute_burn_rates,
        fetch_usage,
        learned_class_rates,
        load_calibration,
    )

    now = utcnow()
    own_dirs = dispatcher.own_project_dirs()
    snap = None
    fetch_exc: Exception | None = None
    if do_fetch:
        try:
            snap = fetch_usage(cfg)
            history.append(snap)
        except UsageFetchError as exc:
            fetch_exc = exc
    if snap is None:
        recent = history.load_recent(hours=max(1.0, cfg.stale_snapshot_minutes / 60))
        if recent and (now - recent[-1].fetched_at).total_seconds() <= cfg.stale_snapshot_minutes * 60:
            snap = recent[-1]
    if snap is None:
        reason = f"usage fetch failed: {fetch_exc}" if fetch_exc else "no usage snapshot available"
        decision = Decision("blocked", 0, False, reason)
        # Usage unknown means Claude may or may not have budget; the local lane
        # costs no budget either way, so it may still run.
        activity = detect_activity(cfg, own_dirs, now)
        decision.local_concurrency = decide_local(decision, activity, cfg, now)
        actions = dispatcher.apply(decision, now)
        _write_state(cfg, {"at": now.isoformat(), "decision": decision.to_dict(),
                           "error": str(fetch_exc) if fetch_exc else reason})
        return decision, actions, fetch_exc, None, False

    rolled = any(
        w.resets_at is not None and now >= w.resets_at
        for w in (snap.five_hour, snap.seven_day)
    )
    snap = normalize(snap, now)

    rates = compute_burn_rates(cfg, history, own_dirs, now)
    activity = detect_activity(cfg, own_dirs, now)
    class_rates = learned_class_rates(cfg, load_calibration(cfg))
    queue = dispatcher.queue_stats()
    decision = decide(snap, rates, activity, queue, cfg, class_rates, now)

    throttle = _throttle_active(cfg)
    if throttle and snap.seven_day.utilization < 0.999:
        decision = Decision(
            "surge", cfg.surge_concurrency, True,
            "Full throttle (manual override): exhausting weekly budget.",
        )
        _ensure_throttle_task(cfg, dispatcher)
    elif not throttle:
        stale_task = dispatcher.get(THROTTLE_TASK_ID)
        if stale_task is not None and stale_task.status == "pending":
            dispatcher.set_status(THROTTLE_TASK_ID, "killed")

    decision.local_concurrency = decide_local(decision, activity, cfg, now)
    actions = dispatcher.apply(decision, now)

    _write_state(cfg, {
        "at": now.isoformat(),
        "usage": snap.to_dict(),
        "pacing": pacing(snap, cfg, now),
        "rates": rates.__dict__,
        "activity": {
            "user_active": activity.user_active,
            "last_user_activity": activity.last_user_activity.isoformat()
            if activity.last_user_activity else None,
            "sessions": activity.active_foreign_sessions,
        },
        "decision": decision.to_dict(),
        "queue": dispatcher.queue_stats().__dict__,
        "local": {
            "enabled": cfg.local_enabled,
            "model": cfg.local_model,
            "engine_healthy": dispatcher.local_engine_healthy,
        },
        "distribution": _distribution(cfg, own_dirs, now),
        "usage_stale": bool(fetch_exc) or not do_fetch,
        "fetch_error": str(fetch_exc) if fetch_exc else None,
        "throttle": throttle,
    })
    return decision, actions, fetch_exc, snap, rolled


def cmd_run(cfg: Config, once: bool) -> int:
    from .usage import RateLimitedError, UsageHistory

    dispatcher = Dispatcher(cfg, supervise=True)
    history = UsageHistory(cfg)
    print(f"TokenDistributor loop started (poll every {cfg.poll_seconds}s, ctrl-c to stop)")
    next_fetch_ts = 0.0
    backoff = 0.0
    while True:
        do_fetch = time.monotonic() >= next_fetch_ts
        decision, actions, fetch_exc, snap, rolled = _tick(cfg, dispatcher, history, do_fetch)
        stamp = datetime.now().strftime("%H:%M:%S")
        if rolled and backoff == 0.0:
            next_fetch_ts = 0.0
            print(f"{stamp}   window boundary passed; refetching usage next tick")
        if fetch_exc is not None:
            base = backoff * 2 if backoff else float(cfg.fetch_backoff_base_seconds)
            if isinstance(fetch_exc, RateLimitedError) and fetch_exc.retry_after:
                base = max(base, fetch_exc.retry_after)
            backoff = min(base, float(cfg.fetch_backoff_max_seconds))
            next_fetch_ts = time.monotonic() + backoff
            print(f"{stamp}   usage fetch backing off {backoff:.0f}s ({fetch_exc})")
        elif do_fetch:
            backoff = 0.0
        local_note = (f" local={decision.local_concurrency}"
                      if decision.local_concurrency else "")
        print(f"{stamp} mode={decision.mode} conc={decision.target_concurrency}"
              f"{local_note} | {decision.reason}")
        for line in actions:
            print(f"{stamp}   {line}")
        if once:
            return 0
        sleep_s = float(cfg.poll_seconds)
        pending = next_fetch_ts - time.monotonic()
        if pending > 0:
            sleep_s = min(sleep_s, max(5.0, pending + 1.0))
        if rolled:
            sleep_s = min(sleep_s, 10.0)
        elif snap is not None:
            for window in (snap.five_hour, snap.seven_day):
                if window.resets_at is None:
                    continue
                until = (window.resets_at - utcnow()).total_seconds() + 10.0
                if until > 0:
                    sleep_s = min(sleep_s, max(10.0, until))
        sig = _throttle_sig(cfg)
        deadline = time.monotonic() + sleep_s
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(10.0, remaining))
                if _throttle_sig(cfg) != sig:
                    break
        except KeyboardInterrupt:
            print("stopped; running background tasks continue detached")
            return 0


def cmd_status(cfg: Config) -> int:
    from .scheduler import pacing
    from .usage import UsageFetchError, fetch_usage

    try:
        snap = fetch_usage(cfg)
    except UsageFetchError as exc:
        print(f"live usage unavailable: {exc}")
        if cfg.state_file.exists():
            state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
            print(f"last known state from {state.get('at')}:")
            print(json.dumps(state.get("usage"), indent=2))
        return 1

    now = utcnow()
    p = pacing(snap, cfg, now)
    print("weekly  " + _bar(snap.seven_day.utilization)
          + f"  resets in {p['time_left_h']:.1f}h")
    print("5-hour  " + _bar(snap.five_hour.utilization))
    for name, w in snap.extra.items():
        print(f"{name:<7} " + _bar(w.utilization))
    print(f"pace: elapsed {p['elapsed_frac']:.1%}, reserve {p['reserve']:.1%}, "
          f"required {p['required_total_pct_per_hr']:.2f}%/h")

    if cfg.state_file.exists():
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        d = state.get("decision", {})
        print(f"last decision [{state.get('at', '?')}]: {d.get('mode')} "
              f"conc={d.get('target_concurrency')} - {d.get('reason')}")

    dispatcher = Dispatcher(cfg)
    stats = dispatcher.queue_stats()
    print(f"queue: {stats.running} running ({stats.running_local} local), "
          f"{stats.pending_heavy} heavy pending, "
          f"{stats.pending_light} light pending")
    if cfg.local_enabled:
        engine = "up" if dispatcher.local_engine_up() else "down"
        print(f"local lane: {cfg.local_model} via {cfg.local_base_url} - engine {engine}")
    for t in dispatcher.tasks():
        extra = f" tokens={t.session_tokens}" if t.session_tokens else ""
        extra += f" err={t.error}" if t.error else ""
        lane = f" [{t.lane}]" if t.lane else ""
        print(f"  {t.id:<24} {t.weight:<6} {t.status:<8} prio={t.priority}{lane}{extra}")
    return 0


def cmd_add(cfg: Config, args: argparse.Namespace) -> int:
    dispatcher = Dispatcher(cfg)
    task = TaskSpec(
        id=args.id,
        prompt=args.prompt,
        cwd=str(Path(args.cwd).resolve()),
        weight=args.weight,
        model=args.model,
        priority=args.priority,
        max_minutes=args.max_minutes,
    )
    dispatcher.add(task)
    print(f"added task {task.id} ({task.weight})")
    return 0


def cmd_list(cfg: Config) -> int:
    dispatcher = Dispatcher(cfg)
    for t in dispatcher.tasks():
        print(f"{t.id:<24} {t.weight:<6} {t.status:<8} prio={t.priority} cwd={t.cwd}")
    return 0


def cmd_set_status(cfg: Config, task_id: str, status: str) -> int:
    dispatcher = Dispatcher(cfg)
    dispatcher.set_status(task_id, status)
    print(f"task {task_id} -> {status}")
    return 0


def cmd_history(cfg: Config, hours: float) -> int:
    from .usage import UsageHistory

    history = UsageHistory(cfg)
    snaps = history.load_recent(hours)
    if not snaps:
        print("no history yet")
        return 0
    for s in snaps:
        print(f"{s.fetched_at.isoformat()}  weekly {s.seven_day.utilization:.1%}  "
              f"5h {s.five_hour.utilization:.1%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tracker",
        description="TokenDistributor: pace Claude Code weekly token budget across sessions",
    )
    parser.add_argument("--root", default=str(ROOT))
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="start the control loop")
    run_p.add_argument("--once", action="store_true", help="single tick then exit")
    sub.add_parser("status", help="live usage, pacing and queue")
    sub.add_parser("list", help="list tasks")
    sub.add_parser("overlay", help="always-on-top panel docked to the Sundial layer")

    add_p = sub.add_parser("add", help="queue a task")
    add_p.add_argument("--id", required=True)
    add_p.add_argument("--prompt", required=True)
    add_p.add_argument("--cwd", required=True)
    add_p.add_argument("--weight", choices=("heavy", "light"), default="light")
    add_p.add_argument("--model", default=None)
    add_p.add_argument("--priority", type=int, default=0)
    add_p.add_argument("--max-minutes", type=int, default=90)

    req_p = sub.add_parser("requeue", help="reset a task to pending")
    req_p.add_argument("id")
    can_p = sub.add_parser("cancel", help="mark a pending task failed")
    can_p.add_argument("id")

    hist_p = sub.add_parser("history", help="recent utilization snapshots")
    hist_p.add_argument("--hours", type=float, default=12.0)

    args = parser.parse_args(argv)
    cfg = load_config(args.root)

    try:
        if args.cmd == "run":
            return cmd_run(cfg, args.once)
        if args.cmd == "status":
            return cmd_status(cfg)
        if args.cmd == "add":
            return cmd_add(cfg, args)
        if args.cmd == "list":
            return cmd_list(cfg)
        if args.cmd == "requeue":
            return cmd_set_status(cfg, args.id, "pending")
        if args.cmd == "cancel":
            return cmd_set_status(cfg, args.id, "failed")
        if args.cmd == "history":
            return cmd_history(cfg, args.hours)
        if args.cmd == "overlay":
            from .overlay import run_overlay
            return run_overlay(cfg)
    except DispatchError as exc:
        print(f"error: {exc}")
        return 1
    return 0
