from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, lines, state, util
from .agent import NO_SESSION, Session

RESULTS = ("pass", "fail", "ask", "stop")
LABELS = ("runner", "model", "effort", "tier", "kind", "auth_mode")
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


class OnlyDone(Exception):
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
    n: int = 0

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
    found = {}
    for key in LABELS:
        values = [str(c[key]) for c in made if c.get(key)]
        if key == "auth_mode":
            values = [mode for value in values for mode in value.split(", ")]
        found[key] = ", ".join(dict.fromkeys(values))
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
        skipped = next((line["skipped"] for line in read
                        if line.get("event") == "route" and type(line.get("skipped")) is int), None)
        done = (done if skipped is None else done[:skipped]) + ends(read)
    return done


def started(run_id: str, name: str) -> bool:
    return any(line.get("event") == "step_start" and line.get("step") == name
               for read in chain(run_id) for line in read)


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
                                   str(session.get("runner") or ""), str(session.get("credential") or ""),
                                   str(session.get("account") or ""), str(session.get("generation") or "")))


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
    def __init__(self, run_id: str, continues: str = "", only: str = "", ctx=None,
                 labels: Callable[[str], dict] | None = None, again: str = "",
                 restore: Callable[[dict], str] | None = None) -> None:
        if only and only not in STEPS:
            raise KeyError(f"only {only}: no step by that name")
        if again and again not in STEPS:
            raise KeyError(f"from {again}: no step by that name")
        chained = chain(continues)
        self.run_id, self.continues, self.only, self.ctx, self.again = run_id, continues, only, ctx, again
        self.labels = labels or (lambda name: {})
        self.restore = restore or (lambda line: "a step can run alone only on a run whose folder is kept")
        self.earlier = history(chained)
        self.again_at = max((i for i, line in enumerate(self.earlier) if again and line.get("step") == again),
                            default=-1)
        self.tries = tries_in([lines.read(run_id), *chained])
        self.last_prints = {str(line["tree"]): str(line.get("fingerprint") or "")
                            for line in self.earlier if line.get("tree")}
        self.prints: dict[str, str] = {}
        self.skipped, self.matching, self.done = 0, bool(continues or only), False
        if self.matching:
            lines.write(run_id, "route", continues=continues, only=only, again=again)

    def step(self, name: str, *args, **kw) -> Outcome:
        if name not in STEPS:
            raise KeyError(f"no step {name}")
        if self.done:
            raise OnlyDone(f"only {self.only} runs in this flow")
        if self.matching:
            if self.takes(name) and (found := self.stored(name, kw)):
                self.skipped += 1
                return found
            if self.only and name != self.only:
                if not any(line.get("step") == self.only for line in self.earlier):
                    raise Refused(f"only {self.only}: run {self.continues} never started {self.only}")
                raise Refused(f"only {self.only}: {name} before it has no stored outcome it can take")
            self.route()
        try:
            return self.run(name, args, kw)
        finally:
            self.done = self.done or name == self.only

    def takes(self, name: str) -> bool:
        return name != self.only and (name != self.again or self.skipped < self.again_at)

    def stored(self, name: str, kw: dict) -> Outcome | None:
        line = self.earlier[self.skipped] if self.skipped < len(self.earlier) else {}
        if line.get("step") != name or not self.takable(line):
            return None
        if self.only and line.get("tree") and not os.path.isdir(line["tree"]):
            why = self.restore(line)
            if why or not os.path.isdir(line["tree"]):
                why = why or "its work folder is still missing after restoring from origin"
                raise Refused(f"only {self.only}: run {self.continues} removed its work folder {line['tree']} when it "
                              f"ended, and {why}")
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
                    only=self.only, again=self.again)
        self.matching = False

    def run(self, name: str, args: tuple, kw: dict) -> Outcome:
        n = self.tries[name] = self.tries.get(name, 0) + 1
        calls, started = Calls(self.run_id, name, n=n), time.monotonic()
        try:
            labels = self.labels(name)
        except Exception as e:
            lines.write(self.run_id, "step_start", step=name, **{"try": n})
            self.failed(name, n, e, calls, started, kw)
            raise
        lines.write(self.run_id, "step_start", step=name, **{"try": n}, **labels)
        try:
            outcome = self.call(name, calls, args, kw)
        except Exception as e:
            self.failed(name, n, e, calls, started, kw)
            raise
        except BaseException as e:
            self.end(name, n, Outcome("stop"), f"stopped by {type(e).__name__}", calls, started, kw)
            raise
        self.end(name, n, outcome, "" if outcome.passed else calls.error, calls, started, kw)
        return outcome

    def failed(self, name: str, n: int, e: Exception, calls: Calls, started: float, kw: dict) -> None:
        message = f"{type(e).__name__}: {e}"[:MAX_ERROR_CHARS]
        self.end(name, n, Outcome("fail"), message, calls, started, kw)
        lines.write(self.run_id, "error", step=name, message=message)

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
        return lines.write(self.run_id, "report", **{**fields, "status": status, "steps": tally(self.run_id)})


