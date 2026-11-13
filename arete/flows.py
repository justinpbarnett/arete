from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import (
    asks,
    config,
    diffguard,
    document,
    factory,
    gh,
    lines,
    outbound,
    paths,
    pipeline,
    record,
    repairs,
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
CHECK_REPAIR = "check_repair"
REVIEW_REPAIR = "review_repair"
CI_REPAIR = "ci_repair"
ROLE_OF = {"plan": "plan", "implement": BUILD, "revise": BUILD, "repair": BUILD, "verify": "verify", "review": "review", "learn": "learn",
           "document": "document"}
SETTABLE = {**ROLE_OF, CHECK_REPAIR: CHECK_REPAIR, REVIEW_REPAIR: REVIEW_REPAIR, CI_REPAIR: CI_REPAIR}


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
    job = Job(cfg, run, ws, slot=slot, continues=continues, only=only)
    refusal = guard(job, continues) if continues else ""
    if refusal:
        job.note_task()
    elif continues and not only and not job.pending and (moved := base_moved(continues)):
        run.log(f"{moved} since run {continues} prepared, so this run prepares again and redoes every step after it")
        again = "prepare"
    return factory.Flow(run.id, continues=continues, only=only, ctx=job, labels=job.labels, again=again,
                        restore=lambda line: restore(job, line, only), refusal=refusal, hold=job.hold)


def pending_refusal(job: Job) -> str:
    source = job.continues if job.only else job.run.id
    step = " --step pr" if factory.started(job.continues, "pr") else ""
    return (f"{', '.join(job.pending)} holds commits of this run's chain that are not on origin, and preparing again "
            "would drop them, so arete ran no model, reset nothing and left the commits on the branch and in the "
            f"worktree; `arete resume {source}{step}` publishes them")


def guard(job: Job, continues: str) -> str:
    data = prepared_data(continues)
    branch, listed = str(data.get("branch") or ""), data.get("checkouts")
    if not continues or not branch or not isinstance(listed, list):
        return ""
    job.branch = branch
    try:
        checkouts = [pipeline.Checkout(**{**e, "src": Path(e["src"]), "wt": Path(e["wt"])}) for e in listed]
        layer = pipeline.checked_layer(job.cfg, Path(data["layer"]))
        if why := pipeline.kept_work(job.ws, checkouts, job.run.repo.path, layer.copy, commits=False):
            return f"arete started no step and made no tracker call: {why}"
    except (pipeline.RunFailed, RuntimeError, OSError, KeyError, TypeError) as e:
        return f"arete started no step and made no tracker call: could not check the worktree: {e}"
    runs = chain_runs(continues)
    for c in checkouts:
        if why := job.reclaim(c, *authority(continues, c, c.lease), runs, attached=c.wt.is_dir()):
            return why
    return ""


def prepared_data(continues: str) -> dict:
    prepared = [line for line in factory.history(factory.chain(continues))
                if line.get("step") == "prepare" and line.get("result") == "pass"]
    data = prepared[-1].get("data") if prepared else None
    return data if isinstance(data, dict) else {}


def base_moved(continues: str) -> str:
    for c in prepared_data(continues).get("checkouts") or []:
        if not isinstance(c, dict) or not c.get("base_commit"):
            continue
        try:
            now = pipeline.remote_head(Path(c["src"]), c["base"])
        except RuntimeError:
            continue
        if now and now != c["base_commit"]:
            return f"`origin/{c['base']}` moved from {c['base_commit'][:12]} to {now[:12]} in {c['github']}"
    return ""


def chain_runs(run_id: str) -> set[str]:
    return {str(read[0].get("run") or "") for read in factory.chain(run_id) if read} - {""}


def publication(run_id: str) -> dict:
    for read in factory.chain(run_id):
        for line in reversed(factory.ends(read)):
            data = line.get("data")
            if line.get("step") == "pr" and line.get("result") == "pass" and isinstance(data, dict):
                return data
    return {}


def authority(run_id: str, c: pipeline.Checkout, lease: str) -> tuple[str, bool]:
    pub = publication(run_id)
    heads = pub.get("heads") if isinstance(pub.get("heads"), dict) else {}
    changed = pub.get("changed") if isinstance(pub.get("changed"), list) else []
    return str(heads.get(c.github) or lease), c.github in changed


def foreign(wt: Path, tip: str, bases: list[str], runs: set[str]) -> list[str]:
    out = util.git(["log", "--first-parent", f"--format={repairs.TRAILER}", tip, *(f"^{b}" for b in bases if b)], wt)
    found = []
    for row in out.splitlines():
        sha, _, trailers = row.partition("\t")
        if sha and not runs & {v.strip() for v in trailers.split(",")}:
            found.append(sha)
    return found


def ancestor(wt: Path, a: str, b: str) -> bool:
    return util.sh(["git", "merge-base", "--is-ancestor", a, b], cwd=wt).returncode == 0


def remade(line: dict) -> tuple[str, list[pipeline.Checkout], str]:
    data = line.get("data") if isinstance(line.get("data"), dict) else {}
    branch, listed = str(data.get("branch") or ""), data.get("checkouts")
    if line.get("step") != "prepare" or not branch or not isinstance(listed, list):
        return branch, [], "only the folder of a prepare step can be made again"
    made = [pipeline.Checkout(**{**c, "src": Path(c["src"]), "wt": Path(c["wt"])}) for c in listed]
    try:
        for c in made:
            c.lease = pipeline.remote_head(c.src, branch)
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


def restore(job: Job, line: dict, only: str = "") -> str:
    why = rebuild(job, line)
    if why and not only and line.get("step") == "prepare":
        raise pipeline.RunFailed(why)
    return why


def rebuild(job: Job, line: dict) -> str:
    branch, made, why = remade(line)
    if why:
        return why
    job.branch = branch
    leases = {str(e.get("github")): str(e.get("lease") or "") for e in line["data"]["checkouts"]}
    runs = chain_runs(job.continues)
    for c in made:
        if why := job.reclaim(c, *authority(job.continues, c, leases.get(c.github, "")), runs, attached=False):
            return why
    ws = Path(line["tree"])
    layer = pipeline.checked_layer(job.cfg, Path(line["data"]["layer"]))
    try:
        pipeline.clear_workspace(ws, made, job.run.repo.path, layer.copy)
    except pipeline.RunFailed as e:
        return e.why
    job.run.workspace = ws, made
    try:
        for c in made:
            if c.lease:
                pipeline.fetch(c.src, branch)
            local = util.git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], c.src, check=False).strip()
            pipeline.add_worktree(c, branch, local or (f"origin/{branch}" if c.lease else c.revision))
            job.restored[str(c.wt)] = c.lease
    except RuntimeError as e:
        return f"arete could not make it again from origin/{branch}: {util.first_line(str(e), 300)}"
    job.run.log(f"made {ws} again from origin/{branch}")
    return pipeline.furnish(job.cfg, job.run, layer, ws)


