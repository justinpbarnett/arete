from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config, gh, paths, record, runner, state, tracker, util, verify
from .evals import (
    EVALS,
    DirSource,
    EvalRun,
    Source,
    checkout_without_origin_refs,
    fetch_origin,
    grade,
    grade_failure,
    load_tasks,
    prepare,
)
from .pipeline import (
    MAX_DIFF_IN_PROMPT,
    bash_rules,
    code_list,
    commit_layer,
    make_worktree,
    prompt,
    remove_worktree,
    slug,
)
from .util import git

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "why": {"type": "string"},
        "title": {"type": "string"},
        "prompt": {"type": "string"},
        "verify": {"type": "string"},
    },
    "required": ["skip", "why", "title", "prompt", "verify"],
}


@dataclass
class Candidate:
    repo: config.Repo
    layer: config.RepoLayer
    run: dict

    @property
    def pr_url(self) -> str:
        return self.run["pr_url"]

    @property
    def name(self) -> str:
        return tracker.run_ticket_name(self.run)

    @property
    def job(self) -> str:
        return f"{slug(self.name)}-pr-{gh.pr_number(self.pr_url)}"


@dataclass
class Outcome:
    status: str
    why: str = ""
    task: str = ""


@dataclass
class Draft:
    title: str
    prompt: str
    verify: str
    base: str
    source: str

    @property
    def toml(self) -> str:
        fields = {"title": self.title, "source": self.source, "base": self.base}
        lines = [f"{k} = {toml_string(v)}" for k, v in fields.items()]
        return "\n".join(lines + [f"prompt = {toml_text(self.prompt)}", f"verify = {toml_text(self.verify)}", ""])

    def matches(self, task) -> bool:
        return ((task.title, task.prompt.strip(), task.verify.strip(), task.base, task.source)
                == (self.title, self.prompt, self.verify, self.base, self.source))


def toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_text(text: str) -> str:
    if "'''" in text or any(ch == "\x7f" or ord(ch) < 32 and ch not in "\t\n" for ch in text):
        return toml_string(text)
    return f"'''\n{text}\n'''"


def unminable(repo: config.Repo, layer: config.RepoLayer) -> str:
    if not repo.path.is_dir():
        return f"no checkout at {paths.short_path(repo.path)}"
    if any(p.dir for p in repo.parts):
        return "evals run on a single repo, and this one has parts"
    if not layer.verify:
        return f"its layer at {paths.short_path(layer.dir)} has no `[verify]` section"
    return ""


def find(cfg: config.Config, repo_name: str) -> tuple[list[Candidate], list[str]]:
    repos = [cfg.repo(repo_name)] if repo_name else [r for r in cfg.repos if r.pipeline]
    done = state.mined_urls()
    found, skips = [], []
    for repo in repos:
        layer = config.load_layer(repo.layer_dir)
        why = unminable(repo, layer)
        if why:
            skips.append(f"{repo.name}: {why}")
            continue
        seen, before = set(), 0
        for r in state.runs(kind="pipeline", repo=repo.name, limit=1000):
            url = r["pr_url"] or ""
            if r["status"] != "merged" or not url or url in seen:
                continue
            seen.add(url)
            if url in done:
                before += 1
            else:
                found.append(Candidate(repo, layer, r))
        if before:
            skips.append(f"{repo.name}: {util.count(before, 'merged PR')} mined before")
    found.sort(key=lambda c: c.run["started"], reverse=True)
    return found, skips


def surface(v: config.Verify) -> str:
    if v.start:
        how = (f"Arete starts the app with `{v.start}` and gives the grader its address. The grader uses it in a "
               "headless browser and with curl.")
    else:
        how = "Arete starts no app. The grader runs the commands the product's user runs, with real arguments."
    return f"{how}\n\n{v.notes}".strip()


