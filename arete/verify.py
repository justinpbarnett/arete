from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, fields, replace
from fnmatch import fnmatchcase
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import SplitResult, urlsplit

from . import (
    _test_stacks, browser, config, credential_files, environment, paths, sandbox, scratch, tailnet, util, vault,
    verify_faults,
)
from .util import git

PLAYWRIGHT_TOOLS = tuple("browser_" + n for n in (
    "navigate", "navigate_back", "snapshot", "click", "type", "press_key", "hover", "select_option", "fill_form",
    "wait_for", "take_screenshot", "console_messages", "network_requests", "resize", "handle_dialog", "tabs", "close", "file_upload"))
PORT_FIELD = re.compile(r"\{(port\d*)\}")
SCRATCH_FIELD = "{scratch}"
HOP_BY_HOP = frozenset(("connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
                        "transfer-encoding", "upgrade"))
EVIDENCE_BRANCH = "arete/evidence"
MAX_SCREENSHOTS = 8
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024
MAX_SAW_CHARS = 1200
VERDICTS = ("pass", "fail", "blocked")
ENVIRONMENT_FAULT = re.compile(r"[Oo]peration not permitted|\bEPERM\b|\[Errno 1\]|\bsandbox-exec\b")
SHELL_QUOTING = r"""'[^'\n]*'|"(?:[^"\\\n]|\\.)*"|\\."""
SHELL_WORD_BREAKS = r"""\s'"\\;&|()<>"""
SHELL_LITERAL = rf"(?:[^{SHELL_WORD_BREAKS}*?]|{SHELL_QUOTING})"
ENV_RULE_PREFIX = re.compile(
    rf"(?![A-Za-z_][A-Za-z0-9_]*=)(?P<program>{SHELL_LITERAL}+)[ \t]+"
    rf"(?:-{SHELL_LITERAL}*[ \t]+)*(?P<env>env[ \t]+)?")
JOINED_TERMINAL_WILDCARD = re.compile(r"[^ \t]\*[ \t]*$")
ENV_ASSIGNMENT = re.compile(
    rf"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>(?:[^{SHELL_WORD_BREAKS}]|{SHELL_QUOTING})*)(?P<space>[ \t]+|$)")
EXAMPLE_SERVER_PORT = "51390"

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
        "unmeasured_requirements": {"type": "array", "items": {"type": "string"}},
        "blocked_by": {"type": "string"},
    },
    "required": ["verdict", "summary", "checks"],
}


class ServerError(RuntimeError):
    pass


