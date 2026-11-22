from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import loops, paths, util
from .releasebuild import BUILD

NAME = "release-journeys"
MAX_JOURNEYS = 2
MAX_ACTIONS = 16
RUN_SECONDS = 60
ACTION_SECONDS = 10
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
LOG = logging.getLogger(__name__)


class Blocked(Exception):
    def __init__(self, outcome: str, reason: str):
        self.outcome = outcome
        self.reason = reason
        self.cleanup: str | None = None
        super().__init__(reason)


@dataclass(frozen=True)
class Selection:
    python: Path
    wheel: Path
    build: str
    journeys: tuple[str, ...]


def read_file(path: Path) -> bytes:
    if not stat.S_ISREG(path.stat().st_mode):
        raise OSError("expected regular file")
    with path.open("rb") as stream:
        return stream.read(MAX_OUTPUT + 1)


def selection(path: Path) -> Selection:
    try:
        data = read_file(path)
        if len(data) > MAX_OUTPUT:
            raise ValueError
        config = tomllib.loads(data.decode())
    except Exception:
        raise Blocked("environment", "config-unreadable") from None
    python, wheel, build, names = (config.get(key) for key in ("python", "wheel", "build", "journeys"))
    if not isinstance(python, str) or not Path(python).is_absolute() or "\x00" in python:
        raise Blocked("environment", "absolute-release-python-required")
    if (not isinstance(wheel, str) or not Path(wheel).is_absolute()
            or "\x00" in wheel or Path(wheel).suffix != ".whl"):
        raise Blocked("environment", "absolute-release-wheel-required")
    if not isinstance(build, str) or not BUILD.fullmatch(build):
        raise Blocked("environment", "release-build-identity-required")
    if (not isinstance(names, list) or not 1 <= len(names) <= MAX_JOURNEYS
            or any(not isinstance(name, str) or name not in JOURNEYS for name in names)
            or len(set(names)) != len(names)):
        raise Blocked("environment", "invalid-journey-selection")
    return Selection(Path(python), Path(wheel), build, tuple(names))


@dataclass
class Budget:
    remaining: float
    actions: int = 0

    def start(self, now: float) -> float:
        if self.actions >= MAX_ACTIONS or self.remaining <= 0:
            raise Blocked("infrastructure", "run-limit")
        self.actions += 1
        return now + min(self.remaining, ACTION_SECONDS)


def read_output(stream: BinaryIO, deadline: float) -> bytes:
    data = bytearray()
    with selectors.DefaultSelector() as readable:
        readable.register(stream, selectors.EVENT_READ)
        while (left := deadline - time.monotonic()) > 0 and readable.select(left):
            chunk = stream.read1(MAX_OUTPUT + 1 - len(data))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > MAX_OUTPUT:
                raise Blocked("infrastructure", "output-limit")
    raise Blocked("timeout", "action-timeout")


def stop_action(proc: subprocess.Popen) -> str | None:
    failure = None
    try:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        failure = "action-cleanup-failed"
    try:
        proc.stdin.close()
    except OSError:
        failure = "action-cleanup-failed"
    try:
        proc.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return failure or "action-cleanup-timeout"
    except OSError:
        return "action-cleanup-failed"
    return failure


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


@dataclass(frozen=True)
class ActionResult:
    code: int
    output: bytes
    cleanup: str | None = None


def invoke(selected: Selection, argv: tuple[str, ...], scratch: Path, deadline: float) -> ActionResult:
    if time.monotonic() >= deadline:
        raise Blocked("timeout", "action-timeout")
    env = {paths.SCRATCH_ENV: str(scratch), "PATH": os.defpath, "TMPDIR": str(scratch),
           "HOME": str(scratch), "XDG_STATE_HOME": str(scratch / "client")}
    if argv == BUILD_ACTION:
        command = (str(BUILD_PROBE), str(selected.wheel))
    elif argv == SETTINGS_ACTION:
        command = ("-c", SETTINGS_PROBE)
    else:
        command = ("-m", "arete.cli", *argv)
    release = (str(selected.python), "-I", "-B", "-X", f"pycache_prefix={scratch / 'bytecode'}", *command)
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
            code, out = action_result(proc, deadline)
        except Blocked as failure:
            error = failure
        finally:
            cleanup = stop_action(proc)
    if error is not None:
        error.cleanup = cleanup
        raise error
    return ActionResult(code, out, cleanup)


