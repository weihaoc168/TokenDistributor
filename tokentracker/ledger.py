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

The bill is the other figure, and it is a separate axis: `cost_usd` prices the
same usage at the published per-model list prices `pricing.py` resolves, and
the summary's `cost` block breaks that USD out by model, tier, source, role,
category, hour and commit. A model the price table does not name is `unpriced`
- None, never a guess - and the page greys it and names it in a caveat.

The bill has five terms, not four, because cache writes have two prices: the
usage records split `cache_creation` into 5-minute and 1-hour writes, and the
published table bills the 1-hour ones at 2x base input against the 5-minute
ones' 1.25x. On this machine's own traffic the 1-hour share runs 10-40% of
creation depending on the model, so folding the two together is not a rounding
choice - it is a systematic understatement of every dollar figure downstream.

Nothing here raises into the run loop: `maybe_report` swallows everything, and
generation happens on a background thread so a poll is never blocked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .models import parse_iso, utcnow
from .pricing import PER_TOKENS, PRICE_FIELDS, is_priced, price_for

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

# `cache_creation_1h_input_tokens` is not a key the API writes: it is the
# `usage.cache_creation.ephemeral_1h_input_tokens` sub-count lifted flat, and it
# is a SUBSET of cache_creation_input_tokens (creation = 5m + 1h in every record
# on this machine). Every token figure on the page still reads the total; only
# the bill splits it, because the two durations are two prices.
CREATION_KEY = "cache_creation_input_tokens"
CREATION_1H_KEY = "cache_creation_1h_input_tokens"
USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_read_input_tokens", CREATION_KEY, CREATION_1H_KEY)
# Where the API actually records the 1-hour share.
CREATION_BLOCK = "cache_creation"
CREATION_1H_FIELD = "ephemeral_1h_input_tokens"
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
# The role every session and agent row is stamped with. It is the identity of
# the transcript, not of the model: which hat was being worn.
ROLE_STAMPS = (MAIN_SOURCE, FORK_SOURCE, AGENT_SOURCE)
# The panel's three tiers (state/tiers.json), which are the agentic graph's,
# not the report's two. The advisory tier has no transcript of its own: the
# review lenses are Workflow agents whose brief names them, so they are split
# out of the worker lanes by the lane role their own brief declares.
PANEL_EXEC = "executive"
PANEL_ADVISORY = "advisory"
PANEL_WORKERS = "workers"
PANEL_TIERS = (PANEL_EXEC, PANEL_ADVISORY, PANEL_WORKERS)
ADVISORY_ROLES = ("reviewer", "judge", "synthesis")
# state/tiers.json + state/ledger_cache.json.
TIERS_KEYS = ("window", "tiers", "generated_at")
TIER_FIELDS = ("input", "output", "sessions")
TIERS_REFRESH_SECONDS = 300
# The input side of the panel's bars is everything that was read: fresh input,
# what was written into the cache, and what was read back out of it.
INPUT_KEYS = ("input_tokens", CREATION_KEY, "cache_read_input_tokens")
CACHE_VERSION = 1
# A transcript with more turns than this is tallied but not cached: the cache
# entry carries the message ids it counted (that is what keeps the cross-file
# dedup exact when an entry is reused), and there is a size past which keeping
# them costs more than re-reading the file.
CACHE_MAX_IDS = 20000
SERIES_MAX_POINTS = 60
LEDGER_ROWS = 12
COST_ROWS = 12
# The price fields cost_usd adds up, in the order the page stacks them. The
# token count behind each one comes from `billed_tokens`, not from a flat key:
# the two cache-write durations share one usage counter and have to be split.
COST_FIELDS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")
# The roles a lane's label can name. `director` is the fallback for a session
# (main or fork), `worker` for a Workflow agent, so every dollar lands in one.
ROLE_DIRECTOR = "director"
ROLE_WORKER = "worker"
ROLES = (ROLE_WORKER, "reviewer", "judge", "synthesis", "author", "verify",
         ROLE_DIRECTOR)
# The words each role answers to; the first role that matches wins, so the more
# specific lanes are asked about first.
ROLE_WORDS = (
    ("judge", ("judge",)),
    ("synthesis", ("synthesis", "synthesiser", "synthesizer")),
    ("reviewer", ("reviewer", "review")),
    # The fix-and-re-run lane declares itself "the fixer" as often as "the
    # verifier"; it is the same lane and the same dollars.
    ("verify", ("verifier", "verify", "verification", "fixer")),
    ("author", ("author", "writer")),
    (ROLE_WORKER, ("implementer", "worker", "builder")),
    (ROLE_DIRECTOR, ("director", "orchestrator", "dispatcher")),
)
# Only a *declaration* counts, never a passing mention: "You are the reviewer",
# a line opening "Implementer:", "the verify lane", or a label that is just the
# word. Every brief here mentions half of these in prose - a worker brief says
# "verify results with Tools/verify.sh", a review brief quotes the
# implementer's task - and matching bare occurrences filed the entire worker
# tier under `verify`.
ROLE_RE = {
    role: re.compile(
        # "you are the adversarial reviewer", "you are the implementer": up to
        # three adjectives may sit between the article and the role word.
        r"you are (?:the |a |an )?(?:[\w-]+[ \t]+){{0,3}}(?:{w})\b"
        r"|(?:^|\n)[ \t>*-]*(?:{w})[ \t]*[:—-]"
        r"|\b(?:{w})[ \t]+(?:lane|lens|agent|pass)\b".format(
            w="|".join(re.escape(word) for word in words)),
        re.IGNORECASE)
    for role, words in ROLE_WORDS
}
BRIEF_SCAN_CHARS = 4000
FORK_PROBE_CHARS = 60
REASON_MILESTONE = "fork milestone"
REASON_STOP = "stopped"
REASON_MANUAL = "manual"
REPORT_KEYS = ("last_report", "last_reason", "generated_at", "window",
               "last_stop_key")

_BUSY = threading.Lock()
# Separate from _BUSY: the tier-share refresh and a report are two different
# jobs, and one running must not silently skip the other.
_TIERS_BUSY = threading.Lock()


# --------------------------------------------------------------- primitives

def new_usage() -> dict[str, int]:
    return {k: 0 for k in USAGE_KEYS}


