"""The operator's clock: local time on screen, UTC on disk.

Every timestamp TokenDistributor *stores* is ISO UTC - state/state.json,
state/allocation.json, state/snapshot.json, state/history.jsonl, the handover
log. That is deliberate: the files are compared, differenced and replayed, and
a local timestamp that jumps an hour twice a year makes all three lie.

Every timestamp TokenDistributor *shows* is local, because "the weekly window
resets Fri 07:00" is only useful in the clock the operator reads. This module
is the one place that converts, so the panel, `status`, `alloc`, `graph`,
`report` and `pricing` cannot drift into rendering different zones.

The zone comes from `config.json`:

    "timezone":        "America/Chicago"   tried first, through zoneinfo
    "tz_offset_hours": -5                  the fallback when that fails
    "tz_label":        ""                  "" = derive it ("CT")

Windows ships no IANA tz database and CPython's `zoneinfo` does not carry one,
so on a box without the `tzdata` package `ZoneInfo("America/Chicago")` raises
`ZoneInfoNotFoundError` - which is the case on this machine. That is why the
fixed offset exists and why it is not treated as an error: the fallback is a
plain `timezone(timedelta(hours=-5))`, which renders the right wall clock for
as long as the offset holds.

The cost of the fallback is DST: a fixed offset does not shift, so the offset
has to be edited when Central Time changes (-5 in summer, -6 in winter), or
`pip install tzdata` installed once and the zoneinfo path takes over
automatically. `describe()` says which path is live, so the panel and `status`
can be honest about it rather than quietly showing an hour that is wrong.

Nothing here raises. Every function is reached from the poll, from a status
line and from an overlay frame that repaints every five seconds; a bad zone
name, a `tz_offset_hours` of "abc" and a naive datetime all degrade to a
readable string or to UTC.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

# The default zone, matching config.json's shipped value.
DEFAULT_TZ = "America/Chicago"
DEFAULT_OFFSET_HOURS = -5.0
DEFAULT_FORMAT = "%a %H:%M"

# Zone -> the label an operator would say out loud. These stay constant across
# DST on purpose: "CT" is what the user asked for, where the live abbreviation
# would flip between CDT and CST twice a year and read as a different zone.
ZONE_LABELS = {
    "america/chicago": "CT",
    "us/central": "CT",
    "america/winnipeg": "CT",
    "america/new_york": "ET",
    "us/eastern": "ET",
    "america/toronto": "ET",
    "america/denver": "MT",
    "us/mountain": "MT",
    "america/phoenix": "MST",
    "america/los_angeles": "PT",
    "us/pacific": "PT",
    "america/anchorage": "AKT",
    "europe/london": "UK",
    "utc": "UTC",
    "etc/utc": "UTC",
}

# The Config the bare-signature helpers read when they are given none. Set by
# `use()` from load_config/reload_config, so `fmt_local(dt)` works from a
# display helper that has no Config in hand (the overlay's draw calls, the
# allocator's reset labels) without every one of them threading it through.
_DEFAULT_CFG: Any = None
# (zone name, offset hours) -> tzinfo. Rebuilding a ZoneInfo per drawn label
# would re-stat the tz database on every overlay frame.
_ZONE_CACHE: dict[tuple[str, float], tuple[Any, bool]] = {}


def use(cfg: Any) -> None:
    """Adopt `cfg` as the zone source for the bare-signature helpers."""
    global _DEFAULT_CFG
    _DEFAULT_CFG = cfg


def _cfg(cfg: Any = None) -> Any:
    return _DEFAULT_CFG if cfg is None else cfg


def _finite(value: Any, fallback: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def tz_name(cfg: Any = None) -> str:
    """The configured IANA zone name, or the built-in default."""
    raw = getattr(_cfg(cfg), "timezone", None)
    name = str(raw).strip() if isinstance(raw, str) else ""
    return name or DEFAULT_TZ


def tz_offset_hours(cfg: Any = None) -> float:
    """The fallback offset in hours, clamped to a real UTC offset."""
    hours = _finite(getattr(_cfg(cfg), "tz_offset_hours", DEFAULT_OFFSET_HOURS),
                    DEFAULT_OFFSET_HOURS)
    return min(max(hours, -18.0), 18.0)


def zone(cfg: Any = None) -> tuple[Any, bool]:
    """(tzinfo, from_zoneinfo). Never raises; falls back to the fixed offset.

    `from_zoneinfo` is False when the tz database was missing and the fixed
    offset is what is actually in force - the state this Windows box is in, and
    the one thing a reader of a rendered time needs to know to judge it around
    a DST boundary.
    """
    name = tz_name(cfg)
    offset = tz_offset_hours(cfg)
    key = (name, offset)
    hit = _ZONE_CACHE.get(key)
    if hit is not None:
        return hit
    result: tuple[Any, bool]
    try:
        from zoneinfo import ZoneInfo

        result = (ZoneInfo(name), True)
    except Exception:
        # ZoneInfoNotFoundError on a box with no tz database (this one), and
        # ImportError/ValueError on anything older or hand-edited. All of them
        # mean the same thing here: use the offset the operator gave.
        result = (timezone(timedelta(hours=offset)), False)
    _ZONE_CACHE[key] = result
    return result


def uses_zoneinfo(cfg: Any = None) -> bool:
    return zone(cfg)[1]


def now_utc() -> datetime:
    """Now, tz-aware, in UTC. The clock every stored timestamp is written on."""
    return datetime.now(timezone.utc)


def to_local(dt: Any, cfg: Any = None) -> datetime | None:
    """`dt` in the operator's zone, or None when it is not a usable time.

    A naive datetime is read as UTC rather than as local: everything this
    project stores is UTC, so that is the only reading that cannot silently
    shift a stored timestamp by the offset.
    """
    if isinstance(dt, str):
        from .models import parse_iso

        dt = parse_iso(dt)
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(zone(cfg)[0])
    except (OSError, OverflowError, ValueError):
        return None


def label(cfg: Any = None) -> str:
    """The zone label shown beside a rendered time: "CT".

    `tz_label` in config.json wins; then the zone-name table; then the live
    abbreviation collapsed across DST (CDT and CST both read "CT"); then the
    offset itself, which is at least never wrong.
    """
    configured = getattr(_cfg(cfg), "tz_label", None)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    name = tz_name(cfg)
    mapped = ZONE_LABELS.get(name.strip().lower())
    if mapped:
        return mapped
    tz, from_zoneinfo = zone(cfg)
    if from_zoneinfo:
        try:
            abbrev = datetime.now(timezone.utc).astimezone(tz).tzname() or ""
        except (OSError, OverflowError, ValueError):
            abbrev = ""
        # CDT/CST -> CT: the season is not the zone.
        if len(abbrev) == 3 and abbrev[1] in "DS" and abbrev[2] == "T":
            return abbrev[0] + "T"
        if abbrev and not abbrev.startswith(("UTC", "+", "-")):
            return abbrev
    hours = tz_offset_hours(cfg)
    sign = "-" if hours < 0 else "+"
    whole, frac = divmod(abs(hours), 1)
    minutes = int(round(frac * 60))
    return (f"UTC{sign}{int(whole)}:{minutes:02d}" if minutes
            else f"UTC{sign}{int(whole)}")


def fmt_local(dt: Any, fmt: str = DEFAULT_FORMAT, cfg: Any = None,
              with_label: bool = False, fallback: str = "?") -> str:
    """`dt` rendered in the operator's zone, e.g. "Fri 07:00".

    `with_label` appends the zone ("Fri 07:00 CT"), which is what every line
    that shows an absolute time should do: a bare wall clock on a panel is
    ambiguous exactly when it matters, next to a UTC timestamp in a state file.
    """
    local = to_local(dt, cfg)
    if local is None:
        return fallback
    try:
        text = local.strftime(str(fmt))
    except (ValueError, TypeError, OSError, OverflowError):
        return fallback
    return f"{text} {label(cfg)}" if with_label else text


def stamp(dt: Any = None, fmt: str = DEFAULT_FORMAT, cfg: Any = None) -> str:
    """`fmt_local` with the label always on; `dt=None` means now."""
    return fmt_local(now_utc() if dt is None else dt, fmt, cfg, with_label=True)


def describe(cfg: Any = None) -> str:
    """One line naming the zone in force and how it was resolved.

    Printed by `status` so "the panel says 18:05 and my clock says 19:05" has
    an answer: either the tz database is doing it, or a fixed offset is, and
    the fixed one does not follow DST.
    """
    name = tz_name(cfg)
    if uses_zoneinfo(cfg):
        return f"timezone: {name} ({label(cfg)}) via zoneinfo"
    hours = tz_offset_hours(cfg)
    return (f"timezone: {name} ({label(cfg)}) via fixed offset "
            f"{hours:+g}h - no tz database on this box, so it does not follow "
            "DST (pip install tzdata to fix, or edit tz_offset_hours)")
