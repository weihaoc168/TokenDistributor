# TokenDistributor

Spend Claude Code's weekly token budget before it resets, without ever starving a live session.

<p align="center"><img src="docs/overlay.png" width="360" alt="TokenDistributor overlay"></p>

The overlay: three rings (5-hour, weekly with the gear mascot and an amber goal tick, Fable), live sessions with the main session pinned, token distribution and the current mode, an **AGENTIC GRAPH** ladder chart showing the model each tier is *actually* running on right now over two bars carrying that tier's share of the window's input and output tokens, a `- GOAL 90% +` stepper row, START / STOP, FULL THROTTLE, and VIEW REPORT / REPORT NOW.

## What it does

- **Paces usage across sessions.** Every poll it reads the OAuth usage endpoint (5-hour, weekly, and per-model Fable windows), measures how much budget is left against how much time is left, and launches just enough headless `claude -p` work to land near 100% by the weekly reset.
- **Runs five decision modes.** `pace` when behind the curve, `coast` when ahead, `yield` while a foreign interactive session is active, `surge` in the endgame hours before reset, `blocked` when the weekly limit or the 5-hour guard is hit. A running task is never killed by a mode change, only new launches are gated.
- **Treats the weekly goal as a stopping point.** When the weekly window reaches the goal it writes `state/stop.json` once (keys `reason`, `goal`, `weekly`, `at`), the record the main session reads as its stop point, and parks dispatch. Falling back under the goal only clears the file, so a fresh week never resumes on its own.
- **Gates dispatch with START / STOP.** The overlay buttons write `state/control.json`; STOP zeroes every launch budget so nothing new starts, while already-running work keeps going and is still reaped.
- **Surges on FULL THROTTLE.** The amber button writes `state/throttle.json`, overriding pacing to spend the remaining weekly budget on the project's highest-value work until the weekly limit itself stops it.
- **Falls back to a local lane.** While `blocked`, queued tasks are re-dispatched to a local FreeToken engine (Qwen on the 5090) that burns zero cloud budget. A GPU guard refuses to auto-start the engine while a listed process (the Unreal editor, a game) owns the card, since the engine pins about 31.5 GB of it.
- **Runs a configured agentic graph.** `config.json`'s `graph` names the model and headcount at each tier (executive, advisory, workers); the legacy `worker_model` / `max_concurrency` keys are derived from it, the fork brief is handed the same line through its `{graph}` placeholder, and `state/graph.json` (the overlay's `-` / `+`) overrides it without touching the config.
- **Keeps the tiers in capability order, with a fallback under each.** The executive makes the crucial calls and the advisory lenses judge the work, so the graph must read `executive >= advisory >= workers` on `MODEL_RANK` (Fable 5.1 > Fable 5 > Opus 5 > Opus 4.8 > Sonnet 5 > Haiku 4.5 > the local model); a violation is a warning everywhere it is printed, and `tracker.py graph set` refuses to write a **new** one without `--force` (a violation already standing on disk is reprinted, not re-refused, so a later `workers.count=20` still goes through). Each tier also names a `fallback` (Fable 5 for the executive and advisory, Opus 4.8 for the workers): when a launch dies on a 529, an overload, or a rate / session / usage limit, the dispatcher marks `state/limited.json`, requeues that one task with the tier's fallback forced for its next launch, and keeps new launches for that tier on the fallback for `fallback_minutes` before trying the primary again. The requeue is a queue entry, not a launch, so it still passes the tick's concurrency budget, STOP and the fork's re-arm gate. When the **fallback** is the one that dies, the primary's mark is left exactly as it stands and the row stays failed: overwriting it with the fallback's id would send the next launch straight back into the model the account just refused. It never falls back **upward** - a worker never lands on the executive's model - and the row records the model that actually ran (`model_used`) while `model` keeps holding what the row asked for.
- **Reports where the work went.** It parses the session, fork and Workflow-agent transcripts for a window, splits every turn by tier and by what it did (DECIDE / DELEGATE / READ / AUTHOR / OPS), and writes `reports/latest.html`: a page whose verdict says whether the executive tier stayed executive-only (the 60% hands-on rule). It runs itself when a fork finishes a milestone and when dispatch stops, and on demand from the CLI or the overlay's **VIEW REPORT**.
- **Stamps every row with the role that produced it.** Each session and agent carries a role in `main_session` / `fork_session` / `workflow_agent` and, for a fork, the model it actually ran on, read from the append-only `state/handover.log` (every start and finish the dispatcher writes) and the task rows. Tiers follow that role rather than the model id, which is what keeps the verdict upright in both directions: one model at every tier no longer files each worker lane as executive, and a fork that ran on a model the graph has *since* moved to the worker tier stays executive, flagged **role-tiered** in its row and named in a caveat. The page carries a **Roles** table (role x model: turns, output, weighted cost, dollars) and prints the `graph_in_force` it was generated against in its footer, so a page read next week is not reinterpreted against next week's graph.
- **Prices that work in dollars.** `config.json`'s `pricing` block carries each model's published list price (input / output / cache write 5m / cache write 1h / cache read, USD per 1M tokens) with the source URL and the date it was read, and the report's **What it cost** section bills the window's own usage records at it: total USD, a stacked bar per model of the five components, by tier, by lane role, per hour, and the cost between consecutive commits. A model with no published price is shown as `unpriced` and named in a caveat, never billed at a guessed rate; the local FreeToken lane is priced at 0. `state/pricing.json` (`tracker.py pricing set`) overrides one field at a time without touching the config.
- **Bills both cache-write durations.** `usage.cache_creation` splits every creation figure into 5-minute and 1-hour writes, and the published table charges the 1-hour ones at 2&times; base input against the 5-minute ones' 1.25&times;. So the price table carries both numbers and `cost_usd` has five terms, not four. On this machine's traffic the 1-hour share runs 10-40% of creation per model, which a single-rate formula would quietly understate by about 3.5% of the total and by the same bias in every cut below it.
- **Re-reads `config.json` while it runs.** Every poll stats the file and re-parses it when the mtime or size moved, applying every non-path key onto the live `Config` (state paths never move under a running loop) and logging `config reloaded: <keys>` and `graph changed: <old> -> <new>`. The fork's model and brief are refreshed from the graph in force before every launch, not only on a re-arm, so an executive model edited mid-run reaches the very next fork instead of the next restart - the failure of 2026-09-03 19:48 UTC, when the graph was changed under a running loop, `state/graph.json` carried only the worker count, and the fork launched on the stale in-memory model. The overlay re-reads the same file on every refresh, so an edit shows on the panel within seconds. The one edit that cannot win is a field `state/graph.json` pins: the override wins field by field, so an executive model edited in `config.json` while the override names it is applied to the config and then ignored by every reader. That is named rather than swallowed - startup, `tracker.py graph` and the reload itself print `note: state/graph.json pins executive.model=...; config.json's executive.model is ignored (tracker.py graph set ..., or delete that key from state/graph.json)`, the reload printing only the pins the edit just hit.
- **Draws that graph as a ladder chart, live.** The panel's **AGENTIC GRAPH** block is one rung per tier, executive over advisory over workers, hung off a spine on the left, with the tier, the model and `xN` in aligned columns. The model on each rung is the one **actually in use**, re-derived every refresh: `state/handover.json` for the executive while the fork is running, the running rows' `model_used` for the workers (`+N` when more than one), the configured model - or its fallback while `state/limited.json` names the primary - for the advisory lenses. When it is not what `config.json` asks for, the configured id follows it dimmed as `cfg fable-5-1` and a one-line `active vs configured` note appears under the ladder. Each rung otherwise carries its tier's fallback dimmed after the model (`fable-5-1 ->fable-5`) and a red **LIMITED** tag while that rung's primary is marked limited; the worker rung is the emphasis and its `-` / `+` still write `state/graph.json`. A dimmed dot in front of a model id marks a tier `state/graph.json` pins. Neither dimmed suffix is ever ellipsised - a rung reading `cfg fab...` names no model - so when the tag and the configured id cannot share one rung the model column slides left, then the tag shortens to `LIM`, then to a red dot beside the count, which is what keeps both readable from 100% to 200% DPI.
- **Puts the token shares on the ladder.** Under every rung sit two thin bars: that tier's share of the window's **input** tokens (input + cache creation + cache read) in blue and of its **output** tokens in green, each labelled `in NN%` / `out NN%` in a monospaced right-aligned column, so the six figures compare down the ladder. The numbers come from `state/tiers.json`, which the loop rebuilds every `tiers_refresh_seconds` off the poll thread from the same role-stamped parse the report uses; the overlay only ever reads that file, so a redraw never opens a transcript. Repeat builds are cheap because `state/ledger_cache.json` keys a per-transcript tally on path + mtime + size + window start, and a fork's transcript is only re-read once it has grown. `tracker.py status` prints the same split as one line.
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
| `graph` | E fable-5.1 x1, A fable-5.1 x3, W opus-5 x10/20, each with a `fallback` | the agentic graph; `worker_model`, `throttle_model`, `max_concurrency` and `surge_concurrency` are derived from it (`state/graph.json` overrides) |
| `fallback_minutes` | `30` | how long `state/limited.json` keeps a tier on its fallback before the primary is tried again |
| `known_models` | six `claude-*` ids | allow-list for graph model and fallback ids; an unknown id warns, it never refuses |
| `pricing` | list prices for the five `claude-*` ids + the local model | USD per 1M tokens per model: `input`, `output`, `cache_write` (5m), `cache_write_1h`, `cache_read`, each row with its `source` and `checked` date (`state/pricing.json` overrides). A model missing here, or missing any one of the five, reports as `unpriced` |
| `pricing_default` | `null` | price for a model the table does not name; null on purpose, so nothing is billed at a stand-in rate |
| `report_repo` | `C:/Users/chenw/StarGTA` | repo watched for the fork-milestone report trigger |
| `tiers_refresh_seconds` | `300` | how often the loop rebuilds `state/tiers.json`, the per-tier token shares the panel's bars read |
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
py  -3.13 tracker.py graph               # print the tiers, their fallbacks and the order warnings
py  -3.13 tracker.py graph set workers.count=20 workers.fallback=claude-opus-4-8
py  -3.13 tracker.py graph set --force advisory.model=claude-sonnet-5   # only way past the order rule
py  -3.13 tracker.py pricing             # print the per-model list prices the report bills at
py  -3.13 tracker.py pricing set claude-opus-5.output=25 claude-opus-5.checked=2026-09-03
py  -3.13 tracker.py pricing set claude-opus-5.cache_write_1h=10   # 1-hour writes bill at 2x base input
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
| `graph.json` | per-user agentic-graph override the ladder chart's `-` / `+` writes; a patch, so only the fields set here stop following `config.json` - and those fields are *pinned*: the loop, `tracker.py graph` and the panel's dot say which, because an edit to a pinned field in `config.json` does nothing at all |
| `limited.json` | `{model, since, reason}` for the model a launch last failed on with a 529 or a limit; expires after `fallback_minutes`, and is cleared as soon as that model completes a run |
| `handover.json` | the newest fork handover the parent session watches: task, mode, model, parent session, status, and the finish figures |
| `handover.log` | append-only, one JSON object per line: every `started` / `done` / `failed` handover record ever written. The ledger reads it for the model each fork actually ran on |
| `tiers.json` | `{window, tiers: {executive/advisory/workers: {input, output, sessions}}, generated_at}` - the token shares the ladder's bars draw, rebuilt by the loop every `tiers_refresh_seconds` |
| `ledger_cache.json` | per-transcript tallies keyed on path + mtime + size + window start, with the message ids each one counted, so an unchanged transcript is never parsed twice |
| `pricing.json` | per-user price override `tracker.py pricing set` writes; a patch, so only the fields set here stop following `config.json` |
| `report.json` | the last work-distribution report: path, reason, window |
| `overlay.json` | the overlay's collapsed / expanded state |
| `history.jsonl` | the log of usage snapshots the pacer reads back |

Reports land in `reports/`: `<UTC timestamp>-ledger.html`, the `<timestamp>-summary.json` it was built from, and `latest.html` (a copy of the newest, the one **VIEW REPORT** opens).

## Tests

```powershell
py -3.13 tests/test_all.py               # 146/146
```

## Acknowledgements

Built with Claude Code, using Claude models as the workers it dispatches and the reviewers of its own code. The local fallback lane runs on a FreeToken engine serving a Qwen model. The overlay is plain Tk from the Python standard library, and it is designed to sit beneath Sundial, the desktop widget that first made Claude Code usage visible at a glance.

Unofficial project, not affiliated with Anthropic. The usage endpoint is undocumented and may change at any time.
</content>