def port_refusal(v: config.Verify) -> str:
    if not v.start:
        return ""
    try:
        parts = urlsplit(v.url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return ""
    if port not in paths.dashboard_ports():
        return ""
    return (f"The verify URL {v.url} uses port {port}, which the live dashboard or a running arete serve uses; "
            "use {port} so verification gets a free port.")


def with_free_ports(v: config.Verify) -> config.Verify:
    avoid = paths.dashboard_ports()
    ports = {name: browser.free_port(avoid) for name in dict.fromkeys(PORT_FIELD.findall(
        v.start + v.url + v.notes + " ".join((*v.probes.values(), *v.drivers.values()))))}

    def filled(text: str) -> str:
        return PORT_FIELD.sub(lambda m: str(ports[m.group(1)]), text)

    return replace(v, start=filled(v.start), url=filled(v.url), notes=filled(v.notes),
                   probes={name: filled(command) for name, command in v.probes.items()},
                   drivers={name: filled(command) for name, command in v.drivers.items()})


def with_scratch(v: config.Verify, root: str) -> config.Verify:
    return replace(v, start=v.start.replace(SCRATCH_FIELD, root), notes=v.notes.replace(SCRATCH_FIELD, root),
                   probes={name: command.replace(SCRATCH_FIELD, root) for name, command in v.probes.items()},
                   drivers={name: command.replace(SCRATCH_FIELD, root) for name, command in v.drivers.items()})


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
    if failed:
        failed["cause"] = "environment"
    return Verified("blocked" if failed else "pass", "Verification prerequisites are unavailable." if failed else
                    "Configured verification prerequisites passed.", checks,
                    blocked_by=f"{failed['what']}: {failed['saw']}" if failed else "", finished=not bool(failed),
                    behavior_reached=False)


def run_probes(v: config.Verify, cwd: Path, attempt: scratch.Attempt | None,
               *, journey_drivers: dict[str, str] | None = None,
               stop_on_driver_failure: bool = True) -> Verified | None:
    named = [(f"journey:{name}", v.drivers[driver]) for name, driver in sorted((journey_drivers or {}).items())]
    if not v.probes and not named:
        return None
    label = "verification commands" if named else "verification probes"
    if attempt is None:
        requested = " for " + ", ".join(name for name, _ in named) if named else ""
        return Verified("blocked", f"Configured {label} require trusted scratch confinement.",
                        blocked_by=f"{label} could not obtain scratch authority{requested}", finished=False)
    deadline, checks = time.monotonic() + v.probe_seconds, []
    failure, blocked_by = "", ""
    env = environment.build((_test_stacks.PREFIX_ENV,), scratch.attempt_env(attempt))
    env = {name: value for name, value in env.items() if not paths.CREDENTIAL_NAME.search(name)}

    def public(text):
        text = vault.hidden(text).replace(str(paths.HOME), "~")
        return text.replace(v.url, "<app>") if v.url else text

    for index, (name, command) in enumerate((*v.probes.items(), *named)):
        is_driver = index >= len(v.probes)
        replaying_driver = is_driver and not stop_on_driver_failure
        remaining = v.probe_seconds if replaying_driver else deadline - time.monotonic()
        cause = "change"
        try:
            if remaining <= 0:
                raise TimeoutError(f"{'verification command' if named else 'verification probe'} budget exhausted")
            confined = scratch.service_command(attempt, command)
            with scratch.producing(attempt):
                proc = scratch.spawn(confined, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                                     actor_confined=True, **util.own_group_args())
            try:
                out, err = proc.communicate(timeout=remaining)
                passed = proc.returncode == 0
                status = f"exit {proc.returncode}: "
                if is_driver:
                    output = public("\n".join(filter(None, (out, err))))
                    saw = status + util.tail(output, MAX_SAW_CHARS - len(status) - len("...\n"))
                    confinement_fault = err.startswith("sandbox-exec:")
                else:
                    saw = status + util.tail(out + err, MAX_SAW_CHARS)
                    confinement_fault = ENVIRONMENT_FAULT.search(saw)
                if not passed and (proc.returncode in (126, 127, util.TIMED_OUT) or confinement_fault):
                    cause = "environment"
            finally:
                util.stop_group(proc)
        except (OSError, TimeoutError, subprocess.TimeoutExpired) as error:
            passed, saw, cause = False, f"{type(error).__name__}: {error}", "environment"
            if is_driver and isinstance(error, subprocess.TimeoutExpired):
                saw = "Timed out waiting for the driver to finish."
        saw = public(saw)
        if is_driver:
            saw = saw[:MAX_SAW_CHARS]
        check = {"what": name, "how": public(command), "saw": saw, "passed": passed}
        if is_driver:
            check["driver"] = True
        if not passed:
            check["cause"] = cause
        checks.append(check)
        if not passed:
            blocker = f"{name}: {saw}"
            blocked = cause == "environment"
            summary = f"Configured {label} failed."
            if named:
                summary = f"Could not run {name}." if blocked else f"{name} failed."
            if not replaying_driver:
                return Verified("blocked" if blocked else "fail", summary, checks,
                                blocked_by=(blocker if named else f"probes: {blocker}") if blocked else "",
                                finished=not (named and blocked))
            if blocked:
                blocked_by = blocked_by or blocker
            else:
                failure = failure or summary
    if failure:
        return Verified("fail", failure, checks)
    if blocked_by:
        return Verified("blocked", "A configured journey driver could not run.", checks,
                        blocked_by=blocked_by, finished=False)
    return Verified("pass", f"Owner-configured {label} passed under scratch confinement.", checks)


def relay_server(url: str) -> ThreadingHTTPServer:
    target = urlsplit(url)
    secure: dict[str, SplitResult] = {}
    if target.scheme == "https":
        secure["via"] = target

    def connect(address: str, port: int | None) -> http.client.HTTPConnection:
        if "via" in secure and secure["via"] is not target:
            return util.NamedTlsConnection(address, port or 443, secure["via"].hostname)
        if target.scheme == "https":
            return http.client.HTTPSConnection(address, port)
        return http.client.HTTPConnection(address, port)

    class Relay(BaseHTTPRequestHandler):
        def forward(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP | {"host"}}
            headers["Host"] = secure["via"].netloc if "via" in secure else target.netloc
            plain = "via" not in secure
            try:
                upstream = connect(target.hostname, target.port)
                upstream.request(self.command, self.path, body, headers)
                resp = upstream.getresponse()
                if plain and (moved := util.https_upgrade(resp, target.port)):
                    upstream.close()
                    secure.setdefault("via", moved)
                    moved = secure["via"]
                    headers["Host"] = moved.netloc
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


@dataclass(frozen=True)
class DashboardSession:
    start: str
    cli: str
    url: str
    trust: ssl.SSLContext


def dashboard_tls():
    ca = os.environ.get("ARETE_VERIFY_DASHBOARD_CA")
    bundle = os.environ.get("ARETE_VERIFY_DASHBOARD_TLS")
    if not ca or not bundle:
        raise sandbox.ConfinementUnavailable(
            "Set ARETE_VERIFY_DASHBOARD_CA and ARETE_VERIFY_DASHBOARD_TLS for the configured scratch HTTPS dashboard.")
    try:
        pem = credential_files.read_private(Path(bundle), 65536)
        if not pem:
            raise ValueError("the private TLS bundle is missing or empty")
        public = Path(ca).read_bytes()
        trust = ssl.create_default_context(cadata=public.decode("ascii"))
    except (OSError, ValueError) as error:
        raise sandbox.ConfinementUnavailable(f"Scratch dashboard TLS material could not be loaded ({error}).") from None
    return pem, public, trust


@contextmanager
def dashboard_session(v: config.Verify, lifetime: dict):
    wrapper = dashboard_wrapper(v)
    if not wrapper:
        yield None
        return
    original = urlsplit(v.url)
    if original.port is None:
        raise sandbox.ConfinementUnavailable("The scratch dashboard verify URL needs an explicit port.")
    pem, ca, trust = dashboard_tls()
    url = f"https://{tailnet.VERIFICATION_ADDRESS}:{original.port}"
    root = Path(tempfile.mkdtemp(prefix=verify_faults.DASHBOARD_PREFIX)).resolve()
    private = root / "state" / tailnet.VERIFICATION_TLS
    try:
        dirs = {"HOME": "home", "XDG_CONFIG_HOME": "config-home", "XDG_STATE_HOME": "xdg", "XDG_DATA_HOME": "data",
                "XDG_CACHE_HOME": "cache", "TMPDIR": "tmp", "ARETE_CONFIG_DIR": "config",
                "ARETE_STATE_DIR": "state", "ARETE_LOG_DIR": "logs", "ARETE_WORKTREES_DIR": "worktrees"}
        for folder in dirs.values():
            (root / folder).mkdir(mode=0o700)
        (root / ".arete-owned").write_bytes(verify_faults.MARKER)
        public = root / "dashboard-ca.crt"
        public.write_bytes(ca)
        util.write_bytes_atomic(private, pem)
        cli = [wrapper, "/usr/bin/env", *(f"{key}={root / folder}" for key, folder in dirs.items()),
               f"SSL_CERT_FILE={public}", f"ARETE_SERVER={url}", "uv", "run", "arete"]
        start = shlex.join([*cli, "dashboard", "--port", str(original.port), "--verification-https"])
        reach = url + original.path.rstrip("/")
        yield DashboardSession(start, shlex.join(cli), reach, trust)
    finally:
        private.unlink(missing_ok=True)
        if lifetime["stopped"]:
            shutil.rmtree(root)


def dashboard_pairing(dash: DashboardSession, cwd: Path, env: dict, attempt: scratch.Attempt | None,
                      browser_port: int | None) -> str:
    command = dash.cli + " pair"
    command = scratch.service_command(attempt, command, browser_port) if attempt else command
    with scratch.producing(attempt):
        proc = scratch.spawn(command, shell=isinstance(command, str), cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, env=env, actor_confined=bool(attempt),
                             text=True, **util.own_group_args())
    try:
        output, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        util.stop_group(proc)
        raise ServerError("The scratch dashboard pairing command timed out.") from None
    origin = urlsplit(dash.url).netloc
    match = re.fullmatch(r"Pairing code ([0-9]{6})\n(https://" + re.escape(origin) + r"/pair#\1)\n", output)
    if proc.returncode or not match:
        raise ServerError(f"The scratch dashboard pairing command did not return a link (exit {proc.returncode}).")
    return match[2]


@contextmanager
def serving(v: config.Verify, cwd: Path, log: Path, cfg: config.Config | None = None,
            repo: config.Repo | None = None, lifetime: dict | None = None,
            attempt: scratch.Attempt | None = None):
    life = {} if lifetime is None else lifetime
    life["stopped"] = True
    if not v.start:
        yield ""
        return
    with dashboard_session(v, life) as dash:
        with listener(v, cwd, log, cfg, repo, life, attempt, dash) as url:
            yield url


@contextmanager
def listener(v, cwd, log, cfg, repo, lifetime, attempt, dash):
    url = dash.url if dash else v.url.rstrip("/")
    with relayed(url) if attempt and not browser.loopback(url) else nullcontext(url) as reach:
        listen = None
        if attempt:
            app = browser.app_port(attempt, reach)
            listen = browser.listen_port(attempt, app, paths.dashboard_ports())
            browser.update_browser(attempt, listen=listen)
        start = dash.start if dash else v.start
        command = scratch.service_command(attempt, start, listen) if attempt else start
        env, kept = (environment.build(()), {}) if dash else environment.started(cfg or config.load(), repo)
        if attempt and not dash:
            env = {k: v for k, v in env.items() if not paths.CREDENTIAL_NAME.search(k) or kept.get(k) == v}
            env.update(scratch.attempt_env(attempt))
        log.write_text("")
        with scratch.producing(attempt):
            proc = scratch.spawn(command, shell=isinstance(command, str), cwd=cwd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                 actor_confined=bool(attempt), **util.own_group_args())
        pump = threading.Thread(target=write_hidden, args=(proc, log, set(kept.values())), daemon=True)
        pump.start()
        lifetime["stopped"] = False
        try:
            ready = url + "/" + v.ready.lstrip("/")
            if dash:
                wait_until_ready(proc, ready, v.ready_seconds, log, tls=dash.trust)
                lifetime.update(dashboard_cli=dash.cli,
                                dashboard_pairing=partial(dashboard_pairing, dash, cwd, env, attempt, listen))
            else:
                wait_until_ready(proc, ready, v.ready_seconds, log)
            yield reach
        finally:
            util.stop_group(proc)
            pump.join(util.GROUP_STOP_SECONDS)
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                lifetime["stopped"] = True
            except PermissionError:
                pass


def dashboard_wrapper(v: config.Verify) -> str:
    try:
        wrapper, *command = shlex.split(v.start)
    except ValueError:
        return ""
    if command[:4] != ["uv", "run", "arete", "dashboard"]:
        return ""
    options = command[4:]
    if options and not (len(options) == 2 and options[0] == "--port"
                        or len(options) == 1 and options[0].startswith("--port=")):
        return ""
    if wrapper == "sandbox":
        for pattern in v.allow_bash:
            executable = pattern.removesuffix(" *")
            if trusted_wrapper(executable) and executable != "sandbox":
                wrapper = executable
                break
    return wrapper if trusted_wrapper(wrapper) else ""


def fault_note(v: config.Verify, lifetime: dict | None = None) -> str:
    if not (wrapper := dashboard_wrapper(v)):
        return ""
    cli = (lifetime or {}).get("dashboard_cli", shlex.quote(wrapper) + " uv run arete") + " verify-fault"
    return ("\n\n## Scratch fault controls\n\n"
            "Run these commands from the same worktree as the configured dashboard. Use the CLI to cause the fault, "
            "then observe the result with the browser tools. Do not use page scripts or write directly to the store.\n\n"
            f"- `{cli} arm --route ROUTE --status STATUS [--body TEXT] [--count COUNT]` replaces the next "
            "response on that route by default. Include the query string for an exact query match and quote the route. "
            "Omit the body to use the contract problem for a documented error status, or quote a JSON body as one argument. "
            "Pass a count for multiple responses.\n"
            f"- `{cli} drop-streams` closes the open event streams. New connections can open as usual.\n"
            f"- `{cli} event --kind KIND --run RUN_ID` emits an event for an existing scratch run.\n"
            f"- `{cli} clear` clears response faults and pending events.")


def trusted_wrapper(executable: str) -> bool:
    path = Path(executable)
    return executable == "sandbox" or (
        path.is_absolute() and path.parts[-3:] == ("verification-runtime", "harness", "sandbox"))


def env_example(allow_bash: list[str], url: str) -> str:
    for pattern in allow_bash:
        prefix = ENV_RULE_PREFIX.match(pattern)
        if not prefix or JOINED_TERMINAL_WILDCARD.search(pattern):
            continue
        program, = shlex.split(prefix["program"], comments=False)
        if Path(program).name == "env":
            continue
        args = pattern[prefix.end():]
        if args == "*":
            env = "" if prefix["env"] else "env "
            command = f"{prefix[0]}{env}ARETE_SERVER=<url> uv run arete task list"
        elif prefix["env"] and (example := env_rule_args(args, url)):
            command = prefix[0] + example
        else:
            continue
        if fnmatchcase(command, pattern):
            return f" For example, run `{command}`."
    return ""


def env_rule_args(args: str, url: str) -> str:
    rendered, end, has_wildcard = [], 0, False
    while assignment := ENV_ASSIGNMENT.match(args, end):
        value = assignment["value"]
        if "*" in value:
            has_wildcard = True
            value = env_rule_value(assignment["name"], value, url)
        rendered.append(f'{assignment["name"]}={value}{assignment["space"]}')
        end = assignment.end()
    if not has_wildcard:
        return ""
    return "".join(rendered) + args[end:].replace("*", "example")


def env_rule_value(name: str, pattern: str, url: str) -> str:
    server = name == "ARETE_SERVER"
    samples = (url, f"http://127.0.0.1:{EXAMPLE_SERVER_PORT}") if server else ("arete-placeholder-1",)
    for sample in filter(None, samples):
        shell_quoted = shlex.quote(sample)
        single_quoted = f"'{sample}'" if shell_quoted == sample else shell_quoted
        double_quoted = '"' + re.sub(r'(["\\$`])', r'\\\1', sample) + '"'
        for candidate in (shell_quoted, double_quoted, single_quoted):
            if fnmatchcase(candidate, pattern):
                return candidate
    return pattern.replace("*", EXAMPLE_SERVER_PORT if server else "example")


def wait_until_ready(proc: subprocess.Popen, url: str, seconds: int, log: Path, *, tls: ssl.SSLContext | None = None) -> None:
    open_url = urllib.request.urlopen
    if tls is not None:
        open_url = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                               urllib.request.HTTPSHandler(context=tls)).open
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ServerError(f"the start command exited {proc.returncode} before {url} answered{output(log)}")
        try:
            with open_url(url, timeout=5):
                return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return
        except urllib.error.URLError as e:
            if tls is not None and isinstance(e.reason, ssl.SSLCertVerificationError):
                raise sandbox.ConfinementUnavailable(f"Scratch dashboard TLS verification failed ({e.reason.verify_message}).") from None
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


