import json
import sys
import tempfile
import time
import traceback
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokentracker import activity, dispatch, scheduler, usage
from tokentracker.config import Config
from tokentracker.models import (
    ActivityState,
    BurnRates,
    Decision,
    QueueStats,
    TaskSpec,
    UsageSnapshot,
    WindowUsage,
    utcnow,
)

NOW = utcnow()
MAIN_ID = "690b1fea-bf11-4d69-8e8f-4500794ec87d"


def make_cfg() -> Config:
    tmp = Path(tempfile.mkdtemp(prefix="tokdist_test_"))
    cfg = Config(
        root=tmp,
        credentials_path=tmp / "creds.json",
        projects_dir=tmp / "projects",
        sessions_dir=tmp / "sessions",
        state_dir=tmp / "state",
        logs_dir=tmp / "logs",
        tasks_file=tmp / "tasks.json",
    )
    for d in (cfg.state_dir, cfg.logs_dir, cfg.projects_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg


def snap(u7: float, u5: float = 0.1, left_h: float = 84.0,
         five_left_h: float = 2.0) -> UsageSnapshot:
    return UsageSnapshot(
        fetched_at=NOW,
        five_hour=WindowUsage(u5, NOW + timedelta(hours=five_left_h)),
        seven_day=WindowUsage(u7, NOW + timedelta(hours=left_h)),
    )


def rates(foreign_ema: float = 0.0) -> BurnRates:
    return BurnRates(0.0, 0.0, 0.0, foreign_ema)


def idle() -> ActivityState:
    return ActivityState(False, NOW - timedelta(hours=5))


def active(n: int = 1) -> ActivityState:
    return ActivityState(True, NOW, [f"s{i}" for i in range(n)])


QS = QueueStats(1, 1, 0)
CLASS_RATES = (1.5, 0.3)


def test_decide_yield_when_user_active():
    cfg = make_cfg()
    d = scheduler.decide(snap(0.4, left_h=72), rates(), active(3), QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "yield" and d.target_concurrency == 0, d


def test_decide_pace_when_behind():
    cfg = make_cfg()
    d = scheduler.decide(snap(0.2, left_h=48), rates(), idle(), QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "pace" and d.target_concurrency >= 1 and d.allow_heavy, d


def test_decide_coast_when_ahead():
    cfg = make_cfg()
    d = scheduler.decide(snap(0.9, left_h=120), rates(), idle(), QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "coast" and d.target_concurrency == 0, d


def test_decide_blocked_five_hour_idle_guard():
    cfg = make_cfg()
    d = scheduler.decide(snap(0.4, u5=0.97, left_h=72), rates(), idle(), QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "blocked", d


def test_decide_blocked_five_hour_active_guard():
    cfg = make_cfg()
    recent = ActivityState(False, NOW - timedelta(minutes=10))
    d = scheduler.decide(snap(0.4, u5=0.85, left_h=72), rates(), recent, QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "blocked", d


def test_decide_surge_endgame_idle():
    cfg = make_cfg()
    d = scheduler.decide(snap(0.7, left_h=6), rates(), idle(), QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "surge" and d.target_concurrency == cfg.surge_concurrency and d.allow_heavy, d


def test_decide_surge_outranks_yield():
    cfg = make_cfg()
    d = scheduler.decide(snap(0.7, left_h=6), rates(), active(), QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "surge" and d.target_concurrency == max(1, cfg.max_concurrency - 1), d


def test_decide_blocked_exhausted():
    cfg = make_cfg()
    d = scheduler.decide(snap(0.9995, left_h=30), rates(), idle(), QS, cfg, CLASS_RATES, NOW)
    assert d.mode == "blocked", d


def test_decide_no_reset_time():
    cfg = make_cfg()
    s = UsageSnapshot(NOW, WindowUsage(0.1, None), WindowUsage(0.3, None))
    d = scheduler.decide(s, rates(), idle(), QS, cfg, CLASS_RATES, NOW)
    assert d.mode in ("pace", "coast"), d


def test_pacing_bounds():
    cfg = make_cfg()
    p = scheduler.pacing(snap(0.5, left_h=6), cfg, NOW)
    assert p["reserve"] == 0.0
    p2 = scheduler.pacing(snap(0.5, left_h=100), cfg, NOW)
    assert 0.0 <= p2["elapsed_frac"] <= 1.0 and p2["reserve"] > 0


def test_parse_payload_dict():
    iso = NOW.isoformat()
    five, seven, extra = usage._parse_payload({
        "five_hour": {"utilization": 42.0, "resets_at": iso},
        "seven_day": {"utilization": 0.63, "resets_at": iso},
        "seven_day_opus": {"utilization": 10},
    })
    assert abs(five.utilization - 0.42) < 1e-9
    assert abs(seven.utilization - 0.63) < 1e-9
    assert "seven_day_opus" in extra


def test_parse_payload_list():
    five, seven, extra = usage._parse_payload([
        {"window": "5h", "utilization": 12},
        {"window": "seven_day_overall", "utilization": 55},
    ])
    assert five is not None and abs(five.utilization - 0.12) < 1e-9
    assert seven is not None and abs(seven.utilization - 0.55) < 1e-9


def test_parse_payload_limits_array():
    iso5 = (NOW + timedelta(hours=2)).isoformat()
    iso7 = (NOW + timedelta(hours=40)).isoformat()
    five, seven, extra = usage._parse_payload({
        "five_hour": {"utilization": 10.0, "resets_at": iso5},
        "seven_day": {"utilization": 25.0, "resets_at": iso7},
        "limits": [
            {"kind": "session", "percent": 10, "resets_at": iso5},
            {"kind": "weekly_all", "percent": 25, "resets_at": iso7},
            {"kind": "weekly_scoped", "percent": 29, "resets_at": iso7,
             "scope": {"model": {"id": None, "display_name": "Fable"}}},
        ],
    })
    assert "fable" in extra, extra
    assert abs(extra["fable"].utilization - 0.29) < 1e-9
    assert abs(five.utilization - 0.10) < 1e-9


def test_normalize_rolls_expired_windows():
    s = UsageSnapshot(
        fetched_at=NOW - timedelta(minutes=10),
        five_hour=WindowUsage(0.87, NOW - timedelta(minutes=5)),
        seven_day=WindowUsage(0.25, NOW + timedelta(hours=40)),
    )
    out = scheduler.normalize(s, NOW)
    assert out.five_hour.utilization == 0.0
    assert out.five_hour.resets_at > NOW
    assert abs(out.seven_day.utilization - 0.25) < 1e-9


def test_parse_payload_garbage():
    assert usage._parse_payload("hello") == (None, None, {})
    assert usage._parse_payload(5) == (None, None, {})
    five, seven, extra = usage._parse_payload({"foo": "bar"})
    assert five is None and seven is None and extra == {}


def _write_history(cfg: Config, points: list[tuple[float, float]]) -> usage.UsageHistory:
    hist = usage.UsageHistory(cfg)
    for minutes_ago, util in points:
        s = UsageSnapshot(
            fetched_at=NOW - timedelta(minutes=minutes_ago),
            five_hour=WindowUsage(0.1, None),
            seven_day=WindowUsage(util, None),
        )
        hist.append(s)
    return hist


def test_slope_rising():
    cfg = make_cfg()
    hist = _write_history(cfg, [(60, 0.50), (30, 0.515), (0, 0.53)])
    slope = hist.slope_pct_per_hr(90, NOW)
    assert slope is not None and 2.5 <= slope <= 3.5, slope


def test_slope_ignores_pre_reset():
    cfg = make_cfg()
    hist = _write_history(cfg, [(80, 0.9), (40, 0.01), (0, 0.02)])
    slope = hist.slope_pct_per_hr(120, NOW)
    assert slope is not None and 1.0 <= slope <= 2.0, slope


def test_learned_rates_priors():
    cfg = make_cfg()
    assert usage.learned_class_rates(cfg, {}) == (
        cfg.heavy_pct_per_hr_prior, cfg.light_pct_per_hr_prior)


def test_learned_rates_from_outcomes():
    cfg = make_cfg()
    cal = {
        "budget_tokens_est": 1_000_000,
        "task_outcomes": [
            {"weight": "heavy", "tokens": 10_000, "minutes": 60.0} for _ in range(5)
        ],
    }
    heavy, light = usage.learned_class_rates(cfg, cal)
    assert abs(heavy - 1.0) < 1e-6, heavy
    assert abs(light - cfg.light_pct_per_hr_prior) < 1e-6, light


def test_project_dir_name():
    assert activity.project_dir_name("C:/Users/weiha") == "C--Users-weiha"
    assert activity.project_dir_name(r"C:\Users\x\My Proj_v2.1") == "C--Users-x-My-Proj-v2-1"


def _touch(path: Path, age_seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    stamp = time.time() - age_seconds
    import os
    os.utime(path, (stamp, stamp))


def test_detect_activity_main_session_excluded():
    cfg = make_cfg()
    cfg.main_session_ids = [MAIN_ID]
    _touch(cfg.projects_dir / "proj1" / "other.jsonl", 60)
    _touch(cfg.projects_dir / "proj1" / f"{MAIN_ID}.jsonl", 60)
    _touch(cfg.projects_dir / "proj2" / "old.jsonl", 3 * 86400)
    _touch(cfg.projects_dir / "ownproj" / "mine.jsonl", 60)
    now = utcnow()

    state = activity.detect_activity(cfg, {"ownproj"}, now)
    assert state.user_active, state
    assert "proj1" in state.active_foreign_sessions
    assert "ownproj" not in state.active_foreign_sessions
    assert "proj2" not in state.active_foreign_sessions

    _touch(cfg.projects_dir / "proj1" / "other.jsonl", 3 * 86400)
    state2 = activity.detect_activity(cfg, {"ownproj"}, now)
    assert not state2.user_active, state2


def test_scan_local_tokens_weighting_and_main():
    cfg = make_cfg()
    cfg.main_session_ids = [MAIN_ID]
    recent = (utcnow() - timedelta(minutes=10)).isoformat()
    stale = (utcnow() - timedelta(hours=2)).isoformat()
    proj = cfg.projects_dir / "proj1"
    proj.mkdir(parents=True)
    lines = [
        json.dumps({"type": "assistant", "timestamp": recent, "message": {"usage": {
            "input_tokens": 100, "output_tokens": 50,
            "cache_creation_input_tokens": 10, "cache_read_input_tokens": 1000}}}),
        "not json at all",
        json.dumps({"type": "assistant", "timestamp": stale, "message": {"usage": {
            "input_tokens": 999}}}),
    ]
    (proj / "f1.jsonl").write_text("\n".join(lines), encoding="utf-8")
    (proj / f"{MAIN_ID}.jsonl").write_text(json.dumps(
        {"type": "assistant", "timestamp": recent,
         "message": {"usage": {"input_tokens": 40}}}) + "\n", encoding="utf-8")

    result = usage.scan_local_tokens(cfg, utcnow() - timedelta(hours=1), utcnow())
    assert result.get("proj1") == 260, result
    assert result.get(usage.MAIN_SESSION_KEY) == 40, result


def _seed_tasks(cfg: Config) -> None:
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "a", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "priority": 5, "status": "pending"},
        {"id": "b", "prompt": "p", "cwd": str(cfg.root), "weight": "light",
         "status": "running", "pid": 999999},
    ]}), encoding="utf-8")


def test_dispatcher_orphan_and_queue():
    cfg = make_cfg()
    _seed_tasks(cfg)
    d = dispatch.Dispatcher(cfg, supervise=True)
    b = d.get("b")
    assert b.status == "failed" and "orphaned" in (b.error or ""), b
    stats = d.queue_stats()
    assert stats.pending_heavy == 1 and stats.pending_light == 0 and stats.running == 0

    try:
        d.add(TaskSpec(id="a", prompt="x", cwd=str(cfg.root)))
        raise AssertionError("duplicate id accepted")
    except dispatch.DispatchError:
        pass

    actions = d.apply(Decision("yield", 0, False, "test"), utcnow())
    assert actions == [] and d.get("a").status == "pending"

    d.set_status("b", "pending")
    assert d.get("b").error is None


def test_cli_dispatcher_never_orphan_marks():
    cfg = make_cfg()
    _seed_tasks(cfg)
    d = dispatch.Dispatcher(cfg)
    b = d.get("b")
    assert b.status == "running" and b.error is None, b
    on_disk = json.loads(cfg.tasks_file.read_text(encoding="utf-8"))
    assert on_disk["tasks"][1]["status"] == "running", on_disk


def test_sync_from_disk_merges_external_edits():
    cfg = make_cfg()
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "mine", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "status": "running", "pid": 1},
        {"id": "stale", "prompt": "p", "cwd": str(cfg.root), "status": "pending"},
    ]}), encoding="utf-8")
    d = dispatch.Dispatcher(cfg, supervise=False)
    # Simulate loop ownership of "mine" via a live proc handle.
    out = open(cfg.logs_dir / "o", "w")
    err = open(cfg.logs_dir / "e", "w")
    fake = types.SimpleNamespace(returncode=None, pid=1, poll=lambda: None)
    d._procs["mine"] = dispatch._Proc(fake, out, err)
    # External CLI: adds "new", cancels "stale", falsely orphan-marks "mine".
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "mine", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "status": "failed", "error": "orphaned by tracker restart"},
        {"id": "stale", "prompt": "p", "cwd": str(cfg.root), "status": "failed"},
        {"id": "new", "prompt": "p", "cwd": str(cfg.root), "status": "pending"},
    ]}), encoding="utf-8")
    d.sync_from_disk()
    assert d.get("mine").status == "running"      # proc-owned: memory wins
    assert d.get("stale").status == "failed"      # external cancel sticks
    assert d.get("new") is not None               # external add adopted
    assert d.get("new").status == "pending"


def test_supervisor_adopts_live_orphan():
    cfg = make_cfg()
    my_pid = __import__("os").getpid()
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "fork", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "status": "running", "pid": my_pid},
        {"id": "dead", "prompt": "p", "cwd": str(cfg.root), "weight": "light",
         "status": "running", "pid": 999999},
    ]}), encoding="utf-8")
    d = dispatch.Dispatcher(cfg, supervise=True)
    # Live pid (this test process) adopted; impossible pid orphan-marked.
    assert d.get("fork").status == "running" and "fork" in d._adopted
    assert d.get("dead").status == "failed" and "orphaned" in d.get("dead").error


def test_adopted_task_finalizes_from_output_file():
    cfg = make_cfg()
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "fork", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "status": "running", "pid": 999999,
         "started_at": (utcnow() - timedelta(minutes=3)).isoformat()},
    ]}), encoding="utf-8")
    (cfg.logs_dir / "fork.out.json").write_text(json.dumps({
        "is_error": False, "total_cost_usd": 1.5,
        "usage": {"input_tokens": 10, "output_tokens": 5}}), encoding="utf-8")
    d = dispatch.Dispatcher(cfg, supervise=False)
    d._adopted["fork"] = 999999
    actions = d.reap(utcnow())
    task = d.get("fork")
    assert task.status == "done" and task.cost_usd == 1.5, (task, actions)
    assert any("[adopted]" in a for a in actions), actions
    assert "fork" not in d._adopted


def test_dispatcher_finalize():
    cfg = make_cfg()
    _seed_tasks(cfg)
    d = dispatch.Dispatcher(cfg)
    task = d.get("a")
    task.status = "running"
    task.started_at = (utcnow() - timedelta(minutes=5)).isoformat()
    (cfg.logs_dir / "a.out.json").write_text(json.dumps({
        "total_cost_usd": 0.5, "is_error": False,
        "usage": {"input_tokens": 100, "output_tokens": 50}}), encoding="utf-8")
    out = open(cfg.logs_dir / "dummy1", "w")
    err = open(cfg.logs_dir / "dummy2", "w")
    fake = types.SimpleNamespace(returncode=0, pid=1)
    line = d._finalize(task, dispatch._Proc(fake, out, err), utcnow())
    assert task.status == "done" and task.session_tokens == 150, (task, line)
    assert task.cost_usd == 0.5
    cal = usage.load_calibration(cfg)
    outcomes = cal.get("task_outcomes", [])
    assert outcomes and outcomes[-1]["weight"] == "heavy", cal


def _local_cfg() -> Config:
    cfg = make_cfg()
    cfg.local_enabled = True
    cfg.local_model = "Qwen-test"
    cfg.local_max_concurrency = 1
    return cfg


def test_decide_local_blocked_idle():
    cfg = _local_cfg()
    blocked = Decision("blocked", 0, False, "t")
    assert scheduler.decide_local(blocked, idle(), cfg, NOW) == 1


def test_decide_local_respects_activity():
    cfg = _local_cfg()
    blocked = Decision("blocked", 0, False, "t")
    assert scheduler.decide_local(blocked, active(), cfg, NOW) == 0
    cfg.local_when_active = True
    assert scheduler.decide_local(blocked, active(), cfg, NOW) == 1


def test_decide_local_only_when_blocked():
    cfg = _local_cfg()
    for mode in ("pace", "surge", "coast", "yield"):
        assert scheduler.decide_local(Decision(mode, 1, True, "t"), idle(), cfg, NOW) == 0
    cfg.local_enabled = False
    assert scheduler.decide_local(Decision("blocked", 0, False, "t"), idle(), cfg, NOW) == 0


def test_local_env():
    cfg = _local_cfg()
    base = {"ANTHROPIC_API_KEY": "sk-real", "PATH": "x"}
    env = dispatch.local_env(cfg, base)
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_BASE_URL"] == cfg.local_base_url
    assert env["ANTHROPIC_AUTH_TOKEN"] == cfg.local_auth_token
    for key in dispatch.LOCAL_MODEL_ENV_KEYS:
        assert env[key] == "Qwen-test", key
    assert env["API_TIMEOUT_MS"] == str(cfg.local_api_timeout_ms)
    assert env["PATH"] == "x"
    assert base["ANTHROPIC_API_KEY"] == "sk-real"


def test_dispatcher_local_apply():
    cfg = _local_cfg()
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "fork", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "priority": 9, "status": "pending", "resume_session": "sid-1"},
        {"id": "pod", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "priority": 5, "status": "pending"},
    ]}), encoding="utf-8")
    d = dispatch.Dispatcher(cfg)
    launched: list[tuple[str, str]] = []

    def fake_launch(task, now, lane="cloud"):
        task.status = "running"
        task.lane = lane
        launched.append((task.id, lane))
        return f"task {task.id}: launched {lane}"

    d.launch = fake_launch
    d.local_engine_up = lambda: True
    decision = Decision("blocked", 0, False, "t", local_concurrency=1)
    d.apply(decision, utcnow())
    # Forked main-session tasks never run locally; the pod task does.
    assert launched == [("pod", "local")], launched


def test_dispatcher_local_engine_down():
    cfg = _local_cfg()
    cfg.local_autostart = False
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "pod", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "status": "pending"},
    ]}), encoding="utf-8")
    d = dispatch.Dispatcher(cfg)
    d.local_engine_up = lambda: False
    decision = Decision("blocked", 0, False, "t", local_concurrency=1)
    actions = d.apply(decision, utcnow())
    assert d.get("pod").status == "pending"
    assert any("local engine" in a for a in actions), actions


def test_dispatcher_finalize_local_skips_calibration():
    cfg = _local_cfg()
    _seed_tasks(cfg)
    d = dispatch.Dispatcher(cfg)
    task = d.get("a")
    task.status = "running"
    task.lane = "local"
    task.started_at = (utcnow() - timedelta(minutes=5)).isoformat()
    (cfg.logs_dir / "a.out.json").write_text(json.dumps({
        "is_error": False, "usage": {"input_tokens": 10, "output_tokens": 5}}),
        encoding="utf-8")
    out = open(cfg.logs_dir / "dummy1", "w")
    err = open(cfg.logs_dir / "dummy2", "w")
    fake = types.SimpleNamespace(returncode=0, pid=1)
    line = d._finalize(task, dispatch._Proc(fake, out, err), utcnow())
    assert task.status == "done" and "local" in line, (task, line)
    cal = usage.load_calibration(cfg)
    assert not cal.get("task_outcomes"), cal


def test_reap_local_minutes_multiplier():
    cfg = _local_cfg()
    cfg.local_minutes_multiplier = 3.0
    _seed_tasks(cfg)
    d = dispatch.Dispatcher(cfg)
    task = d.get("a")
    task.status = "running"
    task.lane = "local"
    task.max_minutes = 90
    task.started_at = (utcnow() - timedelta(minutes=120)).isoformat()
    out = open(cfg.logs_dir / "dummy1", "w")
    err = open(cfg.logs_dir / "dummy2", "w")
    fake = types.SimpleNamespace(returncode=None, pid=1,
                                 poll=lambda: None, wait=lambda timeout=None: 0)
    d._procs["a"] = dispatch._Proc(fake, out, err)
    d._kill_tree = lambda pid: None
    assert d.reap(utcnow()) == []
    assert task.status == "running"
    task.lane = "cloud"
    actions = d.reap(utcnow())
    assert task.status == "killed", (task, actions)


def test_local_prompt_preamble():
    cfg = _local_cfg()
    d = dispatch.Dispatcher(cfg)
    task = TaskSpec(id="t", prompt="fix the bug", cwd=str(cfg.root))
    assert d._task_prompt(task, "cloud") == "fix the bug"
    assert d._task_prompt(task, "local") == "fix the bug"
    cfg.local_prompt_preamble = "no GPU work"
    assert d._task_prompt(task, "local") == "no GPU work\n\nfix the bug"
    assert d._task_prompt(task, "cloud") == "fix the bug"
    # The {graph} placeholder is expanded at launch, on both lanes, so a row
    # queued before a graph change still goes out with the current counts.
    from tokentracker import graph as graph_mod
    cfg.max_concurrency = 12
    task.prompt = "brief. {graph} end."
    line = graph_mod.graph_line(graph_mod.read_graph(cfg))
    assert "x12" in line, line
    assert d._task_prompt(task, "cloud") == f"brief. {line} end."
    assert d._task_prompt(task, "local").endswith(f"brief. {line} end.")


def test_task_model_selection():
    cfg = _local_cfg()
    d = dispatch.Dispatcher(cfg)
    task = TaskSpec(id="t", prompt="p", cwd=str(cfg.root))
    assert d._task_model(task, "cloud") is None
    cfg.worker_model = "claude-opus-4-8"
    assert d._task_model(task, "cloud") == "claude-opus-4-8"
    task.model = "haiku"
    assert d._task_model(task, "cloud") == "haiku"
    assert d._task_model(task, "local") == "Qwen-test"


