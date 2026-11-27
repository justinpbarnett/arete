from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from . import (
    check_repair,
    config,
    diffguard,
    document,
    factory,
    gh,
    hosted_check,
    intake,
    journeys,
    outbound,
    owner_asks,
    patching,
    paths,
    pilot,
    pipeline,
    pr_recovery,
    prompt_loader,
    record,
    repairs,
    routing,
    run_lines,
    runner,
    slots,
    tracker,
    util,
    workspace,
)
from .agent import NO_SESSION, Result
from .factory import Outcome
from .pr_recovery import ancestor, current_head, current_ref
from .review import Review
from .store import asks as asks_store
from .store import insights as insights_store
from .store import runs as run_store
from .store import specs as spec_store
from .store import triage as triage_store
from .store import workspaces
from .verify import Verified

TICKET_FIELDS = ("id", "ref", "title", "body", "comments", "kind", "url", "labels", "owner_comments", "graph_body")
BUILD = "build"
REVIEW_FIX = "review_fix"
CHECK_REPAIR = "check_repair"
REVIEW_REPAIR = "review_repair"
CI_REPAIR = "ci_repair"
ROLE_OF = {"plan": "plan", "patch": BUILD, "implement": BUILD, "revise": BUILD, "repair": BUILD, "verify": "verify", "review": "review", "learn": "learn",
           "document": "document"}
SETTABLE = {**ROLE_OF, "orchestrate": "orchestrate", CHECK_REPAIR: CHECK_REPAIR,
            REVIEW_REPAIR: REVIEW_REPAIR, CI_REPAIR: CI_REPAIR}


def atom_of(name: str, kw: dict) -> str:
    if name == "repair":
        return REVIEW_REPAIR
    if name != "implement":
        return ROLE_OF.get(name, "")
    if kw.get("asks"):
        return CI_REPAIR
    if str(kw.get("round") or BUILD).startswith(REVIEW_FIX):
        return REVIEW_REPAIR
    return BUILD if kw.get("round", BUILD) == BUILD else CHECK_REPAIR


def ran_on_seam(run_id: str) -> bool:
    return any(line.get("event") == "route" or (line.get("event") == "step_start" and line.get("step") == "prepare")
               for line in run_lines.read(run_id))


def start(cfg: config.Config, run: pipeline.Run, earlier: dict, slot: slots.RunSlot | None = None,
          continues: str = "", only: str = "", again: str = "") -> factory.Flow:
    from . import requeue

    ws = paths.WORKTREES_DIR / f"{run.repo.name}-{run.workspace_key}"
    if not spec_store.run_root(run):
        continues = continues or (earlier["id"] if earlier.get("status") == "interrupted" and ran_on_seam(earlier["id"])
                                  else "")
    job = Job(cfg, run, ws, slot=slot, continues=continues, only=only)
    related = lineage_runs(continues) if continues else {}
    if continues and only in ("", "review", "ci") and not (run.environment_release or run.path == "orchestrate"):
        job.continued_ci = continued_review(related)
    if run.path == "orchestrate" and not (continues or only or run.dry_run or run.repairs) and requeue.policy_retry(earlier):
        job.policy_retry = requeue.recorded_failure(earlier)
    catch_up = not only and run.path == "orchestrate" and pending_catch_up(continues)
    if continues and not only and not catch_up:
        run.answers_due = asks_store.asks_due(sorted(factory.chain_runs(continues)))
    saved_controller = continues and run.path == "orchestrate" and bool(adaptive_state(continues).get("prepared"))
    restore_prs = saved_controller and not (catch_up or run.environment_release or run.action_only)
    refusal = guard(job, continues, controller_restore=restore_prs) if continues else ""
    renew = ""
    if refusal:
        job.note_task()
    elif (continues and not saved_controller and not only and not catch_up and not run.environment_release
          and kept_pending(job, continues)
          and (moved := base_moved(continues))):
        if run.answers_due:
            job.moved_base = moved
        else:
            run.log(f"{moved} since run {continues} prepared, so this run prepares again, keeps the plan and the "
                    "build when the worktree still holds that run's edits, and runs every later step again")
            renew = "prepare"
    return factory.Flow(run.id, job, continues=continues, only=only, again=again, refusal=refusal, renew=renew,
                        step_runs=[read for read in related.values() if standalone_step(read)])


def pending_refusal(job: Job) -> str:
    source = job.continues if job.only else job.run.id
    step = " --step pr" if factory.started(job.continues, "pr") else ""
    harm = ("a prepare run alone would change them outside that chain, which then could not publish them"
            if job.only else "preparing again would drop them")
    return (f"{', '.join(job.pending)} holds commits of this run's chain that are not on origin, and {harm}, so arete "
            "ran no model, reset nothing and left the commits on the branch and in the worktree; "
            f"`arete resume {source}{step}` publishes them")


def kept_pending(job: Job, continues: str) -> bool:
    if not job.pending:
        return True
    try:
        checkouts = [workspace.Checkout.from_row(e)
                     for e in prepared_data(continues)["checkouts"]]
        kept = workspace.kept_onto(job.ws, checkouts, job.branch)
    except (workspace.RunFailed, RuntimeError, OSError, KeyError, TypeError):
        return False
    return all(str(c.wt) in kept for c in checkouts if c.github in job.pending)


def guard(job: Job, continues: str, *, controller_restore: bool = False) -> str:
    from .store import orchestration

    if orchestration.publication_pending(continues):
        saved = adaptive_state(continues)
        intent = saved.get("publication_intent") or {}
        receipt = pr_recovery.pending_receipt(saved)
        if job.run.path == "orchestrate" and not any((job.only, job.run.dry_run, job.run.action,
                                                      job.run.environment_release)):
            if orchestration.catch_up_push(saved):
                return ""
            if (receipt and not (job.run.recovery or job.run.from_poll or job.run.repairs)
                    and not pr_recovery.manual_publication_hold(intent, verify_pushes=True)):
                return ""
        why = pr_recovery.manual_publication_hold(intent) if receipt else ""
        return why or "publication was interrupted; reconcile its branch/PR effects before resuming"
    if (getattr(job.run, "action", "") and not job.run.environment_release
            and (why := action_workspace_refusal(continues))):
        return why
    if controller_restore:
        return ""
    data = prepared_data(continues)
    branch, listed = str(data.get("branch") or ""), data.get("checkouts")
    if not continues or not branch or not isinstance(listed, list):
        return ""
    job.branch = branch
    try:
        closed = data.get("closed_prs", ())
        checkouts = [workspace.Checkout.from_row(e) for e in listed if e.get("pr") not in closed]
        excluded = {Path(e["wt"]) for e in listed if e.get("pr") in closed}
        layer = pipeline.checked_layer(job.cfg, Path(data["layer"]))
        if held := workspace.scratch_held(job.ws, checkouts):
            error = workspace.scratch_failure(
                f"verification scratch at {held[0]} is still held; poll finalizes it once its holders end, "
                "then run again", job.ws, held)
            job.run.failure = {"class": "environment", **error.recovery}
            return f"arete started no step and made no tracker call: {error.why}"
        if why := workspace.kept_work(job.ws, checkouts, job.run.repo.path, layer.copy, commits=False, excluded=excluded):
            error = workspace.worktree_failure(why, job.ws, branch)
            job.run.failure = {"class": "environment", **error.recovery}
            return f"arete started no step and made no tracker call: {why}"
    except (workspace.RunFailed, RuntimeError, OSError, KeyError, TypeError) as e:
        return f"arete started no step and made no tracker call: could not check the worktree: {e}"
    runs = authors(continues)
    for c in checkouts:
        if why := job.reclaim(c, *authority(continues, c, c.lease), runs, attached=c.wt.is_dir()):
            return why
    return ""


def adaptive_state(run_id: str) -> dict:
    from .store import orchestration

    return orchestration.find(run_id).get("data", {})


def pending_catch_up(run_id: str) -> dict:
    from .store import orchestration

    return orchestration.catch_up_push(adaptive_state(run_id))


def saved_repair_state(run_id: str) -> dict:
    state = util.read_json(paths.RUNS_DIR / run_id / "repair-checks.json", {})
    return {name: rows for name, rows in state.items() if isinstance(rows, dict)} if isinstance(state, dict) else {}


def standalone_step(read: list[dict]) -> str:
    return str(factory.first(read, "route").get("only") or "")


def continued_review(related: dict[str, list[dict]], *, report: dict | None = None) -> dict:
    states = []
    report = report or {}
    reported = repairs.report_review(report)

    def restore(review: Review, line: dict, gate: dict | None = None, only: str = "") -> dict:
        keys = {p["finding"] for p in review.blocking()}
        state = next((state for state in states if keys <= state.get("findings", {}).keys()), {})
        return {"review": review, "ci_review": gate or {}, "repair_state": state, "line": line, "standalone": only}

    for source, read in reversed(related.items()):
        only = standalone_step(read)
        if only and only not in ("review", "ci"):
            continue
        if state := saved_repair_state(source):
            states.append(state)
        for line in reversed(read):
            data = line.get("data") if line.get("event") == "step_end" else None
            data = data if isinstance(data, dict) else {}
            if (state := data.get("repair_state")) and isinstance(state, dict):
                states.append(state)
            gate = pipeline.publication_review_of(line) or {}
            review = gate.get("review") or (data.get("review") if line.get("step") == "review" and only != "ci" else None)
            if not isinstance(review, dict) or review.get("failed") or not review.get("verdict"):
                continue
            if only and not gate:
                gate = {"review": review, "requirements": []}
            if not gate and reported.verdict and source != report.get("run"):
                return restore(reported, report)
            return restore(pipeline.stored_review(review), line, gate, only)
    if reported.verdict:
        return restore(reported, report)
    return {"repair_state": states[0]} if states else {}


def prepared_data(continues: str) -> dict:
    if prepared := adaptive_state(continues).get("prepared"):
        return prepared
    prepared = factory.passed_data(factory.history(factory.chain(continues)), "prepare")
    return prepared[-1] if prepared else classic_hold(continues).get("prepared", {})


def classic_hold(run_id: str) -> dict:
    return next((held for read in factory.chain(run_id) if (held := factory.first(read, "classic_hold"))), {})


def environment_build(job: Job, history: list[dict]) -> None:
    if classic := classic_hold(job.continues):
        plan, build = classic.get("plan"), classic.get("build")
        name = "plan"
    else:
        prior = [(line["step"], factory.outcome_of(line).data) for line in history if line.get("result") == "pass"]
        planned = next((i for i in range(len(prior) - 1, -1, -1)
                        if prior[i][0] in ("plan", "patch") and not prior[i][1].get("escalate")), -1)
        name, plan = prior[planned] if planned >= 0 else ("", None)
        build = plan
        if name == "plan":
            build = next((data for step, data in reversed(prior[planned + 1:])
                          if step == "implement" and data.get("round") == BUILD), None)
    if (not isinstance(plan, dict) or not isinstance(build, dict)
            or not all(isinstance(plan.get(key), str) for key in ("plan", "goal", "left_for_a_person"))
            or not plan["goal"].strip() or type(plan.get("estimate")) is not int
            or not isinstance(build.get("summary"), str)):
        raise workspace.RunFailed("the held run has no complete saved plan and build for verification")
    job.take(name, Outcome("pass", data=plan), stored=True)
    if name != "patch":
        job.take("implement", Outcome("pass", data=build), stored=True)


def environment_checkouts(repo: config.Repo, ws: Path, data: dict, heads: dict[str, str]) -> list[workspace.Checkout]:
    if not data.get("branch") or not data.get("checkouts"):
        raise workspace.RunFailed("the held run has no saved workspace for verification")
    checkouts = [workspace.Checkout.from_row(c) for c in data["checkouts"]]
    configured = {(p.github, repo.path / p.dir, ws / p.dir) for p in repo.parts}
    if {(c.github, c.src, c.wt) for c in checkouts} != configured or len(configured) != len(checkouts):
        raise workspace.RunFailed("the saved worktrees no longer match the configured repository")
    if not set(heads) <= {c.github.casefold() for c in checkouts}:
        raise workspace.RunFailed("the saved workspace does not match the published draft")
    if left := workspace.scratch_held(ws, checkouts):
        raise workspace.scratch_failure(f"verification scratch at {left[0]} is still held", ws, left)
    if why := workspace.kept_work(ws, checkouts, repo.path, config.load_layer(repo.layer_dir).copy, commits=False):
        raise workspace.RunFailed(why)
    branch = data["branch"]
    for c in checkouts:
        remote = heads.get(c.github.casefold(), "")
        head = remote or c.revision
        ref = f"refs/heads/{branch}"
        local = util.git(["rev-parse", "--verify", "-q", ref], c.src, check=False).strip()
        if (not head or workspace.remote_head(c.src, branch) != remote or local and local != head
                or (not remote and not local)):
            raise workspace.RunFailed(f"the held branch of {c.github} no longer matches the saved draft")
        if (c.wt / ".git").exists() and (
                util.git(["symbolic-ref", "-q", "HEAD"], c.wt, check=False).strip() != ref
                or util.git(["rev-parse", "HEAD"], c.wt).strip() != head or workspace.uncommitted(c.wt)):
            raise workspace.RunFailed(f"the held worktree of {c.github} no longer matches the saved draft")
    return checkouts


