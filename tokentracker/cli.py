from __future__ import annotations

import argparse
import json
import os
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
    """The FULL THROTTLE manual override.

    One reader, in the allocator, because the override is the one thing that
    outranks it: the loop, the fork's brief and the panel all have to agree on
    whether the allocator is being ignored right now.
    """
    from .allocator import throttle_active

    return throttle_active(cfg)


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


def _tasks_sig(cfg: Config) -> float | None:
    try:
        return cfg.tasks_file.stat().st_mtime
    except OSError:
        return None


def _wake_sig(cfg: Config) -> tuple[float | None, ...]:
    # Any operator switch (full throttle, start/stop, weekly goal) should cut
    # the sleep short so a click takes effect within seconds, not a whole poll
    # period - a goal dragged down under the current weekly stops the loop now.
    #
    # tasks.json is in here for the same reason and one more: `tracker.py add`
    # writes that file, so the mtime IS the wake signal, and a task queued from
    # a second terminal starts within seconds instead of at the next poll.
    return (_throttle_sig(cfg), _control_sig(cfg), _goal_sig(cfg),
            _tasks_sig(cfg))


def _fork_prompt(cfg: Config) -> str:
    """The director brief handed to the fork; config.json is authoritative.

    The `{graph}` placeholder is expanded here, at launch, so the fork is told
    the agentic graph that is in force this poll - the graph the allocator has
    budgeted out of the operator's ceiling, including a worker count the
    overlay changed a minute ago and an advisory count the ladder just stepped.
    """
    from .graph import allocated_line

    text = str(getattr(cfg, "throttle_prompt", "") or "").strip() or THROTTLE_PROMPT
    if "{graph}" in text:
        text = text.replace("{graph}", allocated_line(cfg))
    return text


def _fork_model(cfg: Config) -> str | None:
    """The executive tier's model, which is what the director fork runs on.

    The graph first and `throttle_model` only under it: `apply_graph` derives
    the scalar from the graph every poll, but a bare CLI Config never runs that
    derivation, and the graph is the thing an operator edits.
    """
    from .graph import EXECUTIVE, read_graph

    tier = str(read_graph(cfg)[EXECUTIVE].get("model") or "")
    return tier or (cfg.throttle_model or None)


def _refresh_fork_spec(cfg: Config, task: TaskSpec) -> bool:
    """Point the fork row at the graph, brief and session in force right now.

    Returns True when anything changed. Run before every launch, not only on a
    re-arm: the row can sit `pending` for polls behind the concurrency budget
    or the cooldown, config.json is re-read on every one of those polls, and a
    row armed on the old executive model would otherwise launch on it. That is
    the failure of 2026-09-03 19:48 UTC, one layer down from the reload.
    """
    before = (task.model, task.prompt, task.resume_session)
    task.model = _fork_model(cfg)
    task.prompt = _fork_prompt(cfg)
    task.resume_session = cfg.main_session_ids[0]
    return before != (task.model, task.prompt, task.resume_session)


def _reload_config(cfg: Config) -> list[str]:
    """Re-read config.json when it changed on disk; returns what to log.

    Called at the top of every tick, so an edit to the agentic graph takes
    effect on the next poll instead of at the next restart. The graph is named
    before and after because that is the change that actually matters: the
    override in state/graph.json carries only the fields it was given (usually
    the worker count), so config.json is where the models come from.

    Except for the fields the override *does* name. state/graph.json wins field
    by field, so an executive model edited in config.json while the override
    pins that field is read, applied to `cfg.graph`, and then ignored by every
    reader - and the "graph changed" line above compares the merged graph with
    itself, so it says nothing. Those pins are named here, once per reload that
    touched one, because a reload that changes nothing is otherwise silent.
    """
    from .config import reload_config
    from .graph import (
        changed_fields,
        config_fields,
        overlay_label,
        pin_notes,
        read_graph,
    )

    before = overlay_label(read_graph(cfg))
    declared = config_fields(cfg)
    changed = reload_config(cfg)
    if changed is None:
        return []
    lines = [f"config reloaded: {', '.join(changed) or 'no field changed'}"]
    after = overlay_label(read_graph(cfg))
    if after != before:
        lines.append(f"graph changed: {before} -> {after}")
    lines += pin_notes(cfg, changed_fields(declared, config_fields(cfg)))
    return lines


