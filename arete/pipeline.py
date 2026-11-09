from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from string import Template
from typing import TextIO

from . import (
    ci,
    config,
    diffguard,
    gh,
    jev,
    jira,
    paths,
    record,
    runner,
    state,
    tracker,
    util,
    verdicts,
    verify,
)
from .agent import Result, Session
from .heartbeat import mark_loop, network_up_or_skip
from .util import git

MAX_DIFF_IN_PROMPT = 60_000
SIBLINGS = re.compile(r"\n*## Other PRs for [^\n]*\n(?:\n- \[[^\n]*)+")
SKIPPED_FILE = paths.STATE_DIR / "skipped.json"
CONTINUE_AFTER_DROP = "The connection dropped mid step. Carry on from where you stopped and finish the step."
CHECKS_LOCK = "checks"
LEFT_TITLE = "Left for a person"
TRIAGE_ANSWERS = "Answers to triage's questions"
FEEDBACK_ASKS = "Does this comment ask for a change to the pull request, or ask a question the author should answer?"
LEFT_HEADING = f"## {LEFT_TITLE}"
LEFT_START, LEFT_END = "<!-- arete:left -->", "<!-- /arete:left -->"
LEFT_FOR_A_PERSON = re.compile(rf"{re.escape(LEFT_START)}\n{LEFT_HEADING}\n\n(.*?)\n\n{re.escape(LEFT_END)}\n*", re.DOTALL)
MAX_LEFT_FOR_A_PERSON_CHARS = 12_000
LEFT_BY_A_PERSON = "A person makes these changes after the PR, since the builder may not edit those paths."
NOT_CHECKED_LEFT_FOR_A_PERSON = (f"{LEFT_HEADING}\n\n{LEFT_BY_A_PERSON} Do not check them, and do not fail the change "
                                 "because they are missing:\n\n")
NOT_CAUSED_LEFT_FOR_A_PERSON = (f"{LEFT_HEADING}\n\n{LEFT_BY_A_PERSON} A red check that only these changes would fix "
                                "is not caused by the PR: set `caused_by_pr` to false, say in its reason that it waits "
                                "on the change left for a person, and do not make it pass another way.\n\n")
CHECKED_IN_USE_HEADING = "## Checked in use"
SLOTS_LOCK = "run-slots"
FREED_SLOT_FOR_CI = "ci: freed the run slot for the wait"
SLOT_POLL_SECONDS = 30

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "ambiguous": {"type": "boolean"},
        "question": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "estimated_lines": {"type": "integer", "minimum": 0},
        "verify": {"type": "string"},
        "left_for_a_person": {"type": "string"},
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
FEEDBACK_QUIET_MINUTES = 10
HUNK_LINES = 6
PR_SEEN_FILE = paths.STATE_DIR / "pr_seen.json"
PR_SEEN_SETTLE = timedelta(minutes=1)
REJECTED_PR_LISTEN = timedelta(days=7)
UAT_TRIES = 3


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
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        return default.format(**values)


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

    def agent(self, step: str, text: str, cwd: Path, **kw) -> Result:
        self.log(f"{step}: starting")
        kw.setdefault("say", lambda note: self.log(f"{step}: {note}"))
        res = runner.run(text, cwd, add_dirs=self.add_dirs, trace=(self.id, step), **kw)
        if res.connection_failed:
            self.log(f"{step}: lost the connection after {util.count(res.turns, 'turn')}, waiting for the network")
            if util.wait_for_network():
                self.cost += res.cost_usd
                self.log(f"{step}: the network is back, the dropped attempt cost {runner.cost_note(res)}")
                if res.session.id and res.turns > 1 and kw.get("persist", True):
                    res = runner.run(CONTINUE_AFTER_DROP, cwd, add_dirs=self.add_dirs, trace=(self.id, step),
                                     **{**kw, "resume": res.session})
                else:
                    res = runner.run(text, cwd, add_dirs=self.add_dirs, trace=(self.id, step), **kw)
        self.cost += res.cost_usd
        self.save(f"{step}.json", {"ok": res.ok, "session": res.session.id, "cost": res.cost_usd,
                                   "turns": res.turns, "text": res.text, "structured": res.structured,
                                   "error": res.error})
        self.log(f"{step}: {'ok' if res.ok else 'FAILED ' + util.first_line(res.error)} {runner.cost_note(res)}")
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
    merged_base: bool = False

    @property
    def prefix(self) -> str:
        return f"{self.dir}/" if self.dir else ""

    @property
    def starting_point(self) -> str:
        if not self.resumes:
            return f"fresh from `{self.base}`"
        start = (f"continuing the open PR {self.pr}" if self.pr
                 else "continuing commits already pushed to the branch, with no PR yet")
        return start + (f", with `{self.origin_base}` merged in since the base moved after the last push"
                        if self.merged_base else "")

    @property
    def pr_name(self) -> str:
        return f"{self.github.split('/')[-1]}#{gh.pr_number(self.pr)}"

    @property
    def origin_base(self) -> str:
        return f"origin/{self.base}"

    @property
    def diff_from_base(self) -> str:
        return f"git {f'-C {self.dir} ' if self.dir else ''}diff {self.origin_base}...HEAD"


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
        add_worktree(c, branch, f"origin/{branch}" if c.resumes else c.origin_base)
    return ws, checkouts


def make_worktree(repo: config.Repo, base: str, branch: str, key: str) -> Path:
    c = Checkout("", repo.github, repo.path, paths.WORKTREES_DIR / f"{repo.name}-{key}", base)
    remove_workspace(c.wt, [c])
    fetch(c.src, base)
    add_worktree(c, branch, c.origin_base)
    return c.wt


def remove_worktree(repo: config.Repo, wt: Path) -> None:
    remove_workspace(wt, [Checkout("", repo.github, repo.path, wt, "")])


def stale_base_note(c: Checkout) -> str:
    return (f"Compare with `origin/<base>`, as in `{c.diff_from_base}`, never with the local base branch: arete "
            "fetched `origin`, but the local base branch can be days old.")


