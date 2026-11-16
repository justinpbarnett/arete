from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, environment, factory, loops, paths, record, state, tracker, util, workspace
from .lab import verifier_suites
from .pipeline import AgentRun, TicketWork, use_product

PR = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", re.IGNORECASE)
CHECK = re.compile(r"^- \*\*(?:Passed|Failed(?: \(environment\))?)\*\*:\s*(.+)$")
NUMBER = re.compile(r"^(?:step\s+)?#?(\d+)[.):\s]+(.+)$", re.IGNORECASE)
HOLD_EXIT = 75


class Blocked(ValueError):
    pass


def loop_dir() -> Path:
    return Path(os.environ.get("ARETE_LOOP_DIR") or loops.BUNDLED_LOOPS_DIR / "merge-check")


def builtin(loop: loops.Loop) -> bool:
    return loop.name == "merge-check" and loop.command == ["arete", "merge-check"]


def merged(cfg: config.Config) -> list[dict]:
    repos = {r.name: r for r in cfg.repos if r.pipeline}
    found: dict[str, dict] = {}
    for row in state.merged_runs():
        if row["repo"] not in repos or not row.get("finished"):
            continue
        for match in PR.finditer(row["pr_url"]):
            url = f"https://github.com/{match[1].lower()}/pull/{int(match[2])}"
            item = {"key": url, "repo": row["repo"], "pr": int(match[2]), "url": url,
                    "run_id": row["id"], "merged_at": row["finished"], "issue": row.get("issue")}
            if url not in found:
                found[url] = item
    return sorted(found.values(), key=lambda r: (r["merged_at"], r["run_id"], r["key"]))


def events(cfg: config.Config, since_cursor: bool = True) -> list[dict]:
    rows = merged(cfg)
    if os.environ.get("ARETE_SOURCE_SAMPLE") == "1":
        rows = [r for repo in cfg.repos for r in [i for i in rows if i["repo"] == repo.name][-2:]]
    elif since_cursor:
        seen = state.source_seen(os.environ.get("ARETE_SOURCE") or "merge-check#merged")
        rows = [r for r in rows if r["key"] not in seen]
    return [{**r, "type": "command", "title": f"Merged {r['repo']} PR #{r['pr']}",
             "data": {**r, "at": r["merged_at"]}} for r in rows]


def read_events(text: str) -> list[dict]:
    if not text.strip():
        return []
    try:
        rows = json.loads(text) if text.lstrip().startswith("[") else [json.loads(s) for s in text.splitlines() if s.strip()]
    except ValueError:
        raise Blocked("stdin must contain JSON events, one per line or a JSON list") from None
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("repo"), str) for r in rows):
        raise Blocked("stdin events must be objects with a repo")
    return rows


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save_json(path: Path, data) -> None:
    util.write_durably(path, json.dumps(data, sort_keys=True, indent=2).encode(), 0o600)


@contextmanager
def locked(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "consumer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def quota(folder: Path, repo: str) -> tuple[dict, int, str]:
    data = read_json(folder / "quota.json", {})
    today = datetime.now(UTC).date()
    used = data.get(repo, {}).get("attempts", 0) if data.get(repo, {}).get("day") == str(today) else 0
    reset = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC).isoformat()
    return data, used, reset


def held_until(folder: Path, repo: str) -> str:
    _, used, reset = quota(folder / ".state", repo)
    return reset if used >= 6 else ""


@dataclass
class Step:
    run: str
    number: int
    name: str
    how: str
    pr: int
    neighbour: bool = False

    def goal(self, n: int) -> str:
        return f"{n}. [{self.run} step {self.number}] {self.name}: {self.how}"


