"""The day ledger: one page per local day, with every generation appended.

The single-window report answers "what happened since the last one". That is
the wrong question to open with on a day where nine forked sessions ran: each
page holds one slice, the slices are archived under nine different names, and
the day as a whole is nowhere.

So every generation now writes TWO things. The timestamped window page stays
exactly as it was, the archive of its slice. Beside it,
`reports/<YYYY-MM-DD>-day-ledger.html` and `-day-summary.json` are recomputed
over [local midnight, now] - every total, every tier block, every dollar - and
the generation itself is APPENDED to the day summary's `windows` list as one
more entry. Earlier entries are never rewritten: they are read back as they
were stored and handed straight through, so the file is the record of what the
day looked like at each point rather than one that re-states its own history.

`latest.html` becomes a copy of the day page, so VIEW REPORT, the overlay and
the CLI all open the whole day with the newest window last and highlighted.

Three things the day page carries that a window page cannot:

    fork_runs   one row per run of the director fork today, in start order,
                from the append-only state/handover.log, the current
                state/handover.json record, and - for a fork that ran before
                the log existed - the fork transcript's own first and last turn
    windows     the append-only list above, one card per generation
    day totals  the whole day recomputed, not a sum of the slices (a sum would
                double-count nothing but would also inherit every window's
                rounding, and could not restate a verdict)

The day boundary is the operator's, not UTC's: `clock` resolves local midnight
(America/Chicago here, through zoneinfo or the fixed offset) and the file is
named for that local date. Every timestamp INSIDE the file stays ISO UTC, like
every other file this project writes; the page renders them in the zone the
`timezone` block names.

Nothing here raises for a reason a reader could have anticipated: a missing
handover log, a torn day summary, a repo git cannot answer for, a transcript
that vanished - all of them degrade to an empty row or an empty section.

The one thing the append is not is atomic across PROCESSES. `ledger._BUSY`
keeps two report threads in one process apart, and the file is swapped in
whole so a reader never sees half of it, but a `tracker.py report` typed at the
same second as the loop's own generation can read the same window list twice
and one of the two appends is then lost. It costs a card, never the day: the
totals above the cards are a fresh parse either way.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ledger
from .config import Config
from .models import parse_iso, utcnow

DAY_PAGE_SUFFIX = "-day-ledger.html"
DAY_SUMMARY_SUFFIX = "-day-summary.json"
# The keys the day summary adds to the window schema `build_summary` returns.
# `day` is also the template's mode flag: the page draws its day-only sections
# when, and only when, DATA carries this block.
DAY_KEYS = ("day", "date", "fork_runs", "windows")
WINDOW_KEYS = ("seq", "reason", "window", "generated_at", "totals_by_model",
               "cost_total", "tier_blocks", "commits")
# One row per run of the fork. `status` is the handover record's own word, plus
# one this file adds for a run only the transcript remembers.
RUN_KEYS = ("run", "task_id", "model", "started_at", "finished_at", "minutes",
            "tokens", "total_tokens", "cost_usd", "fork_session_id", "commits",
            "status", "source")
# The three counters `dispatch._finalize_record` sums into the handover
# record's `tokens` field. `tokens` on a run row is OUTPUT tokens and nothing
# else; the handover figure is carried as `total_tokens`, in its own column,
# because the two are different quantities and one column cannot hold both.
BILLED_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens")
STATUS_UNRECORDED = "unrecorded"
SOURCE_LOG = "handover.log"
SOURCE_RECORD = "handover.json"
SOURCE_TRANSCRIPT = "transcript"
SHORT_ID = 8
# A day is a whole day of forks at most; the cap is there so a corrupted log
# cannot turn one page into a memory problem.
RUN_MAX = 200
COMMIT_SUBJECT_CHARS = 120


# ------------------------------------------------------------------- clock

def day_bounds(cfg: Config, now: datetime | None = None,
               ) -> tuple[datetime, datetime, str]:
    """(local midnight as UTC, now, "YYYY-MM-DD" in the local zone).

    The day is the operator's day. `clock` resolves the zone through zoneinfo
    when the box has a tz database and through the configured fixed offset when
    it does not, which is the live path on this machine; either way the date in
    the file name is the one the operator would say out loud.
    """
    from . import clock

    now = now or utcnow()
    local = clock.to_local(now, cfg)
    if local is None:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now, now.strftime("%Y-%m-%d")
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        start = midnight.astimezone(timezone.utc)
    except (OSError, OverflowError, ValueError):
        start = now - timedelta(hours=24)
    # A start after `now` would be a zone that moved under us mid-call; the
    # whole day is the safe reading, never a negative window.
    if start > now:
        start = now - timedelta(hours=24)
    return start, now, midnight.strftime("%Y-%m-%d")


def day_date(cfg: Config, now: datetime | None = None) -> str:
    return day_bounds(cfg, now)[2]


def day_page_path(cfg: Config, date: str) -> Path:
    return cfg.reports_dir / f"{date}{DAY_PAGE_SUFFIX}"


def day_summary_path(cfg: Config, date: str) -> Path:
    return cfg.reports_dir / f"{date}{DAY_SUMMARY_SUFFIX}"


def latest_day_page(cfg: Config, now: datetime | None = None) -> Path | None:
    """Today's day page, or None when today has not been generated yet."""
    page = day_page_path(cfg, day_date(cfg, now))
    return page if page.exists() else None


