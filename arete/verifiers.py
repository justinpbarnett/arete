from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    config,
    devices,
    environment,
    evals,
    outbound,
    paths,
    record,
    routing,
    runner,
    scratch,
    steps,
    tracker,
    util,
    verify,
)
from .agent import Result
from .pipeline import AgentRun, Checkout, TicketWork, use_product

NAME = re.compile(r"[A-Za-z0-9_-]+")
RESERVED = ("record", "scratch")
EXPECTED_BY_KIND = {"known_good": "pass", "seeded_regression": "fail", "historical_failure": "fail"}
TOP_KEYS = ("name", "repo", "repeats", "max_false_pass_rate", "limits", "verify", "arms", "tasks")
LIMIT_KEYS = ("minutes", "spend_usd", "timeout_minutes")
VERIFY_KEYS = ("start", "url", "ready", "ready_seconds", "notes", "allow_bash", "preflight_seconds", "preflight")
ARM_KEYS = ("tier", "runner", "provider", "model", "effort")
TASK_KEYS = ("kind", "expect", "title", "prompt", "requirements", "fails", "state", "base", "source")
CHECK_NUMBER = re.compile(r"^\W*(?:step|requirement)?\s*#?(\d+)\b", re.IGNORECASE)
NPX_REASON = "the browser's MCP server needs npx, which is not on PATH"
NO_CHROME_REASON = "Chrome was not found, and the browser's MCP server drives it"
CHROME_PATHS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",)
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chrome")
BROWSER_PROBE_SECONDS = 120
RESULTS = "results.jsonl"


class Refused(ValueError):
    pass


@dataclass
class Task:
    name: str
    kind: str
    expect: str
    title: str
    prompt: str
    requirements: list[str]
    fails: list[int]
    state: str = ""
    base: str = ""
    source: str = ""

    def expected(self, k: int) -> str:
        return "fail" if k in self.fails else "pass"


@dataclass
class Suite:
    name: str
    folder: Path
    repo: config.Repo | None
    layer: config.RepoLayer
    repeats: int
    max_false_pass_rate: float
    minutes: float
    spend_usd: float
    timeout_minutes: int
    verify: config.Verify
    arms: dict[str, dict]
    tasks: list[Task]

    @property
    def starts_app(self) -> bool:
        return bool(self.verify.start)

    @property
    def limits(self) -> dict:
        return {"minutes": self.minutes, "spend_usd": self.spend_usd, "timeout_minutes": self.timeout_minutes}


def table(found, where: str) -> dict:
    if not isinstance(found, dict):
        raise Refused(f"{where} must be a table")
    return found


def only(found: dict, allowed: tuple[str, ...], where: str) -> None:
    if bad := sorted(set(found) - set(allowed)):
        raise Refused(f"{where}: unknown key {', '.join(bad)}; use {', '.join(allowed)}")


def text_of(found: dict, key: str, where: str) -> str:
    value = found.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{where}: {key} must be a nonempty string")
    return value.strip()


def whole(found: dict, key: str, where: str, default: int | None = None) -> int:
    value = found.get(key, default)
    if type(value) is not int or value < 1:
        raise Refused(f"{where}: {key} must be a whole number of at least 1")
    return value


def amount(found: dict, key: str, where: str) -> float:
    value = found.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise Refused(f"{where}: {key} must be a number above 0")
    return float(value)


