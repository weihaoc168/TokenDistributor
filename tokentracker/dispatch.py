from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from typing import IO

from .config import Config
from .graph import (
    EXECUTIVE,
    WORKERS,
    clear_limited,
    limited_model,
    may_fall_back,
    read_graph,
    read_limited,
    write_limited,
)
from .handover import (
    FORK_FALLBACK_MODEL,
    FORK_MODES,
    FORK_TASK_ID,
    finish_handover,
    write_handover,
)
from .models import Decision, QueueStats, TaskSpec, parse_iso, utcnow

LOCAL_HEALTH_TIMEOUT = 4.0
# "the model is busy", not "the task is wrong": a 529, an overload, or any of
# the limit wordings the CLI prints when the account, the session or the
# five-hour window is out of room. Matched against the `claude -p` result JSON
# and the run's stderr; 529 needs digit boundaries so a token count or a task
# id carrying those three digits is not read as a status code.
LIMIT_PATTERN = re.compile(
    r"(?<!\d)529(?!\d)"
    r"|overloaded"
    r"|rate[ _-]?limit"
    r"|session limit"
    r"|usage limit"
    r"|hit your",
    re.IGNORECASE,
)
# How much of a stderr file is worth reading back: the limit wording is in the
# last few lines, and these files can hold a whole session's chatter.
ERR_TAIL_BYTES = 8000


def limited_reason(text: object) -> str | None:
    """The marker saying the *model* was the problem, or None. Never raises."""
    match = LIMIT_PATTERN.search(str(text or ""))
    return match.group(0).lower() if match else None


def task_tier(task: TaskSpec) -> str:
    """Which tier of the agentic graph a task row belongs to.

    Only two of the three can own a row: the director fork is the executive,
    everything else the loop launches is a worker lane. The advisory tier runs
    as review lenses *inside* the fork's own workflows, so it never has a row
    here - which is also why its fallback is carried in the fork's prompt.
    """
    return EXECUTIVE if task.id == FORK_TASK_ID else WORKERS
_PROCESS_QUERY = 0x00101000
_WAIT_TIMEOUT = 0x102


def pid_alive(pid: int | None) -> bool:
    """Best-effort liveness probe for a process this tracker did not spawn.

    Windows reuses pids, so a false positive is possible; for adoption that
    only delays finalization until the impostor exits, which is acceptable.
    """
    if not pid:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
    except (ImportError, AttributeError):
        return False
    handle = k32.OpenProcess(_PROCESS_QUERY, False, int(pid))
    if not handle:
        return False
    try:
        return k32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)