# ------------------------------------------------------------ the day file

def read_day_summary(cfg: Config, date: str) -> dict:
    """The stored day summary, or {} when there is none (or it is unreadable).

    A torn file reads as "no day yet" rather than raising: the next generation
    starts a new one, which loses the day's window list and never the report.
    """
    try:
        data = json.loads(day_summary_path(cfg, date).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def stored_windows(summary: Any) -> list[dict]:
    """The `windows` list exactly as it was stored, junk entries dropped.

    Handed straight through to the next write - never rebuilt, never re-keyed -
    which is what makes the list append-only in fact and not only in intent.
    """
    raw = summary.get("windows") if isinstance(summary, dict) else None
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def last_window_end(windows: list[dict]) -> datetime | None:
    """When the previous generation of the day ended. None when there is none."""
    for entry in reversed(windows):
        window = entry.get("window")
        stamp = parse_iso(window.get("end")) if isinstance(window, dict) else None
        if stamp is not None:
            return stamp
    return None


def write_day_summary(cfg: Config, date: str, summary: dict) -> Path:
    path = day_summary_path(cfg, date)
    ledger._write_json(path, summary)
    return path


# ------------------------------------------------------- one window's slice

def segment_stats(cfg: Config, start: datetime, end: datetime,
                  graph: dict | None = None) -> dict:
    """What ONE generation's slice holds: per-model totals, dollars, blocks.

    Deliberately not a second `build_summary`: a window card shows what was
    produced, what it cost and what each tier did, and that is one parse pass
    over the slice plus the block readers. The day totals above it are the full
    build, over the whole day.
    """
    from .graph import read_graph
    from .pricing import default_row, read_pricing_source

    prices, _source = read_pricing_source(cfg)
    fallback = default_row(cfg)
    sessions = ledger.discover_sessions(cfg, start, end)
    overall = ledger.Tally(prices, fallback)
    # One dedup set for the slice, main session first, exactly as build_summary
    # folds them: a resumed fork opens with a verbatim copy of the parent's
    # turns and they belong to the parent.
    seen: set[str] = set()
    for session in sessions:
        overall.merge(ledger.parse_transcript(session["path"], start, end, seen,
                                              prices, fallback))
        for path in ledger.agent_transcripts(cfg, str(session.get("sid") or "")):
            overall.merge(ledger.parse_transcript(path, start, end, seen,
                                                  prices, fallback))
    totals = {
        model: {
            "messages": row["messages"],
            "output_tokens": row["usage"]["output_tokens"],
            "weighted_cost": ledger._round(row["weighted"]),
            "cost_usd": ledger._round_usd(row.get("cost_usd")),
            "unpriced": bool(row.get("unpriced")),
        }
        for model, row in sorted(overall.by_model.items(),
                                 key=lambda kv: -kv[1]["weighted"])
    }
    return {
        "totals_by_model": totals,
        "cost_total": round(overall.usd(), 4),
        "tier_blocks": ledger.tier_blocks(cfg, start, end, sessions,
                                          graph if isinstance(graph, dict)
                                          else read_graph(cfg)),
        "commits": commit_rows(cfg, start, end),
    }


def commit_rows(cfg: Config, start: datetime, end: datetime) -> list[dict]:
    """[{commit, subject, at}] in the tracked repo, by AUTHOR date in the span.

    The author date, not the committer date, for the same reason the tier
    blocks use it: a rebase or an amend moves the committer date, and the
    question here is when the work was done.
    """
    rows: list[dict] = []
    for commit in ledger.author_commits(getattr(cfg, "report_repo", ""),
                                        start, end):
        at = commit.get("at")
        rows.append({
            "commit": str(commit.get("commit") or "")[:SHORT_ID],
            "subject": ledger.scrub(commit.get("subject"), COMMIT_SUBJECT_CHARS),
            "at": at.isoformat() if isinstance(at, datetime) else None,
        })
    return rows


def window_entry(cfg: Config, seq: int, reason: str, start: datetime,
                 end: datetime, now: datetime | None = None,
                 graph: dict | None = None) -> dict:
    """The record of ONE generation, in the shape the day file appends.

    Stored once and never touched again, so every field it needs is in it: the
    page draws a window card from this entry alone.
    """
    stats = segment_stats(cfg, start, end, graph)
    return {
        "seq": int(seq),
        "reason": str(reason or ledger.REASON_MANUAL),
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": round(max(0.0, (end - start).total_seconds()) / 3600, 2),
        },
        "generated_at": (now or utcnow()).isoformat(),
        "totals_by_model": stats["totals_by_model"],
        "cost_total": stats["cost_total"],
        "tier_blocks": stats["tier_blocks"],
        "commits": stats["commits"],
    }


