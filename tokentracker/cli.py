from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from .config import Config, load_config
from .control import STOPPED, gate_decision, read_control
from .dispatch import Dispatcher, DispatchError
from .goal import (
    apply_goal_stop,
    parse_goal,
    read_goal,
    read_goal_source,
    read_stop,
    write_goal,
)
from .handover import FORK_MODES, FORK_TASK_ID, fork_status_line
from .models import Decision, TaskSpec, parse_iso, utcnow

ROOT = Path(__file__).resolve().parent.parent
BAR_WIDTH = 30
THROTTLE_TASK_ID = FORK_TASK_ID
FORK_COOLDOWN_SECONDS = 120
# Fallback only: the live brief is config.json's throttle_prompt, which is what
# turns the fork into the acting technical director.
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


def _control_sig(cfg: Config) -> float | None:
    try:
        return cfg.control_file.stat().st_mtime
    except OSError:
        return None


def _goal_sig(cfg: Config) -> float | None:
    try:
        return cfg.goal_file.stat().st_mtime
    except OSError:
        return None


def _wake_sig(cfg: Config) -> tuple[float | None, float | None, float | None]:
    # Any operator switch (full throttle, start/stop, weekly goal) should cut
    # the sleep short so a click takes effect within seconds, not a whole poll
    # period - a goal dragged down under the current weekly stops the loop now.
    return _throttle_sig(cfg), _control_sig(cfg), _goal_sig(cfg)


def _fork_prompt(cfg: Config) -> str:
    """The director brief handed to the fork; config.json is authoritative.

    The `{graph}` placeholder is expanded here, at launch, so the fork is told
    the agentic graph that is in force this poll - including a worker count the
    operator changed from the overlay a minute ago.
    """
    from .graph import graph_line, read_graph

    text = str(getattr(cfg, "throttle_prompt", "") or "").strip() or THROTTLE_PROMPT
    if "{graph}" in text:
        text = text.replace("{graph}", graph_line(read_graph(cfg)))
    return text


def _fork_cooldown(cfg: Config) -> float:
    try:
        return max(0.0, float(getattr(cfg, "fork_cooldown_seconds",
                                      FORK_COOLDOWN_SECONDS)))
    except (TypeError, ValueError):
        return float(FORK_COOLDOWN_SECONDS)


def _rearm_ready(cfg: Config, task: TaskSpec, now) -> bool:
    """True once the cooldown since the previous fork finished has elapsed.

    Without it a fork that dies on launch would be relaunched on every poll,
    burning the weekly budget on nothing but process starts.
    """
    cooldown = _fork_cooldown(cfg)
    if cooldown <= 0:
        return True
    finished = parse_iso(task.finished_at)
    if finished is None:
        return True
    return (now - finished).total_seconds() >= cooldown


def _fork_wanted(cfg: Config, mode: str, control: str, throttle: bool) -> bool:
    """Whether the continue fork should be ensured on this tick.

    The handover is only armed while dispatch is actually running, the weekly
    goal has not been reached, and the loop is in a working mode: pace (normal
    dispatch, when fork_in_pace is on) or surge (full throttle). blocked,
    yield, coast and stopped never hand the director job over.
    """
    if not cfg.main_session_ids or not cfg.throttle_fork_enabled:
        return False
    if control == STOPPED or mode not in FORK_MODES:
        return False
    if read_stop(cfg) is not None:
        return False
    return True if throttle else bool(getattr(cfg, "fork_in_pace", True))


