from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config, environment, gh, paths, record, runner, scratch, state, tracker, util, verify
from .diffguard import denied_paths
from .heartbeat import mark_loop
from .pipeline import (
    MAX_DIFF_IN_PROMPT,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    AgentRun,
    Checkout,
    RunFailed,
    TicketWork,
    bash_rules,
    check_calls,
    check_list,
    check_script,
    code_list,
    prompt,
    review_problems,
    run_checks,
    use_product,
)
from .util import git

EVALS = ".agents/evals"
OVERLAY_EXTRA = [".agents/config.toml"]
ARETE_PROMPTS = "arete/prompts"
BUILDER_PROMPTS = ("plan", "build", "fix")


@dataclass
class Task:
    name: str
    title: str
    prompt: str
    check: str = ""
    verify: str = ""
    base: str = ""
    source: str = ""
    timeout_minutes: int = 30
    hidden_files: dict[str, bytes] = field(default_factory=dict)

    @property
    def graded_by(self) -> str:
        return " and ".join(filter(None, [f"check `{self.check}`" if self.check else "",
                                          "steps graded in use" if self.verify else ""])) or "nothing"


@dataclass
class EvalRun(AgentRun):
    task: str = ""
    trial: str = ""
    reviews: bool = False
    checks_required: bool = False

    @property
    def ref(self) -> str:
        return self.task

    @property
    def where(self) -> str:
        return f"eval {self.repo.name}/{self.task}"

    def repair_limit(self, layer) -> int:
        return self.settings("check_repair").tries if self.trial else layer.max_attempts

    def gate(self, layer, wt: Path, task, timeout: float) -> tuple[bool, str]:
        return run_checks(layer, wt, [wt], self, self.time_for(timeout))

    def may_repair(self, attempts: int, limit: int) -> bool:
        return attempts < limit


@dataclass
class Prompts:
    dir: Path = paths.PROMPTS_DIR
    label: str = ""

    def missing(self) -> list[str]:
        return [f"{name}.md" for name in BUILDER_PROMPTS if not (self.dir / f"{name}.md").is_file()]

    def applied_to(self, harness: str) -> str:
        return f"{harness} with prompts {self.label}" if self.label else harness


@dataclass
class Arm:
    run_id: str
    results: list[dict]

    @property
    def score(self) -> str:
        return f"{sum(r['passed'] for r in self.results)}/{len(self.results)}"

    @property
    def paid(self) -> float:
        return sum(r["paid"] for r in self.results)


class Source:
    def __init__(self, repo: config.Repo, ref: str = ""):
        self.repo, self.ref = repo, ref
        self.label = ref or state.BASELINE_SOURCE
        self._modes: dict[str, str] | None = None

    def modes(self) -> dict[str, str]:
        if self._modes is None:
            self._modes = {}
            for row in util.git_records(["ls-tree", "-r", self.ref], self.repo.path):
                meta, _, name = row.partition("\t")
                mode, kind, _sha = meta.split()
                if kind == "blob":
                    self._modes[name] = mode
        return self._modes

    def files(self, under: str = "") -> list[str]:
        if self.ref:
            prefix = under.rstrip("/") + "/" if under else ""
            return [f for f in self.modes() if f.startswith(prefix)]
        out = util.git_records(["ls-files", "--cached", "--others", "--exclude-standard", "--deduplicate", "--",
                                under or "."], self.repo.path)
        keep = []
        for f in out:
            p = self.repo.path / f
            if p.is_symlink() or p.is_file():
                keep.append(f)
        return keep

    def link(self, rel: str) -> str | None:
        if self.ref:
            return self.read(rel).decode() if self.modes().get(rel) == "120000" else None
        p = self.repo.path / rel
        return os.readlink(p) if p.is_symlink() else None

    def read(self, rel: str) -> bytes:
        if self.ref:
            res = subprocess.run(scratch.protected_command(["git", "show", f"{self.ref}:{rel}"], self.repo.path),
                                 cwd=self.repo.path, capture_output=True,
                                 timeout=60, check=False)
            if res.returncode != 0:
                raise FileNotFoundError(rel)
            return res.stdout
        return (self.repo.path / rel).read_bytes()


