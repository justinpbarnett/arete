from __future__ import annotations

import hashlib
import json
import math
import signal
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path

from . import lines, paths, state, util

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
        for item in lines.read(rid):
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
    for item in reversed(lines.read(run_id)):
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
    rows = state.ticket_runs(repo, tid, limit=1)
    row = rows[0] if rows else {}
    for rid in family(row["id"]) if row else ():
        if saved := metadata(rid, "parked.json"):
            return state.run_get(rid) or row, saved
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


def repair_count(run_id: str) -> int:
    read = lines.read(run_id)
    saved = metadata(run_id, "budget.json")
    if "repair_calls" in saved:
        count = saved["repair_calls"]
        if type(count) is not int or count < 0:
            raise ValueError(f"run {run_id} has invalid repair usage")
        return count
    if saved:
        return sum(item.get("event") == "budget_repair" for item in read)
    found = set()
    for item in read:
        name = str(item.get("call") or item.get("step") or "")
        if (item.get("event") == "step_start" and (item.get("atom") in REPAIRS
                or name.startswith(REPAIR_NAMES))):
            found.add((str(item.get("step") or ""), int(item.get("try") or 1)))
    return len(found)


def elapsed(row: dict) -> float:
    return max(0, (util.parse_iso(row.get("finished") or util.iso()) -
                   util.parse_iso(row["started"])).total_seconds())


def verification_seconds(run_id: str, row: dict) -> float:
    read = lines.read(run_id)
    saved = metadata(run_id, "budget.json")
    active, spent = saved.get("verification_active_since"), saved.get("verification_seconds", 0)
    if type(spent) not in (int, float) or not math.isfinite(spent) or spent < 0:
        raise ValueError(f"run {run_id} has invalid verification usage")
    if active is not None and (not isinstance(active, str) or not active):
        raise ValueError(f"run {run_id} has invalid active verification checkpoint")
    if active or spent:
        return max(0, float(spent)) + (max(0, (util.parse_iso(row.get("finished") or util.iso()) -
                                             util.parse_iso(active)).total_seconds()) if active else 0)
    if saved:
        active, spent = None, 0.0
        for item in read:
            if item.get("event") == "budget_verify_start":
                active = item["ts"]
            elif item.get("event") == "budget_verify_end":
                spent += max(0, float(item.get("seconds") or 0))
                active = None
        return spent + (max(0, (util.parse_iso(row.get("finished") or util.iso()) -
                                util.parse_iso(active)).total_seconds()) if active else 0)
    return sum(max(0, float(step.get("seconds") or 0)) for step in state.steps(run_id)
               if str(step.get("step") or "").startswith(("verify", "grade")))


