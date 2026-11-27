from __future__ import annotations

import hashlib
import json
import math
import re
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from functools import wraps
from pathlib import Path

from . import budget_legacy, gh, paths, run_lines, util
from .store import db as foundation
from .store import insights as insights_store
from .store import runs as run_store

IDLE_WAITS = ("limit", "slot")
CHECKPOINT_SECONDS = 30
REPAIRS = {"check_repair", "review_repair", "ci_repair"}
REPAIR_NAMES = ("repair", "fix", "verify_fix", "review_fix", "ci_fix")
RETRY = ("Change the ticket specification, published branch head, or budget settings, then retry with "
         "arete run REPO TICKET --force.")
HEAD_ERROR = "could not confirm the parked branch head; work remains parked"
OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class Parked(BaseException):
    pass


LIMIT_TRANCHES = {"max_run_minutes": 60, "max_verify_minutes": 20, "max_repair_cycles": 2}


def exhausted(reason: str) -> str:
    if reason.startswith("the run chain reached its cumulative "):
        return "max_run_minutes"
    if reason.startswith("verification reached its cumulative "):
        return "max_verify_minutes"
    if reason.startswith("the repair chain used its "):
        return "max_repair_cycles"
    if reason.startswith("controller used its "):
        return "max_decisions"
    if reason.startswith("run used its ") and " action allowance" in reason:
        return "max_actions"
    return ""


@dataclass(frozen=True)
class Policy:
    max_repair_cycles: int = 2
    max_run_minutes: int = 60
    max_verify_minutes: int = 20

    @classmethod
    def of(cls, layer):
        return cls(**{name: getattr(layer, name) for name in cls.__dataclass_fields__})


def artifact(run_id: str, name: str) -> Path:
    return paths.RUNS_DIR / run_id / name


def metadata(run_id: str, name: str) -> dict:
    try:
        value = json.loads(artifact(run_id, name).read_text())
    except FileNotFoundError:
        return {}
    if isinstance(value, dict):
        return value
    raise ValueError(f"run {run_id} has invalid {name}")


def lineage_ids(run_id: str, ids) -> list[str]:
    if not isinstance(ids, list) or any(not isinstance(rid, str) or not rid for rid in ids):
        raise ValueError(f"run {run_id} has invalid budget lineage")
    return ids


def family(run_id: str, *, repairs: bool = False) -> list[str]:
    found, todo, dropped = {}, [run_id] if run_id else [], set()
    while todo:
        rid = todo.pop()
        if rid in found:
            continue
        found[rid] = None
        saved = metadata(rid, "budget.json")
        if saved:
            if saved.get("version") != 1:
                raise ValueError(f"run {rid} has invalid budget lineage")
            parents = lineage_ids(rid, saved.get("parents"))
            left = lineage_ids(rid, saved.get("left_out", []))
            if repairs:
                parents = lineage_ids(rid, saved.get("repair_parents", parents))
                left = lineage_ids(rid, saved.get("repair_left_out", left))
            dropped.update(left)
            todo.extend(parents)
            continue
        for item in run_lines.read(rid):
            if item.get("event") == "repair" and item.get("source"):
                todo.append(str(item["source"]))
            if item.get("event") == "route" and item.get("continues"):
                todo.append(str(item["continues"]))
    return [rid for rid in found if rid not in dropped]


def digest(title, body) -> str:
    return hashlib.sha256(json.dumps([title, body], ensure_ascii=True).encode()).hexdigest()


def specification(ticket) -> str:
    return digest(ticket.title, ticket.body)


def recorded_specification(run_id: str) -> str:
    saved = metadata(run_id, "budget.json").get("specification")
    if isinstance(saved, str) and saved:
        return saved
    for item in reversed(run_lines.read(run_id)):
        data = item.get("data")
        if item.get("step") == "prepare" and isinstance(data, dict) and isinstance(data.get("ticket"), dict):
            return digest(data["ticket"].get("title", ""), data["ticket"].get("body", ""))
    return ""