def test_throttle_task_uses_throttle_model():
    from tokentracker import cli
    cfg = _local_cfg()
    cfg.main_session_ids = [MAIN_ID]
    cfg.throttle_model = "claude-fable-5-1"
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d)
    task = d.get(cli.THROTTLE_TASK_ID)
    assert task is not None and task.model == "claude-fable-5-1", task
    assert task.resume_session == MAIN_ID


def test_throttle_fork_disabled():
    from tokentracker import cli
    cfg = _local_cfg()
    cfg.main_session_ids = [MAIN_ID]
    cfg.throttle_fork_enabled = False
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d)
    assert d.get(cli.THROTTLE_TASK_ID) is None
    # An existing finished fork row must stay finished (no resurrect).
    d.add(TaskSpec(id=cli.THROTTLE_TASK_ID, prompt="p", cwd=str(cfg.root),
                   status="done"))
    cli._ensure_throttle_task(cfg, d)
    assert d.get(cli.THROTTLE_TASK_ID).status == "done"


def test_throttle_task_respec_on_requeue():
    from tokentracker import cli
    cfg = _local_cfg()
    cfg.main_session_ids = ["old-session"]
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d)
    d.set_status(cli.THROTTLE_TASK_ID, "killed")
    cfg.main_session_ids = [MAIN_ID]
    cfg.throttle_model = "claude-fable-5-1"
    cli._ensure_throttle_task(cfg, d)
    task = d.get(cli.THROTTLE_TASK_ID)
    assert task.status == "pending", task
    assert task.model == "claude-fable-5-1" and task.resume_session == MAIN_ID, task


def _fork_cfg() -> Config:
    cfg = _local_cfg()
    cfg.main_session_ids = [MAIN_ID]
    cfg.throttle_model = "claude-opus-5"
    cfg.throttle_fork_enabled = True
    cfg.fork_in_pace = True
    cfg.throttle_prompt = "director brief"
    return cfg


def test_fork_wanted_in_pace_and_surge():
    # The handover is armed in normal pace mode, not only under full throttle.
    from tokentracker import cli, control
    cfg = _fork_cfg()
    for mode in ("pace", "surge"):
        assert cli._fork_wanted(cfg, mode, control.RUNNING, False), mode
        assert cli._fork_wanted(cfg, mode, control.RUNNING, True), mode
    cfg.fork_in_pace = False
    assert not cli._fork_wanted(cfg, "pace", control.RUNNING, False)
    assert cli._fork_wanted(cfg, "surge", control.RUNNING, True)


def test_fork_not_wanted_when_stopped_or_goal_reached():
    from tokentracker import cli, control, goal
    cfg = _fork_cfg()
    assert not cli._fork_wanted(cfg, "pace", control.STOPPED, False)
    assert not cli._fork_wanted(cfg, "surge", control.STOPPED, True)
    goal.write_goal(cfg, 0.50)
    goal.apply_goal_stop(cfg, 0.90, NOW)
    assert cfg.stop_file.exists()
    assert not cli._fork_wanted(cfg, "pace", control.RUNNING, False)
    assert not cli._fork_wanted(cfg, "surge", control.RUNNING, True)
    goal.clear_stop(cfg)
    assert cli._fork_wanted(cfg, "pace", control.RUNNING, False)
    cfg.throttle_fork_enabled = False
    assert not cli._fork_wanted(cfg, "pace", control.RUNNING, False)
    cfg.throttle_fork_enabled = True
    cfg.main_session_ids = []
    assert not cli._fork_wanted(cfg, "pace", control.RUNNING, False)


def test_fork_not_wanted_in_blocked_yield_coast():
    from tokentracker import cli, control
    cfg = _fork_cfg()
    for mode in ("blocked", "yield", "coast", "stopped"):
        assert not cli._fork_wanted(cfg, mode, control.RUNNING, False), mode
        assert not cli._fork_wanted(cfg, mode, control.RUNNING, True), mode


def test_tick_ensures_fork_in_pace_and_retires_it_when_stopped():
    from tokentracker import cli, control
    cfg = _fork_cfg()
    d, launched = _gate_dispatcher(cfg)
    history = usage.UsageHistory(cfg)
    real_fetch = usage.fetch_usage
    usage.fetch_usage = lambda _cfg: snap(0.2, left_h=100.0)
    try:
        decision, _a, _e, _s, _r = cli._tick(cfg, d, history)
        assert decision.mode == "pace", decision
        task = d.get(cli.THROTTLE_TASK_ID)
        assert task is not None, d.tasks()
        assert task.prompt == "director brief" and task.priority == 100, task
        assert task.resume_session == MAIN_ID and task.model == "claude-opus-5", task
        assert launched == [(cli.THROTTLE_TASK_ID, "cloud")], launched
        # STOP retires a fork that has not launched yet, and arms nothing new.
        d.set_status(cli.THROTTLE_TASK_ID, "pending")
        control.write_control(cfg, control.STOPPED)
        cli._tick(cfg, d, history)
        assert d.get(cli.THROTTLE_TASK_ID).status == "killed", d.tasks()
    finally:
        usage.fetch_usage = real_fetch


def test_fork_rearm_waits_for_cooldown():
    from tokentracker import cli
    cfg = _fork_cfg()
    cfg.fork_cooldown_seconds = 120
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d, NOW)
    task = d.get(cli.THROTTLE_TASK_ID)
    task.status = "failed"
    task.finished_at = NOW.isoformat()
    cli._ensure_throttle_task(cfg, d, NOW + timedelta(seconds=60))
    assert d.get(cli.THROTTLE_TASK_ID).status == "failed"
    cli._ensure_throttle_task(cfg, d, NOW + timedelta(seconds=121))
    assert d.get(cli.THROTTLE_TASK_ID).status == "pending"
    # A pending or running fork is never disturbed by a later tick.
    d.get(cli.THROTTLE_TASK_ID).status = "running"
    cli._ensure_throttle_task(cfg, d, NOW + timedelta(hours=1))
    assert d.get(cli.THROTTLE_TASK_ID).status == "running"


def test_fork_argv_never_launches_model_less():
    from tokentracker import cli, handover
    cfg = _fork_cfg()
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d, NOW)
    task = d.get(cli.THROTTLE_TASK_ID)
    argv = d._argv(task, "cloud", "claude.exe")
    assert argv[argv.index("--model") + 1] == "claude-opus-5", argv
    assert argv[argv.index("--resume") + 1] == MAIN_ID, argv
    assert "--fork-session" in argv, argv
    # Every model source emptied: the fork still names one (the audited launch
    # that went out with model=None must not be reachable again).
    task.model = None
    cfg.throttle_model = ""
    cfg.worker_model = ""
    assert d._task_model(task, "cloud") == handover.FORK_FALLBACK_MODEL
    argv = d._argv(task, "cloud", "claude.exe")
    assert argv[argv.index("--model") + 1] == handover.FORK_FALLBACK_MODEL, argv
    # An ordinary worker keeps the old behaviour (no --model at all).
    plain = TaskSpec(id="pod", prompt="p", cwd=str(cfg.root))
    assert d._task_model(plain, "cloud") is None
    assert "--model" not in d._argv(plain, "cloud", "claude.exe")


def _fake_subprocess(capture: list) -> object:
    """Stand-in for the subprocess module inside dispatch (no real process)."""
    import io as _io
    import subprocess as _sp

    def fake_popen(cmd, **_kwargs):
        capture.append(list(cmd))
        return types.SimpleNamespace(pid=4242, stdin=_io.StringIO(),
                                     returncode=None, poll=lambda: None)

    return types.SimpleNamespace(
        PIPE=_sp.PIPE, Popen=fake_popen,
        CREATE_NEW_PROCESS_GROUP=getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0),
        CREATE_NO_WINDOW=getattr(_sp, "CREATE_NO_WINDOW", 0))


def test_fork_handover_written_on_launch_and_updated_on_finish():
    from tokentracker import cli, handover
    cfg = _fork_cfg()
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d, NOW)
    task = d.get(cli.THROTTLE_TASK_ID)
    d.current_mode = "pace"
    captured: list[list[str]] = []
    real_sub, real_shutil = dispatch.subprocess, dispatch.shutil
    dispatch.subprocess = _fake_subprocess(captured)
    dispatch.shutil = types.SimpleNamespace(which=lambda _n: "C:/fake/claude.exe")
    try:
        line = d.launch(task, NOW)
    finally:
        dispatch.subprocess, dispatch.shutil = real_sub, real_shutil
    assert "launched cloud" in line, line
    argv = captured[0]
    assert argv[argv.index("--model") + 1] == "claude-opus-5", argv
    assert argv[argv.index("--resume") + 1] == MAIN_ID, argv
    assert "--fork-session" in argv, argv

    rec = handover.read_handover(cfg)
    assert list(rec) == list(handover.START_KEYS), rec
    assert rec["task_id"] == cli.THROTTLE_TASK_ID and rec["status"] == "started"
    assert rec["mode"] == "pace" and rec["model"] == "claude-opus-5", rec
    assert rec["parent_session"] == MAIN_ID and rec["fork_session_id"] is None
    assert rec["started_at"] == NOW.isoformat(), rec
    assert handover.fork_active(cfg)

    (cfg.logs_dir / f"{task.id}.out.json").write_text(json.dumps({
        "is_error": False, "total_cost_usd": 2.25, "session_id": "fork-sid-9",
        "usage": {"input_tokens": 100, "output_tokens": 50,
                  "cache_creation_input_tokens": 10}}), encoding="utf-8")
    end = NOW + timedelta(minutes=5)
    d._finalize_record(task, 0, end)
    done = handover.read_handover(cfg)
    assert set(done) == set(handover.HANDOVER_KEYS), done
    assert done["status"] == "done" and done["tokens"] == 160, done
    assert done["cost_usd"] == 2.25, done
    assert done["fork_session_id"] == "fork-sid-9", done
    assert done["finished_at"] == end.isoformat(), done
    # The launch half is preserved in place, not rewritten.
    assert done["mode"] == "pace" and done["started_at"] == NOW.isoformat(), done
    assert not handover.fork_active(cfg)


def test_fork_kill_paths_close_the_handover():
    # Both timeout kills - the loop's own process and an adopted session from a
    # previous loop - must close the record, or the monitor keeps reporting a
    # director that was just killed.
    from tokentracker import cli, handover
    for adopted in (False, True):
        cfg = _fork_cfg()
        d = dispatch.Dispatcher(cfg)
        cli._ensure_throttle_task(cfg, d, NOW)
        task = d.get(cli.THROTTLE_TASK_ID)
        task.status = "running"
        task.started_at = (NOW - timedelta(hours=9)).isoformat()  # max 240 min
        pid = __import__("os").getpid() if adopted else 999999
        task.pid = pid
        handover.write_handover(cfg, task_id=task.id, mode="surge",
                                model="claude-opus-5", parent_session=MAIN_ID,
                                started_at=task.started_at)
        assert handover.fork_active(cfg)
        killed: list[int] = []
        d._kill_tree = killed.append
        if adopted:
            d._adopted[task.id] = pid
        else:
            out = open(cfg.logs_dir / "k.out", "w")
            err = open(cfg.logs_dir / "k.err", "w")
            fake = types.SimpleNamespace(pid=pid, returncode=None,
                                         poll=lambda: None,
                                         wait=lambda timeout=None: 0)
            d._procs[task.id] = dispatch._Proc(fake, out, err)
        actions = d.reap(NOW)
        assert killed == [pid], (adopted, killed, actions)
        assert d.get(cli.THROTTLE_TASK_ID).status == "killed", d.tasks()
        rec = handover.read_handover(cfg)
        assert set(rec) == set(handover.HANDOVER_KEYS), (adopted, rec)
        assert rec["status"] == "failed", (adopted, rec)
        assert rec["finished_at"] == NOW.isoformat(), (adopted, rec)
        # The launch half survives the close.
        assert rec["mode"] == "surge" and rec["model"] == "claude-opus-5", rec
        assert not handover.fork_active(cfg), (adopted, rec)


def test_orphaned_fork_closes_the_handover():
    # Loop and fork die together (reboot): on restart the fork row is
    # orphan-marked, and the handover has to close with it. Otherwise `status`
    # and the overlay chip keep announcing a director that no longer exists -
    # forever, if a stop point means the fork is never re-armed.
    from tokentracker import cli, handover
    from tokentracker.models import parse_iso
    cfg = _fork_cfg()
    started = (NOW - timedelta(hours=2)).isoformat()
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": handover.FORK_TASK_ID, "prompt": "p", "cwd": str(cfg.root),
         "weight": "heavy", "status": "running", "pid": 999999,
         "started_at": started},
    ]}), encoding="utf-8")
    handover.write_handover(cfg, task_id=handover.FORK_TASK_ID, mode="pace",
                            model="claude-opus-5", parent_session=MAIN_ID,
                            started_at=started)
    assert handover.fork_active(cfg)
    d = dispatch.Dispatcher(cfg, supervise=True)
    task = d.get(handover.FORK_TASK_ID)
    assert task.status == "failed" and "orphaned" in (task.error or ""), task
    rec = handover.read_handover(cfg)
    assert set(rec) == set(handover.HANDOVER_KEYS), rec
    assert rec["status"] == "failed" and rec["mode"] == "pace", rec
    assert rec["started_at"] == started and rec["finished_at"], rec
    assert not handover.fork_active(cfg), rec
    assert handover.fork_status_line(cfg).startswith("fork: failed since")
    # finished_at is stamped too, so the re-arm cooldown actually applies to
    # the first post-restart relaunch (the launch-crash-loop case).
    finished = parse_iso(task.finished_at)
    assert finished is not None, task
    assert not cli._rearm_ready(cfg, task, finished + timedelta(seconds=60))
    assert cli._rearm_ready(cfg, task, finished + timedelta(seconds=121))


def test_handover_finish_maps_killed_and_survives_a_missing_record():
    from tokentracker import handover
    cfg = make_cfg()
    assert handover.read_handover(cfg) is None
    assert handover.fork_status_line(cfg) is None
    assert not handover.fork_active(cfg)
    rec = handover.finish_handover(cfg, status="killed", finished_at=NOW.isoformat())
    assert set(rec) == set(handover.HANDOVER_KEYS), rec
    assert rec["status"] == "failed" and rec["task_id"] is None, rec
    for junk in ("{not json", "[]", "", '"started"'):
        cfg.handover_file.write_text(junk, encoding="utf-8")
        assert handover.read_handover(cfg) is None, junk
        assert not handover.fork_active(cfg), junk
        assert handover.fork_status_line(cfg) is None, junk


def test_status_prints_fork_line():
    from tokentracker import cli, handover
    cfg = make_cfg()
    real_fetch = usage.fetch_usage
    usage.fetch_usage = lambda _cfg: snap(0.4)
    try:
        assert "fork:" not in _capture(lambda: cli.cmd_status(cfg))
        handover.write_handover(cfg, task_id=handover.FORK_TASK_ID, mode="pace",
                                model="claude-opus-5", parent_session=MAIN_ID,
                                started_at=NOW.isoformat())
        out = _capture(lambda: cli.cmd_status(cfg))
        assert (f"fork: started since {NOW.isoformat()} (pace, claude-opus-5)"
                in out.splitlines()), out
        handover.finish_handover(cfg, status="done",
                                 finished_at=NOW.isoformat(), tokens=5)
        out = _capture(lambda: cli.cmd_status(cfg))
        assert (f"fork: done since {NOW.isoformat()} (pace, claude-opus-5)"
                in out.splitlines()), out
    finally:
        usage.fetch_usage = real_fetch


def test_status_prints_report_line():
    # The monitor session quotes report freshness from `status`; before any
    # report exists the line has to say so rather than vanish.
    from tokentracker import cli
    from tokentracker.ledger import report_status_line, write_report_state
    cfg = make_cfg()
    real_fetch = usage.fetch_usage
    usage.fetch_usage = lambda _cfg: snap(0.4)
    try:
        out = _capture(lambda: cli.cmd_status(cfg))
        assert "report: none yet" in out, out
        page = cfg.reports_dir / "latest.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("<html></html>", encoding="utf-8")
        write_report_state(cfg, path=page, reason="fork milestone: new commit",
                           window={"start": NOW.isoformat(),
                                   "end": NOW.isoformat(), "hours": 12.0})
        line = report_status_line(cfg)
        assert line.startswith("report: report just now"), line
        assert "fork milestone: new commit" in line and "12h window" in line
        assert str(page) in line, line
        assert line in _capture(lambda: cli.cmd_status(cfg)).splitlines()
    finally:
        usage.fetch_usage = real_fetch


def test_repo_config_arms_the_fork_on_the_executive_model():
    # The shipped config.json is what actually revives the handover.
    from tokentracker import cli, handover
    from tokentracker.config import load_config
    cfg = load_config(ROOT)
    assert cfg.throttle_fork_enabled and cfg.fork_in_pace
    assert cfg.throttle_model == "claude-fable-5-1", cfg.throttle_model  # user directive: executive tier is Fable 5.1
    assert cfg.fork_cooldown_seconds == 120
    assert cfg.main_session_ids[0].startswith("329cb798"), cfg.main_session_ids
    prompt = cfg.throttle_prompt
    assert 0 < len(prompt) < 2500, len(prompt)
    assert "acting technical director" in prompt and "{graph}" in prompt  # models come from the graph line, not literals
    assert "monitor-only" in prompt and "dev_JSON/HANDOFF.md" in prompt
    # The stop rule the brief gives the fork must name the file the tracker
    # actually writes: the fork runs with cwd=~, so a relative state/stop.json
    # would resolve under the home directory and never exist.
    stop_path = str(cfg.stop_file).replace("\\", "/")
    assert f"Stop when {stop_path} exists" in prompt, prompt
    assert "state/stop.json" not in prompt.replace(stop_path, ""), prompt
    # dev_JSON / Tools paths stay short, so the brief has to name their root.
    assert "relative to C:/Users/chenw/StarGTA" in prompt, prompt
    # The brief carries the agentic graph as a placeholder, expanded at launch
    # so the fork is told the counts that are actually in force this poll.
    from tokentracker import graph as graph_mod
    assert "{graph}" in prompt, prompt
    expanded = cli._fork_prompt(cfg)
    assert "{graph}" not in expanded, expanded
    assert graph_mod.graph_line(graph_mod.read_graph(cfg)) in expanded, expanded
    assert expanded == prompt.replace(
        "{graph}", graph_mod.graph_line(graph_mod.read_graph(cfg)))
    task = TaskSpec(id=handover.FORK_TASK_ID, prompt=prompt, cwd=str(ROOT),
                    model=cfg.throttle_model,
                    resume_session=cfg.main_session_ids[0])
    argv = dispatch.Dispatcher(cfg)._argv(task, "cloud", "claude.exe")
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1", argv  # the fork runs on the executive model
    assert argv[argv.index("--resume") + 1].startswith("329cb798"), argv
    assert "--fork-session" in argv, argv


def test_overlay_exposes_fork_chip():
    import inspect
    try:
        import tkinter  # noqa: F401  - absent on headless builds
        from tokentracker import overlay
    except ImportError as exc:
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    assert callable(overlay.Overlay._draw_fork_chip)
    src = inspect.getsource(overlay.Overlay._draw_fork_chip)
    assert "FORK ACTIVE" in src, src
    # Scaled geometry only: raw design pixels would break on a 2x display.
    assert "P = self._px" in src and "self._pxf(" in src, src
    refresh = inspect.getsource(overlay.Overlay._refresh)
    assert "_draw_fork_chip" in refresh and "fork_active" in refresh, refresh


def test_gpu_guard():
    cfg = _local_cfg()
    d = dispatch.Dispatcher(cfg)
    cfg.local_gpu_guard_procs = []
    assert d.gpu_guard_proc() is None
    cfg.local_gpu_guard_procs = ["definitely-not-running-xyz.exe"]
    assert d.gpu_guard_proc() is None
    # The test itself runs under python.exe, so tasklist must report it.
    cfg.local_gpu_guard_procs = ["python.exe"]
    assert d.gpu_guard_proc() == "python.exe"
    cfg.local_autostart = True
    msg = d._start_local_engine(utcnow())
    assert "deferred" in msg and "python.exe" in msg, msg


def test_read_control_defaults_to_running():
    from tokentracker import control
    cfg = make_cfg()
    assert not cfg.control_file.exists()
    assert control.read_control(cfg) == control.RUNNING
    for junk in ("{not json", "[]", '"stopped"', '{"dispatch": "wat"}', ""):
        cfg.control_file.write_text(junk, encoding="utf-8")
        assert control.read_control(cfg) == control.RUNNING, junk


def test_write_control_round_trip():
    from tokentracker import control
    cfg = make_cfg()
    assert control.write_control(cfg, "stopped") == control.STOPPED
    assert control.read_control(cfg) == control.STOPPED
    payload = json.loads(cfg.control_file.read_text(encoding="utf-8"))
    assert payload["dispatch"] == "stopped"
    assert utcnow() - datetime.fromisoformat(payload["changed_at"]) < timedelta(minutes=5)
    assert control.write_control(cfg, "running") == control.RUNNING
    assert control.read_control(cfg) == control.RUNNING
    # Anything unrecognized is normalized to running, never left ambiguous.
    assert control.write_control(cfg, "nonsense") == control.RUNNING
    assert control.read_control(cfg) == control.RUNNING


def test_gate_decision_passes_through_when_running():
    from tokentracker import control
    d = Decision("pace", 3, True, "behind", local_concurrency=2)
    assert control.gate_decision(d, control.RUNNING) is d


def test_gate_decision_zeroes_both_lanes_when_stopped():
    from tokentracker import control
    d = Decision("surge", 4, True, "throttle", local_concurrency=1)
    gated = control.gate_decision(d, control.STOPPED)
    assert gated.mode == "stopped"
    assert gated.target_concurrency == 0 and gated.local_concurrency == 0
    assert not gated.allow_heavy
    assert d.target_concurrency == 4, "gate must not mutate the input decision"


def _gate_cfg() -> Config:
    cfg = _local_cfg()
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "pod", "prompt": "p", "cwd": str(cfg.root), "weight": "heavy",
         "status": "pending"},
    ]}), encoding="utf-8")
    return cfg


def test_stopped_gate_launches_nothing():
    from tokentracker import control
    cfg = _gate_cfg()
    d = dispatch.Dispatcher(cfg)
    launched: list[tuple[str, str]] = []

    def fake_launch(task, now, lane="cloud"):
        task.status = "running"
        task.lane = lane
        launched.append((task.id, lane))
        return f"task {task.id}: launched {lane}"

    d.launch = fake_launch
    d.local_engine_up = lambda: True

    running = Decision("pace", 2, True, "t", local_concurrency=1)
    d.apply(control.gate_decision(running, control.RUNNING), utcnow())
    assert launched == [("pod", "cloud")], launched

    launched.clear()
    d.get("pod").status = "pending"
    stopped = control.gate_decision(running, control.STOPPED)
    actions = d.apply(stopped, utcnow())
    assert launched == [], launched
    assert d.get("pod").status == "pending", d.get("pod")
    assert actions == [], actions


