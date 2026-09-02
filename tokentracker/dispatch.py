from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from typing import IO

from .config import Config
from .models import Decision, QueueStats, TaskSpec, parse_iso, utcnow


class DispatchError(Exception):
    pass


class _Proc:
    def __init__(self, popen: subprocess.Popen, out: IO, err: IO) -> None:
        self.popen = popen
        self.out = out
        self.err = err


class Dispatcher:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._tasks: list[TaskSpec] = []
        self._procs: dict[str, _Proc] = {}
        self.load()
        changed = False
        for task in self._tasks:
            if task.status == "running" and task.id not in self._procs:
                task.status = "failed"
                task.error = "orphaned by tracker restart"
                changed = True
        if changed:
            self.save()

    def load(self) -> None:
        if not self.cfg.tasks_file.exists():
            self._tasks = []
            return
        data = json.loads(self.cfg.tasks_file.read_text(encoding="utf-8"))
        self._tasks = [TaskSpec.from_dict(d) for d in data.get("tasks", [])]

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
        exit_code = proc.popen.returncode
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

        if exit_code == 0 and not result.get("is_error"):
            task.status = "done"
        else:
            task.status = "failed"
            task.error = str(result.get("result", ""))[:200] or f"exit code {exit_code}"

        started = parse_iso(task.started_at) or now
        minutes = max((now - started).total_seconds() / 60, 0.1)
        try:
            from .usage import record_task_outcome
            record_task_outcome(self.cfg, task.weight, tokens, minutes)
        except ImportError:
            pass
        return f"task {task.id}: {task.status} ({tokens} tokens, {minutes:.1f} min)"

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
            if started and (now - started).total_seconds() > task.max_minutes * 60:
                self._kill_tree(proc.popen.pid)
                proc.popen.wait(timeout=15)
                proc.out.close()
                proc.err.close()
                task.status = "killed"
                task.error = f"exceeded max_minutes={task.max_minutes}"
                task.finished_at = now.isoformat()
                del self._procs[task_id]
                actions.append(f"task {task.id}: killed after {task.max_minutes} min limit")
        if actions:
            self.save()
        return actions

    def launch(self, task: TaskSpec, now: datetime) -> str:
        exe = shutil.which(self.cfg.claude_cmd)
        if exe is None:
            raise DispatchError(f"claude executable not found: {self.cfg.claude_cmd}")
        cmd = [exe, "-p", "--output-format", "json"]
        if task.model:
            cmd += ["--model", task.model]
        if task.resume_session:
            cmd += ["--resume", task.resume_session, "--fork-session"]
        if self.cfg.permission_mode == "bypass":
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd += ["--permission-mode", self.cfg.permission_mode]
        cmd += self.cfg.extra_claude_args

        cwd = task.cwd
        os.makedirs(cwd, exist_ok=True)
        out = open(self.cfg.logs_dir / f"{task.id}.out.json", "w", encoding="utf-8")
        err = open(self.cfg.logs_dir / f"{task.id}.err.txt", "w", encoding="utf-8")
        flags = 0
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        popen = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=out, stderr=err,
            creationflags=flags, text=True, encoding="utf-8",
        )
        assert popen.stdin is not None
        popen.stdin.write(task.prompt)
        popen.stdin.close()

        task.status = "running"
        task.started_at = now.isoformat()
        task.pid = popen.pid
        task.error = None
        self._procs[task.id] = _Proc(popen, out, err)
        self.save()
        return f"task {task.id}: launched ({task.weight}, pid {popen.pid})"

    def apply(self, decision: Decision, now: datetime) -> list[str]:
        actions = self.reap(now)
        running = sum(1 for t in self._tasks if t.status == "running")
        if running >= decision.target_concurrency:
            return actions
        candidates = sorted(
            (t for t in self._tasks if t.status == "pending"
             and (decision.allow_heavy or t.weight != "heavy")),
            key=lambda t: (-t.priority, t.id),
        )
        for task in candidates:
            if running >= decision.target_concurrency:
                break
            try:
                actions.append(self.launch(task, now))
                running += 1
            except (DispatchError, OSError) as exc:
                task.status = "failed"
                task.error = str(exc)[:200]
                self.save()
                actions.append(f"task {task.id}: launch failed ({exc})")
        return actions
