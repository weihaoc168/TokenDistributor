from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import OAUTH_BETA_HEADER, Config
from .models import BurnRates, UsageSnapshot, WindowUsage, parse_iso, utcnow

REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "TokenDistributor/0.1 (local usage pacing; github-none)"
FIVE_HOUR_KEYS = ("five_hour", "5h")
SEVEN_DAY_KEYS = ("seven_day", "7d", "seven_day_overall")
FIVE_HOUR_HINTS = ("5h", "five")
SEVEN_DAY_HINTS = ("7d", "seven", "week")
UTILIZATION_FRACTION_MAX = 1.5
RESET_DROP_FRACTION = 0.05
MIN_SLOPE_SPAN_MINUTES = 5.0
CACHE_READ_WEIGHT = 0.1
MAIN_SESSION_KEY = "__main_session__"
MAX_TASK_OUTCOMES = 50
OUTCOMES_PER_CLASS = 10
MIN_OUTCOMES_FOR_FULL_TRUST = 5
LEARNED_BLEND_WEIGHT = 0.7
BUDGET_EMA_ALPHA = 0.3
BUDGET_MIN_TOTAL_PCT_PER_HR = 0.2
BUDGET_MIN_OWN_SHARE = 0.8


class UsageFetchError(Exception):
    pass


class TokenError(UsageFetchError):
    pass


