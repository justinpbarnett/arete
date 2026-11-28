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

from . import config, configrepo, cron, events, factory_control, gh, jira, paths, util
from .loops import Loop, as_list, blank, github_remote
from .store import db as foundation
from .store import intake as intake_store
from .store import loops as loop_store
from .store import sources as source_store


class PollCrashed(RuntimeError):
    """Raised when both the poll transaction and its error-recording path fail."""


class _PollStale(Exception):
    """Internal rollback signal when cursor version or source binding changed during fetch."""

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
            tables_to_move = ["cursors", "source_owners"]
            if intake_store.available(c):
                tables_to_move.append("intake_items")
            for tbl in tables_to_move:
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


def canonical_external_key(origin_id: str, kind: str, driver_key: str) -> str:
    """Collision-safe encoding of (canonical upstream origin, event kind, driver event key)."""
    return json.dumps([origin_id, kind, driver_key], separators=(",", ":"), ensure_ascii=False)


def _command_producer(src: Src | None, sid: str) -> str:
    if src is not None:
        raw_cmd = src.definition.get("command")
        if isinstance(raw_cmd, str) and raw_cmd.strip():
            try:
                argv = shlex.split(raw_cmd)
            except ValueError:
                argv = raw_cmd.split()
            if argv:
                return argv[0]
        elif isinstance(raw_cmd, list) and raw_cmd:
            return str(raw_cmd[0])
    return sid or "command"


def _github_repo_from_key(raw_key: str) -> str:
    text = raw_key.removeprefix("advisory:")
    if "#" in text:
        head = text.split("#", 1)[0].strip()
        if "/" in head:
            return head
    return ""


def intake_identity_for_event(
    src: Src | None,
    cfg: config.Config | None,
    ev: dict,
    *,
    source_id: str = "",
) -> tuple[str, str, str, str]:
    """Return (origin_id, external_key, external_rev, kind) for a fetched event."""
    sid = src.id if src is not None else source_id
    kind = str(ev.get("type") or "command")
    raw_key = str(ev.get("key") or "")
    data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
    stype = str(src.definition.get("type") or "") if src is not None else ""

    if sid.endswith("#webhook") or kind == "webhook":
        origin_id = f"webhook:{sid}"
        return origin_id, raw_key, str(ev.get("rev") or ""), kind

    if kind.startswith("github.") or stype == "github" or (stype == "closed-specs" and src and src.definition.get("tracker") == "github"):
        repo = str(ev.get("repo") or _github_repo_from_key(raw_key)).strip().lower()
        origin_id = f"github:https://github.com:{repo}"
        ext_rev = str(ev.get("rev") or "")
        return origin_id, canonical_external_key(origin_id, kind, raw_key), ext_rev, kind

    if kind.startswith("jira.") or stype == "jira" or (stype == "closed-specs" and src and src.definition.get("tracker") == "jira"):
        endpoint = ((cfg.jira.base_url or "").strip().rstrip("/").lower()) if cfg is not None else ""
        origin_id = f"jira:{endpoint}"
        if "@" in raw_key:
            base_key, ext_rev = raw_key.split("@", 1)
        else:
            base_key = str(data.get("key") or raw_key)
            ext_rev = str(ev.get("rev") or "")
        return origin_id, canonical_external_key(origin_id, kind, base_key), ext_rev, kind

    if kind.startswith("log.") or stype == "log":
        file_path = str(data.get("file") or "")
        origin_id = f"log:{file_path}"
        return origin_id, canonical_external_key(origin_id, kind, raw_key), str(ev.get("rev") or ""), kind

    explicit_origin = ev.get("origin") or data.get("origin")
    if isinstance(explicit_origin, str) and explicit_origin.strip():
        origin_id = explicit_origin.strip()
    else:
        origin_id = f"command:{_command_producer(src, sid)}"
    return origin_id, canonical_external_key(origin_id, kind, raw_key), str(ev.get("rev") or ""), kind