def workspace_note(branch: str, checkouts: list[Checkout]) -> str:
    if len(checkouts) == 1 and not checkouts[0].dir:
        c = checkouts[0]
        return (f"You are in a worktree on branch `{branch}`, {c.starting_point}. PRs target `{c.base}`. "
                f"{stale_base_note(c)}")
    rows = [f"- `{c.prefix}`: {c.github}, {c.starting_point}; PRs target `{c.base}`" for c in checkouts]
    return "\n".join([
        f"The working directory holds one git worktree per repo, each on branch `{branch}`:", "", *rows, "",
        ("The working directory itself is not a git repo, so run git as `git -C <dir> ...`. A change can touch one "
         f"repo or several; arete commits each changed repo and opens a PR in it. {stale_base_note(checkouts[0])}"),
    ])


def git_reads(checkouts: list[Checkout]) -> list[str]:
    return [f"git -C {c.dir} {cmd}*" for c in checkouts if c.dir for cmd in ("status", "diff", "log", "show")]


@dataclass
class Staged(diffguard.Diff):
    patch: str = ""
    changed: list[Checkout] = field(default_factory=list)


def stage(checkouts: list[Checkout], deny: list[str]) -> Staged:
    out = Staged()
    for c in checkouts:
        drop_untracked_bytecode(c.wt)
        diff = diffguard.stage(c.wt, deny, prefix=c.prefix)
        if not diff.counts:
            continue
        out += diff
        out.changed.append(c)
        out.patch += checkout_patch(c, diff.locks, "--cached")
    return out


def checkout_patch(c: Checkout, locks: list[str], *revs: str) -> str:
    patch = git(["diff", "--no-renames", f"--src-prefix=a/{c.prefix}", f"--dst-prefix=b/{c.prefix}", *revs,
                 "--", ".", *(f":(exclude){name.removeprefix(c.prefix)}" for name in locks)], c.wt)
    return patch + (f"\n(lockfile changes not shown: {', '.join(locks)})\n" if locks else "")


def patch_from_base(checkouts: list[Checkout]) -> str:
    patch = ""
    for c in checkouts:
        since_base = f"{c.origin_base}...HEAD"
        patch += checkout_patch(c, diffguard.between(c.wt, since_base, prefix=c.prefix).locks, since_base)
    return patch


def lines_by_file(staged: Staged, top: int = 12) -> str:
    rows = sorted(((name, n) for name, n in staged.counts.items() if n), key=lambda r: (-r[1], r[0]))
    out = [f"- `{name}`: {n}" for name, n in rows[:top]]
    if len(rows) > top:
        out.append(f"- {len(rows) - top} more files: {sum(n for _, n in rows[top:])}")
    return "\n".join(out)


def untracked_files(wt: Path) -> set[str]:
    return set(util.git_records(["ls-files", "--others", "--exclude-standard"], wt))


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