def _ensure_throttle_task(cfg: Config, dispatcher: Dispatcher, now=None) -> None:
    # With forking disabled, full throttle still surges queued workers but the
    # executive continuation runs inside the main session itself (its agent
    # graph), not as a forked headless copy.
    if not cfg.main_session_ids or not cfg.throttle_fork_enabled:
        return
    now = now or utcnow()
    task = dispatcher.get(THROTTLE_TASK_ID)
    if task is None:
        dispatcher.add(TaskSpec(
            id=THROTTLE_TASK_ID,
            prompt=_fork_prompt(cfg),
            cwd=str(Path.home()),
            weight="heavy",
            model=cfg.throttle_model or None,
            priority=100,
            max_minutes=240,
            resume_session=cfg.main_session_ids[0],
        ))
    elif task.status in ("done", "failed", "killed"):
        if not _rearm_ready(cfg, task, now):
            return
        # The stored spec may predate a config change (main session handover,
        # executive model directive, a rewritten brief); refresh it before
        # every relaunch.
        task.model = cfg.throttle_model or None
        task.prompt = _fork_prompt(cfg)
        task.resume_session = cfg.main_session_ids[0]
        dispatcher.set_status(THROTTLE_TASK_ID, "pending")
    # pending or running: the handover already stands, nothing to re-arm.


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
    from .graph import apply_graph
    from .ledger import maybe_report
    from .scheduler import decide, decide_local, normalize, pacing
    from .usage import (
        UsageFetchError,
        compute_burn_rates,
        fetch_usage,
        learned_class_rates,
        load_calibration,
    )

    now = utcnow()
    # The graph is re-derived every poll, not once at startup: the overlay's
    # worker -/+ writes state/graph.json, and this is what turns that click
    # into the concurrency the scheduler reads a few lines below.
    apply_graph(cfg)
    own_dirs = dispatcher.own_project_dirs()
    # The fork's status before this tick's reap; a running -> done transition
    # is what the milestone trigger watches for.
    fork_before = dispatcher.get(THROTTLE_TASK_ID)
    fork_state = ((fork_before.status, fork_before.started_at)
                  if fork_before is not None else (None, None))
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
    control = read_control(cfg)
    if snap is None:
        reason = f"usage fetch failed: {fetch_exc}" if fetch_exc else "no usage snapshot available"
        decision = Decision("blocked", 0, False, reason)
        # Usage unknown means Claude may or may not have budget; the local lane
        # costs no budget either way, so it may still run.
        activity = detect_activity(cfg, own_dirs, now)
        decision.local_concurrency = decide_local(decision, activity, cfg, now)
        decision = gate_decision(decision, control)
        actions = dispatcher.apply(decision, now)
        report_line = maybe_report(cfg, dispatcher, before=fork_state,
                                   control=control, stop=read_stop(cfg), now=now)
        if report_line is not None:
            actions.append(report_line)
        _write_state(cfg, {"at": now.isoformat(), "decision": decision.to_dict(),
                           "dispatch": control,
                           "error": str(fetch_exc) if fetch_exc else reason})
        return decision, actions, fetch_exc, None, False

    rolled = any(
        w.resets_at is not None and now >= w.resets_at
        for w in (snap.five_hour, snap.seven_day)
    )
    snap = normalize(snap, now)

    # The weekly goal is checked before anything is launched, so the poll that
    # crosses it is also the poll that stops dispatch. apply_goal_stop may have
    # just written the control file, hence the re-read.
    goal = read_goal(cfg)
    stop, goal_line = apply_goal_stop(cfg, snap.seven_day.utilization, now)
    if goal_line is not None:
        control = read_control(cfg)

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
    # The handover is armed in normal pace mode too, not only under full
    # throttle: the fork is the acting director for as long as the loop is
    # dispatching at all. read_stop is current here - apply_goal_stop ran above.
    if _fork_wanted(cfg, decision.mode, control, throttle):
        _ensure_throttle_task(cfg, dispatcher, now)
    else:
        stale_task = dispatcher.get(THROTTLE_TASK_ID)
        if stale_task is not None and stale_task.status == "pending":
            dispatcher.set_status(THROTTLE_TASK_ID, "killed")

    decision.local_concurrency = decide_local(decision, activity, cfg, now)
    # Operator STOP is the last word: it zeroes both launch budgets, so apply()
    # reaps and adopts as usual but starts nothing new.
    decision = gate_decision(decision, control)
    actions = dispatcher.apply(decision, now)
    if goal_line is not None:
        actions.insert(0, goal_line)
    # Report triggers run after the reap, so a fork that finished on this very
    # tick is already marked done; generation itself is off-thread.
    report_line = maybe_report(cfg, dispatcher, before=fork_state,
                               control=control, stop=stop, now=now)
    if report_line is not None:
        actions.append(report_line)

    decision_payload = decision.to_dict()
    if stop is not None:
        # Name the reason whenever the stop record stands. The mode is only
        # forced while dispatch is actually parked: if the operator pressed
        # START back over the goal, the state must not claim to be stopped.
        decision_payload["stop_reason"] = stop.get("reason")
        if control == STOPPED:
            decision_payload["mode"] = STOPPED

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
        "decision": decision_payload,
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
        "dispatch": control,
        "weekly_goal": goal,
        "goal_stop": stop,
    })
    return decision, actions, fetch_exc, snap, rolled


