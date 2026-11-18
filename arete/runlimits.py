from __future__ import annotations

import hashlib
import json
import math
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path

from . import budget_legacy, paths, run_lines, util
from .store import db as foundation
from .store import runs as run_store

IDLE_WAITS = ("limit", "slot")
CHECKPOINT_SECONDS = 30
REPAIRS = {"check_repair", "review_repair", "ci_repair"}
REPAIR_NAMES = ("repair", "fix", "verify_fix", "review_fix", "ci_fix")
RETRY = ("Change the ticket specification, published branch head, or budget settings, then retry with "
         "arete run REPO TICKET --force.")


class Parked(BaseException):
    pass


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


def family(run_id: str) -> list[str]:
    found, todo, dropped = [], [run_id] if run_id else [], set()
    while todo:
        rid = todo.pop()
        if rid in found:
            continue
        if len(found) >= 1000:
            raise ValueError("the repair chain exceeds 1000 runs")
        found.append(rid)
        saved = metadata(rid, "budget.json")
        if saved:
            if saved.get("version") != 1 or not isinstance(saved.get("parents"), list) or any(
                    not isinstance(parent, str) or not parent for parent in saved["parents"]):
                raise ValueError(f"run {rid} has invalid budget lineage")
            if "left_out" in saved:
                if not isinstance(saved["left_out"], list) or any(
                        not isinstance(left, str) or not left for left in saved["left_out"]):
                    raise ValueError(f"run {rid} has invalid budget lineage")
                dropped.update(saved["left_out"])
            todo.extend(saved.get("parents", []))
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


