from __future__ import annotations

import json
from datetime import timedelta

from .. import run_lines, util
from ._rows import insert_sql, json_dict, validate_columns
from .db import db, reader, tx
from .runs import OPEN_PR_RUN

TABLES = {
    "usage": "configured keep_days",
    "research": "kept",
    "evals": "kept",
    "feedback": "kept",
    "verdicts": "kept",
    "captures": "kept",
    "capture_checks": "until draft discard",
    "capture_evidence": "until removed or draft discard",
}


USAGE_COLUMNS = ("at", "tab", "device", "kind", "view", "name", "via", "ms")


def add_verdict(run_id: str, kind: str, subject: str, about: str, answers: dict, held: str,
                outcome: str = "") -> None:
    with db() as c:
        c.execute("INSERT INTO verdicts (at, run_id, loop, kind, subject, about, answers, held, outcome, outcome_at) "
                  "VALUES (?,?,(SELECT loop FROM runs WHERE id = ?),?,?,?,?,?,?,?) "
                  "ON CONFLICT (run_id, kind, subject) DO UPDATE SET at = excluded.at, about = excluded.about, "
                  "answers = excluded.answers, held = excluded.held, outcome = excluded.outcome, "
                  "outcome_at = excluded.outcome_at, sha = '', approval = NULL",
                  (util.iso(), run_id, run_id, kind, subject, about[:300], json.dumps(answers), held[:1000], outcome,
                   util.iso() if outcome else None))


def settle_verdicts(run_id: str, outcome: str, approval: int | None = None, was: tuple[str, ...] = ("",),
                    *, c=None) -> None:
    sql = (f"UPDATE verdicts SET outcome = ?, outcome_at = ?, approval = COALESCE(?, approval) "  # noqa: S608
           f"WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome IN ({','.join('?' * len(was))})")
    args = (outcome, util.iso(), approval, run_id, *was)
    if c is not None:
        c.execute(sql, args)
        return
    with db() as conn:
        conn.execute(sql, args)


def set_verdict_sha(run_id: str, sha: str, *, c=None) -> None:
    sql = "UPDATE verdicts SET sha = ? WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome != 'replaced'"
    if c is not None:
        c.execute(sql, (sha, run_id))
        return
    with db() as conn:
        conn.execute(sql, (sha, run_id))


def verdicts_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "answers": json_dict(r["answers"])}
                for r in c.execute("SELECT * FROM verdicts WHERE at >= ? ORDER BY at", (since,))]


EVAL_LINEAGE = ("engine_sha", "prompts_sha", "prompts_from", "context_sha", "settings", "base_commit",
                "after_step", "last_step")
EVAL_INTERIM = frozenset(("engine_sha", "prompts_sha", "prompts_from", "settings"))


def add_eval(run_id: str, repo: str, task: str, passed: bool, seconds: float, cost: float, source: str = "",
             lineage: dict | None = None) -> None:
    lineage = lineage or {}
    with db() as c:
        c.execute(
            f"INSERT INTO evals (run_id, repo, task, passed, seconds, cost_usd, ts, source, {', '.join(EVAL_LINEAGE)}) "  # noqa: S608
            f"VALUES ({','.join('?' * 16)})",
            (run_id, repo, task, int(passed), seconds, cost, util.iso(), source,
             *("" if k in EVAL_INTERIM else lineage.get(k, 0 if k in ("after_step", "last_step") else "")
               for k in EVAL_LINEAGE)),
        )


BASELINE_SOURCE = "worktree"


def task_history(repo: str, task: str, limit: int = 5, source: str = BASELINE_SOURCE) -> list[dict]:
    with reader() as c:
        rows = c.execute(f"SELECT run_id, passed, {', '.join(EVAL_LINEAGE)} FROM evals "  # noqa: S608
                         "WHERE repo = ? AND task = ? AND source = ? ORDER BY ts DESC LIMIT ?",
                         (repo, task, source, limit))
        return [{**dict(r), "passed": bool(r["passed"])} for r in rows]


