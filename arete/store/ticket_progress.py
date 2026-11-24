from __future__ import annotations

import json
from collections.abc import Iterable

from . import db, events, runs

KIND = "ticket.progress"


def subject(repo: str, ticket: str | int) -> str:
    return f"ticket:{repo}:{ticket}"


def rows(c, repo: str, ticket: str | int):
    return c.execute("SELECT seq, at, data FROM events_log WHERE subject = ? AND kind = ? "
                     "ORDER BY seq DESC", (subject(repo, ticket), KIND))


def get(repo: str, ticket: str | int) -> dict:
    with db.reader() as c:
        if not (row := rows(c, repo, ticket).fetchone()):
            return {}
        status = json.loads(row["data"])
        newest = runs.newest_live_whole_run(repo, ticket) or {}
        expected = {"waiting": "pr", "verifying": "running", "owner-review": "pr"}
        live = (newest.get("id") == status["attempt"] and not newest.get("stop_asked")
                and newest["status"] == expected.get(status["follow_up"]["state"]))
        return {**status, "active": live, "seq": row["seq"], "at": row["at"]}


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


def record(repo: str, ticket: str | int, status: dict | None = None, *, evidence: Iterable[tuple[str, str, str]] = (),
           prerequisite_reads: dict | None = None, only_current: bool = False, declared: bool = False) -> None:
    with db.tx() as c:
        history = [json.loads(row["data"]) for row in rows(c, repo, ticket)]
        previous = history[0] if history else {}
        prior = {k: v for k, v in previous.items() if k not in ("evidence", "updates")}
        if only_current and previous.get("run") != (status or {}).get("run"):
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
            refs, prs = prerequisite_state(previous, prerequisite_reads, deployed, retained)
        current.update(prerequisite_issues=refs, prerequisites=prs)
        attempts: dict[str, set[str]] = {}
        for item in history:
            for cause, attempt in item["evidence"]:
                attempts.setdefault(cause, set()).add(attempt)
        fresh, updates = [], []
        for cause, attempt, message in evidence:
            seen = attempts.setdefault(cause, set())
            if attempt in seen:
                continue
            if seen:
                updates.append({"kind": "recurring-blocker", "cause": cause, "attempt": attempt,
                                "message": message})
            seen.add(attempt)
            fresh.append([cause, attempt])
        known = {u["pr"] for item in history for u in item["updates"] if u["kind"] == "prerequisite-pr"}
        for pr in prs:
            if pr["key"] not in known:
                delivery = "deployed" if pr["deployed_release"] else pr["state"]
                updates.append({"kind": "prerequisite-pr", "pr": pr["key"],
                                "message": f"Found prerequisite PR {pr['url']} ({delivery})."})
        if fresh or updates or current != prior:
            events.emit(KIND, subject(repo, ticket), cls="durable", task=str(ticket) if str(ticket).startswith("t-")
                        else None, data={**current, "evidence": fresh, "updates": updates})
