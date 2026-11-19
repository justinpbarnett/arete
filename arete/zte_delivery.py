from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from . import config, delegation, jev, launch_lease, paths, pilot, record, scratch, util, workspace, zte
from .store import db, orchestration
from .store import zte as store
from .zte_github import GitHub, Identity

INTENTS = {"push_intent", "pr_intent", "merge_intent"}
JEV_STEP = "ZTE exact patch"
JEV_USAGE_PENDING = paths.ZTE_AUTHORITY / "jev-response.pending"


def settled(delivery, policy, repo, layer) -> bool:
    data = delivery.data
    run = pilot.scheduled_run(delivery.repo, delivery.task)
    if run and not run.get("finished"):
        return False
    pilot.require(run and (run["id"], run["status"]) == (data["run"], "pr"),
                  "the original run did not settle an exact candidate")
    row = orchestration.find(run["id"])
    pilot.require(row and row["root"] == row["current_run"] == run["id"] and row["generation"] == 1,
                  "the original run generation changed")
    if util.pid_alive(row["owner"]):
        return False
    lease = orchestration.Lease(run["id"], run["id"], 1)
    actions = orchestration.actions(lease)
    completed = [a for a in actions if a["op"] == "publication.request" and a["state"] == "succeeded"]
    pilot.require(not orchestration.pending(lease) and len(completed) == 1
                  and completed[0]["result"].get("prepared_head") == data["head"]
                  and all(a["state"] in ("succeeded", "failed", "cancelled") and a["op"] != "owner.ask"
                          and not delegation.live(a["result"].get("pid"), a["result"].get("identity")) for a in actions),
                  "the candidate has an unresolved or live producer")
    checkout = zte.check_checkout(delivery, policy, repo, [workspace.Checkout.from_row(c)
        for c in row["data"].get("prepared", {}).get("checkouts", [])])
    where = Path(data["worktree"])
    pilot.require(checkout.wt == where and not scratch.pending(where), "the workspace or producer state changed")
    signal = policy.signal(delivery.base)
    pilot.require(zte.task_version(delivery, signal, "pr") == data["pr_task_version"],
                  "the ZTE task changed after candidate preparation")
    pilot.require(pilot.git(where, "rev-parse", "HEAD").strip() == data["head"]
                  and pilot.exact_edit(where, signal, data["head"], signal.expected(pilot.readme(where, signal.base)))
                  == data["tree"] and not pilot.git(where, "status", "--porcelain", "--untracked-files=all"),
                  "the exact ZTE candidate changed after preparation")
    pilot.require_evidence(row["data"], policy.identity.name, data["tree"], layer.checks)
    return True


def check_stop(delivery, policy, *, engine: str = "") -> None:
    pilot.require(store.find(delivery.task) == delivery and zte.active(delivery, engine=engine) == policy,
                  "ZTE authority changed")
    pilot.require(not record.stop_asked(delivery.data["run"]), "the original run was stopped")
    pilot.require(zte.task_version(delivery, policy.signal(delivery.base), "pr") == delivery.data["pr_task_version"],
                  "the reserved task changed before an external effect")


def require_landing(delivery, client, expected: str = "", observed: dict | None = None) -> dict:
    data = delivery.data
    pull = client.pull(data["pr"])
    if observed is not None:
        observed.update({"pr": data["pr"], "merged": pull.get("merged"), "sha": pull.get("merge_commit_sha"),
                         "target": (pull.get("base") or {}).get("ref"), "head": (pull.get("head") or {}).get("sha"),
                         "actor": (pull.get("merged_by") or {}).get("id")})
    client.require_pull(pull, node=data["pr_node"], head=data["head"], branch=data["branch"])
    landed = pull.get("merge_commit_sha") or ""
    pilot.require(pull.get("state") == "closed" and pull.get("merged") is True and pilot.SHA.fullmatch(landed)
                  and (pull.get("merged_by") or {}).get("id") == client.identity.bot
                  and (not expected or expected == landed) and pull.get("merged_at")
                  and util.parse_iso(pull["merged_at"]) >= util.parse_iso(data["merge_sent_after"]),
                  "GitHub has not confirmed the exact squash and delivery identity")
    commit = client.commit(landed)
    pilot.require(commit.get("tree", {}).get("sha") == data["tree"]
                  and [p["sha"] for p in commit["parents"]] == [delivery.base] and client.tip() == landed,
                  "the observed landing has a different parent, tree or current main")
    return {"release": landed, "merge_actor": pull["merged_by"]["id"], "merged_at": pull["merged_at"]}