class DirSource:
    def __init__(self, root: Path):
        self.root = root
        self.label = paths.short_path(root)

    def files(self, under: str = "") -> list[str]:
        top = self.root / under
        return sorted(p.relative_to(self.root).as_posix() for p in top.rglob("*")
                      if p.is_symlink() or p.is_file()) if top.is_dir() else []

    def link(self, rel: str) -> str | None:
        p = self.root / rel
        return os.readlink(p) if p.is_symlink() else None

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()


def task_source(repo: config.Repo, src: Source) -> tuple[Source | DirSource, str]:
    return (DirSource(repo.layer), "evals") if repo.layer else (src, EVALS)


def load_tasks(src: Source | DirSource, under: str, only: str = "") -> list[Task]:
    tasks = []
    files = src.files(under)
    for f in sorted(files):
        p = Path(f)
        if p.suffix != ".toml" or p.parent.as_posix() != under:
            continue
        if only and p.stem != only:
            continue
        data = tomllib.loads(src.read(f).decode())
        t = Task(name=p.stem, title=data.get("title", p.stem), prompt=data["prompt"], check=data.get("check", ""),
                 verify=data.get("verify", ""), base=data.get("base", ""), source=data.get("source", ""),
                 timeout_minutes=int(data.get("timeout_minutes", 30)))
        prefix = f"{under}/{p.stem}/"
        for h in files:
            if h.startswith(prefix) and src.link(h) is None:
                t.hidden_files[h[len(prefix):]] = src.read(h)
        tasks.append(t)
    return tasks


def remove_empty_parents(d: Path, stop: Path) -> None:
    while d != stop and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def remove_base_harness(wt: Path, globs: list[str]) -> None:
    for f in util.git_records(["ls-files"], wt):
        p = wt / f
        if util.matches_any(f, globs) and not f.startswith(EVALS) and (p.is_symlink() or p.is_file()):
            p.unlink()
            remove_empty_parents(p.parent, wt)


def harness_globs() -> list[str]:
    return [g for g in config.HARNESS_GLOBS if not g.startswith(EVALS)] + OVERLAY_EXTRA


def harness_files(src: Source) -> list[str]:
    globs = harness_globs()
    return [f for f in src.files() if util.matches_any(f, globs) and not f.startswith(EVALS + "/")]


def overlay_harness(src: Source, wt: Path) -> list[str]:
    shutil.rmtree(wt / EVALS, ignore_errors=True)
    remove_base_harness(wt, harness_globs())
    copied = harness_files(src)
    for f in copied:
        dest = wt / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        target = src.link(f)
        if target is not None:
            os.symlink(target, dest)
        else:
            dest.write_bytes(src.read(f))
    return copied


