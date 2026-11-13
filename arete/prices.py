from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import paths, record, util

SNAPSHOT_FILE = paths.STATE_DIR / "price-snapshot.json"
SCHEMA = 1
MAX_BYTES = 8 * 1024 * 1024
KINDS = ("native", "marketplace")
UNITS = {"per_million_tokens": 1.0, "per_thousand_tokens": 1_000.0, "per_token": 1_000_000.0}
COMPONENTS = ("input", "output", "cache_read", "cache_write")
TOP_KEYS = ("schema", "source", "observed", "routes")
ROUTE_KEYS = ("route", "kind", "provider", "model", "aliases", "currency", "unit", "rates", "retired",
              "replacement", "source", "observed")
RATE_KEYS = (*COMPONENTS, "from", "until", "when", "label")
STALE_AFTER = timedelta(days=30)
MATERIAL_SHARE = 0.01
MAX_PRICE = 1e12


def moment(value, where: str) -> str:
    try:
        parsed = util.parse_iso(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"{where}: must be an ISO time with a zone, not {value!r}")
    return util.iso(parsed.astimezone(UTC))


def observation(value, where: str) -> str:
    stamp = moment(value, where)
    if util.parse_iso(stamp) > util.now():
        raise ValueError(f"{where}: observation time is in the future: {value!r}")
    return stamp


def text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: must be a nonempty string")
    return value.strip()


def table(data, allowed: tuple, where: str) -> dict:
    if isinstance(data, dict):
        for key in data:
            if key not in allowed:
                raise ValueError(f"{where}: unknown key {key!r}")
        return data
    raise ValueError(f"{where}: must be an object")


def items(data, where: str) -> list:
    if isinstance(data, list):
        return data
    raise ValueError(f"{where}: must be a list")


def condition(data, where: str) -> dict:
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{where}: must be a nonempty table")
    found = {}
    for key, value in data.items():
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError(f"{where} {key}: must be a scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{where} {key}: must be a finite scalar")
        if key == "min_input_tokens":
            whole = (isinstance(value, int) and not isinstance(value, bool)) or (
                isinstance(value, float) and value.is_integer())
            if not whole or value < 1:
                raise ValueError(f"{where} {key}: must be a whole number of 1 or more")
            value = int(value)
        found[key] = value
    return found


def rate_entry(data, where: str) -> dict:
    from . import routing

    data = table(data, RATE_KEYS, where)
    found: dict = {}
    for name in COMPONENTS:
        if name in data:
            try:
                price = routing.number(data[name])
            except OverflowError:
                price = None
            if price is None or price > MAX_PRICE:
                raise ValueError(f"{where} {name}: price must be a finite nonnegative number "
                                 f"of at most {MAX_PRICE:g}")
            found[name] = price
    if not found:
        raise ValueError(f"{where}: needs at least one of {', '.join(COMPONENTS)}")
    for key in ("from", "until"):
        if key in data:
            found[key] = moment(data[key], f"{where} {key}")
    if "from" in found and "until" in found and util.parse_iso(found["until"]) <= util.parse_iso(found["from"]):
        raise ValueError(f"{where}: until must be after from")
    if "when" in data:
        found["when"] = condition(data["when"], f"{where} when")
    if "label" in data:
        if not isinstance(data["label"], str):
            raise ValueError(f"{where} label: must be a string")
        found["label"] = data["label"]
    return found


def route_entry(data, where: str, top: dict, claimed: dict) -> dict:
    from . import routing, runner

    data = table(data, ROUTE_KEYS, where)
    for key in ("route", "kind", "provider", "model", "currency", "unit", "rates"):
        if key not in data:
            raise ValueError(f"{where}: missing {key}")
    name = text(data["route"], f"{where} route")
    if data["kind"] not in KINDS:
        raise ValueError(f"{where} kind: must be one of {', '.join(KINDS)}, not {data['kind']!r}")
    currency = data["currency"]
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isascii() and currency.isalpha()
            and currency.isupper()):
        raise ValueError(f"{where} currency: must be three uppercase letters, not {currency!r}")
    if not isinstance(data["unit"], str) or data["unit"] not in UNITS:
        raise ValueError(f"{where} unit: must be one of {', '.join(UNITS)}, not {data['unit']!r}")
    rates = data["rates"]
    if not isinstance(rates, list) or not rates:
        raise ValueError(f"{where} rates: must be a nonempty list")
    aliases = items(data.get("aliases", []), f"{where} aliases")
    vendor = routing.identity(*runner.route(name))[0]
    names = [("route", name), *[(f"aliases[{n}]", text(a, f"{where} aliases[{n}]")) for n, a in enumerate(aliases)]]
    for label, key in names:
        if key in claimed:
            raise ValueError(f"{where} {label}: name {key!r} is already used by {claimed[key]}")
        if label != "route" and routing.identity(*runner.route(key))[0] != vendor:
            raise ValueError(f"{where} {label}: alias {key!r} is a different vendor than {name!r}")
        claimed[key] = where
    entry = {"route": name, "kind": data["kind"], "provider": text(data["provider"], f"{where} provider"),
             "model": text(data["model"], f"{where} model"), "aliases": [key for label, key in names[1:]],
             "currency": currency, "unit": data["unit"],
             "rates": [rate_entry(r, f"{where} rates[{n}]") for n, r in enumerate(rates)],
             "source": text(data["source"], f"{where} source") if "source" in data else top["source"],
             "observed": observation(data["observed"], f"{where} observed") if "observed" in data
             else top["observed"]}
    if data.get("retired") is not None:
        entry["retired"] = moment(data["retired"], f"{where} retired")
    if data.get("replacement") is not None:
        entry["replacement"] = text(data["replacement"], f"{where} replacement")
    return entry