def reports(item: dict) -> tuple[str, Path]:
    run_id = item["run_id"]
    history = factory.history(factory.chain(run_id))
    applicable = [line for line in history if line.get("step") == "verify"]
    if applicable:
        last = applicable[-1]
        owner = str(last.get("run") or run_id)
        data = last.get("data") or {}
        verified = data.get("verified") or {}
        saved = verified.get("saved")
        if not isinstance(saved, str) or not saved.strip():
            raise Blocked(f"{run_id}: latest verification has no saved report")
        name = Path(saved).name
        if not re.fullmatch(r"verify\d*\.md", name):
            raise Blocked(f"{run_id}: latest verification has an invalid saved report name")
        saved_owner = Path(saved).parent.name
        owners = {run_id, *(str(line.get("run") or "") for read in factory.chain(run_id) for line in read)}
        if saved_owner in owners:
            owner = saved_owner
        return owner, paths.RUNS_DIR / owner / name
    for read in factory.chain(run_id):
        owner = str(next((line.get("run") for line in read if line.get("run")), run_id))
        folder = paths.RUNS_DIR / owner
        names = [p for p in folder.glob("verify*.md") if re.fullmatch(r"verify\d*\.md", p.name)]
        if names:
            return owner, max(names, key=lambda p: int(p.stem[6:] or 1))
    raise Blocked(f"{run_id}: missing saved verification report")


def report_steps(item: dict, neighbour: bool = False) -> list[Step]:
    owner, path = reports(item)
    try:
        text = path.read_text()
    except OSError:
        raise Blocked(f"{item['run_id']}: unreadable verification report {path}") from None
    found, name, how = [], "", ""
    for line in [*text.splitlines(), "- **Passed**: __end__"]:
        if match := CHECK.match(line):
            if name:
                if not how:
                    raise Blocked(f"{item['run_id']}: check {name!r} has no How instructions")
                numbered = NUMBER.match(name)
                number = int(numbered[1]) if numbered else len(found) + 1
                short = numbered[2] if numbered else name
                found.append(Step(owner, number, util.one_line(short, 120), how, item["pr"], neighbour))
            name, how = match[1].strip(), ""
        elif name and line.startswith("  - How: "):
            how = line.removeprefix("  - How: ").strip()
    if not found or len({s.number for s in found}) != len(found):
        raise Blocked(f"{item['run_id']}: verification report has no usable, uniquely numbered checks")
    return found


def changed(item: dict) -> set[str]:
    run_id = item["run_id"]
    owners = [run_id]
    for read in factory.chain(run_id):
        owners += [str(line["run"]) for line in read if line.get("run")]
    for owner in dict.fromkeys(owners):
        marks = list(record.lines(paths.record_dir(owner) / record.TIMELINE))
        diffs = [r for r in marks if r.get("kind") == "diff" and isinstance(r.get("files"), list)]
        if diffs:
            return set(diffs[-1]["files"])
        patch = paths.RUNS_DIR / owner / "diff.patch"
        if patch.is_file():
            files = set()
            for line in patch.read_text().splitlines():
                if line.startswith(("+++ b/", "--- a/")):
                    files.add(line[6:].split("\t")[0])
            if files:
                return files
    raise Blocked(f"{run_id}: changed-file history is unavailable; cannot select a neighbour")


@dataclass
class Plan:
    repo: config.Repo
    layer: config.RepoLayer
    batch: list[dict]
    steps: list[Step]
    base: str
    worktree: Path
    neighbour: dict | None = None
    matches: list[str] = field(default_factory=list)
    startup_failed: bool = False


def plan(cfg: config.Config, repo: config.Repo, rows: list[dict], batch: list[dict], run_id: str,
         preview_only: bool = False) -> Plan:
    layer = config.load_layer(repo.layer_dir)
    if not layer.verify:
        raise Blocked(f"{repo.name}: no [verify] configuration")
    base = layer.base_branch
    if not base:
        ref = util.git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repo.path, check=False).strip()
        base = ref.removeprefix("refs/remotes/origin/") if ref else ""
    if not base and not preview_only:
        base = util.origin_default_branch(repo.path)
    if not base:
        raise Blocked(f"{repo.name}: set [git] base or origin/HEAD before checking")
    unique = {item["run_id"]: item for item in batch}
    selected = [s for item in unique.values() for s in report_steps(item)]
    files = set().union(*(changed(item) for item in batch))
    earliest = min((r["merged_at"], r["run_id"], r["key"]) for r in batch)
    earlier = [r for r in rows if r["repo"] == repo.name and
               (r["merged_at"], r["run_id"], r["key"]) < earliest and
               r["run_id"] not in unique and
               not (r.get("issue") and r["issue"] in {item.get("issue") for item in batch})]
    neighbour, matches = None, []
    for item in reversed(earlier):
        if overlap := files & changed(item):
            neighbour, matches = item, sorted(overlap)
            selected.append(report_steps(item, neighbour=True)[0])
            break
    return Plan(repo, layer, batch, selected, base, paths.WORKTREES_DIR / f"{run_id}-{repo.name}", neighbour, matches)


