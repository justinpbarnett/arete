from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import re
import shlex
import threading
import tomllib
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, cron, events, factory_control, gh, jira, paths, util
from .loops import Loop, as_list, blank, github_remote
from .store import db as foundation
from .store import loops as loop_store
from .store import sources as source_store

SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DEFAULT_EVERY = 300
MAX_BACKOFF_S = 3600
GITHUB_WATCH = ("ci_red", "issue", "advisory")
LOG_PATTERN = r"ERROR|CRITICAL|FATAL|Exception|Traceback|panic:"
MAX_LOG_READ = 2_000_000
CATCH_UP_SLACK = timedelta(minutes=2)
SHOWN_KEYS = 5
SIGNATURE_NOISE = [
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.IGNORECASE), "<hex>"),
    (re.compile(r"(?<!\w)'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\d+"), "<n>"),
]

type BindingResolver = Callable[[str, dict, config.Config], dict[str, object]]
_BINDING_RESOLVER: BindingResolver | None = None
_POLL_POOL_LOCK = threading.Lock()
_POLL_POOL: factory_control.WorkPool | None = None


def set_binding_resolver(resolver: BindingResolver | None) -> None:
    global _BINDING_RESOLVER
    _BINDING_RESOLVER = resolver


def set_poll_pool(pool: factory_control.WorkPool | None) -> None:
    global _POLL_POOL
    with _POLL_POOL_LOCK:
        _POLL_POOL = pool


def submit_poll(fn: Callable, *args, **kwargs) -> Future:
    global _POLL_POOL
    with _POLL_POOL_LOCK:
        if _POLL_POOL is None:
            _POLL_POOL = factory_control.WorkPool(4, thread_name_prefix="source-poll")
        pool = _POLL_POOL
    return pool.submit(fn, *args, **kwargs)


@dataclass(frozen=True)
class Src:
    id: str
    definition: dict
    origin: str
    loops: tuple[str, ...]
    version: str
    scope_sha: str
    dir: Path
    repo: str = ""
    enabled: bool = True
    feeds: str = ""
    line: str | None = None
    why: str | None = None
    loop_objects: tuple[Loop, ...] = ()


def loop_slug(loop_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "-", loop_name.lower())


def is_file_ref(table: dict) -> bool:
    return set(table.keys()) == {"id"}


def table_source_id(loop_name: str, table: dict) -> str:
    if "id" in table and table["id"] is not None:
        return str(table["id"])
    slug = loop_slug(loop_name)
    if "name" in table and table["name"] is not None:
        return f"{slug}-{table['name']}"
    return slug


def source_file_path(sid: str, config_dir: Path | None = None) -> Path | None:
    if not isinstance(sid, str) or not SOURCE_ID_RE.fullmatch(sid):
        return None
    root = config_dir or paths.CONFIG_DIR
    user_file = root / "sources" / f"{sid}.toml"
    if user_file.is_file():
        return user_file
    default_file = paths.DEFAULTS_DIR / "sources" / f"{sid}.toml"
    if default_file.is_file():
        return default_file
    return None


def source_file_rel(path: Path, config_dir: Path | None = None) -> str:
    root = (config_dir or paths.CONFIG_DIR).resolve()
    resolved = path.resolve()
    if resolved.is_relative_to(root):
        return resolved.relative_to(root).as_posix()
    pkg = paths.PACKAGE_DIR.parent.resolve()
    if resolved.is_relative_to(pkg):
        return resolved.relative_to(pkg).as_posix()
    return str(path)


def content_version(sid: str, raw: bytes) -> str:
    return f"source/{sid}@{hashlib.sha256(raw).hexdigest()[:12]}"


def inline_version(sid: str, table: dict) -> str:
    encoded = json.dumps(table, sort_keys=True, default=str).encode("utf-8")
    return content_version(sid, encoded)


def default_effective_binding(source_type: str, definition: dict, cfg: config.Config) -> dict[str, object]:
    """Project effective connector/service/endpoint/account identity without secret material."""
    connector = str(definition.get("connector") or "").strip() or None
    if source_type == "jira":
        endpoint = (cfg.jira.base_url or "").strip().rstrip("/").lower() or None
        account = (cfg.jira.email or "").strip().lower() or None
        return {"connector": connector, "driver": "jira", "endpoint": endpoint, "account": account}
    if source_type == "github":
        return {"connector": connector, "driver": "github", "endpoint": "https://github.com", "account": None}
    if source_type == "closed-specs":
        tracker_name = str(definition.get("tracker") or "").strip()
        if tracker_name == "jira":
            return {
                "connector": connector,
                "driver": "jira",
                "endpoint": (cfg.jira.base_url or "").strip().rstrip("/").lower() or None,
                "account": (cfg.jira.email or "").strip().lower() or None,
            }
        return {
            "connector": connector,
            "driver": tracker_name or "github",
            "endpoint": "https://github.com" if tracker_name == "github" else None,
            "account": None,
        }
    return {"connector": connector, "driver": source_type, "endpoint": None, "account": None}


def effective_binding(source_type: str, definition: dict, cfg: config.Config) -> dict[str, object]:
    if _BINDING_RESOLVER is not None:
        return dict(_BINDING_RESOLVER(source_type, definition, cfg))
    return default_effective_binding(source_type, definition, cfg)