def checkouts(repo) -> list[tuple[str, Path]]:
    return [(p.dir, repo.path / p.dir) for p in repo.parts] or [("", repo.path)]


def branch_head(src: Path, branch: str) -> str:
    ref = f"refs/heads/{branch}"
    result = util.sh(["git", "ls-remote", "origin", ref], cwd=src, timeout=10, keeps=("ssh-agent",))
    if result.returncode:
        raise ValueError(HEAD_ERROR)
    if not result.stdout:
        return ""
    head = ""
    for line in result.stdout.removesuffix("\n").split("\n"):
        oid, tab, name = line.partition("\t")
        if not tab or not name.startswith("refs/") or not OID.fullmatch(oid):
            raise ValueError(HEAD_ERROR)
        if name == ref:
            if head:
                raise ValueError(HEAD_ERROR)
            head = oid
        elif not name.endswith(f"/{ref}"):
            raise ValueError(HEAD_ERROR)
    return head


def heads(repo, branch: str) -> dict[str, str]:
    if not branch:
        return {}
    return {name: branch_head(src, branch) for name, src in checkouts(repo)}


def latest_park(repo: str, tid: str) -> tuple[dict, dict]:
    rows = run_store.ticket_runs(repo, tid, limit=1, live=True)
    row = rows[0] if rows else {}
    for rid in family(row["id"]) if row else ():
        if saved := metadata(rid, "parked.json"):
            prior = run_store.run_get(rid) or row
            if not prior["dry_run"] and prior["status"] != "dry-run":
                return prior, saved
    return row, {}


def park_reason(row: dict, saved: dict) -> str:
    return f"parked after run {row['id']}: {saved.get('reason', 'its process budget expired')}. {RETRY}"


def park_branch(row: dict, saved: dict) -> str:
    return str(saved.get("branch") or row.get("branch") or "")


def specification_changed(saved: dict, spec: str) -> bool:
    return bool(saved.get("specification")) and saved["specification"] != spec


def inputs_changed(saved: dict, spec: str, policy: dict) -> bool:
    return specification_changed(saved, spec) or saved.get("policy") != policy


def readmission_key(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def effective_origins(repo) -> dict[str, str]:
    parts = checkouts(repo)
    projects = {p.dir: p.github.casefold() for p in repo.parts} if len(parts) > 1 else {}
    bindings = {}
    for name, src in parts:
        result = util.sh(["git", "remote", "get-url", "origin"], cwd=src, timeout=10)
        url = result.stdout.removesuffix("\n")
        if result.returncode or not url or "\n" in url or name in bindings:
            raise ValueError(HEAD_ERROR)
        if projects:
            project = gh.origin_project(url)
            if not project or project != projects[name]:
                raise ValueError(f"could not confirm {name}'s origin matches its configured GitHub project")
            bindings[name] = project
        else:
            bindings[name] = readmission_key([str(src.resolve()), url])
    return bindings


def complete_heads(value, names: set[str]) -> bool:
    return (isinstance(value, dict) and value.keys() == names
            and all(isinstance(oid, str) and (not oid or OID.fullmatch(oid)) for oid in value.values()))


@dataclass(frozen=True)
class Readmission:
    run_id: str
    baseline: str
    inputs: str
    branch: str = ""
    origins: dict[str, str] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)

    @classmethod
    def decode(cls, data: str) -> Readmission | None:
        if not data:
            return None
        try:
            return cls(**json.loads(data))
        except TypeError as error:
            raise ValueError("invalid poll readmission") from error

    def matches(self, repo, row: dict, saved: dict, ticket, policy: Policy) -> bool:
        spec, limits = specification(ticket), asdict(policy)
        if self.baseline != readmission_key(saved) or self.inputs != readmission_key([spec, limits]):
            return False
        if not self.branch:
            return inputs_changed(saved, spec, limits)
        names = {name for name, _ in checkouts(repo)}
        return (self.branch == park_branch(row, saved) and complete_heads(saved.get("heads"), names)
                and complete_heads(self.heads, names) and self.heads != saved["heads"]
                and self.origins == effective_origins(repo))

    def attempted(self, *, record: bool = False) -> bool:
        attempts = metadata(self.run_id, "poll-readmissions.json")
        key = readmission_key(asdict(self))
        if key in attempts:
            return True
        if record:
            util.write_json(artifact(self.run_id, "poll-readmissions.json"), {**attempts, key: True})
        return False