def finding(p: Plan, failed: list[int]) -> dict:
    prs = ", ".join(f"#{n}" for n in sorted({r["pr"] for r in p.batch}))
    runs = ", ".join(dict.fromkeys(r["run_id"] for r in p.batch))
    names = "; ".join(f"{p.steps[n - 1].run} step {p.steps[n - 1].number}: {p.steps[n - 1].name}"
                      + (f" (neighbour PR #{p.steps[n - 1].pr})" if p.steps[n - 1].neighbour else "") for n in failed)
    key = hashlib.sha256(json.dumps([sorted(r["key"] for r in p.batch), failed]).encode()).hexdigest()[:20]
    return {"title": f"Merged base fails use check: {p.repo.name} after PRs {prs}",
            "fingerprint": f"merge-check/{p.repo.name}/{key}", "repo": p.repo.name,
            "summary": f"Merged PRs {prs}. Runs: {runs}. "
                       + ("Product startup failed before the required checks could run. " if p.startup_failed else "")
                       + f"Failed checks: {names}."}


def preview(p: Plan, folder: Path) -> None:
    sha = util.git(["rev-parse", "--verify", f"origin/{p.base}^{{commit}}"], p.repo.path, check=False).strip()
    _, used, reset = quota(folder, p.repo.name)
    v = p.layer.verify
    print(json.dumps({"preview": True, "repo": p.repo.name, "base": f"origin/{p.base}", "sha": sha,
                      "worktree": str(p.worktree), "start": v.start, "url": v.url,
                      "runs": p.batch, "steps": [asdict(s) for s in p.steps],
                      "neighbour": p.neighbour or "no applicable neighbour", "matching_files": p.matches,
                      "attempts_today": used, "cap": 6, "held_until": reset if used >= 6 else "",
                      "model_calls": 0, "sample_failure": finding(p, list(range(1, len(p.steps) + 1))),
                      "sample_metric": {"value": 0}}), file=sys.stderr)


@dataclass
class CheckRun(AgentRun):
    deadline: float = 0
    invoked: bool = False

    def log(self, msg: str) -> None:
        print(f"{self.id}: {msg}", file=sys.stderr, flush=True)
        with (self.dir / "run.log").open("a") as f:
            f.write(msg + "\n")

    def wait_for_network(self) -> bool:
        return False

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw):
        if self.invoked:
            raise Blocked("merge-check permits one verifier invocation")
        self.invoked = True
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Blocked("merge-check exceeded its 30-minute deadline")
        return super().agent(step, text, cwd, role, **{**kw, "max_steps": 40, "timeout": remaining,
                             "fallback": False, "wait_out_limits": False})


@contextmanager
def deadline():
    def expired(*_):
        raise Blocked("merge-check exceeded its 30-minute deadline")

    def stopped(*_):
        raise Blocked("merge-check interrupted")

    old_alarm = signal.signal(signal.SIGALRM, expired)
    old_term = signal.signal(signal.SIGTERM, stopped)
    previous, interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, min(1800, previous) if previous else 1800)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, max(0.001, previous - (time.monotonic() - started)) if previous else 0,
                         interval)
        signal.signal(signal.SIGALRM, old_alarm)
        signal.signal(signal.SIGTERM, old_term)