def test_stopped_gate_still_reaps_running_work():
    from tokentracker import control
    cfg = _gate_cfg()
    d = dispatch.Dispatcher(cfg)

    def never_launch(task, now, lane="cloud"):
        raise AssertionError(f"launched {task.id} while dispatch was stopped")

    d.launch = never_launch
    task = TaskSpec(id="live", prompt="p", cwd=str(cfg.root), status="running",
                    started_at=(utcnow() - timedelta(minutes=2)).isoformat())
    d.add(task)
    (cfg.logs_dir / "live.out.json").write_text(json.dumps({
        "is_error": False, "usage": {"input_tokens": 7}}), encoding="utf-8")
    out = open(cfg.logs_dir / "d1", "w")
    err = open(cfg.logs_dir / "d2", "w")
    d._procs["live"] = dispatch._Proc(
        types.SimpleNamespace(returncode=0, pid=1, poll=lambda: 0), out, err)

    stopped = control.gate_decision(
        Decision("pace", 3, True, "t", local_concurrency=1), control.STOPPED)
    actions = d.apply(stopped, utcnow())
    assert d.get("live").status == "done", d.get("live")
    assert any("live" in a for a in actions), actions


def _gate_dispatcher(cfg: Config) -> tuple[object, list]:
    d = dispatch.Dispatcher(cfg)
    launched: list[tuple[str, str]] = []

    def fake_launch(task, now, lane="cloud"):
        task.status = "running"
        task.lane = lane
        launched.append((task.id, lane))
        return f"task {task.id}: launched {lane}"

    d.launch = fake_launch
    d.local_engine_up = lambda: True
    return d, launched


def test_tick_gates_launches_on_control_file():
    # Covers the real call-site wiring in cli._tick, not just gate_decision:
    # the per-tick read, both gate points and the "dispatch" key in the state.
    from tokentracker import cli, control
    cfg = _gate_cfg()
    d, launched = _gate_dispatcher(cfg)
    history = usage.UsageHistory(cfg)
    real_fetch = usage.fetch_usage
    usage.fetch_usage = lambda _cfg: snap(0.2, left_h=100.0)
    try:
        control.write_control(cfg, control.STOPPED)
        decision, actions, _exc, _snap, _rolled = cli._tick(cfg, d, history)
        assert decision.mode == "stopped", decision
        assert decision.target_concurrency == 0 and decision.local_concurrency == 0
        assert launched == [], launched
        assert d.get("pod").status == "pending", d.get("pod")
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        assert state["dispatch"] == "stopped", state
        assert state["decision"]["mode"] == "stopped", state

        control.write_control(cfg, control.RUNNING)
        decision, actions, _exc, _snap, _rolled = cli._tick(cfg, d, history)
        assert decision.mode == "pace" and decision.target_concurrency >= 1, decision
        assert launched == [("pod", "cloud")], launched
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        assert state["dispatch"] == "running", state
    finally:
        usage.fetch_usage = real_fetch


def test_tick_gates_the_no_snapshot_branch():
    # With usage unknown the local lane normally still runs; stopped must zero
    # that too, and the state must say so.
    from tokentracker import cli, control
    cfg = _gate_cfg()
    d, launched = _gate_dispatcher(cfg)
    history = usage.UsageHistory(cfg)
    real_fetch = usage.fetch_usage

    def boom(_cfg):
        raise usage.UsageFetchError("no network")

    usage.fetch_usage = boom
    try:
        decision, _actions, exc, _snap, _rolled = cli._tick(cfg, d, history)
        assert isinstance(exc, usage.UsageFetchError), exc
        assert decision.local_concurrency >= 1, decision

        launched.clear()
        d.get("pod").status = "pending"
        control.write_control(cfg, control.STOPPED)
        decision, _actions, _exc, _snap, _rolled = cli._tick(cfg, d, history)
        assert decision.mode == "stopped", decision
        assert decision.local_concurrency == 0, decision
        assert launched == [], launched
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        assert state["dispatch"] == "stopped", state
        assert state["decision"]["mode"] == "stopped", state
    finally:
        usage.fetch_usage = real_fetch


def test_status_prints_dispatch_state():
    import contextlib
    import io

    from tokentracker import cli, control
    cfg = make_cfg()
    real_fetch = usage.fetch_usage
    usage.fetch_usage = lambda _cfg: snap(0.4)
    try:
        for mode in (control.RUNNING, control.STOPPED):
            control.write_control(cfg, mode)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert cli.cmd_status(cfg) == 0
            assert f"dispatch: {mode}" in buf.getvalue().splitlines(), buf.getvalue()
    finally:
        usage.fetch_usage = real_fetch


def test_read_goal_defaults_to_config():
    from tokentracker import goal
    cfg = make_cfg()
    assert not cfg.goal_file.exists()
    assert abs(goal.read_goal(cfg) - cfg.weekly_goal) < 1e-9
    value, source = goal.read_goal_source(cfg)
    assert source == goal.SOURCE_CONFIG and abs(value - 0.90) < 1e-9
    cfg.weekly_goal = 0.5
    assert abs(goal.read_goal(cfg) - 0.5) < 1e-9


def test_read_goal_clamps_both_ends():
    from tokentracker import goal
    cfg = make_cfg()
    for raw, want in ((0.0, goal.GOAL_MIN), (-3, goal.GOAL_MIN),
                      (2.5, goal.GOAL_MAX), (1.0, 1.0), (0.05, 0.05)):
        cfg.goal_file.write_text(json.dumps({"weekly_goal": raw}), encoding="utf-8")
        assert abs(goal.read_goal(cfg) - want) < 1e-9, raw
    # A config default out of range is clamped the same way.
    cfg.goal_file.unlink()
    cfg.weekly_goal = 0.0
    assert goal.read_goal(cfg) == goal.GOAL_MIN


def test_read_goal_survives_malformed_override():
    from tokentracker import goal
    cfg = make_cfg()
    # json.loads accepts the non-standard NaN / Infinity literals, and NaN would
    # pass every comparison in apply_goal_stop as False - a goal that can never
    # be reached. It has to read as junk, not as an override.
    for junk in ("{not json", "[]", '"0.5"', "", "{}", '{"weekly_goal": null}',
                 '{"weekly_goal": "abc"}', '{"weekly_goal": true}',
                 '{"weekly_goal": NaN}', '{"weekly_goal": Infinity}',
                 '{"weekly_goal": -Infinity}', '{"weekly_goal": "nan"}'):
        cfg.goal_file.write_text(junk, encoding="utf-8")
        value, source = goal.read_goal_source(cfg)
        assert abs(value - cfg.weekly_goal) < 1e-9, junk
        assert source == goal.SOURCE_CONFIG, junk
    # A string percentage is still honored rather than discarded.
    cfg.goal_file.write_text('{"weekly_goal": "85%"}', encoding="utf-8")
    assert abs(goal.read_goal(cfg) - 0.85) < 1e-9


def test_write_goal_round_trip():
    from tokentracker import goal
    cfg = make_cfg()
    assert abs(goal.write_goal(cfg, 0.85) - 0.85) < 1e-9
    value, source = goal.read_goal_source(cfg)
    assert abs(value - 0.85) < 1e-9 and source == goal.SOURCE_OVERRIDE
    payload = json.loads(cfg.goal_file.read_text(encoding="utf-8"))
    assert set(payload) == {"weekly_goal", "set_at"}, payload
    assert utcnow() - datetime.fromisoformat(payload["set_at"]) < timedelta(minutes=5)
    # The override wins over config.json, and out-of-range writes are clamped.
    cfg.weekly_goal = 0.4
    assert abs(goal.read_goal(cfg) - 0.85) < 1e-9
    assert goal.write_goal(cfg, 5.0) == goal.GOAL_MAX
    assert goal.write_goal(cfg, 0.0) == goal.GOAL_MIN
    # A non-finite goal is refused outright rather than stored as a target no
    # weekly value can ever reach.
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            goal.write_goal(cfg, bad)
            raise AssertionError(f"stored {bad}")
        except ValueError:
            pass
    assert abs(goal.read_goal(cfg) - goal.GOAL_MIN) < 1e-9


def test_read_goal_survives_hostile_config_default():
    # config.json is hand-edited, and read_goal runs on every poll of the run
    # loop: a bad value there must degrade to the built-in default, never raise.
    from tokentracker import goal
    cfg = make_cfg()
    for bad in (None, "ninety", "", [0.9], {}, True, float("nan"), float("inf")):
        cfg.weekly_goal = bad
        assert abs(goal.read_goal(cfg) - goal.GOAL_FALLBACK) < 1e-9, bad
        assert goal.read_goal_source(cfg)[1] == goal.SOURCE_CONFIG, bad
    # And a plausible hand edit means what the same token means on the CLI.
    for raw, want in ((90, 0.90), ("85%", 0.85), ("0.7", 0.70), (85, 0.85)):
        cfg.weekly_goal = raw
        assert abs(goal.read_goal(cfg) - want) < 1e-9, raw
        assert abs(goal.parse_goal(str(raw)) - want) < 1e-9, raw


def test_parse_goal_accepts_fraction_percent_and_suffix():
    from tokentracker import goal
    for text in ("0.85", "85", "85%", " 85 % ", ".85"):
        assert abs(goal.parse_goal(text) - 0.85) < 1e-9, text
    assert goal.parse_goal("100%") == 1.0
    assert goal.parse_goal("1") == 1.0
    assert goal.parse_goal("3") == goal.GOAL_MIN
    for bad in ("abc", "", "%", "eighty", "nan", "NaN", "inf", "-inf",
                "infinity", "nan%"):
        try:
            goal.parse_goal(bad)
            raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass


def _capture(fn) -> str:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def test_cli_goal_prints_and_sets():
    from tokentracker import cli, goal
    cfg = make_cfg()
    out = _capture(lambda: cli.cmd_goal(cfg, None))
    assert f"weekly goal: 90% (source: {goal.SOURCE_CONFIG})" in out, out
    for text, want_pct in (("0.85", 85), ("85", 85), ("85%", 85), ("0.6", 60)):
        cfg.goal_file.unlink(missing_ok=True)
        out = _capture(lambda: cli.cmd_goal(cfg, text))
        assert f"weekly goal: {want_pct}%" in out, (text, out)
        assert abs(goal.read_goal(cfg) - want_pct / 100) < 1e-9, text
        assert goal.read_goal_source(cfg)[1] == goal.SOURCE_OVERRIDE
    # Unparseable input is refused without touching the stored goal.
    assert cli.cmd_goal(cfg, "later") == 1
    assert abs(goal.read_goal(cfg) - 0.6) < 1e-9


def test_cli_goal_subcommand_wiring():
    from tokentracker import goal
    from tokentracker.cli import main as cli_main
    from tokentracker.config import load_config
    tmp = Path(tempfile.mkdtemp(prefix="tokdist_goalcli_"))
    out = _capture(lambda: cli_main(["--root", str(tmp), "goal", "85%"]))
    assert "weekly goal: 85%" in out, out
    cfg = load_config(tmp)
    assert abs(goal.read_goal(cfg) - 0.85) < 1e-9
    out = _capture(lambda: cli_main(["--root", str(tmp), "goal"]))
    assert f"weekly goal: 85% (source: {goal.SOURCE_OVERRIDE})" in out, out


def test_goal_stop_writes_once_then_clears():
    from tokentracker import control, goal
    cfg = make_cfg()
    goal.write_goal(cfg, 0.90)

    # Under goal: nothing written, dispatch untouched.
    stop, line = goal.apply_goal_stop(cfg, 0.5, NOW)
    assert stop is None and line is None
    assert not cfg.stop_file.exists()
    assert control.read_control(cfg) == control.RUNNING

    # Crossing the goal: the stop point appears and dispatch halts.
    stop, line = goal.apply_goal_stop(cfg, 0.91, NOW)
    assert list(stop) == ["reason", "goal", "weekly", "at"], stop
    assert stop["reason"] == goal.STOP_REASON
    assert abs(stop["goal"] - 0.90) < 1e-9 and abs(stop["weekly"] - 0.91) < 1e-9
    assert stop["at"] == NOW.isoformat()
    assert line and "weekly goal" in line
    on_disk = json.loads(cfg.stop_file.read_text(encoding="utf-8"))
    assert on_disk == stop, on_disk
    assert control.read_control(cfg) == control.STOPPED

    # Still over goal: the record is left exactly as written and START sticks,
    # so the operator is not re-stopped on every poll.
    control.write_control(cfg, control.RUNNING)
    again, line2 = goal.apply_goal_stop(cfg, 0.99, NOW + timedelta(hours=1))
    assert again == stop and line2 is None
    assert json.loads(cfg.stop_file.read_text(encoding="utf-8")) == stop
    assert control.read_control(cfg) == control.RUNNING

    # Weekly reset drops below the goal: cleared, but never auto-restarted.
    control.write_control(cfg, control.STOPPED)
    cleared, line3 = goal.apply_goal_stop(cfg, 0.02, NOW + timedelta(days=7))
    assert cleared is None and line3 and "cleared" in line3
    assert not cfg.stop_file.exists()
    assert control.read_control(cfg) == control.STOPPED
    # And once cleared it is silent again until the next crossing.
    assert goal.apply_goal_stop(cfg, 0.02, NOW) == (None, None)


def test_goal_stop_uses_current_goal_value():
    from tokentracker import goal
    cfg = make_cfg()
    goal.write_goal(cfg, 0.50)
    stop, _line = goal.apply_goal_stop(cfg, 0.55, NOW)
    assert stop is not None and abs(stop["goal"] - 0.50) < 1e-9
    goal.clear_stop(cfg)
    goal.write_goal(cfg, 0.95)
    assert goal.apply_goal_stop(cfg, 0.55, NOW) == (None, None)


def test_goal_stop_ignores_an_unusable_weekly_reading():
    # utilization comes from remote JSON: null and NaN both reach here (models
    # float()s it, usage._coerce_utilization lets NaN through). Neither may kill
    # the loop, and neither may clear a standing stop - clearing one resumes the
    # main session as if the week had rolled over.
    from tokentracker import control, goal
    cfg = make_cfg()
    goal.write_goal(cfg, 0.90)
    for junk in (None, float("nan"), "abc", [1], float("inf")):
        assert goal.apply_goal_stop(cfg, junk, NOW) == (None, None), junk
        assert not cfg.stop_file.exists(), junk
        assert control.read_control(cfg) == control.RUNNING, junk

    stop, _line = goal.apply_goal_stop(cfg, 0.95, NOW)
    assert stop is not None and cfg.stop_file.exists()
    for junk in (None, float("nan"), "abc"):
        assert goal.apply_goal_stop(cfg, junk, NOW) == (stop, None), junk
        assert json.loads(cfg.stop_file.read_text(encoding="utf-8")) == stop, junk


def test_goal_stop_replaces_an_unreadable_record():
    # A truncated or hand-edited stop.json is still a JSON object, so it used to
    # read as "already stopped": the real record was never written and dispatch
    # was never halted for the rest of the week.
    from tokentracker import control, goal
    cfg = make_cfg()
    goal.write_goal(cfg, 0.90)
    for junk in ("{}", '{"reason": "weekly goal reached"}',
                 '{"reason": 1, "goal": 0.9, "weekly": 0.95, "at": "x"}',
                 '{"reason": "r", "goal": null, "weekly": 0.95, "at": "x"}',
                 '{"reason": "r", "goal": NaN, "weekly": 0.95, "at": "x"}',
                 '{"reason": "r", "goal": 0.9, "weekly": 0.95, "at": "x", "n": 1}'):
        cfg.stop_file.write_text(junk, encoding="utf-8")
        control.write_control(cfg, control.RUNNING)
        stop, line = goal.apply_goal_stop(cfg, 0.95, NOW)
        assert goal.valid_stop(stop), (junk, stop)
        assert list(stop) == ["reason", "goal", "weekly", "at"], (junk, stop)
        assert line and "weekly goal" in line, (junk, line)
        assert control.read_control(cfg) == control.STOPPED, junk
        assert json.loads(cfg.stop_file.read_text(encoding="utf-8")) == stop, junk
        goal.clear_stop(cfg)

    # Below the goal the same junk is discarded rather than left for the main
    # session to read as a stop point.
    cfg.stop_file.write_text("{}", encoding="utf-8")
    cleared, line = goal.apply_goal_stop(cfg, 0.10, NOW)
    assert cleared is None and line and "discarded" in line, line
    assert not cfg.stop_file.exists()
    # A record written by write_goal/apply_goal_stop is of course kept as is.
    stop, _line = goal.apply_goal_stop(cfg, 0.95, NOW)
    assert goal.valid_stop(stop) and goal.apply_goal_stop(cfg, 0.96, NOW) == (stop, None)


def test_tick_stops_dispatch_at_weekly_goal():
    # The wiring inside cli._tick: the goal check runs before anything launches,
    # gates that same tick, and the state names the stop reason.
    from tokentracker import cli, control, goal
    cfg = _gate_cfg()
    goal.write_goal(cfg, 0.90)
    d, launched = _gate_dispatcher(cfg)
    history = usage.UsageHistory(cfg)
    real_fetch = usage.fetch_usage
    usage.fetch_usage = lambda _cfg: snap(0.95, left_h=100.0)
    try:
        decision, actions, _exc, _snap, _rolled = cli._tick(cfg, d, history)
        assert decision.mode == "stopped", decision
        assert launched == [], launched
        assert control.read_control(cfg) == control.STOPPED
        stop = json.loads(cfg.stop_file.read_text(encoding="utf-8"))
        assert list(stop) == ["reason", "goal", "weekly", "at"], stop
        assert any("weekly goal" in a for a in actions), actions
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        assert state["decision"]["mode"] == "stopped", state
        assert state["decision"]["stop_reason"] == goal.STOP_REASON, state
        assert abs(state["weekly_goal"] - 0.90) < 1e-9, state
        assert state["goal_stop"] == stop, state

        # Second tick over the goal: no second log line, no rewrite.
        _decision, actions2, _e, _s, _r = cli._tick(cfg, d, history)
        assert not any("weekly goal" in a for a in actions2), actions2
        assert json.loads(cfg.stop_file.read_text(encoding="utf-8")) == stop

        # Weekly reset: the stop point clears, dispatch stays parked.
        usage.fetch_usage = lambda _cfg: snap(0.05, left_h=160.0)
        decision, actions3, _e, _s, _r = cli._tick(cfg, d, history)
        assert not cfg.stop_file.exists()
        assert any("cleared" in a for a in actions3), actions3
        assert control.read_control(cfg) == control.STOPPED
        assert decision.mode == "stopped", decision
        assert launched == [], launched
        state = json.loads(cfg.state_file.read_text(encoding="utf-8"))
        assert state["goal_stop"] is None, state

        # Only now, on an operator START, does work resume.
        control.write_control(cfg, control.RUNNING)
        cli._tick(cfg, d, history)
        assert launched == [("pod", "cloud")], launched
    finally:
        usage.fetch_usage = real_fetch


def test_status_prints_weekly_goal_line():
    from tokentracker import cli, goal
    cfg = make_cfg()
    goal.write_goal(cfg, 0.85)
    real_fetch = usage.fetch_usage
    usage.fetch_usage = lambda _cfg: snap(0.57)
    try:
        out = _capture(lambda: cli.cmd_status(cfg))
        assert "weekly goal: 85% (weekly now 57%)" in out.splitlines(), out
        goal.apply_goal_stop(cfg, 0.57, NOW)  # under goal: no stop line
        assert "STOPPED:" not in _capture(lambda: cli.cmd_status(cfg))
        goal.apply_goal_stop(cfg, 0.86, NOW)
        out = _capture(lambda: cli.cmd_status(cfg))
        assert f"STOPPED: {goal.STOP_REASON}" in out, out
    finally:
        usage.fetch_usage = real_fetch


def test_overlay_exposes_goal_controls():
    import inspect
    try:
        import tkinter  # noqa: F401  - absent on headless builds
        from tokentracker import overlay
    except ImportError as exc:
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    src = inspect.getsource(overlay.Overlay)
    for tag in ("goal_minus", "goal_plus"):
        assert f'tag_bind("{tag}"' in src, tag
    for name in ("_click_goal_minus", "_click_goal_plus", "_step_goal",
                 "_draw_goal_row", "_draw_goal_tick", "_draw_stop_band",
                 "_stop_text"):
        assert callable(getattr(overlay.Overlay, name)), name
    # Both handlers must swallow the click so the drag binding never sees it.
    for name in ("_click_goal_minus", "_click_goal_plus"):
        assert 'return "break"' in inspect.getsource(
            getattr(overlay.Overlay, "_step_goal")), name


def test_overlay_goal_step_clamps_and_writes():
    try:
        from tokentracker import goal, overlay
    except ImportError as exc:
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    cfg = make_cfg()
    fake = overlay.Overlay.__new__(overlay.Overlay)
    fake.cfg = cfg
    fake._refresh = lambda: None
    fake._goal = goal.write_goal(cfg, 0.90)
    assert fake._step_goal(0.05) == "break"
    assert abs(goal.read_goal(cfg) - 0.95) < 1e-9
    fake._step_goal(0.05)
    fake._step_goal(0.05)          # clamped at 100%
    assert abs(goal.read_goal(cfg) - 1.0) < 1e-9
    for _ in range(30):            # walk it down past the floor
        fake._step_goal(-0.05)
    assert abs(goal.read_goal(cfg) - goal.GOAL_MIN) < 1e-9
    # An off-grid goal snaps onto the 5-point grid instead of drifting.
    fake._goal = goal.write_goal(cfg, 0.87)
    fake._step_goal(-0.05)
    assert abs(goal.read_goal(cfg) - 0.80) < 1e-9
    # The taps are the only in-overlay repair for a broken goal, so they must
    # survive one: round(nan) raises, which would deaden the button for good.
    for broken in (float("nan"), None, float("inf")):
        fake._goal = broken
        assert fake._step_goal(0.05) == "break", broken
        assert abs(goal.read_goal(cfg) - 0.95) < 1e-9, broken


