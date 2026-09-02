from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from typing import IO

from .config import Config
from .models import Decision, QueueStats, TaskSpec, parse_iso, utcnow

LOCAL_HEALTH_TIMEOUT = 4.0
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
        self.load()
        if not supervise:
            return
        changed = False
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
        cost = result.get("total_cost_usd")
        task.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
        task.finished_at = now.isoformat()

        ok = (exit_code == 0 or (exit_code is None and bool(result)))
        if ok and not result.get("is_error"):
            task.status = "done"
        else:
            task.status = "failed"
            task.error = str(result.get("result", ""))[:200] or f"exit code {exit_code}"

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
                del self._procs[task_id]
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
                actions.append(
                    f"task {task.id}: adopted session killed after {limit_minutes:g} min limit")
        if actions:
            self.save()
        return actions

    def launch(self, task: TaskSpec, now: datetime, lane: str = "cloud") -> str:
        exe = shutil.which(self.cfg.claude_cmd)
        if exe is None:
            raise DispatchError(f"claude executable not found: {self.cfg.claude_cmd}")
        cmd = [exe, "-p", "--output-format", "json"]
        env = None
        if lane == "local":
            env = local_env(self.cfg)
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
        task.pid = popen.pid
        task.error = None
        self._procs[task.id] = _Proc(popen, out, err)
        self.save()
        suffix = f" -> {self.cfg.local_model}" if lane == "local" else ""
        return f"task {task.id}: launched {lane}{suffix} ({task.weight}, pid {popen.pid})"

    def _task_model(self, task: TaskSpec, lane: str) -> str | None:
        if lane == "local":
            return self.cfg.local_model
        return task.model or self.cfg.worker_model or None

    def _task_prompt(self, task: TaskSpec, lane: str) -> str:
        if lane == "local" and self.cfg.local_prompt_preamble:
            return f"{self.cfg.local_prompt_preamble}\n\n{task.prompt}"
        return task.prompt

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
            try:
                actions.append(self.launch(task, now, lane=lane))
                running += 1
            except (DispatchError, OSError) as exc:
                task.status = "failed"
                task.error = str(exc)[:200]
                self.save()
                actions.append(f"task {task.id}: launch failed ({exc})")

    def apply(self, decision: Decision, now: datetime) -> list[str]:
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
