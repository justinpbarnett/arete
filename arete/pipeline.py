from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from string import Template

from . import ci, claude, config, gh, jira, paths, state, tracker, util, verify
from .heartbeat import mark_loop, network_up_or_skip
from .util import git

ALWAYS_DENY = [".agents/config.toml", ".github/workflows/**", ".env", ".env.*", "**/.env", "**/.env.*"]
MAX_DIFF_IN_PROMPT = 60_000
LOCKFILES = {"pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb", "uv.lock", "poetry.lock",
             "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock"}
NEVER_STAGED = [":(exclude,glob)**/__pycache__/**"]
SIBLINGS = re.compile(r"\n*## Other PRs for [^\n]*\n(?:\n- \[[^\n]*)+")
SKIPPED_FILE = paths.STATE_DIR / "skipped.json"
CONTINUE_AFTER_DROP = "The connection dropped mid step. Carry on from where you stopped and finish the step."
STEP_COST = re.compile(r"\(\d+ turns?, \$([\d.]+)\)")
CHECKS_LOCK = "checks"

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "ambiguous": {"type": "boolean"},
        "question": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "estimated_lines": {"type": "integer", "minimum": 0},
        "verify": {"type": "string"},
        "split": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"],
            },
        },
    },
    "required": ["plan", "ambiguous", "risk", "estimated_lines", "verify"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "summary": {"type": "string"},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "problem": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                },
                "required": ["problem", "severity"],
            },
        },
    },
    "required": ["verdict", "summary", "problems"],
}
LEARN_SCHEMA = {
    "type": "object",
    "properties": {
        "expertise": {"type": "string"},
        "added": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "removed": {"type": "array", "items": {
            "type": "object",
            "properties": {"lesson": {"type": "string"}, "why": {"type": "string"}},
            "required": ["lesson", "why"],
        }},
    },
    "required": ["expertise", "added", "removed"],
}
EXPERTISE_MAX_LINES = 80
UAT_SCHEMA = {
    "type": "object",
    "properties": {
        "before": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
    },
    "required": ["before", "steps"],
}
REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "replies": {"type": "array", "items": {
            "type": "object",
            "properties": {"comment_id": {"type": "integer"}, "reply": {"type": "string"}},
            "required": ["comment_id", "reply"],
        }},
    },
    "required": ["summary", "replies"],
}
CI_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "caused_by_pr": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["name", "caused_by_pr", "reason"],
        }},
        "summary": {"type": "string"},
    },
    "required": ["checks", "summary"],
}
MAX_CI_REASON_CHARS = 700
CI_FIX_LAST_ROUND = ("change nothing. This is the last round, so arete will not push again; it fails the run and "
                     "turns the PRs to drafts for a person to pick up")
FEEDBACK_QUIET_MINUTES = 10
HUNK_LINES = 6
PR_SEEN_FILE = paths.STATE_DIR / "pr_seen.json"
PR_SEEN_SETTLE = timedelta(minutes=1)
REJECTED_PR_LISTEN = timedelta(days=7)
UAT_TRIES = 3
READ_TOOLS = ["Read", "Glob", "Grep"]
EDIT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "TodoWrite"]
NO_GIT_WRITES = [
    "Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)", "Bash(git checkout*)",
    "Bash(git switch*)", "Bash(git rebase*)", "Bash(git merge*)", "Bash(gh *)",
]


def prompt(name: str, prompts_dir: Path = paths.PROMPTS_DIR, **values) -> str:
    text = (prompts_dir / f"{name}.md").read_text()
    return Template(text).safe_substitute({k: ("" if v is None else str(v)) for k, v in values.items()})


def bash_rules(patterns: list[str]) -> list[str]:
    return [f"Bash({p})" for p in patterns]


def code_list(names: list[str]) -> str:
    return ", ".join(f"`{n}`" for n in names)


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > n:
        s = s[:n + 1].rpartition("-")[0] or s[:n]
    return s or "change"


def fill_template(template: str, default: str, **values) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return default.format(**values)


def cost_note(res: claude.Result) -> str:
    return f"({res.turns} turns, ${res.cost_usd:.2f})"


def logged_cost(log_lines: list[str]) -> float:
    return round(sum(float(m.group(1)) for line in log_lines if (m := STEP_COST.search(line))), 2)


@dataclass
class AgentRun:
    id: str
    repo: config.Repo
    dir: Path
    dry_run: bool = False
    cost: float = 0.0
    add_dirs: list[Path] = field(default_factory=list)
    story: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return ""

    @property
    def where(self) -> str:
        return self.repo.name

    def log(self, msg: str) -> None:
        line = f"{util.iso()} [{self.where}] {msg}"
        print(line, flush=True)
        with open(self.dir / "run.log", "a") as f:
            f.write(line + "\n")

    def save(self, name: str, data) -> None:
        p = self.dir / name
        p.write_text(data if isinstance(data, str) else json.dumps(data, indent=2, default=str))

    def agent(self, step: str, text: str, cwd: Path, **kw) -> claude.Result:
        self.log(f"{step}: starting")
        res = claude.run(text, cwd, add_dirs=self.add_dirs, **kw)
        if res.connection_failed:
            self.log(f"{step}: lost the connection after {res.turns} turns, waiting for the network")
            if util.wait_for_network():
                self.cost += res.cost_usd
                self.log(f"{step}: the network is back, the dropped attempt cost {cost_note(res)}")
                if res.session.id and res.turns > 1 and kw.get("persist", True):
                    res = claude.run(CONTINUE_AFTER_DROP, cwd, add_dirs=self.add_dirs, **{**kw, "resume": res.session})
                else:
                    res = claude.run(text, cwd, add_dirs=self.add_dirs, **kw)
        self.cost += res.cost_usd
        self.save(f"{step}.json", {"ok": res.ok, "session": res.session.id, "cost": res.cost_usd,
                                   "turns": res.turns, "text": res.text, "structured": res.structured,
                                   "error": res.error})
        self.log(f"{step}: {'ok' if res.ok else 'FAILED ' + util.first_line(res.error)} {cost_note(res)}")
        return res


@dataclass(kw_only=True)
class Run(AgentRun):
    tracker: tracker.Tracker
    tid: str
    force: bool = False
    feedback: list[dict] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return self.tracker.ref(self.tid)

    @property
    def where(self) -> str:
        return tracker.ticket_name(self.repo.name, self.ref)

    @property
    def feedback_prs(self) -> list[str]:
        return list(dict.fromkeys(f["pr_url"] for f in self.feedback))

    @property
    def failed_state(self) -> str:
        return "pr" if self.feedback else "failed"

    @property
    def retry_hint(self) -> str:
        if self.feedback:
            return (f"Comment on the PR again to retry: a submitted review starts at once, a comment after "
                    f"{FEEDBACK_QUIET_MINUTES} quiet minutes")
        return f"Remove `{self.tracker.label_name('failed')}` to retry"

    def enter(self, status: str) -> None:
        if self.dry_run:
            return
        others = [s for s in config.STATES if s != status]
        for err in (self.tracker.label(self.tid, add=[status], remove=others), self.tracker.move(self.tid, status)):
            if err:
                self.log(f"tracker: {util.first_line(err, 300)}")

    def leave(self, status: str) -> None:
        if not self.dry_run and (err := self.tracker.label(self.tid, remove=[status])):
            self.log(f"tracker: {util.first_line(err, 300)}")

    def say(self, body: str) -> str:
        if self.dry_run:
            return ""
        err = self.say_on_prs(body) if self.feedback else self.tracker.comment(self.tid, f"{tracker.ARETE_TAG} {body}")
        if err:
            self.log(f"tracker: {util.first_line(err, 300)}")
        return err

    def say_on_prs(self, body: str) -> str:
        errors = []
        for url in self.feedback_prs:
            try:
                gh.pr_comment(url, body)
            except gh.GhError as e:
                errors.append(f"{url}: {e}")
        return "; ".join(errors)

    def give_up(self, why: str) -> None:
        self.enter(self.failed_state)
        self.say(f"Run `{self.id}` {why}. {self.retry_hint}.")


@dataclass
class Checkout:
    dir: str
    github: str
    src: Path
    wt: Path
    base: str
    pr: str = ""
    lease: str = ""
    resumes: bool = False

    @property
    def prefix(self) -> str:
        return f"{self.dir}/" if self.dir else ""

    @property
    def starting_point(self) -> str:
        if self.pr:
            return f"continuing the open PR {self.pr}"
        if self.resumes:
            return "continuing commits already pushed to the branch, with no PR yet"
        return f"fresh from `{self.base}`"

    @property
    def pr_name(self) -> str:
        return f"{self.github.split('/')[-1]}#{gh.pr_number(self.pr)}"


def remote_head(src: Path, branch: str) -> str:
    out = git(["ls-remote", "origin", f"refs/heads/{branch}"], src).split()
    return out[0] if out else ""


def fetch(src: Path, ref: str) -> None:
    with util.repo_lock(src):
        git(["fetch", "origin", ref], src)


def add_worktree(c: Checkout, branch: str, start: str) -> None:
    c.wt.parent.mkdir(parents=True, exist_ok=True)
    with util.repo_lock(c.src):
        git(["worktree", "add", "-B", branch, str(c.wt), start], c.src)