def facts(name: str, data: dict) -> dict:
    if name in ("plan", "revise", "repair"):
        return {"assumptions": pipeline.assumptions_of(data), "left_for_a_person": str(data.get("left_for_a_person") or "")}
    if name == "pr":
        return {"held": data.get("draft_why") or ""}
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
    reviews: int = 0
    built: pipeline.Build | None = None
    slot: pipeline.RunSlot | None = None
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

    @property
    def with_prs(self) -> list[pipeline.Checkout]:
        return [c for c in self.checkouts if c.pr]

    def picked(self, names: list[str]) -> list[pipeline.Checkout]:
        return [c for c in self.checkouts if c.github in names]

    @property
    def builder_bash(self) -> list[str]:
        return self.layer.builder_bash + pipeline.git_reads(self.checkouts) + self.check_rules

    @property
    def commands(self) -> str:
        return pipeline.code_list(self.layer.builder_bash + pipeline.git_reads(self.checkouts) + self.check_commands)

    @property
    def checks(self) -> str:
        return pipeline.check_calls(self.check_commands)

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
                expertise=self.expertise, left_for_a_person=self.left_for_a_person, check_commands=self.checks,
                from_base=self.run.alone or bool(self.run.repairs) or self.carried, plan_text=self.plan_text, build_summary=self.build_summary)
        return self.built

    def staged(self) -> pipeline.Build:
        if self.b.staged.empty:
            self.b.restage()
        return self.b

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
        if outcome.passed or name == "review":
            run.facts.update(facts(name, data))
        if "story" in data:
            run.story = list(data["story"])
        if name == "prepare" and outcome.passed:
            self.prepared(data, stored)
        elif name == "plan" and outcome.passed:
            self.plan_text, self.goal, self.estimate = data["plan"], data["goal"], data["estimate"]
            self.left_for_a_person = data["left_for_a_person"]
        elif name in ("revise", "repair") and outcome.passed:
            self.plan_text, self.goal, self.estimate = data["plan"], data["goal"], 0
            self.left_for_a_person, self.build_summary, self.replies = (data["left_for_a_person"], data["summary"],
                                                                        data.get("replies", []))
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
            self.review, self.reviews = data["review"], data.get("attempt", 0)
        elif name == "document" and outcome.passed:
            self.document = data.get("text", "")
        elif name == "pr" and outcome.passed:
            self.published(data, stored)
        elif name == "ci" and outcome.passed:
            self.ci_result = data["summary"]

    def published(self, data: dict, stored: bool) -> None:
        for c in self.checkouts:
            c.pr = data["prs"].get(c.github, c.pr)
        self.opened, self.pushed, self.changed = data["opened"], data["pushed"], data["changed"]
        self.drafted, self.ci_stopped = data.get("drafted", []), data.get("ci_stopped", data["changed"])
        self.found = data.get("found", [])
        self.draft_why, self.report_body, self.summary = data["draft_why"], data["report_body"], data["summary"]
        self.b.committed_lines = data["committed_lines"]
        self.committed = bool(data.get("committed", True))
        if stored:
            for c in self.picked(self.changed):
                c.lease = util.git(["rev-parse", "HEAD"], c.wt).strip()

    def hold(self, name: str) -> str:
        if name != "prepare" or not self.pending:
            return ""
        self.note_task()
        return pending_refusal(self)

    def note_task(self) -> None:
        data = prepared_data(self.continues)
        if self.noted or not isinstance(data.get("ticket"), dict):
            return
        self.noted = True
        opened(self.run, tracker.Ticket(**data["ticket"]))

    def reclaim(self, c: pipeline.Checkout, expected: str, published: bool, runs: set[str],
                attached: bool = True) -> str:
        branch, remote, head = self.branch, "", ""
        here = c.wt if attached else c.src
        try:
            if attached:
                on = util.git(["symbolic-ref", "-q", "HEAD"], c.wt, check=False).strip()
                if on != f"refs/heads/{branch}":
                    where = f"branch {on.removeprefix('refs/heads/')}" if on else "a detached HEAD"
                    return (f"the worktree of {c.github} is on {where}, not {branch}, so arete ran no model and "
                            f"published nothing; check out {branch} there or remove the worktree, then continue again")
            remote = pipeline.remote_head(c.src, branch)
            if remote:
                pipeline.fetch(c.src, branch)
                if util.sh(["git", "cat-file", "-e", f"{remote}^{{commit}}"], cwd=here).returncode:
                    return f"{branch} of {c.github} moved while arete checked it, so arete ran no model and published nothing"
            if attached:
                head = util.git(["rev-parse", "HEAD"], c.wt).strip()
            else:
                head = util.git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], c.src, check=False).strip() or remote
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
        c.lease = remote
        if head != c.revision:
            self.carried = True
        if head not in (remote, c.revision) and c.github not in self.pending:
            self.pending.append(c.github)
        return ""

    def prepared(self, data: dict, stored: bool) -> None:
        run = self.run
        self.ticket = tracker.Ticket(**data["ticket"])
        self.branch, self.attachments, self.triage = data["branch"], data["attachments"], data["triage"]
        self.expertise, self.approval = data["expertise"], data.get("approval")
        self.checkouts = [pipeline.Checkout(**{**c, "src": Path(c["src"]), "wt": Path(c["wt"])})
                          for c in data["checkouts"]]
        leases = {c.github: c.lease for c in self.checkouts}
        for c in self.checkouts:
            if str(c.wt) in self.restored:
                c.lease = self.restored[str(c.wt)]
        self.layer = pipeline.checked_layer(self.cfg, Path(data["layer"]))
        self.check_commands, self.check_rules = pipeline.check_script(run.dir, self.layer, self.ws)
        run.cfg, run.layer, run.kind = self.cfg, self.layer, self.ticket.kind
        run.add_dirs += [d for d in map(Path, data["add_dirs"]) if d not in run.add_dirs]
        if stored and not run.workspace:
            if why := pipeline.kept_work(self.ws, self.checkouts, run.repo.path, self.layer.copy):
                raise pipeline.RunFailed(why)
            run.workspace = self.ws, self.checkouts
        if stored:
            runs = chain_runs(self.continues)
            for c in self.checkouts:
                if why := self.reclaim(c, *authority(self.continues, c, leases[c.github]), runs):
                    raise pipeline.RunFailed(why)
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


