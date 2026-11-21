from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from . import loops, paths, util
from .releasebuild import BUILD

NAME = "release-journeys"
MAX_JOURNEYS = 2
MAX_ACTIONS = 16
RUN_SECONDS = 60
ACTION_SECONDS = 10
CLEANUP_RESERVE_SECONDS = 1
REPORT_RESERVE_SECONDS = 2
RECOVERY_RESERVE_SECONDS = 0.75
REAP_SECONDS = 0.25
MAX_OUTPUT = 65536
CUSTOM_CONFIG = b"max_parallel = 2\n"
BUILD_ACTION = ("build-identity",)
BUILD_PROBE = Path(__file__).with_name("releasebuild.py")
SETTINGS_ACTION = ("validate-settings",)
SETTINGS_PROBE = """\
from arete.config import load_required

try:
    load_required()
except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
    print("rejected")
else:
    print("accepted")
"""
ACTION_PROGRAM = """\
import os
import signal
import sys
from threading import Thread

def exec_release(cwd, command):
    status_fd = os.dup(2)
    try:
        os.chdir(cwd)
    except OSError:
        os.write(status_fd, b"scratch-unavailable\\n")
        os._exit(1)
    try:
        with open(os.devnull, "r+b") as empty:
            os.dup2(empty.fileno(), 0)
            os.dup2(empty.fileno(), 2)
        os.execv(command[0], command)
    except OSError:
        os.write(status_fd, b"unavailable\\n")
        os._exit(1)

def report_exit(child):
    _, status = os.waitpid(child, 0)
    os.write(2, str(os.waitstatus_to_exitcode(status)).encode())
    os.close(2)

child = os.fork()
if child == 0:
    exec_release(sys.argv[1], sys.argv[2:])
else:
    try:
        os.close(1)
        Thread(target=report_exit, args=(child,), daemon=True).start()
        os.read(0, 1)
    finally:
        os.killpg(0, signal.SIGKILL)
"""
CLEANUP_PROGRAM = """\
import os
import shutil
import sys
from threading import Thread

def watch_owner():
    os.read(0, 1)
    os._exit(1)

Thread(target=watch_owner, daemon=True).start()
try:
    shutil.rmtree(sys.argv[1])
except FileNotFoundError as error:
    if error.filename != sys.argv[1]:
        raise
"""


class Blocked(Exception):
    def __init__(self, outcome: str, reason: str):
        self.outcome = outcome
        self.reason = reason
        self.cleanup: str | None = None
        super().__init__(reason)


@dataclass(frozen=True)
class Selection:
    python: Path
    build: str
    journeys: tuple[str, ...]


