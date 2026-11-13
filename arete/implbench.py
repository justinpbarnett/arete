from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import evals, experiment, paths, pipeline, record, replay, routing, runner, state, steps, util

GROUPS = ("ordinary", "difficult")
DECIDES = "holdout"
NOTE = "This report changes no route, tier or config."
WORDS = {"add-word": "kumquat", "exact-word": "quince", "two-files": "medlar"}
SETUP = "setup"
SAMPLE_ARMS = {"current": 0.04, "strong": 0.09, "cheap": 0.004}


@dataclass
class Bench:
    exp: experiment.Experiment
    groups: dict[str, str]
    references: dict[str, str]


def declared_tasks(data: dict) -> tuple[dict[str, str], dict[str, str]]:
    groups, references = {}, {}
    table = data.get("tasks")
    for name, entry in (table.items() if isinstance(table, dict) else ()):
        if not isinstance(entry, dict):
            continue
        group = experiment.text(entry, "group", f"tasks.{name}.")
        if group not in GROUPS:
            raise experiment.Refused(f"tasks.{name}.group must be one of {', '.join(GROUPS)}, not {group!r}")
        groups[name] = group
        references[name] = experiment.text(entry, "reference", f"tasks.{name}.", "")
        del entry["group"]
        entry.pop("reference", None)
    return groups, references


def declared_accept(data: dict) -> None:
    accept = data.get("accept")
    if not isinstance(accept, dict):
        return
    if "min_difference" not in accept:
        raise experiment.Refused("accept.min_difference is required: it is the quality margin, declared before any "
                                 "trial runs")
    if accept.setdefault("split", DECIDES) != DECIDES:
        raise experiment.Refused(f"accept.split: a benchmark decides on {DECIDES}")


def reviewer(exp: experiment.Experiment) -> dict:
    try:
        s = steps.resolve(exp.cfg, exp.layer, "review", "")
    except ValueError as e:
        raise experiment.Refused(f"the reviewer: {e}") from e
    which = runner.chosen(s.model, s.runner, exp.cfg)[0]
    if why := runner.CANNOT_STOP.get(which) or runner.CANNOT_GATE.get(which):
        raise experiment.Refused(f"the reviewer: {why}")
    return {"runner": s.runner, "model": s.model, "effort": s.effort, "settings": experiment.effective(exp.cfg, s),
            "prompt": experiment.sha((paths.PROMPTS_DIR / "review.md").read_bytes())}


def content_of(title: str, prompt: str, verify: str, base: str, source: str) -> tuple:
    return title, prompt.strip(), verify.strip(), base, source


def mined_unchanged(exp: experiment.Experiment, task: evals.Task) -> bool:
    row = state.mined_row(task.source) if task.source else {}
    if row.get("status") != "accepted" or row.get("repo") != exp.repo.name or not row.get("run_id"):
        return False
    now = content_of(task.title, task.prompt, task.verify, task.base, task.source)
    for saved in (paths.RUNS_DIR / row["run_id"]).glob("*/task.toml"):
        try:
            data = tomllib.loads(saved.read_text())
        except (OSError, ValueError):
            continue
        then = content_of(str(data.get("title", "")), str(data.get("prompt", "")), str(data.get("verify", "")),
                          str(data.get("base", "")), str(data.get("source", "")))
        if then == now:
            return True
    return False


def resolved_references(exp: experiment.Experiment, references: dict[str, str]) -> dict[str, str]:
    found = {}
    for name, task in exp.tasks.items():
        if task.check:
            if not references[name]:
                raise experiment.Refused(f"tasks.{name}: reference is required for a task with a `check`: the commit "
                                         "the hidden check must pass on")
            found[name] = experiment.base_commit(exp.repo, name, references[name], "reference")
            if found[name] == task.base:
                raise experiment.Refused(f"tasks.{name}: reference is the base, so the change is empty")
        elif mined_unchanged(exp, task):
            found[name] = ""
        else:
            raise experiment.Refused(f"tasks.{name}: its steps were never shown to pass on the merged change and fail "
                                     "before it, as the task is now; add a `check` with hidden files and a "
                                     "`reference`")
    return found