def test_overlay_exposes_control_and_close_buttons():
    import inspect
    try:
        import tkinter  # noqa: F401  - absent on headless builds
        from tokentracker import overlay
    except ImportError as exc:
        # Only a missing tkinter may skip this; a broken overlay import (say a
        # renamed name in control.py) must fail loudly instead of passing.
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    assert overlay.MODE_COLORS["stopped"] == overlay.RED
    src = inspect.getsource(overlay.Overlay)
    for tag in ("throttle_btn", "start_btn", "stop_btn", "min_btn", "close_btn"):
        assert f'tag_bind("{tag}"' in src, tag
    assert 'tags="close_btn"' in src
    assert 'tags=f"{kind}_btn"' in src
    for name in ("_click_start", "_click_stop", "_click_close",
                 "_draw_ctl_button", "_draw_close_button", "_draw_title_buttons"):
        assert callable(getattr(overlay.Overlay, name)), name


# Runs in its own interpreter: a bad close handler takes the whole process down
# with an access violation, which would otherwise swallow the suite's output.
_TK_PROBE = r'''
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
tmp = Path(sys.argv[2])

try:
    import tkinter as tk
    import tkinter.font as tkfont
except Exception as exc:
    print("SKIP tkinter:", exc)
    raise SystemExit(0)

from tokentracker import control, goal, graph, handover, overlay
from tokentracker.config import Config

cfg = Config(
    root=tmp, credentials_path=tmp / "creds.json", projects_dir=tmp / "projects",
    sessions_dir=tmp / "sessions", state_dir=tmp / "state", logs_dir=tmp / "logs",
    tasks_file=tmp / "tasks.json",
)
for d in (cfg.state_dir, cfg.logs_dir, cfg.projects_dir):
    d.mkdir(parents=True, exist_ok=True)
full = {"utilization": 1.0, "resets_at": None}
cfg.state_file.write_text(json.dumps({
    "at": overlay.utcnow().isoformat(),
    "usage": {"five_hour": dict(full), "seven_day": dict(full),
              "extra": {"fable_weekly": dict(full)}},
    "decision": {"mode": "pace"},
    "queue": {},
    "distribution": {"window_minutes": 60, "total_tokens": 0, "shares": []},
}), encoding="utf-8")
control.write_control(cfg, control.STOPPED)
goal.write_goal(cfg, 0.90)
goal.apply_goal_stop(cfg, 1.0, overlay.utcnow())
# 1 / 3 / 10 (surge 20): the counts the ladder chart is measured against below.
graph.write_graph(cfg, {
    graph.EXECUTIVE: {"model": "claude-opus-5", "count": 1},
    graph.ADVISORY: {"model": "claude-opus-5", "count": 3},
    graph.WORKERS: {"model": "claude-haiku-4-5-20251001", "count": 10,
                    "surge_count": 20},
})
handover.write_handover(cfg, task_id=handover.FORK_TASK_ID, mode="pace",
                        model="claude-opus-5", parent_session="329cb798",
                        started_at=overlay.utcnow().isoformat())

try:
    ov = overlay.Overlay(cfg)
except tk.TclError as exc:
    print("SKIP display:", exc)
    raise SystemExit(0)


def spans():
    out = []
    for item in ov.canvas.find_all():
        if ov.canvas.type(item) != "text":
            continue
        x0, _y0, x1, _y1 = ov.canvas.bbox(item)
        out.append((ov.canvas.itemcget(item, "text"), x0, x1))
    return out


def check_collapsed(tag, expect_mode=True):
    ov._collapsed = True
    ov._refresh()
    ov.root.update_idletasks()
    drawn = spans()
    labels = [t for t, _a, _b in drawn]
    if expect_mode:
        assert "STOPPED" in labels, (tag, drawn)
    # The stop band must say the weekly reading it exists to report, not an
    # ellipsis, and must not slide under the close / minimize buttons that are
    # painted over it.
    band = [t for t in labels if t.startswith("GOAL ")]
    assert band and "..." not in band[0], (tag, drawn)
    box = ov.canvas.bbox("stop_band")
    buttons = [ov.canvas.bbox(i) for t in ("close_btn", "min_btn")
               for i in ov.canvas.find_withtag(t)]
    assert box and buttons, (tag, box, buttons)
    assert box[2] <= min(b[0] for b in buttons), (tag, box, buttons)
    # Collapsed omits the ladder chart entirely rather than squeezing it into
    # a one-line bar: nothing it draws may be on the canvas at all.
    for absent in ("ladder", "ladder_spine", "graph_minus", "graph_plus",
                   "rung_executive", "rung_advisory", "rung_workers"):
        assert not ov.canvas.find_withtag(absent), (tag, absent)
    assert "AGENTIC GRAPH" not in labels, (tag, labels)
    for i, (ta, a0, a1) in enumerate(drawn):
        for tb, b0, b1 in drawn[i + 1:]:
            assert a1 <= b0 or b1 <= a0, (tag, ta, tb, a0, a1, b0, b1)


check_collapsed("native")

# Again at a 2x display scale, where the wide "Fable 100%" readout used to run
# straight into the mode label.
try:
    ov.root.tk.call("tk", "scaling",
                    2.0 * overlay.BASELINE_DPI / overlay.POINTS_PER_INCH)
    ov._font = tkfont.Font(family="Segoe UI", size=9)
    ov._font_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
    ov._font_small = tkfont.Font(family="Segoe UI", size=8)
    ov.s = 2.0
    ov.width = ov._px(cfg.overlay_width)
except tk.TclError as exc:
    print("SKIP scaling:", exc)
else:
    check_collapsed("2x")

# START pressed after a goal stop, with no loop state to read: no mode word is
# drawn, so nothing pushes the band left off the title buttons on its own.
state_body = cfg.state_file.read_text(encoding="utf-8")
cfg.state_file.unlink()
control.write_control(cfg, control.RUNNING)
check_collapsed("no-mode", expect_mode=False)
cfg.state_file.write_text(state_body, encoding="utf-8")
control.write_control(cfg, control.STOPPED)

# The close button must exit the way Escape does. Destroying synchronously from
# inside the canvas item binding frees the canvas mid-dispatch and faults tk.
ov._collapsed = False
ov._refresh()
ov.root.update()

# Expanded: the goal row taps and the red stop band must all be on the canvas,
# and no two texts may overlap now that two rows were added under the footer.
for tag in ("goal_minus", "goal_plus", "graph_minus", "graph_plus",
            "view_report", "report_now"):
    assert ov.canvas.find_withtag(tag), tag
texts = [t for t, _a, _b in spans()]
assert any(t.startswith("GOAL ") for t in texts), texts
assert any(t.startswith("STOPPED: weekly goal") for t in texts), texts
# The ladder chart and the report row are the two newest blocks; neither may
# overlap the rows it was wedged between, at any DPI.
assert "no report yet" in texts, texts
boxes = {t: ov.canvas.bbox(t) for t in
         ("graph_minus", "graph_plus", "view_report", "report_now")}
assert all(boxes.values()), boxes
assert boxes["graph_minus"][2] <= boxes["graph_plus"][0], boxes
assert boxes["view_report"][2] <= boxes["report_now"][0], boxes
goal_box = ov.canvas.bbox("goal_minus")
throttle_box = ov.canvas.bbox("throttle_btn")
assert boxes["graph_minus"][3] <= goal_box[1], (boxes, goal_box)
assert throttle_box[3] <= boxes["view_report"][1], (throttle_box, boxes)

# ------------------------------------------------- the AGENTIC GRAPH ladder
# Three rungs, executive / advisory / workers top to bottom, under the label.
assert "AGENTIC GRAPH" in texts, texts
for tier in ("EXECUTIVE", "ADVISORY", "WORKERS"):
    assert tier in texts, (tier, texts)
# The graph written above is 1 / 3 / 10 with a surge of 20.
for want in ("x1", "x3", "x10", "surge x20", "opus-5", "haiku-4-5"):
    assert want in texts, (want, texts)
rungs = [(t, ov.canvas.bbox(f"rung_{t}"))
         for t in ("executive", "advisory", "workers")]
assert all(b for _t, b in rungs), rungs
# Ordered top to bottom, and no two rungs share a pixel of height.
for (ta, a), (tb, b) in zip(rungs, rungs[1:]):
    assert a[3] <= b[1], (ta, tb, a, b)
# Width is the headcount: 10 >= 3 >= 1, and every rung is a visible bar.
widths = [b[2] - b[0] for _t, b in rungs]
assert widths[2] >= widths[1] >= widths[0] > 0, (rungs, widths)
assert widths[2] > widths[0], (rungs, widths)
# The surge ghost extends past the solid worker rung, and stays inside the card.
ghost = ov.canvas.bbox("ladder_ghost")
assert ghost and ghost[2] >= rungs[2][1][2], (ghost, rungs[2])
assert ghost[2] <= ov.width, (ghost, ov.width)
# The whole chart is wedged between the footer above and the goal row below.
chart = ov.canvas.bbox("ladder")
label_y = [ov.canvas.bbox(i)[1] for i in ov.canvas.find_all()
           if ov.canvas.type(i) == "text"
           and ov.canvas.itemcget(i, "text") == "AGENTIC GRAPH"]
assert chart and label_y, (chart, label_y)
assert label_y[0] >= chart[1] - 2, (label_y, chart)
assert chart[3] <= goal_box[1], (chart, goal_box)
spine = ov.canvas.bbox("ladder_spine")
assert spine[0] <= chart[0] + ov._px(4), (spine, chart)
# The spine runs the height of the rungs, which is what makes it read as one
# ladder rather than three loose bars.
assert spine[1] <= rungs[0][1][1] + 2 and spine[3] >= rungs[2][1][3] - 2, (
    spine, rungs)
# The xN column is tabular: one right edge shared down all three rungs, and
# the model ids share one left edge the same way.
count_right = sorted({round(x1) for t, _x0, x1 in spans()
                      if t in ("x1", "x3", "x10")})
assert len(count_right) == 1, count_right
model_left = sorted({round(a) for t, a, _b in spans()
                     if t in ("opus-5", "haiku-4-5")})
assert len(model_left) == 1, model_left
# No rung's model id may collide with its tier name or its count.
for tier_name, model in (("EXECUTIVE", "opus-5"), ("WORKERS", "haiku-4-5")):
    name_span = next(s for s in spans() if s[0] == tier_name)
    assert name_span[2] <= model_left[0], (name_span, model_left)
assert max(b for t, _a, b in spans() if t in ("opus-5", "haiku-4-5")) <= min(
    a for t, a, _b in spans() if t in ("x1", "x3", "x10")), spans()
# The handover chip shares the header row with the close / minimize buttons.
assert "FORK ACTIVE" in texts, texts
chip = ov.canvas.bbox("fork_chip")
title_btns = [ov.canvas.bbox(i) for t in ("close_btn", "min_btn")
              for i in ov.canvas.find_withtag(t)]
assert chip and title_btns, (chip, title_btns)
assert chip[2] <= min(b[0] for b in title_btns), (chip, title_btns)
lowest = max(ov.canvas.bbox(i)[3] for i in ov.canvas.find_all())
assert lowest <= int(ov.canvas["height"]) + 2, (lowest, ov.canvas["height"])

ids = ov.canvas.find_withtag("close_btn")
assert ids, "no close button drawn"
x0, y0, x1, y1 = ov.canvas.bbox(ids[0])
ov.canvas.event_generate("<ButtonPress-1>",
                         x=int((x0 + x1) // 2), y=int((y0 + y1) // 2))
try:
    ov.root.update()
    gone = not ov.root.winfo_exists()
except tk.TclError:
    gone = True
assert gone, "close button did not destroy the window"
print("OK")
'''


def test_overlay_close_button_and_collapsed_bar_render():
    import subprocess
    tmp = Path(tempfile.mkdtemp(prefix="tokdist_tk_"))
    probe = tmp / "probe.py"
    probe.write_text(_TK_PROBE, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(probe), str(ROOT), str(tmp)],
        capture_output=True, text=True, timeout=180)
    out = f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, out
    assert "OK" in proc.stdout or "SKIP" in proc.stdout, out


# --------------------------------------------------------------- the graph

def test_graph_derives_legacy_keys_from_config():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.throttle_model = "claude-opus-5"
    cfg.worker_model = "claude-sonnet-5"
    cfg.max_concurrency = 7
    cfg.surge_concurrency = 12
    # With no graph section the legacy keys imply one, and applying it back is
    # a no-op: an old config.json keeps behaving exactly as it did.
    g = G.default_graph(cfg)
    # Every fallback here is None: the stock ones (Fable 5 above the executive,
    # Opus 4.8 above a Sonnet worker) would all be promotions, and a config
    # that predates the field must never have one invented over its own models.
    assert g[G.EXECUTIVE] == {"model": "claude-opus-5", "fallback": None,
                              "count": 1}, g
    assert g[G.ADVISORY] == {"model": "claude-opus-5", "fallback": None,
                             "count": 3}, g
    assert g[G.WORKERS] == {"model": "claude-sonnet-5", "fallback": None,
                            "count": 7, "surge_count": 12}, g
    assert G.apply_graph(cfg) == g
    assert (cfg.max_concurrency, cfg.surge_concurrency) == (7, 12)
    assert cfg.worker_model == "claude-sonnet-5"

    # A graph in config.json is authoritative over all four legacy keys.
    cfg.graph = {"executive": {"model": "claude-opus-4-8", "count": 1},
                 "advisory": {"model": "claude-sonnet-5", "count": 5},
                 "workers": {"model": "claude-haiku-4-5-20251001",
                             "count": 20, "surge_count": 30}}
    G.apply_graph(cfg)
    assert cfg.throttle_model == "claude-opus-4-8", cfg.throttle_model
    assert cfg.worker_model == "claude-haiku-4-5-20251001", cfg.worker_model
    assert (cfg.max_concurrency, cfg.surge_concurrency) == (20, 30)
    # An empty worker model stays empty: plain workers keep launching with no
    # --model at all rather than being silently pinned to the executive's.
    cfg.graph = {}
    cfg.worker_model = ""
    assert G.default_graph(cfg)[G.WORKERS]["model"] == ""


def test_graph_override_file_wins_and_clamps():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.throttle_model = "claude-opus-5"
    cfg.worker_model = "claude-opus-5"
    cfg.max_concurrency = 4
    base, source = G.read_graph_source(cfg)
    assert source == G.SOURCE_CONFIG and base[G.WORKERS]["count"] == 4

    # A partial override (what the overlay's -/+ writes) merges over config.
    G.write_graph(cfg, {G.WORKERS: {"count": 20}})
    g, source = G.read_graph_source(cfg)
    assert source == G.SOURCE_OVERRIDE, source
    assert g[G.WORKERS]["count"] == 20 and g[G.EXECUTIVE]["model"] == "claude-opus-5"
    assert set(json.loads(cfg.graph_file.read_text(encoding="utf-8"))) == {
        "graph", "set_at"}
    G.apply_graph(cfg)
    assert cfg.max_concurrency == 20 and cfg.surge_concurrency >= 20

    # Counts are clamped, not trusted; junk falls back to the config value.
    for raw, want in ((0, G.COUNT_MIN), (-5, G.COUNT_MIN), (999, G.COUNT_MAX),
                      ("x", 20), (None, 20), (True, 20)):
        G.write_graph(cfg, {G.WORKERS: {"count": raw}})
        assert G.read_graph(cfg)[G.WORKERS]["count"] == want, raw
        G.write_graph(cfg, {G.WORKERS: {"count": 20}})
    # A malformed override is ignored rather than taking the loop down.
    # (apply_graph folded the 20 onto the Config above; put the config value
    # back so the fallback is visibly the config one, not the override's.)
    cfg.max_concurrency = 4
    cfg.surge_concurrency = 4
    for junk in ("{not json", "[]", "", "{}", '{"graph": 5}', '"hello"', "5",
                 "null", "true", '{"workers": null}', '{"workers": []}'):
        cfg.graph_file.write_text(junk, encoding="utf-8")
        g, source = G.read_graph_source(cfg)
        assert source == G.SOURCE_CONFIG and g[G.WORKERS]["count"] == 4, junk
        # Silently ignored by the read path, but not silent everywhere: the
        # CLI has a way to say the file on disk is doing nothing.
        assert G.override_warning(cfg), junk
    # A readable override switches the warning back off.
    G.write_graph(cfg, {G.WORKERS: {"count": 6}})
    assert G.override_warning(cfg) is None


def test_graph_override_is_a_patch_not_a_snapshot():
    """state/graph.json holds the fields that were set, and nothing else.

    Persisting the whole resolved graph instead meant one tap on the worker '+'
    copied the models and the advisory count into the override too, and every
    later edit to config.json's graph section was silently dead.
    """
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.throttle_model = "claude-opus-5"
    cfg.worker_model = "claude-opus-5"
    cfg.max_concurrency = 4
    cfg.graph = G.default_graph(cfg)
    G.write_graph(cfg, {G.WORKERS: {"count": 12}})
    stored = json.loads(cfg.graph_file.read_text(encoding="utf-8"))["graph"]
    assert stored == {G.WORKERS: {"count": 12}}, stored

    # config.json edited afterwards: everything the tap did not name follows it.
    cfg.graph = {G.EXECUTIVE: {"model": "claude-opus-4-8", "count": 1},
                 G.ADVISORY: {"model": "claude-sonnet-5", "count": 5},
                 G.WORKERS: {"model": "claude-haiku-4-5-20251001", "count": 6,
                             "surge_count": 8}}
    g = G.read_graph(cfg)
    assert g[G.EXECUTIVE]["model"] == "claude-opus-4-8", g
    assert g[G.ADVISORY] == {"model": "claude-sonnet-5", "fallback": None,
                             "count": 5}, g
    assert g[G.WORKERS]["model"] == "claude-haiku-4-5-20251001", g
    assert g[G.WORKERS]["count"] == 12, g       # the tap, and only the tap
    assert g[G.WORKERS]["surge_count"] == 12, g  # never under the lane count

    # A second patch adds its field and keeps the first.
    after = G.write_graph(cfg, {G.ADVISORY: {"count": 2}})
    stored = json.loads(cfg.graph_file.read_text(encoding="utf-8"))["graph"]
    assert stored == {G.ADVISORY: {"count": 2}, G.WORKERS: {"count": 12}}, stored
    assert after == G.read_graph(cfg), after
    assert after[G.EXECUTIVE]["model"] == "claude-opus-4-8", after
    assert after[G.ADVISORY]["model"] == "claude-sonnet-5", after
    # A patch that names nothing leaves the file exactly as it was.
    G.write_graph(cfg, {})
    assert json.loads(cfg.graph_file.read_text(encoding="utf-8"))["graph"] == stored


def test_graph_reads_never_raise_on_hostile_input():
    """Every read path degrades to the config value; none of them raises.

    read_graph_source runs once per poll inside the run loop, on every overlay
    refresh and from load_config, so an infinity in state/graph.json used to be
    a crash in all three: json.loads turns both `1e999` and the bare
    `Infinity` literal into float("inf"), and int(inf) raises OverflowError,
    which the old _int did not catch (it caught only TypeError/ValueError).
    """
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.throttle_model = "claude-opus-5"
    cfg.worker_model = "claude-opus-5"
    cfg.max_concurrency = 4
    cfg.surge_concurrency = 4
    # Junk *inside* a well-formed tier: the file is still an override (the
    # other tiers in it must keep applying), only the bad field falls back.
    for body in ('{"workers": {"count": 1e999}}',
                 '{"workers": {"count": Infinity}}',
                 '{"workers": {"count": -Infinity}}',
                 '{"workers": {"count": NaN}}',
                 '{"workers": {"surge_count": 1e999}}',
                 '{"workers": {"count": null}}',
                 '{"workers": {"count": "x"}}',
                 '{"workers": {"count": [1, 2]}}',
                 '{"workers": {"count": {"n": 1}}}',
                 '{"workers": {"model": null}}',
                 '{"workers": {"model": 5}}',
                 '{"workers": {}}',
                 '{"executive": {"count": Infinity}}',
                 '{"graph": {"workers": {"count": 1e999}}}'):
        cfg.graph_file.write_text(body, encoding="utf-8")
        g = G.read_graph(cfg)
        assert g[G.WORKERS]["count"] == 4, body
        assert g[G.WORKERS]["surge_count"] >= 4, body
        assert g[G.EXECUTIVE]["count"] == 1, body
        assert g[G.WORKERS]["model"] == "claude-opus-5", body
        # And the derivation onto the live Config, which is what the run loop
        # actually calls every tick.
        G.apply_graph(cfg)
        assert cfg.max_concurrency == 4, body
    cfg.graph_file.unlink()

    # A hostile config.json is the same story one level up: default_graph reads
    # the legacy scalars, and they are hand-edited far more often than the
    # override is.
    for attr, value in (("max_concurrency", float("inf")),
                        ("max_concurrency", float("nan")),
                        ("max_concurrency", None),
                        ("max_concurrency", "lots"),
                        ("surge_concurrency", float("inf")),
                        ("throttle_model", None),
                        ("worker_model", 5)):
        hostile = make_cfg()
        setattr(hostile, attr, value)
        block = G.default_graph(hostile)[G.WORKERS]
        assert G.COUNT_MIN <= block["count"] <= G.COUNT_MAX, (attr, value)
        assert block["surge_count"] >= block["count"], (attr, value)
        assert G.read_graph_source(hostile)[1] == G.SOURCE_CONFIG, (attr, value)

    # A hand-edited "graph" section in config.json, likewise.
    for section in ({"workers": {"count": float("inf")}}, {"workers": 5},
                    {"workers": {"count": float("nan")}}, [1, 2], "junk", None):
        hostile = make_cfg()
        hostile.graph = section
        graph, source = G.read_graph_source(hostile)
        assert source == G.SOURCE_CONFIG, section
        assert set(graph) == set(G.TIERS), section

    # And the display helpers, which the overlay's refresh timer and the fork's
    # prompt expansion call on whatever graph they were handed: a missing tier
    # is a filled-in default, not a KeyError.
    partial = {G.WORKERS: {"count": 1}}
    assert set(G.normalize({}, partial)) == set(G.TIERS)
    assert "executive" in G.graph_line(partial)
    assert G.overlay_label(partial).startswith("E ")
    assert len(G.format_tiers(partial)) == 3
    assert len(G.tiers_of(None)) == 3
    # validate_graph turns a broken graph into words rather than an exception.
    assert len(G.validate_graph({G.WORKERS: 5}, ["claude-opus-5"])) == 3
    _g, errors = G.set_assignments(partial, ["workers.count=9"])
    assert errors == [] and _g[G.EXECUTIVE]["count"] == 1


