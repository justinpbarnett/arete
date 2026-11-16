from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Sequence
from datetime import timedelta

from . import util
from .store import db as foundation
from .store import incidents as incident_store
from .store import loops as loop_store
from .store import runs as run_store
from .store import sources as source_store

LATEST = foundation.LATEST
MIGRATE_WAIT_SECONDS = foundation.MIGRATE_WAIT_SECONDS
NEEDED = foundation.NEEDED
TIME_COLUMNS = foundation.TIME_COLUMNS
TIME_GLOB = foundation.TIME_GLOB
IncompatibleSchema = foundation.IncompatibleSchema
NoDatabase = foundation.NoDatabase
checked_connection = foundation.checked_connection
contract_blockers = foundation.contract_blockers
db = foundation.db
ensure_schema = foundation.ensure_schema
execute_statements = foundation.execute_statements
migrate = foundation.migrate
missing_database = foundation.missing_database
one_read = foundation.one_read
owned = foundation.owned
owning_transaction = foundation.owning_transaction
read_connection = foundation.read_connection
refuse_empty_file = foundation.refuse_empty_file
tx = foundation.tx

OPEN_PR_RUN = run_store.OPEN_PR_RUN
DECIDABLE = ("pending", "failed")
APPROVAL_NOTES = ("plain_summary", "plain_summary_tried", "review", "review_tried")
WHOLE_RUN = run_store.WHOLE_RUN
USAGE_COLUMNS = ("at", "tab", "device", "kind", "view", "name", "via", "ms")
REFRESH_SCOPE = "credential-refresh-v1:"


reader = foundation.reader
read_snapshot = foundation.read_snapshot
write_connection = foundation.write_connection


backfill_legacy_cost = run_store.backfill_legacy_cost


cost_drift = run_store.cost_drift


FIRINGS = run_store.FIRINGS


runs = run_store.runs


run_facets = run_store.run_facets


paid_by_run = run_store.paid_by_run


merged_runs = run_store.merged_runs


source_seen = source_store.source_seen


runs_since = run_store.runs_since


runs_reading_pr_comments = run_store.runs_reading_pr_comments


open_runs = run_store.open_runs


run_lock = run_store.run_lock


loop_lock = loop_store.loop_lock


run_get = run_store.run_get


save_report = run_store.save_report


report = run_store.report


steps = run_store.steps


step_tries = run_store.step_tries


steps_since = run_store.steps_since


add_feed = run_store.add_feed


insert_feed = run_store.insert_feed


feed = run_store.feed


loop_state = loop_store.loop_state


loop_states = loop_store.loop_states


loop_marks = loop_store.loop_marks


set_loop_state = loop_store.set_loop_state


finding_seen = loop_store.finding_seen


record_finding = loop_store.record_finding


mark_emitted = loop_store.mark_emitted


set_finding_url = loop_store.set_finding_url


findings = loop_store.findings


add_event = loop_store.add_event


claim_seen = source_store.claim_seen


forget_seen = source_store.forget_seen


cursor = source_store.cursor


set_cursor = source_store.set_cursor


adopt_source = source_store.adopt_source


source_owner = source_store.source_owner


set_source_owner = source_store.set_source_owner


cursors = source_store.cursors


enqueue = loop_store.enqueue


pending = loop_store.pending


queue_item = loop_store.queue_item


take_queue_item = loop_store.take_queue_item


drop_queue_item = loop_store.drop_queue_item


release_claimed_items = loop_store.release_claimed_items


finish_claim = loop_store.finish_claim


end_claim = loop_store.end_claim


release_settled_claims = loop_store.release_settled_claims


dead_items = loop_store.dead_items


requeue_item = loop_store.requeue_item


SEEN_AGAIN = incident_store.SEEN_AGAIN
CLOSED_AT = incident_store.CLOSED_AT


def prune(days: float = 14, keep_days: float = 30, lines_days: float = 90) -> None:
    from . import store

    store.prune(days=days, keep_days=keep_days, lines_days=lines_days)


write_incident = incident_store.write_incident


record_incident = incident_store.record_incident


add_incident_note = incident_store.add_incident_note


open_incidents = incident_store.open_incidents


incidents_to_decide = incident_store.incidents_to_decide


known_fingerprints = incident_store.known_fingerprints


update_incident = incident_store.update_incident


woken_incidents = incident_store.woken_incidents


closed_alerted = incident_store.closed_alerted


add_repair = incident_store.add_repair


set_repair = incident_store.set_repair


incident_repairs = incident_store.incident_repairs


parsed = incident_store.parsed


incidents = incident_store.incidents


blocked_incidents = incident_store.blocked_incidents


incident = incident_store.incident


def add_approval(loop: str, run_id: str, emit: dict, summary: str, failed: str = "") -> int:
    with db() as c:
        cur = c.execute("INSERT INTO approvals (loop, run_id, emit, summary, status, result, created) "
                        "VALUES (?,?,?,?,?,?,?)", (loop, run_id, json.dumps(emit, default=str), summary[:2000],
                                                   "failed" if failed else "pending", failed[:1000], util.iso()))
        return cur.lastrowid


