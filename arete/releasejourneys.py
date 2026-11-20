from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from . import loops, paths, util

NAME = "release-journeys"
MAX_JOURNEYS = 2
MAX_ACTIONS = 16
RUN_SECONDS = 60
ACTION_SECONDS = 10
MAX_OUTPUT = 65536
BUILD = re.compile(r"\d{1,6}\.\d{1,6}\.\d{1,6} (?:code\.)?[0-9a-f]{12}")
CUSTOM_CONFIG = b"max_parallel = 2\n"
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


class Blocked(Exception):
    def __init__(self, outcome: str, reason: str):
        self.outcome = outcome
        self.reason = reason
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


def selection(path: Path) -> Selection:
    try:
        data = read_file(path)
        if len(data) > MAX_OUTPUT:
            raise ValueError
        config = tomllib.loads(data.decode())
    except (OSError, ValueError):
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

    def start(self) -> float:
        now = time.monotonic()
        if self.actions >= MAX_ACTIONS or now >= self.deadline:
            raise Blocked("infrastructure", "run-limit")
        self.actions += 1
        return min(self.deadline, now + ACTION_SECONDS)


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
    raise Blocked("infrastructure", "action-timeout")


def invoke(python: Path, argv: tuple[str, ...], scratch: Path, deadline: float) -> tuple[int, bytes]:
    env = {paths.SCRATCH_ENV: str(scratch), "PATH": os.defpath, "TMPDIR": str(scratch),
           "HOME": str(scratch), "XDG_STATE_HOME": str(scratch / "client")}
    command = ("-c", SETTINGS_PROBE) if argv == SETTINGS_ACTION else ("-m", "arete.cli", *argv)
    try:
        proc = subprocess.Popen([str(python), "-I", "-B", *command], cwd=scratch, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                **util.own_group_args())
    except OSError:
        raise Blocked("environment", "release-executable-unavailable") from None
    with proc:
        try:
            out = read_output(proc.stdout, deadline)
            return proc.wait(timeout=max(0, deadline - time.monotonic())), out
        except subprocess.TimeoutExpired:
            raise Blocked("infrastructure", "action-timeout") from None
        finally:
            util.kill_group(proc)


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
            code, out = self.call("--version")
        except Blocked as error:
            if error.reason == "action-timeout":
                raise Blocked("startup", error.reason) from None
            raise
        build = out.removeprefix(b"arete ").strip().decode("ascii", errors="replace")
        if code != 0 or not out.startswith(b"arete ") or not BUILD.fullmatch(build):
            raise Blocked("startup", "version-probe-failed")
        self.evidence["observed_build"] = build
        if build != self.selected.build:
            raise Blocked("environment", "release-build-mismatch")

    def read_scratch(self, path: Path, limit: int) -> bytes:
        try:
            return paths.read_scratch_file(path, limit)
        except FileNotFoundError:
            return b""

    def config_contents(self) -> bytes:
        return self.read_scratch(self.config, MAX_OUTPUT + 1)

    def check_settings(self, invariant: str) -> None:
        out = self.ok(*SETTINGS_ACTION)
        if out not in (b"accepted\n", b"rejected\n"):
            raise Blocked("infrastructure", "unexpected-settings-result")
        self.check(invariant, out == b"accepted\n")


def first_run(attempt: Attempt) -> None:
    attempt.ok("init")
    attempt.check("initial-config-created", 0 < len(attempt.config_contents()) <= MAX_OUTPUT)
    attempt.check_settings("initial-config-loads")
    attempt.ok("migrate")
    database = attempt.scratch / "state" / "arete.db"
    attempt.check("state-initialized", bool(attempt.read_scratch(database, 1)))
    attempt.check("no-live-runs-in-fresh-state", not attempt.ok("status", "--json").strip())


def config_protection(attempt: Attempt) -> None:
    attempt.budget.start()
    attempt.evidence["actions"].append({"fixture": "custom-config"})
    attempt.config.parent.mkdir()
    attempt.config.write_bytes(CUSTOM_CONFIG)
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


def run_attempt(selected: Selection, name: str, folder: Path, budget: Budget) -> dict:
    evidence = {"actions": [], "checks": []}
    scratch = folder / (paths.SCRATCH_PREFIX + uuid.uuid4().hex[:16])
    created = False
    try:
        scratch.mkdir(mode=0o700)
        created = True
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
            shutil.rmtree(scratch, ignore_errors=True)
            if scratch.exists():
                evidence["cleanup"] = "scratch-cleanup-failed"
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


def run(config: Path, root: Path, *, trial: bool = False) -> int:
    started = time.monotonic()
    budget = Budget()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    relative = f"runs/{run_id}/report.json"
    folder = (root / relative).parent
    folder.mkdir(mode=0o700, parents=True)
    report = {"run": run_id, "started": util.iso(), "journeys": [], "findings": []}
    try:
        selected = selection(config)
        report["build"] = selected.build
        for name in selected.journeys:
            result = run_journey(selected, name, folder, budget)
            report["journeys"].append(result)
            if result["outcome"] == "product-failure":
                report["findings"].append(finding(result, relative))
        completed = all(result["outcome"] in ("pass", "product-failure") for result in report["journeys"])
        report["outcome"] = ("partial" if report["findings"] else "blocked") if not completed else (
            "product-failure" if report["findings"] else "pass")
    except Blocked as error:
        report.update(outcome=error.outcome, reason=error.reason)
    report.update(actions=budget.actions, seconds=round(time.monotonic() - started, 3))
    save_json(root / relative, report)
    for row in report["findings"]:
        if trial or coalesce(root, folder, row):
            print(json.dumps(row))
    print(f"{NAME}: {report['outcome']}; local report {relative}")
    return int(report["outcome"] in ("blocked", "environment"))


def builtin(loop: loops.Loop) -> bool:
    return loop.name == NAME and loop.command == ["arete", NAME]


def command(args) -> int:
    config = Path(args.config) if args.config else paths.CONFIG_DIR / f"{NAME}.toml"
    try:
        return run(config, (paths.STATE_DIR / NAME).resolve(), trial=os.environ.get("ARETE_TRIAL") == "1")
    except OSError:
        print(f"{NAME}: environment; could not write local evidence")
        return 1