def remove_workspace(ws: Path, checkouts: list[Checkout]) -> None:
    for c in checkouts:
        with util.repo_lock(c.src):
            git(["worktree", "remove", "--force", str(c.wt)], c.src, check=False)
    shutil.rmtree(ws, ignore_errors=True)
    for c in checkouts:
        with util.repo_lock(c.src):
            git(["worktree", "prune"], c.src, check=False)


def make_workspace(repo: config.Repo, base: str, branch: str, key: str) -> tuple[Path, list[Checkout]]:
    ws = paths.WORKTREES_DIR / f"{repo.name}-{key}"
    checkouts = [Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir, base or gh.default_branch(p.github))
                 for p in repo.parts]
    remove_workspace(ws, checkouts)
    for c in checkouts:
        fetch(c.src, c.base)
        c.pr = gh.open_pr_url(c.github, branch)
        c.lease = remote_head(c.src, branch)
        c.resumes = bool(c.pr) or (bool(c.lease) and c.lease not in gh.closed_pr_heads(c.github, branch))
        if c.resumes:
            fetch(c.src, branch)
        add_worktree(c, branch, f"origin/{branch}" if c.resumes else f"origin/{c.base}")
    return ws, checkouts


def make_worktree(repo: config.Repo, base: str, branch: str, key: str) -> Path:
    c = Checkout("", repo.github, repo.path, paths.WORKTREES_DIR / f"{repo.name}-{key}", base)
    remove_workspace(c.wt, [c])
    fetch(c.src, base)
    add_worktree(c, branch, f"origin/{base}")
    return c.wt


def remove_worktree(repo: config.Repo, wt: Path) -> None:
    remove_workspace(wt, [Checkout("", repo.github, repo.path, wt, "")])


def workspace_note(branch: str, checkouts: list[Checkout]) -> str:
    if len(checkouts) == 1 and not checkouts[0].dir:
        c = checkouts[0]
        return f"You are in a worktree on branch `{branch}`, {c.starting_point}. PRs target `{c.base}`."
    rows = [f"- `{c.prefix}`: {c.github}, {c.starting_point}" for c in checkouts]
    return "\n".join([
        f"The working directory holds one git worktree per repo, each on branch `{branch}`:", "", *rows, "",
        ("The working directory itself is not a git repo, so run git as `git -C <dir> ...`. A change can touch one "
         "repo or several; arete commits each changed repo and opens a PR in it."),
    ])


def git_reads(checkouts: list[Checkout]) -> list[str]:
    return [f"git -C {c.dir} {cmd}*" for c in checkouts if c.dir for cmd in ("status", "diff", "log", "show")]


def is_lockfile(name: str) -> bool:
    return Path(name).name in LOCKFILES


def staged_line_counts(wt: Path) -> list[tuple[str, int | None]]:
    out = []
    for row in git(["diff", "--cached", "--numstat", "--no-renames"], wt).splitlines():
        parts = row.split("\t")
        if len(parts) == 3:
            added, deleted, name = parts
            out.append((name, None if added == "-" else int(added) + int(deleted)))
    return out


@dataclass
class Staged:
    files: list[str] = field(default_factory=list)
    lines: int = 0
    diff: str = ""
    denied: list[str] = field(default_factory=list)
    changed: list[Checkout] = field(default_factory=list)


def stage(checkouts: list[Checkout], deny: list[str]) -> Staged:
    out = Staged()
    for c in checkouts:
        drop_untracked_bytecode(c.wt)
        git(["add", "-A", "--", ".", *NEVER_STAGED], c.wt)
        rows = staged_line_counts(c.wt)
        if not rows:
            continue
        out.changed.append(c)
        names = [name for name, _ in rows]
        locks = [name for name in names if is_lockfile(name)]
        out.files += [c.prefix + name for name in names]
        out.lines += sum(n for name, n in rows if n and name not in locks)
        out.denied += [c.prefix + name for name in names
                       if util.matches_any(name, deny) or util.matches_any(c.prefix + name, deny)]
        out.diff += git(["diff", "--cached", "--no-renames", f"--src-prefix=a/{c.prefix}", f"--dst-prefix=b/{c.prefix}",
                         "--", ".", *(f":(exclude){name}" for name in locks)], c.wt)
        if locks:
            out.diff += f"\n(lockfile changes not shown: {', '.join(c.prefix + name for name in locks)})\n"
    return out


def lines_by_file(checkouts: list[Checkout], top: int = 12) -> str:
    rows = sorted(((c.prefix + name, n) for c in checkouts for name, n in staged_line_counts(c.wt)
                   if n and not is_lockfile(name)), key=lambda r: (-r[1], r[0]))
    out = [f"- `{name}`: {n}" for name, n in rows[:top]]
    if len(rows) > top:
        out.append(f"- {len(rows) - top} more files: {sum(n for _, n in rows[top:])}")
    return "\n".join(out)


def untracked_files(wt: Path) -> set[str]:
    return set(git(["ls-files", "--others", "--exclude-standard", "-z"], wt).split("\0")) - {""}


def untracked_by_dir(dirs: list[Path]) -> dict[Path, set[str]]:
    return {d: untracked_files(d) for d in dirs}


def untracked_added_since(before: dict[Path, set[str]]) -> list[tuple[Path, str]]:
    return [(d, name) for d, names in before.items() for name in sorted(untracked_files(d) - names)]


def remove_untracked(wt: Path, name: str) -> None:
    f = wt / name
    f.unlink(missing_ok=True)
    for d in f.parents:
        if d == wt or not d.is_dir() or any(d.iterdir()):
            break
        d.rmdir()


def drop_untracked_bytecode(wt: Path) -> None:
    for name in untracked_files(wt):
        if Path(name).parent.name == "__pycache__":
            remove_untracked(wt, name)


@contextmanager
def machine_to_itself(run: AgentRun):
    with util.lock(CHECKS_LOCK) as free:
        if free:
            yield
            return
    run.log("checks: waiting for another run's checks to finish")
    with util.lock(CHECKS_LOCK, wait=True):
        yield


def run_checks(layer: config.RepoLayer, cwd: Path, git_dirs: list[Path], run: AgentRun,
               timeout: float) -> tuple[bool, str]:
    if not layer.checks:
        return True, "no checks configured"
    untracked_before = untracked_by_dir(git_dirs)
    report, ok = [], True
    with machine_to_itself(run):
        for name, cmd in layer.checks.items():
            res = util.sh(cmd, cwd=cwd, timeout=timeout)
            passed = res.returncode == 0
            ok = ok and passed
            run.log(f"check {name}: {'pass' if passed else f'FAIL (exit {res.returncode})'}")
            if not passed:
                report.append(f"### {name} failed: `{cmd}` exit {res.returncode}\n```\n{util.tail(res.stdout + res.stderr, 6000)}\n```")
            else:
                report.append(f"### {name} passed: `{cmd}`")
    for d, name in untracked_added_since(untracked_before):
        remove_untracked(d, name)
    return ok, "\n\n".join(report)


class RetryNextPoll(Exception):
    pass


class RunFailed(Exception):
    def __init__(self, why: str, detail: str = ""):
        super().__init__(why)
        self.why, self.detail = why, detail


@dataclass
class TicketWork:
    cfg: config.Config
    run: AgentRun
    ticket: tracker.Ticket
    ws: Path
    checkouts: list[Checkout]
    layer: config.RepoLayer
    timeout: float
    workspace: str
    attachments: str = ""
    steps: Counter = field(default_factory=Counter)

    @property
    def git_dirs(self) -> list[Path]:
        return [c.wt for c in self.checkouts]

    @property
    def read_bash(self) -> list[str]:
        return config.DEFAULT_ALLOW_BASH + git_reads(self.checkouts)

    def next_step(self, name: str) -> str:
        self.steps[name] += 1
        return name if self.steps[name] == 1 else f"{name}{self.steps[name]}"


@dataclass(kw_only=True)
class Build(TicketWork):
    tools: list[str]
    deny: list[str]
    expertise: str
    session: claude.Session = field(default_factory=claude.Session)
    report: str = ""
    staged: Staged = field(default_factory=Staged)

    def resume(self, step: str, text: str, **kw) -> claude.Result:
        res = self.run.agent(step, text, self.ws, model=self.cfg.model("build"), allowed_tools=self.tools,
                             disallowed_tools=NO_GIT_WRITES, resume=self.session, timeout=self.timeout, **kw)
        self.session = res.session
        return res

    def check(self) -> bool:
        ok, self.report = run_checks(self.layer, self.ws, self.git_dirs, self.run, self.timeout)
        return ok

    def restage(self) -> Staged:
        self.staged = stage(self.checkouts, self.deny)
        return self.staged

    def require_valid_diff(self, after: str, planned: str = "") -> None:
        staged = self.restage()
        limit = self.layer.max_diff_lines
        if not staged.files:
            raise RunFailed(f"{after} undid every change")
        if staged.denied:
            raise RunFailed(f"{after} touched denied paths: {', '.join(staged.denied[:5])}")
        if staged.lines > limit:
            raise RunFailed(f"after {after} the diff is {staged.lines} lines, over the {limit} line limit{planned}; "
                            "split the work", f"### Lines changed by file\n\n{lines_by_file(self.checkouts)}")

    def recheck(self, after: str) -> None:
        if not self.check():
            raise RunFailed(f"the checks failed after {after}", self.report)
        self.require_valid_diff(after)