def approvals(connect=reader) -> list[dict]:
    with connect() as c:
        return [approval_of(r) for r in c.execute("SELECT a.*, COALESCE(r.repo, '') AS run_repo FROM approvals a "
                                                 "LEFT JOIN runs r ON r.id = a.run_id "
                                                 f"WHERE a.status IN ({','.join('?' * len(DECIDABLE))}) "
                                                 "ORDER BY a.id DESC LIMIT 100", DECIDABLE)]


def set_approval_note(approval_id: int, note: str, value: str, while_in: tuple[str, ...] = DECIDABLE) -> bool:
    if note not in APPROVAL_NOTES:
        raise ValueError(f"an approval has no note {note}; it has {', '.join(APPROVAL_NOTES)}")
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET {note} = ? WHERE id = ? AND status IN ({','.join('?' * len(while_in))})",
                  (value, approval_id, *while_in))
        return c.total_changes > before


def approval(approval_id: int) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return approval_of(r) if r else None


def approval_of(row) -> dict:
    return {**dict(row), "emit": json.loads(row["emit"] or "{}"), "review": json.loads(row["review"] or "{}")}


def claim_approval(approval_id: int, until: str, came_from: tuple[str, ...] = DECIDABLE) -> str:
    token = secrets.token_hex(8)
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = 'approved', result = '', decided = ?, claimed_by = ?, "
                  f"claimed_until = ? WHERE id = ? AND status IN ({','.join('?' * len(came_from))})",
                  (util.iso(), token, until, approval_id, *came_from))
        return token if c.total_changes > before else ""


def renew_claim(approval_id: int, token: str, until: str) -> bool:
    with db() as c:
        before = c.total_changes
        c.execute("UPDATE approvals SET claimed_until = ? WHERE id = ? AND status = 'approved' AND claimed_by = ?",
                  (until, approval_id, token))
        return c.total_changes > before


def lapsed_claims() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT id, claimed_by, claimed_until FROM approvals "
                                           "WHERE status = 'approved' AND claimed_until < ? ORDER BY id", (util.iso(),))]


def settle_approval(approval_id: int, status: str, outcome: str, result: str = "", *, judged: bool,
                    came_from: tuple[str, ...] = ("approved",), token: str | None = None,
                    until: str | None = None) -> bool:
    held = [f"status IN ({','.join('?' * len(came_from))})", *(["claimed_by = ?"] if token is not None else []),
            *(["claimed_until = ?"] if until is not None else [])]
    args = [*came_from, *(a for a in (token, until) if a is not None)]
    with db() as c:
        before = c.total_changes
        c.execute(f"UPDATE approvals SET status = ?, result = ?, decided = ?, claimed_by = '', claimed_until = '' "
                  f"WHERE id = ? AND {' AND '.join(held)}", (status, result[:1000], util.iso(), approval_id, *args))
        if c.total_changes == before:
            return False
        if judged:
            c.execute("UPDATE verdicts SET outcome = ?, outcome_at = ?, approval = ? WHERE kind IN ('push', 'proof') "
                      "AND outcome IN ('waiting', 'failed', 'approved') "
                      "AND run_id = (SELECT run_id FROM approvals WHERE id = ?)",
                      (outcome, util.iso(), approval_id, approval_id))
        return True


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


def settle_verdicts(run_id: str, outcome: str, approval: int | None = None, was: tuple[str, ...] = ("",)) -> None:
    with db() as c:
        c.execute(f"UPDATE verdicts SET outcome = ?, outcome_at = ?, approval = COALESCE(?, approval) "
                  f"WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome IN ({','.join('?' * len(was))})",
                  (outcome, util.iso(), approval, run_id, *was))


def set_verdict_sha(run_id: str, sha: str) -> None:
    with db() as c:
        c.execute("UPDATE verdicts SET sha = ? WHERE run_id = ? AND kind IN ('push', 'proof') AND outcome != 'replaced'",
                  (sha, run_id))


def verdicts_since(since: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "answers": json.loads(r["answers"] or "{}")}
                for r in c.execute("SELECT * FROM verdicts WHERE at >= ? ORDER BY at", (since,))]


def add_message(run_id: str, text: str) -> None:
    with db() as c:
        c.execute("INSERT INTO messages (run_id, text, ts) VALUES (?,?,?)", (run_id, text, util.iso()))


def take_messages(run_id: str) -> list[str]:
    with db() as c:
        rows = c.execute("SELECT id, text FROM messages WHERE run_id = ? AND taken IS NULL ORDER BY id",
                         (run_id,)).fetchall()
        c.executemany("UPDATE messages SET taken = ? WHERE id = ?", [(util.iso(), r["id"]) for r in rows])
        return [r["text"] for r in rows]


CHAT_USAGE = ("(SELECT COALESCE(SUM(m.paid_usd), 0) FROM chat_messages m WHERE m.chat_id = chats.id) AS paid_usd, "
              "(SELECT COUNT(*) FROM chat_messages m WHERE m.chat_id = chats.id "
              "AND m.paid_usd IS NULL AND m.cost != 0) AS unclassified_messages")


def chat_add(project: str) -> int:
    ts = util.iso()
    with db() as c:
        return c.execute("INSERT INTO chats (project, created, updated) VALUES (?,?,?)", (project, ts, ts)).lastrowid


def chat_get(chat_id: int) -> dict | None:
    with reader() as c:
        r = c.execute(f"SELECT chats.*, {CHAT_USAGE} FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return dict(r) if r else None


def chats() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(f"SELECT chats.*, {CHAT_USAGE} FROM chats ORDER BY created DESC, id DESC")]


