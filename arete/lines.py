from __future__ import annotations

import json
import math
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path

from . import config, configrepo, events, paths, principals, refusals, settings, util, viewfilter
from .store import db as foundation
from .store import inbox as inbox_store
from .store import projects as projects_store
from .store import tasks as task_store
from .store import writes

PROJECT_WRITABLE = frozenset(("name", "repo", "priority", "rank", "archived"))
LINE_WRITABLE = frozenset((
    "project", "name", "kind", "repo", "adw_route", "device_pool", "rank",
    "concurrency", "wip_limit", "priority", "paused", "paused_why", "band",
))
VIEW_WRITABLE = frozenset(("name", "project", "filter", "group_by", "sort", "layout", "columns", "rank"))

LINE_KINDS = frozenset(("pipeline", "loops"))
VIEW_GROUP_BY = frozenset(("state", "line", "project", "priority", "kind", "owner"))
VIEW_SORT = frozenset(("rank", "priority", "updated", "created"))
VIEW_LAYOUT = frozenset(("board", "table"))

SORT_SQL: dict[str, str] = {
    "rank": "(t.rank IS NULL), t.rank ASC, t.id ASC",
    "priority": "(t.priority IS NULL), t.priority ASC, t.id ASC",
    "updated": "t.updated DESC, t.id DESC",
    "created": "t.created DESC, t.id DESC",
}

WIP_STATES = frozenset(("running", "needs-you", "review"))
TERMINAL_TASK_STATES = frozenset(("done", "closed"))
FINISHED_RUN_STATUSES = frozenset((
    "done", "pr", "failed", "stopped", "interrupted", "needs-info", "skipped", "merged", "dry-run",
))

type ReferenceReader = Callable[[Path, str], list[dict]]
_REFERENCE_READERS: dict[str, ReferenceReader] = {}
_ACTIVE_VALIDATOR_HOOKS = frozenset(("task_assignment", "parent_admission", "config_publication"))


class ValidationError(refusals.Invalid, ValueError):
    code = "validation"

    def __init__(self, field: str, message: str):
        self.field = field
        detail = f"{field}: {message}" if field and not message.startswith(f"{field}:") else message
        super().__init__(detail)


class NotFound(refusals.NotFound):
    code = "not_found"


class LineInUse(refusals.Conflict):
    code = "line_in_use"

    def __init__(self, line_id: str, blockers: list[dict]):
        self.line_id = line_id
        self.blockers = blockers
        summary = "; ".join(str(b.get("detail") or b.get("id") or b.get("kind") or "") for b in blockers[:5])
        super().__init__(f"line '{line_id}' cannot be removed: {summary or 'line is still in use'}")


class IncompatibleWriters(refusals.Conflict):
    code = "incompatible_writers"

    def __init__(self, blockers: list[str]):
        self.blockers = list(blockers)
        self.retry_guidance = (
            "Wait for active or queued incompatible writers to drain normally or upgrade to "
            f"{foundation.ACTIVE_LINE_VALIDATOR_CAPABILITY}, then retry line.delete."
        )
        super().__init__(
            f"Incompatible writers are still active ({', '.join(self.blockers)}). {self.retry_guidance}"
        )


def active_line_validator_installed() -> bool:
    return len(_ACTIVE_VALIDATOR_HOOKS) == 3


def register_reference_reader(name: str, reader_fn: ReferenceReader) -> None:
    if not name or not callable(reader_fn):
        raise ValueError("reference reader requires a non-empty name and callable")
    _REFERENCE_READERS[name] = reader_fn


def unregister_reference_reader(name: str) -> None:
    _REFERENCE_READERS.pop(name, None)


def registered_reference_readers() -> dict[str, ReferenceReader]:
    return dict(_REFERENCE_READERS)


def _extract_toml_line_refs(data: object, rel: str) -> list[dict]:
    refs: list[dict] = []
    if not isinstance(data, dict):
        return refs
    if isinstance(data.get("line"), str) and data["line"].strip():
        refs.append({"kind": "config", "path": rel, "line": data["line"].strip(), "detail": f"{rel} references line '{data['line'].strip()}'"})
    for section in ("loop", "loops", "source", "sources", "repo"):
        entries = data.get(section)
        if isinstance(entries, list):
            for idx, item in enumerate(entries):
                if isinstance(item, dict) and isinstance(item.get("line"), str) and item["line"].strip():
                    lid = item["line"].strip()
                    name = item.get("id") or item.get("name") or f"{section}[{idx}]"
                    refs.append({
                        "kind": "source" if section in ("source", "sources") else "config",
                        "path": rel,
                        "line": lid,
                        "detail": f"{rel} ({name}) references line '{lid}'",
                    })
        elif isinstance(entries, dict):
            for key, item in entries.items():
                if isinstance(item, dict) and isinstance(item.get("line"), str) and item["line"].strip():
                    lid = item["line"].strip()
                    refs.append({
                        "kind": "config",
                        "path": rel,
                        "line": lid,
                        "detail": f"{rel} ({section}.{key}) references line '{lid}'",
                    })
    return refs