def heads(repo, branch: str) -> dict[str, str]:
    if not branch:
        return {}
    found = {}
    parts = [(p.dir, repo.path / p.dir) for p in repo.parts] or [("", repo.path)]
    for name, src in parts:
        result = util.sh(["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=src, timeout=10,
                         keeps=("ssh-agent",))
        if result.returncode:
            raise ValueError("could not confirm the parked branch head; work remains parked")
        row = result.stdout.split()
        found[name] = row[0] if row else ""
    return found


def latest_park(repo: str, tid: str) -> tuple[dict, dict]:
    rows = run_store.ticket_runs(repo, tid, limit=1)
    row = rows[0] if rows else {}
    for rid in family(row["id"]) if row else ():
        if saved := metadata(rid, "parked.json"):
            return run_store.run_get(rid) or row, saved
    return row, {}


def admission(repo, tid: str, tracker, policy: Policy, explicit: bool) -> tuple[str, bool]:
    row, saved = latest_park(repo.name, tid)
    if not saved:
        return "", False
    reason = f"parked after run {row['id']}: {saved.get('reason', 'its process budget expired')}. {RETRY}"
    if not explicit:
        return reason, False
    changed = (bool(saved.get("specification")) and saved["specification"] != specification(tracker.get(tid))
               or saved.get("policy") != asdict(policy))
    if not changed and isinstance(saved.get("heads"), dict):
        changed = saved.get("heads") != heads(repo, str(saved.get("branch") or row.get("branch") or ""))
    return ("", True) if changed else (reason, False)


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
    def __init__(self, run, policy: Policy, previous: str, ticket=None, forced: bool = False,
                 *, wall_deadline: float = math.inf) -> None:
        self.run, self.policy, self.wall_deadline = run, policy, wall_deadline
        chain = family(previous)
        rows = {rid: run_store.run_get(rid) for rid in chain}
        if any(row is None or (row["repo"], str(row["issue"])) != (run.repo.name, str(run.tid))
               for row in rows.values()):
            raise ValueError("the repair budget chain names a different or missing ticket run")
        current = specification(ticket) if ticket else None
        left = [rid for rid in chain
                if forced and current and (found := recorded_specification(rid)) and found != current]
        left_repairs = sum(repair_count(rid) for rid in left)
        left_seconds = sum(active_seconds(rid, rows[rid]) for rid in left)
        left_verify = sum(verification_seconds(rid, rows[rid]) for rid in left)
        self.left_out = left
        self.parents = [rid for rid in chain if rid not in left]
        kept = [rows[rid] for rid in self.parents]
        self.prior_seconds = sum(active_seconds(row["id"], row) for row in kept)
        self.earlier_idle = {cause: 0.0 for cause in IDLE_WAITS}
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
        self.prior_repairs = sum(repair_count(rid) for rid in self.parents)
        self.repair_calls = repair_count(run.id)
        self.current_verification_seconds = verification_seconds(run.id, own_row)
        self.verify_seconds += self.current_verification_seconds
        self.verification_active_since = None
        self.verify_started = None
        self.started = time.monotonic() - active_seconds(run.id, own_row)
        self.deadline = self.started + policy.max_run_minutes * 60 - self.prior_seconds
        self.verify_deadline = None
        self.specification = current or next(
            (found for rid in self.parents if (found := recorded_specification(rid))), None)
        self.charged = {row["id"]: {
            "run_minutes": round(active_seconds(row["id"], row) / 60, 1),
            "verification_minutes": round(verification_seconds(row["id"], row) / 60, 1),
            "repair_calls": repair_count(row["id"]), "estimate": row.get("measured_at") == ""
        } for row in kept}
        run_lines.write(run.id, "budget_chain", charged=self.charged)
        run.log("budget chain: " + ("; ".join(
            f"{rid}: {v['run_minutes']:.1f} run minutes, {v['verification_minutes']:.1f} verification minutes, "
            f"{v['repair_calls']} repair calls" + (" (estimate)" if v["estimate"] else "")
            for rid, v in self.charged.items()) or "no parents"))
        self.persist()
        if left:
            run.log(f"budget: left out {util.count(left_repairs, 'earlier repair cycle')}, "
                    f"{left_seconds / 60:.1f} run minutes and {left_verify / 60:.1f} verification minutes of "
                    f"{util.count(len(left), 'earlier run')} because the ticket specification changed since they ran")

    def persist(self) -> None:
        self.checkpoint()

    def run_seconds(self) -> float:
        with self.lock:
            now = time.monotonic()
            opened = now - self.idle_since if self.idle_since is not None else 0
            return max(0, now - self.started - self.idled - opened)

    def checkpoint(self) -> None:
        with self.lock:
            now, at = time.monotonic(), util.iso()
            opened = now - self.idle_since if self.idle_since is not None else 0
            active = max(0, now - self.started - self.idled - opened)
            verify_open = max(0, now - self.verify_started) if self.verify_started is not None else 0
            saved = {
                "version": 1, "parents": self.parents, "left_out": self.left_out, "policy": asdict(self.policy),
                "specification": self.specification, **self.left_out_waits(now)}
            compatibility = getattr(self, "compatibility", True)
            try:
                with foundation.tx() as conn:
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
    def watching(self):
        def expire(*_):
            raise Parked(self.reason())

        if self.run_deadline() <= time.monotonic():
            raise Parked(self.reason())
        previous = signal.signal(signal.SIGALRM, expire)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        self.watched = True
        stopped = threading.Event()

        def checkpointing():
            while not stopped.wait(CHECKPOINT_SECONDS):
                self.checkpoint()

        worker = threading.Thread(target=checkpointing, name=f"budget-{self.run.id}", daemon=True)
        try:
            worker.start()
            self.arm()
            yield
        finally:
            self.watched = False
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous)
            stopped.set()
            if worker.ident is not None:
                worker.join()
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
        remaining = self.policy.max_verify_minutes * 60 - self.verify_seconds
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

    def charge(self, step: str, role: str) -> None:
        if time.monotonic() >= self.run_deadline() or (self.verify_deadline is not None
                                               and time.monotonic() >= self.verify_deadline):
            raise Parked(self.reason())
        if role not in REPAIRS and not step.startswith(REPAIR_NAMES):
            self.checkpoint()
            return
        count = self.prior_repairs + self.repair_calls
        if count >= self.policy.max_repair_cycles:
            raise Parked(f"the repair chain used its {self.policy.max_repair_cycles} repair cycles")
        self.repair_calls += 1
        self.persist()
        run_lines.write(self.run.id, "budget_repair", call=step, atom=role, used=count + 1)

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
            "reason": reason, "charged": self.charged, "policy": asdict(self.policy), "specification": self.specification,
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