def write(cfg: config.Config, run: EvalRun, c: Candidate, ticket: tracker.Ticket, wt: Path, base: str,
          merge: str) -> Draft | Outcome:
    v = c.layer.verify
    res = run.agent("write", prompt("eval_mine", repo=c.repo.name, ref=ticket.ref, title=ticket.title,
                                     body=ticket.body, comments=ticket.comments, pr_body=gh.pr_body(c.pr_url),
                                     diff=util.tail(git(["diff", base, merge], c.repo.path), MAX_DIFF_IN_PROMPT),
                                     surface=surface(v), commands=code_list(config.DEFAULT_ALLOW_BASH + v.bash)),
                    wt, model=cfg.model("eval_mine"), allowed_tools=runner.READ_TOOLS + bash_rules(config.DEFAULT_ALLOW_BASH),
                    json_schema=WRITER_SCHEMA, timeout=c.layer.timeout_minutes * 60, persist=False)
    if not res.ok or not isinstance(res.structured, dict):
        raise RuntimeError(f"the writer did not finish: {util.first_line(res.error or 'no task came back', 200)}")
    found = res.structured
    if found.get("skip"):
        return Outcome("skipped", found.get("why", "").strip() or "the writer gave no reason")
    draft = Draft(found.get("title", "").strip(), found.get("prompt", "").strip(), found.get("verify", "").strip(),
                  base, c.pr_url)
    if not (draft.title and draft.prompt and draft.verify):
        raise RuntimeError("the writer left the title, the prompt or the steps empty")
    run.save("task.toml", draft.toml)
    return draft


def graded_on(cfg: config.Config, parent: EvalRun, where: str, layer: config.RepoLayer, wt: Path,
              draft: Draft) -> verify.Verified:
    run = EvalRun(id=parent.id, repo=parent.repo, dir=parent.dir / slug(where), task=parent.task)
    run.dir.mkdir(parents=True, exist_ok=True)
    graded = grade(cfg, run, layer, wt, draft.title, draft.prompt, draft.verify, layer.timeout_minutes * 60)
    if not graded.finished:
        raise RuntimeError(f"the grader did not finish on the {where}: {graded.blocked_by}")
    parent.log(f"graded on the {where}: {graded.verdict}")
    return graded


def commit_lines(repo: Path, args: list[str]) -> list[str]:
    return git(["log", "--format=%at %ae %s", *args, "--"], repo, check=False).splitlines()


def commits_landed(repo: Path, merge: str, branch: str) -> int:
    if not branch:
        return 1
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        own = commit_lines(repo, [f"{merge}..{ref}"])
        if len(own) > 1 and commit_lines(repo, ["--first-parent", "-n", str(len(own)), merge]) == own:
            return len(own)
    return 1


def mine_one(cfg: config.Config, run: EvalRun, c: Candidate) -> Outcome:
    run.dir.mkdir(parents=True, exist_ok=True)
    merge = (gh.pr_view(c.pr_url).get("mergeCommit") or {}).get("oid") or ""
    if not merge:
        return Outcome("skipped", "GitHub gives no merge commit for it")
    fetch_origin(c.repo)
    back = commits_landed(c.repo.path, merge, c.run["branch"] or "")
    base = git(["rev-parse", f"{merge}~{back}"], c.repo.path).strip()
    ticket = tracker.for_ticket(cfg, c.repo, str(c.run["issue"])).get(str(c.run["issue"]))
    wt = paths.WORKTREES_DIR / f"mine-{c.repo.name}-{run.task}"
    try:
        checkout_without_origin_refs(c.repo.path, merge, wt)
        draft = write(cfg, run, c, ticket, wt, base, merge)
        if isinstance(draft, Outcome):
            return draft
        on_merge = graded_on(cfg, run, "merge commit", prepare(c.repo, Source(c.repo), wt, run), wt, draft)
        if on_merge.verdict != "pass":
            return Outcome("rejected", f"on the merge commit, {grade_failure(on_merge)}")
        checkout_without_origin_refs(c.repo.path, base, wt)
        on_base = graded_on(cfg, run, "base", prepare(c.repo, Source(c.repo), wt, run), wt, draft)
        if on_base.verdict == "pass":
            return Outcome("rejected", "the steps pass on the base too, so they do not test the change")
        if on_base.verdict == "blocked":
            return Outcome("rejected", f"on the base, {grade_failure(on_base)}")
        if not on_base.failed_checks:
            return Outcome("rejected", f"on the base, {util.first_line(on_base.summary, 200)}")
        return Outcome("accepted", task=save(run, c, ticket, draft, on_merge, on_base))
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def free_name(name: str, taken: Callable[[str], bool]) -> str:
    n, found = 1, name
    while taken(found):
        n += 1
        found = f"{name}-{n}"
    return found


def read_back(src: DirSource, under: str, name: str, draft: Draft) -> None:
    tasks = load_tasks(src, under, name)
    if len(tasks) != 1 or not draft.matches(tasks[0]):
        raise RuntimeError(f"{under}/{name}.toml does not read back as the task written")


