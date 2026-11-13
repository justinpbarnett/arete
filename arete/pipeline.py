from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from string import Template
from typing import TextIO

from . import (
    ci,
    config,
    control,
    diffguard,
    document,
    environment,
    factory,
    gh,
    jev,
    jira,
    lines,
    outbound,
    paths,
    record,
    routing,
    runlimits,
    runner,
    scratch,
    state,
    steps,
    tracker,
    util,
    verdicts,
    verify,
)
from .agent import NO_SESSION, Result, Session
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
SLOT_KINDS = ("pipeline", "experiment")
RUNS_LOG = "runs.log"
RUN_START_SECONDS = 10
SUCCEEDED = ("pr", "noop", "needs-info", "dry-run", "done")
ANSWER_FILE = "answer.md"
FREED_SLOT_FOR_CI = "ci: freed the run slot for the wait"
SLOT_POLL_SECONDS = 30
MAX_STARTS_PER_POLL = 64
CHECKS_SCRIPT = "checks"
NO_CHECKS = "(none configured)"
SUMMARY_CHARS = 3000
PLAN_CHARS = 6000
CUT_MARK = " …"

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
        "assumptions": {"type": "array", "items": {"type": "string"}},
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
        "changes": {"type": "string"},
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
RESOLVER_SETS = {"RUFF_NO_CACHE": "true"}

RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {"resolved": {"type": "boolean"}, "summary": {"type": "string"}},
    "required": ["resolved", "summary"],
}
CONFLICT_MARKER = re.compile(r"^(<<<<<<<|>>>>>>>|\|\|\|\|\|\|\|)( |$)", re.MULTILINE)
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


def prompt(name: str, prompts_dir: Path = paths.PROMPTS_DIR, **values) -> str:
    text = (prompts_dir / f"{name}.md").read_text()
    return Template(text).safe_substitute({k: ("" if v is None else str(v)) for k, v in values.items()})


def public_text(run: AgentRun, text: str) -> str:
    return outbound.redact(text) if run.repo.public else text


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
    paid: float = 0.0
    add_dirs: list[Path] = field(default_factory=list)
    story: list[str] = field(default_factory=list)
    cfg: config.Config | None = None
    layer: config.RepoLayer | None = None
    kind: str = ""
    overrides: dict = field(default_factory=dict)
    step_overrides: dict[str, dict] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)
    writes_lines: bool = True
    step_calls: dict[str, int] = field(default_factory=dict)
    budget: runlimits.Budget | None = None

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

    def write_line(self, event: str, **fields) -> None:
        if self.writes_lines:
            lines.write(self.id, event, **fields)

    def waiting(self, cause: str):
        return lines.waiting(self.id, cause) if self.writes_lines else nullcontext()

    def save(self, name: str, data) -> None:
        p = self.dir / name
        p.write_text(data if isinstance(data, str) else json.dumps(data, indent=2, default=str))

    @property
    def config(self) -> config.Config:
        if self.cfg is None:
            self.cfg = config.load()
        return self.cfg

    def settings(self, role: str) -> steps.Settings:
        role = config.STEP_ALIASES.get(role, role)
        return steps.resolve(self.config, self.layer, role, self.kind,
                             {**self.overrides, **self.step_overrides.get(role, {})},
                             self.step_overrides.get(config.INHERITS.get(role, ""), {}))

    def time_for(self, seconds: float) -> float:
        return seconds

    def wait_for_network(self) -> bool:
        return util.wait_for_network()

    def retry_kw(self) -> dict:
        return {}

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw) -> Result:
        s = self.settings(role) if role else None
        if self.budget:
            self.budget.charge(step, s.step if s else role)
        if s:
            reach = s.agent_kw(self.config, kw.pop("allowed_tools", ()), kw.pop("mcp_config", ""))
            kw = {**s.call, **reach,
                  "resume": self.sessions.get(config.INHERITS.get(s.step, s.step), NO_SESSION)
                  if s.session == "resume" else NO_SESSION, **kw}
        cfg = kw.get("cfg") or self.config
        if s and s.requires.declared and not cfg.replay.all:
            size = routing.request_tokens(text, kw.get("system", ""), kw.get("json_schema"))
            found = routing.Discovery(cfg, self.repo, kw.get("device", "local"), layer=self.layer)
            servers = json.loads(kw.get("mcp_config") or "{}").get("mcpServers", {})
            bare, denied = runner.reach(kw)
            effective = replace(s, tools=bare, denied=denied, mcp=tuple(servers))
            picked = routing.select(found, effective, request_tokens=size)
            kw["route"] = routing.described_selection(picked, effective, size)
            if picked.error:
                kw["refusal"] = picked.error
            elif alternative := picked.route:
                kw.update(model=alternative.model, runner=alternative.runner, effort=alternative.effort,
                          instructions=list(alternative.instructions), mode=alternative.mode)
                self.log(f"{step}: {picked.reason}")
        run_with = kw.get("runner", "") or kw.get("resume", NO_SESSION).runner
        which, model = runner.chosen(kw.get("model", ""), run_with, cfg)
        effort, started, spent = kw.get("effort", ""), time.monotonic(), self.cost
        tier, kind = kw.setdefault("tier", s.tier if s else ""), kw.setdefault("kind", self.kind)
        kw.setdefault("waits", self.waiting)
        kw.setdefault("repo", self.repo)
        kw.setdefault("layer", self.layer)
        seam = factory.inside(self.id)
        lined = self.writes_lines and seam is None
        if s:
            kw.setdefault("settings", s)
        if self.writes_lines:
            def announce(route: dict) -> None:
                fields = {k: route.get(k) for k in ("runner", "model", "provider", "auth_mode", "credential",
                                                    "billing", "effort", "requested_effort", "tier", "position",
                                                    "requested", "env", "skipped", "launch") if k in route}
                if s:
                    fields["atom"] = s.step
                if seam is None:
                    self.write_line("step_start", step=step, kind=kind, **fields)
                else:
                    self.write_line("step_start", step=seam.step, call=step, **{"try": seam.n}, kind=kind, **fields)

            kw["on_launch"] = announce
        attempt_no = self.step_calls[step] = max(state.step_tries(self.id, step), self.step_calls.get(step, 0)) + 1
        kw["try_no"] = attempt_no
        self.log(f"{step}: starting")
        kw.setdefault("say", lambda note: self.log(f"{step}: {note}"))
        with lines.beating(self.id, step) if lined else nullcontext():
            res = runner.run(text, cwd, add_dirs=self.add_dirs, trace=(self.id, step), **kw)
            if res.connection_failed:
                self.log(f"{step}: lost the connection after {util.count(res.turns, 'turn')}, waiting for the network")
                if self.wait_for_network():
                    dropped = res
                    self.log(f"{step}: the network is back, the dropped attempt cost {runner.cost_note(res)}")
                    if res.session.id and res.turns > 1 and kw.get("persist", True):
                        res = runner.run(CONTINUE_AFTER_DROP, cwd, add_dirs=self.add_dirs, trace=(self.id, step),
                                         **{**kw, "resume": res.session, **self.retry_kw()})
                    else:
                        res = runner.run(text, cwd, add_dirs=self.add_dirs, trace=(self.id, step),
                                         **{**kw, **self.retry_kw()})
                    res = runner.with_spent(res, [dropped])
        if s and res.session.id:
            self.sessions[config.INHERITS.get(s.step, s.step)] = res.session
        self.cost += res.cost_usd
        self.paid += res.paid_usd
        usage = {"runner": res.runner or which, "model": res.model or model, "effort": res.effort or effort, "tier": tier,
                 "credential": res.credential, "auth_mode": ", ".join(res.modes),
                 "provider": res.provider,
                 "kind": kind, "cost_usd": round(self.cost - spent, 6), "tokens_in": res.tokens_in,
                 "tokens_out": res.tokens_out, "turns": res.turns, "cache_read": res.cache_read,
                 "cache_write": res.cache_write, "api_seconds": round(res.api_seconds, 2)}
        if s:
            usage["atom"] = s.step
        if seam is not None:
            seam.add(error="" if res.ok else res.error or "", **usage)
        else:
            self.write_line("step_end", step=step, ok=res.ok, seconds=round(time.monotonic() - started, 1),
                            error=(res.error or "")[:500], **{"try": attempt_no}, **usage)
        self.save(f"{step}.json", {"ok": res.ok, "session": res.session.id, "cost": res.cost_usd,
                                   "auth_mode": usage["auth_mode"], "auth_modes": list(res.modes),
                                   "paid_usd": res.paid_usd,
                                   "turns": res.turns, "text": res.text, "structured": res.structured,
                                   "error": res.error, "runner": usage["runner"], "model": usage["model"],
                                   "skipped": res.skipped})
        self.log(f"{step}: {'ok' if res.ok else 'FAILED ' + util.first_line(res.error)} {runner.cost_note(res)}")
        return res


@dataclass(kw_only=True)
class Run(AgentRun):
    tracker: tracker.Tracker
    tid: str
    force: bool = False
    feedback: list[dict] = field(default_factory=list)
    answering: dict = field(default_factory=dict)
    path: str = ""
    alone: bool = False
    facts: dict = field(default_factory=dict)
    repairs: str = ""
    workspace: tuple[Path, list[Checkout]] | None = None

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
        return "pr" if self.feedback or self.repairs else "failed"

    @property
    def retry_hint(self) -> str:
        if self.feedback:
            return (f"Comment on the PR again to retry: a submitted review starts at once, a comment after "
                    f"{FEEDBACK_QUIET_MINUTES} quiet minutes")
        if self.repairs:
            return f"Continue it with `arete resume {self.id}`"
        return f"Remove `{self.tracker.label_name('failed')}` to retry"

    def enter(self, status: str) -> None:
        if self.dry_run:
            return
        others = [s for s in self.tracker.states if s != status]
        labeled, moved = self.tracker.label(self.tid, add=[status], remove=others), self.tracker.move(self.tid, status)
        for err in (labeled, moved):
            if err:
                self.log(f"tracker: {util.first_line(err, 300)}")
        if not moved and isinstance(self.tracker, tracker.JiraTracker) and self.tracker.cfg.transitions.get(status):
            record.note(self.id, moved=status)

    def leave(self, status: str) -> None:
        if not self.dry_run and (err := self.tracker.label(self.tid, remove=[status])):
            self.log(f"tracker: {util.first_line(err, 300)}")

    def say(self, body: str) -> str:
        if self.dry_run:
            return ""
        body = public_text(self, body)
        err = self.say_on_prs(body) if self.feedback else self.tracker.comment(self.tid, f"{tracker.ARETE_TAG} {body}")
        if err:
            self.log(f"tracker: {util.first_line(err, 300)}")
        return err

    def say_on_prs(self, body: str) -> str:
        errors = []
        for url in self.feedback_prs:
            try:
                gh.pr_comment(url, public_text(self, body))
            except gh.GhError as e:
                errors.append(f"{url}: {e}")
        return "; ".join(errors)

    def give_up(self, why: str) -> None:
        if self.alone:
            return
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
    revision: str = ""
    base_commit: str = ""
    resolved: list[str] = field(default_factory=list)
    merge: str = ""
    resolved_lines: int = 0

    @property
    def prefix(self) -> str:
        return f"{self.dir}/" if self.dir else ""

    @property
    def starting_point(self) -> str:
        if not self.resumes:
            return f"fresh from `{self.base}`"
        start = (f"continuing the open PR {self.pr}" if self.pr
                 else "continuing commits already pushed to the branch, with no PR yet")
        if not self.merged_base:
            return start
        at = f" at `{self.base_commit[:12]}`" if self.base_commit else ""
        fixed = (f"; arete resolved its merge conflicts in {', '.join(f'`{p}`' for p in self.resolved)}"
                 if self.resolved else "")
        return start + f", with `{self.origin_base}`{at} merged in since the base moved after the last push" + fixed

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


