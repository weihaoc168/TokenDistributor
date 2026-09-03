"""Per-model list prices, so the work-distribution report can say what it cost.

Weighted cost (`ledger.weighted`) compares tiers on one scale; it is not money.
This module carries the other half: published USD list prices per million
tokens, five numbers per model.

    input           base input tokens
    output          output tokens
    cache_write     5-minute cache writes  (1.25x base input)
    cache_write_1h  1-hour cache writes    (2x base input)
    cache_read      cache hits and refreshes

Cache writes are two prices, not one, because the transcripts record two kinds:
`usage.cache_creation` splits every creation figure into
`ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`, and the published
table bills the 1-hour ones at 2x base input against the 5-minute ones' 1.25x.
Billing every creation token at the 5-minute rate is the cheap answer and the
wrong one - on this machine's own traffic the 1-hour share runs 10-40% of
creation per model, which understates the bill by several percent. So the split
the usage record already carries is the split the price table carries.

`config.json`'s `pricing` block holds the checked-in table, each row carrying
the `source` it was read from and the `checked` date it was read on;
`state/pricing.json` is the per-user override, exactly the goal/graph pattern -
a *patch*, so `tracker.py pricing set claude-opus-5.output=30` pins one number
and leaves every other field still following config.json.

Nothing here invents a price. A model the table does not price, or prices with
junk, comes back unpriced (None), and the report renders it as `unpriced` and
names it in a caveat rather than quietly billing it at some neighbour's rate.
The only price this module supplies by itself is the local lane: the model in
`local_model` runs on the operator's own GPU, so it is seeded at 0 with source
`local` when the table does not already name it.

Never raises. It is read from `build_summary`, from the CLI and from
`load_config`'s callers, so a hand-edited file degrades to the configured table
rather than taking a report - or the poll - down.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from .config import Config
from .models import utcnow

# The five numbers a row needs before anything may be billed against it. A row
# that names four of them is a row that cannot bill one of the two cache-write
# durations, so `is_priced` says no rather than billing 1-hour writes at the
# 5-minute rate.
PRICE_FIELDS = ("input", "output", "cache_write", "cache_write_1h", "cache_read")
META_FIELDS = ("source", "checked")
ROW_FIELDS = PRICE_FIELDS + META_FIELDS
SET_FIELDS = ROW_FIELDS

SOURCE_CONFIG = "config.json"
SOURCE_OVERRIDE = "state/pricing.json"
LOCAL_SOURCE = "local"
UNPRICED = "unpriced"
# USD per this many tokens: every published price is quoted per million.
PER_TOKENS = 1_000_000.0


def _usd(value: Any) -> float | None:
    """A non-negative finite dollar amount, or None. Never raises.

    None is the whole point: it is what "this model has no published price"
    looks like everywhere downstream, and it is what junk degrades to.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lstrip("$").replace(",", "")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def normalize_row(raw: Any, base: Any = None) -> dict[str, Any]:
    """One complete price row: `base` first, then whatever `raw` actually names.

    A field `raw` does not mention keeps the base value, so an override that
    sets one number is a patch over the configured row. A field it sets to junk
    also keeps the base value - a typo must not silently unprice a model - but
    an explicit `null` does clear it, which is how a row is marked unpriced on
    purpose.
    """
    row: dict[str, Any] = {field: None for field in ROW_FIELDS}
    for source in (base, raw):
        if not isinstance(source, dict):
            continue
        for field in PRICE_FIELDS:
            if field not in source:
                continue
            value = source[field]
            if value is None:
                row[field] = None
            else:
                parsed = _usd(value)
                if parsed is not None:
                    row[field] = parsed
        for field in META_FIELDS:
            if field not in source:
                continue
            if source[field] is None:
                row[field] = None
            else:
                parsed_text = _text(source[field])
                if parsed_text is not None:
                    row[field] = parsed_text
    return row