def legacy_seen_identity(
    src: Src | None,
    cfg: config.Config | None,
    sid: str,
    raw_key: str,
) -> tuple[str, str, str, str]:
    """Return (external_key, external_rev, kind, shape) when backfilling pre-expand seen rows."""
    if sid.endswith("#webhook"):
        return raw_key, "", "webhook", "legacy"

    if src is None:
        return f"legacy-unresolved:{raw_key}", "", "legacy", "legacy_unresolved"

    stype = str(src.definition.get("type") or "")
    if stype == "github":
        if raw_key.startswith("advisory:") and "#" in raw_key:
            repo = _github_repo_from_key(raw_key).lower()
            origin_id = f"github:https://github.com:{repo}"
            return canonical_external_key(origin_id, "github.advisory", raw_key), "", "github.advisory", "legacy"
        if "#" in raw_key and "/" in raw_key.split("#", 1)[0]:
            repo = _github_repo_from_key(raw_key).lower()
            origin_id = f"github:https://github.com:{repo}"
            return canonical_external_key(origin_id, "github.issue", raw_key), "", "github.issue", "legacy"
        if raw_key.isdigit() and cfg is not None:
            gh_kind = SOURCES["github"]
            repos = gh_kind.repos(src.definition, src.repo) if isinstance(gh_kind, GitHub) else []
            if len(repos) == 1:
                try:
                    resolved = (github_name(cfg, repos[0]) or repos[0]).strip().lower()
                except config.UnknownRepo:
                    resolved = repos[0].strip().lower()
                origin_id = f"github:https://github.com:{resolved}"
                return canonical_external_key(origin_id, "github.ci_red", raw_key), "", "github.ci_red", "legacy"
        return f"legacy-unresolved:{raw_key}", "", "legacy", "legacy_unresolved"

    if stype == "jira":
        endpoint = ((cfg.jira.base_url or "").strip().rstrip("/").lower()) if cfg is not None else ""
        if not endpoint:
            return f"legacy-unresolved:{raw_key}", "", "legacy", "legacy_unresolved"
        origin_id = f"jira:{endpoint}"
        if "@" in raw_key:
            base_key, ext_rev = raw_key.split("@", 1)
        else:
            base_key, ext_rev = raw_key, ""
        return canonical_external_key(origin_id, "jira.issue", base_key), ext_rev, "jira.issue", "legacy"

    if stype == "command":
        origin_id = f"command:{_command_producer(src, sid)}"
        return canonical_external_key(origin_id, "command", raw_key), "", "command", "legacy"

    if stype == "log":
        raw_path = str(src.definition.get("path") or "")
        if raw_path and not any(ch in raw_path for ch in "*?["):
            resolved_path = str(paths.expand(raw_path))
            origin_id = f"log:{resolved_path}"
            return canonical_external_key(origin_id, "log.error", raw_key), "", "log.error", "legacy"
        return f"legacy-unresolved:{raw_key}", "", "legacy", "legacy_unresolved"

    return f"legacy-unresolved:{raw_key}", "", "legacy", "legacy_unresolved"


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


def _revalidate_source_under_fence(src: Src) -> bool:
    """Revalidate fetched source scope/effective binding against authoritative config under the instance fence."""
    cfg_now, problem = config.load_or_problem()
    if problem:
        return True
    try:
        from . import loops as loops_mod

        live_loops = loops_mod.load_all(cfg_now, check=False)
        live_by_id = {s.id: s for s in load_all(cfg_now, live_loops, include_derived=False)}
    except Exception:
        return True
    live_src = live_by_id.get(src.id)
    if live_src is None:
        return src.origin != "file"
    return bool(live_src.enabled) and live_src.scope_sha == src.scope_sha