def load(path: Path) -> Bench:
    data = tomllib.loads(path.read_text())
    reviews = data.pop("review", True)
    if not isinstance(reviews, bool):
        raise experiment.Refused(f"review must be true or false, not {reviews!r}")
    data.setdefault("atom", "build")
    groups, references = declared_tasks(data)
    declared_accept(data)
    exp = experiment.designed(data)
    for group in dict.fromkeys(groups.values()):
        if not any(exp.splits[t] == DECIDES for t, g in groups.items() if g == group):
            raise experiment.Refused(f"the {group} group has no {DECIDES} task")
    references = resolved_references(exp, references)
    held = reviewer(exp) if reviews else None
    design = {**exp.design, "benchmark": {"groups": groups, "references": references, "review": held}}
    return Bench(replace(exp, design=design, digest=experiment.sha_json(design)[:12], reviews=reviews,
                         checks_required=True), groups, references)


@dataclass
class Clock:
    exp: experiment.Experiment
    path: Path
    used: experiment.Totals
    began: float = field(default_factory=time.monotonic)
    noted: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def left(self) -> float:
        return self.exp.limits["minutes"] * 60 - self.used.seconds - (time.monotonic() - self.began)

    def check(self) -> None:
        if self.left() <= 0:
            spent = replace(self.used, seconds=self.exp.limits["minutes"] * 60)
            raise experiment.Capped(experiment.reached(self.exp.limits, spent, launching=False))

    def note(self) -> None:
        with self.lock:
            now = time.monotonic()
            seconds = round(now - self.noted, 2)
            if seconds > 0:
                experiment.append(self.path, {"event": SETUP, "seconds": seconds, "at": util.iso()})
            self.noted = now

    @contextmanager
    def saving(self):
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(experiment.ALIVE_SECONDS):
                self.note()

        beater = threading.Thread(target=beat, name="benchmark setup", daemon=True)
        beater.start()
        try:
            yield
        finally:
            stop.set()
            beater.join()
            self.note()


@dataclass
class ProofRun(evals.EvalRun):
    clock: Clock | None = None

    def time_for(self, seconds: float) -> float:
        self.clock.check()
        return min(seconds, self.clock.left())


def default_path(name: str) -> Path:
    return paths.STATE_DIR / "benchmarks" / f"{name}.jsonl"


def proven(path: Path) -> set[str]:
    return {e.get("task") for e in experiment.events(path) if e.get("event") == "proof"}


def exit_code(exp: experiment.Experiment, task: evals.Task, commit: str, run: ProofRun) -> int:
    wt = paths.WORKTREES_DIR / f"eval-{exp.repo.name}-{task.name}-proof"
    try:
        run.clock.check()
        evals.checkout_without_origin_refs(exp.repo.path, commit, wt)
        try:
            evals.prepare(exp.repo, evals.Source(exp.repo), wt, run)
        except pipeline.RunFailed as e:
            run.clock.check()
            raise experiment.Refused(f"tasks.{task.name}: {e.why} on {commit[:7]}") from e
        code = evals.hidden_check(exp.cfg, exp.repo, task, wt, run.time_for(task.timeout_minutes * 60)).returncode
        if code == util.TIMED_OUT:
            run.clock.check()
        return code
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def prove_check(bench: Bench, path: Path, name: str, run: ProofRun) -> str:
    exp, task, reference = bench.exp, bench.exp.tasks[name], bench.references[name]
    on_reference = exit_code(exp, task, reference, run)
    on_base = exit_code(exp, task, task.base, run)
    if on_reference != 0:
        return f"tasks.{name}: the hidden check fails on the reference {reference[:7]} (exit {on_reference})"
    if on_base == 0:
        return (f"tasks.{name}: the hidden check passes on the base {task.base[:7]}, so it does not test the change")
    experiment.append(path, {"event": "proof", "task": name, "by": "check", "base": task.base,
                             "reference": reference, "on_reference": on_reference, "on_base": on_base,
                             "at": util.iso()})
    print(f"proven {name}: the hidden check passes on the reference {reference[:7]} and fails on the base "
          f"{task.base[:7]}", flush=True)
    return ""