def read_file(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("expected regular file")
        return stream.read(MAX_OUTPUT + 1)


def selection(path: Path, deadline: float) -> Selection:
    try:
        data = storage(deadline, "config", read_file, path)
        if len(data) > MAX_OUTPUT:
            raise ValueError
        config = tomllib.loads(data.decode())
    except (OSError, ValueError, RecursionError):
        raise Blocked("environment", "config-unreadable") from None
    python, build, names = (config.get(key) for key in ("python", "build", "journeys"))
    if not isinstance(python, str) or not Path(python).is_absolute() or "\x00" in python:
        raise Blocked("environment", "absolute-release-python-required")
    if not isinstance(build, str) or not BUILD.fullmatch(build):
        raise Blocked("environment", "release-build-identity-required")
    if (not isinstance(names, list) or not 1 <= len(names) <= MAX_JOURNEYS
            or any(not isinstance(name, str) or name not in JOURNEYS for name in names)
            or len(set(names)) != len(names)):
        raise Blocked("environment", "invalid-journey-selection")
    return Selection(Path(python), build, tuple(names))


@dataclass
class Budget:
    deadline: float = field(default_factory=lambda: time.monotonic() + RUN_SECONDS)
    actions: int = 0

    @property
    def cleanup_deadline(self) -> float:
        return self.deadline - REPORT_RESERVE_SECONDS

    @property
    def action_deadline(self) -> float:
        return self.cleanup_deadline - CLEANUP_RESERVE_SECONDS

    @property
    def report_deadline(self) -> float:
        return self.deadline - RECOVERY_RESERVE_SECONDS

    def start(self) -> float:
        now = time.monotonic()
        if self.actions >= MAX_ACTIONS or now >= self.action_deadline:
            raise Blocked("infrastructure", "run-limit")
        self.actions += 1
        return min(self.action_deadline, now + ACTION_SECONDS)


def read_pipe(stream: BinaryIO, deadline: float, limit: int) -> bytes:
    data = bytearray()
    with selectors.DefaultSelector() as readable:
        readable.register(stream, selectors.EVENT_READ)
        while (left := deadline - time.monotonic()) > 0 and readable.select(left):
            chunk = stream.read1(limit + 1 - len(data))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise OverflowError
    raise TimeoutError


def read_output(stream: BinaryIO, deadline: float) -> bytes:
    try:
        return read_pipe(stream, deadline, MAX_OUTPUT)
    except TimeoutError:
        raise Blocked("infrastructure", "action-timeout") from None
    except OverflowError:
        raise Blocked("infrastructure", "output-limit") from None


def reap_storage(pid: int, deadline: float) -> None:
    with suppress(ChildProcessError):
        while not os.waitpid(pid, os.WNOHANG)[0]:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.005, left))


def storage_worker(incoming: BinaryIO, outgoing: BinaryIO, fn, args: tuple) -> None:
    status = 1
    try:
        incoming.close()
        for fd in (0, 1, 2):
            if fd != outgoing.fileno():
                with suppress(OSError):
                    os.close(fd)
        result = fn(*args)
        with outgoing:
            outgoing.write(b"1" + (result if result is not None else b""))
        status = 0
    finally:
        os._exit(status)


def storage(deadline: float, operation: str, fn, *args) -> bytes:
    stop_at = deadline - REAP_SECONDS
    if time.monotonic() >= stop_at:
        raise Blocked("infrastructure", f"{operation}-io-timeout")
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb") as incoming, os.fdopen(writer, "wb") as outgoing:
        pid = os.fork()
        if pid == 0:
            storage_worker(incoming, outgoing, fn, args)
        finished = False
        try:
            outgoing.close()
            result = read_pipe(incoming, stop_at, MAX_OUTPUT + 2)
            finished = True
        except TimeoutError:
            raise Blocked("infrastructure", f"{operation}-io-timeout") from None
        except OverflowError:
            raise OSError(f"{operation}-io-failed") from None
        finally:
            if not finished:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            reap_storage(pid, min(deadline, time.monotonic() + REAP_SECONDS))
    if not result.startswith(b"1"):
        raise OSError(f"{operation}-io-failed")
    return result[1:]


