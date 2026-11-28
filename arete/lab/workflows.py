from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from functools import partial
from pathlib import Path

from .. import agent, codebase, config, factory, flows, paths, pipeline, record, util, workspace
from ..store import db as foundation
from ..store import runs as run_store
from ..store import tasks as task_store
from . import experiment

EXECUTORS = ("fixed", "single-worker", "orchestrated")


def designed(data: dict, carried=None, follow=False):
    if data.get("executor") != "workflow" or "atom" in data or follow:
        raise experiment.Refused("whole-workflow designs use executor = 'workflow', no atom and no optimizer")
    modes, arms, caps = {}, {}, {}
    for name, arm in experiment.section(data, "arms").items():
        if not isinstance(arm, dict) or arm.get("executor") not in EXECUTORS:
            raise experiment.Refused(f"arms.{name}.executor must be one of {', '.join(EXECUTORS)}")
        if "max_builders" in arm:
            if arm["executor"] != "orchestrated":
                raise experiment.Refused(f"arms.{name}.max_builders requires the orchestrated executor")
            try:
                caps[name] = config.orchestration_table({"max_builders": arm["max_builders"]}).max_builders
            except ValueError as error:
                raise experiment.Refused(str(error)) from error
        modes[name], arms[name] = arm["executor"], {k: v for k, v in arm.items() if k not in ("executor", "max_builders")}
    normalized = {key: value for key, value in data.items() if key != "executor"}
    exp = experiment.designed({**normalized, "atom": "build", "arms": arms}, carried)
    if exp.extras:
        raise experiment.Refused("whole-workflow arms use the shared repository context and registered prompts")
    exp.executors = modes
    exp.design.pop("atom")
    exp.design["executor"] = "workflow"
    exp.design["orchestration"] = asdict(exp.layer.orchestration)
    exp.design["workflow_sha"] = fingerprint(exp.cfg, exp.layer)
    for name, mode in modes.items():
        exp.design["arms"][name]["executor"] = mode
        if name in caps:
            exp.design["arms"][name]["max_builders"] = caps[name]
    for role in ("orchestrate", "plan", "verify", "review", "check_repair", "review_repair", "ci_repair"):
        setting = pipeline.steps.resolve(exp.cfg, exp.layer, role)
        exp.design.setdefault("workflow_settings", {})[role] = experiment.effective(exp.cfg, setting)
    exp.digest = experiment.sha_json(exp.design)[:12]
    exp.make_run = lambda designed, arm, given: experiment.TrialRun(**given, executor=modes[arm], max_builders=caps.get(arm))
    return exp


def fingerprint(cfg, layer):
    return experiment.sha_json({"config": json.loads(json.dumps(config.policy_dict(cfg), default=str)),
        "layer": json.loads(json.dumps(config.policy_dict(layer), default=str)),
        "prompts": {path.name: experiment.sha(path.read_bytes()) for path in sorted(paths.PROMPTS_DIR.glob("*.md"))}})


