from __future__ import annotations

import os
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config, gh, paths, record, state, tracker, util, verify
from .heartbeat import mark_loop
from .pipeline import (
    EDIT_TOOLS,
    NO_GIT_WRITES,
    PLAN_SCHEMA,
    READ_TOOLS,
    AgentRun,
    Checkout,
    RunFailed,
    TicketWork,
    bash_rules,
    check_list,
    code_list,
    denied_paths,
    prompt,
    run_checks,
    use_product,
    workspace_note,
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

    @property
    def ref(self) -> str:
        return self.task

    @property
    def where(self) -> str:
        return f"eval {self.repo.name}/{self.task}"


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
    def cost(self) -> float:
        return sum(r["cost"] for r in self.results)


class Source:
    def __init__(self, repo: config.Repo, ref: str = ""):
        self.repo, self.ref = repo, ref
        self.label = ref or state.BASELINE_SOURCE
        self._modes: dict[str, str] | None = None

    def modes(self) -> dict[str, str]:
        if self._modes is None:
            self._modes = {}
            for row in git(["ls-tree", "-r", self.ref], self.repo.path).splitlines():
                meta, _, name = row.partition("\t")
                mode, kind, _sha = meta.split()
                if kind == "blob":
                    self._modes[name] = mode
        return self._modes

    def files(self, under: str = "") -> list[str]:
        if self.ref:
            prefix = under.rstrip("/") + "/" if under else ""
            return [f for f in self.modes() if f.startswith(prefix)]
        out = git(["ls-files", "--cached", "--others", "--exclude-standard", "--deduplicate", "--", under or "."],
                  self.repo.path)
        keep = []
        for f in out.splitlines():
            p = self.repo.path / f
            if f and (p.is_symlink() or p.is_file()):
                keep.append(f)
        return keep

    def link(self, rel: str) -> str | None:
        if self.ref:
            return self.read(rel).decode() if self.modes().get(rel) == "120000" else None
        p = self.repo.path / rel
        return os.readlink(p) if p.is_symlink() else None

    def read(self, rel: str) -> bytes:
        if self.ref:
            res = subprocess.run(["git", "show", f"{self.ref}:{rel}"], cwd=self.repo.path, capture_output=True,
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
    for f in git(["ls-files"], wt).splitlines():
        if util.matches_any(f, globs) and not f.startswith(EVALS):
            (wt / f).unlink(missing_ok=True)
            remove_empty_parents((wt / f).parent, wt)


def overlay_harness(src: Source, wt: Path) -> list[str]:
    shutil.rmtree(wt / EVALS, ignore_errors=True)
    globs = [g for g in config.HARNESS_GLOBS if not g.startswith(EVALS)] + OVERLAY_EXTRA
    remove_base_harness(wt, globs)
    copied = []
    for f in src.files():
        if util.matches_any(f, globs) and not f.startswith(EVALS + "/"):
            dest = wt / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            target = src.link(f)
            if target is not None:
                os.symlink(target, dest)
            else:
                dest.write_bytes(src.read(f))
            copied.append(f)
    return copied


def isolated_checkout(repo_path: Path, base: str, dest: Path) -> None:
    # Not a worktree: a worktree shares the remote refs, so the agent could read the real fix from origin/main.
    shutil.rmtree(dest, ignore_errors=True)
    sha = git(["rev-parse", f"{base}^{{commit}}"], repo_path).strip()
    dest.mkdir(parents=True)
    git(["init", "-q"], dest)
    git(["fetch", "-q", "--no-tags", str(repo_path), sha], dest, timeout=600)
    git(["checkout", "-q", "-B", "main", "FETCH_HEAD"], dest)


def commit_overlay(wt: Path) -> None:
    git(["add", "-A"], wt)
    git(["-c", "user.name=arete", "-c", "user.email=arete@localhost", "commit", "-q", "--allow-empty",
         "--no-verify", "-m", "eval harness overlay"], wt)


def fetch_origin(repo: config.Repo) -> None:
    with util.repo_lock(repo.path):
        git(["fetch", "-q", "origin"], repo.path, check=False, timeout=300)


def prepare(repo: config.Repo, src: Source, wt: Path, run: AgentRun) -> config.RepoLayer:
    copied = overlay_harness(src, wt)
    run.log(f"harness from {src.label} ({len(copied)} files)")
    layer = config.load_layer(repo.layer or wt / ".agents")
    for c in layer.copy:
        s = repo.path / c
        if s.exists():
            (wt / c).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, wt / c)
    for cmd in layer.setup:
        if util.sh(cmd, cwd=wt, timeout=1200).returncode != 0:
            raise RunFailed(f"setup `{cmd}` failed")
    commit_overlay(wt)
    return layer


def grade(cfg: config.Config, run: EvalRun, layer: config.RepoLayer, wt: Path, title: str, ask: str, steps: str,
          timeout: float, step: str = "grade") -> verify.Verified:
    ticket = tracker.Ticket(id=run.task, ref=run.task, title=title, body=ask)
    work = TicketWork(cfg, run, ticket, wt, [Checkout("", run.repo.github, run.repo.path, wt, "")], layer, timeout,
                      workspace="You are in a checkout of its code.")
    return use_product(work, step, "grade", goal=steps)


def grade_failure(graded: verify.Verified) -> str:
    if graded.verdict == "blocked":
        return f"grading blocked: {util.first_line(graded.blocked_by or graded.summary, 200)}"
    failed = graded.failed_checks
    return f"failed in use: {util.first_line(failed[0]['what'] if failed else graded.summary, 200)}"


def run_task(cfg: config.Config, repo: config.Repo, src: Source, task: Task, run_id: str, dry_run: bool,
             prompts: Prompts) -> dict:
    run_dir = paths.RUNS_DIR / run_id / task.name
    run_dir.mkdir(parents=True, exist_ok=True)
    run = EvalRun(id=run_id, repo=repo, dir=run_dir, dry_run=dry_run, task=task.name)
    base = task.base or f"origin/{gh.default_branch(repo.github) if repo.github else 'main'}"
    wt = paths.WORKTREES_DIR / f"eval-{repo.name}-{task.name}"
    started = time.time()
    result = {"task": task.name, "passed": False, "why": "", "cost": 0.0, "seconds": 0.0}
    try:
        if not task.check and not task.verify:
            raise RunFailed("the task has neither `check` nor `verify`")
        fetch_origin(repo)
        isolated_checkout(repo.path, base, wt)
        run.log(f"checkout at {base}")
        layer = prepare(repo, src, wt, run)
        if prompts.label:
            run.log(f"plan, build and fix prompts from {prompts.label}")
        if task.verify and not layer.verify:
            raise RunFailed("the task has `verify` steps but the layer has no `[verify]` section")

        expertise = layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"
        checks = check_list(layer)
        timeout = task.timeout_minutes * 60
        commands = code_list(layer.builder_bash)
        workspace = workspace_note("eval", [Checkout("", repo.github, repo.path, wt, base)])
        res = run.agent("plan", prompt("plan", prompts.dir, repo=repo.name, workspace=workspace, ref=task.name, title=task.title,
                                        body=task.prompt, comments="", attachments="", expertise=expertise,
                                        commands=commands, denied=code_list(denied_paths(layer)),
                                        max_lines=layer.max_diff_lines, checks=checks),
                        wt, model=cfg.model("plan"), allowed_tools=READ_TOOLS + bash_rules(config.DEFAULT_ALLOW_BASH),
                        json_schema=PLAN_SCHEMA, timeout=timeout)
        if not res.ok or not isinstance(res.structured, dict):
            result["why"] = "plan step failed"
            return result
        plan = res.structured.get("plan", "")
        estimate = int(res.structured.get("estimated_lines") or 0)
        in_use = res.structured.get("verify", "")
        tools = EDIT_TOOLS + bash_rules(layer.builder_bash)
        res = run.agent("build", prompt("build", prompts.dir, repo=repo.name, workspace=workspace, ref=task.name,
                                         title=task.title, body=task.prompt, comments="", attachments="", plan=plan,
                                         expertise=expertise, checks=checks, commands=commands,
                                         max_lines=layer.max_diff_lines, estimate=estimate,
                                         verify=in_use),
                        wt, model=cfg.model("build"), allowed_tools=tools, disallowed_tools=NO_GIT_WRITES, timeout=timeout)
        if not res.ok:
            result["why"] = "build step failed"
            return result
        session, attempts = res.session, 1
        ok, report = run_checks(layer, wt, [wt], run, timeout)
        while not ok and attempts < layer.max_attempts:
            attempts += 1
            res = run.agent(f"fix{attempts}", prompt("fix", prompts.dir, attempt=attempts, max_attempts=layer.max_attempts,
                                                     failures=report),
                            wt, model=cfg.model("build"), allowed_tools=tools, disallowed_tools=NO_GIT_WRITES,
                            resume=session, timeout=timeout)
            session = res.session
            ok, report = run_checks(layer, wt, [wt], run, timeout)
        git(["add", "-A"], wt)
        (run_dir / "diff.patch").write_text(git(["diff", "--cached", "HEAD"], wt))
        failures = []
        if task.verify:
            graded = grade(cfg, run, layer, wt, task.title, task.prompt, task.verify, timeout)
            if graded.verdict != "pass":
                failures.append(grade_failure(graded))
        if task.check:
            for rel, data in task.hidden_files.items():
                dest = wt / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            res = util.sh(task.check, cwd=wt, timeout=timeout)
            (run_dir / "check.log").write_text(res.stdout + res.stderr)
            run.log(f"held out check: {'PASS' if res.returncode == 0 else 'FAIL'}")
            if res.returncode != 0:
                failures.append(f"check exit {res.returncode}")
        result["passed"] = not failures
        result["why"] = "; ".join(failures) + ("" if ok or not failures else ", repo checks failing")
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
        result["cost"] = run.cost
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
    for t in tasks:
        prior = state.task_history(repo.name, t.name, 3)
        r = run_task(cfg, repo, src, t, run_id, False, prompts)
        state.add_eval(run_id, repo.name, t.name, r["passed"], r["seconds"], r["cost"], label)
        r["regressed"] = len(prior) == 3 and all(prior) and not r["passed"]
        results.append(r)
    arm = Arm(run_id, results)
    summary = f"{arm.score} passed on {label}"
    record.settle(run_id, "done", summary=summary)
    print(f"{repo.name}: {summary}, ${record.spent(run_id):.2f}")
    for r in results:
        print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['task']:<28} {r['seconds']:>6.0f}s ${r['cost']:.2f} {r['why']}"
              + ("  REGRESSED" if r["regressed"] else ""))
    (paths.RUNS_DIR / run_id / "results.md").write_text(results_md(repo.name, label, results))
    return arm


