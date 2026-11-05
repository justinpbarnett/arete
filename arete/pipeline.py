from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from string import Template

from . import claude, config, gh, jira, paths, state, tracker, util
from .heartbeat import mark_loop

ALWAYS_DENY = [".agents/config.toml", ".github/workflows/**", ".env", ".env.*", "**/.env", "**/.env.*"]
MAX_DIFF_IN_PROMPT = 60_000
LOCKFILES = {"pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb", "uv.lock", "poetry.lock",
             "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock"}
NEVER_STAGED = [":(exclude,glob)**/__pycache__/**"]
SIBLINGS = re.compile(r"\n*## Other PRs for [^\n]*\n(?:\n- \[[^\n]*)+")
SKIPPED_FILE = paths.STATE_DIR / "skipped.json"
CONTINUE_AFTER_DROP = "The connection dropped mid step. Carry on from where you stopped and finish the step."

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "ambiguous": {"type": "boolean"},
        "question": {"type": "string"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "estimated_lines": {"type": "integer", "minimum": 0},
        "split": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                "required": ["title", "body"],
            },
        },
    },
    "required": ["plan", "ambiguous", "risk", "estimated_lines"],
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
FEEDBACK_QUIET_MINUTES = 10
HUNK_LINES = 6
PR_SEEN_FILE = paths.STATE_DIR / "pr_seen.json"
PR_SEEN_SETTLE = timedelta(minutes=1)
READ_TOOLS = ["Read", "Glob", "Grep"]
EDIT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "TodoWrite"]
NO_GIT_WRITES = [
    "Bash(git commit*)", "Bash(git push*)", "Bash(git reset*)", "Bash(git checkout*)",
    "Bash(git switch*)", "Bash(git rebase*)", "Bash(git merge*)", "Bash(gh *)",
]


def prompt(name: str, **values) -> str:
    text = (paths.PROMPTS_DIR / f"{name}.md").read_text()
    return Template(text).safe_substitute({k: ("" if v is None else str(v)) for k, v in values.items()})


def bash_rules(patterns: list[str]) -> list[str]:
    return [f"Bash({p})" for p in patterns]


def command_list(patterns: list[str]) -> str:
    return ", ".join(f"`{p}`" for p in patterns)


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "change"


def fill_template(template: str, default: str, **values) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return default.format(**values)


@dataclass
class AgentRun:
    id: str
    repo: config.Repo
    dir: Path
    dry_run: bool = False
    cost: float = 0.0
    add_dirs: list[Path] = field(default_factory=list)

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
        for _ in range(util.NETWORK_RETRIES - 1):
            if not res.connection_failed:
                break
            self.cost += res.cost_usd
            self.log(f"{step}: lost the connection after {res.turns} turns, waiting for the network")
            if not util.wait_for_network():
                break
            if res.session_id and res.turns > 1 and kw.get("persist", True):
                res = claude.run(CONTINUE_AFTER_DROP, cwd, add_dirs=self.add_dirs, **{**kw, "resume": res.session_id})
            else:
                res = claude.run(text, cwd, add_dirs=self.add_dirs, **kw)
        self.cost += res.cost_usd
        self.save(f"{step}.json", {"ok": res.ok, "session": res.session_id, "cost": res.cost_usd,
                                   "turns": res.turns, "text": res.text, "structured": res.structured,
                                   "error": res.error})
        self.log(f"{step}: {'ok' if res.ok else 'FAILED ' + util.first_line(res.error)} "
                 f"({res.turns} turns, ${res.cost_usd:.2f})")
        return res


@dataclass(kw_only=True)
class Run(AgentRun):
    tracker: tracker.Tracker
    tid: str
    force: bool = False
    story: list[str] = field(default_factory=list)
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


