from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    codebase,
    config,
    environment,
    gh,
    local_paths,
    paths,
    prompt_loader,
    record,
    routing,
    sandbox,
    steps,
    tracker,
    util,
    vault,
    verify,
    workspace,
)
from .agent import BUILD_TOOLS, READ_TOOLS
from .diffguard import denied_paths
from .heartbeat import mark_loop
from .pipeline import (
    MAX_DIFF_IN_PROMPT,
    PLAN_SCHEMA,
    AgentRun,
    TicketWork,
    check_calls,
    check_list,
    check_script,
    run_checks,
    use_product,
)
from .review import REVIEW_SCHEMA, review_problems
from .store import insights as insights_store
from .store import runs as run_store
from .util import git

EVALS = ".agents/evals"
MAX_BASES = 4
BASES: dict[tuple[str, str, str], Base] = {}
BASES_LOCK = threading.Lock()
LAYER_CONFIG = ".agents/config.toml"
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
    harness_paths: list[str] = field(default_factory=list)
    local_layer_settings: bytes | None = None
    executor: str = ""
    execution: dict = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return self.task

    @property
    def where(self) -> str:
        return f"eval {self.repo.name}/{self.task}"

    def repair_limit(self, layer) -> int:
        return layer.max_attempts

    def call_kw(self, cfg: config.Config, role: str, tools: list[str], rules: list[str]) -> dict:
        return {"model": cfg.model(config.INHERITS.get(role, role)), "allowed_tools": tools + rules}

    def adopt(self, cfg: config.Config, layer: config.RepoLayer) -> None:
        return None

    def gate(self, layer, wt: Path, task, timeout: float) -> tuple[bool, str]:
        return run_checks(layer, wt, [wt], self, self.time_for(timeout))

    def may_repair(self, attempts: int, limit: int) -> bool:
        return attempts < limit


@dataclass
class Prompts:
    dir: Path = paths.PROMPTS_DIR
    label: str = ""
    commit: str = ""

    def missing(self) -> list[str]:
        return [f"{name}.md" for name in BUILDER_PROMPTS if not (self.dir / f"{name}.md").is_file()]

    def applied_to(self, harness: str) -> str:
        return f"{harness} with prompts {self.label}" if self.label else harness


def prompts_digest(prompts: Prompts) -> str:
    files = {p.name: p for p in paths.PROMPTS_DIR.glob("*.md")}
    files.update({f"{name}.md": prompts.dir / f"{name}.md" for name in BUILDER_PROMPTS})
    return util.digest(*(part for name, p in sorted(files.items()) for part in (name, p.read_bytes())))


def prompts_from(prompts: Prompts) -> str:
    if prompts.commit:
        return f"{prompts.label}@{prompts.commit[:12]} {prompts.dir.relative_to(paths.RUNS_DIR).as_posix()}"
    return str(prompts.dir) if prompts.dir != paths.PROMPTS_DIR else ""


def lineage_changes(before: dict, after: dict, skip=()) -> list[str]:
    changed = []
    for key, name in (("engine_sha", "Arete"), ("prompts_sha", "prompts"),
                      ("base_commit", "base"), ("context_sha", "harness")):
        if name not in skip and before.get(key, "") != after.get(key, ""):
            changed.append(f"Arete {before.get(key, '')} to {after.get(key, '')}" if name == "Arete" else name)
    old, new = (json.loads(row.get("settings") or "{}") for row in (before, after))
    for key in sorted(old.keys() | new.keys()):
        if key == "models":
            a, b = old.get(key, {}), new.get(key, {})
            for role in sorted(a.keys() | b.keys()):
                name = f"{role} model"
                if name not in skip and a.get(role) != b.get(role):
                    changed.append(f"{name} {a.get(role, '')} to {b.get(role, '')}")
        elif key not in skip and old.get(key) != new.get(key):
            changed.append(key.replace("_", " "))
    return changed


def has_lineage(row: dict) -> bool:
    return any(row.get(k) for k in insights_store.EVAL_LINEAGE[:6])


@dataclass
class TaskResult:
    task: str
    passed: bool = False
    why: str = ""
    cost: float = 0.0
    paid: float = 0.0
    seconds: float = 0.0
    repairs: int = 0
    failures: list[str] = field(default_factory=list)
    checks: str = ""
    verify: str = ""
    hidden: str = ""
    blocking: int | None = None
    base_commit: str = ""
    context_sha: str = ""
    settings: str = ""
    regressed: bool = False
    changed: list[str] = field(default_factory=list)
    graded: bool = False
    prompts_used: set[str] = field(default_factory=set)
    required_behavior_failed: bool = False
    shared_context_sha: str = ""


