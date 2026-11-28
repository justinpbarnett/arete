from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from .. import events, refusals, util
from .db import in_write, reader, tx

TABLES = {}
FIELDS = {
    "runs": frozenset(("kind", "repo", "issue", "loop", "loop_kind", "pid", "started", "finished", "status",
                       "summary", "cost_usd", "tokens_in", "tokens_out", "pr_url", "branch", "only_step",
                       "files_purged", "mined_until", "stop_asked", "ab_held", "active_seconds", "verify_seconds",
                       "verify_open_seconds", "repair_calls", "measured_at", "schema_version", "label_pending",
                       "landed", "device", "attempts", "model", "runner", "log", "trigger", "queue", "grp", "lock",
                       "moved", "wait_minutes", "engine_sha", "failure", "dry_run", "environment_retry_of")),
    "tasks": frozenset(("repo", "title", "body", "kind", "state", "parent", "created", "updated")),
    "task_comments": frozenset(("task", "author", "body", "ts")),
}
IDENTITY = {"runs": "id", "tasks": "id", "task_comments": "rowid"}
SUBJECT = {"runs": "run", "tasks": "task", "task_comments": "task_comment"}


class VersionConflict(refusals.Conflict):
    code = "version_conflict"

    def __init__(self, current):
        super().__init__("This object changed; refresh it before trying again.")
        self.current = current


def validate_version(value):
    if value is not None and (type(value) is not int or value < 1):
        raise refusals.Invalid("base_version must be a positive integer")


def current(c, table, identity):
    column = IDENTITY[table]
    row = c.execute(f"SELECT {column}, * FROM {table} WHERE {column} = ?", (identity,)).fetchone()  # noqa: S608
    return dict(row) if row else None


@contextmanager
def atomic():
    with tx() as c:
        from .events import RECEIPTS

        receipts = RECEIPTS.get()
        count = len(receipts) if receipts is not None else 0
        c.execute("SAVEPOINT versioned_write")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK TO versioned_write")
            if receipts is not None:
                del receipts[count:]
            raise
        finally:
            c.execute("RELEASE versioned_write")


def fields_for(table, fields):
    if set(fields) - FIELDS[table]:
        raise ValueError(f"invalid {table} fields: {', '.join(sorted(set(fields) - FIELDS[table]))}")


def emit(c, table, identity, row, kind):
    data = row
    if table == "runs":
        data = {**row, "run": identity}
        issue = row.get("issue")
        if isinstance(issue, str) and issue.startswith("t-"):
            task = c.execute("SELECT id, state, updated, version FROM tasks WHERE id = ? AND repo = ?",
                             (issue, row["repo"])).fetchone()
            if task:
                data["board_task"] = dict(task)
    return events.emit(kind, f"{SUBJECT[table]}:{identity}", cls="derived", ver=row["version"],
                       task=row.get("issue", row.get("task", row.get("id"))), data=data)


def insert(table, fields, identity=None, kind=None):
    fields_for(table, fields)
    values = dict(fields)
    if identity is not None:
        values[IDENTITY[table]] = identity
    with atomic() as c:
        if table in ("tasks", "runs"):
            from . import factory

            factory.admit(c)
        cursor = c.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({','.join('?' for _ in values)})",  # noqa: S608
                           tuple(values.values()))
        identity = identity if identity is not None else cursor.lastrowid
        row = current(c, table, identity)
        emit(c, table, identity, row, kind or f"{SUBJECT[table]}.created")
        return row


def update(table, identity, fields, *, base_version=None, expect=(), increments=None, kind=None, delete=False):
    validate_version(base_version)
    fields_for(table, fields)
    increments = increments or {}
    if (table != "runs" and increments) or set(increments) - {"cost_usd", "tokens_in", "tokens_out"}:
        raise ValueError("invalid arithmetic fields")
    with atomic() as c:
        row = current(c, table, identity)
        if row is None:
            return None
        if base_version is not None and row["version"] != base_version:
            raise VersionConflict(row)
        status = "status" if table == "runs" else "state"
        if expect and row[status] not in expect:
            return None
        where, args = f"{IDENTITY[table]} = ?", [identity]
        if base_version is not None:
            where += " AND version = ?"
            args.append(base_version)
        if expect:
            where += f" AND {status} IN ({','.join('?' for _ in expect)})"
            args.extend(expect)
        if delete:
            c.execute(f"DELETE FROM {table} WHERE {where}", args)  # noqa: S608
            row = {**row, "version": row["version"] + 1}
        else:
            setters = [f"{key} = ?" for key in fields]
            setters += [f"{key} = COALESCE({key}, 0) + ?" for key in increments]
            setters.append("version = version + 1")
            changed = c.execute(f"UPDATE {table} SET {', '.join(setters)} WHERE {where}",  # noqa: S608
                                (*fields.values(), *increments.values(), *args)).rowcount
            if not changed:
                raise VersionConflict(current(c, table, identity))
            row = current(c, table, identity)
        emit(c, table, identity, row, kind or f"{SUBJECT[table]}.changed")
        return row


def check_run(run_id, base_version=None, *, public=False, for_stop=False):
    validate_version(base_version)
    with tx() as c:
        row = current(c, "runs", run_id)
        deadline = c.execute("SELECT value FROM schema_meta WHERE key = 'versioned_api_grace_deadline'").fetchone()[0]
        if deadline:
            util.parse_iso(deadline)
        if row is None:
            return None
        version = row["version"]
        minimum = row["stop_version"] if for_stop else version
        if ((base_version is not None and not minimum <= base_version <= version)
                or (public and base_version is None and deadline and util.iso() >= deadline)):
            raise VersionConflict(row)
        return row


def activate_grace():
    with reader() as c:
        saved = c.execute("SELECT value FROM schema_meta WHERE key = 'versioned_api_grace_deadline'").fetchone()[0]
    if saved:
        return saved
    with tx() as c:
        deadline = util.iso(util.now() + timedelta(days=30))
        c.execute("UPDATE schema_meta SET value = ? WHERE key = 'versioned_api_grace_deadline' AND value = ''",
                  (deadline,))
        return c.execute("SELECT value FROM schema_meta WHERE key = 'versioned_api_grace_deadline'").fetchone()[0]


def receipt(subject, ver=None, kind=None):
    if in_write():
        return None
    with reader() as c:
        row = c.execute("SELECT seq, subject, ver FROM events_log WHERE subject = ? "
                        "AND (? IS NULL OR ver = ?) AND (? IS NULL OR kind = ?) ORDER BY seq DESC LIMIT 1",
                        (subject, ver, ver, kind, kind)).fetchone()
        if not row:
            return None
        epoch = c.execute("SELECT value FROM schema_meta WHERE key = 'restore_epoch'").fetchone()[0]
        return {"event_seq": f"{epoch}.{row['seq']}", "subject": row["subject"], "ver": row["ver"]}
