"""Operator dispatch switch (the overlay's START / STOP buttons).

The state lives in a single small file so the overlay process and the run loop
stay decoupled: the overlay writes it on click, the loop reads it once per poll.
Stopping is a launch gate only - workers already running are left alone and are
still reaped/adopted normally.
"""

from __future__ import annotations

import json
import os

from .config import Config
from .models import Decision, utcnow

RUNNING = "running"
STOPPED = "stopped"
CONTROL_MODES = (RUNNING, STOPPED)


def read_control(cfg: Config) -> str:
    """Current dispatch mode; "running" when the file is missing or malformed.

    Defaulting to running matters: a corrupt or half-written file must never
    silently park the whole loop.
    """
    try:
        data = json.loads(cfg.control_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return RUNNING
    if not isinstance(data, dict):
        return RUNNING
    return STOPPED if data.get("dispatch") == STOPPED else RUNNING


def write_control(cfg: Config, mode: str) -> str:
    """Persist the dispatch mode; returns the normalized mode written."""
    mode = STOPPED if mode == STOPPED else RUNNING
    body = json.dumps({"dispatch": mode, "changed_at": utcnow().isoformat()})
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


def gate_decision(decision: Decision, mode: str) -> Decision:
    """Decision the dispatcher may act on, given the operator switch.

    Stopped drops every launch budget (cloud and local) to zero, which is what
    keeps `Dispatcher.apply` from starting anything new; apply still reaps and
    finalizes adopted work first, so nothing running is disturbed.
    """
    if mode != STOPPED:
        return decision
    return Decision(
        STOPPED, 0, False,
        f"Dispatch stopped by operator (would be {decision.mode}); "
        "no new tasks will launch.",
        local_concurrency=0,
    )