def check_environment_workspace(repo: config.Repo, row: dict, heads: dict[str, str]) -> None:
    data = prepared_data(row["id"])
    if data.get("branch") != row["branch"]:
        raise workspace.RunFailed("the held PRs do not match the saved branch")
    ws = paths.WORKTREES_DIR / f"{repo.name}-{row['issue']}"
    environment_checkouts(repo, ws, data, {gh.pr_repo(url).casefold(): head for url, head in heads.items()})


def restore_environment_workspace(job: Job, data: dict, heads: dict[str, str]) -> None:
    checkouts = environment_checkouts(job.run.repo, job.ws, data, heads)
    job.branch = data["branch"]
    if not all((c.wt / ".git").exists() for c in checkouts):
        if why := rebuild(job, {"step": "prepare", "data": data, "tree": str(job.ws)}, setup=False):
            raise workspace.RunFailed(why)
    job.take("prepare", Outcome("pass", data=data), stored=True)
    for c in job.checkouts:
        remote = heads.get(c.github.casefold(), "")
        if c.lease != remote or util.git(["rev-parse", "HEAD"], c.wt).strip() != (remote or c.revision):
            raise workspace.RunFailed(f"the held branch of {c.github} moved while restoring it")
        c.onto, c.merge, c.resumes = "", "", bool(remote)
        workspace.fetch(c.src, c.base)
    try:
        pipeline.merge_moved_bases(job.run, job.checkouts, job.layer, job.ticket, environment_retry=True)
        job.use_layer(pipeline.resolve_layer(job.cfg, job.run, job.ws))
        if why := pipeline.furnish(job.cfg, job.run, job.layer, job.ws):
            raise workspace.RunFailed(why)
        for c in job.checkouts:
            if (util.git(["rev-parse", "HEAD"], c.wt).strip() != (c.merge or c.lease or c.revision)
                    or workspace.uncommitted(c.wt)):
                raise workspace.RunFailed(f"setup changed the tree of {c.github} before verification")
    except BaseException:
        reset_environment_base(job.checkouts)
        raise
    job.carried = True


def reset_environment_base(checkouts: list[workspace.Checkout]) -> bool:
    reset = False
    for c in checkouts:
        if c.merge:
            with util.repo_lock(c.src):
                if util.git(["rev-parse", "HEAD"], c.wt).strip() == c.merge and not workspace.uncommitted(c.wt):
                    head = c.lease or c.revision
                    util.git(["reset", "--keep", head], c.wt)
                    c.base_commit = util.git(["merge-base", head, c.origin_base], c.wt).strip()
                    c.merge, c.merged_base = "", False
                    reset = True
    return reset


def restore_environment_draft(job: Job, prepared: dict) -> None:
    from . import environment_gaps

    source = run_store.run_get(job.continues) or {}
    hold = environment_gaps.hold_of(job.continues)
    if not hold or not environment_gaps.owned_drafts(source, hold):
        raise factory.Refused("the run no longer has its saved verification hold")
    prs = {gh.pr_repo(url).casefold(): (url, head) for url, head in hold.heads.items()}
    if prepared.get("branch") != source.get("branch") or len(prs) != len(hold.heads):
        raise factory.Refused("the held PRs do not match the saved branch")
    restore_environment_workspace(job, prepared, {repo: head for repo, (_, head) in prs.items()})
    for c in job.checkouts:
        c.pr = prs.get(c.github.casefold(), ("", ""))[0]


def retry_environment(flow: factory.Flow) -> tuple[str, str, str]:
    from . import environment_gaps

    job: Job = flow.ctx
    if flow.refusal:
        raise factory.Refused(flow.refusal)
    flow.skipped = len(flow.earlier)
    flow.route()
    environment_build(job, flow.earlier)
    restore_environment_draft(job, prepared_data(job.continues))
    states = [line.get("data", {}).get("repair_state") for line in flow.earlier]
    if state := next((state for state in reversed(states) if state), classic_hold(job.continues).get("repair_state")):
        job.b.restore_repair_state(state)
    ws = str(job.ws)
    trees = pipeline.verification_trees(job.b)
    if not trees:
        raise factory.Refused("the restored tree could not be identified for verification")
    if not (verified := step(flow, "verify", tree=ws)).passed:
        reset_environment_base(job.checkouts)
        return ended(flow, verified)
    if not step(flow, "checks", tree=ws).passed:
        raise factory.Refused("the full checks did not pass after verification")
    if job.patch_mode and (why := patching.change_refusal(job.layer.patch, job.b.staged.files, job.b.staged.lines)):
        raise factory.Refused(why)
    if not step(flow, "review", attempt=1, tries=1).passed or not job.review.approved():
        raise factory.Refused("the restored draft did not pass review")
    if trees != pipeline.verification_trees(job.b):
        raise factory.Refused("the restored tree changed while checking or reviewing it")
    source = run_store.run_get(job.continues) or {}
    hold = environment_gaps.hold_of(job.continues)
    if not hold or not environment_gaps.owned_drafts(source, hold):
        raise factory.Refused("the held pull request changed before publication")
    return deliver(flow)


def action_workspace_refusal(run_id: str) -> str:
    data = prepared_data(run_id)
    checkouts = [c for c in data.get("checkouts") or [] if c.get("pr") not in data.get("closed_prs", ())]
    if checkouts and all(c.get("wt") and (Path(c["wt"]) / ".git").exists() for c in checkouts):
        return ""
    return "adaptive action reruns need retained worktrees; the saved workspace was removed"


def base_moved(continues: str, *, repos: set[str] | None = None) -> str:
    for c in prepared_data(continues).get("checkouts") or []:
        if not isinstance(c, dict) or not c.get("base_commit"):
            continue
        if repos is not None and c["github"] not in repos:
            continue
        try:
            now = workspace.remote_head(Path(c["src"]), c["base"])
        except RuntimeError:
            continue
        if now and now != c["base_commit"]:
            return f"`origin/{c['base']}` moved from {c['base_commit'][:12]} to {now[:12]} in {c['github']}"
    return ""


STEP_RUNS_READ = 500


def lineage_runs(run_id: str) -> dict[str, list[dict]]:
    whole = {read[0]["run"]: read for read in reversed(factory.chain(run_id)) if read}
    row = run_store.run_get(run_id)
    if not row or not row.get("repo") or not row.get("issue"):
        return whole
    known, found = set(whole), {}
    for other in reversed(run_store.ticket_runs(row["repo"], row["issue"], limit=STEP_RUNS_READ)):
        source = other["id"]
        if source in whole:
            found[source] = whole[source]
        elif (only := other.get("only_step")) and source not in known:
            read = run_lines.read(source)
            route = factory.first(read, "route")
            if route.get("only") == only and route.get("continues") in known:
                known.add(source)
                found[source] = read
    return {**{source: read for source, read in whole.items() if source not in found}, **found}


def step_runs(run_id: str) -> dict[str, list[dict]]:
    return {source: read for source, read in lineage_runs(run_id).items() if standalone_step(read)}


def authors(run_id: str) -> set[str]:
    return factory.chain_runs(run_id) | set(step_runs(run_id))


def publication(run_id: str) -> dict:
    if published := adaptive_state(run_id).get("publication"):
        return published
    for read in factory.chain(run_id):
        for line in reversed(factory.ends(read)):
            data = line.get("data")
            if line.get("step") == "pr" and line.get("result") == "pass" and isinstance(data, dict):
                return data
    return classic_hold(run_id).get("publication", {})


def authority(run_id: str, c: workspace.Checkout, lease: str) -> tuple[str, bool]:
    pub = publication(run_id)
    heads = pub.get("heads") if isinstance(pub.get("heads"), dict) else {}
    changed = pub.get("changed") if isinstance(pub.get("changed"), list) else []
    return str(c.recovery_head or heads.get(c.github) or lease), c.github in changed


def foreign(wt: Path, tip: str, bases: list[str], runs: set[str]) -> list[str]:
    return pr_recovery.unowned(wt, tip, bases, runs, first_parent=True)


def remade(line: dict) -> tuple[str, list[workspace.Checkout], str]:
    data = line.get("data") if isinstance(line.get("data"), dict) else {}
    branch, listed = str(data.get("branch") or ""), data.get("checkouts")
    if line.get("step") != "prepare" or not branch or not isinstance(listed, list):
        return branch, [], "only the folder of a prepare step can be made again"
    made = [workspace.Checkout.from_row(c) for c in listed]
    try:
        for c in made:
            c.lease = workspace.remote_head(c.src, branch)
            if c.lease:
                continue
            if not c.revision:
                return branch, made, (f"its branch {branch} is not on origin for {c.github}, "
                                      "and no starting revision was recorded")
            local = util.git(["rev-parse", "--verify", f"refs/heads/{branch}"], c.src, check=False).strip()
            if c.resumes or c.pr or not local:
                return branch, made, (f"its branch {branch} is not on origin for {c.github}, "
                                      "and it was not left unchanged")
    except RuntimeError as e:
        return branch, made, f"arete could not look for its branch {branch} on origin: {e}"
    return branch, made, ""


def rebuild(job: Job, line: dict, *, layer: config.RepoLayer | None = None, setup: bool = True) -> str:
    branch, made, why = remade(line)
    if why:
        return why
    job.branch = branch
    leases = {str(e.get("github")): str(e.get("lease") or "") for e in line["data"]["checkouts"]}
    runs = authors(job.continues)
    for c in made:
        if why := job.reclaim(c, *authority(job.continues, c, leases.get(c.github, "")), runs, attached=False):
            return why
    ws = Path(line["tree"])
    admitted = layer is not None
    layer = layer or pipeline.checked_layer(job.cfg, Path(line["data"]["layer"]) if setup else job.run.repo.layer_dir)
    excluded = job.run.closed_checkout_paths
    if excluded:
        if held := workspace.scratch_held(ws, made):
            return f"verification scratch at {held[0]} is still held; poll finalizes it once its holders end"
        if why := workspace.kept_work(ws, made, job.run.repo.path, layer.copy, excluded=excluded):
            return why
    else:
        try:
            workspace.clear_workspace(ws, made, job.run.repo.path, layer.copy)
        except workspace.RunFailed as e:
            return e.why
    pipeline.claim_workspace(job.run, ws, made)
    try:
        for c in made:
            if c.lease:
                workspace.fetch(c.src, branch)
            local = util.git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], c.src, check=False).strip()
            workspace.add_worktree(c, branch, local or (f"origin/{branch}" if c.lease else c.revision))
            job.restored[str(c.wt)] = c.lease
    except RuntimeError as e:
        return f"arete could not make it again from origin/{branch}: {util.first_line(str(e), 300)}"
    job.run.log(f"made {ws} again from origin/{branch}")
    if not setup:
        return ""
    if admitted and layer != pipeline.checked_layer(job.cfg, layer.dir):
        return "the restored repository layer changed; reconcile before setup"
    return pipeline.furnish(job.cfg, job.run, layer, ws)