def chat_messages(chat_id: int) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT id, sender, text, cost, paid_usd, ts FROM chat_messages WHERE chat_id = ? "
                                           "ORDER BY id", (chat_id,))]


def chat_claim(chat_id: int, text: str, title: str) -> int:
    ts = util.iso()
    with db() as c:
        claimed = c.execute("UPDATE chats SET busy = 1, updated = ?, title = CASE WHEN title = '' THEN ? ELSE title END "
                            "WHERE id = ? AND busy = 0", (ts, title, chat_id)).rowcount
        if claimed:
            return c.execute("INSERT INTO chat_messages (chat_id, sender, text, ts) VALUES (?,?,?,?)",
                             (chat_id, "you", text, ts)).lastrowid
        return 0


def chat_say(chat_id: int, sender: str, text: str, cost: float = 0.0, *, paid_usd: float | None = None,
             reported_cost: float | None = None, before: Sequence[tuple[str, str]] = (), **fields) -> None:
    ts = util.iso()
    reported_cost = cost if reported_cost is None else reported_cost
    paid_usd = 0.0 if reported_cost == 0 and paid_usd is None else paid_usd
    with db() as c:
        for kind, note in before:
            c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                      (chat_id, kind, note, 0, 0, ts))
        c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                  (chat_id, sender, text, reported_cost, paid_usd, ts))
        c.execute(f"UPDATE chats SET {''.join(f'{k} = ?, ' for k in fields)}updated = ?, spent = spent + ? WHERE id = ?",
                  (*fields.values(), ts, reported_cost, chat_id))


CHAT_SESSION_COLUMNS = ("l.run_id, l.chat_id, l.budget_usd, l.told, r.status, r.started, r.finished, r.repo, "
                        "r.branch, r.device, r.model, r.runner, r.summary, r.pr_url, r.lock, r.pid")


def chat_link_session(chat_id: int, run_id: str, budget_usd: float | None) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO chat_sessions (run_id, chat_id, budget_usd, linked) VALUES (?,?,?,?)",
                  (run_id, chat_id, budget_usd, util.iso()))


def chat_sessions(chat_id: int | None = None, connect=reader) -> list[dict]:
    where, args = ("WHERE l.chat_id = ?", (chat_id,)) if chat_id is not None else ("", ())
    with connect() as c:
        return [dict(r) for r in c.execute(
            f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_sessions l LEFT JOIN runs r ON r.id = l.run_id {where} "
            "ORDER BY l.linked DESC, l.rowid DESC", args)]


def chat_of_session(run_id: str, connect=reader) -> int | None:
    with connect() as c:
        r = c.execute("SELECT chat_id FROM chat_sessions WHERE run_id = ?", (run_id,)).fetchone()
        return r["chat_id"] if r else None


def chat_sessions_untold() -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            f"SELECT {CHAT_SESSION_COLUMNS} FROM chat_sessions l LEFT JOIN runs r ON r.id = l.run_id "
            "WHERE l.told = '' AND COALESCE(r.status, '') != 'running' ORDER BY l.rowid")]


def chat_tell_session(run_id: str, text: str) -> bool:
    ts = util.iso()
    with db() as c:
        row = c.execute("SELECT chat_id FROM chat_sessions WHERE run_id = ? AND told = ''", (run_id,)).fetchone()
        if not row or not c.execute("UPDATE chat_sessions SET told = ? WHERE run_id = ? AND told = ''",
                                    (ts, run_id)).rowcount:
            return False
        c.execute("INSERT INTO chat_messages (chat_id, sender, text, cost, paid_usd, ts) VALUES (?,?,?,?,?,?)",
                  (row["chat_id"], "session", text, 0, 0, ts))
        c.execute("UPDATE chats SET updated = ? WHERE id = ?", (ts, row["chat_id"]))
        return True


def chat_summarize(chat_id: int, summary: str, upto: int) -> None:
    with db() as c:
        c.execute("UPDATE chats SET summary = ?, summarized_to = ? WHERE id = ?", (summary, upto, chat_id))


def chat_drop_draft(chat_id: int, title: str, body: str) -> None:
    with db() as c:
        c.execute("UPDATE chats SET draft_title = '', draft_body = '' WHERE id = ? AND draft_title = ? AND draft_body = ?",
                  (chat_id, title, body))


def chat_newest_draft() -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM chats WHERE draft_title != '' ORDER BY updated DESC, id DESC LIMIT 1").fetchone()
        return dict(r) if r else None


def busy_chats() -> list[int]:
    with reader() as c:
        return [r[0] for r in c.execute("SELECT id FROM chats WHERE busy = 1 ORDER BY id")]


loop_spent_since = run_store.loop_spent_since


def hold(scope: str, until: str, why: str) -> None:
    with tx() as c:
        links = refresh_links(c)
        ts = util.iso()
        for linked in linked_scopes(scope, links):
            save_hold(c, linked, until, why, ts)


def save_hold(c, scope: str, until: str, why: str, ts: str) -> None:
    c.execute("INSERT INTO holds (scope, until, why, ts) VALUES (?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
              "until = CASE WHEN holds.until = '' OR excluded.until = '' THEN '' "
              "ELSE max(holds.until, excluded.until) END, "
              "why = CASE WHEN holds.until != '' AND (excluded.until = '' OR excluded.until > holds.until) "
              "THEN excluded.why ELSE holds.why END, ts = max(holds.ts, excluded.ts)", (scope, until, why, ts))