def prove(bench: Bench, path: Path, clock: Clock) -> None:
    exp = bench.exp
    todo = [t for t in exp.tasks if t not in proven(path)]
    for name in (t for t in todo if not exp.tasks[t].check):
        experiment.append(path, {"event": "proof", "task": name, "by": "mine", "source": exp.tasks[name].source,
                                 "at": util.iso()})
        print(f"proven {name}: mined from a merged change, accepted, and unchanged since", flush=True)
    checked = [t for t in todo if exp.tasks[t].check]
    if not checked:
        return
    clock.check()
    run_id = record.start("eval", tag=f"benchmark-{exp.name}-proof", repo=exp.repo.name)
    with record.failed_on_crash(run_id):
        run = ProofRun(id=run_id, repo=exp.repo, dir=paths.RUNS_DIR / run_id, cfg=exp.cfg, task="proof", clock=clock)
        run.dir.mkdir(parents=True, exist_ok=True)
        try:
            failures = [why for name in checked if (why := prove_check(bench, path, name, run))]
        except experiment.Capped as e:
            record.settle(run_id, "stopped", summary=str(e))
            raise
        record.settle(run_id, "failed" if failures else "done",
                      summary="; ".join(failures) or f"{util.count(len(checked), 'task')} proven")
    if failures:
        raise experiment.Refused("\n".join(failures))


def unusable(candidates: list, atom: str) -> list:
    return [c for c in candidates if not (c.state == "ready" and c.runner not in runner.CANNOT_STOP
                                          and not (atom == "plan" and c.runner in runner.CANNOT_GATE))]


def unavailable(exp: experiment.Experiment) -> dict[str, str]:
    if exp.cfg.replay.all:
        return {}
    discovery = routing.Discovery(exp.cfg, exp.repo, layer=exp.layer, priced=False)
    missing = {}
    for arm, s in exp.settings.items():
        which, name = runner.chosen(s.model, s.runner, exp.cfg)
        if which == runner.REPLAY and not s.requires.declared:
            continue
        kw = {**s.agent_kw(exp.cfg), **({} if exp.atom == "plan" else {"disallowed_tools": runner.NO_GIT_WRITES})}
        found = discovery.routes(runner.request_for(exp.cfg, exp.repo, which, name, s, True, kw, 0, exp.layer))
        if len(unusable(found, exp.atom)) == len(found):
            missing[arm] = "; ".join(f"{c.runner} ({c.credential}) {c.state}: {c.detail}" for c in found) or "no route"
    return missing


def drive(bench: Bench, path: Path) -> int:
    exp = bench.exp
    found = experiment.events(path)
    experiment.check_design(exp, path, found)
    if not any(e.get("event") == "manifest" for e in found):
        experiment.append(path, {"event": "manifest", "digest": exp.digest, "design": exp.design})
    experiment.append(path, {"event": "session", "at": util.iso(), "limits": exp.limits, "pid": os.getpid()})
    used = experiment.totals_of(experiment.events(path))
    if used.seconds > 0:
        print(f"{used.seconds:.1f} seconds of limits.minutes = {exp.limits['minutes']:g} already used in "
              f"{paths.short_path(path)}", flush=True)
    clock = Clock(exp, path, used)
    try:
        with clock.saving():
            prove(bench, path, clock)
            missing = unavailable(exp)
        experiment.append(path, {"event": "availability", "at": util.iso(), "arms": missing})
        for arm, why in missing.items():
            print(f"unavailable {arm}: {why}", flush=True)
        baseline = exp.accept["baseline"]
        if baseline in missing:
            raise experiment.Refused(f"the baseline {baseline} is unavailable: {missing[baseline]}")
        experiment.settle_interrupted(exp, path)
        planned = len(exp.tasks) * exp.repeats * (len(exp.arms) - len(missing))
        if done := len(experiment.completed(experiment.events(path))):
            print(f"{done} of {planned} trials already done in {paths.short_path(path)}", flush=True)
        code = experiment.work(replace(exp, skip=frozenset(missing)), path)
    except experiment.Capped as e:
        experiment.append(path, {"event": "stop", "why": str(e), "at": util.iso()})
        print(f"stopped: {e}", flush=True)
        code = experiment.LIMIT_EXIT
    except (KeyboardInterrupt, pipeline.Stopped):
        experiment.append(path, {"event": "stop", "why": "interrupted", "at": util.iso()})
        code = experiment.INTERRUPT_EXIT
    print(render(report(path)))
    return code