def stop_action(proc: subprocess.Popen, deadline: float) -> bool:
    proc.stdin.close()
    try:
        proc.wait(timeout=min(REAP_SECONDS, max(0, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        return False
    return True


def action_result(proc: subprocess.Popen, deadline: float) -> tuple[int, bytes]:
    try:
        out = read_output(proc.stdout, deadline)
        status = read_output(proc.stderr, deadline)
    except OSError:
        raise Blocked("infrastructure", "command-io-failed") from None
    if status.startswith(b"unavailable\n"):
        raise Blocked("environment", "release-executable-unavailable")
    if status.startswith(b"scratch-unavailable\n"):
        raise Blocked("environment", "scratch-io-failed")
    if not re.fullmatch(rb"-?\d{1,3}", status):
        raise Blocked("infrastructure", "action-status-unavailable")
    return int(status), out


def invoke(python: Path, argv: tuple[str, ...], scratch: Path, deadline: float) -> tuple[int, bytes]:
    stop_at = deadline - REAP_SECONDS
    if time.monotonic() >= stop_at:
        raise Blocked("infrastructure", "action-timeout")
    env = {paths.SCRATCH_ENV: str(scratch), "PATH": os.defpath, "TMPDIR": str(scratch),
           "HOME": str(scratch), "XDG_STATE_HOME": str(scratch / "client")}
    if argv == BUILD_ACTION:
        command = (str(BUILD_PROBE),)
    elif argv == SETTINGS_ACTION:
        command = ("-c", SETTINGS_PROBE)
    else:
        command = ("-m", "arete.cli", *argv)
    release = (str(python), "-I", "-B", "-X", f"pycache_prefix={scratch / 'bytecode'}", *command)
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", ACTION_PROGRAM,
                                 str(scratch), *release], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **util.own_group_args())
    except OSError:
        raise Blocked("environment", "release-executable-unavailable") from None
    error = None
    with proc.stdout, proc.stderr:
        try:
            result = action_result(proc, stop_at)
        except Blocked as failure:
            error = failure
        finally:
            stopped = stop_action(proc, deadline)
    if error is not None:
        if not stopped:
            error.cleanup = "action-cleanup-timeout"
        raise error
    if not stopped:
        raise Blocked("infrastructure", "action-cleanup-timeout")
    return result


@dataclass
class Attempt:
    selected: Selection
    scratch: Path
    budget: Budget
    evidence: dict

    @property
    def config(self) -> Path:
        return self.scratch / "config" / "config.toml"

    def call(self, *argv: str) -> tuple[int, bytes]:
        deadline = self.budget.start()
        log = {"argv": list(argv)}
        self.evidence["actions"].append(log)
        try:
            code, out = invoke(self.selected.python, argv, self.scratch, deadline)
        except Blocked as error:
            log["error"] = error.reason
            if error.cleanup is not None:
                log["cleanup"] = error.cleanup
            raise
        log["exit"] = code
        return code, out

    def ok(self, *argv: str) -> bytes:
        code, out = self.call(*argv)
        if code != 0:
            raise Blocked("infrastructure", "unexpected-command-exit")
        return out

    def check(self, invariant: str, passed: bool) -> None:
        self.evidence["checks"].append({"invariant": invariant, "passed": passed})
        if not passed:
            raise Blocked("assertion", invariant)

    def verify_build(self) -> None:
        try:
            code, out = self.call(*BUILD_ACTION)
        except Blocked as error:
            if error.reason == "action-timeout":
                raise Blocked("startup", error.reason) from None
            raise
        build = out.strip().decode("ascii", errors="replace")
        if code != 0 or not BUILD.fullmatch(build):
            raise Blocked("startup", "build-probe-failed")
        self.evidence["observed_build"] = build
        if build != self.selected.build:
            raise Blocked("environment", "release-build-mismatch")

    def read_scratch(self, path: Path, limit: int) -> bytes:
        return storage(self.budget.action_deadline, "scratch", paths.read_scratch_file, path, limit)

    def config_contents(self) -> bytes:
        return self.read_scratch(self.config, MAX_OUTPUT + 1)

    def check_settings(self, invariant: str) -> None:
        out = self.ok(*SETTINGS_ACTION)
        if out not in (b"accepted\n", b"rejected\n"):
            raise Blocked("infrastructure", "unexpected-settings-result")
        self.check(invariant, out == b"accepted\n")


def first_run(attempt: Attempt) -> None:
    if not paths.ON_MAC:
        raise Blocked("environment", "confined-state-unsupported")
    attempt.ok("init")
    attempt.check("initial-config-created", 0 < len(attempt.config_contents()) <= MAX_OUTPUT)
    attempt.check_settings("initial-config-loads")
    attempt.ok("migrate")
    database = attempt.scratch / "state" / "arete.db"
    attempt.check("state-initialized", bool(attempt.read_scratch(database, 1)))
    attempt.check("no-live-runs-in-fresh-state", not attempt.ok("status", "--json").strip())


def seed_config(path: Path) -> None:
    path.parent.mkdir()
    path.write_bytes(CUSTOM_CONFIG)


def config_protection(attempt: Attempt) -> None:
    deadline = attempt.budget.start()
    attempt.evidence["actions"].append({"fixture": "custom-config"})
    storage(deadline, "scratch", seed_config, attempt.config)
    code, out = attempt.call("init")
    expected = f"{attempt.config} exists; pass --force to overwrite\n".encode()
    if code != 0 and (code, out) != (1, expected):
        raise Blocked("infrastructure", "unexpected-command-exit")
    attempt.check("existing-config-preserved", attempt.config_contents() == CUSTOM_CONFIG)
    attempt.check("overwrite-refused", code == 1)
    code, out = attempt.call("init", "--force")
    if code != 0 and (code, out) != (1, expected):
        raise Blocked("infrastructure", "unexpected-command-exit")
    changed = attempt.config_contents()
    attempt.check("forced-init-replaces-config", code == 0 and changed != CUSTOM_CONFIG and 0 < len(changed) <= MAX_OUTPUT)
    attempt.check_settings("forced-config-loads")


JOURNEYS = {"first-run": first_run, "config-protection": config_protection}


def cleanup_scratch(scratch: Path, deadline: float) -> None:
    stop_at = deadline - REAP_SECONDS
    try:
        if stop_at <= time.monotonic():
            raise Blocked("infrastructure", "scratch-cleanup-timeout")
        proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", CLEANUP_PROGRAM, str(scratch)],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with proc.stdin:
            try:
                if proc.wait(timeout=max(0, stop_at - time.monotonic())) != 0:
                    raise Blocked("infrastructure", "scratch-cleanup-failed")
            finally:
                if proc.returncode is None:
                    proc.kill()
                    proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise Blocked("infrastructure", "scratch-cleanup-timeout") from None
    except OSError:
        raise Blocked("infrastructure", "scratch-cleanup-failed") from None


def make_scratch(scratch: Path) -> None:
    scratch.mkdir(mode=0o700)


def run_attempt(selected: Selection, name: str, folder: Path, budget: Budget) -> dict:
    evidence = {"actions": [], "checks": []}
    scratch = folder / (paths.SCRATCH_PREFIX + uuid.uuid4().hex[:16])
    created = False
    try:
        if time.monotonic() >= budget.action_deadline:
            raise Blocked("infrastructure", "run-limit")
        created = True
        storage(budget.action_deadline, "scratch", make_scratch, scratch)
        attempt = Attempt(selected, scratch, budget, evidence)
        attempt.verify_build()
        JOURNEYS[name](attempt)
        evidence["outcome"] = "pass"
    except Blocked as error:
        evidence.update(outcome=error.outcome, reason=error.reason)
    except OSError:
        evidence.update(outcome="environment", reason="scratch-io-failed")
    finally:
        if created:
            try:
                cleanup_scratch(scratch, budget.cleanup_deadline)
            except Blocked as error:
                evidence["cleanup"] = error.reason
                if evidence.get("outcome") in ("pass", "assertion"):
                    evidence.update(outcome=error.outcome, reason=error.reason)
    return evidence


def run_journey(selected: Selection, name: str, folder: Path, budget: Budget) -> dict:
    first = run_attempt(selected, name, folder, budget)
    attempts = [first]
    outcome = first["outcome"]
    result = {"journey": name, "attempts": attempts, "outcome": outcome}
    if outcome == "assertion":
        replay = run_attempt(selected, name, folder, budget)
        attempts.append(replay)
        if replay["outcome"] == "assertion" and replay["reason"] == first["reason"]:
            result.update(outcome="product-failure", invariant=first["reason"])
        else:
            result["outcome"] = "inconclusive" if replay["outcome"] in ("pass", "assertion") else replay["outcome"]
    return result


def finding(result: dict, evidence: str) -> dict:
    name, invariant = result["journey"], result["invariant"]
    return {"title": f"Release journey failed: {name}",
            "summary": f"The {invariant} invariant failed twice in fresh state.",
            "fingerprint": f"{NAME}:{name}:{invariant}", "evidence": evidence}


def save_json(path: Path, data: dict) -> None:
    util.write_durably(path, json.dumps(data, indent=2).encode() + b"\n", mode=0o600)


def coalesce(root: Path, folder: Path, row: dict) -> bytes:
    key = hashlib.sha256(row["fingerprint"].encode()).hexdigest()
    canonical = root / "findings" / f"{key}.json"
    canonical.parent.mkdir(mode=0o700, exist_ok=True)
    candidate = folder / f"{key}.json"
    save_json(candidate, row)
    try:
        os.link(candidate, canonical)
    except FileExistsError:
        return b""
    finally:
        candidate.unlink()
    util.flush_folder(canonical.parent)
    return b"1"


def prepare_folder(root: Path, relative: str) -> bytes:
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / relative).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.fsencode(root)