def named(name, where: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED or name.startswith("trial-"):
        raise Refused(f"{where}: the name {name!r} takes only letters, digits, _ and -, and is not reserved")
    return name


def verify_of(data: dict, inherited: config.Verify | None) -> config.Verify:
    found = table(data.get("verify", {}), "[verify]")
    only(found, VERIFY_KEYS, "[verify]")
    base = inherited or config.Verify()
    if "verify" not in data and not inherited:
        raise Refused("[verify] is required: it says how to start the app")
    for key in ("start", "url", "ready", "notes"):
        if key in found and not isinstance(found[key], str):
            raise Refused(f"[verify]: {key} must be a string")
    preflight = table(found.get("preflight", base.preflight), "[verify.preflight]")
    if any(not isinstance(k, str) or not k.strip() or not isinstance(c, str) or not c.strip()
           for k, c in preflight.items()):
        raise Refused("[verify.preflight] must map names to nonempty command strings")
    seconds = found.get("preflight_seconds", base.preflight_seconds)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= 300:
        raise Refused("[verify] preflight_seconds must be greater than 0 and at most 300")
    allow = found.get("allow_bash", base.allow_bash)
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise Refused("[verify] allow_bash must be a list of strings")
    ready_seconds = whole(found, "ready_seconds", "[verify]", base.ready_seconds)
    made = config.Verify(start=found.get("start", base.start), url=found.get("url", base.url),
                         ready=found.get("ready", base.ready), ready_seconds=ready_seconds,
                         allow_bash=list(allow), notes=found.get("notes", base.notes).strip(),
                         publish_evidence=inherited.publish_evidence if inherited else False,
                         preflight=dict(preflight), preflight_seconds=float(seconds))
    if not made.start and not inherited:
        raise Refused("[verify]: start is required")
    outbound.keep_verify_private(made.url)
    return made


def git_entries(root: Path) -> list[str]:
    found = []
    for here, dirs, files in os.walk(root, followlinks=False):
        found += [(Path(here) / name).relative_to(root).as_posix() for name in (*dirs, *files) if name == ".git"]
    return sorted(found)


def task_of(name: str, found, folder: Path, repo: config.Repo | None) -> Task:
    where = f"task {named(name, 'task')}"
    found = table(found, where)
    only(found, TASK_KEYS, where)
    kind, expect = found.get("kind"), found.get("expect")
    if kind not in EXPECTED_BY_KIND:
        raise Refused(f"{where}: kind must be one of {', '.join(EXPECTED_BY_KIND)}")
    if expect not in ("pass", "fail"):
        raise Refused(f"{where}: expect must be pass or fail")
    if expect != EXPECTED_BY_KIND[kind]:
        raise Refused(f"{where}: a {kind} task must expect {EXPECTED_BY_KIND[kind]}, not {expect}")
    wanted = found.get("requirements")
    if not isinstance(wanted, list) or not wanted or not all(isinstance(r, str) and r.strip() for r in wanted):
        raise Refused(f"{where}: requirements must be a nonempty list of strings")
    fails = found.get("fails", [])
    if (not isinstance(fails, list) or any(type(k) is not int or not 1 <= k <= len(wanted) for k in fails)
            or len(set(fails)) != len(fails)):
        raise Refused(f"{where}: fails must list requirement numbers from 1 to {len(wanted)}")
    if (expect == "fail") != bool(fails):
        raise Refused(f"{where}: fails must name a requirement when it expects fail, and none when it expects pass")
    state, base = found.get("state", ""), found.get("base", "")
    if bool(state) == bool(base):
        raise Refused(f"{where}: give exactly one of state and base")
    if state:
        shown = Path(text_of(found, "state", where))
        if shown.is_absolute() or ".." in shown.parts or not (folder / shown).is_dir():
            raise Refused(f"{where}: state must be a folder inside {folder}")
        if inner := git_entries(folder / shown):
            raise Refused(f"{where}: state {shown.as_posix()} holds {inner[0]}; a state folder is copied as plain "
                          "files into a new repository, so remove every .git folder and file, or use base")
    if base:
        text_of(found, "base", where)
        if repo is None:
            raise Refused(f"{where}: base needs a repo in the suite")
    if "source" in found and not isinstance(found["source"], str):
        raise Refused(f"{where}: source must be a string")
    return Task(name, kind, expect, text_of(found, "title", where), text_of(found, "prompt", where),
                [r.strip() for r in wanted], list(fails), state, base, found.get("source", ""))


def load(path: Path | str, cfg: config.Config | None = None) -> Suite:
    path = Path(path)
    data = tomllib.loads(path.read_text())
    cfg = cfg or config.load()
    only(data, TOP_KEYS, "the suite")
    name = named(data.get("name"), "name")
    if "repo" in data and not isinstance(data["repo"], str):
        raise Refused("repo must be a name")
    repo = cfg.repo(data["repo"]) if "repo" in data else None
    try:
        layer = config.load_layer(repo.layer_dir) if repo else config.RepoLayer(dir=path.parent)
    except ValueError as e:
        raise Refused(str(e)) from None
    repeats = whole(data, "repeats", "the suite", 1)
    rate = data.get("max_false_pass_rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        raise Refused("max_false_pass_rate is required: a number from 0 to 1, the most false passes you accept")
    limits = table(data.get("limits"), "[limits]") if "limits" in data else None
    if limits is None:
        raise Refused("[limits] is required: minutes, spend_usd and timeout_minutes")
    only(limits, LIMIT_KEYS, "[limits]")
    suite_verify = verify_of(data, layer.verify)
    arms = {named(n, "arm"): table(t, f"arm {n}") for n, t in table(data.get("arms"), "[arms]").items()}
    tasks = [task_of(n, t, path.parent, repo) for n, t in table(data.get("tasks"), "[tasks]").items()]
    if not arms or not tasks:
        raise Refused("the suite needs at least one arm in [arms] and one task in [tasks]")
    real_cfg = replace(cfg, replay=replace(cfg.replay, all=False))
    for arm, found in arms.items():
        only(found, ARM_KEYS, f"arm {arm}")
        try:
            steps.resolve(real_cfg, layer, "verify", "", found)
        except ValueError as e:
            raise Refused(f"arm {arm}: {e}") from None
    return Suite(name, path.parent, repo, layer, repeats, float(rate), amount(limits, "minutes", "[limits]"),
                 amount(limits, "spend_usd", "[limits]"), whole(limits, "timeout_minutes", "[limits]"), suite_verify,
                 arms, tasks)


def state_digest(suite: Suite, task: Task) -> str:
    if task.base:
        assert suite.repo
        return util.git(["rev-parse", f"{task.base}^{{commit}}"], suite.repo.path).strip()
    root, digest = suite.folder / task.state, hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts):
        digest.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return digest.hexdigest()


def plan(suite: Suite) -> list[tuple[int, Task, int, str]]:
    found = [(task, repeat, arm) for task in suite.tasks for repeat in range(1, suite.repeats + 1) for arm in suite.arms]
    return [(n, *rest) for n, rest in enumerate(found, 1)]


def real_config(cfg: config.Config) -> config.Config:
    return replace(cfg, replay=replace(cfg.replay, all=False))


def arm_status(suite: Suite, cfg: config.Config, arm: str) -> dict:
    real = real_config(cfg)
    s = steps.resolve(real, suite.layer, "verify", "", suite.arms[arm])
    which, name = runner.stands_for(s.model, s.runner)
    replayed = runner.chosen(s.model, s.runner, cfg)[0] == runner.REPLAY
    own = verify.mcp_config(paths.RUNS_DIR) if suite.starts_app else ""
    kw = {**s.agent_kw(real, ["mcp__playwright"] if suite.starts_app else [], own),
          "disallowed_tools": runner.NO_GIT_WRITES}
    asked = runner.request_for(real, suite.repo, which, name, s, False, kw, 0, suite.layer)
    reason = routing.fit_refusal(asked, devices.LOCAL)
    if not reason and suite.starts_app and not replayed and shutil.which("npx") is None:
        reason = NPX_REASON
    return {"arm": arm, "available": not reason, "reason": reason, "runner": which, "model": name,
            "effort": s.effort, "device": "local", "replayed": replayed}


def blocked_probe(suite: Suite, cfg: config.Config) -> str:
    if not suite.verify.preflight:
        return ""
    found = verify.preflight(suite.verify, suite.folder, cfg, suite.repo)
    return found.blocked_by.rstrip(" :") if found and found.verdict == "blocked" else ""


def browser_reason(suite: Suite, cfg: config.Config) -> str:
    candidates = [*CHROME_PATHS, *(shutil.which(name) for name in CHROME_NAMES)]
    chrome = next((p for p in candidates if p and Path(p).is_file()), "")
    if not chrome:
        return NO_CHROME_REASON
    real = real_config(cfg)
    probes = (("Chrome would not run headless", [chrome, "--headless", "--disable-gpu", "--dump-dom", "about:blank"]),
              ("the browser's MCP server would not launch", ["npx", "-y", verify.PLAYWRIGHT_MCP, "--version"]))
    for failure, command in probes:
        try:
            found = environment.sh(command, real, suite.repo, cwd=suite.folder, timeout=BROWSER_PROBE_SECONDS)
        except OSError as e:
            return f"{failure}: {util.first_line(str(e) or type(e).__name__, 200)}"
        if found.returncode != 0:
            return f"{failure}: {util.first_line(found.stderr or found.stdout or f'exit {found.returncode}', 200)}"
    return ""


def grouped(statuses: list[dict], blocked: str, suite: Suite, cfg: config.Config) -> list[dict]:
    if blocked:
        return [{**s, "available": False, "reason": blocked} for s in statuses]
    needing = [s for s in statuses if s["available"] and suite.starts_app and not s["replayed"]]
    reason = browser_reason(suite, cfg) if needing else ""
    return [{**s, "available": False, "reason": reason} if reason and s in needing else s for s in statuses]


class Results:
    def __init__(self, path: Path):
        self.path = path

    def add(self, line: str, /, **fields) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps({"type": line, **fields}) + "\n")


