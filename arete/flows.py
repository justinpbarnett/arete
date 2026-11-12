from __future__ import annotations

import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import (
    config,
    diffguard,
    environment,
    factory,
    gh,
    jev,
    lines,
    paths,
    pipeline,
    record,
    runner,
    state,
    tracker,
    util,
)
from .agent import NO_SESSION, Result
from .factory import Outcome
from .verify import Verified

ADW = "adw"
TICKET_FIELDS = ("id", "ref", "title", "body", "comments", "kind", "url")
BUILD = "build"
REVIEW_FIX = "review_fix"
ROLE_OF = {"plan": "plan", "implement": BUILD, "revise": BUILD, "verify": "verify", "review": "review", "learn": "learn"}


def wanted(repo: config.Repo) -> str:
    return config.load_layer(repo.layer_dir).factory_path


def has_origin(repo: config.Repo) -> bool:
    return repo.path.is_dir() and "origin" in util.git(["remote"], repo.path, check=False).split()


def ran_on_seam(run_id: str) -> bool:
    return any(line.get("event") == "route" or (line.get("event") == "step_start" and line.get("step") == "prepare")
               for line in lines.read(run_id))


def start(cfg: config.Config, run: pipeline.Run, earlier: dict, slot: pipeline.RunSlot | None = None,
          continues: str = "", only: str = "", again: str = "") -> factory.Flow:
    ws = paths.WORKTREES_DIR / f"{run.repo.name}-{run.tid}"
    continues = continues or (earlier["id"] if earlier.get("status") == "interrupted" and ran_on_seam(earlier["id"])
                              else "")
    job = Job(cfg, run, ws, slot=slot)
    return factory.Flow(run.id, continues=continues, only=only, ctx=job, labels=job.labels, again=again)


def facts(name: str, data: dict) -> dict:
    if name in ("plan", "revise"):
        return {"assumptions": pipeline.assumptions_of(data), "left_for_a_person": str(data.get("left_for_a_person") or "")}
    if name == "verify" and isinstance(data.get("verified"), dict):
        return {"requirements": Verified.stored(data["verified"]).requirements()}
    if name == "review" and isinstance(data.get("review"), dict):
        return {"review": pipeline.report_review(data["review"])}
    return {}


def seam_facts(run_id: str) -> dict:
    found: dict = {}
    for line in factory.history(factory.chain(run_id)):
        if line.get("result") == "pass" or line.get("step") == "review":
            found.update(facts(str(line.get("step") or ""), factory.outcome_of(line).data))
    return found