def execute(cfg: config.Config, p: Plan, run_id: str) -> list[int]:
    record.start("merge-check", run_id=run_id, repo=p.repo.name)
    try:
        failed = execute_attempt(cfg, p, run_id)
    except BaseException:
        record.settle(run_id, "failed", summary="merge-check blocked; evidence retained locally")
        raise
    record.settle(run_id, "done", summary="merged base failed in use" if failed else "merged base passed in use")
    return failed


def execute_attempt(cfg: config.Config, p: Plan, run_id: str) -> list[int]:
    folder = paths.RUNS_DIR / run_id
    folder.mkdir(parents=True, exist_ok=False)
    run = CheckRun(id=run_id, repo=p.repo, dir=folder, cfg=cfg, layer=p.layer,
                   deadline=time.monotonic() + 1800, writes_lines=False)
    made = False
    with deadline():
        try:
            util.git(["fetch", "origin"], p.repo.path)
            sha = util.git(["rev-parse", "--verify", f"origin/{p.base}^{{commit}}"], p.repo.path).strip()
            p.worktree.parent.mkdir(parents=True, exist_ok=True)
            util.git(["worktree", "add", "--detach", str(p.worktree), sha], p.repo.path)
            made = True
            run.save("manifest.json", {"base": f"origin/{p.base}", "sha": sha, "worktree": str(p.worktree),
                                      "runs": p.batch, "steps": [asdict(s) for s in p.steps]})
            for rel in p.layer.copy:
                src, dst = p.repo.path / rel, p.worktree / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            for command in p.layer.setup:
                result = environment.sh(command, cfg, p.repo, cwd=p.worktree,
                                        timeout=max(1, run.deadline - time.monotonic()))
                if result.returncode:
                    raise Blocked(f"{p.repo.name}: setup failed")
            ticket = tracker.Ticket(id=run_id, ref=run_id, title="Use the merged base", body="")
            work = TicketWork(cfg, run, ticket, p.worktree,
                              [workspace.Checkout("", p.repo.github, p.repo.path, p.worktree, p.base)], p.layer, 1800,
                              workspace=f"Detached checkout of origin/{p.base} at {sha}.")
            goal = "\n".join(s.goal(n) for n, s in enumerate(p.steps, 1))
            verified = use_product(work, "verify", "grade", goal=goal, require_released=True)
            run.save("result.json", asdict(verified))
            if verified.verdict == "fail" and not run.invoked:
                p.startup_failed = True
                prevented = list(range(1, len(p.steps) + 1))
                run.save("startup-failure.json", {"prevented_steps": prevented})
                return prevented
            if verified.verdict not in ("pass", "fail") or not run.invoked:
                raise Blocked(verified.blocked_by or "the product or verifier did not start")
            accounted: dict[int, bool] = {}
            for check in verified.checks:
                match = NUMBER.match(str(check.get("what") or ""))
                if not match or int(match[1]) not in range(1, len(p.steps) + 1) or type(check.get("passed")) is not bool:
                    raise Blocked("verifier returned an unknown or invalid step")
                n = int(match[1])
                accounted[n] = accounted.get(n, True) and check["passed"]
            if len(accounted) != len(p.steps):
                raise Blocked("verifier did not account for every manifest step")
            failed = [n for n, passed in sorted(accounted.items()) if not passed]
            if verified.verdict == "fail" and not failed:
                raise Blocked("verifier reported failure without a failed manifest step")
            return failed
        finally:
            if made:
                if (folder / "verify-retained.json").exists() or verifier_suites.held(folder, p.worktree, "verify"):
                    print(f"merge-check: retained {p.worktree}; scratch ownership is unresolved", file=sys.stderr)
                    raise Blocked("verification scratch ownership is unresolved; workspace retained")
                else:
                    util.git(["worktree", "remove", "--force", str(p.worktree)], p.repo.path)


def consume(cfg: config.Config, repo_name: str, supplied: list[dict], dry_run: bool = False) -> int:
    if dry_run:
        return consume_batch(cfg, repo_name, supplied, True)
    with deadline():
        return consume_batch(cfg, repo_name, supplied)