def parse(data) -> dict:
    data = table(data, TOP_KEYS, "snapshot")
    for key in TOP_KEYS:
        if key not in data:
            raise ValueError(f"snapshot: missing {key}")
    if isinstance(data["schema"], bool) or data["schema"] != SCHEMA:
        raise ValueError(f"snapshot schema: must be {SCHEMA}, not {data['schema']!r}")
    top = {"schema": SCHEMA, "source": text(data["source"], "snapshot source"),
           "observed": observation(data["observed"], "snapshot observed")}
    if not isinstance(data["routes"], list) or not data["routes"]:
        raise ValueError("snapshot routes: must be a nonempty list")
    claimed: dict = {}
    body = {**top, "routes": [route_entry(r, f"routes[{n}]", top, claimed) for n, r in enumerate(data["routes"])]}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**body, "version": digest[:12]}


def read_file(path) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
    except OSError as e:
        raise ValueError(f"{path}: {e.strerror or e}") from e
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{path}: larger than {MAX_BYTES} bytes")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"{path}: not JSON: {e}") from e
    return parse(data)


def store(snapshot: dict) -> None:
    util.write_json(SNAPSHOT_FILE, snapshot)


def load() -> dict | None:
    try:
        stored = json.loads(Path(SNAPSHOT_FILE).read_text())
        stored.pop("version", None)
        return parse(stored)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def index(snapshot: dict) -> dict[str, dict]:
    return {name: entry for entry in snapshot["routes"] for name in (entry["route"], *entry["aliases"])}


def in_effect(rate: dict, at: datetime) -> bool:
    return ((not rate.get("from") or util.parse_iso(rate["from"]) <= at)
            and (not rate.get("until") or at < util.parse_iso(rate["until"])))


def proven(rate: dict, request_input: int | None) -> bool:
    when = rate.get("when")
    if not when:
        return True
    return (set(when) == {"min_input_tokens"} and request_input is not None
            and request_input >= when["min_input_tokens"])


def applied(snapshot: dict | None, runner: str, models: tuple, at: datetime,
            request_input: int | None = None) -> dict | None:
    if not snapshot:
        return None
    names = index(snapshot)
    matched = next((key for model in models for key in (f"{runner}:{model}", model) if key in names), None)
    if matched is None:
        return None
    entry = names[matched]
    live = [(n, r) for n, r in enumerate(entry["rates"], 1) if in_effect(r, at)]
    usable = [r for _, r in live if proven(r, request_input)]
    chosen = usable[-1] if usable else {}
    unproven = [r.get("label") or f"rate {n}" for n, r in live if not proven(r, request_input)]
    return {"source": entry["source"], "observed": entry["observed"], "version": snapshot["version"],
            "route": entry["route"], "matched": matched, "kind": entry["kind"], "provider": entry["provider"],
            "model": entry["model"], "currency": entry["currency"], "unit": entry["unit"],
            **{name: chosen.get(name) for name in COMPONENTS}, "label": chosen.get("label", ""),
            "from": chosen.get("from"), "until": chosen.get("until"), "when": chosen.get("when"),
            "unproven": unproven, "at": util.iso(at), "retired": entry.get("retired"),
            "replacement": entry.get("replacement")}


def per_million_usd(quote: dict) -> tuple:
    if quote["currency"] != "USD":
        return (None,) * 4
    return tuple(None if quote[name] is None else round(quote[name] * UNITS[quote["unit"]], 6)
                 for name in COMPONENTS)


def reference_usd(quote: dict | None, tokens_in: int, tokens_out: int, cache_read: int,
                  cache_write: int) -> float | None:
    if not quote:
        return None
    fresh = tokens_in - cache_read - cache_write
    if fresh < 0:
        return None
    total = 0.0
    for count, rate in zip((fresh, tokens_out, cache_read, cache_write), per_million_usd(quote), strict=True):
        if count > 0:
            if rate is None:
                return None
            total += count * rate
    return round(total / 1e6, 6) if math.isfinite(total) else None