# ------------------------------------------------------ today's fork runs

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _blank_run() -> dict:
    return {"task_id": "", "model": "", "started_at": "", "finished_at": "",
            "fork_session_id": "", "status": "", "tokens": None,
            "cost_usd": None, "source": SOURCE_LOG}


def _touches_day(record: Any, start: datetime, end: datetime) -> bool:
    """Could the run this record describes have been inside [start, end]?

    Deliberately the SAME predicate `fork_runs` applies to a folded row (started
    in the day, finished in it, or spanned into it), asked one record early. It
    has to be applied here rather than only there, because RUN_MAX is a cap on
    how many rows the fold will create: state/handover.log is append-only and
    holds every fork this box ever launched, so a cap applied to the whole
    history would be spent by the OLDEST runs and today's - always at the tail -
    would be the ones dropped. Filter to the day first, then cap what survives.

    A record with neither stamp is not datable, and `fork_runs` drops it too.
    A start record from yesterday whose run finished today is skipped here and
    the finish record then creates the row: the finish carries every START_KEY
    (handover.finish_handover copies them), so nothing is lost with it.
    """
    if not isinstance(record, dict):
        return False
    began = parse_iso(record.get("started_at"))
    ended = parse_iso(record.get("finished_at"))
    lo = began or ended
    hi = ended or began
    if lo is None or hi is None:
        return False
    return not (hi < start or lo > end)


def _fold_record(rows: dict[str, dict], record: Any, source: str,
                 start: datetime, end: datetime) -> None:
    """One handover record into the run it belongs to, later fields winning.

    A run is TWO records - the start and the finish - and they are joined on
    `started_at`, which the finish copies from the record the start wrote. The
    finish is the only one that knows the session id, the tokens and the bill,
    so a later non-empty field always replaces an earlier empty one.

    Only a record that could belong to [start, end] may CREATE a row; one that
    joins a row already there is folded whatever its own stamps say.
    """
    if not isinstance(record, dict):
        return
    started = _text(record.get("started_at"))
    key = started or _text(record.get("logged_at")) or f"run{len(rows)}"
    fresh = key not in rows
    if fresh and (not _touches_day(record, start, end) or len(rows) >= RUN_MAX):
        return
    row = rows.setdefault(key, _blank_run())
    for field in ("task_id", "model", "started_at", "finished_at",
                  "fork_session_id", "status"):
        value = _text(record.get(field))
        if value:
            row[field] = value
    for field in ("tokens", "cost_usd"):
        value = _number(record.get(field))
        if value is not None:
            row[field] = value
    # Only a row this record CREATED is credited to it: handover.json repeats
    # the newest fork the log already has, and relabelling that row would say
    # the log had lost it.
    if fresh and source != SOURCE_LOG:
        row["source"] = source