@dataclass
class Job:
    cfg: config.Config
    run: pipeline.Run
    ws: Path
    ticket: tracker.Ticket = field(default_factory=lambda: tracker.Ticket("", "", ""))
    branch: str = ""
    checkouts: list[workspace.Checkout] = field(default_factory=list)
    layer: config.RepoLayer = field(default_factory=lambda: config.RepoLayer(dir=Path()))
    expertise: str = ""
    attachments: str = ""
    triage: str = ""
    plan_text: str = ""
    goal: str = ""
    estimate: int = 0
    left_for_a_person: str = ""
    build_summary: str = ""
    verified: Verified = field(default_factory=lambda: Verified("unverified"))
    review: Review = field(default_factory=lambda: Review.stored({}))
    fix_reply: str = ""
    reviews: int = 0
    review_exhausted: bool = False
    built: pipeline.Build | None = None
    slot: slots.RunSlot | None = None
    approval: dict | None = None
    replies: list[dict] = field(default_factory=list)
    attempts: int = 1
    report_body: str = ""
    document: str = ""
    draft_why: str = ""
    summary: str = ""
    opened: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    drafted: list[str] = field(default_factory=list)
    ci_stopped: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    ci_result: str = ""
    check_commands: list[str] = field(default_factory=list)
    check_rules: list[str] = field(default_factory=list)
    restored: dict[str, str] = field(default_factory=dict)
    continues: str = ""
    carried: bool = False
    committed: bool = True
    pending: list[str] = field(default_factory=list)
    only: str = ""
    noted: bool = False
    moved_base: str = ""
    patch_mode: bool = False
    prepared_feedback_prs: list[str] = field(default_factory=list)
    closed_prs: set[str] = field(default_factory=set)
    policy_retry: dict = field(default_factory=dict)
    check_publication_targets: Callable[[], None] | None = None
    continued_ci: dict = field(default_factory=dict)
    before_pr_recovery: Callable[[], None] | None = None
    on_pr_plan_installed: Callable[[workspace.Checkout], None] | None = None
    pr_plans: list[pr_recovery.Plan] = field(default_factory=list)
    sibling_pr_inputs: list[pr_recovery.Inputs] = field(default_factory=list)
    pr_recovery_ci: bool = False
    recovery_publication: pr_recovery.Publication | None = None
    direct_push: dict[str, dict] = field(default_factory=dict)

    @property
    def requires_behavior_on_block(self) -> bool:
        return bool(self.patch_mode or patching.specification_refusal(self.ticket)
                    or patching.sensitive_files(self.layer.patch, self.b.staged.files))

    @property
    def held_reason(self) -> str:
        if self.run.environment_release and not retry_passed(self.verified):
            return "verification did not pass on the live release"
        if self.verified.verdict == "blocked" and self.requires_behavior_on_block:
            return "the check in use was blocked; this change requires behavioral evidence"
        why = pipeline.draft_reason(self.review, self.verified, self.reviews)
        if not why and (pending := self.b.in_use_repair_hold(self.verified)):
            return f"Independent in-use evidence is still required for {pending}."
        return why

    def merge_moved_base(self, name: str, *, before_resolve: Callable[[], None] | None = None) -> list[workspace.Checkout]:
        moved, self.moved_base = self.moved_base, ""
        held = [c for c in self.checkouts if c.onto]
        merged = []
        if moved and held:
            self.run.log(f"{moved} since run {self.continues} prepared, so arete merges the new base into the edits "
                         f"that run kept before {name} runs with the answers")
            merged = pipeline.merge_base_into_kept(self.run, held, self.layer, self.ticket, before_resolve=before_resolve)
            if merged:
                self.run.base_note = pipeline.base_merge_note(merged)
                if self.built is not None:
                    self.built.workspace = self.workspace
                    for checkout in merged:
                        self.built.worker_trees.pop(checkout.github, None)
                workspace.note_made(self.ws, self.checkouts, excluded=self.run.closed_checkout_paths)
        self.run.kept_note = pipeline.kept_edits_note(held) if self.run.answers_due else ""
        return merged

    @property
    def workspace_checkouts(self) -> list[workspace.Checkout]:
        return self.checkouts + self.run.closed_checkouts

    @property
    def with_prs(self) -> list[workspace.Checkout]:
        return [c for c in self.checkouts if c.pr]

    def picked(self, names: list[str]) -> list[workspace.Checkout]:
        return [c for c in self.checkouts if c.github in names]

    @property
    def builder_bash(self) -> list[str]:
        return self.layer.builder_bash + pipeline.git_reads(self.checkouts) + self.check_rules

    @property
    def commands(self) -> str:
        return prompt_loader.code_list(self.layer.builder_bash + pipeline.git_reads(self.checkouts) + self.check_commands)

    @property
    def checks(self) -> str:
        return pipeline.check_calls(self.check_commands)

    @property
    def workspace(self) -> str:
        return pipeline.workspace_note(self.branch, self.checkouts)

    @property
    def brief(self) -> prompt_loader.Brief:
        t = self.ticket
        return prompt_loader.Brief(self.run.repo.name, self.workspace, t.ref, t.title, t.body,
                                   owner=self.run.owner_note, comments=t.comments, attachments=self.attachments,
                                   expertise=self.expertise, commands=self.commands, max_lines=self.layer.max_diff_lines,
                                   denied=prompt_loader.code_list(diffguard.denied_paths(self.layer)),
                                   class_check=pipeline.class_check_example(self.run.dir, self.layer))

    @property
    def b(self) -> pipeline.Build:
        if self.built is None:
            self.built = pipeline.Build(
                cfg=self.cfg, run=self.run, ticket=self.ticket, ws=self.ws, checkouts=self.checkouts, layer=self.layer,
                timeout=self.layer.timeout_minutes * 60, workspace=self.workspace, attachments=self.attachments,
                tools=prompt_loader.bash_rules(self.builder_bash), deny=diffguard.denied_paths(self.layer),
                left_for_a_person=self.left_for_a_person, check_commands=self.checks,
                from_base=self.run.alone or bool(self.run.repairs) or self.carried, plan_text=self.plan_text, build_summary=self.build_summary)
        return self.built

    def staged(self) -> pipeline.Build:
        if self.b.staged.empty:
            self.b.restage()
        return self.b

    def restore(self, line: dict, *, layer: config.RepoLayer | None = None) -> str:
        why = rebuild(self, line, layer=layer)
        if why and not self.only and line.get("step") == "prepare":
            raise workspace.RunFailed(why)
        return why

    def policy(self, name: str, kw: dict) -> str:
        if name == "checks":
            value = (self.layer.checks, self.layer.quick_checks, self.layer.targeted_checks,
                     self.layer.targeted_commands, bool(kw.get("quick")))
            if kw.get("focused"):
                value += (kw["focused"],)
        elif name == "verify":
            verify = self.layer.verify
            value = (verify.policy() if verify else None, self.goal, self.left_for_a_person)
            if self.built is not None and (required := self.b.in_use_repair_checks()):
                value += (required,)
            if verify and verify.feature_map:
                value += (journeys.requested(verify.feature_map, self.ticket.title, self.ticket.body),)
        else:
            return ""
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def accepts(self, name: str, outcome: Outcome, kw: dict) -> bool:
        if name == "revise" and not self.only and not self.includes_feedback(outcome.data):
            self.run.log(f"{name}: pending PR feedback was absent from the saved outcome; rerunning is required")
            return False
        if name == "ci" and not outcome.passed:
            return False
        if name == "review" and outcome.passed:
            if self.built is not None and self.b.ci_review_evidence and (
                    self.b.ci_review_pending() or self.continued_ci.get("standalone")):
                self.run.log("review: later repair review evidence prevents reuse of the earlier approval")
                return False
            trees = (outcome.data.get("review") or {}).get("trees")
            if outcome.data.get("repair_state") and (not trees or trees != pipeline.verification_trees(self.b)):
                self.run.log("review: the saved repair review was on a different tree; rerunning is required")
                return False
        if name == "checks" and not hosted_check.current(outcome.data):
            return False
        if (expected := self.policy(name, kw)) and outcome.data.get("policy") != expected:
            self.run.log(f"{name}: stored evidence used a different policy; rerunning is required")
            return False
        return True

    def includes_feedback(self, data: dict) -> bool:
        feedback = self.run.feedback
        if not feedback:
            return True
        keys = data.get("feedback_keys", data.get("feedback_ids"))
        if keys is not None:
            known = {tuple(key) for key in keys}
            return all(pipeline.feedback_key(row) in known for row in feedback)
        return (not insights_store.has_fresh_feedback(feedback)
                and data.get("plan") == revision_plan(pipeline.feedback_markdown(feedback)))

    def claim_revision_feedback(self) -> None:
        if self.run.claim_feedback_on_revise:
            self.run.claim_feedback_on_revise = False
            self.run.claim_feedback()

    def keeps(self, name: str, kw: dict) -> bool:
        return any(c.onto for c in self.checkouts) and (
            name in ("plan", "patch", "revise", "repair") or name == "implement" and kw.get("round") == BUILD)

    def labels(self, name: str, kw: dict | None = None) -> dict:
        atom = atom_of(name, kw or {})
        if not atom:
            return {}
        s = self.run.settings(atom)
        return {"runner": s.runner, "model": s.model, "effort": s.effort, "tier": s.tier, "kind": self.run.kind,
                "env": runner.env_names(s.model, s.runner, self.cfg, self.run.repo, self.run.id), "atom": s.step}

    def with_left(self, text: str) -> str:
        if not self.left_for_a_person:
            return text
        return f"{text}\n\n{pipeline.NOT_CHECKED_LEFT_FOR_A_PERSON}{self.left_for_a_person}"

    def take(self, name: str, outcome: Outcome, stored: bool) -> None:
        data, run = outcome.data, self.run
        if name == "prepare" and not stored:
            for ask in run.answers_due:
                ask["session"] = {}
        if outcome.passed or name == "review":
            run.facts.update(pipeline.facts(name, data))
        if "story" in data:
            run.story = list(data["story"])
        if "repair_state" in data and (not stored or not self.continued_ci.get("repair_state")):
            self.b.restore_repair_state(data["repair_state"])
        if name == "prepare" and outcome.passed:
            self.prepared(data, stored)
            if not self.only:
                self.restore_continued_ci()
        elif name in ("plan", "patch") and outcome.passed:
            self.patch_mode = name == "patch" and not data.get("escalate")
            self.plan_text, self.goal, self.estimate = data["plan"], data["goal"], data["estimate"]
            self.left_for_a_person = data["left_for_a_person"]
            if name == "patch":
                self.build_summary = data.get("summary", "")
                if outcome.session.id:
                    run.sessions[BUILD] = outcome.session
            if self.built is not None:
                self.built.plan_text = self.plan_text
                self.built.build_summary = self.build_summary
        elif name in ("revise", "repair") and outcome.passed:
            self.plan_text, self.goal, self.estimate = data["plan"], data["goal"], 0
            self.left_for_a_person, self.build_summary, self.replies = (data["left_for_a_person"], data["summary"],
                                                                        data.get("replies", []))
            if self.built is not None:
                self.built.plan_text, self.built.build_summary = self.plan_text, self.build_summary
                self.built.left_for_a_person = self.left_for_a_person
            if outcome.session.id:
                run.sessions[BUILD] = outcome.session
        elif name == "implement":
            self.build_summary = data.get("summary", self.build_summary)
            if str(data.get("round") or "").startswith(REVIEW_FIX):
                self.fix_reply = str(data.get("reply") or "")
            if outcome.session.id:
                run.sessions[BUILD] = outcome.session
        elif name == "checks":
            self.b.report = data.get("report", self.b.report)
            if not outcome.passed:
                self.b.checked = {}
            elif "checked" in data:
                self.b.checked = data["checked"]
            elif data.get("full"):
                self.b.checked = {"tree": str(data.get("checked_tree") or ""), "commands": data.get("commands", {}),
                                  "report": data["report"]}
            if "attempts" in data and not data.get("after"):
                self.attempts = data["attempts"]
                if stored:
                    record.note(run.id, attempts=self.attempts)
        elif name == "verify" and (outcome.passed or data.get("focused_hold")):
            if outcome.passed:
                self.verified = Verified.stored(data["verified"])
            if "checked" in data:
                self.b.checked = data["checked"]
            self.b.steps = Counter(data["names"])
        elif name == "review":
            self.review_exhausted = data.get("review_exhausted") is True
            if "review" in data:
                self.review, self.reviews = pipeline.stored_review(data["review"]), data.get("attempt", 0)
                if stored and self.review.blocking() and not self.continued_ci.get("ci_review"):
                    self.b.carry_review(self.review, restored=True)
                if not stored and not self.review.failed and self.built is not None:
                    self.b.ci_review_evidence = {}
        elif name == "document" and outcome.passed:
            self.document = data.get("text", "")
        elif name == "pr" and outcome.passed:
            self.published(data, stored)
        elif name == "ci":
            if stored and not self.continued_ci and "ci_review" in data:
                self.b.ci_review_evidence = data["ci_review"]
            if outcome.passed:
                self.ci_result = data["summary"]
        if self.built is not None:
            facts = pipeline.publication_review_facts(self.b.ci_review_evidence)
            if self.continued_ci.get("standalone") == "review" and self.b.ci_review_evidence == self.continued_ci.get("ci_review"):
                facts.pop("requirements", None)
            run.facts.update(facts)

    def restore_continued_ci(self) -> None:
        if not self.continued_ci:
            return
        b = self.b
        b.restore_repair_state(self.continued_ci.get("repair_state", {}))
        b.ci_review_evidence = self.continued_ci.get("ci_review", {})
        if review := b.ci_review():
            b.carry_review(review, restored=True)
        if state := b.repair_state():
            self.run.save("repair-checks.json", state)
        if b.ci_review_pending():
            prs = publication(self.continues).get("prs", {})
            known = [url for c in self.checkouts if (url := prs.get(c.github)) and url not in self.closed_prs]
            if known:
                record.note(self.run.id, branch=self.branch, pr_url=" ".join(known))

    def published(self, data: dict, stored: bool) -> None:
        for c in self.checkouts:
            c.pr = data["prs"].get(c.github, c.pr)
        self.opened, self.pushed, self.changed = data["opened"], data["pushed"], data["changed"]
        self.drafted, self.ci_stopped = data.get("drafted", []), data.get("ci_stopped", data["changed"])
        self.found = data.get("found", [])
        self.draft_why, self.report_body, self.summary = data["draft_why"], data["report_body"], data["summary"]
        self.b.committed_lines = data["committed_lines"]
        self.committed = bool(data.get("committed", True))
        self.direct_push = dict(data.get("direct_push") or {})
        if stored:
            for c in self.picked(self.changed):
                head = c.publication_baseline or c.onto or util.git(["rev-parse", "HEAD"], c.wt).strip()
                c.note_pushed_head(head)
            if self.with_prs:
                pipeline.note_prs(self.run, self.branch, self.with_prs)

    def hold(self, name: str) -> str:
        if name != "prepare" or not self.pending or not self.only and kept_pending(self, self.continues):
            return ""
        self.note_task()
        return pending_refusal(self)

    def note_task(self) -> None:
        data = prepared_data(self.continues)
        if self.noted or not isinstance(data.get("ticket"), dict):
            return
        self.noted = True
        opened(self.run, tracker.Ticket(**data["ticket"]))

    def reclaim(self, c: workspace.Checkout, expected: str, published: bool, runs: set[str],
                attached: bool = True) -> str:
        branch, remote, head = self.branch, "", ""
        here = c.wt if attached else c.src
        try:
            if attached:
                on = current_ref(c)
                if on != f"refs/heads/{branch}":
                    where = f"branch {on.removeprefix('refs/heads/')}" if on else "a detached HEAD"
                    return (f"the worktree of {c.github} is on {where}, not {branch}, so arete ran no model and "
                            f"published nothing; check out {branch} there or remove the worktree, then continue again")
            remote = workspace.remote_head(c.src, branch)
            if attached:
                head = current_head(c)
            else:
                head = util.git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], c.src, check=False).strip() or remote
            if (attached and self.before_pr_recovery and not self.run.workspace and c.pr
                    and (c.recovery_head or expected and remote != expected)):
                pinned = pr_recovery.expected_inputs(c, branch, head)
                if (self.pr_recovery_ci and c.recovery_head and expected == remote == c.lease
                        and pinned.refs.aligned() and pr_recovery.observe(c, branch) == pinned.refs):
                    pr_recovery.check_retained(c, pinned)
                    self.sibling_pr_inputs.append(pinned)
                else:
                    self.plan_pr_recovery(c, expected, head, remote, runs)
                    return ""
            if remote:
                workspace.fetch(c.src, branch)
                if util.sh(["git", "cat-file", "-e", f"{remote}^{{commit}}"], cwd=here).returncode:
                    return f"{branch} of {c.github} moved while arete checked it, so arete ran no model and published nothing"
            if (expected or published) and not remote:
                return f"{branch} is gone from origin for {c.github}, so arete ran no model and published nothing"
            if remote:
                if expected and not ancestor(here, expected, remote):
                    return (f"origin/{branch} of {c.github} no longer contains {expected[:12]}, so arete ran no model, "
                            "adopted nothing and left both as they are")
                if others := foreign(here, remote, [expected, c.revision], runs):
                    return (f"origin/{branch} of {c.github} moved to {remote[:12]} with commits this run's chain did "
                            f"not make ({', '.join(o[:12] for o in others)}), so arete ran no model, adopted nothing "
                            "and left both as they are")
                if not ancestor(here, remote, head):
                    return (f"{branch} in the worktree of {c.github} does not contain origin/{branch} at {remote[:12]}, "
                            "so arete ran no model and left both as they are")
            if others := foreign(here, head, [remote, c.revision], runs):
                return (f"the worktree's {branch} of {c.github} holds commits that are not on origin and were not made "
                        f"by this run's chain ({', '.join(o[:12] for o in others)}), so arete ran no model and "
                        "published nothing")
        except RuntimeError as e:
            return f"arete could not check {branch} of {c.github} against origin: {util.first_line(str(e), 300)}"
        c.note_pushed_head(remote)
        if not c.pr and remote and head == remote == expected:
            c.onto = ""
        if head != c.revision:
            self.carried = True
        if head not in (remote, c.revision) and c.github not in self.pending:
            self.pending.append(c.github)
        return ""

    def prepared(self, data: dict, stored: bool, *, reconciling_catch_up: bool = False) -> None:
        run = self.run
        self.prepared_feedback_prs = [url for url in data.get("feedback_prs", []) if url not in self.closed_prs]
        self.ticket = tracker.Ticket(**data["ticket"])
        self.branch, self.attachments, self.triage = data["branch"], data["attachments"], data["triage"]
        self.expertise, self.approval = data["expertise"], data.get("approval")
        checkouts = [workspace.Checkout.from_row(c) for c in data["checkouts"]]
        self.checkouts = [c for c in checkouts if c.pr not in self.closed_prs]
        run.closed_checkouts = [c for c in checkouts if c.pr in self.closed_prs]
        leases = {c.github: c.lease for c in self.checkouts}
        for c in self.checkouts:
            if str(c.wt) in self.restored:
                c.lease = self.restored[str(c.wt)]
        self.use_layer(pipeline.checked_layer(self.cfg, Path(data["layer"])))
        run.add_dirs += [d for d in map(Path, data["add_dirs"]) if d not in run.add_dirs]
        if stored and not run.workspace:
            if held := workspace.scratch_held(self.ws, self.checkouts):
                raise workspace.scratch_failure(f"verification scratch at {held[0]} is still held; poll finalizes it "
                                                "once its holders end, then run again", self.ws, held)
            if not reconciling_catch_up:
                kept = workspace.kept_onto(self.ws, self.checkouts, self.branch)
                if why := workspace.kept_work(self.ws, self.checkouts, run.repo.path, self.layer.copy,
                                              excluded=run.closed_checkout_paths):
                    raise workspace.worktree_failure(why, self.ws, self.branch)
                for c in self.checkouts:
                    c.onto = kept.get(str(c.wt), "")
        elif stored:
            for c in self.checkouts:
                c.onto = ""
        if stored and not reconciling_catch_up:
            self.pr_recovery_ci = bool(adaptive_state(self.continues).get("ci_failed"))
            runs = authors(self.continues)
            for c in self.checkouts:
                if why := self.reclaim(c, *authority(self.continues, c, leases[c.github]), runs):
                    raise workspace.RunFailed(why)
            if self.pr_plans:
                self.plan_sibling_prs(runs)
            self.install_pr_plans()
        pipeline.claim_workspace(run, self.ws, self.checkouts)
        if stored and not reconciling_catch_up:
            opened(run, self.ticket)
            if not run.alone:
                run.enter("running")

    def plan_pr_recovery(self, c: workspace.Checkout, expected: str, head: str, remote: str,
                         runs: set[str]) -> None:
        if self.before_pr_recovery:
            self.before_pr_recovery()
        recorded = workspace.head_recorded(workspace.made_before(self.ws), c)
        self.pr_plans.append(pr_recovery.plan(c, self.branch, expected, head, remote, runs, self.run.id,
                                              recorded, diffguard.denied_paths(self.layer)))

    def plan_sibling_prs(self, runs: set[str]) -> None:
        planned = {i.checkout.github for i in self.sibling_pr_inputs} | {p.inputs.checkout.github for p in self.pr_plans}
        for c in self.with_prs:
            if c.github in planned:
                continue
            head = current_head(c)
            expected = pr_recovery.expected_inputs(c, self.branch, head)
            refs = pr_recovery.observe(c, self.branch)
            if (refs == expected.refs and refs.aligned() and refs.target_matches(c, self.branch)
                    and ancestor(c.wt, refs.base, head)):
                pr_recovery.check_retained(c, expected)
                self.sibling_pr_inputs.append(expected)
                c.recovery_base, c.resumes = refs.base, True
                continue
            self.plan_pr_recovery(c, c.lease, head, refs.origin, runs)

    def recheck_pr_plans(self, during: str, *, installed: bool = False) -> None:
        planned = [replace(p.inputs, local=p.candidate) if installed else p.inputs for p in self.pr_plans]
        for inputs in planned + self.sibling_pr_inputs:
            pr_recovery.check_retained(inputs.checkout, inputs)
            inputs.recheck(during=during)
        if installed and self.pr_plans:
            pr_recovery.check_staging([c for c in self.checkouts if not c.pr])

    def install_pr_plans(self) -> None:
        for plan in self.pr_plans:
            plan.check_local(self.ws, self.run.repo.path, self.layer.copy)
        self.recheck_pr_plans("during isolated reconciliation")
        if self.pr_plans:
            planned = {p.inputs.checkout.github: p.retained for p in self.pr_plans}
            for c in self.checkouts:
                files = planned.get(c.github) or pr_recovery.loose_files(c.wt)
                pr_recovery.check_retained(c, files=files, remember=True)
        for plan in self.pr_plans:
            c = plan.inputs.checkout
            with workspaces.folder_lock(self.ws):
                made = workspace.made_before(self.ws)
                row = workspaces.open_for(self.ws.name)
                if not row or workspace.head_recorded(made, c) != plan.inputs.local:
                    raise plan.inputs.hold("the retained workspace record changed during recovery",
                                           "Inspect the retained checkout and reconcile its workspace record")
                plan.check_local(self.ws, self.run.repo.path, self.layer.copy)
                plan.install()
                made[workspace.head_key(c)] = plan.candidate
                made.pop(workspace.onto_key(c), None)
                workspaces.save_snapshot(row, made)
                if self.on_pr_plan_installed:
                    self.on_pr_plan_installed(c)
                plan.check_files(installed=True)
            self.carried = True
            if plan.candidate != c.lease and c.github not in self.pending:
                self.pending.append(c.github)
            self.run.log(f"reconciled {c.pr}: retained {plan.inputs.local}, PR head {c.lease}, "
                         f"candidate {plan.candidate} compared with PR base {c.recovery_base}")

    def use_layer(self, layer: config.RepoLayer) -> None:
        self.layer = layer
        self.check_commands, self.check_rules = pipeline.check_script(self.run.dir, layer, self.ws)
        self.run.cfg, self.run.layer, self.run.kind = self.cfg, layer, self.ticket.kind


