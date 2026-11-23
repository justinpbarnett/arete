from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from . import (alerts, config, delegation, environment, factory, flows, gh, launch_lease, outbound,
               owner_asks, paths, pilot, pipeline, principals, prompt_loader, queueing, record, run_lines, runlimits,
               steps, tasks, tracker, util, vault, webpush, zte)
from .agent import json_from_text
from .store import asks as asks_store
from .store import db
from .store import incidents as incidents_store
from .store import orchestration as controllers
from .store import runs as runs_store
from .store import tasks as tasks_store
from .store import unblock as ledger

INTERVAL = 20
RECHECK = 120
ACTOR = "rule:unblock"
HIGH = ("high", "xhigh", "max")
OWNER_DIRECTIVE = "Arete owner action: "
DEPENDENCY_DIRECTIVE = "Arete dependency correction: remove "
EXCLUDED_LABELS = {"no-agent", "agent:hold", tracker.ZTE_LABEL}
CONTROLLER_LIMITS = {"max_decisions", "max_actions"}
ACTIONS = {"retry": {"direction", "limit"}, "clarify": {"direction", "limit"}, "answer": {"answers"},
           "wait_for": set(), "needs_owner": {"asks"}}


def active(cfg, repo: str, ticket: str) -> bool:
    return repo in cfg.unblock_repos and not pilot.is_pilot_task(ticket) and not zte.reserved(ticket)


def exclude(repo: str, ticket: str) -> bool:
    return tracker.is_task_id(ticket) and any(r["repo"] == repo and r["task"] == ticket
                                             for r in incidents_store.incident_repairs())


def safe(text, limit=4000) -> str:
    return outbound.plain(outbound.screen(vault.hidden(str(text or ""))), limit)


def digest(data) -> str:
    return controllers.digest(data)


def publish_queue(repo, source, tickets) -> None:
    graph = {t.id: sorted(queueing.dependencies(t)) for t in tickets if not EXCLUDED_LABELS.intersection(t.labels)}
    ledger.save_queue(repo.name, source.name, {"graph": graph, "rank": queueing.scores(list(tickets))})


def cycles(graph: dict) -> dict[str, list[str]]:
    def reachable(start):
        seen, pending = set(), list(graph.get(start, ()))
        while pending:
            node = pending.pop()
            if node in graph and node not in seen:
                seen.add(node)
                pending.extend(graph[node])
        return seen

    found, seen = {}, set()
    for node in graph:
        if node in seen or node not in (forward := reachable(node)):
            continue
        members = sorted(other for other in forward if node in reachable(other))
        found[members[0]] = members
        seen.update(members)
    return found


def approval(source, t) -> tuple[bool, int]:
    if not t.open or EXCLUDED_LABELS.intersection(t.labels):
        return False, 0
    if isinstance(source, tracker.LocalTracker):
        row = tasks_store.task_get(t.id) or {}
        return row.get("repo") == source.repo.name and row.get("state") in config.TASK_STATES[:-2], row.get("version", 0)
    return tracker.LABEL in t.labels and (not t.blocker or t.blocker.startswith("it waits on ")), 0


def prs_of(row: dict) -> dict:
    out = {}
    for url in (row.get("pr_url") or "").split():
        # Only use PRs on the checkouts already configured for this ticket.
        cfg = config.load()
        repo = cfg.repo(row["repo"])
        if gh.pr_repo(url).casefold() not in {p.github.casefold() for p in repo.parts}:
            raise ValueError("saved PR does not match a configured checkout")
        pr = gh.pr_merge_details(url, timeout=20, retry=False)
        if not pr.get("headRefOid") or not pr.get("state"):
            raise ValueError("the current PR could not be confirmed")
        out[url] = {k: pr.get(k) for k in ("state", "isDraft", "headRefOid", "baseRefOid", "mergeable", "mergeStateStatus")}
    return out