def commit_without_evals(repo_path: Path, sha: str, staging: Path) -> str:
    git(["init", "-q"], staging)
    git(["fetch", "-q", "--no-tags", "--depth", "1", str(repo_path), sha], staging, timeout=600)
    git(["read-tree", sha], staging)
    git(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", EVALS], staging)
    tree = git(["write-tree"], staging).strip()
    return git([*util.ARETE_IDENTITY, "commit-tree", tree, "-m",
                "eval base"], staging).strip()


def write_files_through_repo(repo_path: Path, snapshot: str, dest: Path) -> None:
    env = {"GIT_DIR": git(["rev-parse", "--absolute-git-dir"], repo_path).strip(), "GIT_WORK_TREE": str(dest),
           "GIT_INDEX_FILE": str(dest / ".git" / "index"),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(dest / ".git" / "objects")}
    git(["read-tree", snapshot], dest, env=env)
    git(["checkout-index", "-a"], dest, timeout=600, env=env)


def checkout_without_origin_refs(repo_path: Path, base: str, dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    sha = git(["rev-parse", f"{base}^{{commit}}"], repo_path).strip()
    dest.mkdir(parents=True)
    git(["init", "-q"], dest)
    with tempfile.TemporaryDirectory(prefix=f"{dest.name}-base-", dir=dest.parent) as staging:
        snapshot = commit_without_evals(repo_path, sha, Path(staging))
        git(["fetch", "-q", "--no-tags", staging, snapshot], dest, timeout=600)
    write_files_through_repo(repo_path, snapshot, dest)
    git(["checkout", "-q", "-B", "main", "FETCH_HEAD"], dest)


def commit_overlay(wt: Path) -> None:
    git(["add", "-A"], wt)
    git([*util.ARETE_IDENTITY, "commit", "-q", "--allow-empty",
         "--no-verify", "-m", "eval harness overlay"], wt)


def fetch_origin(repo: config.Repo) -> None:
    with util.repo_lock(repo.path):
        git(["fetch", "-q", "origin"], repo.path, check=False, timeout=300)


def prepare(repo: config.Repo, src: Source, wt: Path, run: EvalRun) -> config.RepoLayer:
    copied = overlay_harness(src, wt)
    run.log(f"harness from {src.label} ({len(copied)} files)")
    layer = config.load_layer(repo.layer or wt / ".agents")
    for c in layer.copy:
        s = repo.path / c
        if s.exists():
            (wt / c).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, wt / c)
    for cmd in layer.setup:
        if environment.sh(cmd, run.config, repo, cwd=wt, timeout=run.time_for(1200)).returncode != 0:
            raise RunFailed(f"setup `{cmd}` failed")
    commit_overlay(wt)
    return layer


def grade(cfg: config.Config, run: EvalRun, layer: config.RepoLayer, wt: Path, title: str, ask: str, steps: str,
          timeout: float, step: str = "grade") -> verify.Verified:
    ticket = tracker.Ticket(id=run.task, ref=run.task, title=title, body=ask)
    run.cfg, run.layer, run.overrides = cfg, layer, {"timeout_minutes": max(1, round(timeout / 60))}
    work = TicketWork(cfg, run, ticket, wt, [Checkout("", run.repo.github, run.repo.path, wt, "")], layer, timeout,
                      workspace="You are in a checkout of its code.")
    return use_product(work, step, "grade", role="eval_grade", goal=steps)


def grade_failure(graded: verify.Verified) -> str:
    if graded.verdict == "blocked":
        return f"grading blocked: {util.first_line(graded.blocked_by or graded.summary, 200)}"
    failed = graded.failed_checks
    return f"failed in use: {util.first_line(failed[0]['what'] if failed else graded.summary, 200)}"


def step_call(cfg: config.Config, run: EvalRun, role: str, tools: list[str], rules: list[str]) -> dict:
    if run.trial:
        return {"role": role, "allowed_tools": rules}
    return {"model": cfg.model(config.INHERITS.get(role, role)), "allowed_tools": tools + rules}


def switched_prompt(which: str, wt: Path, task: Task, plan: str, summary: str, feedback: str) -> str:
    return (f"The earlier session cannot continue on {which}. Continue the existing work in {wt}; inspect its "
            f"current diff before editing.\n\n## Task {task.name}: {task.title}\n\n{task.prompt}\n\n"
            f"## Saved plan\n\n{plan}\n\n## Earlier builder summary\n\n{summary}\n\n"
            "Keep the current task's tool restrictions and never write git metadata or secret files."
            f"\n\n## Current feedback\n\n{feedback}")


def hidden_check(cfg: config.Config, repo: config.Repo, task: Task, wt: Path, timeout: float):
    for rel, data in task.hidden_files.items():
        dest = wt / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return environment.sh(task.check, cfg, repo, cwd=wt, timeout=timeout)


def review(run: EvalRun, wt: Path, task: Task, plan: str, report: str, evidence: str, diff: str, workspace: str,
           timeout: float) -> int:
    res = run.agent("review", prompt("review", repo=run.repo.name, workspace=workspace, ref=task.name,
                                     title=task.title, body=task.prompt, comments="", attachments="", plan=plan,
                                     checks=report, evidence=evidence, diff=util.tail(diff, MAX_DIFF_IN_PROMPT)),
                    wt, role="review", allowed_tools=bash_rules(config.DEFAULT_ALLOW_BASH),
                    json_schema=REVIEW_SCHEMA, timeout=timeout)
    if not res.ok or not isinstance(res.structured, dict):
        raise RunFailed("review step failed")
    return sum(p["severity"] == "blocker" for p in review_problems(res.structured.get("problems")))


def run_task(cfg: config.Config, repo: config.Repo, src: Source, task: Task, run_id: str, dry_run: bool,
             prompts: Prompts, run: EvalRun | None = None) -> dict:
    run = run or EvalRun(id=run_id, repo=repo, dir=paths.RUNS_DIR / run_id / task.name, dry_run=dry_run,
                         task=task.name)
    run_dir = run.dir
    run_dir.mkdir(parents=True, exist_ok=True)
    base = task.base or f"origin/{gh.default_branch(repo.github) if repo.github else 'main'}"
    wt = paths.WORKTREES_DIR / "-".join(filter(None, ["eval", repo.name, task.name, run.trial]))
    started = time.time()
    result = {"task": task.name, "passed": False, "why": "", "cost": 0.0, "paid": 0.0, "seconds": 0.0,
              "repairs": 0, "failures": [], "checks": "", "verify": "", "hidden": "", "blocking": None}
    try:
        if not task.check and not task.verify:
            raise RunFailed("the task has neither `check` nor `verify`")
        fetch_origin(repo)
        checkout_without_origin_refs(repo.path, base, wt)
        run.log(f"checkout at {base}")
        layer = prepare(repo, src, wt, run)
        if run.trial:
            run.cfg, run.layer = cfg, layer
        if prompts.label:
            run.log(f"plan, build and fix prompts from {prompts.label}")
        if task.verify and not layer.verify:
            raise RunFailed("the task has `verify` steps but the layer has no `[verify]` section")

        expertise = layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"
        checks = check_list(layer)
        timeout = task.timeout_minutes * 60
        check_commands, check_rules = check_script(run_dir, layer, wt)
        runs_checks = check_calls(check_commands)
        commands = code_list(layer.builder_bash + check_commands)
        workspace = ("You are in a checkout of its code on branch `main`. It has no `origin` remote, so compare with "
                     "`HEAD`, as in `git diff HEAD`.")
        res = run.agent("plan", prompt("plan", prompts.dir, repo=repo.name, workspace=workspace, ref=task.name, title=task.title,
                                        body=task.prompt, comments="", attachments="", triage="", expertise=expertise,
                                        commands=commands, denied=code_list(denied_paths(layer)),
                                        max_lines=layer.max_diff_lines, checks=checks),
                        wt, **step_call(cfg, run, "plan", runner.READ_TOOLS, bash_rules(config.DEFAULT_ALLOW_BASH)),
                        json_schema=PLAN_SCHEMA, timeout=timeout)
        if not res.ok or not isinstance(res.structured, dict):
            result["why"] = "plan step failed"
            return result
        plan = res.structured.get("plan", "")
        estimate = int(res.structured.get("estimated_lines") or 0)
        in_use = res.structured.get("verify", "")
        rules = bash_rules(layer.builder_bash + check_rules)
        res = run.agent("build", prompt("build", prompts.dir, repo=repo.name, workspace=workspace, ref=task.name,
                                         title=task.title, body=task.prompt, comments="", attachments="", plan=plan,
                                         expertise=expertise, checks=runs_checks, commands=commands,
                                         max_lines=layer.max_diff_lines, estimate=estimate,
                                         verify=in_use),
                        wt, **step_call(cfg, run, "build", runner.BUILD_TOOLS, rules),
                        disallowed_tools=runner.NO_GIT_WRITES, timeout=timeout)
        if not res.ok:
            result["why"] = "build step failed"
            return result
        session, attempts, summary = res.session, 1, res.text
        max_attempts = run.repair_limit(layer)
        ok, report = run.gate(layer, wt, task, timeout)
        try:
            while not ok and run.may_repair(attempts, max_attempts):
                attempts += 1
                fix = prompt("fix", prompts.dir, attempt=attempts, max_attempts=max_attempts, failures=report,
                             checks=runs_checks)
                res = run.agent(f"fix{attempts}", fix, wt,
                                **step_call(cfg, run, "check_repair", runner.BUILD_TOOLS, rules),
                                disallowed_tools=runner.NO_GIT_WRITES, resume=session, timeout=timeout,
                                fresh_prompt=lambda which, fix=fix, summary=summary: switched_prompt(
                                    which, wt, task, plan, summary, fix))
                session, summary = res.session, res.text
                ok, report = run.gate(layer, wt, task, timeout)
        except RunFailed:
            result["repairs"] = sum(state.step_tries(run.id, f"fix{n}") > 0 for n in range(2, attempts + 1))
            raise
        result["repairs"] = attempts - 1
        result["checks"] = "pass" if ok else "fail"
        git(["add", "-A"], wt)
        diff = git(["diff", "--cached", "HEAD"], wt)
        (run_dir / "diff.patch").write_text(diff)
        failures = ["repo checks failing"] if run.checks_required and not ok else []
        evidence ="No check in use ran for this task."
        if task.verify:
            graded = grade(cfg, run, layer, wt, task.title, task.prompt, task.verify, run.time_for(timeout))
            result["verify"] = graded.verdict
            evidence = graded.markdown()
            if graded.verdict != "pass":
                failures.append(grade_failure(graded))
        if run.reviews:
            blocking = review(run, wt, task, plan, report, evidence, diff, workspace, run.time_for(timeout))
            result["blocking"] = blocking
            if blocking:
                failures.append(util.count(blocking, "blocking review finding"))
        if task.check:
            res = hidden_check(cfg, repo, task, wt, run.time_for(timeout))
            result["hidden"] = "pass" if res.returncode == 0 else "fail"
            (run_dir / "check.log").write_text(res.stdout + res.stderr)
            run.log(f"held out check: {'PASS' if res.returncode == 0 else 'FAIL'}")
            if res.returncode != 0:
                failures.append(f"check exit {res.returncode}")
        result["passed"] = not failures
        result["failures"] = failures
        result["why"] = "; ".join(failures) + ("" if ok or not failures or run.checks_required else ", repo checks failing")
        return result
    except RunFailed as e:
        result["why"] = e.why
        run.log(f"stopped: {e.why}")
        return result
    except Exception as e:
        result["why"] = f"{type(e).__name__}: {util.first_line(str(e), 200)}"
        run.log(f"crashed: {result['why']}")
        return result
    finally:
        result["cost"], result["paid"] = run.cost, run.paid
        result["seconds"] = round(time.time() - started, 1)
        shutil.rmtree(wt, ignore_errors=True)


def run(repo_name: str, task: str = "", ref: str = "", dry_run: bool = False, prompts: Prompts | None = None) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    repo = cfg.repo(repo_name)
    prompts = prompts or Prompts()
    if missing := prompts.missing():
        print(f"{paths.short_path(prompts.dir)} has no {', '.join(missing)}: plan, build and fix all need a prompt")
        return 1
    if any(p.dir for p in repo.parts):
        print(f"{repo.name}: evals run on a single repo, and this one has parts")
        return 1
    src = Source(repo, ref)
    tasks_src, under = task_source(repo, src)
    tasks = load_tasks(tasks_src, under, task)
    if not tasks:
        print(f"{repo.name}: no eval tasks in {under} ({tasks_src.label})")
        return 0 if not task else 1
    if dry_run:
        for t in tasks:
            print(f"{t.name}: base {t.base or 'default branch'}, graded by {t.graded_by}, "
                  f"{len(t.hidden_files)} hidden files")
        return 0
    with util.lock(state.run_lock("eval", repo.name)) as got:
        if not got:
            print(f"evals already running for {repo.name}")
            return 0
        run_suite(cfg, repo, src, tasks, repo.name, prompts)
        return 0


def run_suite(cfg: config.Config, repo: config.Repo, src: Source, tasks: list[Task], tag: str,
              prompts: Prompts) -> Arm:
    label = prompts.applied_to(src.label)
    run_id = record.start("eval", tag=tag, lock=state.run_lock("eval", repo.name), repo=repo.name)
    results = []
    with record.failed_on_crash(run_id):
        for t in tasks:
            prior = state.task_history(repo.name, t.name, 3)
            r = run_task(cfg, repo, src, t, run_id, False, prompts)
            state.add_eval(run_id, repo.name, t.name, r["passed"], r["seconds"], r["cost"], label)
            r["regressed"] = len(prior) == 3 and all(prior) and not r["passed"]
            results.append(r)
        arm = Arm(run_id, results)
        summary = f"{arm.score} passed on {label}"
        record.settle(run_id, "done", summary=summary)
    print(f"{repo.name}: {summary}, {util.paid_note(record.metered_spent(run_id))}")
    for r in results:
        print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['task']:<28} {r['seconds']:>6.0f}s {util.paid_note(r['paid']):>12} {r['why']}"
              + ("  REGRESSED" if r["regressed"] else ""))
    (paths.RUNS_DIR / run_id / "results.md").write_text(results_md(repo.name, label, results))
    return arm