def scratch_left(ws: Path, checkouts: list[Checkout]) -> list[Path]:
    found = []
    for d in {ws, *(c.wt for c in checkouts)}:
        if scratch.pending(d):
            found.append(d)
        try:
            try:
                d.lstat()
            except FileNotFoundError:
                continue
            if d.is_symlink():
                found.append(d)
                continue
            for root, dirs, files in os.walk(d, onerror=lambda e, found=found, d=d: found.append(d)):
                found += [Path(root, n) for n in dirs + files if n.startswith(paths.SCRATCH_PREFIX)]
                dirs[:] = [n for n in dirs if n != ".git" and not n.startswith(paths.SCRATCH_PREFIX)]
        except OSError:
            found.append(d)
    return found


def remove_workspace(ws: Path, checkouts: list[Checkout]) -> bool:
    if scratch_left(ws, checkouts):
        return False
    made = made_before(ws)
    salt = str(made.get(SALT_KEY) or "")

    def unknown_outside(examined: set[Path]) -> bool:
        try:
            return any(not (salt and made.get(n) == fingerprint(ws / n, salt))
                       for n in unexamined(ws, examined))
        except OSError:
            return True

    if unknown_outside({c.wt for c in checkouts if (c.wt / ".git").exists()}):
        return False
    for c in checkouts:
        with util.repo_lock(c.src):
            if scratch_left(ws, checkouts):
                return False
            if (c.wt / ".git").exists():
                try:
                    if any(not (salt and made.get(c.prefix + n) == fingerprint(c.wt / n, salt))
                           for n in [*ignored_files(c.wt), *untracked_files(c.wt)]):
                        return False
                except (OSError, RuntimeError):
                    return False
            git(["worktree", "remove", "--force", str(c.wt)], c.src, check=False)
    if scratch_left(ws, checkouts) or unknown_outside(set()):
        return False
    shutil.rmtree(ws, ignore_errors=True)
    if os.path.lexists(ws):
        return False
    made_record(ws).unlink(missing_ok=True)
    for c in checkouts:
        with util.repo_lock(c.src):
            git(["worktree", "prune"], c.src, check=False)
    return True


def made_copy(ws: Path, source: Path, copies: list[str], rel: str) -> bool:
    return (rel in copies and (source / rel).is_file() and (ws / rel).is_file()
            and (ws / rel).read_bytes() == (source / rel).read_bytes())


def made_record(ws: Path) -> Path:
    return paths.STATE_DIR / "workspaces" / f"{ws.name}.json"


def fingerprint(p: Path, salt: str) -> str:
    if not p.exists() and not p.is_symlink():
        return "missing"
    walked = os.walk(p) if p.is_dir() and not p.is_symlink() else [(str(p.parent), [], [p.name])]
    h = hashlib.sha256(salt.encode())
    for root, dirs, files in walked:
        dirs.sort()
        for name in sorted(dirs + files):
            q = Path(root, name)
            st = q.lstat()
            h.update(f"{q.relative_to(p.parent).as_posix()}\0{st.st_mode}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
            if stat.S_ISLNK(st.st_mode):
                h.update(os.readlink(q).encode())
            elif stat.S_ISREG(st.st_mode):
                with open(q, "rb") as f:
                    while chunk := f.read(1 << 20):
                        h.update(chunk)
    return h.hexdigest()


def made_before(ws: Path) -> dict:
    try:
        made = json.loads(made_record(ws).read_text())
    except (OSError, ValueError):
        return {}
    return made if isinstance(made, dict) else {}


def ignored_files(wt: Path) -> list[str]:
    return [r[3:] for r in util.git_records(["status", "--porcelain", "--ignored=matching",
                                             "--untracked-files=normal"], wt) if r.startswith("!! ")]


def unexamined(ws: Path, examined: set[Path]) -> list[str]:
    if ws in examined or not ws.is_dir():
        return []
    found = []
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if Path(root, d) not in examined]
        found += [Path(root, f).relative_to(ws).as_posix() for f in files]
        found += [Path(root, d).relative_to(ws).as_posix() for d in dirs if Path(root, d).is_symlink()]
    return sorted(found)


def leftovers(ws: Path, checkouts: list[Checkout]) -> list[str]:
    examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
    found = [c.prefix + n for c in checkouts if c.wt in examined for n in uncommitted(c.wt) + ignored_files(c.wt)]
    return found + unexamined(ws, examined)


def head_key(c: Checkout) -> str:
    return f"\0head {c.prefix}"


SALT_KEY = "\0salt"


def outside_scratch(ws: Path, names: list[str], held: list[Path]) -> list[str]:
    return [n for n in names
            if not any(part.startswith(paths.SCRATCH_PREFIX) for part in Path(n).parts)
            and not any(h.is_relative_to(ws / n) for h in held)]


def note_made(ws: Path, checkouts: list[Checkout]) -> None:
    try:
        names = outside_scratch(ws, leftovers(ws, checkouts), scratch_left(ws, checkouts)) if ws.is_dir() else []
        heads = {head_key(c): git(["rev-parse", "HEAD"], c.wt).strip() for c in checkouts if (c.wt / ".git").exists()}
        if names or heads:
            salt = os.urandom(16).hex()
            made = {n: fingerprint(ws / n, salt) for n in names} | heads | {SALT_KEY: salt}
            made_record(ws).parent.mkdir(parents=True, exist_ok=True)
            made_record(ws).write_text(json.dumps(made))
            return
    except (OSError, RuntimeError):
        pass
    made_record(ws).unlink(missing_ok=True)


def kept_work(ws: Path, checkouts: list[Checkout], source: Path, copies: list[str], commits: bool = True) -> str:
    made = made_before(ws)
    salt = str(made.get(SALT_KEY) or "")

    def unknown(names: list[str]) -> list[str]:
        return [n for n in names if not made_copy(ws, source, copies, n)
                and not (salt and made.get(n) == fingerprint(ws / n, salt))]

    examined = {c.wt for c in checkouts if (c.wt / ".git").exists()}
    for c in checkouts:
        if c.wt not in examined:
            continue
        try:
            edits = unknown([c.prefix + n for n in uncommitted(c.wt)])
            ignored = unknown([c.prefix + n for n in ignored_files(c.wt)])
            ahead = git(["rev-list", "--count", "HEAD", "--not", "--remotes=origin"], c.wt).strip() if commits else "0"
            if made.get(head_key(c)) == git(["rev-parse", "HEAD"], c.wt).strip():
                ahead = "0"
        except (RuntimeError, OSError) as e:
            return f"arete could not read the existing worktree {c.wt} of {c.github}, so it left it as it is: {e}"
        if edits or ignored or ahead != "0":
            what = (f"uncommitted edits that arete has no record of leaving there ({', '.join(edits[:5])})" if edits
                    else (f"git-ignored files that arete has no record of leaving there ({', '.join(ignored[:5])})"
                          if ignored else f"{util.count(int(ahead), 'commit')} that are not on origin"))
            return (f"the existing worktree {c.wt} of {c.github} holds {what}, so arete did not make it again and "
                    "left them as they are; move or remove them, then run again")
    try:
        others = unknown(unexamined(ws, examined))
    except OSError as e:
        return f"arete could not read the existing workspace {ws}, so it left it as it is: {e}"
    if others:
        return (f"the existing workspace {ws} holds files outside this repo's worktrees ({', '.join(others[:5])}), "
                "so arete did not make it again and left them as they are; move or remove them, then run again")
    return ""


def clear_workspace(ws: Path, checkouts: list[Checkout], source: Path, copies: list[str]) -> None:
    if scratch_left(ws, checkouts):
        raise RunFailed("unresolved or unknown scratch resources keep the existing workspace retained")
    if why := kept_work(ws, checkouts, source, copies):
        raise RunFailed(why)
    if not remove_workspace(ws, checkouts):
        raise RunFailed("workspace removal withheld to preserve scratch resources")


def make_workspace(run_id: str, repo: config.Repo, base: str, branch: str, key: str,
                   copies: list[str]) -> tuple[Path, list[Checkout]]:
    ws = paths.WORKTREES_DIR / f"{repo.name}-{key}"
    checkouts = [Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir, base or gh.default_branch(p.github))
                 for p in repo.parts]
    clear_workspace(ws, checkouts, repo.path, copies)
    for c in checkouts:
        with record.timed(run_id, "clone", repo=c.github, base=c.base, branch=branch, path=str(c.wt)) as clone:
            fetch(c.src, c.base)
            c.pr = gh.open_pr_url(c.github, branch)
            c.lease = remote_head(c.src, branch)
            c.resumes = bool(c.pr) or (bool(c.lease) and c.lease not in gh.closed_pr_heads(c.github, branch))
            if c.resumes:
                fetch(c.src, branch)
            add_worktree(c, branch, f"origin/{branch}" if c.resumes else c.origin_base)
            clone["resumed"] = c.resumes
    return ws, checkouts


def make_worktree(repo: config.Repo, base: str, branch: str, key: str, resume: bool = False) -> Path:
    c = Checkout("", repo.github, repo.path, paths.WORKTREES_DIR / f"{repo.name}-{key}", base)
    if not remove_workspace(c.wt, [c]):
        raise RunFailed("worktree removal withheld to preserve existing resources")
    fetch(c.src, base)
    if resume:
        fetch(c.src, branch)
    add_worktree(c, branch, f"origin/{branch}" if resume else c.origin_base)
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
    merged: list[Checkout] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.files and not self.merged


def stage(checkouts: list[Checkout], deny: list[str], from_base: bool = False) -> Staged:
    out = Staged()
    for c in checkouts:
        drop_untracked_bytecode(c.wt)
        against = git(["merge-base", c.origin_base, "HEAD"], c.wt).strip() if from_base else "HEAD"
        diff = diffguard.stage(c.wt, deny, against=against, prefix=c.prefix)
        merged = pending_merge(c)
        if merged and not from_base:
            brought = diffguard.between(c.wt, c.lease, c.merge, prefix=c.prefix).counts
            diff.counts = {name: 0 for name in brought} | diff.counts
        if merged:
            out.merged.append(c)
        elif not diff.counts:
            continue
        out += diff
        out.changed.append(c)
        out.patch += checkout_patch(c, diff.locks, "--cached", against)
        if merged and c.resolved and not from_base:
            out.patch += git(["diff", "--no-renames", f"--src-prefix=a/{c.prefix}", f"--dst-prefix=b/{c.prefix}",
                              c.base_commit, "HEAD", "--", *c.resolved], c.wt)
    return out


def pending_merge(c: Checkout) -> bool:
    return bool(c.merge and c.lease) and git(["rev-list", "--count", f"{c.lease}..{c.merge}"], c.wt).strip() != "0"


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


def ignored_state(wt: Path) -> dict[str, str]:
    names = util.git_records(["ls-files", "--others", "--ignored", "--exclude-standard"], wt)
    return {n: fingerprint(wt / n, "") for n in names
            if not (Path(n).parent.name == "__pycache__" and Path(n).suffix == ".pyc")}


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
    with nullcontext() if layer.parallel_checks else machine_to_itself(run):
        for name, cmd in layer.checks.items():
            with record.timed(run.id, "check", name=name, command=cmd) as check:
                res = environment.sh(cmd, run.config, run.repo, cwd=cwd, timeout=run.time_for(timeout))
                check["exit"] = res.returncode
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
    report: str = ""
    staged: Staged = field(default_factory=Staged)
    committed_lines: int = 0
    left_for_a_person: str = ""
    check_commands: str = NO_CHECKS
    from_base: bool = False
    plan_text: str = ""
    build_summary: str = ""

    def resume(self, step: str, text: str, role: str = "build", **kw) -> Result:
        saved = self.run.sessions.get("build", NO_SESSION)
        settings = self.run.settings(role)

        def switched(which: str) -> str:
            plan = self.run.dir / "plan.md"
            previous = util.read_json(self.run.dir / "build.json", {})
            context = (f"The builder changed from {saved.runner} to {which}. Continue the existing work "
                       f"in {self.ws}; inspect its current diff before editing.\n\n"
                       f"## Issue {self.ticket.ref}: {self.ticket.title}\n\n{self.ticket.body}\n\n"
                       f"{self.ticket.comments}\n\n## Saved plan\n\n"
                       f"{self.plan_text or (plan.read_text() if plan.exists() else '')}\n\n"
                       f"## Earlier builder summary\n\n{self.build_summary or previous.get('text', '')}\n\n"
                       "Keep the current task's tool restrictions and never write git metadata or secret files.")
            return f"{context}\n\n## Current feedback\n\n{text}"

        if saved.id and saved.runner != settings.runner and settings.explicit_route:
            kw["resume"] = NO_SESSION
            text = switched(settings.runner)
        elif saved.id and settings.session == "resume":
            kw["fresh_prompt"] = switched
        elif saved.id and saved.runner != settings.runner:
            text = switched(settings.runner)
        return self.run.agent(step, text, self.ws, role=role, allowed_tools=self.tools,
                              disallowed_tools=runner.NO_GIT_WRITES, **kw)

    def check(self) -> bool:
        ok, self.report = run_checks(self.layer, self.ws, self.git_dirs, self.run, self.timeout)
        return ok

    def restage(self, *, from_base: bool | None = None) -> Staged:
        self.staged = stage(self.checkouts, self.deny, self.from_base if from_base is None else from_base)
        record.mark(self.run.id, "diff", files=self.staged.files, lines=self.staged.lines, denied=self.staged.denied,
                    patch=self.staged.patch)
        return self.staged

    def require_valid_diff(self, after: str, planned: str = "") -> None:
        staged = self.restage()
        if staged.empty:
            raise RunFailed(f"{after} undid every change")
        earlier = 0 if self.from_base else self.committed_lines + sum(c.resolved_lines for c in self.checkouts)
        why = staged.refusal(self.layer.max_diff_lines, earlier=earlier)
        if staged.denied:
            raise RunFailed(f"{after} {why}")
        if why:
            pushed = f"\n- pushed earlier in this run: {earlier}" if earlier else ""
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


