from __future__ import annotations

import ctypes
import json
import math
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, reload_config
from .control import RUNNING as CONTROL_RUNNING
from .control import STOPPED as CONTROL_STOPPED
from .control import read_control, write_control
from .goal import GOAL_FALLBACK, GOAL_STEP, read_goal, read_stop, write_goal
from .graph import (
    COUNT_MAX,
    COUNT_MIN,
    TIERS,
    WORKERS,
    active_graph,
    limited_model,
    read_graph,
    short_model,
    tiers_of,
    write_graph,
)
from .handover import fork_active
from .ledger import (
    generate_async,
    latest_report,
    open_report,
    read_tiers,
    report_age,
    tier_shares,
)
from .models import parse_iso, utcnow

TRANSPARENT = "#010203"
CARD_BG = "#232325"
BORDER = "#3d3d40"
FG = "#f0f0f2"
DIM = "#9a9aa0"
TRACK = "#39393d"
AMBER = "#e0a83c"
PINK = "#e585a8"
SILVER = "#b8b8bc"
GREEN = "#4caf7d"
BLUE = "#5b8dd9"
RED = "#d9534f"
MODE_COLORS = {
    "pace": GREEN, "surge": AMBER, "yield": BLUE, "coast": DIM, "blocked": RED,
    "stopped": RED,
}
FONT_BIG = ("Segoe UI", 11, "bold")
FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)
# The ladder's share labels ("in 62%", "out 9%"): monospaced, so the digits sit
# in the same column down all six of them and the percentages can be compared
# by eye rather than read one at a time.
FONT_MONO = ("Consolas", 7)
FABLE_HINTS = ("fable", "mythos")
WEEK_HOURS_F = 168.0
FIVE_HOURS_F = 5.0
# All geometry below is authored in 96-dpi design pixels and scaled at draw
# time by the same DPI factor tk applies to point-sized fonts; keeping the two
# in lockstep is what stops text from outgrowing its boxes on scaled displays.
CARD_MARGIN = 3
CARD_RADIUS = 22
PAD = 18
GAUGE_RADIUS = 33
GAUGE_STROKE = 7
GAUGE_CY = 64
ROW_H = 24
GEAR_TEETH = 8
GEAR_OUTER = 26
GEAR_VALLEY = 19
GEAR_BODY = "#d97757"
GEAR_TOOTH = "#c9683f"
GEAR_STEP_DEG = 5.0
GEAR_FRAME_MS = 90
SUB_BG = "#2c2c2f"
BTN_H = 30
# Vertical gap between the START/STOP row and the FULL THROTTLE button, and the
# horizontal gap splitting START from STOP.
BTN_ROW_GAP = 8
CTL_BTN_GAP = 8
# WEEKLY GOAL row: a shorter strip above START/STOP holding "-", the goal, "+".
GOAL_ROW_H = 24
GOAL_STEP_BTN_W = 26
# AGENTIC GRAPH ladder: one rung per tier, top to bottom (executive, advisory,
# workers), hung off a thin spine on the left. Each rung is a text line - tier,
# the model ACTUALLY in use, the headcount - over two thin bars carrying that
# tier's share of the window's input and output tokens.
#
# The bars used to be the headcount, which the "xN" column already says; what
# they say now is the thing no column could, namely where the tokens actually
# went. The data comes from state/tiers.json, written by the loop, so a refresh
# draws a frame without parsing a transcript.
LADDER_LABEL_H = 15
LADDER_RUNG_H = 36
LADDER_RUNG_GAP = 3
LADDER_SPINE_X = 1
LADDER_SPINE_W = 2
LADDER_BAR_X = 4
# The bars are thin bands *under* each rung's text rather than blocks behind
# it: a block wide enough to mean something has its right edge somewhere in
# the middle of the row, and that edge cuts the tier name in half.
LADDER_TEXT_H = 12
# First band's top, then the centre-to-centre step down to the second. Both are
# set by the share labels beside them, not by the bands: a label is three times
# the height of the 4px band it annotates, and two of them plus the bold "xN"
# above have to clear each other at every DPI.
LADDER_BAND_Y = 17
LADDER_BAND_ROW = 10
LADDER_BAND_H = 4
LADDER_BAR_RADIUS = 2
LADDER_TEXT_PAD = 6
# A share bar's track must stay a bar even in the narrowest card.
LADDER_BAR_MIN = 10
# The "active vs configured" line under the ladder, drawn only while some tier
# is running on something other than the model config.json names.
LADDER_NOTE_H = 14
# The dimmed "->fallback" (or "cfg <model>") after a rung's model, and the red
# limited tag beside its count, both live inside the rung's existing text line -
# the ladder's height is unchanged. Neither suffix is ever ellipsised: it is
# drawn whole or not at all, because "cfg fab..." names no model. When the tag
# and a cfg suffix cannot both fit, the model column slides left and then the
# tag shortens to LIMITED_SHORT; the suffix is the last thing to go.
LIMITED_TAG = "LIMITED"
LIMITED_SHORT = "LIM"
# The narrowest form of the tag: a red dot beside the count, worth about a
# fifth of the word. Drawn only when the word and "LIM" would both cost the
# configured id its place on the rung.
LIMITED_DOT = "dot"
LADDER_LIMIT_DOT_R = 2.5
# The dot marking a rung whose model state/graph.json pins, and the width it
# takes out of the model column.
LADDER_PIN_W = 7
LADDER_PIN_R = 2.0
# The -/+ taps on the worker count, in a gutter every rung reserves so the
# "xN" column lands on the same x down all three.
LADDER_STEP_W = 17
LADDER_STEP_GAP = 3
# REPORT row: a wide VIEW REPORT button beside a narrow REPORT NOW tap target,
# with the report's age on its own line underneath.
REPORT_BTN_H = 26
REPORT_NOW_W = 84
REPORT_AGE_H = 16
# Red band naming the stop point, drawn under the mode label when stop.json is
# on disk (expanded), or across the usage readouts (collapsed).
STOP_BAND_H = 20
# Centre-to-centre spacing of the close and minimize buttons (each is 16 design
# px wide), so close sits immediately left of minimize with a small gap.
TITLE_BTN_STEP = 20
# "FORK ACTIVE" chip in the header row, drawn while the forked director session
# is running. 16 design px tall on the title-button baseline (centre y 22), so
# it ends at 30 - one pixel clear of the gauge arcs, which start at 31.
FORK_CHIP_H = 16
FORK_CHIP_PAD = 7
FORK_CHIP_MIN_W = 30
BTN_ACTIVE_BG = "#8a3d33"
OTHERS_VISIBLE = 2
SCROLLBAR_W = 3
SESSION_CARD_H = 44
SESSION_CARD_GAP = 6
TRANSCRIPT_TAIL_BYTES = 200_000
TITLE_HEAD_BYTES = 64_000
NAME_PRETRIM = 60
STALE_FACTOR = 3
STALE_MIN_SECONDS = 600
FALLBACK_ANCHOR = (100, 100)
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
BASELINE_DPI = 96.0
POINTS_PER_INCH = 72.0


def _enable_dpi_awareness() -> None:
    # Sundial stores its anchor in physical pixels; without per-monitor DPI
    # awareness tk works in virtualized coordinates and the anchor can land
    # outside every display.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _virtual_screen() -> tuple[int, int, int, int]:
    try:
        u = ctypes.windll.user32
        return (
            u.GetSystemMetrics(SM_XVIRTUALSCREEN),
            u.GetSystemMetrics(SM_YVIRTUALSCREEN),
            u.GetSystemMetrics(SM_CXVIRTUALSCREEN),
            u.GetSystemMetrics(SM_CYVIRTUALSCREEN),
        )
    except (AttributeError, OSError):
        return (0, 0, 1920, 1080)


def _fmt_tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


PROCESS_ACCESS = 0x00101000
WAIT_TIMEOUT = 0x102
PROC_START_TOLERANCE = 10_000_000


def _pid_alive(pid: int, proc_start: str | None) -> bool:
    try:
        k32 = ctypes.windll.kernel32
    except AttributeError:
        return True
    handle = k32.OpenProcess(PROCESS_ACCESS, False, int(pid))
    if not handle:
        return False
    try:
        if k32.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:
            return False
        if proc_start:
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel_time = ctypes.c_ulonglong()
            user_time = ctypes.c_ulonglong()
            ok = k32.GetProcessTimes(handle, ctypes.byref(creation),
                                     ctypes.byref(exit_time),
                                     ctypes.byref(kernel_time),
                                     ctypes.byref(user_time))
            if ok:
                try:
                    return abs(creation.value - int(proc_start)) < PROC_START_TOLERANCE
                except ValueError:
                    return True
        return True
    finally:
        k32.CloseHandle(handle)