def results_md(repo: str, source: str, results: list[dict]) -> str:
    passed = sum(r["passed"] for r in results)
    lines = [f"Evals for {repo} on `{source}`: {passed}/{len(results)} passed.", "",
             "| task | result | time | paid |", "|---|---|---|---|"]
    for r in results:
        why = r["why"].replace("|", "\\|")
        res = "pass" if r["passed"] else f"fail ({why})"
        if r.get("regressed"):
            res += ", passed the three runs before"
        lines.append(f"| {r['task']} | {res} | {r['seconds']:.0f}s | ${r['paid']:.2f} |")
    return "\n".join(lines) + "\n"


def suites(cfg: config.Config) -> list[config.Repo]:
    return [r for r in cfg.repos if r.path.is_dir() and (r.layer_dir / "evals").is_dir()]


def run_all(dry_run: bool = False) -> int:
    cfg = config.load()
    code = 0
    for r in suites(cfg):
        code |= run(r.name, dry_run=dry_run)
    if not dry_run:
        mark_loop("eval", code == 0)
    return code


def compare(repo_name: str, ref: str) -> str:
    cfg = config.load()
    repo = cfg.repo(repo_name)
    before = {h["run_id"]: h for h in state.eval_history(repo.name, 50)}
    run(repo_name, ref=ref)
    after = [h for h in state.eval_history(repo.name, 50) if h["run_id"] not in before]
    base = next((h for h in before.values() if h["source"] == state.BASELINE_SOURCE), None)
    if not after:
        return ""
    a = after[0]
    line = f"Evals on this branch: {a['passed']}/{a['total']} passed"
    if base:
        line += f"; the last baseline run (`{base['source']}`, {base['ts'][:10]}) passed {base['passed']}/{base['total']}"
    table = (paths.RUNS_DIR / a["run_id"] / "results.md")
    return (line + ".\n\n" + (table.read_text() if table.exists() else "")
            + "\nThe repo's eval tasks, run with this branch's harness.\n")