def rule_lines(bench: Bench) -> list[str]:
    accept = bench.exp.accept
    return [(f"rule: {accept['baseline']} is the baseline; {DECIDES} decides, in each group apart; a candidate wins "
             f"only with a difference above {accept['min_difference']:g} over at least {accept['min_pairs']} pairs "
             f"at {accept['confidence']:g} confidence; otherwise {accept['baseline']} stays")]


def dry_lines(bench: Bench, path: Path) -> list[str]:
    exp = bench.exp
    done = proven(path)
    out = experiment.plan_lines(exp, path, "benchmark")
    for name in exp.tasks:
        reference = bench.references[name][:7] or "mined"
        out.append(f"benchmark task {name}: group {bench.groups[name]}, reference {reference}, "
                   f"{'proven' if name in done else 'to prove'}")
    held = exp.design["benchmark"]["review"]
    out.append(f"reviewer (fixed): runner {held['runner']}, model {held['model']}, effort {held['effort'] or 'default'}"
               if held else "review: off")
    out += [f"unavailable {arm}: {why}" for arm, why in unavailable(exp).items()]
    return out + rule_lines(bench)


def run(manifest: Path, out: str = "", dry: bool = False) -> int:
    bench = load(manifest)
    path = paths.expand(out) if out else default_path(bench.exp.name)
    if dry:
        print("\n".join(dry_lines(bench, path)))
        return 0
    return experiment.exclusive(bench.exp.repo.name, lambda: drive(bench, path))


def count_of(trials: list[dict], key: str, value: str) -> int:
    return sum(t.get(key) == value for t in trials)


def arm_row(arm: str, mine: list[dict], confidence: float) -> dict:
    attempts = [t for t in mine if t.get("arm") == arm]
    done = [t for t in attempts if t.get("status") == "done"]
    row = experiment.arm_summary(arm, mine, confidence)
    cost = experiment.total_of(attempts, "cost_usd")
    return {**row, "cost_usd": cost, "cost_per_accepted": experiment.per_accepted(cost, row["accepted"]),
            "tool_errors": sum(t.get("tool_errors") or 0 for t in attempts),
            "schema_errors": sum(t.get("schema_errors") or 0 for t in attempts),
            "checks_failed": count_of(done, "checks", "fail"), "hidden_failed": count_of(done, "hidden_check", "fail"),
            "verify_failed": count_of(done, "verify", "fail"), "verify_blocked": count_of(done, "verify", "blocked"),
            "blocking_findings": sum(t.get("blocking_findings") or 0 for t in done),
            "reviewed": sum(t.get("blocking_findings") is not None for t in done)}


def recommend(baseline: str, verdicts: dict, rows: dict, pairs: dict, order: list[str]) -> dict:
    winners = [c for c, v in verdicts.items() if v["state"] == "wins"]
    if not winners:
        return {"candidate": None, "text": f"keep {baseline}: no candidate clears the quality rule"}

    def rank(c: str) -> tuple:
        cost = rows[c]["cost_per_accepted"]
        return math.inf if cost is None else cost, -pairs[c]["difference"], order.index(c)

    best = min(winners, key=rank)
    return {"candidate": best, "text": f"{best} beats {baseline} on {DECIDES} at "
                                       f"{experiment.money(rows[best]['cost_per_accepted'])} per accepted task"}