def recovery_report(root: Path, relative: str, report: dict, deadline: float, reason: str, saved: bool) -> str:
    failure = dict(report, outcome="infrastructure", reason=reason, findings=[])
    if saved:
        failure["completed_report"] = relative
    recovery = str(Path(relative).with_name("infrastructure.json"))
    try:
        storage(deadline, "report", save_json, root / recovery, failure)
    except (OSError, Blocked):
        return relative if saved else ""
    return recovery


def finish(root: Path, relative: str, report: dict, budget: Budget, trial: bool) -> int:
    saved = False
    phase = "report"
    try:
        storage(budget.report_deadline, phase, save_json, root / relative, report)
        saved = True
        phase = "finding"
        for row in report["findings"]:
            if trial or storage(budget.report_deadline, phase, coalesce, root, (root / relative).parent, row):
                print(json.dumps(row))
    except (OSError, Blocked) as error:
        reason = error.reason if isinstance(error, Blocked) else f"{phase}-io-failed"
        location = recovery_report(root, relative, report, budget.deadline, reason, saved)
        suffix = f"; local report {location}" if location else ""
        print(f"{NAME}: infrastructure; {reason}{suffix}")
        return 1
    print(f"{NAME}: {report['outcome']}; local report {relative}")
    return int(report["outcome"] in ("blocked", "environment", "infrastructure"))