def save(run: EvalRun, c: Candidate, ticket: tracker.Ticket, draft: Draft, on_merge: verify.Verified,
         on_base: verify.Verified) -> str:
    wanted = slug(f"{ticket.ref} {draft.title}")
    summary = (f"An eval task mined from {c.pr_url} ({c.name}). The grader passed its steps on the merge commit and "
               f"failed them on the base `{draft.base[:10]}`.")
    if c.repo.layer:
        name = free_name(wanted, lambda n: (c.layer.evals_dir / f"{n}.toml").exists())
        f = c.layer.evals_dir / f"{name}.toml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(draft.toml)
        read_back(DirSource(c.layer.dir), "evals", name, draft)
        commit = commit_layer(c.layer.dir, f"{c.repo.name}: eval {name}\n\n{summary}\n\nArete-Run: {run.id}", [f])
        run.log(f"saved {paths.short_path(f)}" + (f" ({commit[:12]})" if commit else ""))
        return paths.short_path(f)
    base = c.layer.base_branch or gh.default_branch(c.repo.github)
    merged = set(Source(c.repo, f"origin/{base}").files(EVALS))
    pushed = set(git(["for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes/origin/arete"], c.repo.path).split())
    name = free_name(wanted, lambda n: f"{EVALS}/{n}.toml" in merged or f"arete/evals-{n}" in pushed)
    branch = f"arete/evals-{name}"
    wt = make_worktree(c.repo, base, branch, f"evals-{name}")
    try:
        rel = f"{EVALS}/{name}.toml"
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(draft.toml)
        read_back(DirSource(wt), EVALS, name, draft)
        git(["add", "--", rel], wt)
        util.git_as_owner(["commit", "-q", "-m", f"chore(evals): add {name}\n\n{summary}\n\nArete-Run: {run.id}"], wt)
        git(["push", "-q", "--force-with-lease", "-u", "origin", branch], wt, timeout=300)
        body = "\n\n".join([summary, f"## Task\n\n**{draft.title}**\n\n{draft.prompt}", f"## Steps\n\n{draft.verify}",
                            f"## On the merge commit\n\n{on_merge.markdown()}", f"## On the base\n\n{on_base.markdown()}",
                            f"Run `{run.id}`."])
        url = gh.pr_create(c.repo.github, branch, base, f"chore(evals): add {name}", body, cwd=wt)
        run.log(f"opened {url}")
        return f"{rel} in {url}"
    finally:
        remove_worktree(c.repo, wt)


def run(repo_name: str, limit: int, dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    found, skips = find(cfg, repo_name)
    picked = found[:limit]
    print(f"{'would mine' if dry_run else 'mining'} {util.count(len(picked), 'merged PR')}"
          + (f", {len(found) - len(picked)} more left for a later mine" if len(found) > len(picked) else ""))
    for c in picked:
        print(f"  {c.name:<20} {c.pr_url}  run {c.run['id']}")
    for s in skips:
        print(f"skip {s}")
    if dry_run or not picked:
        return 0
    lock = state.run_lock("eval-mine")
    with util.lock(lock) as got:
        if not got:
            print("an eval mine is already running")
            return 0
        run_id = record.start("eval", tag="-".join(filter(None, ["mine", repo_name])), lock=lock,
                              repo=repo_name or None)
        tally = {}
        with record.failed_on_crash(run_id):
            for c in picked:
                job = EvalRun(id=run_id, repo=c.repo, dir=paths.RUNS_DIR / run_id / c.job, task=c.job)
                try:
                    o = mine_one(cfg, job, c)
                except Exception as e:
                    o = Outcome("crashed", f"{type(e).__name__}: {util.first_line(str(e), 300)}; the next mine retries it")
                else:
                    state.add_mined(c.pr_url, c.repo.name, str(c.run["issue"]), run_id, o.status, o.why, o.task)
                job.log(f"{o.status}: {o.task or o.why}")
                tally[o.status] = tally.get(o.status, 0) + 1
            summary = f"mined {util.count(len(picked), 'merged PR')}: " + ", ".join(f"{n} {s}" for s, n in tally.items())
            record.settle(run_id, "done", summary=summary)
        print(f"{summary}, {util.paid_note(record.metered_spent(run_id))}")
        return 1 if "crashed" in tally else 0
