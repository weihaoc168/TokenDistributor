<h1 align="center">⚙️ TokenDistributor</h1>

<p align="center"><b>Spend every token before the weekly reset — without ever starving a live session.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="python">
  <img src="https://img.shields.io/badge/platform-Windows-0078d6.svg" alt="platform">
  <img src="https://img.shields.io/badge/deps-stdlib%20only-success.svg" alt="deps">
  <img src="https://img.shields.io/badge/tests-24%2F24-brightgreen.svg" alt="tests">
</p>

---

Claude Code's weekly token budget expires whether you use it or not. TokenDistributor watches your live usage, senses when you are (and aren't) at the keyboard, and dispatches queued project work to headless `claude -p` sessions so the budget lands at ~100% right as the week resets — yielding instantly the moment you start typing.

## ✨ Highlights

- 🔄 **Live usage feed** — polls the same OAuth usage endpoint the Sundial widget reads: 5-hour window, weekly, and per-model (Fable) utilization, with reset-boundary roll-over so an expired window reads 0% the second it resets
- 🧠 **Five modes** — `pace` / `coast` / `yield` / `surge` / `blocked`, decided every tick from pacing math, learned per-task burn rates, and live activity detection
- 🙋 **Human first** — foreign session activity throttles background work; a 5-hour-window guard means the tracker can never lock you out mid-day
- 🏁 **Endgame surge** — in the final hours before reset, unburned budget is lost, so it maximizes instead
- 📌 **Main session aware** — one session is designated the project's own; its burn never counts against you
- 🎛️ **Desktop overlay** — Sundial-style card docked to the Sundial widget: ring gauges, a rotating gear mascot, live session cards (real titles, pinned main, scrollable), token distribution, and a FULL THROTTLE button
- 🧾 **Attribution** — parses local session transcripts to split burn between main / tracker / interactive, and calibrates task burn rates online
- 🖥️ **Local fallback lane** — when Claude is `blocked` (weekly exhausted or 5-hour guard), queued work is dispatched to a local FreeToken engine (Qwen on the 5090) instead of stalling: zero cloud tokens, auto-starts the engine via the ft daemon, GPU-guarded so it never fights the UE editor for VRAM

## 🚀 Quickstart

```powershell
git clone https://github.com/weihaoc168/TokenDistributor
cd TokenDistributor

python tracker.py status          # live bars, pacing, queue
python tracker.py run             # start the control loop (leave running)
python tracker.py overlay         # desktop panel docked under Sundial

# queue real work
python tracker.py add --id my-task --prompt "..." --cwd C:\path\to\project --weight heavy --priority 5
```

Requires Python 3.12+ (stdlib only) and a logged-in Claude Code install — the OAuth token in `~/.claude/.credentials.json` is read at runtime and sent only to `api.anthropic.com`.

## 🧭 How it decides

| Mode | When | Effect |
|---|---|---|
| `blocked` | weekly exhausted, or 5h window ≥ guard (80% active / 95% idle) | launch nothing on the cloud; dispatch to the **local lane** if enabled |
| `surge` | < 12 h to weekly reset with budget left | max concurrency, even during activity |
| `yield` | a foreign (interactive) session is active | launch nothing |
| `coast` | ahead of the pacing curve | launch nothing |
| `pace` | behind the curve | launch enough heavy/light tasks to land at ~100% by reset |

Throttling never kills running tasks — it only stops launching new ones. Failed usage fetches fall back to the last snapshot (≤ 30 min) with exponential backoff honoring `Retry-After`.

## 🖥️ Local fallback lane (FreeToken × Qwen)

When the decision is `blocked`, pending tasks are re-dispatched as headless `claude -p` sessions pointed at the local FreeToken engine (`ANTHROPIC_BASE_URL` swap, all model aliases pinned to `local_model`, 20-min API timeout — the same recipe as `claude-local.cmd`). Rules:

- Only when **blocked**, and only while the user is idle (`local_when_active` overrides)
- If the engine on `local_base_url` is down, it is auto-started through `ft daemon start` — but **never** while a process in `local_gpu_guard_procs` (UE editor, games) is running: the engine pins ~31.5GB of the 32GB card
- `local_prompt_preamble` is prepended to every local dispatch — the technical director's standing instructions telling the Qwen agent what work is safe (CPU-only; no editor/PIE/visual verification, defer those to the cloud tier via DEFERRED.md)
- Tasks forked from the main session (full throttle) never run locally; local runs never feed the cloud burn-rate calibration; timeouts get `local_minutes_multiplier` headroom for slower decode