def run(config: Path, root: Path, *, trial: bool = False) -> int:
    started = time.monotonic()
    budget = Budget()
    run_id = uuid.uuid4().hex
    relative = f"runs/{run_id}/report.json"
    report = {"run": run_id, "started": util.iso(), "journeys": [], "findings": []}
    try:
        root = Path(os.fsdecode(storage(budget.action_deadline, "evidence", prepare_folder, root, relative)))
        selected = selection(config, budget.action_deadline)
        report["build"] = selected.build
        for name in selected.journeys:
            result = run_journey(selected, name, (root / relative).parent, budget)
            report["journeys"].append(result)
            if result["outcome"] == "product-failure":
                report["findings"].append(finding(result, relative))
        completed = all(result["outcome"] in ("pass", "product-failure") for result in report["journeys"])
        report["outcome"] = ("partial" if report["findings"] else "blocked") if not completed else (
            "product-failure" if report["findings"] else "pass")
    except Blocked as error:
        report.update(outcome=error.outcome, reason=error.reason)
    except OSError:
        report.update(outcome="infrastructure", reason="evidence-io-failed")
    report.update(actions=budget.actions, seconds=round(time.monotonic() - started, 3))
    return finish(root, relative, report, budget, trial)


def builtin(loop: loops.Loop) -> bool:
    return loop.name == NAME and loop.command == ["arete", NAME]


def command(args) -> int:
    config = Path(args.config) if args.config else paths.CONFIG_DIR / f"{NAME}.toml"
    return run(config, paths.STATE_DIR / NAME, trial=os.environ.get("ARETE_TRIAL") == "1")
