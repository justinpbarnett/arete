from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable

from . import db, events, runs

KIND = "ticket.progress"
INTERRUPTED_RETRY_STATUSES = ("interrupted", "stopped")


def subject(repo: str, ticket: str | int) -> str:
    return f"ticket:{repo}:{ticket}"


def rows(c, repo: str, ticket: str | int, *, newest_first: bool = True):
    order = "DESC" if newest_first else "ASC"
    return c.execute("SELECT seq, at, data FROM events_log WHERE subject = ? AND kind = ? AND class = 'durable' "
                     f"ORDER BY seq {order}", (subject(repo, ticket), KIND))


def owner_review_started_at(repo: str, issue: str | int, run_id: str, attempt_id: str, *,
                            connect=db.reader) -> str | None:
    with connect() as c:
        for row in rows(c, repo, issue, newest_first=False):
            data = json.loads(row["data"])
            if (data["run"], data["attempt"], data["follow_up"]["state"]) == (run_id, attempt_id, "owner-review"):
                return row["at"]
    return None


def get(repo: str, ticket: str | int) -> dict:
    with db.reader() as c:
        if not (row := rows(c, repo, ticket).fetchone()):
            return {}
        status = json.loads(row["data"])
        newest = runs.newest_live_whole_run(repo, ticket) or {}
        state = status["follow_up"]["state"]
        expected = {"waiting": "pr", "verifying": "running", "owner-review": "pr"}.get(state)
        live = newest.get("id") == status["attempt"]
        if live and state == "owner-review" and (status["blocker"] or {}).get("kind") == "retry-interrupted":
            hold = runs.environment_hold(status["run"])
            live = (newest["status"] in INTERRUPTED_RETRY_STATUSES and hold.get("released") == 0
                    and hold["retry_id"] == status["attempt"])
        elif (live := live and newest["status"] == expected and not newest.get("stop_asked")):
            if (hold := runs.environment_hold(status["run"])) and hold["released"]:
                live = state == hold["release_state"] == "owner-review" and not hold["owner_review_ended_at"]
        return {**status, "active": live, "seq": row["seq"], "at": row["at"]}


def declared_prerequisite_tickets(repo: str) -> list[str]:
    prefix = f"ticket:{repo}:"
    with db.reader() as c:
        return [row[0] for row in c.execute("""
            SELECT substr(e.subject, ?) AS ticket FROM events_log e
            JOIN (SELECT MAX(seq) AS seq FROM events_log WHERE kind = ? AND substr(subject, 1, ?) = ?
                  GROUP BY subject) latest ON latest.seq = e.seq
            WHERE json_array_length(e.data, '$.declared_prerequisites') > 0 ORDER BY ticket
        """, (len(prefix) + 1, KIND, len(prefix), prefix))]


def prerequisite_state(previous: dict, reads: dict, deployed: dict, retained: dict) -> tuple[dict, list[dict]]:
    pulls = {}
    observed = set()
    for issue in sorted(reads.keys() | retained.keys()):
        found = reads.get(issue)
        known = found if found is not None else [p for p in previous.get("prerequisites", ()) if issue in p["issues"]]
        for pr in known:
            key = pr["key"]
            prior = pulls.get(key, {})
            chosen = prior if found is None and key in observed else pr
            if found is not None:
                observed.add(key)
            pulls[key] = {**chosen, "issues": [*prior.get("issues", ()), issue],
                          "deployed_release": deployed.get((key, chosen["merge_commit"])) or chosen["deployed_release"]}
    refs = {**retained, **{ref: value is not None for ref, value in reads.items()}}
    return refs, sorted(pulls.values(), key=lambda p: p["key"])


def record_evidence(run_id: str, evidence: Iterable[tuple[str, str, str]]) -> None:
    if ((run := runs.run_get(run_id)) and run.get("kind") == "pipeline" and run.get("repo") and run.get("issue")
            and not (run.get("only_step") or run.get("dry_run") or run.get("status") == "dry-run")
            and (fresh := list(evidence))):
        record(run["repo"], run["issue"], evidence=fresh)


def record(repo: str, ticket: str | int, status: dict | None = None, *, evidence: Iterable[tuple[str, str, str]] = (),
           prerequisite_reads: dict | None = None, only_current: bool = False, declared: bool = False,
           declared_refs: Iterable[str] | None = None) -> None:
    with db.tx() as c:
        history = [json.loads(row["data"]) for row in rows(c, repo, ticket)]
        prior = {k: v for k, v in (history[0] if history else {}).items() if k not in ("evidence", "updates")}
        if only_current and prior.get("run") != (status or {}).get("run"):
            return
        current = {**prior, **(status or {})}
        if not current.get("follow_up"):
            current.update(run="", attempt="", blocker=None, next_event="",
                           follow_up={"state": "inactive", "prs": [], "draft": None})
        refs, prs = prior.get("prerequisite_issues", {}), prior.get("prerequisites", [])
        if prerequisite_reads is not None:
            deployed = {(p["key"], p["merge_commit"]): p["deployed_release"] for item in history
                        for p in item["prerequisites"] if p["deployed_release"]}
            retained = {}
            if declared:
                old_declared = set(prior.get("declared_prerequisites", ()))
                if not old_declared and not prerequisite_reads:
                    return
                retained = {ref: read for ref, read in refs.items() if ref not in old_declared}
                current["declared_prerequisites"] = sorted(prerequisite_reads)
            elif declared_refs or (declared_refs is not None and "declared_prerequisites" in prior):
                current["declared_prerequisites"] = sorted(declared_refs)
            refs, prs = prerequisite_state(prior, prerequisite_reads, deployed, retained)
        current.update(prerequisite_issues=refs, prerequisites=prs)
        attempts = defaultdict(set)
        for cause, attempt in (pair for item in history for pair in item["evidence"]):
            attempts[cause].add(attempt)
        fresh, updates = [], []
        for cause, attempt, message in evidence:
            seen = attempts[cause]
            if attempt not in seen:
                if seen:
                    updates.append(dict(kind="recurring-blocker", cause=cause, attempt=attempt, message=message))
                seen.add(attempt)
                fresh.append([cause, attempt])
        known = {u["pr"] for item in history for u in item["updates"] if u["kind"] == "prerequisite-pr"}
        for pr in prs:
            if pr["key"] not in known:
                delivery = "deployed" if pr["deployed_release"] else pr["state"]
                updates.append(dict(kind="prerequisite-pr", pr=pr["key"],
                                    message=f"Found prerequisite PR {pr['url']} ({delivery})."))
        if fresh or updates or current != prior:
            events.emit(KIND, subject(repo, ticket), cls="durable", task=str(ticket) if str(ticket).startswith("t-")
                        else None, data={**current, "evidence": fresh, "updates": updates})