def _fork_cooldown(cfg: Config) -> float:
    """The fork re-arm cooldown in force: the ALLOCATED one, not the configured.

    Fork cadence is the third rung of the allocation ladder (x2, then x4, then
    the configured maximum), so the gate that decides when the director may be
    relaunched has to read the allocation rather than `fork_cooldown_seconds`
    straight off the Config - otherwise the rung would be decided every poll
    and applied by nobody.
    """
    from .allocator import allocate

    return max(0.0, float(allocate(cfg).fork_cooldown_seconds))


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


def _fork_wanted(cfg: Config, mode: str, control: str, throttle: bool,
                 dispatcher: Dispatcher | None = None) -> bool:
    """Whether the continue fork should be ensured on this tick.

    The handover is only armed while dispatch is actually running, the weekly
    goal has not been reached, and the loop is in a working mode: pace (normal
    dispatch, when fork_in_pace is on) or surge (full throttle). blocked,
    yield, coast and stopped never hand the director job over.

    A queued or running screenshot pass also disarms it. That is the whole
    point of the snapshot policy: it fires when the budget is nearly gone, and
    a fork re-armed beside it would take the concurrency slot and spend the
    reserve the pacer set aside for exactly this task.
    """
    if not cfg.main_session_ids or not cfg.throttle_fork_enabled:
        return False
    if control == STOPPED or mode not in FORK_MODES:
        return False
    if read_stop(cfg) is not None:
        return False
    if dispatcher is not None:
        from .snapshot import pending_or_running

        if pending_or_running(dispatcher) is not None:
            return False
    return True if throttle else bool(getattr(cfg, "fork_in_pace", True))


def _fork_launch_gate(cfg: Config, fork_wanted: bool, now):
    """The gate the dispatcher asks before it starts a pending row.

    Only the director fork is ever held. A fallback requeue is written inside
    `dispatcher.apply()`, i.e. *after* this tick already decided whether the
    handover is armed, so without this a fork that died on a usage limit would
    be started again in a mode that wants no fork at all - and inside the
    re-arm cooldown that exists to stop exactly that kind of relaunch storm.
    Everything else is the decision's own business (target_concurrency and
    allow_heavy already gate it), so the gate says nothing about it.
    """
    def gate(task: TaskSpec) -> str | None:
        if task.id != THROTTLE_TASK_ID:
            return None
        if not fork_wanted:
            return "fork not armed in this mode"
        if not _rearm_ready(cfg, task, now):
            return "fork re-arm cooldown has not elapsed"
        return None
    return gate


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
            model=_fork_model(cfg),
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
        _refresh_fork_spec(cfg, task)
        dispatcher.set_status(THROTTLE_TASK_ID, "pending")
    elif task.status == "pending" and _refresh_fork_spec(cfg, task):
        # Armed on an earlier tick and still waiting to start: config.json may
        # have been edited since, and this tick's launch batch is about to run.
        dispatcher.save()
    # running: the handover already stands, nothing to re-arm.


def _bar(frac: float) -> str:
    frac = min(max(frac, 0.0), 1.0)
    filled = round(frac * BAR_WIDTH)
    return f"[{'#' * filled}{'.' * (BAR_WIDTH - filled)}] {frac:.1%}"


def _write_state(cfg: Config, payload: dict) -> None:
    tmp = cfg.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
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