def worktree_of(row: dict) -> dict:
    if not row:
        return {}
    prepared = flows.prepared_data(row["id"])
    out = {}
    for entry in prepared.get("checkouts", []):
        wt = Path(entry["wt"])
        if not wt.is_relative_to(paths.WORKTREES_DIR) or not wt.resolve().is_relative_to(paths.WORKTREES_DIR.resolve()):
            raise ValueError("saved workspace is outside the configured worktrees")
        fingerprint = factory.fingerprint(wt) if wt.exists() else "missing"
        if not fingerprint:
            raise ValueError("saved workspace could not be confirmed")
        out[str(wt)] = fingerprint
    return out


def asks_of(row: dict) -> list[dict]:
    return asks_store.asks_of(row["id"]) if row.get("status") == "needs-info" else []


def ask_binding(asks: list[dict]) -> str:
    return digest([{k: a[k] for k in ("id", "status", "questions", "answers", "staged_answers", "recovery_route")}
                   for a in asks])


def policy(cfg, repo) -> str:
    return digest([delegation.settings_sha(cfg), delegation.settings_sha(config.load_layer(repo.layer_dir))])


def snapshot(cfg, repo, source, t, row, *, prs=None) -> dict:
    approved, version = approval(source, t)
    if not approved:
        raise ValueError("the ticket is no longer approved")
    branch = row.get("branch") or ""
    if not branch and row:
        branch = flows.prepared_data(row["id"]).get("branch", "")
    return {"run": row.get("id", ""), "version": row.get("version", 0), "status": row.get("status", ""),
            "spec": runlimits.specification(t), "comments": digest(t.comments), "task_version": version,
            "policy": policy(cfg, repo),
            "branch": branch, "heads": runlimits.heads(repo, branch), "trees": worktree_of(row),
            "prs": prs_of(row) if prs is None else prs, "asks": ask_binding(asks_of(row))}


def evidence_for(t, row, saved, asks, prs, cycle) -> dict:
    packet = {"ticket": {"id": t.id, "title": safe(t.title, 300), "description": safe(t.body, 6000)},
              "comments": safe(t.comments, 4000)}
    if cycle:
        packet["dependencies"] = {"cycle": cycle}
    if row:
        report = pipeline.report_fields(row, pipeline.seam_facts(row["id"]))
        review = report.get("review") or {}
        packet["run"] = {"status": row["status"], "summary": safe(row.get("summary")),
                         "held": safe(report.get("held")), "merge_held": safe(report.get("merge_held")),
                         "review": {"verdict": safe(review.get("verdict"), 40),
                                    "summary": safe(review.get("summary"), 600),
                                    "problems": safe(ledger.encoded(review.get("problems") or []), 2500)},
                         "finished_steps": report.get("steps", {}), "parked": safe(saved.get("reason"))}
        for step in ("check", "verify", "review", "ci", "catch_up"):
            lines = [line for line in run_lines.read(row["id"]) if line.get("step") == step and line.get("data")]
            if lines:
                packet[step] = safe(ledger.encoded(lines[-1]["data"]), 2400)
    if asks:
        packet["asks"] = {a["id"]: {"why": safe(a["why"], 500), "tried": safe(a["tried"], 500), "questions": {
            str(i): ({"kind": "secret", "name": q["name"]} if q["kind"] == "secret" else
                     {"kind": q["kind"], "question": safe(q["question"], 600),
                      "options": [safe(o["label"], 150) for o in q.get("options", [])]})
            for i, q in enumerate(a["questions"]) if str(i) not in a["staged_answers"]}}
            for a in asks if a["status"] == "open"}
    if prs:
        packet["pull_requests"] = [{k: value for k, value in pr.items() if k not in ("headRefOid", "baseRefOid")}
                                   for pr in prs.values()]
    return packet