def tail_of_both_streams(stdout: str, stderr: str, n: int) -> str:
    stdout_room = n - min(len(stderr), n // 2)
    stderr_room = n - min(len(stdout), n // 2)
    return util.tail(stdout, stdout_room) + util.tail(stderr, stderr_room)


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
                report.append(f"### {name} failed: `{cmd}` exit {res.returncode}\n```\n{tail_of_both_streams(res.stdout, res.stderr, 6000)}\n```")
            else:
                report.append(f"### {name} passed: `{cmd}`")
    for d, name in untracked_added_since(untracked_before):
        remove_untracked(d, name)
    return ok, "\n\n".join(report)


class RetryNextPoll(Exception):
    pass


class Stopped(BaseException):
    pass


def stop_on_term(run_id: str) -> Callable:
    def stop(*_) -> None:
        if record.stop_asked(run_id):
            raise Stopped(record.STOPPED)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
    return stop


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
    session: Session = field(default_factory=Session)
    report: str = ""
    staged: Staged = field(default_factory=Staged)
    committed_lines: int = 0
    left_for_a_person: str = ""

    def resume(self, step: str, text: str, **kw) -> Result:
        res = self.run.agent(step, text, self.ws, model=self.cfg.model("build"), allowed_tools=self.tools,
                             disallowed_tools=runner.NO_GIT_WRITES, resume=self.session, timeout=self.timeout, **kw)
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
        if not staged.files:
            raise RunFailed(f"{after} undid every change")
        why = staged.refusal(self.layer.max_diff_lines, earlier=self.committed_lines)
        if staged.denied:
            raise RunFailed(f"{after} {why}")
        if why:
            pushed = f"\n- pushed earlier in this run: {self.committed_lines}" if self.committed_lines else ""
            raise RunFailed(f"after {after} {why}{planned}; split the work",
                            f"### Lines changed by file\n\n{lines_by_file(staged)}{pushed}")

    def push(self, branch: str, message: str) -> None:
        commit_and_push(self.checkouts, self.staged.changed, branch, message)
        self.committed_lines += self.staged.lines

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
    lock = state.run_lock("pipeline", repo.name, tid)
    with util.lock(lock) as got:
        if not got:
            print(f"{name} is already running")
            return 0
        with util.lock(SLOTS_LOCK, wait=True):
            slot = RunSlot(repo.name, str(tid), cfg.max_parallel)
            if not dry_run:
                if slots_free_to_start(cfg.max_parallel) <= 0:
                    print(f"{name} was not started: {no_free_slot(cfg.max_parallel)}. Wait for a run to "
                          f"finish or raise max_parallel in {paths.short_path(paths.CONFIG_FILE)}.")
                    return 1
                slot.hold()
            earlier = state.ticket_runs(repo.name, tid, limit=1)
            restarting = bool(earlier) and earlier[0]["status"] == "interrupted"
            run_id = record.start("pipeline", tag=f"{repo.name}-{tid}", lock=lock, repo=repo.name, issue=tid)
            run = Run(run_id, repo, paths.RUNS_DIR / run_id, dry_run, tracker=tk, tid=tid, force=force,
                      feedback=pending_feedback(repo.name, tid))
            run.dir.mkdir(parents=True, exist_ok=True)
        if restarting:
            run.log(f"restarting after run {earlier[0]['id']} was interrupted")
        if run.feedback:
            run.log(f"revising {', '.join(run.feedback_prs)} for {util.count(len(run.feedback), 'comment')}")
            if not dry_run:
                state.take_feedback(run.feedback, run_id)
        signal.signal(signal.SIGTERM, stop_on_term(run_id))
        try:
            status, summary, pr_url = _run(cfg, run, slot)
        except Stopped:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            status, summary, pr_url = "stopped", record.STOPPED, ""
            run.log(record.STOPPED)
            run.give_up("was stopped by request")
        except RunFailed as e:
            status, summary, pr_url = _fail(run, e.why, detail=e.detail)
        except Exception as e:
            run.log(f"crashed: {type(e).__name__}: {e}")
            status, summary, pr_url = "failed", f"crashed: {e}"[:300], ""
            run.give_up("crashed before finishing")
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            slot.free()
        if restarting and status in ("skipped", "noop") and not run.feedback:
            run.enter("failed")
            run.say(f"I did not restart this after the interruption: {summary}. Remove "
                    f"`{tk.label_name('failed')}` to retry.")
        if run.feedback and not dry_run:
            state.settle_feedback(run_id, {"pr": "done", "noop": "recorded"}.get(status, "failed"))
        run.log(f"done: {status} {pr_url or summary}")
        record.settle(run_id, status, summary=summary, pr_url=pr_url)
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


def triage_notes(approval: dict | None) -> str:
    if not approval:
        return ""
    assumed = "".join(f"\n- {a}" for a in approval["assumptions"]) or "\n- nothing it named"
    assumption_rule = ("Keep an assumption only where the ticket still leaves it open. End `plan` with an \"Assumed\" list "
                       "of every choice the ticket leaves open, these and your own, so the owner can check them in the "
                       "pull request.")
    if approval["sent_by"] == "owner":
        return (f"## Sent by its owner after triage\n\nIts owner sent this ticket to the pipeline from arete's triage "
                f"board at {approval['ts']}, after reading triage's questions and assumptions. Any answers they gave are "
                f"in their own comment that starts \"{TRIAGE_ANSWERS}\", and an answer wins over what triage assumed. "
                f"Triage assumed:{assumed}\n\n{assumption_rule}")
    return (f"## Approved by triage, not by a person\n\nArete's triage approved this ticket on its own at "
            f"{approval['ts']}, so its owner has not read it closely. Triage assumed:{assumed}\n\n{assumption_rule}")


def triage_block(approval: dict | None) -> str:
    if not approval:
        return ""
    assumed = "".join(f"\n- {a}" for a in approval["assumptions"])
    if approval["sent_by"] == "owner":
        return (f"## Sent from the triage board\n\nIts owner sent this ticket from arete's triage board. Triage "
                f"assumed:{assumed}\n\n") if assumed else ""
    return ("## Approved by triage\n\nArete's triage approved this ticket on its own, so check the choices it made"
            + (f". It assumed:{assumed}" if assumed else "; the plan below lists them.") + "\n\n")


def _run(cfg: config.Config, run: Run, slot: RunSlot) -> tuple[str, str, str]:
    repo, tk = run.repo, run.tracker
    try:
        t = tk.get(run.tid)
    except tracker.ERRORS as e:
        return "failed", f"could not read {run.ref}: {e}", ""
    approval = state.triage_approval(t.ref if repo.tracker == "jira" else tracker.ticket_name(repo.name, t.ref))
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
    for c in merge_moved_bases(run, [c for c in checkouts if c.resumes]):
        c.merged_base = True
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
    read_tools = runner.READ_TOOLS + bash_rules(config.DEFAULT_ALLOW_BASH + reads)
    build_tools = runner.BUILD_TOOLS + bash_rules(builder_bash)
    deny = diffguard.denied_paths(layer)
    limit = layer.max_diff_lines
    info = tk.label_name("needs-info")
    replies: list[dict] = []

    if run.feedback:
        feedback = feedback_markdown(run.feedback)
        plan_text = f"Revise the open PR for the owner's feedback:\n\n{feedback}"
        left_for_a_person = left_on_prs(run.feedback_prs)
        goal = ("Check each change the owner's feedback asks for, and that what the issue asks for still works.\n\n"
                + feedback)
        estimate = 0
        run.story.append(f"The owner reviewed the PR and left this feedback:\n\n{feedback}")
        res = run.agent("revise", prompt("revise", repo=repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                         body=t.body, comments=t.comments, attachments=attachments,
                                         feedback=feedback, expertise=expertise, checks=checks,
                                         commands=commands, max_lines=limit),
                        ws, model=cfg.model("build"), allowed_tools=build_tools, disallowed_tools=runner.NO_GIT_WRITES,
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
                                        triage=triage_notes(approval), expertise=expertise, commands=commands,
                                        denied=code_list(deny), max_lines=limit, checks=checks),
                        ws, model=cfg.model("plan"), allowed_tools=read_tools, json_schema=PLAN_SCHEMA,
                        timeout=step_timeout)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "planning step failed")
        plan = res.structured
        plan_text = plan.get("plan", "")
        left_for_a_person = left_section(run, (plan.get("left_for_a_person") or "").strip())
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
                        ws, model=cfg.model("build"), allowed_tools=build_tools, disallowed_tools=runner.NO_GIT_WRITES,
                        timeout=step_timeout)
        if not res.ok:
            return _fail(run, "build step failed", res.text)
        build_summary = res.text
        if not stage(checkouts, deny).files:
            if estimate == 0:
                return nothing_to_build(run, t, ws, checkouts, branch, build_summary, left_for_a_person)
            return _fail(run, f"the builder stopped without changing anything, though the plan estimated "
                              f"{util.count(estimate, 'line')}", build_summary)
    plan_to_review = plan_text
    if left_for_a_person:
        not_checked = NOT_CHECKED_LEFT_FOR_A_PERSON + left_for_a_person
        goal, plan_to_review = f"{goal}\n\n{not_checked}", f"{plan_text}\n\n{not_checked}"
    b = Build(cfg=cfg, run=run, ticket=t, ws=ws, checkouts=checkouts, layer=layer, timeout=step_timeout,
              workspace=workspace, attachments=attachments, tools=build_tools, deny=deny, expertise=expertise,
              session=res.session, left_for_a_person=left_for_a_person)

    attempts = 1
    ok = b.check()
    while not ok and attempts < layer.max_attempts:
        run.story.append(f"Checks failed on attempt {attempts}:\n{util.tail(b.report, 2500)}")
        attempts += 1
        b.resume(f"fix{attempts}", prompt("fix", attempt=attempts, max_attempts=layer.max_attempts, failures=b.report))
        ok = b.check()
    run.save("checks.md", b.report)
    record.note(run.id, attempts=attempts)
    if not ok:
        run.story.append("Checks still failing after the last attempt.")
        _learn(cfg, run, ws, layer.expertise_file, t.title, expertise, apply=False)
        return _fail(run, f"checks still failing after {attempts} attempts")

    b.require_valid_diff("the build and its fixes", f" (the plan estimated {estimate})" if estimate else "")

    verified = verify_in_use(b, goal, build_summary)
    review = _review(b, plan_to_review, verified)
    if review.get("verdict") == "request_changes":
        problems = "\n".join(f"- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in review.get("problems", []))
        run.story.append(f"Review requested changes:\n{problems}")
        b.resume("review_fix", prompt("review_fix", problems=problems))
        b.recheck("the review fixes")
        verified = verify_in_use(b, goal, build_summary)
        review = _review(b, plan_to_review, verified, "review2")

    draft_why = draft_reason(review.get("verdict") == "approve", verified)
    _learn(cfg, run, ws, layer.expertise_file, t.title, expertise, apply=True)
    staged = b.restage()
    size = f"{len(staged.files)} files, {staged.lines} lines"
    outcome = f"{size}, review {review.get('verdict')}, in use {verified.verdict}"

    if run.dry_run:
        run.save("diff.patch", staged.patch)
        run.log(f"dry run: {outcome}; worktree kept at {ws}")
        return "dry-run", outcome, ""

    subject = fill_template(layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    problems = review.get("problems", [])
    images = evidence_images(run, staged.changed[0], verified)
    report_body = (
        f"## Summary\n\n{build_summary.strip()[:3000]}\n\n"
        + ("" if run.feedback else triage_block(approval))
        + left_block(left_for_a_person)
        + f"{CHECKED_IN_USE_HEADING}\n\n{verified.markdown(images)}\n\n"
        f"## Review\n\n**{review.get('verdict', 'none')}**: {review.get('summary', '')}\n"
        + ("".join(f"\n- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in problems) if problems else "")
        + f"\n\n## Checks\n\nAll passed after {util.count(attempts, 'attempt')}: {checks}\n\n"
        + ("" if run.feedback else f"<details><summary>Plan</summary>\n\n{plan_text[:6000]}\n\n</details>\n\n")
        + f"Run `{run.id}`, {size}, ${record.spent(run.id):.2f}."
    )
    revised = "Revised for the review feedback on the PR.\n\n" if run.feedback else ""
    link = tk.pr_link(t, whole=not left_for_a_person)
    message = f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}"
    b.push(branch, message)
    opened: list[Checkout] = []
    for c in staged.changed:
        if not c.pr:
            c.pr = gh.pr_create(c.github, branch, c.base, tk.pr_title(subject, t), f"{link}\n\n{report_body}",
                                labels=tk.pr_labels, draft=bool(draft_why), cwd=c.wt)
            opened.append(c)
            run.log(f"opened {c.pr}{' as draft' if draft_why else ''}")
        else:
            run.log(f"pushed to {c.pr}")
    pushed = [c for c in staged.changed if c not in opened]
    with_prs = [c for c in checkouts if c.pr]
    for c in with_prs:
        others = sibling_section(t.ref, c, with_prs)
        if c in opened:
            if others:
                gh.pr_edit_body(c.pr, f"{link}\n\n{report_body}{others}")
            continue
        body = gh.pr_body(c.pr)
        new_body = synced_pr_body(run, c.pr, body, t, left_for_a_person)
        if opened:
            new_body = with_siblings(new_body, others)
        if new_body != body:
            gh.pr_edit_body(c.pr, new_body)
        if c in pushed and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Pushed more work for {t.ref}.\n\n{report_body}")
    try:
        ci_result = watch_ci(b, slot, branch, staged.changed, with_prs, draft_why)
    except RunFailed as e:
        if run.feedback:
            raise
        prs = "\n".join(f"- [{c.pr_name}]({c.pr})" for c in with_prs)
        status, why, _ = _fail(run, e.why, detail=f"{e.detail}\n\n### PRs\n\n{prs}\n\nReview comments on a PR "
                                                  "start a revision, as on any arete PR.")
        return status, why, record_prs(run, branch, with_prs)
    outcome += f", CI: {ci_result}"
    run.enter("pr")
    pr_lines = "\n".join(f"- {'Opened' if c in opened else 'Pushed to' if c in pushed else 'Unchanged'} [{c.pr_name}]({c.pr})"
                         for c in with_prs)
    if run.feedback:
        answer_feedback(run, replies, "Done in the latest push.")
        run.say(f"Revised for your review.\n\n{report_body}\n\nCI: {ci_result}.")
    else:
        run.say(f"PRs for this ticket:\n\n{pr_lines}\n\nCI: {ci_result}."
                + ("" if not opened else draft_note(draft_why, verified)))
    if not draft_why and layer.trust_level == "automerge" and approval and approval["sent_by"] == "triage":
        run.log("automerge left off: triage approved the ticket, so a person merges it")
    elif not draft_why and layer.trust_level == "automerge":
        risk = jev.risk(patch_from_base(opened), f"{t.title}\n\n{build_summary.strip()[:3000]}", cfg,
                        trace=(run.id, "automerge risk jev")) if opened and jev.ready(cfg, [repo]) else None
        for c in opened:
            try:
                if risk and risk.hold:
                    run.log(f"automerge left off on {c.pr}: {risk.hold}")
                    gh.pr_comment(c.pr, f"Automerge stays off for this PR: {risk.hold}. A person merges it.")
                    continue
                gh.pr_merge_auto(c.github, gh.pr_number(c.pr))
                run.log(f"automerge enabled on {c.pr}")
            except gh.GhError as e:
                run.log(f"automerge failed on {c.pr}: {e}")
    urls = record_prs(run, branch, with_prs)
    remove_workspace(ws, checkouts)
    return "pr", outcome, urls


def nothing_to_build(run: Run, t: tracker.Ticket, ws: Path, checkouts: list[Checkout], branch: str, summary: str,
                     left: str) -> tuple[str, str, str]:
    with_prs = [c for c in checkouts if c.pr]
    why = f"the open PRs already cover {t.ref}" if with_prs else "the plan found no code change to make"
    run.log(f"build: nothing to build, since {why}")
    if run.dry_run:
        remove_workspace(ws, checkouts)
        return "dry-run", f"nothing to build: {why}", ""
    said = summary.strip()[:3000] + (f"\n\n{LEFT_HEADING}\n\n{left}" if left else "")
    if not with_prs:
        return _ask(run, ws, checkouts,
                    f"I made no change, since {why}.\n\n{said}\n\nClose this if it is done. If it is not, say here "
                    f"what is missing, then remove the `{run.tracker.label_name('needs-info')}` label and I will "
                    "pick it up.", f"nothing to build: {why}")
    for c in with_prs:
        body = gh.pr_body(c.pr)
        if (new_body := synced_pr_body(run, c.pr, body, t, left)) != body:
            gh.pr_edit_body(c.pr, new_body)
    run.enter("pr")
    prs = "\n".join(f"- [{c.pr_name}]({c.pr})" for c in with_prs)
    run.say(f"Nothing more to build, since the open PRs already cover this:\n\n{prs}\n\n{said}")
    urls = record_prs(run, branch, with_prs)
    remove_workspace(ws, checkouts)
    return "pr", f"nothing to build: {why}", urls


def synced_pr_body(run: Run, pr: str, body: str, t: tracker.Ticket, left: str) -> str:
    link = run.tracker.pr_link(t, whole=not left)
    relinked_body = relinked(body, link, run.tracker.pr_link(t, whole=bool(left)))
    if relinked_body != body:
        run.log(f"{pr} now links the ticket with: {link}")
    left_synced = with_left_section(relinked_body, left)
    if left_synced != relinked_body:
        run.log(f"{pr}: {'wrote' if left else 'removed'} the {LEFT_TITLE} section")
    return left_synced


def left_section(run: Run, text: str) -> str:
    if len(text) <= MAX_LEFT_FOR_A_PERSON_CHARS:
        return text
    return (f"{text[:MAX_LEFT_FOR_A_PERSON_CHARS]}\n\n(Cut at {MAX_LEFT_FOR_A_PERSON_CHARS} characters. The whole "
            f"change is `left_for_a_person` in `plan.json` of run `{run.id}`.)")


def left_block(left: str) -> str:
    return f"{LEFT_START}\n{LEFT_HEADING}\n\n{left}\n\n{LEFT_END}\n\n" if left else ""


def with_left_section(body: str, left: str) -> str:
    found = LEFT_FOR_A_PERSON.search(body)
    if (found.group(1) if found else "") == left:
        return body
    if found:
        return body[:found.start()] + left_block(left) + body[found.end():]
    checked = body.find(f"\n{CHECKED_IN_USE_HEADING}\n")
    if checked < 0:
        return f"{body.rstrip()}\n\n{left_block(left).rstrip()}"
    return body[:checked + 1] + left_block(left) + body[checked + 1:]


def left_on_prs(urls: list[str]) -> str:
    for url in urls:
        if found := LEFT_FOR_A_PERSON.search(gh.pr_body(url)):
            return found.group(1)
    return ""


def relinked(body: str, link: str, other_link: str) -> str:
    first, newline, rest = body.partition("\n")
    return f"{link}{newline}{rest}" if first == other_link else body


def record_prs(run: Run, branch: str, with_prs: list[Checkout]) -> str:
    urls = [c.pr for c in with_prs]
    supersede_older_runs(run, urls)
    record.note(run.id, branch=branch)
    return " ".join(urls)


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


def watch_ci(b: Build, slot: RunSlot, branch: str, pushed: list[Checkout], with_prs: list[Checkout],
             draft_why: str) -> str:
    run, max_rounds = b.run, b.layer.max_attempts
    stopped_before = [c for c in pushed if stopped_by_ci(run, c)]
    rounds: list[ci.Round] = []
    try:
        for current in ci_rounds(b, slot, branch, pushed, max_rounds):
            rounds.append(current)
            publish_ci(run, pushed, ci.section(rounds, max_rounds))
    except Exception as e:
        failed = e if isinstance(e, RunFailed) else RunFailed(f"the CI watch crashed: {type(e).__name__}: {e}")
        drafted = [c for c in with_prs if set_draft(run, c, True)]
        ending = f"{ci.STOPPED} {failed.why}. {drafted_note(drafted, with_prs)}"
        publish_ci(run, with_prs, ci.section(rounds, max_rounds, ending))
        if failed is e:
            raise
        raise failed from e
    last = rounds[-1]
    if stopped_before and not draft_why and last.all_read:
        for c in stopped_before:
            set_draft(run, c, False)
    elif stopped_before:
        why = draft_why or "arete could not read every check"
        ending = f"{ci.STOPPED} an earlier CI watch made this PR a draft. It stays a draft, since in this run {why}."
        publish_ci(run, stopped_before, ci.section(rounds, max_rounds, ending))
    return last.summary()


def stopped_by_ci(run: AgentRun, c: Checkout) -> bool:
    try:
        return ci.stopped(gh.pr_body(c.pr))
    except gh.GhError as e:
        run.log(f"ci: could not read the body of {c.pr}: {util.first_line(str(e), 300)}")
        return False


def drafted_note(drafted: list[Checkout], with_prs: list[Checkout]) -> str:
    if drafted != with_prs:
        return "Arete could not turn every PR to a draft."
    return "The PR is now a draft." if len(with_prs) == 1 else "The PRs are now drafts."


def set_draft(run: AgentRun, c: Checkout, draft: bool) -> bool:
    now = "a draft" if draft else "ready for review"
    try:
        gh.pr_set_draft(c.pr, draft)
    except gh.GhError as e:
        run.log(f"ci: could not mark {c.pr} {now}: {util.first_line(str(e), 300)}")
        return False
    run.log(f"ci: marked {c.pr} {now}")
    return True


def ci_rounds(b: Build, slot: RunSlot, branch: str, pushed: list[Checkout], max_rounds: int) -> Iterator[ci.Round]:
    run = b.run
    watched = list(pushed)
    not_caused: dict[tuple[str, str, str], str] = {}
    for n in range(1, max_rounds + 1):
        slot.free()
        run.log(FREED_SLOT_FOR_CI)
        current = ci.Round(n, [ci.compare_with_base(ci.watch(ci_target(c), run.log), run.log) for c in watched])
        judged = [r for r in current.reds if r.kind != "base"]
        for r in judged:
            if r.key in not_caused:
                r.caused, r.reason = False, not_caused[r.key]
        fresh = [r for r in judged if r.key not in not_caused]
        if fresh:
            slot.take(run.log)
        try:
            fix_summary = judge_ci(b, fresh, n, max_rounds, watched) if fresh else ""
        except RunFailed:
            yield current
            raise
        not_caused.update({r.key: r.reason for r in judged if not r.caused})
        yield current
        caused = current.caused
        if not caused:
            if fresh:
                discard_edits(b)
            return
        names = ", ".join(r.label for r in caused)
        if n == max_rounds:
            raise RunFailed(f"CI still fails after {util.count(max_rounds, 'round')}: {names}", ci.red_list(caused))
        push_ci_fix(b, branch, caused, names, n)
        watched += [c for c in b.staged.changed if c not in watched]
        current.story = f"{names} failed, caused by this PR, and arete pushed a fix: {fix_summary}".rstrip(": ")
        run.log(f"ci: pushed a fix for {names}")


def push_ci_fix(b: Build, branch: str, caused: list[ci.Red], names: str, n: int) -> None:
    staged = b.restage()
    if not staged.files:
        raise RunFailed(f"{names} failed, caused by this PR, and the CI fix step changed nothing", ci.red_list(caused))
    if no_pr := [c.github for c in staged.changed if not c.pr]:
        raise RunFailed(f"the CI fix step changed {', '.join(no_pr)}, where this run has no PR, so arete pushed nothing",
                        ci.red_list(caused))
    if not b.check():
        report = f"checks-ci-round{n}.md"
        b.run.save(report, b.report)
        raise RunFailed("the checks failed after the CI fix",
                        f"{ci.red_list(caused)}\n\nThe check output is in `{report}` of run `{b.run.id}`.")
    b.require_valid_diff("the CI fix")
    b.push(branch, ci_fix_message(b, caused))


def ci_target(c: Checkout) -> ci.Target:
    return ci.Target(c.github, c.pr, c.pr_name, c.lease, c.base)


def merge_moved_bases(run: Run, checkouts: list[Checkout], log_prefix: str = "") -> list[Checkout]:
    merged = []
    for c in checkouts:
        try:
            if git(["rev-list", "--count", f"HEAD..{c.origin_base}"], c.wt).strip() == "0":
                continue
            git(["merge", "-q", "--no-edit", c.origin_base], c.wt)
        except RuntimeError as e:
            git(["merge", "--abort"], c.wt, check=False)
            run.log(f"{log_prefix}could not merge `{c.origin_base}` into the branch in {c.github}, so this run works "
                    f"on the branch as it is: {e}")
            continue
        run.log(f"{log_prefix}merged `{c.origin_base}` into the branch in {c.github}, since the base moved after "
                "the last push")
        merged.append(c)
    return merged


def merge_moved_bases_for_ci_fix(b: Build, watched: list[Checkout]) -> str:
    fetched = []
    for c in watched:
        try:
            fetch(c.src, c.base)
        except RuntimeError as e:
            b.run.log(f"ci: could not fetch `{c.origin_base}` in {c.github}, so the CI fix works on the branch as it "
                      f"is: {e}")
            continue
        fetched.append(c)
    merged = merge_moved_bases(b.run, fetched, "ci: ")
    if not merged:
        return ""
    names = ", ".join(f"`{c.origin_base}` into `{c.prefix or './'}`" for c in merged)
    return (f"Before this step arete merged {names}, since the base moved after the last push. CI tested the PR "
            "merged with that base, so the worktree now holds what CI ran, and files the base added are yours to "
            "edit like any other.")


def judge_ci(b: Build, reds: list[ci.Red], n: int, max_rounds: int, watched: list[Checkout]) -> str:
    run = b.run
    merged_base = merge_moved_bases_for_ci_fix(b, watched)
    logs = run.dir / "ci"
    logs.mkdir(exist_ok=True)
    if logs not in run.add_dirs:
        run.add_dirs.append(logs)
    failures = "\n\n".join(ci.failure_note(r, logs / f"round{n}-{slug(r.label)}.log") for r in reds)
    diff = ", ".join(f"`{c.diff_from_base}`" for c in b.checkouts)
    left = NOT_CAUSED_LEFT_FOR_A_PERSON + b.left_for_a_person if b.left_for_a_person else ""
    res = b.resume(b.next_step("ci_fix"), prompt("ci_fix", round=n, max_rounds=max_rounds, failures=failures,
                                                 diff=diff, checks=check_list(b.layer), merged_base=merged_base,
                                                 left_for_a_person=left),
                   json_schema=CI_FIX_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        for r in reds:
            r.reason = "The CI fix step failed before it judged this check."
        raise RunFailed("the CI fix step failed", ci.red_list(reds))
    verdicts = {name_key(v.get("name", "")): v for v in res.structured.get("checks", []) if isinstance(v, dict)}
    for r in reds:
        v = verdicts.get(name_key(r.label)) or verdicts.get(name_key(r.check.name)) or {}
        r.caused = v.get("caused_by_pr") is not False
        r.reason = ci.clip(v.get("reason") or "The fix step gave no verdict for this check.", MAX_CI_REASON_CHARS)
        run.log(f"ci: {r.label} {'caused' if r.caused else 'not caused'} by the PR: {util.first_line(r.reason, 200)}")
    return ci.clip(res.structured.get("summary", ""), MAX_CI_REASON_CHARS)


def name_key(name: str) -> str:
    return " ".join(name.lower().split())


def discard_edits(b: Build) -> None:
    for c in b.restage().changed:
        git(["reset", "-q", "--hard"], c.wt)
        b.run.log(f"ci: threw away the fix step's edits in {c.github}, since the PR caused no failure")


def ci_fix_message(b: Build, caused: list[ci.Red]) -> str:
    names = ", ".join(dict.fromkeys(r.check.name for r in caused))
    subject = fill_template(b.layer.subject, config.RepoLayer.subject, kind="fix", id=b.ticket.id,
                            title=f"make {names} pass in CI")
    return f"{tracker.with_ref(subject, b.ticket.ref)}\n\nArete-Run: {b.run.id}"


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
            record.note(r["id"], status="superseded")
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
    urls = record_prs(run, branch, [c for c in checkouts if c.pr])
    remove_workspace(ws, checkouts)
    return "pr", "answered the review without code changes", urls


def _review(b: Build, plan: str, verified: verify.Verified, step: str = "review") -> dict:
    t = b.ticket
    res = b.run.agent(step, prompt("review", repo=b.run.repo.name, workspace=b.workspace, ref=t.ref, title=t.title,
                                   body=t.body, comments=t.comments, attachments=b.attachments, plan=plan,
                                   checks=b.report, evidence=verified.markdown(),
                                   diff=util.tail(b.staged.patch, MAX_DIFF_IN_PROMPT)),
                      b.ws, model=b.cfg.model("review"), allowed_tools=runner.READ_TOOLS + bash_rules(b.read_bash),
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
                       diff=util.tail(b.staged.patch, MAX_DIFF_IN_PROMPT))


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
                            allowed_tools=runner.READ_TOOLS + bash_rules(bash) + (["mcp__playwright"] if url else []),
                            disallowed_tools=runner.NO_GIT_WRITES, json_schema=verify.SCHEMA, timeout=b.timeout,
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
            "- After each state you check, wait until the page settles (loading done, entrance animations finished) "
            "so the shot shows what a person sees, then call `browser_take_screenshot` with `filename` set to an "
            f"absolute path in `{evidence}`, such as `{evidence}/01-list.png`.\n"
            "- Read `browser_console_messages` after each state. Name each console error and each dev error overlay "
            "or badge in `saw`, and say whether the change causes it.\n"
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


def draft_reason(approved: bool, verified: verify.Verified) -> str:
    if verified.verdict == "blocked":
        return "the check in use was blocked"
    return "" if approved else "the review did not approve it"


def draft_note(draft_why: str, verified: verify.Verified) -> str:
    blocked_by = f" {verified.blocked_by}" if verified.verdict == "blocked" else ""
    return f"\n\nThe new PRs are drafts: {draft_why}.{blocked_by}".rstrip() if draft_why else ""


def check_list(layer: config.RepoLayer) -> str:
    return ", ".join(f"`{c}`" for c in layer.checks.values()) or "(none configured)"


def _learn(cfg, run: AgentRun, wt: Path, expertise_file: Path, title: str, expertise: str, apply: bool) -> None:
    if not run.story:
        run.log("learn: clean run, nothing to learn")
        return
    res = run.agent("learn", prompt("learn", repo=run.repo.name, ref=run.ref, title=title,
                                     story="\n\n".join(run.story), expertise=expertise,
                                     max_lines=EXPERTISE_MAX_LINES),
                    wt, model=cfg.model("learn"), allowed_tools=runner.READ_TOOLS, json_schema=LEARN_SCHEMA,
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


def feedback_text(row: dict) -> str:
    kind = (f"Kind: line comment on {row.get('path')} line {row.get('line')}\n\nCode:\n```\n{row.get('hunk') or ''}\n```"
            if row["kind"] == "line" else f"Kind: {row['kind']}")
    return f"A comment from the repo's owner on a pull request an agent opened.\n\n{kind}\n\nComment:\n{row['body']}"


def judge_feedback(cfg: config.Config, repo_name: str, url: str, rows: list[dict]) -> None:
    known = state.feedback_keys(url)
    fresh = [row for row in rows if row["status"] == "new" and (row["kind"], row["gh_id"]) not in known]
    try:
        repo = cfg.repo(repo_name)
    except config.UnknownRepo:
        return
    if not fresh or not jev.ready(cfg, [repo]):
        return
    trace = ("", f"feedback {url} jev")
    replies = jev.ask_many([(feedback_text(row), {"asks": jev.yes_no(FEEDBACK_ASKS)}) for row in fresh], cfg,
                           trace=trace)
    for row, reply in zip(fresh, replies, strict=True):
        if (a := reply.answers.get("asks")) is None:
            continue
        score = verdicts.scored(a.yes, cfg.jev.feedback_skip, over=False)
        skip = verdicts.holds(score)
        row["asks"] = score["p"]
        if skip:
            row["status"] = "skipped"
        verdicts.record(jev.traced_run(trace), "feedback", f"{url} {row['kind']} {row['gh_id']}", row["body"] or "",
                        {"asks": score}, f"asks for no change and no answer (p {a.yes:.2f})" if skip else "",
                        "skipped" if skip else "went on")
        print(f"{util.iso()} {url}: JEV reads your {row['kind']} comment {row['gh_id']} as asking for a change or an "
              f"answer at p {a.yes:.2f}" + (f", under the {cfg.jev.feedback_skip:g} skip line, so no run takes it"
                                           if skip else ""))


def record_feedback(cfg: config.Config, r: dict, urls: list[str], prs: list[dict], seen: dict, fresh: dict) -> None:
    for url, pr in zip(urls, prs):
        updated = pr.get("updatedAt", "")
        fresh[url] = seen.get(url, "")
        if updated and seen.get(url) == updated:
            continue
        try:
            rows = owner_feedback(url, pr.get("state", ""), r["repo"], str(r["issue"]))
            judge_feedback(cfg, r["repo"], url, rows)
            added = state.add_feedback(rows)
        except gh.GhError as e:
            print(f"{util.iso()} {url}: could not read the review comments: {util.first_line(str(e), 300)}")
            continue
        fresh[url] = updated if util.iso(util.now() - PR_SEEN_SETTLE) > updated else ""
        if added:
            print(f"{util.iso()} {url}: {util.count(added, 'new comment')} from you")


def sync_outcomes(cfg: config.Config) -> None:
    seen, fresh = util.read_json(PR_SEEN_FILE, {}), {}
    for r in state.runs_reading_pr_comments(util.iso(util.now() - REJECTED_PR_LISTEN)):
        urls = r["pr_url"].split()
        try:
            prs = [gh.pr_view(url) for url in urls]
        except gh.GhError:
            continue
        record_feedback(cfg, r, urls, prs, seen, fresh)
        closed = all(pr.get("state") in ("MERGED", "CLOSED") for pr in prs)
        if r["status"] == "rejected":
            if not closed:
                record.note(r["id"], status="pr")
            continue
        if not closed:
            continue
        for url, pr in zip(urls, prs):
            state.close_feedback(url, pr["state"])
        merged = all(pr.get("mergedAt") for pr in prs)
        closed_at = max(pr.get("closedAt") or "" for pr in prs) or util.iso()
        record.note(r["id"], status="merged" if merged else "rejected", finished=closed_at)
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
        record.note(r["id"], landed=reached)
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
                    run.repo.path, model=cfg.model("uat"), allowed_tools=runner.READ_TOOLS, json_schema=UAT_SCHEMA,
                    timeout=600, persist=False)
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


def settle_abandoned_runs(cfg: config.Config) -> None:
    record.reap(tickets=True, then=lambda row: tell_ticket_run_died(cfg, row))


def tell_ticket_run_died(cfg: config.Config, row: dict) -> None:
    repo = cfg.repo(row["repo"])
    run = Run(row["id"], repo, paths.RUNS_DIR / row["id"], tracker=tracker.for_repo(cfg, repo), tid=str(row["issue"]),
              feedback=pending_feedback(repo.name, str(row["issue"])))
    run.dir.mkdir(parents=True, exist_ok=True)
    if row["status"] == "stopped":
        run.log(record.STOPPED)
        run.give_up("was stopped by request")
    elif row["status"] == "failed":
        run.log(f"{record.INTERRUPTED}, the second interruption in a row")
        run.give_up(f"{record.INTERRUPTED}, the second interruption in a row")
    else:
        run.log(record.INTERRUPTED)
        run.leave("running")
        run.say(f"Run `{run.id}` {record.INTERRUPTED}; I will start it again.")


def holding(rows: list[dict], lock_kind: str) -> list[dict]:
    return [r for r in rows
            if r["kind"] == "pipeline" and util.lock_is_held(state.run_lock(lock_kind, r["repo"], r["issue"]))]


def live_pipeline_runs() -> list[dict]:
    return holding(state.runs_marked_running(), "pipeline")


def runs_in_slots() -> list[dict]:
    return holding(state.runs_marked_running(), "slot")


def runs_waiting_for_a_slot() -> list[dict]:
    return holding(state.runs_marked_running(), "slot-wait")


def slots_free_to_start(max_parallel: int) -> int:
    if runs_waiting_for_a_slot():
        return 0
    return max_parallel - len(runs_in_slots())


def freed_slot_for_ci(run_id: str) -> bool:
    log = paths.RUNS_DIR / run_id / "run.log"
    return log.exists() and FREED_SLOT_FOR_CI in log.read_text()


def live_max_parallel(last_read: int) -> int:
    try:
        return config.load().max_parallel
    except (OSError, ValueError):
        return last_read


@dataclass
class RunSlot:
    repo: str
    tid: str
    max_parallel: int
    held: TextIO | None = None

    def hold(self) -> None:
        self.held = util.hold_lock(state.run_lock("slot", self.repo, self.tid))

    def take(self, log: Callable[[str], None]) -> None:
        if self.held:
            return
        waiting: TextIO | None = None
        try:
            while True:
                with util.lock(SLOTS_LOCK, wait=True):
                    self.max_parallel = live_max_parallel(self.max_parallel)
                    if len(runs_in_slots()) < self.max_parallel:
                        self.hold()
                        log("ci: took a run slot back" + (" after waiting for one" if waiting else ""))
                        return
                    if not waiting:
                        waiting = util.hold_lock(state.run_lock("slot-wait", self.repo, self.tid))
                        log(f"ci: waiting for a free run slot (max_parallel {self.max_parallel}), ahead of new runs")
                time.sleep(SLOT_POLL_SECONDS)
        finally:
            if waiting:
                waiting.close()

    def free(self) -> None:
        if self.held:
            self.held.close()
            self.held = None


def no_free_slot(max_parallel: int) -> str:
    if waiting := len(runs_waiting_for_a_slot()):
        return (f"{util.count(waiting, 'run')} {'waits' if waiting == 1 else 'wait'} to take a run slot back first "
                f"(max_parallel {max_parallel})")
    return f"no run slot is free (max_parallel {max_parallel})"


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
        going = Counter(r["repo"] for r in live_pipeline_runs())
        slots = slots_free_to_start(cfg.max_parallel)
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
                if going[repo.name] >= cfg.max_parallel_per_repo:
                    print(f"{util.iso()} {name}: waiting, {repo.name} already has {util.count(going[repo.name], 'run')} "
                          f"going (max_parallel_per_repo {cfg.max_parallel_per_repo})")
                elif slots <= 0:
                    print(f"{util.iso()} {name}: waiting, {no_free_slot(cfg.max_parallel)}")
                else:
                    print(f"{util.iso()} {name}: {why}" + (" (dry run, not started)" if dry_run else ""))
                    if not dry_run:
                        spawn_run(repo.name, tid)
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