@dataclass
class Script:
    path: str
    drive: Callable
    continues: str = ""
    only: str = ""
    overrides: dict[str, dict] = field(default_factory=dict)
    again: str = ""
    repairs: str = ""

    @property
    def run_fields(self) -> dict:
        return {"path": self.path, "alone": bool(self.only), "step_overrides": self.overrides}


def script_file(cfg: config.Config, name: str) -> Path:
    folder = cfg.adws or paths.ADWS_DIR
    if not folder.is_dir():
        raise FileNotFoundError(f"adws folder {folder} does not exist")
    if not (found := folder / f"{name}.py").is_file():
        raise FileNotFoundError(f"no adw script {found}")
    return found


def flow_function(cfg: config.Config, name: str) -> Callable:
    found = script_file(cfg, name)
    if str(found.parent) not in sys.path:
        sys.path.insert(0, str(found.parent))
    spec = importlib.util.spec_from_file_location(f"adws_{name}", found)
    module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if (fn := getattr(module, name, None)) is None:
        raise AttributeError(f"{found} defines no function {name}, which a script in adws/ names after its file")
    return fn


def script_command(cfg: config.Config, name: str, *args: str) -> list[str]:
    script = script_file(cfg, name).resolve()
    if script.parent == paths.ADWS_DIR.resolve():
        entrypoint = (f"import sys, runpy; sys.path[:0] = {[str(paths.PACKAGE_DIR.parent), str(script.parent)]!r}; "
                      f"runpy.run_path({str(script)!r}, run_name='__main__')")
        return [sys.executable, "-I", "-c", entrypoint, *args]
    return scratch.protected_command([sys.executable, str(script), *args])


def answer_text(asked_id: str) -> str:
    try:
        return (paths.RUNS_DIR / asked_id / ANSWER_FILE).read_text()
    except OSError:
        return ""


def run_command(repo_name: str, tid: str | int, force: bool = False, dry_run: bool = False) -> list[str]:
    from . import flows
    cfg = config.load()
    flags = [*(["--force"] if force else []), *(["--dry-run"] if dry_run else [])]
    try:
        adw = flows.wanted(cfg.repo(repo_name)) == flows.ADW
    except (ValueError, TypeError):
        adw = False
    if not adw:
        return control.command(["run", repo_name, str(tid), "--local", *flags])
    newest = state.ticket_runs(repo_name, str(tid), limit=1, whole=True)
    if newest and newest[0]["status"] == "interrupted" and repaired_by(newest[0]["id"]):
        return script_command(cfg, "repair", repo_name, str(tid), *flags)
    name = "revise" if pending_feedback(repo_name, str(tid)) else "feature"
    return script_command(cfg, name, repo_name, str(tid), *flags)


def exec_script(repo_name: str, tid: str, dry_run: bool, force: bool) -> int:
    try:
        command = run_command(repo_name, tid, force, dry_run)
    except FileNotFoundError as e:
        print(e)
        return 1
    sys.stdout.flush()
    os.execv(command[0], command)


def script_refusal(cfg: config.Config, repo: config.Repo, tid: str, script: Script, dry_run: bool = False) -> str:
    if script.repairs:
        if script.continues or script.only or script.again or dry_run:
            return "--repair does not go with --continue, --only, --from or --dry-run"
        row = state.run_get(script.repairs) or {}
        if (row.get("repo"), str(row.get("issue"))) != (repo.name, str(tid)):
            return f"--repair {script.repairs}: no run of {repo.name} {tid} has that id"
    if script.again and not script.continues:
        return "--from needs --continue"
    if script.again and script.only:
        return "--from and --only do not go together"
    if script.continues:
        row = state.run_get(script.continues) or {}
        if (row.get("repo"), str(row.get("issue"))) != (repo.name, str(tid)):
            return f"--continue {script.continues}: no run of {repo.name} {tid} has that id"
    try:
        layer = config.load_layer(repo.layer_dir)
    except (ValueError, TypeError, OSError):
        layer = None
    for role in config.STEP_ROLES:
        inherited = script.overrides.get(config.INHERITS.get(role, ""), {})
        own = script.overrides.get(role, {})
        if own or inherited:
            try:
                steps.resolve(cfg, layer, role, "", own, inherited)
            except ValueError as e:
                return f"--set: {e}"
    return ""


def run_refusal(repo: config.Repo, script: bool = False) -> tuple[bool, str]:
    from . import flows
    try:
        path, broken = flows.wanted(repo), ""
    except (ValueError, TypeError, OSError) as e:
        path, broken = "classic", f"; {paths.short_path(repo.layer_dir / 'config.toml')}: {e}"
    adw = path == flows.ADW or script
    if repo.parts or adw and flows.has_origin(repo):
        return adw, ""
    return adw, f"{repo.name} has no github remote configured" + (
        " and no git remote origin, which [factory] path = \"adw\" needs" if adw else broken)


def runnable(repo: config.Repo) -> bool:
    return bool(repo.parts) or not run_refusal(repo)[1]


def run_issue(repo_name: str, tid: str | int, dry_run: bool = False, force: bool = False,
              script: Script | None = None) -> int:
    try:
        producer_baseline = scratch.process_ids()
    except OSError:
        producer_baseline = None
    paths.ensure_dirs()
    cfg = config.load()
    repo = cfg.repo(repo_name)
    from . import flows
    adw, refusal = run_refusal(repo, script is not None)
    if refusal:
        print(refusal)
        return 1
    tk = tracker.for_ticket(cfg, repo, tid)
    try:
        tid = tk.normalize(tid)
    except ValueError as e:
        print(f"{repo_name}: {e}")
        return 1
    if adw and not script:
        return exec_script(repo.name, tid, dry_run, force)
    if script and (refusal := script_refusal(cfg, repo, tid, script, dry_run)):
        print(refusal)
        return 2
    name = tracker.ticket_name(repo.name, tk.ref(tid))
    lock = state.run_lock("pipeline", repo.name, tid)
    with util.lock(lock) as got:
        if not got:
            print(f"{name} is already running")
            return 0
        try:
            layer = config.load_layer(repo.layer_dir)
            budget_history = state.ticket_runs(repo.name, tid, limit=1)
            blocked, reset_budget = runlimits.admission(repo, str(tid), tk, runlimits.Policy.of(layer), force)
        except (ValueError, OSError) as e:
            print(f"could not validate the process budget: {e}")
            return 2
        if blocked and not dry_run:
            print(blocked)
            return 2
        if script and script.repairs and (later := state.later_run(repo.name, tid, script.repairs)):
            print(f"run {later['id']} of {name} came after run {script.repairs}, so it is not repaired twice")
            return 1
        with util.lock(SLOTS_LOCK, wait=True):
            slot = RunSlot(repo.name, str(tid), cfg.max_parallel)
            if not dry_run:
                if slots_free_to_start(cfg.max_parallel) <= 0:
                    print(f"{name} was not started: {no_free_slot(cfg.max_parallel)}. Wait for a run to "
                          f"finish or raise max_parallel in {paths.short_path(paths.CONFIG_FILE)}.")
                    return 1
                slot.hold()
            only = script.only if script else ""
            earlier = [] if only else state.ticket_runs(repo.name, tid, limit=1, whole=True)
            restarting = bool(earlier) and earlier[0]["status"] == "interrupted"
            mending = (script.repairs or repaired_by(script.continues or (earlier[0]["id"] if restarting else ""))
                       ) if script else ""
            run_id = record.start("pipeline", tag=f"{repo.name}-{tid}", lock=lock, repo=repo.name, issue=tid,
                                  only_step=only)
            scratch.RUN_BASELINES[run_id] = producer_baseline
            run = Run(run_id, repo, paths.RUNS_DIR / run_id, dry_run, tracker=tk, tid=tid, force=force,
                      feedback=[] if mending else pending_feedback(repo.name, tid), repairs=mending,
                      answering=earlier[0] if earlier and earlier[0]["status"] == "needs-info" else {},
                      **(script.run_fields if script else {}))
            run.dir.mkdir(parents=True, exist_ok=True)
            if mending:
                lines.write(run_id, "repair", source=mending)
                run.log(f"repairing the review findings of run {mending}")
        opening_waits(run_id, run.answering,
                      "" if dry_run or run.alone else state.take_passed_over(repo.name, str(tid)))
        if restarting:
            run.log(f"restarting after run {earlier[0]['id']} was interrupted")
        flow = (flows.start(cfg, run, earlier[0] if restarting else {}, slot, script.continues, script.only,
                            script.again) if script else None)
        if run.feedback:
            run.log(f"revising {', '.join(run.feedback_prs)} for {util.count(len(run.feedback), 'comment')}")
            if not dry_run and not run.alone:
                state.take_feedback(run.feedback, run_id)
        signal.signal(signal.SIGTERM, stop_on_term(run_id))
        pr_url, passed = "", None
        try:
            parent = (script.repairs or script.continues if script else "") or (
                earlier[0]["id"] if earlier and earlier[0]["status"] not in ("done", "merged", "rejected") else "")
            if budget_history and budget_history[0]["status"] not in ("done", "merged", "rejected"):
                parent = budget_history[0]["id"]
            previous = "" if reset_budget else parent
            fresh = bool(previous) and force and not restarting and not run.repairs and not run.alone and not (
                flow and flow.continues)
            current = tk.get(tid) if fresh else None
            run.budget = runlimits.Budget(run, runlimits.Policy.of(layer), previous, current, fresh)
            with run.budget.watching():
                if not (flow and flow.refusal):
                    run.budget.capture(current or tk.get(tid))
                status, summary, pr_url = flows.driven(flow, script.drive) if script else _run(cfg, run, slot)
            if flow and flow.only:
                status, summary, passed = flows.alone(flow)
        except runlimits.Parked as e:
            reason = str(e)
            status, summary = "failed", f"parked: {reason}; branch and evidence retained, workspace left in place. {runlimits.RETRY}"
            kept = run.budget.park(reason)
            record.note(run.id, **kept)
            pr_url = kept["pr_url"]
            run.log(summary)
            run.enter("failed")
        except factory.OnlyDone:
            status, summary, passed = flows.alone(flow)
        except factory.Refused as e:
            status, summary = "failed", str(e)
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
            if run.workspace:
                note_made(*run.workspace)
            slot.free()
        if restarting and status in ("skipped", "noop") and not run.feedback:
            run.enter("failed")
            run.say(f"I did not restart this after the interruption: {summary}. Remove "
                    f"`{tk.label_name('failed')}` to retry.")
        if run.feedback and not dry_run and not run.alone:
            state.settle_feedback(run_id, {"pr": "done", "noop": "recorded"}.get(status, "failed"))
        if status == "failed":
            if flow and flow.matching:
                flow.route()
            if not lines.ended_in_error(run_id, reply="respond" if flow else ""):
                lines.write(run_id, "error", step=lines.last_step(run_id), message=summary)
        run.log(f"done: {status} {pr_url or summary}")
        record.settle(run_id, status, summary=summary, pr_url=pr_url)
        write_report(state.run_get(run_id) or {"id": run_id, "status": status, "pr_url": pr_url}, flow, run)
        if status == "skipped":
            print(f"{name} was not run: {summary}. Pass --force to run it anyway.")
        return 0 if (status in SUCCEEDED if passed is None else passed) else 1


