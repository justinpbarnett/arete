from __future__ import annotations

import json
import os
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import config, evals, experiment, implbench, paths, pipeline, record, replay, runner, steps, util
from .agent import Result

KINDS = ("patch", "test", "explore", "extract")
RULES = ("unchanged_patch", "same_failure", "either")
MAX_ATTEMPTS = 8
WORKER, ESCALATION = "worker", "escalation"
HANDOFFS = ("attempt limit", "no progress", "worker unavailable", "worker call failed")
ROLES = ("build", "check_repair")
BRIEF = {"plan": "No planner ran. The task above is the whole brief: do what it asks and nothing more.",
         "estimated_lines": 0, "verify": ""}
QUOTE = "the quote lives on this line"
SAMPLE_REPO = "workers-sample"
LAUNCH_FILE = "worker-launches.json"
PER_STEP = {"cheap": 0.01, "strong": 0.03}


def bounded(table: dict, key: str, where: str) -> int:
    found = experiment.number(table, key, where, whole=True)
    if not 1 <= found <= MAX_ATTEMPTS:
        raise experiment.Refused(f"{where}{key} must be from 1 to {MAX_ATTEMPTS}, not {found}")
    return found


def declared_escalation(name: str, route) -> dict | None:
    if route is None:
        return None
    where = f"arms.{name}.escalation"
    if not isinstance(route, dict):
        raise experiment.Refused(f"{where} must be a table")
    attempts = bounded(route, "attempts", f"{where}.")
    del route["attempts"]
    experiment.unknown(route, experiment.ARM_KEYS, where)
    if not route:
        raise experiment.Refused(f"{where}: name the route it takes")
    try:
        return {"route": config.step_table(route, where), "attempts": attempts}
    except ValueError as e:
        raise experiment.Refused(str(e)) from e


def declared_arms(data: dict) -> dict[str, dict]:
    found = {}
    table = data.get("arms")
    for name, entry in (table.items() if isinstance(table, dict) else ()):
        if not isinstance(entry, dict):
            continue
        where = f"arms.{name}."
        attempts = bounded(entry, "attempts", where)
        rule = experiment.text(entry, "no_progress", where)
        if rule not in RULES:
            raise experiment.Refused(f"{where}no_progress must be one of {', '.join(RULES)}, not {rule!r}")
        escalation = declared_escalation(name, entry.pop("escalation", None))
        del entry["attempts"], entry["no_progress"]
        found[name] = {"attempts": attempts, "no_progress": rule, "escalation": escalation}
    return found


def declared_tasks(data: dict) -> tuple[dict[str, str], dict[str, str]]:
    kinds, references = {}, {}
    table = data.get("tasks")
    for name, entry in (table.items() if isinstance(table, dict) else ()):
        if not isinstance(entry, dict):
            continue
        where = f"tasks.{name}."
        kind = experiment.text(entry, "kind", where)
        if kind not in KINDS:
            raise experiment.Refused(f"{where}kind must be one of {', '.join(KINDS)}, not {kind!r}")
        kinds[name] = kind
        references[name] = experiment.text(entry, "reference", where)
        del entry["kind"], entry["reference"]
        entry.setdefault("split", "validation")
    return kinds, references


def resolved_escalation(exp: experiment.Experiment, arm: str, found: dict | None) -> dict | None:
    if found is None:
        return None
    where = f"arms.{arm}.escalation"
    try:
        s = steps.resolve(exp.cfg, exp.layer, "check_repair", "", found["route"])
    except ValueError as e:
        raise experiment.Refused(f"{where}: {e}") from e
    experiment.codex_route(where, s, exp.limits)
    return {**found, "runner": s.runner, "model": s.model, "settings": experiment.effective(exp.cfg, s)}


