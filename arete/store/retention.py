from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .. import paths, record, retention, run_lines, util
from . import approvals, devices, events, idempotency, incidents, loops, orchestration, runs, writes
from .db import (
    RECORD_TURNS_SCHEMA,
    RUN_RETENTION_SCHEMA,
    RUNS_LINEAGE_SCHEMA,
    SPEC_DELIVERY_SCHEMA,
    WORKER_EXCHANGE_SCHEMA,
    migration_applied,
    tx,
)

GB = 10 ** 9
LIVE_SECONDS = 3600
PROTECTED_STATUSES = frozenset(("queued", "running", "pr", "needs-info"))
FOLDER_RELEASING_PROPOSALS = frozenset(("merged", "rejected", "skipped", "filed", "superseded"))
RECORD_RELEASING_PROPOSALS = frozenset(approvals.RELEASING_PROPOSALS)


@dataclass(frozen=True)
class Measured:
    runs_bytes: int
    folder_bytes: dict[str, int]
    record_bytes: dict[str, int]
    record_written: dict[str, float]
    continues_edges: dict[str, str]


@dataclass(frozen=True)
class RowsPruned:
    rows: int
    queued_bytes: int
    remaining_bytes: int
    over_budget: bool


@dataclass(frozen=True)
class FilesPruned:
    folders: int
    records: int
    blobs: int
    size: int
    errors: tuple[str, ...]


def _tree_stats(folder: Path) -> tuple[int, float]:
    size = 0
    latest = 0.0
    for root, _, names in os.walk(folder, followlinks=False):
        for name in names:
            entry = Path(root) / name
            try:
                info = os.lstat(entry)
            except OSError:
                continue
            if os.path.islink(entry):
                continue
            size += info.st_size
            if info.st_mtime > latest:
                latest = info.st_mtime
    if latest == 0.0:
        with contextlib.suppress(OSError):
            latest = folder.stat().st_mtime
    return size, latest


def _continues_of(run_folder: Path) -> str:
    log = run_folder / run_lines.FILE
    if log.is_symlink() or not log.is_file():
        return ""
    try:
        lines = run_lines.whole(log.read_bytes())
    except OSError:
        return ""
    for line in lines:
        if line.get("event") == "route":
            parent = line.get("continues")
            return str(parent) if parent else ""
    return ""