@dataclass
class Arm:
    run_id: str
    results: list[TaskResult]

    @property
    def score(self) -> str:
        return f"{sum(r.passed for r in self.results)}/{len(self.results)}"

    @property
    def paid(self) -> float:
        return sum(r.paid for r in self.results)


@dataclass
class Comparison:
    markdown: str
    regressions: list[str] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)
    complete: bool = False
    incomplete: list[str] = field(default_factory=list)


class Source:
    def __init__(self, repo: config.Repo, ref: str = ""):
        self.repo, self.ref = repo, ref
        self.label = ref or insights_store.BASELINE_SOURCE
        self._modes: dict[str, str] | None = None

    def modes(self) -> dict[str, str]:
        if self._modes is None:
            modes = {}
            for row in util.git_records(["ls-tree", "-r", self.ref], self.repo.path):
                meta, _, name = row.partition("\t")
                mode, kind, _sha = meta.split()
                if kind == "blob":
                    modes[name] = mode
            self._modes = modes
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

    def executable(self, rel: str) -> bool:
        if self.ref:
            return self.modes().get(rel) == "100755"
        return bool((self.repo.path / rel).stat().st_mode & 0o100)

    def read(self, rel: str) -> bytes:
        if self.ref:
            res = subprocess.run(sandbox.protected_command(["git", "show", f"{self.ref}:{rel}"], self.repo.path),
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


class HarnessOverlay(Source):
    def __init__(self, repo: config.Repo, ref: str, changed: list[str]):
        super().__init__(repo, ref)
        self.changed = frozenset(changed)


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
    return [g for g in config.HARNESS_GLOBS if not g.startswith(EVALS)] + [LAYER_CONFIG]


def harness_files(src: Source) -> list[str]:
    globs = harness_globs()
    return [f for f in src.files() if util.matches_any(f, globs) and not f.startswith(EVALS + "/")]


def copy_harness_file(src: Source, rel: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = src.link(rel)
    if target is not None:
        os.symlink(target, dest)
    else:
        dest.write_bytes(src.read(rel))
        if src.executable(rel):
            dest.chmod(dest.stat().st_mode | 0o111)


def overlay_harness(src: Source, wt: Path) -> list[str]:
    shutil.rmtree(wt / EVALS, ignore_errors=True)
    remove_base_harness(wt, harness_globs())
    copied = harness_files(src)
    for f in copied:
        dest = wt / f
        if dest.parent.resolve() != wt.resolve() / Path(f).parent:
            raise workspace.RunFailed(f"harness path {f} has a symlink parent")
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        copy_harness_file(src, f, dest)
    return copied


def commit_without_evals(repo_path: Path, sha: str, staging: Path) -> str:
    git(["init", "-q"], staging)
    git(["fetch", "-q", "--no-tags", "--depth", "1", str(repo_path), sha], staging, timeout=600)
    git(["read-tree", sha], staging)
    git(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", EVALS], staging)
    tree = git(["write-tree"], staging).strip()
    return git([*util.ARETE_IDENTITY, "commit-tree", tree, "-m",
                "eval base"], staging).strip()


@dataclass
class Base:
    folder: tempfile.TemporaryDirectory
    snapshot: str
    git_dir: str

    @property
    def path(self) -> Path:
        return Path(self.folder.name)


def write_files_through_repo(git_dir: str, snapshot: str, dest: Path) -> None:
    env = {"GIT_DIR": git_dir, "GIT_WORK_TREE": str(dest),
           "GIT_INDEX_FILE": str(dest / ".git" / "index"),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(dest / ".git" / "objects")}
    git(["read-tree", snapshot], dest, env=env)
    git(["checkout-index", "-a"], dest, timeout=600, env=env)


def base_without_evals(repo_path: Path, sha: str, beside: Path) -> Base:
    key = (str(repo_path), sha, str(beside))
    found = BASES.get(key)
    if found and found.path.is_dir():
        return found
    folder = tempfile.TemporaryDirectory(prefix="eval-base-", dir=beside)
    try:
        git(["init", "-q"], Path(folder.name))
        with tempfile.TemporaryDirectory(prefix="eval-staging-", dir=beside) as staging:
            snapshot = commit_without_evals(repo_path, sha, Path(staging))
            git(["fetch", "-q", "--no-tags", staging, snapshot], Path(folder.name), timeout=600)
        git(["update-ref", "refs/eval/base", snapshot], Path(folder.name))
        made = Base(folder, snapshot, git(["rev-parse", "--absolute-git-dir"], repo_path).strip())
    except BaseException:
        folder.cleanup()
        raise
    BASES.pop(key, None)
    while len(BASES) >= MAX_BASES:
        BASES.pop(next(iter(BASES))).folder.cleanup()
    BASES[key] = made
    return made


def checkout_without_origin_refs(repo_path: Path, base: str, dest: Path) -> str:
    shutil.rmtree(dest, ignore_errors=True)
    sha = git(["rev-parse", f"{base}^{{commit}}"], repo_path).strip()
    dest.mkdir(parents=True)
    git(["init", "-q"], dest)
    with BASES_LOCK:
        made = base_without_evals(repo_path, sha, dest.parent)
        git(["fetch", "-q", "--no-tags", str(made.path), made.snapshot], dest, timeout=600)
    write_files_through_repo(made.git_dir, made.snapshot, dest)
    git(["checkout", "-q", "-B", "main", "FETCH_HEAD"], dest)
    return sha


def commit_overlay(wt: Path) -> None:
    git(["add", "-A"], wt)
    git([*util.ARETE_IDENTITY, "commit", "-q", "--allow-empty",
         "--no-verify", "-m", "eval harness overlay"], wt)


def fetch_origin(repo: config.Repo) -> None:
    with util.repo_lock(repo.path):
        git(["fetch", "-q", "origin"], repo.path, check=False, timeout=300)


def prepare(repo: config.Repo, src: Source, wt: Path, run: EvalRun) -> config.RepoLayer:
    copied = overlay_harness(src, wt)
    run.harness_paths = copied
    run.log(f"harness from {src.label} ({len(copied)} files)")
    local_layer = repo.path / LAYER_CONFIG
    if isinstance(src, HarnessOverlay) and not repo.layer and LAYER_CONFIG not in copied and local_layer.is_file():
        run.local_layer_settings = local_layer.read_bytes()
        write_fixture(wt, LAYER_CONFIG, run.local_layer_settings)
    layer = config.load_layer(repo.layer or wt / ".agents")
    for c in layer.copy:
        if isinstance(src, HarnessOverlay) and util.matches_any(os.path.normpath(c), harness_globs()):
            continue
        s = repo.path / c
        if s.exists():
            dest = fixture_path(wt.resolve(), c)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dest)
    for cmd in layer.setup:
        if environment.sh(cmd, run.config, repo, cwd=wt, timeout=run.time_for(1200)).returncode != 0:
            raise workspace.RunFailed(f"setup `{cmd}` failed")
    commit_overlay(wt)
    return layer


def grade(cfg: config.Config, run: EvalRun, layer: config.RepoLayer, wt: Path, title: str, ask: str, steps: str,
          timeout: float, step: str = "grade") -> verify.Verified:
    ticket = tracker.Ticket(id=run.task, ref=run.task, title=title, body=ask)
    run.cfg, run.layer, run.overrides = cfg, layer, {"timeout_minutes": max(1, round(timeout / 60))}
    work = TicketWork(cfg, run, ticket, wt, [workspace.Checkout("", run.repo.github, run.repo.path, wt, "")], layer, timeout,
                      workspace="You are in a checkout of its code.")
    return use_product(work, step, "grade", role="eval_grade", goal=steps)


def grade_failure(graded: verify.Verified) -> str:
    if graded.verdict == "blocked":
        return f"grading blocked: {util.first_line(graded.blocked_by or graded.summary, 200)}"
    failed = graded.failed_checks
    return f"failed in use: {util.first_line(failed[0]['what'] if failed else graded.summary, 200)}"


def context_digest(src: Source, repo: config.Repo, run: EvalRun, expertise: str, *, ignore=()) -> str:
    parts = []
    for name in sorted(set(run.harness_paths).difference(ignore)):
        link = src.link(name)
        parts.extend((name, link.encode() if link is not None else src.read(name)))
        if link is None and src.executable(name):
            parts.append("executable")
    if repo.layer and (repo.layer / "config.toml").exists():
        parts.extend(("layer/config.toml", (repo.layer / "config.toml").read_bytes()))
    if run.local_layer_settings is not None:
        parts.extend((LAYER_CONFIG, run.local_layer_settings))
    if not repo.layer and ".agents/expertise.md" in ignore:
        expertise = ""
    return util.digest(*parts, "expertise", expertise)


def eval_settings(cfg: config.Config, layer: config.RepoLayer) -> str:
    grade = steps.resolve(cfg, layer, "eval_grade")
    models = {role: cfg.model(config.INHERITS.get(role, role)) for role in ("plan", "build", "check_repair")}
    models.update(review=steps.resolve(cfg, layer, "review").model, eval_grade=grade.model)
    return json.dumps({"models": models, "runner": grade.runner, "effort": grade.effort, "tier": grade.tier,
                       "builder_bash": layer.builder_bash, "checks": layer.checks,
                       "denied_paths": denied_paths(layer)}, sort_keys=True)


def switched_prompt(which: str, wt: Path, task: Task, plan: str, summary: str, feedback: str) -> str:
    text = (f"The earlier session cannot continue on {which}. Continue the existing work in {wt}; inspect its "
            f"current diff before editing.\n\n## Task {task.name}: {task.title}\n\n{task.prompt}\n\n"
            f"## Saved plan\n\n{plan}\n\n## Earlier builder summary\n\n{summary}\n\n"
            "Keep the current task's tool restrictions and never write git metadata or secret files."
            f"\n\n## Current feedback\n\n{feedback}")
    return prompt_loader.task_prompt(text)


def fixture_path(root: Path, rel: str) -> Path:
    dest = (root / rel).resolve()
    if Path(rel).is_absolute() or dest == root or not dest.is_relative_to(root):
        raise workspace.RunFailed(f"fixture {rel} resolves outside the eval checkout")
    return dest


def write_fixture(wt: Path, rel: str, data: bytes) -> None:
    checkout = os.open(wt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root = wt.resolve()
        dest = fixture_path(root, rel)
        with local_paths.directory_fd(dest.parent.relative_to(root), checkout, create=True) as directory:
            fd = os.open(dest.name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644,
                         dir_fd=directory)
            with os.fdopen(fd, "wb") as out:
                mode = os.fstat(fd)
                if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
                    raise workspace.RunFailed(f"fixture {rel} must write to a regular file in the eval checkout")
                out.truncate(0)
                out.write(data)
    finally:
        os.close(checkout)


def hidden_check(cfg: config.Config, repo: config.Repo, task: Task, wt: Path, timeout: float):
    for rel, data in task.hidden_files.items():
        write_fixture(wt, rel, data)
    return environment.sh(task.check, cfg, repo, cwd=wt, timeout=timeout)


def review(run: EvalRun, wt: Path, brief: prompt_loader.Brief, plan: str, report: str, evidence: str, diff: str,
           timeout: float) -> int:
    res = run.agent("review", brief.prompt(
        "review", plan=plan, checks=report, evidence=evidence, diff=util.tail(diff, MAX_DIFF_IN_PROMPT),
        compare="`git diff HEAD`", earlier=""),
                    wt, role="review", allowed_tools=prompt_loader.bash_rules(config.DEFAULT_ALLOW_BASH),
                    json_schema=REVIEW_SCHEMA, timeout=timeout)
    if not res.ok or not isinstance(res.structured, dict):
        raise workspace.RunFailed("review step failed")
    return sum(p["severity"] == "blocker" for p in review_problems(res.structured.get("problems")))


def required_check_outcome(verdict: str, cause: str | None = None, exit_code: int | None = None) -> str:
    if verdict == "pass":
        return "pass"
    if (verdict == "fail" and cause in (None, "product") and exit_code is not None
            and exit_code >= 0 and exit_code not in (124, 126, 127) and not 129 <= exit_code <= 192):
        return "fail"
    return ""


def acceptance(cfg, run, layer, wt, task, brief, result, plan, report, ok, timeout):
    git(["add", "-A"], wt)
    diff = vault.hidden(git(["diff", "--cached", "HEAD"], wt))
    (run.dir / "diff.patch").write_text(diff)
    failures = result.failures + (["repo checks failing"] if run.checks_required and not ok else [])
    complete = True
    evidence = "No check in use ran for this task."
    if task.verify:
        graded = grade(cfg, run, layer, wt, task.title, task.prompt, task.verify, run.time_for(timeout))
        result.verify = graded.verdict
        evidence = graded.markdown()
        failed = []
        if graded.behavior_reached:
            for check in graded.failed_checks:
                exit_status = re.match(r"\s*exit(?: code| status)?\s+(-?\d+)\b", check.get("saw", ""), re.I)
                cause = check.get("cause")
                failed.append(required_check_outcome("fail" if check.get("passed") is False else "",
                                                     "product" if cause == "change" else cause,
                                                     int(exit_status[1]) if exit_status else None))
        complete = graded.behavior_reached and graded.finished and (
            (graded.verdict == "pass" and not graded.failed_checks)
            or (graded.verdict == "fail" and bool(failed) and all(failed)))
        result.required_behavior_failed = "fail" in failed
        if graded.verdict != "pass":
            failures.append(grade_failure(graded))
    if run.reviews:
        blocking = review(run, wt, brief, plan, report, evidence, diff, run.time_for(timeout))
        result.blocking = blocking
        if blocking:
            failures.append(util.count(blocking, "blocking review finding"))
    if task.check:
        res = hidden_check(cfg, run.repo, task, wt, run.time_for(timeout))
        result.hidden = "pass" if res.returncode == 0 else "fail"
        cause = "environment" if re.search(r"^sandbox-exec:", res.stderr, re.M) else None
        outcome = required_check_outcome(result.hidden, cause, res.returncode)
        complete = complete and bool(outcome)
        result.required_behavior_failed |= outcome == "fail"
        (run.dir / "check.log").write_text(res.stdout + res.stderr)
        run.log(f"held out check: {'PASS' if res.returncode == 0 else 'FAIL'}")
        if res.returncode != 0:
            failures.append(f"check exit {res.returncode}")
    result.graded = complete
    result.passed = not failures
    result.failures = failures
    result.why = "; ".join(failures) + ("" if ok or not failures or run.checks_required else ", repo checks failing")
    return result


def run_task(cfg: config.Config, src: Source, task: Task, prompts: Prompts, run: EvalRun) -> TaskResult:
    repo = run.repo
    run_dir = run.dir
    run_dir.mkdir(parents=True, exist_ok=True)
    base = task.base or f"origin/{gh.default_branch(repo.github) if repo.github else 'main'}"
    wt = paths.WORKTREES_DIR / "-".join(filter(None, ["eval", repo.name, task.name, run.trial]))
    started = time.time()
    original = wt
    result = TaskResult(task.name)
    try:
        if not task.check and not task.verify:
            raise workspace.RunFailed("the task has neither `check` nor `verify`")
        fetch_origin(repo)
        result.base_commit = checkout_without_origin_refs(repo.path, base, wt)
        run.log(f"checkout at {base} ({result.base_commit[:12]})")
        layer = prepare(repo, src, wt, run)
        run.adopt(cfg, layer)
        if prompts.label:
            run.log(f"plan, build and fix prompts from {prompts.label}")
        if task.verify and not layer.verify:
            raise workspace.RunFailed("the task has `verify` steps but the layer has no `[verify]` section")

        expertise = layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"
        result.context_sha = context_digest(src, repo, run, expertise)
        if isinstance(src, HarnessOverlay):
            result.shared_context_sha = context_digest(src, repo, run, expertise, ignore=src.changed)
        result.settings = eval_settings(cfg, layer)
        checks = check_list(layer)
        timeout = task.timeout_minutes * 60
        check_commands, check_rules = check_script(run_dir, layer, wt)
        runs_checks = check_calls(check_commands)
        commands = prompt_loader.code_list(layer.builder_bash + check_commands)
        workspace_text = ("You are in a checkout of its code on branch `main`. It has no `origin` remote, so compare with "
                     "`HEAD`, as in `git diff HEAD`.")
        brief = prompt_loader.Brief(repo.name, workspace_text, task.name, task.title, task.prompt,
                                    comments="",
                                    attachments="", expertise=expertise, commands=commands,
                                    max_lines=layer.max_diff_lines)
        if run.executor:
            from .lab import workflows

            wt, facts = workflows.execute(cfg, run, layer, wt, task)
            result.checks = "pass"
            if facts.get("review", {}).get("unresolved") or any(r.get("passed") is not True
                                                               for r in facts.get("requirements", [])):
                result.failures.append("workflow retained unresolved final evidence")
            return acceptance(cfg, run, layer, wt, task, brief, result, "", "Engine workflow gates completed.",
                              True, timeout)
        res = run.agent("plan", brief.prompt(
            "plan", prompts.dir, triage="", denied=prompt_loader.code_list(denied_paths(layer)), checks=checks),
                        wt, **run.call_kw(cfg, "plan", READ_TOOLS, prompt_loader.bash_rules(config.DEFAULT_ALLOW_BASH)),
                        json_schema=PLAN_SCHEMA, timeout=timeout)
        if not res.ok or not isinstance(res.structured, dict):
            result.why = "plan step failed"
            return result
        result.prompts_used.add("plan.md")
        plan = res.structured.get("plan", "")
        estimate = int(res.structured.get("estimated_lines") or 0)
        in_use = res.structured.get("verify", "")
        rules = prompt_loader.bash_rules(layer.builder_bash + check_rules)
        res = run.agent("build", brief.prompt(
            "build", prompts.dir, plan=plan, checks=runs_checks, estimate=estimate, verify=in_use),
                        wt, **run.call_kw(cfg, "build", BUILD_TOOLS, rules),
                        disallowed_tools=routing.NO_GIT_WRITES, timeout=timeout)
        if not res.ok:
            result.why = "build step failed"
            return result
        result.prompts_used.add("build.md")
        session, attempts, summary = res.session, 1, res.text
        max_attempts = run.repair_limit(layer)
        ok, report = run.gate(layer, wt, task, timeout)
        try:
            while not ok and run.may_repair(attempts, max_attempts):
                attempts += 1
                fix = prompt_loader.prompt("fix", prompts.dir, attempt=attempts, max_attempts=max_attempts, failures=report,
                             checks=runs_checks)
                res = run.agent(f"fix{attempts}", prompt_loader.task_prompt(fix), wt,
                                **run.call_kw(cfg, "check_repair", BUILD_TOOLS, rules),
                                disallowed_tools=routing.NO_GIT_WRITES, resume=session, timeout=timeout,
                                fresh_prompt=lambda which, fix=fix, summary=summary: switched_prompt(
                                    which, wt, task, plan, summary, fix))
                if res.ok or res.turns:
                    result.prompts_used.add("fix.md")
                session, summary = res.session, res.text
                ok, report = run.gate(layer, wt, task, timeout)
        except workspace.RunFailed:
            result.repairs = sum(run_store.step_tries(run.id, f"fix{n}") > 0 for n in range(2, attempts + 1))
            raise
        result.repairs = attempts - 1
        result.checks = "pass" if ok else "fail"
        return acceptance(cfg, run, layer, wt, task, brief, result, plan, report, ok, timeout)
    except workspace.RunFailed as e:
        result.why = e.why
        run.log(f"stopped: {e.why}")
        return result
    except Exception as e:
        result.why = f"{type(e).__name__}: {util.first_line(str(e), 200)}"
        run.log(f"crashed: {result.why}")
        return result
    finally:
        result.cost, result.paid = run.cost, run.paid
        result.seconds = round(time.time() - started, 1)
        shutil.rmtree(wt, ignore_errors=True)
        if original != wt:
            shutil.rmtree(original, ignore_errors=True)


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
    with util.lock(run_store.run_lock("eval", repo.name)) as got:
        if not got:
            print(f"evals already running for {repo.name}")
            return 0
        run_suite(cfg, repo, src, tasks, repo.name, prompts)
        return 0


def run_suite(cfg: config.Config, repo: config.Repo, src: Source, tasks: list[Task], tag: str,
              prompts: Prompts) -> Arm:
    label = prompts.applied_to(src.label)
    engine = codebase.load()
    lineage = {"engine_sha": engine, "prompts_sha": prompts_digest(prompts), "prompts_from": prompts_from(prompts)}
    run_id = record.start("eval", tag=tag, lock=run_store.run_lock("eval", repo.name), repo=repo.name)
    results = []
    with record.failed_on_crash(run_id):
        for t in tasks:
            prior = insights_store.task_history(repo.name, t.name, 3)
            after_step = run_store.last_step_id(run_id)
            r = run_task(cfg, src, t, prompts, EvalRun(id=run_id, repo=repo, dir=paths.RUNS_DIR / run_id / t.name,
                                                       task=t.name))
            current = {**lineage, "base_commit": r.base_commit, "context_sha": r.context_sha,
                       "settings": r.settings, "after_step": after_step, "last_step": run_store.last_step_id(run_id)}
            insights_store.add_eval(run_id, repo.name, t.name, r.passed, r.seconds, r.cost, label, current)
            r.regressed = len(prior) == 3 and all(p["passed"] for p in prior) and not r.passed
            if r.regressed:
                r.changed = (lineage_changes(prior[0], current) or ["nothing recorded changed"]
                             if has_lineage(prior[0]) else ["the last pass predates lineage"])
            results.append(r)
        arm = Arm(run_id, results)
        summary = f"{arm.score} passed on {label}"
        record.settle(run_id, "done", summary=summary)
    print(f"{repo.name}: {summary}, {util.paid_note(record.metered_spent(run_id))}")
    for r in results:
        print(f"  {'PASS' if r.passed else 'FAIL'} {r.task:<28} {r.seconds:>6.0f}s {util.paid_note(r.paid):>12} {r.why}"
              + (f"  REGRESSED: {', '.join(r.changed)}" if r.regressed else ""))
    (paths.RUNS_DIR / run_id / "results.md").write_text(results_md(repo.name, label, results))
    return arm


def results_md(repo: str, source: str, results: list[TaskResult]) -> str:
    passed = sum(r.passed for r in results)
    lines = [f"Evals for {repo} on `{source}`: {passed}/{len(results)} passed.", "",
             "| task | result | time | paid |", "|---|---|---|---|"]
    for r in results:
        res = "pass" if r.passed else f"fail ({r.why})"
        if r.regressed:
            res += f", passed the three runs before; REGRESSED: {', '.join(r.changed)}"
        res = res.replace("|", "\\|")
        lines.append(f"| {r.task} | {res} | {r.seconds:.0f}s | ${r.paid:.2f} |")
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
    before = {h["run_id"]: h for h in insights_store.eval_history(repo.name, 50)}
    run(repo_name, ref=ref)
    after = [h for h in insights_store.eval_history(repo.name, 50) if h["run_id"] not in before]
    base = next((h for h in before.values() if h["source"] == insights_store.BASELINE_SOURCE), None)
    if not after:
        return ""
    a = after[0]
    line = f"Evals on this branch: {a['passed']}/{a['total']} passed"
    if base:
        line += f"; the last baseline run (`{base['source']}`, {base['ts'][:10]}) passed {base['passed']}/{base['total']}"
        baseline = {r["task"]: r for r in insights_store.eval_rows(base["run_id"])}
        changes = []
        for row in insights_store.eval_rows(a["run_id"]):
            if prior := baseline.get(row["task"]):
                changes.extend(lineage_changes(prior, row, skip=("harness",)) if has_lineage(prior)
                               else ["the baseline predates lineage"])
        if changes:
            line += f"; since that run: {', '.join(dict.fromkeys(changes))}"
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


def paired_tasks(repo: config.Repo, default_base: str = "") -> list[Task]:
    tasks = load_tasks(*task_source(repo, Source(repo)))
    if not tasks:
        return []
    if not default_base:
        fetch_origin(repo)
        default_base = f"origin/{gh.default_branch(repo.github) if repo.github else 'main'}"
    refs = dict.fromkeys(t.base or default_base for t in tasks)
    commits = {ref: git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo.path, check=False).strip() or ref
               for ref in refs}
    return [replace(t, base=commits[t.base or default_base]) for t in tasks]


def unmeasured_prompts(names: list[str]) -> Comparison:
    return Comparison(f"Not measured, as no eval runs {prompt_loader.code_list(names)}.\n",
                      unmeasured=names, complete=True)


def prompt_arms(repo: config.Repo, base: str, head: str, fork: str) -> tuple[dict[str, Prompts], list[str]]:
    root = paths.RUNS_DIR / record.new_id("eva", "prompts")
    arms = {"main": Prompts(root / "main", base, base), "pr": Prompts(root / "pr", head, head)}
    before, pr = (export_prompts(repo, p.commit, p.dir) for p in arms.values())
    if fork and fork != base:
        before = export_prompts(repo, fork, root / "fork")
    changed = sorted(name for name in before.keys() | pr.keys() if before.get(name) != pr.get(name))
    return arms, changed


def compare_prompts(cfg: config.Config, arete: config.Repo, base: str, head: str, *, fork: str = "",
                    harness: list[str] | None = None) -> Comparison:
    prompts, changed = prompt_arms(arete, base, head, fork)
    if not changed and not harness:
        return Comparison("")
    cross_repo = any(Path(name).stem in BUILDER_PROMPTS for name in changed)
    if not cross_repo and not harness:
        return unmeasured_prompts(changed)
    return compare_arms(cfg, arete, base, head, prompts, changed, harness or [], cross_repo=cross_repo)


def compare_harness(cfg: config.Config, repo: config.Repo, base: str, head: str, files: list[str]) -> Comparison:
    prompts = {arm: Prompts() for arm in ("main", "pr")}
    return compare_arms(cfg, repo, base, head, prompts, [], files, cross_repo=False)


def compare_arms(cfg: config.Config, target: config.Repo, base: str, head: str, prompts: dict[str, Prompts],
                 changed: list[str], harness: list[str], *, cross_repo: bool) -> Comparison:
    ran, skipped = [], []
    repos = suites(cfg) if cross_repo else []
    if harness and target not in repos:
        repos.append(target)
    for repo in repos:
        multipart = any(p.dir for p in repo.parts)
        suite_base = base if repo.name == target.name else ""
        try:
            tasks = load_tasks(*task_source(repo, Source(repo))) if multipart else paired_tasks(repo, suite_base)
        except (ValueError, TypeError, KeyError, RuntimeError, OSError) as e:
            print(f"{repo.name}: eval tasks do not load: {type(e).__name__}: {e}")
            skipped.append((repo, "its eval tasks do not load", []))
            continue
        if not tasks:
            if harness and suite_base:
                skipped.append((repo, "its eval suite has no tasks", []))
            continue
        if multipart:
            skipped.append((repo, "it has parts", [task.name for task in tasks]))
            continue
        with util.lock(run_store.run_lock("eval", repo.name)) as got:
            if not got:
                skipped.append((repo, "its evals were already running", [task.name for task in tasks]))
                continue
            sources = ({"main": HarnessOverlay(repo, base, harness), "pr": HarnessOverlay(repo, head, harness)}
                       if harness and suite_base else dict.fromkeys(prompts, Source(repo)))
            ran.append((repo, {arm: run_suite(cfg, repo, sources[arm], tasks, f"{repo.name}-{arm}", prompt)
                               for arm, prompt in prompts.items()}))
    if not ran and not skipped and changed:
        return unmeasured_prompts(changed)
    if harness:
        lead = (f"The {target.name} eval suite used the harness from each commit."
                if any(repo.name == target.name for repo, _ in ran) else "No eval suite ran for the harness changes.")
        if any(repo.name != target.name for repo, _ in ran):
            lead += " External suites compared the prompts with their own harness."
    else:
        lead = ("Each eval suite ran its tasks with main's prompts and this branch's prompts" if ran
                else "No eval suite ran") + "."
    if changed:
        lead += f" Changed prompts were {prompt_loader.code_list(changed)}."
    # The paired evals do not load or track the changed skill.
    skills = tuple(sorted(f for f in harness if f.startswith(("skills/", ".claude/skills/"))))
    return comparison(target, base, lead, ran, changed, skipped, skills=skills)


def show_flips(flips: list[tuple[str, bool]], private: bool) -> str:
    if not flips:
        return "none"
    if not private:
        return ", ".join(f"`{task}` now {'passes' if passed else 'fails'}" for task, passed in flips)
    up = sum(passed for _, passed in flips)
    down = len(flips) - up
    return ", ".join(filter(None, [f"{util.count(up, 'task')} now {'passes' if up == 1 else 'pass'}" if up else "",
                                   f"{util.count(down, 'task')} now {'fails' if down == 1 else 'fail'}" if down else ""]))


def comparison(target: config.Repo, base: str, lead: str, ran: list[tuple[config.Repo, dict[str, Arm]]],
               changed_prompts: list[str], skipped: list[tuple[config.Repo, str, list[str]]], *,
               skills: tuple[str, ...] = ()) -> Comparison:
    aliases: dict[str, str] = {}

    def hidden(repo: config.Repo) -> bool:
        return repo.name != target.name and not repo.public

    def shown(repo: config.Repo) -> str:
        return aliases.setdefault(repo.name, f"private repo {len(aliases) + 1}") if hidden(repo) else repo.name

    def shown_tasks(repo: config.Repo, tasks: list[str]) -> str:
        return (f"{shown(repo)} ({util.count(len(tasks), 'task')})" if hidden(repo)
                else f"{prompt_loader.code_list(tasks)} in {shown(repo)}")

    def inputs(r: TaskResult) -> tuple[str, ...]:
        return (r.base_commit, r.shared_context_sha or r.context_sha, r.settings)

    def confirmed(r: TaskResult) -> bool:
        return r.graded or r.required_behavior_failed

    lines = [lead, ""]
    regressions = []
    incomplete = []
    exercised = set()
    complete = bool(ran) and not skipped
    if ran:
        lines += [f"| suite | `{base[:12]}` | this branch | tasks that changed |", "|---|---|---|---|"]
        for repo, result in ran:
            main, pr = result["main"], result["pr"]
            before = {r.task: r for r in main.results if confirmed(r)}
            paired = [(prior, r) for r in pr.results if confirmed(r) and (prior := before.get(r.task))
                      and r.base_commit and r.context_sha and inputs(prior) == inputs(r)]
            exercised.update(name for r in pr.results if r.graded for name in r.prompts_used)
            flips = [(r.task, not r.required_behavior_failed) for prior, r in paired
                     if prior.required_behavior_failed != r.required_behavior_failed]
            lines.append(f"| {shown(repo)} | {main.score} | {pr.score} | {show_flips(flips, hidden(repo))} |")
            failed = [task for task, passed in flips if not passed]
            if failed:
                if hidden(repo):
                    print(f"{repo.name}: eval regression in {', '.join(failed)}")
                regressions.append(shown_tasks(repo, failed))
            paired_names = {r.task for _, r in paired}
            unfinished = sorted({r.task for r in main.results + pr.results} - paired_names)
            if unfinished:
                incomplete.append(shown_tasks(repo, unfinished))
            complete = complete and bool(paired) and len(paired_names) == len(main.results) == len(pr.results)
        totals = {arm: Arm("", [r for _, result in ran for r in result[arm].results]) for arm in ("main", "pr")}
        lines += [f"| all | {totals['main'].score} | {totals['pr'].score} | |", ""]
        ids = [a.run_id for repo, result in ran if not hidden(repo) for a in result.values()]
        lines.append(f"{util.paid_note(totals['main'].paid + totals['pr'].paid).capitalize()}."
                     + (f" Runs {prompt_loader.code_list(ids)}." if ids else ""))
    unmeasured = [name for name in changed_prompts if name not in exercised] + list(skills)
    if unmeasured:
        lines.append(f"Not measured in the candidate arm: {prompt_loader.code_list(unmeasured)}.")
    incomplete.extend(shown_tasks(repo, tasks) for repo, _, tasks in skipped if tasks)
    if incomplete:
        lines.append(f"Incomplete eval tasks: {', '.join(incomplete)}.")
    if skipped:
        lines.append("Skipped: " + ", ".join(f"{shown(repo)} ({why})" for repo, why, _ in skipped) + ".")
    return Comparison("\n".join(lines).rstrip() + "\n", regressions, unmeasured, complete, incomplete)