def _fail(run: Run, why: str, builder_said: str = "", detail: str = "") -> tuple[str, str, str]:
    run.log(f"failed: {why}")
    run.enter(run.failed_state)
    extra = f"\n\n{detail.strip()}" if detail.strip() else ""
    if builder_said.strip():
        extra += f"\n\n### What the builder said\n\n{builder_said.strip()[:4000]}"
    task = ("apply your feedback" if run.feedback else "repair the review findings" if run.repairs
            else "finish this one")
    run.say(f"Could not {task}: {why}{extra}\n\nRun `{run.id}`; details in `arete status` on the "
            f"Mac. {run.retry_hint}.")
    return "failed", why, ""


def write_report(row: dict, flow: factory.Flow | None = None, run: Run | None = None) -> None:
    from . import flows
    facts = run.facts if run else flows.seam_facts(row["id"])
    fields = report_fields(row, facts)
    record.note(row["id"], wait_minutes=json.dumps(fields["wait_minutes"]))
    if flow:
        flow.respond(**fields)
    else:
        lines.write(row["id"], "report", **fields)


def report_fields(row: dict, facts: dict) -> dict:
    read = lines.read(row["id"])
    asked = next((line for line in reversed(read) if line.get("event") == "question"), {})
    return {"task": str(row.get("issue") or ""), "status": row.get("status") or "",
            "prs": (row.get("pr_url") or "").split(), "summary": row.get("summary") or "",
            "requirements": facts.get("requirements") or [], "assumptions": facts.get("assumptions") or [],
            "left_for_a_person": facts.get("left_for_a_person") or "", "held": facts.get("held") or "",
            "review": facts.get("review") or report_review({}), "question": str(asked.get("summary") or ""),
            "steps": factory.tally(row["id"]), "minutes": run_minutes(row),
            "cost_usd": round(float(row.get("cost_usd") or 0), 4), "wait_minutes": lines.wait_minutes(row["id"])}


def round_name(name: str, n: int) -> str:
    return name if n == 1 else f"{name}{n}"


