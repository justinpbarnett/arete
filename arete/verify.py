from __future__ import annotations

import http.client
import json
import os
import re
import signal
import socket
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, fields, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import config, environment, paths, scratch, util, vault
from .util import git

PLAYWRIGHT_MCP = "@playwright/mcp@0.0.75"
PLAYWRIGHT_TOOLS = tuple("browser_" + n for n in (
    "navigate", "navigate_back", "snapshot", "click", "type", "press_key", "hover", "select_option", "fill_form",
    "wait_for", "take_screenshot", "console_messages", "network_requests", "resize", "handle_dialog", "tabs", "close", "file_upload"))
PORT_FIELD = re.compile(r"\{(port\d*)\}")
SCRATCH_FIELD = "{scratch}"
STOP_GRACE_SECONDS = 10
HOP_BY_HOP = frozenset(("connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
                        "transfer-encoding", "upgrade"))
EVIDENCE_BRANCH = "arete/evidence"
MAX_SCREENSHOTS = 8
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_SAW_CHARS = 1200
VERDICTS = ("pass", "fail", "blocked")
ENVIRONMENT_FAULT = re.compile(r"[Oo]peration not permitted|\bEPERM\b|\[Errno 1\]|\bsandbox-exec\b")

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
                "cause": {"type": "string", "enum": ["change", "environment"]},
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


def free_port(avoid: frozenset[int] = frozenset()) -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid:
            return port


def port_refusal(v: config.Verify) -> str:
    if not v.start:
        return ""
    try:
        parts = urlsplit(v.url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return ""
    if port not in scratch.dashboard_ports():
        return ""
    return (f"The verify URL {v.url} uses port {port}, which the live dashboard or a running arete serve uses; "
            "use {port} so verification gets a free port.")


def with_free_ports(v: config.Verify) -> config.Verify:
    avoid = scratch.dashboard_ports()
    ports = {name: free_port(avoid) for name in dict.fromkeys(PORT_FIELD.findall(v.start + v.url + v.notes))}

    def filled(text: str) -> str:
        return PORT_FIELD.sub(lambda m: str(ports[m.group(1)]), text)

    return replace(v, start=filled(v.start), url=filled(v.url), notes=filled(v.notes))


def with_scratch(v: config.Verify, root: str) -> config.Verify:
    return replace(v, start=v.start.replace(SCRATCH_FIELD, root), notes=v.notes.replace(SCRATCH_FIELD, root))


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


def relay_server(url: str) -> ThreadingHTTPServer:
    target = urlsplit(url)
    connect = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection

    class Relay(BaseHTTPRequestHandler):
        def forward(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP | {"host"}}
            headers["Host"] = target.netloc
            try:
                upstream = connect(target.hostname, target.port)
                upstream.request(self.command, self.path, body, headers)
                resp = upstream.getresponse()
            except (OSError, http.client.HTTPException):
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"the relay could not reach the app")
                return
            try:
                self.send_response_only(resp.status, resp.reason)
                for key, value in resp.getheaders():
                    if key.lower() not in HOP_BY_HOP:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := resp.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                upstream.close()

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = forward

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    server.daemon_threads = True
    return server


@contextmanager
def relayed(url: str):
    server = relay_server(url)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def serving(v: config.Verify, cwd: Path, log: Path, cfg: config.Config | None = None,
            repo: config.Repo | None = None, sets: dict[str, str] | None = None, lifetime: dict | None = None,
            attempt: scratch.Attempt | None = None):
    if lifetime is not None:
        lifetime["stopped"] = True
    if not v.start:
        yield ""
        return
    url = v.url.rstrip("/")
    with relayed(url) if attempt and not scratch.loopback(url) else nullcontext(url) as reach:
        listen = None
        if attempt:
            app = scratch.app_port(attempt, reach)
            listen = scratch.listen_port(attempt, app, scratch.dashboard_ports())
            scratch.update_browser(attempt, listen=listen)
        command = scratch.service_command(attempt, v.start, listen) if attempt else v.start
        env, kept = environment.started(cfg or config.load(), repo, sets)
        if attempt:
            env = {k: v for k, v in env.items() if not paths.CREDENTIAL_NAME.search(k) or kept.get(k) == v}
            env.update({scratch.ENV_NAME: attempt.root, "ARETE_CONFIG_DIR": attempt.root + "/config",
                        "ARETE_STATE_DIR": attempt.root + "/state", "ARETE_LOG_DIR": attempt.root + "/state/logs",
                        "ARETE_WORKTREES_DIR": attempt.root + "/state/worktrees"})
        log.write_text("")
        with scratch.producing(attempt):
            proc = scratch.spawn(command, shell=isinstance(command, str), cwd=cwd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                 actor_confined=bool(attempt), **util.own_group_args())
        pump = threading.Thread(target=write_hidden, args=(proc, log, set(kept.values())), daemon=True)
        pump.start()
        if lifetime is not None:
            lifetime["stopped"] = False
        try:
            wait_until_ready(proc, url + "/" + v.ready.lstrip("/"), v.ready_seconds, log)
            yield reach
        finally:
            stop(proc)
            pump.join(STOP_GRACE_SECONDS)
            if lifetime is not None:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    lifetime["stopped"] = True
                except PermissionError:
                    pass


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