def _event_author(ev: dict) -> str:
    data = ev.get("data")
    if isinstance(data, dict):
        for key in ("author", "reporter"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()
    return ""


def _resolve_target_loops(src: Src, cfg: config.Config, fire_loops: dict[str, Loop] | None) -> list[Loop]:
    from . import loops as loops_mod

    if not src.loops:
        return []
    if fire_loops is not None:
        targets = fire_loops
    elif src.loop_objects:
        targets = {loop.name: loop for loop in src.loop_objects if loop.enabled and not loop.error}
    else:
        targets = {loop.name: loop for loop in loops_mod.load_all(cfg) if loop.enabled and not loop.error}
    return [targets[name] for name in src.loops if name in targets]


def poll(
    src: Src,
    cfg: config.Config,
    say: Callable[[str], None] = lambda _: None,
    *,
    fire_loops: dict[str, Loop] | None = None,
) -> tuple[list[dict], list[int]]:
    from . import inbox
    from .store import factory

    version = factory.admit()
    stype = str(src.definition["type"])
    kind = SOURCES[stype]
    started = util.iso()
    known = source_store.cursor(src.id)
    expected_version = int(known["version"]) if known and known.get("version") is not None else None
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
        try:
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
        except Exception as err2:
            raise PollCrashed(f"{err_text} (error path failed: {type(err2).__name__}: {err2})") from err2
        raise

    rate = _extract_rate(rate_box)
    prepared: list[tuple[dict, str, str, str, str, str | None, str | None, str, int]] = []
    seen_batch_identities: set[tuple[str, str]] = set()
    for ev in found:
        origin_id, ext_key, ext_rev, ev_kind = intake_identity_for_event(src, cfg, ev)
        ident = (ext_key, ext_rev)
        if ident in seen_batch_identities:
            continue
        seen_batch_identities.add(ident)
        raw_obj = {**ev, "origin": origin_id}
        raw_bytes = json.dumps(raw_obj, sort_keys=True, default=str).encode("utf-8")
        raw_text, raw_blob, raw_sha = intake_store.prepare_raw(raw_bytes)
        prepared.append((ev, origin_id, ext_key, ext_rev, ev_kind, raw_text, raw_blob, raw_sha, len(raw_bytes)))

    target_loops = _resolve_target_loops(src, cfg, fire_loops)
    forwarded: list[dict] = []
    older: list[dict] = []
    items: list[int] = []
    rescope_item: dict | None = None

    try:
        with configrepo.maybe_instance_fence(), foundation.tx() as c:
            factory.admit(c, version=version)
            if not _revalidate_source_under_fence(src):
                raise _PollStale("source scope or binding changed during fetch")
            cur_now = c.execute("SELECT * FROM cursors WHERE source = ?", (src.id,)).fetchone()
            if expected_version is None:
                if cur_now is not None:
                    raise _PollStale("cursor created concurrently during fetch")
            elif cur_now is None or int(cur_now["version"]) != expected_version:
                raise _PollStale("cursor version moved during fetch")

            poll_id = source_store.begin_poll_row(c, src.id, started)
            fresh_records: list[tuple[int, dict, str, str, str]] = []
            for ev, origin_id, ext_key, ext_rev, ev_kind, raw_text, raw_blob, raw_sha, raw_len in prepared:
                intake_store.record_raw_blob(c, raw_blob, raw_len)
                item_num, created = intake_store.insert_item(
                    c,
                    source=src.id,
                    source_version=src.version,
                    poll_id=poll_id,
                    external_key=ext_key,
                    external_rev=ext_rev,
                    kind=ev_kind,
                    title=str(ev.get("title") or ""),
                    author=_event_author(ev),
                    origin_url=str(ev.get("url") or ""),
                    trust=str(ev.get("trust") or "untrusted"),
                    raw=raw_text,
                    raw_blob=raw_blob,
                    raw_sha256=raw_sha,
                    status="new",
                    shape="event",
                    received=started,
                )
                if created and item_num is not None:
                    fresh_records.append((item_num, ev, origin_id, ext_key, ext_rev))

            fresh_events = [rec[1] for rec in fresh_records]
            if first:
                forwarded, older = kind.baseline(fresh_events)
            elif changed:
                forwarded, older = kind.catch_up(fresh_events, last_good_poll(known))
            else:
                forwarded, older = fresh_events, []

            older_ids = {id(ev) for ev in older}
            forwarded_records = [rec for rec in fresh_records if id(rec[1]) not in older_ids]
            older_records = [rec for rec in fresh_records if id(rec[1]) in older_ids]

            if first:
                for item_num, _, _, _, _ in older_records:
                    c.execute("UPDATE intake_items SET status = 'dismissed' WHERE id = ?", (item_num,))
            elif changed and older_records:
                limit = _source_limit(src)
                held_payloads: list[dict] = []
                for idx, (item_num, ev, origin_id, ext_key, ext_rev) in enumerate(older_records):
                    if idx < limit:
                        c.execute("UPDATE intake_items SET status = 'held' WHERE id = ?", (item_num,))
                        held_payloads.append({
                            **ev,
                            "intake_item": intake_store.format_id(item_num),
                            "origin": origin_id,
                            "external_key": ext_key,
                            "external_rev": ext_rev,
                            "source_version": src.version,
                        })
                    else:
                        c.execute("UPDATE intake_items SET status = 'dismissed' WHERE id = ?", (item_num,))
                if held_payloads:
                    rescope_item = inbox.raise_rescope(c, src, held_payloads)

            refused_count = 0
            if forwarded_records and target_loops:
                accepted_nums: set[int] = set()
                refusal_by_num: dict[int, str] = {}
                for loop_obj in target_loops:
                    batches: dict[str, list[dict]] = {}
                    for item_num, ev, _, _, _ in forwarded_records:
                        try:
                            reason = loop_obj.trigger.refusal(ev)
                            if reason:
                                if item_num not in accepted_nums:
                                    refusal_by_num.setdefault(item_num, reason)
                                continue
                            grp = loop_obj.trigger.group_of(ev)
                        except Exception as err:
                            if item_num not in accepted_nums:
                                refusal_by_num.setdefault(item_num, f"{type(err).__name__}: {err}")
                            continue
                        accepted_nums.add(item_num)
                        refusal_by_num.pop(item_num, None)
                        loop_store.add_event(loop_obj.name, ev, connect=lambda: nullcontext(c))
                        batches.setdefault(grp, []).append(ev)
                    for grp, batch in batches.items():
                        qid = loop_store.enqueue(loop_obj.name, loop_obj.queue_name, grp, "event", batch, c=c)
                        if qid:
                            items.append(qid)
                for item_num, reason in refusal_by_num.items():
                    c.execute(
                        "UPDATE intake_items SET status = 'refused', refuse_reason = ? WHERE id = ?",
                        (reason, item_num),
                    )
                    refused_count += 1

            prev_value = known.get("value") if known else None
            advanced = bool(fresh_records) or (value != prev_value)
            detail = f"{len(found)} read, {len(fresh_records)} new"
            if first:
                detail += " (baseline)"
            elif changed:
                detail += f", scope changed, {len(older)} older skipped"
            ended_ts = util.iso()
            row = source_store.upsert_cursor_ok(
                c,
                src,
                started,
                value=value,
                advanced=advanced,
                detail=detail,
                ended=ended_ts,
            )
            source_store.finish_poll_row(
                c,
                poll_id,
                ended=ended_ts,
                ok=True,
                read=len(found),
                new=len(fresh_records),
                refused=refused_count,
                rate=rate,
            )
            for item_num, _ev, _, ext_key, ext_rev in fresh_records:
                item_row = intake_store.get_item_conn(c, item_num)
                if item_row is not None:
                    events.emit(
                        "intake.created",
                        f"intake:{item_row['id']}",
                        cls="durable",
                        ver=item_row["version"],
                        data={
                            "item": item_row["id"],
                            "source": src.id,
                            "poll_id": poll_id,
                            "external_key": ext_key,
                            "external_rev": ext_rev,
                            "kind": item_row["kind"],
                            "status": item_row["status"],
                        },
                        connection=c,
                    )
            events.emit(
                "source.polled",
                f"source:{src.id}",
                cls="notice",
                ver=row["version"],
                data={
                    "source": src.id,
                    "read": len(found),
                    "new": len(fresh_records),
                    "refused": refused_count,
                    "forwarded": len(forwarded),
                    "mode": "baseline" if first else "rescope" if changed else "poll",
                },
                connection=c,
            )
    except _PollStale:
        return [], []
    except Exception as e:
        if isinstance(e, factory.Off):
            raise
        err_text = f"{type(e).__name__}: {util.first_line(str(e), 300)}"
        try:
            with foundation.tx() as c:
                source_store.record_poll_error(c, src.id, started, err_text, rate=rate)
        except Exception as err2:
            raise PollCrashed(f"{err_text} (error path failed: {type(err2).__name__}: {err2})") from err2
        raise

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
    known_identities = intake_store.item_identities(src.id)
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
    items = []
    for e in found:
        _, ext_key, ext_rev, _ = intake_identity_for_event(src, cfg, e)
        items.append({**e, "seen": (ext_key, ext_rev) in known_identities})
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