@dataclass
class Job:
    cfg: config.Config
    run: pipeline.Run
    ws: Path
    ticket: tracker.Ticket = field(default_factory=lambda: tracker.Ticket("", "", ""))
    branch: str = ""
    checkouts: list[pipeline.Checkout] = field(default_factory=list)
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
    review: dict = field(default_factory=dict)
    built: pipeline.Build | None = None
    slot: pipeline.RunSlot | None = None
    approval: dict | None = None
    replies: list[dict] = field(default_factory=list)
    attempts: int = 1
    report_body: str = ""
    draft_why: str = ""
    summary: str = ""
    opened: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    ci_result: str = ""

    @property
    def with_prs(self) -> list[pipeline.Checkout]:
        return [c for c in self.checkouts if c.pr]

    def picked(self, names: list[str]) -> list[pipeline.Checkout]:
        return [c for c in self.checkouts if c.github in names]

    @property
    def builder_bash(self) -> list[str]:
        return self.layer.builder_bash + pipeline.git_reads(self.checkouts)

    @property
    def workspace(self) -> str:
        return pipeline.workspace_note(self.branch, self.checkouts)

    @property
    def b(self) -> pipeline.Build:
        if self.built is None:
            self.built = pipeline.Build(
                cfg=self.cfg, run=self.run, ticket=self.ticket, ws=self.ws, checkouts=self.checkouts, layer=self.layer,
                timeout=self.layer.timeout_minutes * 60, workspace=self.workspace, attachments=self.attachments,
                tools=pipeline.bash_rules(self.builder_bash), deny=diffguard.denied_paths(self.layer),
                expertise=self.expertise, left_for_a_person=self.left_for_a_person)
        return self.built

    def staged(self) -> pipeline.Build:
        if not self.b.staged.files:
            self.b.restage()
        return self.b

    def labels(self, name: str) -> dict:
        if name not in ROLE_OF:
            return {}
        s = self.run.settings(ROLE_OF[name])
        return {"runner": s.runner, "model": s.model, "effort": s.effort, "tier": s.tier, "kind": self.run.kind,
                "env": runner.env_names(s.model, s.runner, self.cfg, self.run.repo, self.run.id)}

    def with_left(self, text: str) -> str:
        if not self.left_for_a_person:
            return text
        return f"{text}\n\n{pipeline.NOT_CHECKED_LEFT_FOR_A_PERSON}{self.left_for_a_person}"

    def take(self, name: str, outcome: Outcome, stored: bool) -> None:
        data, run = outcome.data, self.run
        if outcome.passed or name == "review":
            run.facts.update(facts(name, data))
        if "story" in data:
            run.story = list(data["story"])
        if name == "prepare" and outcome.passed:
            self.prepared(data, stored)
        elif name == "plan" and outcome.passed:
            self.plan_text, self.goal, self.estimate = data["plan"], data["goal"], data["estimate"]
            self.left_for_a_person = data["left_for_a_person"]
        elif name == "revise" and outcome.passed:
            self.plan_text, self.goal, self.estimate = data["plan"], data["goal"], 0
            self.left_for_a_person, self.build_summary, self.replies = (data["left_for_a_person"], data["summary"],
                                                                        data["replies"])
            if outcome.session.id:
                run.sessions[BUILD] = outcome.session
        elif name == "implement":
            self.build_summary = data.get("summary", self.build_summary)
            if outcome.session.id:
                run.sessions[BUILD] = outcome.session
        elif name == "checks" and "report" in data:
            self.b.report = data["report"]
            if not data.get("after"):
                self.attempts = data["attempts"]
                if stored:
                    record.note(run.id, attempts=self.attempts)
        elif name == "verify" and outcome.passed:
            self.verified = Verified.stored(data["verified"])
            self.b.steps = Counter(data["names"])
        elif name == "review" and "review" in data:
            self.review = data["review"]
        elif name == "pr" and outcome.passed:
            self.published(data, stored)
        elif name == "ci" and outcome.passed:
            self.ci_result = data["summary"]

    def published(self, data: dict, stored: bool) -> None:
        for c in self.checkouts:
            c.pr = data["prs"].get(c.github, c.pr)
        self.opened, self.pushed, self.changed = data["opened"], data["pushed"], data["changed"]
        self.draft_why, self.report_body, self.summary = data["draft_why"], data["report_body"], data["summary"]
        self.b.committed_lines = data["committed_lines"]
        if stored:
            for c in self.picked(self.changed):
                c.lease = util.git(["rev-parse", "HEAD"], c.wt).strip()

    def prepared(self, data: dict, stored: bool) -> None:
        run = self.run
        self.ticket = tracker.Ticket(**data["ticket"])
        self.branch, self.attachments, self.triage = data["branch"], data["attachments"], data["triage"]
        self.expertise, self.approval = data["expertise"], data.get("approval")
        self.checkouts = [pipeline.Checkout(**{**c, "src": Path(c["src"]), "wt": Path(c["wt"])})
                          for c in data["checkouts"]]
        self.layer = pipeline.checked_layer(self.cfg, Path(data["layer"]))
        run.cfg, run.layer, run.kind = self.cfg, self.layer, self.ticket.kind
        run.add_dirs += [d for d in map(Path, data["add_dirs"]) if d not in run.add_dirs]
        if stored:
            opened(run, self.ticket)
            if not run.alone:
                run.enter("running")