def cmd_run(cfg: Config, once: bool) -> int:
    """The control loop, with the loop-exit report bolted onto both exits.

    ctrl-c and a SystemExit both mean dispatch has stopped, which is one of the
    report triggers; the report is written synchronously here because the
    process is about to end.
    """
    from .ledger import report_on_exit

    try:
        return _run_loop(cfg, once)
    except (KeyboardInterrupt, SystemExit):
        print("stopped; running background tasks continue detached")
        line = report_on_exit(cfg)
        if line is not None:
            print(line)
        return 0


def _run_loop(cfg: Config, once: bool) -> int:
    from .usage import RateLimitedError, UsageHistory

    from .graph import (
        known_models,
        override_warning,
        overlay_label,
        read_graph_source,
        validate_graph,
    )

    dispatcher = Dispatcher(cfg, supervise=True)
    history = UsageHistory(cfg)
    print(f"TokenDistributor loop started (poll every {cfg.poll_seconds}s, ctrl-c to stop)")
    # Named with its source: the numbers in force can come from either file, and
    # "why is config.json not applying" has no other symptom.
    graph, source = read_graph_source(cfg)
    print(f"graph: {overlay_label(graph)} (source: {source})")
    ignored = override_warning(cfg)
    if ignored:
        print(ignored)
    # A warning, not a refusal: an unknown id is usually a new model.
    for warning in validate_graph(graph, known_models(cfg)):
        print(warning)
    next_fetch_ts = 0.0
    backoff = 0.0
    while True:
        do_fetch = time.monotonic() >= next_fetch_ts
        # Sampled before the tick, not after: a switch clicked while the tick is
        # in flight (the usage fetch alone can take seconds) is then still newer
        # than this signature, so the sleep below breaks out at once instead of
        # sitting on the click for a whole poll period. One extra tick is cheap.
        sig = _wake_sig(cfg)
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
        deadline = time.monotonic() + sleep_s
        # KeyboardInterrupt is deliberately not caught here: cmd_run owns the
        # exit so the loop-exit report is written on every way out.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(10.0, remaining))
            if _wake_sig(cfg) != sig:
                break


def cmd_status(cfg: Config) -> int:
    from .ledger import report_status_line
    from .scheduler import pacing
    from .usage import UsageFetchError, fetch_usage

    try:
        snap = fetch_usage(cfg)
    except UsageFetchError as exc:
        print(f"live usage unavailable: {exc}")
        print(f"dispatch: {read_control(cfg)}")
        fork_line = fork_status_line(cfg)
        if fork_line is not None:
            print(fork_line)
        print(report_status_line(cfg))
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

    goal = read_goal(cfg)
    print(f"weekly goal: {goal:.0%} (weekly now {snap.seven_day.utilization:.0%})")
    stop = read_stop(cfg)
    if stop is not None:
        print(f"STOPPED: {stop.get('reason')} - goal {float(stop.get('goal', 0)):.0%}, "
              f"weekly {float(stop.get('weekly', 0)):.0%} at {stop.get('at')}")

    print(f"dispatch: {read_control(cfg)}")
    # The monitor session reports from this line: who is acting as director,
    # since when, in which mode and on which model.
    fork_line = fork_status_line(cfg)
    if fork_line is not None:
        print(fork_line)
    # The work-distribution report the overlay's VIEW REPORT button opens; the
    # monitor session reads its freshness and trigger from here.
    print(report_status_line(cfg))

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


