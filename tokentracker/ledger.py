"""Work-distribution report: where the tokens actually went, as one page.

This is the usage audit the director ran by hand, ported into the tracker so it
runs itself. It parses the transcripts on disk for a window, splits every
assistant turn by model tier (executive/advisory vs workers, from the agentic
graph) and by what the turn *did*, and renders `reports/<ts>-ledger.html` from
`templates/ledger.html`.

Four sources, all files, nothing fetched:

    1. the main session transcript(s) named in config `main_session_ids`
    2. every fork the dispatcher launched (session id captured at exit, with a
       fallback scan for transcripts touched in the window that carry the fork
       prompt)
    3. the Workflow subagent transcripts under those sessions
    4. `state/history.jsonl`, for the utilization series

One transcript artifact drives the whole parser: Claude Code writes ONE logical
assistant turn as SEVERAL JSONL entries (roughly one per content block) and
every one of them repeats the IDENTICAL full `message.usage`. Counting raw
entries inflates every token figure ~3.4x, so turns are deduplicated by
`message.id` and the content blocks of a turn are merged before categorising.

The same artifact appears one level up, between files: a `--resume
--fork-session` transcript opens with a verbatim copy of the parent's history,
identical message ids, timestamps and usage included. So the dedup set is
shared across every source `build_summary` folds, main session first, and each
turn is attributed to the transcript that actually produced it.

Weighted cost is a comparison proxy, not a bill:

    weighted = output + 0.1*(input + cache_creation) + 0.01*cache_read

Nothing here raises into the run loop: `maybe_report` swallows everything, and
generation happens on a background thread so a poll is never blocked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .models import parse_iso, utcnow

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "ledger.html"
DATA_PLACEHOLDER = "const DATA = __DATA__;"
TITLE_PLACEHOLDER = "__TITLE__"
LATEST_NAME = "latest.html"

# Category rules, verbatim from the audit: the first non-READ category in the
# order below wins, a turn with no tool_use at all is DECIDE.
READ_TOOLS = {"Read", "Grep", "Glob", "ToolSearch", "ListAgents"}
AUTHOR_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
OPS_TOOLS = {"Bash", "PowerShell", "Monitor", "BashOutput", "KillShell"}
DELEGATE_TOOLS = {"Workflow", "Agent", "SendMessage", "Skill", "Task",
                  "SendUserMessage", "SendUserFile", "TaskStop", "TaskOutput"}
ARTIFACT_TOOLS = {"Artifact"}
CATS = ("DECIDE", "DELEGATE", "READ", "AUTHOR", "OPS", "ARTIFACT")
HANDS_ON_CATS = ("AUTHOR", "OPS", "READ")
# The audit's rule, kept exactly: over 60% hands-on weighted cost and the tier
# was not executive-only, it was doing the building itself.
HANDS_ON_LIMIT = 0.60

USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens")
WEIGHTING = ("weighted_cost = output_tokens + 0.1*(input_tokens + "
             "cache_creation_input_tokens) + 0.01*cache_read_input_tokens")

EXEC_TIER = "fable"    # template key for the executive/advisory tier
WORK_TIER = "opus"     # template key for the worker tier
MIXED_TIER = "mixed"   # a model whose turns landed in both tiers
# Which tier a transcript's role means, used when the graph names one model at
# both ends and the model id therefore cannot tell the tiers apart.
MAIN_SOURCE = "main_session"
FORK_SOURCE = "fork_session"
AGENT_SOURCE = "workflow_agent"
SOURCE_TIERS = {MAIN_SOURCE: EXEC_TIER, FORK_SOURCE: EXEC_TIER,
                AGENT_SOURCE: WORK_TIER}
SERIES_MAX_POINTS = 60
LEDGER_ROWS = 12
FORK_PROBE_CHARS = 60
REASON_MILESTONE = "fork milestone"
REASON_STOP = "stopped"
REASON_MANUAL = "manual"
REPORT_KEYS = ("last_report", "last_reason", "generated_at", "window",
               "last_stop_key")

_BUSY = threading.Lock()


# --------------------------------------------------------------- primitives

def new_usage() -> dict[str, int]:
    return {k: 0 for k in USAGE_KEYS}


def add_usage(dst: dict[str, int], src: Any) -> None:
    if not isinstance(src, dict):
        return
    for key in USAGE_KEYS:
        value = src.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            dst[key] += int(value)


def weighted(usage: dict[str, int]) -> float:
    return (usage["output_tokens"]
            + 0.1 * (usage["input_tokens"] + usage["cache_creation_input_tokens"])
            + 0.01 * usage["cache_read_input_tokens"])


def categorise(tools: Iterable[str]) -> str:
    """First non-READ of AUTHOR > OPS > DELEGATE > ARTIFACT, else READ.

    A turn with no tool_use at all (text and thinking only), or one that only
    asked the operator a question, is the tier deciding: DECIDE.
    """
    names = [t for t in tools if t != "AskUserQuestion"]
    if not names:
        return "DECIDE"
    for group, label in ((AUTHOR_TOOLS, "AUTHOR"), (OPS_TOOLS, "OPS"),
                         (DELEGATE_TOOLS, "DELEGATE"),
                         (ARTIFACT_TOOLS, "ARTIFACT"), (READ_TOOLS, "READ")):
        if any(t in group for t in names):
            return label
    return "READ"


def _round(value: float) -> float:
    return round(float(value), 1)


class Tally:
    """Per-model, per-category totals plus the hourly output histogram."""

    def __init__(self) -> None:
        self.by_model: dict[str, dict[str, Any]] = {}
        self.hourly: dict[str, dict[str, dict[str, int]]] = {}

    def _model(self, model: str) -> dict[str, Any]:
        row = self.by_model.get(model)
        if row is None:
            row = {"messages": 0, "usage": new_usage(), "weighted": 0.0,
                   "cats": {}}
            self.by_model[model] = row
        return row

    def add_turn(self, model: str, cat: str, usage: dict[str, int],
                 ts: datetime | None) -> None:
        row = self._model(model)
        row["messages"] += 1
        for key in USAGE_KEYS:
            row["usage"][key] += usage[key]
        cost = weighted(usage)
        row["weighted"] += cost
        cell = row["cats"].setdefault(
            cat, {"messages": 0, "output_tokens": 0, "weighted": 0.0})
        cell["messages"] += 1
        cell["output_tokens"] += usage["output_tokens"]
        cell["weighted"] += cost
        if ts is not None:
            hour = ts.strftime("%Y-%m-%dT%H:00Z")
            bucket = self.hourly.setdefault(model, {}).setdefault(
                hour, {"output_tokens": 0, "messages": 0})
            bucket["output_tokens"] += usage["output_tokens"]
            bucket["messages"] += 1

    def merge(self, other: "Tally") -> None:
        for model, row in other.by_model.items():
            mine = self._model(model)
            mine["messages"] += row["messages"]
            mine["weighted"] += row["weighted"]
            for key in USAGE_KEYS:
                mine["usage"][key] += row["usage"][key]
            for cat, cell in row["cats"].items():
                dst = mine["cats"].setdefault(
                    cat, {"messages": 0, "output_tokens": 0, "weighted": 0.0})
                dst["messages"] += cell["messages"]
                dst["output_tokens"] += cell["output_tokens"]
                dst["weighted"] += cell["weighted"]
        for model, hours in other.hourly.items():
            dst_hours = self.hourly.setdefault(model, {})
            for hour, bucket in hours.items():
                dst = dst_hours.setdefault(
                    hour, {"output_tokens": 0, "messages": 0})
                dst["output_tokens"] += bucket["output_tokens"]
                dst["messages"] += bucket["messages"]

    def totals(self) -> tuple[int, float]:
        out = sum(r["usage"]["output_tokens"] for r in self.by_model.values())
        cost = sum(r["weighted"] for r in self.by_model.values())
        return out, cost

    def cat_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.by_model.values():
            for cat, cell in row["cats"].items():
                counts[cat] = counts.get(cat, 0) + cell["messages"]
        return counts

    def hands_on_share(self) -> float:
        hands = 0.0
        total = 0.0
        for row in self.by_model.values():
            total += row["weighted"]
            for cat in HANDS_ON_CATS:
                hands += row["cats"].get(cat, {}).get("weighted", 0.0)
        return hands / total if total else 0.0


# ------------------------------------------------------------ transcripts

def _iter_entries(path: Path) -> Iterable[dict]:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def parse_transcript(path: Path, start: datetime, end: datetime,
                     seen: set[str] | None = None) -> Tally:
    """One transcript's turns inside [start, end], deduplicated by message.id.

    The dedup is the whole point, and it has to reach across files as well as
    within one:

      * within a file, several JSONL entries share one message.id and each
        repeats the same usage, so usage is taken once per id while the
        tool_use blocks of every entry are merged before categorising;
      * across files, a resumed fork carries a verbatim copy of the parent's
        history - same ids, same timestamps, same usage - so `seen` (one set
        for the whole report, passed by `build_summary` with the main session
        read first) keeps the copy from being counted again per fork.

    `seen` is read *and* written: every id this call counts is added to it.
    Called without one, each call dedups only itself, which is the old
    single-transcript behaviour.
    """
    turns: dict[str, dict[str, Any]] = {}
    counted = seen if seen is not None else set()
    for entry in _iter_entries(path):
        if entry.get("type") != "assistant":
            continue
        stamp = parse_iso(entry.get("timestamp"))
        if stamp is None or stamp < start or stamp > end:
            continue
        message = entry.get("message") or {}
        if not isinstance(message, dict):
            continue
        mid = message.get("id") or f"noid::{entry.get('uuid')}"
        turn = turns.get(mid)
        if turn is None:
            if mid in counted:
                # Already counted from an earlier transcript: this is the copy
                # a resumed fork inherited, not a turn the fork produced.
                continue
            usage = new_usage()
            add_usage(usage, message.get("usage"))
            model = str(message.get("model") or "unknown").strip("<>") or "unknown"
            turn = {"model": model, "usage": usage, "ts": stamp,
                    "tools": [], "tool_ids": set()}
            turns[mid] = turn
            counted.add(mid)
        elif stamp < turn["ts"]:
            turn["ts"] = stamp
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "?")
            bid = block.get("id") or f"anon::{name}::{len(turn['tools'])}"
            if bid in turn["tool_ids"]:
                continue
            turn["tool_ids"].add(bid)
            turn["tools"].append(name)

    tally = Tally()
    for turn in turns.values():
        tally.add_turn(turn["model"], categorise(turn["tools"]),
                       turn["usage"], turn["ts"])
    return tally


def _fork_probe(prompt: str) -> str:
    """A JSON-escaped needle from the fork brief, for a raw substring scan."""
    first = str(prompt or "").strip().splitlines()[0] if str(prompt or "").strip() else ""
    if len(first) < 20:
        return ""
    return json.dumps(first[:FORK_PROBE_CHARS])[1:-1]


def carries_fork_prompt(path: Path, probe: str) -> bool:
    """True when the transcript contains the fork brief as a user message.

    A `--resume --fork-session` transcript opens with the *parent's* history,
    so the brief is not the first line of the file; it is the first thing the
    fork was told, which is what this looks for.
    """
    if not probe:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if probe in line:
                    return True
    except OSError:
        return False
    return False


def find_transcript(cfg: Config, session_id: str) -> Path | None:
    if not session_id:
        return None
    try:
        for project in cfg.projects_dir.iterdir():
            if not project.is_dir():
                continue
            candidate = project / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
    except OSError:
        return None
    return None


def agent_transcripts(cfg: Config, session_id: str) -> list[Path]:
    """Workflow subagent transcripts filed under one session."""
    found: list[Path] = []
    try:
        for project in cfg.projects_dir.iterdir():
            if not project.is_dir():
                continue
            root = project / session_id / "subagents" / "workflows"
            if not root.is_dir():
                continue
            for workflow in sorted(root.iterdir()):
                if not workflow.is_dir():
                    continue
                found.extend(sorted(workflow.glob("agent-*.jsonl")))
    except OSError:
        return found
    return found


def fork_session_ids(cfg: Config) -> list[str]:
    """Ids the dispatcher recorded: task rows first, then the handover record."""
    ids: list[str] = []
    try:
        data = json.loads(cfg.tasks_file.read_text(encoding="utf-8"))
        for row in data.get("tasks", []):
            sid = row.get("fork_session_id") if isinstance(row, dict) else None
            if isinstance(sid, str) and sid and sid not in ids:
                ids.append(sid)
    except (OSError, json.JSONDecodeError, ValueError, AttributeError, TypeError):
        pass
    try:
        record = json.loads(cfg.handover_file.read_text(encoding="utf-8"))
        sid = record.get("fork_session_id") if isinstance(record, dict) else None
        if isinstance(sid, str) and sid and sid not in ids:
            ids.append(sid)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return ids


def discover_sessions(cfg: Config, start: datetime, end: datetime) -> list[dict]:
    """[{sid, path, role}] for the main session(s) and every fork in the window.

    Forks are found by the session id `_finalize_record` captured; a fork whose
    id was never recorded (killed, still running, an older row) is picked up by
    the fallback scan over transcripts touched inside the window.
    """
    sessions: list[dict] = []
    seen: set[str] = set()

    def add(sid: str, path: Path | None, role: str) -> None:
        if not sid or sid in seen or path is None:
            return
        seen.add(sid)
        sessions.append({"sid": sid, "path": path, "role": role})

    for sid in cfg.main_session_ids:
        add(str(sid), find_transcript(cfg, str(sid)), "main")
    for sid in fork_session_ids(cfg):
        add(sid, find_transcript(cfg, sid), "fork")

    probe = _fork_probe(getattr(cfg, "throttle_prompt", ""))
    if not probe:
        return sessions
    lo, hi = start.timestamp(), end.timestamp()
    try:
        projects = [p for p in cfg.projects_dir.iterdir() if p.is_dir()]
    except OSError:
        projects = []
    for project in projects:
        try:
            candidates = sorted(project.glob("*.jsonl"))
        except OSError:
            continue
        for path in candidates:
            sid = path.stem
            if sid in seen:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < lo or mtime > hi + 3600:
                continue
            if carries_fork_prompt(path, probe):
                add(sid, path, "fork")
    return sessions


# --------------------------------------------------------------- utilization

def utilization_series(cfg: Config, start: datetime, end: datetime) -> list[dict]:
    """Downsampled samples from state/history.jsonl, as the page wants them."""
    points: list[dict] = []
    try:
        lines = cfg.history_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return points
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        stamp = parse_iso(record.get("fetched_at"))
        if stamp is None or stamp < start or stamp > end:
            continue
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        scoped = None
        for value in extra.values():
            if isinstance(value, dict) and value.get("utilization") is not None:
                scoped = float(value.get("utilization") or 0.0)
                break
        points.append({
            "t": record.get("fetched_at"),
            "five_hour": float((record.get("five_hour") or {}).get("utilization") or 0.0),
            "seven_day": float((record.get("seven_day") or {}).get("utilization") or 0.0),
            "fable": scoped,
        })
    if len(points) <= SERIES_MAX_POINTS:
        return points
    step = max(1, -(-len(points) // SERIES_MAX_POINTS))
    thinned = points[::step]
    if thinned[-1] is not points[-1]:
        thinned.append(points[-1])
    return thinned


# ------------------------------------------------------------------ summary

def graph_separates_tiers(graph: dict) -> bool:
    """True when the graph names a worker model the executive tier does not use.

    False is the shipped default: config.json names claude-opus-5 at all three
    tiers, so a model id says nothing about which hat the turn was wearing.
    """
    from .graph import tiers_of

    executive, advisory, workers = tiers_of(graph)
    worker = str(workers.get("model") or "")
    exec_models = {str(executive.get("model") or ""),
                   str(advisory.get("model") or "")}
    return bool(worker) and worker not in exec_models


def tier_of(model: str, graph: dict, source: str | None = None) -> str:
    """Which tier a turn belongs to: by model id, or by role when ids collide.

    Two readings of the same question - did the model wearing the director's
    hat also do the building?

    * The graph names different models for the executive and the workers: the
      model id answers it, executive/advisory membership winning a tie so a
      model named twice is still counted once.
    * The graph names ONE model at both ends (the shipped default is opus
      everywhere): the id cannot answer it at all, and keying on it puts every
      worker lane in the executive tier and drops the actual director - running
      whatever model it happens to run - into the worker tier, inverting the
      verdict. So the transcript's own role decides: main and fork sessions are
      the executive tier, Workflow subagents are the workers.

    `source` is the role (`main_session`, `fork_session`, `workflow_agent`).
    Without one - a per-model label with no single role behind it - the model
    rule is used, as before.
    """
    from .graph import ADVISORY, EXECUTIVE, WORKERS

    if source is not None and not graph_separates_tiers(graph):
        return SOURCE_TIERS.get(source, WORK_TIER)
    name = str(model or "")
    exec_models = {str(graph.get(EXECUTIVE, {}).get("model", "")),
                   str(graph.get(ADVISORY, {}).get("model", ""))}
    if name in exec_models:
        return EXEC_TIER
    if name == str(graph.get(WORKERS, {}).get("model", "")):
        return WORK_TIER
    return WORK_TIER


def _breakdown(tally: Tally) -> dict[str, dict[str, float]]:
    out_total, cost_total = tally.totals()
    cells: dict[str, dict[str, float]] = {}
    for cat in CATS:
        out = sum(r["cats"].get(cat, {}).get("output_tokens", 0)
                  for r in tally.by_model.values())
        cost = sum(r["cats"].get(cat, {}).get("weighted", 0.0)
                   for r in tally.by_model.values())
        msgs = sum(r["cats"].get(cat, {}).get("messages", 0)
                   for r in tally.by_model.values())
        cells[cat] = {
            "messages": msgs,
            "output": out,
            "weighted": _round(cost),
            "share": round(cost / cost_total, 4) if cost_total else 0.0,
            "output_share": round(out / out_total, 4) if out_total else 0.0,
        }
    return cells


def build_summary(cfg: Config, start: datetime, end: datetime,
                  reason: str = REASON_MANUAL) -> dict:
    """The whole report payload, in the schema templates/ledger.html renders."""
    from .graph import ADVISORY, EXECUTIVE, WORKERS, read_graph

    graph = read_graph(cfg)
    by_model_split = graph_separates_tiers(graph)
    sessions = discover_sessions(cfg, start, end)
    overall = Tally()
    tiers = {EXEC_TIER: Tally(), WORK_TIER: Tally()}
    model_sources: dict[str, list[str]] = {}
    model_tiers: dict[str, set[str]] = {}
    sinks: list[dict] = []
    agent_files = 0
    # One dedup set for the whole report: a resumed fork's transcript repeats
    # the parent's turns verbatim, and discover_sessions yields the main
    # session first, so the parent keeps its own turns and each fork keeps only
    # what it actually produced.
    seen_turns: set[str] = set()

    def fold(tally: Tally, source: str) -> None:
        overall.merge(tally)
        for model, row in tally.by_model.items():
            model_sources.setdefault(model, [])
            if source not in model_sources[model]:
                model_sources[model].append(source)
            split = Tally()
            split.by_model[model] = row
            split.hourly = {model: tally.hourly.get(model, {})}
            tier = tier_of(model, graph, source)
            model_tiers.setdefault(model, set()).add(tier)
            tiers[tier].merge(split)

    for session in sessions:
        source = MAIN_SOURCE if session["role"] == "main" else FORK_SOURCE
        tally = parse_transcript(session["path"], start, end, seen_turns)
        fold(tally, source)
        for model, row in tally.by_model.items():
            cats = {c: cell["messages"] for c, cell in row["cats"].items()}
            hands = sum(row["cats"].get(c, {}).get("weighted", 0.0)
                        for c in HANDS_ON_CATS)
            share = hands / row["weighted"] if row["weighted"] else 0.0
            sinks.append({
                "source": source,
                "id_or_label": f"{session['sid'][:8]} {session['role']} / {model}",
                "what": (f"{session['role']} session on {model}: "
                         f"{cats.get('OPS', 0)} OPS, {cats.get('AUTHOR', 0)} AUTHOR, "
                         f"{cats.get('DECIDE', 0)} DECIDE turns"),
                "fable_output": row["usage"]["output_tokens"],
                "fable_cache_read": row["usage"]["cache_read_input_tokens"],
                "weighted_cost": _round(row["weighted"]),
                "verdict": "hands-on" if share > HANDS_ON_LIMIT else "executive",
            })
        agents = Tally()
        for path in agent_transcripts(cfg, session["sid"]):
            agent_files += 1
            agents.merge(parse_transcript(path, start, end, seen_turns))
        if agents.by_model:
            fold(agents, AGENT_SOURCE)
            for model, row in agents.by_model.items():
                cats = {c: cell["messages"] for c, cell in row["cats"].items()}
                sinks.append({
                    "source": AGENT_SOURCE,
                    "id_or_label": f"{session['sid'][:8]} workflow agents / {model}",
                    "what": (f"Workflow subagents under {session['sid'][:8]} on "
                             f"{model}: {cats.get('OPS', 0)} OPS, "
                             f"{cats.get('AUTHOR', 0)} AUTHOR turns"),
                    "fable_output": row["usage"]["output_tokens"],
                    "fable_cache_read": row["usage"]["cache_read_input_tokens"],
                    "weighted_cost": _round(row["weighted"]),
                    "verdict": "worker-tier",
                })

    sinks.sort(key=lambda s: -s["weighted_cost"])
    exec_out, exec_cost = tiers[EXEC_TIER].totals()
    work_out, work_cost = tiers[WORK_TIER].totals()
    out_total = exec_out + work_out
    cost_total = exec_cost + work_cost
    exec_hands = tiers[EXEC_TIER].hands_on_share()
    work_hands = tiers[WORK_TIER].hands_on_share()
    exec_only = exec_hands <= HANDS_ON_LIMIT
    # Membership is what actually landed in each tier, not what tier_of says
    # about a bare model id: with one model at both ends the same id is in both.
    exec_models = sorted(tiers[EXEC_TIER].by_model)
    work_models = sorted(tiers[WORK_TIER].by_model)
    cat_counts = tiers[EXEC_TIER].cat_counts()
    exec_label = ", ".join(exec_models) or "none"
    work_label = ", ".join(work_models) or "none"
    if not by_model_split:
        # Say which turns the tier is, or "executive tier (claude-opus-5)" and
        # "worker tier (claude-opus-5)" read as the same sentence twice.
        exec_label = f"main + fork sessions on {exec_label}"
        work_label = f"workflow agents on {work_label}"

    verdict_para = (
        "The executive tier ({models}) spent {hands:.1%} of its weighted cost on "
        "hands-on work (AUTHOR/OPS/READ) against a {limit:.0%} line, so it "
        "{did} stay executive-only in this window. It produced {eout:,} output "
        "tokens ({eshare:.1%} of all output) across {ecount} turns; the worker "
        "tier ({wmodels}) produced {wout:,}. Executive turns split {decide} "
        "DECIDE / {delegate} DELEGATE against {ops} OPS and {author} AUTHOR."
    ).format(
        models=exec_label, wmodels=work_label,
        hands=exec_hands, limit=HANDS_ON_LIMIT,
        did="did" if exec_only else "did NOT",
        eout=exec_out, eshare=(exec_out / out_total if out_total else 0.0),
        ecount=sum(r["messages"] for r in tiers[EXEC_TIER].by_model.values()),
        wout=work_out,
        decide=cat_counts.get("DECIDE", 0), delegate=cat_counts.get("DELEGATE", 0),
        ops=cat_counts.get("OPS", 0), author=cat_counts.get("AUTHOR", 0),
    )

    evidence = [
        f"Executive tier hands-on share {exec_hands:.1%} of weighted cost "
        f"(rule: over {HANDS_ON_LIMIT:.0%} is not executive-only).",
        f"Worker tier hands-on share {work_hands:.1%}, "
        f"{work_out:,} output tokens over "
        f"{sum(r['messages'] for r in tiers[WORK_TIER].by_model.values())} turns.",
        f"Sources parsed: {len(sessions)} session transcript(s) "
        f"({sum(1 for s in sessions if s['role'] == 'fork')} fork), "
        f"{agent_files} workflow agent transcript(s).",
        f"Graph in force: executive {graph[EXECUTIVE]['model']} "
        f"x{graph[EXECUTIVE]['count']}, advisory {graph[ADVISORY]['model']} "
        f"x{graph[ADVISORY]['count']}, workers {graph[WORKERS]['model']} "
        f"x{graph[WORKERS]['count']} (surge {graph[WORKERS]['surge_count']}).",
        ("Tiers split by model id, which the graph names differently at the "
         "executive and worker tiers." if by_model_split else
         "Tiers split by transcript role (main and fork sessions are the "
         "executive tier, Workflow subagents the workers): the graph names one "
         "model at both ends, so the model id cannot tell them apart."),
    ]

    root_causes = ([] if exec_only else [
        "The executive tier ran the tools itself instead of dispatching: "
        f"{cat_counts.get('OPS', 0)} OPS and {cat_counts.get('AUTHOR', 0)} "
        "AUTHOR turns in the director's own sessions.",
        "Worker lanes were idle or under-used while the executive tier worked, "
        f"so only {(work_out / out_total if out_total else 0.0):.1%} of output "
        "came from the worker tier.",
    ])
    recommendations = [
        f"Keep every Workflow agent pinned to {graph[WORKERS]['model']} and "
        f"run up to {graph[WORKERS]['count']} lanes "
        f"({graph[WORKERS]['surge_count']} in surge).",
        f"Use {graph[ADVISORY]['count']} review lenses on "
        f"{graph[ADVISORY]['model']} rather than reviewing in the executive turn.",
        "Read `tracker.py graph` before a long pass; the fork brief carries the "
        "same line, so the graph is what the director is told to build.",
    ]

    return {
        "generated_at": utcnow().isoformat(),
        "reason": reason,
        "sources": {
            "main_session": ", ".join(
                s["sid"] for s in sessions if s["role"] == "main") or "none",
            "fork_sessions": ", ".join(
                s["sid"] for s in sessions if s["role"] == "fork") or "none",
            "workflow_agents": f"{agent_files} subagent transcript(s)",
            "tokendistributor": str(cfg.root),
        },
        "weighting": WEIGHTING,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": round((end - start).total_seconds() / 3600, 2),
        },
        "totals_by_model": {
            model: {
                "messages": row["messages"],
                "output_tokens": row["usage"]["output_tokens"],
                "cache_read_tokens": row["usage"]["cache_read_input_tokens"],
                "cache_creation_tokens": row["usage"]["cache_creation_input_tokens"],
                "input_tokens": row["usage"]["input_tokens"],
                "weighted_cost": _round(row["weighted"]),
                # Where this model's turns actually landed; "mixed" when the
                # same id ran in both tiers, which is the normal case once the
                # split is by role rather than by model.
                "tier": (sorted(model_tiers[model])[0]
                         if len(model_tiers.get(model, ())) == 1
                         else MIXED_TIER),
                "sources": model_sources.get(model, []),
            }
            for model, row in sorted(overall.by_model.items(),
                                     key=lambda kv: -kv[1]["weighted"])
        },
        "fable_vs_opus": {
            "fable_output": exec_out,
            "opus_output": work_out,
            "fable_share_output": round(exec_out / out_total, 4) if out_total else 0.0,
            "fable_weighted": _round(exec_cost),
            "opus_weighted": _round(work_cost),
            "fable_share_weighted": round(exec_cost / cost_total, 4) if cost_total else 0.0,
            "fable_models": exec_models,
            "opus_models": work_models,
        },
        "fable_work_breakdown": {
            EXEC_TIER: _breakdown(tiers[EXEC_TIER]),
            WORK_TIER: _breakdown(tiers[WORK_TIER]),
        },
        "where_fable_went": sinks[:LEDGER_ROWS],
        "verdict": {
            "executive_only": bool(exec_only),
            "one_paragraph": verdict_para,
            "evidence": evidence,
        },
        "root_causes": root_causes,
        "recommendations": recommendations,
        "utilization_series": utilization_series(cfg, start, end),
        "hourly_output_by_model": {
            model: dict(sorted(hours.items()))
            for model, hours in sorted(overall.hourly.items())
        },
        "caveats": [
            "Turns are deduplicated by message.id across every transcript, not "
            "only within one: Claude Code repeats the same usage on each JSONL "
            "entry of a turn, and a resumed fork opens with a verbatim copy of "
            "the parent's history. A turn is counted once, against the "
            "transcript that produced it (the main session is read first).",
            "Weighted cost is a comparison proxy, not a bill, and models no "
            "per-model pricing.",
            ("Tiers come from the agentic graph. It names different models at "
             "the executive and worker tiers, so each turn is tiered by its "
             "model id, executive winning a tie."
             if by_model_split else
             "Tiers come from the transcript's role, because the graph names "
             "one model at both the executive and the worker tier: main and "
             "fork sessions are the executive tier, Workflow subagents the "
             "workers. Tiering by model id would put every worker lane in the "
             "executive tier and the director in the worker tier."),
            "Only transcripts on this machine are read; a fork whose session id "
            "was never recorded is found by its brief, and one that wrote "
            "nothing in the window is invisible.",
        ],
    }


# ------------------------------------------------------------------ render

def report_stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def render(cfg: Config, summary: dict, now: datetime | None = None) -> Path:
    """Splice the summary into the template; returns the page written.

    Three files land in `reports/`: the timestamped page, the timestamped
    summary it was built from, and `latest.html` (a copy, so the overlay's
    VIEW REPORT button always has one stable path to open).
    """
    now = now or utcnow()
    stamp = report_stamp(now)
    reports = cfg.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    if html.count(DATA_PLACEHOLDER) != 1:
        raise ValueError(f"template placeholder missing: {TEMPLATE_PATH}")
    end = str(summary.get("window", {}).get("end", ""))[:10] or now.date().isoformat()
    payload = json.dumps(summary, ensure_ascii=True, indent=2).replace("</", "<\\/")
    html = html.replace(TITLE_PLACEHOLDER, f"Work Distribution {end}")
    html = html.replace(DATA_PLACEHOLDER, f"const DATA = {payload};")

    page = reports / f"{stamp}-ledger.html"
    page.write_text(html, encoding="utf-8", newline="\n")
    (reports / f"{stamp}-summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    shutil.copyfile(page, reports / LATEST_NAME)
    return page


def latest_report(cfg: Config) -> Path | None:
    path = cfg.reports_dir / LATEST_NAME
    return path if path.exists() else None


# ------------------------------------------------------------- report state

def read_report_state(cfg: Config) -> dict:
    try:
        data = json.loads(cfg.report_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_report_state(cfg: Config, *, path: Path, reason: str, window: dict,
                       stop_key: str | None = None) -> dict:
    record = {
        "last_report": str(path),
        "last_reason": reason,
        "generated_at": utcnow().isoformat(),
        "window": window,
        "last_stop_key": stop_key or read_report_state(cfg).get("last_stop_key"),
    }
    body = json.dumps(record, indent=2)
    target = cfg.report_file
    tmp = target.parent / f"{target.name}.tmp"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        try:
            target.write_text(body, encoding="utf-8")
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
    return record


def window_for(cfg: Config, since: datetime | None = None,
               hours: float | None = None, now: datetime | None = None,
               ) -> tuple[datetime, datetime]:
    """[start, end] for the next report: since the last one, or the fallback."""
    end = now or utcnow()
    if since is not None:
        return since, end
    if hours is not None and hours > 0:
        return end - timedelta(hours=hours), end
    previous = parse_iso(read_report_state(cfg).get("generated_at"))
    if previous is not None and previous < end:
        return previous, end
    try:
        fallback = float(getattr(cfg, "report_window_hours", 24.0))
    except (TypeError, ValueError):
        fallback = 24.0
    return end - timedelta(hours=max(fallback, 0.1)), end


# ---------------------------------------------------------------- triggers

def milestone_wanted(status: str | None, repo_changed: bool,
                     enabled: bool = True) -> bool:
    """A finished fork earns a report only when the repo moved with it.

    done + a new commit (or a rewritten PROGRESS_REPORT.json) is a milestone;
    done with nothing to show for it is just a fork that ended.
    """
    return bool(enabled and status == "done" and repo_changed)


def stop_wanted(stop_key: str | None, last_stop_key: str | None,
                enabled: bool = True) -> bool:
    """One report per stop, not one per poll.

    `stop_key` identifies the stop episode (the control file's changed_at with
    the goal record's `at` appended, or the loop's exit time); while it is
    unchanged the stop has already been reported.
    """
    return bool(enabled and stop_key and stop_key != last_stop_key)


def repo_changed_since(cfg: Config, since: datetime | None) -> tuple[bool, str]:
    """Did the tracked repo gain a commit, or a new PROGRESS_REPORT.json?

    Two independent signals so a fork that reports progress without committing
    still counts, and a `git` that is missing or slow can never wedge the poll.
    """
    if since is None:
        return False, "no start time"
    repo = Path(str(getattr(cfg, "report_repo", "") or ""))
    if not repo.name:
        return False, "no repo configured"
    progress = repo / "dev_JSON" / "PROGRESS_REPORT.json"
    try:
        if progress.stat().st_mtime > since.timestamp():
            return True, "PROGRESS_REPORT.json changed"
    except OSError:
        pass
    git = shutil.which("git")
    if git is None or not (repo / ".git").exists():
        return False, "no new commit"
    try:
        done = subprocess.run(
            [git, "-C", str(repo), "log", "-1", "--format=%cI"],
            capture_output=True, text=True, timeout=20, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False, "git unavailable"
    committed = parse_iso((done.stdout or "").strip())
    if committed is not None and committed > since:
        return True, "new commit"
    return False, "no new commit"


def stop_key_for(cfg: Config, control: str, stop: dict | None) -> str | None:
    """The identity of the stop episode standing right now, or None.

    The control file leads and the goal record only extends the key, because
    state/stop.json is deliberately NOT cleared when the operator presses START
    over the goal (goal.py keeps the record while the week is over goal). A key
    built from the goal record alone would therefore freeze for the rest of the
    week: the first goal stop reports, and every later STOP press produces the
    same key and is silently swallowed as "already reported".

    Nothing is returned while dispatch is running: a stop record left standing
    behind a START is not a stop, and reporting it would fire mid-run.
    """
    from .control import STOPPED

    if control != STOPPED:
        return None
    try:
        data = json.loads(cfg.control_file.read_text(encoding="utf-8"))
        changed = data.get("changed_at") if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        changed = None
    key = f"stop:{changed or 'unknown'}"
    at = stop.get("at") if isinstance(stop, dict) else None
    # Composite, so the goal stop and a manual STOP at the same changed_at are
    # still two episodes: the goal writes the record and the control file
    # together, and the operator can stop again afterwards.
    return f"{key}|goal:{at}" if at else key


# -------------------------------------------------------------- generation

def generate(cfg: Config, reason: str = REASON_MANUAL, *,
             since: datetime | None = None, hours: float | None = None,
             stop_key: str | None = None, now: datetime | None = None) -> Path:
    """Build and render one report; returns the page written."""
    now = now or utcnow()
    start, end = window_for(cfg, since=since, hours=hours, now=now)
    summary = build_summary(cfg, start, end, reason=reason)
    page = render(cfg, summary, now)
    write_report_state(cfg, path=page, reason=reason,
                       window=summary["window"], stop_key=stop_key)
    return page


def generate_async(cfg: Config, reason: str = REASON_MANUAL, **kwargs) -> bool:
    """Run `generate` off the poll thread; False when one is already running.

    Not a daemon thread: a report triggered by a stop has to survive the few
    seconds the loop takes to shut down.
    """
    if _BUSY.locked():
        return False

    def work() -> None:
        with _BUSY:
            try:
                generate(cfg, reason, **kwargs)
            except (OSError, ValueError, KeyError, TypeError):
                pass

    threading.Thread(target=work, name="ledger-report", daemon=False).start()
    return True


def maybe_report(cfg: Config, dispatcher, *, before: tuple[str | None, str | None],
                 control: str, stop: dict | None, now: datetime | None = None,
                 ) -> str | None:
    """The trigger site: called once per poll, never raises, never blocks.

    Two triggers. A fork that finished `done` since the previous poll and left
    a commit (or a fresh PROGRESS_REPORT.json) behind is a milestone. A stop -
    the STOP button, or the weekly goal writing stop.json - is reported once
    per stop, keyed on the record that caused it.
    """
    from .handover import FORK_TASK_ID

    try:
        state = read_report_state(cfg)
        before_status, before_started = before
        task = dispatcher.get(FORK_TASK_ID)
        if (task is not None and before_status == "running"
                and task.status == "done"):
            started = parse_iso(before_started or task.started_at)
            changed, why = repo_changed_since(cfg, started)
            if milestone_wanted(task.status, changed,
                                bool(getattr(cfg, "report_on_milestone", True))):
                if generate_async(cfg, f"{REASON_MILESTONE}: {why}", now=now):
                    return f"work-distribution report queued ({REASON_MILESTONE})"
        key = stop_key_for(cfg, control, stop)
        if stop_wanted(key, state.get("last_stop_key"),
                       bool(getattr(cfg, "report_on_stop", True))):
            if generate_async(cfg, REASON_STOP, stop_key=key, now=now):
                return f"work-distribution report queued ({REASON_STOP})"
    except Exception:  # noqa: BLE001 - a report must never take the loop down
        return None
    return None


def report_on_exit(cfg: Config) -> str | None:
    """The loop is exiting (ctrl-c / SystemExit): generate one last report.

    Synchronous on purpose - a background thread would race the interpreter
    shutting down, and the process has nothing left to block.
    """
    if not bool(getattr(cfg, "report_on_stop", True)):
        return None
    try:
        now = utcnow()
        page = generate(cfg, REASON_STOP, stop_key=f"exit:{now.isoformat()}",
                        now=now)
        return f"work-distribution report: {page}"
    except Exception:  # noqa: BLE001 - never turn a clean exit into a traceback
        return None


def open_report(cfg: Config) -> Path | None:
    """Hand reports/latest.html to the shell; None when there is none yet."""
    path = latest_report(cfg)
    if path is None:
        return None
    opener = getattr(os, "startfile", None)
    if opener is not None:
        try:
            opener(str(path))
        except OSError:
            return path
    return path


def report_status_line(cfg: Config, now: datetime | None = None) -> str:
    """The `tracker.py status` line for the work-distribution report.

    Says when the last one was written, what triggered it and where it is, so
    the monitor session can quote freshness without opening the page.
    """
    state = read_report_state(cfg)
    age = report_age(cfg, now)
    if age is None or not state.get("last_report"):
        return "report: none yet (tracker.py report --hours 12)"
    reason = str(state.get("last_reason") or "?")
    window = state.get("window") if isinstance(state.get("window"), dict) else {}
    hours = window.get("hours")
    span = f", {float(hours):g}h window" if isinstance(hours, (int, float)) else ""
    return (f"report: {age} ({reason}{span}) "
            f"-> {cfg.reports_dir / LATEST_NAME}")


def report_age(cfg: Config, now: datetime | None = None) -> str | None:
    """"report 4m ago" for the overlay, or None when none was ever written."""
    stamp = parse_iso(read_report_state(cfg).get("generated_at"))
    if stamp is None:
        return None
    seconds = max(0.0, ((now or utcnow()) - stamp).total_seconds())
    if seconds < 90:
        return "report just now"
    if seconds < 3600:
        return f"report {int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"report {int(seconds // 3600)}h ago"
    return f"report {int(seconds // 86400)}d ago"