def _snapshot_state(cfg: Config, allocation, now) -> dict:
    """What the panel draws for the screenshot policy. Never raises."""
    from .snapshot import age, enabled, read_state, status_line

    try:
        return {"enabled": enabled(cfg),
                "line": status_line(cfg, allocation.buckets, now),
                "age": age(cfg, now),
                **{k: v for k, v in read_state(cfg).items() if k != "commits"}}
    except Exception:
        return {}


def _tick(
    cfg: Config, dispatcher: Dispatcher, history, do_fetch: bool = True,
) -> tuple[Decision, list[str], Exception | None, "object | None", bool]:
    from .activity import detect_activity
    from .allocator import allocate, evaluate, tick_notes
    from .graph import apply_graph
    from .ledger import maybe_refresh_tiers, maybe_report
    from .scheduler import decide, decide_local, normalize, pacing
    from .snapshot import maybe_enqueue as maybe_snapshot
    from .snapshot import note_finished as note_snapshot
    from .usage import (
        UsageFetchError,
        compute_burn_rates,
        fetch_usage,
        learned_class_rates,
        load_calibration,
    )

    now = utcnow()
    # config.json is re-read every poll, before anything reads a value off the
    # Config: an edited graph must reach this tick's fork launch, not the next
    # restart. The graph is then re-derived (the overlay's worker -/+ writes
    # state/graph.json) into the concurrency the scheduler reads below.
    notes = _reload_config(cfg)
    apply_graph(cfg)
    # The panel's token-share bars, rebuilt off-thread when they are due.
    tiers_note = maybe_refresh_tiers(cfg, now)
    if tiers_note is not None:
        notes.append(tiers_note)
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
        actions = notes + dispatcher.apply(decision, now)
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

    # The allocation is decided here, once, from this poll's reading: each
    # bucket's burn rate against the time left to its own reset. It writes
    # state/allocation.json, and the apply_graph below folds the graph it
    # decided onto the Config - so the concurrency `decide` reads, the cooldown
    # the fork's re-arm gate reads and the {graph} line the fork is handed are
    # all this poll's allocation rather than the last one's.
    standing = allocate(cfg)
    allocation = evaluate(cfg, snap, now)
    notes += tick_notes(standing, allocation)
    apply_graph(cfg)

    # The weekly goal is checked before anything is launched, so the poll that
    # crosses it is also the poll that stops dispatch. apply_goal_stop may have
    # just written the control file, hence the re-read.
    goal = read_goal(cfg)
    stop, goal_line = apply_goal_stop(cfg, snap.seven_day.utilization, now)
    if goal_line is not None:
        control = read_control(cfg)

    # The screenshot policy, off this poll's own forecasts, and BEFORE the
    # decision is made: the row has to exist for `_fork_wanted` to see it and
    # for this tick's launch batch to start it, priority 200 ahead of the
    # fork's 100. `goal_line is not None` is the poll that wrote the stop.
    snapshot_line = maybe_snapshot(cfg, dispatcher, allocation.buckets, now,
                                   stop_written=(goal_line is not None
                                                 and stop is not None))
    if snapshot_line is not None:
        notes.append(snapshot_line)

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
    fork_wanted = _fork_wanted(cfg, decision.mode, control, throttle, dispatcher)
    # apply() below may requeue the fork onto its tier's fallback (a limit
    # exit), which happens after this decision is made; the gate carries the
    # decision into the launch batch so that requeue obeys it too.
    dispatcher.launch_gate = _fork_launch_gate(cfg, fork_wanted, now)
    if fork_wanted:
        _ensure_throttle_task(cfg, dispatcher, now)
    else:
        stale_task = dispatcher.get(THROTTLE_TASK_ID)
        if stale_task is not None and stale_task.status == "pending":
            dispatcher.set_status(THROTTLE_TASK_ID, "killed")

    decision.local_concurrency = decide_local(decision, activity, cfg, now)
    # Operator STOP is the last word: it zeroes both launch budgets, so apply()
    # reaps and adopts as usual but starts nothing new.
    decision = gate_decision(decision, control)
    actions = notes + dispatcher.apply(decision, now)
    if goal_line is not None:
        actions.insert(0, goal_line)
    # Report triggers run after the reap, so a fork that finished on this very
    # tick is already marked done; generation itself is off-thread.
    report_line = maybe_report(cfg, dispatcher, before=fork_state,
                               control=control, stop=stop, now=now)
    if report_line is not None:
        actions.append(report_line)
    # A snapshot that finished on this tick's reap reports its commit hash;
    # record it so `status` and the ledger's milestone table can name it.
    commit_line = note_snapshot(cfg, dispatcher)
    if commit_line is not None:
        actions.append(commit_line)

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
        # The graph the loop actually ran this tick, beside the ceiling it came
        # from. state/allocation.json carries the buckets behind it.
        "allocation": {**allocation.to_dict(),
                       "reasons": allocation.reasons,
                       "notes": allocation.notes},
        # The screenshot policy's own line, so the panel draws it without
        # recomputing the forecast on every frame.
        "snapshot": _snapshot_state(cfg, allocation, now),
    })
    return decision, actions, fetch_exc, snap, rolled