def export_prompts(repo: config.Repo, ref: str, dest: Path) -> dict[str, bytes]:
    src = Source(repo, ref)
    dest.mkdir(parents=True, exist_ok=True)
    found = {}
    for f in src.files(ARETE_PROMPTS):
        p = Path(f)
        if p.suffix == ".md" and p.parent.as_posix() == ARETE_PROMPTS:
            found[p.name] = src.read(f)
            (dest / p.name).write_bytes(found[p.name])
    return found


def compare_prompts(cfg: config.Config, arete: config.Repo, branch: str) -> str:
    fetch_origin(arete)
    root = paths.RUNS_DIR / record.new_id("eva", "prompts")
    arms = {"main": Prompts(root / "main", f"origin/{gh.default_branch(arete.github)}"),
            "pr": Prompts(root / "pr", f"origin/{branch}")}
    _, pr = (export_prompts(arete, p.label, p.dir) for p in arms.values())
    fork = git(["merge-base", arms["main"].label, arms["pr"].label], arete.path).strip()
    before = export_prompts(arete, fork, root / "fork")
    changed = sorted(name for name in before.keys() | pr.keys() if before.get(name) != pr.get(name))
    unmeasured = [name for name in changed if Path(name).stem not in BUILDER_PROMPTS]
    if not changed:
        return ""
    if unmeasured == changed:
        return f"No eval runs {code_list(changed)}, so this change is not measured.\n"
    ran, skipped = [], []
    for repo in suites(cfg):
        if any(p.dir for p in repo.parts):
            skipped.append((repo, "it has parts"))
            continue
        try:
            tasks = load_tasks(*task_source(repo, Source(repo)))
        except (tomllib.TOMLDecodeError, ValueError, TypeError, KeyError) as e:
            print(f"{repo.name}: eval tasks do not load: {type(e).__name__}: {e}")
            skipped.append((repo, "its eval tasks do not load"))
            continue
        if not tasks:
            continue
        with util.lock(state.run_lock("eval", repo.name)) as got:
            if not got:
                skipped.append((repo, "its evals were already running"))
                continue
            ran.append((repo, {arm: run_suite(cfg, repo, Source(repo), tasks, f"{repo.name}-{arm}", prompts)
                               for arm, prompts in arms.items()}))
    return prompt_comparison(arete, arms, changed, unmeasured, ran, skipped)


