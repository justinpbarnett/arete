from __future__ import annotations

import shutil
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, diffguard, factory, lines, paths, pipeline, record, runner, state, tracker, util
from .agent import NO_SESSION
from .factory import Outcome
from .verify import Verified

ADW = "adw"
TICKET_FIELDS = ("id", "ref", "title", "body", "comments", "kind", "url")
BUILD = "build"
REVIEW_FIX = "review_fix"


def wanted(repo: config.Repo) -> str:
    return config.load_layer(repo.layer_dir).factory_path


def has_origin(repo: config.Repo) -> bool:
    return repo.path.is_dir() and "origin" in util.git(["remote"], repo.path, check=False).split()


def ran_on_seam(run_id: str) -> bool:
    return any(line.get("event") == "route" or (line.get("event") == "step_start" and line.get("step") == "prepare")
               for line in lines.read(run_id))


def start(cfg: config.Config, run: pipeline.Run, earlier: dict) -> factory.Flow:
    ws = paths.WORKTREES_DIR / f"{run.repo.name}-{run.tid}"
    continues = earlier["id"] if earlier.get("status") == "interrupted" and ran_on_seam(earlier["id"]) else ""
    return factory.Flow(run.id, continues=continues, ctx=Job(cfg, run, ws))


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

    def with_left(self, text: str) -> str:
        if not self.left_for_a_person:
            return text
        return f"{text}\n\n{pipeline.NOT_CHECKED_LEFT_FOR_A_PERSON}{self.left_for_a_person}"

    def take(self, name: str, outcome: Outcome, stored: bool) -> None:
        data, run = outcome.data, self.run
        if "story" in data:
            run.story = list(data["story"])
        if name == "prepare" and outcome.passed:
            self.prepared(data, stored)
        elif name == "plan" and outcome.passed:
            self.plan_text, self.goal, self.estimate = data["plan"], data["goal"], data["estimate"]
            self.left_for_a_person = data["left_for_a_person"]
        elif name == "implement":
            self.build_summary = data.get("summary", self.build_summary)
            if outcome.session.id:
                run.sessions[BUILD] = outcome.session
        elif name == "checks" and "report" in data:
            self.b.report = data["report"]
        elif name == "verify" and outcome.passed:
            self.verified = Verified.stored(data["verified"])
            self.b.steps = Counter(data["names"])
        elif name == "review" and "review" in data:
            self.review = data["review"]

    def prepared(self, data: dict, stored: bool) -> None:
        run = self.run
        self.ticket = tracker.Ticket(**data["ticket"])
        self.branch, self.attachments, self.triage = data["branch"], data["attachments"], data["triage"]
        self.expertise = data["expertise"]
        self.checkouts = [pipeline.Checkout(**{**c, "src": Path(c["src"]), "wt": Path(c["wt"])})
                          for c in data["checkouts"]]
        self.layer = pipeline.checked_layer(self.cfg, Path(data["layer"]))
        run.cfg, run.layer, run.kind = self.cfg, self.layer, self.ticket.kind
        run.add_dirs += [d for d in map(Path, data["add_dirs"]) if d not in run.add_dirs]
        if stored:
            opened(run, self.ticket)
            run.enter("running")


def opened(run: pipeline.Run, t: tracker.Ticket) -> None:
    lines.write(run.id, "task", ticket=t.ref, kind=t.kind, repo=run.repo.name, title=t.title)
    if run.answering:
        lines.write(run.id, "answer", asked_in=run.answering["id"], question=run.answering.get("summary") or "")


def end_as(result: str, kind: str, **fields) -> Outcome:
    return Outcome(result, feedback=fields.get("why") or fields.get("summary") or "", data={"ending": kind, **fields})


def local_workspace(run: pipeline.Run, base: str, branch: str, ws: Path) -> list[pipeline.Checkout]:
    repo = run.repo
    c = pipeline.Checkout("", repo.name, repo.path, ws, base or util.origin_default_branch(repo.path))
    pipeline.remove_workspace(ws, [c])
    with record.timed(run.id, "clone", repo=repo.name, base=c.base, branch=branch, path=str(ws)) as clone:
        pipeline.fetch(c.src, c.base)
        c.lease = pipeline.remote_head(c.src, branch)
        c.resumes = bool(c.lease)
        if c.resumes:
            pipeline.fetch(c.src, branch)
        pipeline.add_worktree(c, branch, f"origin/{branch}" if c.resumes else c.origin_base)
        clone["resumed"] = c.resumes
    return [c]


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
    if not t.open:
        return end_as("stop", "quiet", status="noop", summary="ticket is not open")
    if t.blocker and not run.force:
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
    run.enter("running")
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
            res = util.sh(cmd, cwd=job.ws, timeout=1200)
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
        "attachments": pipeline.images_note(images), "triage": pipeline.triage_notes(approval),
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
        "left_for_a_person": pipeline.left_section(run, (found.get("left_for_a_person") or "").strip())})