def measure() -> Measured:
    runs_bytes = 0
    folder_bytes: dict[str, int] = {}
    record_bytes: dict[str, int] = {}
    record_written: dict[str, float] = {}
    continues_edges: dict[str, str] = {}
    if not paths.RUNS_DIR.exists():
        return Measured(0, folder_bytes, record_bytes, record_written, continues_edges)
    for entry in sorted(paths.RUNS_DIR.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_file():
            with contextlib.suppress(OSError):
                runs_bytes += entry.lstat().st_size
            continue
        if not entry.is_dir():
            continue
        run_id = entry.name
        size, _ = _tree_stats(entry)
        folder_bytes[run_id] = size
        runs_bytes += size
        rec_dir = entry / "record"
        if not rec_dir.is_symlink() and rec_dir.is_dir():
            rec_size, rec_mtime = _tree_stats(rec_dir)
            record_bytes[run_id] = rec_size
            record_written[run_id] = rec_mtime
        if parent := _continues_of(entry):
            continues_edges[run_id] = parent
    return Measured(runs_bytes, folder_bytes, record_bytes, record_written, continues_edges)


def _cited_runs(c, releasing: frozenset[str], candidate_ids: set[str]) -> set[str]:
    if not candidate_ids:
        return set()
    marks = ",".join("?" * len(releasing))
    rows = c.execute(
        f"SELECT run_id, evidence FROM proposals WHERE status IS NULL OR status NOT IN ({marks})",  # noqa: S608
        tuple(sorted(releasing)),
    ).fetchall()
    if not rows:
        return set()
    cited: set[str] = set()
    for row in rows:
        direct = row["run_id"] or ""
        if direct in candidate_ids:
            cited.add(direct)
        evidence = row["evidence"] or ""
        if evidence:
            for rid in candidate_ids:
                if rid in evidence:
                    cited.add(rid)
    return cited


def _chain_kept(c, recent_runs: set[str], continues_edges: dict[str, str]) -> set[str]:
    if not recent_runs:
        return set()
    adj: dict[str, set[str]] = defaultdict(set)
    for child, parent in continues_edges.items():
        if child and parent:
            adj[child].add(parent)
            adj[parent].add(child)
    for row in c.execute("SELECT root, current_run, data FROM orchestrations"):
        members: set[str] = set()
        if row["root"]:
            members.add(str(row["root"]))
        if row["current_run"]:
            members.add(str(row["current_run"]))
        try:
            data = json.loads(row["data"] or "{}")
        except ValueError:
            data = {}
        for item in data.get("runs") or ():
            if isinstance(item, str) and item:
                members.add(item)
        if len(members) > 1:
            anchor = next(iter(members))
            for other in members:
                if other != anchor:
                    adj[anchor].add(other)
                    adj[other].add(anchor)
    by_chat: dict[int, list[str]] = defaultdict(list)
    for row in c.execute("SELECT chat_id, run_id FROM chat_sessions"):
        if row["run_id"]:
            by_chat[row["chat_id"]].append(str(row["run_id"]))
    for members_list in by_chat.values():
        if len(members_list) > 1:
            anchor = members_list[0]
            for other in members_list[1:]:
                adj[anchor].add(other)
                adj[other].add(anchor)
    if migration_applied(RUNS_LINEAGE_SCHEMA, c):
        by_chain: dict[str, list[str]] = defaultdict(list)
        for row in c.execute("SELECT id, chain, continues FROM runs WHERE COALESCE(chain, '') != '' OR COALESCE(continues, '') != ''"):
            rid = str(row["id"])
            if row["chain"]:
                by_chain[str(row["chain"])].append(rid)
            if row["continues"]:
                parent = str(row["continues"])
                adj[rid].add(parent)
                adj[parent].add(rid)
        for members_list in by_chain.values():
            if len(members_list) > 1:
                anchor = members_list[0]
                for other in members_list[1:]:
                    adj[anchor].add(other)
                    adj[other].add(anchor)
    visited = set(recent_runs)
    queue = deque(recent_runs)
    while queue:
        current = queue.popleft()
        for neighbor in adj.get(current, ()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return visited


def prune_rows(policy: retention.Policy, measured: Measured) -> RowsPruned:
    now_dt = util.now()
    now = util.iso(now_dt)
    now_ts = now_dt.timestamp()
    firings_since = util.iso(now_dt - timedelta(days=policy.firings.days))
    folders_since = util.iso(now_dt - timedelta(days=policy.folders.days))
    records_since = util.iso(now_dt - timedelta(days=policy.records.days))
    records_since_ts = now_ts - policy.records.days * 86400
    recent_record_ts = now_ts - LIVE_SECONDS
    limit_bytes = policy.records.max_gb * GB

    with tx() as c:
        has_records_purged = migration_applied(RUN_RETENTION_SCHEMA, c)
        has_turn_index = migration_applied(RECORD_TURNS_SCHEMA, c)
        rows_removed = 0
        queued_reason: dict[str, str] = {}
        queued_bytes = 0
        for existing in c.execute("SELECT run_id, reason FROM run_purges").fetchall():
            rid, reason = existing["run_id"], existing["reason"]
            queued_reason[rid] = reason
            if reason in ("firing", "lines"):
                queued_bytes += measured.folder_bytes.get(rid, 0)
            else:
                queued_bytes += measured.record_bytes.get(rid, 0)

        def queue_purge(run_id: str, reason: str) -> None:
            nonlocal queued_bytes
            if reason in ("firing", "lines"):
                c.execute(
                    "INSERT INTO run_purges (run_id, reason, queued) VALUES (?, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET reason = excluded.reason, queued = excluded.queued "
                    "WHERE run_purges.reason = 'record'",
                    (run_id, reason, now),
                )
                prev = queued_reason.get(run_id)
                if prev is None:
                    queued_bytes += measured.folder_bytes.get(run_id, 0)
                    queued_reason[run_id] = reason
                elif prev == "record":
                    queued_bytes += max(0, measured.folder_bytes.get(run_id, 0) - measured.record_bytes.get(run_id, 0))
                    queued_reason[run_id] = reason
            else:
                c.execute("INSERT OR IGNORE INTO run_purges (run_id, reason, queued) VALUES (?, 'record', ?)",
                          (run_id, now))
                if run_id not in queued_reason:
                    queued_bytes += measured.record_bytes.get(run_id, 0)
                    queued_reason[run_id] = "record"

        pinned = {r[0] for r in c.execute("SELECT run_id FROM record_pins")}
        open_asks = {
            r[0]
            for r in c.execute(
                "SELECT DISTINCT run FROM asks WHERE status = 'open' "
                "OR (status = 'answered' AND COALESCE(taken_by, '') = '')"
            )
            if r[0]
        }

        # 1. Firings: delete runs, steps, feed rows and queue folder removal.
        firing_rows = c.execute(
            f"SELECT id FROM runs WHERE {runs.FIRINGS} AND started < ?",  # noqa: S608
            (firings_since,),
        ).fetchall()
        firing_ids = {r["id"] for r in firing_rows}
        firing_cited = _cited_runs(c, FOLDER_RELEASING_PROPOSALS, firing_ids)
        for row in firing_rows:
            rid = row["id"]
            if rid in pinned or rid in open_asks or rid in firing_cited:
                continue
            queue_purge(rid, "firing")
            if has_turn_index:
                rows_removed += c.execute("DELETE FROM turn_index WHERE run_id = ?", (rid,)).rowcount
            rows_removed += c.execute("DELETE FROM steps WHERE run_id = ?", (rid,)).rowcount
            rows_removed += c.execute("DELETE FROM feed WHERE run_id = ?", (rid,)).rowcount
            writes.update("runs", rid, {}, delete=True, kind="run.deleted")
            rows_removed += 1

        # Load remaining runs for folder & record phases.
        rec_col = "records_purged" if has_records_purged else "NULL AS records_purged"
        all_runs = [
            dict(r)
            for r in c.execute(
                f"SELECT id, kind, repo, issue, status, started, finished, files_purged, {rec_col} FROM runs"  # noqa: S608
            ).fetchall()
        ]
        all_ids = {r["id"] for r in all_runs}

        recent_runs = {r["id"] for r in all_runs if r["started"] and r["started"] >= folders_since}
        recent_tickets = {
            (r["repo"], str(r["issue"]))
            for r in all_runs
            if r["started"] and r["started"] >= folders_since and r["repo"] and r["issue"] not in (None, "")
        }
        chain_kept = _chain_kept(c, recent_runs, measured.continues_edges)

        active_workers: set[str] = set()
        if migration_applied(WORKER_EXCHANGE_SCHEMA, c):
            active_workers = {
                str(r[0])
                for r in c.execute(
                    "SELECT DISTINCT j.value FROM orchestration_workers w "
                    "JOIN orchestrations o ON o.root = w.root, json_each(o.data, '$.runs') j "
                    "WHERE w.phase NOT IN ('completed', 'integrated', 'cancelled')"
                )
                if r[0]
            }
        active_specs: set[tuple[str, str]] = set()
        if migration_applied(SPEC_DELIVERY_SCHEMA, c):
            active_specs = {
                (r["repo"], str(r["issue"]))
                for r in c.execute("SELECT repo, issue FROM spec_deliveries WHERE state != 'complete'")
                if r["repo"] and r["issue"] not in (None, "")
            }

        folder_cited = _cited_runs(c, FOLDER_RELEASING_PROPOSALS, all_ids)
        record_cited = _cited_runs(c, RECORD_RELEASING_PROPOSALS, all_ids)

        # 2. Folders / lines past [folders] days.
        lines_purged_now: set[str] = set()
        for r in all_runs:
            rid = r["id"]
            if r["files_purged"] is not None:
                continue
            age_ref = r["finished"] or r["started"] or ""
            if not age_ref or age_ref >= folders_since:
                continue
            if (
                r["status"] in PROTECTED_STATUSES
                or rid in pinned
                or rid in open_asks
                or rid in active_workers
                or rid in folder_cited
                or rid in chain_kept
            ):
                continue
            ticket_key = (r["repo"], str(r["issue"])) if r["repo"] and r["issue"] not in (None, "") else None
            if ticket_key and (ticket_key in recent_tickets or ticket_key in active_specs):
                continue
            queue_purge(rid, "lines")
            rows_removed += c.execute("DELETE FROM feed WHERE run_id = ?", (rid,)).rowcount
            fields = {"files_purged": now}
            if has_records_purged and r["records_purged"] is None:
                fields["records_purged"] = now
            writes.update("runs", rid, fields)
            orchestration.purge(c, rid)
            lines_purged_now.add(rid)

        # 3. Records past [records] days.
        records_purged_now: set[str] = set()
        for r in all_runs:
            rid = r["id"]
            if r["files_purged"] is not None or rid in lines_purged_now:
                continue
            if has_records_purged and r["records_purged"] is not None:
                continue
            if queued_reason.get(rid) in ("firing", "lines"):
                continue
            if r["status"] in PROTECTED_STATUSES or rid in pinned or rid in open_asks or rid in record_cited:
                continue
            rec_mtime = measured.record_written.get(rid)
            if rec_mtime is not None and rec_mtime > recent_record_ts:
                continue
            age_ref = r["finished"] or r["started"] or ""
            is_old = bool(age_ref and age_ref < records_since) or (
                rec_mtime is not None and rec_mtime < records_since_ts
            )
            if not is_old:
                continue
            if rid in measured.record_bytes:
                queue_purge(rid, "record")
            if has_records_purged:
                writes.update("runs", rid, {"records_purged": now})
            records_purged_now.add(rid)

        # 4. Records oldest first while runs/ minus bytes already queued is over max_gb.
        remaining_bytes = max(0, measured.runs_bytes - queued_bytes)
        if remaining_bytes > limit_bytes:
            candidates = [
                r
                for r in all_runs
                if r["id"] in measured.record_bytes
                and r["id"] not in queued_reason
                and r["id"] not in lines_purged_now
                and r["id"] not in records_purged_now
                and r["files_purged"] is None
                and not (has_records_purged and r["records_purged"] is not None)
                and r["status"] not in PROTECTED_STATUSES
                and r["id"] not in pinned
                and r["id"] not in open_asks
                and r["id"] not in record_cited
                and measured.record_written.get(r["id"], 0.0) <= recent_record_ts
            ]
            candidates.sort(key=lambda r: (measured.record_written[r["id"]], r["finished"] or r["started"] or "", r["id"]))
            for r in candidates:
                if remaining_bytes <= limit_bytes:
                    break
                rid = r["id"]
                queue_purge(rid, "record")
                if has_records_purged:
                    writes.update("runs", rid, {"records_purged": now})
                remaining_bytes = max(0, measured.runs_bytes - queued_bytes)
        over_budget = remaining_bytes > limit_bytes

        # 5. Time-keyed tables in the same transaction.
        rows_removed += events.trim(
            derived_days=policy.events_log.derived_days,
            notice_days=policy.events_log.notice_days,
        )
        rows_removed += idempotency.prune(c=c, days=policy.idempotency.days)
        rows_removed += loops.prune(
            c,
            queue_days=policy.queue.days,
            loop_events_days=policy.loop_events.days,
            dead_letters_days=policy.dead_letters.days,
        )
        rows_removed += incidents.prune(
            c,
            days=policy.incidents.days,
            notes_days=policy.incidents.notes_days,
            keep_days=policy.incidents.keep_days,
        )
        rows_removed += devices.prune(c)

    return RowsPruned(
        rows=rows_removed,
        queued_bytes=queued_bytes,
        remaining_bytes=remaining_bytes,
        over_budget=over_budget,
    )


def prune_files() -> FilesPruned:
    queued = runs.purge_queue()
    folders = 0
    records = 0
    blobs = 0
    size = 0
    errors: list[str] = []
    sessions_dir = paths.STATE_DIR / "sessions"
    for item in queued:
        run_id = item["run_id"]
        reason = item["reason"]
        kind = item["kind"]
        try:
            if not record.one_component(run_id):
                raise ValueError(f"unsafe run folder {run_id}")
            if reason in ("firing", "lines"):
                run_folder = paths.RUNS_DIR / run_id
                session_folder = sessions_dir / run_id
                if run_folder.is_symlink() or session_folder.is_symlink():
                    raise ValueError(f"unsafe run folder {run_id}")
                removed_any = False
                if run_folder.exists():
                    measured_size, _ = _tree_stats(run_folder)
                    shutil.rmtree(run_folder)
                    size += measured_size
                    removed_any = True
                if (kind == "session" or session_folder.exists()) and session_folder.exists():
                    measured_size, _ = _tree_stats(session_folder)
                    shutil.rmtree(session_folder)
                    size += measured_size
                    removed_any = True
                if removed_any:
                    folders += 1
                if not run_folder.exists() and not session_folder.exists():
                    runs.forget_purge(run_id)
            elif reason == "record":
                run_folder = paths.RUNS_DIR / run_id
                rec_folder = run_folder / "record"
                if run_folder.is_symlink() or rec_folder.is_symlink():
                    raise ValueError(f"unsafe record folder {run_id}")
                if rec_folder.exists():
                    measured_size, _ = _tree_stats(rec_folder)
                    shutil.rmtree(rec_folder)
                    records += 1
                    size += measured_size
                if not rec_folder.exists():
                    runs.forget_purge(run_id)
            else:
                raise ValueError(f"unknown purge reason {reason!r} for {run_id}")
        except (OSError, ValueError) as e:
            errors.append(f"{run_id}: {type(e).__name__}: {e}")
    return FilesPruned(folders=folders, records=records, blobs=blobs, size=size, errors=tuple(errors))