def opened(run: pipeline.Run, t: tracker.Ticket) -> None:
    lines.write(run.id, "task", ticket=t.ref, kind=t.kind, repo=run.repo.name, title=t.title,
                **({"path": run.path} if run.path else {}))
    if run.answering:
        lines.write(run.id, "answer", asked_in=run.answering["id"], question=run.answering.get("summary") or "",
                    text=pipeline.answer_text(run.answering["id"]))


def end_as(result: str, kind: str, **fields) -> Outcome:
    return Outcome(result, feedback=fields.get("why") or fields.get("summary") or "", data={"ending": kind, **fields})


def local_workspace(run: pipeline.Run, base: str, branch: str, ws: Path) -> list[pipeline.Checkout]:
    repo = run.repo
    checkouts = [pipeline.Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir,
                                   base or util.origin_default_branch(repo.path / p.dir))
                 for p in repo.parts or [config.Part("", repo.name)]]
    pipeline.remove_workspace(ws, checkouts)
    for c in checkouts:
        with record.timed(run.id, "clone", repo=c.github, base=c.base, branch=branch, path=str(c.wt)) as clone:
            pipeline.fetch(c.src, c.base)
            c.lease = pipeline.remote_head(c.src, branch)
            c.resumes = bool(c.lease)
            if c.resumes:
                pipeline.fetch(c.src, branch)
            pipeline.add_worktree(c, branch, f"origin/{branch}" if c.resumes else c.origin_base)
            clone["resumed"] = c.resumes
    return checkouts