@dataclass
class Verified:
    verdict: str
    summary: str = ""
    checks: list[dict] = field(default_factory=list)
    blocked_by: str = ""
    finished: bool = True
    saved: str = ""
    native_trees: dict[str, str] = field(default_factory=dict)
    behavior_reached: bool = True
    unmeasured_requirements: list[str] = field(default_factory=list)

    @classmethod
    def from_agent(cls, found: dict) -> Verified:
        said = found.get("verdict")
        checks = [{k: v for k, v in c.items() if k != "driver"} for c in found.get("checks", []) if isinstance(c, dict)]
        names = found.get("unmeasured_requirements")
        unmeasured = [name for name in names if isinstance(name, str)] if isinstance(names, list) else []
        v = cls(said.strip().lower() if isinstance(said, str) else "", found.get("summary", "").strip(),
                checks, found.get("blocked_by", "").strip(), unmeasured_requirements=unmeasured)
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

    def observed_requirements(self) -> set[str]:
        failed = self.failed_checks
        if self.verdict == "pass" and self.finished and not failed:
            unmeasured = {one_line(name).casefold() for name in self.unmeasured_requirements}
            passed = {one_line(c.get("what")) for c in self.checks if c.get("passed") is True}
            return {name for name in passed if name and name.casefold() not in unmeasured}
        return {one_line(c.get("what")) for c in failed
                if c.get("passed") is False and str(c.get("cause") or "").strip().lower() != "environment"} - {""}

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
            status = "Passed" if c.get("passed") else "Failed"
            if status == "Failed" and str(c.get("cause") or "").strip().lower() == "environment":
                status += " (environment)"
            if c.get("driver") is True:
                status = f"Driver {status}"
            lines += ["", f"- **{status}**: {c['what'].strip()}",
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
        command = sandbox.protected_command(["git", "hash-object", "-w", "--stdin"], src)
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
