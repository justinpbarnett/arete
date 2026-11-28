from __future__ import annotations

import hashlib
import json
import math
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import paths, record, util

SNAPSHOT_FILE = paths.STATE_DIR / "price-snapshot.json"
MODELS_REL_PATH = "prices/models.toml"
BILLING_REL_PATH = "prices/billing.toml"
JEV_BUILTIN_PRICE_VERSION = "builtin:jev:v1"
SCHEMA = 1
MAX_BYTES = 8 * 1024 * 1024
KINDS = ("native", "marketplace")
UNITS = {"per_million_tokens": 1.0, "per_thousand_tokens": 1_000.0, "per_token": 1_000_000.0}
COMPONENTS = ("input", "output", "cache_read", "cache_write")
TOP_KEYS = ("schema", "source", "observed", "routes")
ROUTE_KEYS = ("route", "kind", "provider", "model", "aliases", "currency", "unit", "rates", "retired",
              "replacement", "source", "observed")
RATE_KEYS = (*COMPONENTS, "from", "until", "when", "label")
BILLING_TOP_KEYS = ("schema", "local_providers", "models")
BILLING_RATE_KEYS = ("input", "output", "unit")
STALE_AFTER = timedelta(days=30)
MATERIAL_SHARE = 0.01
MAX_PRICE = 1e12
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTENT_VERSION_RE = re.compile(r"^prices/(models|billing)@([0-9a-f]{12})$")


@dataclass(frozen=True)
class BillingRate:
    input: float | None = None
    output: float | None = None
    unit: str = "per_million_tokens"

    def per_million(self) -> tuple[float | None, float | None]:
        scale = UNITS[self.unit]
        return (
            None if self.input is None else self.input * scale,
            None if self.output is None else self.output * scale,
        )

    def cost_usd(self, tokens_in: int, tokens_out: int) -> float:
        r_in, r_out = self.per_million()
        return (tokens_in * (r_in or 0.0) + tokens_out * (r_out or 0.0)) / 1_000_000

    def cost_nusd(self, tokens_in: int, tokens_out: int) -> int:
        return int(util.nusd(self.cost_usd(tokens_in, tokens_out)) or 0)


@dataclass(frozen=True)
class BillingPolicy:
    models: Mapping[str, BillingRate]
    local_providers: tuple[str, ...] = ("ollama",)
    content_version: str = ""
    sha256: str = ""
    config_commit: str = ""
    source: str = "compat"

    def lookup(self, runner: str, model: str) -> tuple[str, BillingRate] | None:
        if model and model in self.models:
            return model, self.models[model]
        prefixed = f"{runner}:{model}" if runner and model else ""
        if prefixed and prefixed in self.models:
            return prefixed, self.models[prefixed]
        return None

    def as_cfg_pricing(self) -> dict[str, tuple[float | None, float | None]]:
        return {name: rate.per_million() for name, rate in self.models.items()}

    def meta(self) -> dict[str, str]:
        return {
            "id": self.content_version,
            "sha256": self.sha256,
            "commit": self.config_commit,
            "path": BILLING_REL_PATH if self.content_version else "",
            "source": self.source,
        }


@dataclass(frozen=True)
class ChargeOutcome:
    cost_nusd: int | None
    paid_nusd: int | None
    auth_mode: str
    price_version: str

    @property
    def cost_usd(self) -> float:
        return 0.0 if self.cost_nusd is None else self.cost_nusd / 1_000_000_000

    @property
    def paid_usd(self) -> float:
        return 0.0 if self.paid_nusd is None else self.paid_nusd / 1_000_000_000


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
    data = table(data, RATE_KEYS, where)
    found: dict = {}
    for name in COMPONENTS:
        if name in data:
            try:
                price = util.nonnegative_number(data[name])
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
    from . import routing

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
    vendor = routing.identity(*routing.route(name))[0]
    names = [("route", name), *[(f"aliases[{n}]", text(a, f"{where} aliases[{n}]")) for n, a in enumerate(aliases)]]
    for label, key in names:
        if key in claimed:
            raise ValueError(f"{where} {label}: name {key!r} is already used by {claimed[key]}")
        if label != "route" and routing.identity(*routing.route(key))[0] != vendor:
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
    stripped = raw.lstrip()
    if stripped.startswith(b"{") or str(path).endswith(".json"):
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"{path}: not JSON: {e}") from e
    else:
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise ValueError(f"{path}: not valid TOML: {e}") from e
    return parse(data)


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise ValueError(f"unsupported TOML value: {value!r}")