def _count(value: Any) -> int | None:
    """A token count, or None when the field is absent or is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def add_usage(dst: dict[str, int], src: Any) -> None:
    """Fold one `message.usage` record into a running total.

    The 1-hour cache-write share is the one key the API does not write flat: it
    lives in `usage.cache_creation.ephemeral_1h_input_tokens`. It is read from
    there, and from the flat `cache_creation_1h_input_tokens` too, so a
    hand-built usage dict works either way. The flat key wins when both are
    present, which is what lets a caller state the split outright; the nested
    block is only consulted when nothing flat named it.
    """
    if not isinstance(src, dict):
        return
    for key in USAGE_KEYS:
        value = _count(src.get(key))
        if value is not None:
            dst[key] += value
    if _count(src.get(CREATION_1H_KEY)) is not None:
        return
    block = src.get(CREATION_BLOCK)
    if isinstance(block, dict):
        nested = _count(block.get(CREATION_1H_FIELD))
        if nested is not None:
            dst[CREATION_1H_KEY] += nested


def weighted(usage: dict[str, int]) -> float:
    return (usage["output_tokens"]
            + 0.1 * (usage["input_tokens"] + usage["cache_creation_input_tokens"])
            + 0.01 * usage["cache_read_input_tokens"])


def billed_tokens(usage: Any) -> dict[str, int]:
    """{price field: tokens billed at it} for one usage record.

    Four of the five fall straight out of the usage counters. The fifth is the
    reason this function exists: `cache_creation_input_tokens` is ONE counter
    covering TWO prices, 5-minute writes at 1.25x base input and 1-hour writes
    at 2x, and the record carries the 1-hour share separately. So the 1-hour
    count is billed at its own rate and the 5-minute line is what is left.

    The subtraction is clamped at zero and the 1-hour share at the total: a
    record whose sub-counts disagree with its total (nothing on disk does, but
    a hand-edited one could) must never produce a negative token count or bill
    more cache writes than were created.
    """
    src = usage if isinstance(usage, dict) else {}

    def get(key: str) -> int:
        value = _count(src.get(key))
        return max(0, value) if value is not None else 0

    creation = get(CREATION_KEY)
    hour = min(get(CREATION_1H_KEY), creation)
    return {
        "input": get("input_tokens"),
        "output": get("output_tokens"),
        "cache_write": creation - hour,
        "cache_write_1h": hour,
        "cache_read": get("cache_read_input_tokens"),
    }


def cost_usd(usage: dict[str, int], price: Any) -> float | None:
    """What this usage costs at `price`, in USD. None when it is not priced.

    Every published price is USD per 1M tokens, so each billed token count is
    charged at its own rate:

        input/1e6*input + output/1e6*output
        + cache_5m/1e6*cache_write + cache_1h/1e6*cache_write_1h
        + cache_read/1e6*cache_read

    where cache_5m + cache_1h is the record's whole cache_creation figure. See
    `billed_tokens` for why the cache writes are two lines and not one.

    None - not zero - is the answer for a model the price table does not name.
    Zero would be a claim (this model was free); None is the truth (nobody
    said), and it is what makes the page render `unpriced` instead of a
    plausible-looking dollar figure nobody can source.
    """
    if not is_priced(price):
        return None
    tokens = billed_tokens(usage)
    return sum(tokens[field] / PER_TOKENS * float(price[field])
               for field in COST_FIELDS)


def cost_components(usage: dict[str, int], price: Any) -> dict[str, float] | None:
    """cost_usd, split into the five lines it adds up from; None when unpriced."""
    if not is_priced(price):
        return None
    tokens = billed_tokens(usage)
    return {field: tokens[field] / PER_TOKENS * float(price[field])
            for field in COST_FIELDS}


def _add_usd(current: float | None, value: float | None) -> float | None:
    """Sum two USD figures where None means "unpriced", and stays unpriced.

    Sticky by design: a total that silently dropped an unpriced lane would read
    as a complete bill. Totals that are *meant* to cover only the priced part
    (the page's headline) skip the Nones explicitly, in `_usd_total`.
    """
    if current is None or value is None:
        return None
    return current + value


def _usd_total(values: Iterable[float | None]) -> float:
    """The priced part of a set of figures; unpriced lanes contribute nothing.

    Paired everywhere with `priced_share`, which says how much of the window
    this total actually covers.
    """
    return sum(v for v in values if isinstance(v, (int, float)))


def _round_usd(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


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
    """Per-model, per-category totals, the hourly histogram, and the USD bill.

    A Tally is built against a price table: every row, category cell and hour
    bucket carries `cost_usd` alongside its tokens, and a model the table does
    not price carries None there for the life of the tally. Built without a
    table (the default), nothing is priced - which is what a caller that only
    wants token counts gets, unchanged.
    """

    def __init__(self, prices: Any = None, default: Any = None) -> None:
        self.by_model: dict[str, dict[str, Any]] = {}
        self.hourly: dict[str, dict[str, dict[str, Any]]] = {}
        self.prices: dict[str, Any] = prices if isinstance(prices, dict) else {}
        self.default: dict[str, Any] | None = default if is_priced(default) else None
        # (timestamp, model, usd) per turn, for the per-commit buckets. Kept
        # here rather than recomputed because the milestone windows are minutes
        # wide, far finer than the hourly histogram.
        self.events: list[tuple[datetime, str, float | None]] = []

    def price_of(self, model: str) -> dict[str, Any] | None:
        return price_for(self.prices, model, self.default)

    def _model(self, model: str) -> dict[str, Any]:
        row = self.by_model.get(model)
        if row is None:
            priced = self.price_of(model) is not None
            row = {"messages": 0, "usage": new_usage(), "weighted": 0.0,
                   "cats": {}, "cost_usd": 0.0 if priced else None,
                   "unpriced": not priced}
            self.by_model[model] = row
        return row

    def _cell(self, row: dict[str, Any], cat: str) -> dict[str, Any]:
        return row["cats"].setdefault(cat, {
            "messages": 0, "output_tokens": 0, "weighted": 0.0,
            "cost_usd": None if row["unpriced"] else 0.0})

    def _bucket(self, model: str, hour: str, unpriced: bool) -> dict[str, Any]:
        return self.hourly.setdefault(model, {}).setdefault(hour, {
            "output_tokens": 0, "messages": 0,
            "cost_usd": None if unpriced else 0.0})

    def add_turn(self, model: str, cat: str, usage: dict[str, int],
                 ts: datetime | None) -> None:
        row = self._model(model)
        row["messages"] += 1
        for key in USAGE_KEYS:
            # `.get`, not `[]`: a caller may hand in a usage dict built before
            # the 1-hour cache-write sub-count existed, and a missing sub-count
            # is zero of it, not a KeyError mid-report.
            row["usage"][key] += _count(usage.get(key)) or 0
        cost = weighted(usage)
        row["weighted"] += cost
        usd = cost_usd(usage, self.price_of(model))
        row["cost_usd"] = _add_usd(row["cost_usd"], usd)
        cell = self._cell(row, cat)
        cell["messages"] += 1
        cell["output_tokens"] += usage["output_tokens"]
        cell["weighted"] += cost
        cell["cost_usd"] = _add_usd(cell["cost_usd"], usd)
        if ts is not None:
            hour = ts.strftime("%Y-%m-%dT%H:00Z")
            bucket = self._bucket(model, hour, row["unpriced"])
            bucket["output_tokens"] += usage["output_tokens"]
            bucket["messages"] += 1
            bucket["cost_usd"] = _add_usd(bucket["cost_usd"], usd)
            self.events.append((ts, model, usd))

    def merge(self, other: "Tally") -> None:
        for model, row in other.by_model.items():
            mine = self._model(model)
            mine["messages"] += row["messages"]
            mine["weighted"] += row["weighted"]
            mine["cost_usd"] = _add_usd(mine["cost_usd"], row.get("cost_usd"))
            mine["unpriced"] = mine["unpriced"] or bool(row.get("unpriced"))
            for key in USAGE_KEYS:
                mine["usage"][key] += row["usage"][key]
            for cat, cell in row["cats"].items():
                dst = self._cell(mine, cat)
                dst["messages"] += cell["messages"]
                dst["output_tokens"] += cell["output_tokens"]
                dst["weighted"] += cell["weighted"]
                dst["cost_usd"] = _add_usd(dst["cost_usd"], cell.get("cost_usd"))
        for model, hours in other.hourly.items():
            unpriced = bool(self.by_model.get(model, {}).get("unpriced", True))
            for hour, bucket in hours.items():
                dst = self._bucket(model, hour, unpriced)
                dst["output_tokens"] += bucket["output_tokens"]
                dst["messages"] += bucket["messages"]
                dst["cost_usd"] = _add_usd(dst["cost_usd"], bucket.get("cost_usd"))
        self.events.extend(other.events)

    def totals(self) -> tuple[int, float]:
        out = sum(r["usage"]["output_tokens"] for r in self.by_model.values())
        cost = sum(r["weighted"] for r in self.by_model.values())
        return out, cost

    def usd(self) -> float:
        """The priced part of this tally's bill; see `_usd_total`."""
        return _usd_total(r.get("cost_usd") for r in self.by_model.values())

    def priced_weighted(self) -> tuple[float, float]:
        """(weighted cost that had a price, weighted cost in total)."""
        priced = sum(r["weighted"] for r in self.by_model.values()
                     if not r.get("unpriced"))
        return priced, sum(r["weighted"] for r in self.by_model.values())

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
                     seen: set[str] | None = None, prices: Any = None,
                     default: Any = None) -> Tally:
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

    `prices` is the table the returned Tally bills its turns at; without one
    every model comes back unpriced and only the token figures are filled in.
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

    tally = Tally(prices, default)
    for turn in turns.values():
        tally.add_turn(turn["model"], categorise(turn["tools"]),
                       turn["usage"], turn["ts"])
    return tally


