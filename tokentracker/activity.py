from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .models import ActivityState

KEEP_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
)
STALE_DAYS = 7.0
JSONL_SUFFIX = ".jsonl"


def project_dir_name(cwd: str) -> str:
    path = Path(cwd)
    if not path.is_absolute():
        path = Path(os.path.abspath(str(path)))
    return "".join(ch if ch in KEEP_CHARS else "-" for ch in str(path))


def _newest_direct_jsonl(
    root: str, min_mtime: float, excluded_stems: set[str]
) -> float | None:
    newest: float | None = None
    try:
        entries = list(os.scandir(root))
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_file() or not entry.name.endswith(JSONL_SUFFIX):
                continue
            if entry.name[: -len(JSONL_SUFFIX)] in excluded_stems:
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= min_mtime and (newest is None or mtime > newest):
            newest = mtime
    return newest


def _newest_jsonl_mtime(
    root: str, min_mtime: float, excluded_stems: set[str]
) -> float | None:
    newest = _newest_direct_jsonl(root, min_mtime, excluded_stems)
    try:
        entries = list(os.scandir(root))
    except OSError:
        return newest
    for entry in entries:
        try:
            if not entry.is_dir() or entry.stat().st_mtime < min_mtime:
                continue
        except OSError:
            continue
        sub = _newest_direct_jsonl(entry.path, min_mtime, excluded_stems)
        if sub is not None and (newest is None or sub > newest):
            newest = sub
    return newest


def detect_activity(cfg: Config, own_dirs: set[str], now: datetime) -> ActivityState:
    idle_cutoff = now - timedelta(minutes=cfg.activity_idle_minutes)
    min_mtime = (now - timedelta(days=STALE_DAYS)).timestamp()
    main_ids = set(cfg.main_session_ids)
    newest_overall: float | None = None
    active: list[str] = []
    try:
        project_entries = list(os.scandir(cfg.projects_dir))
    except OSError:
        return ActivityState(user_active=False, last_user_activity=None)
    for entry in project_entries:
        try:
            if not entry.is_dir() or entry.name in own_dirs:
                continue
        except OSError:
            continue
        newest = _newest_jsonl_mtime(entry.path, min_mtime, main_ids)
        if newest is None:
            continue
        if newest_overall is None or newest > newest_overall:
            newest_overall = newest
        if datetime.fromtimestamp(newest, tz=timezone.utc) >= idle_cutoff:
            active.append(entry.name)
    active.sort()
    last = (
        datetime.fromtimestamp(newest_overall, tz=timezone.utc)
        if newest_overall is not None
        else None
    )
    return ActivityState(
        user_active=bool(active),
        last_user_activity=last,
        active_foreign_sessions=active,
    )