def load(path: Path) -> implbench.Bench:
    data = tomllib.loads(path.read_text())
    if data.setdefault("atom", "build") != "build":
        raise experiment.Refused("atom: a worker trial runs the build step; leave atom out or set build")
    arms = declared_arms(data)
    kinds, references = declared_tasks(data)
    exp = experiment.designed(data)
    for name, task in exp.tasks.items():
        if not task.check:
            raise experiment.Refused(f"tasks.{name}: a worker task needs a `check`: its acceptance gate")
    references = implbench.resolved_references(exp, references)
    held = {arm: {**b, "escalation": resolved_escalation(exp, arm, b["escalation"])} for arm, b in arms.items()}
    design = {**exp.design, "workers": {"kinds": kinds, "references": references, "arms": held}}
    codex = [f"arms.{arm}.escalation ({w} {e['model']})" for arm, b in held.items() if (e := b["escalation"])
             and (w := runner.stands_for(e["model"], e["runner"])[0]) in runner.CANNOT_STOP]
    return implbench.Bench(replace(exp, design=design, digest=experiment.sha_json(design)[:12], checks_required=True,
                                   make_run=worker_run, recovered=recovered, codex=[*exp.codex, *codex]), kinds,
                           references)


def declared(path: Path) -> bool:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    arms = data.get("arms")
    return isinstance(arms, dict) and any(
        isinstance(e, dict) and {"attempts", "no_progress", "escalation"} & set(e) for e in arms.values())


def holds(path: Path) -> bool:
    manifest = next((e for e in experiment.events(path) if e.get("event") == "manifest"), None)
    return bool(manifest) and "workers" in (manifest.get("design") or {})


def call_entry(call: dict) -> dict:
    quote, usd = call.get("quote"), call.get("reference_usd")
    unpriced = usd is None or (usd == 0 and bool((quote or {}).get("unproven")))
    return {"runner": call.get("runner"), "model": call.get("model"), "provider": call.get("provider"),
            "tokens_in": call.get("tokens_in") or 0, "tokens_out": call.get("tokens_out") or 0,
            "cost_usd": call.get("cost_usd") or 0, "tariff": quote, "tariff_usd": None if unpriced else usd}


def launch_file(run_id: str) -> Path:
    return paths.RUNS_DIR / run_id / LAUNCH_FILE


def accounted(run_id: str, launches: list[dict]) -> list[dict]:
    files = record.record_files(run_id)
    ends = [launch["start"] for launch in launches[1:]] + [len(files)]
    found = []
    for launch, end in zip(launches, ends, strict=True):
        framed = [experiment.framed(f) for f in files[launch["start"]:end]] if launch["launched"] else []
        found.append([r for h, r in framed if r and not h.get("refused") and h.get("step") == launch["step"]])
    return [attempt_entry(n, launch, results) for n, (launch, results) in enumerate(zip(launches, found), 1)]


def outcomes_of(run_id: str, launches: list[dict], handoff: str) -> dict:
    attempts = accounted(run_id, launches)
    first = attempts[0] if attempts else {}
    return {"attempts": attempts, "handoff": handoff,
            "first_pass": len(attempts) == 1 and first["phase"] == WORKER and first.get("gate") == "pass",
            "unpriced_attempts": sum(a["launched"] and a["tariff_usd"] is None for a in attempts)}


def recovered(run_id: str) -> dict:
    kept = util.read_json(launch_file(run_id), None)
    if isinstance(kept, dict):
        return outcomes_of(run_id, kept.get("launches") or [], kept.get("handoff") or "")
    unaccounted = len(record.record_files(run_id))
    return {"handoff": "", "first_pass": False, "unpriced_attempts": unaccounted,
            **({} if unaccounted else {"attempts": []})}


