from __future__ import annotations

import re

from arete.store.migrations.m0002_foundation_time_shape import TIME_GLOB

PHASE = "contract"

_OLD_TASK_RE = re.compile(r"(?<![\w-])(t-[0-9a-f]{4,})(?![\w-])", re.IGNORECASE)
_WAIT_LINE_RE = re.compile(
    r"^([ \t]*(?:[-*+][ \t]+|#{1,6}[ \t]+)?(?:\*\*|__)?"
    r"(?:waits[ \t]+on|blocked[ \t]+by)\b(?:\*\*|__)?[ \t]*:?[ \t]*(?:\*\*|__)?)(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def _rewrite_wait_body(body: str, id_to_num: dict[str, int]) -> tuple[str, bool]:
    if not body or not id_to_num:
        return body, False
    changed = False

    def _replace_line(match: re.Match[str]) -> str:
        nonlocal changed
        prefix, rest = match.group(1), match.group(2)

        def _replace_id(id_match: re.Match[str]) -> str:
            nonlocal changed
            token = id_match.group(1).lower()
            if token in id_to_num:
                changed = True
                return f"T-{id_to_num[token]}"
            return id_match.group(0)

        return prefix + _OLD_TASK_RE.sub(_replace_id, rest)

    rewritten = _WAIT_LINE_RE.sub(_replace_line, body)
    return rewritten, changed


def _fallback_owner(conn) -> str:
    from contextlib import suppress

    from arete import config

    with suppress(OSError, ValueError):
        cfg, _ = config.load_or_problem()
        if cfg and cfg.automation_payer:
            return cfg.automation_payer
    row = conn.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, rowid LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else "hand"


def _owner_principal(conn) -> str:
    row = conn.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, rowid LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else "hand"


def _is_person(conn, actor: str | None) -> bool:
    if not actor:
        return False
    if actor == "owner":
        return True
    return conn.execute(
        "SELECT 1 FROM principals WHERE id = ? AND kind = 'human'", (actor,)
    ).fetchone() is not None


def _task_creator(conn, old_id: str, num: int) -> str | None:
    row = conn.execute(
        "SELECT actor FROM events_log WHERE subject IN (?, ?, ?) AND kind = 'task.created' "
        "ORDER BY seq ASC LIMIT 1",
        (f"task:{old_id}", f"task:{num}", f"task:T-{num}"),
    ).fetchone()
    if row and _is_person(conn, row[0]):
        return str(row[0])
    return None


def _comment_actor(conn, num: int, rowid: int) -> str:
    for subject in (f"task_comment:{num}", f"comment:{num}", f"task_comment:{rowid}"):
        row = conn.execute(
            "SELECT actor FROM events_log WHERE subject = ? AND kind IN ('comment.changed', 'task_comment.created', 'task_comment.changed') "
            "ORDER BY seq DESC LIMIT 1",
            (subject,),
        ).fetchone()
        if row and row[0] and row[0] != "arete":
            return str(row[0])
    return "agent:build"


def apply(conn) -> None:
    from arete import config, events, principals, util
    from arete.store import tasks as task_store

    # 1. Final idempotent catch-up before destructive contract.
    task_store.backfill_identities(conn)

    # Ensure every task and comment has a num before building maps.
    conn.execute("UPDATE tasks SET num = rowid WHERE num IS NULL")
    conn.execute("UPDATE task_comments SET num = rowid WHERE num IS NULL")

    now = util.iso()
    default_owner = _fallback_owner(conn)
    owner_id = _owner_principal(conn)

    old_tasks = [dict(r) for r in conn.execute("SELECT rowid AS _rowid, * FROM tasks ORDER BY num")]
    id_to_num: dict[str, int] = {}
    legacy_to_num: dict[str, int] = {}
    for row in old_tasks:
        num = int(row["num"])
        raw_id = str(row["id"])
        id_to_num[raw_id] = num
        id_to_num[raw_id.lower()] = num
        id_to_num[f"T-{num}"] = num
        if re.fullmatch(r"t-[0-9a-f]{4,}", raw_id, re.IGNORECASE):
            legacy_to_num[raw_id] = num
            legacy_to_num[raw_id.lower()] = num

    old_comments = [dict(r) for r in conn.execute("SELECT rowid AS _rowid, * FROM task_comments ORDER BY num")]

    # Drop expand triggers and old indexes.
    conn.execute("DROP TRIGGER IF EXISTS tasks_num_fill")
    conn.execute("DROP TRIGGER IF EXISTS task_comments_num_fill")
    conn.execute("DROP TRIGGER IF EXISTS tasks_time_insert")
    conn.execute("DROP TRIGGER IF EXISTS tasks_time_update")
    conn.execute("DROP TRIGGER IF EXISTS task_comments_time_insert")
    conn.execute("DROP TRIGGER IF EXISTS task_comments_time_update")
    conn.execute("DROP INDEX IF EXISTS tasks_repo")
    conn.execute("DROP INDEX IF EXISTS tasks_num")
    conn.execute("DROP INDEX IF EXISTS task_comments_num")
    conn.execute("DROP INDEX IF EXISTS task_comments_task")

    # 2. Create new STRICT tasks table.
    conn.execute(f"""
        CREATE TABLE tasks_new (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'feat',
            state TEXT NOT NULL DEFAULT 'ready'
                CHECK (state IN ('intake', 'ready', 'running', 'needs-you', 'review', 'done', 'closed')),
            parent INTEGER,
            project TEXT,
            line TEXT,
            fault INTEGER NOT NULL DEFAULT 0,
            fault_detail TEXT,
            priority INTEGER,
            rank REAL,
            owner TEXT NOT NULL,
            origin TEXT NOT NULL,
            delegate TEXT,
            estimate TEXT,
            answer TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL CHECK (created GLOB '{TIME_GLOB}'),
            updated TEXT NOT NULL CHECK (updated GLOB '{TIME_GLOB}')
        ) STRICT
    """)

    rewritten_bodies: list[tuple[int, dict]] = []
    for row in old_tasks:
        num = int(row["num"])
        raw_id = str(row["id"])
        state, fault = config.canonical_task_state(str(row.get("state") or "todo"), int(row.get("fault") or 0))
        raw_parent = row.get("parent")
        parent_num: int | None = None
        if isinstance(raw_parent, int):
            parent_num = raw_parent
        elif isinstance(raw_parent, str) and raw_parent.strip():
            p = raw_parent.strip()
            if p in id_to_num:
                parent_num = id_to_num[p]
            elif p.lower() in id_to_num:
                parent_num = id_to_num[p.lower()]
            elif m := re.fullmatch(r"T-([0-9]+)", p):
                parent_num = int(m.group(1))
        owner = (row.get("owner") or "").strip() or _task_creator(conn, raw_id, num) or default_owner
        if owner == "owner":
            owner = owner_id
        origin = (row.get("origin") or "").strip()
        if not origin:
            origin = owner if owner.startswith("u:") else f"u:{owner}"
        created = (row.get("created") or "").strip() or (row.get("updated") or "").strip() or now
        updated = (row.get("updated") or "").strip() or created
        body = row.get("body") or ""
        new_body, body_changed = _rewrite_wait_body(body, legacy_to_num)
        version = int(row.get("version") or 1)
        new_row = {
            "id": num,
            "repo": row.get("repo") or "",
            "title": row.get("title") or "",
            "body": new_body,
            "kind": row.get("kind") or "feat",
            "state": state,
            "parent": parent_num,
            "project": row.get("project"),
            "line": row.get("line"),
            "fault": fault,
            "fault_detail": row.get("fault_detail"),
            "priority": row.get("priority"),
            "rank": row.get("rank"),
            "owner": owner,
            "origin": origin,
            "delegate": row.get("delegate"),
            "estimate": row.get("estimate"),
            "answer": row.get("answer"),
            "version": version,
            "created": created,
            "updated": updated,
        }
        conn.execute(
            "INSERT INTO tasks_new (id, repo, title, body, kind, state, parent, project, line, fault, fault_detail, "
            "priority, rank, owner, origin, delegate, estimate, answer, version, created, updated) "
            "VALUES (:id, :repo, :title, :body, :kind, :state, :parent, :project, :line, :fault, :fault_detail, "
            ":priority, :rank, :owner, :origin, :delegate, :estimate, :answer, :version, :created, :updated)",
            new_row,
        )
        if body_changed:
            rewritten_bodies.append((num, new_row))

    # 3. Create new STRICT task_comments table.
    conn.execute(f"""
        CREATE TABLE task_comments_new (
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            audience TEXT NOT NULL DEFAULT 'internal',
            ref INTEGER,
            remote_id TEXT,
            outbox_id INTEGER,
            version INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL CHECK (created GLOB '{TIME_GLOB}'),
            updated TEXT NOT NULL CHECK (updated GLOB '{TIME_GLOB}')
        ) STRICT
    """)

    for row in old_comments:
        cnum = int(row["num"])
        raw_task = str(row.get("task") or "")
        tnum = id_to_num.get(raw_task) or id_to_num.get(raw_task.lower())
        if tnum is None:
            if m := re.fullmatch(r"T-([0-9]+)", raw_task):
                tnum = int(m.group(1))
            elif raw_task.isdigit():
                tnum = int(raw_task)
            else:
                continue
        author = str(row.get("author") or "")
        if author == "owner":
            author = owner_id
        elif author == "arete":
            author = _comment_actor(conn, cnum, int(row["_rowid"]))
        elif not author:
            author = "agent:build"
        created = (row.get("created") or "").strip() or (row.get("ts") or "").strip() or now
        updated = (row.get("updated") or "").strip() or created
        audience = (row.get("audience") or "").strip() or "internal"
        conn.execute(
            "INSERT INTO task_comments_new (id, task_id, author, body, audience, ref, remote_id, outbox_id, "
            "version, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cnum, tnum, author, row.get("body") or "", audience, row.get("ref"), row.get("remote_id"),
             row.get("outbox_id"), int(row.get("version") or 1), created, updated),
        )

    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.execute("CREATE INDEX tasks_state ON tasks (state, created)")

    conn.execute("DROP TABLE task_comments")
    conn.execute("ALTER TABLE task_comments_new RENAME TO task_comments")
    conn.execute("CREATE INDEX task_comments_task ON task_comments (task_id, id)")

    # 4. Preserve task history seqs by rewriting legacy task:<old_id> subjects to task:<num>.
    for old_id, num in legacy_to_num.items():
        if old_id != old_id.lower():
            continue
        conn.execute("UPDATE events_log SET subject = ? WHERE subject = ?", (f"task:{num}", f"task:{old_id}"))
        conn.execute("UPDATE events_log SET task = ? WHERE task = ?", (f"T-{num}", old_id))

    # 5. Rewrite body waits -> task_links and emit task.changed for rewritten bodies.
    with principals.acting("hand"):
        for num, row in rewritten_bodies:
            events.emit(
                "task.changed",
                f"task:{num}",
                cls="derived",
                ver=row["version"],
                task=f"T-{num}",
                data={**row, "id": f"T-{num}"},
                connection=conn,
            )
    for row in conn.execute("SELECT id, body FROM tasks"):
        if row["body"]:
            task_store.TaskStore.sync_body_links(conn, int(row["id"]), row["body"])

    # 6. Rewrite incident_repairs.task and zte_deliveries.task from t-<hex> to T-<num>.
    for repair in conn.execute("SELECT id, task FROM incident_repairs WHERE task LIKE 't-%'").fetchall():
        key = str(repair["task"]).strip().lower()
        if key in legacy_to_num:
            conn.execute("UPDATE incident_repairs SET task = ? WHERE id = ?",
                         (f"T-{legacy_to_num[key]}", repair["id"]))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'zte_deliveries'").fetchone():
        for zrow in conn.execute("SELECT rowid, task FROM zte_deliveries WHERE task LIKE 't-%'").fetchall():
            key = str(zrow["task"]).strip().lower()
            if key in legacy_to_num:
                conn.execute("UPDATE zte_deliveries SET task = ? WHERE rowid = ?",
                             (f"T-{legacy_to_num[key]}", zrow["rowid"]))

    # 7. Enforce non-null task_id on pipeline runs.
    conn.execute("""
        CREATE TRIGGER runs_task_required BEFORE INSERT ON runs
        WHEN NEW.kind = 'pipeline' AND NEW.task_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'pipeline runs require task_id');
        END
    """)

    # 8. Advance oldest_writer to contract version.
    conn.execute(
        "UPDATE schema_meta SET value = CASE WHEN CAST(COALESCE(value, '0') AS INTEGER) < 59 "
        "THEN '59' ELSE value END WHERE key = 'oldest_writer'"
    )