def settle_pr(delivery, client) -> None:
    data = delivery.data
    found = client.branch_prs(data["branch"])
    pilot.require(len(found) == 1, "GitHub did not confirm a unique PR; no creation retry")
    pull = found[0]
    client.require_pull(pull, node=pull["node_id"], head=data["head"], branch=data["branch"])
    pilot.require(pull.get("state") == "open" and pull.get("draft") is False
                  and pull.get("base", {}).get("sha") == delivery.base and pull.get("created_at")
                  and util.parse_iso(pull["created_at"]) >= util.parse_iso(data["pr_sent_after"]),
                  "GitHub did not confirm the authorized fresh PR")
    returned = data.get("created_pr")
    pilot.require(not returned or returned == pull["node_id"], "GitHub returned another PR after creation")
    with db.tx(timeout=0):
        store.transition(delivery, "pr_ready", pr=pull["number"], pr_node=pull["node_id"], pr_url=pull["html_url"])
        record.note(data["run"], branch=data["branch"], pr_url=pull["html_url"])


def ready_to_squash(delivery, client) -> bool:
    data = delivery.data
    pull = client.pull(data["pr"])
    client.require_pull(pull, node=data["pr_node"], head=data["head"], branch=data["branch"])
    return client.readiness(pull, delivery.base)


def send_next(delivery, policy, client, cfg, repo) -> None:
    data = delivery.data
    branch, head, tree = data["branch"], data["head"], data["tree"]
    where = Path(data["worktree"])
    proof = risk = None
    ci_scope = {"base": delivery.base, "pr": data.get("pr", 0), "branch": branch}
    if delivery.phase == "pr_ready":
        from . import zte_release

        proof = client.ci(head, tree, "pull_request", **ci_scope)
        if not proof or not ready_to_squash(delivery, client):
            return
        deadline = zte.wall_deadline(delivery)
        pilot.require(jev.ready(cfg, [repo]), "JEV is unavailable for this exact patch")
        remaining = deadline - time.monotonic()
        pilot.require(remaining > 0, jev.DEADLINE_EXPIRED)
        patch = util.git(["diff", "--no-ext-diff", "--no-textconv", delivery.base, head], where,
                         timeout=min(remaining, 30), retry=False)
        pilot.require(patch, "the exact patch is not available to JEV")
        delivery, risk = screen_patch(delivery, policy, cfg, patch, deadline)
        if not reconcile_jev_usage(deadline=deadline):
            return
        check_jev_deadline(deadline)
        zte_release.preflight(delivery, policy)
    source = client.push_source(where, head, tree, delivery.base) if delivery.phase == "prepared" else nullcontext()
    with source as isolated:
        send_authorized(delivery, policy, client, isolated, proof, risk, ci_scope)


def screen_patch(delivery, policy, cfg, patch: str, deadline: float):
    data = delivery.data
    subject = {"patch": orchestration.digest(patch), "tree": data["tree"], "head": data["head"],
               "model": cfg.jev.model, "risk_hold": cfg.jev.risk_hold, "irreversible_hold": cfg.jev.irreversible_hold}
    if (receipt := data.get("jev_receipt")) is not None:
        pilot.require(data.get("jev_attempt", {}).get("subject") == subject and not receipt["hold"]
                      and all(receipt.get(key) == value for key, value in subject.items()),
                      "the exact JEV receipt no longer matches the candidate")
        return delivery, receipt
    pilot.require("jev_attempt" not in data, "JEV attempt has no confirmed verdict; usage may be unknown, no retry")

    def check_deadline():
        check_jev_deadline(deadline)

    try:
        with util.lock(zte.INTERLOCK) as got:
            pilot.require(got and not JEV_USAGE_PENDING.exists(), "JEV screening is busy or has pending usage")
            with db.tx(timeout=0, before_commit=check_deadline):
                check_stop(delivery, policy)
                check_deadline()
                delivery = store.annotate(delivery, jev_attempt={"subject": subject, "at": util.iso()})
        with new_jev_journal(data["run"], deadline) as response, util.lock(zte.INTERLOCK) as got:
            pilot.require(got, "the Stop interlock is busy before the JEV request")
            with db.one_read(timeout=0):
                check_stop(delivery, policy)
            result = jev.risk(patch, policy.signal(delivery.base).task(), cfg, deadline=deadline, receipt=response)
    except jev.Unjudged as error:
        raise pilot.Held(f"JEV could not judge the exact patch: {error}") from error
    check_deadline()
    pilot.require(not result.hold, result.hold)
    receipt = {**asdict(result), **subject, "at": util.iso()}
    with util.lock(zte.INTERLOCK) as got:
        pilot.require(got, "the Stop interlock is busy after the JEV request")
        with db.tx(timeout=0, before_commit=check_deadline):
            check_stop(delivery, policy)
            delivery = store.annotate(delivery, jev_receipt=receipt)
    return delivery, receipt


def check_jev_deadline(deadline: float) -> None:
    pilot.require(time.monotonic() < deadline, jev.DEADLINE_EXPIRED)