def consume_batch(cfg: config.Config, repo_name: str, supplied: list[dict], dry_run: bool = False) -> int:
    repos = [cfg.repo(repo_name)] if repo_name else [cfg.repo(name) for name in dict.fromkeys(e["repo"] for e in supplied)]
    if any(not r.pipeline for r in repos):
        raise Blocked("merge-check only checks configured pipeline repositories")
    rows = merged(cfg)
    known = {r["key"]: r for r in rows}
    for event in supplied:
        key = event.get("key") or event.get("url")
        if not isinstance(key, str) or key not in known or known[key]["repo"] != event["repo"]:
            raise Blocked("event does not identify a recorded merged pipeline PR")
        if repo_name and event["repo"] != repo_name:
            raise Blocked("stdin event repo does not match the requested repository")
    folder = loop_dir() / ".state"
    if dry_run:
        previews = []
        for repo in repos:
            sample_keys = {e.get("key") or e.get("url") for e in supplied if e["repo"] == repo.name}
            checked = read_json(folder / "checked.json", {}).get(repo.name, [])
            batch = [r for r in rows if r["repo"] == repo.name and
                     (r["key"] in sample_keys if sample_keys else r["key"] not in checked)]
            if batch:
                previews.append(plan(cfg, repo, rows, batch, "merge-check-preview", preview_only=True))
        for p in previews:
            preview(p, folder)
            if os.environ.get("ARETE_TRIAL") == "1":
                print(json.dumps(finding(p, list(range(1, len(p.steps) + 1)))))
                print(json.dumps({"value": 0}))
        if not previews:
            print("merge-check: skipped; nothing merged; zero model calls", file=sys.stderr)
        return 0
    with locked(folder):
        checked = read_json(folder / "checked.json", {})
        plans = []
        run_id = record.new_id("merge-check")
        for repo in repos:
            own = [r for r in rows if r["repo"] == repo.name]
            if repo.name not in checked:
                supplied_keys = {e.get("key") or e.get("url") for e in supplied if e["repo"] == repo.name}
                index = min((n for n, row in enumerate(own) if row["key"] in supplied_keys), default=0)
                checked[repo.name] = [r["key"] for r in own[:index]]
                if own:
                    save_json(folder / "checked.json", checked)
            batch = [r for r in own if r["key"] not in checked[repo.name]]
            if batch:
                plans.append(plan(cfg, repo, rows, batch, run_id + "-" + repo.name))
        for p in plans:
            if held_until(loop_dir(), p.repo.name):
                print(f"merge-check: quota hold for {p.repo.name} until {held_until(loop_dir(), p.repo.name)}", file=sys.stderr)
                return HOLD_EXIT
        if not plans:
            return 0
        save_json(folder / "checked.json", checked)
        for p in plans:
            data, used, _ = quota(folder, p.repo.name)
            data[p.repo.name] = {"day": str(datetime.now(UTC).date()), "attempts": used + 1}
            save_json(folder / "quota.json", data)
        outputs = []
        for p in plans:
            failed = execute(cfg, p, run_id + "-" + p.repo.name)
            if failed:
                outputs.append(finding(p, failed))
            outputs.append({"value": 0 if failed else 1})
            checked[p.repo.name] += [r["key"] for r in p.batch]
        save_json(folder / "checked.json", checked)
        for output in outputs:
            print(json.dumps(output, separators=(",", ":")))
    return 0


def command(args) -> int:
    try:
        cfg = config.load()
        if args.cmd == "merged":
            for event in events(cfg, args.since_cursor):
                print(json.dumps(event))
            return 0
        if args.repo:
            cfg.repo(args.repo)
        supplied = read_events(sys.stdin.read() if not sys.stdin.isatty() else "")
        return consume(cfg, args.repo, supplied, args.dry_run)
    except (config.UnknownRepo, ValueError, OSError, RuntimeError) as error:
        print(f"arete: {util.first_line(str(error), 400)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("arete: merge-check interrupted; unchecked work is retained", file=sys.stderr)
        return 130