def local_workspace(run: pipeline.Run, base: str, branch: str, ws: Path, copies: list[str]) -> list[pipeline.Checkout]:
    repo = run.repo
    checkouts = [pipeline.Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir,
                                   base or util.origin_default_branch(repo.path / p.dir))
                 for p in repo.parts or [config.Part("", repo.name)]]
    pipeline.clear_workspace(ws, checkouts, repo.path, copies)
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
    mended = state.run_get(run.repairs) or {} if run.repairs else {}
    if run.feedback or run.repairs:
        urls = run.feedback_prs or str(mended.get("pr_url") or "").split()
        if closed := [url for url in urls if gh.pr_view(url).get("state") != "OPEN"]:
            return end_as("stop", "quiet", status="noop",
                          summary=f"the PR closed before arete could {'revise' if run.feedback else 'repair'} it: "
                                  f"{', '.join(closed)}")
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
    branch = (mended.get("branch") or pipeline.open_branch(run)
              or pipeline.fill_template(main_layer.branch, config.RepoLayer.branch, kind=t.kind, id=t.id,
                                        slug=pipeline.slug(t.title)))
    if not run.dry_run:
        tk.prepare()
    if not run.alone:
        run.enter("running")
    try:
        if repo.parts and not run.dry_run:
            checkouts = pipeline.make_workspace(run.id, repo, main_layer.base_branch, branch, run.tid,
                                                main_layer.copy)[1]
        else:
            checkouts = local_workspace(run, main_layer.base_branch, branch, job.ws, main_layer.copy)
        run.workspace = job.ws, checkouts
        for c in checkouts:
            run.log(f"worktree {c.wt} on {branch}, {c.starting_point}")
        run.cfg, run.layer, run.kind = cfg, main_layer, t.kind
        pipeline.note_fresh_bases(checkouts)
        pipeline.merge_moved_bases(run, [c for c in checkouts if c.resumes], main_layer, t)
    except pipeline.RunFailed as e:
        return end_as("fail", "failed", why=e.why, detail=e.detail)
    for c in checkouts:
        c.revision = util.git(["rev-parse", "HEAD"], c.wt).strip()
    layer = pipeline.resolve_layer(cfg, run, job.ws)
    if why := pipeline.furnish(cfg, run, layer, job.ws):
        return end_as("fail", "failed", why=why)
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
                                            commands=job.commands,
                                            denied=pipeline.code_list(diffguard.denied_paths(layer)),
                                            max_lines=limit, checks=pipeline.check_list(layer),
                                            owner=run.owner_note),
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
        raise run.owner_ask("plan", [asks.text_ask(str(found["question"]))], res.session)
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
                                              checks=job.checks, commands=job.commands,
                                              max_lines=layer.max_diff_lines, owner=run.owner_note),
                    job.ws, role=BUILD, allowed_tools=pipeline.bash_rules(job.builder_bash),
                    disallowed_tools=runner.NO_GIT_WRITES, json_schema=pipeline.REVISE_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        return end_as("fail", "failed", why="revise step failed", said=res.text)
    summary, replies = res.structured.get("summary", ""), res.structured.get("replies") or []
    if pipeline.stage(job.checkouts, diffguard.denied_paths(layer)).empty:
        return end_as("stop", "unchanged", summary=summary, replies=replies)
    return Outcome("pass", data={
        "plan": f"Revise the open PR for the owner's feedback:\n\n{feedback}", "left_for_a_person": left,
        "goal": ("Check each change the owner's feedback asks for, and that what the issue asks for still works.\n\n"
                 + feedback),
        "summary": summary, "replies": replies, "story": run.story}, session=run.sessions.get(BUILD, NO_SESSION))