def holds(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM holds WHERE scope NOT LIKE ? "
                                           "AND (until > ? OR until = '') ORDER BY until",
                                           (REFRESH_SCOPE + "%", util.iso()))]


def clear_holds(scopes: list[str], set_before: str) -> None:
    with tx() as c:
        links = refresh_links(c)
        linked = {s for scope in scopes for s in linked_scopes(scope, links)}
        c.executemany("DELETE FROM holds WHERE scope = ? AND ts < ?", [(s, set_before) for s in linked])


def refresh_links(c) -> list[tuple[str, str, str, str]]:
    links = []
    for row in c.execute("SELECT scope, why FROM holds WHERE scope LIKE ?", (REFRESH_SCOPE + "%",)):
        try:
            before, after = row["scope"][len(REFRESH_SCOPE):].split(":")
            previous, latest = json.loads(row["why"])
            if before and after and all(isinstance(s, str) and s for s in (previous, latest)):
                links.append((before, after, previous, latest))
        except (ValueError, TypeError):
            continue
    return links


def linked_scopes(scope: str, links: list[tuple[str, str, str, str]]) -> set[str]:
    found, pending = set(), [scope]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        for _, _, previous, latest in links:
            for before, after in ((previous, latest), (latest, previous)):
                if current.startswith(before):
                    linked = after + current[len(before):]
                    if linked not in found:
                        pending.append(linked)
    return found


def refresh_continues(previous: str, latest: str, connect=reader) -> bool:
    if not previous or not latest:
        return False
    if previous == latest:
        return True
    with connect() as c:
        links = refresh_links(c)
    found, pending = set(), [previous]
    while pending:
        current = pending.pop()
        if current == latest:
            return True
        if current not in found:
            found.add(current)
            pending.extend(after for before, after, _, _ in links if before == current and after not in found)
    return False


def record_refresh(before: str, after: str, previous: str, latest: str) -> None:
    if not all((before, after, previous, latest)) or before == after:
        return
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO holds (scope, until, why, ts) VALUES (?,?,?,?)",
                  (REFRESH_SCOPE + before + ":" + after, "", json.dumps([previous, latest]), util.iso()))
        links = refresh_links(c)
        rows = c.execute("SELECT * FROM holds WHERE scope NOT LIKE ? AND (until = '' OR until > ?)",
                         (REFRESH_SCOPE + "%", util.iso())).fetchall()
        for row in rows:
            for scope in linked_scopes(row["scope"], links) - {row["scope"]}:
                save_hold(c, scope, row["until"], row["why"], row["ts"])


pass_over = source_store.pass_over


tickets_passed_over = source_store.tickets_passed_over


take_passed_over = source_store.take_passed_over


drop_passed_over = source_store.drop_passed_over


runs_marked_running = run_store.runs_marked_running


def add_vm(name: str, run_id: str | None, device: dict, vmid: int | None = None, connect=db) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO vms (name, run_id, device, vmid, created) VALUES (?,?,?,?,?)",
                  (name, run_id, json.dumps(device), vmid, util.iso()))


def note_vm_answered(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET answered = 1 WHERE name = ?", (name,))


def forget_vm(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ?", (name,))


def forget_gone_vm(name: str, unanswered_before: str, connect=db) -> None:
    with connect() as c:
        c.execute("DELETE FROM vms WHERE name = ? AND (answered = 1 OR created < ?)", (name, unanswered_before))


def note_delete_failed(name: str, connect=db) -> None:
    with connect() as c:
        c.execute("UPDATE vms SET failing_since = COALESCE(failing_since, ?) WHERE name = ?", (util.iso(), name))


def vms_of_run(run_id: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])}
                for r in c.execute("SELECT * FROM vms WHERE run_id = ? ORDER BY created", (run_id,))]