def new_jev_journal(run: str, deadline: float):
    path = JEV_USAGE_PENDING
    with paths.directory_fd(path.parent) as parent:
        launch_lease.private_directory(parent)
        util.write_durably(path, json.dumps({"run": run}).encode() + b"\n", mode=0o600,
                           before_replace=lambda: check_jev_deadline(deadline))
        fd = os.open(path.name, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    journal = os.fdopen(fd, "r+b")
    try:
        pilot.require(launch_lease.plain(os.fstat(fd)), "unsafe JEV response journal")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal.seek(0, os.SEEK_END)
        return journal
    except BaseException:
        journal.close()
        raise


def reconcile_jev_usage(*, deadline: float | None = None) -> bool:
    path = JEV_USAGE_PENDING
    try:
        with paths.directory_fd(path.parent) as parent:
            launch_lease.private_directory(parent)
            fd = os.open(path.name, os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return True
    with os.fdopen(fd, "rb") as journal:
        try:
            # The transport inherits this file description. An orphaned curl may still be writing it.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        pilot.require(launch_lease.plain(os.fstat(fd)), "unsafe JEV response journal")
        run = json.loads(journal.readline(512))["run"]
        marker = journal.read(1)
        pilot.require(marker in (b"", jev.REQUEST_ADMITTED), "invalid JEV response journal marker")
        if not marker:
            path.unlink()
            util.flush_folder(path.parent)
            return True
        reply, seconds = jev.journal_reply(journal, jev.risk_questions())
        usage = jev.result([reply])
        usage.model = usage.model[:128].encode(errors="replace").decode()

        def check_deadline():
            if deadline is not None:
                check_jev_deadline(deadline)

        check_deadline()
        util.flush_fd(fd)
        check_deadline()
        try:
            with db.tx(timeout=0, before_commit=check_deadline) as conn:
                delivery = store.latest()
                pilot.require(delivery and delivery.data.get("run") == run and "jev_attempt" in delivery.data,
                              "JEV response journal has no matching delivery attempt")
                if "jev_receipt" not in delivery.data and usage.ok:
                    usage.ok = False
                    usage.error = "provider usage recovered without an accepted JEV verdict"
                prior = conn.execute("SELECT ok, model, cost_usd, tokens_in, error, seconds FROM steps "
                                     "WHERE run_id=? AND step=? AND try=1", (run, JEV_STEP)).fetchone()
                if prior is None:
                    pilot.require(conn.execute("SELECT 1 FROM runs WHERE id=?", (run,)).fetchone(), "JEV usage has no run")
                    record.step(run, JEV_STEP, usage, seconds, try_no=1)
                else:
                    pilot.require(tuple(prior) == (usage.ok, usage.model, usage.cost_usd, usage.tokens_in,
                                                  usage.error, seconds), "JEV usage conflicts with a recorded step")
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise
            return False
        check_deadline()
        path.unlink()
        util.flush_folder(path.parent)
    return True


def database_busy(error: sqlite3.OperationalError) -> bool:
    return (getattr(error, "sqlite_errorcode", 0) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


class HostedDeadlineExpired(pilot.Held):
    pass


def send_authorized(delivery, policy, client, isolated, proof, risk, ci_scope) -> None:
    deadline = zte.wall_deadline(delivery)

    def check_deadline():
        if time.monotonic() >= deadline:
            raise HostedDeadlineExpired("ZTE grant expired at the hosted write boundary")

    data = delivery.data
    branch, head, tree = data["branch"], data["head"], data["tree"]
    check_deadline()
    token = client.token()
    policy.audit(client)
    if proof:
        pilot.require(client.ci(head, tree, "pull_request", **ci_scope) == proof,
                      "CI provenance changed during risk screening")
    if delivery.phase == "prepared":
        pilot.require(not client.branch_head(branch) and client.tip() == delivery.base,
                      "the reserved branch exists or protected main moved before push")
    elif delivery.phase == "pushed":
        pilot.require(client.branch_head(branch) == head and not client.branch_prs(branch)
                      and client.tip() == delivery.base, "the reserved branch, PR or base changed after push")
    else:
        pilot.require(delivery.phase == "pr_ready" and proof and risk, "no authorized ZTE squash")
        if not ready_to_squash(delivery, client):
            return
    pr_body = policy.signal(delivery.base).task() if delivery.phase == "pushed" else ""
    response = {}
    with util.lock(zte.INTERLOCK) as got:
        if not got:
            return
        pilot.require(zte.settings(config.load(), policy)[2] == data["config"], "ZTE configuration changed")
        check_deadline()
        try:
            with db.tx(timeout=0, before_commit=check_deadline):
                check_stop(delivery, policy)
                sent = util.iso()
                if delivery.phase == "prepared":
                    intent = store.transition(delivery, "push_intent", push_sent_after=sent)
                elif delivery.phase == "pushed":
                    intent = store.transition(delivery, "pr_intent", pr_sent_after=sent)
                else:
                    intent = store.transition(delivery, "merge_intent", pr_ci=proof, risk=risk, merge_sent_after=sent)
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise
            return
        check_deadline()
        if intent.phase == "push_intent":
            client.push(isolated, branch, head, token)
        elif intent.phase == "pr_intent":
            created = client.create(branch, pilot.TASK_TITLE, pr_body, token)
            if isinstance(created.get("node_id"), str):
                response["created_pr"] = created["node_id"]
        else:
            reply = client.squash(data["pr"], head, token)
            if reply.get("merged") is True and pilot.SHA.fullmatch(reply.get("sha") or ""):
                response["returned_merge"] = reply["sha"]

    # A missing response hint leaves the committed intent for read-only reconciliation, not a resend.
    if response and time.monotonic() < deadline:
        try:
            with db.tx(timeout=0, before_commit=check_deadline):
                store.annotate(intent, **response)
        except HostedDeadlineExpired:
            return
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise


def reconcile(delivery, client, observed: dict) -> None:
    if delivery.phase == "push_intent":
        pilot.require(client.branch_head(delivery.data["branch"]) == delivery.data["head"],
                      "the exact push effect is not observable; no retry")
        with db.tx(timeout=0):
            store.transition(delivery, "pushed")
    elif delivery.phase == "pr_intent":
        settle_pr(delivery, client)
    else:
        receipt = require_landing(delivery, client, expected=delivery.data.get("returned_merge", ""), observed=observed)
        with db.tx(timeout=0):
            store.transition(delivery, "merged", **receipt)


def advance(cfg) -> None:
    try:
        with db.one_read(timeout=0):
            delivery = store.latest()
    except sqlite3.OperationalError as error:
        if not database_busy(error):
            raise
        return
    if not delivery or delivery.phase in store.TERMINAL | {"release_ready", "deploy_intent", "rollback_intent"}:
        return
    observed: dict = {}
    if delivery.phase in INTENTS:
        timestamp = delivery.data[f"{delivery.phase.removesuffix('_intent')}_sent_after"]
        until = util.parse_iso(timestamp) + timedelta(minutes=3)
        try:
            identity = Identity(**delivery.data["github"])
            client = GitHub(identity, util.iso(max(until, util.now() + timedelta(seconds=25))), Path("/dev/null"),
                            observer=zte.OBSERVER_FILE)
            reconcile(delivery, client, observed)
        except sqlite3.OperationalError as error:
            if not database_busy(error):
                raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
            if util.now() >= until or (observed.get("merged") and observed.get("target") != "main"):
                try:
                    zte.hold(delivery, error, uncertain=True, timeout=0,
                             **({"observed_landing": observed} if observed else {}))
                except sqlite3.OperationalError as busy:
                    if not database_busy(busy):
                        raise
        return
    try:
        policy = zte.active(delivery)
        repo, layer, digest = zte.settings(cfg, policy)
        pilot.require(repo.name == delivery.repo and digest == delivery.data["config"], "ZTE configuration changed")
        client = policy.client(delivery.deadline)
        if delivery.phase == "queued":
            return
        if delivery.phase == "dispatching":
            if not launch_lease.held(delivery.repo, delivery.task):
                pilot.require(store.find(delivery.task) != delivery, "the scheduled child exited before registration")
            return
        if delivery.phase == "running":
            run = pilot.scheduled_run(delivery.repo, delivery.task)
            pilot.require(run and not run.get("finished"), "the run stopped before preparing an exact candidate")
            return
        if not settled(delivery, policy, repo, layer):
            return
        if delivery.phase == "merged":
            policy.audit(client)
            release = delivery.data["release"]
            require_landing(delivery, client, release)
            proof = client.ci(release, delivery.data["tree"], "push")
            if proof:
                with db.tx(timeout=0, before_commit=lambda: client.time_left()):
                    check_stop(delivery, policy)
                    store.transition(delivery, "release_ready", release_ci=proof)
        else:
            send_next(delivery, policy, client, cfg, repo)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        try:
            with db.one_read(timeout=0):
                current = store.find(delivery.task)
            if current == delivery or (current and delivery.phase == current.phase == "pr_ready"
                                       and "jev_attempt" in current.data and current.data.get("run") == delivery.data["run"]):
                zte.hold(current, error, timeout=0)
        except sqlite3.OperationalError as busy:
            if not database_busy(busy):
                raise


def tick(cfg) -> None:
    with util.lock(zte.DELIVERY_LOCK) as got:
        if not got or not reconcile_jev_usage():
            return
        advance(cfg)
        if not reconcile_jev_usage():
            return
    zte.finder(cfg)
