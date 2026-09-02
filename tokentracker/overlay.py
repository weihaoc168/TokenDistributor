from __future__ import annotations

import ctypes
import json
import math
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
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
}
FONT_BIG = ("Segoe UI", 11, "bold")
FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)
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
        self._after_id: str | None = None

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

        for widget in (self.root, self.canvas):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.bind("<Button-3>", lambda _e: self.root.destroy())
        self.canvas.tag_bind("throttle_btn", "<Button-1>", self._toggle_throttle)
        self.canvas.bind("<MouseWheel>", self._on_wheel)

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
        sessions = _live_sessions(self.cfg)
        self._throttle = _read_throttle(self.cfg)
        self.canvas.delete("all")

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
        rows_y = dist_header_y + P(26)
        footer_y = rows_y + len(rows) * row_h + P(12)
        btn_y = footer_y + P(22)
        height = btn_y + P(BTN_H) + P(30)

        self.canvas.config(height=height)
        self._round_card(width, height)

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
        self._draw_button(pad, btn_y, width - pad, btn_y + P(BTN_H), self._throttle)

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