def review(snapshot: dict, now: datetime) -> list[dict]:
    found = []
    for entry in snapshot["routes"]:
        name = entry["route"]
        reasons = []
        if now - util.parse_iso(entry["observed"]) > STALE_AFTER:
            reasons.append(f"observed {entry['observed']}")
        if not any(in_effect(r, now) for r in entry["rates"]):
            reasons.append("no rate in effect now")
        if reasons:
            found.append({"route": name, "finding": "stale", "detail": "; ".join(reasons)})
        if entry.get("retired"):
            when = "retired since" if util.parse_iso(entry["retired"]) <= now else "retires on"
            replacement = f"; replacement {entry['replacement']}" if entry.get("replacement") else ""
            found.append({"route": name, "finding": "retired",
                          "detail": f"{when} {entry['retired']}{replacement}"})
    return found


def moved(old, new) -> bool:
    if old is None or new is None:
        return old is not new
    if old == new:
        return False
    change, threshold = abs(new - old), MATERIAL_SHARE * abs(old)
    return old == 0 or change >= threshold or math.isclose(change, threshold, rel_tol=1e-9)


def current(snapshot: dict, name: str, now: datetime) -> dict:
    return applied(snapshot, "", (name,), now)


def schedule_changes(before: dict, after: dict) -> list[str]:
    old = dict(enumerate(before["rates"], 1))
    new = dict(enumerate(after["rates"], 1))
    found = []
    for n in sorted(old.keys() | new.keys()):
        left, right = old.get(n), new.get(n)
        label = "conditional rate" if (left or {}).get("when") or (right or {}).get("when") else "rate"
        if left is None or right is None:
            found.append(f"{label} {n} {json.dumps(left, sort_keys=True)} -> "
                         f"{json.dumps(right, sort_keys=True)}")
            continue
        for key in (*COMPONENTS, "when", "from", "until"):
            a, b = left.get(key), right.get(key)
            if moved(a, b) if key in COMPONENTS else a != b:
                found.append(f"{label} {n} {key} {a} -> {b}")
    return found


def changes(name: str, old: dict, new: dict, now: datetime) -> list[str]:
    before, after = current(old, name, now), current(new, name, now)
    found = [f"{key} {before[key]} -> {after[key]}" for key in COMPONENTS if moved(before[key], after[key])]
    for key in ("currency", "unit", "kind", "provider", "model", "retired", "replacement"):
        if before[key] != after[key]:
            found.append(f"{key} {before[key]} -> {after[key]}")
    found += schedule_changes(index(old)[name], index(new)[name])
    return found


def diff(old: dict | None, new: dict, now: datetime) -> list[dict]:
    found = review(new, now)
    if old is None:
        return found
    before = {e["route"] for e in old["routes"]}
    after = {e["route"] for e in new["routes"]}
    for name in sorted(before & after):
        if detail := changes(name, old, new, now):
            found.append({"route": name, "finding": "changed", "detail": "; ".join(detail)})
    old_names, new_names = index(old), index(new)
    for name in sorted(old_names.keys() & new_names.keys()):
        if old_names[name]["route"] != new_names[name]["route"]:
            found.append({"route": name, "finding": "changed",
                          "detail": f"alias {name} moved from {old_names[name]['route']} "
                                    f"to {new_names[name]['route']}"})
    for name in sorted(new_names.keys() - old_names.keys()):
        if name != new_names[name]["route"]:
            found.append({"route": name, "finding": "added",
                          "detail": f"alias {name} added to {new_names[name]['route']}"})
    for name in sorted(old_names.keys() - new_names.keys()):
        if name != old_names[name]["route"]:
            found.append({"route": name, "finding": "dropped",
                          "detail": f"alias {name} removed from {old_names[name]['route']}"})
    found += [{"route": name, "finding": "added", "detail": "new route"} for name in sorted(after - before)]
    found += [{"route": name, "finding": "dropped", "detail": "no longer listed"} for name in sorted(before - after)]
    return found


def recompute(run_id: str, step: str) -> dict:
    path = record.find_record(run_id, step)
    if path is None:
        raise ValueError(f"no record for run {run_id} step {step}")
    result = next((e for e in record.lines(path) if e.get("arete") == "result"), None)
    if result is None:
        raise ValueError(f"{run_id} {step}: the record has no result line")
    parts = result.get("calls") or [result]
    if any(not part.get("quote") for part in parts):
        raise ValueError(f"{run_id} {step}: the record retains no quote")
    totals = [reference_usd(p["quote"], p.get("tokens_in") or 0, p.get("tokens_out") or 0,
                            p.get("cache_read") or 0, p.get("cache_write") or 0) for p in parts]
    total = None if any(t is None for t in totals) else round(sum(totals), 6)
    recorded = result.get("reference_usd")
    return {"run": run_id, "step": step, "runner": result.get("runner"), "model": result.get("model"),
            "provider": result.get("provider"), "auth_mode": result.get("auth_mode"),
            "cost_usd": result.get("cost_usd"), "paid_usd": result.get("paid_usd"),
            "tokens_in": result.get("tokens_in"), "tokens_out": result.get("tokens_out"),
            "cache_read": result.get("cache_read"), "cache_write": result.get("cache_write"),
            "quote": parts[-1]["quote"], "recorded_reference_usd": recorded, "reference_usd": total,
            "matches": recorded == total}