@dataclass
class TrialRun(AgentRun):
    results: list[Result] = field(default_factory=list)

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw) -> Result:
        res = super().agent(step, text, cwd, role, **{"fallback": False, "wait_out_limits": False, **kw})
        self.results.append(res)
        return res


def requirement_rows(task: Task, verified: verify.Verified | None) -> list[dict]:
    seen: dict[int, list[bool]] = {}
    for check in verified.checks if verified else []:
        if found := CHECK_NUMBER.match(str(check.get("what") or "")):
            seen.setdefault(int(found.group(1)), []).append(bool(check.get("passed")))
    return [{"n": k, "expected": task.expected(k),
             "seen": "missing" if k not in seen else "pass" if all(seen[k]) else "fail"}
            for k in range(1, len(task.requirements) + 1)]


def numbered(task: Task) -> str:
    return "\n".join(f"{k}. {text}" for k, text in enumerate(task.requirements, 1))


def workspace(suite: Suite, cfg: config.Config, task: Task, wt: Path) -> None:
    shutil.rmtree(wt, ignore_errors=True)
    if task.state:
        shutil.copytree(suite.folder / task.state, wt, ignore=shutil.ignore_patterns(".git"))
        if git_entries(wt):
            raise RuntimeError("the state folder left a .git entry in the trial workspace")
        util.git(["init", "-q", "-b", "main"], wt)
        util.git(["add", "-A"], wt)
        util.git([*util.ARETE_IDENTITY, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "--allow-empty",
                  "-m", "state"], wt)
        return
    assert suite.repo
    evals.checkout_without_origin_refs(suite.repo.path, task.base, wt)
    for cmd in suite.layer.setup:
        if environment.sh(cmd, cfg, suite.repo, cwd=wt, timeout=1200).returncode != 0:
            raise RuntimeError(f"setup `{cmd}` failed")