## 🔥 Full throttle

> **Warning:** the big amber button at the bottom of the overlay overrides *all* pacing (including the 5-hour guard) and forks your main session's full context into a headless run (`claude -p --resume <main> --fork-session`) told to exhaust the remaining weekly budget on the project's highest-value work. Your interactive session is untouched; tap again to stop. The only remaining hard stop is the weekly limit itself.

## ⚙️ Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `main_session_ids` | `[]` | session UUIDs whose burn counts as project burn |
| `poll_seconds` | `300` | usage poll cadence (backoff base 60 s on HTTP 429) |
| `reserve_week_frac` | `0.15` | budget share reserved for interactive use, shrinking to 0 by endgame |
| `endgame_hours` | `12` | surge window before the weekly reset |
| `five_hour_guard_active` / `_idle` | `0.80` / `0.95` | 5-hour-window ceilings for background launches |
| `max_concurrency` / `surge_concurrency` | `3` / `4` | parallel headless sessions |
| `permission_mode` | `acceptEdits` | `bypass` maps to `--dangerously-skip-permissions` — opt-in only |
| `local_enabled` | `false` | turn on the local FreeToken lane |
| `local_base_url` / `local_daemon_url` | `:1919` / `:1900` | engine + supervisor endpoints |
| `local_model` / `local_model_path` | — | served model name / path for `ft daemon start` |
| `local_ft_bin` | — | path to `ft.exe` (engine venv) |
| `local_max_concurrency` | `1` | one 5090 = one session |
| `local_when_active` | `false` | let local tasks run even while the user is at the keyboard |
| `local_gpu_guard_procs` | `["UnrealEditor.exe"]` | never auto-start the engine while these run |
| `local_prompt_preamble` | `""` | standing instructions prepended to every local dispatch |
| `local_minutes_multiplier` | `3.0` | max_minutes headroom for slower local decode |

## 🩻 Under the hood

- **Usage**: `GET /api/oauth/usage` — window objects plus the `limits` array (where model-scoped weeklies like Fable live)
- **Activity**: transcript mtimes under `~/.claude/projects/`, excluding main-session and tracker-owned files
- **Session cards**: `~/.claude/sessions/*.json` registry (PID + start-time verified) + transcript `ai-title` records, falling back to the first-prompt excerpt
- **State**: everything observable lives in `state/` (`state.json`, `history.jsonl`, `calibration.json`, `last_payload.json`)

## 📋 Status

Built and verified live 2026-09-02: gauges match Sundial (including Fable via `weekly_scoped` limits), reset roll-over confirmed at a real 5-hour boundary, yield/blocked transitions observed in production, 24/24 offline tests green.

Known limitations: the burn-rate calibration needs completed tasks to learn from (priors until then); other devices on the account register as foreign burn (safe: it throttles); the first real full-throttle fork run is still untested; no service wrapper — use Task Scheduler for persistence.

<details>
<summary>📁 File index</summary>

| Path | Purpose |
|---|---|
| `tracker.py` | CLI entry point |
| `config.json` / `tasks.json` | tunables / task queue |
| `tokentracker/usage.py` | usage API client, attribution, calibration |
| `tokentracker/activity.py` | activity detection, project-dir munging |
| `tokentracker/scheduler.py` | pacing policy + window normalization |
| `tokentracker/dispatch.py` | queue + headless session lifecycle |
| `tokentracker/cli.py` | run / status / add / list / requeue / cancel / history / overlay |
| `tokentracker/overlay.py` | the desktop panel |
| `tests/test_all.py` | offline suite (24 tests) |

</details>

## 🙏 Acknowledgements

TokenDistributor is designed to sit (literally) beneath [**Sundial**](https://github.com/cams-nir/Sundial) by CAMS-NIR — the little desktop sun that first made Claude Code usage visible at a glance. The overlay docks to Sundial's widget position, mirrors its card aesthetic, and reads the same usage endpoint. If you just want to *see* your limits, use Sundial; TokenDistributor exists to *act* on them.

Unofficial project — not affiliated with Anthropic. The usage endpoint is undocumented and may change at any time.
