from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from . import config, environment, paths, util
from .util import git

PLAYWRIGHT_MCP = "@playwright/mcp@0.0.75"
PORT_FIELD = re.compile(r"\{(port\d*)\}")
STOP_GRACE_SECONDS = 10
EVIDENCE_BRANCH = "arete/evidence"
MAX_SCREENSHOTS = 8
MAX_SAW_CHARS = 1200
VERDICTS = ("pass", "fail", "blocked")

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail", "blocked"]},
        "summary": {"type": "string"},
        "checks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "what": {"type": "string"},
                "how": {"type": "string"},
                "saw": {"type": "string"},
                "passed": {"type": "boolean"},
                "screenshot": {"type": "string"},
            },
            "required": ["what", "how", "saw", "passed"],
        }},
        "blocked_by": {"type": "string"},
    },
    "required": ["verdict", "summary", "checks"],
}


class ServerError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def with_free_ports(v: config.Verify) -> config.Verify:
    ports = {name: free_port() for name in dict.fromkeys(PORT_FIELD.findall(v.start + v.url + v.notes))}

    def filled(text: str) -> str:
        return PORT_FIELD.sub(lambda m: str(ports[m.group(1)]), text)

    return replace(v, start=filled(v.start), url=filled(v.url), notes=filled(v.notes))


def preflight(v: config.Verify, cwd: Path, cfg: config.Config, repo: config.Repo | None = None) -> Verified | None:
    if not v.preflight:
        return None
    deadline, checks = time.monotonic() + v.preflight_seconds, []
    for name, command in v.preflight.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            checks.append({"what": name, "how": command, "saw": "preflight time budget exhausted", "passed": False})
            break
        try:
            result = environment.sh(command, cfg, repo, cwd=cwd, timeout=remaining)
            passed = result.returncode == 0
            saw = f"exit {result.returncode}: " + util.tail(result.stdout + result.stderr, MAX_SAW_CHARS)
        except OSError as error:
            passed, saw = False, f"{type(error).__name__}: {error}"
        checks.append({"what": name, "how": command, "saw": saw, "passed": passed})
        if not passed:
            break
    failed = next((check for check in checks if not check["passed"]), None)
    return Verified("blocked" if failed else "pass", "Verification prerequisites are unavailable." if failed else
                    "Configured verification prerequisites passed.", checks,
                    blocked_by=f"{failed['what']}: {failed['saw']}" if failed else "", finished=not bool(failed))


@contextmanager
def serving(v: config.Verify, cwd: Path, log: Path, cfg: config.Config | None = None,
            repo: config.Repo | None = None):
    if not v.start:
        yield ""
        return
    url = v.url.rstrip("/")
    with open(log, "w") as out:
        proc = subprocess.Popen(v.start, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=environment.commands(cfg or config.load(), repo),
                                **util.own_group_args())
    try:
        wait_until_ready(proc, url + "/" + v.ready.lstrip("/"), v.ready_seconds, log)
        yield url
    finally:
        stop(proc)


def wait_until_ready(proc: subprocess.Popen, url: str, seconds: int, log: Path) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ServerError(f"the start command exited {proc.returncode} before {url} answered{output(log)}")
        try:
            with urllib.request.urlopen(url, timeout=5):
                return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return
        except OSError:
            pass
        time.sleep(1)
    raise ServerError(f"{url} did not answer within {seconds} seconds{output(log)}")


def output(log: Path) -> str:
    text = util.tail(log.read_text(errors="replace").strip(), 3000)
    return f"\n\n```\n{text}\n```" if text else "; the start command printed nothing"