def supervising(cfg: Config, no_supervise: bool = False) -> bool:
    """Whether this run merges tasks.json from disk before every apply.

    On by default, and the default is load-bearing. A loop started without it
    holds the queue in memory and writes it back on every save, so a row added
    by `tracker.py add` from another terminal is silently deleted at the next
    poll - which is exactly what happened to a queued task on 2026-09-03 21:45
    UTC. `--no-supervise` is the deliberate opt-out; `config.json`'s
    `supervise` is the standing setting.
    """
    return False if no_supervise else bool(getattr(cfg, "supervise", True))


def cmd_run(cfg: Config, once: bool, no_supervise: bool = False) -> int:
    """The control loop, with the loop-exit report bolted onto both exits.

    ctrl-c and a SystemExit both mean dispatch has stopped, which is one of the
    report triggers; the report is written synchronously here because the
    process is about to end.
    """
    from .ledger import report_on_exit

    try:
        return _run_loop(cfg, once, no_supervise)
    except (KeyboardInterrupt, SystemExit):
        print("stopped; running background tasks continue detached")
        line = report_on_exit(cfg)
        if line is not None:
            print(line)
        return 0


def _run_loop(cfg: Config, once: bool, no_supervise: bool = False) -> int:
    from .clock import describe
    from .usage import RateLimitedError, UsageHistory

    from .graph import (
        known_models,
        override_warning,
        overlay_label,
        pin_notes,
        read_graph_source,
        validate_graph,
    )

    supervise = supervising(cfg, no_supervise)
    dispatcher = Dispatcher(cfg, supervise=supervise)
    history = UsageHistory(cfg)
    print(f"TokenDistributor loop started (poll every {cfg.poll_seconds}s, ctrl-c to stop)")
    print(describe(cfg))
    if supervise:
        print("supervising tasks.json: external `tracker.py add` rows are "
              "merged in before every apply")
    else:
        print("WARNING: --no-supervise; a task added while this loop runs will "
              "be overwritten at the next poll")
    # Named with its source: the numbers in force can come from either file, and
    # "why is config.json not applying" has no other symptom.
    graph, source = read_graph_source(cfg)
    print(f"graph: {overlay_label(graph)} (source: {source})")
    ignored = override_warning(cfg)
    if ignored:
        print(ignored)
    # Every field the override pins, named at startup: those are the fields an
    # edit to config.json can never move, and the config re-read below would
    # otherwise report "config reloaded: graph" for an edit that did nothing.
    for note in pin_notes(cfg):
        print(note)
    # A warning, not a refusal: an unknown id is usually a new model, and a
    # graph that breaks the superiority rule still has to run.
    for warning in validate_graph(graph, known_models(cfg), cfg):
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
    from .allocator import status_line
    from .clock import describe, fmt_local
    from .graph import active_graph, active_label
    from .ledger import report_status_line, tiers_status_line
    from .scheduler import pacing
    from .snapshot import status_line as snapshot_line
    from .usage import UsageFetchError, fetch_usage

    # Every absolute time below is the operator's clock; every stored one stays
    # UTC. The zone, and how it was resolved, is named once at the top so a
    # reading that looks an hour out has somewhere to be checked.
    print(describe(cfg))
    try:
        snap = fetch_usage(cfg)
    except UsageFetchError as exc:
        print(f"live usage unavailable: {exc}")
        print(f"dispatch: {read_control(cfg)}")
        fork_line = fork_status_line(cfg)
        if fork_line is not None:
            print(fork_line)
        print(report_status_line(cfg))
        print(tiers_status_line(cfg))
        print(snapshot_line(cfg))
        print(status_line(cfg))
        if cfg.state_file.exists():
            state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
            print("last known state from "
                  f"{fmt_local(state.get('at'), '%a %H:%M', cfg, with_label=True)}:")
            print(json.dumps(state.get("usage"), indent=2))
        return 1

    now = utcnow()
    p = pacing(snap, cfg, now)
    print("weekly  " + _bar(snap.seven_day.utilization)
          + f"  resets in {p['time_left_h']:.1f}h ("
          + fmt_local(snap.seven_day.resets_at, "%a %H:%M", cfg, with_label=True)
          + ")")
    print("5-hour  " + _bar(snap.five_hour.utilization)
          + "  resets "
          + fmt_local(snap.five_hour.resets_at, "%H:%M", cfg, with_label=True))
    for name, w in snap.extra.items():
        print(f"{name:<7} " + _bar(w.utilization)
              + "  resets "
              + fmt_local(w.resets_at, "%a %H:%M", cfg, with_label=True))
    held = p.get("snapshot_reserve", 0.0)
    shots = f", shots reserve {held:.1%}" if held else ""
    print(f"pace: elapsed {p['elapsed_frac']:.1%}, reserve {p['reserve']:.1%}"
          f"{shots}, required {p['required_total_pct_per_hr']:.2f}%/h")

    goal = read_goal(cfg)
    print(f"weekly goal: {goal:.0%} (weekly now {snap.seven_day.utilization:.0%})")
    stop = read_stop(cfg)
    if stop is not None:
        print(f"STOPPED: {stop.get('reason')} - goal {float(stop.get('goal', 0)):.0%}, "
              f"weekly {float(stop.get('weekly', 0)):.0%} at "
              f"{fmt_local(stop.get('at'), '%a %H:%M', cfg, with_label=True)}")

    print(f"dispatch: {read_control(cfg)}")
    # The monitor session reports from this line: who is acting as director,
    # since when, in which mode and on which model.
    fork_line = fork_status_line(cfg)
    if fork_line is not None:
        print(fork_line)
    # The work-distribution report the overlay's VIEW REPORT button opens; the
    # monitor session reads its freshness and trigger from here.
    print(report_status_line(cfg))
    # The same split the panel's ladder bars draw: where the window's input and
    # output tokens went, per tier of the agentic graph.
    print(tiers_status_line(cfg))
    # What each tier is running on right now against what config.json asks for;
    # printed only when they differ, which is the only time it is news.
    active = active_label(active_graph(cfg))
    if active:
        print(f"active vs configured: {active}")
    # One line for the automatic allocation: the rung the ladder is on, what it
    # moved, and the pace reading that moved it.
    print(status_line(cfg))
    # And one for the screenshot policy: how old the gallery is, and which of
    # the two triggers comes next.
    print(snapshot_line(cfg, now=now))

    if cfg.state_file.exists():
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        d = state.get("decision", {})
        print("last decision ["
              f"{fmt_local(state.get('at'), '%a %H:%M', cfg, with_label=True)}]: "
              f"{d.get('mode')} conc={d.get('target_concurrency')} - {d.get('reason')}")

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


