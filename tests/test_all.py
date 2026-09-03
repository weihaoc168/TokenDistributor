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

from tokentracker import control, goal, overlay
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
for tag in ("goal_minus", "goal_plus"):
    assert ov.canvas.find_withtag(tag), tag
texts = [t for t, _a, _b in spans()]
assert any(t.startswith("GOAL ") for t in texts), texts
assert any(t.startswith("STOPPED: weekly goal") for t in texts), texts
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
