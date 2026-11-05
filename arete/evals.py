from __future__ import annotations

import os
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config, gh, paths, state, util
from .heartbeat import mark_loop
from .pipeline import (
    EDIT_TOOLS,
    NO_GIT_WRITES,
    PLAN_SCHEMA,
    READ_TOOLS,
    AgentRun,
    Checkout,
    bash_rules,
    check_list,
    command_list,
    prompt,
    run_checks,
    workspace_note,
)
from .util import git

EVALS = ".agents/evals"
OVERLAY_EXTRA = [".agents/config.toml"]


@dataclass
class Task:
    name: str
    title: str
    prompt: str
    check: str
    base: str = ""
    timeout_minutes: int = 30
    hidden_files: dict[str, bytes] = field(default_factory=dict)


@dataclass
class EvalRun(AgentRun):
    task: str = ""

    @property
    def where(self) -> str:
        return f"eval {self.repo.name}/{self.task}"


class Source:
    def __init__(self, repo: config.Repo, ref: str = ""):
        self.repo, self.ref = repo, ref
        self.label = ref or "worktree"
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
        t = Task(name=p.stem, title=data.get("title", p.stem), prompt=data["prompt"], check=data["check"],
                 base=data.get("base", ""), timeout_minutes=int(data.get("timeout_minutes", 30)))
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


def run_task(cfg: config.Config, repo: config.Repo, src: Source, task: Task, run_id: str, dry_run: bool) -> dict:
    run_dir = paths.RUNS_DIR / run_id / task.name
    run_dir.mkdir(parents=True, exist_ok=True)
    run = EvalRun(id=run_id, repo=repo, dir=run_dir, dry_run=dry_run, task=task.name)
    base = task.base or f"origin/{gh.default_branch(repo.github) if repo.github else 'main'}"
    wt = paths.WORKTREES_DIR / f"eval-{repo.name}-{task.name}"
    started = time.time()
    result = {"task": task.name, "passed": False, "why": "", "cost": 0.0, "seconds": 0.0}
    try:
        with util.repo_lock(repo.path):
            git(["fetch", "-q", "origin"], repo.path, check=False, timeout=300)
        isolated_checkout(repo.path, base, wt)
        copied = overlay_harness(src, wt)
        run.log(f"worktree at {base}, harness from {src.label} ({len(copied)} files)")
        layer = config.load_layer(repo.layer or wt / ".agents")
        for c in layer.copy:
            s = repo.path / c
            if s.exists():
                (wt / c).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, wt / c)
        for cmd in layer.setup:
            res = util.sh(cmd, cwd=wt, timeout=1200)
            if res.returncode != 0:
                result["why"] = f"setup `{cmd}` failed"
                return result
        commit_overlay(wt)

        expertise = layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"
        checks = check_list(layer)
        timeout = task.timeout_minutes * 60
        commands = command_list(layer.builder_bash)
        workspace = workspace_note("eval", [Checkout("", repo.github, repo.path, wt, base)])
        res = run.agent("plan", prompt("plan", repo=repo.name, workspace=workspace, ref=task.name, title=task.title,
                                        body=task.prompt, comments="", attachments="", expertise=expertise,
                                        commands=commands, max_lines=layer.max_diff_lines, checks=checks),
                        wt, model=cfg.model("plan"), allowed_tools=READ_TOOLS + bash_rules(config.DEFAULT_ALLOW_BASH),
                        json_schema=PLAN_SCHEMA, timeout=timeout)
        if not res.ok or not isinstance(res.structured, dict):
            result["why"] = "plan step failed"
            return result
        plan = res.structured.get("plan", "")
        estimate = int(res.structured.get("estimated_lines") or 0)
        in_use = res.structured.get("verify", "")
        tools = EDIT_TOOLS + bash_rules(layer.builder_bash)
        res = run.agent("build", prompt("build", repo=repo.name, workspace=workspace, ref=task.name,
                                         title=task.title, body=task.prompt, comments="", attachments="", plan=plan,
                                         expertise=expertise, checks=checks, commands=commands,
                                         max_lines=layer.max_diff_lines, estimate=estimate,
                                         verify=in_use),
                        wt, model=cfg.model("build"), allowed_tools=tools, disallowed_tools=NO_GIT_WRITES, timeout=timeout)
        if not res.ok:
            result["why"] = "build step failed"
            return result
        session, attempts = res.session_id, 1
        ok, report = run_checks(layer, wt, [wt], run, timeout)
        while not ok and attempts < layer.max_attempts:
            attempts += 1
            res = run.agent(f"fix{attempts}", prompt("fix", attempt=attempts, max_attempts=layer.max_attempts,
                                                     failures=report),
                            wt, model=cfg.model("build"), allowed_tools=tools, disallowed_tools=NO_GIT_WRITES,
                            resume=session, timeout=timeout)
            session = res.session_id or session
            ok, report = run_checks(layer, wt, [wt], run, timeout)
        git(["add", "-A"], wt)
        (run_dir / "diff.patch").write_text(git(["diff", "--cached", "HEAD"], wt))
        for rel, data in task.hidden_files.items():
            dest = wt / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        res = util.sh(task.check, cwd=wt, timeout=timeout)
        (run_dir / "check.log").write_text(res.stdout + res.stderr)
        result["passed"] = res.returncode == 0
        result["why"] = "" if result["passed"] else f"check exit {res.returncode}" + ("" if ok else ", repo checks failing")
        run.log(f"held out check: {'PASS' if result['passed'] else 'FAIL'}")
        return result
    except Exception as e:
        result["why"] = f"{type(e).__name__}: {util.first_line(str(e), 200)}"
        run.log(f"crashed: {result['why']}")
        return result
    finally:
        result["cost"] = run.cost
        result["seconds"] = round(time.time() - started, 1)
        shutil.rmtree(wt, ignore_errors=True)