# ------------------------------------------------------------------- roles

def role_of(label: Any, source: str | None = None) -> str:
    """Which lane a label names: worker, reviewer, judge, ... or director.

    The label is whatever names the lane - a Workflow agent's brief, a
    session's own row - and only a *declaration* in it counts: the label is
    the role word itself, or the text says "you are the reviewer", opens a
    line with "Implementer:", or speaks of "the judge lane". A brief that
    merely mentions the word in passing is not that lane, which is the whole
    difference between a useful split and one that files every worker under
    whichever role word its instructions happened to use.

    Nothing declared falls back to the source's own meaning: a main or fork
    session is the director, anything else is a worker, so every dollar lands
    in exactly one role and `by_role` adds up to the priced total.
    """
    text = str(label or "").strip()
    bare = text.lower().strip(" .:-")
    for role, words in ROLE_WORDS:
        if bare in words or bare == role:
            return role
    for role, _words in ROLE_WORDS:
        if ROLE_RE[role].search(text):
            return role
    return ROLE_DIRECTOR if source in (MAIN_SOURCE, FORK_SOURCE) else ROLE_WORKER


def first_user_text(path: Path, limit: int = BRIEF_SCAN_CHARS) -> str:
    """The first user message in a transcript: an agent's brief. "" when none.

    Read straight off disk and truncated, because it is only ever used to
    label a lane - the whole brief can be tens of kilobytes.
    """
    for entry in _iter_entries(path):
        if entry.get("type") != "user":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content[:limit]
        if isinstance(content, list):
            parts = [str(block.get("text") or "") for block in content
                     if isinstance(block, dict) and block.get("type") != "tool_result"]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined[:limit]
    return ""


def agent_role(path: Path) -> str:
    """The role a Workflow agent's own brief names; `worker` when it names none.

    A Workflow agent is never the director, whatever its brief says: the brief
    is written *by* the director and routinely quotes the standing director
    instructions, which is what a `director` match in one almost always is.
    """
    role = role_of(first_user_text(path), AGENT_SOURCE)
    return ROLE_WORKER if role == ROLE_DIRECTOR else role


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


def fork_models(cfg: Config) -> dict[str, str]:
    """{fork session id: the model that fork actually ran on}.

    Two records say it and they are merged rather than ranked: the task row
    (`model_used`, stamped at launch) and state/handover.log (every start and
    finish the dispatcher ever wrote). Neither is complete on its own - a row
    can be deleted, and the log only learns the session id at the finish - and
    both are the model that RAN, which is what a role stamp has to carry: the
    graph's executive model today says nothing about a fork from this morning.
    """
    from .handover import fork_models as logged_fork_models

    models: dict[str, str] = {}
    try:
        data = json.loads(cfg.tasks_file.read_text(encoding="utf-8"))
        rows = data.get("tasks", []) if isinstance(data, dict) else []
    except (OSError, ValueError, TypeError, AttributeError):
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sid = row.get("fork_session_id")
        model = str(row.get("model_used") or row.get("model") or "").strip()
        if isinstance(sid, str) and sid and model:
            models[sid] = model
    models.update(logged_fork_models(cfg))
    return models


