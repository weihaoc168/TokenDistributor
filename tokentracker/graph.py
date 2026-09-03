"""The agentic graph: which model, and how many of it, at each tier.

Three tiers, in the order the work flows through them:

    executive   the acting director (the forked main session)
    advisory    the review lenses that judge the workers' output
    workers     the parallel lanes that actually build

`config.json` carries the checked-in graph; `state/graph.json` is the per-user
override the overlay writes on a tap, exactly like goal.json - so a click never
has to rewrite the checked-in config. The override is a *patch*: `write_graph`
persists only the fields it was handed, so a tap on the worker `+` changes the
worker count and leaves every other field still following config.json.

The legacy scalar keys (`throttle_model`, `worker_model`, `max_concurrency`,
`surge_concurrency`) stay in `config.json` for compatibility and are *derived*
from the graph whenever one is present: `apply_graph` writes them back onto the
live Config, which is what makes the scheduler read the graph's worker counts
without knowing the graph exists.

Nothing here raises on bad input: it is called from inside the run loop, from
every overlay refresh and from `load_config`, so an unknown model id is a
warning and a nonsense count is clamped. That promise covers every read path -
a junk file, a non-dict payload, a missing tier, a non-numeric count, a null
model and the non-finite floats JSON is happy to hand back all degrade to the
configured value instead of taking the loop's next tick down.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from .config import Config
from .models import utcnow

EXECUTIVE = "executive"
ADVISORY = "advisory"
WORKERS = "workers"
TIERS = (EXECUTIVE, ADVISORY, WORKERS)
# workers carry a second count: the lane budget during surge / endgame.
TIER_FIELDS = {
    EXECUTIVE: ("model", "count"),
    ADVISORY: ("model", "count"),
    WORKERS: ("model", "count", "surge_count"),
}
COUNT_MIN = 1
COUNT_MAX = 40
SURGE_MAX = 80
FALLBACK_MODEL = "claude-opus-5"
# The last-resort tier when even the Config is unreadable; the worker model
# stays empty on purpose (see default_graph).
TIER_DEFAULTS: dict[str, dict[str, Any]] = {
    EXECUTIVE: {"model": FALLBACK_MODEL, "count": 1},
    ADVISORY: {"model": FALLBACK_MODEL, "count": 3},
    WORKERS: {"model": "", "count": 3, "surge_count": 4},
}
SOURCE_CONFIG = "config.json"
SOURCE_OVERRIDE = "state/graph.json"
# The ids `tracker.py graph set` accepts without complaint. Anything else is
# still written (a new model id must not need a code change) but is reported.
DEFAULT_KNOWN_MODELS = (
    "claude-fable-5-1",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
)


def _int(value: Any, fallback: int) -> int:
    """A whole number, or `fallback`. Never raises.

    OverflowError is caught alongside TypeError and ValueError because it is
    the one `int()` raises on an infinity, and `json.loads` turns both `1e999`
    and the bare `Infinity` literal into exactly that float. A graph.json
    holding one used to travel all the way out of `read_graph_source` and take
    down the poll - and the overlay refresh, and `load_config` - with it.
    NaN is refused for the same reason `goal.clamp_goal` refuses it: clamping
    it silently produces a count that survives no comparison.
    """
    try:
        if isinstance(value, bool):
            raise TypeError
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _clamp(value: int, high: int = COUNT_MAX) -> int:
    return min(max(value, COUNT_MIN), high)


def _model(value: Any, fallback: str) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    return text or fallback


def _block(base: Any, tier: str) -> dict[str, Any]:
    """One complete tier, read out of `base` as tolerantly as it can be.

    `base` is whatever a caller had lying around - `cfg.graph` straight off a
    hand-edited config.json, a half-built graph passed to `set_assignments`,
    or nothing at all - so a missing tier, a tier that is not a dict and a
    missing field each fall back to the built-in default rather than raising
    the KeyError that indexing `base[tier]["model"]` used to.
    """
    raw = base.get(tier) if isinstance(base, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    fallback = TIER_DEFAULTS[tier]
    out: dict[str, Any] = {}
    for fieldname in TIER_FIELDS[tier]:
        if fieldname == "model":
            out[fieldname] = _model(raw.get(fieldname), fallback[fieldname])
        else:
            high = SURGE_MAX if fieldname == "surge_count" else COUNT_MAX
            out[fieldname] = _clamp(
                _int(raw.get(fieldname), fallback[fieldname]), high)
    return out


def default_graph(cfg: Config) -> dict[str, dict[str, Any]]:
    """The graph implied by the legacy scalar keys; the migration's source.

    The worker model is allowed to stay empty: dispatching with no `--model` at
    all (the account default) is the documented behaviour for plain workers,
    and inventing one here would silently change what every task runs on. The
    executive model is not - the fork must never launch model-less.
    """
    executive = _model(getattr(cfg, "throttle_model", ""), FALLBACK_MODEL)
    worker = _model(getattr(cfg, "worker_model", ""), "")
    count = _clamp(_int(getattr(cfg, "max_concurrency", 3), 3))
    surge = _clamp(_int(getattr(cfg, "surge_concurrency", count), count), SURGE_MAX)
    return {
        EXECUTIVE: {"model": executive, "count": 1},
        ADVISORY: {"model": executive, "count": 3},
        WORKERS: {"model": worker, "count": count,
                  "surge_count": max(surge, count)},
    }


def normalize(raw: Any, base: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Fill `raw` out into a complete graph, taking `base` for what it omits.

    A partial override (the overlay writes only the worker count) is merged
    rather than replacing the whole graph. `base` is read through `_block`, so
    a partial base - `set_assignments` is handed whatever the caller has -
    completes itself instead of raising.
    """
    out = {tier: _block(base, tier) for tier in TIERS}
    if not isinstance(raw, dict):
        return out
    for tier in TIERS:
        block = raw.get(tier)
        if not isinstance(block, dict):
            continue
        for fieldname in TIER_FIELDS[tier]:
            if fieldname not in block:
                continue
            value = block[fieldname]
            if fieldname == "model":
                out[tier]["model"] = _model(value, out[tier]["model"])
            else:
                high = SURGE_MAX if fieldname == "surge_count" else COUNT_MAX
                out[tier][fieldname] = _clamp(
                    _int(value, out[tier][fieldname]), high)
    # A surge budget under the pacing budget would make surge the slower mode.
    out[WORKERS]["surge_count"] = max(out[WORKERS]["surge_count"],
                                      out[WORKERS]["count"])
    return out