def group_report(design: dict, group: str, trials: list[dict], missing: dict[str, str]) -> dict:
    accept, baseline = design["accept"], design["accept"]["baseline"]
    names = [t for t, g in design["benchmark"]["groups"].items() if g == group]
    if not names:
        return {"tasks": [], "arms": {}, "pairs": {}, "verdicts": {},
                "recommendation": {"candidate": None, "text": f"keep {baseline}: no tasks"}}
    mine = [t for t in trials if t.get("task") in names]
    done = {experiment.key_of(t): t for t in mine if t.get("status") == "done"}
    order = [a for a in design["arms"] if a != baseline]
    rows = {arm: arm_row(arm, mine, accept["confidence"]) for arm in design["arms"]}
    splits = [s for s in experiment.SPLITS if s in {design["tasks"][t]["split"] for t in names}]
    pairs = {s: [experiment.pairing(done, baseline, a, s, accept["confidence"]) for a in order if a not in missing]
             for s in splits}
    decided = {p["candidate"]: p for p in pairs.get(DECIDES, [])}
    verdicts = {a: ({"state": "unavailable", "text": f"unavailable, not judged: {missing[a]}"} if a in missing
                    else experiment.judge(decided[a], accept)) for a in order}
    return {"tasks": names, "arms": rows, "pairs": pairs, "verdicts": verdicts,
            "recommendation": recommend(baseline, verdicts, rows, decided, order)}


def report(path: Path) -> dict:
    found = experiment.events(path)
    manifest = next((e for e in found if e.get("event") == "manifest"), None)
    if manifest is None:
        raise experiment.Refused(f"{paths.short_path(path)} has no manifest line")
    design = manifest["design"]
    if "benchmark" not in design:
        raise experiment.Refused(f"{paths.short_path(path)} is not a benchmark file; use `arete experiment summary`")
    missing = next((e.get("arms") or {} for e in reversed(found) if e.get("event") == "availability"), {})
    trials = [e for e in found if e.get("event") == "trial"]
    done = {experiment.key_of(t) for t in trials if t.get("status") == "done"}
    planned = len(design["tasks"]) * design["repeats"] * (len(design["arms"]) - len(missing))
    return {"name": design["name"], "digest": manifest["digest"], "atom": design["atom"], "accept": design["accept"],
            "planned": planned, "done": len(done), "availability": missing,
            "setup_seconds": round(sum(e.get("seconds") or 0 for e in found if e.get("event") == SETUP), 2),
            "groups": {g: group_report(design, g, trials, missing) for g in GROUPS}, "note": NOTE}


def render_row(a: dict, confidence: float, baseline: str, missing: dict[str, str]) -> list[str]:
    if a["arm"] in missing:
        return [f"arm {a['arm']}: unavailable, {util.count(a['attempts'], 'trial')}"]
    rate = (f"{experiment.percent(a['rate'])}, {experiment.percent(confidence)} interval "
            f"{experiment.spread(a['interval'])}" if a["done"] else "no done trial")
    return [(f"arm {a['arm']}{' (baseline)' if a['arm'] == baseline else ''}: accepted {a['accepted']} of "
             f"{a['done']} done trials ({rate})"),
            (f"  checks failed {a['checks_failed']}, hidden check failed {a['hidden_failed']}, verify failed "
             f"{a['verify_failed']}, verify blocked {a['verify_blocked']}, blocking findings "
             f"{a['blocking_findings']} in {util.count(a['reviewed'], 'review')}"),
            f"  tool errors {a['tool_errors']}, schema errors {a['schema_errors']}, repairs {a['repairs']}",
            experiment.cost_line("cost", a["cost_usd"], a["cost_per_accepted"], a["accepted"]),
            experiment.cost_line("charged", a["charged_usd"], a["charged_per_accepted"], a["accepted"]),
            experiment.cost_line("reference", a["reference_usd"], a["reference_per_accepted"], a["accepted"])]