def normalize(raw: Any, base: Any = None) -> dict[str, dict[str, Any]]:
    """Fill `raw` out into a complete table, taking `base` for what it omits."""
    table: dict[str, dict[str, Any]] = {}
    if isinstance(base, dict):
        for model, row in base.items():
            if isinstance(model, str) and model.strip():
                table[model.strip()] = normalize_row(row)
    if isinstance(raw, dict):
        for model, row in raw.items():
            if not isinstance(model, str) or not model.strip():
                continue
            name = model.strip()
            table[name] = normalize_row(row, table.get(name))
    return table


def is_priced(row: Any) -> bool:
    """True only when all four numbers are present; a partial row is unpriced."""
    if not isinstance(row, dict):
        return False
    for field in PRICE_FIELDS:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(float(value)):
            return False
    return True


def price_for(table: Any, model: Any, default: Any = None) -> dict[str, Any] | None:
    """The row to bill `model` at, or None when nothing prices it."""
    row = table.get(str(model)) if isinstance(table, dict) else None
    if is_priced(row):
        return row
    if is_priced(default):
        return default
    return None


def default_row(cfg: Config) -> dict[str, Any] | None:
    """`pricing_default` from the config, or None (the shipped value is null).

    A default row bills every model the table does not name. It ships as null
    on purpose: guessing one model's price from another's is exactly what this
    module exists not to do.
    """
    row = normalize_row(getattr(cfg, "pricing_default", None))
    return row if is_priced(row) else None