def decision_of(raw, evidence: dict, limit: str) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("action"), str) or raw["action"] not in ACTIONS:
        raise ValueError("recovery did not return a recognized action")
    action = raw["action"]
    if raw.keys() != {"action", "evidence", "reason", *ACTIONS[action]}:
        raise ValueError("recovery decision does not match its action schema")
    refs = raw["evidence"]
    if (not isinstance(refs, list) or not refs or len(refs) > len(evidence)
            or any(not isinstance(ref, str) or ref not in evidence for ref in refs)):
        raise ValueError("recovery did not cite evidence available in this observation")
    reason = raw["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1600:
        raise ValueError("recovery requires a bounded reason")
    result = {**raw, "reason": safe(reason, 1600)}
    if action in ("retry", "clarify"):
        if raw["limit"] != limit or not isinstance(raw["direction"], str) or not raw["direction"].strip():
            raise ValueError("recovery must address the recorded blocker with a concrete direction")
        result["direction"] = safe(raw["direction"], 2000)
    if action == "answer" and (not isinstance(raw["answers"], dict) or not raw["answers"]):
        raise ValueError("recovery must answer at least one question")
    if action == "needs_owner" and (not isinstance(raw["asks"], list) or not raw["asks"]
                                    or any(not isinstance(a, str) for a in raw["asks"])):
        raise ValueError("recovery must identify which ask needs the owner")
    return result


def setting_for(cfg, repo):
    layer = config.load_layer(repo.layer_dir)
    configured = cfg.unblock_step
    isolated = replace(cfg, steps={"orchestrate": configured}, kinds={})
    resolved = steps.resolve(isolated, replace(layer, steps={}, kinds={}), "orchestrate",
                             overrides={"tries": 1, "timeout_minutes": min(configured.get("timeout_minutes", 5), 10)})
    if resolved.effort not in HIGH or configured.get("effort") not in HIGH:
        raise ValueError("[unblock.step] needs high or greater reasoning effort")
    # Only explicitly configured routes are admitted, never an automatic replacement.
    return layer, replace(resolved, fallback=False)


def diagnose(cfg, repo, row, attempt: str, evidence: dict, limit: str) -> dict | None:
    layer, setting = setting_for(cfg, repo)
    report = record.start("unblock", repo=repo.name, issue=str(row.get("issue", "")), lock="ticket-unblock")
    run = pipeline.AgentRun(report, repo, paths.RUNS_DIR / report, cfg=cfg, layer=layer)
    run.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    place = run.dir / "controller"
    place.mkdir(mode=0o700)
    prompt = prompt_loader.prompt("unblock", observation=json.dumps({"exhausted_limit": limit, "evidence": evidence}),
                                  verification_policy=prompt_loader.VERIFICATION_POLICY)
    request = delegation.request(SimpleNamespace(run=run, cfg=cfg, layer=layer, ws=place), "unblock", prompt,
                                 setting, (), [])
    request["step"] = "unblock"
    request["closed_context"] = [str(repo.path), str(paths.WORKTREES_DIR)]
    result, status, summary = {}, "failed", "no recovery decision"
    worker = None
    try:
        worker = delegation.Worker(run, "unblock", request)
        while time.monotonic() <= worker.deadline:
            for msg in worker.receive():
                if msg.get("event") == "launch":
                    route = msg["route"]
                    if (route.get("runner") != setting.runner or route.get("model") != setting.model
                            or route.get("effort") not in HIGH and setting.runner != "replay"
                            or not ledger.launched(attempt)):
                        raise ValueError("unblock route or unchanged-blocker launch allowance not admitted")
                    worker.allow()
                else:
                    result = msg
            if result or worker.proc.poll() is not None:
                break
            time.sleep(0.05)
        if result.get("ok"):
            raw = result.get("structured")
            if raw is None:
                raw = json_from_text(result.get("text", ""))
            decision = decision_of(raw, evidence, limit)
            status, summary = "done", decision["action"]
            return decision
        summary = safe(result.get("error") or summary, 300)
    except (OSError, ValueError, TypeError) as error:
        summary = safe(error, 300)
    finally:
        try:
            if worker:
                worker.close()
        finally:
            runs_store.settle(report, {"status": status, "summary": summary, "finished": util.iso(),
                                       "cost_usd": result.get("cost_usd", 0)})
    return None


def grant_of(repo, row, binding, decision) -> dict:
    limit = decision.get("limit", "")
    layer = config.load_layer(repo.layer_dir)
    prior = runlimits.metadata(row["id"], "budget.json").get("recovery", {}) if row else {}
    allowance = dict(prior)
    if limit in runlimits.LIMIT_TRANCHES:
        allowance[limit] = allowance.get(limit, 0) + min(getattr(layer, limit), runlimits.LIMIT_TRANCHES[limit])
    elif limit in CONTROLLER_LIMITS:
        allowance[limit] = allowance.get(limit, 0) + getattr(layer.orchestration, limit)
    return {"binding": binding, "allowance": allowance, "direction": decision.get("direction", "")}


def route_owner(cfg, row, asks, text: str) -> None:
    ids = []
    with util.lock(owner_asks.STEER_LOCK, wait=True), db.tx():
        newest = runs_store.newest_live_whole_run(row["repo"], row["issue"])
        if not newest or newest["id"] != row["id"] or newest["status"] != "needs-info":
            return
        for ask in asks:
            if asks_store.route_to_owner(ask["id"]):
                ids.append(ask["id"])
        if not ids or not ledger.notice(row["id"]):
            return
    alerts.ask_notice(tracker.run_ticket_name(row), text)
    for note in webpush.ask_opened(cfg, ids):
        runs_store.add_feed(row["id"], "unblock", note)


def missing_secret(repo, q) -> bool:
    return q["kind"] == "secret" and not environment.stored(repo).get(q["name"]) and q["name"] not in vault.read(repo.name)


def owner_lines(source, tid):
    if isinstance(source, tracker.LocalTracker):
        comments = [r["body"] for r in tasks_store.task_comments(tid) if principals.is_person(r["author"])]
    elif isinstance(source, tracker.GitHubTracker):
        comments = [r.get("body", "") for r in gh.issue(source.repo.github, int(tid)).get("comments", ())
                    if source.by_owner(r)]
    else:
        me = source.client.me().get("accountId")
        comments = [c.get("body", "") for c in source.client.comments(tid)
                    if me and (c.get("author") or {}).get("accountId") == me]
    return {line.strip() for c in comments if c and not tracker.from_arete(c) for line in c.splitlines()}


def authorised_owner_asks(repo, source, t, asks, decision) -> list[dict]:
    lines = owner_lines(source, t.id) if OWNER_DIRECTIVE in t.comments else set()
    return [a for a in asks if a["id"] in decision["asks"] and a["status"] == "open" and (
        all(missing_secret(repo, q) for q in owner_asks.unstaged(a)["questions"]) or
        f"{OWNER_DIRECTIVE}{a['id']}" in lines)]


def dependency_revision(source, t, direction: str) -> str:
    if not isinstance(source, tracker.LocalTracker) or direction not in owner_lines(source, t.id):
        return ""
    if not direction.startswith(DEPENDENCY_DIRECTIVE):
        return ""
    parent = direction.removeprefix(DEPENDENCY_DIRECTIVE)
    if parent not in queueing.dependencies(t):
        return ""
    original = t.body.splitlines(keepends=True)
    standalone = re.compile(r"\s*(?:[-*+]\s+)?(?:waits on|blocked by):\s*`?" + re.escape(parent) + r"`?\.?\s*",
                            re.IGNORECASE)
    kept = [line for line in original if not standalone.fullmatch(line)]
    if len(kept) != len(original) - 1:
        return ""
    body = "".join(kept)
    return body if set(tracker.waits_on_tasks(body)) == queueing.dependencies(t) - {parent} else ""


def local_revision(source, t, direction: str) -> str:
    body = t.body + "\n\n### Arete recovery clarification\n\n" + direction
    updated = replace(t, body=body)
    if queueing.dependencies(t) != queueing.dependencies(updated) or tracker.batch_ids(t) != tracker.batch_ids(updated):
        raise ValueError("a recovery clarification cannot change ticket directives")
    return body if isinstance(source, tracker.LocalTracker) else ""


def plan_launch(cfg, repo, attempt: dict, row: dict) -> dict:
    case = ledger.get(repo.name, str(row["issue"]))
    if not ledger.allowance(case):
        ledger.mark(case, "waiting", "Autonomous recovery allowance exhausted", RECHECK)
        return {}
    return tasks.launch_recovery(cfg, repo, row, attempt)


def child_grant(cfg, repo, source, tid, *, force, script, dry_run) -> tuple[dict, dict]:
    key = launch_lease.recovery(repo.name, tid)
    if not key:
        return {}, {}
    attempt = ledger.attempt_of(key)
    case = ledger.get(repo.name, tid)
    row = runs_store.newest_live_whole_run(repo.name, tid)
    mode = pipeline.recorded_path(row["id"]) if row else ""
    repairing = attempt.get("grant", {}).get("entry") == "repair"
    if script and row:
        expected = ("repair", row["id"], "") if repairing else (mode, "", row["id"])
        bad_script = (script.path, script.repairs, script.continues) != expected or bool(
            script.only or script.action or script.overrides or script.environment_release)
    else:
        bad_script = repairing or mode != "classic"
    if (not active(cfg, repo.name, tid) or exclude(repo.name, tid) or force or dry_run or bad_script
            or not row or attempt.get("child") or attempt.get("generation") != case.get("generation")
            or (attempt.get("repo"), attempt.get("ticket")) != (repo.name, tid)
            or attempt.get("decision", {}).get("action") not in ("retry", "clarify", "answer")):
        raise ValueError("recovery admission is stale or outside the delegated ticket")
    t = source.get(tid)
    if t.blocker or attempt["grant"].get("binding") != snapshot(cfg, repo, source, t, row):
        raise ValueError("the ticket, ask, policy, or retained heads changed before recovery launch")
    if any(a["status"] == "open" for a in asks_of(row)):
        raise ValueError("the waiting run still has unresolved asks")
    return attempt, case


def register(repo, attempt, case, start) -> str:
    binding = attempt["grant"]["binding"]
    cfg = config.load()
    if not active(cfg, repo.name, case["ticket"]) or binding["policy"] != policy(cfg, repo):
        raise ValueError("recovery policy changed before registration")
    with db.tx():
        latest = runs_store.newest_live_whole_run(repo.name, case["ticket"])
        if not latest or (latest["id"], latest["version"]) != (binding["run"], binding["version"]):
            raise ValueError("the source run changed before registration")
        if binding["asks"] != ask_binding(asks_of(latest)):
            raise ValueError("the source asks changed before registration")
        if binding["task_version"]:
            local = tasks_store.task_get(case["ticket"])
            if not local or local["version"] != binding["task_version"]:
                raise ValueError("the local task changed before registration")
            if body := attempt["grant"].get("local_body"):
                if not tasks_store.task_update(local["id"], local["version"], {"body": body}):
                    raise ValueError("the local task could not be clarified at its approved version")
        run = start()
        ledger.register(case, attempt["id"], run)
    launch_lease.registered(repo.name, case["ticket"])
    return run


def inherited(row: dict, specification) -> dict:
    if not row or row.get("status") != "interrupted":
        return {}
    for run in runlimits.family(row["id"]):
        attempt = ledger.for_run(run)
        if (attempt.get("repo"), attempt.get("ticket")) != (row["repo"], str(row["issue"])):
            continue
        grant = attempt["grant"]
        bound_spec = grant.get("local_spec") or grant["binding"]["spec"]
        return grant if bound_spec == specification() else {}
    return {}


def brief(run, text: str) -> str:
    direction = run.recovery.get("direction", "")
    return "\n\n".join(filter(None, (text, f"## Recovery guidance from the Arete unblocker\n\n{direction}" if direction else "")))


def public_note(repo, source, tid, attempt):
    if attempt["grant"].get("public_posted") or not (note := attempt["grant"].get("public_note")):
        return
    marker = f"<!-- arete:recovery:{attempt['id']} -->"
    screened = outbound.inert(outbound.screen(safe(note, 2000)))
    body = tracker.ARETE_TAG + f"\n\nArete unblocker clarification:\n\n{screened}\n\n{marker}"
    if isinstance(source, tracker.GitHubTracker):
        if not gh.find_issue_comment_marker(repo.github, int(tid), marker):
            gh.issue_comment(repo.github, int(tid), body, single_attempt=True)
    elif isinstance(source, tracker.JiraTracker):
        if not source.client.find_comment_marker(tid, marker):
            source.client.comment(tid, body, single_attempt=True)
    ledger.posted(attempt)
