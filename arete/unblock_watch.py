from __future__ import annotations

import json
import re
import time

from . import config, issue_goal, owner_asks, pipeline, principals, repairs, runlimits, tasks, tracker, unblock, util
from .store import asks as asks_store
from .store import db
from .store import runs as runs_store
from .store import tasks as tasks_store
from .store import unblock as ledger

BATCH = 16
PAGE = BATCH // 2
scan_offsets: dict[str, int] = {}


def blocker(row: dict, saved: dict, asks: list[dict], prs: dict, cycle: list[str]) -> tuple[str, str]:
    if cycle:
        return "dependency-cycle", ""
    if row.get("status") == "needs-info" and any(a["status"] == "open" for a in asks):
        return "ask", ""
    if not row or row.get("stop_asked") or row.get("environment_retry_of"):
        return "", ""
    if row["status"] == "failed" and saved:
        if limit := saved.get("limit") or runlimits.exhausted(saved.get("reason", "")):
            return "budget", limit
    if row["status"] == "failed" and row.get("failure") and json.loads(row["failure"]).get("class") == "work":
        return "delivery", ""
    if any(pr["state"] == "OPEN" and pr["mergeable"] == "CONFLICTING" for pr in prs.values()):
        return "base-conflict", ""
    if any(pr["state"] == "OPEN" and pr.get("failing_checks") for pr in prs.values()):
        return "ci-failure", ""
    if any(pr["state"] == "OPEN" and pr.get("reviewDecision") == "CHANGES_REQUESTED" for pr in prs.values()):
        return "review-feedback", ""
    if any(pr["state"] == "OPEN" and pr["isDraft"] for pr in prs.values()):
        report = pipeline.seam_facts(row["id"])
        if report.get("held") or (report.get("review") or {}).get("verdict") == "request_changes":
            return "draft-hold", ""
    return "", ""


def material(cause: str, limit: str, binding: dict, evidence: dict, row: dict) -> str:
    run = evidence.get("run", {})
    issue = evidence["ticket"]
    grant = ledger.for_run(row["id"]).get("grant", {}) if row else {}
    if grant.get("local_spec") == binding["spec"]:
        issue = {**issue, "description": issue["description"].split("\n\n### Arete recovery clarification", 1)[0]}
    comments = re.sub(r"^### Arete(?: on \d{4}-\d{2}-\d{2})?\n\n.*?(?=^### |\Z)", "", evidence["comments"],
                      flags=re.MULTILINE | re.DOTALL).strip()
    if comments == "## Comments":
        comments = ""
    review = run.get("review") or {}
    return unblock.digest([issue, comments, binding["policy"], binding["heads"], binding["prs"], cause, limit,
                           review.get("verdict"), review.get("problems"), evidence.get("dependencies"),
                           [{"kind": q["kind"], "question": q.get("question"), "name": q.get("name")}
                            for a in unblock.asks_of(row) for q in a["questions"]]])


def notify_if_needed(cfg, repo, row, asks, case):
    secret_asks = [a for a in asks if a["status"] == "open" and
                   all(unblock.missing_secret(repo, q) for q in owner_asks.unstaged(a)["questions"])]
    if secret_asks:
        names = sorted({q["name"] for a in secret_asks for q in owner_asks.unstaged(a)["questions"]})
        unblock.route_owner(cfg, row, secret_asks, "Supply " + ", ".join(names) + " in the secure answer form to continue.")
        ledger.mark(case, "needs_owner", "Waiting for a credential in the secure answer form", unblock.RECHECK)
    return bool(secret_asks)