def tally(run_id: str) -> dict[str, dict]:
    found: dict[str, list] = {}
    for line in lines.read(run_id):
        if line.get("event") == "step_end":
            counts = found.setdefault(str(line.get("step") or ""), [0, 0.0, 0.0])
            counts[0] += 1
            counts[1] += amount(line, "seconds")
            counts[2] += amount(line, "cost_usd")
    return {name: {"tries": n, "minutes": round(seconds / 60, 2), "cost_usd": round(cost, 6)}
            for name, (n, seconds, cost) in found.items()}


def overrides(sets: list[str], role_of: dict[str, str]) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for item in sets:
        name, _, rest = item.partition(".")
        key, eq, value = rest.partition("=")
        if not (name and key and eq):
            raise ValueError(f"--set {item}: write step.key=value")
        if name not in role_of:
            raise ValueError(f"--set {item}: {name} is not a step that calls an agent; use {', '.join(role_of)}")
        table.setdefault(role_of[name], {})[key] = (
            int(value) if key in config.STEP_COUNTS and value.isdigit()
            else [v for v in value.split(",") if v] if key in config.STEP_LISTS
            else value == "true" if key == "structured_output" and value in ("true", "false") else value)
    return config.step_tables(table, "--set")


def main(fn: Callable, argv: list[str] | None = None) -> None:
    from . import flows, pipeline

    p = argparse.ArgumentParser(description=f"run the {fn.__name__} flow on one ticket")
    p.add_argument("repo")
    p.add_argument("ticket", help="an issue number (35), a Jira key (ABC-1234) or a task id (t-1a2b)")
    p.add_argument("--force", action="store_true", help="run a ticket the approval rules would skip")
    p.add_argument("--dry-run", action="store_true", help="plan and build locally, no push, PR or tracker writes")
    p.add_argument("--continue", dest="continues", default="", metavar="RUN",
                   help="take the passing steps of this earlier run of the ticket")
    p.add_argument("--only", default="", choices=sorted(STEPS),
                   help="run this step alone, the steps before it from the stored outcomes")
    p.add_argument("--from", dest="again", default="", choices=sorted(STEPS),
                   help="with --continue, run this step again and every step after it")
    p.add_argument("--repair", dest="repairs", default="", metavar="RUN",
                   help="continue the open PR of this held run from its saved internal review")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="STEP.KEY=VALUE",
                   help="override one setting of one step; repeat for more")
    args = p.parse_args(argv)
    try:
        table = overrides(args.sets, flows.ROLE_OF)
    except (ValueError, TypeError) as e:
        p.error(str(e))
    script = pipeline.Script(Path(inspect.getfile(fn)).stem, fn, args.continues, args.only, table, args.again,
                             args.repairs)
    try:
        code = pipeline.run_issue(args.repo, args.ticket, args.dry_run, args.force, script)
    except config.UnknownRepo as e:
        raise SystemExit(str(e)) from None
    raise SystemExit(code)


def earlier(argv: list[str]) -> tuple[Callable, list[str]]:
    from . import flows, pipeline

    p = argparse.ArgumentParser(description="run one step of an earlier run alone, on the script that run used")
    p.add_argument("run")
    p.add_argument("step", choices=sorted(STEPS))
    p.add_argument("settings", nargs="*", metavar="KEY=VALUE", help="override one setting of the step")
    args = p.parse_args(argv)
    if not (row := state.run_get(args.run)):
        raise SystemExit(f"no run {args.run}")
    if not (path := str(first(lines.read(args.run), "task").get("path") or "")):
        raise SystemExit(f"run {args.run} did not run from a script in adws/; use arete run")
    if args.settings and args.step not in flows.ROLE_OF:
        p.error(f"{args.step} calls no agent, so it takes no settings")
    if not started(args.run, args.step):
        raise SystemExit(f"run {args.run} never started {args.step}, so it cannot run it alone")
    try:
        fn = pipeline.flow_function(config.load(), path)
    except (FileNotFoundError, AttributeError) as e:
        raise SystemExit(str(e)) from None
    return fn, [row["repo"], str(row["issue"]), "--continue", args.run, "--only", args.step,
                *(f"--set={args.step}.{s}" for s in args.settings)]