def write_hidden(proc: subprocess.Popen, log: Path, given: set[str]) -> None:
    try:
        with open(log, "a") as out:
            for line in proc.stdout:
                out.write(vault.hidden(line.decode(errors="replace"), given))
                out.flush()
    except (OSError, ValueError):
        pass


def environment_fault(error: str) -> str:
    lines = error.splitlines()
    if not lines or not lines[0].startswith("the start command exited"):
        return ""
    found = next((m for line in lines[1:] if (m := ENVIRONMENT_FAULT.search(line))), None)
    return found.group(0) if found else ""


def output(log: Path) -> str:
    text = vault.hidden(util.tail(log.read_text(errors="replace").strip(), 3000))
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
        if v.verdict == "fail" and v.failed_checks and all(str(c.get("cause") or "").strip().lower() == "environment" for c in v.failed_checks):
            v.verdict = "blocked"
            v.blocked_by = v.blocked_by or util.first_line(str(v.failed_checks[0].get("what") or "")
                                                           + ": " + str(v.failed_checks[0].get("saw") or ""), 300)
        return v

    @classmethod
    def stored(cls, data: dict) -> Verified:
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})

    @property
    def failed_checks(self) -> list[dict]:
        return [c for c in self.checks if not c.get("passed")]

    def screenshots(self, evidence: Path) -> list[Path]:
        names = [Path(c["screenshot"]).name for c in self.checks if (c.get("screenshot") or "").strip()]
        return [evidence / n for n in dict.fromkeys(names) if evidence_bytes(evidence, n, 0) is not None][:MAX_SCREENSHOTS]

    def requirements(self) -> list[dict]:
        return [{"what": str(c.get("what") or "").strip(), "passed": bool(c.get("passed")),
                 "evidence": self.evidence_of(c)} for c in self.checks]

    def evidence_of(self, check: dict) -> str:
        shot = Path(str(check.get("screenshot") or "")).name
        if self.saved and shot and evidence_bytes(Path(self.saved).parent / "evidence", shot, 0) is not None:
            return os.path.abspath(Path(self.saved).parent / "evidence" / shot)
        return self.saved

    def markdown(self, image_url: dict[str, str] | None = None) -> str:
        if self.verdict == "unverified":
            return self.summary
        lines = [f"**{self.verdict}**: {self.summary}"]
        if self.blocked_by:
            lines.append(f"\nBlocked by: {self.blocked_by}")
        embedded: set[str] = set()
        for c in self.checks:
            lines += ["", f"- **{'Passed' if c.get('passed') else 'Failed (environment)' if str(c.get('cause') or '').strip().lower() == 'environment' else 'Failed'}**: {c['what'].strip()}",
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


def evidence_bytes(folder: Path, name: str, limit: int = MAX_EVIDENCE_BYTES) -> bytes | None:
    try:
        with paths.directory_fd(Path(os.path.abspath(folder))) as parent:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return handle.read(limit + 1) if limit else b""


def push_evidence(src: Path, github: str, run_id: str, files: list[Path]) -> dict[str, str]:
    contents = {f: evidence_bytes(f.parent, f.name) for f in files}
    files = [f for f in files if contents[f] is not None and len(contents[f]) <= MAX_EVIDENCE_BYTES]
    if not files:
        return {}
    blobs = ""
    for f in files:
        command = scratch.protected_command(["git", "hash-object", "-w", "--stdin"], src)
        done = subprocess.run(command, check=False, cwd=src, input=contents[f], capture_output=True, timeout=300)
        if done.returncode != 0:
            raise RuntimeError(f"git hash-object: {util.git_error(done.stderr.decode(errors='replace'))}")
        blobs += f"100644 blob {done.stdout.decode().strip()}\t{f.name}\n"
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