@dataclass
class Attempt:
    selected: Selection
    scratch: Path
    budget: Budget
    evidence: dict

    @property
    def config(self) -> Path:
        return self.scratch / "config" / "config.toml"

    def call(self, *argv: str, expected_error: bytes | None = None) -> tuple[int, bytes]:
        started = time.monotonic()
        deadline = self.budget.start(started)
        log = {"argv": list(argv)}
        self.evidence["actions"].append(log)
        try:
            result = invoke(self.selected, argv, self.scratch, deadline)
        except Blocked as error:
            log["error"] = error.reason
            if error.cleanup is not None:
                log["cleanup"] = error.cleanup
            raise
        finally:
            self.budget.remaining -= min(deadline, time.monotonic()) - started
        log["exit"] = result.code
        if result.cleanup is not None:
            log["cleanup"] = result.cleanup
        if argv == BUILD_ACTION:
            self.verify_build(result.code, result.output)
        elif result.code != 0 and (result.code, result.output) != (1, expected_error):
            raise Blocked("infrastructure", "unexpected-command-exit")
        if result.cleanup is not None:
            raise Blocked("infrastructure", result.cleanup)
        return result.code, result.output

    def ok(self, *argv: str) -> bytes:
        return self.call(*argv)[1]

    def check(self, invariant: str, passed: bool) -> None:
        self.evidence["checks"].append({"invariant": invariant, "passed": passed})
        if not passed:
            raise Blocked("assertion", invariant)

    def verify_build(self, code: int, out: bytes) -> None:
        if code == 2:
            raise Blocked("environment", "release-wheel-unverified")
        build = out.strip().decode("ascii", errors="replace")
        if code != 0 or not BUILD.fullmatch(build):
            raise Blocked("startup", "build-probe-failed")
        self.evidence["observed_build"] = build
        if build != self.selected.build:
            raise Blocked("environment", "release-build-mismatch")

    def read_scratch(self, path: Path, limit: int) -> bytes:
        return paths.read_scratch_file(path, limit) or b""

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
    attempt.check("no-live-runs-in-fresh-state", not attempt.ok("status", "--json", "--offline").strip())


def seed_config(path: Path) -> None:
    path.parent.mkdir()
    path.write_bytes(CUSTOM_CONFIG)


def config_protection(attempt: Attempt) -> None:
    attempt.evidence["actions"].append({"fixture": "custom-config"})
    seed_config(attempt.config)
    expected = f"{attempt.config} exists; pass --force to overwrite\n".encode()
    code, _ = attempt.call("init", expected_error=expected)
    attempt.check("existing-config-preserved", attempt.config_contents() == CUSTOM_CONFIG)
    attempt.check("overwrite-refused", code == 1)
    code, _ = attempt.call("init", "--force", expected_error=expected)
    changed = attempt.config_contents()
    attempt.check("forced-init-replaces-config", code == 0 and changed != CUSTOM_CONFIG and 0 < len(changed) <= MAX_OUTPUT)
    attempt.check_settings("forced-config-loads")


JOURNEYS = {"first-run": first_run, "config-protection": config_protection}


def cleanup_scratches(scratches: list[Path]) -> None:
    for scratch in scratches:
        try:
            shutil.rmtree(scratch)
        except FileNotFoundError:
            pass
        except Exception as error:
            LOG.warning("%s: scratch-cleanup-failed (%s)", NAME, type(error).__name__)