class CandidateBudget:
    def __init__(self, limits, repo, ticket, folder, workers):
        self.limits, self.repo, self.ticket, self.folder = limits, repo, ticket, folder
        self.started, self.calls, self.pending = time.monotonic(), 0, {}
        self.unknown_usd, self.unknown_tokens, self.width = 0.0, 0, workers + 1

    def save(self):
        util.write_durably(self.folder / "admissions.json", json.dumps({
            "calls": self.calls, "pending": self.pending, "unknown_usd": self.unknown_usd,
            "unknown_tokens": self.unknown_tokens}).encode(), 0o600)

    def admit(self, step, role, route):
        prior = self.pending.pop(step, None)
        row = run_store.newest_whole_run(self.repo, self.ticket)
        rows = run_store.steps(row["id"]) if row else []
        seam = factory.inside(row["id"]) if row else None
        receipt = next((r for r in reversed(rows) if r["step"] == (seam.step if seam else step)), None)
        if prior and (not receipt or not (receipt["ok"] or receipt["turns"])):
            self.unknown_usd += prior["max_budget_usd"]
            self.unknown_tokens += prior["max_tokens"]
        paid = run_store.paid_by_run([row["id"]]).get(row["id"], 0) if row else 0
        tokens = sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in rows)
        usd = self.limits["spend_usd"] - paid - self.unknown_usd - sum(r["max_budget_usd"] for r in self.pending.values())
        remaining = self.limits["tokens"] - tokens - self.unknown_tokens - sum(r["max_tokens"] for r in self.pending.values())
        seconds = self.limits["minutes"] * 60 - (time.monotonic() - self.started)
        if self.calls >= self.limits["calls"] or usd <= 0 or remaining < self.width or seconds <= 0:
            raise workspace.RunFailed("whole-workflow trial exhausted its shared launch/resource allowance")
        grant = {"max_tokens": int(remaining // self.width), "max_budget_usd": usd / self.width, "timeout": seconds}
        which = route["runner"]
        if agent.traits_of(which).cannot_gate and role in ("plan", "verify", "review"):
            raise workspace.RunFailed("this structured runner hides a launch; choose a gateable workflow route")
        if agent.traits_of(which).cannot_stop:
            headroom = self.limits.get(experiment.HEADROOM, {})
            if (not headroom or headroom["tokens"] > grant["max_tokens"]
                    or headroom.get("spend_usd", 0) > grant["max_budget_usd"]):
                raise workspace.RunFailed("the selected runner lacks reserved Codex headroom for this trial call")
        self.calls += 1
        self.pending[step] = grant
        self.save()
        return grant

    def done(self, step, known):
        reservation = self.pending.pop(step, None)
        if reservation and not known:
            self.unknown_usd += reservation["max_budget_usd"]
            self.unknown_tokens += reservation["max_tokens"]
        self.save()


def child():
    codebase.load()
    root = Path(sys.argv[1])
    spec = json.loads((root / "request.json").read_text())
    foundation.migrate()
    cfg = config.load()
    repo = cfg.repo(spec["repo"])
    ticket = task_store.task_add(repo.name, spec["prompt"], spec["title"], "feat")
    layer = config.load_layer(repo.layer_dir)
    budget = CandidateBudget(spec["limits"], repo.name, ticket, root, layer.orchestration.max_workers)
    path = {"fixed": "feature", "single-worker": "single_worker", "orchestrated": "orchestrate"}[spec["executor"]]
    drive = pipeline.flow_function(cfg, path)
    if "max_builders" in spec:
        from .. import orchestration

        if path != "orchestrate":
            raise ValueError("builder trial cap requires the orchestrated executor")
        drive = partial(orchestration.orchestrate, max_builders=spec["max_builders"])
    code = pipeline.run_issue(repo.name, ticket, dry_run=True,
                              script=pipeline.Script(path, drive, overrides=spec["overrides"]),
                              admission=budget.admit, launch_done=budget.done,
                              closed_context=tuple(spec["closed_context"]))
    row = run_store.newest_whole_run(repo.name, ticket)
    prepared = flows.prepared_data(row["id"])
    records = [str(path) for path in record.record_files(row["id"])]
    summary = {"run": row, "exit": code, "steps": run_store.steps(row["id"]), "records": records,
               "workspace": prepared.get("checkouts", [{}])[0].get("wt", ""), "launches": budget.calls,
               "unknown_usage": bool(budget.unknown_usd or budget.unknown_tokens or budget.pending),
               "facts": pipeline.seam_facts(row["id"])}
    if spec.get("max_builders"):
        from ..store import orchestration

        detail = orchestration.view(row["id"]) or {}
        summary["workers"] = {"max_builders": spec["max_builders"],
                              "peak_builders": max((a["result"].get("active_builders", 0)
                                                    for a in detail.get("actions", [])), default=0),
                              "conflicts": sum(w.get("conflicts", 0) for w in detail.get("workers", []))}
    util.write_durably(root / "result.json", json.dumps(summary).encode(), 0o600)


def execute(cfg, run, layer, wt, task):
    if run.executor not in EXECUTORS:
        raise workspace.RunFailed("unknown workflow executor")
    root = Path(tempfile.mkdtemp(prefix="workflow-", dir=run.dir))
    runtime = root
    if run.max_builders is not None:
        # The parent experiment state (including grading material) stays closed to every model.
        # Keep the child runtime outside that boundary so its private worktrees can be opened.
        runtime = Path(tempfile.mkdtemp(prefix="workflow-runtime-", dir=paths.STATE_DIR.parent))
    private = runtime / "config"
    private.mkdir(mode=0o700)
    repo_name = "workflow-" + root.name.removeprefix("workflow-")
    origin = root / "origin.git"
    util.git(["clone", "--bare", "-q", str(wt), str(origin)], wt)
    source = root / "repo"
    util.git(["clone", "-q", str(origin), str(source)], wt)
    util.git(["branch", "-f", layer.base_branch, "HEAD"], origin)
    text = paths.CONFIG_FILE.read_text()
    text += (f"\n[[repo]]\nname = {json.dumps(repo_name)}\npath = {json.dumps(str(source))}\n"
             f"layer = {json.dumps(str(layer.dir))}\ntracker = 'local'\npipeline = false\n"
             f"jev = {str(run.repo.jev).lower()}\npublic = {str(run.repo.public).lower()}\n")
    util.write_durably(private / "config.toml", text.encode(), 0o600)
    used = run.used()
    limits = {"calls": run.limits["calls"] - used.calls, "tokens": run.limits["tokens"] - used.tokens,
              "spend_usd": run.limits["spend_usd"] - used.spend,
              "minutes": (run.limits["minutes"] * 60 - used.seconds) / 60,
              **({experiment.HEADROOM: run.limits[experiment.HEADROOM]} if experiment.HEADROOM in run.limits else {})}
    request = {"executor": run.executor, "repo": repo_name, "prompt": task.prompt, "title": task.title,
               "overrides": run.step_overrides, "limits": limits,
               "closed_context": [str(paths.STATE_DIR), str(paths.CONFIG_DIR), str(paths.WORKTREES_DIR),
                                  str(run.repo.path), str(layer.dir / "evals")]}
    if run.max_builders is not None:
        request["runtime"] = str(runtime)
        request["max_builders"] = run.max_builders
    util.write_durably(root / "request.json", json.dumps(request).encode(), 0o600)
    from .. import environment as child_environment

    env = child_environment.build(child_environment.INTERNAL_ARETE, {
        "ARETE_CONFIG_DIR": str(private), "ARETE_STATE_DIR": str(runtime / "state"),
        "ARETE_LOG_DIR": str(runtime / "logs"), "ARETE_WORKTREES_DIR": str(runtime / "worktrees"),
    })
    env.pop("ARETE_RUN_ID", None)
    command = [sys.executable, "-I", "-c", (f"import sys;sys.path.insert(0,{str(paths.PACKAGE_DIR.parent)!r});"
               "from arete.lab.workflows import child;child()"), str(root)]
    run.execution = {"executor": run.executor, "usage_unknown": True, "artifacts": paths.short_path(root)}
    with (root / "execution.log").open("w") as output:
        process = subprocess.Popen(command, env=env, stdout=output, stderr=output, start_new_session=True)
        try:
            process.wait(timeout=max(1, limits["minutes"] * 60))
        except BaseException:
            util.stop_group(process)
            run.stopped_by = "workflow stopped without complete usage; reconcile its retained receipt before continuing"
            raise
    if not (root / "result.json").exists():
        run.stopped_by = "workflow ended without complete usage; reconcile its retained receipt before continuing"
        raise workspace.RunFailed("workflow ended without a complete receipt; costs may be unknown")
    result = json.loads((root / "result.json").read_text())
    imported = []
    for row in result["steps"]:
        value = agent.Result(ok=bool(row["ok"]), **{key: row[key] for key in (
            "runner", "model", "provider", "auth_mode", "cost_usd", "tokens_in", "tokens_out", "cache_read", "cache_write",
            "api_seconds", "turns", "error") if row.get(key) is not None})
        name = "executor-" + row["step"]
        record.step(run.id, name, value, row.get("seconds", 0), effort=row.get("effort", ""), try_no=row["try"])
        record.label_step(run.id, name, row["try"], atom=row.get("atom", ""))
        imported.append(row)
    dest = paths.record_dir(run.id)
    dest.mkdir(parents=True, exist_ok=True)
    for name in result["records"]:
        shutil.copy2(name, dest / ("executor-" + Path(name).name))
    run.cost += sum(row["cost_usd"] or 0 for row in imported)
    run.paid = record.metered_spent(run.id)
    run.execution = {"executor": run.executor, "workflow_run": result["run"]["id"], "launches": result["launches"],
                     "usage_unknown": result["unknown_usage"], "artifacts": paths.short_path(root),
                     "controller_calls": sum(row.get("atom") == "orchestrate" for row in imported),
                     "worker_calls": sum(row["step"].startswith("worker-") for row in imported),
                     "owner_interventions": int(result["run"]["status"] == "needs-info"),
                     "workflow_status": result["run"]["status"], "engine_facts": result["facts"]}
    run.execution.update(result.get("workers", {}))
    if result["unknown_usage"]:
        run.stopped_by = "workflow reported uncertain usage; reconcile before continuing the experiment"
    if result["unknown_usage"] or result["run"]["status"] != "dry-run":
        raise workspace.RunFailed(result["run"]["summary"] or "workflow did not complete its engine gates")
    candidate = Path(result["workspace"])
    if not candidate.is_dir() or candidate == wt or root not in candidate.parents:
        raise workspace.RunFailed("workflow returned no retained owned candidate workspace")
    return candidate, result["facts"]