def opened(run: pipeline.Run, t: tracker.Ticket) -> None:
    run_lines.write(run.id, "task", ticket=t.ref, kind=t.kind, repo=run.repo.name, title=t.title,
                **({"path": run.path} if run.path else {}))
    if run.answers_due:
        for ask in run.answers_due:
            run_lines.write(run.id, "answer", asked_in=ask["run"], ask_id=ask["id"], step=ask["step"],
                        answers=ask["answers"])
    elif run.answering:
        run_lines.write(run.id, "answer", asked_in=run.answering["id"], question=run.answering.get("summary") or "",
                    text=pipeline.answer_text(run.answering["id"]))


def end_as(result: str, kind: str, **fields) -> Outcome:
    return Outcome(result, feedback=fields.get("why") or fields.get("summary") or "", data={"ending": kind, **fields})


def local_workspace(run: pipeline.Run, base: str, branch: str, ws: Path, copies: list[str]) -> list[workspace.Checkout]:
    repo = run.repo
    checkouts = [workspace.Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir,
                                   repo.target_branch or base or util.origin_default_branch(repo.path / p.dir))
                 for p in repo.parts or [config.Part("", repo.name)]]
    workspace.require_target_branch(repo, checkouts, work_branch=branch)
    kept = workspace.kept_or_cleared(ws, checkouts, repo.path, copies, branch)

    def resumes_for(c: workspace.Checkout) -> bool:
        c.lease = workspace.remote_head(c.src, branch)
        return bool(c.lease)

    workspace.place_checkouts(run.id, checkouts, kept, branch, resumes_for)
    return checkouts


