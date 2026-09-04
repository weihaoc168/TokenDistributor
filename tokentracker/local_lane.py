"""The local lane: what the 5090 does once the Claude buckets are spent.

The operator's rule, and the correction that produced this module:

    "loading FreeToken exhausts 31.5 GB of VRAM so it cannot run UE visual
     work; let the Fable agent instruct the proper workloads for the Qwen
     agent in that scenario"

    "The current auto tunner stop all tasks when weekly limit is reached, but
     originally in such condition FreeToken should be activated and take over
     the works"

So a bucket stop is not the end of the shift. `control.mode_for` turns it into
LOCAL-ONLY (cloud zero, local one), and this module supplies what that mode
needs: a backlog of briefs a 27B model can actually finish, the containment
header that keeps it off the GPU and off `main`, and the one task row that
carries a brief onto the local lane.

Three files and one rule:

    state/local_backlog.json   append-only list of briefs. A brief is
                               {id, title, prompt, repo, branch, acceptance,
                               staged_by, staged_at, status}; `status` walks
                               pending -> running -> done/failed and nothing is
                               ever deleted, so what the local shift did is
                               still readable next week.
    HANDOFF.md section 3       where the briefs come from: the cloud director's
                               own list of what is owed.
    Tools/verify.sh            build, auto, smoke - the CPU-only rungs. Never
                               shots, beta-warm or beta-shots: those need the
                               real RHI, and the engine holding the card has
                               ~31.5GB of the 32 in it.

WHO STAGES THE BACKLOG, and why there are two answers:

  the executive   `maybe_stage` fires on the SAME pre-exhaustion forecast that
                  fires the screenshot pass, i.e. while there is still cloud
                  budget and dispatch left to spend it. It queues one cloud
                  task on the executive model, whose whole job is to read
                  HANDOFF.md and write 3-6 briefs. That is the "Fable agent
                  instructs the Qwen agent" half of the rule, and it is the
                  good path: the director knows which of the owed items are
                  worth a local pass.

  the loop        if that model's own bucket is already exhausted, there is
                  nobody left to ask. `fallback_briefs` then parses HANDOFF.md
                  section 3 here and stages one brief per item that names no
                  GPU, PIE, editor, shots or frames. Cruder, and it is meant to
                  be: an empty backlog means the 5090 idles for a week.

Nothing in here raises. `maybe_stage` and `ensure_worker` are called from
inside the poll, `band_text` from every overlay frame.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .models import TaskSpec, parse_iso, utcnow

# state/local_backlog.json: a brief is exactly these keys when it is staged.
# `result` and `finished_at` are added when it ends, and never before.
BRIEF_KEYS = ("id", "title", "prompt", "repo", "branch", "acceptance",
              "staged_by", "staged_at", "status")
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BRIEF_STATUSES = (PENDING, RUNNING, DONE, FAILED)

# Task id prefixes. Both are matched by prefix rather than kept in a list, so a
# row that outlived a restart is still recognized off tasks.json alone.
WORKER_PREFIX = "local-"
STAGE_PREFIX = "stage-local-backlog-"
# Above the director fork's 100 so a staging pass beats the fork to the
# concurrency slot, below the snapshot's 200 so it never delays the gallery.
STAGE_PRIORITY = 150
STAGE_MAX_MINUTES = 60
# The local lane is slow (one GPU, one stream); `local_minutes_multiplier`
# stretches this again at reap time.
WORKER_PRIORITY = 140
WORKER_MAX_MINUTES = 120

# How many briefs one staging pass may ask for, and how many the fallback
# parser will take off HANDOFF.md.
MIN_BRIEFS = 3
MAX_BRIEFS = 6
# Never stage twice inside this many minutes: the forecast that triggers it is
# true for every poll of the hour before exhaustion.
STAGE_GAP_MINUTES = 120.0

DEFAULT_ACCEPTANCE = "Tools/verify.sh build, then auto, then smoke - all green"

# What disqualifies a HANDOFF item from the fallback backlog. Word-bounded
# where a substring would over-match ("copied" contains "pie"), plain
# substrings where it cannot ("beta-shots", "screenshot").
NO_LOCAL_RE = re.compile(
    r"\bgpu\b|\bpie\b|\bvram\b|shots|screenshot|editor|render|frames?\b"
    r"|beta-warm|visual|packaged build",
    re.IGNORECASE,
)
# "## 3. What is owed ..." - the section the briefs come from. Matched on the
# number alone so both hand-off dialects ("What is owed, in the order it
# should be picked up" and "What is owed next, in order") are found.
SECTION_RE = re.compile(r"^\s*#{1,4}\s*3[.)]\s", re.MULTILINE)
ITEM_RE = re.compile(r"^\s{0,3}(\d{1,2})[.)]\s+(.*)$")
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# The only branch name a brief is allowed to carry. Briefs are model-authored
# JSON, and the containment header renders this into a `git checkout -B`: a
# staged `"branch": "main"` would tell the local run to work on main while the
# same sentence forbids pushing to it.
BRANCH_RE = re.compile(r"^local/\d{4}-\d{2}-\d{2}$")
# The engine's own name, for the band: "Qwen3.8-27B-NVFP4" -> "Qwen".
ENGINE_WORD_RE = re.compile(r"^[A-Za-z]+")
BAND_HARDWARE = "on the 5090"

TITLE_CHARS = 90
RESULT_CHARS = 300


# ------------------------------------------------------------ small helpers

def _text(value: Any, limit: int = 0) -> str:
    out = value.strip() if isinstance(value, str) else ""
    return out[:limit].strip() if limit and len(out) > limit else out


def _write_atomic(path, body: str) -> None:
    """Swap the file in whole; a torn backlog reads as no backlog at all."""
    tmp = path.parent / f"{path.name}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass


def backlog_file(cfg: Config):
    path = getattr(cfg, "local_backlog_file", None)
    return path if path is not None else cfg.state_dir / "local_backlog.json"


def repo_of(cfg: Any) -> str:
    """The repo a brief works in when it does not name one of its own."""
    for key in ("local_backlog_repo", "report_repo"):
        value = _text(getattr(cfg, key, ""))
        if value:
            return value
    return "C:/Users/chenw/StarGTA"


def branch_for(cfg: Any, now: datetime | None = None) -> str:
    """`local/<YYYY-MM-DD>` on the operator's clock.

    Local rather than UTC because the operator reads this branch name beside a
    calendar; every timestamp stored in the file itself stays ISO UTC.
    """
    from .clock import fmt_local

    stamp = fmt_local(now or utcnow(), "%Y-%m-%d", cfg, fallback="")
    return f"local/{stamp or (now or utcnow()).strftime('%Y-%m-%d')}"


def brief_branch(cfg: Any, brief: Any, now: datetime | None = None) -> str:
    """The branch a brief may actually name: `local/<YYYY-MM-DD>` or nothing.

    Minted here rather than taken on trust. A brief is model-authored JSON -
    the executive writes the file and `backlog add --file` passes it through -
    so `"branch": "main"` used to render straight into the containment header
    as "Work on branch main ... (git checkout -B main from main), never push to
    main": a self-contradiction handed to a 27B model that is about to check
    out and commit. Anything that is not literally a `local/<date>` name is
    replaced with today's.
    """
    value = _text((brief or {}).get("branch") if isinstance(brief, dict) else "")
    return value if BRANCH_RE.match(value) else branch_for(cfg, now)


def brief_repo(cfg: Any, brief: Any) -> str:
    """The directory a brief may actually run in: the lane's repo, or under it.

    Same reason as the branch, with a wider blast radius: `repo` becomes the
    task row's `cwd` (`build_worker`), so an unchecked value points the local
    run at any directory on disk. A path inside the configured repo is kept -
    a brief scoped to one pod directory is exactly what the staging prompt asks
    for - and everything else falls back to the repo root.
    """
    root = repo_of(cfg)
    value = _text((brief or {}).get("repo") if isinstance(brief, dict) else "")
    if not value:
        return root
    try:
        base = Path(root).resolve()
        candidate = Path(value).resolve()
    except (OSError, ValueError):
        return root
    return value if candidate == base or base in candidate.parents else root


def engine_label(cfg: Any) -> str:
    """"Qwen on the 5090" - the band's name for what is holding the card."""
    model = _text(getattr(cfg, "local_model", "")) or "local model"
    match = ENGINE_WORD_RE.match(model)
    return f"{match.group(0) if match else model} {BAND_HARDWARE}"