def finish_decision(cfg, repo, t, source, row, asks, case, attempt, binding, decision):
    action = decision["action"]
    grant = {"binding": binding, "direction": decision.get("direction", "")}
    if action == "wait_for":
        ledger.decide(case, attempt, decision, {})
        ledger.mark(case, "waiting", decision["reason"], unblock.RECHECK)
        return
    if action == "needs_owner":
        owner = unblock.authorised_owner_asks(repo, source, t, asks, decision)
        ledger.decide(case, attempt, decision, {})
        if owner:
            unblock.route_owner(cfg, row, owner, "Open the run's question to provide the required input.")
            ledger.mark(case, "needs_owner", decision["reason"])
        else:
            ledger.mark(case, "waiting", "The owner-only requirement was not confirmed", unblock.RECHECK)
        return
    if action == "answer":
        if case["cause"] != "ask":
            raise ValueError("this blocker has no ask to answer")
        with util.lock(owner_asks.STEER_LOCK, wait=True), db.tx():
            latest = runs_store.newest_live_whole_run(repo.name, t.id)
            if (not latest or latest["id"] != row["id"] or latest["version"] != binding["version"]
                    or unblock.ask_binding(unblock.asks_of(latest)) != binding["asks"]):
                raise ValueError("the ask group changed during diagnosis")
            if binding["task_version"]:
                task = tasks_store.task_get(t.id)
                if not task or task["version"] != binding["task_version"]:
                    raise ValueError("the local task changed before its questions could be answered")
            tasks.stage_recovery_answers(asks, decision, scrub=unblock.safe)
            updated = unblock.asks_of(row)
            if any(a["status"] == "open" for a in updated):
                ledger.decide(case, attempt, {**decision, "answers": {}}, {})
            else:
                grant["binding"] = {**binding, "asks": unblock.ask_binding(updated)}
                ledger.decide(case, attempt, {**decision, "answers": {}}, grant)
        notify_if_needed(cfg, repo, row, updated, case)
        return
    if case["cause"] == "dependency-cycle" and action == "clarify":
        correct_dependency(repo, source, t, case, attempt, binding, decision)
        return
    if case["cause"] in ("ask", "dependency-cycle"):
        raise ValueError("resolve the question or prerequisite before retrying")
    if action == "clarify" and (body := unblock.local_revision(source, t, decision["direction"])):
        grant.update(local_body=body, local_spec=runlimits.digest(t.title, body))
    elif action == "clarify":
        grant["public_note"] = decision["direction"]
    if case["cause"] in ("draft-hold", "base-conflict") and pipeline.recorded_path(row["id"]) != "orchestrate":
        try:
            found = repairs.source(row)
        except repairs.Refused:
            pass
        else:
            repairs.open_heads(repo, found)
            grant["entry"] = "repair"
    ledger.decide(case, attempt, decision, grant)


def correct_dependency(repo, source, t, case, attempt, binding, decision):
    with db.tx():
        if not (body := unblock.dependency_revision(source, t, decision["direction"])):
            raise ValueError("no confirmed, standalone dependency declaration can be corrected safely")
        tasks_in_repo = [row for row in tasks_store.tasks(repo.name) if row["state"] not in config.TASK_FINISHED]
        graph = {row["id"]: tracker.waits_on_tasks(row["body"]) for row in tasks_in_repo}
        before = unblock.cycles(graph)
        graph[t.id] = tracker.waits_on_tasks(body)
        after = unblock.cycles(graph)
        if t.id not in before or any(t.id in members for members in after.values()):
            raise ValueError("the edit would not resolve the observed cycle")
        raw = tasks_store.task_get(t.id)
        if not raw or raw["version"] != binding["task_version"] or not unblock.approval(source, t)[0]:
            raise ValueError("the local ticket changed before its dependency correction")
        if not tasks_store.task_update(t.id, raw["version"], {"body": body}):
            raise ValueError("the local ticket changed before its dependency correction")
        ledger.decide(case, attempt, decision, {"dependency_before": raw["body"], "dependency_after": body})
        ledger.mark(case, "waiting", "Corrected the owner-confirmed declaration; waiting for normal admission")