@factory.step("prepare")
def prepare(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    cfg, run = job.cfg, job.run
    repo, tk = run.repo, run.tracker
    try:
        t = tk.get(run.tid)
    except tracker.ERRORS as e:
        return end_as("stop", "quiet", status="failed", summary=f"could not read {run.ref}: {e}")
    opened(run, t)
    if run.feedback:
        if closed := [url for url in run.feedback_prs if gh.pr_view(url).get("state") != "OPEN"]:
            return end_as("stop", "quiet", status="noop",
                          summary=f"the PR closed before arete could revise it: {', '.join(closed)}")
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
    branch = pipeline.open_branch(run) or pipeline.fill_template(main_layer.branch, config.RepoLayer.branch,
                                                                 kind=t.kind, id=t.id, slug=pipeline.slug(t.title))
    if not run.dry_run:
        tk.prepare()
    if not run.alone:
        run.enter("running")
    if repo.parts and not run.dry_run:
        checkouts = pipeline.make_workspace(run.id, repo, main_layer.base_branch, branch, run.tid)[1]
    else:
        checkouts = local_workspace(run, main_layer.base_branch, branch, job.ws)
    for c in checkouts:
        run.log(f"worktree {c.wt} on {branch}, {c.starting_point}")
    for c in pipeline.merge_moved_bases(run, [c for c in checkouts if c.resumes]):
        c.merged_base = True
    layer = pipeline.resolve_layer(cfg, run, job.ws)
    for rel in layer.copy:
        if (src := repo.path / rel).exists():
            (dest := job.ws / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    for cmd in layer.setup:
        with record.timed(run.id, "setup", command=cmd) as setup:
            res = environment.sh(cmd, cfg, repo, cwd=job.ws, timeout=1200)
            setup["exit"] = res.returncode
        run.log(f"setup `{cmd}`: exit {res.returncode}")
        if res.returncode != 0:
            run.save("setup.log", res.stdout + res.stderr)
            return end_as("fail", "failed", why=f"setup command `{cmd}` failed")
    approval = state.triage_approval(t.ref if repo.tracker == "jira" else tracker.ticket_name(repo.name, t.ref))
    return Outcome("pass", data={
        "ticket": {k: getattr(t, k) for k in TICKET_FIELDS}, "branch": branch,
        "checkouts": [{**asdict(c), "src": str(c.src), "wt": str(c.wt)} for c in checkouts],
        "layer": str(layer.dir), "add_dirs": [str(run.dir / "images")] if images.paths else [],
        "attachments": pipeline.images_note(images), "triage": pipeline.triage_notes(approval), "approval": approval,
        "expertise": layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"})


@factory.step("plan")
def plan(flow: factory.Flow) -> Outcome:
    job: Job = flow.ctx
    run, t, layer = job.run, job.ticket, job.layer
    limit, info = layer.max_diff_lines, run.tracker.label_name("needs-info")
    reads = pipeline.git_reads(job.checkouts)
    res = run.agent("plan", pipeline.prompt("plan", repo=run.repo.name, workspace=job.workspace, ref=t.ref,
                                            title=t.title, body=t.body, comments=t.comments,
                                            attachments=job.attachments, triage=job.triage, expertise=job.expertise,
                                            commands=pipeline.code_list(job.builder_bash),
                                            denied=pipeline.code_list(diffguard.denied_paths(layer)),
                                            max_lines=limit, checks=pipeline.check_list(layer)),
                    job.ws, role="plan", allowed_tools=pipeline.bash_rules(config.DEFAULT_ALLOW_BASH + reads),
                    json_schema=pipeline.PLAN_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        return end_as("fail", "failed", why="planning step failed")
    found = res.structured
    plan_text = found.get("plan", "")
    run.save("plan.md", plan_text)
    estimate = int(found.get("estimated_lines") or 0)
    if found.get("ambiguous") and found.get("question"):
        run.log("plan: ambiguous, asking the author")
        return end_as("ask", "ask", summary=found["question"],
                      comment=f"Before I build this I need one answer:\n\n{found['question']}\n\n"
                              f"Reply here, then remove the `{info}` label and I will pick it up.")
    if estimate > limit:
        split = [s for s in found.get("split") or [] if isinstance(s, dict) and s.get("title")]
        run.log(f"plan: about {estimate} lines, over the {limit} line limit; proposing a {len(split)} part split")
        run.save("split.json", split)
        return end_as("ask", "ask", summary=f"plan estimates {estimate} lines, over the {limit} line limit; split proposed",
                      comment=pipeline.split_comment(estimate, limit, split, run.tracker.queue_hint, info,
                                                     layer.config_file))
    return Outcome("pass", data={
        "plan": plan_text, "estimate": estimate,
        "goal": found.get("verify", "").strip() or "The planner gave no steps: check what the issue asks for.",
        "left_for_a_person": pipeline.left_section(run, (found.get("left_for_a_person") or "").strip()),
        "assumptions": pipeline.assumptions_of(found)})


@factory.step("revise")
def revision(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    run, t, layer = job.run, job.ticket, job.layer
    feedback = pipeline.feedback_markdown(run.feedback)
    left = pipeline.left_on_prs(run.feedback_prs)
    run.story.append(f"The owner reviewed the PR and left this feedback:\n\n{feedback}")
    res = run.agent("revise", pipeline.prompt("revise", repo=run.repo.name, workspace=job.workspace, ref=t.ref,
                                              title=t.title, body=t.body, comments=t.comments,
                                              attachments=job.attachments, feedback=feedback, expertise=job.expertise,
                                              checks=pipeline.check_list(layer),
                                              commands=pipeline.code_list(job.builder_bash),
                                              max_lines=layer.max_diff_lines),
                    job.ws, role=BUILD, allowed_tools=pipeline.bash_rules(job.builder_bash),
                    disallowed_tools=runner.NO_GIT_WRITES, json_schema=pipeline.REVISE_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        return end_as("fail", "failed", why="revise step failed", said=res.text)
    summary, replies = res.structured.get("summary", ""), res.structured.get("replies") or []
    if not pipeline.stage(job.checkouts, diffguard.denied_paths(layer)).files:
        return end_as("stop", "unchanged", summary=summary, replies=replies)
    return Outcome("pass", data={
        "plan": f"Revise the open PR for the owner's feedback:\n\n{feedback}", "left_for_a_person": left,
        "goal": ("Check each change the owner's feedback asks for, and that what the issue asks for still works.\n\n"
                 + feedback),
        "summary": summary, "replies": replies, "story": run.story}, session=run.sessions.get(BUILD, NO_SESSION))


@factory.step("implement")
def implement(flow: factory.Flow, tree: str, round: str, feedback: str = "", attempt: int = 1,
              tries: int = 1, asks: str = "") -> Outcome:
    job: Job = flow.ctx
    run = job.run
    if asks:
        res = job.b.resume(round, asks, json_schema=pipeline.CI_FIX_SCHEMA)
        return Outcome("pass" if res.ok and isinstance(res.structured, dict) else "fail",
                       data={"round": round, "failures": feedback, "verdicts": res.structured, "said": res.text},
                       session=run.sessions.get(BUILD, NO_SESSION))
    if round == REVIEW_FIX:
        res = job.b.resume(REVIEW_FIX, pipeline.prompt(REVIEW_FIX, problems=feedback))
    elif round != BUILD:
        res = job.b.resume(round, pipeline.prompt("fix", attempt=attempt, max_attempts=tries, failures=feedback))
    else:
        return build(job)
    return Outcome("pass" if res.ok else "fail", data={"round": round},
                   session=run.sessions.get(BUILD, NO_SESSION))


def build(job: Job) -> Outcome:
    run, t, layer = job.run, job.ticket, job.layer
    res = run.agent(BUILD, pipeline.prompt(BUILD, repo=run.repo.name, workspace=job.workspace, ref=t.ref,
                                           title=t.title, body=t.body, comments=t.comments,
                                           attachments=job.attachments, plan=job.plan_text, expertise=job.expertise,
                                           checks=pipeline.check_list(layer),
                                           commands=pipeline.code_list(job.builder_bash),
                                           max_lines=layer.max_diff_lines, estimate=job.estimate, verify=job.goal),
                    job.ws, role=BUILD, allowed_tools=pipeline.bash_rules(job.builder_bash),
                    disallowed_tools=runner.NO_GIT_WRITES)
    if not res.ok:
        return end_as("fail", "failed", why="build step failed", said=res.text)
    if not pipeline.stage(job.checkouts, diffguard.denied_paths(layer)).files:
        if job.estimate == 0:
            return end_as("stop", "nothing", summary=res.text)
        return end_as("fail", "failed", said=res.text,
                      why=f"the builder stopped without changing anything, though the plan estimated "
                          f"{util.count(job.estimate, 'line')}")
    return Outcome("pass", data={"round": BUILD, "summary": res.text},
                   session=run.sessions.get(BUILD, NO_SESSION))


@factory.step("checks")
def checks(flow: factory.Flow, tree: str, attempt: int = 1, tries: int = 1, after: str = "") -> Outcome:
    job: Job = flow.ctx
    run, b = job.run, job.b
    ok = b.check()
    if not after:
        run.save("checks.md", b.report)
        record.note(run.id, attempts=attempt)
    if not ok:
        if not after:
            run.story.append(f"Checks failed on attempt {attempt}:\n{util.tail(b.report, 2500)}" if attempt < tries
                             else "Checks still failing after the last attempt.")
        return Outcome("fail", next="implement", feedback=b.report,
                       data={"report": b.report, "attempts": attempt, "after": after, "story": run.story})
    planned = f" (the plan estimated {job.estimate})" if job.estimate and not after else ""
    b.require_valid_diff(after or "the build and its fixes", planned)
    return Outcome("pass", data={"report": b.report, "attempts": attempt, "after": after, "story": run.story})


@factory.step("verify")
def verify(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    b = job.staged()
    verified = pipeline.verify_in_use(b, job.with_left(job.goal), job.build_summary)
    return Outcome("pass", data={"verified": asdict(verified), "names": dict(b.steps), "story": job.run.story})


@factory.step("review")
def review(flow: factory.Flow, second: bool = False) -> Outcome:
    job: Job = flow.ctx
    run = job.run
    said = pipeline._review(job.staged(), job.with_left(job.plan_text), job.verified,
                            "review2" if second else "review")
    if second or said.get("verdict") != "request_changes":
        return Outcome("pass", data={"review": said, "story": run.story})
    problems = "\n".join(f"- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in said.get("problems", []))
    run.story.append(f"Review requested changes:\n{problems}")
    return Outcome("fail", next="implement", feedback=problems, data={"review": said, "story": run.story})


@factory.step("learn")
def learn(flow: factory.Flow, tree: str, apply: bool) -> Outcome:
    job: Job = flow.ctx
    pipeline._learn(job.run, job.ws, job.layer.expertise_file, job.ticket.title, job.expertise, apply=apply)
    return Outcome("pass", data={"applied": apply})


def change(job: Job) -> tuple[pipeline.Staged, str, str]:
    staged = job.b.restage()
    if not staged.files:
        raise pipeline.RunFailed("nothing was left to push, since the workspace holds no change")
    size = f"{len(staged.files)} files, {staged.lines} lines"
    return staged, size, f"{size}, review {job.review.get('verdict')}, in use {job.verified.verdict}"


@factory.step("pr")
def pr(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    b, run, t, tk = job.b, job.run, job.ticket, job.run.tracker
    staged, size, outcome = change(job)
    subject = pipeline.fill_template(job.layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    if not run.repo.parts:
        b.push(job.branch, f"{tracker.with_ref(subject, t.ref)}\n\nArete-Run: {run.id}")
        record.note(run.id, branch=job.branch)
        return Outcome("pass", data={"summary": outcome, "prs": {}, "opened": [], "pushed": [], "draft_why": "",
                                     "changed": [c.github for c in staged.changed], "report_body": "",
                                     "committed_lines": b.committed_lines})
    draft_why = pipeline.draft_reason(job.review.get("verdict") == "approve", job.verified)
    review, problems, left = job.review, job.review.get("problems", []), job.left_for_a_person
    checks = pipeline.check_list(job.layer)
    images = pipeline.evidence_images(run, staged.changed[0], job.verified)
    report_body = (
        f"## Summary\n\n{job.build_summary.strip()[:3000]}\n\n"
        + ("" if run.feedback else pipeline.triage_block(job.approval))
        + pipeline.left_block(left)
        + f"{pipeline.CHECKED_IN_USE_HEADING}\n\n{job.verified.markdown(images)}\n\n"
        f"## Review\n\n**{review.get('verdict', 'none')}**: {review.get('summary', '')}\n"
        + ("".join(f"\n- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in problems) if problems else "")
        + f"\n\n## Checks\n\nAll passed after {util.count(job.attempts, 'attempt')}: {checks}\n\n"
        + ("" if run.feedback else f"<details><summary>Plan</summary>\n\n{job.plan_text[:6000]}\n\n</details>\n\n")
        + f"Run `{run.id}`, {size}, {util.paid_note(record.metered_spent(run.id))}."
    )
    revised = "Revised for the review feedback on the PR.\n\n" if run.feedback else ""
    link = tk.pr_link(t, whole=not left)
    b.push(job.branch, f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}")
    opened: list[pipeline.Checkout] = []
    for c in staged.changed:
        if not c.pr:
            c.pr = gh.pr_create(c.github, job.branch, c.base, tk.pr_title(subject, t), f"{link}\n\n{report_body}",
                                labels=tk.pr_labels, draft=bool(draft_why), cwd=c.wt)
            opened.append(c)
            run.log(f"opened {c.pr}{' as draft' if draft_why else ''}")
        else:
            run.log(f"pushed to {c.pr}")
    pushed = [c for c in staged.changed if c not in opened]
    with_prs = job.with_prs
    for c in with_prs:
        others = pipeline.sibling_section(t.ref, c, with_prs)
        if c in opened:
            if others:
                gh.pr_edit_body(c.pr, f"{link}\n\n{report_body}{others}")
            continue
        body = gh.pr_body(c.pr)
        new_body = pipeline.synced_pr_body(run, c.pr, body, t, left)
        if opened:
            new_body = pipeline.with_siblings(new_body, others)
        if new_body != body:
            gh.pr_edit_body(c.pr, new_body)
        if c in pushed and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Pushed more work for {t.ref}.\n\n{report_body}")
    return Outcome("pass", data={
        "summary": outcome, "prs": {c.github: c.pr for c in with_prs}, "opened": [c.github for c in opened],
        "pushed": [c.github for c in pushed], "changed": [c.github for c in staged.changed], "draft_why": draft_why,
        "report_body": report_body, "committed_lines": b.committed_lines})


@factory.step("ci")
def watch(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    run = job.run

    def fix(name: str, text: str, failures: str) -> Result:
        done = step(flow, "implement", tree=tree, round=name, feedback=failures, asks=text)
        return Result(done.passed, done.data.get("said", ""), structured=done.data.get("verdicts"))

    try:
        result = pipeline.watch_ci(job.b, job.slot, job.branch, job.picked(job.changed), job.with_prs, job.draft_why,
                                   fix=fix)
    except pipeline.RunFailed as e:
        if run.feedback:
            raise
        prs = "\n".join(f"- [{c.pr_name}]({c.pr})" for c in job.with_prs)
        return end_as("fail", "failed", why=e.why, prs=True,
                      detail=f"{e.detail}\n\n### PRs\n\n{prs}\n\nReview comments on a PR start a revision, as on any "
                             "arete PR.")
    return Outcome("pass", data={"summary": result})


@factory.step("publish")
def publish(flow: factory.Flow) -> Outcome:
    job: Job = flow.ctx
    run = job.run
    if not run.repo.parts:
        run.enter("done")
        return end_as("pass", "done", summary=f"pushed {job.branch} to origin: {job.summary}")
    opened, pushed, with_prs = job.picked(job.opened), job.picked(job.pushed), job.with_prs
    run.enter("pr")
    pr_lines = "\n".join(f"- {'Opened' if c in opened else 'Pushed to' if c in pushed else 'Unchanged'} "
                         f"[{c.pr_name}]({c.pr})" for c in with_prs)
    if run.feedback:
        pipeline.answer_feedback(run, job.replies, "Done in the latest push.")
        run.say(f"Revised for your review.\n\n{job.report_body}\n\nCI: {job.ci_result}.")
    else:
        run.say(f"PRs for this ticket:\n\n{pr_lines}\n\nCI: {job.ci_result}."
                + ("" if not opened else pipeline.draft_note(job.draft_why, job.verified)))
    automerge(job, opened)
    return end_as("pass", "pr", summary=f"{job.summary}, CI: {job.ci_result}")


def automerge(job: Job, opened: list[pipeline.Checkout]) -> None:
    run, t, cfg = job.run, job.ticket, job.cfg
    if job.draft_why or job.layer.trust_level != "automerge":
        return
    if job.approval and job.approval["sent_by"] == "triage":
        run.log("automerge left off: triage approved the ticket, so a person merges it")
        return
    try:
        risk = jev.risk(pipeline.patch_from_base(opened), f"{t.title}\n\n{job.build_summary.strip()[:3000]}", cfg,
                        trace=(run.id, "automerge risk jev")) if opened and jev.ready(cfg, [run.repo]) else None
        hold = risk.hold if risk else ""
    except jev.Unjudged as e:
        run.log(f"automerge risk: JEV could not judge the diff: {e}")
        hold = "JEV could not judge the diff"
    for c in opened:
        try:
            if hold:
                run.log(f"automerge left off on {c.pr}: {hold}")
                gh.pr_comment(c.pr, f"Automerge stays off for this PR: {hold}. A person merges it.")
                continue
            try:
                did, why = gh.pr_merge_auto(c.github, gh.pr_number(c.pr))
            except gh.GhError as e:
                did, why = "left", f"a GitHub call failed: {str(e).rstrip('.')}"
            if did == "merged":
                run.log(f"automerged {c.pr}")
            elif did == "auto":
                run.log(f"automerge enabled on {c.pr}")
            else:
                run.log(f"automerge left {c.pr} for a person: {why}")
                gh.pr_comment(c.pr, f"Automerge could not merge this PR: {why}. A person merges it.")
        except gh.GhError as e:
            run.log(f"automerge failed on {c.pr}: {e}")


@factory.step("respond")
def respond(flow: factory.Flow, ending: str, why: str = "", said: str = "", detail: str = "", comment: str = "",
            summary: str = "", status: str = "", replies: list[dict] | None = None, prs: bool = False) -> Outcome:
    job: Job = flow.ctx
    run, pr_url = job.run, ""
    if ending == "failed":
        status, summary, _ = pipeline._fail(run, why, said, detail)
        pr_url = pipeline.record_prs(run, job.branch, job.with_prs) if prs else ""
    elif ending == "ask":
        status, summary, _ = pipeline._ask(run, job.ws, job.checkouts, comment, summary)
    elif ending == "nothing":
        status, summary, pr_url = pipeline.nothing_to_build(run, job.ticket, job.ws, job.checkouts, job.branch,
                                                            summary, job.left_for_a_person)
    elif ending == "unchanged":
        status, summary, pr_url = pipeline.answer_without_changes(run, job.ws, job.checkouts, job.branch, summary,
                                                                  replies or [])
    elif ending == "pr":
        pr_url = pipeline.record_prs(run, job.branch, job.with_prs)
        pipeline.remove_workspace(job.ws, job.checkouts)
    elif ending == "done":
        run.say(done_comment(job, summary))
        pipeline.remove_workspace(job.ws, job.checkouts)
    elif ending == "dry-run":
        run.log(f"dry run: {summary}; worktree kept at {job.ws}")
    return Outcome("pass", data={"status": status or ending, "summary": summary, "pr_url": pr_url})


def done_comment(job: Job, summary: str) -> str:
    said = job.review
    problems = "".join(f"\n- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in said.get("problems", []))
    return (f"Done: {summary}.\n\n## Summary\n\n{job.build_summary.strip()[:3000]}\n\n"
            f"{pipeline.CHECKED_IN_USE_HEADING}\n\n{job.verified.markdown()}\n\n"
            f"## Review\n\n**{said.get('verdict', 'none')}**: {said.get('summary', '')}\n{problems}\n\n"
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
    return finish(flow, **outcome.data)


def end_dry_run(flow: factory.Flow) -> tuple[str, str, str]:
    staged, _, outcome_text = change(flow.ctx)
    flow.ctx.run.save("diff.patch", staged.patch)
    return finish(flow, "dry-run", summary=outcome_text)


def driven(flow: factory.Flow, drive: Callable[[factory.Flow], tuple[str, str, str]]) -> tuple[str, str, str]:
    try:
        return drive(flow)
    except pipeline.RunFailed as e:
        return finish(flow, "failed", why=e.why, detail=e.detail)


def alone(flow: factory.Flow) -> tuple[str, str, bool]:
    found = [line for line in factory.ends(lines.read(flow.run_id)) if line.get("step") == flow.only]
    if not found:
        return "failed", f"the script never ran {flow.only}", False
    said = util.first_line(str(found[-1].get("error") or found[-1].get("feedback") or ""), 300)
    return "step", f"{flow.only} {found[-1]['result']}" + (f": {said}" if said else ""), found[-1]["result"] == "pass"