def attempt_entry(n: int, launch: dict, results: list[dict]) -> dict:
    launched, route = launch["launched"], launch["route"]
    calls = [call_entry(c) for r in results for c in (r.get("calls") or [r])]
    last = results[-1] if results else {}
    known = [c["tariff_usd"] for c in calls]
    priced = launched and bool(calls) and None not in known
    return {"n": n, "step": launch["step"], "phase": launch["phase"], "launched": launched,
            "ok": bool(last.get("ok")),
            "endpoint": {"runner": last.get("runner") or route["runner"], "model": last.get("model") or route["model"],
                         "provider": last.get("provider") or route["provider"],
                         "credential": last.get("credential") or route["credential"],
                         "auth_mode": last.get("auth_mode") or route["auth_mode"]},
            "effort": last.get("effort") or route["effort"],
            "cost_usd": round(sum(c["cost_usd"] for c in calls), 6),
            "tokens_in": sum(c["tokens_in"] for c in calls), "tokens_out": sum(c["tokens_out"] for c in calls),
            "tariff": calls[-1]["tariff"] if calls else None, "tariff_usd": round(sum(known), 6) if priced else None,
            **({"calls": calls} if len(calls) > 1 else {}),
            **{k: launch[k] for k in ("gate", "changed", "seconds") if k in launch}}


@dataclass
class WorkerRun(experiment.TrialRun):
    bounds: dict = field(default_factory=dict)
    phase: str = WORKER
    launches: list = field(default_factory=list)
    over: bool = False
    patch: str = field(default_factory=lambda: experiment.sha(""))
    failure: str = ""
    going: bool = False
    handoff: str = ""

    def agent(self, step: str, text: str, cwd: Path, role: str = "", **kw):
        if step == "plan":
            return Result(ok=True, structured=dict(BRIEF))
        if role not in ROLES:
            return super().agent(step, text, cwd, role, **kw)
        res = self.launched(step, text, cwd, role, kw)
        if not res.ok and self.phase == WORKER and self.hand_off(
                "worker unavailable" if res.error.startswith(runner.NO_ROUTE) else "worker call failed"):
            if self.stopped_by == res.error:
                self.stopped_by = ""
            res = self.launched(step, text, cwd, role, kw)
        return res

    def launched(self, step: str, text: str, cwd: Path, role: str, kw: dict) -> Result:
        before = len(self.launches)
        try:
            res = super().agent(step, text, cwd, role, **kw)
        except pipeline.RunFailed:
            if self.over and self.phase == WORKER and self.hand_off("attempt limit"):
                self.over = False
                return self.launched(step, text, cwd, role, kw)
            raise
        if not res.ok and len(self.launches) == before and res.error.startswith(runner.NO_ROUTE):
            self.launches.append(self.launch_of(step, {"runner": res.runner, "model": res.model,
                                                       "provider": res.provider}, False))
            self.keep()
        return res

    def launch_of(self, step: str, route: dict, launched: bool) -> dict:
        return {"step": step, "phase": self.phase, "launched": launched, "began": time.monotonic(),
                "start": len(record.record_files(self.id)),
                "route": {k: route.get(k) or "" for k in ("runner", "model", "provider", "credential", "auth_mode",
                                                         "effort")}}

    def write_line(self, event: str, **fields) -> None:
        launching = event == "step_start" and fields.get("atom") in ROLES
        if launching and self.in_phase() >= self.allowed():
            self.over = True
            raise pipeline.RunFailed(f"the {self.phase} route is limited to {self.allowed()} launches")
        super().write_line(event, **fields)
        if launching:
            self.launches.append(self.launch_of(fields["step"], fields, True))
            self.keep()

    def keep(self) -> None:
        util.write_json(launch_file(self.id), {"launches": self.launches, "handoff": self.handoff})

    def in_phase(self) -> int:
        return sum(launch["phase"] == self.phase for launch in self.launches)

    def hand_off(self, why: str) -> bool:
        escalation = self.bounds.get("escalation")
        if not escalation or self.phase == ESCALATION:
            return False
        self.phase, self.handoff, self.failure = ESCALATION, why, ""
        self.keep()
        self.step_overrides = {**self.step_overrides, "build": escalation["route"],
                               "check_repair": escalation["route"]}
        self.log(f"handing the task to {escalation['runner']} {escalation['model']}: {why}")
        return True

    def allowed(self) -> int:
        return self.bounds["attempts"] if self.phase == WORKER else self.bounds["escalation"]["attempts"]

    def repair_limit(self, layer) -> int:
        return self.bounds["attempts"] + (self.bounds["escalation"] or {"attempts": 0})["attempts"]

    def stalled(self, changed: bool, report: str) -> bool:
        unchanged, repeated = not changed, report == self.failure
        return {"unchanged_patch": unchanged, "same_failure": repeated,
                "either": unchanged or repeated}[self.bounds["no_progress"]]

    def gate(self, layer, wt: Path, task, timeout: float) -> tuple[bool, str]:
        util.git(["add", "-A"], wt)
        patch = experiment.sha(util.git(["diff", "--cached", "HEAD"], wt))
        changed, self.patch = patch != self.patch, patch
        ok, report = super().gate(layer, wt, task, timeout)
        if ok:
            ok, report = self.task_gate(task, wt, timeout)
        last = self.launches[-1]
        last.update(gate="pass" if ok else "fail", changed=changed, seconds=round(time.monotonic() - last["began"], 2))
        self.keep()
        stalled = not ok and self.stalled(changed, report)
        self.failure = report
        under = self.in_phase() < self.allowed()
        self.going = not ok and ((under and not stalled)
                                 or self.hand_off("no progress" if stalled else "attempt limit"))
        return ok, report

    def task_gate(self, task, wt: Path, timeout: float) -> tuple[bool, str]:
        before = pipeline.untracked_by_dir([wt])
        existed = {rel for rel in task.hidden_files if os.path.lexists(wt / rel)}
        kept = {rel: (wt / rel).read_bytes() for rel in existed if (wt / rel).is_file()}
        try:
            res = evals.hidden_check(self.config, self.repo, task, wt, self.time_for(timeout))
        finally:
            for rel, data in kept.items():
                (wt / rel).write_bytes(data)
            for rel in task.hidden_files.keys() - existed:
                pipeline.remove_untracked(wt, rel)
            for d, name in pipeline.untracked_added_since(before):
                pipeline.remove_untracked(d, name)
        if res.returncode == 0:
            return True, ""
        return False, (f"### task check failed: `{task.check}` exit {res.returncode}\n```\n"
                       f"{pipeline.tail_of_both_streams(res.stdout, res.stderr, 6000)}\n```")

    def may_repair(self, attempts: int, limit: int) -> bool:
        return self.going

    def outcomes(self) -> dict:
        return outcomes_of(self.id, self.launches, self.handoff)