def recommendation_line(found: dict) -> str:
    return f"recommendation: {found['candidate']} ({found['text']})" if found["candidate"] \
        else f"recommendation: {found['text']}"


def render_group(group: str, g: dict, accept: dict, missing: dict[str, str]) -> list[str]:
    baseline, confidence = accept["baseline"], accept["confidence"]
    out = [f"group {group}: {util.count(len(g['tasks']), 'task')}"]
    for a in g["arms"].values():
        out += render_row(a, confidence, baseline, missing)
    for split, pairs in g["pairs"].items():
        out.append(f"pairs on {split}" + ("" if split == DECIDES else " (does not decide)") + ":")
        out += [experiment.render_pair(p, baseline, confidence) for p in pairs]
    out += [f"  {arm}: {v['text']}" for arm, v in g["verdicts"].items()]
    return out + [recommendation_line(g["recommendation"])]


def render(found: dict) -> str:
    out = [f"benchmark {found['name']} (design {found['digest']}), atom {found['atom']}",
           f"{found['done']} of {found['planned']} trials done"]
    if found["setup_seconds"] > 0:
        out.append(f"proof and setup: {found['setup_seconds']:g} seconds, counted against limits.minutes")
    for group, g in found["groups"].items():
        out += render_group(group, g, found["accept"], found["availability"])
    return "\n".join([*out, found["note"]])


def show(path: Path, as_json: bool) -> int:
    found = report(path)
    print(json.dumps(found) if as_json else render(found))
    return 0


def append_line(repo: Path, rel: str, line: str) -> None:
    with open(repo / rel, "a") as f:
        f.write(line + "\n")


def patch_of(repo: Path, edits: dict[str, str]) -> str:
    for rel, line in edits.items():
        append_line(repo, rel, line)
    patch = util.git(["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/"], repo)
    util.git(["checkout", "-q", "--", "."], repo)
    return patch


def commit_line(repo: Path, edits: dict[str, str], message: str) -> str:
    for rel, line in edits.items():
        append_line(repo, rel, line)
    util.git(["add", "-A"], repo)
    util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", message], repo)
    return util.git(["rev-parse", "HEAD"], repo).strip()


def sample_patches(repo: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    commits = [util.git(["rev-parse", "HEAD"], repo).strip()]
    both = {"docs/guide.txt": "medlar", "src/words.txt": "medlar"}
    patches = {"add-word": {"right": patch_of(repo, {"notes.txt": "kumquat"}),
                            "wrong": patch_of(repo, {"notes.txt": "lemon"})}}
    commits.append(commit_line(repo, {"notes.txt": "kumquat"}, "add kumquat"))
    patches["exact-word"] = {"right": patch_of(repo, {"notes.txt": "quince"}),
                             "wrong": patch_of(repo, {"notes.txt": "lime"})}
    commits.append(commit_line(repo, {"notes.txt": "quince"}, "add quince"))
    patches["two-files"] = {"right": patch_of(repo, both),
                            "wrong": patch_of(repo, {"docs/guide.txt": "medlar"})}
    commits.append(commit_line(repo, both, "add medlar to both files"))
    return commits, patches


def write_json(path: Path, answer: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**replay.SAMPLE_USAGE, **answer}, indent=2) + "\n")


def sample_review(blocked: bool) -> dict:
    answer = json.loads(json.dumps(replay.SAMPLE_ANSWERS["review"]))
    if blocked:
        answer["structured"] = {"verdict": "request_changes", "summary": "One of the two files is missing the word.",
                                "problems": [{"file": "src/words.txt", "problem": "The word is missing here.",
                                              "severity": "blocker"}]}
    return answer