def held(trial_dir: Path, wt: Path, step: str) -> bool:
    try:
        found = scratch.records(True)
    except (OSError, ValueError, KeyError, TypeError):
        return scratch.open_marker(trial_dir, step).exists()
    return any(d["state"] != "done" and (d["attempt"]["run_dir"] == str(trial_dir.resolve())
                                         or scratch.overlap(Path(d["attempt"]["workspace"]), wt.resolve()))
               for d in found)


def run_trial(suite: Suite, cfg: config.Config, run_id: str, n: int, task: Task, arm: str, repeat: int,
              minutes_left: float, spend_left: float) -> dict:
    step = f"verify-{n}"
    neutral = paths.RUNS_DIR / run_id / f"trial-{n}"
    trial_dir = neutral / run_id
    final = paths.RUNS_DIR / run_id / task.name / arm / str(repeat)
    wt = paths.WORKTREES_DIR / f"verifiers-{run_id}-{n}"
    row: dict = {"n": n, "step": step, "task": task.name, "kind": task.kind, "expect": task.expect,
                 "arm": arm, "repeat": repeat, "status": "blocked", "outcome": "blocked", "verdict": "",
                 "blocked_by": "", "cost_usd": 0.0, "paid_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                 "api_seconds": 0.0, "runner": "", "model": "", "state_digest": "", "requirements": [],
                 "evidence": "", "screenshots": [], "retained": None}
    started = time.monotonic()
    run = None
    verified = None
    stage = "setup"
    try:
        trial_dir.mkdir(parents=True)
        row["state_digest"] = state_digest(suite, task)
        workspace(suite, cfg, task, wt)
        repo = suite.repo or config.Repo(name=f"verifiers-{suite.name}", path=wt, tracker="local", jev=False,
                                         layer=suite.folder)
        layer = replace(suite.layer, verify=replace(suite.verify, preflight={}))
        timeout = max(1, int(min(suite.timeout_minutes, minutes_left)))
        run = TrialRun(id=run_id, repo=repo, dir=trial_dir, cfg=cfg, layer=layer,
                       overrides={**suite.arms[arm], "timeout_minutes": timeout, "budget_usd": round(spend_left, 6)})
        ticket = tracker.Ticket(id=f"trial-{n}", ref=f"trial-{n}", title=task.title, body=task.prompt)
        work = TicketWork(cfg, run, ticket, wt, [Checkout("", repo.github, repo.path, wt, "")], layer, timeout * 60,
                          workspace="You are in a checkout of its code.")
        stage = "verifier call"
        verified = use_product(work, step, "grade", role="verify", goal=numbered(task))
        if not (trial_dir / f"{step}.md").exists():
            run.save(f"{step}.md", verified.markdown())
        row["verdict"] = verified.verdict
        if not run.results or verified.verdict == "blocked":
            row["blocked_by"] = util.first_line(verified.blocked_by or verified.summary, 300)
        else:
            row.update(status="judged",
                       outcome="correct" if verified.verdict == task.expect else
                       "false_pass" if verified.verdict == "pass" else "false_failure")
    except Exception as e:
        row["blocked_by"] = f"{stage}: {util.first_line(str(e) or type(e).__name__, 300)}"
    finally:
        kept = held(trial_dir, wt, step)
        if not kept:
            shutil.rmtree(wt, ignore_errors=True)
    if kept:
        reason = f"verification scratch is unresolved; kept {wt} and {trial_dir} for recovery by arete poll"
        row.update(status="blocked", outcome="blocked",
                   blocked_by=f"{reason}; {row['blocked_by']}" if row["blocked_by"] else reason,
                   retained={"worktree": str(wt), "folder": str(trial_dir)})
    results = run.results if run else []
    row.update(requirements=requirement_rows(task, verified) if row["status"] == "judged" else [],
               seconds=round(time.monotonic() - started, 3), cost_usd=round(run.cost if run else 0.0, 6),
               paid_usd=round(run.paid if run else 0.0, 6), tokens_in=sum(r.tokens_in for r in results),
               tokens_out=sum(r.tokens_out for r in results), api_seconds=round(sum(r.api_seconds for r in results), 2),
               runner=results[-1].runner if results else "", model=results[-1].model if results else "")
    if kept:
        row["evidence"] = str(trial_dir / "evidence")
        row["screenshots"] = sorted(p.name for p in (trial_dir / "evidence").glob("*.png"))
        return row
    if trial_dir.exists():
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(final, ignore_errors=True)
        shutil.move(trial_dir, final)
        row["evidence"] = str(final / "evidence")
        row["screenshots"] = sorted(p.name for p in (final / "evidence").glob("*.png"))
    shutil.rmtree(neutral, ignore_errors=True)
    return row