def flipped_tasks(main: Arm, pr: Arm, private: bool) -> str:
    before = {r["task"]: r["passed"] for r in main.results}
    flips = [r for r in pr.results if before.get(r["task"], r["passed"]) != r["passed"]]
    if not flips:
        return "none"
    if not private:
        return ", ".join(f"`{r['task']}` now {'passes' if r['passed'] else 'fails'}" for r in flips)
    up = sum(r["passed"] for r in flips)
    down = len(flips) - up
    return ", ".join(filter(None, [f"{util.count(up, 'task')} now {'passes' if up == 1 else 'pass'}" if up else "",
                                   f"{util.count(down, 'task')} now {'fails' if down == 1 else 'fail'}" if down else ""]))


def prompt_comparison(arete: config.Repo, arms: dict[str, Prompts], changed: list[str], unmeasured: list[str],
                      ran: list[tuple[config.Repo, dict[str, Arm]]], skipped: list[tuple[config.Repo, str]]) -> str:
    aliases: dict[str, str] = {}

    def hidden(repo: config.Repo) -> bool:
        return arete.public and not repo.public

    def shown(repo: config.Repo) -> str:
        return aliases.setdefault(repo.name, f"private repo {len(aliases) + 1}") if hidden(repo) else repo.name

    main_label = arms["main"].label
    lead = (f"each eval suite ran its tasks twice today, once with the prompts on `{main_label}` and once with this branch's"
            if ran else "no eval suite ran")
    lines = [f"Prompt evals: {lead}. Changed: {code_list(changed)}.", ""]
    if ran:
        lines += [f"| suite | `{main_label}` | this branch | tasks that changed |", "|---|---|---|---|"]
        for repo, result in ran:
            lines.append(f"| {shown(repo)} | {result['main'].score} | {result['pr'].score} | "
                         f"{flipped_tasks(result['main'], result['pr'], hidden(repo))} |")
        totals = {arm: Arm("", [r for _, result in ran for r in result[arm].results]) for arm in arms}
        lines += [f"| all | {totals['main'].score} | {totals['pr'].score} | |", ""]
        ids = [a.run_id for repo, result in ran if not hidden(repo) for a in result.values()]
        lines.append(f"{util.paid_note(totals['main'].paid + totals['pr'].paid).capitalize()}."
                     + (f" Runs {code_list(ids)}." if ids else ""))
    if unmeasured:
        lines.append(f"Not measured, as no eval runs them: {code_list(unmeasured)}.")
    if skipped:
        lines.append("Skipped: " + ", ".join(f"{shown(repo)} ({why})" for repo, why in skipped) + ".")
    return "\n".join(lines).rstrip() + "\n"