def transcript_bounds(path: Path, start: datetime, end: datetime,
                      ) -> tuple[datetime | None, datetime | None]:
    """(first turn, last turn) inside the window for one transcript.

    The fallback clock for a fork that ran before state/handover.log existed:
    the transcript is the only file that remembers when it worked.
    """
    first: datetime | None = None
    last: datetime | None = None
    for entry in ledger._iter_entries(path):
        stamp = parse_iso(entry.get("timestamp"))
        if stamp is None or stamp < start or stamp > end:
            continue
        if first is None or stamp < first:
            first = stamp
        if last is None or stamp > last:
            last = stamp
    return first, last


def _spent(cfg: Config, path: Path | None, start: datetime, end: datetime,
           prices: Any, fallback: Any, seen: set[str] | None = None,
           ) -> tuple[int | None, int | None, float | None]:
    """(output tokens, billed tokens, USD) a transcript produced in the span.

    Two token figures because the table shows two, and mixing them was a bug:
    `output` is what the run wrote, `billed` is the input + output + cache-write
    sum, which is the same three counters `dispatch._finalize_record` puts in
    the handover record's `tokens` field. Cache READS are in neither, and the
    1-hour cache-write count is a sub-count of the writes, so adding it would
    double-count.

    The span is the run's own, which is also what keeps the parent's inherited
    history out of it: those turns carry their original timestamps, and every
    one of them is older than the fork that copied them.
    """
    # `end < start`, not `end <= start`: a fork that produced exactly one turn
    # has a zero-length span, and that turn is still what it cost.
    if path is None or not isinstance(path, Path) or end < start:
        return None, None, None
    tally = ledger.parse_transcript(path, start, end,
                                    seen if seen is not None else set(),
                                    prices, fallback)
    if not tally.by_model:
        return None, None, None
    output = sum(row["usage"]["output_tokens"] for row in tally.by_model.values())
    billed = sum(row["usage"][key] for row in tally.by_model.values()
                 for key in BILLED_KEYS)
    # `usd()` is the PRICED part of the bill, so a run whose model nothing
    # prices would come back as $0.00 - which reads as "this fork was free"
    # where the truth is "nobody published a price". None says the second.
    priced, _all = tally.priced_weighted()
    if priced <= 0:
        return output, billed, None
    return output, billed, round(tally.usd(), 4)