def discover_sessions(cfg: Config, start: datetime, end: datetime) -> list[dict]:
    """[{sid, path, role, model}] for the main session and every fork.

    `role` is the stamp every downstream figure is grouped by: `main_session`
    or `fork_session` here, `workflow_agent` on the subagent transcripts filed
    under each of them. `model` is the model a fork actually ran on (from
    `fork_models`), empty when nothing recorded it.

    Forks are found by the session id `_finalize_record` captured; a fork whose
    id was never recorded (killed, still running, an older row) is picked up by
    the fallback scan over transcripts touched inside the window.
    """
    sessions: list[dict] = []
    seen: set[str] = set()
    models = fork_models(cfg)

    def add(sid: str, path: Path | None, role: str) -> None:
        if not sid or sid in seen or path is None:
            return
        seen.add(sid)
        sessions.append({"sid": sid, "path": path, "role": role,
                         "model": models.get(sid, "")})

    for sid in cfg.main_session_ids:
        add(str(sid), find_transcript(cfg, str(sid)), MAIN_SOURCE)
    for sid in fork_session_ids(cfg):
        add(sid, find_transcript(cfg, sid), FORK_SOURCE)

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
                add(sid, path, FORK_SOURCE)
    return sessions


def role_word(role: str) -> str:
    """`fork_session` -> `fork`: the stamp in prose, for a sentence or a label."""
    return str(role or "").split("_")[0] or "?"


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


# --------------------------------------------------------------- milestones

GIT_SEP = "\x1f"
GIT_FORMAT = f"%H{GIT_SEP}%cI{GIT_SEP}%s"


def parse_git_log(text: str) -> list[dict]:
    """`git log --format=%H<US>%cI<US>%s` output -> [{commit, at, subject}].

    Pure, and separate from running git, so the bucketing below can be tested
    against a log nobody had to commit.
    """
    commits: list[dict] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or GIT_SEP not in line:
            continue
        parts = line.split(GIT_SEP)
        if len(parts) < 2:
            continue
        stamp = parse_iso(parts[1].strip())
        if stamp is None:
            continue
        commits.append({
            "commit": parts[0].strip(),
            "at": stamp,
            "subject": (parts[2].strip() if len(parts) > 2 else ""),
        })
    commits.sort(key=lambda c: c["at"])
    return commits