LOCAL_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def local_env(cfg: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a claude -p session pinned to the local FreeToken engine.

    Mirrors claude-local.cmd / `ft launch claude`: base URL + token swap, every
    model alias mapped to the local model, and a generous API timeout because a
    single-GPU decode turn is slow.
    """
    env = dict(os.environ if base is None else base)
    env.pop("ANTHROPIC_API_KEY", None)
    env["ANTHROPIC_BASE_URL"] = cfg.local_base_url
    env["ANTHROPIC_AUTH_TOKEN"] = cfg.local_auth_token
    for key in LOCAL_MODEL_ENV_KEYS:
        env[key] = cfg.local_model
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(cfg.local_max_context_tokens)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(cfg.local_max_output_tokens)
    env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = "0"
    env["API_TIMEOUT_MS"] = str(cfg.local_api_timeout_ms)
    return env


class DispatchError(Exception):
    pass


class _Proc:
    def __init__(self, popen: subprocess.Popen, out: IO, err: IO) -> None:
        self.popen = popen
        self.out = out
        self.err = err


class Dispatcher:
    def __init__(self, cfg: Config, supervise: bool = False) -> None:
        # Only the run loop supervises: it alone may orphan-mark running rows
        # (a CLI invocation has an empty _procs by construction, so it must
        # never conclude that a task another process launched is dead).
        self.cfg = cfg
        self.supervise = supervise
        self._tasks: list[TaskSpec] = []
        self._procs: dict[str, _Proc] = {}
        self._local_start_ts: float | None = None
        self.local_engine_healthy: bool | None = None
        self._adopted: dict[str, int] = {}
        # task id -> the limit wording its last exit carried; drained by reap
        # (never inside _finalize_record, which runs while the finished process
        # is still in self._procs and would have its relaunch deleted again).
        self._pending_fallback: dict[str, str] = {}
        # Mode of the decision currently being applied; the handover record has
        # to say whether the director fork was launched in pace or under full
        # throttle, and apply() is the only place that knows.
        self.current_mode: str | None = None
        # Set by the run loop each tick: (task) -> a reason to hold this row
        # back, or None to let it start. The fork's re-arm cooldown and
        # `_fork_wanted` live in cli, not here, and a fallback requeue has to
        # obey them exactly like a fresh arm does. None = no gate (CLI use).
        self.launch_gate = None
        self.load()
        if not supervise:
            return
        changed = False
        now = utcnow()
        for task in self._tasks:
            if task.status == "running" and task.id not in self._procs:
                # A restart must not kill the bookkeeping of a session another
                # loop instance launched and left alive (the fork keeps working
                # detached); adopt it and finalize from its output file later.
                if pid_alive(task.pid):
                    self._adopted[task.id] = task.pid
                    continue
                task.status = "failed"
                task.error = "orphaned by tracker restart"
                # An orphan is an exit like any other: stamp finished_at (the
                # re-arm cooldown is measured from it) and close the handover.
                # Loop and fork usually die together - a reboot - and without
                # this the record stays at "started" for a director that no
                # longer exists, forever if the fork is never re-armed.
                task.finished_at = now.isoformat()
                self._note_fork_finish(task, now)
                changed = True
        if changed:
            self.save()

    def _read_tasks_file(self) -> list[TaskSpec]:
        if not self.cfg.tasks_file.exists():
            return []
        data = json.loads(self.cfg.tasks_file.read_text(encoding="utf-8"))
        return [TaskSpec.from_dict(d) for d in data.get("tasks", [])]

    def load(self) -> None:
        self._tasks = self._read_tasks_file()

    def sync_from_disk(self) -> None:
        """Merge external tasks.json edits (CLI add/requeue/cancel) into a
        long-running loop, which otherwise saves its own memory over them.

        Tasks with a live process are owned by this loop: memory wins. For
        everything else the disk version wins, so CLI edits stick; disk-only
        ids are adopted (external adds), memory-only non-running ids were
        deleted externally and are dropped.
        """
        try:
            disk = {t.id: t for t in self._read_tasks_file()}
        except (OSError, json.JSONDecodeError, TypeError, KeyError):
            return
        merged: list[TaskSpec] = []
        for task in self._tasks:
            if task.id in self._procs:
                merged.append(task)
                disk.pop(task.id, None)
            elif task.id in disk:
                merged.append(disk.pop(task.id))
        merged.extend(disk.values())
        self._tasks = merged

    def save(self) -> None:
        tmp = self.cfg.tasks_file.with_suffix(".tmp")
        payload = {"tasks": [t.to_dict() for t in self._tasks]}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.cfg.tasks_file)

    def tasks(self) -> list[TaskSpec]:
        return list(self._tasks)

    def get(self, task_id: str) -> TaskSpec | None:
        return next((t for t in self._tasks if t.id == task_id), None)

    def add(self, task: TaskSpec) -> None:
        if self.get(task.id) is not None:
            raise DispatchError(f"duplicate task id: {task.id}")
        self._tasks.append(task)
        self.save()

    def set_status(self, task_id: str, status: str) -> None:
        task = self.get(task_id)
        if task is None:
            raise DispatchError(f"unknown task id: {task_id}")
        task.status = status
        if status == "pending":
            task.error = None
            task.pid = None
            # A requeue is a fresh attempt: it gets its own one relaunch, and
            # the forced fallback is dropped so the primary is tried again -
            # `_while_limited` still routes it to the fallback for as long as
            # the mark stands, which is a detour rather than a demotion.
            task.fallback_from = None
            task.fallback_model = None
        self.save()

    def queue_stats(self) -> QueueStats:
        pending = [t for t in self._tasks if t.status == "pending"]
        return QueueStats(
            pending_heavy=sum(1 for t in pending if t.weight == "heavy"),
            pending_light=sum(1 for t in pending if t.weight != "heavy"),
            running=sum(1 for t in self._tasks if t.status == "running"),
            running_local=sum(
                1 for t in self._tasks
                if t.status == "running" and t.lane == "local"
            ),
        )

    def own_project_dirs(self) -> set[str]:
        try:
            from .activity import project_dir_name
        except ImportError:
            return set()
        return {project_dir_name(t.cwd) for t in self._tasks}

    def _finalize(self, task: TaskSpec, proc: _Proc, now: datetime) -> str:
        proc.out.close()
        proc.err.close()
        return self._finalize_record(task, proc.popen.returncode, now)

    def _finalize_record(self, task: TaskSpec, exit_code: int | None,
                         now: datetime) -> str:
        # exit_code None = adopted session (launched by a prior loop instance):
        # the exit code is unknowable, so judge by the output file alone.
        out_path = self.cfg.logs_dir / f"{task.id}.out.json"
        result: dict = {}
        text = ""
        try:
            text = out_path.read_text(encoding="utf-8", errors="replace")
            start = text.find("{")
            if start >= 0:
                result = json.loads(text[start:])
        except (OSError, json.JSONDecodeError):
            result = {}

        usage = result.get("usage") or {}
        tokens = sum(
            int(usage.get(k, 0) or 0)
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")
        )
        task.session_tokens = tokens or None
        # The session id claude minted for this run. `--fork-session` means the
        # id is only knowable from this result, and without it the ledger has
        # to guess which transcript on disk belonged to the fork.
        session_id = result.get("session_id")
        if isinstance(session_id, str) and session_id:
            task.fork_session_id = session_id
        cost = result.get("total_cost_usd")
        task.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
        task.finished_at = now.isoformat()

        ok = (exit_code == 0 or (exit_code is None and bool(result)))
        if ok and not result.get("is_error"):
            task.status = "done"
        else:
            task.status = "failed"
            task.error = str(result.get("result", ""))[:200] or f"exit code {exit_code}"

        # The same exit, read a second way: was it the task that failed, or the
        # model that was unavailable? Only the second earns a fallback.
        self._pending_fallback.pop(task.id, None)
        if task.status == "done":
            # The model that just finished answered fine, so if it was the one
            # marked limited the tier stops being demoted now rather than when
            # fallback_minutes runs out. Only ever for a run whose model is
            # known: an adopted row from a prior loop must not clear a mark
            # that was made for some other model.
            if task.model_used:
                clear_limited(self.cfg, task.model_used)
        else:
            reason = limited_reason(self._failure_text(task, text))
            if reason:
                self._pending_fallback[task.id] = reason

        self._note_fork_finish(task, now, tokens=tokens, cost_usd=task.cost_usd,
                               fork_session_id=result.get("session_id"))

        started = parse_iso(task.started_at) or now
        minutes = max((now - started).total_seconds() / 60, 0.1)
        if task.lane != "local":
            # Local-lane runs burn zero Claude budget; feeding them into the
            # burn-rate calibration would teach the pacer that heavy tasks are
            # free and make it overshoot the real weekly budget.
            try:
                from .usage import record_task_outcome
                record_task_outcome(self.cfg, task.weight, tokens, minutes)
            except ImportError:
                pass
        lane = " local" if task.lane == "local" else ""
        return f"task {task.id}: {task.status}{lane} ({tokens} tokens, {minutes:.1f} min)"

    def _failure_text(self, task: TaskSpec, out_text: str = "") -> str:
        """Everything this exit said, for the limit classifier to read.

        The result JSON is the usual carrier ("Overloaded", "You've hit your
        usage limit"), but a 529 that killed the CLI before it printed any
        JSON only ever shows up on stderr, so both are read.
        """
        parts = [str(task.error or ""), out_text[-ERR_TAIL_BYTES:]]
        try:
            err = (self.cfg.logs_dir / f"{task.id}.err.txt").read_text(
                encoding="utf-8", errors="replace")
            parts.append(err[-ERR_TAIL_BYTES:])
        except OSError:
            pass
        return "\n".join(parts)

    def _fallback_relaunch(self, task: TaskSpec, now: datetime) -> str | None:
        """Requeue `task` onto its tier's fallback after a limit exit, or say why not.

        Two very different exits arrive here and confusing them is what broke
        the mark's whole purpose:

          * the tier's PRIMARY died. Mark it limited and requeue this row with
            the fallback forced for its next launch.
          * the row's FALLBACK died. The primary's record must survive
            untouched - it is the only thing holding the tier off the primary,
            and overwriting it with the fallback's id sent the very next launch
            straight back into the model the account refused minutes ago. No
            second hop either: walking the ladder is how a tier ends up on a
            model nobody chose.

        Requeue, not launch: the row goes back to `pending` and the same
        `apply()` tick's `_launch_batch` starts it, under the decision's
        `target_concurrency` and `allow_heavy` and behind `launch_gate`. A
        direct `self.launch` here bypassed operator STOP, the weekly-goal stop,
        the blocked mode (which is exactly the state a "usage limit" exit
        arrives in) and, for the fork, the re-arm cooldown.

        Called from reap once the finished process is out of `_procs`.
        """
        reason = self._pending_fallback.pop(task.id, None)
        if reason is None:
            return None
        tier = task_tier(task)
        primary = self._primary_model(task, task.lane or "cloud") or ""
        # What this exit actually ran on. `model_used` is stamped by launch, so
        # it is the truth even when the graph moved underneath the row.
        failed_on = task.model_used or primary
        fallback = read_graph(self.cfg)[tier].get("fallback")

        if primary and failed_on and failed_on != primary:
            # Not the primary: leave the primary's mark exactly as it stands
            # (expiring on its own schedule) and leave this row failed.
            label = "fallback" if failed_on == fallback else "model"
            held = (f"; {primary} keeps its mark"
                    if limited_model(self.cfg, now) == primary else "")
            return (f"task {task.id}: {tier} {label} {failed_on} also limited "
                    f"({reason}){held}; leaving it failed")
        if primary:
            write_limited(self.cfg, primary, reason, now)
        if task.fallback_from:
            # Already had the one relaunch this row gets.
            return (f"task {task.id}: fallback {failed_on} also limited "
                    f"({reason}); leaving it failed")
        if not fallback:
            return (f"task {task.id}: {primary} limited ({reason}); "
                    f"no {tier} fallback configured")
        if fallback == failed_on or fallback == primary:
            # `may_fall_back` allows equal ranks (two ids of the same
            # capability), so it does not catch a tier whose fallback IS its
            # primary; relaunching there is a relaunch onto the limited model.
            return (f"task {task.id}: {primary} limited ({reason}); the {tier} "
                    f"fallback is the same model, leaving it failed")
        if not may_fall_back(primary, fallback, self.cfg):
            # Never upward: a worker whose model is busy waits, it does not get
            # promoted onto the executive's.
            return (f"task {task.id}: {primary} limited ({reason}); refusing "
                    f"to fall back UP to {tier} fallback {fallback}")
        task.fallback_from = primary
        task.fallback_model = fallback
        task.status = "pending"
        task.error = None
        task.pid = None
        self.save()
        return (f"task {task.id}: {primary} limited ({reason}); requeued on "
                f"{tier} fallback {fallback}")

    def _note_fork_finish(self, task: TaskSpec, now: datetime,
                          tokens: int | None = None,
                          cost_usd: float | None = None,
                          fork_session_id: str | None = None) -> None:
        """Close out the handover record when the director fork ends.

        Every exit goes through here - finalize, adoption, both kill paths and
        the restart orphan-marking - so the monitor session never sees a fork
        stuck at "started" forever.
        """
        if task.id != FORK_TASK_ID:
            return
        sid = fork_session_id if isinstance(fork_session_id, str) else None
        finish_handover(
            self.cfg, status=task.status,
            finished_at=task.finished_at or now.isoformat(),
            tokens=tokens, cost_usd=cost_usd, fork_session_id=sid,
        )

    def _kill_tree(self, pid: int) -> None:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, check=False,
        )

    def reap(self, now: datetime) -> list[str]:
        actions: list[str] = []
        for task_id, proc in list(self._procs.items()):
            task = self.get(task_id)
            if task is None:
                continue
            if proc.popen.poll() is not None:
                actions.append(self._finalize(task, proc, now))
                # Out of _procs *before* the fallback relaunch: the relaunch
                # puts a new entry under the same id, and deleting after it
                # would drop the live process this loop just started.
                del self._procs[task_id]
                note = self._fallback_relaunch(task, now)
                if note:
                    actions.append(note)
                continue
            started = parse_iso(task.started_at)
            limit_minutes = task.max_minutes * (
                self.cfg.local_minutes_multiplier if task.lane == "local" else 1.0
            )
            if started and (now - started).total_seconds() > limit_minutes * 60:
                self._kill_tree(proc.popen.pid)
                proc.popen.wait(timeout=15)
                proc.out.close()
                proc.err.close()
                task.status = "killed"
                task.error = f"exceeded max_minutes={limit_minutes:g}"
                task.finished_at = now.isoformat()
                del self._procs[task_id]
                self._note_fork_finish(task, now)
                actions.append(f"task {task.id}: killed after {limit_minutes:g} min limit")
        for task_id, pid in list(self._adopted.items()):
            task = self.get(task_id)
            if task is None or task.status != "running":
                del self._adopted[task_id]
                continue
            if not pid_alive(pid):
                del self._adopted[task_id]
                actions.append(
                    self._finalize_record(task, None, now) + " [adopted]")
                note = self._fallback_relaunch(task, now)
                if note:
                    actions.append(note)
                continue
            started = parse_iso(task.started_at)
            limit_minutes = task.max_minutes * (
                self.cfg.local_minutes_multiplier if task.lane == "local" else 1.0
            )
            if started and (now - started).total_seconds() > limit_minutes * 60:
                self._kill_tree(pid)
                del self._adopted[task_id]
                task.status = "killed"
                task.error = f"exceeded max_minutes={limit_minutes:g}"
                task.finished_at = now.isoformat()
                self._note_fork_finish(task, now)
                actions.append(
                    f"task {task.id}: adopted session killed after {limit_minutes:g} min limit")
        if actions:
            self.save()
        return actions

    def _argv(self, task: TaskSpec, lane: str, exe: str) -> list[str]:
        """The command line for one task, in one place so it can be asserted on.

        The director fork's line must carry `--model <throttle_model>` and
        `--resume <parent session> --fork-session`; an earlier audit found a
        launch that went out with no --model at all.
        """
        cmd = [exe, "-p", "--output-format", "json"]
        model = self._task_model(task, lane)
        if model:
            cmd += ["--model", model]
        if task.resume_session:
            cmd += ["--resume", task.resume_session, "--fork-session"]
        if self.cfg.permission_mode == "bypass":
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd += ["--permission-mode", self.cfg.permission_mode]
        cmd += self.cfg.extra_claude_args
        return cmd

    def launch(self, task: TaskSpec, now: datetime, lane: str = "cloud") -> str:
        exe = shutil.which(self.cfg.claude_cmd)
        if exe is None:
            raise DispatchError(f"claude executable not found: {self.cfg.claude_cmd}")
        env = local_env(self.cfg) if lane == "local" else None
        cmd = self._argv(task, lane, exe)

        prompt = self._task_prompt(task, lane)

        cwd = task.cwd
        os.makedirs(cwd, exist_ok=True)
        out = open(self.cfg.logs_dir / f"{task.id}.out.json", "w", encoding="utf-8")
        err = open(self.cfg.logs_dir / f"{task.id}.err.txt", "w", encoding="utf-8")
        flags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        popen = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err,
            creationflags=flags, text=True, encoding="utf-8", env=env,
        )
        assert popen.stdin is not None
        popen.stdin.write(prompt)
        popen.stdin.close()

        task.status = "running"
        task.lane = lane
        task.started_at = now.isoformat()
        task.finished_at = None
        task.pid = popen.pid
        task.error = None
        # What this run actually went out on: `task.model` is the intent, and
        # the two differ whenever the tier is on its fallback.
        task.model_used = self._task_model(task, lane)
        self._procs[task.id] = _Proc(popen, out, err)
        self.save()
        if task.id == FORK_TASK_ID:
            # The handover signal: the parent session stops working here and
            # watches this file. Written after the process actually started, so
            # a failed launch never claims a handover happened.
            mode = self.current_mode if self.current_mode in FORK_MODES else FORK_MODES[0]
            write_handover(
                self.cfg, task_id=task.id, mode=mode,
                model=self._task_model(task, lane),
                parent_session=task.resume_session,
                started_at=task.started_at,
            )
        suffix = f" -> {self.cfg.local_model}" if lane == "local" else ""
        return f"task {task.id}: launched {lane}{suffix} ({task.weight}, pid {popen.pid})"

    def _primary_model(self, task: TaskSpec, lane: str) -> str | None:
        """The model this row *wants*, before any limited-mark detour.

        Its own `model` first, then its tier's model straight out of the graph.
        The graph rather than only `cfg.throttle_model`/`cfg.worker_model`
        because those scalars are re-synced by `apply_graph` (once a poll in
        the loop, never in a bare CLI Dispatcher), and a stale one here would
        make `_fallback_relaunch` mark the wrong model limited.
        """
        if lane == "local":
            return self.cfg.local_model
        if task.model:
            return task.model
        tier_model = str(read_graph(self.cfg)[task_tier(task)].get("model") or "")
        if task.id == FORK_TASK_ID:
            # Never None for the director fork: a model-less launch silently
            # drops it onto the account default (a Fable model), which is
            # exactly what this fork exists not to be.
            return (tier_model or self.cfg.throttle_model
                    or self.cfg.worker_model or FORK_FALLBACK_MODEL)
        return tier_model or self.cfg.worker_model or None

    def _task_model(self, task: TaskSpec, lane: str) -> str | None:
        if lane == "local":
            return self.cfg.local_model
        primary = self._primary_model(task, lane)
        forced = task.fallback_model
        if (forced and forced != primary
                and may_fall_back(primary, forced, self.cfg)):
            # The one forced launch a limited primary buys this row. Re-checked
            # against the ranking every time, so a graph edited between the
            # requeue and the launch can never turn it into a promotion.
            return forced
        return self._while_limited(task, primary)

    def _while_limited(self, task: TaskSpec, primary: str | None) -> str | None:
        """`primary`, or the tier's fallback while state/limited.json names it.

        This is what stops the loop from spending the next poll - and the one
        after that - relaunching onto a model the account has already been told
        it cannot have. Once the record ages past `fallback_minutes`
        `read_limited` returns None again and the primary gets another try.
        """
        if not primary:
            return primary
        record = read_limited(self.cfg)
        if record is None or record.get("model") != primary:
            return primary
        fallback = read_graph(self.cfg)[task_tier(task)].get("fallback")
        if not may_fall_back(primary, fallback, self.cfg):
            return primary
        return fallback

    def _task_prompt(self, task: TaskSpec, lane: str) -> str:
        prompt = task.prompt
        if "{graph}" in prompt:
            # Expanded at launch, not when the row was queued: the agentic
            # graph the fork is told about is the one in force right now,
            # including a worker count the operator just changed.
            from .graph import graph_line, read_graph
            prompt = prompt.replace("{graph}", graph_line(read_graph(self.cfg)))
        if lane == "local" and self.cfg.local_prompt_preamble:
            return f"{self.cfg.local_prompt_preamble}\n\n{prompt}"
        return prompt

    def gpu_guard_proc(self) -> str | None:
        """Name of a running GPU-exclusive process, or None.

        The FreeToken engine pins ~31.5GB of the 32GB card, so it must never
        be started while something like the UE editor needs the GPU.
        """
        names = [n.lower() for n in self.cfg.local_gpu_guard_procs]
        if not names:
            return None
        try:
            listing = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15, check=False,
            ).stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            return None
        for name in names:
            if f'"{name}"' in listing:
                return name
        return None

    def local_engine_up(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"{self.cfg.local_base_url.rstrip('/')}/v1/models",
                timeout=LOCAL_HEALTH_TIMEOUT,
            ) as resp:
                healthy = 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            healthy = False
        self.local_engine_healthy = healthy
        return healthy

    def _start_local_engine(self, now: datetime) -> str:
        # Ask the ft daemon (persistent supervisor) to bring the serve up; it
        # returns immediately and loads the model in the background, so this is
        # retried across ticks until the health probe passes.
        import time

        if not self.cfg.local_autostart:
            return "local engine down (autostart disabled)"
        guard = self.gpu_guard_proc()
        if guard:
            return f"local engine start deferred ({guard} owns the GPU)"
        mono = time.monotonic()
        if (self._local_start_ts is not None
                and mono - self._local_start_ts < self.cfg.local_start_retry_seconds):
            return "local engine still starting"
        ft = self.cfg.local_ft_bin
        if ft is None or not ft.exists():
            return f"local engine down and ft binary missing: {ft}"
        model = self.cfg.local_model_path or self.cfg.local_model
        try:
            port = self.cfg.local_base_url.rsplit(":", 1)[1].strip("/")
            subprocess.Popen(
                [str(ft), "daemon", "start", model, "--port", port,
                 "--url", self.cfg.local_daemon_url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._local_start_ts = mono
            return f"local engine starting ({model})"
        except OSError as exc:
            return f"local engine start failed ({exc})"

    def _launch_batch(
        self, candidates: list[TaskSpec], running: int, target: int,
        now: datetime, actions: list[str], lane: str,
    ) -> None:
        for task in candidates:
            if running >= target:
                break
            if self.launch_gate is not None and self.launch_gate(task):
                # Held back, not failed: the row stays pending and the gate is
                # asked again next poll. Silent on purpose - a fork waiting out
                # its cooldown would otherwise write a line every poll.
                continue
            try:
                actions.append(self.launch(task, now, lane=lane))
                running += 1
            except (DispatchError, OSError) as exc:
                task.status = "failed"
                task.error = str(exc)[:200]
                self.save()
                actions.append(f"task {task.id}: launch failed ({exc})")
                # A launch that never started can still have failed *because*
                # the model was unavailable; classify it the same way.
                reason = limited_reason(exc)
                if reason:
                    self._pending_fallback[task.id] = reason
                    # Requeues rather than launching, so the retry happens on
                    # the next poll under a fresh decision - this batch is
                    # already past this row.
                    note = self._fallback_relaunch(task, now)
                    if note:
                        actions.append(note)

    def apply(self, decision: Decision, now: datetime) -> list[str]:
        self.current_mode = decision.mode
        if self.supervise:
            self.sync_from_disk()
        actions = self.reap(now)
        running_cloud = sum(
            1 for t in self._tasks
            if t.status == "running" and t.lane != "local"
        )
        running_local = sum(
            1 for t in self._tasks
            if t.status == "running" and t.lane == "local"
        )
        pending = [t for t in self._tasks if t.status == "pending"]

        if running_cloud < decision.target_concurrency:
            cloud_candidates = sorted(
                (t for t in pending
                 if decision.allow_heavy or t.weight != "heavy"),
                key=lambda t: (-t.priority, t.id),
            )
            self._launch_batch(cloud_candidates, running_cloud,
                               decision.target_concurrency, now, actions, "cloud")
            pending = [t for t in self._tasks if t.status == "pending"]

        local_target = getattr(decision, "local_concurrency", 0)
        if local_target > 0 and running_local < local_target:
            # Forked main-session tasks (full throttle) carry cloud-sized
            # context and exist to spend cloud budget; they never run locally.
            local_candidates = sorted(
                (t for t in pending if not t.resume_session),
                key=lambda t: (-t.priority, t.id),
            )
            if local_candidates:
                if self.local_engine_up():
                    self._launch_batch(local_candidates, running_local,
                                       local_target, now, actions, "local")
                else:
                    actions.append(self._start_local_engine(now))
        return actions