def worker_run(exp: experiment.Experiment, arm: str, given: dict) -> WorkerRun:
    overrides = {**given["step_overrides"], "check_repair": exp.arms[arm]}
    return WorkerRun(**{**given, "step_overrides": overrides}, bounds=exp.design["workers"]["arms"][arm])


def dry_lines(bench: implbench.Bench, path: Path) -> list[str]:
    exp = bench.exp
    design, done = exp.design["workers"], implbench.proven(path)
    out = experiment.plan_lines(exp, path, "worker benchmark")
    for arm, b in design["arms"].items():
        e = b["escalation"]
        after = f"escalation {e['runner']} {e['model']} for {e['attempts']} attempts" if e else "no escalation"
        out.append(f"bounds {arm}: {b['attempts']} worker attempts, no progress on {b['no_progress']}, {after}")
    for name in exp.tasks:
        out.append(f"worker task {name}: kind {design['kinds'][name]}, reference {design['references'][name][:7]}, "
                   f"{'proven' if name in done else 'to prove'}")
    out += [f"unavailable {arm}: {why}; " + ("its escalation route takes the work" if design["arms"][arm]["escalation"]
                                             else "not judged")
            for arm, why in implbench.unavailable(exp).items()]
    return out


def run(manifest: Path, out: str = "", dry: bool = False) -> int:
    bench = load(manifest)
    path = paths.expand(out) if out else implbench.default_path(bench.exp.name)
    if dry:
        print("\n".join(dry_lines(bench, path)))
        return 0
    hands_off = frozenset(arm for arm, b in bench.exp.design["workers"]["arms"].items() if b["escalation"])
    return experiment.exclusive(bench.exp.repo.name, lambda: implbench.drive(
        bench, path, hands_off=hands_off, shown=lambda p: render(report(p))))