def test_graph_migration_writes_the_section_once():
    from tokentracker import graph as G
    from tokentracker.config import load_config
    tmp = Path(tempfile.mkdtemp(prefix="tokdist_graphmig_"))
    (tmp / "config.json").write_text(json.dumps({
        "worker_model": "claude-opus-5", "throttle_model": "claude-opus-4-8",
        "max_concurrency": 6, "surge_concurrency": 9,
    }), encoding="utf-8")
    cfg = load_config(tmp)
    raw = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    assert "graph" in raw, raw
    # The migration writes the fallback too, but only the one that is a step
    # down from the model already configured (Opus 4.8 under an Opus 5 lane).
    assert raw["graph"]["workers"] == {"model": "claude-opus-5",
                                       "fallback": "claude-opus-4-8",
                                       "count": 6, "surge_count": 9}, raw["graph"]
    assert raw["graph"]["executive"]["fallback"] is None, raw["graph"]
    # The legacy keys are kept for compatibility, not replaced.
    assert raw["worker_model"] == "claude-opus-5" and raw["max_concurrency"] == 6
    assert cfg.max_concurrency == 6 and cfg.throttle_model == "claude-opus-4-8"
    # Second load: the section already exists, so a hand edit survives.
    raw["graph"]["workers"]["count"] = 15
    (tmp / "config.json").write_text(json.dumps(raw), encoding="utf-8")
    cfg2 = load_config(tmp)
    assert cfg2.max_concurrency == 15, cfg2.max_concurrency
    assert G.read_graph(cfg2)[G.WORKERS]["count"] == 15


def test_graph_validation_warns_instead_of_crashing():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.local_model = "Qwen3.8-27B-NVFP4"
    models = G.known_models(cfg)
    assert "claude-opus-5" in models and "Qwen3.8-27B-NVFP4" in models
    good = G.normalize({G.WORKERS: {"model": "claude-opus-5"}},
                       G.default_graph(cfg))
    assert G.validate_graph(good, models) == []
    bad = G.normalize({G.WORKERS: {"model": "claude-mythos-9"}},
                      G.default_graph(cfg))
    warnings = G.validate_graph(bad, models)
    assert len(warnings) == 1 and "claude-mythos-9" in warnings[0], warnings
    # A warning, not a refusal: the id is still what the graph holds.
    assert bad[G.WORKERS]["model"] == "claude-mythos-9"


def test_graph_set_assignment_parsing():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.throttle_model = "claude-opus-5"
    base = G.default_graph(cfg)
    g, errors = G.set_assignments(
        base, ["workers.count=20", "advisory.model=claude-opus-4-8",
               "workers.surge_count=25"])
    assert errors == [], errors
    assert g[G.WORKERS]["count"] == 20 and g[G.WORKERS]["surge_count"] == 25
    assert g[G.ADVISORY]["model"] == "claude-opus-4-8"
    assert g[G.EXECUTIVE] == base[G.EXECUTIVE]
    for bad in ("workers", "workers.count", "nope.count=1", "workers.nope=1",
                "workers.count=lots"):
        _g, errors = G.set_assignments(base, [bad])
        assert len(errors) == 1, (bad, errors)


def test_cli_graph_shows_and_sets():
    from tokentracker import graph as G
    from tokentracker.cli import main as cli_main
    from tokentracker.config import load_config
    tmp = Path(tempfile.mkdtemp(prefix="tokdist_graphcli_"))
    (tmp / "config.json").write_text(json.dumps({
        "worker_model": "claude-opus-5", "throttle_model": "claude-opus-5",
        "max_concurrency": 10, "surge_concurrency": 20,
    }), encoding="utf-8")
    out = _capture(lambda: cli_main(["--root", str(tmp), "graph"]))
    assert f"agentic graph (source: {G.SOURCE_CONFIG})" in out, out
    for tier in G.TIERS:
        assert tier in out, (tier, out)
    assert "x10 (surge x20)" in out, out
    assert "Agentic graph (from TokenDistributor config)" in out, out

    out = _capture(lambda: cli_main(
        ["--root", str(tmp), "graph", "set", "workers.count=20",
         "workers.model=claude-opus-4-8"]))
    assert f"agentic graph (source: {G.SOURCE_OVERRIDE})" in out, out
    cfg = load_config(tmp)
    assert cfg.max_concurrency == 20, cfg.max_concurrency
    assert G.read_graph(cfg)[G.WORKERS]["model"] == "claude-opus-4-8"
    # config.json is never rewritten by a set; the override file carries it.
    raw = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    assert raw["graph"]["workers"]["count"] == 10, raw["graph"]
    # And it carries only what was assigned, so the models config.json declares
    # keep applying to every tier the operator did not name.
    stored = json.loads(cfg.graph_file.read_text(encoding="utf-8"))["graph"]
    assert stored == {G.WORKERS: {"model": "claude-opus-4-8", "count": 20}}, stored
    # An unparseable assignment is refused without touching the override.
    assert cli_main(["--root", str(tmp), "graph", "set", "workers.count=lots"]) == 1
    assert G.read_graph(load_config(tmp))[G.WORKERS]["count"] == 20

    # An assignment that would break the superiority rule is refused, with the
    # rule printed - and goes through when --force says it was deliberate.
    out = _capture(lambda: cli_main(
        ["--root", str(tmp), "graph", "set", "advisory.model=claude-haiku-4-5-20251001"]))
    assert "refusing to break the order" in out, out
    assert "executive >= advisory >= workers" in out, out
    assert G.read_graph(load_config(tmp))[G.ADVISORY]["model"] == "claude-opus-5"
    out = _capture(lambda: cli_main(
        ["--root", str(tmp), "graph", "set", "--force",
         "advisory.model=claude-haiku-4-5-20251001"]))
    graph = G.read_graph(load_config(tmp))
    assert graph[G.ADVISORY]["model"] == "claude-haiku-4-5-20251001", graph
    # Forced, but not silent: printing the graph still says it is upside down.
    assert "ranks below" in out, out
    # A standing violation must not lock the whole command: only what THIS
    # assignment breaks is refused, and a count breaks no order at all.
    out = _capture(lambda: cli_main(
        ["--root", str(tmp), "graph", "set", "workers.count=5"]))
    assert "refusing to break the order" not in out, out
    assert G.read_graph(load_config(tmp))[G.WORKERS]["count"] == 5, out
    # ... and it still says so after the write.
    assert "ranks below" in out, out
    # A NEW violation on top of the standing one is still refused.
    assert cli_main(["--root", str(tmp), "graph", "set",
                     "workers.model=claude-fable-5-1"]) == 1
    assert G.read_graph(load_config(tmp))[G.WORKERS]["model"] == "claude-opus-4-8"


def test_scheduler_reads_the_graph_worker_counts():
    from tokentracker import graph as G
    cfg = _gate_cfg()
    G.write_graph(cfg, {G.EXECUTIVE: {"model": "claude-opus-5"},
                        G.WORKERS: {"model": "claude-opus-5", "count": 8,
                                    "surge_count": 16}})
    G.apply_graph(cfg)
    # Endgame surge takes the graph's surge count, pacing the worker count.
    d = scheduler.decide(snap(0.7, left_h=6), rates(), idle(), QS, cfg,
                         CLASS_RATES, NOW)
    assert d.mode == "surge" and d.target_concurrency == 16, d
    d = scheduler.decide(snap(0.05, left_h=48), rates(), idle(), QS, cfg,
                         (0.05, 0.05), NOW)
    assert d.mode == "pace" and d.target_concurrency == 8, d
    # And the loop re-derives it every tick, so an overlay tap lands within one
    # poll rather than needing a restart.
    G.write_graph(cfg, {G.WORKERS: {"count": 2, "surge_count": 3}})
    from tokentracker import cli
    d2, _a, _e, _s, _r = cli._tick(cfg, _gate_dispatcher(cfg)[0],
                                   usage.UsageHistory(cfg), do_fetch=False)
    assert cfg.max_concurrency == 2 and cfg.surge_concurrency == 3, cfg


def test_repo_config_ships_the_graph():
    from tokentracker import graph as G
    from tokentracker.config import load_config
    cfg = load_config(ROOT)
    g = G.read_graph(cfg)
    assert g[G.EXECUTIVE]["model"] == "claude-fable-5-1", g  # user directive 2026-09-03
    assert g[G.ADVISORY]["count"] == 3 and g[G.WORKERS]["count"] == 10, g
    assert g[G.WORKERS]["surge_count"] == 20, g
    assert G.validate_graph(g, G.known_models(cfg)) == []
    # The legacy keys the scheduler and dispatcher read are the graph's.
    assert cfg.max_concurrency == 10 and cfg.surge_concurrency == 20
    assert cfg.worker_model == "claude-opus-5"
    # The directive's fallbacks ship with it, and none of them is a promotion.
    assert g[G.EXECUTIVE]["fallback"] == "claude-fable-5", g
    assert g[G.ADVISORY]["fallback"] == "claude-fable-5", g
    assert g[G.WORKERS]["fallback"] == "claude-opus-4-8", g
    assert G.order_warnings(g, cfg) == []
    for tier in G.TIERS:
        assert G.may_fall_back(g[tier]["model"], g[tier]["fallback"], cfg), tier
    # Every fallback is a model the report can price, not just one it can name.
    from tokentracker import pricing as P
    assert P.unpriced(P.read_pricing(cfg),
                      [g[t]["fallback"] for t in G.TIERS]) == []


# ------------------------------------------- model ranking and the fallbacks

def test_model_ranking_orders_the_tiers():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.local_model = "Qwen3.8-27B-NVFP4"
    ranked = [G.model_rank(m, cfg) for m in G.MODEL_RANK]
    assert ranked == sorted(ranked, reverse=True), ranked   # most capable first
    assert G.MODEL_RANK[0] == "claude-fable-5-1", G.MODEL_RANK
    assert G.model_rank("claude-fable-5-1", cfg) > G.model_rank("claude-fable-5", cfg)
    assert G.model_rank("claude-fable-5", cfg) > G.model_rank("claude-opus-5", cfg)
    assert G.model_rank("claude-opus-5", cfg) > G.model_rank("claude-opus-4-8", cfg)
    # The local engine ranks last of the known ids; anything unranked (and the
    # account default, which names no model at all) sits below even that.
    assert G.rank_order(cfg)[-1] == cfg.local_model
    assert (G.model_rank("claude-haiku-4-5-20251001", cfg)
            > G.model_rank(cfg.local_model, cfg) > G.RANK_UNKNOWN)
    assert G.model_rank("claude-mythos-9", cfg) == G.RANK_UNKNOWN
    assert G.model_rank("", cfg) == G.model_rank(None, cfg) == G.RANK_UNKNOWN
    # Never upward is the whole point of the ranking.
    assert G.may_fall_back("claude-fable-5-1", "claude-fable-5", cfg)
    assert G.may_fall_back("claude-opus-5", "claude-opus-5", cfg)
    assert not G.may_fall_back("claude-opus-5", "claude-fable-5-1", cfg)
    assert not G.may_fall_back("claude-opus-5", None, cfg)
    assert not G.may_fall_back("claude-mythos-9", "claude-opus-5", cfg)


def test_order_rule_warns_and_never_raises():
    from tokentracker import graph as G
    cfg = make_cfg()
    ok = {G.EXECUTIVE: {"model": "claude-fable-5-1", "fallback": "claude-fable-5",
                        "count": 1},
          G.ADVISORY: {"model": "claude-fable-5", "fallback": "claude-opus-5",
                       "count": 3},
          G.WORKERS: {"model": "claude-opus-5", "fallback": "claude-opus-4-8",
                      "count": 4, "surge_count": 8}}
    assert G.order_warnings(ok, cfg) == []
    assert G.validate_graph(ok, G.known_models(cfg), cfg) == []

    # Workers above the advisory tier: reported, never raised, and the graph
    # still holds what the operator wrote.
    upside_down = json.loads(json.dumps(ok))
    upside_down[G.WORKERS]["model"] = "claude-fable-5-1"
    warnings = G.order_warnings(upside_down, cfg)
    assert len(warnings) == 1 and "advisory" in warnings[0], warnings
    assert "executive >= advisory >= workers" in warnings[0], warnings
    assert warnings == [w for w in G.validate_graph(
        upside_down, G.known_models(cfg), cfg) if "ranks below" in w]

    # A fallback that outranks its own primary is a promotion, not a fallback.
    promoted = json.loads(json.dumps(ok))
    promoted[G.WORKERS]["fallback"] = "claude-fable-5-1"
    warnings = G.order_warnings(promoted, cfg)
    assert any("outranks its primary" in w for w in warnings), warnings
    # It breaks the degraded ladder as well, and both halves are said.
    assert len(warnings) == 2, warnings

    # The degraded ladder has to hold too: the executive's fallback may not
    # sit under the fallback the tier below drops to.
    crossed = json.loads(json.dumps(ok))
    crossed[G.EXECUTIVE]["fallback"] = "claude-opus-4-8"
    warnings = G.order_warnings(crossed, cfg)
    assert len(warnings) == 1 and "fallback" in warnings[0], warnings

    # An id nobody ranks is not accused of anything (it already draws the
    # known_models warning), and neither is a graph missing a tier.
    unknown = json.loads(json.dumps(ok))
    unknown[G.EXECUTIVE]["model"] = "claude-mythos-9"
    unknown[G.EXECUTIVE]["fallback"] = None
    assert G.order_warnings(unknown, cfg) == []
    for junk in (None, {}, [1], "graph", {G.WORKERS: 5},
                 {G.EXECUTIVE: {}, G.ADVISORY: {}, G.WORKERS: {}}):
        assert G.order_warnings(junk, cfg) == [], junk
    assert len(G.validate_graph({G.WORKERS: 5}, ["claude-opus-5"], cfg)) == 3


def test_graph_fallback_defaults_and_override():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.throttle_model = "claude-fable-5-1"
    cfg.worker_model = "claude-opus-5"
    g = G.read_graph(cfg)
    # Stock defaults, applied only where they are a step down.
    assert g[G.EXECUTIVE]["fallback"] == "claude-fable-5", g
    assert g[G.ADVISORY]["fallback"] == "claude-fable-5", g
    assert g[G.WORKERS]["fallback"] == "claude-opus-4-8", g

    # The override carries fallbacks like any other field, as a patch.
    G.write_graph(cfg, {G.WORKERS: {"fallback": "claude-sonnet-5"}})
    stored = json.loads(cfg.graph_file.read_text(encoding="utf-8"))["graph"]
    assert stored == {G.WORKERS: {"fallback": "claude-sonnet-5"}}, stored
    g = G.read_graph(cfg)
    assert g[G.WORKERS]["fallback"] == "claude-sonnet-5", g
    assert g[G.EXECUTIVE]["fallback"] == "claude-fable-5", g

    # An explicit null means "this tier has no fallback" and is kept as one,
    # rather than being re-filled from the defaults on the next read.
    for cleared in (None, "", "none", "null", "-"):
        G.write_graph(cfg, {G.WORKERS: {"fallback": cleared}})
        assert G.read_graph(cfg)[G.WORKERS]["fallback"] is None, cleared
    # config.json's own null is honoured the same way.
    cfg.graph_file.unlink()
    cfg.graph = {G.WORKERS: {"model": "claude-opus-5", "fallback": None}}
    assert G.read_graph(cfg)[G.WORKERS]["fallback"] is None
    # Junk degrades to "no fallback" rather than raising inside the poll.
    for junk in (5, [], {}, True):
        cfg.graph = {G.WORKERS: {"model": "claude-opus-5", "fallback": junk}}
        assert G.read_graph(cfg)[G.WORKERS]["fallback"] is None, junk
    assert G.parse_assignments(["executive.fallback=claude-fable-5"])[1] == []


def test_graph_line_carries_the_fallbacks():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.throttle_model = "claude-fable-5-1"
    cfg.worker_model = "claude-opus-5"
    cfg.max_concurrency = 10
    cfg.surge_concurrency = 20
    line = G.graph_line(G.read_graph(cfg))
    assert "executive claude-fable-5-1 (fallback claude-fable-5) x1" in line, line
    assert "advisory/reviewers claude-fable-5-1 (fallback claude-fable-5) x3" in line, line
    assert ("workers claude-opus-5 (fallback claude-opus-4-8) x10 (surge 20)"
            in line), line
    # The fork runs its own Workflow agents, so it is told what to do when one
    # of them dies on a limit - and told never to promote it.
    assert "529" in line and "fallback model" in line, line
    assert "never move an agent UP" in line, line
    # A tier with no fallback says nothing rather than "(fallback None)".
    bare = G.normalize({G.WORKERS: {"fallback": None}}, G.read_graph(cfg))
    assert "workers claude-opus-5 x10" in G.graph_line(bare), G.graph_line(bare)


def test_limited_record_expires_and_clears():
    from tokentracker import graph as G
    cfg = make_cfg()
    cfg.fallback_minutes = 30.0
    assert G.read_limited(cfg) is None and G.limited_model(cfg) is None
    record = G.write_limited(cfg, "claude-fable-5-1", "529", NOW)
    assert list(record) == list(G.LIMITED_FILE_KEYS), record
    assert G.limited_model(cfg, now=NOW) == "claude-fable-5-1"
    # Younger than fallback_minutes: the tier stays on its fallback.
    assert G.read_limited(cfg, now=NOW + timedelta(minutes=29)) is not None
    # Older: the primary is due another try, without anyone clearing the file.
    assert G.read_limited(cfg, now=NOW + timedelta(minutes=31)) is None
    # Clearing is per model: a worker finishing says nothing about Fable.
    assert not G.clear_limited(cfg, "claude-opus-5")
    assert G.limited_model(cfg, now=NOW) == "claude-fable-5-1"
    assert G.clear_limited(cfg, "claude-fable-5-1")
    assert G.read_limited(cfg, now=NOW) is None
    # Junk on disk is "no record", not an exception in the middle of a poll.
    for junk in ("{not json", "[]", "null", "{}", '{"model": ""}'):
        cfg.limited_file.write_text(junk, encoding="utf-8")
        assert G.read_limited(cfg, now=NOW) is None, junk
        assert G.limited_model(cfg) is None, junk


def test_limit_error_classification():
    # The strings a limited or overloaded model actually comes back with.
    for text in ('API Error: 529 {"type":"overloaded_error"}',
                 "Overloaded",
                 "429 rate limit exceeded",
                 "rate_limit_error",
                 "Claude usage limit reached",
                 "You've hit your session limit",
                 "hit your weekly limit"):
        assert dispatch.limited_reason(text), text
    # ... and the ordinary failures that must NOT cost a model its tier.
    for text in ("", None, "exit code 1", "Error: file not found",
                 "task 1529 failed", "5290 tokens", "limited edition"):
        assert dispatch.limited_reason(text) is None, text


def _limit_cfg() -> Config:
    cfg = make_cfg()
    cfg.main_session_ids = [MAIN_ID]
    cfg.throttle_model = "claude-fable-5-1"
    cfg.worker_model = "claude-opus-5"
    cfg.fallback_minutes = 30.0
    cfg.graph = {
        "executive": {"model": "claude-fable-5-1", "fallback": "claude-fable-5",
                      "count": 1},
        "advisory": {"model": "claude-fable-5-1", "fallback": "claude-fable-5",
                     "count": 3},
        "workers": {"model": "claude-opus-5", "fallback": "claude-opus-4-8",
                    "count": 4, "surge_count": 8},
    }
    return cfg


def _stub_launcher(captured: list):
    """Patch dispatch's subprocess/shutil; returns the restore callable."""
    real_sub, real_shutil = dispatch.subprocess, dispatch.shutil
    dispatch.subprocess = _fake_subprocess(captured)
    dispatch.shutil = types.SimpleNamespace(which=lambda _n: "C:/fake/claude.exe")

    def restore():
        dispatch.subprocess, dispatch.shutil = real_sub, real_shutil
    return restore


def _die(d, cfg, task_id: str, payload: dict, now, code: int = 1,
         decision=None):
    """Let the stubbed process for `task_id` exit with `payload` as its result.

    With `decision`, the exit is processed through a whole `apply()` tick,
    which is what a fallback needs: reap only requeues the row, and it is the
    same tick's launch batch that starts it - under that decision's budget.
    """
    proc = d._procs[task_id]
    proc.out.close()
    (cfg.logs_dir / f"{task_id}.out.json").write_text(
        json.dumps(payload), encoding="utf-8")
    proc.popen.returncode = code
    proc.popen.poll = lambda: code
    if decision is not None:
        return d.apply(decision, now)
    return d.reap(now)


PACE = Decision("pace", 3, True, "test")
STOP = Decision("stopped", 0, False, "operator STOP")


def test_dispatch_falls_back_once_on_a_limited_primary():
    from tokentracker import graph as G
    cfg = _limit_cfg()
    d = dispatch.Dispatcher(cfg)
    d.add(TaskSpec(id="pod", prompt="p", cwd=str(cfg.root)))
    task = d.get("pod")
    captured: list[list[str]] = []
    restore = _stub_launcher(captured)
    try:
        d.launch(task, NOW)
        assert task.model_used == "claude-opus-5", task
        argv = captured[0]
        assert argv[argv.index("--model") + 1] == "claude-opus-5", argv

        # The run dies on a 529: the model was the problem, not the task.
        actions = _die(d, cfg, "pod", {
            "is_error": True,
            "result": 'API Error: 529 {"type":"overloaded_error"}',
        }, NOW + timedelta(minutes=1), decision=PACE)
        record = G.read_limited(cfg)
        assert record["model"] == "claude-opus-5", record
        assert record["reason"] == "529" and record["since"], record
        # Relaunched once, on the workers tier's fallback, and the row says
        # which model actually ran.
        assert len(captured) == 2, captured
        argv = captured[1]
        assert argv[argv.index("--model") + 1] == "claude-opus-4-8", argv
        assert task.status == "running" and task.model_used == "claude-opus-4-8"
        assert task.fallback_from == "claude-opus-5", task
        assert any("requeued on workers fallback" in a for a in actions), actions
        # The row's own intent is untouched: only the launch was forced, so a
        # later requeue goes back to asking for the tier's primary.
        assert task.model is None and task.fallback_model == "claude-opus-4-8"

        # A second task launched inside fallback_minutes skips the primary.
        d.add(TaskSpec(id="pod2", prompt="p", cwd=str(cfg.root)))
        assert d._task_model(d.get("pod2"), "cloud") == "claude-opus-4-8"
        # ... and the fork does the same on the executive tier's fallback.
        fork = TaskSpec(id=dispatch.FORK_TASK_ID, prompt="p", cwd=str(cfg.root),
                        resume_session=MAIN_ID)
        assert d._task_model(fork, "cloud") == "claude-fable-5-1"   # not limited
        G.write_limited(cfg, "claude-fable-5-1", "usage limit")
        assert d._task_model(fork, "cloud") == "claude-fable-5"
        assert d._task_model(d.get("pod2"), "cloud") == "claude-opus-5"

        # Past the window the primary is tried again, without anyone clearing
        # the file: a fallback is a detour, not a demotion.
        G.write_limited(cfg, "claude-opus-5", "529",
                        utcnow() - timedelta(minutes=45))
        assert d._task_model(d.get("pod2"), "cloud") == "claude-opus-5"
        # ... and when that retry finishes, the mark is dropped outright.
        d.launch(d.get("pod2"), NOW)
        assert d.get("pod2").model_used == "claude-opus-5"
        _die(d, cfg, "pod2", {"is_error": False,
                              "usage": {"output_tokens": 1}},
             NOW + timedelta(minutes=2), code=0)
        assert d.get("pod2").status == "done", d.get("pod2")
        assert not cfg.limited_file.exists()
    finally:
        restore()


