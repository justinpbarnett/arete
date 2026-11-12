from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import lines, util
from .agent import NO_SESSION, Session

RESULTS = ("pass", "fail", "ask", "stop")
LABELS = ("runner", "model", "effort", "tier", "kind")
COUNTS = ("tokens_in", "tokens_out", "turns", "cache_read", "cache_write")
MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class Outcome:
    result: str
    next: str = ""
    feedback: str = ""
    data: dict = field(default_factory=dict)
    session: Session = NO_SESSION

    def __post_init__(self) -> None:
        if self.result not in RESULTS:
            raise ValueError(f"outcome result {self.result!r} is not one of {', '.join(RESULTS)}")

    @property
    def passed(self) -> bool:
        return self.result == "pass"


STEPS: dict[str, Callable[..., Outcome]] = {}


class Refused(ValueError):
    pass


def step(name: str) -> Callable:
    def register(fn: Callable[..., Outcome]) -> Callable[..., Outcome]:
        if name in STEPS:
            raise ValueError(f"step {name} is already registered")
        STEPS[name] = fn
        return fn

    return register


@dataclass
class Calls:
    run_id: str
    step: str
    made: list[dict] = field(default_factory=list)

    def add(self, error: str = "", **usage) -> None:
        self.made.append({**usage, "error": error})

    @property
    def error(self) -> str:
        return next((c["error"] for c in reversed(self.made) if c["error"]), "")


_open: ContextVar[Calls | None] = ContextVar("factory_calls", default=None)


def inside(run_id: str) -> Calls | None:
    calls = _open.get()
    return calls if calls is not None and calls.run_id == run_id else None


def envelope(made: list[dict]) -> dict:
    found = {k: ", ".join(dict.fromkeys(str(c[k]) for c in made if c.get(k))) for k in LABELS}
    found["cost_usd"] = round(sum(float(c.get("cost_usd") or 0) for c in made), 6)
    found |= {k: sum(int(c.get(k) or 0) for c in made) for k in COUNTS}
    found["api_seconds"] = round(sum(float(c.get("api_seconds") or 0) for c in made), 2)
    return found


def fingerprint(folder: Path | str) -> str:
    where = Path(folder)
    if not where.is_dir():
        return ""
    found = util.sh(["git", "rev-parse", "--git-path", "index"], cwd=where)
    if found.returncode != 0:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        index, real = Path(tmp) / "index", where / found.stdout.strip()
        if real.is_file():
            shutil.copyfile(real, index)
        env = {"GIT_INDEX_FILE": str(index)}
        if util.sh(["git", "add", "-A"], cwd=where, env=env).returncode != 0:
            return ""
        tree = util.sh(["git", "write-tree"], cwd=where, env=env)
        return tree.stdout.strip() if tree.returncode == 0 else ""


def first(read: list[dict], event: str) -> dict:
    return next((line for line in read if line.get("event") == event), {})


def ends(read: list[dict]) -> list[dict]:
    return [line for line in read if line.get("event") == "step_end" and "result" in line]


def chain(run_id: str) -> list[list[dict]]:
    found, seen = [], set()
    while run_id and run_id not in seen:
        seen.add(run_id)
        found.append(read := lines.read(run_id))
        run_id = str(first(read, "route").get("continues") or "")
    return found


def history(chained: list[list[dict]]) -> list[dict]:
    done: list[dict] = []
    for read in reversed(chained):
        skipped = first(read, "route").get("skipped")
        done = done[:skipped if type(skipped) is int else 0] + ends(read)
    return done


def tries_in(reads: list[list[dict]]) -> dict[str, int]:
    found: dict[str, int] = {}
    for read in reads:
        for line in read:
            n, name = line.get("try"), str(line.get("step") or "")
            if line.get("event") == "step_start" and type(n) is int:
                found[name] = max(found.get(name, 0), n)
    return found


def amount(line: dict, key: str) -> float:
    value = line.get(key)
    return value if type(value) in (int, float) else 0


def outcome_of(line: dict) -> Outcome:
    data, session = line.get("data"), line.get("session")
    session = session if isinstance(session, dict) else {}
    return Outcome(str(line.get("result") or "pass"), next=str(line.get("next") or ""),
                   feedback=str(line.get("feedback") or ""),
                   data=data if isinstance(data, dict) else {},
                   session=Session(str(session.get("id") or ""), float(amount(session, "spent_usd")),
                                   str(session.get("runner") or "")))