def stop(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline and group_alive(proc):
            time.sleep(0.2)
    proc.wait()


def group_alive(proc: subprocess.Popen) -> bool:
    proc.poll()
    try:
        os.killpg(proc.pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def mcp_config(output_dir: Path) -> str:
    args = ["-y", PLAYWRIGHT_MCP, "--headless", "--isolated", "--browser", "chrome", "--output-dir", str(output_dir)]
    return json.dumps({"mcpServers": {"playwright": {"command": "npx", "args": args}}})


@dataclass
class Verified:
    verdict: str
    summary: str = ""
    checks: list[dict] = field(default_factory=list)
    blocked_by: str = ""
    finished: bool = True
    saved: str = ""

    @classmethod
    def from_agent(cls, found: dict) -> Verified:
        said = found.get("verdict")
        v = cls(said.strip().lower() if isinstance(said, str) else "", found.get("summary", "").strip(),
                [c for c in found.get("checks", []) if isinstance(c, dict)], found.get("blocked_by", "").strip())
        if v.verdict not in VERDICTS:
            v.verdict = "blocked"
            v.blocked_by = v.blocked_by or f"the verifier's verdict {json.dumps(said)} is not pass, fail or blocked"
        elif v.verdict == "pass" and not v.checks:
            v.verdict, v.blocked_by = "blocked", "the verifier said pass but listed no checks"
        elif v.verdict == "pass" and v.failed_checks:
            v.verdict = "fail"
        return v

    @classmethod
    def stored(cls, data: dict) -> Verified:
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})

    @property
    def failed_checks(self) -> list[dict]:
        return [c for c in self.checks if not c.get("passed")]

    def screenshots(self, evidence: Path) -> list[Path]:
        names = [Path(c["screenshot"]).name for c in self.checks if (c.get("screenshot") or "").strip()]
        return [evidence / n for n in dict.fromkeys(names) if (evidence / n).is_file()][:MAX_SCREENSHOTS]

    def requirements(self) -> list[dict]:
        return [{"what": str(c.get("what") or "").strip(), "passed": bool(c.get("passed")),
                 "evidence": self.evidence_of(c)} for c in self.checks]

    def evidence_of(self, check: dict) -> str:
        shot = Path(str(check.get("screenshot") or "")).name
        if self.saved and shot and (found := Path(self.saved).parent / "evidence" / shot).is_file():
            return os.path.abspath(found)
        return self.saved

    def markdown(self, image_url: dict[str, str] | None = None) -> str:
        if self.verdict == "unverified":
            return self.summary
        lines = [f"**{self.verdict}**: {self.summary}"]
        if self.blocked_by:
            lines.append(f"\nBlocked by: {self.blocked_by}")
        embedded: set[str] = set()
        for c in self.checks:
            lines += ["", f"- **{'Passed' if c.get('passed') else 'Failed'}**: {c['what'].strip()}",
                      f"  - How: {one_line(c['how'])}", f"  - Saw: {one_line(c['saw'])[:MAX_SAW_CHARS]}"]
            shot = Path(c.get("screenshot") or "").name
            if shot in embedded:
                lines.append(f"  - Screenshot: `{shot}`, shown above")
            elif image_url and shot in image_url:
                lines.append(f"  - ![{shot}]({image_url[shot]})")
                embedded.add(shot)
            elif shot:
                lines.append(f"  - Screenshot: `{shot}` (in the run's evidence folder)")
        return "\n".join(lines)


def not_configured(layer_dir: Path, ws: Path) -> Verified:
    if layer_dir.is_relative_to(ws):
        return Verified("unverified", "Not checked in use: `.agents/config.toml` has no `[verify]` section.")
    return Verified("unverified", f"Not checked in use: arete reads this repo's layer from "
                                  f"`{paths.short_path(layer_dir)}`, outside the branch, and that layer has no "
                                  "`[verify]` section. A `.agents` folder on the branch is not used.")


def one_line(text: str) -> str:
    return " ".join((text or "").split())


def push_evidence(src: Path, github: str, run_id: str, files: list[Path]) -> dict[str, str]:
    if not files:
        return {}
    blobs = "".join(f"100644 blob {git(['hash-object', '-w', str(f)], src).strip()}\t{f.name}\n" for f in files)
    run_tree = git(["mktree"], src, input=blobs).strip()
    old = git(["ls-remote", "origin", f"refs/heads/{EVIDENCE_BRANCH}"], src).split()
    rows = ""
    if old:
        git(["fetch", "-q", "origin", f"refs/heads/{EVIDENCE_BRANCH}"], src)
        rows = "".join(f"{row}\n" for row in git(["ls-tree", old[0]], src).splitlines()
                       if not row.endswith(f"\t{run_id}"))
    root = git(["mktree"], src, input=rows + f"040000 tree {run_tree}\t{run_id}\n").strip()
    commit = util.git_as_owner(["commit-tree", root, *(["-p", old[0]] if old else []), "-m", f"evidence for run {run_id}"], src).strip()
    git(["push", "-q", "origin", f"{commit}:refs/heads/{EVIDENCE_BRANCH}"], src, timeout=300)
    return {f.name: f"https://github.com/{github}/blob/{commit}/{run_id}/{f.name}?raw=true" for f in files}
