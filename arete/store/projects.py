from __future__ import annotations

import json
import re
import tomllib
from contextlib import suppress

from .. import paths, principals, util
from . import events
from .db import reader
from .migrations.m0002_foundation_time_shape import TIME_GLOB

TABLES = {
    "projects": "kept",
    "lines": "kept",
    "views": "kept",
}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SLUG_BAD_RE = re.compile(r"[^a-z0-9-]+")

PROJECT_EVENT_FIELDS = ("id", "name", "repo", "priority", "rank", "archived")
LINE_EVENT_FIELDS = (
    "id", "project", "name", "kind", "repo", "adw_route", "device_pool",
    "rank", "concurrency", "wip_limit", "priority", "paused", "paused_why",
    "band", "archived",
)
VIEW_EVENT_FIELDS = (
    "id", "name", "project", "filter", "filter_sql", "group_by", "sort",
    "layout", "columns", "rank",
)


def slug_id(name: str) -> str:
    raw = _SLUG_BAD_RE.sub("-", str(name).lower()).strip("-")
    if not raw:
        raw = "project"
    if not raw[0].isalnum():
        raw = "p-" + raw
    return raw[:64]


def pipeline_line_id(project_id: str) -> str:
    return f"{project_id[:55]}-pipeline"


def available(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lines'"
    ).fetchone() is not None


def _migration_actor(conn) -> str:
    cur = principals.current()
    if cur and cur != "owner" and "@" not in cur:
        return cur
    row = conn.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, rowid LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else "hand"


def _create_fields_diff(row: dict, fields: tuple[str, ...]) -> dict[str, dict[str, object]]:
    return {k: {"old": None, "new": row[k]} for k in fields}