def checked(name: str, value) -> Outcome:
    if not isinstance(value, Outcome):
        raise TypeError(f"step {name} returned {type(value).__name__}, not an Outcome")
    if not isinstance(value.session, Session):
        raise TypeError(f"step {name} returned a session that is {type(value.session).__name__}, not a Session")
    try:
        if not isinstance(value.data, dict):
            raise TypeError(f"data is {type(value.data).__name__}, not a JSON object")
        json.dumps(value.data, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as e:
        raise ValueError(f"step {name} returned data JSON cannot write: {e}") from None
    return value


class Flow:
    def __init__(self, run_id: str, continues: str = "", only: str = "", ctx=None) -> None:
        if only and only not in STEPS:
            raise KeyError(f"only {only}: no step by that name")
        chained = chain(continues)
        self.run_id, self.continues, self.only, self.ctx = run_id, continues, only, ctx
        self.earlier = history(chained)
        self.tries = tries_in([lines.read(run_id), *chained])
        self.last_prints = {str(line["tree"]): str(line.get("fingerprint") or "")
                            for line in self.earlier if line.get("tree")}
        self.prints: dict[str, str] = {}
        self.skipped, self.matching, self.done = 0, bool(continues or only), False

    def step(self, name: str, *args, **kw) -> Outcome:
        if name not in STEPS:
            raise KeyError(f"no step {name}")
        if self.done:
            return Outcome("stop", feedback=f"only {self.only} runs in this flow")
        if self.matching:
            if name != self.only and (found := self.stored(name, kw)):
                self.skipped += 1
                return found
            if self.only and name != self.only:
                raise Refused(f"only {self.only}: {name} before it has no stored passing outcome")
            self.route()
        try:
            return self.run(name, args, kw)
        finally:
            self.done = self.done or name == self.only

    def stored(self, name: str, kw: dict) -> Outcome | None:
        line = self.earlier[self.skipped] if self.skipped < len(self.earlier) else {}
        if line.get("step") != name or not self.takable(line):
            return None
        if not self.only and line.get("tree"):
            now = self.print_of(kw.get("tree") or line["tree"])
            if not now or now != self.last_prints.get(str(line["tree"])):
                return None
        return outcome_of(line)

    def takable(self, line: dict) -> bool:
        if line.get("result") == "pass":
            return True
        return line.get("result") == "fail" and not line.get("error") and self.skipped + 1 < len(self.earlier)

    def print_of(self, folder: Path | str) -> str:
        key = os.path.abspath(folder)
        if key not in self.prints:
            self.prints[key] = fingerprint(key)
        return self.prints[key]

    def route(self) -> None:
        after = str(self.earlier[self.skipped - 1].get("step") or "") if self.skipped else ""
        lines.write(self.run_id, "route", continues=self.continues, after=after, skipped=self.skipped,
                    only=self.only)
        self.matching = False

    def run(self, name: str, args: tuple, kw: dict) -> Outcome:
        n = self.tries[name] = self.tries.get(name, 0) + 1
        lines.write(self.run_id, "step_start", step=name, **{"try": n})
        calls, started = Calls(self.run_id, name), time.monotonic()
        try:
            outcome = self.call(name, calls, args, kw)
        except Exception as e:
            message = f"{type(e).__name__}: {e}"[:MAX_ERROR_CHARS]
            self.end(name, n, Outcome("fail"), message, calls, started, kw)
            lines.write(self.run_id, "error", step=name, message=message)
            raise
        except BaseException as e:
            self.end(name, n, Outcome("stop"), f"stopped by {type(e).__name__}", calls, started, kw)
            raise
        self.end(name, n, outcome, "" if outcome.passed else calls.error, calls, started, kw)
        return outcome

    def call(self, name: str, calls: Calls, args: tuple, kw: dict) -> Outcome:
        token = _open.set(calls)
        try:
            with lines.beating(self.run_id, name):
                value = STEPS[name](self, *args, **kw)
        finally:
            _open.reset(token)
        return checked(name, value)

    def end(self, name: str, n: int, outcome: Outcome, error: str, calls: Calls, started: float, kw: dict) -> None:
        seconds, tree = round(time.monotonic() - started, 1), {}
        if kw.get("tree"):
            where = os.path.abspath(kw["tree"])
            tree = {"tree": where, "fingerprint": fingerprint(where)}
        lines.write(self.run_id, "step_end", step=name, **{"try": n}, result=outcome.result, ok=outcome.passed,
                    next=outcome.next, feedback=outcome.feedback, data=outcome.data,
                    session=asdict(outcome.session), **envelope(calls.made), seconds=seconds,
                    error=error[:MAX_ERROR_CHARS], **tree)

    def respond(self, status: str, **fields) -> dict:
        if self.matching:
            self.route()
        tally: dict[str, list] = {}
        for line in lines.read(self.run_id):
            if line.get("event") == "step_end":
                counts = tally.setdefault(str(line.get("step") or ""), [0, 0.0, 0.0])
                counts[0] += 1
                counts[1] += amount(line, "seconds")
                counts[2] += amount(line, "cost_usd")
        steps = {name: {"tries": n, "minutes": round(seconds / 60, 2), "cost_usd": round(cost, 6)}
                 for name, (n, seconds, cost) in tally.items()}
        return lines.write(self.run_id, "report", **{**fields, "status": status, "steps": steps})
