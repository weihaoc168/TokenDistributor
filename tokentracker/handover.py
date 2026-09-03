"""Handover record for the forked director session (state/handover.json).

The parent (Fable) session hands the acting-director job to a forked headless
copy that the loop launches as the `throttle-main-continue` task, and then goes
monitor-only: it watches this one small file instead of the work itself. So the
key set is a contract, exactly like goal.STOP_KEYS - the parent parses it.

Written once at launch (status "started") and updated in place when the fork
finishes (status "done" / "failed"). Nothing here ever raises: it is called
from inside the run loop and from every overlay refresh.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .config import Config

# The continue-fork task id lives here, not in cli.py, so the dispatcher can
# recognize the fork without importing the CLI (cli imports dispatch, never the
# other way round).
FORK_TASK_ID = "throttle-main-continue"
# The fork *is* the technical director: it must never launch model-less (an
# audited launch went out with model=None) and never on a Fable model. This is
# the last-resort floor under cfg.throttle_model.
FORK_FALLBACK_MODEL = "claude-opus-5"
# The only modes that may hand the job over: pace is normal dispatch, surge is
# full throttle. blocked / yield / coast / stopped never fork.
FORK_MODES = ("pace", "surge")
STATUS_STARTED = "started"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
# Exactly these keys at launch, plus the three finish keys afterwards.
START_KEYS = ("task_id", "mode", "model", "parent_session", "started_at",
              "status", "fork_session_id")
FINISH_KEYS = ("finished_at", "tokens", "cost_usd")
HANDOVER_KEYS = START_KEYS + FINISH_KEYS
# state/handover.log: one JSON object per line, appended on every start and
# every finish. handover.json only ever holds the newest fork; the ledger needs
# every one of them, because a report window can span several forks and each
# may have run on a different model (a fallback, or a graph edited between
# launches). `logged_at` is the only key the log adds to the record.
LOG_KEY = "logged_at"
LOG_KEYS = HANDOVER_KEYS + (LOG_KEY,)
# A run of years of forks is still a small file, but it is append-only, so the
# reader caps what it will hold in memory.
LOG_MAX_LINES = 5000


def _write_atomic(path, body: str) -> None:
    # Same swap-in-whole dance as control.write_control / goal._write_atomic: a
    # torn read must never look like "no handover" to the monitor session.
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


def read_handover(cfg: Config) -> dict | None:
    """The handover record, or None when there is none (or it is unreadable)."""
    try:
        data = json.loads(cfg.handover_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_handover(cfg: Config, *, task_id: str, mode: str, model: str,
                   parent_session: str | None, started_at: str,
                   fork_session_id: str | None = None) -> dict:
    """Record the handover at launch. Returns the record written.

    `fork_session_id` is normally None: `--resume <parent> --fork-session` lets
    claude mint the new id, so it is only knowable from the launch output, and
    finish_handover backfills it from there.
    """
    record: dict[str, Any] = {
        "task_id": task_id,
        "mode": mode if mode in FORK_MODES else FORK_MODES[0],
        "model": model,
        "parent_session": parent_session,
        "started_at": started_at,
        "status": STATUS_STARTED,
        "fork_session_id": fork_session_id,
    }
    _write_atomic(cfg.handover_file, json.dumps(record, indent=2))
    # Every start and every finish is also appended to the log the ledger reads
    # for its role stamps; handover.json itself is overwritten by the next fork.
    append_log(cfg, record)
    return record


def finish_handover(cfg: Config, *, status: str, finished_at: str,
                    tokens: int | None = None, cost_usd: float | None = None,
                    fork_session_id: str | None = None) -> dict:
    """Update the record in place when the fork ends; returns it.

    Anything but "done" is reported as "failed" (a killed fork is a failed
    handover as far as the monitor is concerned). A missing or partial record
    is filled out with nulls rather than dropped, so the key set holds.
    """
    existing = read_handover(cfg) or {}
    record: dict[str, Any] = {key: existing.get(key) for key in START_KEYS}
    record["status"] = STATUS_DONE if status == STATUS_DONE else STATUS_FAILED
    record["finished_at"] = finished_at
    record["tokens"] = tokens
    record["cost_usd"] = cost_usd
    if fork_session_id and not record.get("fork_session_id"):
        record["fork_session_id"] = fork_session_id
    _write_atomic(cfg.handover_file, json.dumps(record, indent=2))
    append_log(cfg, record)
    return record


def append_log(cfg: Config, record: dict) -> dict:
    """Append one handover record to state/handover.log. Never raises.

    Append-only and one JSON object per line, so a torn write costs the reader
    one line rather than the file, and two processes writing at once interleave
    whole lines rather than corrupting each other's.
    """
    from .models import utcnow

    entry = {key: record.get(key) for key in HANDOVER_KEYS}
    entry[LOG_KEY] = utcnow().isoformat()
    try:
        cfg.handover_log.parent.mkdir(parents=True, exist_ok=True)
        with cfg.handover_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except (OSError, TypeError, ValueError):
        pass
    return entry


def read_log(cfg: Config, limit: int = LOG_MAX_LINES) -> list[dict]:
    """Every handover record ever written, oldest first. [] when there is none.

    A junk line is skipped rather than raising: this is read by the report,
    which must never fail because one append was torn.
    """
    try:
        lines = cfg.handover_log.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError, AttributeError):
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def fork_models(cfg: Config) -> dict[str, str]:
    """{fork session id: the model that fork actually ran on}.

    Built from the log rather than from the graph, which is the whole point:
    the graph says what the executive tier is configured to be *now*, while a
    fork that ran two hours ago may have gone out on a fallback, or on the
    model the operator has since replaced. The later record for a session id
    wins, because the finish record is the one that knows the id.
    """
    models: dict[str, str] = {}
    for entry in read_log(cfg):
        sid = entry.get("fork_session_id")
        model = str(entry.get("model") or "").strip()
        if isinstance(sid, str) and sid and model:
            models[sid] = model
    return models


def fork_active(cfg: Config) -> bool:
    record = read_handover(cfg)
    return bool(isinstance(record, dict)
                and record.get("status") == STATUS_STARTED)


def fork_status_line(cfg: Config) -> str | None:
    """The one line `status` prints for the monitor session, or None."""
    record = read_handover(cfg)
    if not isinstance(record, dict):
        return None
    status = record.get("status")
    if not isinstance(status, str) or not status:
        return None
    since = record.get("started_at") or "?"
    mode = record.get("mode") or "?"
    model = record.get("model") or "?"
    return f"fork: {status} since {since} ({mode}, {model})"