@dataclass(frozen=True)
class Admission:
    reason: str = ""
    reset_time: bool = False
    reset_repairs: bool = False


def authorized_interruption(repo, tid: str, title_and_body) -> bool:
    latest = run_store.newest_live_whole_run(repo.name, tid) or {}
    if latest.get("status") != "interrupted":
        return False
    from . import unblock

    return bool(unblock.inherited(latest, lambda: specification(title_and_body())))


def admission(repo, tid: str, tracker, policy: Policy, explicit: bool, *,
              readmission: Readmission | None = None) -> Admission:
    row, saved = latest_park(repo.name, tid)
    if not saved:
        return Admission("the parked run changed before launch" if readmission else "")
    if not explicit and authorized_interruption(repo, tid, lambda: tracker.get(tid)):
        return Admission()
    if explicit:
        spec = specification(tracker.get(tid)) if saved.get("specification") else ""
        if inputs_changed(saved, spec, asdict(policy)):
            return Admission(reset_time=True, reset_repairs=True)
        if isinstance(saved.get("heads"), dict) and saved["heads"] != heads(repo, park_branch(row, saved)):
            return Admission(reset_time=True, reset_repairs=True)
    if (readmission and readmission.run_id == row["id"]
            and readmission.matches(repo, row, saved, tracker.get(tid), policy)):
        return Admission(reset_time=True, reset_repairs=True)
    return Admission(park_reason(row, saved))


def poll_admission(repo, tid: str, ticket, policy: Policy) -> tuple[str, Readmission | None]:
    row, saved = latest_park(repo.name, tid)
    if not saved or authorized_interruption(repo, tid, lambda: ticket):
        return "", None
    reason = park_reason(row, saved)
    tried = f"{reason} Poll already tried these inputs."
    spec, limits = specification(ticket), asdict(policy)
    proof = Readmission(row["id"], readmission_key(saved), readmission_key([spec, limits]))
    try:
        if inputs_changed(saved, spec, limits):
            if not proof.attempted():
                return "", proof
            reason = tried
        branch = park_branch(row, saved)
        if not branch or not complete_heads(saved.get("heads"), {name for name, _ in checkouts(repo)}):
            return reason, None
        origins = effective_origins(repo)
        if len(origins) > 1:
            published = gh.branch_heads(set(origins.values()), branch)
            current = {name: published[project] for name, project in origins.items()}
        else:
            current = heads(repo, branch)
        if current == saved["heads"]:
            return reason, None
        proof = replace(proof, branch=branch, origins=origins, heads=current)
        if proof.attempted():
            return tried, None
        return "", proof
    except gh.GhError:
        return f"{reason} GitHub could not confirm every parked branch head.", None
    except (OSError, ValueError) as error:
        return f"{reason} {error}", None


def contracted(conn=None) -> bool:
    return foundation.migration_applied(7, conn)


def measured(run_id: str, row: dict) -> bool:
    if row.get("measured_at") is not None:
        return True
    if row.get("kind") == "pipeline" and row.get("status") != "running" and contracted():
        raise ValueError(f"run {run_id} has no measured budget usage")
    return False


def counter(run_id: str, row: dict, field: str) -> float:
    value = row.get(field)
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"run {run_id} has invalid {field}")
    if field == "repair_calls" and type(value) is not int:
        raise ValueError(f"run {run_id} has invalid {field}")
    return value


def repair_count(run_id: str) -> int:
    row = run_store.run_get(run_id) or {}
    if measured(run_id, row):
        return counter(run_id, row, "repair_calls")
    return budget_legacy.repair_count(run_id)