def results_md(repo: str, source: str, results: list[dict]) -> str:
    passed = sum(r["passed"] for r in results)
    lines = [f"Evals for {repo} on `{source}`: {passed}/{len(results)} passed.", "",
             "| task | result | time | cost |", "|---|---|---|---|"]
    for r in results:
        res = "pass" if r["passed"] else f"fail ({r['why']})"
        if r.get("regressed"):
            res += ", passed the three runs before"
        lines.append(f"| {r['task']} | {res} | {r['seconds']:.0f}s | ${r['cost']:.2f} |")
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
    main, pr = (export_prompts(arete, p.label, p.dir) for p in arms.values())
    changed = sorted(name for name in main.keys() | pr.keys() if main.get(name) != pr.get(name))
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
        tasks = load_tasks(*task_source(repo, Source(repo)))
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
    lines = [(f"Prompt evals: each eval suite ran its tasks twice today, once with the prompts on `{main_label}` and "
              f"once with this branch's. Changed: {code_list(changed)}."), ""]
    if ran:
        lines += [f"| suite | `{main_label}` | this branch | tasks that changed |", "|---|---|---|---|"]
        for repo, result in ran:
            lines.append(f"| {shown(repo)} | {result['main'].score} | {result['pr'].score} | "
                         f"{flipped_tasks(result['main'], result['pr'], hidden(repo))} |")
        totals = {arm: Arm("", [r for _, result in ran for r in result[arm].results]) for arm in arms}
        lines += [f"| all | {totals['main'].score} | {totals['pr'].score} | |", ""]
        ids = [a.run_id for repo, result in ran if not hidden(repo) for a in result.values()]
        lines.append(f"Cost ${totals['main'].cost + totals['pr'].cost:.2f}."
                     + (f" Runs {code_list(ids)}." if ids else ""))
    else:
        lines.append("No eval suite ran.")
    if unmeasured:
        lines.append(f"Not measured, as no eval runs them: {code_list(unmeasured)}.")
    if skipped:
        lines.append("Skipped: " + ", ".join(f"{shown(repo)} ({why})" for repo, why in skipped) + ".")
    return "\n".join(lines) + "\n"