def eval_rows(run_id: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM evals WHERE run_id = ? ORDER BY ts", (run_id,))]


def eval_history(repo: str, limit: int = 20) -> list[dict]:
    with reader() as c:
        rows = c.execute(
            "SELECT run_id, MIN(ts) AS ts, MAX(source) AS source, SUM(passed) AS passed, COUNT(*) AS total,"
            " SUM(cost_usd) AS cost FROM evals WHERE repo = ? GROUP BY run_id ORDER BY ts DESC LIMIT ?",
            (repo, limit),
        )
        return [dict(r) for r in rows]


def evals_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM evals WHERE ts >= ? ORDER BY ts", (since,))]


FEEDBACK_COLUMNS = ("pr_url", "kind", "gh_id", "repo", "issue", "body", "path", "line", "hunk", "thread", "at_once",
                    "created", "pr_state", "status", "asks", "recorded")


def feedback_keys(pr_url: str) -> set[tuple[str, int]]:
    with reader() as c:
        return {(r["kind"], r["gh_id"]) for r in c.execute("SELECT kind, gh_id FROM feedback WHERE pr_url = ?",
                                                           (pr_url,))}


def add_feedback(rows: list[dict]) -> int:
    from . import tasks as task_store

    with db() as c:
        before = c.total_changes
        if task_store.has_identity_expand(c):
            cols = (*FEEDBACK_COLUMNS, "task_id")
            now = util.iso()
            prepared = []
            for r in rows:
                tnum = r.get("task_id")
                if tnum is None and r.get("run_id"):
                    rrow = c.execute("SELECT task_id FROM runs WHERE id = ?", (r["run_id"],)).fetchone()
                    if rrow and rrow["task_id"] is not None:
                        tnum = int(rrow["task_id"])
                if tnum is None and r.get("repo") and r.get("issue"):
                    tnum = task_store.TaskStore.find_task_num(c, str(r["repo"]), str(r["issue"]))
                prepared.append((*(r.get(k) for k in FEEDBACK_COLUMNS[:-1]), now, tnum))
            c.executemany(
                f"INSERT OR IGNORE INTO feedback ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608
                prepared,
            )
        else:
            c.executemany(f"INSERT OR IGNORE INTO feedback ({', '.join(FEEDBACK_COLUMNS)}) "  # noqa: S608
                          f"VALUES ({', '.join('?' * len(FEEDBACK_COLUMNS))})",
                          [(*(r.get(k) for k in FEEDBACK_COLUMNS[:-1]), util.iso()) for r in rows])
        return c.total_changes - before


def feedback_due(repo: str, quiet_since: str) -> list[str]:
    with reader() as c:
        rows = c.execute("SELECT issue, MAX(at_once) AS at_once, MAX(created) AS last FROM feedback f "  # noqa: S608
                         "WHERE repo = ? AND status = 'new' AND EXISTS (SELECT 1 FROM runs WHERE runs.repo = f.repo "
                         f"AND runs.issue = f.issue AND {OPEN_PR_RUN} "
                         "AND ' ' || runs.pr_url || ' ' LIKE '% ' || f.pr_url || ' %') "
                         "GROUP BY issue ORDER BY last", (repo,))
        return [str(r["issue"]) for r in rows if r["at_once"] or r["last"] <= quiet_since]


def close_feedback(pr_url: str, pr_state: str) -> None:
    with db() as c:
        c.execute("UPDATE feedback SET pr_state = ?, status = CASE WHEN status IN ('new', 'failed') THEN 'recorded' "
                  "ELSE status END WHERE pr_url = ?", (pr_state, pr_url))


def feedback_waiting(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT repo, issue, COUNT(*) AS n, MAX(at_once) AS at_once, "
                                           "MAX(CASE WHEN at_once THEN created END) AS submitted, "
                                           "MAX(created) AS last FROM feedback WHERE status = 'new' "
                                           "GROUP BY repo, issue ORDER BY last")]


def open_feedback(pr_urls: list[str]) -> list[dict]:
    if not pr_urls:
        return []
    with reader() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM feedback WHERE pr_url IN ({','.join('?' * len(pr_urls))}) "  # noqa: S608
            "AND status IN ('new', 'taken', 'failed') ORDER BY created", pr_urls)]