def _override_payload(cfg: Config) -> dict[str, Any] | None:
    """The tier map inside state/graph.json, or None when there is not one.

    None covers every way the file can fail to be an override: absent,
    unreadable, not JSON, not an object, an object with no tier in it. The
    caller then keeps the config graph, so a fat-fingered file degrades to the
    checked-in numbers rather than to nothing at all.
    """
    try:
        data = json.loads(cfg.graph_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        # ValueError covers json.JSONDecodeError and the UnicodeDecodeError a
        # half-written file gives back; the rest cover a Config without a
        # usable graph_file at all.
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("graph") if isinstance(data.get("graph"), dict) else data
    if not any(isinstance(payload.get(t), dict) for t in TIERS):
        return None
    return payload


def read_graph_source(cfg: Config) -> tuple[dict[str, dict[str, Any]], str]:
    """(graph, where it came from). state/graph.json wins when it is readable.

    Never raises. It runs once per poll inside the run loop, on every overlay
    refresh and from `load_config`, so every failure here has to be a fallback
    to the config source rather than an exception.
    """
    base = normalize(getattr(cfg, "graph", None), default_graph(cfg))
    payload = _override_payload(cfg)
    if payload is None:
        return base, SOURCE_CONFIG
    return normalize(payload, base), SOURCE_OVERRIDE


def read_graph(cfg: Config) -> dict[str, dict[str, Any]]:
    return read_graph_source(cfg)[0]


def override_warning(cfg: Config) -> str | None:
    """"there is a state/graph.json and it is being ignored", or None.

    The fallback itself is silent by design (the loop must not spam its log
    every poll), which would otherwise leave a typo in the override file with
    no symptom at all beyond the worker count quietly not changing.
    """
    try:
        exists = cfg.graph_file.exists()
    except (OSError, AttributeError):
        return None
    if exists and _override_payload(cfg) is None:
        return (f"warning: {SOURCE_OVERRIDE} is unreadable or names no tier; "
                f"using {SOURCE_CONFIG}")
    return None


def _named_fields(patch: Any) -> dict[str, tuple[str, ...]]:
    """{tier: the fields `patch` actually names}, ignoring everything else."""
    named: dict[str, tuple[str, ...]] = {}
    if not isinstance(patch, dict):
        return named
    for tier in TIERS:
        block = patch.get(tier)
        if not isinstance(block, dict):
            continue
        fields = tuple(f for f in TIER_FIELDS[tier] if f in block)
        if fields:
            named[tier] = fields
    return named


def write_graph(cfg: Config, patch: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Persist ONLY the fields `patch` names, merged into the standing override.

    This is a patch, not a snapshot, and that is the whole contract: the
    overlay's -/+ hands over one field (workers.count) and `tracker.py graph
    set` hands over the assignments it was given. Writing the resolved graph
    instead - which is what this used to do - copied the executive and advisory
    models into state/graph.json as a side effect of one tap, and from then on
    every edit to config.json's graph section was silently dead, with the read
    path preferring the override field by field and nothing anywhere saying so.

    Values are clamped against the graph in force, so junk falls back to the
    number the operator can actually see. Returns the graph now in force, read
    back from disk: a write that failed reports the old numbers rather than the
    ones it meant to write.
    """
    effective = read_graph(cfg)                   # config.json + what stands
    resolved = normalize(patch, effective)        # the patch, clamped
    stored = _override_payload(cfg) or {}
    kept = normalize(stored, effective)           # the standing override, clamped
    merged: dict[str, dict[str, Any]] = {}
    for tier, fields in _named_fields(stored).items():
        merged[tier] = {f: kept[tier][f] for f in fields}
    for tier, fields in _named_fields(patch).items():
        merged.setdefault(tier, {}).update(
            {f: resolved[tier][f] for f in fields})
    if not merged:
        # Nothing named: leave the file alone rather than writing an empty
        # override, which reads back as "unreadable" and warns forever.
        return effective
    body = json.dumps({"graph": merged, "set_at": utcnow().isoformat()}, indent=2)
    path = cfg.graph_file
    tmp = path.parent / f"{path.name}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Swap the file in whole, like control/goal: a torn read would look
        # like "no override" and silently drop the operator's worker count.
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
    return read_graph(cfg)


def apply_graph(cfg: Config) -> dict[str, dict[str, Any]]:
    """Derive the legacy scalar keys from the graph, onto the live Config.

    This is the single derivation site: after it runs, `cfg.max_concurrency`
    and friends *are* the graph, so the scheduler, the dispatcher and the fork
    all follow it without importing this module.
    """
    graph, _source = read_graph_source(cfg)
    executive, _advisory, workers = tiers_of(graph)
    # cfg.graph is deliberately left alone: it holds what config.json declared,
    # and folding the override back into it would make a deleted state/graph.json
    # keep applying for the life of the process.
    cfg.throttle_model = executive["model"]
    cfg.worker_model = workers["model"]
    cfg.max_concurrency = workers["count"]
    cfg.surge_concurrency = workers["surge_count"]
    return graph


def known_models(cfg: Config) -> list[str]:
    listed = getattr(cfg, "known_models", None)
    models = [str(m) for m in listed if isinstance(m, str)] if isinstance(listed, list) else []
    if not models:
        models = list(DEFAULT_KNOWN_MODELS)
    local = str(getattr(cfg, "local_model", "") or "")
    if local and local not in models:
        models.append(local)
    return models


def validate_graph(graph: dict[str, dict[str, Any]], models: list[str]) -> list[str]:
    """Warnings, never exceptions: an unknown model id is reported, not refused.

    A model id the allow-list has never heard of is far more often a new model
    than a typo, and refusing it would mean a code change every release.
    """
    warnings: list[str] = []
    for tier in TIERS:
        block = graph.get(tier) if isinstance(graph, dict) else None
        if not isinstance(block, dict):
            # Reported rather than raised: this is the one function whose whole
            # job is turning a bad graph into words.
            warnings.append(f"warning: {tier} tier is missing from the graph")
            continue
        model = str(block.get("model", ""))
        if model and model not in models:
            warnings.append(
                f"warning: {tier} model '{model}' is not in known_models")
    return warnings


def parse_assignments(items: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """`tier.field=value` strings -> (patch, errors).

    The patch holds only the fields that were named, which is what makes
    `tracker.py graph set workers.count=20` write exactly one field to
    state/graph.json instead of pinning the whole graph.
    """
    patch: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for item in items:
        text = str(item).strip()
        if "=" not in text:
            errors.append(f"cannot read '{item}'; use tier.field=value")
            continue
        target, value = text.split("=", 1)
        if "." not in target:
            errors.append(f"cannot read '{item}'; use tier.field=value")
            continue
        tier, fieldname = (part.strip() for part in target.split(".", 1))
        if tier not in TIERS:
            errors.append(f"unknown tier '{tier}'; use {', '.join(TIERS)}")
            continue
        if fieldname not in TIER_FIELDS[tier]:
            errors.append(f"unknown field '{tier}.{fieldname}'; use "
                          + ", ".join(TIER_FIELDS[tier]))
            continue
        value = value.strip()
        if fieldname != "model":
            try:
                value = int(value)
            except ValueError:
                errors.append(f"'{value}' is not a whole number for {target}")
                continue
        patch.setdefault(tier, {})[fieldname] = value
    return patch, errors


def set_assignments(graph: dict[str, dict[str, Any]], items: list[str],
                    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Apply `tier.field=value` strings to `graph`; returns (graph, errors)."""
    patch, errors = parse_assignments(items)
    return normalize(patch, graph), errors


def short_model(model: Any) -> str:
    """Compact label for the overlay row: claude-opus-5 -> opus-5."""
    text = str(model or "?").strip()
    for prefix in ("claude-", "anthropic/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    parts = text.split("-")
    # Drop a trailing release date (haiku-4-5-20251001 -> haiku-4-5).
    if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) == 8:
        parts = parts[:-1]
    return "-".join(parts) or "?"


def _named(model: Any) -> str:
    """A model id for display; an empty one means "whatever the account picks"."""
    return str(model or "") or "(account default)"


def tiers_of(graph: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(executive, advisory, workers), each complete, from whatever is passed.

    Every display helper below goes through this: they are called from the
    overlay's refresh timer and from the fork's prompt expansion, where a
    KeyError on a hand-edited graph would be a crash rather than a bad label.
    """
    return tuple(_block(graph, tier) for tier in TIERS)  # type: ignore[return-value]


def graph_line(graph: dict[str, dict[str, Any]]) -> str:
    """The one line the fork prompt's {graph} placeholder expands into."""
    e, a, w = tiers_of(graph)
    return (
        "Agentic graph (from TokenDistributor config): "
        f"executive {_named(e['model'])} x{e['count']}; "
        f"advisory/reviewers {_named(a['model'])} x{a['count']}; "
        f"workers {_named(w['model'])} x{w['count']} (surge {w['surge_count']}). "
        "Set these models explicitly on every Workflow agent; never exceed the "
        "worker count as concurrent lanes; use the advisory count as the number "
        "of review lenses."
    )


def overlay_label(graph: dict[str, dict[str, Any]]) -> str:
    """The compact one-line graph summary `tracker.py status` prints.

    The overlay drew this too until the ladder chart replaced its GRAPH row.
    """
    e, a, w = tiers_of(graph)
    return (f"E {short_model(e['model'])} x{e['count']} | "
            f"A {short_model(a['model'])} x{a['count']} | "
            f"W {short_model(w['model'])} x{w['count']}/{w['surge_count']}")


def format_tiers(graph: dict[str, dict[str, Any]]) -> list[str]:
    """The lines `tracker.py graph` prints."""
    lines = []
    for tier in TIERS:
        block = _block(graph, tier)
        counts = f"x{block['count']}"
        if tier == WORKERS:
            counts += f" (surge x{block['surge_count']})"
        lines.append(f"  {tier:<9} {_named(block['model']):<28} {counts}")
    return lines


def migrate_config_file(cfg: Config) -> bool:
    """One-time: write the graph section into config.json from the legacy keys.

    Returns True when the file was rewritten. Only ever *adds* the section, so
    a hand-edited graph is never clobbered and the legacy keys stay put.
    """
    path = cfg.root / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(raw, dict) or isinstance(raw.get("graph"), dict):
        return False
    raw["graph"] = default_graph(cfg)
    try:
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