def tariff_of(attempts: list[dict], phase: str) -> float | None:
    found = [a.get("tariff_usd") for a in attempts if a.get("phase") == phase]
    return None if None in found else round(sum(found), 6)


def kind_rows(design: dict, finished: list[dict]) -> dict:
    rows = {}
    for kind in KINDS:
        names = {t for t, k in design["workers"]["kinds"].items() if k == kind}
        mine = [t for t in finished if t.get("task") in names]
        if names:
            rows[kind] = {"done": len(mine), "first_pass": sum(bool(t.get("first_pass")) for t in mine),
                          "accepted": sum(bool(t.get("accepted")) for t in mine)}
    return rows


def accounted_for(trial: dict) -> bool:
    recorded, attempts = trial.get("reference_usd"), trial.get("attempts")
    if recorded is None or not isinstance(attempts, list):
        return False
    return abs(recorded - sum(a.get("tariff_usd") or 0 for a in attempts)) < 1e-6


def arm_row(arm: str, trials: list[dict], design: dict) -> dict:
    row = experiment.arm_summary(arm, trials, design["accept"]["confidence"])
    mine = [t for t in trials if t.get("arm") == arm]
    finished = list({experiment.key_of(t): t for t in mine if t.get("status") == "done"}.values())
    attempts = [a for t in mine for a in t.get("attempts") or []]
    launched = [a for a in attempts if a.get("launched")]
    handoffs = {r: n for r in HANDOFFS if (n := sum(t.get("handoff") == r for t in mine))}
    unknown = any(t.get("unpriced_attempts") or not accounted_for(t) for t in mine)
    worker, escalation = (None, None) if unknown else (tariff_of(launched, WORKER), tariff_of(launched, ESCALATION))
    total = None if None in (worker, escalation) else round(worker + escalation, 6)
    return {"arm": arm, "trials": len(mine), "done": row["done"], "accepted": row["accepted"], "rate": row["rate"],
            "interval": row["interval"], "first_pass": sum(bool(t.get("first_pass")) for t in finished),
            "worker_attempts": sum(a.get("phase") == WORKER for a in attempts),
            "escalation_attempts": sum(a.get("phase") == ESCALATION for a in attempts),
            "never_launched": len(attempts) - len(launched), "handoffs": handoffs,
            "seconds": row["wall_seconds"], "seconds_per_accepted": experiment.per_accepted(
                row["wall_seconds"], row["accepted"]),
            "charged_usd": row["charged_usd"], "charged_per_accepted": row["charged_per_accepted"],
            "tariff_usd": total, "tariff_per_accepted": experiment.per_accepted(total, row["accepted"]),
            "tariff_worker_usd": worker, "tariff_escalation_usd": escalation,
            "unpriced_attempts": sum(t.get("unpriced_attempts") or 0 for t in mine),
            "kinds": kind_rows(design, finished)}


def verdict(row: dict, base: dict) -> str:
    ours, theirs = row["tariff_per_accepted"], base["tariff_per_accepted"]
    if row["tariff_usd"] is None or base["tariff_usd"] is None:
        unpriced = row["unpriced_attempts"] + base["unpriced_attempts"]
        return (f"tariff cost unknown ({unpriced} attempts have no applicable rate): not compared, never counted "
                "as free")
    if ours is None or theirs is None:
        return "no accepted task to price: not compared"
    word = "cheaper" if ours < theirs else "costs more" if ours > theirs else "the same"
    return (f"tariff per accepted task {experiment.money(ours)} against {experiment.money(theirs)}: {word}")