@factory.step("implement")
def implement(flow: factory.Flow, tree: str, round: str, feedback: str = "", attempt: int = 1,
              tries: int = 1) -> Outcome:
    job: Job = flow.ctx
    run = job.run
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
                       data={"report": b.report, "attempts": attempt, "story": run.story})
    planned = f" (the plan estimated {job.estimate})" if job.estimate and not after else ""
    b.require_valid_diff(after or "the build and its fixes", planned)
    return Outcome("pass", data={"report": b.report, "attempts": attempt, "story": run.story})


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


@factory.step("publish")
def publish(flow: factory.Flow) -> Outcome:
    job: Job = flow.ctx
    b, run, t = job.b, job.run, job.ticket
    staged = b.restage()
    if not staged.files:
        raise pipeline.RunFailed("nothing was left to push, since the workspace holds no change")
    outcome = (f"{len(staged.files)} files, {staged.lines} lines, review {job.review.get('verdict')}, "
               f"in use {job.verified.verdict}")
    if run.dry_run:
        run.save("diff.patch", staged.patch)
        return end_as("pass", "dry-run", summary=outcome)
    subject = pipeline.fill_template(job.layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    b.push(job.branch, f"{tracker.with_ref(subject, t.ref)}\n\nArete-Run: {run.id}")
    record.note(run.id, branch=job.branch)
    run.enter("done")
    return end_as("pass", "done", summary=f"pushed {job.branch} to origin: {outcome}")


@factory.step("respond")
def respond(flow: factory.Flow, ending: str, why: str = "", said: str = "", detail: str = "", comment: str = "",
            summary: str = "", status: str = "") -> Outcome:
    job: Job = flow.ctx
    run = job.run
    if ending == "failed":
        status, summary, _ = pipeline._fail(run, why, said, detail)
    elif ending == "ask":
        status, summary, _ = pipeline._ask(run, job.ws, job.checkouts, comment, summary)
    elif ending == "nothing":
        status, summary, _ = pipeline.nothing_to_build(run, job.ticket, job.ws, job.checkouts, job.branch, summary,
                                                       job.left_for_a_person)
    elif ending == "done":
        run.say(done_comment(job, summary))
        pipeline.remove_workspace(job.ws, job.checkouts)
    elif ending == "dry-run":
        run.log(f"dry run: {summary}; worktree kept at {job.ws}")
    return Outcome("pass", data={"status": status or ending, "summary": summary, "pr_url": ""})


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


def feature(flow: factory.Flow) -> tuple[str, str, str]:
    try:
        return drive(flow, flow.ctx)
    except pipeline.RunFailed as e:
        return finish(flow, "failed", why=e.why, detail=e.detail)


def drive(flow: factory.Flow, job: Job) -> tuple[str, str, str]:
    ws = str(job.ws)
    for name, kw in (("prepare", {"tree": ws}), ("plan", {}), ("implement", {"tree": ws, "round": BUILD})):
        if not (outcome := step(flow, name, **kw)).passed:
            return ended(flow, outcome)
    tries = job.run.settings(BUILD).tries
    attempt, outcome = 1, step(flow, "checks", tree=ws, attempt=1, tries=tries)
    while not outcome.passed and attempt < tries:
        attempt += 1
        step(flow, "implement", tree=ws, round=f"fix{attempt}", feedback=outcome.feedback, attempt=attempt,
             tries=tries)
        outcome = step(flow, "checks", tree=ws, attempt=attempt, tries=tries)
    if not outcome.passed:
        step(flow, "learn", tree=ws, apply=False)
        return finish(flow, "failed", why=f"checks still failing after {attempt} attempts")
    step(flow, "verify", tree=ws)
    if not (outcome := step(flow, "review")).passed:
        step(flow, "implement", tree=ws, round=REVIEW_FIX, feedback=outcome.feedback)
        if not (outcome := step(flow, "checks", tree=ws, after="the review fixes")).passed:
            return finish(flow, "failed", why="the checks failed after the review fixes", detail=outcome.feedback)
        step(flow, "verify", tree=ws)
        step(flow, "review", second=True)
    step(flow, "learn", tree=ws, apply=True)
    return ended(flow, step(flow, "publish"))
