from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

from .. import events, refusals, util
from .db import in_write, reader, tx

TABLES = {}
FIELDS = {
    "runs": frozenset(("kind", "repo", "issue", "loop", "loop_kind", "pid", "started", "finished", "status",
                       "summary", "cost_usd", "tokens_in", "tokens_out", "pr_url", "branch", "only_step",
                       "files_purged", "records_purged", "mined_until", "stop_asked", "ab_held", "active_seconds",
                       "verify_seconds", "verify_open_seconds", "repair_calls", "measured_at", "schema_version",
                       "label_pending", "landed", "device", "attempts", "model", "runner", "log", "trigger", "queue",
                       "grp", "lock", "moved", "wait_minutes", "engine_sha", "failure", "dry_run",
                       "environment_retry_of", "config_commit", "resolved_sha", "continues", "chain", "restart",
                       "started_by", "payer", "seq", "adw", "adw_version", "adw_rule",
                       "task_id", "issue_key", "capabilities", "line", "lease_id")),
    "tasks": frozenset(("repo", "title", "body", "kind", "state", "parent", "created", "updated",
                        "num", "project", "line", "fault", "fault_detail", "priority", "rank",
                        "owner", "origin", "delegate", "estimate", "answer")),
    "task_comments": frozenset(("task", "task_id", "author", "body", "ts", "num", "audience", "ref",
                                "remote_id", "outbox_id", "created", "updated")),
}
IDENTITY = {"runs": "id", "tasks": "id", "task_comments": "rowid"}
SUBJECT = {"runs": "run", "tasks": "task", "task_comments": "task_comment"}


def _identity_column(c, table: str) -> str:
    if table == "task_comments":
        cols = {r[1] for r in c.execute("PRAGMA table_info(task_comments)")}
        if "id" in cols:
            return "id"
    return IDENTITY[table]


class VersionConflict(refusals.Conflict):
    code = "version_conflict"

    def __init__(self, current):
        super().__init__("This object changed; refresh it before trying again.")
        self.current = current


def validate_version(value):
    if value is not None and (type(value) is not int or value < 1):
        raise refusals.Invalid("base_version must be a positive integer")


def current(c, table, identity):
    if table not in FIELDS:
        raise ValueError(f"invalid versioned table: {table!r}")
    column = _identity_column(c, table)
    row = c.execute(f"SELECT {column}, * FROM {table} WHERE {column} = ?", (identity,)).fetchone()  # noqa: S608
    if not row:
        return None
    d = dict(row)
    if table == "runs" and "issue" not in d and "issue_key" in d:
        d["issue"] = d["issue_key"]
    return d


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
    if table not in FIELDS:
        raise ValueError(f"invalid versioned table: {table!r}")
    if set(fields) - FIELDS[table]:
        raise ValueError(f"invalid {table} fields: {', '.join(sorted(set(fields) - FIELDS[table]))}")


def emit(c, table, identity, row, kind):
    data = row
    subject_id = identity
    task_tag = row.get("issue_key", row.get("issue", row.get("task", row.get("id"))))
    if table == "runs":
        data = {**row, "run": identity}
        if row.get("task_id") is not None:
            task_tag = f"T-{row['task_id']}"
            task_cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            if "num" in task_cols:
                task = c.execute("SELECT id, num, state, updated, version FROM tasks WHERE num = ?",
                                 (row["task_id"],)).fetchone()
            else:
                task = c.execute("SELECT id, state, updated, version FROM tasks WHERE id = ?",
                                 (row["task_id"],)).fetchone()
            if task:
                td = dict(task)
                td["id"] = f"T-{td.get('num', td['id'])}"
                data["board_task"] = td
        else:
            issue = row.get("issue_key") or row.get("issue")
            if isinstance(issue, str) and issue.startswith("t-"):
                task = c.execute("SELECT id, state, updated, version FROM tasks WHERE id = ? AND repo = ?",
                                 (issue, row["repo"])).fetchone()
                if task:
                    data["board_task"] = dict(task)
    elif table == "tasks":
        num = row.get("num") if row.get("num") is not None else (row["id"] if isinstance(row.get("id"), int) else None)
        if num is not None:
            subject_id = num
            task_tag = f"T-{num}"
            data = {**row, "id": f"T-{num}"}
    elif table == "task_comments":
        if row.get("task_id") is not None:
            task_tag = f"T-{row['task_id']}"
        elif row.get("num") is not None:
            subject_id = row["num"]
    return events.emit(kind, f"{SUBJECT[table]}:{subject_id}", cls="derived", ver=row["version"],
                       task=task_tag, data=data)


def insert(table, fields, identity=None, kind=None):
    fields_for(table, fields)
    values = dict(fields)
    with atomic() as c:
        column = _identity_column(c, table)
        if identity is not None:
            values[column] = identity
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
        column = _identity_column(c, table)
        row = current(c, table, identity)
        if row is None:
            return None
        if base_version is not None and row["version"] != base_version:
            raise VersionConflict(row)
        status = "status" if table == "runs" else "state"
        if expect and row[status] not in expect:
            return None
        where, args = f"{column} = ?", [identity]
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
