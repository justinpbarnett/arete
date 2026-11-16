from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatchcase

from . import backup, install, paths, util
from .store import approvals as approvals_store
from .store import chats as chats_store
from .store import db as foundation
from .store import insights as insights_store
from .store import loops as loops_store
from .store import runs as run_store

SAMPLE_LIMIT = 5
YOUNG_MINUTES = 10
CHAT_GRACE_SECONDS = 300
APPROVAL_HOURS = 1
BACKUP_MAX_HOURS = 3
CHAT_STEPS = "chat"
UNFOLDERED_KINDS = ("session",)
UNRECORDED_FOLDERS = ("chat",)
UNRECORDED_PATTERNS = ("eva-*-prompts",)
ORPHAN_TABLES = ("steps", "evals", "feed", "verdicts")
NAMES = ("cost", "orphans", "chat", "folders", "stuck", "store", "backup")


@dataclass
class Result:
    name: str
    severity: str
    count: int
    ids: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def failing(self) -> bool:
        return self.severity == "fail" and self.count > 0


@dataclass
class Listing:
    folders: dict[str, float] | None = None
    error: Exception | None = None


def result(name: str, severity: str, ids: list[str], detail: str = "", count: int | None = None) -> Result:
    return Result(name, severity, len(ids) if count is None else count, ids[:SAMPLE_LIMIT], detail)


def status(r: Result) -> str:
    return "OK" if r.count == 0 else ("FAIL" if r.severity == "fail" else "INFO")


def summary(r: Result) -> str:
    text = f"{r.detail} [{', '.join(r.ids)}]" if r.ids else r.detail
    return " ".join(text.split())


def line(r: Result) -> str:
    return f"{status(r):<4} {r.name:<8} {r.count:>4}  {summary(r)}"


def older_than(stamp: str | None, cutoff: datetime) -> bool:
    if not stamp:
        return True
    try:
        return util.parse_iso(stamp) < cutoff
    except ValueError:
        return True


def list_folders() -> Listing:
    try:
        with os.scandir(paths.RUNS_DIR) as entries:
            return Listing({e.name: e.stat(follow_symlinks=False).st_mtime for e in entries
                            if e.is_dir(follow_symlinks=False)})
    except FileNotFoundError:
        return Listing({})
    except OSError as e:
        return Listing(error=e)


def check_cost(conn: sqlite3.Connection) -> Result:
    drift = run_store.cost_drift(connect=lambda: nullcontext(conn))
    first = drift[0] if drift else None
    detail = (f"{first['id']} records ${first['run_usd']:.2f} but its steps sum to ${first['steps_usd']:.2f}"
              if first else "run costs match their steps")
    return result("cost", "fail", [d["id"] for d in drift], detail)


def check_orphans(conn: sqlite3.Connection) -> Result:
    total, ids, per_table = 0, [], []
    for table in ORPHAN_TABLES:
        owner = run_store if table in ("steps", "feed") else insights_store
        n, rows = owner.orphan_rows(conn, table, SAMPLE_LIMIT if len(ids) < SAMPLE_LIMIT else 0)
        if n:
            per_table.append(f"{n} in {table}")
        total += n
        if len(ids) < SAMPLE_LIMIT:
            ids += [f"{table}#{r[0]}:{r[1]}" for r in rows]
    detail = ", ".join(per_table) + " with no runs row" if per_table else "every row names a run"
    return result("orphans", "fail", ids, detail, total)


def check_chat(conn: sqlite3.Connection) -> Result:
    rows = run_store.pseudo_chat_steps(conn, CHAT_STEPS)
    return result("chat", "info", [f"steps#{r[0]}" for r in rows],
                  "steps filed under the pseudo run 'chat'" if rows else "no steps under 'chat'")


def unrecorded(name: str) -> bool:
    return name in UNRECORDED_FOLDERS or any(fnmatchcase(name, p) for p in UNRECORDED_PATTERNS)