def report(path: Path) -> dict:
    found = experiment.events(path)
    manifest = next((e for e in found if e.get("event") == "manifest"), None)
    if manifest is None:
        raise experiment.Refused(f"{paths.short_path(path)} has no manifest line")
    design = manifest["design"]
    if "workers" not in design:
        raise experiment.Refused(f"{paths.short_path(path)} is not a worker benchmark file")
    missing = next((e.get("arms") or {} for e in reversed(found) if e.get("event") == "availability"), {})
    trials = [e for e in found if e.get("event") == "trial"]
    baseline = design["accept"]["baseline"]
    rows = {arm: arm_row(arm, trials, design) for arm in design["arms"]}
    return {"name": design["name"], "digest": manifest["digest"], "atom": design["atom"], "accept": design["accept"],
            "planned": len(design["tasks"]) * design["repeats"] * (len(design["arms"]) - len(missing)),
            "done": len({experiment.key_of(t) for t in trials if t.get("status") == "done"}),
            "availability": missing, "arms": rows,
            "against": {arm: {"eventual": f"{r['accepted']} of {r['done']}",
                              "baseline": f"{rows[baseline]['accepted']} of {rows[baseline]['done']}",
                              "verdict": verdict(r, rows[baseline])}
                        for arm, r in rows.items() if arm != baseline and arm not in missing},
            "note": implbench.NOTE}


def render_arm(a: dict, confidence: float, baseline: str, missing: dict) -> list[str]:
    if a["arm"] in missing:
        return [f"arm {a['arm']}: unavailable, not judged"]
    rate = (f"{experiment.percent(a['rate'])}, {experiment.percent(confidence)} interval "
            f"{experiment.spread(a['interval'])}" if a["done"] else "no done trial")
    never = f" ({a['never_launched']} never launched)" if a["never_launched"] else ""
    reasons = ", ".join(f"{r} {n}" for r, n in a["handoffs"].items())
    handoffs = f"handoffs {sum(a['handoffs'].values())}" + (f" ({reasons})" if reasons else "")
    each = f"{a['seconds_per_accepted']:g} per accepted task" if a["accepted"] else "no accepted task"
    out = [(f"arm {a['arm']}{' (baseline)' if a['arm'] == baseline else ''}: first pass {a['first_pass']} of "
            f"{a['done']} done trials ({experiment.percent(a['first_pass'] / a['done']) if a['done'] else 'none'}), "
            f"eventual {a['accepted']} of {a['done']} ({rate})"),
           f"  attempts: worker {a['worker_attempts']}{never}, escalation {a['escalation_attempts']}; {handoffs}",
           f"  seconds: {a['seconds']:g} over every attempt, {each}",
           experiment.cost_line("charged", a["charged_usd"], a["charged_per_accepted"], a["accepted"]),
           (experiment.cost_line("tariff", a["tariff_usd"], a["tariff_per_accepted"], a["accepted"])
            + f"; worker {experiment.money(a['tariff_worker_usd'])}, "
              f"escalation {experiment.money(a['tariff_escalation_usd'])}")]
    return out + [f"  {kind}: first pass {k['first_pass']} of {k['done']}, eventual {k['accepted']} of {k['done']}"
                  for kind, k in a["kinds"].items()]


def render(found: dict) -> str:
    accept = found["accept"]
    out = [f"worker benchmark {found['name']} (design {found['digest']}), atom {found['atom']}",
           f"{found['done']} of {found['planned']} trials done"]
    for a in found["arms"].values():
        out += render_arm(a, accept["confidence"], accept["baseline"], found["availability"])
    out.append(f"against {accept['baseline']}:")
    out += [f"  {arm}: eventual {c['eventual']} against {c['baseline']}; {c['verdict']}"
            for arm, c in found["against"].items()]
    return "\n".join([*out, found["note"]])