def cmd_alloc(cfg: Config) -> int:
    """Print the buckets, their pace, the ladder rung and why it is there.

    The read-only view of state/allocation.json: it never evaluates, so running
    it does not move the graph under the loop that owns that decision.
    """
    from .allocator import (
        MAX_STEP,
        STEP_LABELS,
        allocate,
        format_buckets,
        max_cooldown,
        min_advisory,
        min_workers,
    )
    from .clock import fmt_local, label
    from .graph import ADVISORY, TIERS, WORKERS, read_graph, tiers_of

    allocation = allocate(cfg)
    if allocation.generated_at is None:
        print("no allocation yet: state/allocation.json is written by the loop "
              "(python tracker.py run); the configured graph is in force")
    else:
        # Local for the reader, with the stored UTC beside it: this line is
        # quoted into reports, and the file itself is on the other clock.
        print(f"allocation at "
              f"{fmt_local(allocation.generated_at, '%a %H:%M:%S', cfg)} "
              f"{label(cfg)} ({allocation.generated_at})")
    print(f"step {allocation.step}/{MAX_STEP}: "
          f"{STEP_LABELS.get(allocation.step, '?')}")
    if allocation.override:
        print("FULL THROTTLE (manual override) is on: the allocator is ignored "
              f"and the workers are pinned to x{allocation.worker_count}")
    if allocation.buckets:
        for line in format_buckets(allocation, cfg):
            print(line)
    else:
        print("  no bucket readings yet")
    counts = allocation.counts()
    for tier in TIERS:
        block = allocation.graph.get(tier, {})
        allocated, configured = counts[tier]
        model = str(block.get("model") or "(account default)")
        cfg_model = str(allocation.configured.get(tier, {}).get("model") or "")
        # Allocated first, the ceiling it came out of in brackets after it.
        extras = []
        if cfg_model and cfg_model != block.get("model"):
            extras.append(f"cfg {cfg_model}")
        if allocated != configured:
            extras.append(f"cfg x{configured}")
        tail = f" ({', '.join(extras)})" if extras else ""
        if tier == ADVISORY:
            tail += f" effort {allocation.advisory_effort}"
        if tier == WORKERS:
            tail += f" surge x{block.get('surge_count', allocated)}"
        print(f"  {tier:<9} {model:<28} x{allocated}{tail}")
    print(f"fork cadence {allocation.fork_cooldown_seconds:.0f}s "
          f"(max {max_cooldown(cfg):.0f}s); floors: advisory "
          f"{min_advisory(cfg)}, workers {min_workers(cfg)}")
    for reason in allocation.reasons:
        print(f"  reason: {reason}")
    for note in allocation.notes:
        print(f"  note: {note}")
    ceiling = tiers_of(read_graph(cfg))[2]
    print(f"ceiling (configured): workers x{ceiling['count']} "
          f"(surge x{ceiling['surge_count']}) - config.json and "
          "state/graph.json are never written by the allocator")
    # The screenshot policy reads the same buckets, so its two triggers belong
    # under the same table.
    from .snapshot import status_line as snapshot_line

    print(snapshot_line(cfg, allocation.buckets or None))
    return 0


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    """Generate the work-distribution report now, and optionally open it."""
    from .clock import fmt_local, stamp
    from .ledger import generate, latest_report, open_report

    since = None
    if getattr(args, "since", None):
        since = parse_iso(args.since)
        if since is None:
            print(f"error: cannot read '{args.since}' as an ISO timestamp")
            return 1
        # `--since` is given as ISO UTC and echoed back on the operator's clock,
        # so a window typed in one zone is not read as the other.
        print(f"window starts {fmt_local(since, '%a %H:%M', cfg, with_label=True)}")
    try:
        page = generate(cfg, "manual", since=since,
                        hours=getattr(args, "hours", None))
        print(f"wrote {page} at {stamp(None, '%a %H:%M:%S', cfg)}")
        print(f"latest: {cfg.reports_dir / 'latest.html'}")
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: report generation failed ({exc})")
        if latest_report(cfg) is None:
            return 1
    if getattr(args, "open", False):
        opened = open_report(cfg)
        print(f"opened {opened}" if opened else "no report to open yet")
    return 0


