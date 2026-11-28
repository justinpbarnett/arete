from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime

from .. import jira, util
from ._rows import insert_sql, lists_read, lists_written
from .db import MONEY_EXPAND_SCHEMA, db, migration_applied, reader

TABLES = {
    "triage_verdicts": "kept",
    "triage_approvals": "kept",
}


TRIAGE_COLUMNS = ("ref", "source", "tracker", "repo", "route", "title", "url", "updated", "ready", "code", "size",
                  "questions", "plan", "why", "cost", "ts", "kind", "repos", "assumptions", "in_sprint", "parent",
                  "closed", "checks", "version")


TRIAGE_LISTS = ("questions", "plan", "repos", "assumptions", "checks")


APPROVAL_COLUMNS = ("ref", "repo", "route", "title", "url", "kind", "size", "plan", "assumptions", "why", "ts", "tracker",
                    "sent_by", "checks")


APPROVAL_LISTS = ("plan", "assumptions", "checks")

VERDICT_COLUMNS = (
    "ref", "ref_id", "source", "tracker", "repo", "route", "ready", "code", "size", "kind",
    "reason", "model", "run_id", "at", "questions", "plan", "assumptions", "checks", "repos",
    "why", "cost", "judged_updated",
)


def iso_updated(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        when = jira.parse_time(text)
    except ValueError:
        return util.iso(datetime.min.replace(tzinfo=UTC))
    if not when.tzinfo:
        when = when.replace(tzinfo=UTC)
    return util.iso(when)


def _json_list_str(value: object) -> str:
    if isinstance(value, str):
        return value if value.strip() else "[]"
    if value is None:
        return "[]"
    return json.dumps(list(value))


def _resolve_ref_id(c, ref: str, *, tracker: str = "", repo: str = "", url: str = "",
                    title: str = "", key_override: str = "", actor: str = "hand",
                    create: bool = False) -> int | None:
    from . import tasks as task_store

    if not task_store.has_identity_expand(c):
        return None
    raw = str(ref or "").strip()
    if not raw:
        return None
    if key_override:
        sys_norm = (tracker or ("jira" if jira.KEY_RE.fullmatch(key_override.upper()) else "github")).strip().lower()
        key_norm = key_override.upper() if sys_norm == "jira" else key_override
        if create:
            ref_dict, _ = task_store.TaskStore.ensure_ref(
                c, sys_norm, key_norm, title=title, url=url, actor=actor, emit_event=False,
            )
            return int(ref_dict["num"])
        found = c.execute("SELECT id FROM refs WHERE system = ? AND key = ? LIMIT 1", (sys_norm, key_norm)).fetchone()
        if found is not None:
            return int(found[0])
    if create:
        return task_store.TaskStore.ref_for_triage(
            c, {"ref": raw, "tracker": tracker, "repo": repo, "url": url, "title": title},
            actor=actor, emit_event=False,
        )
    if jira.KEY_RE.fullmatch(raw.upper()):
        found = c.execute("SELECT id FROM refs WHERE system = 'jira' AND key = ? LIMIT 1", (raw.upper(),)).fetchone()
        if found is not None:
            return int(found[0])
    found = c.execute(
        "SELECT id FROM refs WHERE key = ? OR json_extract(facts, '$.triage_ref') = ? ORDER BY id ASC LIMIT 1",
        (raw, raw),
    ).fetchone()
    if found is not None:
        return int(found[0])
    if "#" in raw:
        left, right = raw.rsplit("#", 1)
        if left and right.isascii() and right.isdigit():
            sys_name, _, key, _, _, _ = task_store._parse_external_ticket(left, right)
            found = c.execute("SELECT id FROM refs WHERE system = ? AND key = ? LIMIT 1", (sys_name, key)).fetchone()
            if found is not None:
                return int(found[0])
    return None


def upsert_listed_ref(
    c,
    *,
    source: str,
    tracker: str,
    ref: str,
    title: str = "",
    url: str = "",
    updated: str = "",
    ext_state: str = "",
    in_sprint: object = None,
    parent: str = "",
    listed_at: str | None = None,
    repo: str = "",
    key_override: str = "",
    actor: str = "hand",
) -> int:
    ref_id = _resolve_ref_id(
        c, ref, tracker=tracker, repo=repo, url=url, title=title,
        key_override=key_override, actor=actor, create=True,
    )
    if ref_id is None:
        raise ValueError(f"could not resolve ref {ref!r}")
    existing = c.execute("SELECT facts FROM refs WHERE id = ?", (ref_id,)).fetchone()
    facts: dict[str, object] = {}
    if existing is not None and existing["facts"]:
        try:
            parsed = json.loads(existing["facts"])
            if isinstance(parsed, dict):
                facts = parsed
        except ValueError:
            facts = {}
    facts["triage_ref"] = ref
    if in_sprint is not None:
        facts["sprint"] = int(in_sprint) if isinstance(in_sprint, bool) else in_sprint
    elif "sprint" not in facts:
        facts["sprint"] = None
    facts["parent"] = str(parent or "")
    now = util.iso()
    updated_iso = iso_updated(updated)
    ref_cols = {r[1] for r in c.execute("PRAGMA table_info(refs)")}
    if "listed_at" in ref_cols:
        c.execute(
            "UPDATE refs SET connector = ?, title = ?, url = ?, "
            "ext_state = CASE WHEN ? != '' THEN ? ELSE ext_state END, "
            "remote_updated = ?, facts = ?, listed_at = ?, updated = ? WHERE id = ?",
            (source, title or "", url or "", ext_state or "", ext_state or "",
             updated_iso, json.dumps(facts), listed_at, now, ref_id),
        )
    else:
        c.execute(
            "UPDATE refs SET connector = ?, title = ?, url = ?, "
            "ext_state = CASE WHEN ? != '' THEN ? ELSE ext_state END, "
            "remote_updated = ?, facts = ?, updated = ? WHERE id = ?",
            (source, title or "", url or "", ext_state or "", ext_state or "",
             updated_iso, json.dumps(facts), now, ref_id),
        )
    return ref_id


def record_source_read(
    source: str,
    tracker: str,
    candidates: list,
    stamp: str,
    *,
    ok: bool = True,
    detail: str = "",
    key_for=None,
    repo_for=None,
) -> dict[str, int]:
    from .. import principals
    from . import sources as source_store

    actor = principals.current() or "hand"
    ref_ids: dict[str, int] = {}
    with db() as c:
        cur_name = f"triage:{source}"
        if ok:
            prev_row = c.execute("SELECT value FROM cursors WHERE source = ?", (cur_name,)).fetchone()
            prev_val = str(prev_row["value"]) if prev_row and prev_row["value"] else ""
            read_stamp = stamp
            if prev_val and read_stamp <= prev_val:
                if "#" in prev_val:
                    base, seq_s = prev_val.rsplit("#", 1)
                    next_seq = int(seq_s) + 1 if seq_s.isdigit() else 1
                    read_stamp = f"{base}#{next_seq}"
                else:
                    read_stamp = f"{prev_val}#1"
            for cand in candidates:
                key_override = key_for(cand) if key_for else ""
                repo_name = repo_for(cand) if repo_for else ""
                rid = upsert_listed_ref(
                    c,
                    source=source,
                    tracker=tracker,
                    ref=cand.ref,
                    title=cand.title,
                    url=cand.url,
                    updated=cand.updated,
                    ext_state=getattr(cand, "ext_state", ""),
                    in_sprint=cand.in_sprint,
                    parent=cand.parent,
                    listed_at=read_stamp,
                    repo=repo_name,
                    key_override=key_override,
                    actor=actor,
                )
                ref_ids[cand.ref] = rid
            source_store.set_cursor(cur_name, read_stamp, True, detail, connect=lambda: nullcontext(c))
        else:
            source_store.set_cursor(cur_name, None, False, detail, connect=lambda: nullcontext(c))
    return ref_ids


def drop_unconfigured_cursors(active_sources: list[str]) -> None:
    keep = [f"triage:{name}" for name in active_sources]
    with db() as c:
        if keep:
            placeholders = ",".join("?" * len(keep))
            c.execute(
                f"DELETE FROM cursors WHERE source LIKE 'triage:%' AND source NOT IN ({placeholders})",  # noqa: S608
                keep,
            )
        else:
            c.execute("DELETE FROM cursors WHERE source LIKE 'triage:%'")


def record_verdict(
    row: dict,
    *,
    model: str = "",
    run_id: str = "",
    reason: str = "",
    judged_updated: str | None = None,
    ensure_listed: bool = False,
    key_override: str = "",
    conn=None,
) -> int:
    from .. import principals
    from . import sources as source_store

    actor = principals.current() or "hand"

    def _write(c) -> int:
        ref_str = str(row.get("ref") or "").strip()
        source = str(row.get("source") or "").strip()
        tracker_name = str(row.get("tracker") or "").strip()
        ref_id = row.get("ref_id")
        raw_updated = row.get("updated") or ""
        if ensure_listed:
            cur_name = f"triage:{source}"
            existing_cur = c.execute("SELECT value FROM cursors WHERE source = ?", (cur_name,)).fetchone()
            stamp = str(existing_cur["value"]) if existing_cur and existing_cur["value"] else util.iso()
            if existing_cur is None or not existing_cur["value"]:
                source_store.set_cursor(cur_name, stamp, True, "", connect=lambda: nullcontext(c))
            ref_id = upsert_listed_ref(
                c,
                source=source,
                tracker=tracker_name,
                ref=ref_str,
                title=str(row.get("title") or ""),
                url=str(row.get("url") or ""),
                updated=str(raw_updated),
                in_sprint=row.get("in_sprint"),
                parent=str(row.get("parent") or ""),
                listed_at=None if row.get("closed") else stamp,
                repo=str(row.get("repo") or ""),
                key_override=key_override,
                actor=actor,
            )
        elif ref_id is None:
            ref_id = _resolve_ref_id(
                c, ref_str, tracker=tracker_name, repo=str(row.get("repo") or ""),
                url=str(row.get("url") or ""), title=str(row.get("title") or ""),
                key_override=key_override, actor=actor, create=True,
            )
        judged = iso_updated(judged_updated if judged_updated is not None else str(raw_updated))
        ready_raw = row.get("ready")
        ready_val = None if ready_raw in (None, "") else int(bool(ready_raw))
        code_raw = row.get("code")
        code_val = None if code_raw in (None, "") else int(bool(code_raw))
        at_ts = str(row.get("ts") or util.iso())
        cost_val = float(row.get("cost") or 0.0)
        cost_nusd = util.nusd(cost_val)
        base_values = (
            ref_str,
            int(ref_id) if ref_id is not None else None,
            source,
            tracker_name,
            str(row.get("repo") or ""),
            str(row.get("route") or ""),
            ready_val,
            code_val,
            str(row.get("size") or ""),
            str(row.get("kind") or ""),
            str(reason or ""),
            str(model or ""),
            str(run_id or ""),
            at_ts,
            _json_list_str(row.get("questions")),
            _json_list_str(row.get("plan")),
            _json_list_str(row.get("assumptions")),
            _json_list_str(row.get("checks")),
            _json_list_str(row.get("repos")),
            str(row.get("why") or ""),
            (cost_nusd / 1_000_000_000) if cost_nusd is not None else cost_val,
            judged,
        )
        if migration_applied(MONEY_EXPAND_SCHEMA, c):
            cur = c.execute(
                insert_sql("triage_verdicts", (*VERDICT_COLUMNS, "cost_nusd")),
                (*base_values, cost_nusd),
            )
        else:
            cur = c.execute(insert_sql("triage_verdicts", VERDICT_COLUMNS), base_values)
        return int(cur.lastrowid)

    if conn is not None:
        return _write(conn)
    with db() as c:
        return _write(c)


def latest_verdicts(connect=reader) -> dict[int, dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT v.* FROM triage_verdicts v "
            "JOIN (SELECT ref_id, MAX(id) AS max_id FROM triage_verdicts WHERE ref_id IS NOT NULL GROUP BY ref_id) m "
            "  ON v.id = m.max_id"
        ).fetchall()
        return {int(r["ref_id"]): lists_read(dict(r), TRIAGE_LISTS) for r in rows}


def latest_verdict_by_ref(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        ref_id = _resolve_ref_id(c, ref)
        r = None
        if ref_id is not None:
            r = c.execute(
                "SELECT * FROM triage_verdicts WHERE ref_id = ? ORDER BY id DESC LIMIT 1",
                (ref_id,),
            ).fetchone()
        if r is None:
            r = c.execute(
                "SELECT * FROM triage_verdicts WHERE ref = ? ORDER BY id DESC LIMIT 1",
                (ref.strip(),),
            ).fetchone()
        return lists_read(dict(r), TRIAGE_LISTS) if r else None


def verdicts_for_ref(ref: str | int, connect=reader) -> list[dict]:
    with connect() as c:
        if isinstance(ref, int) and not isinstance(ref, bool):
            rows = c.execute(
                "SELECT * FROM triage_verdicts WHERE ref_id = ? ORDER BY id ASC",
                (ref,),
            ).fetchall()
        else:
            ref_id = _resolve_ref_id(c, str(ref))
            if ref_id is not None:
                rows = c.execute(
                    "SELECT * FROM triage_verdicts WHERE ref_id = ? ORDER BY id ASC",
                    (ref_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM triage_verdicts WHERE ref = ? ORDER BY id ASC",
                    (str(ref).strip(),),
                ).fetchall()
        return [lists_read(dict(r), TRIAGE_LISTS) for r in rows]


def copy_verdict_after_move(ref: str, before: str, after: str) -> bool:
    before_iso = iso_updated(before)
    after_iso = iso_updated(after)
    if not before_iso or not after_iso or before_iso == after_iso:
        return False
    copied = False
    with db() as c:
        ref_id = _resolve_ref_id(c, ref)
        vrow = None
        if ref_id is not None:
            vrow = c.execute(
                "SELECT * FROM triage_verdicts WHERE ref_id = ? ORDER BY id DESC LIMIT 1",
                (ref_id,),
            ).fetchone()
        if vrow is None:
            vrow = c.execute(
                "SELECT * FROM triage_verdicts WHERE ref = ? ORDER BY id DESC LIMIT 1",
                (ref.strip(),),
            ).fetchone()
        if vrow is not None:
            v = dict(vrow)
            if iso_updated(v.get("judged_updated")) == before_iso:
                now = util.iso()
                copy_values = (
                    v["ref"],
                    v["ref_id"],
                    v["source"],
                    v["tracker"],
                    v["repo"],
                    v["route"],
                    v["ready"],
                    v["code"],
                    v["size"],
                    v["kind"],
                    v["reason"],
                    v["model"],
                    v["run_id"],
                    now,
                    v["questions"],
                    v["plan"],
                    v["assumptions"],
                    v["checks"],
                    v["repos"],
                    v["why"],
                    0.0,
                    after_iso,
                )
                if migration_applied(MONEY_EXPAND_SCHEMA, c):
                    c.execute(
                        insert_sql("triage_verdicts", (*VERDICT_COLUMNS, "cost_nusd")),
                        (*copy_values, 0),
                    )
                else:
                    c.execute(insert_sql("triage_verdicts", VERDICT_COLUMNS), copy_values)
                copied = True
        if ref_id is not None:
            c.execute(
                "UPDATE refs SET remote_updated = ?, updated = ? WHERE id = ? AND remote_updated = ?",
                (after_iso, util.iso(), ref_id, before_iso),
            )
            rs = c.execute("SELECT ref, updated FROM research WHERE ref_id = ? LIMIT 1", (ref_id,)).fetchone()
            if rs is not None and iso_updated(rs["updated"]) == before_iso:
                c.execute("UPDATE research SET updated = ? WHERE ref_id = ?", (after_iso, ref_id))
        else:
            rs = c.execute("SELECT ref, updated FROM research WHERE ref = ? LIMIT 1", (ref.strip(),)).fetchone()
            if rs is not None and iso_updated(rs["updated"]) == before_iso:
                c.execute("UPDATE research SET updated = ? WHERE ref = ?", (after_iso, rs["ref"]))
    return copied


def triage_rows(connect=reader) -> list[dict]:
    with connect() as c:
        return [lists_read({**dict(r), "version": int(r["version"]) if "version" in r.keys() and r["version"] else 1}, TRIAGE_LISTS)
                for r in c.execute("SELECT * FROM triage")]


def triage_approve(row: dict, sent_by: str) -> None:
    from .. import principals
    from . import tasks as task_store

    with db() as c:
        has_ref = task_store.has_identity_expand(c)
        cols = (*APPROVAL_COLUMNS, "ref_id") if has_ref else APPROVAL_COLUMNS
        item = {**row, "ts": util.iso(), "sent_by": sent_by}
        if has_ref and item.get("ref_id") is None:
            item["ref_id"] = _resolve_ref_id(
                c,
                str(item.get("ref") or ""),
                tracker=str(item.get("tracker") or ""),
                repo=str(item.get("repo") or ""),
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                actor=principals.current() or "hand",
                create=True,
            )
        c.execute(insert_sql("triage_approvals", cols, "INSERT OR REPLACE"),
                  lists_written(item, cols, APPROVAL_LISTS))


def triage_approvals(since: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM triage_approvals WHERE ts >= ? ORDER BY ts DESC", (since,))
        return [lists_read(dict(r), APPROVAL_LISTS) for r in rows]


def triage_approval(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        ref_id = _resolve_ref_id(c, ref)
        r = None
        if ref_id is not None:
            r = c.execute("SELECT * FROM triage_approvals WHERE ref_id = ? LIMIT 1", (ref_id,)).fetchone()
        if r is None:
            r = c.execute("SELECT * FROM triage_approvals WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return lists_read(dict(r), APPROVAL_LISTS) if r else None