def run_attempt(selected: Selection, name: str, folder: Path, budget: Budget, scratches: list[Path]) -> dict:
    evidence = {"actions": [], "checks": []}
    scratch = folder / (paths.SCRATCH_PREFIX + uuid.uuid4().hex[:16])
    try:
        scratch.mkdir(mode=0o700)
        scratches.append(scratch)
        attempt = Attempt(selected, scratch, budget, evidence)
        attempt.call(*BUILD_ACTION)
        JOURNEYS[name](attempt)
        evidence["outcome"] = "pass"
    except Blocked as error:
        evidence.update(outcome=error.outcome, reason=error.reason)
    except OSError:
        evidence.update(outcome="environment", reason="scratch-io-failed")
    return evidence


def run_journey(selected: Selection, name: str, folder: Path, budget: Budget, scratches: list[Path]) -> dict:
    first = run_attempt(selected, name, folder, budget, scratches)
    attempts = [first]
    outcome = first["outcome"]
    result = {"journey": name, "attempts": attempts, "outcome": outcome}
    if outcome == "assertion":
        replay = run_attempt(selected, name, folder, budget, scratches)
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


def coalesce(root: Path, folder: Path, row: dict) -> bool:
    key = hashlib.sha256(row["fingerprint"].encode()).hexdigest()
    canonical = root / "findings" / f"{key}.json"
    canonical.parent.mkdir(mode=0o700, exist_ok=True)
    candidate = folder / f"{key}.json"
    save_json(candidate, row)
    try:
        os.link(candidate, canonical)
    except FileExistsError:
        return False
    finally:
        candidate.unlink()
    util.flush_folder(canonical.parent)
    return True


def recovery_report(root: Path, relative: str, report: dict, reason: str, saved: bool) -> str:
    failure = dict(report, outcome="infrastructure", reason=reason, findings=[])
    if saved:
        failure["completed_report"] = relative
    recovery = str(Path(relative).with_name("infrastructure.json"))
    try:
        save_json(root / recovery, failure)
    except OSError:
        return relative if saved else ""
    return recovery


def finish(root: Path, relative: str, report: dict, scratches: list[Path], trial: bool) -> int:
    location = ""
    reason = None
    phase = "report"
    try:
        save_json(root / relative, report)
        location = relative
        phase = "finding"
        for row in report["findings"]:
            if trial or coalesce(root, (root / relative).parent, row):
                print(json.dumps(row))
    except OSError:
        reason = f"{phase}-io-failed"
        location = recovery_report(root, relative, report, reason, bool(location))
    if location:
        cleanup_scratches(scratches)
    if reason:
        suffix = f"; local report {location}" if location else ""
        print(f"{NAME}: infrastructure; {reason}{suffix}")
        return 1
    print(f"{NAME}: {report['outcome']}; local report {relative}")
    return int(report["outcome"] in ("blocked", "environment", "infrastructure"))


def run(config: Path, root: Path, *, trial: bool = False) -> int:
    started = time.monotonic()
    budget = Budget(RUN_SECONDS)
    scratches = []
    run_id = uuid.uuid4().hex
    relative = f"runs/{run_id}/report.json"
    report = {"run": run_id, "started": util.iso(), "journeys": [], "findings": []}
    try:
        root = root.resolve()
        (root / relative).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        selected = selection(config)
        report["build"] = selected.build
        for name in selected.journeys:
            result = run_journey(selected, name, (root / relative).parent, budget, scratches)
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
    return finish(root, relative, report, scratches, trial)


def builtin(loop: loops.Loop) -> bool:
    return loop.name == NAME and loop.command == ["arete", NAME]


def command(args) -> int:
    config = Path(args.config) if args.config else paths.CONFIG_DIR / f"{NAME}.toml"
    return run(config, paths.STATE_DIR / NAME, trial=os.environ.get("ARETE_TRIAL") == "1")