def render_models_toml(snapshot: dict) -> str:
    parsed = parse({k: v for k, v in snapshot.items() if k != "version"})
    lines = [
        f"schema = {parsed['schema']}",
        f"source = {_toml_value(parsed['source'])}",
        f"observed = {_toml_value(parsed['observed'])}",
    ]
    for entry in parsed["routes"]:
        lines.append("")
        lines.append("[[routes]]")
        for key in ("route", "kind", "provider", "model", "currency", "unit"):
            lines.append(f"{key} = {_toml_value(entry[key])}")
        if entry.get("aliases"):
            lines.append(f"aliases = {_toml_value(entry['aliases'])}")
        if entry.get("source") and entry["source"] != parsed["source"]:
            lines.append(f"source = {_toml_value(entry['source'])}")
        if entry.get("observed") and entry["observed"] != parsed["observed"]:
            lines.append(f"observed = {_toml_value(entry['observed'])}")
        if entry.get("retired") is not None:
            lines.append(f"retired = {_toml_value(entry['retired'])}")
        if entry.get("replacement") is not None:
            lines.append(f"replacement = {_toml_value(entry['replacement'])}")
        for rate in entry["rates"]:
            lines.append("")
            lines.append("[[routes.rates]]")
            for key in (*COMPONENTS, "from", "until", "label"):
                if key in rate and rate[key] is not None:
                    lines.append(f"{_toml_key(key)} = {_toml_value(rate[key])}")
            if rate.get("when"):
                lines.append("")
                lines.append("[routes.rates.when]")
                for wkey, wval in sorted(rate["when"].items()):
                    lines.append(f"{_toml_key(wkey)} = {_toml_value(wval)}")
    return "\n".join(lines) + "\n"


def parse_billing(data: object, *, where: str = "billing") -> BillingPolicy:
    data = table(data, BILLING_TOP_KEYS, where)
    if "schema" not in data:
        raise ValueError(f"{where}: missing schema")
    if isinstance(data["schema"], bool) or data["schema"] != SCHEMA:
        raise ValueError(f"{where} schema: must be {SCHEMA}, not {data['schema']!r}")
    raw_local = data.get("local_providers", ["ollama"])
    local_list = items(raw_local, f"{where} local_providers")
    local_providers = tuple(text(entry, f"{where} local_providers[{i}]") for i, entry in enumerate(local_list))
    raw_models = data.get("models") or {}
    if not isinstance(raw_models, dict):
        raise ValueError(f"{where} models: must be a table")
    parsed_models: dict[str, BillingRate] = {}
    for model_key, raw_rate in raw_models.items():
        name = text(model_key, f"{where} models key")
        rtbl = table(raw_rate, BILLING_RATE_KEYS, f"{where}.models.{name}")
        unit = rtbl.get("unit", "per_million_tokens")
        if not isinstance(unit, str) or unit not in UNITS:
            raise ValueError(f"{where}.models.{name} unit: must be one of {', '.join(UNITS)}, not {unit!r}")
        vals: dict[str, float | None] = {"input": None, "output": None}
        for comp in ("input", "output"):
            if comp in rtbl:
                try:
                    price = util.nonnegative_number(rtbl[comp])
                except OverflowError:
                    price = None
                if price is None or price > MAX_PRICE:
                    raise ValueError(
                        f"{where}.models.{name} {comp}: price must be a finite nonnegative number of at most {MAX_PRICE:g}"
                    )
                vals[comp] = price
        parsed_models[name] = BillingRate(input=vals["input"], output=vals["output"], unit=unit)
    return BillingPolicy(models=parsed_models, local_providers=local_providers, source="config")


def render_billing_toml(
    models: Mapping[str, BillingRate | tuple[float | None, float | None]],
    local_providers: Sequence[str] = ("ollama",),
) -> str:
    lines = [
        f"schema = {SCHEMA}",
        f"local_providers = {_toml_value(list(local_providers))}",
    ]
    for model_key in sorted(models):
        entry = models[model_key]
        if isinstance(entry, BillingRate):
            r_in, r_out, unit = entry.input, entry.output, entry.unit
        else:
            r_in, r_out, unit = entry[0], entry[1], "per_million_tokens"
        lines.append("")
        lines.append(f"[models.{_toml_key(model_key)}]")
        if r_in is not None:
            lines.append(f"input = {_toml_value(r_in)}")
        if r_out is not None:
            lines.append(f"output = {_toml_value(r_out)}")
        if unit != "per_million_tokens":
            lines.append(f"unit = {_toml_value(unit)}")
    return "\n".join(lines) + "\n"


