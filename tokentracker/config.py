from __future__ import annotations

import copy
import json
import os
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
CONFIG_NAME = "config.json"


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
    # The Fable window's own stopping point. Null (the default) means "the same
    # number as weekly_goal", so an operator who sets one goal sets both.
    fable_goal: float | None = None
    # Automatic allocation (tokentracker/allocator.py): ahead_step, behind_step,
    # min_advisory, min_workers, max_fork_cooldown_seconds, min_dwell_seconds.
    # Every key has a built-in default, so an empty block means "use them".
    allocation: dict[str, Any] = field(default_factory=dict)
    # Screenshot policy (tokentracker/snapshot.py): enabled, eod_local,
    # lead_minutes, reserve_fraction, repo, min_gap_minutes. Same contract as
    # `allocation`: every key defaults, so {} means "the built-in policy".
    snapshot: dict[str, Any] = field(default_factory=dict)
    # The operator's clock (tokentracker/clock.py). Everything on disk stays
    # ISO UTC; these only decide what the screen says. zoneinfo is tried first
    # and `tz_offset_hours` is the fallback for a box with no tz database,
    # which is what Windows is unless `tzdata` is installed.
    timezone: str = "America/Chicago"
    tz_offset_hours: float = -5.0
    tz_label: str = ""
    # The run loop merges tasks.json from disk before every apply, so a
    # `tracker.py add` made while it runs is picked up instead of being
    # overwritten by the loop's own save. `--no-supervise` opts one run out.
    supervise: bool = True
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
    # How long state/limited.json keeps a tier on its fallback model before the
    # primary is tried again (graph.read_limited).
    fallback_minutes: float = 30.0
    # Published list prices per model, USD per 1M tokens, each row carrying the
    # source and the date it was read (tokentracker/pricing.py). A model that is
    # missing here is reported as "unpriced"; nothing is ever guessed.
    pricing: dict[str, Any] = field(default_factory=dict)
    # The row to bill a model the table does not name. Null on purpose.
    pricing_default: dict[str, Any] | None = None
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
    # How often the loop rebuilds state/tiers.json, the per-tier token shares
    # the overlay's ladder bars read (tokentracker/ledger.py).
    tiers_refresh_seconds: int = 300
    # Bookkeeping for the config re-read, never read from config.json itself:
    # the raw payload of the last read and the (mtime, size) it was read at, so
    # `reload_config` can tell a real edit from a re-stat and name the keys that
    # actually changed rather than the ones the graph derived.
    config_raw: dict[str, Any] = field(default_factory=dict, repr=False)
    config_sig: tuple[float, int] | None = field(default=None, repr=False)

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
    def allocation_file(self) -> Path:
        """This poll's bucket forecasts and the ladder rung they put the graph on.

        Written by the loop every tick (tokentracker/allocator.py) and read by
        everything that needs the graph actually in force - `apply_graph`, the
        fork's brief, the panel - so the allocation is decided in one place and
        applied everywhere from a file.
        """
        return self.state_dir / "allocation.json"

    @property
    def snapshot_file(self) -> Path:
        """When the gallery was last refreshed, why, and what it committed.

        {last_run, last_reason, last_commit, next_eod, forecast_trigger_at,
        commits} - written by the loop's snapshot policy
        (tokentracker/snapshot.py) and read by `status`, the panel and the
        ledger's milestone table.
        """
        return self.state_dir / "snapshot.json"

    @property
    def limited_file(self) -> Path:
        """Which model is currently limited/overloaded, and since when.

        Written by the dispatcher when a launch fails on a 529 or a limit;
        read on every launch, so the tier keeps using its fallback instead of
        walking back into the same wall once a poll.
        """
        return self.state_dir / "limited.json"

    @property
    def handover_log(self) -> Path:
        """Append-only history of every handover record the dispatcher wrote.

        state/handover.json holds only the newest fork; this file keeps them
        all, one JSON object per line, which is how the ledger learns which
        model each fork session actually ran on (`role` stamps).
        """
        return self.state_dir / "handover.log"

    @property
    def tiers_file(self) -> Path:
        """Per-tier input/output token totals over the report window.

        Written by the loop every `tiers_refresh_seconds`; the overlay's ladder
        bars read this and nothing else, so drawing a frame never parses a
        transcript.
        """
        return self.state_dir / "tiers.json"

    @property
    def ledger_cache_file(self) -> Path:
        """Per-transcript tallies keyed by path + mtime + size.

        A fork's transcript only ever grows, so an unchanged file is re-read
        from here instead of from disk; see ledger.build_tiers.
        """
        return self.state_dir / "ledger_cache.json"

    @property
    def pricing_file(self) -> Path:
        """Per-user price override; wins over config.json like goal/graph.json."""
        return self.state_dir / "pricing.json"

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
# Fields the process keeps for itself; a config.json naming one is ignored.
RUNTIME_KEYS = ("config_raw", "config_sig")