def _seed_local(cfg: Config, table: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Price the local lane at zero when the table does not already name it.

    The local model runs on the operator's own GPU through FreeToken, so it
    costs no API dollars; leaving it unpriced would put the one lane whose
    price is *known* into the report's unpriced caveat.
    """
    local = str(getattr(cfg, "local_model", "") or "").strip()
    if not local or is_priced(table.get(local)):
        return table
    table[local] = normalize_row(
        {field: 0.0 for field in PRICE_FIELDS},
        {"source": LOCAL_SOURCE, "checked": None},
    )
    return table


def _override_payload(cfg: Config) -> dict[str, Any] | None:
    """The model map inside state/pricing.json, or None when there is not one.

    None covers every way the file can fail to be an override: absent,
    unreadable, not JSON, not an object, an object naming no model.
    """
    try:
        data = json.loads(cfg.pricing_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("pricing") if isinstance(data.get("pricing"), dict) else data
    named = [k for k, v in payload.items()
             if isinstance(k, str) and k.strip() and isinstance(v, dict)]
    if not named:
        return None
    return payload


def read_pricing_source(cfg: Config) -> tuple[dict[str, dict[str, Any]], str]:
    """(table, where it came from). state/pricing.json wins when it is readable."""
    base = _seed_local(cfg, normalize(getattr(cfg, "pricing", None)))
    payload = _override_payload(cfg)
    if payload is None:
        return base, SOURCE_CONFIG
    return normalize(payload, base), SOURCE_OVERRIDE


def read_pricing(cfg: Config) -> dict[str, dict[str, Any]]:
    return read_pricing_source(cfg)[0]


def override_warning(cfg: Config) -> str | None:
    """"there is a state/pricing.json and it is being ignored", or None."""
    try:
        exists = cfg.pricing_file.exists()
    except (OSError, AttributeError):
        return None
    if exists and _override_payload(cfg) is None:
        return (f"warning: {SOURCE_OVERRIDE} is unreadable or names no model; "
                f"using {SOURCE_CONFIG}")
    return None


def _named_fields(patch: Any) -> dict[str, tuple[str, ...]]:
    """{model: the fields `patch` actually names}, ignoring everything else."""
    named: dict[str, tuple[str, ...]] = {}
    if not isinstance(patch, dict):
        return named
    for model, block in patch.items():
        if not isinstance(model, str) or not isinstance(block, dict):
            continue
        fields = tuple(f for f in ROW_FIELDS if f in block)
        if fields:
            named[model.strip()] = fields
    return named


def write_pricing(cfg: Config, patch: dict[str, dict[str, Any]],
                  ) -> dict[str, dict[str, Any]]:
    """Persist ONLY the fields `patch` names, merged into the standing override.

    A patch, not a snapshot - the same contract as `graph.write_graph`, and for
    the same reason: writing the resolved table would copy every price into
    state/pricing.json, and from then on every correction to config.json's
    published figures would be silently dead.
    """
    effective = read_pricing(cfg)
    resolved = normalize(patch, effective)
    stored = _override_payload(cfg) or {}
    kept = normalize(stored, effective)
    merged: dict[str, dict[str, Any]] = {}
    for model, fields in _named_fields(stored).items():
        merged[model] = {f: kept[model][f] for f in fields}
    for model, fields in _named_fields(patch).items():
        merged.setdefault(model, {}).update(
            {f: resolved[model][f] for f in fields})
    if not merged:
        return effective
    body = json.dumps({"pricing": merged, "set_at": utcnow().isoformat()}, indent=2)
    path = cfg.pricing_file
    tmp = path.parent / f"{path.name}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Swapped in whole, like control/goal/graph: a torn read would look like
        # "no override" and silently bill at the checked-in prices instead.
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
    return read_pricing(cfg)


def parse_assignments(items: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """`model.field=value` strings -> (patch, errors).

    The model id is split off at the LAST dot before the `=`, not the first:
    the local model is called `Qwen3.8-27B-NVFP4`, and splitting on the first
    dot would read that as a model `Qwen3` with a field `8-27B-NVFP4`.
    """
    patch: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for item in items:
        text = str(item).strip()
        if "=" not in text:
            errors.append(f"cannot read '{item}'; use model.field=value")
            continue
        target, value = text.split("=", 1)
        target = target.strip()
        if "." not in target:
            errors.append(f"cannot read '{item}'; use model.field=value")
            continue
        model, field = (part.strip() for part in target.rsplit(".", 1))
        if not model:
            errors.append(f"cannot read '{item}'; use model.field=value")
            continue
        if field not in SET_FIELDS:
            errors.append(f"unknown field '{field}'; use " + ", ".join(SET_FIELDS))
            continue
        value = value.strip()
        if field in PRICE_FIELDS:
            if value.lower() in ("none", "null", ""):
                patch.setdefault(model, {})[field] = None
                continue
            amount = _usd(value)
            if amount is None:
                errors.append(
                    f"'{value}' is not a USD amount per 1M tokens for {target}")
                continue
            patch.setdefault(model, {})[field] = amount
        else:
            patch.setdefault(model, {})[field] = value or None
    return patch, errors


def unpriced(table: Any, models: Any, default: Any = None) -> list[str]:
    """The models in `models` nothing prices, in the order given."""
    out: list[str] = []
    for model in models or ():
        name = str(model)
        if price_for(table, name, default) is None and name not in out:
            out.append(name)
    return out


def _money(value: Any) -> str:
    return UNPRICED if value is None else f"${float(value):.2f}"


def format_table(table: dict[str, dict[str, Any]],
                 default: dict[str, Any] | None = None) -> list[str]:
    """The lines `tracker.py pricing` prints; USD per 1M tokens."""
    lines = [f"  {'model':<28} {'input':>9} {'output':>9} {'wr 5m':>9} "
             f"{'wr 1h':>9} {'cache rd':>9}  source"]
    for model in sorted(table):
        row = table[model]
        note = row.get("source") or "?"
        if row.get("checked"):
            note += f" ({row['checked']})"
        if not is_priced(row):
            note = f"{UNPRICED} - {note}"
        lines.append(
            f"  {model:<28} {_money(row.get('input')):>9} "
            f"{_money(row.get('output')):>9} {_money(row.get('cache_write')):>9} "
            f"{_money(row.get('cache_write_1h')):>9} "
            f"{_money(row.get('cache_read')):>9}  {note}")
    if not table:
        lines.append(f"  (no models priced; every model renders {UNPRICED})")
    lines.append(
        "  default for unlisted models: "
        + (f"{_money(default.get('input'))} in / {_money(default.get('output'))} out"
           if is_priced(default) else f"none ({UNPRICED})"))
    return lines