def _read_inline_loop_references(root: Path, line_id: str) -> list[dict]:
    matches: list[dict] = []
    seen_paths: set[str] = set()
    if configrepo.is_repo(root):
        tree = configrepo.live_tree_files(root)
        for rel, raw in sorted(tree.items()):
            if not rel.endswith(".toml"):
                continue
            seen_paths.add(rel)
            try:
                parsed = tomllib.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                raise RuntimeError(f"unreadable authoritative config {rel}: {error}") from error
            for entry in _extract_toml_line_refs(parsed, rel):
                if entry["line"] == line_id:
                    matches.append(entry)
    elif (cfg_file := root / "config.toml").is_file():
        try:
            parsed = tomllib.loads(cfg_file.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise RuntimeError(f"unreadable config.toml: {error}") from error
        seen_paths.add("config.toml")
        for entry in _extract_toml_line_refs(parsed, "config.toml"):
            if entry["line"] == line_id:
                matches.append(entry)

    # Also scan configured loop_dirs outside/inside config root if present on disk.
    cfg, _ = config.load_or_problem()
    for loop_dir in cfg.loop_dirs:
        if not loop_dir.is_dir():
            continue
        for file in sorted(loop_dir.rglob("*.toml")):
            try:
                rel = file.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                rel = str(file)
            if rel in seen_paths:
                continue
            try:
                parsed = tomllib.loads(file.read_bytes().decode("utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                raise RuntimeError(f"unreadable loop config {rel}: {error}") from error
            for entry in _extract_toml_line_refs(parsed, rel):
                if entry["line"] == line_id:
                    matches.append(entry)
    return matches


def _read_source_file_references(root: Path, line_id: str) -> list[dict]:
    matches: list[dict] = []
    entries: list[tuple[str, bytes]] = []
    seen_paths: set[str] = set()
    if configrepo.is_repo(root):
        tree = configrepo.live_tree_files(root)
        for rel, raw in sorted(tree.items()):
            if rel.startswith("sources/") and rel.endswith(".toml"):
                seen_paths.add(rel)
                entries.append((rel, raw))
    sources_dir = root / "sources"
    if sources_dir.is_dir():
        for file in sorted(sources_dir.glob("*.toml")):
            if not file.is_file():
                continue
            rel = f"sources/{file.name}"
            if rel in seen_paths:
                continue
            try:
                entries.append((rel, file.read_bytes()))
            except OSError as error:
                raise RuntimeError(f"unreadable source config {rel}: {error}") from error
    for rel, raw in entries:
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise RuntimeError(f"unreadable authoritative source config {rel}: {error}") from error
        if not isinstance(parsed, dict):
            raise RuntimeError(f"unreadable authoritative source config {rel}: expected a TOML table")
        raw_line = parsed.get("line")
        if isinstance(raw_line, str) and raw_line.strip() == line_id:
            sid = Path(rel).stem
            matches.append({
                "kind": "source",
                "id": sid,
                "path": rel,
                "line": line_id,
                "detail": f"source '{sid}' ({rel}) references line '{line_id}'",
            })
    return matches


register_reference_reader("inline_loops", _read_inline_loop_references)
register_reference_reader("sources", _read_source_file_references)


def validate_config_line_references(changes: Mapping[str, bytes | None], root: Path) -> None:
    if not paths.DB_FILE.exists():
        return
    referenced: list[tuple[str, str]] = []
    for rel, raw in sorted(changes.items()):
        if raw is None or not rel.endswith(".toml"):
            continue
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        for entry in _extract_toml_line_refs(parsed, rel):
            referenced.append((rel, entry["line"]))
    if not referenced:
        return
    with foundation.reader() as conn:
        if not projects_store.available(conn):
            return
        for rel, lid in referenced:
            row = projects_store.get_line(conn, lid)
            if row is None:
                raise configrepo.ConfigRepoError(f"{rel}: unknown line '{lid}'")
            if row["archived"]:
                raise configrepo.ConfigRepoError(f"{rel}: line '{lid}' is archived")


def _load_cfg() -> config.Config:
    cfg, _ = config.load_or_problem()
    return cfg


def _validate_id(value: object) -> str:
    if not isinstance(value, str) or not projects_store.ID_RE.fullmatch(value):
        raise ValidationError("id", "must match ^[a-z0-9][a-z0-9-]{0,63}$")
    return value


def _validate_nonempty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(field, "must be a non-empty string")
    return value.strip()


def _validate_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(field, "must be a string")
    return value


def _validate_bool_flag(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ValidationError(field, "must be a boolean or 0/1")


def _validate_int_or_none(value: object, field: str, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValidationError(field, "must be an integer or null")
    if minimum is not None and value < minimum:
        raise ValidationError(field, f"must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(field, f"must be at most {maximum}")
    return value


def _validate_rank(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError("rank", "must be a finite number or null")
    return float(value)


def _validate_device_pool(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValidationError("device_pool", f"invalid JSON: {error}") from None
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise ValidationError("device_pool", "must be a JSON list of non-empty strings")
    return list(value)


def _validate_band(value: object) -> dict[str, list[float]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValidationError("band", f"invalid JSON: {error}") from None
    if not isinstance(value, dict):
        raise ValidationError("band", "must be a JSON object mapping step node to [min, max] seconds")
    out: dict[str, list[float]] = {}
    for node, bounds in value.items():
        if not isinstance(node, str) or not node.strip():
            raise ValidationError("band", "step node keys must be non-empty strings")
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in bounds)
        ):
            raise ValidationError("band", f"step '{node}' must map to [min, max] non-negative seconds")
        lo, hi = float(bounds[0]), float(bounds[1])
        if lo > hi:
            raise ValidationError("band", f"step '{node}' min ({lo}) cannot exceed max ({hi})")
        out[node] = [lo, hi]
    return out


def _validate_columns(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as error:
            raise ValidationError("columns", f"invalid JSON: {error}") from None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValidationError("columns", "must be a JSON list of filter field names")
    bad = [c for c in value if c not in viewfilter.FIELDS]
    if bad:
        raise ValidationError("columns", f"unknown filter field(s): {', '.join(bad)}")
    return list(value)


def present_project(row: dict, cfg: config.Config | None = None) -> dict:
    loaded = _load_cfg() if cfg is None else cfg
    repo_obj = next((r for r in loaded.repos if r.name == row["repo"]), None) if row.get("repo") else None
    return {
        **row,
        "github": repo_obj.github if repo_obj else "",
        "path": str(repo_obj.path) if repo_obj else "",
    }


def present_line(row: dict) -> dict:
    return dict(row)


def present_view(row: dict) -> dict:
    return {**row, "label": row["name"]}


def _diff_fields(old_row: dict | None, new_row: dict, tracked: tuple[str, ...]) -> dict[str, dict[str, object]]:
    if old_row is None:
        return {k: {"old": None, "new": new_row[k]} for k in tracked}
    changed: dict[str, dict[str, object]] = {}
    for k in tracked:
        if old_row.get(k) != new_row.get(k):
            changed[k] = {"old": old_row.get(k), "new": new_row.get(k)}
    return changed


def _receipt_result(
    conn,
    *,
    data: dict,
    subject: str,
    ver: int,
    seq: int | None,
    replayed: bool = False,
    extra: dict | None = None,
) -> dict:
    epoch = str(conn.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0])
    event_seq = f"{epoch}.{seq}" if seq is not None else None
    out = {
        "data": data,
        "event_seq": event_seq,
        "subject": subject,
        "ver": int(ver),
        "vers": {subject: int(ver)},
        "replayed": bool(replayed),
    }
    if extra:
        out.update(extra)
    return out


def _fold_history(conn, subject: str, target_seq: int) -> dict:
    if type(target_seq) is not int or target_seq < 1:
        raise ValidationError("seq", "must be a positive integer")
    rows = conn.execute(
        "SELECT seq, data FROM events_log WHERE subject = ? AND class = 'durable' AND seq <= ? ORDER BY seq ASC",
        (subject, target_seq),
    ).fetchall()
    if not rows or int(rows[-1]["seq"]) != target_seq:
        raise ValidationError("seq", f"event {target_seq} does not belong to {subject}")
    image: dict[str, object] = {}
    for row in rows:
        payload = json.loads(row["data"])
        fields = payload.get("fields")
        if isinstance(fields, dict):
            for col, diff in fields.items():
                if isinstance(diff, dict) and "new" in diff:
                    image[col] = diff["new"]
    return image


def _encode_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --- Projects ---

def list_projects(cfg: config.Config | None = None) -> list[dict]:
    loaded = _load_cfg() if cfg is None else cfg
    with foundation.reader() as conn:
        return [present_project(r, loaded) for r in projects_store.list_projects(conn)]


def get_project(project_id: str, cfg: config.Config | None = None) -> dict | None:
    loaded = _load_cfg() if cfg is None else cfg
    with foundation.reader() as conn:
        row = projects_store.get_project(conn, project_id)
        return present_project(row, loaded) if row else None


def _validate_project_image(image: dict) -> dict:
    return {
        "id": _validate_id(image.get("id")),
        "name": _validate_nonempty_str(image.get("name"), "name"),
        "repo": _validate_str(image.get("repo", ""), "repo"),
        "priority": _validate_int_or_none(image.get("priority"), "priority"),
        "rank": _validate_rank(image.get("rank")),
        "archived": _validate_bool_flag(image.get("archived", False), "archived"),
    }


def create_project(values: dict) -> dict:
    cfg = _load_cfg()
    clean = _validate_project_image({
        "id": values.get("id"),
        "name": values.get("name"),
        "repo": values.get("repo", ""),
        "priority": values.get("priority"),
        "rank": values.get("rank"),
        "archived": values.get("archived", False),
    })
    now = util.iso()
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        if projects_store.get_project(conn, clean["id"]) is not None:
            raise ValidationError("id", f"project '{clean['id']}' already exists")
        conn.execute(
            "INSERT INTO projects (id, name, repo, priority, rank, archived, version, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (clean["id"], clean["name"], clean["repo"], clean["priority"], clean["rank"],
             int(clean["archived"]), now, now),
        )
        row = projects_store.get_project(conn, clean["id"])
        subject = f"project:{clean['id']}"
        seq = events.emit(
            "project.changed",
            subject,
            cls="durable",
            ver=1,
            data={"fields": _diff_fields(None, row, projects_store.PROJECT_EVENT_FIELDS)},
            connection=conn,
        )
        return _receipt_result(conn, data=present_project(row, cfg), subject=subject, ver=1, seq=seq)


def update_project(project_id: str, base_version: int, fields: dict) -> dict:
    writes.validate_version(base_version)
    if unknown := sorted(set(fields) - PROJECT_WRITABLE):
        raise ValidationError(unknown[0], f"cannot update field(s): {', '.join(unknown)}")
    cfg = _load_cfg()
    now = util.iso()
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        existing = projects_store.get_project(conn, project_id)
        if existing is None:
            raise NotFound(f"No project {project_id}")
        if existing["version"] != base_version:
            raise writes.VersionConflict(present_project(existing, cfg))
        merged = {**existing, **fields}
        clean = _validate_project_image(merged)
        new_ver = existing["version"] + 1
        conn.execute(
            "UPDATE projects SET name = ?, repo = ?, priority = ?, rank = ?, archived = ?, "
            "version = ?, updated = ? WHERE id = ? AND version = ?",
            (clean["name"], clean["repo"], clean["priority"], clean["rank"],
             int(clean["archived"]), new_ver, now, project_id, base_version),
        )
        row = projects_store.get_project(conn, project_id)
        subject = f"project:{project_id}"
        seq = events.emit(
            "project.changed",
            subject,
            cls="durable",
            ver=new_ver,
            data={"fields": _diff_fields(existing, row, projects_store.PROJECT_EVENT_FIELDS)},
            connection=conn,
        )
        return _receipt_result(conn, data=present_project(row, cfg), subject=subject, ver=new_ver, seq=seq)


def restore_project(project_id: str, seq: int, base_version: int) -> dict:
    writes.validate_version(base_version)
    cfg = _load_cfg()
    now = util.iso()
    subject = f"project:{project_id}"
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        existing = projects_store.get_project(conn, project_id)
        if existing is None:
            raise NotFound(f"No project {project_id}")
        if existing["version"] != base_version:
            raise writes.VersionConflict(present_project(existing, cfg))
        image = _fold_history(conn, subject, seq)
        image["id"] = project_id
        clean = _validate_project_image(image)
        new_ver = existing["version"] + 1
        conn.execute(
            "UPDATE projects SET name = ?, repo = ?, priority = ?, rank = ?, archived = ?, "
            "version = ?, updated = ? WHERE id = ? AND version = ?",
            (clean["name"], clean["repo"], clean["priority"], clean["rank"],
             int(clean["archived"]), new_ver, now, project_id, base_version),
        )
        row = projects_store.get_project(conn, project_id)
        emitted_seq = events.emit(
            "project.changed",
            subject,
            cls="durable",
            ver=new_ver,
            data={
                "fields": _diff_fields(existing, row, projects_store.PROJECT_EVENT_FIELDS),
                "restored_from": seq,
            },
            connection=conn,
        )
        return _receipt_result(conn, data=present_project(row, cfg), subject=subject, ver=new_ver, seq=emitted_seq)


def project_history(project_id: str, *, limit: int = 100, after: int | None = None) -> tuple[list[dict], int | None]:
    with foundation.reader() as conn:
        if projects_store.get_project(conn, project_id) is None:
            raise NotFound(f"No project {project_id}")
        return projects_store.entity_history(conn, f"project:{project_id}", limit=limit, after=after)


# --- Lines ---

def list_lines(*, project: str | None = None, include_archived: bool = False) -> list[dict]:
    with foundation.reader() as conn:
        return [present_line(r) for r in projects_store.list_lines(conn, project=project, include_archived=include_archived)]


def get_line(line_id: str) -> dict | None:
    with foundation.reader() as conn:
        row = projects_store.get_line(conn, line_id)
        return present_line(row) if row else None


def _validate_line_image(conn, image: dict, *, cfg: config.Config) -> dict:
    lid = _validate_id(image.get("id"))
    project_id = _validate_nonempty_str(image.get("project"), "project")
    if projects_store.get_project(conn, project_id) is None:
        raise ValidationError("project", f"unknown project '{project_id}'")
    name = _validate_nonempty_str(image.get("name"), "name")
    kind = image.get("kind")
    if kind not in LINE_KINDS:
        raise ValidationError("kind", "must be 'pipeline' or 'loops'")
    repo_name = _validate_nonempty_str(image.get("repo"), "repo")
    configured = {r.name for r in cfg.repos}
    if repo_name not in configured:
        raise ValidationError("repo", f"'{repo_name}' is not a configured repo")
    adw_route = _validate_str(image.get("adw_route", ""), "adw_route")
    device_pool = _validate_device_pool(image.get("device_pool", []))
    rank = _validate_rank(image.get("rank"))
    concurrency = _validate_int_or_none(
        image.get("concurrency", 1), "concurrency", minimum=1, maximum=settings.PARALLEL_MAX,
    )
    if concurrency is None:
        raise ValidationError("concurrency", f"must be between 1 and {settings.PARALLEL_MAX}")
    wip_limit = _validate_int_or_none(image.get("wip_limit"), "wip_limit", minimum=1)
    priority = _validate_int_or_none(image.get("priority"), "priority")
    paused = _validate_bool_flag(image.get("paused", False), "paused")
    paused_why = _validate_str(image.get("paused_why", ""), "paused_why")
    if paused and not paused_why.strip():
        raise ValidationError("paused_why", "required when paused is set")
    band = _validate_band(image.get("band", {}))
    return {
        "id": lid,
        "project": project_id,
        "name": name,
        "kind": kind,
        "repo": repo_name,
        "adw_route": adw_route,
        "device_pool": device_pool,
        "rank": rank,
        "concurrency": concurrency,
        "wip_limit": wip_limit,
        "priority": priority,
        "paused": paused,
        "paused_why": paused_why,
        "band": band,
        "archived": False,
    }


def create_line(values: dict) -> dict:
    if "archived" in values:
        raise ValidationError("archived", "line.create cannot set archived")
    cfg = _load_cfg()
    now = util.iso()
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        clean = _validate_line_image(conn, values, cfg=cfg)
        if projects_store.get_line(conn, clean["id"]) is not None:
            raise ValidationError("id", f"line '{clean['id']}' already exists")
        conn.execute(
            "INSERT INTO lines (id, project, name, kind, repo, adw_route, device_pool, rank, concurrency, "
            "wip_limit, priority, paused, paused_why, band, archived, version, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)",
            (
                clean["id"], clean["project"], clean["name"], clean["kind"], clean["repo"],
                clean["adw_route"], _encode_json(clean["device_pool"]), clean["rank"],
                clean["concurrency"], clean["wip_limit"], clean["priority"],
                int(clean["paused"]), clean["paused_why"], _encode_json(clean["band"]),
                now, now,
            ),
        )
        row = projects_store.get_line(conn, clean["id"])
        subject = f"line:{clean['id']}"
        seq = events.emit(
            "line.changed",
            subject,
            cls="durable",
            ver=1,
            data={"fields": _diff_fields(None, row, projects_store.LINE_EVENT_FIELDS)},
            connection=conn,
        )
        return _receipt_result(conn, data=present_line(row), subject=subject, ver=1, seq=seq)


def update_line(line_id: str, base_version: int, fields: dict) -> dict:
    writes.validate_version(base_version)
    if "archived" in fields:
        raise ValidationError("archived", "archived cannot be modified via line.update; use line.delete or line.restore")
    if unknown := sorted(set(fields) - LINE_WRITABLE):
        raise ValidationError(unknown[0], f"cannot update field(s): {', '.join(unknown)}")
    cfg = _load_cfg()
    now = util.iso()
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        existing = projects_store.get_line(conn, line_id)
        if existing is None:
            raise NotFound(f"No line {line_id}")
        if existing["version"] != base_version:
            raise writes.VersionConflict(present_line(existing))
        if existing["archived"]:
            raise ValidationError("archived", f"line '{line_id}' is archived; restore it before updating")
        merged = {**existing, **fields}
        clean = _validate_line_image(conn, merged, cfg=cfg)
        new_ver = existing["version"] + 1
        conn.execute(
            "UPDATE lines SET project = ?, name = ?, kind = ?, repo = ?, adw_route = ?, device_pool = ?, "
            "rank = ?, concurrency = ?, wip_limit = ?, priority = ?, paused = ?, paused_why = ?, band = ?, "
            "version = ?, updated = ? WHERE id = ? AND version = ?",
            (
                clean["project"], clean["name"], clean["kind"], clean["repo"], clean["adw_route"],
                _encode_json(clean["device_pool"]), clean["rank"], clean["concurrency"],
                clean["wip_limit"], clean["priority"], int(clean["paused"]), clean["paused_why"],
                _encode_json(clean["band"]), new_ver, now, line_id, base_version,
            ),
        )
        if existing["paused"] != clean["paused"]:
            inbox_store.set_line_paused(conn, line_id, clean["paused"])
        row = projects_store.get_line(conn, line_id)
        subject = f"line:{line_id}"
        seq = events.emit(
            "line.changed",
            subject,
            cls="durable",
            ver=new_ver,
            data={"fields": _diff_fields(existing, row, projects_store.LINE_EVENT_FIELDS)},
            connection=conn,
        )
        return _receipt_result(conn, data=present_line(row), subject=subject, ver=new_ver, seq=seq)


def restore_line(line_id: str, seq: int, base_version: int) -> dict:
    writes.validate_version(base_version)
    cfg = _load_cfg()
    now = util.iso()
    subject = f"line:{line_id}"
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        existing = projects_store.get_line(conn, line_id)
        if existing is None:
            raise NotFound(f"No line {line_id}")
        if existing["version"] != base_version:
            raise writes.VersionConflict(present_line(existing))
        image = _fold_history(conn, subject, seq)
        if bool(image.get("archived", False)):
            raise ValidationError("archived", "line.restore accepts active after-images only; cannot replay archived=true")
        image["id"] = line_id
        clean = _validate_line_image(conn, image, cfg=cfg)
        new_ver = existing["version"] + 1
        conn.execute(
            "UPDATE lines SET project = ?, name = ?, kind = ?, repo = ?, adw_route = ?, device_pool = ?, "
            "rank = ?, concurrency = ?, wip_limit = ?, priority = ?, paused = ?, paused_why = ?, band = ?, "
            "archived = 0, version = ?, updated = ? WHERE id = ? AND version = ?",
            (
                clean["project"], clean["name"], clean["kind"], clean["repo"], clean["adw_route"],
                _encode_json(clean["device_pool"]), clean["rank"], clean["concurrency"],
                clean["wip_limit"], clean["priority"], int(clean["paused"]), clean["paused_why"],
                _encode_json(clean["band"]), new_ver, now, line_id, base_version,
            ),
        )
        if existing["paused"] != clean["paused"]:
            inbox_store.set_line_paused(conn, line_id, clean["paused"])
        row = projects_store.get_line(conn, line_id)
        emitted_seq = events.emit(
            "line.changed",
            subject,
            cls="durable",
            ver=new_ver,
            data={
                "fields": _diff_fields(existing, row, projects_store.LINE_EVENT_FIELDS),
                "restored_from": seq,
            },
            connection=conn,
        )
        return _receipt_result(conn, data=present_line(row), subject=subject, ver=new_ver, seq=emitted_seq)


def _find_tombstone_receipt(conn, line_id: str, ver: int) -> tuple[int | None, int | None, bool]:
    subject = f"line:{line_id}"
    tomb = conn.execute(
        "SELECT seq, data FROM events_log WHERE subject = ? AND class = 'durable' AND ver = ? ORDER BY seq DESC LIMIT 1",
        (subject, ver),
    ).fetchone()
    if tomb is None:
        return None, None, False
    payload = json.loads(tomb["data"])
    arch_diff = (payload.get("fields") or {}).get("archived")
    archived_transition = isinstance(arch_diff, dict) and bool(arch_diff.get("new")) is True
    prev = conn.execute(
        "SELECT MAX(seq) FROM events_log WHERE subject = ? AND class = 'durable' AND seq < ?",
        (subject, int(tomb["seq"])),
    ).fetchone()
    pre_seq = int(prev[0]) if prev and prev[0] is not None else None
    return int(tomb["seq"]), pre_seq, archived_transition


def delete_line(line_id: str, base_version: int) -> dict:
    writes.validate_version(base_version)
    with configrepo.maybe_instance_fence() as fence:
        # 1. Compatibility gate: every live/queued writer must support active_line_validator_v1.
        with foundation.reader() as rconn:
            if not active_line_validator_installed():
                raise IncompatibleWriters([f"shared {foundation.ACTIVE_LINE_VALIDATOR_CAPABILITY} hook not installed"])
            if blockers := foundation.incompatible_line_writers(rconn):
                raise IncompatibleWriters(blockers)

        # 2. Under the instance fence (outside SQLite write tx), reconcile any live Git publication
        # missing from SQLite and inspect authoritative current source/loop config bytes.
        root = paths.CONFIG_DIR
        config_refs: list[dict] = []
        try:
            if configrepo.is_repo(root):
                configrepo.reconcile(fence=fence)
        except Exception as error:
            raise LineInUse(line_id, [{
                "kind": "unreconciled_config",
                "detail": f"failed to reconcile authoritative config publication: {error}",
            }]) from error

        for reader_name, reader_fn in sorted(_REFERENCE_READERS.items()):
            try:
                found = reader_fn(root, line_id)
            except Exception as error:
                raise LineInUse(line_id, [{
                    "kind": "unreadable_config",
                    "reader": reader_name,
                    "detail": f"authoritative reference reader '{reader_name}' failed closed: {error}",
                }]) from error
            if found:
                config_refs.extend(found)

        cfg = _load_cfg()
        now = util.iso()
        subject = f"line:{line_id}"

        # 3. Single write transaction under the held instance fence.
        with foundation.tx() as conn:
            existing = projects_store.get_line(conn, line_id)
            if existing is None:
                raise NotFound(f"No line {line_id}")

            if existing["archived"]:
                tomb_seq, pre_seq, was_archive = _find_tombstone_receipt(conn, line_id, existing["version"])
                if base_version == existing["version"] or (
                    was_archive and existing["version"] > 1 and base_version == existing["version"] - 1
                ):
                    return _receipt_result(
                        conn,
                        data=present_line(existing),
                        subject=subject,
                        ver=existing["version"],
                        seq=tomb_seq,
                        replayed=True,
                        extra={"pre_removal_seq": pre_seq},
                    )
                raise writes.VersionConflict(present_line(existing))

            if existing["version"] != base_version:
                raise writes.VersionConflict(present_line(existing))

            in_use: list[dict] = []
            # Nonterminal tasks referencing this line.
            task_rows = conn.execute(
                "SELECT id, state FROM tasks WHERE line = ? AND state NOT IN ('done', 'closed') ORDER BY id",
                (line_id,),
            ).fetchall()
            for tr in task_rows:
                tid = task_store.format_task_id(int(tr["id"])) if isinstance(tr["id"], int) else str(tr["id"])
                in_use.append({
                    "kind": "task",
                    "id": tid,
                    "state": tr["state"],
                    "detail": f"nonterminal task {tid} ({tr['state']}) references line '{line_id}'",
                })

            # Unfinished runs referencing this line.
            run_rows = conn.execute(
                "SELECT id, status, finished FROM runs WHERE line = ? ORDER BY started, id",
                (line_id,),
            ).fetchall()
            for rr in run_rows:
                status = str(rr["status"] or "")
                if status in ("queued", "running", "waiting") or (
                    rr["finished"] is None and status not in FINISHED_RUN_STATUSES
                ):
                    in_use.append({
                        "kind": "run",
                        "id": rr["id"],
                        "status": status,
                        "detail": f"unfinished run {rr['id']} ({status}) references line '{line_id}'",
                    })

            # Authoritative config references.
            in_use.extend(config_refs)

            # Last active pipeline line of an enabled pipeline repo.
            if existing["kind"] == "pipeline":
                repo_obj = next((r for r in cfg.repos if r.name == existing["repo"]), None)
                if repo_obj is not None and repo_obj.pipeline:
                    other = conn.execute(
                        "SELECT COUNT(*) FROM lines WHERE repo = ? AND kind = 'pipeline' "
                        "AND archived = 0 AND id != ?",
                        (existing["repo"], line_id),
                    ).fetchone()[0]
                    if int(other) == 0:
                        in_use.append({
                            "kind": "last_pipeline_line",
                            "repo": existing["repo"],
                            "detail": f"cannot remove the last active pipeline line of enabled pipeline repo '{existing['repo']}'",
                        })

            if in_use:
                raise LineInUse(line_id, in_use)

            prev_row = conn.execute(
                "SELECT MAX(seq) FROM events_log WHERE subject = ? AND class = 'durable'",
                (subject,),
            ).fetchone()
            pre_removal_seq = int(prev_row[0]) if prev_row and prev_row[0] is not None else None

            new_ver = existing["version"] + 1
            conn.execute(
                "UPDATE lines SET archived = 1, version = ?, updated = ? WHERE id = ? AND version = ?",
                (new_ver, now, line_id, base_version),
            )
            row = projects_store.get_line(conn, line_id)
            emitted_seq = events.emit(
                "line.changed",
                subject,
                cls="durable",
                ver=new_ver,
                data={"fields": _diff_fields(existing, row, projects_store.LINE_EVENT_FIELDS)},
                connection=conn,
            )
            return _receipt_result(
                conn,
                data=present_line(row),
                subject=subject,
                ver=new_ver,
                seq=emitted_seq,
                replayed=False,
                extra={"pre_removal_seq": pre_removal_seq},
            )


def line_history(line_id: str, *, limit: int = 100, after: int | None = None) -> tuple[list[dict], int | None]:
    with foundation.reader() as conn:
        if projects_store.get_line(conn, line_id) is None:
            raise NotFound(f"No line {line_id}")
        return projects_store.entity_history(conn, f"line:{line_id}", limit=limit, after=after)


# --- Views ---

def list_views(*, project: str | None = None) -> list[dict]:
    with foundation.reader() as conn:
        return [present_view(r) for r in projects_store.list_views(conn, project=project)]


def get_view(view_id: str) -> dict | None:
    with foundation.reader() as conn:
        row = projects_store.get_view(conn, view_id)
        return present_view(row) if row else None


def _validate_view_image(conn, image: dict) -> dict:
    vid = _validate_id(image.get("id"))
    name = _validate_nonempty_str(image.get("name"), "name")
    project_id = image.get("project")
    if project_id is not None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValidationError("project", "must be a project id or null")
        if projects_store.get_project(conn, project_id) is None:
            raise ValidationError("project", f"unknown project '{project_id}'")
    raw_filter = image.get("filter", "")
    if not isinstance(raw_filter, str):
        raise ValidationError("filter", "must be a string")
    try:
        compiled = viewfilter.compile(raw_filter)
    except viewfilter.FilterError as error:
        raise ValidationError("filter", str(error)) from None
    group_by = image.get("group_by", "state")
    if group_by not in VIEW_GROUP_BY:
        raise ValidationError("group_by", f"must be one of {', '.join(sorted(VIEW_GROUP_BY))}")
    sort = image.get("sort", "rank")
    if sort not in VIEW_SORT:
        raise ValidationError("sort", f"must be one of {', '.join(sorted(VIEW_SORT))}")
    layout = image.get("layout", "board")
    if layout not in VIEW_LAYOUT:
        raise ValidationError("layout", f"must be one of {', '.join(sorted(VIEW_LAYOUT))}")
    columns = _validate_columns(image.get("columns", []))
    rank = _validate_rank(image.get("rank"))
    return {
        "id": vid,
        "name": name,
        "project": project_id,
        "filter": raw_filter,
        "filter_sql": compiled,
        "group_by": group_by,
        "sort": sort,
        "layout": layout,
        "columns": columns,
        "rank": rank,
    }


def create_view(values: dict) -> dict:
    now = util.iso()
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        clean = _validate_view_image(conn, values)
        if projects_store.get_view(conn, clean["id"]) is not None:
            raise ValidationError("id", f"view '{clean['id']}' already exists")
        conn.execute(
            "INSERT INTO views (id, name, project, filter, filter_sql, group_by, sort, layout, columns, "
            "rank, version, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                clean["id"], clean["name"], clean["project"], clean["filter"], clean["filter_sql"],
                clean["group_by"], clean["sort"], clean["layout"], _encode_json(clean["columns"]),
                clean["rank"], now, now,
            ),
        )
        row = projects_store.get_view(conn, clean["id"])
        subject = f"view:{clean['id']}"
        seq = events.emit(
            "view.changed",
            subject,
            cls="durable",
            ver=1,
            data={"fields": _diff_fields(None, row, projects_store.VIEW_EVENT_FIELDS)},
            connection=conn,
        )
        return _receipt_result(conn, data=present_view(row), subject=subject, ver=1, seq=seq)


def update_view(view_id: str, base_version: int, fields: dict) -> dict:
    writes.validate_version(base_version)
    if unknown := sorted(set(fields) - VIEW_WRITABLE):
        raise ValidationError(unknown[0], f"cannot update field(s): {', '.join(unknown)}")
    now = util.iso()
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        existing = projects_store.get_view(conn, view_id)
        if existing is None:
            raise NotFound(f"No view {view_id}")
        if existing["version"] != base_version:
            raise writes.VersionConflict(present_view(existing))
        merged = {**existing, **fields}
        clean = _validate_view_image(conn, merged)
        new_ver = existing["version"] + 1
        conn.execute(
            "UPDATE views SET name = ?, project = ?, filter = ?, filter_sql = ?, group_by = ?, sort = ?, "
            "layout = ?, columns = ?, rank = ?, version = ?, updated = ? WHERE id = ? AND version = ?",
            (
                clean["name"], clean["project"], clean["filter"], clean["filter_sql"],
                clean["group_by"], clean["sort"], clean["layout"], _encode_json(clean["columns"]),
                clean["rank"], new_ver, now, view_id, base_version,
            ),
        )
        row = projects_store.get_view(conn, view_id)
        subject = f"view:{view_id}"
        seq = events.emit(
            "view.changed",
            subject,
            cls="durable",
            ver=new_ver,
            data={"fields": _diff_fields(existing, row, projects_store.VIEW_EVENT_FIELDS)},
            connection=conn,
        )
        return _receipt_result(conn, data=present_view(row), subject=subject, ver=new_ver, seq=seq)


def restore_view(view_id: str, seq: int, base_version: int) -> dict:
    writes.validate_version(base_version)
    now = util.iso()
    subject = f"view:{view_id}"
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        existing = projects_store.get_view(conn, view_id)
        if existing is None:
            raise NotFound(f"No view {view_id}")
        if existing["version"] != base_version:
            raise writes.VersionConflict(present_view(existing))
        image = _fold_history(conn, subject, seq)
        image["id"] = view_id
        clean = _validate_view_image(conn, image)
        new_ver = existing["version"] + 1
        conn.execute(
            "UPDATE views SET name = ?, project = ?, filter = ?, filter_sql = ?, group_by = ?, sort = ?, "
            "layout = ?, columns = ?, rank = ?, version = ?, updated = ? WHERE id = ? AND version = ?",
            (
                clean["name"], clean["project"], clean["filter"], clean["filter_sql"],
                clean["group_by"], clean["sort"], clean["layout"], _encode_json(clean["columns"]),
                clean["rank"], new_ver, now, view_id, base_version,
            ),
        )
        row = projects_store.get_view(conn, view_id)
        emitted_seq = events.emit(
            "view.changed",
            subject,
            cls="durable",
            ver=new_ver,
            data={
                "fields": _diff_fields(existing, row, projects_store.VIEW_EVENT_FIELDS),
                "restored_from": seq,
            },
            connection=conn,
        )
        return _receipt_result(conn, data=present_view(row), subject=subject, ver=new_ver, seq=emitted_seq)


def view_history(view_id: str, *, limit: int = 100, after: int | None = None) -> tuple[list[dict], int | None]:
    with foundation.reader() as conn:
        if projects_store.get_view(conn, view_id) is None:
            raise NotFound(f"No view {view_id}")
        return projects_store.entity_history(conn, f"view:{view_id}", limit=limit, after=after)


# --- Task assignment & active-line validation ---

def assert_line_active(conn, line_id: str, *, field: str = "line") -> dict:
    if not projects_store.available(conn):
        raise ValidationError(field, "lines table is not migrated yet")
    row = projects_store.get_line(conn, line_id)
    if row is None:
        raise ValidationError(field, f"unknown line '{line_id}'")
    if row["archived"]:
        raise ValidationError(field, f"line '{line_id}' is archived")
    return row


def validate_task_assignment(
    conn,
    *,
    repo: str = "",
    project: str | None = None,
    line: str | None = None,
) -> None:
    if project is None and line is None:
        return
    if not projects_store.available(conn):
        return
    proj_row = None
    if project is not None:
        if not isinstance(project, str) or not project.strip():
            raise ValidationError("project", "must be an existing project id or null")
        proj_row = projects_store.get_project(conn, project)
        if proj_row is None:
            raise ValidationError("project", f"unknown project '{project}'")
        if repo and proj_row["repo"] and proj_row["repo"] != repo:
            raise ValidationError("project", f"project '{project}' belongs to repo '{proj_row['repo']}', not '{repo}'")
    if line is not None:
        if not isinstance(line, str) or not line.strip():
            raise ValidationError("line", "must be an existing active line id or null")
        line_row = assert_line_active(conn, line, field="line")
        if project is not None and line_row["project"] != project:
            raise ValidationError(
                "line",
                f"line '{line}' belongs to project '{line_row['project']}', not '{project}'",
            )
        if repo and line_row["repo"] != repo:
            raise ValidationError(
                "line",
                f"line '{line}' belongs to repo '{line_row['repo']}', not '{repo}'",
            )


# --- Ensure projects & pipeline lines for configured repos ---

def ensure(cfg: config.Config | None = None) -> int:
    loaded = _load_cfg() if cfg is None else cfg
    pipeline_repos = [r for r in loaded.repos if r.pipeline]
    if not pipeline_repos or not paths.DB_FILE.exists():
        return 0
    with foundation.reader() as rconn:
        if not projects_store.available(rconn):
            return 0
        existing_projects = {r["repo"]: r for r in projects_store.list_projects(rconn) if r["repo"]}
        existing_project_ids = {r["id"] for r in projects_store.list_projects(rconn)}
        existing_line_repos = {r["repo"] for r in projects_store.list_lines(rconn, include_archived=True)}
        existing_line_ids = {r["id"] for r in projects_store.list_lines(rconn, include_archived=True)}
        needs_work = False
        for r in pipeline_repos:
            proj = existing_projects.get(r.name)
            if proj is not None and proj["archived"]:
                continue
            if proj is None or r.name not in existing_line_repos:
                needs_work = True
                break
        if not needs_work:
            return 0

    created = 0
    now = util.iso()
    actor = principals.current()
    with configrepo.maybe_instance_fence(), foundation.tx() as conn:
        if not projects_store.available(conn):
            return 0
        if not actor or actor == "owner" or "@" in actor:
            actor = projects_store._migration_actor(conn)
        existing_projects = {r["repo"]: r for r in projects_store.list_projects(conn) if r["repo"]}
        existing_project_ids = {r["id"] for r in projects_store.list_projects(conn)}
        existing_line_repos = {r["repo"] for r in projects_store.list_lines(conn, include_archived=True)}
        existing_line_ids = {r["id"] for r in projects_store.list_lines(conn, include_archived=True)}
        with principals.acting(actor):
            for r in pipeline_repos:
                proj = existing_projects.get(r.name)
                if proj is not None and proj["archived"]:
                    continue
                if proj is None:
                    base_pid = projects_store.slug_id(r.name)
                    pid = base_pid
                    suffix = 2
                    while pid in existing_project_ids:
                        tag = f"-{suffix}"
                        pid = f"{base_pid[:64 - len(tag)]}{tag}"
                        suffix += 1
                    conn.execute(
                        "INSERT INTO projects (id, name, repo, priority, rank, archived, version, created, updated) "
                        "VALUES (?, ?, ?, NULL, NULL, 0, 1, ?, ?)",
                        (pid, r.name, r.name, now, now),
                    )
                    proj = projects_store.get_project(conn, pid)
                    existing_projects[r.name] = proj
                    existing_project_ids.add(pid)
                    events.emit(
                        "project.changed",
                        f"project:{pid}",
                        cls="durable",
                        ver=1,
                        data={"fields": _diff_fields(None, proj, projects_store.PROJECT_EVENT_FIELDS)},
                        connection=conn,
                    )
                    created += 1
                if r.name in existing_line_repos:
                    continue
                lid = projects_store.pipeline_line_id(proj["id"])
                if lid in existing_line_ids:
                    continue
                conn.execute(
                    "INSERT INTO lines (id, project, name, kind, repo, adw_route, device_pool, rank, "
                    "concurrency, wip_limit, priority, paused, paused_why, band, archived, version, created, updated) "
                    "VALUES (?, ?, ?, 'pipeline', ?, '', '[]', NULL, 1, NULL, NULL, 0, '', '{}', 0, 1, ?, ?)",
                    (lid, proj["id"], r.name, r.name, now, now),
                )
                line_row = projects_store.get_line(conn, lid)
                existing_line_repos.add(r.name)
                existing_line_ids.add(lid)
                events.emit(
                    "line.changed",
                    f"line:{lid}",
                    cls="durable",
                    ver=1,
                    data={"fields": _diff_fields(None, line_row, projects_store.LINE_EVENT_FIELDS)},
                    connection=conn,
                )
                created += 1
    return created


# --- Line resolution, counters, and view queries ---

def resolve_line_for_ticket(conn, repo_name: str, tid: str | int = "", *, cfg: config.Config | None = None) -> dict | None:
    if not projects_store.available(conn):
        return None
    if tid:
        task_num = task_store.TaskStore.find_task_num(conn, repo_name, tid, cfg=cfg)
        if task_num is not None:
            row = task_store._select_task_row(conn, task_num)
            if row is not None and row["line"]:
                return projects_store.get_line(conn, str(row["line"]))
    return projects_store.default_pipeline_line(conn, repo_name)


def resolve_line_read(repo_name: str, tid: str | int = "", *, cfg: config.Config | None = None) -> dict | None:
    if not paths.DB_FILE.exists():
        return None
    with foundation.reader() as conn:
        return resolve_line_for_ticket(conn, repo_name, tid, cfg=cfg)


def live_runs_by_line(live_runs: list[dict] | None = None) -> Counter[str]:
    from . import pipeline

    running = pipeline.live_pipeline_runs() if live_runs is None else live_runs
    counts: Counter[str] = Counter()
    if not running:
        return counts
    defaults: dict[str, str | None] = {}
    with foundation.reader() as conn:
        has_lines = projects_store.available(conn)
        for run in running:
            lid = run.get("line")
            if lid:
                counts[str(lid)] += 1
            elif has_lines and (repo := run.get("repo")):
                if repo not in defaults:
                    default_line = projects_store.default_pipeline_line(conn, str(repo))
                    defaults[repo] = default_line["id"] if default_line else None
                if defaults[repo]:
                    counts[defaults[repo]] += 1
                else:
                    counts[str(repo)] += 1
            elif repo := run.get("repo"):
                counts[str(repo)] += 1
    return counts


def wip_counts_by_line(conn=None) -> Counter[str]:
    if conn is None:
        if not paths.DB_FILE.exists():
            return Counter()
        with foundation.reader() as rconn:
            return wip_counts_by_line(rconn)
    counts: Counter[str] = Counter()
    if not projects_store.available(conn):
        return counts
    defaults: dict[str, str | None] = {}
    rows = conn.execute(
        "SELECT line, repo, state FROM tasks WHERE state IN ('running', 'needs-you', 'review', 'needs-info', 'pr')"
    ).fetchall()
    for row in rows:
        canon_state, _ = config.canonical_task_state(str(row["state"] or ""))
        if canon_state not in WIP_STATES:
            continue
        lid = row["line"]
        if lid:
            counts[str(lid)] += 1
        elif repo := row["repo"]:
            if repo not in defaults:
                default_line = projects_store.default_pipeline_line(conn, str(repo))
                defaults[repo] = default_line["id"] if default_line else None
            if defaults[repo]:
                counts[defaults[repo]] += 1
    return counts


def query_view_tasks(conn, view_row: dict) -> list[dict]:
    filter_sql = (view_row.get("filter_sql") or "").strip() or "1=1"
    sort_key = view_row.get("sort") or "rank"
    order_sql = SORT_SQL.get(sort_key, SORT_SQL["rank"])
    clauses = [f"({filter_sql})"]
    args: list[object] = []
    if view_row.get("project"):
        clauses.append("t.project = ?")
        args.append(view_row["project"])
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT t.* FROM tasks t WHERE {where} ORDER BY {order_sql}",  # noqa: S608
        args,
    ).fetchall()
    return [task_store._present_task_row(conn, r) for r in rows]


def board_lines_summary(
    conn,
    *,
    project: str | None = None,
    include_archived: bool = False,
    live_runs: list[dict] | None = None,
) -> list[dict]:
    if not projects_store.available(conn):
        return []
    line_rows = projects_store.list_lines(conn, project=project, include_archived=include_archived)
    going = live_runs_by_line(live_runs)
    wip = wip_counts_by_line(conn)
    out: list[dict] = []
    for row in line_rows:
        lid = row["id"]
        out.append({
            **present_line(row),
            "runs_going": int(going.get(lid, 0)),
            "tasks_in_progress": int(wip.get(lid, 0)),
        })
    return out