def show(path: Path, as_json: bool) -> int:
    found = report(path)
    print(json.dumps(found) if as_json else render(found))
    return 0


def sample_files() -> dict[str, str]:
    drafts = {f"drafts/{n}.txt": "draft\n" for n in ("one", "two", "three", "four")}
    return {"src/words.txt": "words\n", "tests/words.test.sh": "# words test\n", "answer.txt": "answer\n",
            "evidence.txt": "evidence\n", "docs/guide.txt": f"guide\n{QUOTE}\n", **drafts}


def sample_edits() -> dict[str, tuple[str, str]]:
    return {"small-patch": ("src/words.txt", "kumquat"),
            "test-work": ("tests/words.test.sh", 'grep -qx words "${WORDS:-src/words.txt}"'),
            "find-file": ("answer.txt", "src/words.txt"), "pull-quote": ("evidence.txt", QUOTE)}


def sample_answers(answers: Path, right: dict[str, str], wrong: list[str]) -> None:
    strong = {task: {"build": (patch, "strong")} for task, patch in right.items()}
    cheap = {"small-patch": {"build": (right["small-patch"], "cheap")},
             "test-work": {"build": (wrong[0], "cheap"), "fix2": (right["test-work"], "cheap")},
             "find-file": {"build": (wrong[0], "cheap"), "fix2": (None, "cheap"),
                           "fix3": (right["find-file"], "strong")},
             "pull-quote": {"build": (wrong[0], "cheap"), "fix2": (wrong[1], "cheap"), "fix3": (wrong[2], "cheap"),
                            "fix4": (wrong[3], "strong")}}
    for arm, script in {"direct": strong, "absent": strong, "cheap": cheap}.items():
        for task, given in script.items():
            for step, (patch, tier) in given.items():
                folder = answers / arm / task
                implbench.write_json(folder / f"{step}.json", {
                    **replay.SAMPLE_ANSWERS["build"], "tokens_in": 1000, "tokens_out": 200, "turns": 1,
                    "cost_usd": PER_STEP[tier]})
                if patch is not None:
                    (folder / f"{step}.patch").write_text(patch)


def sample_layer(layer: Path) -> None:
    layer.mkdir(parents=True)
    runners = "".join(f"\n[steps.{step}]\nrunner = \"replay\"\n" for step in ("plan", "build", "verify", "review"))
    (layer / "config.toml").write_text(f"[checks]\nwords = \"test -s src/words.txt\"\n\n[git]\nbase = \"main\"\n"
                                       f"{runners}")
    hidden = {"small-patch": "grep -qx kumquat src/words.txt",
              "test-work": "sh tests/words.test.sh && ! WORDS=mutant.txt sh tests/words.test.sh",
              "find-file": "grep -qx src/words.txt answer.txt", "pull-quote": f"grep -qxF \"{QUOTE}\" evidence.txt"}
    prompts = {"small-patch": "Add the line kumquat at the end of src/words.txt.",
               "test-work": "Make tests/words.test.sh fail when WORDS names a file without the line words.",
               "find-file": "Write the path of the file that holds the line words on a line of its own in answer.txt.",
               "pull-quote": "Copy the second line of docs/guide.txt on a line of its own at the end of evidence.txt."}
    for task, check in hidden.items():
        (layer / "evals" / task).mkdir(parents=True)
        (layer / "evals" / task / "hidden-check.sh").write_text(check + "\n")
        (layer / "evals" / f"{task}.toml").write_text(
            f"title = \"{task}\"\nprompt = \"{prompts[task]}\"\ncheck = \"sh hidden-check.sh\"\ntimeout_minutes = 5\n")
    (layer / "evals" / "test-work" / "mutant.txt").write_text("other\n")