# ----------------------------------------------------- state/local_backlog.json

def read_backlog(cfg: Config) -> dict[str, Any]:
    """{"briefs": [...], "last_stage_at": ...}. Never raises.

    A bare list is accepted too: that is what a hand-written file, or a
    `backlog add --file` payload copied straight in, tends to look like.
    """
    try:
        data = json.loads(backlog_file(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {"briefs": []}
    if isinstance(data, list):
        return {"briefs": [b for b in data if isinstance(b, dict)]}
    if not isinstance(data, dict):
        return {"briefs": []}
    raw = data.get("briefs")
    briefs = [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []
    return {**data, "briefs": briefs}


def write_backlog(cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    body = {**payload, "briefs": list(payload.get("briefs") or [])}
    _write_atomic(backlog_file(cfg), json.dumps(body, indent=2))
    return body


def briefs(cfg: Config) -> list[dict[str, Any]]:
    return list(read_backlog(cfg)["briefs"])


def counts(cfg: Config) -> dict[str, int]:
    """{pending, running, done, failed} over the whole append-only file."""
    out = {status: 0 for status in BRIEF_STATUSES}
    for brief in briefs(cfg):
        status = _text(brief.get("status")) or PENDING
        if status in out:
            out[status] += 1
    return out


def brief_id(now: datetime, index: int) -> str:
    return f"lb-{now.strftime('%Y%m%dT%H%M%S')}-{index + 1}"


def normalize(cfg: Config, raw: Any, index: int, staged_by: str,
              now: datetime) -> dict[str, Any] | None:
    """One incoming record as a brief, or None when it carries no work.

    The prompt is the only field with no default: a brief with nothing to do is
    not a brief, and staging it would give the local worker an empty turn.
    """
    if not isinstance(raw, dict):
        return None
    prompt = _text(raw.get("prompt")) or _text(raw.get("brief"))
    if not prompt:
        return None
    title = _text(raw.get("title"), TITLE_CHARS) or _text(prompt, TITLE_CHARS)
    status = _text(raw.get("status"))
    return {
        "id": _text(raw.get("id")) or brief_id(now, index),
        "title": title,
        "prompt": prompt,
        # Neither of these is taken on trust: see `brief_repo`/`brief_branch`.
        # A brief is written by a model, and these two fields are the ones that
        # become a working directory and a `git checkout -B`.
        "repo": brief_repo(cfg, raw),
        "branch": brief_branch(cfg, raw, now),
        "acceptance": _text(raw.get("acceptance")) or DEFAULT_ACCEPTANCE,
        "staged_by": _text(raw.get("staged_by")) or staged_by,
        "staged_at": _text(raw.get("staged_at")) or now.isoformat(),
        "status": status if status in BRIEF_STATUSES else PENDING,
    }


def add_briefs(cfg: Config, records: Any, staged_by: str = "operator",
               now: datetime | None = None) -> list[dict[str, Any]]:
    """Append briefs to the backlog; returns the ones actually written.

    Append-only in fact: existing entries are never rewritten here, and an id
    already on file is skipped rather than replaced, so a staging pass that
    runs twice cannot duplicate its own work.
    """
    now = now or utcnow()
    if isinstance(records, dict):
        records = records.get("briefs") or records.get("tasks") or [records]
    if not isinstance(records, list):
        return []
    state = read_backlog(cfg)
    existing = state["briefs"]
    known = {_text(b.get("id")) for b in existing}
    # Deduped by the WORK as well as by the id: a staging pass that ran twice,
    # or an operator re-running `backlog add --file` on the same payload,
    # writes records with no ids at all - fresh ones would be minted and the
    # local lane would do the same brief again.
    seen = {_text(b.get("prompt")) for b in existing}
    fresh: list[dict[str, Any]] = []
    for index, raw in enumerate(records[:MAX_BRIEFS * 2]):
        brief = normalize(cfg, raw, len(existing) + len(fresh), staged_by, now)
        if brief is None or brief["id"] in known or brief["prompt"] in seen:
            continue
        known.add(brief["id"])
        seen.add(brief["prompt"])
        fresh.append(brief)
    if not fresh:
        return []
    write_backlog(cfg, {**state, "briefs": existing + fresh,
                        "last_stage_at": now.isoformat()})
    return fresh


def next_pending(cfg: Config) -> dict[str, Any] | None:
    """The oldest brief still waiting, in staged order. None when there is none."""
    for brief in briefs(cfg):
        if (_text(brief.get("status")) or PENDING) == PENDING:
            return brief
    return None


def get_brief(cfg: Config, ident: Any) -> dict[str, Any] | None:
    wanted = _text(ident)
    for brief in briefs(cfg):
        if _text(brief.get("id")) == wanted:
            return brief
    return None


def set_status(cfg: Config, ident: Any, status: str,
               result: Any = None, now: datetime | None = None) -> bool:
    """Move one brief to running/done/failed, with what it reported.

    The only write that touches an existing entry, and it touches three fields:
    the list itself stays append-only.
    """
    now = now or utcnow()
    wanted = _text(ident)
    state = read_backlog(cfg)
    changed = False
    for brief in state["briefs"]:
        if _text(brief.get("id")) != wanted:
            continue
        brief["status"] = status if status in BRIEF_STATUSES else FAILED
        if result is not None:
            brief["result"] = _text(result, RESULT_CHARS)
        if status in (DONE, FAILED):
            brief["finished_at"] = now.isoformat()
        changed = True
    if changed:
        write_backlog(cfg, state)
    return changed


def clear_backlog(cfg: Config) -> int:
    """Empty the file; returns how many briefs were dropped.

    The one deletion in here, and it is the operator's own command: `backlog
    clear`. Nothing in the loop calls it.
    """
    state = read_backlog(cfg)
    dropped = len(state["briefs"])
    write_backlog(cfg, {**state, "briefs": []})
    return dropped


# ------------------------------------------------------- the local worker task

def containment_header(cfg: Config, brief: dict[str, Any],
                       now: datetime | None = None) -> str:
    """The rules the local run works under, in front of every local brief.

    Fixed text, not a template the operator edits: this is the part that keeps
    a 27B model with filesystem access off the GPU, off `main` and inside one
    repo. Every clause is a failure that would otherwise be possible:

      the branch      `local/<date>`, cut from main, never pushed to it - the
                      cloud tier has to be able to throw the whole shift away.
                      Minted here (`brief_branch`), not read off the brief: the
                      brief is model-authored and a staged `"branch": "main"`
                      would make this paragraph contradict itself.
      the rungs       build/auto/smoke only. `shots`, `beta-warm` and
                      `beta-shots` open a real RHI, and the engine already
                      holds ~31.5GB of the card's 32GB: the run would fail, or
                      take the machine down with it.
      the trailer     so `git log` says which agent wrote which commit
      LOCAL_RESULTS   the hand-back: what changed, and the ladder lines that
                      prove it, in a file the cloud tier reads first
    """
    repo = brief_repo(cfg, brief)
    branch = brief_branch(cfg, brief, now)
    model = _text(getattr(cfg, "local_model", "")) or "local model"
    acceptance = _text(brief.get("acceptance")) or DEFAULT_ACCEPTANCE
    return (
        f"Work on branch {branch} in {repo} (git checkout -B {branch} from "
        f"main), never push to main; run only Tools/verify.sh build, auto, "
        f"smoke; never run shots, beta-warm, beta-shots or open the editor; "
        f"commit by explicit paths with the trailer 'Co-Authored-By: {model} "
        f"(local) <noreply@local>'; when done, write dev_JSON/LOCAL_RESULTS.md "
        f"with what changed and the ladder lines. "
        f"Acceptance for this brief: {acceptance}."
    )


def compose_prompt(cfg: Config, brief: dict[str, Any],
                   now: datetime | None = None) -> str:
    """preamble + containment header + the brief, in that order.

    The preamble is the operator's standing VRAM rule (config.json's
    `local_prompt_preamble`), the header is this task's containment, the brief
    is the work. `Dispatcher._task_prompt` will not prepend the preamble a
    second time - it checks for it - so the row can carry the whole text and
    the composition stays assertable in one place.
    """
    parts = [_text(getattr(cfg, "local_prompt_preamble", "")),
             containment_header(cfg, brief, now),
             _text(brief.get("prompt"))]
    return "\n\n".join(part for part in parts if part)


def worker_task_id(brief: dict[str, Any]) -> str:
    return f"{WORKER_PREFIX}{_text(brief.get('id')) or 'brief'}"


def is_worker(task_id: Any) -> bool:
    return str(task_id or "").startswith(WORKER_PREFIX)


def brief_of_task(task_id: Any) -> str:
    """The brief id a local worker row was built for, or ""."""
    text = str(task_id or "")
    return text[len(WORKER_PREFIX):] if is_worker(text) else ""


def build_worker(cfg: Config, brief: dict[str, Any],
                 now: datetime | None = None) -> TaskSpec:
    """The task row that carries one brief onto the local lane.

    `lane_pref="local"` is the whole point of that field: the row must never be
    picked up by a cloud launch batch when the week rolls over and the budget
    comes back, because its prompt is written for the local engine's rules -
    and, in LOCAL-ONLY, it is what `dispatch.built_for_local` requires before a
    row may go to the 27B at all. Priced at $0 by its model, which the pricing
    table carries at zero.

    `cwd` goes through `brief_repo` rather than off the record: a hand-edited
    backlog file, or a brief a model wrote with a `repo` outside the tree, must
    not choose the directory the local run works in.
    """
    return TaskSpec(
        id=worker_task_id(brief),
        prompt=compose_prompt(cfg, brief, now),
        cwd=brief_repo(cfg, brief),
        weight="heavy",
        model=_text(getattr(cfg, "local_model", "")) or None,
        priority=WORKER_PRIORITY,
        max_minutes=WORKER_MAX_MINUTES,
        lane_pref="local",
    )


def current_worker(dispatcher: Any) -> Any:
    """The local worker row holding the lane right now, or None."""
    try:
        tasks = dispatcher.tasks()
    except (AttributeError, TypeError):
        return None
    for task in tasks:
        if is_worker(getattr(task, "id", "")) and \
                getattr(task, "status", "") in ("pending", "running"):
            return task
    return None


def ensure_worker(cfg: Config, dispatcher: Any,
                  now: datetime | None = None) -> str | None:
    """Queue the next backlog brief on the local lane. The line to log, or None.

    One at a time: while a local worker row is pending or running the lane is
    taken, and the next brief waits. Called from `cli._tick` only in LOCAL-ONLY
    mode, so the cloud coming back stops new briefs from being queued - which
    is exactly what "the lane drains" means.

    Never raises: it runs inside the poll.
    """
    now = now or utcnow()
    try:
        if not bool(getattr(cfg, "local_enabled", False)):
            return None
        if current_worker(dispatcher) is not None:
            return None
        brief = next_pending(cfg)
        if brief is None:
            return None
        task = build_worker(cfg, brief, now)
        if dispatcher.get(task.id) is not None:
            # A row for this brief already ended (done/failed/killed) and the
            # brief was never closed out; close it rather than launching twice.
            set_status(cfg, brief["id"], FAILED,
                       "a task row for this brief already exists", now)
            return None
        dispatcher.add(task)
        set_status(cfg, brief["id"], RUNNING, now=now)
        return (f"local backlog: queued {task.id} "
                f"({_text(brief.get('title'), 60)}) on "
                f"{getattr(cfg, 'local_model', '?')}")
    except Exception:  # pragma: no cover - the poll must survive anything
        return None


def finish_task(cfg: Config, task: Any, result: Any = None,
                now: datetime | None = None) -> str | None:
    """Close the brief a finished local worker row belongs to. Never raises."""
    try:
        ident = brief_of_task(getattr(task, "id", ""))
        if not ident or get_brief(cfg, ident) is None:
            return None
        status = DONE if getattr(task, "status", "") == "done" else FAILED
        summary = _text(result, RESULT_CHARS) or _text(
            getattr(task, "error", ""), RESULT_CHARS)
        if not set_status(cfg, ident, status, summary or None, now):
            return None
        return f"local backlog: {ident} {status}"
    except Exception:  # pragma: no cover
        return None


# ---------------------------------------------------------------- staging

STAGE_PROMPT = (
    "STAGE THE LOCAL BACKLOG. TokenDistributor forecasts that a Claude budget "
    "bucket is about to be exhausted. When it is, the only agent left running "
    "is a 27B model on one RTX 5090 (FreeToken engine, ~31.5GB of the card's "
    "32GB), with no Unreal editor, no PIE and no real-RHI rung available to "
    "it. Your job is to leave it work it can actually finish.\n\n"
    "Read C:/Users/chenw/StarGTA/dev_JSON/HANDOFF.md section 3 (what is owed, "
    "in order) and dev_JSON/PROGRESS_REPORT.json. Write 3-6 self-contained "
    "briefs into state/local_backlog.json by writing a JSON file of the shape "
    '{{"briefs": [{{"title": ..., "prompt": ..., "repo": ..., '
    '"acceptance": ...}}]}} and running\n'
    "    python {root}/tracker.py backlog add --file <that file>\n\n"
    "Every brief must be: CPU-only (reading and editing code, command-line "
    "builds, triaging saved logs, tests, docs); scoped to ONE pod directory, "
    "named in the prompt; self-contained, so the local model needs no context "
    "from this session beyond what the brief says; and accepted by headless "
    "rungs only - Tools/verify.sh build, then auto, then smoke. Never give it "
    "a real-RHI rung (shots, beta-warm, beta-shots), a PIE session, or "
    "anything that opens the editor: name that work as deferred instead. "
    "Report the brief ids you wrote."
)


def stage_task_id(now: datetime) -> str:
    return f"{STAGE_PREFIX}{now.strftime('%Y%m%dT%H%M')}"


def is_stage(task_id: Any) -> bool:
    return str(task_id or "").startswith(STAGE_PREFIX)


def stage_prompt(cfg: Config) -> str:
    return STAGE_PROMPT.format(root=str(getattr(cfg, "root", ".")).replace("\\", "/"))


def executive_model(cfg: Config) -> str | None:
    """The ALLOCATED executive tier's model - the Fable agent, by design.

    The staging pass is the "let the Fable agent instruct the Qwen agent" half
    of the operator's rule, so it goes out on the executive tier rather than on
    the workers'. Its fallback is tried when the tier has one.
    """
    from .allocator import allocate
    from .graph import EXECUTIVE, read_graph

    try:
        tier = allocate(cfg).graph.get(EXECUTIVE, {})
    except Exception:
        tier = {}
    for source in (tier, read_graph(cfg).get(EXECUTIVE, {})):
        for key in ("model", "fallback"):
            model = _text(source.get(key) if isinstance(source, dict) else "")
            if model:
                return model
    return _text(getattr(cfg, "throttle_model", "")) or None


def cloud_can_run(cfg: Config) -> bool:
    """Whether a cloud task queued now could actually start.

    The staging pass is only worth asking the executive for while the cloud
    lane is open. Once dispatch is stopped - which is the very moment the local
    lane needs a backlog - a queued cloud row would sit `pending` until the
    week rolled over, and the 5090 would idle through exactly the days it was
    meant to cover. So a stopped switch reads the same as a spent bucket: the
    loop parses HANDOFF.md itself.
    """
    from .control import STOPPED, read_record

    try:
        return read_record(cfg)["dispatch"] != STOPPED
    except Exception:
        return True


def executive_available(cfg: Config, buckets: Any) -> bool:
    """Whether the executive's own bucket can still pay for a staging run.

    The Fable window is what the executive spends, so a Fable row already at or
    past its hard stop means asking that model to write the briefs would only
    produce a usage-limit exit - and the backlog would stay empty for the whole
    week the local lane is meant to cover. That is when the loop parses
    HANDOFF.md itself.
    """
    from .allocator import FABLE, _finite, _row

    row = (buckets or {}).get(FABLE) if isinstance(buckets, dict) else None
    if row is None:
        return True
    util = _finite(_row(row, "utilization"), float("nan"))
    stop = _finite(_row(row, "stop"), float("nan"))
    if util != util or stop != stop:  # NaN: no usable reading, assume it can
        return True
    return util < stop


def forecast_due(cfg: Config, buckets: Any, now: datetime) -> str:
    """The pre-exhaustion trigger, in the words `snapshot` fires it with.

    The same forecast and the same lead time as the screenshot pass, read off
    the allocator's own bucket rows: the two policies fire together because
    they answer the same question - the budget is nearly gone, what has to
    happen before it is?
    """
    from .snapshot import forecast_hits, lead_minutes

    at, bucket = forecast_hits(cfg, buckets, now)
    if at is None:
        return ""
    minutes = (at - now).total_seconds() / 60.0
    if minutes > lead_minutes(cfg):
        return ""
    return f"{bucket} reaches its stop in {max(minutes, 0.0):.0f} min"


def _staged_recently(state: dict[str, Any], now: datetime) -> bool:
    last = parse_iso(state.get("last_stage_at"))
    if last is None:
        return False
    return now - last < timedelta(minutes=STAGE_GAP_MINUTES)


def maybe_stage(cfg: Config, dispatcher: Any, buckets: Any,
                now: datetime | None = None) -> str | None:
    """Stage the local backlog when the forecast says the cloud is nearly out.

    Returns the line the loop logs, or None. Never raises: it is called from
    inside the tick, right after the snapshot policy, off the same buckets.

    Held back by four things, in this order: the lane being switched off, a
    backlog that already has work waiting, a staging pass already queued or
    running, and one having run inside `STAGE_GAP_MINUTES` - because the
    forecast is true for every poll of the hour before exhaustion.
    """
    now = now or utcnow()
    try:
        if not bool(getattr(cfg, "local_enabled", False)):
            return None
        reason = forecast_due(cfg, buckets, now)
        if not reason:
            return None
        state = read_backlog(cfg)
        if any((_text(b.get("status")) or PENDING) in (PENDING, RUNNING)
               for b in state["briefs"]):
            return None
        for task in dispatcher.tasks():
            if is_stage(getattr(task, "id", "")) and \
                    getattr(task, "status", "") in ("pending", "running"):
                return None
        if _staged_recently(state, now):
            return None
        if cloud_can_run(cfg) and executive_available(cfg, buckets):
            return _stage_via_executive(cfg, dispatcher, reason, now)
        return _stage_fallback(cfg, reason, now)
    except Exception:  # pragma: no cover - the poll must survive anything
        return None


def _stage_via_executive(cfg: Config, dispatcher: Any, reason: str,
                         now: datetime) -> str | None:
    task = TaskSpec(
        id=stage_task_id(now),
        prompt=stage_prompt(cfg),
        cwd=repo_of(cfg),
        weight="light",
        model=executive_model(cfg),
        priority=STAGE_PRIORITY,
        max_minutes=STAGE_MAX_MINUTES,
        # Cloud only: a 27B model writing its own briefs off a hand-off it
        # cannot fit in context is the failure this whole path avoids.
        lane_pref="cloud",
    )
    if dispatcher.get(task.id) is not None:
        return None
    dispatcher.add(task)
    write_backlog(cfg, {**read_backlog(cfg), "last_stage_at": now.isoformat()})
    return (f"local backlog: staging queued ({reason}): {task.id} on "
            f"{task.model or '(account default)'}")


def _stage_fallback(cfg: Config, reason: str, now: datetime) -> str | None:
    """Stage from HANDOFF.md ourselves, because the executive cannot run."""
    items = fallback_briefs(cfg, handoff_text(cfg))
    if not items:
        # Stamped anyway, so this line is said once every STAGE_GAP_MINUTES
        # rather than on every poll of the hour before exhaustion.
        write_backlog(cfg, {**read_backlog(cfg),
                            "last_stage_at": now.isoformat()})
        return (f"local backlog: no cloud model can stage it ({reason}) and "
                f"HANDOFF.md section 3 offered no CPU-only item")
    added = add_briefs(cfg, items, staged_by="allocator-fallback", now=now)
    if not added:
        return None
    return (f"local backlog: no cloud model can stage it ({reason}); staged "
            f"{len(added)} brief(s) from HANDOFF.md section 3")


# ----------------------------------------------- HANDOFF.md section 3 fallback

def handoff_path(cfg: Config):
    return Path(repo_of(cfg)) / "dev_JSON" / "HANDOFF.md"


def handoff_text(cfg: Config) -> str:
    try:
        return handoff_path(cfg).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def section_three(text: Any) -> str:
    """The LAST "## 3." section of the hand-off, body only.

    The last, because HANDOFF.md is append-only: every run adds its own dated
    hand-off with its own section 3, and the newest one is the list of what is
    owed *now*.
    """
    body = text if isinstance(text, str) else ""
    starts = [m.end() for m in SECTION_RE.finditer(body)]
    if not starts:
        return ""
    chunk = body[starts[-1]:]
    stop = re.search(r"^\s*#{1,4}\s", chunk, re.MULTILINE)
    return chunk[:stop.start()] if stop else chunk


def handoff_items(text: Any) -> list[str]:
    """Section 3's numbered items, one string each, continuations folded in."""
    items: list[str] = []
    current: list[str] = []
    for line in section_three(text).splitlines():
        match = ITEM_RE.match(line)
        if match:
            if current:
                items.append(" ".join(current).strip())
            current = [match.group(2).strip()]
        elif current:
            stripped = line.strip()
            if stripped:
                current.append(stripped)
    if current:
        items.append(" ".join(current).strip())
    return [item for item in items if item]


def item_title(item: str) -> str:
    """The item's own bold lead, or its first sentence."""
    bold = BOLD_RE.search(item)
    text = bold.group(1) if bold else item.split(". ")[0]
    # Backticks are dropped rather than only trimmed: a hand-off title is as
    # likely to be "`docs/LICENSES.md` needs a row" as "**GATE**", and the
    # ticks would survive into the panel's band.
    return _text(re.sub(r"\s+", " ", text.replace("`", "")).strip(" .*"),
                 TITLE_CHARS)


def local_safe(item: Any) -> bool:
    """Whether one hand-off item is work a GPU-less local model may take.

    Rejected on a single mention of the GPU, PIE, the editor, shots, frames or
    a packaged build: the cost of wrongly excluding a doable item is one idle
    brief, and the cost of wrongly including one is a 27B model trying to open
    the editor on a card that has 0.5GB free.
    """
    return bool(_text(item)) and NO_LOCAL_RE.search(str(item)) is None


def fallback_briefs(cfg: Config, text: Any,
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """One brief per CPU-only section-3 item, capped at MAX_BRIEFS."""
    now = now or utcnow()
    repo = repo_of(cfg)
    out: list[dict[str, Any]] = []
    for item in handoff_items(text):
        if not local_safe(item):
            continue
        out.append({
            "title": item_title(item) or "HANDOFF item",
            "prompt": (
                f"From {repo}/dev_JSON/HANDOFF.md section 3 (what is owed), "
                f"item: {item}\n\n"
                f"Do the CPU-only part of this item in {repo}. Read the files "
                f"it names first. Keep the change small, correct and "
                f"reviewable, scoped to one pod directory. Anything that "
                f"needs the editor, PIE, rendering or visual verification is "
                f"NOT yours: append it to DEFERRED.md in the repo root with "
                f"exactly what the cloud tier must check."),
            "repo": repo,
            "acceptance": DEFAULT_ACCEPTANCE,
        })
        if len(out) >= MAX_BRIEFS:
            break
    return out


# ------------------------------------------------------------------- display

def band_text(cfg: Config, reason: str, until: Any = None,
              short: bool = False, dispatcher: Any = None) -> str:
    """The panel's band while the local lane has the shift.

    "LOCAL-ONLY: fable bucket 97% - Qwen on the 5090 - resumes cloud Fri 07:00
    CT - <the brief it is on>". Deliberately not a red STOPPED sentence: work
    is happening, and the band's job is to say whose and until when.
    """
    from .control import BUCKET_LABEL, threshold

    label = BUCKET_LABEL.get(reason, reason)
    limit = threshold(cfg, reason)
    pct = f"{limit:.0%}" if limit is not None else "?"
    if short:
        return f"LOCAL-ONLY {label} {pct}"
    tail = ""
    if isinstance(until, datetime):
        from .clock import fmt_local

        tail = " - resumes cloud " + fmt_local(until, "%a %H:%M", cfg,
                                               with_label=True)
    title = current_title(cfg, dispatcher)
    work = f" - {title}" if title else ""
    return (f"LOCAL-ONLY: {label} bucket {pct} - {engine_label(cfg)}"
            f"{tail}{work}")


def current_title(cfg: Config, dispatcher: Any = None) -> str:
    """The title of the brief the local lane is on, or "". Never raises."""
    try:
        for brief in briefs(cfg):
            if _text(brief.get("status")) == RUNNING:
                return _text(brief.get("title"), 60)
    except Exception:
        return ""
    return ""


def gpu_memory() -> str:
    """"31.5/32.0 GB" off nvidia-smi, or "" when it is not there.

    Best effort and never fatal: the panel and `status` both call it, and a box
    without the NVIDIA tools has to print the rest of the line anyway.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    line = next((ln for ln in out.splitlines() if ln.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return ""
    try:
        used, total = float(parts[0]), float(parts[1])
    except ValueError:
        return ""
    return f"{used / 1024:.1f}/{total / 1024:.1f} GB"


def status_lines(cfg: Config, dispatcher: Any = None,
                 engine_up: Any = None) -> list[str]:
    """What `tracker.py status` prints about the local lane.

    Two lines: the engine (model, endpoint, daemon up/down, VRAM when
    nvidia-smi answers) and the backlog (the brief in flight, and the tallies).
    """
    if not bool(getattr(cfg, "local_enabled", False)):
        return []
    if engine_up is None and dispatcher is not None:
        try:
            engine_up = dispatcher.local_engine_up()
        except Exception:
            engine_up = None
    state = "up" if engine_up else ("down" if engine_up is not None else "?")
    vram = gpu_memory()
    head = (f"local lane: {getattr(cfg, 'local_model', '?')} via "
            f"{getattr(cfg, 'local_base_url', '?')} - daemon {state}"
            + (f", VRAM {vram}" if vram else ""))
    tally = counts(cfg)
    title = current_title(cfg)
    now_on = f"on '{title}'" if title else "idle"
    return [head,
            f"local backlog: {now_on} - {tally[PENDING]} pending, "
            f"{tally[RUNNING]} running, {tally[DONE]} done, "
            f"{tally[FAILED]} failed"]