def compute_scope_sha(source_type: str, definition: dict, cfg: config.Config, *, default_repo: str = "") -> str:
    kind = SOURCES.get(source_type)
    scope_vals = kind.resolved_scope(definition, cfg, default_repo=default_repo) if kind else {}
    payload = {
        "type": source_type,
        "scope": scope_vals,
        "binding": effective_binding(source_type, definition, cfg),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def arrived(e: dict) -> datetime | None:
    data = e.get("data")
    stamp = (data.get("at") or data.get("updated")) if isinstance(data, dict) else None
    if not stamp:
        stamp = e.get("at")
    if not stamp:
        return None
    try:
        moment = util.parse_iso(str(stamp))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def every(source: dict) -> float:
    return cron.seconds(source["every"]) if source.get("every") else DEFAULT_EVERY


def compute_backoff(every_s: float, error_streak: int, retry_after: int | float | None = None) -> int:
    exp = every_s * (2 ** max(0, error_streak - 1))
    ra = float(retry_after) if retry_after is not None and math.isfinite(float(retry_after)) else 0.0
    return min(MAX_BACKOFF_S, max(1, math.ceil(max(exp, ra))))


def event(type_: str, source: str, key, title: str, *, repo: str = "", url: str = "", trust: str = "untrusted",
          data: dict | None = None) -> dict:
    return {"type": type_, "source": source, "key": str(key), "repo": repo, "title": util.first_line(title, 300),
            "url": url, "trust": trust, "data": data or {}}


def fire(loop: Loop, how: str, events_list: list[dict] | None = None) -> list[int]:
    from .store import factory

    with foundation.tx() as c:
        factory.admit(c)
        if events_list is None:
            return [item] if (item := loop_store.enqueue(loop.name, loop.queue_name, "", how)) else []
        batches: dict[str, list[dict]] = {}
        for e in events_list:
            if not loop.trigger.refusal(e):
                loop_store.add_event(loop.name, e)
                batches.setdefault(loop.trigger.group_of(e), []).append(e)
        return [loop_store.enqueue(loop.name, loop.queue_name, group, how, batch)
                for group, batch in batches.items()]


def github_name(cfg: config.Config, name: str) -> str:
    if (path := paths.expand(name)).is_absolute():
        return next((r.github for r in cfg.repos if r.path.resolve() == path.resolve()), "") or github_remote(path)
    if "/" in name:
        return name
    return cfg.repo(name).github


def last_good_poll(known: dict) -> datetime | None:
    stamp = known.get("last_ok") or (known.get("polled") if known.get("ok") else None)
    if not stamp:
        return None
    moment = util.parse_iso(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def shown(keys: list[str]) -> str:
    return ", ".join(keys[:SHOWN_KEYS]) + (", ..." if len(keys) > SHOWN_KEYS else "")


def _extract_rate(rate_box: dict | None, exc: BaseException | None = None) -> dict[str, int | None] | None:
    rem = None
    ra = None
    if isinstance(rate_box, dict):
        raw_rate = rate_box.get("rate")
        if isinstance(raw_rate, dict):
            rem = raw_rate.get("remaining")
            ra = raw_rate.get("retry_after")
        elif raw_rate is not None:
            rem = getattr(raw_rate, "remaining", None)
            ra = getattr(raw_rate, "retry_after", None)
    if exc is not None:
        exc_ra = getattr(exc, "retry_after", None)
        if exc_ra is not None:
            ra = exc_ra
    if rem is None and ra is None:
        return None
    return {
        "remaining": int(rem) if rem is not None and not isinstance(rem, bool) else None,
        "retry_after": math.ceil(float(ra)) if ra is not None and not isinstance(ra, bool) else None,
    }


def _file_sources(config_dir: Path) -> dict[str, tuple[Path, bytes, dict]]:
    found: dict[str, tuple[Path, bytes, dict]] = {}
    for folder in (paths.DEFAULTS_DIR / "sources", config_dir / "sources"):
        if not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.toml")):
            if not file.is_file():
                continue
            sid = file.stem
            if not SOURCE_ID_RE.fullmatch(sid):
                continue
            try:
                raw = file.read_bytes()
                parsed = tomllib.loads(raw.decode("utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                continue
            if not isinstance(parsed, dict) or parsed.get("type") not in SOURCES:
                continue
            found[sid] = (file, raw, parsed)
    return found


def build_file_src(
    sid: str,
    defn: dict,
    raw: bytes,
    cfg: config.Config,
    *,
    config_dir: Path | None = None,
    loop_names: tuple[str, ...] = (),
    loop_objs: tuple[Loop, ...] = (),
) -> Src:
    root = config_dir or paths.CONFIG_DIR
    stype = str(defn["type"])
    default_repo = str(defn.get("repo") or "")
    return Src(
        id=sid,
        definition=dict(defn),
        origin="file",
        loops=loop_names,
        version=content_version(sid, raw),
        scope_sha=compute_scope_sha(stype, defn, cfg, default_repo=default_repo),
        dir=root,
        repo=default_repo,
        enabled=bool(defn.get("enabled", True)),
        feeds=str(defn.get("feeds") or ""),
        line=str(defn["line"]) if defn.get("line") else None,
        why=str(defn["why"]) if defn.get("why") is not None else None,
        loop_objects=loop_objs,
    )


def load_all(
    cfg: config.Config,
    loop_list: list[Loop] | None = None,
    *,
    config_dir: Path | None = None,
    include_derived: bool = True,
) -> list[Src]:
    from . import loops as loops_mod
    from . import poll as poll_mod
    from . import tracker

    root = config_dir or paths.CONFIG_DIR
    if loop_list is None:
        loop_list = loops_mod.load_all(cfg)

    file_defs = _file_sources(root)
    file_loop_names: dict[str, list[str]] = {sid: [] for sid in file_defs}
    file_loop_objs: dict[str, list[Loop]] = {sid: [] for sid in file_defs}
    inline_entries: dict[str, Src] = {}

    for loop in loop_list:
        if loop.error:
            continue
        for table in loop.sources:
            sid = table_source_id(loop.name, table)
            if not SOURCE_ID_RE.fullmatch(sid):
                continue
            if is_file_ref(table):
                if sid in file_defs and loop.name not in file_loop_names[sid]:
                    file_loop_names[sid].append(loop.name)
                    file_loop_objs[sid].append(loop)
                continue
            if sid in file_defs or sid in inline_entries:
                continue
            stype = table.get("type")
            if stype not in SOURCES:
                continue
            defn = dict(table)
            default_repo = str(defn.get("repo") or loop.repo)
            inline_entries[sid] = Src(
                id=sid,
                definition=defn,
                origin=f"loop:{loop.name}",
                loops=(loop.name,),
                version=inline_version(sid, defn),
                scope_sha=compute_scope_sha(str(stype), defn, cfg, default_repo=default_repo),
                dir=loop.dir,
                repo=default_repo,
                enabled=bool(defn.get("enabled", True)) and loop.enabled,
                feeds=str(defn.get("feeds") or ""),
                line=str(defn["line"]) if defn.get("line") else None,
                why=str(defn["why"]) if defn.get("why") is not None else None,
                loop_objects=(loop,),
            )

    by_id: dict[str, Src] = {}
    for sid, (_, raw, defn) in file_defs.items():
        by_id[sid] = build_file_src(
            sid,
            defn,
            raw,
            cfg,
            config_dir=root,
            loop_names=tuple(file_loop_names[sid]),
            loop_objs=tuple(file_loop_objs[sid]),
        )
    by_id.update(inline_entries)

    if include_derived:
        for project in sorted(poll_mod.jira_routes_by_project(cfg)):
            slug = re.sub(r"[^a-z0-9]", "-", project.lower()).strip("-")
            sid = f"jira-{slug}"
            if not SOURCE_ID_RE.fullmatch(sid) or sid in by_id:
                continue
            defn = {
                "type": "jira",
                "jql": tracker.candidates_jql(project),
                "feeds": "pipeline",
                "project": project,
            }
            by_id[sid] = Src(
                id=sid,
                definition=defn,
                origin="derived",
                loops=(),
                version=inline_version(sid, defn),
                scope_sha=compute_scope_sha("jira", defn, cfg),
                dir=root,
                repo="",
                enabled=True,
                feeds="pipeline",
                line=None,
                why=None,
                loop_objects=(),
            )

    return [by_id[k] for k in sorted(by_id)]


def _legacy_fingerprints(table: dict, resolved_defn: dict, source_type: str) -> set[str]:
    """Return legacy #258 scope: hashes and pre-#258 all-settings hashes for one-time backfill matching."""
    out: set[str] = set()
    kind = SOURCES.get(source_type)
    if kind is not None:
        for candidate in (table, resolved_defn):
            scope = {"type": source_type} | {k: candidate.get(k) for k in kind.scope}
            out.add("scope:" + hashlib.sha256(json.dumps(scope, sort_keys=True, default=str).encode()).hexdigest())
    for candidate in (table, resolved_defn):
        for drop in (("every",), ("every", "name"), ("every", "name", "id"), ("every", "id")):
            settings = {k: v for k, v in candidate.items() if k not in drop}
            out.add(hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest())
    return out


def _has_legacy_cursors(c) -> bool:
    return (
        c.execute(
            "SELECT 1 FROM cursors WHERE source LIKE '%#%' AND source NOT LIKE 'oncall:%' LIMIT 1"
        ).fetchone()
        is not None
    )


def _backfill_on_conn(c, cfg: config.Config, loop_list: list[Loop]) -> int:
    if not source_store.has_durable_sources(c) or not _has_legacy_cursors(c):
        return 0
    existing_cursors = {
        r["source"]: dict(r)
        for r in c.execute("SELECT * FROM cursors").fetchall()
    }
    owners = {
        r["source"]: str(r["fingerprint"])
        for r in c.execute("SELECT source, fingerprint FROM source_owners").fetchall()
    }
    loaded_by_id = {s.id: s for s in load_all(cfg, loop_list, include_derived=False)}
    moved = 0

    for loop in loop_list:
        if loop.error:
            continue
        prefix = f"{loop.name}#"
        slot_keys = sorted(
            (k for k in existing_cursors if k.startswith(prefix) and k[len(prefix):].isdigit()),
            key=lambda k: int(k[len(prefix):]),
        )
        for index, table in enumerate(loop.sources):
            sid = table_source_id(loop.name, table)
            src = loaded_by_id.get(sid)
            if src is None or sid in existing_cursors:
                continue
            own_slot = f"{loop.name}#{index}"
            candidates: list[str] = []
            if own_slot in existing_cursors:
                candidates.append(own_slot)
            for key in slot_keys:
                if key not in candidates and key in existing_cursors:
                    candidates.append(key)
            if (old_name := table.get("name")) and (named_key := f"{loop.name}#{old_name}") in existing_cursors:
                if named_key not in candidates:
                    candidates.append(named_key)

            expected = _legacy_fingerprints(table, src.definition, str(src.definition["type"]))
            matched_old: str | None = None
            for old_key in candidates:
                if old_key.startswith("oncall:"):
                    continue
                owner = owners.get(old_key)
                if owner is not None and owner in expected:
                    matched_old = old_key
                    break
            if matched_old is None:
                continue
            for tbl in ("cursors", "seen", "source_owners"):
                c.execute(f"UPDATE OR REPLACE {tbl} SET source = ? WHERE source = ?", (sid, matched_old))  # noqa: S608
            row = c.execute("SELECT polled, ok, last_ok FROM cursors WHERE source = ?", (sid,)).fetchone()
            last_ok = row["last_ok"] or (row["polled"] if row["ok"] else None)
            c.execute(
                "UPDATE cursors SET scope_sha = ?, source_version = ?, last_ok = ? WHERE source = ?",
                (src.scope_sha, src.version, last_ok, sid),
            )
            existing_cursors[sid] = existing_cursors.pop(matched_old)
            if matched_old in owners:
                owners[sid] = owners.pop(matched_old)
            moved += 1
    return moved


def backfill_conn(c, cfg: config.Config | None = None, loop_list: list[Loop] | None = None) -> int:
    from . import loops as loops_mod

    if not source_store.has_durable_sources(c) or not _has_legacy_cursors(c):
        return 0
    if cfg is None:
        cfg, problem = config.load_or_problem()
        if problem:
            return 0
    if loop_list is None:
        loop_list = loops_mod.load_all(cfg, check=False)
    return _backfill_on_conn(c, cfg, loop_list)


def backfill(cfg: config.Config | None = None, loop_list: list[Loop] | None = None) -> int:
    with foundation.tx() as c:
        return backfill_conn(c, cfg, loop_list)


def _source_limit(src: Src) -> int:
    defn = src.definition
    if defn.get("limit") is not None:
        try:
            return max(1, int(defn["limit"]))
        except (TypeError, ValueError):
            pass
    if defn.get("type") == "github":
        return 30
    return 50


def poll(
    src: Src,
    cfg: config.Config,
    say: Callable[[str], None] = lambda _: None,
    *,
    fire_loops: dict[str, Loop] | None = None,
) -> tuple[list[dict], list[int]]:
    from . import inbox
    from . import loops as loops_mod
    from .store import factory

    version = factory.admit()
    stype = str(src.definition["type"])
    kind = SOURCES[stype]
    started = util.iso()
    known = source_store.cursor(src.id)
    first = known is None or known.get("last_ok") is None
    changed = not first and known.get("scope_sha") != src.scope_sha
    cursor = None if first else kind.rescope(src.definition, known["value"]) if changed else known["value"]
    rate_box: dict[str, object] = {}
    try:
        found, value = kind.fetch(src, cfg, cursor, rate_box=rate_box)
    except Exception as e:
        if isinstance(e, factory.Off):
            raise
        err_text = f"{type(e).__name__}: {util.first_line(str(e), 300)}"
        rate = _extract_rate(rate_box, e)
        next_streak = (int(known["error_streak"]) if known and known.get("error_streak") is not None else 0) + 1
        backoff_s = compute_backoff(every(src.definition), next_streak, rate.get("retry_after") if rate else None)
        with foundation.tx() as c:
            factory.admit(c, version=version)
            row = source_store.source_poll(c, src, started, False, 0, 0, err_text, rate, backoff_s)
            events.emit(
                "source.failed",
                f"source:{src.id}",
                cls="notice",
                ver=row["version"],
                data={
                    "source": src.id,
                    "error": err_text,
                    "error_streak": int(row["error_streak"] or 0),
                    "backoff_s": backoff_s,
                    "backoff_until": row["backoff_until"],
                },
                connection=c,
            )
        raise

    rate = _extract_rate(rate_box)
    unique = {e["key"]: e for e in found}
    with foundation.tx() as c:
        factory.admit(c, version=version)
        claimed = set(source_store.claim_seen(src.id, list(unique), connect=lambda: nullcontext(c)))
        fresh = [e for k, e in unique.items() if k in claimed]
        if first:
            forwarded, older = kind.baseline(fresh)
        elif changed:
            forwarded, older = kind.catch_up(fresh, last_good_poll(known))
        else:
            forwarded, older = fresh, []
        prev_value = known.get("value") if known else None
        advanced = bool(fresh) or (value != prev_value)
        detail = f"{len(found)} read, {len(fresh)} new"
        if first:
            detail += " (baseline)"
        elif changed:
            detail += f", scope changed, {len(older)} older skipped"
        row = source_store.source_poll(
            c,
            src,
            started,
            True,
            len(found),
            len(fresh),
            None,
            rate,
            None,
            value=value,
            advanced=advanced,
            detail=detail,
        )
        events.emit(
            "source.polled",
            f"source:{src.id}",
            cls="notice",
            ver=row["version"],
            data={
                "source": src.id,
                "read": len(found),
                "new": len(fresh),
                "forwarded": len(forwarded),
                "mode": "baseline" if first else "rescope" if changed else "poll",
            },
            connection=c,
        )

    rescope_item: dict | None = None
    if changed and older:
        held = older[:_source_limit(src)]
        with foundation.tx() as c:
            rescope_item = inbox.raise_rescope(c, src, held)

    items: list[int] = []
    if forwarded and src.loops:
        if fire_loops is not None:
            targets = fire_loops
        elif src.loop_objects:
            targets = {loop.name: loop for loop in src.loop_objects if loop.enabled and not loop.error}
        else:
            targets = {loop.name: loop for loop in loops_mod.load_all(cfg) if loop.enabled and not loop.error}
        for loop_name in src.loops:
            if (loop_obj := targets.get(loop_name)) is not None:
                items.extend(fire(loop_obj, "event", forwarded))

    if first:
        say(
            f"{src.id}: first read, {len(older)} existing events taken as the baseline"
            + (f": {shown([e['key'] for e in older])}" if older else "")
        )
    elif changed:
        tr_note = f" ({rescope_item['id']})" if rescope_item else ""
        say(
            f"{src.id}: scope changed; {len(forwarded)} events since the last good poll go on, "
            f"{len(older)} older are skipped{tr_note}"
            + (f": {shown([e['key'] for e in older])}" if older else "")
        )
    return forwarded, items


def dry_run(src: Src, cfg: config.Config) -> dict:
    from . import poll as poll_mod

    stype = str(src.definition["type"])
    kind = SOURCES[stype]
    if stype == "jira" and src.feeds == "pipeline":
        project = str(src.definition.get("project") or "")
        routes = poll_mod.jira_routes_by_project(cfg)
        labels = routes.get(project, set())
        try:
            check_result = kind.check(cfg, project, labels)
        except jira.JiraError as e:
            return {
                "source": src.id,
                "dry_run": True,
                "ok": False,
                "error": str(e),
                "items": [],
                "would_rescope": False,
                "check": None,
            }
        return {
            "source": src.id,
            "dry_run": True,
            "ok": True,
            "error": None,
            "items": [],
            "would_rescope": False,
            "check": check_result,
        }
    known = source_store.cursor(src.id)
    seen_keys = source_store.source_seen(src.id)
    first = known is None or known.get("last_ok") is None
    would_rescope = not first and known.get("scope_sha") != src.scope_sha
    cursor = None if first else kind.rescope(src.definition, known["value"]) if would_rescope else known["value"]
    try:
        found, _ = kind.fetch(src, cfg, cursor)
    except Exception as e:
        return {
            "source": src.id,
            "dry_run": True,
            "ok": False,
            "error": f"{type(e).__name__}: {util.first_line(str(e), 300)}",
            "items": [],
            "would_rescope": would_rescope,
            "check": None,
        }
    items = [{**e, "seen": e["key"] in seen_keys} for e in found]
    return {
        "source": src.id,
        "dry_run": True,
        "ok": True,
        "error": None,
        "items": items,
        "would_rescope": would_rescope,
        "check": None,
    }


def peek(src: Src, cfg: config.Config) -> list[dict]:
    kind = SOURCES[str(src.definition["type"])]
    found, _ = kind.fetch(src, cfg, "{}", sample=isinstance(kind, Command))
    return found


def source_view(
    src: Src | None,
    sid: str,
    cur: dict | None,
    c,
    *,
    poll_limit: int = 20,
    since_24h: str | None = None,
) -> dict:
    cutoff = since_24h or util.iso(util.now() - timedelta(hours=24))
    polls = source_store.recent_polls(c, sid, limit=poll_limit)
    rate_limit = source_store.latest_rate_limit(c, sid)
    counts = source_store.poll_counts_since(c, sid, cutoff)
    ok_now = cur is None or bool(cur.get("ok"))
    note = "ok" if ok_now else str(cur.get("detail") or "failed")
    defn = src.definition if src is not None else {}
    return {
        "id": sid,
        "subject": f"source:{sid}",
        "version": int(cur["version"]) if cur and cur.get("version") is not None else 0,
        "source_version": src.version if src is not None else (cur.get("source_version") if cur else None),
        "kind": str(defn["type"]) if src is not None else None,
        "line": src.line if src is not None else None,
        "feeds": (src.feeds or None) if src is not None else None,
        "enabled": bool(src.enabled) if src is not None else False,
        "loops": list(src.loops) if src is not None else [],
        "last_ok": cur.get("last_ok") if cur else None,
        "polled": cur.get("polled") if cur else None,
        "advanced": cur.get("advanced") if cur else None,
        "error_streak": int(cur.get("error_streak") or 0) if cur else 0,
        "backoff_until": cur.get("backoff_until") if cur else None,
        "rate_limit": rate_limit,
        "polls": polls,
        "note": note,
        "every": defn.get("every") if src is not None else None,
        "counts": counts,
        "why": src.why if src is not None else None,
    }


def views(
    cfg: config.Config | None = None,
    loop_list: list[Loop] | None = None,
    *,
    connect=foundation.reader,
    poll_limit: int = 20,
) -> list[dict]:
    cfg = cfg or config.load_or_problem()[0]
    loaded = {s.id: s for s in load_all(cfg, loop_list)}
    since_24h = util.iso(util.now() - timedelta(hours=24))
    with connect() as c:
        cursor_rows = {
            str(r["source"]): dict(r)
            for r in c.execute("SELECT * FROM cursors ORDER BY source").fetchall()
        }
        all_ids = sorted(
            set(loaded) | {sid for sid in cursor_rows if not sid.startswith("oncall:")}
        )
        return [
            source_view(loaded.get(sid), sid, cursor_rows.get(sid), c, poll_limit=poll_limit, since_24h=since_24h)
            for sid in all_ids
        ]


def view_for(
    sid: str,
    cfg: config.Config | None = None,
    loop_list: list[Loop] | None = None,
    *,
    connect=foundation.reader,
    poll_limit: int = 50,
) -> dict | None:
    if sid.startswith("oncall:"):
        return None
    cfg = cfg or config.load_or_problem()[0]
    loaded = {s.id: s for s in load_all(cfg, loop_list)}
    since_24h = util.iso(util.now() - timedelta(hours=24))
    with connect() as c:
        row = c.execute("SELECT * FROM cursors WHERE source = ?", (sid,)).fetchone()
        cur = dict(row) if row else None
        src = loaded.get(sid)
        if src is None and cur is None:
            return None
        return source_view(src, sid, cur, c, poll_limit=poll_limit, since_24h=since_24h)


def views_for_loop(
    cfg: config.Config,
    loop: Loop,
    *,
    connect=foundation.reader,
    poll_limit: int = 20,
) -> list[dict]:
    loaded = {s.id: s for s in load_all(cfg, [loop])}
    wanted: list[str] = []
    for table in loop.sources:
        sid = table_source_id(loop.name, table)
        if sid not in wanted and sid in loaded:
            wanted.append(sid)
    since_24h = util.iso(util.now() - timedelta(hours=24))
    with connect() as c:
        out = []
        for sid in wanted:
            row = c.execute("SELECT * FROM cursors WHERE source = ?", (sid,)).fetchone()
            out.append(source_view(loaded[sid], sid, dict(row) if row else None, c, poll_limit=poll_limit, since_24h=since_24h))
        return out


def repo_problems(cfg: config.Config, names: list, what: str) -> list[str]:
    problems = []
    for name in names:
        try:
            if not github_name(cfg, str(name)):
                problems.append(f"{what} repo '{name}' has no github in the config or GitHub remote")
        except config.UnknownRepo:
            problems.append(f"{what} repo '{name}' is not a repo in the config or owner/name")
    return problems


class Source:
    needs = ""
    scope: tuple[str, ...] = ()

    def resolved_scope(self, source: dict, cfg: config.Config, *, default_repo: str = "") -> dict[str, object]:
        return {k: source.get(k) for k in self.scope}

    def rescope(self, source: dict, cursor):
        return cursor

    def baseline(self, fresh: list[dict]) -> tuple[list[dict], list[dict]]:
        return [], fresh

    def catch_up(self, fresh: list[dict], since: datetime | None, untimed: bool = False) -> tuple[list[dict], list[dict]]:
        cutoff = since - CATCH_UP_SLACK if since else None
        forwarded = [e for e in fresh if ((at := arrived(e)) is None and untimed) or (cutoff and at and at >= cutoff)]
        return forwarded, [e for e in fresh if e not in forwarded]

    def problems(self, source: dict, loop: Loop | None, cfg: config.Config) -> list[str]:
        problems = [f"a {source['type']} source needs {self.needs}"] if self.needs and blank(source.get(self.needs)) \
            else []
        try:
            every(source)
        except cron.BadSchedule as e:
            problems.append(str(e))
        return problems

    def fetch(self, src: Src, cfg: config.Config, cursor, *, sample: bool = False,
              rate_box: dict | None = None) -> tuple[list[dict], object]:
        raise NotImplementedError


class GitHub(Source):
    scope = ("repos", "watch", "label", "branch")

    def repos(self, source: dict, default_repo: str = "") -> list[str]:
        return as_list(source.get("repos")) or ([default_repo] if default_repo else [])

    def watches(self, source: dict) -> list[str]:
        return as_list(source.get("watch")) or ["ci_red"]

    def resolved_scope(self, source: dict, cfg: config.Config, *, default_repo: str = "") -> dict[str, object]:
        resolved_repos: list[str] = []
        for name in self.repos(source, default_repo):
            try:
                resolved_repos.append(github_name(cfg, name) or name)
            except config.UnknownRepo:
                resolved_repos.append(name)
        return {
            "repos": resolved_repos,
            "watch": self.watches(source),
            "label": source.get("label"),
            "branch": source.get("branch"),
        }

    def problems(self, source: dict, loop: Loop | None, cfg: config.Config) -> list[str]:
        problems = super().problems(source, loop, cfg)
        problems += [f"github watch '{w}' is not one of {', '.join(GITHUB_WATCH)}" for w in self.watches(source)
                     if w not in GITHUB_WATCH]
        default_repo = str(source.get("repo") or (loop.repo if loop else ""))
        names = self.repos(source, default_repo)
        if not names:
            problems.append("a github source needs repos or [run] repo")
        return problems + repo_problems(cfg, names, "github source")

    def fetch(self, src: Src, cfg: config.Config, cursor, *, sample: bool = False,
              rate_box: dict | None = None) -> tuple[list[dict], object]:
        source = src.definition
        sid = src.id
        watch = self.watches(source)
        me = gh.me() if "issue" in watch else ""
        out: list[dict] = []
        for name in self.repos(source, src.repo):
            repo = github_name(cfg, name)
            if "ci_red" in watch:
                branch = source.get("branch") or gh.default_branch(repo)
                runs = gh.gh_json(["run", "list", "-R", repo, "--branch", branch, "--status", "failure",
                                   "--limit", "10",
                                   "--json", "databaseId,displayTitle,workflowName,headSha,url,createdAt,event"]) or []
                out += [event("github.ci_red", sid, r["databaseId"], f"CI red on {repo}@{branch}: {r['workflowName']}",
                              repo=repo, url=r["url"], trust="internal",
                              data={"workflow": r["workflowName"], "sha": r["headSha"], "branch": branch,
                                    "commit_title": r["displayTitle"], "at": r["createdAt"], "run_id": r["databaseId"]})
                        for r in runs]
            if "issue" in watch:
                args = ["issue", "list", "-R", repo, "--state", "open", "--limit", "30",
                        "--json", "number,title,author,url,labels,body,createdAt,updatedAt"]
                if source.get("label"):
                    args += ["--label", source["label"]]
                for i in gh.gh_json(args) or []:
                    author = (i.get("author") or {}).get("login", "")
                    out.append(event("github.issue", sid, f"{repo}#{i['number']}", i["title"], repo=repo, url=i["url"],
                                     trust="owner" if author == me else "untrusted",
                                     data={"number": i["number"], "author": author, "updated": i.get("updatedAt"),
                                           "body": (i.get("body") or "")[:6000],
                                           "labels": [label["name"] for label in i.get("labels", [])]}))
            if "advisory" in watch:
                for a in gh.open_dependabot_alerts(repo):
                    adv, dep = a.get("security_advisory") or {}, a.get("dependency") or {}
                    pkg = (dep.get("package") or {}).get("name", "")
                    out.append(event("github.advisory", sid, f"advisory:{repo}#{a['number']}",
                                     f"{pkg}: {adv.get('summary', '')}", repo=repo, url=a.get("html_url", ""),
                                     trust="untrusted",
                                     data={"package": pkg, "severity": adv.get("severity"), "ghsa": adv.get("ghsa_id"),
                                           "manifest": dep.get("manifest_path"),
                                           "fixed_in": ((a.get("security_vulnerability") or {})
                                                        .get("first_patched_version") or {}).get("identifier"),
                                           "at": a.get("created_at")}))
        return out, None


class Jira(Source):
    needs = "jql"
    scope = ("jql", "on_update")

    def resolved_scope(self, source: dict, cfg: config.Config, *, default_repo: str = "") -> dict[str, object]:
        return {"jql": source.get("jql"), "on_update": bool(source.get("on_update", False))}

    def fetch(self, src: Src, cfg: config.Config, cursor, *, sample: bool = False,
              rate_box: dict | None = None) -> tuple[list[dict], object]:
        source = src.definition
        sid = src.id
        client = jira.Client.from_env(cfg.jira)
        fields = ["summary", "status", "reporter", "updated", "description", "labels"]
        out = []
        try:
            for issue in client.search(source["jql"], fields, limit=int(source.get("limit", 50))):
                f = issue.get("fields") or {}
                key = issue["key"] + (f"@{f.get('updated')}" if source.get("on_update") else "")
                out.append(event("jira.issue", sid, key, f"{issue['key']}: {f.get('summary', '')}",
                                 repo=str(source.get("repo") or src.repo), url=client.browse_url(issue["key"]),
                                 trust="untrusted",
                                 data={"key": issue["key"], "status": (f.get("status") or {}).get("name"),
                                       "reporter": (f.get("reporter") or {}).get("displayName"),
                                       "updated": f.get("updated"), "labels": f.get("labels"),
                                       "description": json.dumps(f.get("description"))[:6000]}))
        finally:
            if rate_box is not None:
                rate_box["rate"] = getattr(client, "rate", None)
        return out, None

    def check(self, cfg: config.Config, project: str, labels: set[str] | list[str] | None = None) -> dict:
        from . import tracker

        client = jira.Client.from_env(cfg.jira)
        me = client.me()
        jql = tracker.candidates_jql(project)
        waiting = len(client.search(jql, ["labels"], limit=100))
        known = {s.lower() for s in client.statuses(project)}
        targets = [(f"[jira.transitions] {k}", k, v) for k, v in cfg.jira.transitions.items()]
        targets += [(f"[jira.research] {k}", f"research {k}", v) for k, v in cfg.jira.research.items()]
        targets += [(f"[jira.landed] {k}", f"landed on {k}", v) for k, v in cfg.jira.landed.items()]
        missing = [
            {"key": key, "when": when, "status": name}
            for key, when, name in targets
            if name.lower() not in known
        ]
        moves = [{"key": key, "when": when, "status": name} for key, when, name in targets]
        return {
            "base": client.base,
            "login": me.get("displayName") or me.get("emailAddress") or "?",
            "project": project,
            "waiting": waiting,
            "routes": sorted(labels or ()),
            "missing_statuses": missing,
            "moves": moves,
        }


class Command(Source):
    needs = "command"
    scope = ("command",)

    def fetch(self, src: Src, cfg: config.Config, cursor, *, sample: bool = False,
              rate_box: dict | None = None) -> tuple[list[dict], object]:
        source = src.definition
        sid = src.id
        argv = shlex.split(source["command"]) if isinstance(source["command"], str) else list(source["command"])
        loop_name = src.loops[0] if src.loops else ""
        res = util.sh(argv, cwd=src.dir, timeout=float(source.get("timeout", 120)),
                      env={"ARETE_LOOP": loop_name, "ARETE_LOOP_DIR": str(src.dir), "ARETE_SOURCE": sid,
                           "ARETE_SOURCE_SAMPLE": "1" if sample else ""})
        if res.returncode != 0:
            raise RuntimeError(f"exit {res.returncode}: {util.first_line(res.stderr or res.stdout, 300)}")
        out = []
        for line in res.stdout.splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get("key") or row.get("title") or hashlib.sha1(line.strip().encode(), usedforsecurity=False).hexdigest()[:16]
            data = dict(row.get("data") or {})
            if row.get("at") and "at" not in data:
                data["at"] = row["at"]
            out.append(event(row.get("type", "command"), sid, key,
                             str(row.get("title") or ""), repo=str(row.get("repo") or src.repo), url=row.get("url", ""),
                             trust=source.get("trust", "internal"), data=data))
        return out, None


class Log(Source):
    needs = "path"
    scope = ("path", "pattern")

    def rescope(self, source: dict, cursor):
        if not cursor:
            return cursor
        offsets = json.loads(cursor)
        for name in log_files(source):
            if name not in offsets:
                try:
                    info = os.stat(name)
                except OSError:
                    continue
                offsets[name] = [info.st_size, info.st_ino]
        return json.dumps(offsets)

    def catch_up(self, fresh: list[dict], since: datetime | None, untimed: bool = False) -> tuple[list[dict], list[dict]]:
        return fresh, []

    def problems(self, source: dict, loop: Loop | None, cfg: config.Config) -> list[str]:
        problems = super().problems(source, loop, cfg)
        try:
            re.compile(source.get("pattern") or LOG_PATTERN)
        except re.error as e:
            problems.append(f"log source pattern '{source['pattern']}' is not a regex: {e}")
        return problems

    def fetch(self, src: Src, cfg: config.Config, cursor, *, sample: bool = False,
              rate_box: dict | None = None) -> tuple[list[dict], object]:
        source = src.definition
        sid = src.id
        offsets: dict[str, list[int] | int] = json.loads(cursor) if cursor else {}
        pattern = re.compile(source.get("pattern") or LOG_PATTERN)
        files = log_files(source)
        out: dict[str, dict] = {}
        for name in files:
            try:
                info = os.stat(name)
            except OSError:
                continue
            size, inode = info.st_size, info.st_ino
            start = log_start(offsets.get(name), size, inode)
            if cursor is None or start is None:
                offsets[name] = [size, inode]
                if cursor is None:
                    continue
                start = 0
            if (read := read_lines(name, max(start, size - MAX_LOG_READ), size)) is None:
                continue
            found, end = read
            offsets[name] = [start if end is None else end, inode]
            for raw in found:
                if not pattern.search(raw):
                    continue
                sig = signature(raw)
                key = hashlib.sha1(sig.encode(), usedforsecurity=False).hexdigest()[:16]
                if key in out:
                    out[key]["data"]["count"] += 1
                    continue
                out[key] = event("log.error", sid, key, sig, trust="untrusted",
                                 data={"file": name, "sample": raw.strip()[:1500], "signature": sig, "count": 1})
        return list(out.values()), json.dumps({k: v for k, v in offsets.items() if k in files})


def log_files(source: dict) -> list[str]:
    return sorted(glob.glob(str(paths.expand(source["path"]))))


def log_start(mark, size: int, inode: int) -> int | None:
    start, known = mark if isinstance(mark, list) else (mark, inode)
    if start is None:
        return None
    return 0 if size < start or known != inode else start


def read_lines(name: str, start: int, end: int) -> tuple[list[str], int | None] | None:
    try:
        with open(name, "rb") as f:
            f.seek(start)
            chunk = f.read(max(0, end - start))
    except OSError:
        return None
    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        return [], None
    return chunk[:last_newline].decode("utf-8", "replace").splitlines(), start + last_newline + 1


def signature(line: str) -> str:
    text = line.strip()
    for pattern, mark in SIGNATURE_NOISE:
        text = pattern.sub(mark, text)
    return text[:300]


class ClosedSpecs(Source):
    scope = ("tracker",)

    def baseline(self, fresh):
        return ([e for e in fresh if e["data"].get("delivery_root")],
                [e for e in fresh if not e["data"].get("delivery_root")])

    def problems(self, source, loop, cfg):
        problems = super().problems(source, loop, cfg)
        if source.get("tracker") not in ("github", "jira"):
            problems.append("a closed-specs source needs tracker = github or jira")
        return problems

    def fetch(self, src: Src, cfg: config.Config, cursor, *, sample: bool = False,
              rate_box: dict | None = None):
        from . import spec_retro

        return spec_retro.closed_specs(cfg, src.definition["tracker"], src.id), None


class Pipeline(Source):
    scope = ()

    def fetch(self, src: Src, cfg: config.Config, cursor, *, sample: bool = False,
              rate_box: dict | None = None):
        raise NotImplementedError("pipeline sources do not fetch directly")


SOURCES: dict[str, Source] = {
    "github": GitHub(),
    "jira": Jira(),
    "log": Log(),
    "command": Command(),
    "closed-specs": ClosedSpecs(),
    "pipeline": Pipeline(),
}
