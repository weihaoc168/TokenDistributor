from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"


@dataclass
class Config:
    root: Path
    credentials_path: Path
    projects_dir: Path
    sessions_dir: Path
    state_dir: Path
    logs_dir: Path
    tasks_file: Path
    usage_url: str = DEFAULT_USAGE_URL
    poll_seconds: int = 300
    stale_snapshot_minutes: int = 30
    fetch_backoff_base_seconds: int = 60
    fetch_backoff_max_seconds: int = 1800
    slope_window_minutes: int = 45
    foreign_ema_alpha: float = 0.15
    activity_idle_minutes: int = 15
    activity_cooldown_minutes: int = 60
    reserve_week_frac: float = 0.15
    ahead_margin: float = 0.03
    weekly_goal: float = 0.90
    endgame_hours: float = 12.0
    five_hour_guard_active: float = 0.80
    five_hour_guard_idle: float = 0.95
    max_concurrency: int = 3
    surge_concurrency: int = 4
    yield_concurrency: int = 0
    heavy_pct_per_hr_prior: float = 1.5
    light_pct_per_hr_prior: float = 0.3
    claude_cmd: str = "claude"
    permission_mode: str = "acceptEdits"
    worker_model: str = ""
    throttle_model: str = ""
    throttle_fork_enabled: bool = True
    # The continue fork is also ensured in normal pace mode, not only under
    # full throttle, and re-armed only after this cooldown so a fork that dies
    # on launch cannot be relaunched every poll.
    fork_in_pace: bool = True
    fork_cooldown_seconds: int = 120
    throttle_prompt: str = ""
    extra_claude_args: list[str] = field(default_factory=list)
    main_session_ids: list[str] = field(default_factory=list)
    # The agentic graph (executive / advisory / workers). When present it is
    # authoritative and the four legacy keys above are derived from it by
    # graph.apply_graph; see tokentracker/graph.py.
    graph: dict[str, Any] = field(default_factory=dict)
    known_models: list[str] = field(default_factory=list)
    # Work-distribution report (tokentracker/ledger.py).
    report_repo: str = "C:/Users/chenw/StarGTA"
    report_on_milestone: bool = True
    report_on_stop: bool = True
    report_window_hours: float = 24.0
    local_enabled: bool = False
    local_base_url: str = "http://127.0.0.1:1919"
    local_daemon_url: str = "http://127.0.0.1:1900"
    local_auth_token: str = "freetoken"
    local_model: str = ""
    local_model_path: str = ""
    local_ft_bin: Path | None = None
    local_max_concurrency: int = 1
    local_when_active: bool = False
    local_autostart: bool = True
    local_start_retry_seconds: int = 120
    local_minutes_multiplier: float = 3.0
    local_max_context_tokens: int = 262144
    local_max_output_tokens: int = 32768
    local_api_timeout_ms: int = 1200000
    local_prompt_preamble: str = ""
    local_gpu_guard_procs: list[str] = field(default_factory=lambda: ["UnrealEditor.exe"])
    sundial_shell_path: Path | None = None
    overlay_offset_x: int = 0
    overlay_offset_y: int = 430
    overlay_width: int = 300
    overlay_refresh_seconds: int = 5

    @property
    def history_file(self) -> Path:
        return self.state_dir / "history.jsonl"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def calibration_file(self) -> Path:
        return self.state_dir / "calibration.json"

    @property
    def throttle_file(self) -> Path:
        return self.state_dir / "throttle.json"

    @property
    def control_file(self) -> Path:
        return self.state_dir / "control.json"

    @property
    def goal_file(self) -> Path:
        """Per-user weekly-goal override; wins over config.json when present."""
        return self.state_dir / "goal.json"

    @property
    def stop_file(self) -> Path:
        """Written once the weekly goal is reached; the main session's stop point."""
        return self.state_dir / "stop.json"

    @property
    def handover_file(self) -> Path:
        """Fork handover record; the parent session watches this file."""
        return self.state_dir / "handover.json"

    @property
    def graph_file(self) -> Path:
        """Per-user agentic-graph override; wins over config.json like goal.json."""
        return self.state_dir / "graph.json"

    @property
    def report_file(self) -> Path:
        """Record of the last work-distribution report the tracker generated."""
        return self.state_dir / "report.json"

    @property
    def reports_dir(self) -> Path:
        """Where the generated ledger pages live (reports/latest.html and friends)."""
        return self.root / "reports"


_PATH_KEYS = (
    "credentials_path", "projects_dir", "sessions_dir", "state_dir", "logs_dir",
    "tasks_file", "sundial_shell_path", "local_ft_bin",
)


def load_config(root: str | Path) -> Config:
    root = Path(root).resolve()
    raw: dict[str, Any] = {}
    cfg_file = root / "config.json"
    if cfg_file.exists():
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    home = Path.home()
    appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
    defaults: dict[str, Any] = {
        "credentials_path": home / ".claude" / ".credentials.json",
        "projects_dir": home / ".claude" / "projects",
        "sessions_dir": home / ".claude" / "sessions",
        "state_dir": root / "state",
        "logs_dir": root / "logs",
        "tasks_file": root / "tasks.json",
        "sundial_shell_path": Path(appdata) / "Sundial" / "shell.json",
    }
    valid = {f.name for f in fields(Config)} - {"root"}
    kwargs: dict[str, Any] = dict(defaults)
    for key, value in raw.items():
        if key not in valid:
            continue
        if key in _PATH_KEYS:
            p = Path(str(value).replace("~", str(home), 1)) if str(value).startswith("~") else Path(value)
            kwargs[key] = p if p.is_absolute() else root / p
        else:
            kwargs[key] = value

    cfg = Config(root=root, **kwargs)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    # The agentic graph is authoritative over the legacy scalar keys, so it is
    # resolved (config.json + state/graph.json override) and folded onto the
    # Config here, before any caller reads max_concurrency or worker_model.
    # Imported late: graph.py imports Config from this module.
    from .graph import apply_graph, default_graph, migrate_config_file
    if cfg_file.exists() and not isinstance(raw.get("graph"), dict):
        # One-time migration: a config.json that predates the graph gets the
        # section written from the keys it already has, then keeps both.
        migrate_config_file(cfg)
    if not cfg.graph:
        # Freeze the legacy-derived graph as this Config's baseline, so the
        # per-user override stays an override: deleting state/graph.json goes
        # back to the file's own numbers rather than to the last ones applied.
        cfg.graph = default_graph(cfg)
    apply_graph(cfg)
    return cfg