def fork_runs(cfg: Config, start: datetime, end: datetime) -> list[dict]:
    """One row per run of the director fork inside the day, in start order.

    Three sources, folded in this order and never ranked against each other:

        1. state/handover.log - every start and finish ever appended, which is
           the whole history and the only source that survives the next fork
        2. state/handover.json - the newest record, in case the log lost its
           append (a torn line, a log that was rotated by hand)
        3. the fork transcripts the ledger discovers for the day, for a run
           that predates the log entirely: its first and last turn are the span

    The log is filtered to the day as it is folded (`_touches_day`), not after:
    RUN_MAX caps how many runs the DAY may hold, and a cap applied to the whole
    append-only history would be spent by the oldest runs on the file while
    today's, always at its tail, went missing.

    Two token figures, never mixed into one: `tokens` is output tokens and is
    always the transcript's, `total_tokens` is the input + output + cache-write
    sum the handover record carries (and the same three counters off the
    transcript when no record did). Commits are attributed by span from one
    `git log` over the day: a commit lands in the first run whose window
    contains its author date, and a commit made between runs belongs to none of
    them rather than to the nearest.
    """
    from .handover import read_handover, read_log
    from .pricing import default_row, read_pricing_source

    rows: dict[str, dict] = {}
    for entry in read_log(cfg):
        _fold_record(rows, entry, SOURCE_LOG, start, end)
    _fold_record(rows, read_handover(cfg), SOURCE_RECORD, start, end)

    prices, _source = read_pricing_source(cfg)
    fallback = default_row(cfg)
    runs: list[dict] = []
    known: set[str] = set()
    for row in rows.values():
        began = parse_iso(row["started_at"])
        ended = parse_iso(row["finished_at"])
        # Ran today: started in the day, finished in it, or spanned into it.
        span_lo = began or ended
        span_hi = ended or began
        if span_lo is None or span_hi is None:
            continue
        if span_hi < start or span_lo > end:
            continue
        sid = row["fork_session_id"]
        if sid:
            known.add(sid)
        runs.append({**row, "_began": began, "_ended": ended, "_sid": sid})

    # A fork the log never learned about: the transcript is the record.
    for session in ledger.discover_sessions(cfg, start, end):
        if session.get("role") != ledger.FORK_SOURCE:
            continue
        sid = str(session.get("sid") or "")
        if not sid or sid in known or len(runs) >= RUN_MAX:
            continue
        first, last = transcript_bounds(session["path"], start, end)
        if first is None:
            continue
        known.add(sid)
        runs.append({**_blank_run(), "model": str(session.get("model") or ""),
                     "status": STATUS_UNRECORDED, "source": SOURCE_TRANSCRIPT,
                     "started_at": first.isoformat(),
                     "finished_at": (last or first).isoformat(),
                     "fork_session_id": sid, "_began": first,
                     "_ended": last or first, "_sid": sid})

    runs.sort(key=lambda r: (r["_began"] or r["_ended"] or end))
    commits = commit_rows(cfg, start, end)
    taken: set[str] = set()
    out: list[dict] = []
    for index, row in enumerate(runs):
        began = row["_began"] or start
        ended = row["_ended"] or end
        sid = row["_sid"]
        path = ledger.find_transcript(cfg, sid) if sid else None
        # The handover record's `tokens` is input + output + cache writes, not
        # output tokens, so it can only ever be the BILLED figure. Output
        # tokens have one source, the transcript, and the parse happens even
        # when the record already carried a total: a column that read
        # transcript output for one row and a ~50x larger all-in total for the
        # next is worse than a second parse.
        # `seen` is fresh per row on purpose: two runs never share a transcript
        # span, so there is nothing for a shared set to dedup.
        billed = row["tokens"]
        usd = row["cost_usd"]
        tokens, parsed_billed, parsed_usd = _spent(cfg, path, began, ended,
                                                   prices, fallback)
        billed = billed if billed is not None else parsed_billed
        usd = usd if usd is not None else parsed_usd
        mine: list[dict] = []
        for commit in commits:
            at = parse_iso(commit.get("at"))
            if at is None or commit["commit"] in taken:
                continue
            if began <= at <= ended:
                taken.add(commit["commit"])
                mine.append(commit)
        out.append({
            "run": index + 1,
            "task_id": row["task_id"] or "?",
            "model": row["model"] or "unrecorded",
            "started_at": row["started_at"] or None,
            "finished_at": row["finished_at"] or None,
            "minutes": round(max(0.0, (ended - began).total_seconds()) / 60.0, 1),
            "tokens": int(tokens) if isinstance(tokens, (int, float)) else None,
            "total_tokens": (int(billed) if isinstance(billed, (int, float))
                             else None),
            "cost_usd": (round(float(usd), 4)
                         if isinstance(usd, (int, float)) else None),
            "fork_session_id": sid[:SHORT_ID] if sid else "",
            "commits": mine,
            "status": row["status"] or STATUS_UNRECORDED,
            "source": row["source"],
        })
    return out


# ------------------------------------------------------------ the day page

def build_day_summary(cfg: Config, day_start: datetime, day_end: datetime,
                      date: str, reason: str, windows: list[dict]) -> dict:
    """The whole day, recomputed, with the window list appended to it.

    The day figures are a fresh parse over [local midnight, now], not a sum of
    the window slices: a sum inherits every slice's rounding and, worse, cannot
    restate a verdict - the 60% hands-on rule asked of the day is a different
    question from the same rule asked of nine slices.
    """
    summary = ledger.build_summary(cfg, day_start, day_end, reason=reason)
    latest = windows[-1] if windows else {}
    summary["date"] = date
    # The template's mode flag: this block present means "draw the day page".
    summary["day"] = {
        "date": date,
        "start": day_start.isoformat(),
        "end": day_end.isoformat(),
        "windows": len(windows),
        "latest_seq": latest.get("seq"),
        "latest_at": latest.get("generated_at") or day_end.isoformat(),
        "latest_reason": latest.get("reason") or reason,
        "page": day_page_path(cfg, date).name,
    }
    summary["fork_runs"] = fork_runs(cfg, day_start, day_end)
    summary["windows"] = windows
    summary.setdefault("caveats", []).extend([
        f"This is the DAY page for {date}. Every figure above the Windows "
        "section is the whole day recomputed over [local midnight, now], not a "
        "sum of the windows below: a sum would inherit each window's rounding "
        "and could not restate the verdict, which is a different question "
        "asked of the day than of one slice.",
        "The Windows section is append-only. Each card is the record one "
        "generation wrote and was never edited afterwards, so a figure there "
        "is what the tracker saw at that moment even where a later parse of "
        "the same turns would say something slightly different.",
        "Today's forked sessions come from the append-only "
        "state/handover.log, the current state/handover.json record, and - for "
        "a run older than the log - the fork transcript's own first and last "
        "turn. Their two token columns are two different quantities and are "
        "never added together: output tokens are always counted from the run's "
        "own transcript over its own span, while billed tokens are the input + "
        "output + cache-write sum the handover record carries (the same three "
        "counters off the transcript when no record did). Dollars a handover "
        "record did not carry are priced from the transcript the same way; a "
        "run nothing recorded is marked 'unrecorded' rather than guessed at.",
    ])
    return summary