def inspect(cfg, repo, tid, row, cycle, existing) -> None:
    if not unblock.active(cfg, repo.name, tid) or unblock.exclude(repo.name, tid):
        if existing:
            issue_goal.lifecycle(repo.name, tid, "cancel")
            ledger.mark(existing, "cancelled", "Ticket is no longer delegated for recovery")
        return
    source = tracker.for_ticket(cfg, repo, tid)
    t = source.get(tid)
    if row.get("stop_asked") or row.get("status") == "stopped":
        if existing:
            issue_goal.lifecycle(repo.name, tid, "cancel")
            ledger.mark(existing, "cancelled", "Run stopped by request")
        return
    if not t.open:
        if existing:
            issue_goal.lifecycle(repo.name, tid, "finish")
            ledger.mark(existing, "resolved", "The tracker closed the issue")
        return
    if not unblock.approval(source, t)[0]:
        if existing:
            issue_goal.lifecycle(repo.name, tid, "cancel")
            ledger.mark(existing, "cancelled", "The ticket is no longer approved")
        return
    if not cfg.replay.all and not issue_goal.exists(repo.name, tid):
        issue_goal.lifecycle(repo.name, tid, "start")
    attempt = ledger.latest_attempt(existing) if existing else {}
    if (row and attempt.get("child") == row["id"]
            and attempt["grant"].get("binding", {}).get("spec") == runlimits.specification(t)):
        unblock.public_note(repo, source, tid, attempt)
    if row.get("status") in ("running", "interrupted"):
        if existing:
            ledger.mark(existing, "resuming", f"Following {row['id']}", unblock.INTERVAL)
        return
    if row and runs_store.environment_hold(row["id"]).get("prs"):
        if existing:
            ledger.mark(existing, "waiting", "Waiting for verification environment release", unblock.RECHECK)
        return
    cycle = cycle if t.blocker else []
    if t.blocker and not cycle:
        if existing:
            ledger.mark(existing, "waiting", "Waiting for the recorded prerequisite", unblock.RECHECK)
        return
    asks = unblock.asks_of(row)
    saved = runlimits.metadata(row["id"], "parked.json") if row else {}
    prs = unblock.prs_of(row)
    cause, limit = blocker(row, saved, asks, prs, cycle)
    if not cause and row.get("status") == "failed" and not row.get("environment_retry_of"):
        cause = "delivery"
    if not cause and attempt.get("grant") and not attempt.get("child") and row.get("status") == "needs-info":
        cause = "ask" if not any(a["status"] == "open" for a in asks) else ""
    if not cause:
        if not existing and not row:
            return
        case = existing or ledger.observe(repo.name, tid, "goal", "monitor", {"run": row["id"]})
        if prs and all(pr["state"] == "MERGED" for pr in prs.values()):
            issue_goal.lifecycle(repo.name, tid, "finish")
            ledger.mark(case, "resolved", "The issue's pull requests merged")
        else:
            ledger.mark(case, "waiting", "Monitoring until tracker closure or PR merge", unblock.RECHECK)
        return
    binding = unblock.snapshot(cfg, repo, source, t, row, prs=prs)
    if attempt.get("grant", {}).get("binding") == binding and not attempt["child"]:
        if time.time() >= existing["due"]:
            result = tasks.launch_recovery(cfg, repo, row, attempt)
            if result.get("state") == "queued":
                ledger.mark(existing, "queued", result["why"], unblock.INTERVAL)
            elif result.get("state") == "refused":
                ledger.mark(existing, "waiting", result["why"], unblock.RECHECK)
        return
    evidence = unblock.evidence_for(t, row, saved, asks, prs, cycle)
    case = ledger.observe(repo.name, tid, cause, material(cause, limit, binding, evidence, row), binding)
    if time.time() < case["due"] or case["phase"] == "resuming":
        return
    if ("reconcile before resuming" in (row.get("summary") or "")
            and ledger.for_run(row["id"]).get("grant", {}).get("binding", {}).get("policy", binding["policy"])
            == binding["policy"]):
        ledger.mark(case, "waiting", "The retained run requires policy reconciliation", unblock.RECHECK)
        return
    if asks and notify_if_needed(cfg, repo, row, asks, case):
        if not any(a["status"] == "open" and any(q["kind"] in ("choice", "text")
                   for q in owner_asks.unstaged(a)["questions"]) for a in asks):
            return
    if case["phase"] == "needs_owner" or (attempt.get("decision", {}).get("action") == "wait_for"
            and attempt.get("generation") == case["generation"]):
        ledger.mark(case, case["phase"], case["detail"], unblock.RECHECK)
        return
    attempt_id = ledger.begin(case)
    if not (decision := unblock.diagnose(cfg, repo, {**row, "issue": tid}, attempt_id, evidence, limit)):
        ledger.mark(case, "waiting", "No admissible recovery decision; waiting to recheck", unblock.RECHECK)
        return
    fresh = config.load()
    if not unblock.active(fresh, repo.name, tid) or unblock.exclude(repo.name, tid):
        return
    newest = runs_store.newest_live_whole_run(repo.name, tid) or {}
    if newest.get("id") != row.get("id"):
        return
    t = source.get(tid)
    if unblock.snapshot(fresh, repo, source, t, newest) != binding:
        return
    try:
        finish_decision(fresh, repo, t, source, row, asks, case, attempt_id, binding, decision)
    except ValueError as error:
        ledger.mark(case, "waiting", unblock.safe(error, 300), unblock.RECHECK)