def _parse_title(event: dict) -> str | None:
    title = event.get("customTitle") or event.get("aiTitle")
    if not title and event.get("type") in ("title", "session-title"):
        title = event.get("title")
    return str(title) if title else None


def _mainchain_context(event: dict) -> float | None:
    if event.get("type") != "assistant" or event.get("isSidechain"):
        return None
    usage = (event.get("message") or {}).get("usage") or {}
    total = 0.0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
    return total if total > 0 else None


_TITLE_CACHE: dict[str, str | None] = {}


def _full_title_scan(path: Path) -> str | None:
    title = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "Title" not in line and '"title"' not in line:
                    continue
                try:
                    event = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    found = _parse_title(event)
                    if found:
                        title = found
    except OSError:
        return None
    return title


def _first_prompt_name(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            chunk = handle.read(TITLE_HEAD_BYTES)
    except OSError:
        return None
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        if event.get("isSidechain") or event.get("isMeta"):
            continue
        content = (event.get("message") or {}).get("content")
        if isinstance(content, list):
            text = next((c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text"), "")
        else:
            text = content if isinstance(content, str) else ""
        text = " ".join(str(text).split())
        if not text or text.startswith("<") or text.lower().startswith("caveat:"):
            continue
        return text
    return None


def _transcript_stats(path: Path) -> tuple[float | None, str | None]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None, None
    ctx = None
    title = None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        wants_usage = ctx is None and '"usage"' in line
        wants_title = title is None and ("Title" in line or '"title"' in line)
        if not (wants_usage or wants_title):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if title is None:
            title = _parse_title(event)
        if ctx is None:
            ctx = _mainchain_context(event)
        if ctx is not None and title is not None:
            break
    if title is None and size > TRANSCRIPT_TAIL_BYTES:
        try:
            with path.open("rb") as handle:
                head = handle.read(TITLE_HEAD_BYTES).decode("utf-8", errors="replace")
        except OSError:
            head = ""
        for line in head.splitlines():
            if "Title" not in line and '"title"' not in line:
                continue
            try:
                event = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                found = _parse_title(event)
                if found:
                    title = found
    return ctx, title


def _live_sessions(cfg: Config) -> list[dict]:
    from .activity import project_dir_name

    now = utcnow()
    sessions: list[dict] = []
    try:
        entries = list(cfg.sessions_dir.glob("*.json"))
    except OSError:
        return []
    for entry in entries:
        try:
            meta = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        sid = meta.get("sessionId")
        cwd = meta.get("cwd")
        pid = meta.get("pid")
        if not sid or not cwd or not pid:
            continue
        if not _pid_alive(pid, meta.get("procStart")):
            continue
        transcript = cfg.projects_dir / project_dir_name(str(cwd)) / f"{sid}.jsonl"
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        age_s = (now - datetime.fromtimestamp(mtime, tz=timezone.utc)).total_seconds()
        if age_s < 30:
            age = "just now"
        elif age_s < 3600:
            age = f"{int(age_s // 60)}m ago"
        elif age_s < 86400:
            age = f"{int(age_s // 3600)}h ago"
        else:
            age = f"{int(age_s // 86400)}d ago"
        ctx, title = _transcript_stats(transcript)
        if title:
            _TITLE_CACHE[sid] = title
        elif sid not in _TITLE_CACHE:
            _TITLE_CACHE[sid] = (_full_title_scan(transcript)
                                 or _first_prompt_name(transcript))
        title = title or _TITLE_CACHE.get(sid)
        custom = (meta.get("name")
                  if meta.get("nameSource") not in (None, "derived") else None)
        cwd_path = Path(str(cwd))
        folder = None if cwd_path == Path.home() else (cwd_path.name or None)
        display = str(custom or title or folder or "untitled session")[:NAME_PRETRIM]
        detail = f"{_fmt_tokens(ctx)} / 1.0M" if ctx else "ctx n/a"
        sessions.append({
            "sid": str(sid),
            "name": display,
            "age": age,
            "detail": detail,
            "mtime": mtime,
        })
    sessions.sort(key=lambda s: -s["mtime"])
    return sessions


def _read_throttle(cfg: Config) -> bool:
    try:
        data = json.loads(cfg.throttle_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("active"))


def _norm_window(window: dict, hours: float) -> tuple[float, str | None]:
    frac = float(window.get("utilization", 0.0) or 0.0)
    resets = parse_iso(window.get("resets_at"))
    if resets is not None:
        now = utcnow()
        while resets <= now:
            resets += timedelta(hours=hours)
            frac = 0.0
    return frac, resets.isoformat() if resets else None


def _local_clock(iso_value: str | None, with_day: bool = False) -> str | None:
    dt = parse_iso(iso_value)
    if dt is None:
        return None
    local = dt.astimezone()
    return local.strftime("%a %H:%M") if with_day else local.strftime("%H:%M")


class Overlay:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.delta = (cfg.overlay_offset_x, cfg.overlay_offset_y)
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._gear_angle = 0.0
        self._gear_center: tuple[float, float] | None = None
        self._order: list[str] = []
        self._scroll_idx = 0
        self._max_scroll = 0
        self._throttle = False
        self._control = read_control(cfg)
        self._goal = read_goal(cfg)
        self._stop = read_stop(cfg)
        self._fork = fork_active(cfg)
        self._graph = read_graph(cfg)
        self._active = active_graph(cfg, self._graph)
        self._shares = tier_shares(read_tiers(cfg))
        self._limited = limited_model(cfg)
        self._report = latest_report(cfg)
        self._report_age = report_age(cfg)
        self._after_id: str | None = None
        self._collapsed = self._load_collapsed()

        _enable_dpi_awareness()
        self.root = tk.Tk()
        dpi = 0
        try:
            dpi = ctypes.windll.user32.GetDpiForWindow(self.root.winfo_id())
            if dpi:
                self.root.tk.call("tk", "scaling", dpi / POINTS_PER_INCH)
        except (AttributeError, OSError, tk.TclError):
            pass
        if dpi:
            self.s = dpi / BASELINE_DPI
        else:
            # No per-window DPI available: derive the factor from whatever tk
            # is actually using for fonts, so boxes still track text size.
            try:
                self.s = (float(self.root.tk.call("tk", "scaling"))
                          * POINTS_PER_INCH / BASELINE_DPI)
            except (tk.TclError, ValueError):
                self.s = 1.0
        self.width = self._px(cfg.overlay_width)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self._transparent = True
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
            self.root.configure(bg=TRANSPARENT)
        except tk.TclError:
            self._transparent = False
            self.root.configure(bg=CARD_BG)

        bg = TRANSPARENT if self._transparent else CARD_BG
        self.canvas = tk.Canvas(
            self.root, width=self.width, height=self._px(220),
            bg=bg, highlightthickness=0,
        )
        self.canvas.pack()
        self._font = tkfont.Font(family="Segoe UI", size=9)
        self._font_bold = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self._font_small = tkfont.Font(family="Segoe UI", size=8)
        # Measured, not assumed: the share labels reserve their own column out
        # of the bar span, and a proportional font would make that column a
        # different width on every rung.
        self._font_mono = tkfont.Font(family=FONT_MONO[0], size=FONT_MONO[1])

        for widget in (self.root, self.canvas):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.bind("<Button-3>", lambda _e: self.root.destroy())
        self.canvas.tag_bind("throttle_btn", "<Button-1>", self._toggle_throttle)
        self.canvas.tag_bind("start_btn", "<Button-1>", self._click_start)
        self.canvas.tag_bind("stop_btn", "<Button-1>", self._click_stop)
        self.canvas.tag_bind("goal_minus", "<Button-1>", self._click_goal_minus)
        self.canvas.tag_bind("goal_plus", "<Button-1>", self._click_goal_plus)
        self.canvas.tag_bind("graph_minus", "<Button-1>", self._click_graph_minus)
        self.canvas.tag_bind("graph_plus", "<Button-1>", self._click_graph_plus)
        self.canvas.tag_bind("view_report", "<Button-1>", self._click_view_report)
        self.canvas.tag_bind("report_now", "<Button-1>", self._click_report_now)
        self.canvas.tag_bind("min_btn", "<Button-1>", self._toggle_collapsed)
        self.canvas.tag_bind("close_btn", "<Button-1>", self._click_close)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    def _collapsed_file(self) -> Path:
        return self.cfg.state_dir / "overlay.json"

    def _load_collapsed(self) -> bool:
        try:
            data = json.loads(self._collapsed_file().read_text(encoding="utf-8"))
            return bool(isinstance(data, dict) and data.get("collapsed"))
        except (OSError, json.JSONDecodeError, AttributeError):
            return False

    def _toggle_collapsed(self, _event: tk.Event) -> str:
        self._collapsed = not self._collapsed
        try:
            self._collapsed_file().write_text(
                json.dumps({"collapsed": self._collapsed}), encoding="utf-8")
        except OSError:
            pass
        self._refresh()
        return "break"

    def _draw_min_button(self, cx: float, cy: float, collapsed: bool) -> None:
        # Custom because the window has no OS title bar (overrideredirect).
        h = self._pxf(8)
        self._round_rect(cx - h, cy - h, cx + h, cy + h, self._pxf(4),
                         fill=SUB_BG, outline=BORDER, width=1, tags="min_btn")
        w = self._pxf(4)
        self.canvas.create_line(cx - w, cy, cx + w, cy, fill=FG,
                                width=max(1, self._px(2)), tags="min_btn")
        if collapsed:
            self.canvas.create_line(cx, cy - w, cx, cy + w, fill=FG,
                                    width=max(1, self._px(2)), tags="min_btn")

    def _draw_close_button(self, cx: float, cy: float) -> None:
        h = self._pxf(8)
        self._round_rect(cx - h, cy - h, cx + h, cy + h, self._pxf(4),
                         fill=SUB_BG, outline=BORDER, width=1, tags="close_btn")
        w = self._pxf(3.5)
        stroke = max(1, self._px(2))
        self.canvas.create_line(cx - w, cy - w, cx + w, cy + w, fill=FG,
                                width=stroke, tags="close_btn")
        self.canvas.create_line(cx - w, cy + w, cx + w, cy - w, fill=FG,
                                width=stroke, tags="close_btn")

    def _draw_title_buttons(self, min_cx: float, cy: float,
                            collapsed: bool) -> None:
        # Close sits immediately left of minimize; minimize keeps its corner.
        self._draw_close_button(min_cx - self._pxf(TITLE_BTN_STEP), cy)
        self._draw_min_button(min_cx, cy, collapsed=collapsed)

    def _draw_fork_chip(self, x0: float, cy: float, right_limit: float) -> None:
        """Green "FORK ACTIVE" chip: the forked director session is running.

        Sized to its own text and clipped at `right_limit`, which is the left
        edge of the close button, so the chip can never slide under the title
        buttons painted over the same row.
        """
        P = self._px
        label = "FORK ACTIVE"
        x1 = min(x0 + self._font_small.measure(label) + 2 * self._pxf(FORK_CHIP_PAD),
                 right_limit)
        if x1 - x0 < P(FORK_CHIP_MIN_W):
            return
        half = self._pxf(FORK_CHIP_H) / 2
        self._round_rect(x0, cy - half, x1, cy + half, self._pxf(7),
                         fill=SUB_BG, outline=GREEN, width=1, tags="fork_chip")
        self.canvas.create_text(
            (x0 + x1) / 2, cy,
            text=self._fit(label, self._font_small, (x1 - x0) - P(6)),
            font=FONT_SMALL, fill=GREEN, tags="fork_chip")

    def _click_close(self, _event: tk.Event) -> str:
        # Deferred: destroying the window from inside a canvas item binding frees
        # the canvas while tk is still walking that item's binding chain, which
        # faults the interpreter. after_idle runs the same teardown as the
        # Escape / right-click bindings once the event has fully unwound.
        self.root.after_idle(self.root.destroy)
        return "break"

    def _refresh_collapsed(self, state: dict | None) -> None:
        P = self._px
        width = self.width
        pad = P(PAD)
        height = P(40)
        self.canvas.config(height=height)
        self._round_card(width, height)
        cy = height / 2
        parts: list[tuple[str, str]] = []
        mode = None
        if state is None:
            parts.append(("loop offline", AMBER))
        else:
            usage = state.get("usage", {})
            five, _ = _norm_window(usage.get("five_hour", {}), FIVE_HOURS_F)
            seven, _ = _norm_window(usage.get("seven_day", {}), WEEK_HOURS_F)
            parts.append((f"5h {five:.0%}", AMBER))
            parts.append((f"wk {seven:.0%}", SILVER))
            for key, window in usage.get("extra", {}).items():
                if isinstance(window, dict) and any(h in key.lower() for h in FABLE_HINTS):
                    fable, _ = _norm_window(window, WEEK_HOURS_F)
                    parts.append((f"Fable {fable:.0%}", PINK))
                    break
            mode = str(state.get("decision", {}).get("mode", "?"))
        if self._control == CONTROL_STOPPED:
            # The switch is authoritative even when the loop is offline or its
            # last state predates the click.
            mode = "stopped"
        # The mode reserves its room first (it is the one word the bar must never
        # garble), then the usage parts fill whatever is left; Fable drops off
        # the end rather than colliding with STOPPED at wide values.
        mode_right = width - pad - P(44)  # clear of the close + minimize pair
        mode_text = ""
        # Starts at mode_right, not at the card edge: with no decision to show
        # (loop offline, or a mode that came back empty) the strip still has to
        # stop short of the close and minimize buttons, which are drawn over it.
        mode_left = mode_right
        if mode:
            mode_text = self._fit(mode.upper(), self._font_small,
                                  mode_right - pad)
            mode_left = mode_right - self._font_small.measure(mode_text) - P(10)
        # Short form here: the sentence the expanded band uses cannot fit beside
        # the mode word at any DPI, and the half it loses is the weekly reading.
        stop_text = self._stop_text(short=True)
        band_right = mode_left - P(6)
        if stop_text and band_right > pad + P(40):
            # The band takes the whole usage strip rather than sharing it: at
            # this width the readouts and the band cannot both fit unclipped.
            self._draw_stop_band(pad - P(4), cy - P(9), band_right, cy + P(9),
                                 stop_text)
        else:
            x = pad
            for text, color in parts:
                text_w = self._font_bold.measure(text)
                if x + text_w > mode_left:
                    break
                self.canvas.create_text(x, cy, text=text, font=FONT_BOLD,
                                        fill=color, anchor="w")
                x += text_w + P(12)
        if mode_text:
            self.canvas.create_text(mode_right, cy, text=mode_text,
                                    font=FONT_SMALL, anchor="e",
                                    fill=MODE_COLORS.get(mode, FG))
        self._draw_title_buttons(width - P(30), cy, collapsed=True)
        self._gear_center = None
        self.root.update_idletasks()
        self._place()
        self._after_id = self.root.after(
            self.cfg.overlay_refresh_seconds * 1000, self._refresh)

    def _px(self, value: float) -> int:
        return round(value * self.s)

    def _pxf(self, value: float) -> float:
        return value * self.s

    def _sundial_anchor(self) -> tuple[int, int]:
        path = self.cfg.sundial_shell_path
        if path is None:
            return FALLBACK_ANCHOR
        try:
            shell = json.loads(path.read_text(encoding="utf-8"))
            return int(shell["windowX"]), int(shell["windowY"])
        except (OSError, ValueError, KeyError, TypeError):
            return FALLBACK_ANCHOR

    def _place(self) -> None:
        if self._drag_origin is not None:
            return
        ax, ay = self._sundial_anchor()
        width = self.width
        height = self.root.winfo_reqheight()
        vx, vy, vw, vh = _virtual_screen()
        x = min(max(ax + self.delta[0], vx), max(vx, vx + vw - width))
        y = min(max(ay + self.delta[1], vy), max(vy, vy + vh - height))
        geo = f"{width}x{height}+{x}+{y}"
        self.root.geometry(geo)
        self.root.update_idletasks()
        # A position request on a not-yet-mapped overrideredirect window can be
        # silently dropped; verify and re-issue so every refresh self-heals.
        if (self.root.winfo_x(), self.root.winfo_y()) != (x, y):
            self.root.geometry(geo)
        self.root.lift()
        self.root.attributes("-topmost", True)

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_origin = (event.x_root, event.y_root,
                             self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event: tk.Event) -> None:
        if self._drag_origin is None:
            return
        sx, sy, wx, wy = self._drag_origin
        self.root.geometry(f"+{wx + event.x_root - sx}+{wy + event.y_root - sy}")

    def _drag_end(self, _event: tk.Event) -> None:
        if self._drag_origin is None:
            return
        ax, ay = self._sundial_anchor()
        self.delta = (self.root.winfo_x() - ax, self.root.winfo_y() - ay)
        self._drag_origin = None

    def _load_state(self) -> dict | None:
        try:
            state = json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return state if isinstance(state, dict) else None

    def _round_rect(self, x0: float, y0: float, x1: float, y1: float,
                    r: float, **kwargs) -> None:
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _round_card(self, width: int, height: int) -> None:
        m = self._pxf(CARD_MARGIN)
        self._round_rect(m, m, width - m, height - m, self._pxf(CARD_RADIUS),
                         fill=CARD_BG, outline=BORDER, width=1)

    def _fit(self, text: str, font: tkfont.Font, max_px: float) -> str:
        if font.measure(text) <= max_px:
            return text
        while text and font.measure(f"{text}...") > max_px:
            text = text[:-1]
        return f"{text}..."

    def _on_wheel(self, event: tk.Event) -> None:
        step = -1 if event.delta > 0 else 1
        new_idx = min(max(self._scroll_idx + step, 0), self._max_scroll)
        if new_idx != self._scroll_idx:
            self._scroll_idx = new_idx
            self._refresh()

    def _toggle_throttle(self, _event: tk.Event) -> str:
        payload = {"active": not self._throttle, "since": utcnow().isoformat()}
        try:
            self.cfg.throttle_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass
        self._refresh()
        return "break"

    def _draw_button(self, x0: float, y0: float, x1: float, y1: float,
                     active: bool) -> None:
        self._round_rect(x0, y0, x1, y1, self._pxf(14),
                         fill=BTN_ACTIVE_BG if active else SUB_BG,
                         outline=RED if active else AMBER, width=1,
                         tags="throttle_btn")
        label = "THROTTLE ON - tap to stop" if active else "FULL THROTTLE"
        self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                                font=FONT_BOLD, fill=FG if active else AMBER,
                                tags="throttle_btn")

    def _set_control(self, mode: str) -> str:
        self._control = write_control(self.cfg, mode)
        self._refresh()
        return "break"

    def _click_start(self, _event: tk.Event) -> str:
        return self._set_control(CONTROL_RUNNING)

    def _click_stop(self, _event: tk.Event) -> str:
        return self._set_control(CONTROL_STOPPED)

    def _draw_ctl_button(self, x0: float, y0: float, x1: float, y1: float,
                         kind: str) -> None:
        # kind is "start" or "stop"; the active one is whichever matches the
        # control file, and it reads as a state ("RUNNING"/"STOPPED") rather
        # than as an action, so the current mode is never ambiguous.
        running = self._control != CONTROL_STOPPED
        active = running if kind == "start" else not running
        color = GREEN if kind == "start" else RED
        if kind == "start":
            label = "RUNNING" if active else "START"
        else:
            label = "STOPPED" if active else "STOP"
        self._round_rect(x0, y0, x1, y1, self._pxf(14),
                         fill=BTN_ACTIVE_BG if active else SUB_BG,
                         outline=color, width=1, tags=f"{kind}_btn")
        self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                                font=FONT_BOLD, fill=FG if active else color,
                                tags=f"{kind}_btn")

    def _step_goal(self, delta: float) -> str:
        # Snap to the 5-point grid first, so a goal typed as 87% still lands on
        # 90 / 85 rather than drifting off-grid forever. A goal that is not a
        # number at all starts from the default: round() would raise here and
        # the taps are the only way to repair the file from the overlay.
        current = self._goal if isinstance(self._goal, (int, float)) else GOAL_FALLBACK
        if not math.isfinite(current):
            current = GOAL_FALLBACK
        pct = round(current * 100.0 / 5.0) * 5.0 + delta * 100.0
        self._goal = write_goal(self.cfg, min(max(pct, 5.0), 100.0) / 100.0)
        self._refresh()
        return "break"

    def _click_goal_minus(self, _event: tk.Event) -> str:
        return self._step_goal(-GOAL_STEP)

    def _click_goal_plus(self, _event: tk.Event) -> str:
        return self._step_goal(GOAL_STEP)

    def _draw_goal_row(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """"- GOAL 90% +": the weekly utilization the loop stops the week at."""
        P = self._px
        self._sub_card(x0, y0, x1, y1, outline=AMBER)
        btn_w = P(GOAL_STEP_BTN_W)
        mid_y = (y0 + y1) / 2
        for tag, label, bx0 in (("goal_minus", "-", x0),
                                ("goal_plus", "+", x1 - btn_w)):
            self._round_rect(bx0 + P(2), y0 + P(2), bx0 + btn_w - P(2), y1 - P(2),
                             self._pxf(8), fill=CARD_BG, outline=AMBER, width=1,
                             tags=tag)
            self.canvas.create_text((bx0 + btn_w / 2), mid_y, text=label,
                                    font=FONT_BOLD, fill=AMBER, tags=tag)
        text = self._fit(f"GOAL {self._goal:.0%}", self._font_bold,
                         (x1 - x0) - 2 * btn_w - P(8))
        self.canvas.create_text((x0 + x1) / 2, mid_y, text=text,
                                font=FONT_BOLD, fill=AMBER)

    def _step_workers(self, delta: int) -> str:
        """Nudge the worker lane count; the loop re-derives concurrency from it.

        Clamped to the graph's own bounds, and written to state/graph.json so
        the checked-in config.json is never rewritten by a click. Only the one
        field goes into the override: writing the whole graph would copy the
        models into it too, and config.json would stop mattering after a tap.
        """
        count = int(read_graph(self.cfg)[WORKERS]["count"]) + delta
        count = min(max(count, COUNT_MIN), COUNT_MAX)
        self._graph = write_graph(self.cfg, {WORKERS: {"count": count}})
        self._refresh()
        return "break"

    def _click_graph_minus(self, _event: tk.Event) -> str:
        return self._step_workers(-1)

    def _click_graph_plus(self, _event: tk.Event) -> str:
        return self._step_workers(1)

    def _active_note(self) -> str | None:
        """"active vs configured: ..." while a tier is off its configured model.

        None the rest of the time: when every tier is running what config.json
        says, the note is noise, and the rungs already carry the models.
        """
        parts = []
        for tier in TIERS:
            row = self._active.get(tier) if isinstance(self._active, dict) else None
            if not isinstance(row, dict) or not row.get("differs"):
                continue
            parts.append(f"{tier} {short_model(row.get('model'))} "
                         f"(cfg {short_model(row.get('configured'))})")
        return "active vs configured: " + "; ".join(parts) if parts else None

    def _ladder_height(self) -> int:
        """What `_draw_graph_ladder` will occupy, so the card can grow for it."""
        P = self._px
        note = (self._text_row(LADDER_NOTE_H, self._font_small)
                if self._active_note() else 0)
        rung = max(P(LADDER_RUNG_H), self._share_geometry()[2])
        return (self._text_row(LADDER_LABEL_H, self._font_small) + 3 * rung
                + 2 * P(LADDER_RUNG_GAP) + note)

    def _text_row(self, design: float, font: tkfont.Font) -> int:
        """A row at least one line of `font` tall, whatever the DPI.

        Design pixels scale by the DPI factor and text by the font's own
        scaling, and past about 1.5x the second outgrows the first: a row
        sized only in design pixels starts printing over its neighbour.
        """
        return max(self._px(design), font.metrics("linespace") + self._px(2))

    def _share_geometry(self) -> tuple[float, float, int]:
        """(centre of the input bar, centre of the output bar, rung height).

        Offsets from the top of a rung, measured from the fonts rather than
        assumed. The share labels are the tallest things in a rung - three
        times the height of the 4px band each annotates - and text grows with
        the display's DPI faster than design pixels do, so fixed offsets let
        the two labels touch at 1.5x and print over the bold "xN" at 2x.
        """
        P = self._px
        mono = self._font_mono.metrics("linespace")
        text_row = self._text_row(LADDER_TEXT_H, self._font_bold)
        first = max(P(LADDER_BAND_Y) + P(LADDER_BAND_H) / 2,
                    text_row + mono / 2)
        second = first + max(P(LADDER_BAND_ROW), mono + P(2))
        return first, second, max(P(LADDER_RUNG_H),
                                  int(second + mono / 2 + P(2)))

    def _draw_share_bars(self, tier: str, bar_x: float, ry0: float,
                         right: float, emphasis: bool) -> None:
        """The two token-share bars under one rung: input, then output.

        Width is the tier's share of the window's tokens, not its headcount -
        the "xN" column already says the headcount, and it was never the
        question anyone brought to this panel. Input is everything the tier
        read (input + cache creation + cache read) in blue, output what it
        produced in green, each labelled with its own percentage in a
        monospaced right-aligned column so the six figures line up.

        Every bar draws its track first, so an empty window is three pairs of
        visible 0% bars rather than three blank rows.
        """
        P = self._px
        band_h = max(2, P(LADDER_BAND_H))
        radius = self._pxf(LADDER_BAR_RADIUS)
        share = self._shares.get(tier, {})
        label_w = self._font_mono.measure("out 100%")
        span = max(right - P(LADDER_TEXT_PAD) - label_w - bar_x,
                   P(LADDER_BAR_MIN))
        centres = self._share_geometry()[:2]
        for centre, (kind, key, color) in zip(
                centres,
                (("in", "input_share", BLUE), ("out", "output_share", GREEN))):
            value = float(share.get(key, 0.0) or 0.0)
            value = min(max(value, 0.0), 1.0)
            top = ry0 + centre - band_h / 2
            self._round_rect(bar_x, top, bar_x + span, top + band_h, radius,
                             fill=TRACK, outline="",
                             tags=("ladder", f"track_{kind}_{tier}"))
            if value > 0:
                self._round_rect(bar_x, top, bar_x + span * value,
                                 top + band_h, radius, fill=color, outline="",
                                 tags=("ladder", f"rung_{kind}_{tier}"))
            self.canvas.create_text(
                right, ry0 + centre, text=f"{kind} {value:.0%}",
                font=FONT_MONO, anchor="e", fill=color if emphasis else DIM,
                tags=("ladder", f"share_{kind}_{tier}"))

    def _draw_graph_ladder(self, x0: float, y0: float, x1: float) -> None:
        """The AGENTIC GRAPH ladder chart: a rung per tier, top to bottom.

        Each rung is a label line - tier name left, the model that tier is
        ACTUALLY running on next to it, "xN" right-aligned - over two thin
        bars carrying that tier's share of the window's input and output
        tokens, and all three hang off one spine on the left.

        The model shown is the live one, re-derived every refresh: the fork's
        handover record for the executive, the running rows' `model_used` for
        the workers, the configured model (or its fallback while limited) for
        the advisory lenses. When it is not what config.json asks for, the
        configured id follows it dimmed as "cfg <model>" and the ladder grows
        a one-line note - which is the only way to see, from the panel, that
        the fork is still running on yesterday's executive model. A dot in
        front of the model id marks a tier whose model state/graph.json pins,
        i.e. one config.json can be edited for without any effect at all.

        The bars are under the text rather than behind it on purpose: a bar
        wide enough to mean anything ends somewhere in the middle of the row,
        and that edge lands in the middle of a word.

        The three "xN" share one right edge, and so do the six share labels,
        which is what lets them be compared down the rungs instead of read one
        at a time.
        """
        P = self._px
        rung_h = max(P(LADDER_RUNG_H), self._share_geometry()[2])
        gap = P(LADDER_RUNG_GAP)
        label_h = self._text_row(LADDER_LABEL_H, self._font_small)
        # The rung's own text line, sized to the tallest font on it (the bold
        # "xN"), so a tier name can never overhang the rung above.
        text_row = self._text_row(LADDER_TEXT_H, self._font_bold)
        top = y0 + label_h
        bar_x = x0 + P(LADDER_BAR_X)
        step_w = P(LADDER_STEP_W)
        gutter = 2 * step_w + P(LADDER_STEP_GAP)
        num_x = x1 - gutter - P(LADDER_TEXT_PAD)
        blocks = dict(zip(TIERS, tiers_of(self._graph)))
        workers = blocks[WORKERS]
        surge = int(workers.get("surge_count", workers["count"]))

        self.canvas.create_text(x0, y0 + label_h / 2,
                                text="AGENTIC GRAPH", font=FONT_SMALL,
                                fill=DIM, anchor="w", tags="ladder")
        if surge > int(workers["count"]):
            # The surge budget has no column of its own (a second number would
            # break the xN alignment), so the label names it.
            self.canvas.create_text(x1, y0 + label_h / 2,
                                    text=f"surge x{surge}", font=FONT_SMALL,
                                    fill=DIM, anchor="e", tags="ladder")

        for i, tier in enumerate(TIERS):
            block = blocks[tier]
            count = int(block["count"])
            is_workers = tier == WORKERS
            ry0 = top + i * (rung_h + gap)
            ry1 = ry0 + rung_h
            mid = ry0 + text_row / 2
            # The spine, drawn a segment at a time so the worker tier's own
            # stretch of it can carry the emphasis colour.
            spine_x = x0 + P(LADDER_SPINE_X)
            self.canvas.create_rectangle(
                spine_x, ry0, spine_x + max(1, P(LADDER_SPINE_W)),
                ry1 + (gap if i < len(TIERS) - 1 else 0),
                fill=BLUE if is_workers else BORDER, outline="",
                tags="ladder_spine")

            self._draw_share_bars(tier, bar_x, ry0, num_x, is_workers)

            name = tier.upper()
            name_x = bar_x + P(LADDER_TEXT_PAD)
            self.canvas.create_text(name_x, mid, text=name, font=FONT_SMALL,
                                    fill=FG if is_workers else DIM, anchor="w",
                                    tags="ladder")
            counts = f"x{count}"
            self.canvas.create_text(num_x, mid, text=counts, font=FONT_BOLD,
                                    fill=BLUE if is_workers else SILVER,
                                    anchor="e", tags="ladder")
            # Its own column, halfway along the bar span, so the model ids line
            # up down the rungs the way the counts do - but never left of the
            # tier name, which is wider at some DPIs than at others.
            left = name_x + self._font_small.measure(name)
            count_left = num_x - self._font_bold.measure(counts)
            # Whether THIS rung's own primary is the model marked limited; the
            # tag it draws takes its width out of the model column below rather
            # than overprinting it.
            limited = bool(self._limited and self._limited == block["model"])
            # One shared column for all three model ids, so they can be read
            # down the rungs the way the counts are - placed as far left as the
            # longest tier name allows rather than at mid-span, because the
            # dimmed "->fallback" that follows needs the width that buys.
            label_w = max(self._font_small.measure(t.upper()) for t in TIERS)
            floor_x = left + P(LADDER_TEXT_PAD)
            model_x = min(bar_x + (num_x - bar_x) / 2,
                          name_x + label_w + P(LADDER_TEXT_PAD))
            model_x = max(model_x, floor_x)
            # The model in use, not the model configured. They are the same
            # thing most of the time and the difference is exactly what an
            # operator needs to see when they are not.
            active = self._active.get(tier) if isinstance(self._active, dict) else None
            active = active if isinstance(active, dict) else {}
            running = active.get("models") or [block["model"]]
            text = short_model(active.get("model") or block["model"])
            if len(running) > 1:
                # Two models running in one tier: the rung names the first and
                # counts the rest rather than clipping a list nobody can read.
                text += f" +{len(running) - 1}"
            # After the model, dimmed: the configured id when the live one is
            # not it ("opus-5  cfg fable-5-1"), otherwise the tier's fallback,
            # which is where this rung goes when its model is limited
            # ("fable-5-1 ->fable-5"). One suffix, because two would not fit
            # and the first is always the more urgent of the two.
            fallback = block.get("fallback")
            differs = bool(active.get("differs"))
            suffix = (f"cfg {short_model(active.get('configured'))}" if differs
                      else (f"->{short_model(fallback)}" if fallback else ""))
            # A dot before the model id when state/graph.json pins that tier's
            # model: config.json's own model for it is dead, and the panel is
            # where an operator looks first.
            pinned = bool(active.get("pinned"))
            pin_w = P(LADDER_PIN_W) if pinned else 0

            # The rung's text line is laid out from the right: the count column
            # is fixed, the red limited tag sits beside it, the dimmed suffix
            # follows the model id, and the model id takes what is left. The
            # suffix is never ellipsised - "cfg fab..." names no model, which is
            # the one thing it exists to do - so the width comes out of the tag
            # instead: the model column slides left, then the tag shortens to
            # "LIM", then it becomes a red dot beside the count. A tier on a
            # fallback because its primary is limited is exactly the state the
            # cfg label was added to show, so showing the tag and hiding the
            # configured id would be the wrong trade at every DPI.
            gap = P(4)
            pad = P(LADDER_TEXT_PAD)
            dot_w = 2 * self._px(LADDER_LIMIT_DOT_R) + P(6)
            suffix_w = self._font_small.measure(suffix) if suffix else 0
            want = self._font_small.measure(text) + gap + suffix_w

            def tag_w(form: str) -> float:
                if not form:
                    return 0
                if form == LIMITED_DOT:
                    return dot_w
                return self._font_small.measure(form) + P(6)

            def room(form: str, mx: float) -> float:
                return count_left - tag_w(form) - (mx + pin_w) - pad

            tag = LIMITED_TAG if limited else ""
            if suffix:
                options = [(tag, model_x)]
                if differs:
                    # Only for the configured id: a "->fallback" that does not
                    # fit is worth no rearrangement, and dropping it costs an
                    # operator nothing they cannot read off `tracker.py graph`.
                    forms = ([LIMITED_TAG, LIMITED_SHORT, LIMITED_DOT]
                             if limited else [""])
                    options = [(f, mx) for f in forms
                               for mx in (model_x, floor_x)]
                for cand_tag, cand_x in options:
                    if room(cand_tag, cand_x) >= want:
                        tag, model_x = cand_tag, cand_x
                        break
                else:
                    suffix = ""
            right = count_left - tag_w(tag)
            if tag == LIMITED_DOT:
                r = self._pxf(LADDER_LIMIT_DOT_R)
                cx = count_left - P(4) - r
                self.canvas.create_oval(cx - r, mid - r, cx + r, mid + r,
                                        fill=RED, outline="",
                                        tags=("ladder", f"limited_{tier}"))
            elif tag:
                self.canvas.create_text(count_left - P(4), mid, text=tag,
                                        font=FONT_SMALL, fill=RED, anchor="e",
                                        tags=("ladder", f"limited_{tier}"))
            if pinned:
                # A dot rather than a word: the rung has no room for one, and
                # the wording is in the loop's startup log and `tracker.py
                # graph` ("state/graph.json pins executive.model=...").
                r = self._pxf(LADDER_PIN_R)
                cx = model_x + pin_w / 2
                self.canvas.create_oval(cx - r, mid - r, cx + r, mid + r,
                                        fill=DIM, outline="",
                                        tags=("ladder", f"pinned_{tier}"))
            model_x += pin_w
            model = self._fit(text, self._font_small, (right - model_x) - pad)
            self.canvas.create_text(model_x, mid, text=model, anchor="w",
                                    font=FONT_SMALL,
                                    fill=SILVER if is_workers else DIM,
                                    tags=("ladder", f"model_{tier}"))
            fb_x = model_x + self._font_small.measure(model) + gap
            if suffix and right - fb_x - pad >= suffix_w:
                self.canvas.create_text(
                    fb_x, mid, text=suffix, anchor="w", font=FONT_SMALL,
                    fill=DIM,
                    tags=("ladder",
                          f"cfg_{tier}" if differs else f"fallback_{tier}"))

            if is_workers:
                for tag, label, bx0 in (("graph_minus", "-", x1 - gutter),
                                        ("graph_plus", "+", x1 - step_w)):
                    self._round_rect(bx0, ry0, bx0 + step_w, ry0 + text_row,
                                     self._pxf(6), fill=CARD_BG, outline=BLUE,
                                     width=1, tags=tag)
                    self.canvas.create_text(bx0 + step_w / 2, mid, text=label,
                                            font=FONT_BOLD, fill=BLUE, tags=tag)

        note = self._active_note()
        if note:
            # Under the last rung, in the amber the panel uses for "something
            # is not what you set it to". `_ladder_height` reserved the row.
            note_y = (top + 3 * rung_h + 2 * gap
                      + self._text_row(LADDER_NOTE_H, self._font_small) / 2)
            self.canvas.create_text(
                x0, note_y, text=self._fit(note, self._font_small, x1 - x0),
                font=FONT_SMALL, fill=AMBER, anchor="w",
                tags=("ladder", "ladder_note"))

    def _click_view_report(self, _event: tk.Event) -> str:
        # Dead until there is a page: a click with no report must not launch
        # the shell on a path that does not exist.
        if self._report is not None:
            open_report(self.cfg)
        return "break"

    def _click_report_now(self, _event: tk.Event) -> str:
        # Off-thread: parsing the transcripts takes seconds and the overlay
        # redraws on a timer, so it must not block the Tk event loop.
        generate_async(self.cfg, "manual")
        self._refresh()
        return "break"

    def _draw_report_row(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """VIEW REPORT (dimmed until one exists) beside a REPORT NOW tap target."""
        P = self._px
        gap = P(CTL_BTN_GAP)
        now_w = P(REPORT_NOW_W)
        view_x1 = max(x0 + P(60), x1 - now_w - gap)
        have = self._report is not None
        color = BLUE if have else DIM
        self._round_rect(x0, y0, view_x1, y1, self._pxf(12), fill=SUB_BG,
                         outline=color, width=1, tags="view_report")
        label = "VIEW REPORT" if have else "no report yet"
        self.canvas.create_text((x0 + view_x1) / 2, (y0 + y1) / 2,
                                text=self._fit(label, self._font_bold,
                                               (view_x1 - x0) - P(10)),
                                font=FONT_BOLD, fill=color, tags="view_report")
        self._round_rect(view_x1 + gap, y0, x1, y1, self._pxf(12), fill=SUB_BG,
                         outline=GREEN, width=1, tags="report_now")
        self.canvas.create_text((view_x1 + gap + x1) / 2, (y0 + y1) / 2,
                                text=self._fit("REPORT NOW", self._font_small,
                                               (x1 - view_x1 - gap) - P(8)),
                                font=FONT_SMALL, fill=GREEN, tags="report_now")

    def _draw_goal_tick(self, cx: float, cy: float, frac: float) -> None:
        """Amber tick on the weekly ring at the goal, inside the ring's stroke.

        The ring is drawn start=90, extent=-359.9 (12 o'clock, clockwise), so a
        fraction maps to 90 - frac*359.9 degrees in the same canvas angle space.
        """
        frac = min(max(frac, 0.0), 1.0)
        r = self._pxf(GAUGE_RADIUS)
        half = self._pxf(GAUGE_STROKE) / 2 + self._pxf(1.5)
        a = math.radians(90.0 - frac * 359.9)
        dx, dy = math.cos(a), -math.sin(a)
        self.canvas.create_line(cx + dx * (r - half), cy + dy * (r - half),
                                cx + dx * (r + half), cy + dy * (r + half),
                                fill=AMBER, width=max(2, self._px(2)))

    def _stop_text(self, short: bool = False) -> str | None:
        """The red band's wording; `short` is the collapsed bar's one-line form.

        Collapsed, the band shares its row with the mode word (already reading
        STOPPED), so the prefix is dead weight there while the two numbers are
        the whole point - and the long form only ever fits by dropping them.
        """
        if not self._stop:
            return None
        try:
            goal = float(self._stop.get("goal", self._goal))
            weekly = float(self._stop.get("weekly", 0.0))
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(goal) and math.isfinite(weekly)):
            return None
        if short:
            return f"GOAL {goal:.0%} HIT ({weekly:.0%})"
        return f"STOPPED: weekly goal {goal:.0%} reached ({weekly:.0%})"

    def _draw_stop_band(self, x0: float, y0: float, x1: float, y1: float,
                        text: str) -> None:
        # Tagged so the layout tests can measure it against the title buttons.
        self._round_rect(x0, y0, x1, y1, self._pxf(9), fill=BTN_ACTIVE_BG,
                         outline=RED, width=1, tags="stop_band")
        label = self._fit(text, self._font_small, (x1 - x0) - self._px(14))
        self.canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                                font=FONT_SMALL, fill=FG, tags="stop_band")

    def _sub_card(self, x0: float, y0: float, x1: float, y1: float,
                  outline: str = "") -> None:
        self._round_rect(x0, y0, x1, y1, self._pxf(10), fill=SUB_BG,
                         outline=outline, width=1 if outline else 0)

    def _draw_gear_face(self, cx: float, cy: float) -> None:
        f = self._pxf
        self.canvas.create_oval(cx - f(12), cy - f(12), cx + f(12), cy + f(12),
                                fill=GEAR_BODY, outline="", tags="gear_face")
        for dx in (-f(4.5), f(4.5)):
            self.canvas.create_oval(cx + dx - f(1.8), cy - f(4.5),
                                    cx + dx + f(1.8), cy - f(1.0),
                                    fill=CARD_BG, outline="", tags="gear_face")
        self.canvas.create_arc(cx - f(5.5), cy - f(2.0), cx + f(5.5), cy + f(7.0),
                               start=200, extent=140, style="arc",
                               outline=CARD_BG, width=max(2, self._px(2)),
                               tags="gear_face")

    def _draw_gear_teeth(self) -> None:
        try:
            self.canvas.delete("gear_teeth")
            if self._gear_center is None:
                return
            cx, cy = self._gear_center
            outer = self._pxf(GEAR_OUTER)
            valley = self._pxf(GEAR_VALLEY)
            points: list[float] = []
            for i in range(GEAR_TEETH):
                base = math.radians(self._gear_angle + i * 360.0 / GEAR_TEETH)
                for delta_deg, radius in (
                    (-14.0, valley), (-8.0, outer),
                    (8.0, outer), (14.0, valley),
                ):
                    a = base + math.radians(delta_deg)
                    points.append(cx + radius * math.sin(a))
                    points.append(cy - radius * math.cos(a))
            self.canvas.create_polygon(points, fill=GEAR_TOOTH, outline="",
                                       tags="gear_teeth")
            self.canvas.tag_raise("gear_face")
        except tk.TclError:
            pass

    def _animate(self) -> None:
        try:
            self._gear_angle = (self._gear_angle + GEAR_STEP_DEG) % 360.0
            self._draw_gear_teeth()
            self.root.after(GEAR_FRAME_MS, self._animate)
        except tk.TclError:
            return

    def _gauge(self, cx: float, frac: float, color: str, label: str) -> None:
        frac = min(max(frac, 0.0), 1.0)
        r = self._pxf(GAUGE_RADIUS)
        cy = self._pxf(GAUGE_CY)
        stroke = self._px(GAUGE_STROKE)
        bbox = (cx - r, cy - r, cx + r, cy + r)
        self.canvas.create_arc(bbox, start=90, extent=-359.9, style="arc",
                               outline=TRACK, width=stroke)
        if frac > 0:
            self.canvas.create_arc(bbox, start=90, extent=-max(frac * 359.9, 3.0),
                                   style="arc", outline=color, width=stroke)
        self.canvas.create_text(cx, cy - self._pxf(9), text=f"{frac:.0%}",
                                font=FONT_BIG, fill=FG)
        self.canvas.create_text(cx, cy + self._pxf(13), text=label,
                                font=FONT_SMALL, fill=DIM)

    def _refresh(self) -> None:
        P = self._px
        width = self.width
        pad = P(PAD)
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        state = self._load_state()
        self._control = read_control(self.cfg)
        # Both files are re-read every refresh, like the control flag: the loop
        # writes stop.json on its own tick and the goal may change from the CLI.
        self._goal = read_goal(self.cfg)
        self._stop = read_stop(self.cfg)
        # The handover file is written by the loop when it launches the forked
        # director and updated when it ends, so it is re-read here too.
        self._fork = fork_active(self.cfg)
        # config.json itself is re-read here, by mtime: the graph an operator
        # edits lives in that file, and the panel showing yesterday's models
        # until someone restarts the overlay is exactly the bug this closes.
        reload_config(self.cfg)
        # The graph and the last report are both files another process writes
        # (the CLI, the loop's report thread), so they are re-read every pass.
        self._graph = read_graph(self.cfg)
        # Which model the dispatcher has marked limited, if any: it expires on
        # its own (fallback_minutes), so it is re-read rather than remembered.
        self._limited = limited_model(self.cfg)
        # What each tier is running on right now (the handover record, the
        # running rows) against what the graph configures, and the tier token
        # shares the loop last wrote. Both are files, both are re-read every
        # refresh, and neither is ever cached across one.
        self._active = active_graph(self.cfg, self._graph)
        self._shares = tier_shares(read_tiers(self.cfg))
        self._report = latest_report(self.cfg)
        self._report_age = report_age(self.cfg)
        self.canvas.delete("all")
        if self._collapsed:
            self._refresh_collapsed(state)
            return
        sessions = _live_sessions(self.cfg)
        self._throttle = _read_throttle(self.cfg)

        rows: list[tuple[str, str, str]] = []
        footer_left = footer_right = ""
        info_line = None
        warn = None
        mode = None
        mode_label = None
        updated_line = None
        five_frac = weekly_frac = fable_frac = 0.0

        if state is None:
            warn = "loop offline - start: python tracker.py run"
        else:
            usage = state.get("usage", {})
            five = usage.get("five_hour", {})
            seven = usage.get("seven_day", {})
            five_frac, reset5_iso = _norm_window(five, FIVE_HOURS_F)
            weekly_frac, reset7_iso = _norm_window(seven, WEEK_HOURS_F)
            for key, window in usage.get("extra", {}).items():
                if isinstance(window, dict) and any(h in key.lower() for h in FABLE_HINTS):
                    fable_frac, _ = _norm_window(window, WEEK_HOURS_F)
                    break

            reset5 = _local_clock(reset5_iso)
            reset7 = _local_clock(reset7_iso, with_day=True)
            parts = []
            if reset5:
                parts.append(f"5h {reset5}")
            if reset7:
                parts.append(f"wk {reset7}")
            if parts:
                info_line = " · ".join(parts)

            mode = str(state.get("decision", {}).get("mode", "?"))
            mode_label = mode.upper()
            queue = state.get("queue", {})
            if queue.get("running_local"):
                mode_label += " · LOCAL"

            dist = state.get("distribution", {})
            total = float(dist.get("total_tokens", 0) or 0)
            for share in dist.get("shares", []):
                tokens = float(share.get("tokens", 0) or 0)
                kind = share.get("kind", "foreign")
                dot = {"main": GREEN, "own": BLUE}.get(kind, SILVER)
                pct = f" · {tokens / total:.0%}" if total > 0 else ""
                rows.append((dot, str(share.get("label", "?")),
                             f"{_fmt_tokens(tokens)}{pct}"))
            if not rows:
                rows.append((TRACK, "no burn in window", ""))

            footer_left = f"{dist.get('window_minutes', '?')}m · {_fmt_tokens(total)}"
            footer_right = (f"{queue.get('running', 0)}R "
                            f"{queue.get('pending_heavy', 0)}H/{queue.get('pending_light', 0)}L")
            if queue.get("running_local"):
                footer_right += f" · {queue['running_local']} on Qwen"

            at = parse_iso(state.get("at"))
            stale_after = max(STALE_FACTOR * self.cfg.poll_seconds, STALE_MIN_SECONDS)
            if at is not None and utcnow() - at > timedelta(seconds=stale_after):
                warn = f"loop offline - last tick {at.astimezone().strftime('%H:%M')}"
            elif state.get("fetch_error"):
                if "429" in str(state["fetch_error"]):
                    warn = "endpoint rate-limited - using cached usage"
                else:
                    warn = "usage fetch failing - using cached usage"
            if at is not None:
                age_s = (utcnow() - at).total_seconds()
                updated_line = ("updated just now" if age_s < 30
                                else f"updated {int(age_s // 60)}m ago")

        if self._control == CONTROL_STOPPED:
            # The operator switch wins over the last written decision, so a
            # stopped overlay never claims to be pacing - and it still says so
            # when the loop is offline and there is no decision at all.
            mode = "stopped"
            mode_label = "STOPPED"

        main_ids = set(self.cfg.main_session_ids)
        alive = {s["sid"]: s for s in sessions}
        self._order = [sid for sid in self._order
                       if sid in alive and sid not in main_ids]
        for sess in sessions:
            if sess["sid"] not in main_ids and sess["sid"] not in self._order:
                self._order.append(sess["sid"])
        main_sess = next((s for s in sessions if s["sid"] in main_ids), None)
        others = [alive[sid] for sid in self._order]
        self._max_scroll = max(0, len(others) - OTHERS_VISIBLE)
        self._scroll_idx = min(self._scroll_idx, self._max_scroll)
        visible = others[self._scroll_idx:self._scroll_idx + OTHERS_VISIBLE]
        shown = (([("main", main_sess)] if main_sess else [])
                 + [("other", s) for s in visible])
        n_total = len(sessions)

        card_h = P(SESSION_CARD_H)
        card_gap = P(SESSION_CARD_GAP)
        row_h = P(ROW_H)
        gauge_bottom = P(GAUGE_CY) + P(GAUGE_RADIUS)
        sess_header_y = gauge_bottom + (P(54) if info_line else P(34))
        cards_y = sess_header_y + P(16)
        cards_h = (len(shown) * (card_h + card_gap)) if shown else P(18)
        dist_header_y = cards_y + cards_h + P(16)
        stop_text = self._stop_text()
        # The stop band sits between the mode label and the first share row, so
        # the rows move down by exactly the band it makes room for.
        band_y0 = dist_header_y + P(12)
        band_h = P(STOP_BAND_H) if stop_text else 0
        rows_y = dist_header_y + P(26) + (band_h + P(6) if stop_text else 0)
        footer_y = rows_y + len(rows) * row_h + P(12)
        # AGENTIC GRAPH ladder, WEEKLY GOAL row, START/STOP, FULL THROTTLE,
        # then the report row and its age line: the card grows by each block
        # plus its gap, so nothing overlaps the bottom status line at any DPI.
        graph_y = footer_y + P(20)
        goal_y = graph_y + self._ladder_height() + P(8)
        ctl_y = goal_y + P(GOAL_ROW_H) + P(BTN_ROW_GAP)
        btn_y = ctl_y + P(BTN_H) + P(BTN_ROW_GAP)
        rep_y = btn_y + P(BTN_H) + P(BTN_ROW_GAP)
        age_y = rep_y + P(REPORT_BTN_H) + P(REPORT_AGE_H) / 2
        height = rep_y + P(REPORT_BTN_H) + P(REPORT_AGE_H) + P(30)

        self.canvas.config(height=height)
        self._round_card(width, height)
        # Top-right, above the Fable gauge and inside the corner radius.
        self._draw_title_buttons(width - P(34), P(22), collapsed=False)
        if self._fork:
            # Left end of the same row; the limit is the close button's left
            # edge (its centre is one TITLE_BTN_STEP left of minimize).
            close_left = width - P(34) - self._pxf(TITLE_BTN_STEP) - self._pxf(8)
            self._draw_fork_chip(pad - P(6), P(22), close_left - self._pxf(6))

        center_cx = width // 2
        self._gauge(pad + P(48), five_frac, AMBER, "5 hours")
        self._gauge(width - pad - P(48), fable_frac, PINK, "Fable")

        r = self._pxf(GAUGE_RADIUS)
        cy = self._pxf(GAUGE_CY)
        bbox = (center_cx - r, cy - r, center_cx + r, cy + r)
        self.canvas.create_arc(bbox, start=90, extent=-359.9, style="arc",
                               outline=TRACK, width=P(GAUGE_STROKE))
        wf = min(max(weekly_frac, 0.0), 1.0)
        if wf > 0:
            self.canvas.create_arc(bbox, start=90, extent=-max(wf * 359.9, 3.0),
                                   style="arc", outline=SILVER, width=P(GAUGE_STROKE))
        self._draw_goal_tick(center_cx, cy, self._goal)
        self._gear_center = (center_cx, cy)
        self._draw_gear_face(center_cx, cy)
        self._draw_gear_teeth()
        self.canvas.create_text(center_cx, gauge_bottom + P(14),
                                text=f"weekly {wf:.0%}", font=FONT_SMALL, fill=SILVER)

        if info_line:
            self.canvas.create_text(width / 2, gauge_bottom + P(34),
                                    text=info_line, font=FONT_SMALL, fill=DIM)

        self.canvas.create_text(pad, sess_header_y, text="Live sessions",
                                font=FONT_BOLD, fill=FG, anchor="w")
        self.canvas.create_text(width - pad, sess_header_y,
                                text=f"{n_total} active", font=FONT_SMALL,
                                fill=DIM, anchor="e")
        if shown:
            for i, (kind, sess) in enumerate(shown):
                y0 = cards_y + i * (card_h + card_gap)
                self._sub_card(pad - P(6), y0, width - pad + P(6), y0 + card_h,
                               outline=GREEN if kind == "main" else "")
                name_x = pad + P(6)
                name_y = y0 + P(13)
                detail_y = y0 + P(31)
                if kind == "main":
                    self.canvas.create_oval(pad + P(5), y0 + P(9),
                                            pad + P(13), y0 + P(17),
                                            fill=GREEN, outline="")
                    name_x = pad + P(19)
                name = self._fit(sess["name"], self._font_bold,
                                 (width - pad) - name_x - P(6))
                self.canvas.create_text(name_x, name_y, text=name,
                                        font=FONT_BOLD, fill=FG, anchor="w")
                age_px = self._font_small.measure(sess["age"])
                detail = self._fit(sess["detail"], self._font_small,
                                   (width - pad - P(6)) - (pad + P(6))
                                   - age_px - P(10))
                self.canvas.create_text(pad + P(6), detail_y, text=detail,
                                        font=FONT_SMALL, fill=DIM, anchor="w")
                self.canvas.create_text(width - pad - P(6), detail_y,
                                        text=sess["age"],
                                        font=FONT_SMALL, fill=DIM, anchor="e")
            if self._max_scroll > 0:
                first_other = 1 if main_sess else 0
                track_y0 = cards_y + first_other * (card_h + card_gap)
                track_y1 = cards_y + cards_h - card_gap
                track_x = width - pad + P(9)
                bar_w = max(2, P(SCROLLBAR_W))
                self.canvas.create_rectangle(
                    track_x, track_y0, track_x + bar_w, track_y1,
                    fill=TRACK, outline="")
                track_h = track_y1 - track_y0
                thumb_h = max(self._pxf(14), track_h * OTHERS_VISIBLE / len(others))
                thumb_y = track_y0 + (track_h - thumb_h) * (
                    self._scroll_idx / self._max_scroll)
                self.canvas.create_rectangle(
                    track_x, thumb_y, track_x + bar_w, thumb_y + thumb_h,
                    fill=DIM, outline="")
        else:
            self.canvas.create_text(pad, cards_y + P(4), text="no active sessions",
                                    font=FONT_SMALL, fill=DIM, anchor="w")

        self.canvas.create_text(pad, dist_header_y, text="Token distribution",
                                font=FONT_BOLD, fill=FG, anchor="w")
        if mode_label:
            avail = (width - 2 * pad
                     - self._font_bold.measure("Token distribution") - P(10))
            mode_label = self._fit(mode_label, self._font_bold, avail)
            self.canvas.create_text(width - pad, dist_header_y, text=mode_label,
                                    font=FONT_BOLD, anchor="e",
                                    fill=MODE_COLORS.get(mode, FG))
        if stop_text:
            self._draw_stop_band(pad - P(6), band_y0, width - pad + P(6),
                                 band_y0 + band_h, stop_text)

        for i, (dot, label, value) in enumerate(rows):
            y = rows_y + i * row_h + row_h / 2
            self.canvas.create_oval(pad + P(1), y - P(4), pad + P(9), y + P(4),
                                    fill=dot, outline="")
            label = self._fit(label, self._font,
                              (width - pad) - (pad + P(18))
                              - self._font.measure(value) - P(10))
            self.canvas.create_text(pad + P(18), y, text=label, font=FONT,
                                    fill=FG, anchor="w")
            self.canvas.create_text(width - pad, y, text=value, font=FONT,
                                    fill=DIM, anchor="e")

        if footer_left or footer_right:
            self.canvas.create_text(pad, footer_y + P(6), text=footer_left,
                                    font=FONT_SMALL, fill=DIM, anchor="w")
            self.canvas.create_text(width - pad, footer_y + P(6), text=footer_right,
                                    font=FONT_SMALL, fill=DIM, anchor="e")
        self._draw_graph_ladder(pad, graph_y, width - pad)
        self._draw_goal_row(pad, goal_y, width - pad, goal_y + P(GOAL_ROW_H))
        ctl_gap = P(CTL_BTN_GAP)
        ctl_w = (width - 2 * pad - ctl_gap) / 2
        self._draw_ctl_button(pad, ctl_y, pad + ctl_w, ctl_y + P(BTN_H), "start")
        self._draw_ctl_button(width - pad - ctl_w, ctl_y, width - pad,
                              ctl_y + P(BTN_H), "stop")
        self._draw_button(pad, btn_y, width - pad, btn_y + P(BTN_H), self._throttle)
        self._draw_report_row(pad, rep_y, width - pad, rep_y + P(REPORT_BTN_H))
        if self._report_age:
            # Only when there is one to age: with no report the button itself
            # already says so, and a second "no report yet" line would just
            # repeat it.
            self.canvas.create_text(width / 2, age_y, text=self._report_age,
                                    font=FONT_SMALL, fill=DIM)

        bottom = warn or updated_line
        if bottom:
            bottom = self._fit(bottom, self._font_small, width - 2 * pad)
            self.canvas.create_text(width / 2, height - P(14), text=bottom,
                                    font=FONT_SMALL, fill=AMBER if warn else DIM)

        self.root.update_idletasks()
        self._place()
        self._after_id = self.root.after(
            self.cfg.overlay_refresh_seconds * 1000, self._refresh)

    def run(self) -> None:
        self._refresh()
        self.root.update()
        self._place()
        self._animate()
        self.root.mainloop()


def run_overlay(cfg: Config) -> int:
    Overlay(cfg).run()
    return 0
