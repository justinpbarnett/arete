from __future__ import annotations

import json
import os
import re
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from . import alerts, codebase, config, launch_lease, loops, paths, pilot, principals, scratch, steps, util, workspace
from .store import db, orchestration, runs, tasks
from .store import zte as store
from .zte_github import GitHub, Identity

TASK = re.compile(r"t-[0-9a-f]{40}\Z")
CANONICAL = ("justinpbarnett/arete", 1446501487, "R_kgDOVjfcbw", 42453910)
REHEARSAL = ("justinpbarnett/arete-zte-rehearsal-20261118-2f303b9", 1463520326, "R_kgDOVzuMRg", 42453910)
POLICY_FILE = paths.ZTE_AUTHORITY / "zte.toml"
PAUSE_FILE = paths.ZTE_STOP
INTERLOCK = "zte-interlock"
DELIVERY_LOCK = "zte-delivery"
INTERVAL = 60
MAX_SECONDS = 40 * 60


def reserved(tid: str) -> bool:
    return bool(TASK.fullmatch(tid) and store.find(tid))


def private_bytes(path: Path, limit: int) -> bytes:
    with paths.directory_fd(path.parent) as parent:
        launch_lease.private_directory(parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            pilot.require(launch_lease.plain(st) and st.st_size <= limit, f"unsafe private file: {path.name}")
            data = stream.read(limit + 1)
    pilot.require(len(data) <= limit, f"oversized private file: {path.name}")
    return data


@dataclass(frozen=True)
class Policy:
    identity: Identity
    before: str
    after: str
    enabled: bool
    accept_target_race: bool
    github_audit: str
    rehearsal: bool

    @property
    def fingerprint(self) -> str:
        return orchestration.digest(asdict(self))

    def client(self, deadline: str) -> GitHub:
        return GitHub(self.identity, deadline, paths.ZTE_AUTHORITY / "delivery.pem")

    def signal(self, base: str) -> pilot.Signal:
        return pilot.Signal(self.identity.name, base, self.before, self.after)

    def audit(self, client: GitHub) -> None:
        pilot.require(orchestration.digest(client.snapshot()) == self.github_audit,
                      "GitHub's visible writer or protection baseline changed")


def load_policy() -> Policy | None:
    try:
        raw = tomllib.loads(private_bytes(POLICY_FILE, 8192).decode())
    except FileNotFoundError:
        return None
    try:
        identity = Identity(**raw.pop("identity"))
        policy = Policy(identity, **raw)
    except (KeyError, TypeError) as error:
        raise pilot.Held("invalid ZTE policy or identity fields") from error
    pilot.require(type(policy.rehearsal) is bool and type(policy.enabled) is bool
                  and (identity.name, identity.number, identity.node, identity.owner) == (
                      REHEARSAL if policy.rehearsal else CANONICAL)
                  and policy.accept_target_race is True and identity.bot != identity.owner
                  and all(type(v) is int and v > 0 for v in (identity.bot, identity.workflow, identity.app,
                                                            identity.installation))
                  and all(isinstance(line, str) and line.startswith("## ") and 3 < len(line) <= 160 and line.isprintable()
                          for line in (policy.before, policy.after)) and policy.before != policy.after
                  and re.fullmatch(r"[a-f0-9]{64}", policy.github_audit) and pilot.SHA.fullmatch(identity.workflow_blob),
                  "invalid ZTE standing policy")
    return policy


def settings(cfg: config.Config, policy: Policy) -> tuple[config.Repo, config.RepoLayer, str]:
    from .orchestration import policy_fingerprint

    repo = cfg.repo(cfg.arete_repo)
    layer = config.load_layer(repo.layer_dir)
    control = steps.resolve(cfg, layer, "orchestrate", "fix")
    check = steps.resolve(cfg, layer, "verify", "fix")
    limits = layer.orchestration
    target = policy.identity.name
    pilot.require(repo.github == target and repo.pipeline and len(repo.parts) == 1 and not repo.parts[0].dir
                  and repo.parts[0].github == target and repo.layer and layer.base_branch in ("", "main")
                  and layer.trust_level == "pr" and layer.factory_path == "orchestrated" and not cfg.replay.all
                  and layer.verify and not layer.verify.start and not layer.verify.probes
                  and set(layer.checks) >= {"lint", "compile", "imports", "web", "behavior"}
                  and layer.checks["lint"] == "sh tools/lint-arete"
                  and (layer.payment_modes or cfg.payment_modes) == ("subscription",)
                  and (control.runner, control.model, control.effort) == ("pi", "openai/gpt-6.2-sol", "low")
                  and check.runner == "pi" and not control.fallback and not check.fallback
                  and cfg.jev.model == "jev-latest" and cfg.jev_allowed([repo])
                  and not steps.experiment_for(cfg, layer, "orchestrate", "fix")
                  and limits.max_workers == 1 and 0 < limits.max_decisions <= 16 and 0 < limits.max_actions <= 64
                  and 0 < layer.max_run_minutes <= 40 and 0 < layer.max_verify_minutes <= 15
                  and 0 <= layer.max_repair_cycles <= 1, "ZTE requires the bounded subscription-only PR layer")
    return repo, layer, policy_fingerprint(cfg, layer, ())


def active(delivery: store.Delivery, *, engine: str = "") -> Policy:
    policy = load_policy()
    pilot.require(policy and policy.enabled and policy.fingerprint == delivery.policy and not PAUSE_FILE.exists(),
                  "ZTE policy was disabled, changed or stopped")
    pilot.require(util.now() < util.parse_iso(delivery.deadline) and (engine or codebase.STAMP) == delivery.data["engine"],
                  "ZTE deadline expired or its engine changed")
    return policy


def task_version(delivery: store.Delivery, signal: pilot.Signal, *states: str) -> int:
    task = tasks.task_get(delivery.task)
    pilot.require(task and task["repo"] == delivery.repo and task["title"] == pilot.TASK_TITLE
                  and task["kind"] == pilot.TASK_KIND and task["body"] == signal.task() and not task["parent"]
                  and task["state"] in states
                  and all(note["author"] == "arete" for note in tasks.task_comments(delivery.task)),
                  "the reserved ZTE task changed")
    return task["version"]


def pickable(tid: str, repo: str) -> bool:
    if not reserved(tid):
        return True
    try:
        delivery = store.find(tid)
        return bool(delivery and delivery.repo == repo and delivery.phase == "queued" and active(delivery))
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def scheduled_poll() -> str:
    if principals.current() != "rule:poll":
        return ""
    run_id = os.environ.get("ARETE_RUN_ID", "")
    if not run_id or os.environ.get("ARETE_LOOP") != "poll":
        return ""
    with db.one_read() as connection:
        run = runs.run_get(run_id)
        if not (run and run["kind"] == "loop" and run["loop"] == "poll" and run["trigger"] == "every"
                and run["status"] == "running" and not run["stop_asked"] and run["pid"] == os.getppid()
                and run["engine_sha"] == codebase.STAMP):
            return ""
        actor = connection.execute("SELECT actor FROM events_log WHERE kind = 'run.started' AND subject = ?",
                                   (f"run:{run_id}",)).fetchone()
    expected = loops.BUNDLED_LOOPS_DIR / "poll"
    return run_id if actor and actor[0] == "rule:poll" and os.environ.get("ARETE_LOOP_DIR") == str(expected) else ""


def bind_launch(repo: str, tid: str, lease: launch_lease.Lease) -> None:
    if not reserved(tid):
        return
    delivery = store.find(tid)
    poll_run = scheduled_poll()
    pilot.require(poll_run and delivery and delivery.repo == repo and delivery.phase == "queued",
                  "ZTE task has no scheduled launch grant")
    policy = active(delivery)
    pilot.require(task_version(delivery, policy.signal(delivery.base), "todo") == delivery.data["task_version"],
                  "ZTE task was altered before dispatch")
    store.transition(delivery, "dispatching", dispatch=orchestration.digest(lease.message), scheduled_poll=poll_run)


def admit_run(cfg: config.Config, repo: config.Repo, tid: str) -> store.Delivery:
    delivery = store.find(tid)
    pilot.require(delivery and delivery.repo == repo.name and delivery.phase == "dispatching"
                  and principals.current() == "rule:poll"
                  and orchestration.digest(os.environ.get(launch_lease.ENV, "")) == delivery.data["dispatch"]
                  and not runs.ticket_runs(repo.name, tid, limit=1), "ZTE requires its reserved scheduled launch")
    launch_lease.verify(repo.name, tid)
    policy = active(delivery)
    current, _, digest = settings(cfg, policy)
    pilot.require(current == repo and digest == delivery.data["config"], "the admitted ZTE config changed")
    pilot.require(task_version(delivery, policy.signal(delivery.base), "todo") == delivery.data["task_version"],
                  "the reserved task was edited before ZTE launch")
    return delivery


def require_launch_base(delivery: store.Delivery) -> None:
    policy = active(delivery)
    pilot.require(policy.client(delivery.deadline).tip() == delivery.base, "protected main moved before ZTE launch")


def for_run(repo: config.Repo, tid: str, run_id: str, *phases: str) -> tuple[store.Delivery, Policy]:
    delivery = store.find(tid)
    pilot.require(delivery and (delivery.repo, delivery.data.get("run")) == (repo.name, run_id)
                  and delivery.phase in phases, "the ZTE run lost its delivery grant")
    policy = active(delivery)
    current, _, digest = settings(config.load(), policy)
    pilot.require(current == repo and delivery.data["config"] == digest, "the admitted ZTE config changed")
    run = pilot.scheduled_run(repo.name, tid)
    pilot.require(run and run["id"] == run_id, "the ZTE run is not the original scheduled attempt")
    task_version(delivery, policy.signal(delivery.base), "running", "pr")
    return delivery, policy


def check_checkout(delivery: store.Delivery, policy: Policy, repo: config.Repo, checkouts) -> workspace.Checkout:
    pilot.require(len(checkouts) == 1, "ZTE requires exactly one checkout")
    checkout = checkouts[0]
    pilot.require(checkout.github == policy.identity.name and checkout.base == "main" and not checkout.dir
                  and not any((checkout.pr, checkout.lease, checkout.resumes, checkout.merged_base))
                  and checkout.src.resolve() == repo.path.resolve(), "ZTE checkout does not match the approved origin")
    pilot.check_origin(checkout.wt, policy.identity.name)
    return checkout


def prepare_candidate(run, job, evidence: dict) -> store.Delivery:
    delivery, policy = for_run(run.repo, str(run.tid), run.id, "running")
    signal = policy.signal(delivery.base)
    checkout = check_checkout(delivery, policy, run.repo, job.checkouts)
    branch = job.branch
    where = checkout.wt
    pilot.require(re.fullmatch(rf"arete/{re.escape(delivery.task)}-[a-z0-9-]+", branch)
                  and pilot.git(where, "symbolic-ref", "--short", "HEAD").strip() == branch
                  and pilot.git(where, "rev-parse", "HEAD").strip() == signal.base
                  and not job.left_for_a_person and not job.held_reason and not scratch.pending(where),
                  "ZTE cannot publish a resumed, held or active checkout")
    expected = signal.expected(pilot.readme(where, signal.base))
    job.b.restage(from_base=False)
    tree = pilot.git(where, "write-tree").strip()
    pilot.require_exact_tree(where, signal, tree, expected)
    pilot.require_evidence(evidence, checkout.github, tree, job.layer.checks)
    pilot.require(evidence.get("policy_sha") == delivery.data["config"], "the candidate evidence has a different policy")
    util.git_as_owner(["-c", "core.hooksPath=/dev/null", "commit", "-qm",
                       f"{pilot.TASK_TITLE}\n\nArete-Run: {run.id}"], where)
    head = pilot.git(where, "rev-parse", "HEAD").strip()
    pilot.require(pilot.exact_edit(where, signal, head, expected) == tree and not pilot.git(
        where, "status", "--porcelain", "--untracked-files=all"), "the committed README candidate drifted")
    with util.lock(INTERLOCK, wait=True):
        current, _ = for_run(run.repo, delivery.task, run.id, "running")
        pilot.require(current == delivery, "the ZTE delivery grant changed while preparing the candidate")
        run.enter("pr")
        version = task_version(delivery, signal, "pr")
        return store.transition(delivery, "prepared", head=head, tree=tree, branch=branch, worktree=str(where),
                                pr_task_version=version)


def observation(policy: Policy, repo: config.Repo, client: GitHub) -> tuple[str, str]:
    policy.audit(client)
    pilot.check_origin(repo.path, policy.identity.name)
    base = client.tip()
    lines = client.document(base).splitlines()
    if lines.count(policy.before) == 1 and policy.after not in lines:
        return "eligible", base
    if policy.before not in lines and lines.count(policy.after) == 1:
        return "conforming", base
    return "no match", base


def hold(delivery: store.Delivery, error: Exception, *, uncertain: bool = False, **evidence) -> None:
    reason = util.first_line(str(error), 260) or type(error).__name__
    try:
        row = store.transition(delivery, "uncertain" if uncertain else "held", reason=reason, stopped_at=delivery.phase,
                               **evidence)
    except store.Conflict:
        return
    alerts.notify("Arete ZTE held", f"{row.task}: {reason}")


def finder(cfg: config.Config) -> None:
    if not POLICY_FILE.exists() or PAUSE_FILE.exists():
        return
    policy = load_policy()
    if not policy or not policy.enabled:
        return
    with util.lock(DELIVERY_LOCK) as got:
        if not got:
            return
        previous = store.latest()
        if previous and previous.phase != "healthy":
            return
        repo, _, digest = settings(cfg, policy)
        deadline = util.iso(util.now() + timedelta(seconds=MAX_SECONDS))
        client = policy.client(deadline)
        status, base = observation(policy, repo, client)
        if status == "no match" or client.tip() != base:
            return
        with util.lock(INTERLOCK) as got:
            if not got or PAUSE_FILE.exists() or load_policy() != policy or settings(config.load(), policy)[2] != digest:
                return
            if status == "conforming" and previous and previous.policy == policy.fingerprint:
                store.note_conforming(previous, base)
            elif status == "eligible":
                from . import zte_release

                install = zte_release.installation()
                profile = install.to_receipt()
                if policy.rehearsal != profile["rehearsal"] or zte_release.selected(install) != base:
                    return
                signal = policy.signal(base)
                with principals.acting("rule:zte-finder"):
                    store.reserve(policy.fingerprint, repo.name, base, deadline, pilot.TASK_TITLE, pilot.TASK_KIND,
                                  signal.task(), {"engine": codebase.STAMP, "config": digest, "task_version": 1,
                                                  "github": asdict(policy.identity), "approved_installation": profile})


def status(args) -> int:
    try:
        policy = load_policy()
        paused = PAUSE_FILE.exists()
        row = store.latest() if paths.DB_FILE.exists() else None
        print(json.dumps({"enabled": bool(policy and policy.enabled), "paused": paused,
                          "delivery": asdict(row) if row else None}, indent=2))
        if args.dry_run and policy and policy.enabled and not paused:
            repo, _, _ = settings(config.load(), policy)
            client = policy.client(util.iso(util.now() + timedelta(seconds=MAX_SECONDS)))
            found, tip = observation(policy, repo, client)
            print(f"README rule: {found} at {tip}; no task or write requested")
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(f"ZTE status unavailable: {util.first_line(str(error), 260)}")
        return 1
    return 0


def pause(_args) -> int:
    if (any(os.environ.get(key) for key in ("ARETE_AGENT", "ARETE_RUN_ID", "ARETE_ACTOR"))
            or principals.owner() == "hand" or principals.current() != principals.owner()):
        print("Only the authenticated local owner may pause ZTE.")
        return 1
    with util.lock(INTERLOCK, wait=True):
        util.write_durably(PAUSE_FILE, json.dumps({"at": util.iso()}).encode(), mode=0o600)
    print("ZTE paused. A write already sent may still land; inspect arete zte status.")
    return 0


def register(sub) -> None:
    parser = sub.add_parser("zte", help="inspect or stop the bounded README delivery lane")
    modes = parser.add_subparsers(required=True)
    show = modes.add_parser("status", help="inspect the lane's durable state")
    show.add_argument("--dry-run", action="store_true", help="read the finding without reserving a task")
    show.set_defaults(fn=status)
    modes.add_parser("pause", help="persist a local Stop interlock").set_defaults(fn=pause)


def wall_deadline(delivery: store.Delivery) -> float:
    return time.monotonic() + max(0, (util.parse_iso(delivery.deadline) - util.now()).total_seconds())