@factory.step("prepare")
def prepare(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    cfg, run = job.cfg, job.run
    repo, tk = run.repo, run.tracker
    try:
        from . import spec_amendments
        t = spec_amendments.child_ticket(tk, intake.route_ticket(cfg, repo, tk, tk.get(run.tid), dry_run=run.dry_run, log=run.log))
    except tracker.ERRORS as e:
        return end_as("stop", "quiet", status="failed", summary=f"could not read {run.ref}: {e}")
    spec_root = spec_store.run_root(run)
    if spec_root or t.is_spec:
        why = (tracker.spec_blocker(cfg, tk, t) if spec_root and t.spec_identity == run.spec_identity
               else "a spec must run through its own delivery lineage")
        if why:
            return end_as("stop", "quiet", status="skipped", summary=why)
    opened(run, t)
    mended = run_store.run_get(run.repairs) or {} if run.repairs else {}
    feedback_prs = run.feedback_prs
    if not (feedback_prs or run.repairs or run.alone) and run.path in ("feature", "patch"):
        feedback_prs = list(dict.fromkeys(row["pr_url"] for row in pipeline.pending_feedback(repo.name, run.tid)))
    if feedback_prs:
        if closed := run.feedback_closure(feedback_prs):
            return end_as("stop", "quiet", status="noop", summary=closed)
    elif run.repairs:
        urls = str(mended.get("pr_url") or "").split()
        if closed := [url for url in urls if gh.pr_view(url).get("state") != "OPEN"]:
            return end_as("stop", "quiet", status="noop",
                          summary=f"the PR closed before arete could repair it: {', '.join(closed)}")
    elif not t.open:
        return end_as("stop", "quiet", status="noop", summary="ticket is not open")
    elif t.blocker and not run.force:
        return end_as("stop", "quiet", status="skipped", summary=t.blocker)
    try:
        images = tk.save_images(t, run.dir / "images")
    except tracker.ERRORS as e:
        return end_as("stop", "quiet", status="failed", summary=f"could not download the images on {run.ref}: {e}")
    if images.paths:
        run.log(f"images: saved {util.count(len(images.paths), 'image')} from {run.ref}")
    if images.left_out:
        run.log(f"images: left out {', '.join(images.left_out)}")
    main_layer = pipeline.checked_layer(cfg, repo.layer_dir)
    branch = (run.preview_branch or mended.get("branch") or pipeline.open_branch(run)
              or prompt_loader.fill_template(main_layer.branch, config.RepoLayer.branch, kind=t.kind, id=t.id,
                                        slug=prompt_loader.slug(t.title)))
    if job.policy_retry.get("branch") == branch:
        parts = {(p.dir, p.github, job.ws / p.dir) for p in repo.parts}
        closed = [workspace.Checkout.from_row(row) for row in job.policy_retry.get("closed_checkouts", ())]
        closed = [c for c in closed if (c.dir, c.github, c.wt) in parts]
        attached = [c for c in closed if (c.wt / ".git").exists()]
        if not any(workspace.uncommitted(c.wt) for c in attached):
            for c in attached:
                if why := workspace.kept_checkout(job.ws, c, repo.path, main_layer.copy, closed=True):
                    error = workspace.worktree_failure(why, job.ws, branch)
                    return end_as("fail", "failed", why=why, recovery=error.recovery)
            run.closed_checkouts = closed
            job.closed_prs = {c.pr for c in closed}
    if not run.dry_run:
        tk.prepare()
    if not run.alone:
        run.enter("running")
    try:
        if repo.parts and (not run.dry_run or run.preview_of):
            exclusion = {"excluded": run.closed_checkout_paths} if run.closed_checkouts else {}
            checkouts = workspace.make_workspace(run.id, repo, main_layer.base_branch, branch, run.workspace_key,
                                                main_layer.copy, preview_of=run.preview_of, **exclusion)[1]
        else:
            checkouts = local_workspace(run, main_layer.base_branch, branch, job.ws, main_layer.copy)
        pipeline.claim_workspace(run, job.ws, checkouts)
        for c in checkouts:
            run.log(f"worktree {c.wt} on {branch}, {c.starting_point}")
        run.cfg, run.layer, run.kind = cfg, main_layer, t.kind
        pipeline.settle_starts(run, checkouts, main_layer, t, branch)
    except workspace.RunFailed as e:
        return end_as("fail", "failed", why=e.why, detail=e.detail, recovery=e.recovery)
    for c in checkouts:
        c.revision = util.git(["rev-parse", "HEAD"], c.wt).strip()
    layer = pipeline.resolve_layer(cfg, run, job.ws)
    if why := pipeline.furnish(cfg, run, layer, job.ws):
        return end_as("fail", "failed", why=why)
    workspace.note_made(job.ws, checkouts, pipeline.workspace_owner(run, job.ws, checkouts),
                        excluded=run.closed_checkout_paths)
    approval = triage_store.triage_approval(run.triage_key)
    data = prepared_fields(t, branch, [*checkouts, *run.closed_checkouts], layer,
                           [run.dir / "images"] if images.paths else [],
                           pipeline.images_note(images), approval)
    if job.closed_prs:
        data["closed_prs"] = sorted(job.closed_prs)
    if feedback_prs:
        data["feedback_prs"] = feedback_prs
    return Outcome("pass", data=data)


def prepared_fields(ticket: tracker.Ticket, branch: str, checkouts: list[workspace.Checkout], layer: config.RepoLayer,
                    add_dirs: list[Path], attachments: str, approval: dict | None) -> dict:
    return {"ticket": {k: getattr(ticket, k) for k in TICKET_FIELDS}, "branch": branch,
            "checkouts": [c.to_row() for c in checkouts], "layer": str(layer.dir), "add_dirs": list(map(str, add_dirs)),
            "attachments": attachments, "triage": pipeline.triage_notes(approval), "approval": approval,
            "expertise": layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"}


@factory.step("plan")
def plan(flow: factory.Flow) -> Outcome:
    job: Job = flow.ctx
    run, layer = job.run, job.layer
    limit, info = layer.max_diff_lines, run.tracker.label_name("needs-info")
    reads = pipeline.git_reads(job.checkouts)
    res = run.agent("plan", job.brief.prompt(
        "plan", triage=job.triage,
        checks=pipeline.check_list(layer), journeys=journeys.guidance(layer.verify.feature_map, planning=True)
        if layer.verify else ""),
                    job.ws, role="plan", allowed_tools=prompt_loader.bash_rules(config.DEFAULT_ALLOW_BASH + reads),
                    json_schema=pipeline.PLAN_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        return end_as("fail", "failed", why="planning step failed", recovery=runner.start_failure(res, "plan"))
    found = res.structured
    plan_text = found.get("plan", "")
    run.save("plan.md", plan_text)
    estimate = int(found.get("estimated_lines") or 0)
    if found.get("ambiguous") and found.get("question"):
        run.log("plan: ambiguous, asking the author")
        raise run.owner_ask("plan", [owner_asks.text_ask(str(found["question"]))], res.session)
    if estimate > limit:
        split = [s for s in found.get("split") or [] if isinstance(s, dict) and s.get("title")]
        run.log(f"plan: about {estimate} lines, over the {limit} line limit; proposing a {len(split)} part split")
        run.save("split.json", split)
        return end_as("ask", "ask", summary=f"plan estimates {estimate} lines, over the {limit} line limit; split proposed",
                      comment=pipeline.split_comment(estimate, limit, split, run.tracker.queue_hint, info,
                                                     layer.config_file))
    return Outcome("pass", data={
        "plan": plan_text, "estimate": estimate,
        "goal": pipeline.triage_goal(
            found.get("verify", "").strip() or "The planner gave no steps: check what the issue asks for.", job.approval),
        "left_for_a_person": pipeline.left_section(run, (found.get("left_for_a_person") or "").strip()),
        "assumptions": pipeline.assumptions_of(found)})


@factory.step("patch")
def patch(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    policy = job.layer.patch
    res = job.run.agent(BUILD, job.brief.prompt(
        "patch", triage=job.triage, patch_files=policy.max_files,
        patch_lines=min(policy.max_lines, job.layer.max_diff_lines)), job.ws, role=BUILD,
        allowed_tools=prompt_loader.bash_rules(job.builder_bash), disallowed_tools=routing.NO_GIT_WRITES,
        json_schema=patching.SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        return end_as("fail", "failed", why="patch step failed", said=res.text,
                      recovery=runner.start_failure(res, BUILD))
    said = res.structured
    staged = pipeline.stage(job.checkouts, diffguard.denied_paths(job.layer))
    why = (str(said.get("reason") or "the builder requested a full plan") if said.get("needs_feature") else
           patching.change_refusal(policy, staged.files, staged.lines))
    if not why and staged.empty:
        return end_as("stop", "nothing", summary=str(said.get("summary") or "No patch was needed."))
    if why:
        job.run.log(f"patch: escalating to the feature flow: {why}")
    return Outcome("pass", data={
        "plan": str(said.get("plan") or "Implement the repair specified by the issue."),
        "goal": pipeline.triage_goal(
            str(said.get("verify") or "Check the issue's acceptance criteria and one nearby behavior."), job.approval),
        "estimate": min(policy.max_lines, job.layer.max_diff_lines), "left_for_a_person": "",
        "summary": str(said.get("summary") or res.text), "escalate": why,
    }, session=job.run.sessions.get(BUILD, NO_SESSION))


def revision_plan(feedback: str) -> str:
    return f"Revise the open PR for the owner's feedback:\n\n{feedback}"


@factory.step("revise")
def revision(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    run, layer = job.run, job.layer
    feedback = pipeline.feedback_markdown(run.feedback)
    left = pipeline.left_on_prs(run.feedback_prs)
    run.story.append(f"The owner reviewed the PR and left this feedback:\n\n{feedback}")
    res = run.agent("revise", job.brief.prompt("revise", feedback=feedback, checks=job.checks),
                    job.ws, role=BUILD, allowed_tools=prompt_loader.bash_rules(job.builder_bash),
                    disallowed_tools=routing.NO_GIT_WRITES, json_schema=pipeline.REVISE_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        return end_as("fail", "failed", why="revise step failed", said=res.text,
                      recovery=runner.start_failure(res, BUILD))
    summary, replies = res.structured.get("summary", ""), res.structured.get("replies") or []
    if pipeline.stage(job.checkouts, diffguard.denied_paths(layer)).empty:
        return end_as("stop", "unchanged", summary=summary, replies=replies)
    return Outcome("pass", data={
        "plan": revision_plan(feedback), "left_for_a_person": left,
        "goal": ("Check each change the owner's feedback asks for, and that what the issue asks for still works.\n\n"
                 + feedback),
        "feedback_keys": [pipeline.feedback_key(row) for row in run.feedback],
        "summary": summary, "replies": replies, "story": run.story}, session=run.sessions.get(BUILD, NO_SESSION))


@factory.step("repair")
def repair(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    run, layer = job.run, job.layer
    if not run.repairs:
        return end_as("fail", "failed", why="this run names no run to repair; start it with arete repair RUN")
    try:
        found = repairs.source(run_store.run_get(run.repairs) or {})
        targets = [c for c in job.checkouts if c.pr in found.prs]
        if missing := [url for url in found.prs if url not in {c.pr for c in targets}]:
            raise repairs.Refused(f"{', '.join(missing)} is not an open PR of this run's checkouts")
        heads = {c.github: repairs.matched_head(c.wt, c.lease, found) for c in targets}
    except repairs.Refused as e:
        return end_as("fail", "failed", why=str(e))
    moved = [c.github for c in targets if heads[c.github] != c.lease]
    findings = found.review.findings()
    count = len(found.review.blocking())
    run.log(f"repair: run {found.run_id}, {found.evidence}, {util.count(count, 'finding')}")
    for github in moved:
        run.log(f"repair: the PR head of {github} moved since run {found.run_id}; checks, verify and review run "
                "again on it")
    run.story.append(f"Review findings of run {found.run_id} to repair:\n{findings}")
    b = job.b
    b.restore_repair_state(found.repair_state)
    b.carry_review(found.review, restored=True)
    b.capture_repair_baselines("repair:before")
    prompt = job.brief.prompt("repair", plan=found.plan, source=found.run_id,
                              findings=b.repair_feedback() or findings, checks=job.checks)
    res = run.agent("repair", check_repair.builder_prompt(prompt),
                    job.ws, role=REVIEW_REPAIR, allowed_tools=prompt_loader.bash_rules(job.builder_bash),
                    disallowed_tools=routing.NO_GIT_WRITES)
    if not res.ok:
        return end_as("fail", "failed", why="repair step failed", said=res.text, repair_state=b.repair_state())
    passed, reply = b.retry_focused_repairs("repair", res.text)
    if not passed:
        return end_as("fail", "failed", why="focused review regressions did not pass", said=reply,
                      detail=b.repair_feedback(), repair_state=b.repair_state())
    if pipeline.stage(job.checkouts, diffguard.denied_paths(layer)).empty:
        return end_as("fail", "failed", said=res.text,
                      why=f"the builder changed nothing for the findings of run {found.run_id}, so they stay "
                          "unresolved")
    return Outcome("pass", data={
        "plan": found.plan, "left_for_a_person": found.left_for_a_person, "summary": reply,
        "repair_state": b.repair_state(),
        "goal": f"{found.goal}\n\nAlso check that each of these review findings is fixed:\n\n{findings}",
        "source": found.run_id, "evidence": found.evidence, "findings": count, "heads": heads, "moved": moved,
        "story": run.story}, session=run.sessions.get(BUILD, NO_SESSION))


@factory.step("implement")
def implement(flow: factory.Flow, tree: str, round: str, feedback: str = "", attempt: int = 1,
              tries: int = 1, asks: str = "") -> Outcome:
    job: Job = flow.ctx
    run = job.run
    atom = atom_of("implement", {"round": round, "asks": asks})
    if asks:
        res = job.b.resume(round, asks, role=atom, json_schema=pipeline.CI_FIX_SCHEMA)
        return Outcome("pass" if res.ok and isinstance(res.structured, dict) else "fail",
                       data={"round": round, "failures": feedback, "verdicts": res.structured, "said": res.text},
                       session=run.sessions.get(BUILD, NO_SESSION))
    if round == BUILD:
        return build(job)
    b = job.b
    denied = prompt_loader.code_list(b.deny)
    if round.startswith(REVIEW_FIX):
        b.capture_repair_baselines(f"{round}:before")
        problems = "\n\n".join(filter(None, (feedback, b.repair_feedback())))
        prompt = prompt_loader.prompt(REVIEW_FIX, problems=problems, notes=job.review.fix_notes(), denied=denied)
    else:
        prompt = pipeline.check_fix_prompt(attempt, tries, feedback, b.check_commands, b.deny)
    res = b.resume(round, prompt, role=atom)
    reply = res.text
    if res.ok and round.startswith(REVIEW_FIX):
        passed, reply = b.retry_focused_repairs(round, reply)
        if not passed:
            return end_as("fail", "failed", why="focused review regressions did not pass", said=reply,
                          detail=b.repair_feedback(), repair_state=b.repair_state())
    return Outcome("pass" if res.ok else "fail",
                   data={"round": round, "reply": reply, **({"repair_state": state} if (state := b.repair_state()) else {})},
                   session=run.sessions.get(BUILD, NO_SESSION))


def build(job: Job) -> Outcome:
    run, layer = job.run, job.layer
    prompt = job.brief.prompt(BUILD, plan=job.plan_text, checks=job.checks, estimate=job.estimate, verify=job.goal)
    res = run.agent(BUILD, check_repair.builder_prompt(prompt),
                    job.ws, role=BUILD, allowed_tools=prompt_loader.bash_rules(job.builder_bash),
                    disallowed_tools=routing.NO_GIT_WRITES)
    if not res.ok:
        return end_as("fail", "failed", why="build step failed", said=res.text,
                      recovery=runner.start_failure(res, BUILD))
    if not pipeline.stage(job.checkouts, diffguard.denied_paths(layer)).files:
        if job.estimate == 0:
            return end_as("stop", "nothing", summary=res.text)
        return end_as("fail", "failed", said=res.text,
                      why=f"the builder stopped without changing anything, though the plan estimated "
                          f"{util.count(job.estimate, 'line')}")
    return Outcome("pass", data={"round": BUILD, "summary": res.text},
                   session=run.sessions.get(BUILD, NO_SESSION))


@factory.step("checks")
def checks(flow: factory.Flow, tree: str, attempt: int = 1, tries: int = 1, after: str = "",
           quick: bool = False, focused: dict[str, list[str]] | None = None) -> Outcome:
    job: Job = flow.ctx
    run, b = job.run, job.b
    phase = after or "the build and its fixes"
    before = factory.fingerprint(job.ws) if (paths.CONFIG_DIR / "hosted-macos.toml").exists() else ""
    ok = b.check(quick=quick, after=phase, **({"focused": focused} if focused else {}))
    failures = [{k: v for k, v in item.items() if k != "passed"} for item in b.check_results if not item["passed"]]
    executed = [item["name"] for item in b.check_results]
    scope = "failed" if focused else "quick" if quick and set(executed) != set(job.layer.checks) else "full"
    hosted = hosted_check.check(job, before, failures) if not ok and before else None
    if hosted:
        result = hosted["status"]
        description = hosted.get("error") or hosted.get("url") or "waiting for GitHub"
        b.report += f"\n\n### GitHub-hosted macOS check: {result}\n{description}"
        if (result == "success" and hosted.get("exit") == 0
                and (scope != "full" or set(executed) == set(job.layer.checks))):
            failures = []
            ok = True
            if scope == "full":
                b.checked = {"tree": before, "commands": dict(job.layer.checks), "report": b.report}
        elif result == "failure":
            failures = [{"name": "behavior", "selectors": [], "exit": hosted.get("exit")}]
            b.report += "\n\n" + check_repair.diagnostics(failures)
        run.save("checks.md", b.report)
    details = {"scope": scope, "checks_executed": executed, "failed_checks": failures,
               "policy": job.policy("checks", {"quick": quick, "focused": focused}),
               "runner_blocked": bool(failures) and all(f.get("capability") == "macos-sandbox-profile"
                                                        for f in failures)}
    if hosted:
        details["hosted_check"] = hosted
    if not after:
        record.note(run.id, attempts=attempt)
    if not ok:
        if not after:
            retrying = attempt < tries
            heading = f"Checks failed on attempt {attempt}:" if retrying else "Checks still failing after the last attempt."
            run.story.append(pipeline.check_failure_story(heading, failures, b.report if retrying else ""))
        return Outcome("fail", next="implement", feedback=b.report,
                       data={**details, "report": b.report, "attempts": attempt, "after": after, "story": run.story,
                             "checked": b.checked})
    planned = f" (the plan estimated {job.estimate})" if job.estimate and not after else ""
    b.require_valid_diff(phase, planned)
    checked_tree = pipeline.check_tree(pipeline.checkout_trees(job.checkouts))
    full = (not focused and bool(checked_tree) and b.checked.get("tree") == checked_tree
            and b.checked.get("commands") == job.layer.checks
            and b.checked.get("report") == b.report)
    return Outcome("pass", data={**details, "report": b.report, "attempts": attempt, "after": after, "story": run.story,
                                 "full": full, "checked_tree": checked_tree,
                                 "commands": job.layer.checks, "checked": b.checked})


def retry_passed(verified: Verified) -> bool:
    return verified.verdict == "pass" and verified.finished and bool(verified.checks) and not verified.failed_checks


@factory.step("verify")
def verify(flow: factory.Flow, tree: str, single: bool = False, covered_commands: tuple[str, ...] = (),
           delivery_token="", progress_attempt: str = "", *, return_focused_hold: bool = False) -> Outcome:
    job: Job = flow.ctx
    b = job.staged()
    check_only = single or bool(job.run.environment_release)
    goal = job.with_left(job.goal)
    if single and "refactor" in job.ticket.labels and not spec_store.run_root(job.run):
        verified = Verified("unverified", "Check in use skipped because the ticket is labeled `refactor`.")
    elif check_only and not job.layer.verify:
        verified = pipeline.verify.not_configured(job.layer.dir, job.ws)
    elif check_only:
        verified = pipeline.check_in_use(
            b, goal, job.build_summary,
            **({"covered_commands": covered_commands} if single and covered_commands else {}),
            **({"delivery_token": delivery_token} if single and delivery_token else {}),
            **({"progress_attempt": progress_attempt} if progress_attempt else {}))
    else:
        try:
            verified = pipeline.verify_in_use(b, goal, job.build_summary, return_focused_hold=return_focused_hold)
        except pipeline.FocusedRepairHold as hold:
            if not return_focused_hold:
                raise
            return Outcome("fail", next="implement", feedback=hold.detail, data={
                "focused_hold": hold.why, "repair_state": b.repair_state(), "names": dict(b.steps),
                "story": job.run.story, "policy": job.policy("verify", {}), "checked": b.checked})
    if job.run.environment_release and not retry_passed(verified):
        return end_as("stop", "environment-held", verified=asdict(verified),
                      summary="Verification did not pass on the live release. The PR stays a draft.")
    return Outcome("pass", data={"verified": asdict(verified), "names": dict(b.steps), "story": job.run.story,
                                 "policy": job.policy("verify", {}), "checked": b.checked})


@factory.step("review")
def review(flow: factory.Flow, second: bool = False, attempt: int = 1, tries: int = 0, code_review: bool = False) -> Outcome:
    job: Job = flow.ctx
    run = job.run
    attempt = 2 if second else attempt
    stage = pipeline.round_name("review", attempt)
    if job.only == "review":
        job.restore_continued_ci()
    b = job.b
    if pending := b.check_repair_findings(producer=f"{stage}:before"):
        return Outcome("fail", next="implement", feedback=b.repair_feedback(),
                       data={"focused_hold": pending, "repair_state": b.repair_state()})
    before = b.ci_review() or (job.review if attempt > 1 else None)
    said = pipeline._review(job.staged(), job.with_left(job.plan_text), job.verified, stage, before, job.fix_reply,
                            **({"skill": True, "spec": job.with_left(job.goal)} if code_review else {}))
    data = {"review": said.asdict(), "attempt": attempt, "story": run.story}
    if state := b.repair_state():
        data["repair_state"] = state
    if said.verdict != "request_changes":
        return Outcome("pass", data=data)
    problems = said.findings()
    last = second or bool(tries and attempt >= tries)
    run.story.append(f"Review still requested changes after {util.count(attempt, 'review')}, the limit:\n{problems}"
                     if last else f"Review requested changes:\n{problems}")
    return Outcome("fail", next="" if last else "implement", feedback=problems,
                   data={**data, "review_exhausted": last})


@factory.step("learn")
def learn(flow: factory.Flow, tree: str, apply: bool) -> Outcome:
    job: Job = flow.ctx
    commit = pipeline._learn(job.run, job.ws, job.layer.expertise_file, job.ticket.title, apply=apply)
    return Outcome("pass", data={"applied": apply, "commit": commit})


def documents(job: Job) -> bool:
    return config.document_for(job.cfg, job.run.repo, job.layer).enabled


def direct_push_info(job) -> dict:
    value = getattr(job, "direct_push", None)
    return value if isinstance(value, dict) else {}


def deliver(flow: factory.Flow) -> tuple[str, str, str]:
    job: Job = flow.ctx
    ws = str(job.ws)
    step(flow, "learn", tree=ws, apply=True)
    if documents(job):
        step(flow, "document", tree=ws)
    if job.run.dry_run:
        return end_dry_run(flow)
    outcome = step(flow, "pr", tree=ws)
    if outcome.passed:
        if job.run.repo.parts and not direct_push_info(job):
            outcome = step(flow, "ci", tree=ws)
        if outcome.passed:
            outcome = step(flow, "publish")
    return ended(flow, outcome)


@factory.step("document")
def write_document(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    staged = job.staged().staged
    size = pipeline.change_size(staged)
    text = document.pr_text(job.run, job.ticket, pipeline.summary_of(job.review, job.build_summary), job.review,
                            job.verified, size, job.held_reason,
                            job.cfg, job.layer)
    return Outcome("pass", data={"text": text})


def change(job: Job, staged: pipeline.Staged | None = None) -> tuple[pipeline.Staged, str, str]:
    staged = job.b.restage() if staged is None else staged
    if staged.empty:
        raise workspace.RunFailed("nothing was left to push, since the workspace holds no change")
    size = pipeline.change_size(staged)
    return staged, size, f"{size}, review {job.review.verdict}, in use {job.verified.verdict}"


def publishing(job: Job, push: Callable[[], None]) -> None:
    run, branch = job.run, job.branch
    try:
        push()
    except workspace.RunFailed as e:
        if job.recovery_publication:
            raise
        if pilot.is_pilot_task(str(run.tid)):
            raise workspace.RunFailed(e.why, f"{e.detail}\n\nInspect the pilot receipt and this run's branch. "
                                            "This authorization cannot be resumed.") from e
        raise workspace.RunFailed(
            e.why, f"{e.detail}\n\nThis run's commit stays on `{branch}` in its worktree and nothing on origin was "
                   f"overwritten. `arete resume {run.id}` publishes it from the pr step when origin still holds only "
                   "this run's own commits, and refuses otherwise.") from e


def adopt_open_prs(job: Job) -> list[workspace.Checkout]:
    found = []
    for c in job.checkouts if job.run.repo.parts else []:
        if not c.pr and (url := gh.open_pr_url(c.github, job.branch)):
            c.pr = url
            found.append(c)
            job.run.log(f"found open {c.pr}, opened by an earlier try of this step")
    return found


def refuse_changed_prs(job: Job) -> None:
    run = job.run
    reason = f"the repair of run {run.repairs}" if run.repairs else "PR feedback"
    prs = job.prepared_feedback_prs + run.feedback_prs
    if run.repairs or prs:
        prs += [c.pr for c in job.with_prs]
    recovery = job.recovery_publication
    bound = {c.pr for c in job.with_prs if recovery and recovery.bound(c)}
    for url in dict.fromkeys(prs):
        if url in bound:
            continue
        view = gh.pr_view(url)
        if view.get("state") != "OPEN":
            raise workspace.RunFailed(f"{url} is {'merged' if view.get('mergedAt') else 'closed'}, so arete pushed "
                                     f"nothing for {reason}")
        if view.get("headRefName") != job.branch:
            raise workspace.RunFailed(f"{url} is on branch {view.get('headRefName')}, not {job.branch}, so arete "
                                     f"pushed nothing for {reason}")


def settle_unpushed_checkouts(job: Job):
    b = job.b
    fresh = b.restage(from_base=False)
    staged = b.restage(from_base=True) if fresh.empty or b.from_base else fresh
    staged, size, outcome = change(job, staged)
    unpushed = []
    recovery = job.recovery_publication
    if recovery:
        recovery.recheck(during="while settling the retained commits")
    for c in [c for c in staged.changed if c not in fresh.changed]:
        head = util.git(["rev-parse", "HEAD"], c.wt).strip()
        remote = recovery.expected_head(c) if recovery else workspace.remote_head(c.src, job.branch)
        if remote == head:
            c.note_pushed_head(head)
        elif remote == c.lease and (not remote or ancestor(c.wt, remote, head)):
            unpushed.append(c)
        elif recovery:
            raise recovery.failure(f"the retained head {head} in {c.github} does not contain the checked head {remote}")
        else:
            raise workspace.RunFailed(f"nothing was left to push in {c.github}, and its checkout of "
                                     f"{job.branch} does not match origin")
    return fresh, staged, size, outcome, unpushed


def held_pr_check(job: Job, targets: list[workspace.Checkout], *, ordinary_hold: str = "") -> Callable[[], None]:
    check_targets = job.check_publication_targets

    def check_held_targets(*, convert=False):
        if check_targets:
            check_targets()
        for c in targets:
            try:
                pipeline.require_held_pr(job.run, c, convert=convert)
            except workspace.RunFailed as e:
                if ordinary_hold:
                    raise workspace.RunFailed(f"{c.pr} is not a confirmed draft at this run's head", ordinary_hold) from e
                raise workspace.RunFailed(e.why, job.b.repair_feedback()) from e

    check_held_targets(convert=not (job.run.feedback or job.prepared_feedback_prs))
    return check_held_targets


def publish_branch(job: Job, fresh, staged, unpushed, before_push=None, *, check_targets=None):
    b, run, t = job.b, job.run, job.ticket
    check_targets = job.check_publication_targets if check_targets is None else check_targets
    if unpushed:
        publishing(job, lambda: pipeline.commit_and_push(
            job.checkouts, unpushed, job.branch, "", before_push=before_push,
            check_targets=check_targets, recovery=job.recovery_publication))
    if fresh.empty:
        b.committed_lines = max(b.committed_lines, staged.lines)
    committed = not fresh.empty or bool(unpushed)
    subject = prompt_loader.fill_template(job.layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    if spec_store.run_root(run):
        subject = outbound.inert(subject)
    revised = ("Revised for the review feedback on the PR.\n\n" if run.feedback else
               f"Repairs the review findings of run {run.repairs}.\n\n" if run.repairs else "")
    if not fresh.empty:
        b.staged = fresh
        publishing(job, lambda: b.push(
            job.branch, f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}",
            before_push=before_push, check_targets=check_targets, recovery=job.recovery_publication))
        b.staged = staged
    return subject, committed


def publication_outcome(job: Job, staged, summary, committed, draft_why="", report_body="", opened=(), pushed=(),
                        found=(), drafted=(), ci_stopped=(), direct_push: dict[str, dict] | None = None) -> Outcome:
    from .store.orchestration import digest
    graph = job.ticket.body[len(job.ticket.graph_body) + 2:] if isinstance(getattr(job.ticket, "graph_body", None), str) else ""
    data = {**({"graph_contract": digest(graph)} if graph else {}),
            **({"direct_push": direct_push} if direct_push else {}),
            "summary": summary, "prs": {c.github: c.pr for c in job.with_prs} if job.run.repo.parts else {},
            "opened": [c.github for c in opened], "pushed": [c.github for c in pushed], "draft_why": draft_why,
            "changed": [c.github for c in staged.changed], "report_body": report_body,
            "committed_lines": job.b.committed_lines, "heads": {c.github: c.lease for c in staged.changed},
            "committed": committed, "verified": asdict(job.verified)}
    if job.run.repo.parts:
        data.update(found=[c.github for c in found], drafted=[c.github for c in drafted],
                    ci_stopped=[c.github for c in ci_stopped])
    return Outcome("pass", data=data)


def report(job: Job, size: str, images: dict, held: str, *, measurement_section: str = "") -> str:
    return pipeline.report_body(job.run, summary=job.build_summary, review=job.review, verified=job.verified,
                                left=job.left_for_a_person, images=images, approval=job.approval, attempts=job.attempts,
                                checks=pipeline.check_list(job.layer), plan_text=job.plan_text, size=size,
                                held=held, written=job.document, measurement_section=measurement_section)


def open_or_refresh_prs(job: Job, found, staged, size, outcome, subject, draft_why, committed,
                        before_push=None, *, updated: list[workspace.Checkout],
                        held_at_head: list[workspace.Checkout], measurement_section: str = "") -> Outcome:
    run, t, tk = job.run, job.ticket, job.run.tracker
    recovery = job.recovery_publication
    if recovery:
        recovery.recheck(during="before refreshing PR evidence")
    evidence_checkout = next((c for c in staged.changed if not recovery or recovery.bound(c)), None)
    images = pipeline.evidence_images(run, evidence_checkout, job.verified, recovery=recovery) if evidence_checkout else {}
    report_body = report(job, size, images, draft_why, measurement_section=measurement_section)
    link = tk.pr_link(t, whole=not job.left_for_a_person)
    opened = [c for c in found if c in staged.changed]
    found = list(opened)
    stops = {c.github: pipeline.ci.stopped_section(gh.pr_body(c.pr)) for c in found}
    for c in staged.changed:
        if c in opened:
            gh.pr_edit_body(c.pr, with_stop(report_body, stops.get(c.github, "")), link=link)
        elif not c.pr:
            if before_push:
                remote_head = workspace.remote_head(c.src, job.branch)
                if not remote_head or remote_head != c.lease or c.lease != before_push(c):
                    raise workspace.RunFailed("the authorized pilot branch changed before PR creation")
            title = tk.pr_title(subject, t)
            write = (recovery.pr_write(c, "PR creation", "before opening the PR", "after opening the PR",
                                       draft=bool(draft_why)) if recovery else nullcontext())
            body = (pipeline.with_siblings("Gate evidence is posted separately.",
                                           pipeline.sibling_section(t.ref, c, job.with_prs)) if recovery else report_body)
            with write:
                c.pr = gh.pr_create(c.github, job.branch, c.base, title, body, labels=tk.pr_labels,
                                    draft=bool(draft_why), cwd=c.wt, link=link, **({"single_attempt": True} if recovery else {}))
            if not recovery:
                pipeline.note_prs(run, job.branch, job.with_prs)
            opened.append(c)
            if before_push and gh.pr_draft_and_head(c.pr)[1] != c.lease:
                raise workspace.RunFailed("GitHub created a PR with a different head; inspect it before any merge")
            run.log(f"opened {c.pr}{' as draft' if draft_why else ''}")
        else:
            run.log(f"pushed to {c.pr}" if c in updated else f"checked {c.pr} again, nothing new to push")
    standing = [c for c in staged.changed if c not in opened]
    pushed = [c for c in standing if c in updated]
    with_prs = job.with_prs
    if recovery:
        if opened and len(with_prs) > 1:
            pipeline.link_recovered_prs(run, t, recovery, with_prs)
        ci_stopped = pipeline.refresh_recovered_prs(run, t, recovery, with_prs, staged.changed, report_body, link)
        drafted = pipeline.hold_pushed(run, standing + found, ci_stopped, draft_why, recovery=recovery)
        recovery.recheck(during="after refreshing PR evidence")
    else:
        pipeline.note_prs(run, job.branch, with_prs)
        ci_stopped = [c for c in found if stops[c.github]]
        ci_stopped += pipeline.sync_pr_bodies(run, t, with_prs, opened, standing, pushed, report_body,
                                             job.left_for_a_person, link, stops, committed,
                                             measurement_section=measurement_section)
        drafted = held_at_head + pipeline.hold_pushed(
            run, pushed + [c for c in found if c in updated], ci_stopped, draft_why)
    return publication_outcome(job, staged, outcome, committed, draft_why, report_body, opened, pushed,
                               found, drafted, ci_stopped)


def require_environment_owner_clear(job: Job) -> None:
    run = job.run
    if not run.environment_release:
        return
    from . import environment_gaps

    source = run_store.run_get(job.continues) or {}
    talks = {c.pr: gh.pr_discussion(c.pr) for c in job.with_prs}
    if (not source or pipeline.pending_feedback(run.repo.name, str(run.tid))
            or environment_gaps.owner_intervened(source, run.tracker, talks)):
        raise factory.Refused("owner feedback took over the held PR before publication")


def uses_direct_push(job: Job, found: list[workspace.Checkout]) -> bool:
    run = job.run
    return (run.repo.landing == "direct-push" and not found and not job.recovery_publication
            and not run.feedback and not run.repairs and not pilot.is_pilot_task(str(run.tid))
            and not any(c.pr for c in job.checkouts))


def publish_direct_push(job: Job) -> Outcome:
    b, run, t = job.b, job.run, job.ticket
    fresh = b.restage(from_base=False)
    staged = b.restage(from_base=True) if fresh.empty or b.from_base else fresh
    staged, _, outcome = change(job, staged)
    if len(staged.changed) != 1 or len(job.checkouts) != 1:
        raise workspace.RunFailed("a change spanning repositories cannot be direct-pushed atomically")
    c = staged.changed[0]
    subject = prompt_loader.fill_template(job.layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    if spec_store.run_root(run):
        subject = outbound.inert(subject)
    if not fresh.empty:
        message = f"{tracker.with_ref(subject, t.ref)}\n\nArete-Run: {run.id}"
        if util.git(["diff", "--cached", "--name-only"], c.wt).strip():
            before = util.git(["rev-parse", "HEAD"], c.wt).strip()
            try:
                util.git_as_owner(["commit", "-q", "-m", message], c.wt)
            except RuntimeError as error:
                raise workspace.RunFailed(f"the commit in {c.github or run.repo.name} failed, so arete pushed nothing",
                                          str(error)) from error
            c.onto = c.onto or before
        b.committed_lines = max(b.committed_lines, fresh.lines)
    else:
        b.committed_lines = max(b.committed_lines, staged.lines)
    b.staged = staged
    landed = pipeline.direct_push_checkout(
        b, c, job.verified, job.review, pipeline.automerge_about(t, job.build_summary),
        draft_why=job.held_reason, left_for_a_person=job.left_for_a_person, slot=job.slot,
    )
    record.note(run.id, branch=c.base)
    return publication_outcome(job, staged, outcome, committed=True,
                               direct_push={c.github or run.repo.name: landed})


@factory.step("pr")
def pr(flow: factory.Flow, tree: str, *, measurement_section: str = "", repair_exhausted: bool = False) -> Outcome:
    from . import zte

    job: Job = flow.ctx
    run = job.run
    from . import spec_graph_delivery

    focused_draft = ""
    if job.built is not None and (pending := job.b.focused_hold()):
        if ((repair_exhausted or job.review_exhausted) and run.repo.parts and not run.environment_release
                and not pilot.is_pilot_task(str(run.tid)) and not zte.reserved(str(run.tid))
                and job.verified.verdict != "fail" and job.b.full_checks_current(pipeline.verification_trees(job.b))):
            focused_draft = job.b.focused_draft_hold(job.review)
        if not focused_draft:
            raise workspace.RunFailed(f"publication needs current focused repair evidence: {pending}", job.b.repair_feedback())
    spec_graph_delivery.require_active_child(run, job.ticket)
    require_environment_owner_clear(job)
    run.tracker.pr_link(job.ticket, whole=not job.left_for_a_person)
    recovery = job.recovery_publication
    found = [] if recovery else adopt_open_prs(job)
    workspace.require_target_branch(run.repo, job.checkouts, work_branch=job.branch)
    refuse_changed_prs(job)
    if uses_direct_push(job, found):
        return publish_direct_push(job)
    before_push = None
    if pilot.is_pilot_task(str(run.tid)):
        if (len(job.checkouts) != 1 or run.feedback or run.repairs or found
                or any(c.resumes or c.lease or c.pr for c in job.checkouts)
                or not job.branch.startswith(f"arete/{run.tid}-")):
            raise workspace.RunFailed("the pilot can only publish a fresh branch for this local task")

        def before_push(checkout):
            try:
                return pilot.publication_head(run.repo, str(run.tid), run.id, checkout)
            except pilot.Held as error:
                raise workspace.RunFailed(str(error)) from error

    fresh, staged, size, outcome, unpushed = settle_unpushed_checkouts(job)
    updated = unpushed + fresh.changed
    draft_why = "\n\n".join(filter(None, (job.held_reason, focused_draft)))
    repair_hold = focused_draft or job.b.in_use_repair_hold(job.verified)
    held_at_head = []
    feedback_prs = run.feedback_prs + job.prepared_feedback_prs
    if run.repo.parts and draft_why and not recovery and (repair_hold or not feedback_prs):
        ordinary_recheck = job.continues and not (updated or run.repairs or repair_hold)
        held_at_head = [c for c in staged.changed if c.pr and c not in updated
                        and (c in found or (c.revision and (ordinary_recheck or c.lease != c.revision))
                             or (repair_hold and c.pr in feedback_prs))]
    check_targets = job.check_publication_targets
    if repair_hold or held_at_head:
        held_updates = [c for c in updated if c.pr] if repair_hold else []
        check_targets = held_pr_check(job, held_updates + held_at_head, ordinary_hold="" if repair_hold else draft_why)
    subject, committed = publish_branch(job, fresh, staged, unpushed, before_push, check_targets=check_targets)
    if not job.run.repo.parts:
        record.note(job.run.id, branch=job.branch)
        if draft_why:
            return end_as("fail", "failed",
                          why=f"pushed {job.branch}, but {draft_why}, and this repo has no GitHub name for a draft PR")
        return publication_outcome(job, staged, outcome, committed)
    return open_or_refresh_prs(job, found, staged, size, outcome, subject, draft_why, committed, before_push,
                               updated=updated, held_at_head=held_at_head, measurement_section=measurement_section)


def with_stop(text: str, stop: str) -> str:
    return pipeline.ci.with_section(text, stop) if stop else text


@factory.step("ci")
def watch(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    run = job.run
    if job.only == "ci":
        job.restore_continued_ci()
    b = job.b
    b.from_base = False

    def fix(name: str, text: str, failures: str) -> Result:
        done = step(flow, "implement", tree=tree, round=name, feedback=failures, asks=text)
        return Result(done.passed, done.data.get("said", ""), structured=done.data.get("verdicts"))

    draft = job.draft_why
    if job.continued_ci.get("standalone") == "review" and (reviewed := b.ci_review()) and reviewed.approved():
        draft = draft or "a standalone review does not clear the publication hold"
    try:
        result = pipeline.watch_ci(b, job.slot, job.branch, job.picked(job.changed), job.with_prs, draft,
                                   fix=fix, review=job.review, goal=job.with_left(job.goal), patch_mode=job.patch_mode)
    except workspace.RunFailed as e:
        detail = e.detail
        if not run.feedback:
            prs = pipeline.pr_link_list(job.with_prs)
            detail += f"\n\n### PRs\n\n{prs}\n\nReview comments on a PR start a revision, as on any arete PR."
        return end_as("fail", "failed", why=e.why, prs=not run.feedback, detail=detail, recovery=e.recovery)
    return Outcome("pass", data={"summary": result})


def enter_publication(run: pipeline.Run) -> None:
    run.enter("pr")
    run.retry_gate_evidence()


@factory.step("publish")
def publish(flow: factory.Flow, merge: Callable[[list[workspace.Checkout]], dict | None] | None = None) -> Outcome:
    job: Job = flow.ctx
    run = job.run
    require_environment_owner_clear(job)
    workspace.require_target_branch(run.repo, job.checkouts, work_branch=job.branch)
    if landed_map := direct_push_info(job):
        from . import release_proof

        landed = next(iter(landed_map.values()))
        run.enter("done")
        waiting = (release_proof.required(run.repo.name, run.tid) or bool(run.spec_root)
                   or bool(spec_store.find(run.repo.name, str(run.tid))))
        suffix = "; awaiting release verification" if waiting else ""
        what = f"pushed {landed['revision'][:12]} directly to {landed['branch']}{suffix}"
        return end_as("pass", "done", summary=f"{what}: {job.summary}")
    if not run.repo.parts:
        run.enter("done")
        what = (f"pushed {job.branch} to origin" if job.committed
                else f"verified and reviewed {job.branch} again with nothing new to push")
        return end_as("pass", "done", summary=f"{what}: {job.summary}")
    opened, pushed, with_prs = job.picked(job.opened), job.picked(job.pushed), job.with_prs
    fresh = [c for c in opened if c.github not in job.found]
    recovery = job.recovery_publication
    if recovery:
        run.enter("pr")
    else:
        enter_publication(run)
    candidates = publication_candidates(job, fresh)
    if recovery:
        recovery.recheck(during="before finishing publication")
    if merge and (failure := merge(candidates)):
        return Outcome("fail", feedback=failure["error"], data=failure)
    if not merge:
        job.ci_result = pipeline.automerge(
            job.b, job.approval, job.draft_why, job.verified, job.review, candidates,
            pipeline.automerge_about(job.ticket, job.build_summary), slot=job.slot, branch=job.branch,
            goal=job.with_left(job.goal), plan=job.with_left(job.plan_text)) or job.ci_result
    if run.feedback:
        if recovery:
            pipeline.answer_feedback(run, job.replies, "See the recovered publication evidence on this PR.",
                                     recovery=recovery)
            recovery.recheck(during="after replying to PR feedback")
        else:
            pipeline.answer_feedback(run, job.replies, "Done in the latest push.")
            run.say(f"Revised for your review.\n\n{job.report_body}\n\nCI: {job.ci_result}.")
    elif run.repairs:
        run.say(f"PRs for this ticket:\n\n{pipeline.pr_status_lines(with_prs, opened, pushed)}\n\nCI: {job.ci_result}."
                + (pipeline.draft_note(job.draft_why, job.verified, kept=True)
                   or "\n\nThe review now approves it. The PR is still a draft until a person marks it ready."))
    else:
        run.say(f"PRs for this ticket:\n\n{pipeline.pr_status_lines(with_prs, opened, pushed)}\n\nCI: {job.ci_result}."
                + pipeline.published_note(job.draft_why, job.verified, fresh, job.picked(job.drafted)))
    return end_as("pass", "pr", summary=f"{job.summary}, CI: {job.ci_result}")


def can_ready_drafts(job: Job) -> bool:
    return not job.recovery_publication or retry_passed(job.verified) and job.review.approved()


def publication_candidates(job: Job, fresh: list[workspace.Checkout]) -> list[workspace.Checkout]:
    if job.run.feedback or job.prepared_feedback_prs or (job.built is not None and job.b.ci_review_pending()):
        return []
    standing = [c for c in job.picked(job.changed) if c not in fresh]
    return fresh + pipeline.ready_pushed(job.run, standing, job.picked(job.ci_stopped), job.draft_why,
                                         recovery=job.recovery_publication, ready_drafts=can_ready_drafts(job))


@factory.step("respond")
def respond(flow: factory.Flow, ending: str, why: str = "", said: str = "", detail: str = "", comment: str = "",
            summary: str = "", status: str = "", replies: list[dict] | None = None, prs: bool = False,
            asked: list[dict] | None = None, session: dict | None = None, verified: dict | None = None,
            recovery: dict | None = None) -> Outcome:
    job: Job = flow.ctx
    run, pr_url = job.run, ""
    if ending == "failed":
        status, summary, _ = pipeline._fail(run, why, said, detail, recovery=recovery)
        pr_url = pipeline.record_prs(run, job.branch, job.with_prs) if prs else ""
    elif ending == "ask":
        status, summary, _ = pipeline._ask(run, job.ws, job.checkouts, comment, summary, asked, session)
    elif ending == "nothing":
        status, summary, pr_url = pipeline.nothing_to_build(run, job.ticket, job.ws, job.checkouts, job.branch,
                                                            summary, job.left_for_a_person)
    elif ending == "unchanged":
        status, summary, pr_url = pipeline.answer_without_changes(run, job.ws, job.checkouts, job.branch, summary,
                                                                  replies or [])
    elif ending == "pr":
        pr_url = pipeline.record_prs(run, job.branch, job.with_prs)
        pipeline.release_workspace(run, job.ws, job.checkouts)
    elif ending == "environment-held":
        source = run_store.run_get(job.continues) or {}
        pr_url = source.get("pr_url") or ""
        job.verified = Verified.stored(verified or {})
        run.facts["requirements"] = job.verified.requirements()
        run.facts["held"] = summary
        run.enter("pr")
        record.note(run.id, branch=job.branch, pr_url=pr_url)
        pipeline.supersede_older_runs(run, pr_url.split())
        status = "pr"
    elif ending == "done":
        run.say(done_comment(job, summary))
        pipeline.release_workspace(run, job.ws, job.checkouts)
    elif ending == "dry-run":
        run.log(f"dry run: {summary}; worktree kept at {job.ws}")
    return Outcome("pass", data={"status": status or ending, "summary": summary, "pr_url": pr_url})


def done_comment(job: Job, summary: str) -> str:
    said = job.review
    return (f"Done: {summary}.\n\n## Summary\n\n{job.document or pipeline.summary_of(said, job.build_summary)}\n\n"
            f"{pipeline.assumed_block(job.run)}"
            f"{pipeline.CHECKED_IN_USE_HEADING}\n\n{job.verified.markdown()}\n\n"
            f"{said.markdown()}\n\n"
            f"Run `{job.run.id}`, {util.paid_note(record.metered_spent(job.run.id))}.")


def step(flow: factory.Flow, name: str, **kw) -> Outcome:
    before = flow.skipped
    outcome = flow.step(name, **kw)
    flow.ctx.take(name, outcome, stored=flow.skipped > before)
    return outcome


def finish(flow: factory.Flow, ending: str, **fields) -> tuple[str, str, str]:
    said = step(flow, "respond", ending=ending, **fields).data
    return said["status"], said["summary"], said["pr_url"]


def ended(flow: factory.Flow, outcome: Outcome) -> tuple[str, str, str]:
    return finish(flow, **{key: value for key, value in outcome.data.items() if key not in ("repair_state", "ci_review")})


def end_dry_run(flow: factory.Flow, *, measurement_section: str = "") -> tuple[str, str, str]:
    job = flow.ctx
    staged, size, outcome_text = change(job)
    job.run.save("diff.patch", staged.patch)
    job.run.save("pr_body.md", gh.outgoing(report(
        job, size, {}, job.held_reason, measurement_section=measurement_section)))
    return finish(flow, "dry-run", summary=outcome_text)


def rerun_rejected_ci(flow: factory.Flow) -> tuple[str, str, str] | None:
    job: Job = flow.ctx
    if (job.run.dry_run or job.run.path not in ("feature", "patch", "revise", "repair")
            or (job.cfg.adws and job.cfg.adws.resolve() != paths.ADWS_DIR.resolve())):
        return None
    ci_at = max((i for i, line in enumerate(flow.earlier) if line.get("step") == "ci"), default=-1)
    if (ci_at < 0 or not job.continued_ci.get("ci_review")
            or (flow.earlier[ci_at].get("result") != "fail" and not job.continued_ci.get("standalone"))
            or not (published := publication(flow.continues))):
        return None
    tree = str(job.ws)
    if not (prepared := step(flow, "prepare", tree=tree)).passed:
        return ended(flow, prepared)
    for line in flow.earlier[flow.at:ci_at]:
        name = line.get("step")
        if name in ("plan", "patch", "revise", "repair", "implement") and line.get("result") == "pass":
            job.take(name, factory.outcome_of(line), stored=True)
    job.take("pr", Outcome("pass", data=published), stored=True)
    step(flow, "ci", tree=tree)
    status, summary, _ = alone(flow)
    return status, summary, ""


def driven(flow: factory.Flow, drive: Callable[[factory.Flow], tuple[str, str, str]]) -> tuple[str, str, str]:
    try:
        if flow.only == "ci" and (outcome := rerun_rejected_ci(flow)) is not None:
            return outcome
        return drive(flow)
    except (pipeline.CatchUpPushUnconfirmed, pr_recovery.Hold):
        raise
    except workspace.RunFailed as e:
        if flow.ctx.run.environment_release:
            raise
        return finish(flow, "failed", why=e.why, detail=e.detail, recovery=e.recovery)
    except tracker.ERRORS as e:
        if flow.ctx.run.environment_release:
            raise
        return finish(flow, "failed", why=f"tracker refused {flow.ctx.run.ref}: {e}")
    except owner_asks.Asked as e:
        if flow.ctx.run.environment_release:
            raise factory.Refused("verification retry requires owner input") from e
        reached = f"ask limit of {e.limit} reached: " if e.limit is not None else ""
        parks = e.limit is None
        return finish(flow, "ask", summary=reached + owner_asks.summary(e.sent),
                      comment=owner_asks.comment(e.sent, flow.ctx.run.tracker.label_name("needs-info"), e.limit,
                                           run_id=flow.run_id if parks else ""),
                      asked=[{"ask_id": ask_id, "step": e.step, "call": e.call, "why": ask["why"],
                              "tried": ask.get("tried", ""), "questions": ask["questions"]}
                             for ask_id, ask in zip(e.ask_ids, e.sent, strict=True)],
                      session=asdict(e.session) if parks else None)


def alone(flow: factory.Flow) -> tuple[str, str, bool]:
    found = [line for line in factory.ends(run_lines.read(flow.run_id)) if line.get("step") == flow.only]
    if not found:
        return "failed", f"the script never ran {flow.only}", False
    said = util.first_line(str(found[-1].get("error") or found[-1].get("feedback") or ""), 300)
    return "step", f"{flow.only} {found[-1]['result']}" + (f": {said}" if said else ""), found[-1]["result"] == "pass"