def git(args: list[str], cwd: Path, timeout: float = 300, check: bool = True) -> str:
    res = util.sh_with_network(["git", *args], cwd=cwd, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {util.first_line(res.stderr or res.stdout, 300)}")
    return res.stdout


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


def add_worktree(c: Checkout, branch: str, start: str) -> None:
    c.wt.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-B", branch, str(c.wt), start], c.src)


def remove_workspace(ws: Path, checkouts: list[Checkout]) -> None:
    for c in checkouts:
        git(["worktree", "remove", "--force", str(c.wt)], c.src, check=False)
    shutil.rmtree(ws, ignore_errors=True)
    for c in checkouts:
        git(["worktree", "prune"], c.src, check=False)


def make_workspace(repo: config.Repo, base: str, branch: str, key: str) -> tuple[Path, list[Checkout]]:
    ws = paths.WORKTREES_DIR / f"{repo.name}-{key}"
    checkouts = [Checkout(p.dir, p.github, repo.path / p.dir, ws / p.dir, base or gh.default_branch(p.github))
                 for p in repo.parts]
    remove_workspace(ws, checkouts)
    for c in checkouts:
        git(["fetch", "origin", c.base], c.src)
        c.pr = gh.open_pr_url(c.github, branch)
        c.lease = remote_head(c.src, branch)
        c.resumes = bool(c.pr) or (bool(c.lease) and c.lease not in gh.closed_pr_heads(c.github, branch))
        if c.resumes:
            git(["fetch", "origin", branch], c.src)
        add_worktree(c, branch, f"origin/{branch}" if c.resumes else f"origin/{c.base}")
    return ws, checkouts


def make_worktree(repo: config.Repo, base: str, branch: str, key: str) -> Path:
    c = Checkout("", repo.github, repo.path, paths.WORKTREES_DIR / f"{repo.name}-{key}", base)
    remove_workspace(c.wt, [c])
    git(["fetch", "origin", base], c.src)
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


def run_checks(layer: config.RepoLayer, cwd: Path, git_dirs: list[Path], run: AgentRun,
               timeout: float) -> tuple[bool, str]:
    if not layer.checks:
        return True, "no checks configured"
    untracked_before = {d: untracked_files(d) for d in git_dirs}
    report, ok = [], True
    for name, cmd in layer.checks.items():
        res = util.sh(cmd, cwd=cwd, timeout=timeout)
        passed = res.returncode == 0
        ok = ok and passed
        run.log(f"check {name}: {'pass' if passed else f'FAIL (exit {res.returncode})'}")
        if not passed:
            report.append(f"### {name} failed: `{cmd}` exit {res.returncode}\n```\n{util.tail(res.stdout + res.stderr, 6000)}\n```")
        else:
            report.append(f"### {name} passed: `{cmd}`")
    for d, before in untracked_before.items():
        for name in untracked_files(d) - before:
            (d / name).unlink(missing_ok=True)
    return ok, "\n\n".join(report)


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
    check_list = ", ".join(f"`{c}`" for c in layer.checks.values()) or "(none configured)"
    reads = git_reads(checkouts)
    builder_bash = layer.builder_bash + reads
    commands = command_list(builder_bash)
    workspace = workspace_note(branch, checkouts)
    step_timeout = layer.timeout_minutes * 60
    read_tools = READ_TOOLS + bash_rules(config.DEFAULT_ALLOW_BASH + reads)
    build_tools = EDIT_TOOLS + bash_rules(builder_bash)
    git_dirs = [c.wt for c in checkouts]
    deny = ALWAYS_DENY + layer.deny
    limit = layer.max_diff_lines
    info = tk.label_name("needs-info")
    replies: list[dict] = []

    if run.feedback:
        feedback = feedback_markdown(run.feedback)
        plan_text = f"Revise the open PR for the owner's feedback:\n\n{feedback}"
        estimate = 0
        run.story.append(f"The owner reviewed the PR and left this feedback:\n\n{feedback}")
        res = run.agent("revise", prompt("revise", repo=repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                         body=t.body, comments=t.comments, attachments=attachments,
                                         feedback=feedback, expertise=expertise, checks=check_list,
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
                                        expertise=expertise, commands=commands, max_lines=limit),
                        ws, model=cfg.model("plan"), allowed_tools=read_tools, json_schema=PLAN_SCHEMA,
                        timeout=step_timeout)
        if not res.ok or not isinstance(res.structured, dict):
            return _fail(run, "planning step failed")
        plan = res.structured
        plan_text = plan.get("plan", "")
        run.save("plan.md", plan_text)
        estimate = int(plan.get("estimated_lines") or 0)
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
                                         expertise=expertise, checks=check_list, commands=commands, max_lines=limit,
                                         estimate=estimate),
                        ws, model=cfg.model("build"), allowed_tools=build_tools, disallowed_tools=NO_GIT_WRITES,
                        timeout=step_timeout)
        if not res.ok:
            return _fail(run, "build step failed", res.text)
        build_summary = res.text
        if not stage(checkouts, deny).files:
            return _fail(run, "the builder stopped without changing anything", build_summary)
    session = res.session_id

    attempts = 1
    ok, report = run_checks(layer, ws, git_dirs, run, step_timeout)
    while not ok and attempts < layer.max_attempts:
        run.story.append(f"Checks failed on attempt {attempts}:\n{util.tail(report, 2500)}")
        attempts += 1
        res = run.agent(f"fix{attempts}", prompt("fix", attempt=attempts, max_attempts=layer.max_attempts,
                                                 failures=report),
                        ws, model=cfg.model("build"), allowed_tools=build_tools, disallowed_tools=NO_GIT_WRITES,
                        resume=session, timeout=step_timeout)
        session = res.session_id or session
        ok, report = run_checks(layer, ws, git_dirs, run, step_timeout)
    run.save("checks.md", report)
    state.update_run(run.id, attempts=attempts)
    if not ok:
        run.story.append("Checks still failing after the last attempt.")
        _learn(cfg, run, ws, layer.expertise_file, t.title, expertise, apply=False)
        return _fail(run, f"checks still failing after {attempts} attempts")

    staged = stage(checkouts, deny)
    if not staged.files:
        return _fail(run, "the fix attempts undid every change", build_summary)
    if staged.denied:
        return _fail(run, f"the change touched denied paths: {', '.join(staged.denied[:5])}")
    if staged.lines > limit:
        planned = f" (the plan estimated {estimate})" if estimate else ""
        return _fail(run, f"the diff is {staged.lines} lines, over the {limit} line limit{planned}; split the work",
                     detail=f"### Lines changed by file\n\n{lines_by_file(checkouts)}")

    review = _review(cfg, run, ws, workspace, read_tools, t, attachments, plan_text, report, staged.diff,
                     step_timeout)
    if review.get("verdict") == "request_changes":
        problems = "\n".join(f"- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in review.get("problems", []))
        run.story.append(f"Review requested changes:\n{problems}")
        res = run.agent("review_fix", prompt("review_fix", problems=problems), ws, model=cfg.model("build"),
                        allowed_tools=build_tools, disallowed_tools=NO_GIT_WRITES, resume=session,
                        timeout=step_timeout)
        ok, report = run_checks(layer, ws, git_dirs, run, step_timeout)
        staged = stage(checkouts, deny)
        if not ok or not staged.files or staged.denied or staged.lines > limit:
            return _fail(run, "changes after review broke the checks or the limits")
        review = _review(cfg, run, ws, workspace, read_tools, t, attachments, plan_text, report, staged.diff,
                         step_timeout, "review2")

    approved = review.get("verdict") == "approve"
    _learn(cfg, run, ws, layer.expertise_file, t.title, expertise, apply=True)
    staged = stage(checkouts, deny)
    size = f"{len(staged.files)} files, {staged.lines} lines"
    outcome = f"{size}, review {review.get('verdict')}"

    if run.dry_run:
        run.save("diff.patch", staged.diff)
        run.log(f"dry run: {outcome}; worktree kept at {ws}")
        return "dry-run", outcome, ""

    subject = fill_template(layer.subject, config.RepoLayer.subject, kind=t.kind, id=t.id, title=t.title)
    problems = review.get("problems", [])
    report_body = (
        f"## Summary\n\n{build_summary.strip()[:3000]}\n\n"
        f"## Review\n\n**{review.get('verdict', 'none')}**: {review.get('summary', '')}\n"
        + ("".join(f"\n- [{p['severity']}] {p.get('file', '')}: {p['problem']}" for p in problems) if problems else "")
        + f"\n\n## Checks\n\nAll passed after {attempts} attempt{'s' if attempts > 1 else ''}: {check_list}\n\n"
        + ("" if run.feedback else f"<details><summary>Plan</summary>\n\n{plan_text[:6000]}\n\n</details>\n\n")
        + f"Run `{run.id}`, {size}, ${run.cost:.2f}."
    )
    revised = "Revised for the review feedback on the PR.\n\n" if run.feedback else ""
    message = f"{tracker.with_ref(subject, t.ref)}\n\n{revised}Arete-Run: {run.id}"
    for c in staged.changed:
        try:
            git(["commit", "-q", "-m", message], c.wt)
        except RuntimeError as e:
            return _fail(run, f"the commit in {c.github} failed, so arete pushed nothing", detail=str(e))
    moved = [c.github for c in checkouts if remote_head(c.src, branch) != c.lease]
    if moved:
        return _fail(run, f"`{branch}` moved on {', '.join(moved)} while this run worked, so arete pushed nothing",
                     detail="Someone pushed to that branch after this run started. A retry builds on top of their work.")
    pushed_now: list[Checkout] = []
    for c in staged.changed:
        try:
            git(["push", "-q", f"--force-with-lease={branch}:{c.lease}", "-u", "origin", branch], c.wt, timeout=600)
        except RuntimeError as e:
            done = ", ".join(p.github for p in pushed_now) or "nothing"
            return _fail(run, f"the push to {c.github} failed after arete pushed {done}",
                         detail=f"{e}\n\nA retry builds on whatever reached the branch.")
        pushed_now.append(c)
    opened: list[Checkout] = []
    for c in staged.changed:
        if not c.pr:
            c.pr = gh.pr_create(c.github, branch, c.base, tk.pr_title(subject, t), f"{tk.pr_link(t)}\n\n{report_body}",
                                labels=tk.pr_labels, draft=not approved, cwd=c.wt)
            opened.append(c)
            run.log(f"opened {c.pr}{'' if approved else ' as draft'}")
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
    run.enter("pr")
    pr_lines = "\n".join(f"- {'Opened' if c in opened else 'Pushed to' if c in pushed else 'Unchanged'} [{c.pr_name}]({c.pr})"
                         for c in with_prs)
    if run.feedback:
        answer_feedback(run, replies, "Done in the latest push.")
        run.say(f"Revised for your review.\n\n{report_body}")
    else:
        run.say(f"PRs for this ticket:\n\n{pr_lines}"
                + ("" if approved or not opened else "\n\nThe new PRs are drafts: the reviewer still has concerns."))
    if approved and layer.trust_level == "automerge":
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


def _review(cfg, run: Run, ws: Path, workspace: str, read_tools: list[str], t: tracker.Ticket, attachments: str,
            plan: str, report: str, diff: str, timeout: float, step: str = "review") -> dict:
    res = run.agent(step, prompt("review", repo=run.repo.name, workspace=workspace, ref=t.ref, title=t.title,
                                 body=t.body, comments=t.comments, attachments=attachments, plan=plan, checks=report,
                                 diff=util.tail(diff, MAX_DIFF_IN_PROMPT)),
                    ws, model=cfg.model("review"), allowed_tools=read_tools, json_schema=REVIEW_SCHEMA,
                    timeout=timeout)
    if not res.ok or not isinstance(res.structured, dict):
        return {"verdict": "request_changes", "summary": "review step failed", "problems": []}
    return res.structured


def _learn(cfg, run: Run, wt: Path, expertise_file: Path, title: str, expertise: str, apply: bool) -> None:
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
        return (item.get("user") or {}).get("login") == owner and not gh.from_arete(item.get("body"))

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


def sync_outcomes(cfg: config.Config) -> None:
    seen, fresh = util.read_json(PR_SEEN_FILE, {}), {}
    for r in state.runs(kind="pipeline", limit=200):
        if r["status"] != "pr" or not r.get("pr_url"):
            continue
        urls = r["pr_url"].split()
        try:
            prs = [gh.pr_view(url) for url in urls]
        except gh.GhError:
            continue
        record_feedback(r, urls, prs, seen, fresh)
        if not all(pr.get("state") in ("MERGED", "CLOSED") for pr in prs):
            continue
        for url, pr in zip(urls, prs):
            state.close_feedback(url, pr["state"])
        if all(pr.get("mergedAt") for pr in prs):
            state.update_run(r["id"], status="merged")
        else:
            state.update_run(r["id"], status="rejected")
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
                    raise jira.JiraError(say_err)
        except (gh.GhError, jira.JiraError) as e:
            print(f"{util.iso()} {tracker.run_ticket_name(r)}: landing check failed, trying next poll: {util.first_line(str(e), 300)}")
            continue
        state.update_run(r["id"], landed=reached)
        print(f"{util.iso()} {tracker.run_ticket_name(r)}: landed on {reached}, " + (err or f"moved to {target}"))


def uat_steps(cfg: config.Config, run: Run, row: dict, branch: str) -> str:
    saved = run.dir / f"uat-{branch}.md"
    if saved.exists():
        return saved.read_text()
    run.dir.mkdir(parents=True, exist_ok=True)
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
    if steps:
        before = found.get("before", "").strip()
        text = (f"**Before you start:** {before}\n\n" if before else "") + "\n".join(
            f"{i}. {s}" for i, s in enumerate(steps, 1))
    else:
        text = (f"I could not write the steps ({util.first_line(res.error or 'none came back')}); "
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
        if not util.online():
            print(f"{util.iso()} offline, waiting up to {util.WAKE_NETWORK_WAIT_MINUTES} minutes for the network", flush=True)
            if not util.wait_for_network(util.WAKE_NETWORK_WAIT_MINUTES):
                print(f"{util.iso()} still offline, skipped this poll")
                if not dry_run:
                    mark_loop("poll", True, "offline")
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