def test_fallback_is_never_a_promotion_and_never_hops_twice():
    from tokentracker import graph as G
    cfg = _limit_cfg()
    # A fallback that outranks the primary is refused: a worker whose model is
    # busy waits, it is never promoted onto the executive's model.
    cfg.graph["workers"]["fallback"] = "claude-fable-5-1"
    d = dispatch.Dispatcher(cfg)
    d.add(TaskSpec(id="pod", prompt="p", cwd=str(cfg.root)))
    captured: list[list[str]] = []
    restore = _stub_launcher(captured)
    try:
        d.launch(d.get("pod"), NOW)
        actions = _die(d, cfg, "pod", {"is_error": True, "result": "Overloaded"},
                       NOW + timedelta(minutes=1))
        assert len(captured) == 1, captured           # nothing relaunched
        assert d.get("pod").status == "failed"
        assert any("refusing to fall back UP" in a for a in actions), actions
        # The mark still stands, so the next launch is not aimed at it either -
        # but with no legal fallback it stays on the primary rather than up.
        assert G.limited_model(cfg) == "claude-opus-5"
        assert d._task_model(d.get("pod"), "cloud") == "claude-opus-5"

        # With a legal fallback the mark routes the next launch to it, and the
        # row's own intent is left asking for the primary.
        cfg.graph["workers"]["fallback"] = "claude-opus-4-8"
        d.set_status("pod", "pending")
        assert d.get("pod").fallback_from is None
        assert d.get("pod").fallback_model is None
        d.launch(d.get("pod"), NOW)
        assert d.get("pod").model_used == "claude-opus-4-8"  # mark still fresh
        assert d.get("pod").model is None, d.get("pod")
        # When the FALLBACK is the one that dies, the primary's record must
        # survive: it is the only thing holding the tier off the primary, and
        # replacing it with the fallback's id sent the next launch straight
        # back into the model the account refused a minute ago.
        before = len(captured)
        actions = _die(d, cfg, "pod", {"is_error": True, "result": "Overloaded"},
                       NOW + timedelta(minutes=2), decision=PACE)
        assert d.get("pod").status == "failed", d.get("pod")
        assert any("also limited" in a for a in actions), actions
        assert len(captured) == before, captured        # no second hop
        assert G.limited_model(cfg) == "claude-opus-5", G.read_limited(cfg)
        d.add(TaskSpec(id="pod3", prompt="p", cwd=str(cfg.root)))
        assert d._task_model(d.get("pod3"), "cloud") == "claude-opus-4-8"
        # A tier with no fallback at all says so and leaves the row failed.
        cfg.graph["workers"]["fallback"] = None
        d.set_status("pod", "pending")
        d.launch(d.get("pod"), NOW)
        actions = _die(d, cfg, "pod", {"is_error": True, "result": "Overloaded"},
                       NOW + timedelta(minutes=4))
        assert any("no workers fallback configured" in a for a in actions), actions
    finally:
        restore()


def test_fallback_requeue_obeys_the_launch_budget():
    """A limit exit must not spend budget the pacer just decided not to spend.

    The relaunch is a requeue, so it goes through the same `target_concurrency`
    / `allow_heavy` gate as every other launch: under operator STOP (both
    budgets zeroed) the row waits, and it starts on the next tick that allows
    a launch at all.
    """
    from tokentracker import graph as G
    cfg = _limit_cfg()
    d = dispatch.Dispatcher(cfg)
    d.add(TaskSpec(id="pod", prompt="p", cwd=str(cfg.root)))
    captured: list[list[str]] = []
    restore = _stub_launcher(captured)
    try:
        d.launch(d.get("pod"), NOW)
        actions = _die(d, cfg, "pod", {"is_error": True, "result": "Overloaded"},
                       NOW + timedelta(minutes=1), decision=STOP)
        # Marked and requeued, but nothing started: STOP is the last word.
        assert G.limited_model(cfg) == "claude-opus-5", actions
        assert d.get("pod").status == "pending", d.get("pod")
        assert len(captured) == 1, captured
        # The next tick that has room starts it, on the fallback it was
        # requeued for.
        d.apply(PACE, NOW + timedelta(minutes=2))
        assert len(captured) == 2, captured
        argv = captured[1]
        assert argv[argv.index("--model") + 1] == "claude-opus-4-8", argv
    finally:
        restore()


def test_fork_fallback_requeue_waits_for_the_rearm_gate():
    """The loop's fork gate covers the requeue, not just a fresh arm.

    `_fork_wanted` and the re-arm cooldown are evaluated before `apply()`, so
    without the gate a fork that died on a usage limit was started again in
    the same tick - in a mode that wants no fork, and inside the cooldown.
    """
    from tokentracker import cli
    cfg = _limit_cfg()
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d, NOW)
    captured: list[list[str]] = []
    restore = _stub_launcher(captured)
    try:
        d.current_mode = "pace"
        d.launch(d.get(cli.THROTTLE_TASK_ID), NOW)
        # The handover is not armed in this mode: the requeue stands, nothing
        # starts, and the next tick's stale-pending check kills the row.
        d.launch_gate = cli._fork_launch_gate(cfg, False, NOW + timedelta(minutes=1))
        _die(d, cfg, cli.THROTTLE_TASK_ID,
             {"is_error": True, "result": "You've hit your usage limit"},
             NOW + timedelta(minutes=1), decision=PACE)
        assert d.get(cli.THROTTLE_TASK_ID).status == "pending"
        assert len(captured) == 1, captured
        # Armed again, but the cooldown since that exit has not elapsed.
        d.launch_gate = cli._fork_launch_gate(cfg, True, NOW + timedelta(minutes=1))
        d.apply(PACE, NOW + timedelta(minutes=1))
        assert len(captured) == 1, captured
        # Past the cooldown it goes, on the executive tier's fallback.
        d.launch_gate = cli._fork_launch_gate(cfg, True, NOW + timedelta(minutes=9))
        d.apply(PACE, NOW + timedelta(minutes=9))
        assert len(captured) == 2, captured
        argv = captured[1]
        assert argv[argv.index("--model") + 1] == "claude-fable-5", argv
    finally:
        restore()


def test_fork_falls_back_on_the_executive_tier():
    from tokentracker import cli, graph as G, handover
    cfg = _limit_cfg()
    cfg.throttle_prompt = "director brief"
    d = dispatch.Dispatcher(cfg)
    cli._ensure_throttle_task(cfg, d, NOW)
    task = d.get(cli.THROTTLE_TASK_ID)
    captured: list[list[str]] = []
    restore = _stub_launcher(captured)
    try:
        d.current_mode = "pace"
        d.launch(task, NOW)
        assert handover.read_handover(cfg)["model"] == "claude-fable-5-1"
        actions = _die(d, cfg, cli.THROTTLE_TASK_ID, {
            "is_error": True, "result": "You've hit your usage limit"},
            NOW + timedelta(minutes=1), decision=PACE)
        assert G.limited_model(cfg) == "claude-fable-5-1", actions
        argv = captured[1]
        assert argv[argv.index("--model") + 1] == "claude-fable-5", argv
        assert argv[argv.index("--resume") + 1] == MAIN_ID, argv
        # The handover the monitor session reads names the model that RAN.
        record = handover.read_handover(cfg)
        assert record["status"] == "started" and record["model"] == "claude-fable-5"
        assert task.model_used == "claude-fable-5", task
    finally:
        restore()


def test_overlay_ladder_shows_fallbacks_and_limited_tags():
    import inspect
    try:
        import tkinter  # noqa: F401  - absent on headless builds
        from tokentracker import overlay
    except ImportError as exc:
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    ladder = inspect.getsource(overlay.Overlay._draw_graph_ladder)
    # The fallback rides in the rung's own text line, dimmed after the model.
    assert 'f"->{short_model(fallback)}"' in ladder, ladder
    assert "fill=DIM" in ladder and 'f"fallback_{tier}"' in ladder, ladder
    # The LIMITED tag takes its width out of the model column, so it can never
    # land on top of the model id or the count.
    assert '"LIMITED"' in ladder and 'f"limited_{tier}"' in ladder, ladder
    assert "right -= self._font_small.measure(tag)" in ladder, ladder
    assert "P = self._px" in ladder and "self._pxf(" in ladder, ladder
    # Both are read fresh every refresh, like the graph itself.
    refresh = inspect.getsource(overlay.Overlay._refresh)
    assert "limited_model(self.cfg)" in refresh, refresh
    # The ladder is no taller for either of them.
    height = inspect.getsource(overlay.Overlay._ladder_height)
    assert "3 * P(LADDER_RUNG_H)" in height, height


# -------------------------------------------------------------- the ledger

def _entry(mid, model, ts, tools, usage, uuid="u1"):
    """One assistant JSONL entry, in Claude Code's shape."""
    return json.dumps({
        "type": "assistant", "uuid": uuid, "timestamp": ts,
        "message": {
            "id": mid, "model": model, "usage": usage,
            "content": [{"type": "tool_use", "id": f"{mid}-{n}", "name": n}
                        for n in tools],
        },
    })


def _usage(out=0, inp=0, creation=0, read=0, creation_1h=0):
    # `creation` is the whole cache-creation figure and `creation_1h` the slice
    # of it written at the 1-hour duration, exactly as the API records them.
    return {"output_tokens": out, "input_tokens": inp,
            "cache_creation_input_tokens": creation,
            "cache_creation_1h_input_tokens": creation_1h,
            "cache_read_input_tokens": read}


def test_ledger_parser_dedupes_and_categorises():
    from tokentracker import ledger
    cfg = make_cfg()
    start = NOW - timedelta(hours=1)
    inside = (NOW - timedelta(minutes=10)).isoformat()
    outside = (NOW - timedelta(hours=5)).isoformat()
    u = _usage(out=100, inp=10, creation=20, read=1000)
    path = cfg.projects_dir / "t.jsonl"
    path.write_text("\n".join([
        # One logical turn split over two entries, each repeating the same
        # usage: the dedup by message.id is the whole point.
        _entry("m1", "claude-opus-5", inside, ["Edit"], u),
        _entry("m1", "claude-opus-5", inside, ["Bash"], u, uuid="u2"),
        _entry("m2", "claude-opus-5", inside, ["Read", "Grep"], _usage(out=5)),
        _entry("m3", "claude-opus-5", inside, [], _usage(out=7)),
        _entry("m4", "claude-opus-5", inside, ["Workflow"], _usage(out=9)),
        "not json at all",
        _entry("m5", "claude-opus-5", outside, ["Edit"], _usage(out=999)),
    ]), encoding="utf-8")

    tally = ledger.parse_transcript(path, start, NOW)
    row = tally.by_model["claude-opus-5"]
    assert row["messages"] == 4, row            # m5 is outside the window
    assert row["usage"]["output_tokens"] == 100 + 5 + 7 + 9, row
    # AUTHOR beats OPS inside one turn; usage counted once, not twice.
    assert row["cats"]["AUTHOR"]["messages"] == 1, row["cats"]
    assert row["cats"]["AUTHOR"]["output_tokens"] == 100, row["cats"]
    assert row["cats"]["READ"]["messages"] == 1, row["cats"]
    assert row["cats"]["DECIDE"]["messages"] == 1, row["cats"]
    assert row["cats"]["DELEGATE"]["messages"] == 1, row["cats"]
    assert "OPS" not in row["cats"], row["cats"]
    # weighted = output + 0.1*(input + cache_creation) + 0.01*cache_read
    assert abs(row["cats"]["AUTHOR"]["weighted"] - 113.0) < 1e-9, row["cats"]
    assert abs(row["weighted"] - (113.0 + 5 + 7 + 9)) < 1e-9, row
    assert ledger.categorise([]) == "DECIDE"
    assert ledger.categorise(["AskUserQuestion"]) == "DECIDE"
    assert ledger.categorise(["Bash", "Read"]) == "OPS"
    assert ledger.categorise(["SomeMcpTool"]) == "READ"
    hours = tally.hourly["claude-opus-5"]
    assert sum(h["messages"] for h in hours.values()) == 4, hours


def test_ledger_tiers_split_by_the_graph():
    from tokentracker import graph as G
    from tokentracker import ledger
    cfg = make_cfg()
    graph = G.normalize({G.EXECUTIVE: {"model": "claude-opus-5"},
                         G.ADVISORY: {"model": "claude-opus-4-8"},
                         G.WORKERS: {"model": "claude-sonnet-5"}},
                        G.default_graph(cfg))
    assert ledger.tier_of("claude-opus-5", graph) == ledger.EXEC_TIER
    assert ledger.tier_of("claude-opus-4-8", graph) == ledger.EXEC_TIER
    assert ledger.tier_of("claude-sonnet-5", graph) == ledger.WORK_TIER
    # A model the graph never names still has to land somewhere, or the tier
    # totals would not add up to the per-model totals.
    assert ledger.tier_of("claude-fable-5-1", graph) == ledger.WORK_TIER
    # One model at every tier is counted once, at the executive tier.
    flat = G.normalize({G.WORKERS: {"model": "claude-opus-5"}},
                       G.default_graph(cfg))
    flat[G.EXECUTIVE]["model"] = "claude-opus-5"
    assert ledger.tier_of("claude-opus-5", flat) == ledger.EXEC_TIER
    # Which of the two readings applies is a property of the graph, not of the
    # turn: two models named, the id decides; one model named, it cannot.
    assert ledger.graph_separates_tiers(graph)
    assert not ledger.graph_separates_tiers(flat)


def test_ledger_tiers_by_role_when_one_model_serves_every_tier():
    """The shipped graph names claude-opus-5 three times; the id says nothing.

    Keying the split on the model id then puts every worker lane in the
    executive tier and drops the director into the worker tier, which inverts
    the verdict. The transcript's role is what separates them.
    """
    from tokentracker import graph as G
    from tokentracker import ledger
    cfg = make_cfg()
    flat = G.normalize({G.EXECUTIVE: {"model": "claude-opus-5"},
                        G.ADVISORY: {"model": "claude-opus-5"},
                        G.WORKERS: {"model": "claude-opus-5"}},
                       G.default_graph(cfg))
    assert not ledger.graph_separates_tiers(flat)
    for source in (ledger.MAIN_SOURCE, ledger.FORK_SOURCE):
        assert ledger.tier_of("claude-opus-5", flat, source) == ledger.EXEC_TIER
    assert ledger.tier_of("claude-opus-5", flat,
                          ledger.AGENT_SOURCE) == ledger.WORK_TIER
    # Whatever the director happens to be running is still the executive tier.
    assert ledger.tier_of("claude-fable-5-1", flat,
                          ledger.FORK_SOURCE) == ledger.EXEC_TIER
    # Name a second model and the id decides again, role or no role.
    split = G.normalize({G.WORKERS: {"model": "claude-sonnet-5"}}, flat)
    assert ledger.graph_separates_tiers(split)
    assert ledger.tier_of("claude-sonnet-5", split,
                          ledger.MAIN_SOURCE) == ledger.WORK_TIER
    assert ledger.tier_of("claude-opus-5", split,
                          ledger.AGENT_SOURCE) == ledger.EXEC_TIER


def test_ledger_verdict_is_not_inverted_by_a_single_model_graph():
    from tokentracker import graph as G
    from tokentracker import ledger
    cfg = _ledger_cfg(exec_tools=())      # main session: DECIDE turns only
    cfg.worker_model = "claude-opus-5"    # one model at every tier
    G.apply_graph(cfg)
    assert not ledger.graph_separates_tiers(G.read_graph(cfg))
    ts = (utcnow() - timedelta(minutes=4)).isoformat()
    agents = (cfg.projects_dir / "proj" / MAIN_ID / "subagents" / "workflows"
              / "wf1")
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "agent-1.jsonl").write_text("\n".join(
        _entry(f"a{i}", "claude-opus-5", ts, ["Edit"], _usage(out=500),
               uuid=f"ua{i}") for i in range(4)), encoding="utf-8")

    summary = ledger.build_summary(cfg, utcnow() - timedelta(hours=1), utcnow())
    # The four Workflow turns are the worker tier, not the executive one.
    assert summary["fable_vs_opus"]["opus_output"] == 2000, summary["fable_vs_opus"]
    assert summary["fable_vs_opus"]["opus_models"] == ["claude-opus-5"]
    assert summary["fable_work_breakdown"]["opus"]["AUTHOR"]["output"] == 2000
    # ... and the director's own session is the executive tier, on both models
    # it used, so the 60% rule is asked of the right turns.
    assert summary["fable_vs_opus"]["fable_models"] == ["claude-opus-5",
                                                        "claude-sonnet-5"]
    assert summary["fable_work_breakdown"]["fable"]["DECIDE"]["messages"] == 3
    assert summary["verdict"]["executive_only"] is True, summary["verdict"]
    assert "main + fork sessions on" in summary["verdict"]["one_paragraph"]
    assert any("role" in line for line in summary["verdict"]["evidence"]), summary
    # One id in both tiers is labelled as such rather than picking a side.
    assert summary["totals_by_model"]["claude-opus-5"]["tier"] == ledger.MIXED_TIER
    assert summary["totals_by_model"]["claude-sonnet-5"]["tier"] == ledger.EXEC_TIER


def test_ledger_dedupes_one_turn_across_transcripts():
    """A resumed fork opens with a verbatim copy of the parent's history.

    Same message.id, same timestamp, same usage, so a per-file dedup counts the
    parent's whole window once per fork - the same bug the per-file dedup
    exists to prevent, one level up.
    """
    from tokentracker import ledger
    cfg = make_cfg()
    start = NOW - timedelta(hours=1)
    ts = (NOW - timedelta(minutes=10)).isoformat()
    u = _usage(out=100, inp=10, creation=20, read=1000)
    parent = cfg.projects_dir / "parent.jsonl"
    fork = cfg.projects_dir / "fork.jsonl"
    parent.write_text(_entry("shared", "claude-opus-5", ts, ["Edit"], u),
                      encoding="utf-8")
    fork.write_text("\n".join([
        _entry("shared", "claude-opus-5", ts, ["Edit"], u),   # inherited copy
        _entry("own", "claude-opus-5", ts, ["Bash"], _usage(out=7), uuid="u9"),
    ]), encoding="utf-8")

    # On its own, each file sees every turn it holds.
    assert ledger.parse_transcript(fork, start, NOW).by_model[
        "claude-opus-5"]["messages"] == 2
    # Sharing one set, in the order build_summary folds them, the copy stays
    # with the parent and the fork keeps only what it produced.
    seen: set = set()
    first = ledger.parse_transcript(parent, start, NOW, seen)
    second = ledger.parse_transcript(fork, start, NOW, seen)
    assert first.by_model["claude-opus-5"]["messages"] == 1
    assert second.by_model["claude-opus-5"]["messages"] == 1
    assert second.by_model["claude-opus-5"]["usage"]["output_tokens"] == 7
    total = ledger.Tally()
    total.merge(first)
    total.merge(second)
    assert total.by_model["claude-opus-5"]["usage"]["output_tokens"] == 107
    assert abs(total.by_model["claude-opus-5"]["weighted"] - (113.0 + 7)) < 1e-9


def test_ledger_summary_counts_a_copied_parent_turn_once():
    from tokentracker import ledger
    cfg = _ledger_cfg(exec_tools=())
    proj = cfg.projects_dir / "proj"
    inherited = (proj / f"{MAIN_ID}.jsonl").read_text(encoding="utf-8")
    fork_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ts = (utcnow() - timedelta(minutes=2)).isoformat()
    (proj / f"{fork_id}.jsonl").write_text("\n".join([
        inherited,      # the whole parent history, exactly as the fork gets it
        _entry("fk", "claude-sonnet-5", ts, ["Edit"], _usage(out=42), uuid="uf"),
    ]), encoding="utf-8")
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "throttle-main-continue", "prompt": "p", "cwd": str(cfg.root),
         "status": "done", "fork_session_id": fork_id},
    ]}), encoding="utf-8")

    summary = ledger.build_summary(cfg, utcnow() - timedelta(hours=1), utcnow())
    totals = summary["totals_by_model"]
    assert totals["claude-opus-5"]["output_tokens"] == 300, totals
    assert totals["claude-opus-5"]["messages"] == 3, totals
    assert totals["claude-sonnet-5"]["output_tokens"] == 92, totals
    # The fork's row shows the one turn it actually produced, not the parent's.
    fork_rows = [s for s in summary["where_fable_went"]
                 if s["id_or_label"].startswith(fork_id[:8])]
    assert len(fork_rows) == 1 and fork_rows[0]["fable_output"] == 42, fork_rows


def _ledger_cfg(exec_tools=("Edit",)) -> Config:
    """A cfg with one main-session transcript the generator can actually read."""
    cfg = make_cfg()
    cfg.main_session_ids = [MAIN_ID]
    # No tracked repo: the milestone buckets shell out to git, and a report
    # test must not depend on what this machine's StarGTA checkout happens to
    # hold. bucket_milestones is exercised directly instead.
    cfg.report_repo = ""
    cfg.throttle_model = "claude-opus-5"
    cfg.worker_model = "claude-sonnet-5"
    cfg.throttle_prompt = "You are the forked acting technical director."
    ts = (utcnow() - timedelta(minutes=5)).isoformat()
    lines = [_entry(f"m{i}", "claude-opus-5", ts, list(exec_tools),
                    _usage(out=100, read=1000), uuid=f"u{i}")
             for i in range(3)]
    lines.append(_entry("w1", "claude-sonnet-5", ts, ["Bash"],
                        _usage(out=50), uuid="uw"))
    proj = cfg.projects_dir / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{MAIN_ID}.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return cfg