def cmd_goal(cfg: Config, value: str | None) -> int:
    """Print the weekly goal, or set it from 0.85 / 85 / 85%."""
    if value is None:
        goal, source = read_goal_source(cfg)
        print(f"weekly goal: {goal:.0%} (source: {source})")
        stop = read_stop(cfg)
        if stop is not None:
            print(f"stop point active: {stop.get('reason')} "
                  f"({float(stop.get('weekly', 0)):.0%} weekly at {stop.get('at')})")
        return 0
    try:
        goal = parse_goal(value)
    except ValueError:
        print(f"error: cannot read '{value}' as a goal; use 0.85, 85 or 85%")
        return 1
    write_goal(cfg, goal)
    print(f"weekly goal: {goal:.0%} (source: {read_goal_source(cfg)[1]})")
    return 0


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    """Generate the work-distribution report now, and optionally open it."""
    from .ledger import generate, latest_report, open_report

    since = None
    if getattr(args, "since", None):
        since = parse_iso(args.since)
        if since is None:
            print(f"error: cannot read '{args.since}' as an ISO timestamp")
            return 1
    try:
        page = generate(cfg, "manual", since=since,
                        hours=getattr(args, "hours", None))
        print(f"wrote {page}")
        print(f"latest: {cfg.reports_dir / 'latest.html'}")
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: report generation failed ({exc})")
        if latest_report(cfg) is None:
            return 1
    if getattr(args, "open", False):
        opened = open_report(cfg)
        print(f"opened {opened}" if opened else "no report to open yet")
    return 0


def cmd_graph(cfg: Config, assignments: list[str] | None) -> int:
    """Print the agentic graph, or set tiers from `tier.field=value` pairs."""
    from .graph import (
        format_tiers,
        graph_line,
        known_models,
        override_warning,
        parse_assignments,
        read_graph_source,
        validate_graph,
        write_graph,
    )

    ignored = override_warning(cfg)
    graph, source = read_graph_source(cfg)
    if assignments:
        # Only the assignments given are persisted: state/graph.json is a patch
        # over config.json, not a snapshot of it, so setting the worker count
        # never freezes the models the config declares.
        patch, errors = parse_assignments(assignments)
        for error in errors:
            print(f"error: {error}")
        if errors:
            return 1
        write_graph(cfg, patch)
        graph, source = read_graph_source(cfg)
    print(f"agentic graph (source: {source})")
    if ignored and not assignments:
        # The read path swallows a broken override on purpose; without this the
        # only symptom would be the numbers quietly staying at config.json's.
        print(ignored)
    for line in format_tiers(graph):
        print(line)
    for warning in validate_graph(graph, known_models(cfg)):
        print(warning)
    print(f"fork prompt line: {graph_line(graph)}")
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
    goal_p = sub.add_parser(
        "goal", help="show or set the weekly goal (the loop's stopping point)")
    goal_p.add_argument("value", nargs="?", default=None,
                        help="0.85, 85 or 85%% - omit to print the current goal")
    sub.add_parser("list", help="list tasks")
    sub.add_parser("overlay", help="always-on-top panel docked to the Sundial layer")

    rep_p = sub.add_parser(
        "report", help="generate the work-distribution report (reports/latest.html)")
    rep_p.add_argument("--since", default=None,
                       help="window start as an ISO timestamp")
    rep_p.add_argument("--hours", type=float, default=None,
                       help="window length in hours, ending now")
    rep_p.add_argument("--open", action="store_true",
                       help="open reports/latest.html when it is written")

    graph_p = sub.add_parser(
        "graph", help="show or set the agentic graph (executive/advisory/workers)")
    graph_p.add_argument("action", nargs="?", choices=("set",), default=None,
                         help="'set' to assign tiers; omit to print the graph")
    graph_p.add_argument("assignments", nargs="*", metavar="tier.field=value",
                         help="e.g. workers.count=20 advisory.model=claude-opus-5")

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
        if args.cmd == "goal":
            return cmd_goal(cfg, args.value)
        if args.cmd == "report":
            return cmd_report(cfg, args)
        if args.cmd == "graph":
            if args.action != "set" and args.assignments:
                print("error: use 'graph set tier.field=value'")
                return 1
            return cmd_graph(cfg, args.assignments if args.action == "set" else None)
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