def has_fresh_feedback(rows: list[dict]) -> bool:
    return any(row["status"] == "new" and row["run_id"] is None for row in rows)


def has_new_feedback(rows: list[dict]) -> bool:
    return any(row["status"] == "new" for row in rows)


def take_feedback(rows: list[dict], run_id: str) -> list[dict]:
    with tx() as c:
        return [r for r in rows if c.execute(
            "UPDATE feedback SET status = 'taken', previous_run_id = run_id, run_id = ? "
            "WHERE pr_url = ? AND kind = ? AND gh_id = ? "
            "AND status = ? AND run_id IS ? AND (status IN ('new', 'failed') OR (status = 'taken' "
            "AND NOT EXISTS (SELECT 1 FROM runs WHERE id = feedback.run_id AND status = 'running')))",
            (run_id, r["pr_url"], r["kind"], r["gh_id"], r["status"], r.get("run_id"))).rowcount]


def release_feedback(run_id: str, *, closed_prs: tuple[str, ...] = ()) -> None:
    with tx() as c:
        if closed_prs:
            c.execute(f"UPDATE feedback SET status = 'recorded', previous_run_id = NULL "  # noqa: S608
                      f"WHERE run_id = ? AND status = 'taken' AND pr_url IN ({','.join('?' * len(closed_prs))})",
                      (run_id, *closed_prs))
        # Runs launched before the migration saved the previous claim in the run log.
        previous = next((line["previous"] for line in run_lines.read(run_id)
                         if line.get("event") == "feedback_claim"), [])
        c.executemany("UPDATE feedback SET status = 'new', run_id = ? WHERE pr_url = ? AND kind = ? AND gh_id = ? "
                      "AND run_id = ? AND status = 'taken' AND previous_run_id IS NULL",
                      [(prior, url, kind, gh_id, run_id) for url, kind, gh_id, prior in previous])
        c.execute("UPDATE feedback SET status = 'new', run_id = previous_run_id, previous_run_id = NULL "
                  "WHERE run_id = ? AND status = 'taken'", (run_id,))


def release_interrupted_feedback(run_id: str) -> None:
    with tx() as c:
        if c.execute("SELECT 1 FROM feedback WHERE run_id = ? AND status = 'taken' LIMIT 1", (run_id,)).fetchone():
            if not any(line.get("event") == "work_start" or (line.get("event") == "step_start"
                       and line.get("step") != "respond" and not line.get("preflight"))
                       for line in run_lines.read(run_id)):
                release_feedback(run_id)


def settle_feedback(run_id: str, status: str, *, prs: tuple[str, ...] = ()) -> None:
    pr_filter = f" AND pr_url IN ({','.join('?' * len(prs))})" if prs else ""
    with db() as c:
        c.execute(f"UPDATE feedback SET status = ?, previous_run_id = NULL WHERE run_id = ? AND status = 'taken'"  # noqa: S608
                  f"{pr_filter}", (status, run_id, *prs))


def ticket_feedback(repo: str, ref: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback WHERE repo = ? AND issue = ? "
                                           "ORDER BY pr_url, created", (repo, ref))]


def feedback_between(since: str, until: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback WHERE COALESCE(recorded, created) >= ? "
                                           "AND COALESCE(recorded, created) < ? ORDER BY pr_url, created",
                                           (since, until))]


RESEARCH_COLUMNS = ("ref", "source", "title", "url", "updated", "status", "summary", "report", "error", "run_id",
                    "cost", "ts", "ticket", "basis", "answered_by", "filed_path", "filed_url", "moved", "delivered")
RESEARCH_UPDATABLE_FIELDS = frozenset(RESEARCH_COLUMNS) - {"ref"}
CAPTURE_UPDATABLE_FIELDS = frozenset((
    "kind", "text", "expected", "actual", "steps", "context", "jev_cost_usd", "inspect_cost_usd",
    "written_title", "written_body", "written_print", "written_note", "delivery", "issue",
    "issue_url", "issue_title", "filed",
))
CAPTURE_EVIDENCE_COLUMNS = frozenset((
    "capture", "name", "kind", "type", "bytes", "sha256", "source", "seconds", "added",
    "inspection", "link",
))


