# TokenDistributor

Spend Claude Code's weekly token budget before it resets, without ever starving a live session.

<p align="center"><img src="docs/overlay.png" width="360" alt="TokenDistributor overlay"></p>

The overlay: three rings (5-hour, weekly with the gear mascot and an amber goal tick, Fable), live sessions with the main session pinned, token distribution and the current mode, an **AGENTIC GRAPH** ladder chart, a `- GOAL 90% +` stepper row, START / STOP, FULL THROTTLE, and VIEW REPORT / REPORT NOW.

## What it does

- **Paces usage across sessions.** Every poll it reads the OAuth usage endpoint (5-hour, weekly, and per-model Fable windows), measures how much budget is left against how much time is left, and launches just enough headless `claude -p` work to land near 100% by the weekly reset.
- **Runs five decision modes.** `pace` when behind the curve, `coast` when ahead, `yield` while a foreign interactive session is active, `surge` in the endgame hours before reset, `blocked` when the weekly limit or the 5-hour guard is hit. A running task is never killed by a mode change, only new launches are gated.
- **Treats the weekly goal as a stopping point.** When the weekly window reaches the goal it writes `state/stop.json` once (keys `reason`, `goal`, `weekly`, `at`), the record the main session reads as its stop point, and parks dispatch. Falling back under the goal only clears the file, so a fresh week never resumes on its own.
- **Gates dispatch with START / STOP.** The overlay buttons write `state/control.json`; STOP zeroes every launch budget so nothing new starts, while already-running work keeps going and is still reaped.
- **Surges on FULL THROTTLE.** The amber button writes `state/throttle.json`, overriding pacing to spend the remaining weekly budget on the project's highest-value work until the weekly limit itself stops it.
- **Falls back to a local lane.** While `blocked`, queued tasks are re-dispatched to a local FreeToken engine (Qwen on the 5090) that burns zero cloud budget. A GPU guard refuses to auto-start the engine while a listed process (the Unreal editor, a game) owns the card, since the engine pins about 31.5 GB of it.
- **Runs a configured agentic graph.** `config.json`'s `graph` names the model and headcount at each tier (executive, advisory, workers); the legacy `worker_model` / `max_concurrency` keys are derived from it, the fork brief is handed the same line through its `{graph}` placeholder, and `state/graph.json` (the overlay's `-` / `+`) overrides it without touching the config.
- **Reports where the work went.** It parses the session, fork and Workflow-agent transcripts for a window, splits every turn by tier (by model id, or by transcript role when the graph names one model at every tier) and by what it did (DECIDE / DELEGATE / READ / AUTHOR / OPS), and writes `reports/latest.html`: a page whose verdict says whether the executive tier stayed executive-only (the 60% hands-on rule). It runs itself when a fork finishes a milestone and when dispatch stops, and on demand from the CLI or the overlay's **VIEW REPORT**.
- **Draws that graph as a ladder chart.** The panel's **AGENTIC GRAPH** block is one rung per tier, executive over advisory over workers, each a bar as wide as its headcount and hung off a spine on the left, with the tier, the short model id and `xN` in aligned columns; the worker rung is the emphasis and carries its surge budget as a ghost extension, and its `-` / `+` still write `state/graph.json`.
- **Shows an always-on-top overlay.** A Tk card docked near the Sundial widget, refreshed every few seconds, with minimize (collapse to a bar) and close buttons and its own live session, distribution, graph, goal, control, and report rows.

## Setup

Prerequisites:

- Python 3.13 on Windows (the overlay and process control use Windows APIs). No third-party packages, the app is standard library only, so there is nothing to `pip install`.
- Claude Code CLI on `PATH` (`claude`), logged in. The OAuth token in `~/.claude/.credentials.json` is read at runtime and sent only to `api.anthropic.com`.

```powershell
git clone https://github.com/weihaoc168/TokenDistributor
cd TokenDistributor
```

The keys in `config.json` that matter most, with this machine's current values:

| Key | Current value | Meaning |
|---|---|---|
| `graph` | opus-5: E x1, A x3, W x10/20 | the agentic graph; `worker_model`, `throttle_model`, `max_concurrency` and `surge_concurrency` are derived from it (`state/graph.json` overrides) |
| `known_models` | five `claude-*` ids | allow-list for graph model ids; an unknown id warns, it never refuses |
| `report_repo` | `C:/Users/chenw/StarGTA` | repo watched for the fork-milestone report trigger |
| `weekly_goal` | `0.9` | weekly utilization the week stops at (`state/goal.json` overrides) |
| `local_enabled` | `true` | turn on the local FreeToken / Qwen lane |
| `local_model` | `Qwen3.8-27B-NVFP4` | served model name for local dispatch |
| `local_model_path` | `D:\FreeToken Desktop\Qwen3.8-27B-NVFP4` | model path passed to `ft daemon start` |
| `local_ft_bin` | `C:\Users\chenw\AppData\Local\FreeToken\venv\Scripts\ft.exe` | path to the engine's `ft.exe` |
| `local_gpu_guard_procs` | `UnrealEditor.exe`, `UnrealEditor-Cmd.exe`, `RainbowSix.exe` | never auto-start the engine while any of these run |
| `local_prompt_preamble` | standing local-agent instructions | text prepended to every local dispatch |

## Quick start

```powershell
py  -3.13 tracker.py run                 # start the control loop (leave running)
pyw -3.13 tracker.py overlay             # open the always-on-top panel
py  -3.13 tracker.py goal 85             # set the weekly goal (0.85, 85 or 85% all mean 85%)
py  -3.13 tracker.py status              # live rings, pacing, queue
py  -3.13 tracker.py graph               # print the executive / advisory / worker tiers
py  -3.13 tracker.py graph set workers.count=20 advisory.model=claude-opus-5
py  -3.13 tracker.py report --open       # build the work-distribution page and open it

# queue real work
py  -3.13 tracker.py add --id my-task --prompt "..." --cwd C:\path\to\project --weight heavy --priority 5
```

- **Stop dispatch:** press **STOP** in the overlay (writes `state/control.json`); the loop launches nothing new, running work continues. Press **START** to resume.
- **Close the overlay:** the **X** button, `Esc`, or right-click.

State files under `state/`:

| File | What it holds |
|---|---|
| `control.json` | the START / STOP dispatch flag (`running` or `stopped`) |
| `goal.json` | per-user weekly-goal override the `- / +` steppers write |
| `stop.json` | written once the weekly goal is reached, the main session's stop point |
| `throttle.json` | the FULL THROTTLE flag (`{"active": ...}`) |
| `graph.json` | per-user agentic-graph override the ladder chart's `-` / `+` writes; a patch, so only the fields set here stop following `config.json` |
| `report.json` | the last work-distribution report: path, reason, window |
| `overlay.json` | the overlay's collapsed / expanded state |
| `history.jsonl` | the log of usage snapshots the pacer reads back |

Reports land in `reports/`: `<UTC timestamp>-ledger.html`, the `<timestamp>-summary.json` it was built from, and `latest.html` (a copy of the newest, the one **VIEW REPORT** opens).

## Tests

```powershell
py -3.13 tests/test_all.py               # 112/112
```

## Acknowledgements

Built with Claude Code, using Claude models as the workers it dispatches and the reviewers of its own code. The local fallback lane runs on a FreeToken engine serving a Qwen model. The overlay is plain Tk from the Python standard library, and it is designed to sit beneath Sundial, the desktop widget that first made Claude Code usage visible at a glance.

Unofficial project, not affiliated with Anthropic. The usage endpoint is undocumented and may change at any time.
</content>
