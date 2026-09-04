from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

TASK_STATUSES = ("pending", "running", "done", "failed", "killed")
# "local-only" is a working mode, not a parked one: the cloud lane is at zero
# because a budget bucket is spent, and the local engine has the shift.
MODES = ("pace", "coast", "yield", "surge", "blocked", "stopped", "local-only")
WEEK_HOURS = 168.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class WindowUsage:
    utilization: float
    resets_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "utilization": self.utilization,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WindowUsage":
        return cls(float(d.get("utilization", 0.0)), parse_iso(d.get("resets_at")))


@dataclass
class UsageSnapshot:
    fetched_at: datetime
    five_hour: WindowUsage
    seven_day: WindowUsage
    extra: dict[str, WindowUsage] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.isoformat(),
            "five_hour": self.five_hour.to_dict(),
            "seven_day": self.seven_day.to_dict(),
            "extra": {k: v.to_dict() for k, v in self.extra.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UsageSnapshot":
        return cls(
            fetched_at=parse_iso(d.get("fetched_at")) or utcnow(),
            five_hour=WindowUsage.from_dict(d.get("five_hour", {})),
            seven_day=WindowUsage.from_dict(d.get("seven_day", {})),
            extra={k: WindowUsage.from_dict(v) for k, v in d.get("extra", {}).items()},
        )


@dataclass
class BurnRates:
    total_pct_per_hr: float
    own_pct_per_hr: float
    foreign_pct_per_hr: float
    foreign_ema_pct_per_hr: float


@dataclass
class ActivityState:
    user_active: bool
    last_user_activity: datetime | None
    active_foreign_sessions: list[str] = field(default_factory=list)

    def recent_within(self, minutes: float, now: datetime) -> bool:
        if self.user_active:
            return True
        if self.last_user_activity is None:
            return False
        return (now - self.last_user_activity).total_seconds() <= minutes * 60


@dataclass
class TaskSpec:
    id: str
    prompt: str
    cwd: str
    weight: str = "light"
    model: str | None = None
    resume_session: str | None = None
    priority: int = 0
    max_minutes: int = 90
    status: str = "pending"
    lane: str | None = None
    # The lane this row may run on at all: "local" for a backlog brief (its
    # prompt is written for the local engine's rules and its containment
    # header), "cloud" for a row that must not be handed to a 27B model, None
    # for anything either lane can take. Set when the row is built; `lane` is
    # what it actually launched on.
    lane_pref: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    # Minted by `claude -p --output-format json` and printed in its result at
    # exit; the ledger needs it to find the fork's own transcript.
    fork_session_id: str | None = None
    # The model this run actually went out on, which is not always `model`: a
    # tier whose primary is limited launches on its fallback instead.
    model_used: str | None = None
    # Set on the one relaunch a limited primary is allowed: the model that was
    # limited. Its presence is what stops a second fallback hop.
    fallback_from: str | None = None
    # The model forced for the next launch of this row, after its tier's
    # primary died on a limit. Kept apart from `model` on purpose: `model` is
    # the row's standing intent, and overwriting it made the fallback
    # permanent - a requeue would still have gone out on the fallback, and the
    # bookkeeping could no longer tell which model the tier actually wanted.
    # Cleared by `set_status(pending)`, so a requeue tries the primary again.
    fallback_model: str | None = None
    session_tokens: int | None = None
    cost_usd: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in ("model",)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class QueueStats:
    pending_heavy: int
    pending_light: int
    running: int
    running_local: int = 0


@dataclass
class Decision:
    mode: str
    target_concurrency: int
    allow_heavy: bool
    reason: str
    local_concurrency: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