def render_day(cfg: Config, summary: dict, date: str) -> Path:
    """Splice the day summary into the shared template; returns the page.

    Same template as the window page, one mode flag apart: `summary["day"]` is
    what turns the day-only sections on, so the chart code exists once.
    `latest.html` is a copy of THIS page, which is what VIEW REPORT opens.
    """
    reports = cfg.reports_dir
    reports.mkdir(parents=True, exist_ok=True)
    html = ledger.TEMPLATE_PATH.read_text(encoding="utf-8")
    if html.count(ledger.DATA_PLACEHOLDER) != 1:
        raise ValueError(f"template placeholder missing: {ledger.TEMPLATE_PATH}")
    payload = json.dumps(summary, ensure_ascii=True, indent=2).replace("</", "<\\/")
    html = html.replace(ledger.TITLE_PLACEHOLDER, f"Day Ledger {date}")
    html = html.replace(ledger.DATA_PLACEHOLDER, f"const DATA = {payload};")
    page = day_page_path(cfg, date)
    page.write_text(html, encoding="utf-8", newline="\n")
    shutil.copyfile(page, reports / ledger.LATEST_NAME)
    return page


def generate_day(cfg: Config, reason: str = ledger.REASON_MANUAL,
                 now: datetime | None = None) -> Path:
    """Recompute today's day page and append this generation to it.

    The append site: `stored_windows` reads the list back exactly as it was
    written and this function only ever adds one entry to the end of it. No
    caller can rewrite an earlier window, because no caller is handed one.
    """
    now = now or utcnow()
    day_start, day_end, date = day_bounds(cfg, now)
    existing = read_day_summary(cfg, date)
    windows = stored_windows(existing)
    previous = last_window_end(windows)
    # Since the previous generation of the day, or midnight for the first, so
    # the windows tile the day with no gap and no overlap. A stored end in the
    # future (a clock that moved) falls back to midnight rather than inverting.
    segment_start = previous if (previous is not None
                                 and day_start <= previous <= day_end) else day_start
    entry = window_entry(cfg, len(windows) + 1, reason, segment_start, day_end,
                         now)
    summary = build_day_summary(cfg, day_start, day_end, date, reason,
                                windows + [entry])
    write_day_summary(cfg, date, summary)
    return render_day(cfg, summary, date)


# ----------------------------------------------------------------- status

def day_status_line(cfg: Config, now: datetime | None = None) -> str:
    """The `tracker.py status` line for the day ledger.

    "day ledger: 4 windows, last 14:06 CT, $18.44" - how many generations the
    day has had, when the newest one was, and what the day has cost so far.
    """
    from .clock import fmt_local, label

    now = now or utcnow()
    date = day_date(cfg, now)
    summary = read_day_summary(cfg, date)
    windows = stored_windows(summary)
    if not windows:
        return "day ledger: none yet today (tracker.py report --day)"
    count = len(windows)
    last = fmt_local(windows[-1].get("generated_at"), "%H:%M", cfg,
                     fallback="?")
    cost = summary.get("cost") if isinstance(summary.get("cost"), dict) else {}
    total = cost.get("total_usd")
    money = f"${float(total):,.2f}" if isinstance(total, (int, float)) else "$?"
    return (f"day ledger: {count} window{'s' if count != 1 else ''}, "
            f"last {last} {label(cfg)}, {money} "
            f"-> {day_page_path(cfg, date)}")