def parse_models_bytes(raw: bytes, *, config_commit: str = "") -> tuple[dict, dict]:
    data = tomllib.loads(raw.decode("utf-8"))
    snapshot = parse(data)
    sha = hashlib.sha256(raw).hexdigest()
    content_id = f"prices/models@{sha[:12]}"
    meta = {
        "id": content_id,
        "version": snapshot["version"],
        "source": snapshot["source"],
        "sha256": sha,
        "path": MODELS_REL_PATH,
        "commit": config_commit,
    }
    return snapshot, meta


def parse_billing_bytes(raw: bytes, *, config_commit: str = "") -> BillingPolicy:
    data = tomllib.loads(raw.decode("utf-8"))
    parsed = parse_billing(data)
    sha = hashlib.sha256(raw).hexdigest()
    return BillingPolicy(
        models=parsed.models,
        local_providers=parsed.local_providers,
        content_version=f"prices/billing@{sha[:12]}",
        sha256=sha,
        config_commit=config_commit,
        source="pinned" if config_commit else "config",
    )


def load_models(config_dir: Path | None = None, *, config_commit: str = "") -> tuple[dict | None, dict | None]:
    root = paths.CONFIG_DIR if config_dir is None else Path(config_dir)
    models_file = root / MODELS_REL_PATH
    if models_file.is_file():
        try:
            raw = models_file.read_bytes()
            return parse_models_bytes(raw, config_commit=config_commit)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
            return None, None
    if config_dir is None:
        if (snap := load_snapshot_file()) is not None:
            digest = hashlib.sha256(
                json.dumps(snap, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            return snap, {
                "id": "",
                "version": snap["version"],
                "source": snap["source"],
                "sha256": digest,
                "path": SNAPSHOT_FILE.name,
                "commit": "",
            }
    return None, None


def load_billing(config_dir: Path | None = None, *, cfg=None, config_commit: str = "") -> BillingPolicy:
    root = paths.CONFIG_DIR if config_dir is None else Path(config_dir)
    billing_file = root / BILLING_REL_PATH
    if billing_file.is_file():
        try:
            return parse_billing_bytes(billing_file.read_bytes(), config_commit=config_commit)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
            pass
    if cfg is not None:
        compat_models = {
            name: BillingRate(input=rate[0], output=rate[1])
            for name, rate in (cfg.pricing or {}).items()
        }
        return BillingPolicy(
            models=compat_models,
            local_providers=tuple(cfg.local_providers or ("ollama",)),
            content_version="",
            sha256="",
            config_commit="",
            source="compat",
        )
    return BillingPolicy(models={}, local_providers=("ollama",), source="compat")


def store(snapshot: dict) -> None:
    util.write_json(SNAPSHOT_FILE, snapshot)


def sync_snapshot_mirror(raw_models_toml: bytes) -> None:
    snapshot, _ = parse_models_bytes(raw_models_toml)
    store(snapshot)


def load_snapshot_file() -> dict | None:
    try:
        stored = json.loads(Path(SNAPSHOT_FILE).read_text())
        stored.pop("version", None)
        return parse(stored)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def load() -> dict | None:
    models_file = paths.CONFIG_DIR / MODELS_REL_PATH
    if models_file.is_file():
        try:
            snapshot, _ = parse_models_bytes(models_file.read_bytes())
            return snapshot
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
            pass
    return load_snapshot_file()


def migrate_snapshot(*, actor: str = "engine") -> str | None:
    from . import configrepo

    models_file = paths.CONFIG_DIR / MODELS_REL_PATH
    if models_file.is_file():
        try:
            _, meta = parse_models_bytes(models_file.read_bytes())
            return str(meta["id"])
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
            return None
    snap = load_snapshot_file()
    if snap is None:
        return None
    toml_text = render_models_toml(snap)
    configrepo.ensure_repo()
    with configrepo.writer_lock() as fence:
        if models_file.is_file():
            _, meta = parse_models_bytes(models_file.read_bytes())
            return str(meta["id"])
        _, base_blob = configrepo.read(MODELS_REL_PATH)
        configrepo.commit(
            {MODELS_REL_PATH: toml_text},
            {MODELS_REL_PATH: base_blob},
            actor=actor,
            source="engine",
            note="migrate price-snapshot.json to prices/models.toml",
            fence=fence,
        )
    _, meta = parse_models_bytes(toml_text.encode("utf-8"))
    return str(meta["id"])


def materialize_billing(cfg=None, *, actor: str = "engine") -> str | None:
    from . import config, configrepo

    billing_file = paths.CONFIG_DIR / BILLING_REL_PATH
    if billing_file.is_file():
        try:
            return parse_billing_bytes(billing_file.read_bytes()).content_version
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
            return None
    if cfg is None:
        cfg, _ = config.load_or_problem()
    toml_text = render_billing_toml(cfg.pricing, cfg.local_providers)
    configrepo.ensure_repo()
    with configrepo.writer_lock() as fence:
        if billing_file.is_file():
            return parse_billing_bytes(billing_file.read_bytes()).content_version
        _, base_blob = configrepo.read(BILLING_REL_PATH)
        configrepo.commit(
            {BILLING_REL_PATH: toml_text},
            {BILLING_REL_PATH: base_blob},
            actor=actor,
            source="engine",
            note="materialize prices/billing.toml from [pricing]",
            fence=fence,
        )
    return parse_billing_bytes(toml_text.encode("utf-8")).content_version


def import_models(snapshot: dict, *, actor: str = "", note: str = "") -> dict:
    from . import configcheck, configrepo, drafts, principals

    parsed = parse({k: v for k, v in snapshot.items() if k != "version"})
    toml_text = render_models_toml(parsed)
    raw = toml_text.encode("utf-8")
    resolved_actor = actor or principals.current() or "hand"
    configrepo.ensure_repo()
    if paths.DB_FILE.exists() and drafts.available():
        live_row = drafts._current_ver_row("prices", "models", MODELS_REL_PATH)
        base_ver = int(live_row["ver"]) if live_row and live_row.get("ver") is not None else 0
        draft_obj, report = drafts.create_and_check(
            "prices",
            "models",
            toml_text,
            author=resolved_actor,
            why=note or f"import prices/models ({parsed['source']} {parsed['version']})",
            base_version=base_ver,
        )
        if not report or not report["ok"]:
            problems = [p for st in (report or {}).get("stages", ()) for p in st.get("problems", ())]
            raise ValueError("; ".join(problems) or "prices/models check failed")
        activated = drafts.activate(
            draft_obj["id"],
            actor=resolved_actor,
            base_version=base_ver,
            source="draft",
        )
        commit_sha = activated["commit"]
    else:
        report = configcheck.check("prices", "models", MODELS_REL_PATH, toml_text, author=resolved_actor)
        if not report["ok"]:
            problems = [p for st in report["stages"] for p in st.get("problems", ())]
            raise ValueError("; ".join(problems) or "prices/models check failed")
        with configrepo.writer_lock() as fence:
            _, base_blob = configrepo.read(MODELS_REL_PATH)
            commit_sha = configrepo.commit(
                {MODELS_REL_PATH: toml_text},
                {MODELS_REL_PATH: base_blob},
                actor=resolved_actor,
                source="hand",
                note=note or f"import prices/models ({parsed['source']} {parsed['version']})",
                fence=fence,
            )
        store(parsed)
    _, meta = parse_models_bytes(raw, config_commit=commit_sha)
    return {"content_version": meta["id"], "commit": commit_sha, "snapshot": parsed, "meta": meta}


def load_version(selector: str) -> tuple[str, dict | BillingPolicy]:
    from . import configrepo
    from .store import config as config_store
    from .store import db as foundation

    raw_sel = selector.strip()
    if not raw_sel:
        raise ValueError("price_version must be nonempty")
    if raw_sel in ("models", "current"):
        snap, _ = load_models()
        if snap is None:
            raise LookupError("no prices/models catalogue available")
        return "models", snap
    if raw_sel == "billing":
        policy = load_billing()
        if not policy.content_version:
            raise LookupError("no prices/billing policy available")
        return "billing", policy
    matched = _CONTENT_VERSION_RE.fullmatch(raw_sel)
    if matched:
        name = matched.group(1)
        rel = MODELS_REL_PATH if name == "models" else BILLING_REL_PATH
        live_path = paths.CONFIG_DIR / rel
        if live_path.is_file():
            raw = live_path.read_bytes()
            if f"prices/{name}@{hashlib.sha256(raw).hexdigest()[:12]}" == raw_sel:
                if name == "models":
                    return "models", parse_models_bytes(raw)[0]
                return "billing", parse_billing_bytes(raw)
        if paths.DB_FILE.exists():
            try:
                with foundation.reader() as c:
                    rows = config_store.versions_of("prices", name, c)
                for row in rows:
                    row_sha = str(row.get("sha256") or "")
                    row_ver = str(row.get("version") or "")
                    if (row_ver == raw_sel or (row_sha and f"prices/{name}@{row_sha[:12]}" == raw_sel)) and row.get("commit_sha"):
                        data = configrepo.commit_bytes(str(row["commit_sha"]), rel)
                        if data is not None:
                            if name == "models":
                                return "models", parse_models_bytes(data, config_commit=str(row["commit_sha"]))[0]
                            return "billing", parse_billing_bytes(data, config_commit=str(row["commit_sha"]))
            except (OSError, ValueError, KeyError, foundation.NoDatabase, foundation.IncompatibleSchema):
                pass
        if configrepo.is_repo():
            log = configrepo._git(["log", "--format=%H", "--", rel], paths.CONFIG_DIR, check=False)
            for sha in (line.strip() for line in log.stdout.splitlines() if line.strip()):
                data = configrepo.commit_bytes(sha, rel)
                if data is not None and f"prices/{name}@{hashlib.sha256(data).hexdigest()[:12]}" == raw_sel:
                    if name == "models":
                        return "models", parse_models_bytes(data, config_commit=sha)[0]
                    return "billing", parse_billing_bytes(data, config_commit=sha)
        raise LookupError(f"unknown price_version {raw_sel!r}")

    # Bare snapshot version hash or commit SHA for prices/models.toml
    live_snap, _ = load_models()
    if live_snap is not None and live_snap.get("version") == raw_sel:
        return "models", live_snap
    if configrepo.is_repo():
        if len(raw_sel) >= 7 and all(ch in "0123456789abcdef" for ch in raw_sel.lower()):
            data = configrepo.commit_bytes(raw_sel, MODELS_REL_PATH)
            if data is not None:
                return "models", parse_models_bytes(data, config_commit=raw_sel)[0]
            log = configrepo._git(["log", "--format=%H", "--", MODELS_REL_PATH], paths.CONFIG_DIR, check=False)
            for sha in (line.strip() for line in log.stdout.splitlines() if line.strip()):
                data = configrepo.commit_bytes(sha, MODELS_REL_PATH)
                if data is None:
                    continue
                try:
                    snap, meta = parse_models_bytes(data, config_commit=sha)
                except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
                    continue
                if snap.get("version") == raw_sel or str(meta["sha256"]).startswith(raw_sel):
                    return "models", snap
    raise LookupError(f"unknown price_version {raw_sel!r}")


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


def reference_nusd(quote: dict | None, tokens_in: int, tokens_out: int, cache_read: int,
                   cache_write: int) -> int | None:
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
    if not math.isfinite(total):
        return None
    return util.nusd(total / 1e6)


def _provider_of_model(model: str) -> str:
    return model.partition("/")[0] if "/" in model else ""


def price_call(
    *,
    runner: str,
    model: str,
    provider: str,
    auth_mode: str,
    tokens_in: int,
    tokens_out: int,
    cache_read: int = 0,
    cache_write: int = 0,
    provider_cost_usd: float | None = 0.0,
    policy: BillingPolicy | None = None,
    selected_local: bool = False,
    openrouter_rates: Sequence[float | None] | None = None,
    strict_unknown_auth: bool = False,
) -> ChargeOutcome:
    from . import agent

    eff_provider = provider or _provider_of_model(model)
    if runner == agent.REPLAY:
        cost_n = util.nusd(0.0 if provider_cost_usd is None else provider_cost_usd)
        return ChargeOutcome(cost_nusd=cost_n, paid_nusd=0, auth_mode=auth_mode, price_version="replay")
    if selected_local:
        return ChargeOutcome(cost_nusd=0, paid_nusd=0, auth_mode="local", price_version="local")
    if openrouter_rates is not None:
        usd = agent.token_cost(openrouter_rates, tokens_in, tokens_out, cache_read, cache_write)
        cost_n = util.nusd(usd)
        paid_n = _paid_for_mode(cost_n, runner, eff_provider, auth_mode, strict_unknown=strict_unknown_auth)
        return ChargeOutcome(cost_nusd=cost_n, paid_nusd=paid_n, auth_mode=auth_mode, price_version="compat:openrouter")
    if runner == "pi" and auth_mode == "subscription":
        cost_n = util.nusd(0.0 if provider_cost_usd is None else provider_cost_usd)
        return ChargeOutcome(cost_nusd=cost_n, paid_nusd=0, auth_mode="subscription", price_version="provider")
    if policy is not None:
        found = policy.lookup(runner, model)
        if found is not None:
            _, rate = found
            cost_n = rate.cost_nusd(tokens_in, tokens_out)
            pv = policy.content_version if policy.content_version else "compat:pricing"
            paid_n = _paid_for_mode(cost_n, runner, eff_provider, auth_mode, strict_unknown=strict_unknown_auth)
            return ChargeOutcome(cost_nusd=cost_n, paid_nusd=paid_n, auth_mode=auth_mode, price_version=pv)
        if runner == "pi" and eff_provider in policy.local_providers:
            return ChargeOutcome(cost_nusd=0, paid_nusd=0, auth_mode="local", price_version="local")
    cost_n = util.nusd(provider_cost_usd)
    pv = "provider" if cost_n is not None else "compat:unknown"
    paid_n = _paid_for_mode(cost_n, runner, eff_provider, auth_mode, strict_unknown=strict_unknown_auth)
    return ChargeOutcome(cost_nusd=cost_n, paid_nusd=paid_n, auth_mode=auth_mode, price_version=pv)


def _paid_for_mode(
    cost_nusd: int | None,
    runner: str,
    provider: str,
    auth_mode: str,
    *,
    strict_unknown: bool = False,
) -> int | None:
    from . import agent

    if cost_nusd is None:
        return None
    if runner in (agent.REPLAY, "guard") or auth_mode in ("local", "subscription"):
        return 0
    if auth_mode == "api_key":
        return cost_nusd
    if strict_unknown:
        return None
    return 0 if agent.subscription(runner, provider, auth_mode) else cost_nusd


def billing_context_for(
    *,
    runner: str,
    model: str,
    provider: str,
    auth_mode: str,
    account_id: str | None,
    payer: str | None,
    selected_local: bool,
    policy: BillingPolicy,
    openrouter_rates: Sequence[float | None] | None = None,
    reference_meta: dict | None = None,
) -> dict:
    from . import agent

    eff_provider = provider or _provider_of_model(model)
    eff_auth_mode = "local" if selected_local else auth_mode
    matched = policy.lookup(runner, model)
    is_local_pi = (
        runner == "pi"
        and eff_auth_mode != "subscription"
        and matched is None
        and eff_provider in policy.local_providers
    )
    if runner == agent.REPLAY:
        payment_class = "replay"
        billing_source = "replay"
    elif selected_local or is_local_pi:
        payment_class = "local"
        eff_auth_mode = "local"
        billing_source = "local"
    elif openrouter_rates is not None:
        payment_class = "subscription" if eff_auth_mode == "subscription" else "api_key"
        billing_source = "compat:openrouter"
    elif runner == "pi" and eff_auth_mode == "subscription":
        payment_class = "subscription"
        billing_source = "provider"
    elif matched is not None:
        payment_class = "subscription" if eff_auth_mode == "subscription" else (
            "api_key" if eff_auth_mode == "api_key" else (
                "subscription" if agent.subscription(runner, eff_provider, eff_auth_mode) else "api_key"
            )
        )
        billing_source = "pinned" if policy.content_version else "compat:pricing"
    else:
        payment_class = "subscription" if agent.subscription(runner, eff_provider, eff_auth_mode) else "api_key"
        billing_source = "provider"

    rates_map = {
        k: {"input": r.input, "output": r.output, "unit": r.unit}
        for k, r in policy.models.items()
    }
    return {
        "v": 1,
        "runner": runner,
        "model": model,
        "provider": eff_provider,
        "account_id": account_id,
        "payer": payer,
        "auth_mode": eff_auth_mode,
        "payment_class": payment_class,
        "selected_local": bool(selected_local),
        "billing_source": billing_source,
        "billing_version": policy.content_version,
        "billing_sha256": policy.sha256,
        "billing_commit": policy.config_commit,
        "models": rates_map,
        "local_providers": list(policy.local_providers),
        **({"openrouter_rates": list(openrouter_rates)} if openrouter_rates is not None else {}),
        **({
            "reference_version": str(reference_meta.get("id") or ""),
            "reference_commit": str(reference_meta.get("commit") or ""),
        } if isinstance(reference_meta, dict) else {}),
    }


def policy_from_billing_context(ctx: dict) -> BillingPolicy:
    raw_models = ctx.get("models")
    models: dict[str, BillingRate] = {}
    if isinstance(raw_models, dict):
        for key, item in raw_models.items():
            if isinstance(item, dict):
                unit = str(item.get("unit") or "per_million_tokens")
                if unit not in UNITS:
                    unit = "per_million_tokens"
                models[str(key)] = BillingRate(
                    input=item.get("input"),
                    output=item.get("output"),
                    unit=unit,
                )
    raw_local = ctx.get("local_providers")
    local_providers = tuple(str(p) for p in raw_local) if isinstance(raw_local, list) else ("ollama",)
    content_ver = str(ctx.get("billing_version") or "")
    return BillingPolicy(
        models=models,
        local_providers=local_providers,
        content_version=content_ver,
        sha256=str(ctx.get("billing_sha256") or ""),
        config_commit=str(ctx.get("billing_commit") or ""),
        source="pinned" if content_ver else "compat",
    )


def price_turn(header: dict, ev: dict, *, billing_ctx: dict | None = None) -> ChargeOutcome:
    ctx = billing_ctx if isinstance(billing_ctx, dict) else (
        header.get("billing_context") if isinstance(header.get("billing_context"), dict) else None
    )
    tin = int(ev.get("tokens_in") or 0)
    tout = int(ev.get("tokens_out") or 0)
    cr = int(ev.get("cache_read") or 0)
    cw = int(ev.get("cache_write") or 0)
    ev_cost = ev.get("cost_usd")
    provider_cost = float(ev_cost) if ev_cost is not None else None
    if ctx is not None:
        policy = policy_from_billing_context(ctx)
        runner = str(ctx.get("runner") or header.get("runner") or "")
        model = str(ev.get("model") or ctx.get("model") or header.get("model") or "")
        provider = str(ev.get("provider") or ctx.get("provider") or header.get("provider") or "")
        auth_mode = str(ctx.get("auth_mode") or header.get("auth_mode") or "")
        selected_local = bool(ctx.get("selected_local"))
        openrouter_rates = ctx.get("openrouter_rates")
        return price_call(
            runner=runner,
            model=model,
            provider=provider,
            auth_mode=auth_mode,
            tokens_in=tin,
            tokens_out=tout,
            cache_read=cr,
            cache_write=cw,
            provider_cost_usd=provider_cost,
            policy=policy,
            selected_local=selected_local,
            openrouter_rates=openrouter_rates if isinstance(openrouter_rates, list) else None,
            strict_unknown_auth=True,
        )
    # Legacy header without frozen billing_context: preserve explicit compatibility evidence; never read live policy.
    runner = str(header.get("runner") or "")
    model = str(ev.get("model") or header.get("model") or "")
    provider = str(ev.get("provider") or header.get("provider") or "")
    auth_mode = str(header.get("auth_mode") or "")
    rate = header.get("pricing_rate")
    compat_models = (
        {model or header.get("model") or "": BillingRate(input=rate[0], output=rate[1])}
        if isinstance(rate, (list, tuple)) and len(rate) >= 2
        else {}
    )
    policy = BillingPolicy(models=compat_models, local_providers=(), content_version="", source="compat")
    usage_rates = header.get("usage_rates")
    return price_call(
        runner=runner,
        model=model,
        provider=provider,
        auth_mode=auth_mode,
        tokens_in=tin,
        tokens_out=tout,
        cache_read=cr,
        cache_write=cw,
        provider_cost_usd=provider_cost,
        policy=policy,
        selected_local=bool(header.get("local_billing")),
        openrouter_rates=usage_rates if isinstance(usage_rates, (list, tuple)) else None,
        strict_unknown_auth=True,
    )


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