def stop_reason(suite: Suite, started: float, spent: float) -> str:
    minutes = (time.monotonic() - started) / 60
    if minutes >= suite.minutes:
        return f"the time limit of {suite.minutes:g} minutes was reached after {minutes:.1f}"
    if spent >= suite.spend_usd:
        return f"the spend limit of ${suite.spend_usd:g} was reached after ${spent:.3f}"
    return ""


def execute(suite: Suite, cfg: config.Config, run_id: str) -> tuple[Results, bool]:
    (paths.RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
    results = Results(paths.RUNS_DIR / run_id / RESULTS)
    statuses = grouped([arm_status(suite, cfg, a) for a in suite.arms], blocked_probe(suite, cfg), suite, cfg)
    available = {s["arm"]: s["available"] for s in statuses}
    results.add("suite", name=suite.name, run=run_id, repeats=suite.repeats,
                max_false_pass_rate=suite.max_false_pass_rate, limits=suite.limits,
                tasks=[{"name": t.name, "kind": t.kind, "expect": t.expect, "requirements": len(t.requirements),
                        "fails": t.fails} for t in suite.tasks],
                arms=list(suite.arms), publish_evidence=suite.verify.publish_evidence, evidence_published=False,
                evidence=str(paths.RUNS_DIR / run_id))
    for s in statuses:
        results.add("preflight", **s)
    started, spent, stopped = time.monotonic(), 0.0, False
    for n, task, repeat, arm in plan(suite):
        if not available[arm]:
            continue
        if why := stop_reason(suite, started, spent):
            results.add("stop", why=why)
            stopped = True
            break
        left = suite.minutes - (time.monotonic() - started) / 60
        row = run_trial(suite, cfg, run_id, n, task, arm, repeat, left, suite.spend_usd - spent)
        spent += row["cost_usd"]
        results.add("trial", **row)
    return results, stopped


def read(path: Path | str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def ratio(part: int, whole_: int) -> float | None:
    return part / whole_ if whole_ else None


def arm_summary(found: dict, trials: list[dict], planned: int, regressions: set[str], maximum: float) -> dict:
    judged = [t for t in trials if t["status"] == "judged"]
    blocked = [t for t in trials if t["status"] == "blocked"]
    against = [t for t in judged if t["expect"] == "fail"]
    good = [t for t in judged if t["expect"] == "pass"]
    wanted = [r for t in judged for r in t["requirements"]]
    false_passes = sum(t["outcome"] == "false_pass" for t in judged)
    false_failures = sum(t["outcome"] == "false_failure" for t in judged)
    accepted = sum(t["outcome"] == "correct" for t in judged)
    cost = round(sum(t["cost_usd"] for t in trials), 6)
    rate = ratio(false_passes, len(against))
    available = bool(found.get("available"))
    return {"available": available, "reason": found.get("reason", ""), "runner": found.get("runner", ""),
            "model": found.get("model", ""), "effort": found.get("effort", ""), "planned": planned,
            "judged": len(judged), "blocked": len(blocked), "not_run": planned - len(judged) - len(blocked),
            "blocked_by": sorted({t["blocked_by"] for t in blocked}), "false_passes": false_passes,
            "regressions_judged": len(against), "false_pass_rate": rate, "false_failures": false_failures,
            "known_good_judged": len(good), "false_failure_rate": ratio(false_failures, len(good)),
            "requirements": len(wanted), "covered": sum(r["seen"] != "missing" for r in wanted),
            "coverage": ratio(sum(r["seen"] != "missing" for r in wanted), len(wanted)),
            "labels_right": sum(r["seen"] == r["expected"] for r in wanted), "accepted": accepted,
            "seconds": round(sum(t["seconds"] for t in trials), 3), "cost_usd": cost,
            "paid_usd": round(sum(t["paid_usd"] for t in trials), 6),
            "cost_per_accepted": round(cost / accepted, 6) if accepted else None,
            "within_maximum": available and rate is not None and rate <= maximum
            and regressions <= {t["task"] for t in against}}


def standing(name: str, a: dict) -> tuple:
    tried = a["judged"] + a["blocked"]
    return (a["false_pass_rate"], a["false_failure_rate"] is None, a["false_failure_rate"] or 0,
            a["cost_per_accepted"] is None,
            a["cost_per_accepted"] or 0, a["seconds"] / max(tried, 1), name)


def summary(lines: list[dict]) -> dict:
    suite = next((line for line in lines if line.get("type") == "suite"), None)
    if suite is None:
        raise Refused("the file has no suite line; it is not a verifier results file")
    found = {line["arm"]: line for line in lines if line.get("type") == "preflight"}
    trials = [line for line in lines if line.get("type") == "trial"]
    stop = next((line for line in lines if line.get("type") == "stop"), None)
    regressions = {t["name"] for t in suite["tasks"] if t["expect"] == "fail"}
    planned = len(suite["tasks"]) * suite["repeats"]
    maximum = suite["max_false_pass_rate"]
    arms = {a: arm_summary(found.get(a, {}), [t for t in trials if t["arm"] == a], planned, regressions, maximum)
            for a in suite["arms"]}
    within = sorted((a for a in arms if arms[a]["within_maximum"]), key=lambda a: standing(a, arms[a]))
    return {"suite": suite["name"], "run": suite["run"], "max_false_pass_rate": maximum, "arms": arms,
            "recommended": within[0] if within else None, "also_within": within[1:],
            "stopped": stop["why"] if stop else "", "evidence": suite.get("evidence", ""),
            "publish_evidence": suite.get("publish_evidence", False)}


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def arm_lines(name: str, a: dict, maximum: float) -> list[str]:
    if not a["available"]:
        return [f"{name}: unavailable: {a['reason']}, not rated"]
    per = (f"${a['cost_per_accepted']:.3f} per accepted verification" if a["cost_per_accepted"] is not None
           else "no accepted verification")
    lines = [f"{name}: {a['judged']} judged, {a['blocked']} blocked, {a['not_run']} not run",
             (f"  false passes {a['false_passes']} of {a['regressions_judged']} ({percent(a['false_pass_rate'])}), "
              f"false failures {a['false_failures']} of {a['known_good_judged']} "
              f"({percent(a['false_failure_rate'])})"),
             (f"  coverage {a['covered']} of {a['requirements']} requirements, labels right "
              f"{a['labels_right']} of {a['requirements']}"),
             f"  {a['accepted']} accepted, {a['seconds']:.1f} seconds, cost ${a['cost_usd']:.3f}, {per}"]
    lines += [f"  blocked: {why}" for why in a["blocked_by"]]
    rate = a["false_pass_rate"]
    if rate is not None and rate > maximum:
        lines.append(f"  false-pass rate {rate:.2f} is over the declared maximum of {maximum:g}")
    elif not a["within_maximum"]:
        lines.append(f"  not within the declared maximum of {maximum:g}: a regression task was never judged")
    return lines


def render(found: dict) -> str:
    maximum = found["max_false_pass_rate"]
    out = [f"Verifier suite {found['suite']}, run {found['run']}", ""]
    for name, a in found["arms"].items():
        out += arm_lines(name, a, maximum)
    out += ["", f"Declared maximum false-pass rate: {maximum:g}"]
    if found["stopped"]:
        out.append(f"Stopped early: {found['stopped']}")
    out.append(f"Evidence stays in {found['evidence']}/<task>/<arm>/<repeat>/ and is never pushed "
               f"(the layer's publish_evidence is {str(found['publish_evidence']).lower()}).")
    if found["recommended"]:
        out.append(f"Recommendation: {found['recommended']}"
                   + (f" (also within the maximum: {', '.join(found['also_within'])})" if found["also_within"] else ""))
    else:
        rated = [a for a in found["arms"].values() if a["available"]]
        out.append("No recommendation: " + ("no arm was available to rate" if not rated else
                                            f"no available arm is within the declared maximum of {maximum:g}"))
    return "\n".join(out)


def report(path: Path | str, as_json: bool = False) -> int:
    found = summary(read(path))
    print(json.dumps(found) if as_json else render(found))
    return 0


def dry_run(suite: Suite, cfg: config.Config) -> int:
    trials = plan(suite)
    print(f"suite {suite.name}: {len(suite.tasks)} tasks, {len(suite.arms)} arms, {suite.repeats} repeats, "
          f"{len(trials)} trials")
    print("preflight probes (not run): " + (", ".join(suite.verify.preflight) or "none"))
    print("arms")
    for arm in suite.arms:
        s = arm_status(suite, cfg, arm)
        how = f"{s['runner']} {s['model']}" + (f" effort {s['effort']}" if s["effort"] else "")
        print(f"  {arm}: " + (f"available, {how}" + (" (replayed)" if s["replayed"] else "") if s["available"]
                              else f"unavailable, {s['reason']} ({how})"))
    print("tasks")
    for task in suite.tasks:
        print(f"  {task.name}: {task.kind}, expect {task.expect}, state {state_digest(suite, task)[:12]}")
        for k, text in enumerate(task.requirements, 1):
            print(f"    {k}. {text} (expected {task.expected(k)})")
    print("trials")
    for n, task, repeat, arm in trials:
        print(f"  {n} {task.name} {arm} {repeat} verify-{n}.json")
    print(f"declared maximum false-pass rate: {suite.max_false_pass_rate:g}")
    print("limits: " + ", ".join(f"{k} {v:g}" for k, v in suite.limits.items()))
    return 0


def run(path: Path | str, dry: bool = False) -> int:
    cfg = config.load()
    suite = load(path, cfg)
    if dry:
        return dry_run(suite, cfg)
    paths.ensure_dirs()
    lock = f"verifiers-{suite.name}"
    with util.lock(lock) as got:
        if not got:
            print(f"verifiers {suite.name} is already running")
            return 0
        run_id = record.start("eval", tag=lock, lock=lock, **({"repo": suite.repo.name} if suite.repo else {}))
        with record.failed_on_crash(run_id):
            results, stopped = execute(suite, cfg, run_id)
            found = summary(read(results.path))
            tried = sum(a["judged"] + a["blocked"] for a in found["arms"].values())
            record.settle(run_id, "done", summary=f"{tried} verifier trials, "
                          + (f"recommended {found['recommended']}" if found["recommended"] else "no recommendation"))
    print(f"results: {results.path}")
    print(render(found))
    return 3 if stopped else 0


def page(step: int = 1, reset: bool = True) -> str:
    button = '<button id="reset">Reset</button>\n' if reset else ""
    wire = 'document.getElementById("reset").onclick = () => { n = 0; show(); };\n' if reset else ""
    return ('<!doctype html>\n<title>Counter</title>\n<p id="count">Count: 0</p>\n<button id="add">Add one</button>\n'
            f'{button}<script>\nlet n = 0;\nconst show = () => {{ document.getElementById("count").textContent = '
            f'"Count: " + n; }};\ndocument.getElementById("add").onclick = () => {{ n += {step}; show(); }};\n'
            f'{wire}</script>\n')


REQUIREMENTS = ["The page shows Count: 0 when it loads", "Clicking Add one changes the count to 1",
                "Clicking Reset sets the count back to 0"]
SAMPLE_ARMS = {"careful": "replay:scratch-a", "lenient": "replay:scratch-b", "flaky": "replay:scratch-c",
               "no-browser": "replay:openrouter/scratch-d"}
SAMPLE_TASKS = [("counter-works", "known_good", "pass", [], "ui/good"),
                ("reset-missing", "seeded_regression", "fail", [3], "ui/missing"),
                ("adds-two", "seeded_regression", "fail", [2], "ui/incorrect")]
SAMPLE_COSTS = {"careful": 0.02, "lenient": 0.01, "flaky": 0.03}
UNLOADED = "the browser tools did not load"


def sample_manifest() -> str:
    start = f"{shlex.quote(sys.executable)} -m http.server {{port}} --bind 127.0.0.1"
    head = ['name = "counter"', "repeats = 2", "max_false_pass_rate = 0.0", "", "[limits]", "minutes = 15",
            "spend_usd = 1.0", "timeout_minutes = 5", "", "[verify]", f"start = {json.dumps(start)}",
            'url = "http://127.0.0.1:{port}"', ""]
    for arm, model in SAMPLE_ARMS.items():
        head += [f"[arms.{arm}]", f"model = {json.dumps(model)}", ""]
    for name, kind, expect, fails, state in SAMPLE_TASKS:
        head += [f"[tasks.{name}]", f'kind = "{kind}"', f'expect = "{expect}"',
                 'title = "Counter page"',
                 'prompt = "The page is a counter. It starts at 0, Add one adds one, and Reset sets it back to 0."',
                 f"requirements = {json.dumps(REQUIREMENTS)}"]
        head += [f"fails = {fails}"] if fails else []
        head += [f'state = "{state}"', ""]
    return "\n".join(head)


def sample_answer(arm: str, task: int, repeat: int) -> dict:
    cost = SAMPLE_COSTS[arm]
    if (arm, task, repeat) == ("flaky", 1, 0):
        verdict = {"verdict": "blocked", "summary": UNLOADED, "checks": [], "blocked_by": UNLOADED}
        return {"ok": True, "text": UNLOADED, "structured": verdict, "cost_usd": cost, "turns": 1, "seconds": 0}
    if (arm, task, repeat) == ("flaky", 2, 0):
        return {"ok": False, "error": "the verifier stopped before it gave a verdict", "cost_usd": cost, "turns": 1,
                "seconds": 0}
    failing = {1: {3}, 2: {2}}.get(task, set())
    if (arm, task, repeat) == ("flaky", 0, 0):
        failing = {2}
    named_steps = (1, 3) if (arm, task) == ("lenient", 2) else (1, 2, 3)
    if (arm, task) == ("lenient", 2):
        failing = set()
    checks = [{"what": f"Step {k}: {REQUIREMENTS[k - 1]}", "how": "Opened the page in the browser",
               "saw": "The page did not do this" if k in failing else "The page did this", "passed": k not in failing}
              for k in named_steps]
    said = "fail" if failing else "pass"
    return {"ok": True, "text": said, "structured": {"verdict": said, "summary": f"The check said {said}.",
                                                     "checks": checks}, "cost_usd": cost, "turns": 1, "seconds": 0}


def sample(folder: Path | str) -> list[str]:
    where = Path(folder).absolute()
    if where.exists() and any(where.iterdir()):
        raise FileExistsError(f"{where} is not empty")
    pages = {"good": page(), "missing": page(reset=False), "incorrect": page(step=2)}
    for name, html in pages.items():
        (where / "ui" / name).mkdir(parents=True)
        (where / "ui" / name / "index.html").write_text(html)
    (where / "replay").mkdir()
    manifest = sample_manifest()
    (where / "suite.toml").write_text(manifest)
    (where / "unavailable.toml").write_text(manifest.replace(
        "[arms.careful]", '[verify.preflight]\nbrowser = "exit 7"\n\n[arms.careful]', 1))
    n = 0
    for index in range(len(SAMPLE_TASKS)):
        for repeat in range(2):
            for arm in SAMPLE_ARMS:
                n += 1
                if arm in SAMPLE_COSTS:
                    (where / "replay" / f"verify-{n}.json").write_text(
                        json.dumps(sample_answer(arm, index, repeat), indent=2) + "\n")
    return [f"# wrote {where}/suite.toml, {where}/unavailable.toml, {where}/ui and 18 answers in {where}/replay",
            "# replay the suite on scratch config and state:",
            (f"ARETE_REPLAY_DIR={shlex.quote(str(where / 'replay'))} arete verifiers run "
             f"{shlex.quote(str(where / 'suite.toml'))}"),
            "# then print the report again from its results file:",
            "arete verifiers report RESULTS_FILE"]