def decode_project(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    return {
        "id": d["id"],
        "name": d["name"],
        "repo": d["repo"],
        "priority": d["priority"],
        "rank": d["rank"],
        "archived": bool(d["archived"]),
        "version": int(d["version"]),
        "created": d["created"],
        "updated": d["updated"],
    }


def decode_line(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    try:
        pool = json.loads(d["device_pool"] or "[]")
        if not isinstance(pool, list):
            pool = []
    except ValueError:
        pool = []
    try:
        band = json.loads(d["band"] or "{}")
        if not isinstance(band, dict):
            band = {}
    except ValueError:
        band = {}
    archived = bool(d["archived"])
    paused = bool(d["paused"])
    return {
        "id": d["id"],
        "project": d["project"],
        "name": d["name"],
        "kind": d["kind"],
        "repo": d["repo"],
        "adw_route": d["adw_route"] or "",
        "device_pool": pool,
        "rank": d["rank"],
        "concurrency": int(d["concurrency"]),
        "wip_limit": d["wip_limit"],
        "priority": d["priority"],
        "paused": paused,
        "paused_why": d["paused_why"] or "",
        "band": band,
        "archived": archived,
        "enabled": not archived and not paused,
        "version": int(d["version"]),
        "created": d["created"],
        "updated": d["updated"],
    }


def decode_view(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    try:
        cols = json.loads(d["columns"] or "[]")
        if not isinstance(cols, list):
            cols = []
    except ValueError:
        cols = []
    return {
        "id": d["id"],
        "name": d["name"],
        "label": d["name"],
        "project": d["project"],
        "filter": d["filter"] or "",
        "filter_sql": d["filter_sql"] or "1=1",
        "group_by": d["group_by"] or "state",
        "sort": d["sort"] or "rank",
        "layout": d["layout"] or "board",
        "columns": cols,
        "rank": d["rank"],
        "version": int(d["version"]),
        "created": d["created"],
        "updated": d["updated"],
    }


def apply_migration(conn) -> None:
    from .. import settings

    conn.execute(f"""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            repo TEXT NOT NULL DEFAULT '',
            priority INTEGER,
            rank REAL,
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            version INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL CHECK (created GLOB '{TIME_GLOB}'),
            updated TEXT NOT NULL CHECK (updated GLOB '{TIME_GLOB}')
        ) STRICT
    """)
    conn.execute(f"""
        CREATE TABLE lines (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('pipeline', 'loops')),
            repo TEXT NOT NULL,
            adw_route TEXT NOT NULL DEFAULT '',
            device_pool TEXT NOT NULL DEFAULT '[]',
            rank REAL,
            concurrency INTEGER NOT NULL DEFAULT 1 CHECK (concurrency >= 1),
            wip_limit INTEGER CHECK (wip_limit IS NULL OR wip_limit >= 1),
            priority INTEGER,
            paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
            paused_why TEXT NOT NULL DEFAULT '',
            band TEXT NOT NULL DEFAULT '{{}}',
            archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
            version INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL CHECK (created GLOB '{TIME_GLOB}'),
            updated TEXT NOT NULL CHECK (updated GLOB '{TIME_GLOB}')
        ) STRICT
    """)
    conn.execute(f"""
        CREATE TABLE views (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            project TEXT,
            filter TEXT NOT NULL DEFAULT '',
            filter_sql TEXT NOT NULL DEFAULT '1=1',
            group_by TEXT NOT NULL DEFAULT 'state',
            sort TEXT NOT NULL DEFAULT 'rank',
            layout TEXT NOT NULL DEFAULT 'board',
            columns TEXT NOT NULL DEFAULT '[]',
            rank REAL,
            version INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL CHECK (created GLOB '{TIME_GLOB}'),
            updated TEXT NOT NULL CHECK (updated GLOB '{TIME_GLOB}')
        ) STRICT
    """)
    conn.execute("CREATE INDEX lines_project ON lines (project, archived, rank, id)")
    conn.execute("CREATE INDEX lines_repo ON lines (repo, kind, archived, rank, id)")
    conn.execute("CREATE INDEX views_project ON views (project, rank, id)")

    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    if "line" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN line TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS runs_line ON runs (line, status)")

    raw_data: dict = {}
    with suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        if paths.CONFIG_FILE.is_file():
            loaded = tomllib.loads(paths.CONFIG_FILE.read_bytes().decode("utf-8"))
            if isinstance(loaded, dict):
                raw_data = loaded

    raw_limit = raw_data.get("max_parallel_per_repo", 1)
    try:
        concurrency = int(raw_limit) if not isinstance(raw_limit, bool) else 1
    except (TypeError, ValueError):
        concurrency = 1
    concurrency = max(1, min(settings.PARALLEL_MAX, concurrency))

    repos = raw_data.get("repo")
    if not isinstance(repos, list):
        return

    now = util.iso()
    actor = _migration_actor(conn)
    task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    has_task_project = "project" in task_cols
    has_task_line = "line" in task_cols

    seen_projects: set[str] = set()
    seen_lines: set[str] = set()
    with principals.acting(actor):
        for entry in repos:
            if not isinstance(entry, dict):
                continue
            repo_name = entry.get("name")
            if not isinstance(repo_name, str) or not repo_name.strip():
                continue
            repo_name = repo_name.strip()
            base_pid = slug_id(repo_name)
            project_id = base_pid
            suffix = 2
            while project_id in seen_projects:
                tag = f"-{suffix}"
                project_id = f"{base_pid[:64 - len(tag)]}{tag}"
                suffix += 1
            seen_projects.add(project_id)

            conn.execute(
                "INSERT INTO projects (id, name, repo, priority, rank, archived, version, created, updated) "
                "VALUES (?, ?, ?, NULL, NULL, 0, 1, ?, ?)",
                (project_id, repo_name, repo_name, now, now),
            )
            proj_row = decode_project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            )
            events.emit(
                "project.changed",
                f"project:{project_id}",
                cls="durable",
                ver=1,
                data={"fields": _create_fields_diff(proj_row, PROJECT_EVENT_FIELDS)},
                connection=conn,
            )
            if has_task_project:
                conn.execute(
                    "UPDATE tasks SET project = ? WHERE repo = ? AND (project IS NULL OR project = '')",
                    (project_id, repo_name),
                )

            if entry.get("pipeline") is True:
                base_lid = pipeline_line_id(project_id)
                line_id = base_lid
                lsuf = 2
                while line_id in seen_lines:
                    tag = f"-{lsuf}"
                    line_id = f"{base_lid[:64 - len(tag)]}{tag}"
                    lsuf += 1
                seen_lines.add(line_id)

                conn.execute(
                    "INSERT INTO lines (id, project, name, kind, repo, adw_route, device_pool, rank, "
                    "concurrency, wip_limit, priority, paused, paused_why, band, archived, version, created, updated) "
                    "VALUES (?, ?, ?, 'pipeline', ?, '', '[]', NULL, ?, NULL, NULL, 0, '', '{}', 0, 1, ?, ?)",
                    (line_id, project_id, repo_name, repo_name, concurrency, now, now),
                )
                line_row = decode_line(
                    conn.execute("SELECT * FROM lines WHERE id = ?", (line_id,)).fetchone()
                )
                events.emit(
                    "line.changed",
                    f"line:{line_id}",
                    cls="durable",
                    ver=1,
                    data={"fields": _create_fields_diff(line_row, LINE_EVENT_FIELDS)},
                    connection=conn,
                )
                if has_task_line:
                    conn.execute(
                        "UPDATE tasks SET line = ? WHERE repo = ? AND (line IS NULL OR line = '')",
                        (line_id, repo_name),
                    )
                conn.execute(
                    "UPDATE runs SET line = ? WHERE kind = 'pipeline' AND repo = ? AND (line IS NULL OR line = '')",
                    (line_id, repo_name),
                )


def get_project(conn, project_id: str) -> dict | None:
    if not available(conn):
        return None
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return decode_project(row)


def list_projects(conn) -> list[dict]:
    if not available(conn):
        return []
    rows = conn.execute("SELECT * FROM projects ORDER BY (rank IS NULL), rank ASC, id ASC").fetchall()
    return [decode_project(r) for r in rows]


def get_line(conn, line_id: str) -> dict | None:
    if not available(conn):
        return None
    row = conn.execute("SELECT * FROM lines WHERE id = ?", (line_id,)).fetchone()
    return decode_line(row)


def list_lines(conn, *, project: str | None = None, include_archived: bool = False) -> list[dict]:
    if not available(conn):
        return []
    clauses: list[str] = []
    args: list[object] = []
    if not include_archived:
        clauses.append("archived = 0")
    if project:
        clauses.append("project = ?")
        args.append(project)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM lines{where} ORDER BY (rank IS NULL), rank ASC, id ASC",  # noqa: S608
        args,
    ).fetchall()
    return [decode_line(r) for r in rows]


def default_pipeline_line(conn, repo_name: str) -> dict | None:
    if not repo_name or not available(conn):
        return None
    row = conn.execute(
        "SELECT * FROM lines WHERE repo = ? AND kind = 'pipeline' AND archived = 0 "
        "ORDER BY (rank IS NULL), rank ASC, id ASC LIMIT 1",
        (repo_name,),
    ).fetchone()
    return decode_line(row)


def line_is_paused(conn, line_id: str) -> bool:
    if not line_id or not available(conn):
        return False
    row = conn.execute("SELECT paused FROM lines WHERE id = ?", (line_id,)).fetchone()
    return bool(row and row["paused"])


def get_view(conn, view_id: str) -> dict | None:
    if not available(conn):
        return None
    row = conn.execute("SELECT * FROM views WHERE id = ?", (view_id,)).fetchone()
    return decode_view(row)


def list_views(conn, *, project: str | None = None) -> list[dict]:
    if not available(conn):
        return []
    if project:
        rows = conn.execute(
            "SELECT * FROM views WHERE project = ? ORDER BY (rank IS NULL), rank ASC, id ASC",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM views ORDER BY (rank IS NULL), rank ASC, id ASC"
        ).fetchall()
    return [decode_view(r) for r in rows]


def entity_history(
    conn,
    subject: str,
    *,
    limit: int = 100,
    after: int | None = None,
) -> tuple[list[dict], int | None]:
    epoch = str(conn.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0])
    limit = max(1, min(500, int(limit)))
    if after is not None:
        rows = conn.execute(
            "SELECT * FROM events_log WHERE subject = ? AND class = 'durable' AND seq > ? "
            "ORDER BY seq ASC LIMIT ?",
            (subject, int(after), limit + 1),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events_log WHERE subject = ? AND class = 'durable' "
            "ORDER BY seq ASC LIMIT ?",
            (subject, limit + 1),
        ).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    out: list[dict] = []
    for r in page:
        env = events.envelope(conn, dict(r), epoch)
        out.append({
            "seq": int(env["seq"]),
            "id": env["id"],
            "at": env["at"],
            "actor": env["actor"],
            "actor_name": env.get("actor_name"),
            "ver": env.get("ver"),
            "data": env["data"],
        })
    next_after = out[-1]["seq"] if has_more and out else None
    return out, next_after


def default_pipeline_line_read(repo_name: str) -> dict | None:
    with reader() as conn:
        return default_pipeline_line(conn, repo_name)