def research_rows(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM research ORDER BY ts DESC")]


def research_get(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM research WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return dict(r) if r else None


def research_set(ref: str, **fields) -> None:
    from .. import principals
    from . import tasks as task_store

    row = {**(research_get(ref) or {"ref": ref, "cost": 0.0}), **fields, "ts": util.iso()}
    with db() as c:
        if task_store.has_identity_expand(c):
            cols = (*RESEARCH_COLUMNS, "ref_id")
            ref_id = row.get("ref_id")
            if ref_id is None:
                ref_id = task_store.TaskStore.ref_for_triage(c, row, actor=principals.current())
            row["ref_id"] = ref_id
            c.execute(insert_sql("research", cols, "INSERT OR REPLACE"), tuple(row.get(k) for k in cols))
        else:
            c.execute(insert_sql("research", RESEARCH_COLUMNS, "INSERT OR REPLACE"),
                      tuple(row.get(k) for k in RESEARCH_COLUMNS))


def research_keep(ref: str, **fields) -> None:
    validate_columns(fields, RESEARCH_UPDATABLE_FIELDS, "research")
    if not fields:
        return
    with db() as c:
        c.execute(f"UPDATE research SET {', '.join(f'{k} = ?' for k in fields)} WHERE ref = ? COLLATE NOCASE",  # noqa: S608
                  (*fields.values(), ref))


def add_usage(row: dict, keep_days: float) -> None:
    with db() as c:
        c.execute(insert_sql("usage", USAGE_COLUMNS), tuple(row.get(k) for k in USAGE_COLUMNS))
        c.execute("DELETE FROM usage WHERE at < ?", (util.iso(util.now() - timedelta(days=keep_days)),))


OPEN_CAPTURE = "status IN ('draft', 'checking', 'filing', 'queued')"


EDITABLE_CAPTURE = "status IN ('draft', 'checking')"


def capture_draft(connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute(f"SELECT * FROM captures WHERE {OPEN_CAPTURE}").fetchone()  # noqa: S608
        if not r:
            return None
        draft = dict(r)
        draft["evidence"] = [{**dict(e), "inspection": json_dict(e["inspection"])} for e in c.execute(
            "SELECT * FROM capture_evidence WHERE capture = ? ORDER BY id", (draft["id"],))]
        draft["checks"] = {k["name"]: {**dict(k), "detail": json_dict(k["detail"])} for k in c.execute(
            "SELECT * FROM capture_checks WHERE capture = ?", (draft["id"],))}
        return draft


def capture_move(draft_id: int, revision: int, was: str, to: str, delivery: str | None = None) -> bool:
    with tx() as c:
        note = "" if delivery is None else ", delivery = ?"
        args = (to, *(() if delivery is None else (delivery,)), draft_id, was, revision)
        return c.execute(f"UPDATE captures SET status = ?{note} WHERE id = ? AND status = ? AND revision = ?",  # noqa: S608
                         args).rowcount == 1


def capture_begin_checks(draft_id: int, prints: dict[str, str]) -> bool:
    with tx() as c:
        if not c.execute("SELECT 1 FROM captures WHERE id = ? AND status = 'checking'", (draft_id,)).fetchone():
            return False
        for name, print_ in prints.items():
            c.execute("INSERT OR REPLACE INTO capture_checks (capture, name, result, reason, detail, fingerprint, checked) "
                      "VALUES (?, ?, 'running', '', '{}', ?, ?)", (draft_id, name, print_, util.iso()))
        return True


def capture_written(draft_id: int, print_: str, title: str, body: str, note: str) -> None:
    with db() as c:
        c.execute("UPDATE captures SET written_print = ?, written_title = ?, written_body = ?, written_note = ? "
                  "WHERE id = ?", (print_, title, body, note, draft_id))


def capture_linked(evidence_id: int, sha256: str, link: str) -> None:
    with db() as c:
        c.execute("UPDATE capture_evidence SET link = ? WHERE id = ? AND sha256 = ?", (link, evidence_id, sha256))


def capture_note(draft_id: int | None, text: str) -> None:
    with db() as c:
        c.execute("UPDATE captures SET delivery = ? WHERE status = 'filing'" + ("" if draft_id is None else " AND id = ?"),  # noqa: S608
                  (text, *(() if draft_id is None else (draft_id,))))


def capture_filed(draft_id: int, number: int, url: str, title: str) -> bool:
    now = util.iso()
    with tx() as c:
        changed = c.execute("UPDATE captures SET status = 'filed', issue = ?, issue_url = ?, issue_title = ?, filed = ?, "
                            "updated = ?, delivery = '' WHERE id = ? AND status IN ('filing', 'queued')",
                            (number, url, title, now, now, draft_id)).rowcount
        if changed:
            c.execute("UPDATE verdicts SET outcome = 'filed', outcome_at = ? WHERE run_id = ? AND kind = 'capture'",
                      (now, f"capture-{draft_id}"))
        return bool(changed)


def capture_filed_row(draft_id: int | None = None, connect=reader) -> dict | None:
    with connect() as c:
        if draft_id is None:
            r = c.execute("SELECT * FROM captures WHERE status = 'filed' ORDER BY filed DESC, id DESC").fetchone()
        else:
            r = c.execute("SELECT * FROM captures WHERE status = 'filed' AND id = ?", (draft_id,)).fetchone()
        return dict(r) if r else None


def capture_filing(connect=reader) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM captures WHERE status = 'filing'").fetchone() is not None


def capture_check_done(draft_id: int, name: str, result: str, reason: str, detail: dict, cost: float) -> bool:
    with tx() as c:
        changed = c.execute(
            "UPDATE capture_checks SET result = ?, reason = ?, detail = ?, checked = ? WHERE capture = ? AND name = ? "
            "AND result = 'running' AND EXISTS (SELECT 1 FROM captures WHERE id = ? AND status = 'checking')",
            (result, reason, json.dumps(detail), util.iso(), draft_id, name, draft_id)).rowcount
        if changed:
            c.execute("UPDATE captures SET jev_cost_usd = COALESCE(jev_cost_usd, 0) + ? WHERE id = ?", (cost, draft_id))
        return bool(changed)


def capture_inspected(evidence_id: int, sha256: str, inspection: dict, cost: float) -> bool:
    with tx() as c:
        row = c.execute("SELECT capture FROM capture_evidence WHERE id = ? AND sha256 = ?",
                        (evidence_id, sha256)).fetchone()
        if not row:
            return False
        c.execute("UPDATE capture_evidence SET inspection = ? WHERE id = ?", (json.dumps(inspection), evidence_id))
        c.execute("UPDATE captures SET inspect_cost_usd = COALESCE(inspect_cost_usd, 0) + ? WHERE id = ?",
                  (cost, row["capture"]))
        return True


def capture_end_checks(draft_id: int | None, reason: str) -> None:
    with tx() as c:
        args = () if draft_id is None else (draft_id,)
        c.execute("UPDATE capture_checks SET result = 'interrupted', reason = ?, checked = ? WHERE result = 'running'"  # noqa: S608
                  + ("" if draft_id is None else " AND capture = ?"), (reason, util.iso(), *args))
        c.execute("UPDATE captures SET status = 'draft' WHERE status = 'checking'"  # noqa: S608
                  + ("" if draft_id is None else " AND id = ?"), args)


def capture_checking(connect=reader) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM captures WHERE status = 'checking'").fetchone() is not None


def capture_evidence(evidence_id: int, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT e.* FROM capture_evidence e JOIN captures k ON k.id = e.capture "
                      "WHERE e.id = ? AND k.status IN ('draft', 'checking', 'filing', 'queued')", (evidence_id,)).fetchone()
        return dict(r) if r else None


def capture_stage(row: dict, admit, keep) -> tuple[dict, bool]:
    now = util.iso()
    with tx() as c:
        draft = c.execute(f"SELECT id, status FROM captures WHERE {OPEN_CAPTURE}").fetchone()  # noqa: S608
        draft_id = draft["id"] if draft else c.execute(
            "INSERT INTO captures (status, revision, created, updated) VALUES ('draft', 0, ?, ?)", (now, now)).lastrowid
        held = [dict(r) for r in c.execute("SELECT * FROM capture_evidence WHERE capture = ? ORDER BY id", (draft_id,))]
        admit(held, draft["status"] if draft else "draft")
        same = next((r for r in held if r["sha256"] == row["sha256"]), None)
        if same:
            return same, True
        keep(draft_id)
        row = {**row, "capture": draft_id, "added": now}
        validate_columns(row, CAPTURE_EVIDENCE_COLUMNS, "capture_evidence")
        columns = tuple(row)
        row["id"] = c.execute(insert_sql("capture_evidence", columns), tuple(row[k] for k in columns)).lastrowid
        c.execute("UPDATE captures SET revision = revision + 1, updated = ? WHERE id = ?", (now, draft_id))
        return row, False


def capture_drop_evidence(evidence_id: int) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT e.* FROM capture_evidence e JOIN captures k ON k.id = e.capture "
                      "WHERE e.id = ? AND k.status IN ('draft', 'checking')", (evidence_id,)).fetchone()
        if not r:
            return None
        c.execute("DELETE FROM capture_evidence WHERE id = ?", (evidence_id,))
        c.execute("UPDATE captures SET revision = revision + 1, updated = ? WHERE id = ?", (util.iso(), r["capture"]))
        return dict(r)


def capture_save(**fields) -> bool:
    validate_columns(fields, CAPTURE_UPDATABLE_FIELDS, "captures")
    now = util.iso()
    names = list(fields)
    sets = "".join(f"{n} = ?, " for n in names)
    with tx() as c:
        changed = c.execute(f"UPDATE captures SET {sets}revision = revision + 1, updated = ? WHERE {EDITABLE_CAPTURE}",  # noqa: S608
                            (*fields.values(), now)).rowcount
        if changed:
            return True
        if c.execute("SELECT 1 FROM captures WHERE status IN ('filing', 'queued')").fetchone():
            return False
        cols = f"{', '.join(names)}, " if names else ""
        marks = f"{', '.join('?' * len(names))}, " if names else ""
        c.execute(f"INSERT INTO captures ({cols}status, revision, created, updated) "  # noqa: S608
                  f"VALUES ({marks}'draft', 1, ?, ?)", (*fields.values(), now, now))
        return True


def capture_discard() -> int | None:
    with tx() as c:
        draft = c.execute(f"SELECT id FROM captures WHERE {EDITABLE_CAPTURE}").fetchone()  # noqa: S608
        if not draft:
            return None
        c.execute("DELETE FROM capture_evidence WHERE capture = ?", (draft["id"],))
        c.execute("DELETE FROM capture_checks WHERE capture = ?", (draft["id"],))
        c.execute("UPDATE captures SET status = 'discarded', revision = revision + 1, updated = ? WHERE id = ?",
                  (util.iso(), draft["id"]))
        return draft["id"]


def usage_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM usage WHERE at >= ? ORDER BY at", (since,))]


def judge_approval(c, approval_id: int, outcome: str) -> None:
    c.execute("UPDATE verdicts SET outcome = ?, outcome_at = ?, approval = ? WHERE kind IN ('push', 'proof') "
              "AND outcome IN ('waiting', 'failed', 'approved') "
              "AND run_id = (SELECT run_id FROM approvals WHERE id = ?)",
              (outcome, util.iso(), approval_id, approval_id))


def orphan_rows(c, table, limit):
    if table not in ("evals", "verdicts"):
        raise ValueError(table)
    query = (f"FROM {table} t LEFT JOIN runs ON runs.id = t.run_id "
             "WHERE t.run_id IS NOT NULL AND t.run_id <> '' AND runs.id IS NULL")
    count = c.execute(f"SELECT COUNT(*) {query}").fetchone()[0]
    rows = c.execute(f"SELECT t.rowid, t.run_id {query} ORDER BY t.rowid LIMIT ?", (limit,)).fetchall() if limit else []
    return count, rows