@factory.step("repair")
def repair(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    run, t, layer = job.run, job.ticket, job.layer
    if not run.repairs:
        return end_as("fail", "failed", why="this run names no run to repair; start it with arete repair RUN")
    try:
        found = repairs.source(state.run_get(run.repairs) or {})
        targets = [c for c in job.checkouts if c.pr in found.prs]
        if missing := [url for url in found.prs if url not in {c.pr for c in targets}]:
            raise repairs.Refused(f"{', '.join(missing)} is not an open PR of this run's checkouts")
        heads = {c.github: repairs.matched_head(c.wt, c.lease, found) for c in targets}
    except repairs.Refused as e:
        return end_as("fail", "failed", why=str(e))
    moved = [c.github for c in targets if heads[c.github] != c.lease]
    findings = pipeline.review_findings(found.review)
    count = len(found.review["problems"])
    run.log(f"repair: run {found.run_id}, {found.evidence}, {util.count(count, 'finding')}")
    for github in moved:
        run.log(f"repair: the PR head of {github} moved since run {found.run_id}; checks, verify and review run "
                "again on it")
    run.story.append(f"Review findings of run {found.run_id} to repair:\n{findings}")
    res = run.agent("repair", pipeline.prompt("repair", repo=run.repo.name, workspace=job.workspace, ref=t.ref,
                                              title=t.title, body=t.body, comments=t.comments,
                                              attachments=job.attachments, plan=found.plan, source=found.run_id,
                                              findings=findings, expertise=job.expertise, checks=job.checks,
                                              commands=job.commands, max_lines=layer.max_diff_lines,
                                              owner=run.owner_note),
                    job.ws, role=REVIEW_REPAIR, allowed_tools=pipeline.bash_rules(job.builder_bash),
                    disallowed_tools=runner.NO_GIT_WRITES)
    if not res.ok:
        return end_as("fail", "failed", why="repair step failed", said=res.text)
    if pipeline.stage(job.checkouts, diffguard.denied_paths(layer)).empty:
        return end_as("fail", "failed", said=res.text,
                      why=f"the builder changed nothing for the findings of run {found.run_id}, so they stay "
                          "unresolved")
    return Outcome("pass", data={
        "plan": found.plan, "left_for_a_person": found.left_for_a_person, "summary": res.text,
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
    if round.startswith(REVIEW_FIX):
        res = job.b.resume(round, pipeline.prompt(REVIEW_FIX, problems=feedback), role=atom)
    elif round != BUILD:
        res = job.b.resume(round, pipeline.prompt("fix", attempt=attempt, max_attempts=tries, failures=feedback,
                                                  checks=job.b.check_commands), role=atom)
    else:
        return build(job)
    return Outcome("pass" if res.ok else "fail", data={"round": round},
                   session=run.sessions.get(BUILD, NO_SESSION))


def build(job: Job) -> Outcome:
    run, t, layer = job.run, job.ticket, job.layer
    res = run.agent(BUILD, pipeline.prompt(BUILD, repo=run.repo.name, workspace=job.workspace, ref=t.ref,
                                           title=t.title, body=t.body, comments=t.comments,
                                           attachments=job.attachments, plan=job.plan_text, expertise=job.expertise,
                                           checks=job.checks, commands=job.commands,
                                           max_lines=layer.max_diff_lines, estimate=job.estimate, verify=job.goal,
                                           owner=run.owner_note),
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
def review(flow: factory.Flow, second: bool = False, attempt: int = 1, tries: int = 0) -> Outcome:
    job: Job = flow.ctx
    run = job.run
    attempt = 2 if second else attempt
    said = pipeline._review(job.staged(), job.with_left(job.plan_text), job.verified,
                            pipeline.round_name("review", attempt))
    data = {"review": said, "attempt": attempt, "story": run.story}
    if said.get("verdict") != "request_changes":
        return Outcome("pass", data=data)
    problems = pipeline.review_findings(said)
    last = second or bool(tries and attempt >= tries)
    run.story.append(f"Review still requested changes after {util.count(attempt, 'review')}, the limit:\n{problems}"
                     if last else f"Review requested changes:\n{problems}")
    return Outcome("fail", next="" if last else "implement", feedback=problems, data=data)


@factory.step("learn")
def learn(flow: factory.Flow, tree: str, apply: bool) -> Outcome:
    job: Job = flow.ctx
    pipeline._learn(job.run, job.ws, job.layer.expertise_file, job.ticket.title, job.expertise, apply=apply)
    return Outcome("pass", data={"applied": apply})


def documents(job: Job) -> bool:
    return config.document_for(job.cfg, job.run.repo, job.layer).enabled


@factory.step("document")
def write_document(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    staged = job.staged().staged
    size = f"{len(staged.files)} files, {staged.lines} lines"
    text = document.pr_text(job.run, job.ticket, pipeline.summary_of(job.review, job.build_summary), job.review,
                            job.verified, size, pipeline.draft_reason(job.review, job.verified, job.reviews),
                            job.cfg, job.layer)
    return Outcome("pass", data={"text": text})


def change(job: Job, staged: pipeline.Staged | None = None) -> tuple[pipeline.Staged, str, str]:
    staged = job.b.restage() if staged is None else staged
    if staged.empty:
        raise pipeline.RunFailed("nothing was left to push, since the workspace holds no change")
    size = f"{len(staged.files)} files, {staged.lines} lines"
    return staged, size, f"{size}, review {job.review.get('verdict')}, in use {job.verified.verdict}"


def publishing(run: pipeline.Run, branch: str, push: Callable[[], None]) -> None:
    try:
        push()
    except pipeline.RunFailed as e:
        raise pipeline.RunFailed(
            e.why, f"{e.detail}\n\nThis run's commit stays on `{branch}` in its worktree and nothing on origin was "
                   f"overwritten. `arete resume {run.id}` publishes it from the pr step when origin still holds only "
                   "this run's own commits, and refuses otherwise.") from e


@factory.step("pr")
def pr(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    b, run, t, tk = job.b, job.run, job.ticket, job.run.tracker
    found: list[pipeline.Checkout] = []
    for c in job.checkouts if run.repo.parts else []:
        if not c.pr and (url := gh.open_pr_url(c.github, job.branch)):
            c.pr = url
            found.append(c)
            run.log(f"found open {c.pr}, opened by an earlier try of this step")
    opened = list(found)
    for c in job.with_prs if run.repairs else []:
        view = gh.pr_view(c.pr)
        if view.get("state") != "OPEN":
            raise pipeline.RunFailed(f"{c.pr} is {'merged' if view.get('mergedAt') else 'closed'}, so arete pushed "
                                     f"nothing for the repair of run {run.repairs}")
        if view.get("headRefName") != job.branch:
            raise pipeline.RunFailed(f"{c.pr} is on branch {view.get('headRefName')}, not {job.branch}, so arete "
                                     f"pushed nothing for the repair of run {run.repairs}")
    fresh = b.restage(from_base=False)
    staged = b.restage(from_base=True) if fresh.empty or b.from_base else fresh
    staged, size, outcome = change(job, staged)
    unpushed: list[pipeline.Checkout] = []
    for c in [c for c in staged.changed if c not in fresh.changed]:
        head = util.git(["rev-parse", "HEAD"], c.wt).strip()
        remote = pipeline.remote_head(c.src, job.branch)
        if remote == head:
            c.lease = head
        elif remote == c.lease and (not remote or ancestor(c.wt, remote, head)):
            unpushed.append(c)
        else:
            raise pipeline.RunFailed(f"nothing was left to push in {c.github}, and its checkout of "
                                     f"{job.branch} does not match origin")
    if unpushed:
        publishing(run, job.branch, lambda: pipeline.commit_and_push(job.checkouts, unpushed, job.branch, ""))
    if fresh.empty:
        b.committed_lines = max(b.committed_lines, staged.lines)
    committed = not fresh.empty or bool(unpushed)
    subject = pipeline.fill_template(job.layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    draft_why = pipeline.draft_reason(job.review, job.verified, job.reviews)
    revised = ("Revised for the review feedback on the PR.\n\n" if run.feedback else
               f"Repairs the review findings of run {run.repairs}.\n\n" if run.repairs else "")
    if not fresh.empty:
        b.staged = fresh
        publishing(run, job.branch,
                   lambda: b.push(job.branch, f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}"))
        b.staged = staged
    if not run.repo.parts:
        record.note(run.id, branch=job.branch)
        if draft_why:
            return end_as("fail", "failed",
                          why=f"pushed {job.branch}, but {draft_why}, and this repo has no GitHub name for a draft PR")
        return Outcome("pass", data={"summary": outcome, "prs": {}, "opened": [], "pushed": [], "draft_why": "",
                                     "changed": [c.github for c in staged.changed], "report_body": "",
                                     "committed_lines": b.committed_lines,
                                     "heads": {c.github: c.lease for c in staged.changed}, "committed": committed})
    left = job.left_for_a_person
    images = pipeline.evidence_images(run, staged.changed[0], job.verified)
    report_body = pipeline.report_body(run, summary=job.build_summary, review=job.review, verified=job.verified,
                                       left=left, images=images, approval=job.approval, attempts=job.attempts,
                                       checks=pipeline.check_list(job.layer), plan_text=job.plan_text, size=size,
                                       held=draft_why, written=job.document)
    link = tk.pr_link(t, whole=not left)
    opened = [c for c in opened if c in staged.changed]
    found = [c for c in found if c in opened]
    stops = {c.github: pipeline.ci.stopped_section(gh.pr_body(c.pr)) for c in found}
    for c in staged.changed:
        if c in opened:
            gh.pr_edit_body(c.pr, with_stop(f"{link}\n\n{report_body}", stops.get(c.github, "")))
        elif not c.pr:
            c.pr = gh.pr_create(c.github, job.branch, c.base, tk.pr_title(subject, t), f"{link}\n\n{report_body}",
                                labels=tk.pr_labels, draft=bool(draft_why), cwd=c.wt)
            opened.append(c)
            run.log(f"opened {c.pr}{' as draft' if draft_why else ''}")
        else:
            run.log(f"pushed to {c.pr}" if committed else f"checked {c.pr} again, nothing new to push")
    standing = [c for c in staged.changed if c not in opened]
    pushed = standing if committed else []
    with_prs = job.with_prs
    ci_stopped = [c for c in found if stops[c.github]]
    for c in with_prs:
        others = pipeline.sibling_section(t.ref, c, with_prs)
        if c in opened:
            if others:
                gh.pr_edit_body(c.pr, with_stop(f"{link}\n\n{report_body}{others}", stops.get(c.github, "")))
            continue
        body = gh.pr_body(c.pr)
        if c in standing and pipeline.ci.stopped(body):
            ci_stopped.append(c)
        new_body = pipeline.synced_pr_body(run, c.pr, body, t, left)
        if opened:
            new_body = pipeline.with_siblings(new_body, others)
        if new_body != body:
            gh.pr_edit_body(c.pr, new_body)
        if c in pushed and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Pushed more work for {t.ref}.\n\n{report_body}")
        elif not committed and c in staged.changed and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Verified and reviewed {t.ref} again; nothing new was pushed.\n\n{report_body}")
    drafted = pipeline.hold_pushed(run, standing + found, ci_stopped, draft_why)
    return Outcome("pass", data={
        "summary": outcome, "prs": {c.github: c.pr for c in with_prs}, "opened": [c.github for c in opened],
        "found": [c.github for c in found],
        "pushed": [c.github for c in pushed], "drafted": [c.github for c in drafted],
        "ci_stopped": [c.github for c in ci_stopped], "changed": [c.github for c in staged.changed], "draft_why": draft_why,
        "report_body": report_body, "committed_lines": b.committed_lines,
        "heads": {c.github: c.lease for c in staged.changed}, "committed": committed})


def with_stop(text: str, stop: str) -> str:
    return pipeline.ci.with_section(text, stop) if stop else text


@factory.step("ci")
def watch(flow: factory.Flow, tree: str) -> Outcome:
    job: Job = flow.ctx
    run = job.run
    job.b.from_base = False

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
        what = (f"pushed {job.branch} to origin" if job.committed
                else f"verified and reviewed {job.branch} again with nothing new to push")
        return end_as("pass", "done", summary=f"{what}: {job.summary}")
    opened, pushed, with_prs = job.picked(job.opened), job.picked(job.pushed), job.with_prs
    fresh = [c for c in opened if c.github not in job.found]
    run.enter("pr")
    pr_lines = "\n".join(f"- {'Opened' if c in opened else 'Pushed to' if c in pushed else 'Unchanged'} "
                         f"[{c.pr_name}]({c.pr})" for c in with_prs)
    if run.feedback:
        pipeline.answer_feedback(run, job.replies, "Done in the latest push.")
        run.say(f"Revised for your review.\n\n{job.report_body}\n\nCI: {job.ci_result}.")
    elif run.repairs:
        run.say(f"PRs for this ticket:\n\n{pr_lines}\n\nCI: {job.ci_result}."
                + (pipeline.draft_note(job.draft_why, job.verified, kept=True)
                   or "\n\nThe review now approves it. The PR is still a draft until a person marks it ready."))
    else:
        run.say(f"PRs for this ticket:\n\n{pr_lines}\n\nCI: {job.ci_result}."
                + pipeline.published_note(job.draft_why, job.verified, fresh, job.picked(job.drafted)))
    standing = [c for c in job.picked(job.changed) if c not in fresh]
    ready = pipeline.ready_pushed(run, standing, job.picked(job.ci_stopped), job.draft_why)
    pipeline.automerge(job.cfg, run, job.layer, job.approval, job.draft_why, fresh + ready,
                       f"{job.ticket.title}\n\n{job.build_summary.strip()[:3000]}")
    return end_as("pass", "pr", summary=f"{job.summary}, CI: {job.ci_result}")


@factory.step("respond")
def respond(flow: factory.Flow, ending: str, why: str = "", said: str = "", detail: str = "", comment: str = "",
            summary: str = "", status: str = "", replies: list[dict] | None = None, prs: bool = False,
            asked: list[dict] | None = None) -> Outcome:
    job: Job = flow.ctx
    run, pr_url = job.run, ""
    if ending == "failed":
        status, summary, _ = pipeline._fail(run, why, said, detail)
        pr_url = pipeline.record_prs(run, job.branch, job.with_prs) if prs else ""
    elif ending == "ask":
        status, summary, _ = pipeline._ask(run, job.ws, job.checkouts, comment, summary, asked)
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
    return (f"Done: {summary}.\n\n## Summary\n\n{job.document or pipeline.summary_of(said, job.build_summary)}\n\n"
            f"{pipeline.assumed_block(job.run)}"
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
    job = flow.ctx
    staged, size, outcome_text = change(job)
    job.run.save("diff.patch", staged.patch)
    job.run.save("pr_body.md", outbound.screen(pipeline.report_body(
        job.run, summary=job.build_summary, review=job.review, verified=job.verified,
        left=job.left_for_a_person, images={}, approval=job.approval, attempts=job.attempts,
        checks=pipeline.check_list(job.layer), plan_text=job.plan_text, size=size,
        held=pipeline.draft_reason(job.review, job.verified, job.reviews), written=job.document)))
    return finish(flow, "dry-run", summary=outcome_text)


def driven(flow: factory.Flow, drive: Callable[[factory.Flow], tuple[str, str, str]]) -> tuple[str, str, str]:
    try:
        return drive(flow)
    except pipeline.RunFailed as e:
        return finish(flow, "failed", why=e.why, detail=e.detail)
    except asks.Asked as e:
        reached = f"ask limit of {e.limit} reached: " if e.limit is not None else ""
        return finish(flow, "ask", summary=reached + asks.summary(e.sent),
                      comment=asks.comment(e.sent, flow.ctx.run.tracker.label_name("needs-info"), e.limit),
                      asked=[{"ask_id": ask_id, "step": e.step, "call": e.call, "questions": ask["questions"]}
                             for ask_id, ask in zip(e.ask_ids, e.sent, strict=True)])


def alone(flow: factory.Flow) -> tuple[str, str, bool]:
    found = [line for line in factory.ends(lines.read(flow.run_id)) if line.get("step") == flow.only]
    if not found:
        return "failed", f"the script never ran {flow.only}", False
    said = util.first_line(str(found[-1].get("error") or found[-1].get("feedback") or ""), 300)
    return "step", f"{flow.only} {found[-1]['result']}" + (f": {said}" if said else ""), found[-1]["result"] == "pass"