def watch(cfg):
    with util.lock("ticket-unblock") as got, principals.acting(unblock.ACTOR):
        if not got:
            return
        for ask in asks_store.open_asks():
            if ask["recovery_route"] == "unblock" and not unblock.active(cfg, ask["repo"], ask["ticket"]):
                if row := runs_store.run_get(ask["run"]):
                    unblock.route_owner(cfg, row, asks_store.asks_of(row["id"], "open"), "A paused run needs an answer.")
        known = {r.name: r for r in cfg.repos if r.name in cfg.unblock_repos}
        for existing in ledger.cases():
            if existing["repo"] not in known:
                issue_goal.lifecycle(existing["repo"], existing["ticket"], "cancel")
                ledger.mark(existing, "cancelled", "Automatic recovery is disabled")
        for repo in known.values():
            scan_repo(cfg, repo)


def scan_repo(cfg, repo):
    queued = ledger.queue(repo.name)
    leads = {tid: members for source in queued.values() for tid, members in unblock.cycles(source["graph"]).items()}
    rank = {tid: counts for source in queued.values() for tid, counts in source["rank"].items()}
    current = {r["ticket"]: r for r in ledger.cases(repo.name)}
    offset = scan_offsets.get(repo.name, 0)
    page = ledger.candidates(repo.name, PAGE, offset)
    scan_offsets[repo.name] = offset + PAGE if len(page) == PAGE else 0
    rows = {str(r["issue"]): r for r in page}
    due = {tid for tid, case in current.items() if time.time() >= case["due"]}

    def priority(tid):
        return -rank.get(tid, [0, 0])[1], tid

    fresh = sorted(rows.keys() - current.keys(), key=priority)
    # Reserve room for new candidates and let overdue cases take turns, even with a busy queue.
    follow_up = sorted(due | (leads.keys() - current.keys()),
                       key=lambda tid: (bool(tid in current), current.get(tid, {}).get("updated", ""), priority(tid)))
    for tid in (fresh + [tid for tid in follow_up if tid not in fresh])[:BATCH]:
        existing = current.get(tid, {})
        row = rows.get(tid) or runs_store.newest_live_whole_run(repo.name, tid) or {}
        try:
            inspect(cfg, repo, tid, row, leads.get(tid, []), existing)
        except (*tracker.ERRORS, OSError, TypeError, ValueError, RuntimeError) as error:
            if case := ledger.get(repo.name, tid):
                ledger.mark(case, "waiting", unblock.safe(error, 300), unblock.RECHECK)