def price_snapshot(conditional: bool) -> dict:
    cheap = {"input": 5, "output": 25}
    if conditional:
        cheap = {**cheap, "when": {"min_input_tokens": 200000}, "label": "long context"}
    rates = {"cheap": cheap, "strong": {"input": 15, "output": 75}}
    return {"schema": 1, "source": "scratch sample", "observed": util.iso(),
            "routes": [{"route": f"replay:{name}", "kind": "native", "provider": "scratch", "model": name,
                        "currency": "USD", "unit": "per_million_tokens", "rates": [rate]}
                       for name, rate in rates.items()]}


def sample_manifest(first: str, reference: str) -> str:
    kinds = {"small-patch": "patch", "test-work": "test", "find-file": "explore", "pull-quote": "extract"}
    route = "no_progress = \"unchanged_patch\"\n"
    arms = (f"[arms.direct]\nmodel = \"replay:strong\"\nattempts = 2\n{route}\n"
            f"[arms.cheap]\nmodel = \"replay:cheap\"\nattempts = 3\n{route}\n"
            "[arms.cheap.escalation]\nmodel = \"replay:strong\"\nattempts = 1\n\n"
            f"[arms.absent]\nmodel = \"absent-provider/absent-model\"\nattempts = 2\n{route}\n"
            "[arms.absent.escalation]\nmodel = \"replay:strong\"\nattempts = 1\n\n")
    tasks = "".join(f"[tasks.{task}]\nkind = \"{kind}\"\nbase = \"{first}\"\nreference = \"{reference}\"\n\n"
                    for task, kind in kinds.items())
    return (f"name = \"{SAMPLE_REPO}\"\nrepo = \"{SAMPLE_REPO}\"\nrepeats = 2\n\n{arms}{tasks}"
            "[accept]\nbaseline = \"direct\"\n\n[limits]\nspend_usd = 5\nminutes = 30\ncalls = 400\ntokens = 1000000\n")


def sample(where: Path) -> list[str]:
    where = Path(os.path.abspath(where))
    if where.exists() and any(where.iterdir()):
        raise FileExistsError(f"{where} is not empty")
    repo, layer, answers = where / "repo", where / "layer", where / "replay"
    for made in (repo / "docs", repo / "src", repo / "tests", repo / "drafts", answers):
        made.mkdir(parents=True)
    util.git(["init", "-q", "-b", "main"], repo)
    for rel, text in sample_files().items():
        (repo / rel).write_text(text)
    util.git(["add", "-A"], repo)
    util.git_as_owner(["-c", "commit.gpgsign=false", "commit", "-q", "-m", "first commit"], repo)
    first = util.git(["rev-parse", "HEAD"], repo).strip()
    edits = sample_edits()
    right = {task: implbench.patch_of(repo, {rel: line}) for task, (rel, line) in edits.items()}
    wrong = [implbench.patch_of(repo, {f"drafts/{n}.txt": "wrong"}) for n in ("one", "two", "three", "four")]
    reference = implbench.commit_line(repo, dict(edits.values()), "the four right edits")
    sample_answers(answers, right, wrong)
    sample_layer(layer)
    manifest = where / "workers.toml"
    manifest.write_text(sample_manifest(first, reference))
    util.write_json(where / "prices.json", price_snapshot(False))
    util.write_json(where / "prices-conditional.json", price_snapshot(True))
    return [f"# wrote {repo}, {layer}, {answers}, {manifest}, prices.json and prices-conditional.json",
            (f"# add these lines to {paths.short_path(paths.CONFIG_FILE)}, best in a scratch "
             "ARETE_CONFIG_DIR; import prices.json with `arete prices import`, then run:"),
            f"#   arete benchmark run {manifest}",
            "",
            "[replay]",
            "# no all = true: the arm without credentials is then judged by routing, not replayed",
            f"dir = {json.dumps(str(answers))}",
            "",
            "[[repo]]", f"name = \"{SAMPLE_REPO}\"", f"path = {json.dumps(str(repo))}",
            f"layer = {json.dumps(str(layer))}", "tracker = \"local\"", "jev = false"]