def sample_answers(answers: Path, patches: dict[str, dict[str, str]]) -> None:
    right = {"current": ("right", "wrong", "right"), "strong": ("right", "right", "right"),
             "cheap": ("wrong", "wrong", "wrong")}
    for arm, cost in SAMPLE_ARMS.items():
        for task, kind in zip(patches, right[arm], strict=True):
            folder = answers / arm / task
            write_json(folder / "plan.json", replay.SAMPLE_ANSWERS["plan"])
            write_json(folder / "build.json", {**replay.SAMPLE_ANSWERS["build"], "cost_usd": cost})
            write_json(folder / "review.json", sample_review(arm == "cheap" and task == "two-files"))
            (folder / "build.patch").write_text(patches[task][kind])


def sample_layer(layer: Path, commits: list[str]) -> str:
    layer.mkdir(parents=True)
    runners = "".join(f"\n[steps.{step}]\nrunner = \"replay\"\n" for step in ("plan", "build", "verify", "review"))
    (layer / "config.toml").write_text(f"[checks]\nnotes = \"test -s notes.txt\"\n\n[git]\nbase = \"main\"\n{runners}")
    tasks = []
    for (task, word), base, reference in zip(WORDS.items(), commits[:-1], commits[1:], strict=True):
        files = ("docs/guide.txt", "src/words.txt") if task == "two-files" else ("notes.txt",)
        check = " && ".join(f"grep -qx {word} {rel}" for rel in files) + "\n"
        (layer / "evals" / task).mkdir(parents=True)
        (layer / "evals" / task / "hidden-check.sh").write_text(check)
        (layer / "evals" / f"{task}.toml").write_text(
            f"title = \"Add {word}\"\nprompt = \"Add the line {word} where it belongs.\"\n"
            f"check = \"sh hidden-check.sh\"\ntimeout_minutes = 5\n")
        group = "difficult" if task == "two-files" else "ordinary"
        tasks.append(f"[tasks.{task}]\ngroup = \"{group}\"\nsplit = \"holdout\"\nbase = \"{base}\"\n"
                     f"reference = \"{reference}\"\n")
    return "\n".join(tasks)


def sample(where: Path) -> list[str]:
    where = Path(os.path.abspath(where))
    if where.exists() and any(where.iterdir()):
        raise FileExistsError(f"{where} is not empty")
    repo, layer, answers = where / "repo", where / "layer", where / "replay"
    for made in (repo / "docs", repo / "src", answers):
        made.mkdir(parents=True)
    util.git(["init", "-q", "-b", "main"], repo)
    for rel, text in (("notes.txt", "notes\n"), ("docs/guide.txt", "guide\n"), ("src/words.txt", "words\n")):
        (repo / rel).write_text(text)
    util.git(["add", "-A"], repo)
    util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "first commit"], repo)
    commits, patches = sample_patches(repo)
    sample_answers(answers, patches)
    tasks = sample_layer(layer, commits)
    arms = "".join(f"[arms.{arm}]\nmodel = \"replay:{arm}\"\n" for arm in SAMPLE_ARMS)
    manifest = where / "benchmark.toml"
    manifest.write_text(
        "name = \"benchmark-sample\"\nrepo = \"benchmark-sample\"\nrepeats = 6\n\n" + arms
        + "[arms.absent]\nmodel = \"absent-provider/absent-model\"\n\n" + tasks
        + "\n[accept]\nbaseline = \"current\"\nmin_difference = 0.1\nmin_pairs = 6\nconfidence = 0.95\n\n"
        "[limits]\nspend_usd = 5\nminutes = 30\ncalls = 400\ntokens = 1000000\n")
    return [f"# wrote {repo}, {layer}, {answers} and {manifest}",
            (f"# add these lines to {paths.short_path(paths.CONFIG_FILE)}, best in a scratch "
             "ARETE_CONFIG_DIR; run the benchmark with:"),
            f"#   arete benchmark run {manifest}",
            "",
            "[replay]",
            "# no all = true: the arm without credentials is then judged by routing, not replayed",
            f"dir = {json.dumps(str(answers))}",
            "",
            "[[repo]]", "name = \"benchmark-sample\"", f"path = {json.dumps(str(repo))}",
            f"layer = {json.dumps(str(layer))}", "tracker = \"local\"", "jev = false"]