def split_comment(estimate: int, limit: int, split: list[dict], queue_hint: str, info_label: str,
                  config_file: Path) -> str:
    parts = [(f"The plan comes to about {estimate} changed lines, over this repo's {limit} line limit, so I stopped "
              "before building.")]
    if split:
        parts.append("A split where each part fits and leaves the repo working, in build order:")
        parts += [f"### {n}. {s.get('title', '').strip()}\n\n{s.get('body', '').strip()}" for n, s in enumerate(split, 1)]
    parts.append(f"File the parts as {queue_hint}, or narrow this one, then remove `{info_label}`. To build it whole, "
                 f"raise `max_diff_lines` in `{paths.short_path(config_file)}`.")
    return "\n\n".join(parts)


def run_issue(repo_name: str, tid: str | int, dry_run: bool = False, force: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    repo = cfg.repo(repo_name)
    if not repo.parts:
        print(f"{repo_name} has no github remote configured")
        return 1
    tk = tracker.for_repo(cfg, repo)
    try:
        tid = tk.normalize(tid)
    except ValueError as e:
        print(f"{repo_name}: {e}")
        return 1
    name = tracker.ticket_name(repo.name, tk.ref(tid))
    with util.lock(state.run_lock("pipeline", repo.name, tid)) as got:
        if not got:
            print(f"{name} is already running")
            return 0
        with util.lock("run-slots", wait=True):
            live = live_pipeline_runs()
            if not dry_run and len(live) >= cfg.max_parallel:
                print(f"{name} was not started: {len(live)} of {cfg.max_parallel} runs going. Wait for one to finish "
                      f"or raise max_parallel in {paths.short_path(paths.CONFIG_FILE)}.")
                return 1
            earlier = state.ticket_runs(repo.name, tid, limit=1)
            restarting = bool(earlier) and earlier[0]["status"] == "interrupted"
            run_id = state.new_run_id("pipeline") + f"-{repo.name}-{tid}"
            run = Run(run_id, repo, paths.RUNS_DIR / run_id, dry_run, tracker=tk, tid=tid, force=force,
                      feedback=pending_feedback(repo.name, tid))
            run.dir.mkdir(parents=True, exist_ok=True)
            state.start_run(run_id, "pipeline", repo.name, tid)
        if restarting:
            run.log(f"restarting after run {earlier[0]['id']} was interrupted")
        if run.feedback:
            run.log(f"revising {', '.join(run.feedback_prs)} for {util.count(len(run.feedback), 'comment')}")
            if not dry_run:
                state.take_feedback(run.feedback, run_id)
        try:
            status, summary, pr_url = _run(cfg, run)
        except RunFailed as e:
            status, summary, pr_url = _fail(run, e.why, detail=e.detail)
        except Exception as e:
            run.log(f"crashed: {type(e).__name__}: {e}")
            status, summary, pr_url = "failed", f"crashed: {e}"[:300], ""
            run.give_up("crashed before finishing")
        if restarting and status in ("skipped", "noop") and not run.feedback:
            run.enter("failed")
            run.say(f"I did not restart this after the interruption: {summary}. Remove "
                    f"`{tk.label_name('failed')}` to retry.")
        if run.feedback and not dry_run:
            state.settle_feedback(run_id, {"pr": "done", "noop": "recorded"}.get(status, "failed"))
        state.finish_run(run_id, status, summary=summary, pr_url=pr_url, cost_usd=round(run.cost, 4))
        run.log(f"done: {status} {pr_url or summary}")
        if status == "skipped":
            print(f"{name} was not run: {summary}. Pass --force to run it anyway.")
        return 0 if status in ("pr", "noop", "needs-info", "dry-run") else 1


def _fail(run: Run, why: str, builder_said: str = "", detail: str = "") -> tuple[str, str, str]:
    run.log(f"failed: {why}")
    run.enter(run.failed_state)
    extra = f"\n\n{detail.strip()}" if detail.strip() else ""
    if builder_said.strip():
        extra += f"\n\n### What the builder said\n\n{builder_said.strip()[:4000]}"
    task = "apply your feedback" if run.feedback else "finish this one"
    run.say(f"Could not {task}: {why}{extra}\n\nRun `{run.id}`; details in `arete status` on the "
            f"Mac. {run.retry_hint}.")
    return "failed", why, ""


def _ask(run: Run, ws: Path, checkouts: list[Checkout], comment: str, summary: str) -> tuple[str, str, str]:
    run.say(comment)
    run.enter("needs-info")
    remove_workspace(ws, checkouts)
    return "needs-info", summary[:300], ""


def resolve_layer(run: Run, ws: Path) -> config.RepoLayer:
    repo = run.repo
    if repo.layer:
        return config.load_layer(repo.layer)
    if (ws / ".agents" / "config.toml").exists():
        return config.load_layer(ws / ".agents")
    run.log(f"layer: {paths.short_path(repo.path / '.agents')} is not committed; lessons stay in the main checkout")
    return config.load_layer(repo.path / ".agents")


def _run(cfg: config.Config, run: Run) -> tuple[str, str, str]:
    repo, tk = run.repo, run.tracker
    try:
        t = tk.get(run.tid)
    except tracker.ERRORS as e:
        return "failed", f"could not read {run.ref}: {e}", ""
    if run.feedback:
        closed = [url for url in run.feedback_prs if gh.pr_view(url).get("state") != "OPEN"]
        if closed:
            return "noop", f"the PR closed before arete could revise it: {', '.join(closed)}", ""
    elif not t.open:
        return "noop", "ticket is not open", ""
    elif t.blocker and not run.force:
        return "skipped", t.blocker, ""
    try:
        images = tk.save_images(t, run.dir / "images")
    except tracker.ERRORS as e:
        return "failed", f"could not download the images on {run.ref}: {e}", ""
    if images.paths:
        run.add_dirs.append(run.dir / "images")
        run.log(f"images: saved {util.count(len(images.paths), 'image')} from {run.ref}")
    if images.left_out:
        run.log(f"images: left out {', '.join(images.left_out)}")
    attachments = images_note(images)

    main_layer = config.load_layer(repo.layer_dir)
    branch = open_branch(run) or fill_template(main_layer.branch, config.RepoLayer.branch, kind=t.kind, id=t.id,
                                               slug=slug(t.title))
    if not run.dry_run:
        tk.prepare()
    run.enter("running")

    ws, checkouts = make_workspace(repo, main_layer.base_branch, branch, t.id)
    for c in checkouts:
        run.log(f"worktree {c.wt} on {branch}, {c.starting_point}")
    layer = resolve_layer(run, ws)
    for rel in layer.copy:
        src = repo.path / rel
        if src.exists():
            dest = ws / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    for cmd in layer.setup:
        res = util.sh(cmd, cwd=ws, timeout=1200)
        run.log(f"setup `{cmd}`: exit {res.returncode}")
        if res.returncode != 0:
            run.save("setup.log", res.stdout + res.stderr)
            return _fail(run, f"setup command `{cmd}` failed")

    expertise = layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"
    checks = check_list(layer)
    reads = git_reads(checkouts)
    builder_bash = layer.builder_bash + reads
    commands = code_list(builder_bash)
    workspace = workspace_note(branch, checkouts)
    step_timeout = layer.timeout_minutes * 60
    read_tools = READ_TOOLS + bash_rules(config.DEFAULT_ALLOW_BASH + reads)
    build_tools = EDIT_TOOLS + bash_rules(builder_bash)
    deny = ALWAYS_DENY + layer.deny
    limit = layer.max_diff_lines
    info = tk.label_name("needs-info")
    replies: list[dict] = []

    if run.feedback:
        feedback = feedback_markdown(run.feedback)
        plan_text = f"Revise the open PR for the owner's feedback:\n\n{feedback}"
        goal = ("Check each change the owner's feedback asks for, and that what the issue asks for still works.\n\n"
                + feedback)
        estimate = 0
        run.story.append(f"The owner reviewed the PR and left this feedback:\n\n{feedback}")
        res = run.agent("revise", prompt("revise", repo=repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                         body=t.body, comments=t.comments, attachments=attachments,
                                         feedback=feedback, expertise=expertise, checks=checks,
                                         commands=commands, max_lines=limit),
                        ws, model=cfg.model("build"), allowed_tools=build_tools, disallowed_tools=NO_GIT_WRITES,
                        json_schema=REVISE_SCHEMA, timeout=step_timeout)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "revise step failed", res.text)
        build_summary = res.structured.get("summary", "")
        replies = res.structured.get("replies") or []
        if not stage(checkouts, deny).files:
            return answer_without_changes(run, ws, checkouts, branch, build_summary, replies)
    else:
        res = run.agent("plan", prompt("plan", repo=repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                        body=t.body, comments=t.comments, attachments=attachments,
                                        expertise=expertise, commands=commands, denied=code_list(deny), max_lines=limit,
                                        checks=checks),
                        ws, model=cfg.model("plan"), allowed_tools=read_tools, json_schema=PLAN_SCHEMA,
                        timeout=step_timeout)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "planning step failed")
        plan = res.structured
        plan_text = plan.get("plan", "")
        run.save("plan.md", plan_text)
        estimate = int(plan.get("estimated_lines") or 0)
        goal = plan.get("verify", "").strip() or "The planner gave no steps: check what the issue asks for."
        if plan.get("ambiguous") and plan.get("question"):
            run.log("plan: ambiguous, asking the author")
            return _ask(run, ws, checkouts, f"Before I build this I need one answer:\n\n{plan['question']}\n\n"
                        f"Reply here, then remove the `{info}` label and I will pick it up.", plan["question"])
        if estimate > limit:
            split = [s for s in plan.get("split") or [] if isinstance(s, dict) and s.get("title")]
            run.log(f"plan: about {estimate} lines, over the {limit} line limit; proposing a {len(split)} part split")
            run.save("split.json", split)
            return _ask(run, ws, checkouts,
                        split_comment(estimate, limit, split, tk.queue_hint, info, layer.config_file),
                        f"plan estimates {estimate} lines, over the {limit} line limit; split proposed")

        res = run.agent("build", prompt("build", repo=repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                         body=t.body, comments=t.comments, attachments=attachments, plan=plan_text,
                                         expertise=expertise, checks=checks, commands=commands, max_lines=limit,
                                         estimate=estimate, verify=goal),
                        ws, model=cfg.model("build"), allowed_tools=build_tools, disallowed_tools=NO_GIT_WRITES,
                        timeout=step_timeout)
        if not res.ok:
            return _fail(run, "build step failed", res.text)
        build_summary = res.text
        if not stage(checkouts, deny).files:
            return _fail(run, "the builder stopped without changing anything", build_summary)
    b = Build(cfg=cfg, run=run, ticket=t, ws=ws, checkouts=checkouts, layer=layer, timeout=step_timeout,
              workspace=workspace, attachments=attachments, tools=build_tools, deny=deny, expertise=expertise,
              session=res.session)

    attempts = 1
    ok = b.check()
    while not ok and attempts < layer.max_attempts:
        run.story.append(f"Checks failed on attempt {attempts}:\n{util.tail(b.report, 2500)}")
        attempts += 1
        b.resume(f"fix{attempts}", prompt("fix", attempt=attempts, max_attempts=layer.max_attempts, failures=b.report))
        ok = b.check()
    run.save("checks.md", b.report)
    state.update_run(run.id, attempts=attempts)
    if not ok:
        run.story.append("Checks still failing after the last attempt.")
        _learn(cfg, run, ws, layer.expertise_file, t.title, expertise, apply=False)
        return _fail(run, f"checks still failing after {attempts} attempts")

    b.require_valid_diff("the build and its fixes", f" (the plan estimated {estimate})" if estimate else "")

    verified = verify_in_use(b, goal, build_summary)
    review = _review(b, plan_text, verified)
    if review.get("verdict") == "request_changes":
        problems = "\n".join(f"- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in review.get("problems", []))
        run.story.append(f"Review requested changes:\n{problems}")
        b.resume("review_fix", prompt("review_fix", problems=problems))
        b.recheck("the review fixes")
        verified = verify_in_use(b, goal, build_summary)
        review = _review(b, plan_text, verified, "review2")

    approved = review.get("verdict") == "approve"
    blocked = verified.verdict == "blocked"
    _learn(cfg, run, ws, layer.expertise_file, t.title, expertise, apply=True)
    staged = b.restage()
    size = f"{len(staged.files)} files, {staged.lines} lines"
    outcome = f"{size}, review {review.get('verdict')}, in use {verified.verdict}"

    if run.dry_run:
        run.save("diff.patch", staged.diff)
        run.log(f"dry run: {outcome}; worktree kept at {ws}")
        return "dry-run", outcome, ""

    subject = fill_template(layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    problems = review.get("problems", [])
    images = evidence_images(run, staged.changed[0], verified)
    report_body = (
        f"## Summary\n\n{build_summary.strip()[:3000]}\n\n"
        f"## Checked in use\n\n{verified.markdown(images)}\n\n"
        f"## Review\n\n**{review.get('verdict', 'none')}**: {review.get('summary', '')}\n"
        + ("".join(f"\n- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in problems) if problems else "")
        + f"\n\n## Checks\n\nAll passed after {util.count(attempts, 'attempt')}: {checks}\n\n"
        + ("" if run.feedback else f"<details><summary>Plan</summary>\n\n{plan_text[:6000]}\n\n</details>\n\n")
        + f"Run `{run.id}`, {size}, ${run.cost:.2f}."
    )
    revised = "Revised for the review feedback on the PR.\n\n" if run.feedback else ""
    message = f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}"
    commit_and_push(checkouts, staged.changed, branch, message)
    opened: list[Checkout] = []
    for c in staged.changed:
        if not c.pr:
            c.pr = gh.pr_create(c.github, branch, c.base, tk.pr_title(subject, t), f"{tk.pr_link(t)}\n\n{report_body}",
                                labels=tk.pr_labels, draft=not approved or blocked, cwd=c.wt)
            opened.append(c)
            run.log(f"opened {c.pr}{'' if approved and not blocked else ' as draft'}")
        else:
            run.log(f"pushed to {c.pr}")
    pushed = [c for c in staged.changed if c not in opened]
    with_prs = [c for c in checkouts if c.pr]
    for c in with_prs:
        others = sibling_section(t.ref, c, with_prs)
        if c in opened:
            if others:
                gh.pr_edit_body(c.pr, f"{tk.pr_link(t)}\n\n{report_body}{others}")
        elif opened:
            gh.pr_edit_body(c.pr, with_siblings(gh.pr_body(c.pr), others))
        if c in pushed and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Pushed more work for {t.ref}.\n\n{report_body}")
    ci_result = watch_ci(b, branch, staged.changed, with_prs)
    outcome += f", CI: {ci_result}"
    run.enter("pr")
    pr_lines = "\n".join(f"- {'Opened' if c in opened else 'Pushed to' if c in pushed else 'Unchanged'} [{c.pr_name}]({c.pr})"
                         for c in with_prs)
    if run.feedback:
        answer_feedback(run, replies, "Done in the latest push.")
        run.say(f"Revised for your review.\n\n{report_body}\n\nCI: {ci_result}.")
    else:
        run.say(f"PRs for this ticket:\n\n{pr_lines}\n\nCI: {ci_result}."
                + ("" if not opened else draft_note(approved, verified)))
    if approved and not blocked and layer.trust_level == "automerge":
        for c in opened:
            try:
                gh.pr_merge_auto(c.github, gh.pr_number(c.pr))
                run.log(f"automerge enabled on {c.pr}")
            except gh.GhError as e:
                run.log(f"automerge failed on {c.pr}: {e}")
    urls = [c.pr for c in with_prs]
    supersede_older_runs(run, urls)
    state.update_run(run.id, branch=branch)
    remove_workspace(ws, checkouts)
    return "pr", outcome, " ".join(urls)


def commit_and_push(checkouts: list[Checkout], changed: list[Checkout], branch: str, message: str) -> None:
    for c in changed:
        try:
            git(["commit", "-q", "-m", message], c.wt)
        except RuntimeError as e:
            raise RunFailed(f"the commit in {c.github} failed, so arete pushed nothing", str(e)) from e
    moved = [c.github for c in checkouts if remote_head(c.src, branch) != c.lease]
    if moved:
        raise RunFailed(f"`{branch}` moved on {', '.join(moved)} while this run worked, so arete pushed nothing",
                        "Someone pushed to that branch after this run started. A retry builds on top of their work.")
    pushed: list[Checkout] = []
    for c in changed:
        try:
            git(["push", "-q", f"--force-with-lease={branch}:{c.lease}", "-u", "origin", branch], c.wt, timeout=600)
        except RuntimeError as e:
            done = ", ".join(p.github for p in pushed) or "nothing"
            raise RunFailed(f"the push to {c.github} failed after arete pushed {done}",
                            f"{e}\n\nA retry builds on whatever reached the branch.") from e
        c.lease = git(["rev-parse", "HEAD"], c.wt).strip()
        pushed.append(c)


def watch_ci(b: Build, branch: str, pushed: list[Checkout], with_prs: list[Checkout]) -> str:
    rounds: list[ci.Round] = []
    most = b.layer.max_attempts
    try:
        result = ci_rounds(b, branch, pushed, rounds)
    except RunFailed as e:
        drafted = [c for c in with_prs if to_draft(b.run, c)]
        if rounds:
            now = drafted_note(drafted, with_prs)
            publish_ci(b.run, pushed, ci.section(rounds, most, f"Arete stopped: {e.why}. {now}"))
        raise
    publish_ci(b.run, pushed, ci.section(rounds, most))
    return result


def drafted_note(drafted: list[Checkout], with_prs: list[Checkout]) -> str:
    if drafted != with_prs:
        return "Arete could not turn every PR to a draft."
    return "The PR is now a draft." if len(with_prs) == 1 else "The PRs are now drafts."


def to_draft(run: AgentRun, c: Checkout) -> bool:
    try:
        gh.pr_draft(c.pr)
    except gh.GhError as e:
        run.log(f"ci: could not turn {c.pr} to a draft: {util.first_line(str(e), 300)}")
        return False
    run.log(f"ci: turned {c.pr} to a draft")
    return True


def ci_rounds(b: Build, branch: str, pushed: list[Checkout], rounds: list[ci.Round]) -> str:
    run, most = b.run, b.layer.max_attempts
    targets = {c.github: ci_target(c) for c in pushed}
    cleared: dict[tuple[str, str, str], str] = {}
    for n in range(1, most + 1):
        current = ci.Round(n, [ci.compare_with_base(ci.watch(t, run.log), run.log) for t in targets.values()])
        rounds.append(current)
        judged = [r for r in current.reds if not r.red_on_base]
        for r in judged:
            if r.key in cleared:
                r.caused, r.reason = False, cleared[r.key]
        fresh = [r for r in judged if r.key not in cleared]
        fix_summary = judge_ci(b, fresh, n) if fresh else ""
        cleared.update({r.key: r.reason for r in judged if not r.caused})
        caused = current.caused
        if not caused:
            if fresh:
                discard_edits(b)
            return current.summary()
        names = ", ".join(r.label for r in caused)
        if n == most:
            raise RunFailed(f"CI still fails after {util.count(most, 'round')}: {names}", ci_detail(caused))
        if not b.restage().files:
            raise RunFailed(f"the CI fix step said the PR caused {names} but changed nothing", ci_detail(caused))
        b.recheck("the CI fix")
        commit_and_push(b.checkouts, b.staged.changed, branch, ci_fix_message(b, caused))
        for c in b.staged.changed:
            targets[c.github] = ci_target(c)
        current.story = f"{names} failed, caused by this PR, and arete pushed a fix: {fix_summary}".rstrip(": ")
        run.log(f"ci: pushed a fix for {names}")
    raise AssertionError("the CI loop returns or raises in its last round")


def ci_target(c: Checkout) -> ci.Target:
    return ci.Target(c.github, c.pr, c.pr_name, git(["rev-parse", "HEAD"], c.wt).strip(), c.base)


def judge_ci(b: Build, reds: list[ci.Red], n: int) -> str:
    run = b.run
    logs = run.dir / "ci"
    logs.mkdir(exist_ok=True)
    if logs not in run.add_dirs:
        run.add_dirs.append(logs)
    failures = "\n\n".join(ci_failure(r, logs / f"round{n}-{slug(r.label)}.log") for r in reds)
    diff = ", ".join(f"`git {f'-C {c.dir} ' if c.dir else ''}diff origin/{c.base}...HEAD`" for c in b.checkouts)
    most = b.layer.max_attempts
    when_caused = (CI_FIX_LAST_ROUND if n == most else
                   f"fix the cause in the code. Do not weaken, skip, or delete tests, and do not change the CI setup. "
                   f"Run the checks yourself before you finish: {check_list(b.layer)}")
    res = b.resume(b.next_step("ci_fix"), prompt("ci_fix", round=n, max_rounds=most, failures=failures, diff=diff,
                                                 when_caused=when_caused), json_schema=CI_FIX_SCHEMA)
    found = res.structured if res.ok and isinstance(res.structured, dict) else {}
    verdicts = {plain(v.get("name", "")): v for v in found.get("checks", []) if isinstance(v, dict)}
    for r in reds:
        v = verdicts.get(plain(r.label)) or verdicts.get(plain(r.check.name)) or {}
        r.caused = v.get("caused_by_pr") is not False
        r.reason = ci.clip(v.get("reason") or "The fix step gave no verdict for this check.", MAX_CI_REASON_CHARS)
        run.log(f"ci: {r.label} {'caused' if r.caused else 'not caused'} by the PR: {util.first_line(r.reason, 200)}")
    return ci.clip(found.get("summary", ""), MAX_CI_REASON_CHARS)


def plain(name: str) -> str:
    return " ".join(name.lower().split())


def ci_failure(r: ci.Red, log_file: Path) -> str:
    lines = [f"### {r.label}", "", f"- Link: {r.check.link or 'none'}", f"- On the base: {r.base_result}"]
    if r.check.about:
        lines.append(f"- GitHub says: {r.check.about}")
    if not r.check.job:
        return "\n".join([*lines, "- This check has no Actions log. Judge it from its name, what GitHub says, and the diff."])
    try:
        text = gh.job_log(r.target.github, r.check.job)
    except gh.GhError as e:
        return "\n".join([*lines, f"- Arete could not read its log: {util.first_line(str(e), 300)}"])
    log_file.write_text(text)
    return "\n".join([*lines, f"- Full log: `{log_file}`", "", "The log up to its last error:", "", "````",
                      ci.excerpt(text), "````"])


def discard_edits(b: Build) -> None:
    for c in b.restage().changed:
        git(["reset", "-q", "--hard"], c.wt)
        b.run.log(f"ci: threw away the fix step's edits in {c.github}, since the PR caused no failure")


def ci_fix_message(b: Build, caused: list[ci.Red]) -> str:
    names = ", ".join(dict.fromkeys(r.check.name for r in caused))
    subject = fill_template(b.layer.subject, config.RepoLayer.subject, kind="fix", id=b.ticket.id,
                            title=f"make {names} pass in CI")
    return f"{tracker.with_ref(subject, b.ticket.ref)}\n\nArete-Run: {b.run.id}"


def ci_detail(reds: list[ci.Red]) -> str:
    return "### CI\n\n" + "\n".join(f"- [{r.target.pr_name}]({r.target.pr}): {ci.linked(r.check)}. {r.reason}".rstrip()
                                     for r in reds)


def publish_ci(run: AgentRun, pushed: list[Checkout], ci_section: str) -> None:
    for c in pushed:
        try:
            gh.pr_edit_body(c.pr, ci.with_section(gh.pr_body(c.pr), ci_section))
        except gh.GhError as e:
            run.log(f"ci: could not write the CI section on {c.pr}: {util.first_line(str(e), 300)}")


def images_note(images: tracker.SavedImages) -> str:
    lines = []
    if images.paths:
        lines += ["## Images on the issue", "", "Open each one with Read; they show what the issue describes.", ""]
        lines += [f"- `{p}`" for p in images.paths]
    if images.left_out:
        lines += ["", (f"Not downloaded, over the {tracker.MAX_IMAGES} image or "
                       f"{tracker.MAX_IMAGE_BYTES // 2**20} MB cap: {', '.join(images.left_out)}")]
    return "\n".join(lines).strip()


def open_branch(run: Run) -> str:
    return next((r["branch"] for r in state.open_runs(run.repo.name, run.tid) if r.get("branch")), "")


def with_siblings(body: str, section: str) -> str:
    return SIBLINGS.sub("", body).rstrip() + section


def sibling_section(ref: str, c: Checkout, with_prs: list[Checkout]) -> str:
    others = [o for o in with_prs if o is not c]
    if not others:
        return ""
    return f"\n\n## Other PRs for {ref}\n\n" + "\n".join(f"- [{o.pr_name}]({o.pr})" for o in others)


def supersede_older_runs(run: Run, urls: list[str]) -> None:
    for r in state.open_runs(run.repo.name, run.tid):
        older = (r.get("pr_url") or "").split()
        if older and set(older) <= set(urls):
            state.update_run(r["id"], status="superseded")
            run.log(f"run {r['id']} superseded: its PRs carry on in this run")


def pending_feedback(repo_name: str, tid: str) -> list[dict]:
    return state.open_feedback([url for r in state.open_runs(repo_name, tid) for url in (r.get("pr_url") or "").split()])


def feedback_markdown(rows: list[dict]) -> str:
    out = []
    for url in dict.fromkeys(f["pr_url"] for f in rows):
        out.append(f"### On {url}")
        for f in (f for f in rows if f["pr_url"] == url):
            if f["kind"] == "line":
                where = f"`{f['path']}`" + (f" line {f['line']}" if f.get("line") else "")
                in_thread = f", a reply in thread {f['thread']}" if f["thread"] != f["gh_id"] else ""
                out.append(f"#### Line comment {f['gh_id']} on {where}{in_thread}\n\n```diff\n{f['hunk']}\n```\n\n"
                           f"{f['body'].strip()}")
            elif f["kind"] == "review":
                out.append(f"#### Review summary\n\n{f['body'].strip()}")
            else:
                out.append(f"#### Comment on the PR\n\n{f['body'].strip()}")
    return "\n\n".join(out)


def answer_feedback(run: Run, replies: list[dict], fallback: str) -> None:
    said = {r["comment_id"]: r["reply"].strip() for r in replies if r.get("reply", "").strip()}
    threads: dict[tuple[str, int], str] = {}
    for f in run.feedback:
        if f["kind"] == "line":
            key = (f["pr_url"], f["thread"])
            threads[key] = said.get(f["gh_id"]) or said.get(f["thread"]) or threads.get(key) or fallback
    for (url, thread), text in threads.items():
        try:
            gh.pr_reply(url, thread, text)
        except gh.GhError as e:
            run.log(f"reply to comment {thread} on {url} failed: {util.first_line(str(e), 300)}")


def answer_without_changes(run: Run, ws: Path, checkouts: list[Checkout], branch: str, summary: str,
                           replies: list[dict]) -> tuple[str, str, str]:
    run.log("revise: no code changes, answering the review")
    answer_feedback(run, replies, "No code change for this one; see the overall reply.")
    run.say(f"No code changes for your review.\n\n{summary.strip()[:3000]}")
    run.enter("pr")
    urls = [c.pr for c in checkouts if c.pr]
    supersede_older_runs(run, urls)
    state.update_run(run.id, branch=branch)
    remove_workspace(ws, checkouts)
    return "pr", "answered the review without code changes", " ".join(urls)


def _review(b: Build, plan: str, verified: verify.Verified, step: str = "review") -> dict:
    t = b.ticket
    res = b.run.agent(step, prompt("review", repo=b.run.repo.name, workspace=b.workspace, ref=t.ref, title=t.title,
                                   body=t.body, comments=t.comments, attachments=b.attachments, plan=plan,
                                   checks=b.report, evidence=verified.markdown(),
                                   diff=util.tail(b.staged.diff, MAX_DIFF_IN_PROMPT)),
                      b.ws, model=b.cfg.model("review"), allowed_tools=READ_TOOLS + bash_rules(b.read_bash),
                      json_schema=REVIEW_SCHEMA, timeout=b.timeout)
    if not res.ok or not isinstance(res.structured, dict):
        return {"verdict": "request_changes", "summary": "review step failed", "problems": []}
    return res.structured


def verify_in_use(b: Build, goal: str, summary: str) -> verify.Verified:
    if not b.layer.verify:
        b.run.log("verify: the layer has no [verify] section, so nothing checks the change in use")
        return verify.not_configured(b.layer.dir, b.ws)
    for attempt in range(1, b.layer.max_attempts + 1):
        verified = check_in_use(b, goal, summary)
        if verified.verdict != "fail":
            return verified
        b.run.story.append(f"The check in use failed on attempt {attempt}:\n{util.tail(verified.markdown(), 2500)}")
        if attempt == b.layer.max_attempts:
            break
        b.resume(b.next_step("verify_fix"), prompt("verify_fix", attempt=attempt + 1, max_attempts=b.layer.max_attempts,
                                                   failures=verified.markdown(), checks=check_list(b.layer)))
        b.recheck("fixing what the check in use found")
    b.run.story.append("Still failing in use after the last attempt.")
    _learn(b.cfg, b.run, b.ws, b.layer.expertise_file, b.ticket.title, b.expertise, apply=False)
    raise RunFailed(f"it still does not work in use after {util.count(b.layer.max_attempts, 'attempt')}",
                    verified.markdown())


def check_in_use(b: Build, goal: str, summary: str) -> verify.Verified:
    return use_product(b, "verify", "verify", goal=goal, summary=summary.strip()[:4000],
                       diff=util.tail(b.staged.diff, MAX_DIFF_IN_PROMPT))


def use_product(b: TicketWork, step_name: str, prompt_name: str, **values) -> verify.Verified:
    run, v, t = b.run, verify.with_free_ports(b.layer.verify), b.ticket
    step = b.next_step(step_name)
    evidence = run.dir / "evidence"
    evidence.mkdir(exist_ok=True)
    if evidence not in run.add_dirs:
        run.add_dirs.append(evidence)
    bash = b.read_bash + v.bash
    untracked_before = untracked_by_dir(b.git_dirs)
    try:
        with verify.serving(v, b.ws, run.dir / f"{step}-server.log") as url:
            if url:
                run.log(f"{step}: the app answers at {url}")
            res = run.agent(step, prompt(prompt_name, repo=run.repo.name, workspace=b.workspace, ref=t.ref,
                                         title=t.title, body=t.body, comments=t.comments, attachments=b.attachments,
                                         surface=surface_note(v, url, evidence), notes=v.notes,
                                         commands=code_list(bash), checks=check_list(b.layer), **values),
                            b.ws, model=b.cfg.model("verify"),
                            allowed_tools=READ_TOOLS + bash_rules(bash) + (["mcp__playwright"] if url else []),
                            disallowed_tools=NO_GIT_WRITES, json_schema=verify.SCHEMA, timeout=b.timeout,
                            mcp_config=verify.mcp_config(evidence) if url else "")
    except verify.ServerError as e:
        run.log(f"{step}: the app did not start")
        return verify.Verified("fail", f"The app did not start: {e}")
    finally:
        restore_worktrees(untracked_before, evidence)
    if not res.ok or not isinstance(res.structured, dict):
        return verify.Verified("blocked", "The check in use did not finish.", finished=False,
                               blocked_by=util.first_line(res.error or "the verifier returned no verdict", 300))
    verified = verify.Verified.from_agent(res.structured)
    run.save(f"{step}.md", verified.markdown())
    run.log(f"{step}: {verified.verdict}, {len(verified.checks) - len(verified.failed_checks)} of "
            f"{util.count(len(verified.checks), 'check')} passed")
    return verified


def surface_note(v: config.Verify, url: str, evidence: Path) -> str:
    if not url:
        return ("Arete started no app for this repo. Run the commands its user runs, with real arguments, and read "
                "the output and the exit code.")
    return (f"Arete started the app with `{v.start}` and it answers at {url}. Arete stops it when you finish.\n\n"
            "- Do in the browser what a person does in the browser, with the `mcp__playwright__browser_*` tools "
            "(load them with ToolSearch first): navigate, read the page with `browser_snapshot`, click, type.\n"
            "- After each state you check, call `browser_take_screenshot` with `filename` set to an absolute path "
            f"in `{evidence}`, such as `{evidence}/01-list.png`, and read `browser_console_messages` for errors.\n"
            "- Send API requests with curl, shaped the way the app's clients send them, and read the status and body.")


def restore_worktrees(untracked_before: dict[Path, set[str]], evidence: Path) -> None:
    for d, name in untracked_added_since(untracked_before):
        f = d / name
        if f.suffix == ".png" and not (evidence / f.name).exists():
            shutil.move(f, evidence / f.name)
        else:
            remove_untracked(d, name)
    for d in untracked_before:
        git(["checkout", "-q", "--", "."], d)


def evidence_images(run: Run, c: Checkout, verified: verify.Verified) -> dict[str, str]:
    shots = verified.screenshots(run.dir / "evidence")
    if not shots:
        return {}
    try:
        with util.repo_lock(c.src):
            images = verify.push_evidence(c.src, c.github, run.id, shots)
    except RuntimeError as e:
        run.log(f"evidence: the push failed, so the PR shows no screenshots: {util.first_line(str(e), 300)}")
        return {}
    run.log(f"evidence: pushed {util.count(len(shots), 'screenshot')} to {verify.EVIDENCE_BRANCH} on {c.github}")
    return images


def draft_note(approved: bool, verified: verify.Verified) -> str:
    if verified.verdict == "blocked":
        return f"\n\nThe new PRs are drafts: the check in use was blocked. {verified.blocked_by}".rstrip()
    if not approved:
        return "\n\nThe new PRs are drafts: the reviewer still has concerns."
    return ""


def check_list(layer: config.RepoLayer) -> str:
    return ", ".join(f"`{c}`" for c in layer.checks.values()) or "(none configured)"


def _learn(cfg, run: AgentRun, wt: Path, expertise_file: Path, title: str, expertise: str, apply: bool) -> None:
    if not run.story:
        run.log("learn: clean run, nothing to learn")
        return
    res = run.agent("learn", prompt("learn", repo=run.repo.name, ref=run.ref, title=title,
                                     story="\n\n".join(run.story), expertise=expertise,
                                     max_lines=EXPERTISE_MAX_LINES),
                    wt, model=cfg.model("learn"), allowed_tools=READ_TOOLS, json_schema=LEARN_SCHEMA,
                    timeout=900, persist=False)
    found = res.structured if res.ok and isinstance(res.structured, dict) else {}
    rewrite = found.get("expertise", "").strip()
    lines = len(rewrite.splitlines())
    if not rewrite:
        run.log("learn: no expertise file came back, kept the old one")
        return
    if lines > EXPERTISE_MAX_LINES:
        run.log(f"learn: the rewrite has {lines} lines, over the {EXPERTISE_MAX_LINES} line cap; kept the old file")
        return
    added = [a.strip() for a in found.get("added", []) if a.strip()]
    removed = [r for r in found.get("removed", []) if r.get("lesson", "").strip()]
    if (expertise_file.read_text().strip() if expertise_file.exists() else "") == rewrite:
        run.log("learn: expertise unchanged")
        return
    report = "\n".join([f"+ {a}" for a in added] + [f"- {r['lesson']} ({r['why']})" for r in removed])
    run.save("expertise-report.md", report)
    if not apply:
        run.save("expertise.md", rewrite + "\n")
        run.log(f"learn: rewrite saved in the run, not applied ({len(added)} added, {len(removed)} removed)")
        return
    expertise_file.parent.mkdir(parents=True, exist_ok=True)
    expertise_file.write_text(rewrite + "\n")
    run.log(f"learn: {len(added)} added, {len(removed)} removed, {lines} lines in {paths.short_path(expertise_file)}")
    if run.repo.layer:
        try:
            commit_layer(expertise_file.parent, f"{run.repo.name}: learn from {run.ref} ({run.id})\n\n{report}")
        except RuntimeError as e:
            run.log(f"learn: wrote the file but the layer commit failed: {util.first_line(str(e), 300)}")


def commit_layer(layer_dir: Path, message: str) -> None:
    if git(["rev-parse", "--is-inside-work-tree"], layer_dir, check=False).strip() != "true":
        git(["init", "-q"], layer_dir)
    git(["add", "-A", "--", "."], layer_dir)
    if util.sh(["git", "diff", "--cached", "--quiet", "--", "."], cwd=layer_dir).returncode:
        git(["commit", "-q", "-m", message, "--", "."], layer_dir)


def owner_feedback(url: str, pr_state: str, repo_name: str, issue: str) -> list[dict]:
    owner = gh.me()
    talk = gh.pr_discussion(url)

    def from_owner(item: dict) -> bool:
        return (item.get("user") or {}).get("login") == owner and not gh.has_arete_mark(item.get("body"))

    reviews = {r["id"]: r for r in talk.reviews}
    lines_in_review = Counter(c.get("pull_request_review_id") for c in talk.line_comments)
    base = {"pr_url": url, "repo": repo_name, "issue": issue, "pr_state": pr_state,
            "status": "new" if pr_state == "OPEN" else "recorded"}
    rows = [{**base, "kind": "review", "gh_id": r["id"], "body": r["body"], "at_once": 1, "created": r.get("submitted_at")}
            for r in talk.reviews if from_owner(r) and r.get("state") != "PENDING" and (r.get("body") or "").strip()]
    for c in talk.line_comments:
        review = reviews.get(c.get("pull_request_review_id")) or {}
        if not from_owner(c) or review.get("state") == "PENDING":
            continue
        grouped = bool((review.get("body") or "").strip()) or lines_in_review[c.get("pull_request_review_id")] > 1
        rows.append({**base, "kind": "line", "gh_id": c["id"], "body": c.get("body") or "", "path": c.get("path"),
                     "line": c.get("line") or c.get("original_line"),
                     "hunk": "\n".join((c.get("diff_hunk") or "").splitlines()[-HUNK_LINES:]),
                     "thread": c.get("in_reply_to_id") or c["id"], "at_once": int(grouped),
                     "created": c.get("created_at")})
    rows += [{**base, "kind": "comment", "gh_id": c["id"], "body": c.get("body") or "", "at_once": 0,
              "created": c.get("created_at")} for c in talk.comments if from_owner(c)]
    return rows


def record_feedback(r: dict, urls: list[str], prs: list[dict], seen: dict, fresh: dict) -> None:
    for url, pr in zip(urls, prs):
        updated = pr.get("updatedAt", "")
        fresh[url] = seen.get(url, "")
        if updated and seen.get(url) == updated:
            continue
        try:
            added = state.add_feedback(owner_feedback(url, pr.get("state", ""), r["repo"], str(r["issue"])))
        except gh.GhError as e:
            print(f"{util.iso()} {url}: could not read the review comments: {util.first_line(str(e), 300)}")
            continue
        fresh[url] = updated if util.iso(util.now() - PR_SEEN_SETTLE) > updated else ""
        if added:
            print(f"{util.iso()} {url}: {util.count(added, 'new comment')} from you")


def reads_pr_comments(r: dict) -> bool:
    if not r.get("pr_url"):
        return False
    rejected_lately = r["status"] == "rejected" and (r.get("finished") or "") >= util.iso(util.now() - REJECTED_PR_LISTEN)
    return r["status"] == "pr" or rejected_lately


def sync_outcomes(cfg: config.Config) -> None:
    seen, fresh = util.read_json(PR_SEEN_FILE, {}), {}
    for r in state.runs(kind="pipeline", limit=200):
        if not reads_pr_comments(r):
            continue
        urls = r["pr_url"].split()
        try:
            prs = [gh.pr_view(url) for url in urls]
        except gh.GhError:
            continue
        record_feedback(r, urls, prs, seen, fresh)
        closed = all(pr.get("state") in ("MERGED", "CLOSED") for pr in prs)
        if r["status"] == "rejected":
            if not closed:
                state.update_run(r["id"], status="pr")
            continue
        if not closed:
            continue
        for url, pr in zip(urls, prs):
            state.close_feedback(url, pr["state"])
        merged = all(pr.get("mergedAt") for pr in prs)
        closed_at = max(pr.get("closedAt") or "" for pr in prs) or util.iso()
        state.update_run(r["id"], status="merged" if merged else "rejected", finished=closed_at)
    util.write_json(PR_SEEN_FILE, fresh)
    for repo in cfg.repos:
        if repo.pipeline and repo.github and not repo.layer:
            try:
                _maybe_propose_trust(repo)
            except (RuntimeError, OSError) as e:
                print(f"{util.iso()} {repo.name}: trust proposal failed: {util.first_line(str(e), 300)}")


def sync_landings(cfg: config.Config) -> None:
    branches = list(cfg.jira.landed)
    if not branches:
        return
    for r in state.runs(kind="pipeline", limit=200):
        if r["status"] != "merged" or not r.get("pr_url") or r.get("landed") == branches[-1]:
            continue
        try:
            repo = cfg.repo(r["repo"])
        except KeyError:
            continue
        if repo.tracker != "jira":
            continue
        try:
            reached = furthest_branch(branches, r["pr_url"].split(), str(r["issue"]))
            already = branches.index(r["landed"]) if r.get("landed") in branches else -1
            if reached is None or branches.index(reached) <= already:
                continue
            target = cfg.jira.landed[reached]
            tk = tracker.for_repo(cfg, repo)
            err = tk.move_to(str(r["issue"]), target)
            if reached != branches[-1]:
                run = Run(r["id"], repo, paths.RUNS_DIR / r["id"], tracker=tk, tid=str(r["issue"]))
                moved = "" if err else f" and moved to {target}"
                if say_err := run.say(f"This reached `{reached}`{moved}. To test it there:\n\n"
                                      + uat_steps(cfg, run, r, reached)):
                    raise RetryNextPoll(say_err)
        except (gh.GhError, jira.JiraError, RetryNextPoll) as e:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: landing check failed, trying next poll: {util.first_line(str(e), 300)}")
            continue
        state.update_run(r["id"], landed=reached)
        print(f"{util.iso()} {tracker.run_ticket_name(r)}: landed on {reached}, " + (err or f"moved to {target}"))


def uat_steps(cfg: config.Config, run: Run, row: dict, branch: str) -> str:
    saved = run.dir / f"uat-{branch}.md"
    if saved.exists():
        return saved.read_text()
    run.dir.mkdir(parents=True, exist_ok=True)
    tries_file = run.dir / f"uat-{branch}.tries"
    tries = int(tries_file.read_text() or 0) + 1 if tries_file.exists() else 1
    t = run.tracker.get(run.tid)
    urls = row["pr_url"].split()
    share = MAX_DIFF_IN_PROMPT // len(urls)
    prs = "\n\n".join(f"### {url}\n\n{gh.pr_body(url).strip()}\n\n```diff\n{cut(gh.pr_diff(url), share)}\n```"
                       for url in urls)
    res = run.agent("uat", prompt("uat", ref=t.ref, title=t.title, ticket="\n\n".join(filter(None, [t.body, t.comments])),
                                  prs=prs, branch=branch),
                    run.repo.path, model=cfg.model("uat"), allowed_tools=READ_TOOLS, json_schema=UAT_SCHEMA,
                    timeout=600, persist=False)
    state.update_run(run.id, cost_usd=(row.get("cost_usd") or 0) + run.cost)
    found = res.structured if res.ok and isinstance(res.structured, dict) else {}
    steps = [re.sub(r"^\s*\d+[.)]\s*", "", s).strip() for s in found.get("steps", []) if s.strip()]
    failure = util.first_line(res.error or "none came back")
    if steps:
        before = found.get("before", "").strip()
        text = (f"**Before you start:** {before}\n\n" if before else "") + "\n".join(
            f"{i}. {s}" for i, s in enumerate(steps, 1))
    elif tries < UAT_TRIES:
        tries_file.write_text(str(tries))
        raise RetryNextPoll(f"no test steps came back, try {tries} of {UAT_TRIES}: {failure}")
    else:
        text = (f"I could not write the steps ({failure}); "
                f"the PR shows what changed: {' '.join(urls)}")
    saved.write_text(text)
    return text


def cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n... (cut)"


def furthest_branch(branches: list[str], pr_urls: list[str], key: str) -> str | None:
    merges = [(gh.pr_repo(url), (gh.pr_view(url).get("mergeCommit") or {}).get("oid", "")) for url in pr_urls]
    for branch in reversed(branches):
        if all(branch_holds_work(repo, branch, sha, key) for repo, sha in merges):
            return branch
    return None


def branch_holds_work(repo: str, branch: str, merge_sha: str, key: str) -> bool:
    return bool(merge_sha) and gh.branch_contains(repo, branch, merge_sha) or gh.merged_pr_names(repo, branch, key)


def streak(repo_name: str) -> int:
    n = 0
    for r in state.runs(kind="pipeline", repo=repo_name, limit=100):
        if r["status"] == "merged":
            n += 1
        elif r["status"] in ("rejected", "failed"):
            break
    return n


def layer_is_committed(root: Path) -> bool:
    return bool(git(["ls-files", ".agents/config.toml"], root, check=False).strip())


def _maybe_propose_trust(repo: config.Repo) -> None:
    layer = config.load_layer(repo.layer_dir)
    if layer.trust_level != "pr" or streak(repo.name) < layer.automerge_after or not layer_is_committed(repo.path):
        return
    branch = "arete/trust-automerge"
    if gh.open_pr_url(repo.github, branch):
        return
    base = layer.base_branch or gh.default_branch(repo.github)
    labels = ["arete-trust"] if repo.tracker == "github" else []
    wt = make_worktree(repo, base, branch, "trust")
    try:
        f = wt / ".agents" / "config.toml"
        if not f.exists():
            return
        text = f.read_text()
        new = re.sub(r'(?m)^(\s*level\s*=\s*)"pr"', r'\1"automerge"', text)
        if new == text:
            return
        f.write_text(new)
        git(["commit", "-q", "-am", "chore(agents): raise trust to automerge"], wt)
        git(["push", "-q", "--force-with-lease", "-u", "origin", branch], wt)
        for lb in labels:
            gh.ensure_label(repo.github, lb, "0e8a16", "Trust level change proposed by arete")
        n = streak(repo.name)
        gh.pr_create(repo.github, branch, base, "chore(agents): raise trust to automerge",
                     f"The last {n} arete PRs in this repo merged with none rejected or failed in between.\n\n"
                     "Merging this lets arete enable automerge on PRs that pass checks and review. "
                     "Close it to keep every PR waiting for you.", labels=labels)
    finally:
        remove_worktree(repo, wt)


def cleanup_worktrees(days: float = 7) -> None:
    cutoff = util.now().timestamp() - days * 86400
    for d in paths.WORKTREES_DIR.glob("*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


class SkipLog:
    def __init__(self, before: dict):
        self.before = before
        self.current: dict[str, dict] = {}

    def note(self, name: str, t: tracker.Ticket, blocker: str) -> None:
        old = self.before.get(name) or {}
        same = old.get("why") == blocker
        self.current[name] = {"why": blocker, "title": t.title, "url": t.url,
                          "since": old.get("since") if same else util.iso()}
        if not same:
            print(f"{util.iso()} {name}: not run, {blocker}")


STOPPED = "stopped before it finished, with nothing left running"
INTERRUPTED = ("stopped before it finished (the Mac restarted, lost power or the process was killed); "
               "I will start it again")


def interrupted_twice(row: dict) -> bool:
    earlier = [r for r in state.ticket_runs(row["repo"], row["issue"], limit=3) if r["id"] != row["id"]]
    return bool(earlier) and earlier[0]["status"] == "interrupted"


def settle_abandoned_runs(cfg: config.Config) -> None:
    for row in state.runs_marked_running():
        with util.lock(state.run_lock(row["kind"], row["repo"], row["issue"])) as free:
            if not free:
                continue
            if row["kind"] != "pipeline":
                state.finish_run(row["id"], "failed", summary=STOPPED)
                print(f"{util.iso()} {row['id']}: {STOPPED}")
                continue
            repo = cfg.repo(row["repo"])
            run = Run(row["id"], repo, paths.RUNS_DIR / row["id"], tracker=tracker.for_repo(cfg, repo),
                      tid=str(row["issue"]), feedback=pending_feedback(repo.name, str(row["issue"])))
            run.dir.mkdir(parents=True, exist_ok=True)
            if interrupted_twice(row):
                state.finish_run(row["id"], "failed", summary=STOPPED)
                run.log(f"{STOPPED}, the second interruption in a row")
                run.give_up(f"{STOPPED}, the second interruption in a row")
            else:
                state.finish_run(row["id"], "interrupted", summary=INTERRUPTED)
                run.log(INTERRUPTED)
                run.leave("running")
                run.say(f"Run `{run.id}` {INTERRUPTED}.")


def live_pipeline_runs() -> list[dict]:
    live = []
    for row in state.runs_marked_running():
        if row["kind"] != "pipeline":
            continue
        with util.lock(state.run_lock("pipeline", row["repo"], row["issue"])) as free:
            if not free:
                live.append(row)
    return live


def spawn_run(repo_name: str, tid: str) -> None:
    with open(paths.LOG_DIR / "runs.log", "a") as log:
        subprocess.Popen([sys.executable, "-m", "arete.cli", "run", repo_name, tid], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def next_ticket(repo: config.Repo, tk: tracker.Tracker, skips: SkipLog,
                other_routes: set[str] = frozenset()) -> tracker.Ticket | None:
    for t in tk.todo():
        if other_routes & set(t.labels) or state.issue_running(repo.name, t.id):
            continue
        if blocker := tk.blocker(t.id):
            skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker)
            continue
        return t
    return None


def jira_routes_by_project(cfg: config.Config) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in cfg.repos:
        if r.pipeline and r.parts and r.tracker == "jira":
            out.setdefault(r.jira_project or cfg.jira.project, set()).add(tracker.route_label(r))
    return out


def next_pick(repo: config.Repo, tk: tracker.Tracker, skips: SkipLog,
              other_routes: set[str]) -> tuple[str, str] | None:
    fresh = next_ticket(repo, tk, skips, other_routes)
    for tid in state.interrupted_tickets(repo.name):
        if not state.issue_running(repo.name, tid):
            return tid, "restarting after an interruption"
    for tid in state.feedback_due(repo.name, util.iso(util.now() - timedelta(minutes=FEEDBACK_QUIET_MINUTES))):
        if not state.issue_running(repo.name, tid):
            return tid, "revising the PR for your review comments"
    for t in tk.replied():
        if state.issue_running(repo.name, t.id):
            continue
        if blocker := tk.blocker(t.id):
            skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker)
            continue
        return t.id, f"{t.title} (you replied to the question)"
    return (fresh.id, fresh.title) if fresh else None


def poll(dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock("poll") as got:
        if not got:
            print(f"{util.iso()} poll already running")
            return 0
        if not network_up_or_skip("poll", dry_run):
            return 0
        errors: list[str] = []

        def failed(where: str, e: Exception) -> None:
            errors.append(f"{where}: {type(e).__name__}: {e}")
            print(f"{util.iso()} {errors[-1]}")

        if not dry_run:
            try:
                settle_abandoned_runs(cfg)
            except Exception as e:
                failed("settling abandoned runs", e)
            try:
                sync_outcomes(cfg)
            except Exception as e:
                failed("syncing PR outcomes", e)
        live = live_pipeline_runs()
        busy = {r["repo"] for r in live}
        slots = cfg.max_parallel - len(live)
        starts = state.last_pipeline_starts()
        pipeline_repos = [r for r in cfg.repos if r.pipeline and r.parts]
        restarting = {r.name for r in pipeline_repos if state.interrupted_tickets(r.name)}
        skips = SkipLog({} if dry_run else util.read_json(SKIPPED_FILE, {}))
        routes = jira_routes_by_project(cfg)
        reached_clients: dict[str, jira.Client] = {}
        for repo in sorted(pipeline_repos, key=lambda r: (r.name not in restarting, starts.get(r.name, ""))):
            try:
                tk = tracker.for_repo(cfg, repo)
                other_routes: set[str] = set()
                if isinstance(tk, tracker.JiraTracker):
                    other_routes = routes.get(tk.project, set()) - {tk.route}
                    reached_clients.setdefault(tk.project, tk.client)
                pick = next_pick(repo, tk, skips, other_routes)
                if not pick:
                    continue
                tid, why = pick
                name = tracker.ticket_name(repo.name, tk.ref(tid))
                if repo.name in busy:
                    print(f"{util.iso()} {name}: waiting, {repo.name} already has a run going")
                elif slots <= 0:
                    print(f"{util.iso()} {name}: waiting, {cfg.max_parallel} of {cfg.max_parallel} runs going")
                else:
                    print(f"{util.iso()} {name}: {why}" + (" (dry run, not started)" if dry_run else ""))
                    if not dry_run:
                        spawn_run(repo.name, tid)
                    busy.add(repo.name)
                    slots -= 1
            except Exception as e:
                failed(repo.name, e)
        for project, client in reached_clients.items():
            try:
                for t in tracker.jira_unrouted(project, routes.get(project, set()), client):
                    skips.note(t.ref, t, t.blocker)
            except Exception as e:
                failed(f"jira {project}", e)
        if not dry_run:
            try:
                util.write_json(SKIPPED_FILE, skips.current)
                sync_landings(cfg)
                cleanup_worktrees()
            except Exception as e:
                failed("poll", e)
            mark_loop("poll", not errors, "; ".join(errors))
        return 1 if errors else 0


def skipped_tickets() -> dict:
    return util.read_json(SKIPPED_FILE, {})
