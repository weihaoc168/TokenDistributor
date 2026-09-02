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