def test_ledger_verdict_uses_the_sixty_percent_rule():
    from tokentracker import graph as G
    from tokentracker import ledger
    hands = _ledger_cfg(exec_tools=("Edit",))
    G.apply_graph(hands)
    summary = ledger.build_summary(hands, utcnow() - timedelta(hours=1), utcnow())
    assert summary["verdict"]["executive_only"] is False, summary["verdict"]
    assert summary["fable_vs_opus"]["fable_models"] == ["claude-opus-5"]
    assert summary["fable_vs_opus"]["opus_models"] == ["claude-sonnet-5"]
    assert summary["fable_work_breakdown"]["fable"]["AUTHOR"]["output"] == 300
    assert summary["root_causes"], summary["root_causes"]

    # The same tier that only decides and delegates passes the same rule.
    exec_only = _ledger_cfg(exec_tools=())
    G.apply_graph(exec_only)
    summary = ledger.build_summary(exec_only, utcnow() - timedelta(hours=1),
                                   utcnow())
    assert summary["verdict"]["executive_only"] is True, summary["verdict"]
    assert summary["root_causes"] == [], summary["root_causes"]
    breakdown = summary["fable_work_breakdown"]["fable"]
    assert breakdown["DECIDE"]["share"] == 1.0, breakdown
    for cat in ledger.CATS:
        assert set(breakdown[cat]) >= {"output", "weighted", "share"}, breakdown


def test_ledger_writes_timestamped_page_and_latest_copy():
    from tokentracker import ledger
    cfg = _ledger_cfg()
    page = ledger.generate(cfg, "manual", hours=1.0)
    assert page.exists() and page.parent == cfg.reports_dir, page
    assert page.name.endswith("-ledger.html"), page.name
    stamp = page.name[:-len("-ledger.html")]
    assert len(stamp) == 16 and stamp[8] == "T" and stamp.endswith("Z"), stamp
    summary_path = cfg.reports_dir / f"{stamp}-summary.json"
    assert summary_path.exists(), list(cfg.reports_dir.iterdir())
    latest = cfg.reports_dir / ledger.LATEST_NAME
    assert latest.read_bytes() == page.read_bytes(), "latest.html is a copy"
    assert ledger.latest_report(cfg) == latest

    html = page.read_text(encoding="utf-8")
    assert ledger.TITLE_PLACEHOLDER not in html and "__DATA__" not in html
    day = json.loads(summary_path.read_text(encoding="utf-8"))["window"]["end"][:10]
    assert f"<title>Work Distribution {day}</title>" in html, html[:200]

    state = json.loads(cfg.report_file.read_text(encoding="utf-8"))
    assert set(state) == set(ledger.REPORT_KEYS), state
    assert state["last_report"] == str(page) and state["last_reason"] == "manual"
    assert state["window"]["hours"] == 1.0, state
    assert ledger.report_age(cfg).startswith("report "), ledger.report_age(cfg)

    # A second run lands on its own file and the window starts where the last
    # report ended, so consecutive reports do not double-count the same turns.
    later = utcnow() + timedelta(seconds=61)
    page2 = ledger.generate(cfg, "manual", now=later)
    assert page2 != page and page2.exists()
    window = json.loads(cfg.report_file.read_text(encoding="utf-8"))["window"]
    assert window["start"] == state["generated_at"], (window, state)


def test_ledger_cli_report_command():
    from tokentracker import ledger
    from tokentracker.cli import main as cli_main
    cfg = _ledger_cfg()
    out = _capture(lambda: cli_main(["--root", str(cfg.root), "report",
                                     "--hours", "2"]))
    # The CLI builds its own Config from --root, so only the files matter.
    assert "wrote " in out and "latest:" in out, out
    reports = cfg.root / "reports"
    assert (reports / "latest.html").exists(), list(reports.iterdir())
    assert cli_main(["--root", str(cfg.root), "report",
                     "--since", "not-a-time"]) == 1


def test_ledger_trigger_decisions_are_pure():
    from tokentracker import ledger
    # A fork that finished with something to show for it earns a report.
    assert ledger.milestone_wanted("done", True)
    # Done with nothing committed does not.
    assert not ledger.milestone_wanted("done", False)
    assert not ledger.milestone_wanted("failed", True)
    assert not ledger.milestone_wanted("killed", True)
    assert not ledger.milestone_wanted("running", True)
    assert not ledger.milestone_wanted("done", True, enabled=False)
    # A stop reports once: the same stop key never fires twice.
    assert ledger.stop_wanted("stop:12:00", None)
    assert not ledger.stop_wanted("stop:12:00", "stop:12:00")
    assert ledger.stop_wanted("stop:13:00", "stop:12:00")
    assert not ledger.stop_wanted(None, None)
    assert not ledger.stop_wanted("stop:13:00", None, enabled=False)


def test_ledger_repo_change_detection():
    from tokentracker import ledger
    cfg = make_cfg()
    repo = Path(tempfile.mkdtemp(prefix="tokdist_repo_"))
    cfg.report_repo = str(repo)
    since = utcnow()
    assert ledger.repo_changed_since(cfg, None) == (False, "no start time")
    assert not ledger.repo_changed_since(cfg, since)[0]
    progress = repo / "dev_JSON" / "PROGRESS_REPORT.json"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text("{}", encoding="utf-8")
    import os as _os
    stale = (since - timedelta(hours=1)).timestamp()
    _os.utime(progress, (stale, stale))
    assert not ledger.repo_changed_since(cfg, since)[0]
    fresh = (since + timedelta(minutes=1)).timestamp()
    _os.utime(progress, (fresh, fresh))
    changed, why = ledger.repo_changed_since(cfg, since)
    assert changed and "PROGRESS_REPORT" in why, why
    cfg.report_repo = ""
    assert ledger.repo_changed_since(cfg, since) == (False, "no repo configured")


def test_ledger_fires_on_fork_milestone_and_once_per_stop():
    from tokentracker import cli, control, ledger
    cfg = _ledger_cfg()
    repo = Path(tempfile.mkdtemp(prefix="tokdist_milestone_"))
    cfg.report_repo = str(repo)
    progress = repo / "dev_JSON" / "PROGRESS_REPORT.json"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text("{}", encoding="utf-8")
    d = dispatch.Dispatcher(cfg)
    d.add(TaskSpec(id=cli.THROTTLE_TASK_ID, prompt="p", cwd=str(cfg.root),
                   status="done",
                   started_at=(utcnow() - timedelta(hours=1)).isoformat()))
    calls: list[tuple] = []
    real = ledger.generate_async
    ledger.generate_async = lambda c, reason, **kw: (calls.append((reason, kw))
                                                     or True)
    try:
        # running -> done with a fresh PROGRESS_REPORT.json: report.
        line = ledger.maybe_report(cfg, d, before=("running", None),
                                   control=control.RUNNING, stop=None)
        assert line is not None and "milestone" in line, line
        assert len(calls) == 1 and "milestone" in calls[0][0], calls
        # Already done at the start of the tick: nothing new happened.
        calls.clear()
        assert ledger.maybe_report(cfg, d, before=("done", None),
                                   control=control.RUNNING, stop=None) is None
        assert calls == [], calls
        # Done, but the repo did not move: not a milestone.
        cfg.report_repo = str(repo / "nowhere")
        assert ledger.maybe_report(cfg, d, before=("running", None),
                                   control=control.RUNNING, stop=None) is None
        assert calls == [], calls
        # A stop reports once, keyed on the record that caused it.
        control.write_control(cfg, control.STOPPED)
        line = ledger.maybe_report(cfg, d, before=("done", None),
                                   control=control.STOPPED, stop=None)
        assert line is not None and "stopped" in line, line
        key = calls[0][1]["stop_key"]
        assert key and key.startswith("stop:"), calls
        ledger.write_report_state(cfg, path=Path("x"), reason="stopped",
                                  window={}, stop_key=key)
        calls.clear()
        assert ledger.maybe_report(cfg, d, before=("done", None),
                                   control=control.STOPPED, stop=None) is None
        assert calls == [], calls
        # The weekly-goal stop is its own episode and reports on its own; the
        # key carries both halves, so it cannot collide with the manual stop
        # that was just reported at the same control-file changed_at.
        stop = {"reason": "weekly goal reached", "goal": 0.9, "weekly": 0.95,
                "at": utcnow().isoformat()}
        line = ledger.maybe_report(cfg, d, before=("done", None),
                                   control=control.STOPPED, stop=stop)
        assert line is not None, line
        goal_key = calls[0][1]["stop_key"]
        assert goal_key.startswith("stop:") and "|goal:" in goal_key, calls
        assert goal_key != key, (goal_key, key)
    finally:
        ledger.generate_async = real


def test_ledger_stop_key_is_not_frozen_by_a_standing_goal_record():
    """A manual STOP after a goal stop is its own episode, and still reports.

    state/stop.json is deliberately not cleared when the operator presses START
    over the goal, so a key built from the goal record alone stayed identical
    for the rest of the week and every later STOP was swallowed as "already
    reported".
    """
    from tokentracker import control, ledger
    cfg = make_cfg()
    stop = {"reason": "weekly goal reached", "goal": 0.9, "weekly": 0.95,
            "at": utcnow().isoformat()}
    # The goal stop: it writes the record and the control file together.
    control.write_control(cfg, control.STOPPED)
    first = ledger.stop_key_for(cfg, control.STOPPED, stop)
    assert ledger.stop_wanted(first, None)
    ledger.write_report_state(cfg, path=Path("x"), reason="stopped",
                              window={}, stop_key=first)
    # START over the goal: the record still stands, but dispatch is running, so
    # there is no stop episode at all and nothing may fire mid-run.
    control.write_control(cfg, control.RUNNING)
    assert ledger.stop_key_for(cfg, control.RUNNING, stop) is None
    assert not ledger.stop_wanted(None, first)
    # STOP pressed again, record unchanged: a new episode, and it reports.
    time.sleep(0.01)
    control.write_control(cfg, control.STOPPED)
    second = ledger.stop_key_for(cfg, control.STOPPED, stop)
    assert second != first, (first, second)
    assert ledger.stop_wanted(second, first), (first, second)
    # Same episode polled again: silent.
    assert not ledger.stop_wanted(
        ledger.stop_key_for(cfg, control.STOPPED, stop), second)


def test_ledger_survives_a_broken_dispatcher():
    # maybe_report runs inside the poll; nothing it touches may raise.
    from tokentracker import control, ledger

    class Boom:
        def get(self, _task_id):
            raise RuntimeError("nope")

    cfg = make_cfg()
    assert ledger.maybe_report(cfg, Boom(), before=("running", None),
                               control=control.RUNNING, stop=None) is None
    assert ledger.read_report_state(cfg) == {}
    cfg.report_file.write_text("{not json", encoding="utf-8")
    assert ledger.read_report_state(cfg) == {}
    assert ledger.report_age(cfg) is None
    assert ledger.latest_report(cfg) is None


def test_ledger_finds_forks_by_recorded_id_and_by_prompt():
    from tokentracker import ledger
    cfg = _ledger_cfg()
    proj = cfg.projects_dir / "proj"
    ts = (utcnow() - timedelta(minutes=3)).isoformat()
    # A fork whose session id the dispatcher captured at exit.
    known = "11111111-2222-3333-4444-555555555555"
    (proj / f"{known}.jsonl").write_text(
        _entry("f1", "claude-sonnet-5", ts, ["Bash"], _usage(out=11)),
        encoding="utf-8")
    cfg.tasks_file.write_text(json.dumps({"tasks": [
        {"id": "throttle-main-continue", "prompt": "p", "cwd": str(cfg.root),
         "status": "done", "fork_session_id": known},
    ]}), encoding="utf-8")
    # A fork whose id was never recorded, found by the brief it was given.
    unknown = "99999999-8888-7777-6666-555555555555"
    (proj / f"{unknown}.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "timestamp": ts, "message": {
            "role": "user", "content": cfg.throttle_prompt + " Continue."}}),
        _entry("f2", "claude-sonnet-5", ts, ["Edit"], _usage(out=13)),
    ]), encoding="utf-8")

    found = ledger.discover_sessions(cfg, utcnow() - timedelta(hours=1), utcnow())
    roles = {s["sid"]: s["role"] for s in found}
    assert roles.get(MAIN_ID) == "main", roles
    assert roles.get(known) == "fork", roles
    assert roles.get(unknown) == "fork", roles
    summary = ledger.build_summary(cfg, utcnow() - timedelta(hours=1), utcnow())
    assert summary["totals_by_model"]["claude-sonnet-5"]["output_tokens"] == 74
    assert "fork" in summary["sources"]["fork_sessions"] or True
    labels = [s["id_or_label"] for s in summary["where_fable_went"]]
    assert any(known[:8] in label for label in labels), labels


def test_dispatch_records_the_fork_session_id():
    # Without it the ledger cannot tell which transcript on disk was the fork.
    cfg = _fork_cfg()
    _seed_tasks(cfg)
    d = dispatch.Dispatcher(cfg)
    task = d.get("a")
    task.status = "running"
    task.started_at = (utcnow() - timedelta(minutes=2)).isoformat()
    (cfg.logs_dir / "a.out.json").write_text(json.dumps({
        "is_error": False, "session_id": "sid-from-claude",
        "usage": {"output_tokens": 5}}), encoding="utf-8")
    d._finalize_record(task, 0, utcnow())
    assert task.fork_session_id == "sid-from-claude", task
    d.save()
    on_disk = json.loads(cfg.tasks_file.read_text(encoding="utf-8"))
    row = next(t for t in on_disk["tasks"] if t["id"] == "a")
    assert row["fork_session_id"] == "sid-from-claude", row


def test_ledger_utilization_series_from_history():
    from tokentracker import ledger
    cfg = make_cfg()
    _write_history(cfg, [(50, 0.40), (20, 0.50), (0, 0.60)])
    points = ledger.utilization_series(cfg, NOW - timedelta(hours=1), NOW)
    assert len(points) == 3, points
    assert abs(points[-1]["seven_day"] - 0.60) < 1e-9, points[-1]
    assert set(points[0]) == {"t", "five_hour", "seven_day", "fable"}, points[0]
    assert ledger.utilization_series(cfg, NOW + timedelta(hours=1),
                                     NOW + timedelta(hours=2)) == []


# ------------------------------------------------------------- the overlay

def test_overlay_exposes_report_and_graph_controls():
    import inspect
    try:
        import tkinter  # noqa: F401  - absent on headless builds
        from tokentracker import overlay
    except ImportError as exc:
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    src = inspect.getsource(overlay.Overlay)
    for tag in ("view_report", "report_now", "graph_minus", "graph_plus"):
        assert f'tag_bind("{tag}"' in src, tag
        assert f'tags="{tag}"' in src or f'tags=tag' in src, tag
    for name in ("_click_view_report", "_click_report_now", "_draw_report_row",
                 "_click_graph_minus", "_click_graph_plus", "_step_workers",
                 "_draw_graph_ladder", "_ladder_height"):
        assert callable(getattr(overlay.Overlay, name)), name
    # Every handler swallows the click, or the drag binding would see it.
    for name in ("_click_view_report", "_click_report_now", "_step_workers"):
        assert 'return "break"' in inspect.getsource(
            getattr(overlay.Overlay, name)), name
    # Scaled geometry only: raw design pixels break on a 2x display.
    for name in ("_draw_report_row", "_draw_graph_ladder", "_ladder_height"):
        body = inspect.getsource(getattr(overlay.Overlay, name))
        assert "P = self._px" in body, name
        assert "self._pxf(" in body or name == "_ladder_height", name
    refresh = inspect.getsource(overlay.Overlay._refresh)
    assert "_draw_report_row" in refresh and "_draw_graph_ladder" in refresh
    # The card has to grow by the chart, or the goal row lands on top of it.
    assert "_ladder_height()" in refresh, refresh
    assert "report_age" in refresh and "latest_report" in refresh
    # The chart replaced the textual GRAPH row rather than joining it.
    assert "_draw_graph_row" not in src and "overlay_label" not in src
    # ... and the collapsed bar omits it entirely rather than squeezing it in.
    collapsed = inspect.getsource(overlay.Overlay._refresh_collapsed)
    assert "_draw_graph_ladder" not in collapsed, collapsed


def test_overlay_worker_step_clamps_and_writes_the_override():
    try:
        from tokentracker import graph as G
        from tokentracker import overlay
    except ImportError as exc:
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    cfg = make_cfg()
    cfg.throttle_model = "claude-opus-5"
    cfg.max_concurrency = 10
    fake = overlay.Overlay.__new__(overlay.Overlay)
    fake.cfg = cfg
    fake._refresh = lambda: None
    fake._graph = G.read_graph(cfg)
    assert fake._step_workers(1) == "break"
    assert G.read_graph(cfg)[G.WORKERS]["count"] == 11
    for _ in range(50):
        fake._step_workers(1)
    assert G.read_graph(cfg)[G.WORKERS]["count"] == G.COUNT_MAX
    for _ in range(60):
        fake._step_workers(-1)
    assert G.read_graph(cfg)[G.WORKERS]["count"] == G.COUNT_MIN
    # The override is what changed; config.json is never touched by a tap, and
    # the tap writes one field so the rest of the graph still follows the file.
    assert cfg.graph_file.exists() and not (cfg.root / "config.json").exists()
    stored = json.loads(cfg.graph_file.read_text(encoding="utf-8"))["graph"]
    assert stored == {G.WORKERS: {"count": G.COUNT_MIN}}, stored
    assert G.overlay_label(G.read_graph(cfg)).startswith("E opus-5 x1 | A ")


def test_overlay_view_report_needs_a_report():
    try:
        from tokentracker import ledger, overlay
    except ImportError as exc:
        if exc.name not in ("tkinter", "_tkinter"):
            raise
        return
    cfg = make_cfg()
    fake = overlay.Overlay.__new__(overlay.Overlay)
    fake.cfg = cfg
    fake._refresh = lambda: None
    fake._report = None
    opened: list = []
    real = ledger.open_report
    overlay.open_report = lambda c: opened.append(c) or Path("x")
    try:
        # No page yet: the button is inert rather than handing the shell a
        # path that does not exist.
        assert fake._click_view_report(None) == "break"
        assert opened == [], opened
        fake._report = Path("some.html")
        fake._click_view_report(None)
        assert opened == [cfg], opened
    finally:
        overlay.open_report = real


# ------------------------------------------------------------- the pricing

PRICES = {
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write": 6.25,
                      "cache_write_1h": 10.0, "cache_read": 0.5,
                      "source": "https://example/pricing",
                      "checked": "2026-09-03"},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write": 12.5,
                         "cache_write_1h": 20.0, "cache_read": 0.25,
                         "source": "https://example/pricing",
                         "checked": "2026-09-03"},
}