def cmd_graph(cfg: Config, assignments: list[str] | None,
              force: bool = False) -> int:
    """Print the agentic graph, or set tiers from `tier.field=value` pairs.

    A `set` that would break the superiority rule (executive >= advisory >=
    workers, and no fallback above its own primary) is refused: printing the
    graph warns about a violation that is already on disk, but there is no
    reason to *write* one by accident. `--force` says it was not an accident.
    """
    from .graph import (
        ORDER_RULE,
        format_tiers,
        graph_line,
        known_models,
        normalize,
        order_warnings,
        override_warning,
        parse_assignments,
        pin_notes,
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
        # Only what THIS assignment breaks. A violation already standing on
        # disk (set with --force, or hand-edited into config.json) used to
        # refuse every later set, including `graph set workers.count=20` -
        # which breaks no order at all - and left the operator with no way
        # back except another --force.
        standing = set(order_warnings(graph, cfg))
        violations = [w for w in order_warnings(normalize(patch, graph), cfg)
                      if w not in standing]
        if violations and not force:
            for warning in violations:
                print(f"error: {warning.removeprefix('warning: ')}")
            print(ORDER_RULE)
            print("refusing to break the order; pass --force to set it anyway")
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
    # Which of those numbers config.json no longer answers for. Printed on the
    # read path as well as after a `set`, because "I edited config.json and
    # nothing happened" is answered here or nowhere.
    for note in pin_notes(cfg):
        print(note)
    for warning in validate_graph(graph, known_models(cfg), cfg):
        print(warning)
    # The tiers above are the CEILING. What is actually running is the
    # allocation, which is only ever a step down from it (or the worker lanes a
    # step up toward surge), so it is named here rather than left to `alloc`.
    from .allocator import allocate
    from .clock import fmt_local, label as tz_label

    allocation = allocate(cfg)
    if allocation.differs or allocation.reasons:
        when = (f" (allocated {fmt_local(allocation.generated_at, '%a %H:%M', cfg)}"
                f" {tz_label(cfg)})" if allocation.generated_at else "")
        print(f"allocated now: {allocation.label()}{when}")
        for reason in allocation.reasons:
            print(f"  reason: {reason}")
    print(f"fork prompt line: {graph_line(allocation.graph, allocation)}")
    return 0


def cmd_pricing(cfg: Config, assignments: list[str] | None) -> int:
    """Print the price table, or set prices from `model.field=value` pairs."""
    from .clock import stamp
    from .pricing import (
        default_row,
        format_table,
        override_warning,
        parse_assignments,
        read_pricing_source,
        write_pricing,
    )

    ignored = override_warning(cfg)
    table, source = read_pricing_source(cfg)
    if assignments:
        # A patch, like `graph set`: only the fields named here stop following
        # config.json, so correcting one output price never freezes the rest of
        # the published table.
        patch, errors = parse_assignments(assignments)
        for error in errors:
            print(f"error: {error}")
        if errors:
            return 1
        write_pricing(cfg, patch)
        table, source = read_pricing_source(cfg)
    # The per-row `checked` values are dates, not clock times, so they are left
    # as written; the read itself is stamped on the operator's clock.
    print(f"model prices, USD per 1M tokens (source: {source}, read "
          f"{stamp(None, '%a %H:%M', cfg)})")
    if ignored and not assignments:
        print(ignored)
    for line in format_table(table, default_row(cfg)):
        print(line)
    return 0


def loop_liveness(cfg: Config, now=None) -> tuple[bool, str]:
    """(a loop is live, how it will treat a task added right now).

    state/state.json is stamped at the top of every tick, so its age is the
    loop's own heartbeat. Anything inside a couple of poll periods is live.
    The line matters because `add` is the command whose failure mode is
    silent: on 2026-09-03 21:45 UTC a queued task vanished at the next poll,
    and the only way to have known was to check whether the running loop was
    supervising.
    """
    now = now or utcnow()
    stamp = None
    try:
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        stamp = parse_iso(state.get("at")) if isinstance(state, dict) else None
    except (OSError, ValueError, TypeError, AttributeError):
        stamp = None
    if stamp is None:
        return False, ("no loop heartbeat in state/state.json: the task waits "
                       "on disk until `py -3.13 tracker.py run` is started")
    age = max(0.0, (now - stamp).total_seconds())
    limit = max(2.5 * float(getattr(cfg, "poll_seconds", 300) or 300), 120.0)
    if age > limit:
        return False, (f"last loop tick was {age / 60:.0f}m ago (stale): the "
                       "task waits on disk until a loop runs again")
    return True, (f"a live loop (last tick {age:.0f}s ago) picks it up within "
                  "seconds - tasks.json is its wake signal")


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
    print(f"added task {task.id} ({task.weight}, priority {task.priority})")
    # `add` already wrote tasks.json, and its mtime is what `_wake_sig` watches
    # - but a save inside one filesystem timestamp tick can land on the mtime
    # the file already had, and then the sleeping loop sees no change at all.
    # One explicit touch costs nothing and closes that window.
    try:
        os.utime(cfg.tasks_file, None)
    except OSError:
        pass
    live, note = loop_liveness(cfg)
    print(("queued: " if live else "queued, but ") + note)
    if live and not bool(getattr(cfg, "supervise", True)):
        print("warning: config.json sets supervise=false, so a loop started "
              "with it will overwrite this row at its next save")
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
    run_p.add_argument("--no-supervise", action="store_true",
                       help="do not merge external tasks.json edits before "
                            "each apply (a task added while this loop runs "
                            "will be overwritten)")
    sub.add_parser("status", help="live usage, pacing and queue")
    goal_p = sub.add_parser(
        "goal", help="show or set the weekly goal (the loop's stopping point)")
    goal_p.add_argument("value", nargs="?", default=None,
                        help="0.85, 85 or 85%% - omit to print the current goal")
    sub.add_parser(
        "alloc", help="the automatic allocation: buckets, pace, ladder step")
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
                         help="e.g. workers.count=20 advisory.model=claude-opus-5 "
                              "executive.fallback=claude-fable-5")
    graph_p.add_argument("--force", action="store_true",
                         help="set an assignment that breaks the superiority "
                              "rule (executive >= advisory >= workers)")

    price_p = sub.add_parser(
        "pricing", help="show or set the per-model list prices the report bills at")
    price_p.add_argument("action", nargs="?", choices=("set",), default=None,
                         help="'set' to edit prices; omit to print the table")
    price_p.add_argument("assignments", nargs="*", metavar="model.field=usd",
                         help="e.g. claude-opus-5.output=25 "
                              "claude-opus-5.source=https://...")

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
            return cmd_run(cfg, args.once,
                           bool(getattr(args, "no_supervise", False)))
        if args.cmd == "status":
            return cmd_status(cfg)
        if args.cmd == "goal":
            return cmd_goal(cfg, args.value)
        if args.cmd == "alloc":
            return cmd_alloc(cfg)
        if args.cmd == "report":
            return cmd_report(cfg, args)
        if args.cmd == "graph":
            if args.action != "set" and args.assignments:
                print("error: use 'graph set tier.field=value'")
                return 1
            return cmd_graph(cfg, args.assignments if args.action == "set" else None,
                             force=bool(getattr(args, "force", False)))
        if args.cmd == "pricing":
            if args.action != "set" and args.assignments:
                print("error: use 'pricing set model.field=usd'")
                return 1
            return cmd_pricing(cfg, args.assignments if args.action == "set" else None)
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