def check_folders(conn: sqlite3.Connection, listing: Listing, now: datetime) -> Result:
    if listing.error:
        raise listing.error
    cutoff = now - timedelta(minutes=YOUNG_MINUTES)
    rows = run_store.folder_rows(conn)
    known = {r["id"] for r in rows}
    stray = sorted(n for n, mtime in listing.folders.items()
                   if n not in known and not unrecorded(n) and mtime < cutoff.timestamp())
    reasons: dict[str, list[str]] = {}
    for r in rows:
        if r["files_purged"] or not older_than(r["started"], cutoff):
            continue
        if r["kind"] not in UNFOLDERED_KINDS and r["id"] not in listing.folders:
            reasons.setdefault(r["id"], []).append("no folder")
        if r["log"] and not paths.expand(r["log"]).exists():
            reasons.setdefault(r["id"], []).append("log missing")
    ids = stray + sorted(reasons)
    parts = []
    if stray:
        parts.append(f"{len(stray)} folders without a row")
    for why in ("no folder", "log missing"):
        n = sum(why in v for v in reasons.values())
        if n:
            parts.append(f"{n} rows with {why}")
    return result("folders", "fail", ids, ", ".join(parts) or "folders and rows agree")


def check_stuck(conn: sqlite3.Connection, now: datetime) -> Result:
    from . import chat

    chat_cutoff = util.iso(now - timedelta(seconds=chat.TIMEOUT_SECONDS + CHAT_GRACE_SECONDS))
    approval_cutoff = util.iso(now - timedelta(hours=APPROVAL_HOURS))
    chats = [f"chat#{r[0]}" for r in chats_store.stale_busy(conn, chat_cutoff)]
    queue = [f"queue#{r[0]}" for r in loops_store.settled_claims(conn)]
    approvals = [f"approval#{r[0]}" for r in approvals_store.stale_approved(conn, approval_cutoff)]
    parts = [f"{len(v)} {label}" for v, label in ((chats, "busy chats"), (queue, "queue claims on settled runs"),
                                                 (approvals, "approvals left approved")) if v]
    return result("stuck", "fail", chats + queue + approvals, ", ".join(parts) or "nothing is stuck")


def check_store(conn: sqlite3.Connection) -> Result:
    messages = foundation.quick_check(conn)
    if messages == ["ok"]:
        return Result("store", "fail", 0, [], "quick_check ok")
    return Result("store", "fail", len(messages) or 1, [], f"quick_check: {(messages or ['no answer'])[0]}")


def check_backup(now: datetime) -> Result:
    newest = backup.newest_snapshot()
    if newest is None:
        if install.serve_installed():
            return result("backup", "fail", [], "no snapshot of arete.db yet, though serve is installed", 1)
        return result("backup", "info", [], "no snapshot yet; serve is not installed", 1)
    hours = (now.timestamp() - newest.stat().st_mtime) / 3600
    recorded = backup.read_last().get("quick_check")
    if not isinstance(recorded, str) or not recorded:
        return result("backup", "fail", [newest.name], f"no recorded quick_check, {hours:.1f} hours old", 1)
    if recorded != "ok":
        return result("backup", "fail", [newest.name], f"recorded quick_check: {recorded}", 1)
    if hours > BACKUP_MAX_HOURS:
        return result("backup", "fail", [newest.name], f"newest snapshot is {hours:.1f} hours old", 1)
    return Result("backup", "fail", 0, [], f"{newest.name}, {hours:.1f} hours old, quick_check ok")


def failed(name: str, error: Exception) -> Result:
    return Result(name, "fail", 1, [], f"{type(error).__name__}: {error}")


def run() -> list[Result]:
    now = util.now()
    listing = list_folders()
    out: list[Result] = []
    with ExitStack() as stack:
        conn, opened = None, None
        try:
            conn = stack.enter_context(foundation.reader())
        except Exception as e:
            conn, opened = None, e

        def guarded(name: str, check: Callable[[], Result]) -> None:
            try:
                if name != "backup" and conn is None:
                    raise opened
                out.append(check())
            except Exception as e:
                out.append(failed(name, e))

        guarded("cost", lambda: check_cost(conn))
        guarded("orphans", lambda: check_orphans(conn))
        guarded("chat", lambda: check_chat(conn))
        guarded("folders", lambda: check_folders(conn, listing, now))
        guarded("stuck", lambda: check_stuck(conn, now))
        guarded("store", lambda: check_store(conn))
        guarded("backup", lambda: check_backup(now))
    return out