def repo_commits(cfg: Config, start: datetime, end: datetime) -> list[dict]:
    """Commits in `report_repo` inside the window. [] when git cannot answer.

    Never raises and never blocks for long: this runs inside report generation,
    which itself runs off a background thread during a poll.
    """
    repo = Path(str(getattr(cfg, "report_repo", "") or ""))
    if not repo.name:
        return []
    git = shutil.which("git")
    if git is None or not (repo / ".git").exists():
        return []
    try:
        done = subprocess.run(
            [git, "-C", str(repo), "log", "--reverse",
             f"--since={start.isoformat()}", f"--until={end.isoformat()}",
             f"--format={GIT_FORMAT}"],
            capture_output=True, timeout=20, check=False,
            # Decoded here rather than by `text=True`: git writes commit
            # subjects as UTF-8, and the console codepage this runs under
            # (GBK on this machine) raises UnicodeDecodeError on the first
            # non-ASCII subject - inside a reader thread, where it would be
            # a stack trace on stderr and an empty milestone table.
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [c for c in parse_git_log(done.stdout or "")
            if start <= c["at"] <= end]


def bucket_milestones(commits: list[dict], events: Iterable[tuple], start: datetime,
                      ) -> list[dict]:
    """USD spent on the way to each commit: [{commit, subject, usd, minutes}].

    One row per commit, covering the span that ended with it - the previous
    commit, or the window start for the first. Work done after the last commit
    has no milestone to belong to and is deliberately not invented into one;
    the row totals are therefore <= the window total, which is what the page
    says on the table.
    """
    rows: list[dict] = []
    ordered = sorted(commits, key=lambda c: c["at"])
    turns = sorted((e for e in events if e and e[0] is not None),
                   key=lambda e: e[0])
    index = 0
    previous = start
    for commit in ordered:
        at = commit["at"]
        spent = 0.0
        while index < len(turns) and turns[index][0] <= at:
            usd = turns[index][2]
            if isinstance(usd, (int, float)) and turns[index][0] > previous:
                spent += float(usd)
            index += 1
        rows.append({
            "commit": str(commit.get("commit") or "")[:8],
            "subject": str(commit.get("subject") or ""),
            "usd": round(spent, 4),
            "minutes": round(max(0.0, (at - previous).total_seconds()) / 60.0, 1),
        })
        previous = at
    return rows


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
    """Which tier a turn belongs to: by role when there is one, else by model.

    One question, two ways of asking it - did the model wearing the director's
    hat also do the building?

    * The turn came from a transcript with a ROLE stamp (`main_session`,
      `fork_session`, `workflow_agent`): the role answers it outright. Main and
      fork sessions are the executive tier and Workflow subagents the workers,
      whatever model each happened to run on. That holds across a graph change
      too: a fork launched two hours ago on a model that now sits at the worker
      tier was still the director while it ran (`role_tiered` flags it).
    * No role - a per-model label with no single transcript behind it: the
      model id decides, executive/advisory membership winning a tie so a model
      named at two tiers is still counted once.

    Keying a stamped turn on its model id is what inverts the verdict when one
    model serves every tier: every worker lane lands in the executive tier and
    the director drops into the worker tier.
    """
    from .graph import ADVISORY, EXECUTIVE, WORKERS

    if source is not None:
        return SOURCE_TIERS.get(source, WORK_TIER)
    name = str(model or "")
    exec_models = {str(graph.get(EXECUTIVE, {}).get("model", "")),
                   str(graph.get(ADVISORY, {}).get("model", ""))}
    if name in exec_models:
        return EXEC_TIER
    if name == str(graph.get(WORKERS, {}).get("model", "")):
        return WORK_TIER
    return WORK_TIER


def role_tiered(model: str, graph: dict, source: str | None) -> bool:
    """True when this row is in the executive tier by ROLE against its model.

    The transition case: the graph named Fable at the executive tier when a
    fork launched, the operator has since moved the executive to Opus - or the
    fork fell back - and the model that fork ran on is now the *workers'*
    model. The turn is still the director's, so it stays in the executive tier
    and carries this flag, which is what the page's caveat explains rather than
    letting the reader assume a worker lane was miscounted.
    """
    from .graph import WORKERS

    if source not in (MAIN_SOURCE, FORK_SOURCE):
        return False
    if not graph_separates_tiers(graph):
        return False
    return str(model or "") == str(graph.get(WORKERS, {}).get("model", ""))


def panel_tier(source: str, lane_role: str | None = None) -> str:
    """Which of the graph's three tiers a transcript's tokens belong to.

    Sessions are the executive. A Workflow agent is an advisory lens when its
    own brief says so (reviewer / judge / synthesis) and a worker otherwise,
    which is the only place the advisory tier's tokens can come from: the
    lenses run inside the fork's workflows and have no session of their own.
    """
    if source in (MAIN_SOURCE, FORK_SOURCE):
        return PANEL_EXEC
    return PANEL_ADVISORY if lane_role in ADVISORY_ROLES else PANEL_WORKERS


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
    from .pricing import default_row, read_pricing_source
    from .pricing import unpriced as unpriced_models

    graph = read_graph(cfg)
    by_model_split = graph_separates_tiers(graph)
    prices, price_source = read_pricing_source(cfg)
    fallback_price = default_row(cfg)
    sessions = discover_sessions(cfg, start, end)
    overall = Tally(prices, fallback_price)
    tiers = {EXEC_TIER: Tally(prices, fallback_price),
             WORK_TIER: Tally(prices, fallback_price)}
    model_sources: dict[str, list[str]] = {}
    model_tiers: dict[str, set[str]] = {}
    sinks: list[dict] = []
    cost_sinks: list[dict] = []
    # Sessions whose tier came from their role against their model id; named in
    # a caveat, so a reader never has to guess why a row is where it is.
    role_tiered_rows: list[str] = []
    by_source_usd = {MAIN_SOURCE: 0.0, FORK_SOURCE: 0.0, AGENT_SOURCE: 0.0}
    by_role_usd = {role: 0.0 for role in ROLES}
    # (role stamp, model) -> turns / output / weighted / USD. The role is the
    # transcript's, so this is the one cut that survives a graph change: it
    # says what each hat cost, on whichever model it was wearing at the time.
    by_role_model: dict[tuple[str, str], dict[str, Any]] = {}
    agent_files = 0

    def stamp_role(role: str, model: str, row: dict) -> None:
        cell = by_role_model.setdefault((role, model), {
            "role": role, "model": model, "turns": 0, "output": 0,
            "weighted": 0.0, "cost_usd": 0.0, "unpriced": False})
        cell["turns"] += row["messages"]
        cell["output"] += row["usage"]["output_tokens"]
        cell["weighted"] += row["weighted"]
        cell["unpriced"] = cell["unpriced"] or bool(row.get("unpriced"))
        cell["cost_usd"] = _add_usd(cell["cost_usd"], row.get("cost_usd"))
    # One dedup set for the whole report: a resumed fork's transcript repeats
    # the parent's turns verbatim, and discover_sessions yields the main
    # session first, so the parent keeps its own turns and each fork keeps only
    # what it actually produced.
    seen_turns: set[str] = set()

    def fold(tally: Tally, source: str) -> None:
        overall.merge(tally)
        by_source_usd[source] = by_source_usd.get(source, 0.0) + tally.usd()
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

    def cost_sink(source: str, label: str, what: str, model: str,
                  row: dict) -> None:
        cost_sinks.append({
            "source": source,
            "role": source,
            "id_or_label": label,
            "what": what,
            "model": model,
            "usd": _round_usd(row.get("cost_usd")),
            "tokens_out": row["usage"]["output_tokens"],
            "cache_read": row["usage"]["cache_read_input_tokens"],
        })

    for session in sessions:
        source = session["role"]
        word = role_word(source)
        tally = parse_transcript(session["path"], start, end, seen_turns,
                                 prices, fallback_price)
        fold(tally, source)
        session_role = role_of(f"{word} session", source)
        by_role_usd[session_role] = by_role_usd.get(session_role, 0.0) + tally.usd()
        for model, row in tally.by_model.items():
            stamp_role(source, model, row)
            cats = {c: cell["messages"] for c, cell in row["cats"].items()}
            hands = sum(row["cats"].get(c, {}).get("weighted", 0.0)
                        for c in HANDS_ON_CATS)
            share = hands / row["weighted"] if row["weighted"] else 0.0
            # A fork that ran on the model now sitting at the worker tier is
            # still the director: it is tiered by its role, and says so.
            tiered_by_role = role_tiered(model, graph, source)
            what = (f"{word} session on {model}: "
                    f"{cats.get('OPS', 0)} OPS, {cats.get('AUTHOR', 0)} AUTHOR, "
                    f"{cats.get('DECIDE', 0)} DECIDE turns")
            if tiered_by_role:
                what += (f" (role-tiered: {model} is the graph's worker model "
                         "now, this transcript was the director)")
                role_tiered_rows.append(f"{session['sid'][:8]} {word} on {model}")
            sinks.append({
                "source": source,
                # The stamp, carried on the row itself: the page's ledger table
                # shows it, and a row's tier follows it rather than its model.
                "role": source,
                "model": model,
                "role_tiered": tiered_by_role,
                "id_or_label": f"{session['sid'][:8]} {word} / {model}",
                "what": what,
                "fable_output": row["usage"]["output_tokens"],
                "fable_cache_read": row["usage"]["cache_read_input_tokens"],
                "weighted_cost": _round(row["weighted"]),
                "verdict": "hands-on" if share > HANDS_ON_LIMIT else "executive",
            })
            cost_sink(source, f"{session['sid'][:8]} {word} ({session_role})",
                      what, model, row)
        agents = Tally(prices, fallback_price)
        for path in agent_transcripts(cfg, session["sid"]):
            agent_files += 1
            one = parse_transcript(path, start, end, seen_turns, prices,
                                   fallback_price)
            agents.merge(one)
            if not one.by_model:
                continue
            # The lane's own brief names what it was for; the dollars follow
            # that label rather than the flat "workflow agent" bucket, which is
            # the only way review and judge spend can be told from build spend.
            lane = agent_role(path)
            by_role_usd[lane] = by_role_usd.get(lane, 0.0) + one.usd()
            label = (f"{path.parent.name}/"
                     f"{path.stem.replace('agent-', '')[:8]} ({lane})")
            for model, row in one.by_model.items():
                cats = {c: cell["messages"] for c, cell in row["cats"].items()}
                cost_sink(AGENT_SOURCE, label,
                          f"{lane} lane under {session['sid'][:8]} on {model}: "
                          f"{cats.get('OPS', 0)} OPS, {cats.get('AUTHOR', 0)} "
                          f"AUTHOR, {row['messages']} turns", model, row)
        if agents.by_model:
            fold(agents, AGENT_SOURCE)
            for model, row in agents.by_model.items():
                stamp_role(AGENT_SOURCE, model, row)
                cats = {c: cell["messages"] for c, cell in row["cats"].items()}
                sinks.append({
                    "source": AGENT_SOURCE,
                    "role": AGENT_SOURCE,
                    "model": model,
                    "role_tiered": False,
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
    # Unpriced lanes sort last rather than as $0: they are not cheap, they are
    # unknown, and the page labels them so.
    cost_sinks.sort(key=lambda s: (s["usd"] is None, -(s["usd"] or 0.0)))
    exec_out, exec_cost = tiers[EXEC_TIER].totals()
    work_out, work_cost = tiers[WORK_TIER].totals()
    out_total = exec_out + work_out
    cost_total = exec_cost + work_cost

    # ---- what it cost, in USD -------------------------------------------
    priced_weighted, all_weighted = overall.priced_weighted()
    unpriced = unpriced_models(prices, sorted(overall.by_model), fallback_price)
    usd_by_model: dict[str, float | None] = {}
    usd_components: dict[str, dict[str, float] | None] = {}
    pricing_used: dict[str, dict[str, Any]] = {}
    for model in sorted(overall.by_model,
                        key=lambda m: -(overall.by_model[m].get("cost_usd") or 0.0)):
        row = overall.by_model[model]
        price = price_for(prices, model, fallback_price)
        usd_by_model[model] = _round_usd(row.get("cost_usd"))
        parts = cost_components(row["usage"], price)
        usd_components[model] = (None if parts is None else
                                 {k: round(v, 4) for k, v in parts.items()})
        used = dict(price) if isinstance(price, dict) else {
            field: None for field in PRICE_FIELDS}
        used.setdefault("source", None)
        used.setdefault("checked", None)
        # The footer prints this verbatim, so it carries the source and the
        # date the number was read, not just the number.
        pricing_used[model] = {**used, "unpriced": price is None}
    usd_hours: dict[str, dict[str, float]] = {
        hour: {} for buckets in overall.hourly.values() for hour in buckets}
    for model, buckets in overall.hourly.items():
        for hour, bucket in buckets.items():
            if isinstance(bucket.get("cost_usd"), (int, float)):
                usd_hours[hour][model] = round(float(bucket["cost_usd"]), 4)
    # How much of the window's cache creation was written at the 1-hour
    # duration, which bills at 2x base input where a 5-minute write bills at
    # 1.25x. Kept as a figure rather than a footnote because it is the
    # difference between this bill and the one a four-term formula would print.
    creation_total = sum(r["usage"][CREATION_KEY]
                         for r in overall.by_model.values())
    creation_1h = sum(r["usage"][CREATION_1H_KEY]
                      for r in overall.by_model.values())
    cost_block = {
        "total_usd": round(overall.usd(), 4),
        "cache_write_1h_tokens": creation_1h,
        "cache_write_1h_share": (round(creation_1h / creation_total, 4)
                                 if creation_total else 0.0),
        # How much of the window this bill actually covers: 1.0 means every
        # turn had a published price behind it.
        "priced_share": (round(priced_weighted / all_weighted, 4)
                         if all_weighted else 0.0),
        "pricing_source": price_source,
        "unpriced_models": unpriced,
        "by_model": usd_by_model,
        "by_model_components": usd_components,
        "by_tier": {"executive": round(tiers[EXEC_TIER].usd(), 4),
                    "worker": round(tiers[WORK_TIER].usd(), 4)},
        "by_source": {source: round(value, 4)
                      for source, value in by_source_usd.items()},
        "by_role": {role: round(by_role_usd.get(role, 0.0), 4) for role in ROLES},
        "by_category": {
            cat: round(_usd_total(r["cats"].get(cat, {}).get("cost_usd")
                                  for r in overall.by_model.values()), 4)
            for cat in CATS},
        "top_sinks": cost_sinks[:COST_ROWS],
        "per_hour": [{"hour": hour, "usd_by_model": usd_hours[hour]}
                     for hour in sorted(usd_hours)],
        "per_milestone": bucket_milestones(repo_commits(cfg, start, end),
                                           overall.events, start),
        "pricing_used": pricing_used,
    }
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
        f"({sum(1 for s in sessions if s['role'] == FORK_SOURCE)} fork), "
        f"{agent_files} workflow agent transcript(s).",
        f"List-price cost of the window ${cost_block['total_usd']:,.2f}: "
        f"${cost_block['by_tier']['executive']:,.2f} executive tier, "
        f"${cost_block['by_tier']['worker']:,.2f} worker tier, "
        f"{cost_block['priced_share']:.1%} of weighted cost priced "
        f"(prices from {price_source}).",
        f"Graph in force: executive {graph[EXECUTIVE]['model']} "
        f"x{graph[EXECUTIVE]['count']}, advisory {graph[ADVISORY]['model']} "
        f"x{graph[ADVISORY]['count']}, workers {graph[WORKERS]['model']} "
        f"x{graph[WORKERS]['count']} (surge {graph[WORKERS]['surge_count']}).",
        ("Tiers split by transcript role: main and fork sessions are the "
         "executive tier, Workflow subagents the workers, whatever model each "
         "ran on." + (f" {len(role_tiered_rows)} session(s) ran on the graph's "
                      "current worker model and are tiered by role: "
                      + ", ".join(role_tiered_rows) + "."
                      if role_tiered_rows else "")),
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

    # The graph exactly as it stood when this page was built, so a page read
    # next week is not silently reinterpreted against next week's graph.
    graph_in_force = {
        tier: {"model": graph[tier]["model"],
               "fallback": graph[tier].get("fallback"),
               "count": graph[tier]["count"],
               **({"surge_count": graph[tier]["surge_count"]}
                  if tier == WORKERS else {})}
        for tier in (EXECUTIVE, ADVISORY, WORKERS)}
    by_role_rows = sorted(
        ({**cell,
          "weighted": _round(cell["weighted"]),
          "cost_usd": _round_usd(cell["cost_usd"])}
         for cell in by_role_model.values()),
        key=lambda r: (ROLE_STAMPS.index(r["role"])
                       if r["role"] in ROLE_STAMPS else len(ROLE_STAMPS),
                       -r["weighted"]))

    return {
        "generated_at": utcnow().isoformat(),
        "reason": reason,
        "graph_in_force": graph_in_force,
        "by_role": by_role_rows,
        "sources": {
            "main_session": ", ".join(
                s["sid"] for s in sessions if s["role"] == MAIN_SOURCE) or "none",
            "fork_sessions": ", ".join(
                s["sid"] for s in sessions if s["role"] == FORK_SOURCE) or "none",
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
                "cache_creation_tokens": row["usage"][CREATION_KEY],
                # The slice of cache_creation_tokens written at the 1-hour
                # duration, which bills at 2x base input instead of 1.25x. A
                # subset of the figure above, never an addition to it.
                "cache_creation_1h_tokens": row["usage"][CREATION_1H_KEY],
                "input_tokens": row["usage"]["input_tokens"],
                "weighted_cost": _round(row["weighted"]),
                # The bill for this model's turns at its published list price,
                # and null when nothing published one.
                "cost_usd": _round_usd(row.get("cost_usd")),
                "unpriced": bool(row.get("unpriced")),
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
        "cost": cost_block,
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
            "Weighted cost is a comparison proxy for comparing tiers, not a "
            "bill; the USD figures under 'What it cost' are the bill, priced at "
            f"published list prices ({price_source}) against the transcripts' "
            "own usage records. They are an estimate of list-price spend, not "
            "an invoice: subscription plans, discounts, batch, fast-mode, "
            "data-residency and long-context modifiers are not modelled, and "
            "server-side tool charges (web search is billed per search) are "
            "not in any token count.",
            "Cache writes are billed at two rates, not one, because the usage "
            "records carry two: a 5-minute write costs 1.25x base input and a "
            "1-hour write costs 2x, and "
            f"{cost_block['cache_write_1h_share']:.1%} of this window's "
            f"{creation_total:,} cache-creation tokens "
            f"({creation_1h:,}) were written at the 1-hour duration. Billing "
            "the whole figure at the 5-minute rate would understate the total.",
        ] + ([
            "Unpriced in this window, shown as 'unpriced' rather than billed at "
            "a guessed rate: " + ", ".join(unpriced) + ". Their tokens are in "
            "every token figure on this page and in none of the USD ones, which "
            f"is why the priced share is {cost_block['priced_share']:.1%}.",
        ] if unpriced else []) + [
            "Tiers come from the transcript's role, not from its model id: "
            "main and fork sessions are the executive tier, Workflow subagents "
            "the workers. Tiering by model id inverts the verdict whenever one "
            "model serves every tier (every worker lane lands in the executive "
            "tier and the director in the worker tier), and it misfiles a fork "
            "whenever the graph moves under it."
            + (" Flagged 'role-tiered' in this window, having run on the model "
               "the graph now names at the worker tier while being the "
               "director: " + ", ".join(role_tiered_rows) + "."
               if role_tiered_rows else ""),
            "Every row carries the role of the transcript behind it "
            f"({', '.join(ROLE_STAMPS)}) and, for a fork, the model it "
            "actually ran on, read from state/handover.log and the task rows "
            "rather than from the graph in force now.",
            "Only transcripts on this machine are read; a fork whose session id "
            "was never recorded is found by its brief, and one that wrote "
            "nothing in the window is invisible.",
        ],
    }


# ------------------------------------------------------------- tier shares
#
# state/tiers.json is the overlay's whole view of where the tokens went: the
# panel draws two bars per rung from it and parses nothing itself, because a
# Tk refresh every few seconds cannot afford to read a transcript.
#
# The file is rebuilt by the loop every `tiers_refresh_seconds`, over the same
# window the next report would use, and the cost of rebuilding it is what
# state/ledger_cache.json is for: a transcript that has not grown since the
# last pass is read back from the cache instead of from disk.

def _tally_totals(tally: Tally) -> dict[str, int]:
    """One transcript's tokens, on the panel's two axes.

    Input is everything that was *read*: fresh input, the tokens written into
    the cache, and the tokens read back out of it. Output is what came back.
    """
    return {
        "input": sum(sum(row["usage"][key] for key in INPUT_KEYS)
                     for row in tally.by_model.values()),
        "output": sum(row["usage"]["output_tokens"]
                      for row in tally.by_model.values()),
        "turns": sum(row["messages"] for row in tally.by_model.values()),
    }


def read_ledger_cache(cfg: Config) -> dict:
    """The per-transcript tally cache, or {} when there is none."""
    try:
        data = json.loads(cfg.ledger_cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_ledger_cache(cfg: Config, cache: dict) -> None:
    _write_json(cfg.ledger_cache_file, cache)


def _write_json(path: Path, payload: Any) -> None:
    """Swap a JSON file in whole; never raises. Same dance as goal/control."""
    try:
        body = json.dumps(payload, indent=2)
    except (TypeError, ValueError):
        return
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


def cache_signature(path: Path, start: datetime) -> str | None:
    """path + mtime + size + window start, or None when the file is gone.

    The window start is part of the key because a tally is only ever a tally
    *of a window*: the same unchanged file yields different numbers once the
    window moves. The end is not, and does not need to be - a file whose size
    and mtime are unchanged has gained no turn for a later end to include.
    """
    try:
        stat = path.stat()
    except (OSError, AttributeError):
        return None
    return f"{CACHE_VERSION}|{stat.st_mtime_ns}|{stat.st_size}|{start.isoformat()}"


def file_tally(path: Path, start: datetime, end: datetime, seen: set[str],
               cache: dict, parse=None, lane=None) -> dict:
    """One transcript's totals: {input, output, turns, lane, cached}.

    Reuses the cache entry when the file has neither grown nor been rewritten
    since it was made. The entry carries the message ids that tally counted,
    and a reuse puts them back into `seen`, so the cross-file dedup stays
    exact: a fork transcript opens with a verbatim copy of the parent's turns,
    and skipping the parse must not also skip the "already counted" marks that
    parse would have left behind.

    `lane` is a zero-argument callable naming an agent transcript's own lane
    role. It is only asked on a miss - reading it means reading the file's
    first user message - and is remembered in the entry.
    """
    parse = parse or parse_transcript
    key = str(path)
    signature = cache_signature(path, start)
    entry = cache.get(key) if isinstance(cache, dict) else None
    if (signature is not None and isinstance(entry, dict)
            and entry.get("sig") == signature):
        ids = entry.get("ids")
        if isinstance(ids, list):
            seen.update(str(i) for i in ids)
        return {"input": _count(entry.get("input")) or 0,
                "output": _count(entry.get("output")) or 0,
                "turns": _count(entry.get("turns")) or 0,
                "lane": entry.get("lane"), "cached": True}
    before = set(seen)
    totals = _tally_totals(parse(path, start, end, seen))
    totals["lane"] = lane() if lane is not None else None
    totals["cached"] = False
    counted = sorted(seen - before)
    if (signature is not None and isinstance(cache, dict)
            and len(counted) <= CACHE_MAX_IDS):
        cache[key] = {"sig": signature, "ids": counted,
                      **{k: totals[k] for k in ("input", "output", "turns")},
                      "lane": totals["lane"]}
    return totals


def build_tiers(cfg: Config, start: datetime, end: datetime,
                cache: dict | None = None, parse=None,
                now: datetime | None = None) -> dict:
    """Per-tier input/output tokens over [start, end], in tiers.json's schema.

    The split is by ROLE, exactly like the report's: sessions are the
    executive, and a Workflow agent is an advisory lens or a worker according
    to the role its own brief declares. `cache` is updated in place and pruned
    of every entry this pass did not touch, so it never grows past the set of
    transcripts the current window actually has.
    """
    cache = cache if isinstance(cache, dict) else {}
    tiers = {tier: {key: 0 for key in TIER_FIELDS} for tier in PANEL_TIERS}
    seen: set[str] = set()
    touched: set[str] = set()

    def fold(tier: str, totals: dict) -> None:
        tiers[tier]["input"] += totals["input"]
        tiers[tier]["output"] += totals["output"]
        tiers[tier]["sessions"] += 1

    for session in discover_sessions(cfg, start, end):
        touched.add(str(session["path"]))
        fold(panel_tier(session["role"]),
             file_tally(session["path"], start, end, seen, cache, parse))
        for path in agent_transcripts(cfg, session["sid"]):
            touched.add(str(path))
            totals = file_tally(path, start, end, seen, cache, parse,
                                lane=lambda p=path: agent_role(p))
            fold(panel_tier(AGENT_SOURCE, totals.get("lane")), totals)
    for key in [k for k in cache if k not in touched]:
        cache.pop(key, None)
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "tiers": tiers,
        "generated_at": (now or utcnow()).isoformat(),
    }


def read_tiers(cfg: Config) -> dict | None:
    """state/tiers.json, or None when it is missing or unreadable."""
    try:
        data = json.loads(cfg.tiers_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tiers"), dict):
        return None
    return data


def write_tiers(cfg: Config, payload: dict) -> dict:
    _write_json(cfg.tiers_file, payload)
    return payload


def tier_shares(payload: Any) -> dict[str, dict[str, float]]:
    """Per tier: its tokens and its share of the three tiers' totals.

    The shares are of the window's own totals, so the three input shares add
    to 1 and the three output shares add to 1 whenever anything was spent. An
    empty window - no file, no turns, a payload of junk - is 0% everywhere
    rather than an even split: the bars still have to draw something, and "no
    tokens" is the true thing to draw.
    """
    tiers = payload.get("tiers") if isinstance(payload, dict) else None
    tiers = tiers if isinstance(tiers, dict) else {}
    values: dict[str, dict[str, float]] = {}
    for tier in PANEL_TIERS:
        block = tiers.get(tier)
        block = block if isinstance(block, dict) else {}
        values[tier] = {key: max(0, _count(block.get(key)) or 0)
                        for key in TIER_FIELDS}
    total_in = sum(v["input"] for v in values.values())
    total_out = sum(v["output"] for v in values.values())
    for tier in PANEL_TIERS:
        values[tier]["input_share"] = (values[tier]["input"] / total_in
                                       if total_in else 0.0)
        values[tier]["output_share"] = (values[tier]["output"] / total_out
                                        if total_out else 0.0)
    return values


def tiers_refresh_seconds(cfg: Config) -> float:
    try:
        value = float(getattr(cfg, "tiers_refresh_seconds",
                              TIERS_REFRESH_SECONDS))
    except (TypeError, ValueError):
        return float(TIERS_REFRESH_SECONDS)
    # The comparison rejects NaN and both infinities without importing math.
    return value if 0 <= value <= 86400 else float(TIERS_REFRESH_SECONDS)


def tiers_due(cfg: Config, now: datetime | None = None) -> bool:
    """True when state/tiers.json is missing or older than the refresh period."""
    payload = read_tiers(cfg)
    if payload is None:
        return True
    stamp = parse_iso(payload.get("generated_at"))
    if stamp is None:
        return True
    # A stamp in the future (a clock that moved, a tick that passed its own
    # `now`) counts as fresh: rebuilding on every poll until the clock catches
    # up would be the more expensive way to be wrong.
    return ((now or utcnow()) - stamp).total_seconds() >= tiers_refresh_seconds(cfg)


def refresh_tiers(cfg: Config, now: datetime | None = None) -> dict:
    """Rebuild state/tiers.json now, over the window the next report would use."""
    now = now or utcnow()
    start, end = window_for(cfg, now=now)
    cache = read_ledger_cache(cfg)
    payload = build_tiers(cfg, start, end, cache, now=now)
    write_ledger_cache(cfg, cache)
    return write_tiers(cfg, payload)


def maybe_refresh_tiers(cfg: Config, now: datetime | None = None) -> str | None:
    """Refresh the tier shares off the poll thread when they are due.

    Off-thread for the same reason the report is: the first pass parses every
    transcript in the window, and the loop's tick must not wait on it. Later
    passes are cheap - the mtime cache means only the files that grew are read
    again - but "cheap" is not "instant" and a poll is not the place to find out.
    """
    if not tiers_due(cfg, now) or _TIERS_BUSY.locked():
        return None

    def work() -> None:
        with _TIERS_BUSY:
            try:
                refresh_tiers(cfg, now)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass

    threading.Thread(target=work, name="ledger-tiers", daemon=True).start()
    return "tier shares refreshing"


def tiers_status_line(cfg: Config, now: datetime | None = None) -> str:
    """The `tracker.py status` line for the panel's token-share bars."""
    payload = read_tiers(cfg)
    if payload is None:
        return "tier shares: none yet (state/tiers.json)"
    shares = tier_shares(payload)
    parts = " | ".join(
        f"{tier[:1].upper()} in {shares[tier]['input_share']:.0%} "
        f"out {shares[tier]['output_share']:.0%}" for tier in PANEL_TIERS)
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    start, end = parse_iso(window.get("start")), parse_iso(window.get("end"))
    span = (f", {(end - start).total_seconds() / 3600:.1f}h window"
            if start and end and end > start else "")
    stamp = parse_iso(payload.get("generated_at"))
    age = ("" if stamp is None else
           f", {int(max(0.0, ((now or utcnow()) - stamp).total_seconds()) // 60)}m ago")
    return f"tier shares: {parts}{span}{age}"


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