class Budget:
    def __init__(self, run, policy: Policy, previous: str, ticket=None, forced: bool = False) -> None:
        self.run, self.policy = run, policy
        chain = family(previous)
        rows = {rid: state.run_get(rid) for rid in chain}
        if any(row is None or (row["repo"], str(row["issue"])) != (run.repo.name, str(run.tid))
               for row in rows.values()):
            raise ValueError("the repair budget chain names a different or missing ticket run")
        current = specification(ticket) if ticket else None
        left = [rid for rid in chain
                if forced and current and (found := recorded_specification(rid)) and found != current]
        left_repairs = sum(repair_count(rid) for rid in left)
        left_seconds = sum(elapsed(rows[rid]) for rid in left)
        left_verify = sum(verification_seconds(rid, rows[rid]) for rid in left)
        self.left_out = left
        self.parents = [rid for rid in chain if rid not in left]
        kept = [rows[rid] for rid in self.parents]
        self.prior_seconds = sum(elapsed(row) for row in kept)
        self.verify_seconds = sum(verification_seconds(row["id"], row) for row in kept)
        self.prior_repairs = sum(repair_count(rid) for rid in self.parents)
        self.repair_calls = 0
        self.current_verification_seconds = 0.0
        self.verification_active_since = None
        self.started = time.monotonic() - elapsed(state.run_get(run.id))
        self.deadline = self.started + policy.max_run_minutes * 60 - self.prior_seconds
        self.verify_deadline = None
        self.specification = current or next(
            (found for rid in self.parents if (found := recorded_specification(rid))), None)
        self.persist()
        if left:
            run.log(f"budget: left out {util.count(left_repairs, 'earlier repair cycle')}, "
                    f"{left_seconds / 60:.1f} run minutes and {left_verify / 60:.1f} verification minutes of "
                    f"{util.count(len(left), 'earlier run')} because the ticket specification changed since they ran")

    def persist(self) -> None:
        util.write_json(artifact(self.run.id, "budget.json"), {
            "version": 1, "parents": self.parents, "left_out": self.left_out, "policy": asdict(self.policy),
            "specification": self.specification, "repair_calls": self.repair_calls,
            "verification_seconds": self.current_verification_seconds,
            "verification_active_since": self.verification_active_since})

    def capture(self, ticket) -> None:
        self.specification = specification(ticket)
        self.persist()

    def reason(self) -> str:
        if self.verify_deadline is not None and self.verify_deadline <= self.deadline:
            return f"verification reached its cumulative {self.policy.max_verify_minutes:g} minute budget"
        return f"the run chain reached its cumulative {self.policy.max_run_minutes:g} minute budget"

    def arm(self) -> None:
        deadline = min(self.deadline, self.verify_deadline) if self.verify_deadline is not None else self.deadline
        signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - time.monotonic()))

    @contextmanager
    def watching(self):
        def expire(*_):
            raise Parked(self.reason())

        if self.deadline <= time.monotonic():
            raise Parked(self.reason())
        previous = signal.signal(signal.SIGALRM, expire)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        try:
            self.arm()
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous)

    @contextmanager
    def verifying(self):
        remaining = self.policy.max_verify_minutes * 60 - self.verify_seconds
        if remaining <= 0:
            raise Parked(f"verification reached its cumulative {self.policy.max_verify_minutes:g} minute budget")
        started = time.monotonic()
        self.verify_deadline = started + remaining
        self.verification_active_since = util.iso()
        self.persist()
        lines.write(self.run.id, "budget_verify_start")
        self.arm()
        try:
            yield
        finally:
            spent = time.monotonic() - started
            self.verify_seconds += spent
            self.current_verification_seconds += spent
            self.verification_active_since = None
            self.persist()
            lines.write(self.run.id, "budget_verify_end", seconds=spent)
            self.verify_deadline = None
            signal.setitimer(signal.ITIMER_REAL, 0)
            if self.deadline > time.monotonic():
                self.arm()

    def charge(self, step: str, role: str) -> None:
        if time.monotonic() >= self.deadline or (self.verify_deadline is not None
                                               and time.monotonic() >= self.verify_deadline):
            raise Parked(self.reason())
        if role not in REPAIRS and not step.startswith(REPAIR_NAMES):
            return
        count = self.prior_repairs + self.repair_calls
        if count >= self.policy.max_repair_cycles:
            raise Parked(f"the repair chain used its {self.policy.max_repair_cycles} repair cycles")
        self.repair_calls += 1
        self.persist()
        lines.write(self.run.id, "budget_repair", call=step, atom=role, used=count + 1)

    def park(self, reason: str) -> dict:
        row = state.run_get(self.run.id) or {}
        branch = str(row.get("branch") or next((r.get("branch") for rid in self.parents
                     if (r := state.run_get(rid)) and r.get("branch")), ""))
        try:
            published = heads(self.run.repo, branch)
        except (OSError, ValueError, TimeoutError):
            published = None
        pr_url = str(row.get("pr_url") or next((r.get("pr_url") for rid in self.parents
                     if (r := state.run_get(rid)) and r.get("pr_url")), ""))
        util.write_json(artifact(self.run.id, "parked.json"), {
            "reason": reason, "policy": asdict(self.policy), "specification": self.specification,
            "heads": published, "branch": branch, "pr_url": pr_url,
            "workspace": str(self.run.workspace[0]) if self.run.workspace else "", "retry": RETRY})
        lines.write(self.run.id, "parked", reason=reason, retry=RETRY)
        return {"branch": branch, "pr_url": pr_url}


def verification(function):
    @wraps(function)
    def wrapped(build, *args, **kw):
        if build.run.budget is None:
            return function(build, *args, **kw)
        with build.run.budget.verifying():
            return function(build, *args, **kw)
    return wrapped