def review_findings(review: dict) -> str:
    return "\n".join(f"- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in review.get("problems", []))


def repaired_by(run_id: str) -> str:
    return str(factory.first(lines.read(run_id), "repair").get("source") or "") if run_id else ""


def report_review(review: dict) -> dict:
    return {"verdict": str(review.get("verdict") or ""), "summary": str(review.get("summary") or ""),
            "unresolved": review.get("verdict") == "request_changes",
            "problems": [{"file": p["file"], "text": p["problem"], "severity": p["severity"]}
                         for p in review_problems(review.get("problems"))]}


def note_review(run: Run, review: dict, verified: verify.Verified) -> None:
    run.facts.update(requirements=verified.requirements(), review=report_review(review))


def assumptions_of(plan: dict) -> list[str]:
    listed = plan.get("assumptions")
    return [a.strip() for a in listed if isinstance(a, str) and a.strip()] if isinstance(listed, list) else []


def opening_waits(run_id: str, answering: dict, passed_over: str) -> None:
    now = util.iso()
    if answering:
        asked = next((line.get("ts") for line in reversed(lines.read(answering["id"]))
                      if line.get("event") == "question"), None) or answering.get("finished")
        if asked:
            lines.waited(run_id, "person", asked, passed_over or now)
    if passed_over:
        lines.waited(run_id, "slot", passed_over, now)


def run_minutes(row: dict) -> float:
    if not (row.get("started") and row.get("finished")):
        return 0
    return round((util.parse_iso(row["finished"]) - util.parse_iso(row["started"])).total_seconds() / 60, 1)


def _ask(run: Run, ws: Path, checkouts: list[Checkout], comment: str, summary: str) -> tuple[str, str, str]:
    lines.write(run.id, "question", question=comment, summary=summary[:300])
    run.say(comment)
    run.enter("needs-info")
    remove_workspace(ws, checkouts)
    return "needs-info", summary[:300], ""


def checked_layer(cfg: config.Config, layer_dir: Path) -> config.RepoLayer:
    try:
        layer = config.load_layer(layer_dir)
        steps.check(cfg, layer)
    except (ValueError, TypeError) as e:
        raise RunFailed(f"{paths.short_path(layer_dir / 'config.toml')}: {e}") from e
    return layer


def resolve_layer(cfg: config.Config, run: Run, ws: Path) -> config.RepoLayer:
    repo = run.repo
    if repo.layer:
        return checked_layer(cfg, repo.layer)
    if (ws / ".agents" / "config.toml").exists():
        return checked_layer(cfg, ws / ".agents")
    run.log(f"layer: {paths.short_path(repo.path / '.agents')} is not committed; lessons stay in the main checkout")
    return checked_layer(cfg, repo.path / ".agents")


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
    lines.write(run.id, "task", ticket=t.ref, kind=t.kind, repo=repo.name, title=t.title)
    if run.answering:
        lines.write(run.id, "answer", asked_in=run.answering["id"], question=run.answering.get("summary") or "",
                    text=answer_text(run.answering["id"]))
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

    main_layer = checked_layer(cfg, repo.layer_dir)
    branch = open_branch(run) or fill_template(main_layer.branch, config.RepoLayer.branch, kind=t.kind, id=t.id,
                                               slug=slug(t.title))
    if not run.dry_run:
        tk.prepare()
    run.enter("running")

    try:
        ws, checkouts = make_workspace(run.id, repo, main_layer.base_branch, branch, t.id, main_layer.copy)
        run.workspace = ws, checkouts
        for c in checkouts:
            run.log(f"worktree {c.wt} on {branch}, {c.starting_point}")
        run.cfg, run.layer, run.kind = cfg, main_layer, t.kind
        note_fresh_bases(checkouts)
        merge_moved_bases(run, [c for c in checkouts if c.resumes], main_layer, t)
    except RunFailed as e:
        return _fail(run, e.why, detail=e.detail)
    layer = resolve_layer(cfg, run, ws)
    run.cfg, run.layer, run.kind = cfg, layer, t.kind
    if why := furnish(cfg, run, layer, ws):
        return _fail(run, why)

    expertise = layer.expertise_file.read_text() if layer.expertise_file.exists() else "(none yet)"
    checks = check_list(layer)
    check_commands, check_rules = check_script(run.dir, layer, ws)
    runs_checks = check_calls(check_commands)
    reads = git_reads(checkouts)
    builder_bash = layer.builder_bash + reads + check_rules
    commands = code_list(layer.builder_bash + reads + check_commands)
    workspace = workspace_note(branch, checkouts)
    read_rules = bash_rules(config.DEFAULT_ALLOW_BASH + reads)
    build_rules = bash_rules(builder_bash)
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
                                         feedback=feedback, expertise=expertise, checks=runs_checks,
                                         commands=commands, max_lines=limit),
                        ws, role="build", allowed_tools=build_rules, disallowed_tools=runner.NO_GIT_WRITES,
                        json_schema=REVISE_SCHEMA)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "revise step failed", res.text)
        run.facts["left_for_a_person"] = left_for_a_person
        build_summary = res.structured.get("summary", "")
        replies = res.structured.get("replies") or []
        if stage(checkouts, deny).empty:
            return answer_without_changes(run, ws, checkouts, branch, build_summary, replies)
    else:
        res = run.agent("plan", prompt("plan", repo=repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                        body=t.body, comments=t.comments, attachments=attachments,
                                        triage=triage_notes(approval), expertise=expertise, commands=commands,
                                        denied=code_list(deny), max_lines=limit, checks=checks),
                        ws, role="plan", allowed_tools=read_rules, json_schema=PLAN_SCHEMA)
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
        run.facts.update(assumptions=assumptions_of(plan), left_for_a_person=left_for_a_person)

        res = run.agent("build", prompt("build", repo=repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                         body=t.body, comments=t.comments, attachments=attachments, plan=plan_text,
                                         expertise=expertise, checks=runs_checks, commands=commands, max_lines=limit,
                                         estimate=estimate, verify=goal),
                        ws, role="build", allowed_tools=build_rules, disallowed_tools=runner.NO_GIT_WRITES)
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
    b = Build(cfg=cfg, run=run, ticket=t, ws=ws, checkouts=checkouts, layer=layer, timeout=layer.timeout_minutes * 60,
              workspace=workspace, attachments=attachments, tools=build_rules, deny=deny, expertise=expertise,
              left_for_a_person=left_for_a_person, check_commands=runs_checks,
              plan_text=plan_text, build_summary=build_summary)

    attempts, tries = 1, run.settings("check_repair").tries
    ok = b.check()
    while not ok and attempts < tries:
        run.story.append(f"Checks failed on attempt {attempts}:\n{util.tail(b.report, 2500)}")
        attempts += 1
        b.resume(f"fix{attempts}", prompt("fix", attempt=attempts, max_attempts=tries, failures=b.report,
                                          checks=runs_checks), role="check_repair")
        ok = b.check()
    run.save("checks.md", b.report)
    record.note(run.id, attempts=attempts)
    if not ok:
        run.story.append("Checks still failing after the last attempt.")
        _learn(run, ws, layer.expertise_file, t.title, expertise, apply=False)
        return _fail(run, f"checks still failing after {attempts} attempts")

    b.require_valid_diff("the build and its fixes", f" (the plan estimated {estimate})" if estimate else "")

    verified = verify_in_use(b, goal, build_summary)
    reviews, n = max(1, run.settings("review").tries), 1
    review = _review(b, plan_to_review, verified)
    note_review(run, review, verified)
    while review.get("verdict") == "request_changes" and n < reviews:
        if problems := review_findings(review):
            run.story.append(f"Review requested changes:\n{problems}")
            b.resume(round_name("review_fix", n), prompt("review_fix", problems=problems), role="review_repair")
            b.recheck("the review fixes")
            verified = verify_in_use(b, goal, build_summary)
        n += 1
        review = _review(b, plan_to_review, verified, round_name("review", n))
        note_review(run, review, verified)
    draft_why = draft_reason(review, verified, n)
    note_review(run, review, verified)
    run.facts["held"] = draft_why
    _learn(run, ws, layer.expertise_file, t.title, expertise, apply=True)
    staged = b.restage()
    size = f"{len(staged.files)} files, {staged.lines} lines"
    outcome = f"{size}, review {review.get('verdict')}, in use {verified.verdict}"
    written = document.pr_text(run, t, summary_of(review, build_summary), review, verified, size, draft_why, cfg, layer)

    if run.dry_run:
        run.save("diff.patch", staged.patch)
        run.save("pr_body.md", outbound.screen(report_body(
            run, summary=build_summary, review=review, verified=verified, left=left_for_a_person, images={},
            approval=approval, attempts=attempts, checks=checks, plan_text=plan_text, size=size, held=draft_why,
            written=written)))
        run.log(f"dry run: {outcome}; worktree kept at {ws}")
        return "dry-run", outcome, ""

    subject = fill_template(layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    pr_report = report_body(run, summary=build_summary, review=review, verified=verified, left=left_for_a_person,
                            images=evidence_images(run, staged.changed[0], verified), approval=approval,
                            attempts=attempts, checks=checks, plan_text=plan_text, size=size,
                            held=draft_why, written=written)
    revised = "Revised for the review feedback on the PR.\n\n" if run.feedback else ""
    link = tk.pr_link(t, whole=not left_for_a_person)
    message = f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}"
    b.push(branch, message)
    opened: list[Checkout] = []
    for c in staged.changed:
        if not c.pr:
            c.pr = gh.pr_create(c.github, branch, c.base, tk.pr_title(subject, t), f"{link}\n\n{pr_report}",
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
                gh.pr_edit_body(c.pr, f"{link}\n\n{pr_report}{others}")
            continue
        body = gh.pr_body(c.pr)
        new_body = synced_pr_body(run, c.pr, body, t, left_for_a_person)
        if opened:
            new_body = with_siblings(new_body, others)
        if new_body != body:
            gh.pr_edit_body(c.pr, new_body)
        if c in pushed and c.pr not in run.feedback_prs:
            gh.pr_comment(c.pr, f"Pushed more work for {t.ref}.\n\n{pr_report}")
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
        run.say(f"Revised for your review.\n\n{pr_report}\n\nCI: {ci_result}.")
    else:
        run.say(f"PRs for this ticket:\n\n{pr_lines}\n\nCI: {ci_result}."
                + ("" if not opened else draft_note(draft_why, verified)))
    automerge(cfg, run, layer, approval, draft_why, opened, f"{t.title}\n\n{build_summary.strip()[:3000]}")
    urls = record_prs(run, branch, with_prs)
    remove_workspace(ws, checkouts)
    return "pr", outcome, urls


def furnish(cfg: config.Config, run: Run, layer: config.RepoLayer, ws: Path) -> str:
    for rel in layer.copy:
        if (src := run.repo.path / rel).exists():
            (dest := ws / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    for cmd in layer.setup:
        with record.timed(run.id, "setup", command=cmd) as setup:
            res = environment.sh(cmd, cfg, run.repo, cwd=ws, timeout=1200)
            setup["exit"] = res.returncode
        run.log(f"setup `{cmd}`: exit {res.returncode}")
        if res.returncode != 0:
            run.save("setup.log", res.stdout + res.stderr)
            return f"setup command `{cmd}` failed"
    return ""


def summary_of(review: dict, build_summary: str) -> str:
    return cut_at_end(str(review.get("changes") or "").strip() or build_summary, SUMMARY_CHARS)


def report_body(run: Run, *, summary: str, review: dict, verified: verify.Verified, left: str,
                images: dict[str, str], approval: dict | None, attempts: int, checks: str, plan_text: str,
                size: str, held: str = "", written: str = "") -> str:
    problems = review.get("problems", [])
    return public_text(run, (
        f"## Summary\n\n{written or summary_of(review, summary)}\n\n"
        + ("" if run.feedback else triage_block(approval))
        + left_block(left)
        + f"{CHECKED_IN_USE_HEADING}\n\n{verified.markdown(images)}\n\n"
        f"## Review\n\n**{review.get('verdict', 'none')}**: {review.get('summary', '')}\n"
        + "".join(f"\n- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in problems)
        + (f"\n\nHeld as a draft: {held}." if held else "")
        + f"\n\n## Checks\n\nAll passed after {util.count(attempts, 'attempt')}: {checks}\n\n"
        + ("" if run.feedback else
           f"<details><summary>Plan</summary>\n\n{cut_at_end(plan_text, PLAN_CHARS)}\n\n</details>\n\n")
        + f"Run `{run.id}`, {size}, {util.paid_note(record.metered_spent(run.id))}."
        + (f" Repairs the review findings of run `{run.repairs}`." if run.repairs else "")
    ))


def automerge(cfg: config.Config, run: Run, layer: config.RepoLayer, approval: dict | None, draft_why: str,
              opened: list[Checkout], about: str) -> None:
    if draft_why or layer.trust_level != "automerge":
        return
    if approval and approval["sent_by"] == "triage":
        run.log("automerge left off: triage approved the ticket, so a person merges it")
        return
    try:
        risk = jev.risk(patch_from_base(opened), about, cfg,
                        trace=(run.id, "automerge risk jev")) if opened and jev.ready(cfg, [run.repo]) else None
        hold = risk.hold if risk else ""
    except jev.Unjudged as e:
        run.log(f"automerge risk: JEV could not judge the diff: {e}")
        hold = "JEV could not judge the diff"
    for c in opened:
        try:
            if hold:
                run.log(f"automerge left off on {c.pr}: {hold}")
                gh.pr_comment(c.pr, public_text(run, f"Automerge stays off for this PR: {hold}. A person merges it."))
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
                gh.pr_comment(c.pr, public_text(run, f"Automerge could not merge this PR: {why}. A person merges it."))
        except gh.GhError as e:
            run.log(f"automerge failed on {c.pr}: {e}")


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
    return public_text(run, left_synced)


def left_section(run: Run, text: str) -> str:
    if len(text) <= MAX_LEFT_FOR_A_PERSON_CHARS:
        return outbound.screen(text)
    return outbound.screen(
        f"{text[:MAX_LEFT_FOR_A_PERSON_CHARS]}\n\n(Cut at {MAX_LEFT_FOR_A_PERSON_CHARS} characters. The whole "
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
        if not git(["diff", "--cached", "--name-only"], c.wt).strip():
            continue
        try:
            util.git_as_owner(["commit", "-q", "-m", message], c.wt)
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


CiFix = Callable[[str, str, str], Result]


def watch_ci(b: Build, slot: RunSlot, branch: str, pushed: list[Checkout], with_prs: list[Checkout],
             draft_why: str, fix: CiFix | None = None) -> str:
    run, max_rounds = b.run, b.run.settings("ci_repair").tries
    stopped_before = [c for c in pushed if stopped_by_ci(run, c)]
    rounds: list[ci.Round] = []
    try:
        for current in ci_rounds(b, slot, branch, pushed, max_rounds, fix):
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


def ci_rounds(b: Build, slot: RunSlot, branch: str, pushed: list[Checkout], max_rounds: int,
              fix: CiFix | None = None) -> Iterator[ci.Round]:
    run = b.run
    watched = list(pushed)
    not_caused: dict[tuple[str, str, str], str] = {}
    for n in range(1, max_rounds + 1):
        slot.free()
        run.log(FREED_SLOT_FOR_CI)
        with run.waiting("ci"):
            current = ci.Round(n, [ci.compare_with_base(ci.watch(ci_target(c), run.log), run.log) for c in watched])
        judged = [r for r in current.reds if r.kind != "base"]
        for r in judged:
            if r.key in not_caused:
                r.caused, r.reason = False, not_caused[r.key]
        fresh = [r for r in judged if r.key not in not_caused]
        if fresh:
            slot.take(run.log, run.waiting)
        try:
            fix_summary = judge_ci(b, fresh, n, max_rounds, watched, fix) if fresh else ""
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
    if staged.empty:
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


def unmerged_paths(wt: Path) -> list[str]:
    return util.git_records(["diff", "--name-only", "--diff-filter=U"], wt)


def uncommitted(wt: Path) -> list[str]:
    return [r[3:] for r in util.git_records(["status", "--porcelain", "--no-renames", "--untracked-files=all"], wt)]


def marked(wt: Path, names: list[str]) -> list[str]:
    return [n for n in names if (wt / n).is_file()
            and CONFLICT_MARKER.search((wt / n).read_text(errors="replace"))]


def abort_merge(wt: Path) -> None:
    if git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wt, check=False).strip():
        git(["merge", "--abort"], wt, check=False)


def resolution_diff(c: Checkout) -> diffguard.Diff:
    if not c.resolved:
        return diffguard.Diff()
    return diffguard.between(c.wt, c.base_commit, "HEAD", "--", *c.resolved, prefix=c.prefix)


def note_fresh_bases(checkouts: list[Checkout]) -> None:
    for c in checkouts:
        if not c.resumes:
            c.base_commit = git(["rev-parse", "HEAD"], c.wt).strip()


def merge_moved_bases(run: AgentRun, checkouts: list[Checkout], layer: config.RepoLayer, ticket: tracker.Ticket,
                      log_prefix: str = "") -> list[Checkout]:
    merged = []
    for c in checkouts:
        c.base_commit = git(["rev-parse", c.origin_base], c.wt).strip()
        if git(["rev-list", "--count", f"HEAD..{c.base_commit}"], c.wt).strip() == "0":
            continue
        if edits := uncommitted(c.wt):
            raise RunFailed(f"{c.github} has uncommitted edits ({', '.join(edits[:5])}), so arete merged nothing into "
                            "them and left them as they are")
        head = git(["rev-parse", "HEAD"], c.wt).strip()
        with record.timed(run.id, "base_merge", repo=c.github, base=c.base, base_commit=c.base_commit,
                          head=head) as rec:
            try:
                try:
                    with util.repo_lock(c.src):
                        util.git_as_owner(["merge", "-q", "--no-edit", c.base_commit], c.wt)
                except RuntimeError as e:
                    conflicts = unmerged_paths(c.wt)
                    if not conflicts:
                        raise RunFailed(f"git could not merge `{c.origin_base}` into the branch in {c.github}: {e}")
                    rec["conflicts"] = conflicts
                    resolve_conflicts(run, c, conflicts, layer, ticket)
                else:
                    conflicts = []
                rec["outcome"] = "resolved" if conflicts else "clean"
                rec["merge"] = c.merge = git(["rev-parse", "HEAD"], c.wt).strip()
            except BaseException:
                abort_merge(c.wt)
                raise
        c.merged_base, c.resolved = True, conflicts
        c.resolved_lines = resolution_diff(c).lines
        run.log(f"{log_prefix}merged `{c.origin_base}` into the branch in {c.github}, "
                + (f"and resolved conflicts in {', '.join(conflicts)}" if conflicts
                   else "since the base moved after the last push"))
        merged.append(c)
    return merged


def resolve_conflicts(run: AgentRun, c: Checkout, conflicts: list[str], layer: config.RepoLayer,
                      ticket: tracker.Ticket) -> None:
    deny = diffguard.denied_paths(layer)
    if protected := [p for p in conflicts if util.matches_any(p, deny) or util.matches_any(c.prefix + p, deny)]:
        raise RunFailed(f"merging `{c.origin_base}` conflicts in protected paths in {c.github} "
                        f"({', '.join(protected)}), which a person must merge")
    tries = max(1, run.settings("build").tries)
    feedback = "this is the first attempt"
    for n in range(1, tries + 1):
        untracked_before = untracked_files(c.wt)
        ignored_before = ignored_state(c.wt)
        previous = run.sessions.get("build")
        text = prompt("resolve", repo=run.repo.name, github=c.github, base=c.origin_base,
                      base_commit=c.base_commit[:12], head=git(["rev-parse", "HEAD"], c.wt).strip()[:12],
                      ref=ticket.ref, title=ticket.title, body=ticket.body,
                      conflicts="\n".join(f"- `{p}`" for p in conflicts),
                      branch_log=git(["log", "--oneline", "-n30", "MERGE_HEAD..HEAD"], c.wt),
                      base_log=git(["log", "--oneline", "-n30", "HEAD..MERGE_HEAD"], c.wt),
                      commands=code_list(layer.builder_bash), attempt=n, tries=tries, feedback=feedback)
        try:
            res = run.agent(round_name("resolve", n), text, c.wt, role="build",
                            allowed_tools=bash_rules(layer.builder_bash), disallowed_tools=runner.NO_GIT_WRITES,
                            json_schema=RESOLVE_SCHEMA, resume=NO_SESSION, extra_env=dict(RESOLVER_SETS))
        finally:
            if previous is None:
                run.sessions.pop("build", None)
            else:
                run.sessions["build"] = previous
        if not res.ok:
            raise RunFailed(f"the conflict resolution step failed: {res.error}", res.text)
        answer = res.structured if isinstance(res.structured, dict) else {}
        if answer.get("resolved") is not True:
            raise RunFailed(f"the resolver could not keep both the branch's work and the base's in {c.github}: "
                            f"{answer.get('summary') or 'no reason given'}", res.text)
        for name in untracked_files(c.wt) - untracked_before:
            if Path(name).parent.name == "__pycache__" and Path(name).suffix == ".pyc":
                remove_untracked(c.wt, name)
        ignored_after = ignored_state(c.wt)
        changed_ignored = {n for n in ignored_before.keys() | ignored_after.keys()
                           if ignored_before.get(n) != ignored_after.get(n)}
        touched = (set(util.git_records(["diff", "--name-only"], c.wt)) | (untracked_files(c.wt) - untracked_before)
                   | changed_ignored)
        if outside := sorted(touched - set(conflicts)):
            raise RunFailed(f"the resolver edited files outside the conflict in {c.github}: {', '.join(outside)}")
        if left := marked(c.wt, conflicts):
            feedback = f"conflict markers remain in {', '.join(left)}; remove every one"
            if n == tries:
                raise RunFailed(f"conflict markers remain in {', '.join(left)} after {util.count(n, 'attempt')} "
                                f"in {c.github}")
            continue
        git(["add", "-A", "--", *conflicts], c.wt)
        if unmerged_paths(c.wt):
            raise RunFailed(f"files are still unmerged in {c.github} after the resolver ran")
        with util.repo_lock(c.src):
            util.git_as_owner(["commit", "-q", "--no-edit"], c.wt)
        return


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
    merged = merge_moved_bases(b.run, fetched, b.layer, b.ticket, "ci: ")
    if not merged:
        return ""
    names = ", ".join(f"`{c.origin_base}` into `{c.prefix or './'}`" for c in merged)
    fixed = "".join(f" Arete resolved merge conflicts in {', '.join(f'`{p}`' for p in c.resolved)} of "
                    f"`{c.prefix or './'}`." for c in merged if c.resolved)
    return (f"Before this step arete merged {names}, since the base moved after the last push.{fixed} CI tested the PR "
            "merged with that base, so the worktree now holds what CI ran, and files the base added are yours to "
            "edit like any other.")


def judge_ci(b: Build, reds: list[ci.Red], n: int, max_rounds: int, watched: list[Checkout],
             fix: CiFix | None = None) -> str:
    run = b.run
    try:
        merged_base = merge_moved_bases_for_ci_fix(b, watched)
    except RunFailed as e:
        for r in reds:
            r.reason = "Arete could not merge the moved base before judging this check."
        raise RunFailed(e.why, ci.red_list(reds))
    logs = run.dir / "ci"
    logs.mkdir(exist_ok=True)
    if logs not in run.add_dirs:
        run.add_dirs.append(logs)
    failures = "\n\n".join(ci.failure_note(r, logs / f"round{n}-{i}-{slug(r.label)}.log")
                           for i, r in enumerate(reds, 1))
    diff = ", ".join(f"`{c.diff_from_base}`" for c in b.checkouts)
    left = NOT_CAUSED_LEFT_FOR_A_PERSON + b.left_for_a_person if b.left_for_a_person else ""
    name = b.next_step("ci_fix")
    text = prompt("ci_fix", round=n, max_rounds=max_rounds, failures=failures, diff=diff, checks=b.check_commands,
                  merged_base=merged_base, left_for_a_person=left)
    res = fix(name, text, failures) if fix else b.resume(name, text, role="ci_repair", json_schema=CI_FIX_SCHEMA)
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
            gh.pr_edit_body(c.pr, public_text(run, ci.with_section(gh.pr_body(c.pr), ci_section)))
        except gh.GhError as e:
            run.log(f"ci: could not write the CI section on {c.pr}: {util.first_line(str(e), 300)}")


def images_note(images: tracker.SavedImages) -> str:
    rows = []
    if images.paths:
        rows += ["## Images on the issue", "", "Open each one with Read; they show what the issue describes.", ""]
        rows += [f"- `{p}`" for p in images.paths]
    if images.left_out:
        rows += ["", (f"Not downloaded, over the {tracker.MAX_IMAGES} image or "
                      f"{tracker.MAX_IMAGE_BYTES // 2**20} MB cap: {', '.join(images.left_out)}")]
    return "\n".join(rows).strip()


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
            gh.pr_reply(url, thread, public_text(run, text))
        except gh.GhError as e:
            run.log(f"reply to comment {thread} on {url} failed: {util.first_line(str(e), 300)}")


def answer_without_changes(run: Run, ws: Path, checkouts: list[Checkout], branch: str, summary: str,
                           replies: list[dict]) -> tuple[str, str, str]:
    if run.dry_run:
        run.log("revise: no code changes; a dry run leaves the review unanswered")
        remove_workspace(ws, checkouts)
        return "dry-run", "no code changes for the review", ""
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
                      b.ws, role="review", allowed_tools=bash_rules(b.read_bash),
                      json_schema=REVIEW_SCHEMA)
    if not res.ok or not isinstance(res.structured, dict):
        return {"verdict": "request_changes", "summary": "review step failed", "problems": []}
    return {**res.structured, "problems": review_problems(res.structured.get("problems"))}


def review_problems(listed) -> list[dict]:
    if not isinstance(listed, list):
        return []
    return [{"file": p.get("file") or "", "problem": p.get("problem") or "", "severity": p.get("severity") or "unrated"}
            for p in listed if isinstance(p, dict)]


@runlimits.verification
def verify_in_use(b: Build, goal: str, summary: str) -> verify.Verified:
    if not b.layer.verify:
        b.run.log("verify: the layer has no [verify] section, so nothing checks the change in use")
        return verify.not_configured(b.layer.dir, b.ws)
    tries = b.run.settings("verify").tries
    for attempt in range(1, tries + 1):
        verified = check_in_use(b, goal, summary)
        if verified.verdict != "fail":
            return verified
        b.run.story.append(f"The check in use failed on attempt {attempt}:\n{util.tail(verified.markdown(), 2500)}")
        if attempt == tries:
            break
        b.resume(b.next_step("verify_fix"), prompt("verify_fix", attempt=attempt + 1, max_attempts=tries,
                                                   failures=verified.markdown(), checks=b.check_commands),
                 role="check_repair")
        b.recheck("fixing what the check in use found")
    b.run.story.append("Still failing in use after the last attempt.")
    _learn(b.run, b.ws, b.layer.expertise_file, b.ticket.title, b.expertise, apply=False)
    raise RunFailed(f"it still does not work in use after {util.count(tries, 'attempt')}",
                    verified.markdown())


def check_in_use(b: Build, goal: str, summary: str) -> verify.Verified:
    return use_product(b, "verify", "verify", goal=goal, summary=summary.strip()[:4000],
                       diff=util.tail(b.staged.patch, MAX_DIFF_IN_PROMPT))


def use_product(b: TicketWork, step_name: str, prompt_name: str, role: str = "verify", **values) -> verify.Verified:
    run, v, t = b.run, verify.with_free_ports(b.layer.verify), b.ticket
    step = b.next_step(step_name)
    if refusal := verify.port_refusal(v):
        run.log(f"{step}: {refusal}")
        return verify.Verified("blocked", "Verification cannot use a port the live dashboard serves.",
                               blocked_by=refusal, finished=False)
    if ready := verify.preflight(v, b.ws, b.cfg, run.repo):
        run.save(f"{step}-preflight.json", json.dumps(asdict(ready), indent=2))
        run.log(f"{step}: preflight {ready.verdict}")
        if ready.verdict == "blocked":
            ready.saved = os.path.abspath(run.dir / f"{step}-preflight.md")
            run.save(f"{step}-preflight.md", ready.markdown())
            return ready
    evidence = run.dir / "evidence"
    evidence.mkdir(exist_ok=True)
    if evidence not in run.add_dirs:
        run.add_dirs.append(evidence)
    untracked_before = untracked_by_dir(b.git_dirs)
    attempt = None
    try:
        attempt = scratch.make(run.id, step, b.ws, run.dir)
    except (OSError, ValueError) as e:
        run.log(f"{step}: trusted scratch unavailable: {util.first_line(str(e), 300)}")
    agent_started, agent_finished = False, False
    lifetime = {"stopped": True}
    fixture_blocker = ""
    try:
        if attempt:
            run.log(f"{step}: scratch folder {attempt.root}")
            v = verify.with_scratch(v, attempt.root)
        else:
            run.log(f"{step}: ordinary verification without trusted scratch authority")
            if verify.SCRATCH_FIELD in v.start:
                fixture_blocker = "Required service needs unavailable scratch fixtures for start command: " + json.dumps(v.start)
                v = replace(v, start="", url="", notes=v.notes + "\n" + fixture_blocker)
        bash = b.read_bash + v.bash
        with verify.serving(
                v, b.ws, run.dir / f"{step}-server.log", b.cfg, run.repo,
                sets={scratch.ENV_NAME: attempt.root} if attempt else None, lifetime=lifetime,
                attempt=attempt) as url:
            if url:
                run.log(f"{step}: the app answers at {url}")
            agent_started = True
            res = run.agent(step, prompt(prompt_name, repo=run.repo.name, workspace=b.workspace, ref=t.ref,
                                         title=t.title, body=t.body, comments=t.comments, attachments=b.attachments,
                                         surface=surface_note(v, url, evidence), notes=v.notes,
                                         scratch=scratch.note(attempt),
                                         commands=code_list(bash), checks=check_list(b.layer), **values),
                            b.ws, role=role,
                            allowed_tools=bash_rules(bash) + ([f"mcp__playwright__{t}" for t in verify.PLAYWRIGHT_TOOLS] if url else []),
                            disallowed_tools=runner.NO_GIT_WRITES, json_schema=verify.SCHEMA,
                            mcp_config=verify.mcp_config(evidence) if url else "",
                            scratch=attempt)
            agent_finished = True
    except scratch.ConfinementUnavailable as e:
        return verify.Verified("blocked", "Trusted fixture verification could not start.", finished=False,
                               blocked_by=util.first_line(str(e), 300))
    except verify.ServerError as e:
        run.log(f"{step}: the app did not start")
        return verify.Verified("fail", f"The app did not start: {e}")
    finally:
        try:
            if not attempt:
                archived, moved, kept = "", [], []
            elif not lifetime["stopped"] or (agent_started and not agent_finished):
                scratch.retain(attempt)
                archived, moved, kept = "", [], [attempt.root]
            else:
                archived, moved, kept = scratch.end(attempt, run.dir)
            if moved:
                run.log(f"{step}: preserved {util.count(len(moved), 'fixture')} from the scratch folder at "
                        f"{archived}")
            if kept:
                fixture_blocker = "Verification scratch resources remain unresolved; the workspace is retained."
                run.log(f"{step}: retained unresolved scratch at {attempt.root}, and with it the worktree")
        finally:
            restore_worktrees(untracked_before, evidence)
    if not res.ok or not isinstance(res.structured, dict):
        return verify.Verified("blocked", "The check in use did not finish.", finished=False,
                               blocked_by=util.first_line(res.error or "the verifier returned no verdict", 300))
    verified = verify.Verified.from_agent(res.structured)
    if fixture_blocker and verified.verdict == "pass":
        verified.verdict, verified.blocked_by = "blocked", fixture_blocker
    verified.saved = os.path.abspath(run.dir / f"{step}.md")
    run.save(f"{step}.md", verified.markdown())
    run.log(f"{step}: {verified.verdict}, {len(verified.checks) - len(verified.failed_checks)} of "
            f"{util.count(len(verified.checks), 'check')} passed")
    return verified


def surface_note(v: config.Verify, url: str, evidence: Path) -> str:
    if not url:
        return ("Arete started no app for this repo. Run the commands its user runs, with real arguments, and read "
                "the output and the exit code.")
    return (f"Arete started the app with `{v.start}` and it answers at {url}. Arete stops it when you finish.\n\n"
            "- Do in the browser what a person does in the browser, with these `mcp__playwright__` tools (load "
            f"them with ToolSearch first): {', '.join(f'`{t}`' for t in verify.PLAYWRIGHT_TOOLS)}. Read the page "
            "with `browser_snapshot`; no tool runs your own script in the page.\n"
            "- After each state you check, wait until the page settles (loading done, entrance animations finished) "
            "so the shot shows what a person sees, then call `browser_take_screenshot` with `filename` set to an "
            f"absolute path in `{evidence}`, such as `{evidence}/01-list.png`.\n"
            "- Read `browser_console_messages` after each state. Name each console error and each dev error overlay "
            "or badge in `saw`, and say whether the change causes it.\n"
            "- Send API requests with curl, shaped the way the app's clients send them, and read the status and body.")


def restore_worktrees(untracked_before: dict[Path, set[str]], evidence: Path) -> None:
    for d, name in untracked_added_since(untracked_before):
        f = d / name
        if scratch.pending(d) or any(p.startswith(paths.SCRATCH_PREFIX) for p in Path(name).parts):
            continue
        if f.suffix == ".png" and not (evidence / f.name).exists():
            shutil.move(f, evidence / f.name)
        else:
            remove_untracked(d, name)
    for d in untracked_before:
        if not scratch.pending(d):
            git(["checkout", "-q", "--", "."], d)


def evidence_images(run: Run, c: Checkout, verified: verify.Verified) -> dict[str, str]:
    if run.layer and run.layer.verify and not run.layer.verify.publish_evidence:
        run.log("evidence: publication disabled by the layer; screenshots stay in the run folder")
        return {}
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


def draft_reason(review: dict, verified: verify.Verified, reviews: int = 0) -> str:
    if verified.verdict == "blocked":
        return "the check in use was blocked"
    if review.get("verdict") == "approve":
        return ""
    if review.get("verdict") == "request_changes" and reviews:
        return (f"the review still requested changes after {util.count(reviews, 'review')}, the most the review "
                f"step's tries allow, so {util.count(len(review.get('problems', [])), 'finding')} stay unresolved")
    return "the review did not approve it"


def draft_note(draft_why: str, verified: verify.Verified, kept: bool = False) -> str:
    blocked_by = f" {verified.blocked_by}" if verified.verdict == "blocked" else ""
    lead = "The PR stays a draft" if kept else "The new PRs are drafts"
    return f"\n\n{lead}: {draft_why}.{blocked_by}".rstrip() if draft_why else ""


def check_list(layer: config.RepoLayer) -> str:
    return ", ".join(f"`{c}`" for c in layer.checks.values()) or NO_CHECKS


def check_calls(commands: list[str]) -> str:
    return code_list(commands) or NO_CHECKS


def check_script(folder: Path, layer: config.RepoLayer, cwd: Path) -> tuple[list[str], list[str]]:
    if not layer.checks:
        return [], []
    script = folder / CHECKS_SCRIPT
    arms = "".join(f"{shlex.quote(name)}) exec sh -c {shlex.quote(cmd)} ;;\n" for name, cmd in layer.checks.items())
    usage = shlex.quote(f"usage: {script} NAME, where NAME is one of: {', '.join(layer.checks)}")
    folder.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/bin/sh\ncd {shlex.quote(str(cwd))} || exit 1\ncase \"${{1-}}\" in\n{arms}"
                      f"*) echo {usage} >&2\n   exit 2 ;;\nesac\n")
    script.chmod(0o755)
    return [shlex.join([str(script), name]) for name in layer.checks], [f"{shlex.quote(str(script))} *"]


def cut_at_end(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    room = max(0, limit - len(CUT_MARK))
    head = text[:room + 1]
    ends = (head.rfind("\n\n"), max(head.rfind(f"{p}{s}") + 1 for p in ".!?" for s in " \n"), head.rfind(" "))
    at = next((at for at in ends if room // 2 < at <= room), room)
    return head[:at].rstrip() + CUT_MARK


def _learn(run: AgentRun, wt: Path, expertise_file: Path, title: str, expertise: str, apply: bool) -> None:
    if not run.story:
        run.log("learn: clean run, nothing to learn")
        return
    res = run.agent("learn", prompt("learn", repo=run.repo.name, ref=run.ref, title=title,
                                     story="\n\n".join(run.story), expertise=expertise,
                                     max_lines=EXPERTISE_MAX_LINES),
                    wt, role="learn", json_schema=LEARN_SCHEMA, persist=False)
    found = res.structured if res.ok and isinstance(res.structured, dict) else {}
    rewrite = found.get("expertise", "").strip()
    line_count = len(rewrite.splitlines())
    if not rewrite:
        run.log("learn: no expertise file came back, kept the old one")
        return
    if line_count > EXPERTISE_MAX_LINES:
        run.log(f"learn: the rewrite has {line_count} lines, over the {EXPERTISE_MAX_LINES} line cap; kept the old file")
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
    run.log(f"learn: {len(added)} added, {len(removed)} removed, {line_count} lines in {paths.short_path(expertise_file)}")
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
        util.git_as_owner(["commit", "-q", "-m", message, "--", "."], layer_dir)


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
        merged = all(pr.get("mergedAt") for pr in prs)
        if r["status"] == "rejected" and not merged:
            if not closed:
                record.note(r["id"], status="pr")
            continue
        if not closed:
            continue
        for url, pr in zip(urls, prs):
            state.close_feedback(url, pr["state"])
        closed_at = max(pr.get("closedAt") or "" for pr in prs) or util.iso()
        record.note(r["id"], status="merged" if merged else "rejected", finished=closed_at)
    util.write_json(PR_SEEN_FILE, fresh)
    for repo in cfg.repos:
        if repo.pipeline and repo.github and not repo.layer:
            try:
                _maybe_propose_trust(repo)
            except (RuntimeError, OSError) as e:
                print(f"{util.iso()} {repo.name}: trust proposal failed: {util.first_line(str(e), 300)}")


CATCH_UP = ("pr", "needs-info", "failed")


def sync_moves(cfg: config.Config) -> None:
    if not any(cfg.jira.transitions.get(s) for s in CATCH_UP):
        return
    seen: set[tuple[str, str]] = set()
    for r in state.runs(kind="pipeline", limit=200):
        key = (r["repo"] or "", str(r["issue"]))
        if key in seen:
            continue
        seen.add(key)
        status = r["status"]
        target = cfg.jira.transitions.get(status, "") if status in CATCH_UP else ""
        if not target or r.get("moved") == status:
            continue
        try:
            repo = cfg.repo(r["repo"])
        except KeyError:
            continue
        tk = tracker.for_ticket(cfg, repo, str(r["issue"]))
        if not isinstance(tk, tracker.JiraTracker):
            continue
        try:
            m = tk.shift(str(r["issue"]), target, keep_done=True)
        except jira.JiraError as e:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: could not move it to {target}, trying next poll: "
                  f"{util.first_line(str(e), 300)}")
            continue
        record.note(r["id"], moved=status)
        if m.error or m.moved:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: "
                  + (f"not moved: {util.first_line(m.error, 300)}" if m.error else f"{status}, moved to {target}"))


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
        if repo.tracker != "jira" or tracker.is_task_id(r["issue"]):
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
                run = Run(r["id"], repo, paths.RUNS_DIR / r["id"], tracker=tk, tid=str(r["issue"]), writes_lines=False)
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
    saved = run.dir / f"uat-{tracker.safe_filename(branch)}.md"
    if saved.exists():
        return saved.read_text()
    run.dir.mkdir(parents=True, exist_ok=True)
    tries_file = run.dir / f"uat-{tracker.safe_filename(branch)}.tries"
    tries = int(tries_file.read_text() or 0) + 1 if tries_file.exists() else 1
    t = run.tracker.get(run.tid)
    try:
        run.cfg, run.layer, run.kind = cfg, config.load_layer(run.repo.layer_dir), t.kind
        most = run.settings("uat").tries
    except (ValueError, TypeError) as e:
        raise RetryNextPoll(f"{paths.short_path(run.repo.layer_dir / 'config.toml')}: {e}") from e
    urls = row["pr_url"].split()
    share = MAX_DIFF_IN_PROMPT // len(urls)
    prs = "\n\n".join(f"### {url}\n\n{gh.pr_body(url).strip()}\n\n```diff\n{cut(gh.pr_diff(url), share)}\n```"
                       for url in urls)
    res = run.agent("uat", prompt("uat", ref=t.ref, title=t.title, ticket="\n\n".join(filter(None, [t.body, t.comments])),
                                  prs=prs, branch=branch),
                    run.repo.path, role="uat", json_schema=UAT_SCHEMA, persist=False)
    found = res.structured if res.ok and isinstance(res.structured, dict) else {}
    listed = [re.sub(r"^\s*\d+[.)]\s*", "", s).strip() for s in found.get("steps", []) if s.strip()]
    failure = util.first_line(res.error or "none came back")
    if listed:
        before = found.get("before", "").strip()
        text = (f"**Before you start:** {before}\n\n" if before else "") + "\n".join(
            f"{i}. {s}" for i, s in enumerate(listed, 1))
    elif tries < most:
        tries_file.write_text(str(tries))
        raise RetryNextPoll(f"no test steps came back, try {tries} of {most}: {failure}")
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


def streak(repo_name: str, since: str = "") -> int:
    n = 0
    for r in state.runs(kind="pipeline", repo=repo_name, limit=100):
        if r["status"] == "merged" and (not since or (r["finished"] or "") > since):
            n += 1
        elif r["status"] in ("rejected", "failed"):
            break
    return n


def layer_is_committed(root: Path) -> bool:
    return bool(git(["ls-files", ".agents/config.toml"], root, check=False).strip())


def with_automerge_trust(text: str) -> str | None:
    data = tomllib.loads(text)
    if data.get("trust", {}).get("level") == "automerge":
        return text
    want = data | {"trust": data.get("trust", {}) | {"level": "automerge"}}
    for new in automerge_trust_edits(text if not text or text.endswith("\n") else text + "\n"):
        try:
            if tomllib.loads(new) == want:
                return new
        except tomllib.TOMLDecodeError:
            continue
    return None


def automerge_trust_edits(text: str) -> Iterator[str]:
    for m in re.finditer(r"""\blevel[ \t]*=[ \t]*("[^"\n]*"|'[^'\n]*')""", text):
        yield text[:m.start(1)] + '"automerge"' + text[m.end(1):]
    for m in re.finditer(r"(?m)^[ \t]*\[[ \t]*trust[ \t]*\][^\n]*\n", text):
        yield text[:m.end()] + 'level = "automerge"\n' + text[m.end():]
    yield text + '\n[trust]\nlevel = "automerge"\n'


def _maybe_propose_trust(repo: config.Repo) -> None:
    layer = config.load_layer(repo.layer_dir)
    if layer.trust_level != "pr" or streak(repo.name) < layer.automerge_after or not layer_is_committed(repo.path):
        return
    branch = "arete/trust-automerge"
    if gh.open_pr_url(repo.github, branch):
        return
    if gh.closed_pr_heads(repo.github, branch):
        closed_at = gh.pr_view(branch, repo.github).get("closedAt") or ""
        if streak(repo.name, since=closed_at) < layer.automerge_after:
            return
    base = layer.base_branch or gh.default_branch(repo.github)
    labels = ["arete-trust"] if repo.tracker == "github" else []
    wt = make_worktree(repo, base, branch, "trust")
    try:
        f = wt / ".agents" / "config.toml"
        if not f.exists():
            return
        text = f.read_text()
        new = with_automerge_trust(text)
        if new is None:
            raise RuntimeError(f".agents/config.toml on {base} sets trust in a form arete cannot edit, "
                               'so set trust.level = "automerge" there by hand')
        if new == text:
            return
        f.write_text(new)
        util.git_as_owner(["commit", "-q", "-am", "chore(agents): raise trust to automerge"], wt)
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
            if (d.is_dir() and not d.is_symlink() and d.stat().st_mtime < cutoff and not scratch_left(d, [])
                    and not unexamined(d, set())):
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


class SkipLog:
    def __init__(self, before: dict):
        self.before = before
        self.current: dict[str, dict] = {}

    def note(self, name: str, t: tracker.Ticket, blocker: str, repo: str = "") -> None:
        old = self.before.get(name) or {}
        same = old.get("why") == blocker
        self.current[name] = {"why": blocker, "title": t.title, "url": t.url, "repo": repo, "id": str(t.id),
                              "since": old.get("since") if same else util.iso()}
        if not same:
            print(f"{util.iso()} {name}: not run, {blocker}")

    def keep_repo(self, repo: str) -> None:
        self.keep_earlier(lambda name, v: v.get("repo") == repo)

    def keep_project(self, project: str) -> None:
        self.keep_earlier(lambda name, v: not v.get("repo") and name.startswith(f"{project}-"))

    def keep_earlier(self, belongs: Callable[[str, dict], bool]) -> None:
        for name, v in self.before.items():
            if belongs(name, v):
                self.current.setdefault(name, v)


def settle_abandoned_runs(cfg: config.Config) -> None:
    record.reap(tickets=True, then=lambda row: tell_ticket_run_died(cfg, row))


def retry_scratch() -> None:
    for root in scratch.recover_pending():
        print(f"preserved and finalized verification scratch {root}")
    for data in scratch.records():
        if data["state"] in ("pending", "retained", "preserved") and data["diagnostic"]:
            print("verification scratch retained: " + json.dumps({"run": data["attempt"]["run_id"],
                  "attempt": data["attempt"]["selector"], "diagnostic": data["diagnostic"]}, ensure_ascii=True))


def tell_ticket_run_died(cfg: config.Config, row: dict) -> None:
    repo = cfg.repo(row["repo"])
    run = Run(row["id"], repo, paths.RUNS_DIR / row["id"], tracker=tracker.for_ticket(cfg, repo, str(row["issue"])),
              tid=str(row["issue"]), feedback=pending_feedback(repo.name, str(row["issue"])),
              alone=bool(row.get("only_step")))
    run.dir.mkdir(parents=True, exist_ok=True)
    if row["status"] != "stopped":
        lines.write(row["id"], "error", step=lines.last_step(row["id"]), message=row.get("summary") or "")
    write_report(row)
    if run.alone:
        run.log(f"{record.STOPPED if row['status'] == 'stopped' else record.INTERRUPTED}, a run of "
                f"{row['only_step']} alone, so the ticket stays as it was")
    elif row["status"] == "stopped":
        run.log(record.STOPPED)
        run.give_up("was stopped by request")
    elif row["status"] == "failed":
        run.log(f"{record.INTERRUPTED}, the second interruption in a row")
        run.give_up(f"{record.INTERRUPTED}, the second interruption in a row")
    else:
        run.log(record.INTERRUPTED)
        run.leave("running")
        run.say(f"Run `{run.id}` {record.INTERRUPTED}; I will start it again.")


def holding(rows: list[dict], lock_kind: str, kinds: tuple[str, ...] = ("pipeline",)) -> list[dict]:
    return [r for r in rows
            if r["kind"] in kinds and util.lock_is_held(state.run_lock(lock_kind, r["repo"], r["issue"]))]


def live_pipeline_runs() -> list[dict]:
    return holding(state.runs_marked_running(), "pipeline")


def runs_in_slots() -> list[dict]:
    return holding(state.runs_marked_running(), "slot", SLOT_KINDS)


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

    def take(self, log: Callable[[str], None],
             waits: Callable[[str], AbstractContextManager] = lambda cause: nullcontext()) -> None:
        if self.held:
            return
        waiting = False
        with ExitStack() as stack:
            while True:
                with util.lock(SLOTS_LOCK, wait=True):
                    self.max_parallel = live_max_parallel(self.max_parallel)
                    if len(runs_in_slots()) < self.max_parallel:
                        self.hold()
                        log("ci: took a run slot back" + (" after waiting for one" if waiting else ""))
                        return
                    if not waiting:
                        waiting = True
                        stack.enter_context(util.hold_lock(state.run_lock("slot-wait", self.repo, self.tid)))
                        stack.enter_context(waits("slot"))
                        log(f"ci: waiting for a free run slot (max_parallel {self.max_parallel}), ahead of new runs")
                time.sleep(SLOT_POLL_SECONDS)

    def free(self) -> None:
        if self.held:
            self.held.close()
            self.held = None


def no_free_slot(max_parallel: int) -> str:
    if waiting := len(runs_waiting_for_a_slot()):
        return (f"{util.count(waiting, 'run')} {'waits' if waiting == 1 else 'wait'} to take a run slot back first "
                f"(max_parallel {max_parallel})")
    return f"no run slot is free (max_parallel {max_parallel})"


def held_back(cfg: config.Config, repo_name: str, going: Counter, slots: int) -> str:
    if going[repo_name] >= cfg.max_parallel_per_repo:
        return (f"{repo_name} already has {util.count(going[repo_name], 'run')} going "
                f"(max_parallel_per_repo {cfg.max_parallel_per_repo})")
    return no_free_slot(cfg.max_parallel) if slots <= 0 else ""


def spawn_run(repo_name: str, tid: str, force: bool = False) -> subprocess.Popen:
    return control.launch(run_command(repo_name, tid, force), RUNS_LOG)


class NoFreeSlot(RuntimeError):
    pass


def run_skipped(name: str) -> dict:
    entry = skipped_tickets().get(name)
    if not entry:
        raise control.NotFound(f"{name} is not on the skipped list; the last poll either ran it or no longer skips it")
    if not entry.get("repo"):
        raise control.Refused(f"{name} names no repo to run in; a ticket the pipeline routes to a repo gets one at "
                              "the next poll")
    cfg = config.load()
    if state.issue_running(entry["repo"], entry["id"]):
        raise control.Busy(name)
    if slots_free_to_start(cfg.max_parallel) <= 0:
        raise NoFreeSlot(f"{name} was not started: {no_free_slot(cfg.max_parallel)}; wait for a run to finish or "
                         "raise max_parallel in Settings")
    run = spawn_run(entry["repo"], entry["id"], force=True)
    give_up = time.monotonic() + RUN_START_SECONDS
    while not state.issue_running(entry["repo"], entry["id"]) and time.monotonic() < give_up:
        if run.poll() is not None:
            raise control.Refused(f"{name} ended before it showed as running; the end of "
                                  f"{paths.short_path(paths.LOG_DIR / RUNS_LOG)} says why")
        time.sleep(0.1)
    return {"ticket": name, "repo": entry["repo"], "id": entry["id"]}


def sources(repo: config.Repo, tk: tracker.Tracker) -> list[tracker.Tracker]:
    if isinstance(tk, tracker.LocalTracker):
        return [tk]
    return [*([tk] if repo.parts or isinstance(tk, tracker.JiraTracker) else []), tracker.LocalTracker(repo)]


def next_ticket(repo: config.Repo, tk: tracker.Tracker, skips: SkipLog,
                other_routes: set[str] = frozenset(), excluded: set[str] = frozenset()) -> tracker.Ticket | None:
    for source in sources(repo, tk):
        for t in source.todo():
            if str(t.id) in excluded or other_routes & set(t.labels) or state.issue_running(repo.name, t.id):
                continue
            if blocker := source.blocker(t.id):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker, repo.name)
                continue
            return t
    return None


def jira_routes_by_project(cfg: config.Config) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in cfg.repos:
        if r.pipeline and runnable(r) and r.tracker == "jira":
            out.setdefault(r.jira_project or cfg.jira.project, set()).add(tracker.route_label(r))
    return out


def next_pick(repo: config.Repo, tk: tracker.Tracker, skips: SkipLog,
              other_routes: set[str], excluded: set[str] = frozenset()) -> tuple[str, str] | None:
    fresh = next_ticket(repo, tk, skips, other_routes, excluded)
    for tid in state.interrupted_tickets(repo.name):
        if str(tid) not in excluded and not state.issue_running(repo.name, tid):
            return tid, "restarting after an interruption"
    for tid in state.feedback_due(repo.name, util.iso(util.now() - timedelta(minutes=FEEDBACK_QUIET_MINUTES))):
        if str(tid) not in excluded and not state.issue_running(repo.name, tid):
            return tid, "revising the PR for your review comments"
    for source in sources(repo, tk):
        for t in source.replied():
            if str(t.id) in excluded or state.issue_running(repo.name, t.id):
                continue
            if other_routes & set(t.labels):
                skips.note(t.ref, t, tracker.route_blocker(other_routes | {tracker.route_label(repo)}, t.labels))
                continue
            if blocker := source.blocker(t.id):
                skips.note(tracker.ticket_name(repo.name, t.ref), t, blocker, repo.name)
                continue
            return t.id, f"{t.title} (you replied to the question)"
    return (fresh.id, fresh.title) if fresh else None


def route_blocker(cfg: config.Config, repo: config.Repo, ticket: tracker.Ticket,
                  discovery: routing.Discovery, layer: config.RepoLayer) -> str:
    if cfg.replay.all:
        return ""
    first = ("plan", "build")
    newest = state.ticket_runs(repo.name, ticket.id, limit=1, whole=True)
    if (layer.factory_path == "adw" and newest and newest[0]["status"] == "interrupted"
            and repaired_by(newest[0]["id"])):
        first = ("review_repair",)
    elif pending_feedback(repo.name, ticket.id):
        first = ("build",)
    for role in (*first, *(("verify",) if layer.verify else ()), "review"):
        setting = steps.resolve(cfg, layer, role, ticket.kind)
        if setting.runner == runner.REPLAY:
            continue
        routes = discovery.routes(setting)
        if not any(route.state == "ready" for route in routes):
            reason = routing.held_words(routes) or "; ".join(dict.fromkeys(route.detail for route in routes))
            return f"{role} has no eligible route" + (f": {reason}" if reason else "")
    return ""


def poll(dry_run: bool = False) -> int:
    paths.ensure_dirs()
    cfg = config.load()
    with util.lock("poll") as got:
        if not got:
            print(f"{util.iso()} poll already running")
            return 0
        if not dry_run:
            retry_scratch()
        if any(r.parts or r.tracker != "local" for r in cfg.repos) and not network_up_or_skip("poll", dry_run):
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
        pipeline_repos = [r for r in cfg.repos if r.pipeline and runnable(r)]
        restarting = {r.name for r in pipeline_repos if state.interrupted_tickets(r.name)}
        skips = SkipLog({} if dry_run else util.read_json(SKIPPED_FILE, {}))
        routes = jira_routes_by_project(cfg)
        reached_clients: dict[str, jira.Client] = {}
        contexts = []
        for repo in sorted(pipeline_repos, key=lambda r: (r.name not in restarting, starts.get(r.name, ""))):
            try:
                tk = tracker.for_repo(cfg, repo)
                other_routes: set[str] = set()
                if isinstance(tk, tracker.JiraTracker):
                    other_routes = routes.get(tk.project, set()) - {tk.route}
                    reached_clients.setdefault(tk.project, tk.client)
                layer = config.load_layer(repo.layer_dir)
                contexts.append((repo, tk, other_routes, layer,
                                 routing.Discovery(cfg, repo, priced=False, layer=layer), set()))
            except Exception as e:
                failed(repo.name, e)
                skips.keep_repo(repo.name)
        examined, exhausted = 0, set()
        while examined < MAX_STARTS_PER_POLL:
            progressed = False
            for repo, tk, other_routes, layer, discovery, selected in contexts:
                if repo.name in exhausted or examined >= MAX_STARTS_PER_POLL:
                    continue
                try:
                    pick = next_pick(repo, tk, skips, other_routes, selected)
                    if not pick:
                        exhausted.add(repo.name)
                        if not dry_run:
                            state.drop_passed_over(repo.name)
                        continue
                    tid, why = pick
                    selected.add(str(tid))
                    examined += 1
                    progressed = True
                    source = tracker.for_ticket(cfg, repo, tid)
                    name = tracker.ticket_name(repo.name, source.ref(tid))
                    if waiting := held_back(cfg, repo.name, going, slots):
                        exhausted.add(repo.name)
                        if not dry_run:
                            state.pass_over(repo.name, str(tid))
                        print(f"{util.iso()} {name}: waiting, {waiting}")
                        continue
                    blocked, _ = runlimits.admission(repo, str(tid), source, runlimits.Policy.of(layer), False)
                    ticket = source.get(tid)
                    blocked = blocked or source.blocker(tid) or route_blocker(cfg, repo, ticket, discovery, layer)
                    if blocked:
                        skips.note(name, ticket, blocked, repo.name)
                        print(f"{util.iso()} {name}: {blocked}")
                        continue
                    print(f"{util.iso()} {name}: {why}" + (" (dry run, not started)" if dry_run else ""))
                    if not dry_run:
                        state.drop_passed_over(repo.name, keep=str(tid))
                        spawn_run(repo.name, tid)
                    slots -= 1
                    going[repo.name] += 1
                except Exception as e:
                    failed(repo.name, e)
                    skips.keep_repo(repo.name)
                    exhausted.add(repo.name)
            if not progressed:
                break
        for project, client in reached_clients.items():
            try:
                for t in tracker.jira_unrouted(project, routes.get(project, set()), client):
                    skips.note(t.ref, t, t.blocker)
            except Exception as e:
                failed(f"jira {project}", e)
                skips.keep_project(project)
        for project in routes.keys() - reached_clients.keys():
            skips.keep_project(project)
        if not dry_run:
            try:
                sync_moves(cfg)
            except Exception as e:
                failed("moving tickets", e)
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