def run(repo_name: str, task: str = "", ref: str = "", dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    repo = cfg.repo(repo_name)
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
            print(f"{t.name}: base {t.base or 'default branch'}, check `{t.check}`, {len(t.hidden_files)} hidden files")
        return 0
    with util.lock(state.run_lock("eval", repo.name)) as got:
        if not got:
            print(f"evals already running for {repo.name}")
            return 0
        run_id = state.new_run_id(f"eval-{repo.name}")
        state.start_run(run_id, "eval", repo=repo.name)
        results = []
        for t in tasks:
            prior = state.task_history(repo.name, t.name, 3)
            r = run_task(cfg, repo, src, t, run_id, dry_run)
            state.add_eval(run_id, repo.name, t.name, r["passed"], r["seconds"], r["cost"], src.label)
            r["regressed"] = len(prior) == 3 and all(prior) and not r["passed"]
            results.append(r)
        passed = sum(r["passed"] for r in results)
        cost = sum(r["cost"] for r in results)
        summary = f"{passed}/{len(results)} passed on {src.label}"
        state.finish_run(run_id, "done", summary=summary, cost_usd=cost)
        print(f"{repo.name}: {summary}, ${cost:.2f}")
        for r in results:
            print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['task']:<28} {r['seconds']:>6.0f}s ${r['cost']:.2f} {r['why']}"
                  + ("  REGRESSED" if r["regressed"] else ""))
        (paths.RUNS_DIR / run_id / "results.md").write_text(results_md(repo.name, src.label, results))
        return 0


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


def run_all(dry_run: bool = False) -> int:
    cfg = config.load()
    code = 0
    for r in cfg.repos:
        if r.path.is_dir() and (r.layer_dir / "evals").is_dir():
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
    base = next((h for h in before.values() if h["source"] != ref), None)
    if not after:
        return ""
    a = after[0]
    line = f"Evals on this branch: {a['passed']}/{a['total']} passed"
    if base:
        line += f"; the last baseline run (`{base['source']}`, {base['ts'][:10]}) passed {base['passed']}/{base['total']}"
    table = (paths.RUNS_DIR / a["run_id"] / "results.md")
    return line + ".\n\n" + (table.read_text() if table.exists() else "")