def test_pricing_cost_usd_arithmetic():
    from tokentracker import ledger
    from tokentracker import pricing as P
    table = P.normalize(PRICES)
    price = P.price_for(table, "claude-opus-5")
    # One million of each, so every rate lands in the total exactly once. The
    # whole million of cache creation is a 5-minute write here.
    million = _usage(out=1_000_000, inp=1_000_000, creation=1_000_000,
                     read=1_000_000)
    assert abs(ledger.cost_usd(million, price) - (5 + 25 + 6.25 + 0.5)) < 1e-9
    parts = ledger.cost_components(million, price)
    assert parts == {"input": 5.0, "output": 25.0, "cache_write": 6.25,
                     "cache_write_1h": 0.0, "cache_read": 0.5}, parts
    # The realistic shape: cache reads dominate the token count and not the bill.
    real = _usage(out=2_000, inp=500, creation=10_000, read=400_000)
    want = (500 * 5 + 2_000 * 25 + 10_000 * 6.25 + 400_000 * 0.5) / 1e6
    assert abs(ledger.cost_usd(real, price) - want) < 1e-12, want

    # Cache creation is ONE counter over TWO prices: the 1-hour share bills at
    # 2x base input, the rest at 1.25x. Billing the whole figure at the
    # 5-minute rate is the understatement this split exists to prevent.
    mixed = _usage(out=2_000, inp=500, creation=10_000, read=400_000,
                   creation_1h=4_000)
    mixed_want = (500 * 5 + 2_000 * 25 + 6_000 * 6.25 + 4_000 * 10.0
                  + 400_000 * 0.5) / 1e6
    assert abs(ledger.cost_usd(mixed, price) - mixed_want) < 1e-12, mixed_want
    assert ledger.cost_usd(mixed, price) > ledger.cost_usd(real, price)
    assert ledger.billed_tokens(mixed) == {
        "input": 500, "output": 2_000, "cache_write": 6_000,
        "cache_write_1h": 4_000, "cache_read": 400_000}, mixed
    parts = ledger.cost_components(mixed, price)
    assert abs(parts["cache_write"] - 6_000 * 6.25 / 1e6) < 1e-12, parts
    assert abs(parts["cache_write_1h"] - 4_000 * 10.0 / 1e6) < 1e-12, parts
    assert abs(sum(parts.values()) - mixed_want) < 1e-12, parts
    # All of it at the 1-hour duration, and none of it double-billed: the two
    # write lines always partition cache_creation, never exceed it.
    allhour = _usage(creation=10_000, creation_1h=10_000)
    assert ledger.billed_tokens(allhour)["cache_write"] == 0
    assert abs(ledger.cost_usd(allhour, price) - 10_000 * 10.0 / 1e6) < 1e-12
    # A record whose sub-count contradicts its total is clamped, never negative.
    broken = _usage(creation=1_000, creation_1h=9_999)
    assert ledger.billed_tokens(broken) == {
        "input": 0, "output": 0, "cache_write": 0, "cache_write_1h": 1_000,
        "cache_read": 0}, broken
    # A usage dict from before the split still prices, as all-5-minute.
    legacy = {"output_tokens": 1_000, "input_tokens": 0,
              "cache_creation_input_tokens": 8_000, "cache_read_input_tokens": 0}
    assert abs(ledger.cost_usd(legacy, price)
               - (1_000 * 25 + 8_000 * 6.25) / 1e6) < 1e-12

    # The 1-hour share is lifted out of the nested block the API writes it in.
    folded = ledger.new_usage()
    ledger.add_usage(folded, {"input_tokens": 2, "output_tokens": 7,
                              "cache_creation_input_tokens": 10_828,
                              "cache_read_input_tokens": 35_935,
                              "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                 "ephemeral_1h_input_tokens": 10_828}})
    assert folded[ledger.CREATION_1H_KEY] == 10_828, folded
    assert folded[ledger.CREATION_KEY] == 10_828, folded
    # The nested block is not read twice when the flat key is already there.
    twice = ledger.new_usage()
    ledger.add_usage(twice, {"cache_creation_input_tokens": 100,
                             "cache_creation_1h_input_tokens": 40,
                             "cache_creation": {"ephemeral_1h_input_tokens": 40}})
    assert twice[ledger.CREATION_1H_KEY] == 40, twice
    # Junk in the nested block is ignored rather than raising inside a report.
    junk = ledger.new_usage()
    ledger.add_usage(junk, {"cache_creation": {"ephemeral_1h_input_tokens": "lots"}})
    ledger.add_usage(junk, {"cache_creation": "not a block"})
    assert junk[ledger.CREATION_1H_KEY] == 0, junk
    # Unpriced is None, never zero: zero would be a claim nobody made.
    assert ledger.cost_usd(million, P.price_for(table, "claude-sonnet-5")) is None
    assert ledger.cost_components(million, None) is None
    # A half-filled row prices nothing at all.
    half = P.normalize({"m": {"input": 5.0, "output": 25.0}})
    assert not P.is_priced(half["m"])
    assert ledger.cost_usd(million, half["m"]) is None
    # ... and a table that prices the model bills the tally through to the row.
    tally = ledger.Tally(table)
    tally.add_turn("claude-opus-5", "AUTHOR", real, NOW)
    tally.add_turn("claude-sonnet-5", "OPS", real, NOW)
    rows = tally.by_model
    assert abs(rows["claude-opus-5"]["cost_usd"] - want) < 1e-12, rows
    assert rows["claude-opus-5"]["unpriced"] is False
    assert rows["claude-sonnet-5"]["cost_usd"] is None, rows
    assert rows["claude-sonnet-5"]["unpriced"] is True
    assert abs(tally.usd() - want) < 1e-12, tally.usd()
    # The priced share is measured in weighted cost, so it says how much of the
    # window the dollar figure actually covers.
    priced, total = tally.priced_weighted()
    assert priced == total / 2, (priced, total)


def test_pricing_summary_cost_block():
    from tokentracker import graph as G
    from tokentracker import ledger
    cfg = _ledger_cfg(exec_tools=("Edit",))
    cfg.pricing = {"claude-opus-5": PRICES["claude-opus-5"]}  # sonnet unpriced
    G.apply_graph(cfg)
    summary = ledger.build_summary(cfg, utcnow() - timedelta(hours=1), utcnow())
    cost = summary["cost"]
    assert set(cost) >= {"total_usd", "priced_share", "by_model", "by_tier",
                         "by_source", "by_role", "by_category", "top_sinks",
                         "per_hour", "per_milestone", "pricing_used",
                         "unpriced_models", "by_model_components",
                         "cache_write_1h_tokens",
                         "cache_write_1h_share"}, sorted(cost)
    # 3 opus turns: 300 output + 3000 cache read.
    want = (300 * 25 + 3000 * 0.5) / 1e6
    assert abs(cost["total_usd"] - round(want, 4)) < 1e-9, cost["total_usd"]
    assert abs(cost["by_model"]["claude-opus-5"] - round(want, 4)) < 1e-9
    assert cost["by_model"]["claude-sonnet-5"] is None, cost["by_model"]
    assert cost["unpriced_models"] == ["claude-sonnet-5"], cost["unpriced_models"]
    assert cost["by_model_components"]["claude-sonnet-5"] is None
    assert abs(cost["by_model_components"]["claude-opus-5"]["output"]
               - 300 * 25 / 1e6) < 1e-9
    # Five components, so the two cache-write durations are separable.
    assert set(cost["by_model_components"]["claude-opus-5"]) == {
        "input", "output", "cache_write", "cache_write_1h",
        "cache_read"}, cost["by_model_components"]
    # This fixture writes no cache at all, so the 1-hour share is a real zero.
    assert cost["cache_write_1h_tokens"] == 0, cost
    assert cost["cache_write_1h_share"] == 0.0, cost
    assert summary["totals_by_model"]["claude-opus-5"]["cache_creation_1h_tokens"] == 0
    # weighted: 3 x (100 + 0.01*1000) priced, 50 unpriced.
    assert abs(cost["priced_share"] - round(330 / 380, 4)) < 1e-9, cost
    # Every dollar lands in exactly one tier, one source, one role, one category.
    assert abs(cost["by_tier"]["executive"] - cost["total_usd"]) < 1e-9, cost
    assert abs(cost["by_source"]["main_session"] - cost["total_usd"]) < 1e-9
    assert set(cost["by_role"]) == set(ledger.ROLES), cost["by_role"]
    assert abs(cost["by_role"]["director"] - cost["total_usd"]) < 1e-9, cost
    assert abs(cost["by_category"]["AUTHOR"] - cost["total_usd"]) < 1e-9, cost
    assert cost["per_hour"] and "usd_by_model" in cost["per_hour"][0], cost
    # The table the footer prints, with the source behind each number.
    used = cost["pricing_used"]
    assert used["claude-opus-5"]["source"] == "https://example/pricing"
    assert used["claude-opus-5"]["checked"] == "2026-09-03"
    assert used["claude-opus-5"]["unpriced"] is False
    assert used["claude-sonnet-5"]["unpriced"] is True, used
    assert used["claude-sonnet-5"]["input"] is None, used
    sink = cost["top_sinks"][0]
    assert set(sink) == {"source", "id_or_label", "what", "model", "usd",
                         "tokens_out", "cache_read"}, sink
    # The per-model totals carry the same bill, and say when there is none.
    totals = summary["totals_by_model"]
    assert abs(totals["claude-opus-5"]["cost_usd"] - round(want, 4)) < 1e-9
    assert totals["claude-sonnet-5"]["cost_usd"] is None
    assert totals["claude-sonnet-5"]["unpriced"] is True
    # An unpriced model is named in a caveat rather than billed at a guess.
    assert any("claude-sonnet-5" in c and "unpriced" in c
               for c in summary["caveats"]), summary["caveats"]
    # The existing keys still carry what the page already renders.
    assert summary["totals_by_model"]["claude-opus-5"]["weighted_cost"] == 330.0
    assert summary["where_fable_went"], summary["where_fable_went"]


def test_pricing_bills_one_hour_cache_writes_end_to_end():
    """A transcript that writes 1-hour cache is billed at 2x, not 1.25x.

    The regression this pins is silent by construction: the four-term formula
    produces a plausible number that is simply too small, and every figure
    derived from it (by model, tier, role, hour, commit) carries the same bias.
    So the test computes both formulas and demands the larger, correct one.
    """
    from tokentracker import graph as G
    from tokentracker import ledger
    cfg = _ledger_cfg(exec_tools=("Edit",))
    cfg.pricing = {"claude-opus-5": PRICES["claude-opus-5"]}
    G.apply_graph(cfg)
    ts = (utcnow() - timedelta(minutes=5)).isoformat()
    # The API's own shape: the flat total, plus the nested split beside it.
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 100_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 60_000,
                                "ephemeral_1h_input_tokens": 40_000}}
    proj = cfg.projects_dir / "proj"
    path = proj / f"{MAIN_ID}.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n"
                    + _entry("cache1", "claude-opus-5", ts, ["Read"], usage,
                             uuid="ucache"), encoding="utf-8")

    summary = ledger.build_summary(cfg, utcnow() - timedelta(hours=1), utcnow())
    cost = summary["cost"]
    row = summary["totals_by_model"]["claude-opus-5"]
    assert row["cache_creation_tokens"] == 100_000, row
    assert row["cache_creation_1h_tokens"] == 40_000, row
    assert cost["cache_write_1h_tokens"] == 40_000, cost
    assert cost["cache_write_1h_share"] == 0.4, cost

    base = (300 * 25 + 3_000 * 0.5) / 1e6           # the three existing turns
    five_term = base + (60_000 * 6.25 + 40_000 * 10.0) / 1e6
    four_term = base + (100_000 * 6.25) / 1e6       # what folding them gives
    assert abs(cost["total_usd"] - round(five_term, 4)) < 1e-9, cost["total_usd"]
    assert cost["total_usd"] > four_term, (cost["total_usd"], four_term)
    parts = cost["by_model_components"]["claude-opus-5"]
    assert abs(parts["cache_write"] - 60_000 * 6.25 / 1e6) < 1e-9, parts
    assert abs(parts["cache_write_1h"] - 40_000 * 10.0 / 1e6) < 1e-9, parts
    # Every derived cut carries the corrected figure, not just the headline.
    assert abs(cost["by_tier"]["executive"] - cost["total_usd"]) < 1e-9, cost
    assert abs(cost["by_role"]["director"] - cost["total_usd"]) < 1e-9, cost
    assert abs(sum(cost["by_category"].values()) - cost["total_usd"]) < 1e-9, cost
    hourly = sum(sum(h["usd_by_model"].values()) for h in cost["per_hour"])
    assert abs(hourly - cost["total_usd"]) < 1e-3, hourly
    # And the page says so rather than presenting the total as the whole story.
    assert any("1-hour" in c and "1.25" in c for c in summary["caveats"]), \
        summary["caveats"]


def test_pricing_milestone_bucketing():
    from tokentracker import ledger
    sep = ledger.GIT_SEP
    log = "\n".join([
        f"aaaaaaaa1111{sep}2026-09-03T10:00:00+00:00{sep}beta: pod pass",
        "",
        f"bbbbbbbb2222{sep}2026-09-03T12:00:00+00:00{sep}beta: crowd pass",
        "not a log line at all",
    ])
    commits = ledger.parse_git_log(log)
    assert [c["commit"] for c in commits] == ["aaaaaaaa1111", "bbbbbbbb2222"]
    assert commits[0]["subject"] == "beta: pod pass"

    start = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)

    def at(hour, minute):
        return datetime(2026, 9, 3, hour, minute, tzinfo=timezone.utc)

    events = [
        (at(8, 30), "claude-opus-5", 99.0),    # before the window start
        (at(9, 30), "claude-opus-5", 1.0),     # -> first commit
        (at(10, 30), "claude-opus-5", 2.0),    # -> second commit
        (at(11, 59), "claude-opus-5", 4.0),    # -> second commit
        (at(12, 30), "claude-opus-5", 8.0),    # after the last commit: no row
        (at(11, 0), "claude-opus-5", None),    # unpriced turns add nothing
    ]
    rows = ledger.bucket_milestones(commits, events, start)
    assert [r["commit"] for r in rows] == ["aaaaaaaa", "bbbbbbbb"], rows
    assert rows[0]["usd"] == 1.0 and rows[0]["minutes"] == 60.0, rows[0]
    assert rows[1]["usd"] == 6.0 and rows[1]["minutes"] == 120.0, rows[1]
    assert rows[1]["subject"] == "beta: crowd pass"
    # No commits, no invented milestone.
    assert ledger.bucket_milestones([], events, start) == []


def test_pricing_override_precedence():
    from tokentracker import pricing as P
    cfg = make_cfg()
    cfg.pricing = {"claude-opus-5": dict(PRICES["claude-opus-5"])}
    cfg.local_model = "Qwen3.8-27B-NVFP4"
    table, source = P.read_pricing_source(cfg)
    assert source == P.SOURCE_CONFIG
    assert table["claude-opus-5"]["output"] == 25.0
    # The local lane costs no API dollars, and is priced rather than unpriced.
    assert P.is_priced(table["Qwen3.8-27B-NVFP4"])
    assert table["Qwen3.8-27B-NVFP4"]["source"] == P.LOCAL_SOURCE
    assert table["Qwen3.8-27B-NVFP4"]["output"] == 0.0

    # The override wins, and is a patch: one field moves, the rest of the row
    # keeps following config.json.
    P.write_pricing(cfg, {"claude-opus-5": {"output": 30.0}})
    table, source = P.read_pricing_source(cfg)
    assert source == P.SOURCE_OVERRIDE
    assert table["claude-opus-5"]["output"] == 30.0
    assert table["claude-opus-5"]["input"] == 5.0, table["claude-opus-5"]
    assert table["claude-opus-5"]["source"] == "https://example/pricing"
    stored = json.loads(cfg.pricing_file.read_text(encoding="utf-8"))["pricing"]
    assert stored == {"claude-opus-5": {"output": 30.0}}, stored
    # A second set keeps the first, so two corrections do not overwrite.
    P.write_pricing(cfg, {"claude-sonnet-5": {"input": 2.0, "output": 10.0,
                                              "cache_write": 2.5,
                                              "cache_write_1h": 4.0,
                                              "cache_read": 0.2}})
    table = P.read_pricing(cfg)
    assert table["claude-opus-5"]["output"] == 30.0
    assert P.is_priced(table["claude-sonnet-5"]), table["claude-sonnet-5"]
    # Four of the five numbers is not a price: a row that cannot bill 1-hour
    # cache writes must not bill them at the 5-minute rate by omission.
    P.write_pricing(cfg, {"claude-haiku-4-5": {"input": 1.0, "output": 5.0,
                                               "cache_write": 1.25,
                                               "cache_read": 0.1}})
    assert not P.is_priced(P.read_pricing(cfg)["claude-haiku-4-5"])
    P.write_pricing(cfg, {"claude-haiku-4-5": {"cache_write_1h": 2.0}})
    assert P.is_priced(P.read_pricing(cfg)["claude-haiku-4-5"])
    # A correction to config.json still applies to every field not pinned.
    cfg.pricing["claude-opus-5"]["input"] = 6.0
    assert P.read_pricing(cfg)["claude-opus-5"]["input"] == 6.0

    # Junk never unprices a model, and never raises inside the poll.
    cfg.pricing_file.write_text("{not json", encoding="utf-8")
    table, source = P.read_pricing_source(cfg)
    assert source == P.SOURCE_CONFIG and table["claude-opus-5"]["output"] == 25.0
    assert P.override_warning(cfg) is not None
    cfg.pricing_file.write_text(json.dumps(
        {"pricing": {"claude-opus-5": {"output": "free"}}}), encoding="utf-8")
    assert P.read_pricing(cfg)["claude-opus-5"]["output"] == 25.0
    # An explicit null is the one way to say "this has no published price".
    cfg.pricing_file.write_text(json.dumps(
        {"pricing": {"claude-opus-5": {"output": None}}}), encoding="utf-8")
    assert not P.is_priced(P.read_pricing(cfg)["claude-opus-5"])
    assert P.unpriced(P.read_pricing(cfg), ["claude-opus-5"]) == ["claude-opus-5"]


def test_pricing_assignment_parsing_and_roles():
    from tokentracker import ledger
    from tokentracker import pricing as P
    patch, errors = P.parse_assignments([
        "claude-opus-5.output=25", "claude-opus-5.source=https://x/y",
        # The local model id has a dot in it, so the field is split off at the
        # LAST dot: this is a price for Qwen3.8-27B-NVFP4, not for "Qwen3".
        "Qwen3.8-27B-NVFP4.input=0", "claude-opus-5.cache_read=$0.50",
    ])
    assert errors == [], errors
    assert patch["claude-opus-5"] == {"output": 25.0, "source": "https://x/y",
                                      "cache_read": 0.5}, patch
    assert patch["Qwen3.8-27B-NVFP4"] == {"input": 0.0}, patch
    for bad in ("claude-opus-5.output", "outputis=5", "claude-opus-5.rate=5",
                "claude-opus-5.output=lots", "claude-opus-5.input=-3"):
        _patch, errs = P.parse_assignments([bad])
        assert errs, bad

    # Roles are read off the lane's own label, and only a declaration counts.
    agent = ledger.AGENT_SOURCE
    assert ledger.role_of("You are the implementer. Implement 1-5.", agent) == "worker"
    assert ledger.role_of('You are the adversarial reviewer, lens "tests-cli": '
                          "try to refute it.", agent) == "reviewer"
    assert ledger.role_of("You are the judge. Weigh the three lenses.",
                          agent) == "judge"
    assert ledger.role_of("Synthesis: merge the three review lenses.",
                          agent) == "synthesis"
    assert ledger.role_of("You are the fixer. Fix every finding.", agent) == "verify"
    assert ledger.role_of("You are the author of the README.", agent) == "author"
    assert ledger.role_of("reviewer", agent) == "reviewer"     # a bare label
    # A passing mention is not a declaration: this is the failure that filed
    # every worker lane under `verify`, because the standing brief says so.
    assert ledger.role_of(
        "You are the implementer. Verify results with Tools/verify.sh, and "
        "review the tests before you commit.", agent) == "worker"
    assert ledger.role_of("Read dev_JSON/BETA.md and build the pods.",
                          agent) == "worker"
    # Nothing declared: a session is the director, an agent is a worker.
    assert ledger.role_of("main session", ledger.MAIN_SOURCE) == "director"
    assert ledger.role_of("fork session", ledger.FORK_SOURCE) == "director"
    assert ledger.role_of("", agent) == "worker"
    assert set(ledger.ROLES) == {"worker", "reviewer", "judge", "synthesis",
                                 "author", "verify", "director"}


def test_cli_pricing_shows_and_sets():
    from tokentracker import pricing as P
    from tokentracker.cli import main as cli_main
    from tokentracker.config import load_config
    tmp = Path(tempfile.mkdtemp(prefix="tokdist_pricecli_"))
    (tmp / "config.json").write_text(json.dumps({
        "pricing": {"claude-opus-5": PRICES["claude-opus-5"]},
        "pricing_default": None,
        "local_model": "Qwen3.8-27B-NVFP4",
    }), encoding="utf-8")

    out = _capture(lambda: cli_main(["--root", str(tmp), "pricing"]))
    assert f"(source: {P.SOURCE_CONFIG})" in out, out
    assert "claude-opus-5" in out and "$25.00" in out, out
    assert "Qwen3.8-27B-NVFP4" in out and "local" in out, out
    # Both cache-write durations are columns, so the table shows what is billed.
    assert "wr 5m" in out and "wr 1h" in out, out
    assert "$6.25" in out and "$10.00" in out, out

    out = _capture(lambda: cli_main(
        ["--root", str(tmp), "pricing", "set", "claude-opus-5.cache_write_1h=11"]))
    assert "$11.00" in out, out
    assert P.read_pricing(load_config(tmp))["claude-opus-5"]["cache_write_1h"] == 11.0
    _capture(lambda: cli_main(
        ["--root", str(tmp), "pricing", "set", "claude-opus-5.cache_write_1h=10"]))

    out = _capture(lambda: cli_main(
        ["--root", str(tmp), "pricing", "set", "claude-opus-5.output=30"]))
    assert f"(source: {P.SOURCE_OVERRIDE})" in out, out
    assert "$30.00" in out, out
    cfg = load_config(tmp)
    assert P.read_pricing(cfg)["claude-opus-5"]["output"] == 30.0
    # config.json is never rewritten by a set.
    raw = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    assert raw["pricing"]["claude-opus-5"]["output"] == 25.0, raw["pricing"]

    # Unparseable assignments are refused without touching the override.
    assert cli_main(["--root", str(tmp), "pricing", "set",
                     "claude-opus-5.output=lots"]) == 1
    # An assignment without the `set` verb is refused by the parser itself.
    try:
        _capture(lambda: cli_main(["--root", str(tmp), "pricing",
                                   "claude-opus-5.output=1"]))
        raise AssertionError("accepted an assignment without 'set'")
    except SystemExit as exc:
        assert exc.code == 2, exc.code
    assert P.read_pricing(load_config(tmp))["claude-opus-5"]["output"] == 30.0
    # A model with no published price prints as unpriced rather than as $0.
    _capture(lambda: cli_main(["--root", str(tmp), "pricing", "set",
                               "claude-sonnet-5.input=2"]))
    out = _capture(lambda: cli_main(["--root", str(tmp), "pricing"]))
    assert P.UNPRICED in out, out


def test_pricing_page_renders_the_cost_section():
    from tokentracker import ledger
    cfg = _ledger_cfg()
    cfg.pricing = {"claude-opus-5": PRICES["claude-opus-5"]}
    page = ledger.generate(cfg, "manual", hours=1.0)
    html = page.read_text(encoding="utf-8")
    assert "__DATA__" not in html and ledger.TITLE_PLACEHOLDER not in html
    assert ">What it cost</h2>" in html, "the cost section title"
    for marker in ("cost-chart", "cost-tier-chart", "cost-role-chart",
                   "cost-hourly-chart", "cost-milestone-table", "pricing-used",
                   "--cost-input", "drawCost(",
                   # the fifth cost segment, its ink, and the five-term formula
                   "--cost-write-1h", "--cost-write-1h-ink", "Cache write 1h",
                   "cache_write_1h_price"):
        assert marker in html, marker
    # The 1-hour token counts reach the page, both cuts of them.
    assert "cache_creation_1h_tokens" in html and "cache_write_1h_share" in html
    # The data the section reads is spliced in, cost block and all.
    data = json.loads(
        (cfg.reports_dir / f"{page.name[:16]}-summary.json").read_text(
            encoding="utf-8"))
    assert data["cost"]["total_usd"] > 0, data["cost"]
    assert '"cost"' in html and "total_usd" in html


def test_repo_config_ships_the_priced_table():
    from tokentracker import pricing as P
    from tokentracker.config import load_config
    cfg = load_config(ROOT)
    # Read the checked-in table, not the resolved one: a local state/pricing.json
    # must not decide whether the repo ships prices.
    table = P.normalize(cfg.pricing)
    for model in ("claude-opus-5", "claude-fable-5-1", "claude-sonnet-5",
                  "claude-opus-4-8", "claude-haiku-4-5-20251001"):
        row = table.get(model)
        assert P.is_priced(row), (model, row)
        assert str(row["source"]).startswith("http"), (model, row)
        assert row["checked"], (model, row)
        assert row["cache_write_1h"] > row["cache_write"] > row["input"] > 0, \
            (model, row)
        assert row["output"] > row["input"] > row["cache_read"] > 0, (model, row)
        # The published multipliers, which is what makes these two prices and
        # not one: 1.25x base input for a 5-minute write, 2x for a 1-hour one.
        assert abs(row["cache_write"] - 1.25 * row["input"]) < 1e-9, (model, row)
        assert abs(row["cache_write_1h"] - 2.0 * row["input"]) < 1e-9, (model, row)
    # The published figures, as fetched (USD per 1M tokens).
    assert table["claude-opus-5"]["input"] == 5.0
    assert table["claude-opus-5"]["output"] == 25.0
    assert table["claude-opus-5"]["cache_write_1h"] == 10.0
    assert table["claude-fable-5-1"]["cache_read"] == 0.25
    assert table["claude-fable-5-1"]["cache_write_1h"] == 20.0
    assert table["claude-sonnet-5"]["output"] == 10.0
    assert table["claude-sonnet-5"]["cache_write_1h"] == 4.0
    assert table["claude-haiku-4-5-20251001"]["input"] == 1.0
    assert table["claude-haiku-4-5-20251001"]["cache_write_1h"] == 2.0
    # The local lane is free, and says so.
    assert table[cfg.local_model]["source"] == P.LOCAL_SOURCE
    assert table[cfg.local_model]["output"] == 0.0
    # Nothing is billed at a stand-in rate for a model nobody priced.
    assert cfg.pricing_default is None
    assert P.default_row(cfg) is None
    # Every model the graph can name is priced, so a report cannot come out
    # with the whole executive tier unpriced.
    from tokentracker import graph as G
    assert P.unpriced(P.read_pricing(cfg), G.known_models(cfg)) == []


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