def vms_left_over(probes_created_before: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [{**dict(r), "device": json.loads(r["device"])} for r in c.execute(
            "SELECT * FROM vms v WHERE (v.run_id IS NULL AND v.created < ?) OR (v.run_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM runs r WHERE r.id = v.run_id AND r.status = 'running')) ORDER BY created",
            (probes_created_before,))]


ticket_runs = run_store.ticket_runs


newest_whole_run = run_store.newest_whole_run


ticket_runs_marked_running = run_store.ticket_runs_marked_running


ab_placements = run_store.ab_placements


ab_held = run_store.ab_held


ticket_runs_since = run_store.ticket_runs_since


later_run = run_store.later_run


interrupted_tickets = run_store.interrupted_tickets


last_pipeline_starts = run_store.last_pipeline_starts


running_run = run_store.running_run


pending_label = run_store.pending_label


issue_running = run_store.issue_running


def probe_get(name: str) -> dict | None:
    with reader() as c:
        r = c.execute("SELECT * FROM probes WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None


def probe_set(name: str, ok: bool, detail: str) -> None:
    ts = util.iso()
    prev = probe_get(name) or {}
    last_ok = ts if ok else prev.get("last_ok")
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO probes (name, last_run, last_ok, ok, detail) VALUES (?,?,?,?,?)",
            (name, ts, last_ok, int(ok), detail[:500]),
        )


alert_get = incident_store.alert_get


alert_set = incident_store.alert_set


alert_clear = incident_store.alert_clear


def probes(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM probes ORDER BY name")]


alerts = incident_store.alerts


def add_proposal(run_id: str, p: dict, status: str, pr_url: str = "") -> None:
    with db() as c:
        c.execute(
            "INSERT INTO proposals (run_id, layer, repo, file, change, why, evidence, confidence, status, pr_url, created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                p.get("layer"),
                p.get("repo"),
                p.get("file"),
                p.get("change"),
                p.get("why"),
                json.dumps(p.get("evidence", [])),
                float(p.get("confidence", 0)),
                status,
                pr_url,
                util.iso(),
            ),
        )


RELEASING_PROPOSALS = ("rejected", "skipped", "filed")


pin_record = run_store.pin_record


unpin_record = run_store.unpin_record


record_pins = run_store.record_pins


def runs_cited_by_kept_proposals(run_ids, connect=reader) -> set[str]:
    marks = ",".join("?" * len(RELEASING_PROPOSALS))
    with connect() as c:
        rows = c.execute(f"SELECT run_id, evidence FROM proposals WHERE status IS NULL OR status NOT IN ({marks})",
                         RELEASING_PROPOSALS).fetchall()
    return {r for r in run_ids if any(r == row["run_id"] or r in (row["evidence"] or "") for row in rows)}


def proposals(status: list[str] | None = None, limit: int = 200, connect=reader) -> list[dict]:
    q, args = "SELECT * FROM proposals", []
    if status:
        q += f" WHERE status IN ({','.join('?' * len(status))})"
        args += status
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as c:
        return [dict(r) for r in c.execute(q, args)]


def proposal(proposal_id: int, connect=reader) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
    return dict(row) if row else None


def move_proposal(proposal_id: int, before: tuple[str, ...], after: str) -> bool:
    with db() as c:
        return c.execute(f"UPDATE proposals SET status = ? WHERE id = ? AND status IN ({','.join('?' * len(before))})",
                         (after, proposal_id, *before)).rowcount > 0


def forget_proposal(proposal_id: int) -> bool:
    return move_proposal(proposal_id, ("memory", "hand"), "filed")


def set_proposal_status_by_pr(pr_url: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE proposals SET status = ? WHERE pr_url = ?", (status, pr_url))


EVAL_LINEAGE = ("engine_sha", "prompts_sha", "prompts_from", "context_sha", "settings", "base_commit",
                "after_step", "last_step")


def add_eval(run_id: str, repo: str, task: str, passed: bool, seconds: float, cost: float, source: str = "",
             lineage: dict | None = None) -> None:
    lineage = lineage or {}
    with db() as c:
        c.execute(
            f"INSERT INTO evals (run_id, repo, task, passed, seconds, cost_usd, ts, source, {', '.join(EVAL_LINEAGE)}) "
            f"VALUES ({','.join('?' * 16)})",
            (run_id, repo, task, int(passed), seconds, cost, util.iso(), source,
             *(lineage.get(k, 0 if k in ("after_step", "last_step") else "") for k in EVAL_LINEAGE)),
        )


BASELINE_SOURCE = "worktree"


def task_history(repo: str, task: str, limit: int = 5, source: str = BASELINE_SOURCE) -> list[dict]:
    with reader() as c:
        rows = c.execute(f"SELECT passed, {', '.join(EVAL_LINEAGE)} FROM evals "
                         "WHERE repo = ? AND task = ? AND source = ? ORDER BY ts DESC LIMIT ?",
                         (repo, task, source, limit))
        return [{**dict(r), "passed": bool(r["passed"])} for r in rows]


last_step_id = run_store.last_step_id


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
    with db() as c:
        before = c.total_changes
        c.executemany(f"INSERT OR IGNORE INTO feedback ({', '.join(FEEDBACK_COLUMNS)}) "
                      f"VALUES ({', '.join('?' * len(FEEDBACK_COLUMNS))})",
                      [(*(r.get(k) for k in FEEDBACK_COLUMNS[:-1]), util.iso()) for r in rows])
        return c.total_changes - before


def feedback_due(repo: str, quiet_since: str) -> list[str]:
    with reader() as c:
        rows = c.execute("SELECT issue, MAX(at_once) AS at_once, MAX(created) AS last FROM feedback f "
                         "WHERE repo = ? AND status = 'new' AND EXISTS (SELECT 1 FROM runs WHERE runs.repo = f.repo "
                         f"AND runs.issue = f.issue AND {run_store.OPEN_PR_RUN} "
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
            f"SELECT * FROM feedback WHERE pr_url IN ({','.join('?' * len(pr_urls))}) "
            "AND status IN ('new', 'taken', 'failed') ORDER BY created", pr_urls)]


def take_feedback(rows: list[dict], run_id: str) -> None:
    with db() as c:
        c.executemany("UPDATE feedback SET status = 'taken', run_id = ? WHERE pr_url = ? AND kind = ? AND gh_id = ?",
                      [(run_id, r["pr_url"], r["kind"], r["gh_id"]) for r in rows])


def settle_feedback(run_id: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE feedback SET status = ? WHERE run_id = ? AND status = 'taken'", (status, run_id))


def feedback_between(since: str, until: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute("SELECT * FROM feedback WHERE COALESCE(recorded, created) >= ? "
                                           "AND COALESCE(recorded, created) < ? ORDER BY pr_url, created",
                                           (since, until))]


def mined_until() -> str | None:
    with reader() as c:
        return c.execute("SELECT MAX(mined_until) FROM runs WHERE kind = 'miner'").fetchone()[0]


def add_mined(pr_url: str, repo: str, issue: str, run_id: str, status: str, why: str = "", task: str = "") -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO mined (pr_url, repo, issue, run_id, status, why, task, ts) "
                  "VALUES (?,?,?,?,?,?,?,?)", (pr_url, repo, issue, run_id, status, why, task, util.iso()))


def mined_row(pr_url: str) -> dict:
    with reader() as c:
        row = c.execute("SELECT * FROM mined WHERE pr_url = ?", (pr_url,)).fetchone()
    return dict(row) if row else {}


def mined_urls() -> set[str]:
    with reader() as c:
        return {r["pr_url"] for r in c.execute("SELECT pr_url FROM mined")}


TRIAGE_COLUMNS = ("ref", "source", "tracker", "repo", "route", "title", "url", "updated", "ready", "code", "size",
                  "questions", "plan", "why", "cost", "ts", "kind", "repos", "assumptions", "in_sprint", "parent",
                  "closed", "checks")
TRIAGE_LISTS = ("questions", "plan", "repos", "assumptions", "checks")
APPROVAL_COLUMNS = ("ref", "repo", "route", "title", "url", "kind", "size", "plan", "assumptions", "why", "ts", "tracker",
                    "sent_by", "checks")
APPROVAL_LISTS = ("plan", "assumptions", "checks")
RESEARCH_COLUMNS = ("ref", "source", "title", "url", "updated", "status", "summary", "report", "error", "run_id",
                    "cost", "ts", "ticket", "basis", "answered_by", "filed_path", "filed_url", "moved", "delivered")


def lists_read(row: dict, keys: tuple[str, ...]) -> dict:
    return {**row, **{k: json.loads(row.get(k) or "[]") for k in keys}}


def lists_written(row: dict, columns: tuple[str, ...], keys: tuple[str, ...]) -> tuple:
    return tuple(json.dumps(row.get(k) or []) if k in keys else row.get(k) for k in columns)


def insert_sql(table: str, columns: tuple[str, ...], verb: str = "INSERT") -> str:
    return f"{verb} INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"


def triage_rows(connect=reader) -> list[dict]:
    with connect() as c:
        return [lists_read(dict(r), TRIAGE_LISTS) for r in c.execute("SELECT * FROM triage")]


def triage_replace(source: str, rows: list[dict]) -> None:
    refs = [r["ref"] for r in rows]
    with db() as c:
        c.execute("DELETE FROM triage WHERE source = ?", (source,))
        c.executemany("DELETE FROM triage WHERE ref = ?", [(ref,) for ref in refs])
        c.executemany(insert_sql("triage", TRIAGE_COLUMNS),
                      [lists_written(r, TRIAGE_COLUMNS, TRIAGE_LISTS) for r in rows])


def triage_put(row: dict) -> None:
    with db() as c:
        c.execute(insert_sql("triage", TRIAGE_COLUMNS, "INSERT OR REPLACE"), lists_written(row, TRIAGE_COLUMNS, TRIAGE_LISTS))


def triage_drop(ref: str) -> None:
    with db() as c:
        c.execute("DELETE FROM triage WHERE ref = ?", (ref,))


def triage_approve(row: dict, sent_by: str) -> None:
    with db() as c:
        c.execute(insert_sql("triage_approvals", APPROVAL_COLUMNS, "INSERT OR REPLACE"),
                  lists_written({**row, "ts": util.iso(), "sent_by": sent_by}, APPROVAL_COLUMNS, APPROVAL_LISTS))


def triage_approvals(since: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        rows = c.execute("SELECT * FROM triage_approvals WHERE ts >= ? ORDER BY ts DESC", (since,))
        return [lists_read(dict(r), APPROVAL_LISTS) for r in rows]


def triage_approval(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM triage_approvals WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return lists_read(dict(r), APPROVAL_LISTS) if r else None


def research_rows(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM research ORDER BY ts DESC")]


def research_get(ref: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM research WHERE ref = ? COLLATE NOCASE", (ref,)).fetchone()
    return dict(r) if r else None


def research_set(ref: str, **fields) -> None:
    row = {**(research_get(ref) or {"ref": ref, "cost": 0.0}), **fields, "ts": util.iso()}
    with db() as c:
        c.execute(insert_sql("research", RESEARCH_COLUMNS, "INSERT OR REPLACE"),
                  tuple(row.get(k) for k in RESEARCH_COLUMNS))


def research_keep(ref: str, **fields) -> None:
    with db() as c:
        c.execute(f"UPDATE research SET {', '.join(f'{k} = ?' for k in fields)} WHERE ref = ? COLLATE NOCASE",
                  (*fields.values(), ref))


def triage_prune(sources: list[str]) -> None:
    with db() as c:
        c.execute(f"DELETE FROM triage WHERE source NOT IN ({','.join('?' * len(sources))})", sources)


def add_usage(row: dict, keep_days: float) -> None:
    with db() as c:
        c.execute(insert_sql("usage", USAGE_COLUMNS), tuple(row.get(k) for k in USAGE_COLUMNS))
        c.execute("DELETE FROM usage WHERE at < ?", (util.iso(util.now() - timedelta(days=keep_days)),))


OPEN_CAPTURE = "status IN ('draft', 'checking', 'filing')"
EDITABLE_CAPTURE = "status IN ('draft', 'checking')"


def capture_draft(connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute(f"SELECT * FROM captures WHERE {OPEN_CAPTURE}").fetchone()
        if not r:
            return None
        draft = dict(r)
        draft["evidence"] = [{**dict(e), "inspection": json.loads(e["inspection"] or "{}")} for e in c.execute(
            "SELECT * FROM capture_evidence WHERE capture = ? ORDER BY id", (draft["id"],))]
        draft["checks"] = {k["name"]: {**dict(k), "detail": json.loads(k["detail"] or "{}")} for k in c.execute(
            "SELECT * FROM capture_checks WHERE capture = ?", (draft["id"],))}
        return draft


def capture_move(draft_id: int, revision: int, was: str, to: str, delivery: str | None = None) -> bool:
    with tx() as c:
        note = "" if delivery is None else ", delivery = ?"
        args = (to, *(() if delivery is None else (delivery,)), draft_id, was, revision)
        return c.execute(f"UPDATE captures SET status = ?{note} WHERE id = ? AND status = ? AND revision = ?",
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
        c.execute("UPDATE captures SET delivery = ? WHERE status = 'filing'" + ("" if draft_id is None else " AND id = ?"),
                  (text, *(() if draft_id is None else (draft_id,))))


def capture_filed(draft_id: int, number: int, url: str, title: str) -> bool:
    now = util.iso()
    with tx() as c:
        changed = c.execute("UPDATE captures SET status = 'filed', issue = ?, issue_url = ?, issue_title = ?, filed = ?, "
                            "updated = ?, delivery = '' WHERE id = ? AND status = 'filing'",
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
        c.execute("UPDATE capture_checks SET result = 'interrupted', reason = ?, checked = ? WHERE result = 'running'"
                  + ("" if draft_id is None else " AND capture = ?"), (reason, util.iso(), *args))
        c.execute("UPDATE captures SET status = 'draft' WHERE status = 'checking'"
                  + ("" if draft_id is None else " AND id = ?"), args)


def capture_checking(connect=reader) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM captures WHERE status = 'checking'").fetchone() is not None


def capture_evidence(evidence_id: int, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT e.* FROM capture_evidence e JOIN captures k ON k.id = e.capture "
                      "WHERE e.id = ? AND k.status IN ('draft', 'checking', 'filing')", (evidence_id,)).fetchone()
        return dict(r) if r else None


def capture_stage(row: dict, admit, keep) -> tuple[dict, bool]:
    now = util.iso()
    with tx() as c:
        draft = c.execute(f"SELECT id, status FROM captures WHERE {OPEN_CAPTURE}").fetchone()
        draft_id = draft["id"] if draft else c.execute(
            "INSERT INTO captures (status, revision, created, updated) VALUES ('draft', 0, ?, ?)", (now, now)).lastrowid
        held = [dict(r) for r in c.execute("SELECT * FROM capture_evidence WHERE capture = ? ORDER BY id", (draft_id,))]
        admit(held, draft["status"] if draft else "draft")
        same = next((r for r in held if r["sha256"] == row["sha256"]), None)
        if same:
            return same, True
        keep(draft_id)
        row = {**row, "capture": draft_id, "added": now}
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
    now = util.iso()
    names = list(fields)
    sets = "".join(f"{n} = ?, " for n in names)
    with tx() as c:
        changed = c.execute(f"UPDATE captures SET {sets}revision = revision + 1, updated = ? WHERE {EDITABLE_CAPTURE}",
                            (*fields.values(), now)).rowcount
        if changed:
            return True
        if c.execute("SELECT 1 FROM captures WHERE status = 'filing'").fetchone():
            return False
        c.execute(f"INSERT INTO captures ({', '.join(names)}, status, revision, created, updated) "
                  f"VALUES ({', '.join('?' * len(names))}, 'draft', 1, ?, ?)", (*fields.values(), now, now))
        return True


def capture_discard() -> int | None:
    with tx() as c:
        draft = c.execute(f"SELECT id FROM captures WHERE {EDITABLE_CAPTURE}").fetchone()
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


def task_add(repo: str, body: str, title: str = "", kind: str = "feat", parent: str = "") -> str:
    ts = util.iso()
    digits = 4
    with db() as c:
        while True:
            tid = f"t-{secrets.randbits(4 * digits):0{digits}x}"
            try:
                c.execute("INSERT INTO tasks (id, repo, title, body, kind, state, parent, created, updated) "
                          "VALUES (?,?,?,?,?,'todo',?,?,?)", (tid, repo, title, body, kind, parent, ts, ts))
                return tid
            except sqlite3.IntegrityError:
                digits += 1


def task_get(tid: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        return dict(r) if r else None


def tasks(repo: str = "", in_state: str = "", connect=reader) -> list[dict]:
    q, args = "SELECT * FROM tasks WHERE 1=1", []
    for column, value in (("repo", repo), ("state", in_state)):
        if value:
            q += f" AND {column} = ?"
            args.append(value)
    with connect() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY created, rowid", args)]


def task_children(parent: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tasks WHERE parent = ? ORDER BY created, rowid", (parent,))]


def task_set_state(tid: str, new: str, from_states: tuple[str, ...] = ()) -> bool:
    q, args = "UPDATE tasks SET state = ?, updated = ? WHERE id = ?", [new, util.iso(), tid]
    if from_states:
        q += f" AND state IN ({','.join('?' * len(from_states))})"
        args.extend(from_states)
    with db() as c:
        return c.execute(q, args).rowcount > 0


def task_comment(tid: str, author: str, body: str) -> bool:
    ts = util.iso()
    with db() as c:
        if not c.execute("UPDATE tasks SET updated = ? WHERE id = ?", (ts, tid)).rowcount:
            return False
        c.execute("INSERT INTO task_comments (task, author, body, ts) VALUES (?,?,?,?)", (tid, author, body, ts))
        return True


def task_comments(tid: str, connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT author, body, ts FROM task_comments WHERE task = ? ORDER BY rowid",
                                           (tid,))]


def tasks_replied(repo: str) -> list[dict]:
    with reader() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE repo = ? AND state = 'needs-info' AND "
            "(SELECT author = 'owner' OR author LIKE 'u:%' FROM task_comments c "
            "WHERE c.task = tasks.id ORDER BY c.rowid DESC LIMIT 1) ORDER BY created, rowid", (repo,))]


def ask_row(r) -> dict:
    return {**dict(r), "questions": json.loads(r["questions"]), "session": json.loads(r["session"] or "{}"),
            "answers": json.loads(r["answers"] or "[]")}


def ask_add(ask_id: str, run: str, repo: str, ticket: str, step: str, call: str, why: str, tried: str,
            questions: list[dict], session: dict) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO asks (id, run, repo, ticket, step, call, why, tried, questions, asked, "
                  "session) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (ask_id, run, repo, str(ticket), step, call, why, tried, json.dumps(questions), util.iso(),
                   json.dumps(session)))


def ask_get(ask_id: str, connect=reader) -> dict | None:
    with connect() as c:
        r = c.execute("SELECT * FROM asks WHERE id = ?", (ask_id,)).fetchone()
        return ask_row(r) if r else None


def asks_of(run: str, status: str = "", connect=reader) -> list[dict]:
    with connect() as c:
        return [ask_row(r) for r in c.execute(
            "SELECT * FROM asks WHERE run = ?" + (" AND status = ?" if status else "") + " ORDER BY asked, rowid",
            (run, status) if status else (run,))]


def open_asks(connect=reader) -> list[dict]:
    with connect() as c:
        return [ask_row(r) for r in c.execute(
            "SELECT * FROM asks WHERE status = 'open' AND NOT EXISTS (SELECT 1 FROM runs a, runs later WHERE "
            "a.id = asks.run AND later.kind = 'pipeline' AND later.repo = a.repo AND later.issue = a.issue AND "
            "COALESCE(later.only_step, '') = '' AND later.rowid > a.rowid) ORDER BY asked, rowid")]


def asks_due(run_ids) -> list[dict]:
    ids = list(run_ids)
    if not ids:
        return []
    with reader() as c:
        return [ask_row(r) for r in c.execute(
            f"SELECT * FROM asks WHERE status = 'answered' AND taken_by = '' AND run IN ({','.join('?' * len(ids))}) "
            "ORDER BY asked, rowid", ids)]


def ask_close(ask_id: str, status: str, answers: list[dict] | None = None) -> bool:
    with db() as c:
        return c.execute("UPDATE asks SET status = ?, answered = ?, answers = ? WHERE id = ? AND status = 'open'",
                         (status, util.iso(), json.dumps(answers or []), ask_id)).rowcount > 0


def asks_retire(run_id: str, status: str) -> None:
    with db() as c:
        c.execute("UPDATE asks SET status = ?, answered = COALESCE(answered, ?) WHERE run = ? AND "
                  "(status = 'open' OR (status = 'answered' AND taken_by = ''))", (status, util.iso(), run_id))


def asks_taken(ids: list[str], run_id: str) -> None:
    with db() as c:
        c.executemany("UPDATE asks SET taken_by = ? WHERE id = ?", [(run_id, i) for i in ids])


def ask_remind(ask_id: str) -> bool:
    with db() as c:
        return c.execute("UPDATE asks SET reminded = ? WHERE id = ? AND status = 'open' AND reminded = ''",
                         (util.iso(), ask_id)).rowcount > 0


def push_add(endpoint: str, p256dh: str, auth: str, label: str, contact: str) -> None:
    with db() as c:
        c.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth, label, contact, created) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
                  "auth = excluded.auth, label = excluded.label, contact = excluded.contact",
                  (endpoint, p256dh, auth, label, contact, util.iso()))


def push_list(connect=reader) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM push_subscriptions ORDER BY created, rowid")]


def push_drop(endpoints: list[str]) -> int:
    with db() as c:
        return sum(c.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (e,)).rowcount for e in endpoints)


def push_succeeded(endpoints: list[str]) -> None:
    with db() as c:
        c.executemany("UPDATE push_subscriptions SET last_success = ? WHERE endpoint = ?",
                      [(util.iso(), e) for e in endpoints])


def asks_asked_before(iso: str) -> list[dict]:
    with reader() as c:
        return [ask_row(r) for r in c.execute("SELECT * FROM asks WHERE status = 'open' AND asked < ? "
                                              "ORDER BY asked, rowid", (iso,))]