class RateLimitedError(UsageFetchError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _find_access_token(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "accessToken" and isinstance(value, str) and value:
                return value
            found = _find_access_token(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_access_token(item)
            if found:
                return found
    return None


def _load_token(cfg: Config) -> str:
    try:
        raw = cfg.credentials_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TokenError(
            f"Credentials file not found at {cfg.credentials_path}; open Claude Code once to create it"
        ) from exc
    except OSError as exc:
        raise TokenError(
            f"Cannot read credentials file at {cfg.credentials_path}: {exc.__class__.__name__}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TokenError(f"Credentials file at {cfg.credentials_path} is not valid JSON") from exc

    token: str | None = None
    if isinstance(data, dict):
        oauth = data.get("claudeAiOauth")
        if isinstance(oauth, dict):
            candidate = oauth.get("accessToken")
            if isinstance(candidate, str) and candidate:
                token = candidate
    if token is None:
        token = _find_access_token(data)
    if not token:
        raise TokenError(f"No OAuth accessToken found in {cfg.credentials_path}")
    return token


def _coerce_utilization(value: Any) -> float:
    try:
        util = float(value)
    except (TypeError, ValueError):
        return 0.0
    if util < 0.0:
        return 0.0
    if util <= UTILIZATION_FRACTION_MAX:
        return util
    return util / 100.0


def _parse_window(obj: dict[str, Any]) -> WindowUsage:
    return WindowUsage(
        utilization=_coerce_utilization(obj.get("utilization")),
        resets_at=parse_iso(obj.get("resets_at")),
    )


def _label_matches(label: str, hints: tuple[str, ...]) -> bool:
    lowered = label.lower()
    return any(hint in lowered for hint in hints)


def _promote(
    extra: dict[str, WindowUsage], hints: tuple[str, ...]
) -> tuple[WindowUsage | None, dict[str, WindowUsage]]:
    for key in list(extra):
        if _label_matches(key, hints):
            return extra.pop(key), extra
    return None, extra


def _parse_payload(
    payload: Any,
) -> tuple[WindowUsage | None, WindowUsage | None, dict[str, WindowUsage]]:
    if isinstance(payload, dict):
        items: list[tuple[str, Any]] = []
        for key, value in payload.items():
            if isinstance(value, dict) and "utilization" not in value:
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, dict) and "utilization" in sub_value:
                        items.append((f"{key}.{sub_key}", sub_value))
                continue
            items.append((key, value))
    elif isinstance(payload, list):
        items = []
        for index, obj in enumerate(payload):
            if not isinstance(obj, dict):
                continue
            label = ""
            for key in ("window", "name", "type"):
                value = obj.get(key)
                if isinstance(value, str) and value:
                    label = value
                    break
            items.append((label or f"window_{index}", obj))
    else:
        return None, None, {}

    five: WindowUsage | None = None
    seven: WindowUsage | None = None
    extra: dict[str, WindowUsage] = {}
    for key, value in items:
        if not isinstance(value, dict) or "utilization" not in value:
            continue
        window = _parse_window(value)
        if five is None and key in FIVE_HOUR_KEYS:
            five = window
        elif seven is None and key in SEVEN_DAY_KEYS:
            seven = window
        else:
            extra[key] = window
    limits = payload.get("limits") if isinstance(payload, dict) else None
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict) or "percent" not in entry:
                continue
            try:
                pct = min(max(float(entry.get("percent") or 0), 0.0), 100.0)
            except (TypeError, ValueError):
                continue
            window = WindowUsage(pct / 100.0, parse_iso(entry.get("resets_at")))
            kind = str(entry.get("kind", ""))
            scope = entry.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            display = model.get("display_name") if isinstance(model, dict) else None
            if kind == "session":
                if five is None:
                    five = window
            elif kind == "weekly_all":
                if seven is None:
                    seven = window
            elif display:
                extra[str(display).lower()] = window
            elif kind:
                extra.setdefault(kind, window)

    if seven is None:
        seven, extra = _promote(extra, SEVEN_DAY_HINTS)
    if five is None:
        five, extra = _promote(extra, FIVE_HOUR_HINTS)
    return five, seven, extra


def fetch_usage(cfg: Config) -> UsageSnapshot:
    token = _load_token(cfg)
    request = urllib.request.Request(
        cfg.usage_url,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise TokenError(
                f"Usage API returned HTTP {exc.code}: the stored OAuth token is expired or invalid; "
                f"open Claude Code once to refresh it"
            ) from exc
        if exc.code == 429:
            try:
                retry_after = float(exc.headers.get("retry-after", ""))
            except (TypeError, ValueError):
                retry_after = None
            raise RateLimitedError(
                "Usage API returned HTTP 429 (rate limited)", retry_after
            ) from exc
        raise UsageFetchError(f"Usage API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise UsageFetchError(f"Usage API request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise UsageFetchError(
            f"Usage API request timed out after {REQUEST_TIMEOUT_SECONDS}s"
        ) from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise UsageFetchError("Usage API returned a non-JSON response") from exc

    try:
        (cfg.state_dir / "last_payload.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError:
        pass

    five, seven, extra = _parse_payload(payload)
    if seven is None:
        if isinstance(payload, dict):
            found = sorted(payload.keys())
        elif isinstance(payload, list):
            found = [f"list[{len(payload)}]"]
        else:
            found = [type(payload).__name__]
        raise UsageFetchError(f"No seven-day window in usage response; top-level keys: {found}")
    return UsageSnapshot(
        fetched_at=utcnow(),
        five_hour=five if five is not None else WindowUsage(0.0, None),
        seven_day=seven,
        extra=extra,
        raw=payload if isinstance(payload, dict) else {"windows": payload},
    )


class UsageHistory:
    def __init__(self, cfg: Config) -> None:
        self._path = cfg.history_file

    def append(self, snap: UsageSnapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snap.to_dict()) + "\n")

    def load_recent(self, hours: float = 12.0) -> list[UsageSnapshot]:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        cutoff = utcnow() - timedelta(hours=hours)
        snapshots: list[UsageSnapshot] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            snap = UsageSnapshot.from_dict(record)
            if snap.fetched_at >= cutoff:
                snapshots.append(snap)
        return snapshots

    def slope_pct_per_hr(self, window_minutes: int, now: datetime) -> float | None:
        cutoff = now - timedelta(minutes=window_minutes)
        lookback_hours = max(
            (utcnow() - cutoff).total_seconds() / 3600.0, window_minutes / 60.0
        )
        points = sorted(
            (snap.fetched_at, snap.seven_day.utilization)
            for snap in self.load_recent(hours=lookback_hours + 0.1)
            if cutoff <= snap.fetched_at <= now
        )
        last_drop = 0
        for i in range(1, len(points)):
            if points[i][1] < points[i - 1][1] - RESET_DROP_FRACTION:
                last_drop = i
        points = points[last_drop:]
        if len(points) < 2:
            return None
        span_hours = (points[-1][0] - points[0][0]).total_seconds() / 3600.0
        if span_hours * 60.0 < MIN_SLOPE_SPAN_MINUTES:
            return None
        origin = points[0][0]
        xs = [(ts - origin).total_seconds() / 3600.0 for ts, _ in points]
        ys = [util for _, util in points]
        n = len(points)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom == 0.0:
            return None
        slope_fraction_per_hr = (
            sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
        )
        return slope_fraction_per_hr * 100.0


def _jsonl_files(root: str, min_mtime: float) -> list[Path]:
    found: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for entry in entries:
        try:
            if entry.is_file() and entry.name.endswith(".jsonl"):
                if entry.stat().st_mtime >= min_mtime:
                    found.append(Path(entry.path))
            elif entry.is_dir():
                try:
                    subentries = list(os.scandir(entry.path))
                except OSError:
                    continue
                for sub in subentries:
                    try:
                        if (
                            sub.is_file()
                            and sub.name.endswith(".jsonl")
                            and sub.stat().st_mtime >= min_mtime
                        ):
                            found.append(Path(sub.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _usage_weighted(usage: dict[str, Any]) -> float:
    def _num(key: str) -> float:
        value = usage.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    return (
        _num("input_tokens")
        + _num("output_tokens")
        + _num("cache_creation_input_tokens")
        + CACHE_READ_WEIGHT * _num("cache_read_input_tokens")
    )


def _sum_file_tokens(path: Path, since: datetime, now: datetime) -> float:
    total = 0.0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "assistant":
                    continue
                ts = parse_iso(event.get("timestamp"))
                if ts is None or not since <= ts <= now:
                    continue
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if isinstance(usage, dict):
                    total += _usage_weighted(usage)
    except OSError:
        return total
    return total


def scan_local_tokens(cfg: Config, since: datetime, now: datetime) -> dict[str, int]:
    weighted_totals: dict[str, float] = {}
    min_mtime = since.timestamp()
    main_ids = set(cfg.main_session_ids)
    try:
        project_entries = list(os.scandir(cfg.projects_dir))
    except OSError:
        return {}
    for entry in project_entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        for path in _jsonl_files(entry.path, min_mtime):
            key = MAIN_SESSION_KEY if path.stem in main_ids else entry.name
            weighted = _sum_file_tokens(path, since, now)
            if weighted > 0:
                weighted_totals[key] = weighted_totals.get(key, 0.0) + weighted
    return {key: int(value) for key, value in weighted_totals.items()}


def load_calibration(cfg: Config) -> dict:
    try:
        data = json.loads(cfg.calibration_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_calibration(cfg: Config, cal: dict) -> None:
    cfg.calibration_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.calibration_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    os.replace(tmp, cfg.calibration_file)


def record_task_outcome(cfg: Config, weight: str, tokens: int, minutes: float) -> None:
    cal = load_calibration(cfg)
    outcomes = cal.get("task_outcomes")
    if not isinstance(outcomes, list):
        outcomes = []
    outcomes.append(
        {"weight": weight, "tokens": tokens, "minutes": minutes, "at": utcnow().isoformat()}
    )
    cal["task_outcomes"] = outcomes[-MAX_TASK_OUTCOMES:]
    save_calibration(cfg, cal)


def learned_class_rates(cfg: Config, cal: dict) -> tuple[float, float]:
    priors = {"heavy": cfg.heavy_pct_per_hr_prior, "light": cfg.light_pct_per_hr_prior}
    budget = cal.get("budget_tokens_est")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        return priors["heavy"], priors["light"]
    outcomes = cal.get("task_outcomes")
    if not isinstance(outcomes, list):
        outcomes = []
    rates: dict[str, float] = {}
    for weight, prior in priors.items():
        recent = [
            o for o in outcomes if isinstance(o, dict) and o.get("weight") == weight
        ][-OUTCOMES_PER_CLASS:]
        samples: list[float] = []
        for outcome in recent:
            tokens = outcome.get("tokens")
            minutes = outcome.get("minutes")
            if (
                isinstance(tokens, (int, float))
                and isinstance(minutes, (int, float))
                and not isinstance(tokens, bool)
                and not isinstance(minutes, bool)
                and minutes > 0
            ):
                samples.append(float(tokens) / float(minutes) * 60.0)
        if not samples:
            rates[weight] = prior
            continue
        tokens_per_hr = sum(samples) / len(samples)
        learned = tokens_per_hr / float(budget) * 100.0
        if len(samples) < MIN_OUTCOMES_FOR_FULL_TRUST:
            learned = LEARNED_BLEND_WEIGHT * learned + (1.0 - LEARNED_BLEND_WEIGHT) * prior
        rates[weight] = learned
    return rates["heavy"], rates["light"]


def compute_burn_rates(
    cfg: Config, history: UsageHistory, own_dirs: set[str], now: datetime
) -> BurnRates:
    slope = history.slope_pct_per_hr(cfg.slope_window_minutes, now)
    total = slope if slope is not None else 0.0
    window_hours = cfg.slope_window_minutes / 60.0
    since = now - timedelta(minutes=cfg.slope_window_minutes)
    tokens = scan_local_tokens(cfg, since, now)
    effective_own = own_dirs | {MAIN_SESSION_KEY}
    all_local = sum(tokens.values())
    own_local = sum(count for name, count in tokens.items() if name in effective_own)
    own_share = own_local / all_local if all_local > 0 else 0.0
    own_pct_per_hr = total * own_share
    foreign_pct_per_hr = total - own_pct_per_hr

    cal = load_calibration(cfg)
    clamped_foreign = max(foreign_pct_per_hr, 0.0)
    prev_ema = cal.get("foreign_ema_pct_per_hr")
    if isinstance(prev_ema, (int, float)) and not isinstance(prev_ema, bool):
        ema = (
            cfg.foreign_ema_alpha * clamped_foreign
            + (1.0 - cfg.foreign_ema_alpha) * float(prev_ema)
        )
    else:
        ema = clamped_foreign
    cal["foreign_ema_pct_per_hr"] = ema

    if total > BUDGET_MIN_TOTAL_PCT_PER_HR and own_share > BUDGET_MIN_OWN_SHARE and all_local > 0:
        delta_util_fraction = total / 100.0 * window_hours
        if delta_util_fraction > 0:
            budget_sample = all_local / delta_util_fraction
            prev_budget = cal.get("budget_tokens_est")
            if (
                isinstance(prev_budget, (int, float))
                and not isinstance(prev_budget, bool)
                and prev_budget > 0
            ):
                cal["budget_tokens_est"] = (
                    BUDGET_EMA_ALPHA * budget_sample
                    + (1.0 - BUDGET_EMA_ALPHA) * float(prev_budget)
                )
            else:
                cal["budget_tokens_est"] = budget_sample
    save_calibration(cfg, cal)

    return BurnRates(
        total_pct_per_hr=total,
        own_pct_per_hr=own_pct_per_hr,
        foreign_pct_per_hr=foreign_pct_per_hr,
        foreign_ema_pct_per_hr=ema,
    )