def config_file(cfg: Config) -> Path:
    return cfg.root / CONFIG_NAME


def config_signature(cfg: Config) -> tuple[float, int] | None:
    """(mtime, size) of config.json, or None when it cannot be stat'd.

    Size rides along with the mtime because a file rewritten inside one
    filesystem timestamp tick - an editor saving twice in the same second -
    keeps the mtime it had, and the loop would then hold the stale graph for
    as long as nothing else touched the file.
    """
    try:
        stat = config_file(cfg).stat()
    except (OSError, AttributeError, TypeError):
        return None
    return (stat.st_mtime, stat.st_size)


def read_config_raw(cfg: Config) -> dict[str, Any] | None:
    """config.json as a dict, or None when it is missing or unreadable."""
    try:
        raw = json.loads(config_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        # ValueError covers JSONDecodeError and the UnicodeDecodeError a
        # half-written file gives back.
        return None
    return raw if isinstance(raw, dict) else None


def _field_default(name: str) -> Any:
    for f in fields(Config):
        if f.name != name:
            continue
        if f.default is not MISSING:
            return f.default
        if f.default_factory is not MISSING:  # type: ignore[misc]
            return f.default_factory()  # type: ignore[misc]
    return None


def reloadable_keys() -> set[str]:
    """The config.json keys a live reload may set on a running Config.

    Paths are deliberately excluded: `state_dir`, `tasks_file` and friends are
    resolved once at load, and the dispatcher, the history file, the overlay
    and the loop all hold objects built from them. Moving one under a running
    loop would split its state across two directories.
    """
    return ({f.name for f in fields(Config)} - {"root"}
            - set(RUNTIME_KEYS) - set(_PATH_KEYS))


def apply_raw(cfg: Config, raw: dict[str, Any]) -> list[str]:
    """Set every reloadable field `raw` names onto `cfg`; returns what changed.

    "Changed" is measured against the previous *raw payload*, not against the
    live attributes, because several attributes are derived rather than read:
    `apply_graph` writes the graph's worker count onto `max_concurrency`, so a
    Config running a state/graph.json override would otherwise report
    `max_concurrency` changed on every single reload.
    """
    valid = reloadable_keys()
    previous = cfg.config_raw if isinstance(cfg.config_raw, dict) else {}
    changed: list[str] = []
    for key in sorted(valid):
        if key not in raw:
            continue
        before = previous.get(key) if previous else getattr(cfg, key, None)
        if raw[key] != before:
            changed.append(key)
        setattr(cfg, key, raw[key])
    for key in sorted((set(previous) & valid) - set(raw)):
        # A key deleted from the file goes back to the dataclass default rather
        # than keeping the value the deleted line used to set.
        changed.append(key)
        setattr(cfg, key, _field_default(key))
    # Deep-copied: `cfg.graph` is handed out of this dict, and a snapshot that
    # aliased it would compare a value against itself the next time round.
    cfg.config_raw = copy.deepcopy(raw)
    return changed


def reload_config(cfg: Config) -> list[str] | None:
    """Re-read config.json onto the live Config when the file changed on disk.

    Returns the keys whose value changed (possibly an empty list), or None when
    there was nothing to do: same (mtime, size) as the last read, or a file
    that cannot be read at all.

    This is what makes a `graph` edited while the loop runs take effect on the
    next poll. Before it existed the loop kept the graph it was started with,
    the state/graph.json override carried only the worker count, and the next
    fork launched on the stale in-memory executive model - a real failure, on
    2026-09-03 19:48 UTC.

    Never raises: it runs inside the poll and inside every overlay refresh, so
    a half-written config.json has to degrade to the values already in memory.
    The signature is recorded even for an unreadable file, so a broken config
    is parsed once rather than on every poll until it is fixed.
    """
    signature = config_signature(cfg)
    if signature is None or signature == cfg.config_sig:
        return None
    cfg.config_sig = signature
    raw = read_config_raw(cfg)
    if raw is None:
        return None
    changed = apply_raw(cfg, raw)
    from .clock import use as use_clock
    from .graph import apply_graph, default_graph
    if not isinstance(cfg.graph, dict) or not cfg.graph:
        cfg.graph = default_graph(cfg)
    apply_graph(cfg)
    # An edited `timezone` takes effect on the next rendered line, not at the
    # next restart, the same as the graph.
    use_clock(cfg)
    return changed


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
    valid = {f.name for f in fields(Config)} - {"root"} - set(RUNTIME_KEYS)
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
    # Every display helper that renders a time without a Config in hand (the
    # overlay's draw calls, the allocator's reset labels) reads this one.
    from .clock import use as use_clock
    use_clock(cfg)
    # What was on disk, and when: `reload_config` compares against both, so the
    # loop can pick up an edit to config.json without being restarted.
    cfg.config_raw = copy.deepcopy(raw)
    cfg.config_sig = config_signature(cfg)
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