def active_seconds(run_id: str, row: dict) -> float:
    if measured(run_id, row):
        return counter(run_id, row, "active_seconds")
    return budget_legacy.active_seconds(run_id, row)


def verification_seconds(run_id: str, row: dict) -> float:
    if measured(run_id, row):
        return counter(run_id, row, "verify_seconds") + counter(run_id, row, "verify_open_seconds")
    return budget_legacy.verification_seconds(run_id, row)


idle_waits = budget_legacy.idle_waits


class Budget:
    issue_goal = False

    def __init__(self, run, policy: Policy, previous: str, ticket=None, forced: bool = False,
                 *, pending_specification: bool = False, issue_goal: bool = False, repair_previous: str | None = None,
                 answered_runs: tuple[str, ...] = (),
                 wall_deadline: float = math.inf, checkpoint_timeout: float | None = None,
                 checkpoint_gate=None, fixed_gaps: tuple[tuple[str, int], ...] = (), started: float | None = None) -> None:
        recovery = getattr(run, "recovery", {})
        self.issue_goal = issue_goal
        self.base_policy = policy
        self.recovery = recovery.get("allowance", {})
        policy = replace(policy, **{k: getattr(policy, k) + self.recovery[k]
                                    for k in LIMIT_TRANCHES if k in self.recovery})
        self.run, self.policy, self.wall_deadline = run, policy, wall_deadline
        self.pending_specification = pending_specification
        self.checkpoint_timeout, self.checkpoint_gate = checkpoint_timeout, checkpoint_gate
        chain = family(previous)
        repair_chain = family(previous if repair_previous is None else repair_previous, repairs=True)
        rows = {rid: run_store.run_get(rid) for rid in dict.fromkeys(chain + repair_chain)}
        if any(row is None or (row["repo"], str(row["issue"])) != (run.repo.name, str(run.tid))
               for row in rows.values()):
            raise ValueError("the repair budget chain names a different or missing ticket run")
        current = specification(ticket) if ticket else None
        if not run.dry_run and (run.environment_release or run.preview_of):
            live = {rid for rid, row in rows.items() if not row["dry_run"] and row["status"] != "dry-run"}
            chain = [rid for rid in chain if rid in live]
            repair_chain = [rid for rid in repair_chain if rid in live]
        fresh_feedback = (not recovery and not run.dry_run and not run.alone and run.path == "orchestrate"
                          and insights_store.has_fresh_feedback(run.feedback))
        excluded = set(rows) if fresh_feedback else set()
        if not (run.dry_run or fresh_feedback) and forced and current:
            excluded = {rid for rid in rows if (found := recorded_specification(rid)) and found != current}
        left = [rid for rid in chain if rid in excluded]
        self.answer_omitted = repair_chain if not run.dry_run and set(answered_runs).intersection(repair_chain) else []
        self.repair_left_out = self.answer_omitted or [rid for rid in repair_chain if rid in excluded]
        self.repair_parents = [rid for rid in repair_chain if rid not in self.repair_left_out]
        left_repairs = sum(repair_count(rid) for rid in self.repair_left_out if rid in excluded)
        left_seconds = sum(active_seconds(rid, rows[rid]) for rid in left)
        left_verify = sum(verification_seconds(rid, rows[rid]) for rid in left)
        self.left_out = left
        self.parents = [rid for rid in chain if rid not in left]
        kept = [rows[rid] for rid in self.parents]
        self.prior_seconds = sum(active_seconds(row["id"], row) for row in kept)
        self.earlier_idle = dict.fromkeys(IDLE_WAITS, 0.0)
        for row in kept:
            for cause, seconds in idle_waits(row["id"], row).items():
                self.earlier_idle[cause] += seconds
        own_row = run_store.run_get(run.id)
        self.idle = idle_waits(run.id, own_row)
        self.idle_since = None
        self.idle_cause = ""
        self.idle_depth = 0
        self.idled = 0.0
        self.lock = threading.RLock()
        self.watched = False
        self.verify_seconds = sum(verification_seconds(row["id"], row) for row in kept)
        self.prior_repairs = sum(repair_count(rid) for rid in self.repair_parents)
        self.repair_calls = repair_count(run.id)
        self.current_verification_seconds = verification_seconds(run.id, own_row)
        self.verify_seconds += self.current_verification_seconds
        self.verification_active_since = None
        self.verify_started = None
        self.started = time.monotonic() - active_seconds(run.id, own_row) if started is None else started
        inherited_seconds = 0 if self.pending_specification else self.prior_seconds
        self.deadline = math.inf if issue_goal else self.started + policy.max_run_minutes * 60 - inherited_seconds
        self.verify_deadline = None
        self.specification = current or next(
            (found for rid in self.parents if (found := recorded_specification(rid))), None)
        self.charged = {row["id"]: {
            "run_minutes": round(active_seconds(rid, row) / 60, 1) if rid in self.parents else 0,
            "verification_minutes": round(verification_seconds(rid, row) / 60, 1) if rid in self.parents else 0,
            "repair_calls": repair_count(rid) if rid in self.repair_parents else 0,
            "estimate": row.get("measured_at") == ""
        } for rid, row in rows.items() if rid in self.parents or rid in self.repair_parents}
        if self.repair_parents and run.environment_release and fixed_gaps and not run.dry_run:
            reset = run_store.claim_environment_budget_resets(run.repo.name, str(run.tid), run.id, fixed_gaps)
            if reset:
                gaps = ', '.join(f'#{number}' for number in reset)
                fixed = f"gap {gaps} was" if len(reset) == 1 else f"gaps {gaps} were"
                run.log(f"budget: left out {util.count(self.prior_repairs, 'earlier repair cycle')} of "
                        f"{util.count(len(self.repair_parents), 'earlier run')} because the environment {fixed} fixed since they ran")
                self.prior_repairs = 0
                for usage in self.charged.values():
                    usage["repair_calls"] = 0
        self.left_out_message = ""
        if left or (fresh_feedback and self.repair_left_out):
            reason = "the owner commented on the PR" if fresh_feedback else "the ticket specification changed"
            omitted_runs = len(set(left) | set(self.repair_left_out)) if fresh_feedback else len(left)
            self.left_out_message = (
                f"budget: left out {util.count(left_repairs, 'earlier repair cycle')}, "
                f"{left_seconds / 60:.1f} run minutes and {left_verify / 60:.1f} verification minutes of "
                f"{util.count(omitted_runs, 'earlier run')} because {reason} since they ran")
        self.persist()
        if not self.pending_specification:
            self.log_chain()

    def log_chain(self) -> None:
        omitted = ({"repair_left_out": self.answer_omitted, "reason": "owner answer"} if self.answer_omitted else {})
        run_lines.write(self.run.id, "budget_chain", charged=self.charged, **omitted)
        self.run.log("budget chain: " + ("; ".join(
            f"{rid}: {v['run_minutes']:.1f} run minutes, {v['verification_minutes']:.1f} verification minutes, "
            f"{v['repair_calls']} repair calls" + (" (estimate)" if v["estimate"] else "")
            for rid, v in self.charged.items()) or "no parents") +
            (f"; left out of repair accounting because of an owner answer: {', '.join(self.answer_omitted)}"
             if self.answer_omitted else ""))
        if self.left_out_message:
            self.run.log(self.left_out_message)

    def persist(self) -> None:
        self.checkpoint()

    def run_seconds(self) -> float:
        with self.lock:
            now = time.monotonic()
            opened = now - self.idle_since if self.idle_since is not None else 0
            return max(0, now - self.started - self.idled - opened)

    def checkpoint(self) -> None:
        with self.lock:
            if self.checkpoint_gate is not None and not self.checkpoint_gate():
                return
            now, at = time.monotonic(), util.iso()
            opened = now - self.idle_since if self.idle_since is not None else 0
            active = max(0, now - self.started - self.idled - opened)
            verify_open = max(0, now - self.verify_started) if self.verify_started is not None else 0
            saved = {
                "version": 1, "parents": self.parents, "repair_parents": self.repair_parents,
                "policy": asdict(self.base_policy),
                "specification": self.specification, **self.left_out_waits(now)}
            if self.recovery:
                saved.update(recovery=self.recovery, effective_policy=asdict(self.policy))
            if not self.run.dry_run:
                saved.update(left_out=self.left_out, repair_left_out=self.repair_left_out)
            compatibility = getattr(self, "compatibility", True)
            try:
                with foundation.tx(timeout=self.checkpoint_timeout) as conn:
                    compatibility = not contracted(conn)
                    self.compatibility = compatibility
                    run_store.checkpoint(self.run.id, active, self.current_verification_seconds,
                                         verify_open, self.repair_calls, at)
            except sqlite3.Error as e:
                self.run.log(f"budget checkpoint failed: {e}")
            if compatibility:
                saved.update(run_seconds=active, run_seconds_at=at, repair_calls=self.repair_calls,
                             verification_seconds=self.current_verification_seconds,
                             verification_open_seconds=verify_open,
                             verification_active_since=self.verification_active_since)
            util.write_json(artifact(self.run.id, "budget.json"), saved)

    def left_out_waits(self, now: float | None = None) -> dict:
        current = dict(self.idle)
        if self.idle_since is not None:
            current[self.idle_cause] += (time.monotonic() if now is None else now) - self.idle_since
        return {"wait_minutes_left_out": {cause: round(current[cause] / 60, 2) for cause in IDLE_WAITS},
                "earlier_wait_minutes_left_out": {cause: round(self.earlier_idle[cause] / 60, 2)
                                                  for cause in IDLE_WAITS}}

    def run_deadline(self) -> float:
        return min(self.wall_deadline, math.inf if self.idle_since is not None else self.deadline)

    def capture(self, ticket) -> None:
        self.specification = specification(ticket)
        self.persist()

    def reason(self) -> str:
        if self.verify_deadline is not None and self.verify_deadline <= self.run_deadline():
            return f"verification reached its cumulative {self.policy.max_verify_minutes:g} minute budget"
        if self.run_deadline() == self.wall_deadline and not math.isinf(self.wall_deadline):
            return "the one-shot pilot authorization expired"
        return f"the run chain reached its cumulative {self.policy.max_run_minutes:g} minute budget"

    def arm(self) -> None:
        if not self.watched:
            return
        deadline = min(self.run_deadline(), math.inf if self.verify_deadline is None else self.verify_deadline)
        if math.isinf(deadline):
            signal.setitimer(signal.ITIMER_REAL, 0)
            return
        signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - time.monotonic()))

    @contextmanager
    def watching(self, *, admit=None, work_admitted=None, periodic: bool = True):
        def expire(*_):
            raise Parked(self.reason())

        if admit:
            admit()
        if (not self.pending_specification and self.run.path == "orchestrate"
                and self.run.feedback and not self.run.dry_run):
            self.check_repairs()
        if self.run_deadline() <= time.monotonic():
            raise Parked(self.reason())
        previous = signal.signal(signal.SIGALRM, expire)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        self.watched = True
        stopped = threading.Event()

        def checkpointing():
            while not stopped.wait(CHECKPOINT_SECONDS):
                if work_admitted is None or work_admitted():
                    self.checkpoint()

        worker = threading.Thread(target=checkpointing, name=f"budget-{self.run.id}", daemon=True) if periodic else None
        try:
            if admit:
                admit()
            if worker is not None:
                worker.start()
            self.arm()
            yield
        finally:
            self.watched = False
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous)
            stopped.set()
            if worker is not None and worker.ident is not None:
                worker.join()
            if work_admitted is None or work_admitted():
                self.checkpoint()

    @contextmanager
    def idling(self, cause: str):
        if cause not in IDLE_WAITS:
            yield
            return
        with self.lock:
            self.idle_depth += 1
            if self.idle_depth == 1:
                self.idle_since, self.idle_cause = time.monotonic(), cause
                self.arm()
        try:
            yield
        finally:
            with self.lock:
                self.idle_depth -= 1
                if self.idle_depth == 0:
                    spent = time.monotonic() - self.idle_since
                    self.idled += spent
                    self.idle[self.idle_cause] += spent
                    self.idle_since = None
                    self.deadline += spent
                    self.persist()
                    if self.watched:
                        signal.setitimer(signal.ITIMER_REAL, 0)
                        due = min(self.deadline, math.inf if self.verify_deadline is None else self.verify_deadline)
                        if due > time.monotonic():
                            self.arm()

    @contextmanager
    def verifying(self):
        remaining = math.inf if self.issue_goal else self.policy.max_verify_minutes * 60 - self.verify_seconds
        if remaining <= 0:
            raise Parked(f"verification reached its cumulative {self.policy.max_verify_minutes:g} minute budget")
        with self.lock:
            self.verify_started = time.monotonic()
            self.verify_deadline = self.verify_started + remaining
            self.verification_active_since = util.iso()
            self.persist()
        run_lines.write(self.run.id, "budget_verify_start")
        self.arm()
        try:
            yield
        finally:
            with self.lock:
                spent = time.monotonic() - self.verify_started
                self.verify_seconds += spent
                self.current_verification_seconds += spent
                self.verify_started = None
                self.verification_active_since = None
                self.persist()
            run_lines.write(self.run.id, "budget_verify_end", seconds=spent)
            self.verify_deadline = None
            signal.setitimer(signal.ITIMER_REAL, 0)
            if self.run_deadline() > time.monotonic():
                self.arm()

    def repair_limit_reason(self) -> str:
        if not self.issue_goal and self.prior_repairs + self.repair_calls >= self.policy.max_repair_cycles:
            return f"the repair chain used its {self.policy.max_repair_cycles} repair cycles"
        return ""

    def charge(self, step: str, role: str) -> None:
        if time.monotonic() >= self.run_deadline() or (self.verify_deadline is not None
                                               and time.monotonic() >= self.verify_deadline):
            raise Parked(self.reason())
        if role not in REPAIRS and not step.startswith(REPAIR_NAMES):
            self.checkpoint()
            return
        self.check_repairs()
        self.repair_calls += 1
        self.persist()
        run_lines.write(self.run.id, "budget_repair", call=step, atom=role, used=self.prior_repairs + self.repair_calls)

    def check_repairs(self) -> int:
        if reason := self.repair_limit_reason():
            raise Parked(reason)
        return self.prior_repairs + self.repair_calls

    def park(self, reason: str, kept: dict | None = None) -> dict:
        row = run_store.run_get(self.run.id) or {}
        branch = str(row.get("branch") or next((r.get("branch") for rid in self.parents
                     if (r := run_store.run_get(rid)) and r.get("branch")), ""))
        try:
            published = heads(self.run.repo, branch)
        except (OSError, ValueError, TimeoutError):
            published = None
        pr_url = str(row.get("pr_url") or next((r.get("pr_url") for rid in self.parents
                     if (r := run_store.run_get(rid)) and r.get("pr_url")), ""))
        util.write_json(artifact(self.run.id, "parked.json"), {
            "reason": reason, "limit": exhausted(reason), "charged": self.charged,
            "policy": asdict(self.base_policy), "specification": self.specification,
            "heads": published, "branch": branch, "pr_url": pr_url, "kept": kept or {},
            "workspace": str(self.run.workspace[0]) if self.run.workspace else "", "retry": RETRY,
            **self.left_out_waits()})
        run_lines.write(self.run.id, "parked", reason=reason, retry=RETRY)
        return {"branch": branch, "pr_url": pr_url}


def verification(function):
    @wraps(function)
    def wrapped(build, *args, **kw):
        if build.run.budget is None:
            return function(build, *args, **kw)
        with build.run.budget.verifying():
            return function(build, *args, **kw)
    return wrapped
