from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unicodedata import normalize

from . import accounts, browser, child, claude, config, environment, guard, paths, sandbox, uploads, util
from . import scratch as verification_scratch
from .agent import (
    NO_SESSION,
    Result,
    Session,
    UsageLimit,
    credit_limit,
    json_from_text,
    overload_limit,
    provider_failure_fields,
    unreachable_limit,
)
from .pi import SCHEMA_RULE

LIMIT_SCOPE = "codex"
LOGIN_VARS = ("CODEX_HOME",)
NEVER = (*claude.PARENT_SESSION_VARS, "CODEX_API_KEY", "OPENAI_API_KEY")
AGENT_SETS = {"ARETE_AGENT": "1"}
CHATGPT_PROVIDER = "chatgpt"
KEY_PROVIDER = "openai"
READ_PROFILE, WRITE_PROFILE = "read_only", "workspace_write"
PROFILE_BASE = {READ_PROFILE: ":read-only", WRITE_PROFILE: ":workspace"}
TEMP_NAMES = ("TMPDIR", "TMP", "TEMP")
LOG = logging.getLogger(__name__)
WRITING_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
READ_BASH = re.compile(r"git -C \S+ (?:status|diff|log|show)\*|arete session (?:start|list|report|stop) --chat \d+(?: \*)?")
FEATURES_OFF = ("plugins", "apps", "tool_suggest", "memories", "image_generation", "goals", "shell_snapshot", "multi_agent", "multi_agent_v2",
                "browser_use", "computer_use", "skill_search", "skill_mcp_dependency_install")
ALWAYS = ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--dangerously-bypass-hook-trust")
SETTINGS = {"approval_policy": "never", "web_search": "disabled", "allow_login_shell": False,
            "skills.include_instructions": False, "skills.bundled.enabled": False}
SHELL_WORDS = frozenset({"sh", "bash", "zsh", "dash", "fish"})
WRAPPER_WORDS = frozenset({"env", "command", "exec", "nice", "nohup", "time", "sudo", "xargs", "builtin"})
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
GIT_READS = frozenset({"status", "diff", "log", "show", "blame", "rev-parse", "ls-files", "ls-tree", "cat-file", "grep",
                       "describe", "shortlog", "merge-base", "rev-list", "name-rev", "check-ignore", "diff-tree",
                       "show-ref", "for-each-ref", "help", "version"})
BRANCH_LISTING = frozenset({"--show-current", "-a", "-r", "-v", "-vv", "--all", "--remotes", "--list"})
GIT_READ_FORMS = {"stash": ("list", "show"), "worktree": ("list",), "remote": ("-v", "show", "get-url"),
                  "config": ("--get", "--get-all", "--list", "-l")}
SHELL_BREAKS = ";|&\n()`{}"
SHELL_TOOLS = frozenset({"Bash", "shell", "shell_command", "exec_command"})
KEYCHAIN_COMMAND = re.compile(r"(?<![\w.-])security(?![\w.-])", re.IGNORECASE)
REDIRECT = re.compile(r"[0-9]*(?:<<<|<<-|<<|<>|<&|>>|>\||>&|[<>])|&>>?")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
FUNCTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
LITERAL_SHELL_WORD = re.compile(r"""(?:'[^']*'|"(?:[^"$`\\]|\\[\s\S])*"|\\[\s\S]|[^$`'"\\*?\[{}~<>()])+""")
DISPLAY_FORMAT = re.compile(r"(?!-)(?:[^%]|%[%bsdi])*")
SHELL_PREFIX = frozenset({"!", "{", "}", "if", "then", "elif", "else", "do", "while", "until"})
SHELL_WORD_BREAKS = " \t\n;|&()<>"
SCRIPT_TOOLS = {"awk", "gawk", "node", "osascript", "perl", "php", "python", "python3", "ruby", "sed"}
SED_PATTERN = r"/(?:\\[^\n]|[^/\\\n])*/"
SED_ADDRESS = rf"(?:[0-9]+|\$|{SED_PATTERN})"
SED_SEARCH = re.compile(SED_PATTERN + "p")
SED_LITERAL_SEARCH = re.compile(rf"\s*(?:{SED_ADDRESS}[ \t]*,[ \t]*(?:{SED_ADDRESS}|\+[0-9]+)[ \t]*p"
                                r"|/security/\s*(?:p|=|!d|\{\s*p\s*;?\s*\}))\s*")
AWK_SEARCH = re.compile(r'(?:/security/|\$0[ \t]*~[ \t]*/security/|index\(\$0,[ \t]*"security"\))'
                        r'(?:\s*\{\s*(?:print(?:[ \t]+(?:\$[0-9]+|F?NR(?:[ \t]*(?:,|"[: ]+")[ \t]*\$[0-9]+)?))?\s*;?\s*\}'
                        r'|(?P<count>[A-Za-z_][A-Za-z_0-9]*)[ \t]*\+\+\s*;?\s*\}\s*;?\s*END\s*\{'
                        r'\s*print[ \t]+(?P=count)(?:[ \t]*\+[ \t]*0)?\s*;?\s*\}))?\s*')
BACKTICK = re.compile(r"`((?:\\[\s\S]|[^`\\])*)`")
ANSI_C_QUOTE = re.compile(r"\$'((?:\\[\s\S]|[^'\\])*)'")
ANSI_C_ESCAPE = re.compile(rb"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|c(?:\\\\|[\x00-\x7f])|.)",
                         re.DOTALL)
ANSI_C_SIMPLE = dict(zip(b"abefnrtvE\\'\"?", b"\a\b\x1b\f\n\r\t\v\x1b\\'\"?", strict=True))
HEREDOC_EXPANSION = re.compile(r"\\(?:[$`\\]|\n)|\$\(|`")
COMMAND_OPTIONS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-R", "-r", "-t", "--user", "--group", "--chdir"},
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "time": {"-f", "-o", "--format", "--output"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter", "--replace",
             "--eof", "--max-lines", "--max-args", "--max-procs", "--max-chars"},
}
PATCH_PATHS = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE)
USAGE_LIMIT_TEXT = re.compile(r"usage limit|hit your [^.\n]{0,40}limit", re.IGNORECASE)
TRY_AT = re.compile(r"try again at ([^\n]+?)\.?\s*$", re.IGNORECASE | re.MULTILINE)
TRY_IN = re.compile(r"try again in ([^\n.!?]+)", re.IGNORECASE)
DURATIONS = re.compile(r"(\d+)\s*(day|hour|minute|min|second|sec)s?\b", re.IGNORECASE)
UNIT_SECONDS = {"day": 86400, "hour": 3600, "minute": 60, "min": 60, "second": 1, "sec": 1}
RESET_FORMATS = ("%b %d, %Y %I:%M %p", "%b %d %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%b %d, %I:%M %p", "%b %d %I:%M %p",
                 "%I:%M %p", "%H:%M")
ORDINAL = re.compile(r"(\d+)(?:st|nd|rd|th)\b")
KEPT_OUT_OF_GIT = ("Arete keeps git writes out of agent steps, and `{0}` is one. Leave the change in the working tree, "
                   "since arete commits and pushes it, and say in your summary what is left.")
RUNS_LOCALLY = "codex runs only on the local device, since it needs your codex login and its own sandbox; pick local"
OWNER_INSTRUCTIONS_REFUSAL = ("codex cannot exclude this login's global AGENTS.md instructions; the call is withheld "
                              "to keep your own setup out; pick claude or pi")
NOTES = ("read-only or workspace-write sandbox according to the step's tools; network off",
         "git writes and gh are refused by the guard, including shell command arrays and bash -lc",
         "all secret patterns and credential directories are excluded recursively, independently of file discovery",
         "repo instructions only; a login with global AGENTS.md instructions cannot be isolated and is refused",
         "only declared MCP servers/tools are enabled and preapproved; environment values stay out of argv",
         "under trusted scratch the only MCP server is the engine's pinned browser, confined by its own profile")
LEADING_WORD = re.compile(r"\w+=.*|-.*|\d+")


CLEANUP_TIMEOUT_SECONDS = 30
CLEANUP_FILE_NAME = "codex-cleanup.json"
CLEANUP_REMOVALS = ("rmdir", "rmtree", "tempdir")
SANDBOX_PLATFORMS = {"darwin": "macos", "linux": "linux"}
NESTED_SANDBOX = "codex sandbox could not start inside an existing sandbox; directory cleanup was not measured"
CLEANUP_PROBE = """
import json, os, shutil, tempfile
results = {}
def attempt(name, action):
    try:
        action()
        results[name] = "ok"
    except PermissionError:
        results[name] = "denied"
    except Exception as e:
        results[name] = "error: " + type(e).__name__
def tree():
    os.makedirs("tree/inner")
    shutil.rmtree("tree")
def temporary():
    with tempfile.TemporaryDirectory(dir="."):
        pass
attempt("mkdir", lambda: os.mkdir("made"))
attempt("rmdir", lambda: os.rmdir("made"))
attempt("rmtree", tree)
attempt("tempdir", temporary)
def reads():
    denied = 0
    for name in (".env", "nested/deep/.env"):
        try:
            open(name).read()
        except PermissionError:
            denied += 1
    return "denied" if denied == 2 else "readable"
try:
    results["secret"] = reads()
except Exception as e:
    results["secret"] = "error: " + type(e).__name__
print(json.dumps(results))
"""


@dataclass(frozen=True)
class Cleanup:
    status: str
    detail: str
    results: dict
    version: str = ""
    checked: str = ""


def cleanup_verdict(results: dict, version: str = "") -> Cleanup:
    secret = results.get("secret")
    if secret != "denied":
        return Cleanup("unchecked", f"the placeholder secret was not denied (secret: {secret or 'missing'})",
                       results, version)
    if results.get("mkdir") != "ok":
        return Cleanup("unchecked", f"the probe could not create a directory (mkdir: {results.get('mkdir', 'missing')})",
                       results, version)
    outcomes = [results.get(name) for name in CLEANUP_REMOVALS]
    if all(o == "ok" for o in outcomes):
        return Cleanup("able", "codex removes directories it creates under arete's workspace-write profile",
                       results, version)
    if all(o == "denied" for o in outcomes):
        return Cleanup(
            "denied",
            f"codex {version} cannot remove directories it creates under arete's workspace-write profile with recursive "
            "secret exclusions (EPERM on rmdir, rmtree, tempdir). This is a codex sandbox restriction, not a test or "
            "product failure; retrying the same verification on codex fails the same way, so steps that need directory "
            "cleanup take another configured route, or move the step off codex.", results, version)
    mixed = ", ".join(f"{name} {results.get(name, 'missing')}" for name in CLEANUP_REMOVALS)
    return Cleanup("unchecked", f"mixed results: {mixed}", results, version)


def cleanup_file() -> Path:
    return paths.STATE_DIR / CLEANUP_FILE_NAME


def probe_failure(found: subprocess.CompletedProcess, what: str) -> str:
    if found.returncode == util.TIMED_OUT:
        return f"timed out after {CLEANUP_TIMEOUT_SECONDS}s"
    if found.returncode == 127:
        return f"codex not found: {util.last_line(found.stderr)}"
    return f"{what} exited {found.returncode}: {util.last_line(found.stderr)}"


def run_cleanup_probe(version: str) -> Cleanup:
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="codex-cleanup-", dir=paths.STATE_DIR))
    try:
        for name in (".env", "nested/deep/.env"):
            (workspace / name).parent.mkdir(parents=True, exist_ok=True)
            (workspace / name).write_text("PLACEHOLDER=not-a-secret\n")
        cmd = [binary(), "sandbox", *profile(workspace, [], True, []), "--",
               sys.executable, "-I", "-c", CLEANUP_PROBE]
        try:
            proc = launch(cmd, workspace, "", {**agent_env(), "TMPDIR": str(workspace)}, CLEANUP_TIMEOUT_SECONDS,
                          lambda line: None)
        except subprocess.TimeoutExpired:
            return Cleanup("unchecked", f"timed out after {CLEANUP_TIMEOUT_SECONDS}s", {}, version)
        except FileNotFoundError as e:
            return Cleanup("unchecked", f"codex not found: {e}", {}, version)
        if proc.returncode != 0:
            if "sandbox_apply" in proc.stderr:
                return Cleanup("unchecked", NESTED_SANDBOX, {}, version)
            return Cleanup("unchecked", f"codex sandbox exited {proc.returncode}: {util.last_line(proc.stderr)}", {},
                           version)
        try:
            parsed = json.loads(util.last_line(proc.stdout, 2000))
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            return Cleanup("unchecked", "the probe printed no result", {}, version)
        return cleanup_verdict(parsed, version)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def cleanup() -> Cleanup:
    if sys.platform not in SANDBOX_PLATFORMS:
        return Cleanup("unchecked", f"codex sandbox is not available on {sys.platform}", {})
    found = util.sh([binary(), "--version"], timeout=CLEANUP_TIMEOUT_SECONDS, env=agent_env(), inherit=False)
    if found.returncode != 0:
        return Cleanup("unchecked", probe_failure(found, "codex --version"), {})
    words = found.stdout.split()
    version = words[-1] if words else ""
    key = {"version": version, "platform": sys.platform,
           "probe": hashlib.sha256(CLEANUP_PROBE.encode()).hexdigest()[:16]}
    saved = util.read_json(cleanup_file(), {})
    if (isinstance(saved, dict) and all(saved.get(k) == v for k, v in key.items())
            and saved.get("status") in ("able", "denied")):
        return Cleanup(saved["status"], saved.get("detail", ""), saved.get("results", {}), version,
                       saved.get("checked", ""))
    result = run_cleanup_probe(version)
    if result.status == "unchecked":
        return result
    result = Cleanup(result.status, result.detail, result.results, version, util.iso())
    util.write_json(cleanup_file(), {**key, "status": result.status, "detail": result.detail,
                                     "results": result.results, "checked": result.checked})
    return result


def binary() -> str:
    return shutil.which("codex") or "codex"


def home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or paths.HOME / ".codex")


def agent_env(passing: list[str] | None = None, extra: dict[str, str] | None = None, source=None, *,
              api_key: str = "") -> dict[str, str]:
    passing = [] if passing is None else passing
    env = {k: v for k, v in environment.build([*LOGIN_VARS, *passing], {**AGENT_SETS, **(extra or {})},
                                             never=NEVER, source=source).items() if k not in NEVER}
    if api_key:
        env["CODEX_API_KEY"] = api_key
    return env


def command_names(passing: list[str], extra: dict[str, str] | None = None) -> list[str]:
    return sorted(set(agent_env(passing, extra)) - {"CODEX_HOME"})


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)}={toml_value(v)}"
                               for k, v in value.items()) + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def setting(key: str, value) -> list[str]:
    return ["-c", f"{key}={toml_value(value)}"]


def writes(tools: list[str] | None, allowed_tools: list[str] | None) -> bool:
    rules = list(allowed_tools or [])
    names = [r.split("(", 1)[0] for r in rules] if tools is None else list(tools)
    if WRITING_TOOLS & set(names):
        return True
    bash_rules = [r for r in rules if r.startswith("Bash(")]
    if "Bash" in names and (tools is not None or "Bash" in rules) and not bash_rules:
        return True
    return any(not read_rule(r[5:-1]) for r in bash_rules)


def read_rule(pattern: str) -> bool:
    return pattern in config.DEFAULT_ALLOW_BASH or bool(READ_BASH.fullmatch(pattern))


def both(p: Path | str) -> list[str]:
    return list(dict.fromkeys([str(Path(p).absolute()), str(Path(p).resolve())]))


def kept_out(roots: list[Path], hidden: list[Path | str], floor: guard.Floor | None = None) -> dict[str, str]:
    floor = floor or guard.floor(os.environ.get("ARETE_RUN_ID", ""), roots[0] if roots else Path.cwd(), roots[1:])
    denied = {f"{root.rstrip('/')}/**/{name}": "deny" for r in [Path('/'), *roots, paths.HOME] for root in both(r)
              for name in (*sandbox.SECRET_NAMES, *sorted(sandbox.SECRET_DIRS))}
    denied |= {f"{p}/**": "deny" for p in denied if Path(p).name in sandbox.SECRET_DIRS}
    denied |= {path: "deny" for p in hidden for path in both(p)}
    denied |= {str(paths.HOME / d): "deny" for d in sorted(sandbox.SECRET_DIRS)}
    denied |= {p: "deny" for d in (*floor.closed, *floor.files) for p in both(d)}
    if gh := shutil.which("gh"):
        denied |= dict.fromkeys(both(gh), "deny")
    return denied


def grant_path(spelling: str) -> Path:
    return Path(normalize("NFD", os.path.normpath(spelling).casefold()))


def executables(allowed_tools: list[str] | None, hidden: list[Path | str]) -> dict[str, str]:
    granted = {}
    blocked = [home(), paths.HOME / ".claude", paths.HOME / ".pi", *hidden]
    if gh := shutil.which("gh"):
        blocked.append(Path(gh))
    blocked_paths = {grant_path(spelling) for path in blocked for spelling in both(path)}
    for rule in allowed_tools or []:
        if not (rule.startswith("Bash(") and rule.endswith(")")):
            continue
        try:
            words = shlex.split(rule[5:-1])
        except ValueError:
            continue
        if not words or not Path(words[0]).is_absolute() or any(c in words[0] for c in "*?["):
            continue
        path = Path(words[0])
        spellings = both(path)
        candidates = {grant_path(p) for p in spellings}
        if (not path.is_file() or not os.access(path, os.X_OK) or
                any(candidate.is_relative_to(root) for candidate in candidates for root in blocked_paths) or
                any(sandbox.secret_path(p) or p.name in ("auth.json", "config.toml", *sandbox.SECRET_DIRS)
                    for p in map(Path, spellings))):
            continue
        granted |= dict.fromkeys(spellings, "read")
    return granted


def profile(cwd: Path, add_dirs: list[Path], writing: bool, hidden: list[Path | str],
            allowed_tools: list[str] | None = None, *, floor: guard.Floor | None = None,
            temp_dir: Path | None = None, authority: tuple[Path, ...] | None = None) -> list[str]:
    floor = floor or guard.floor(os.environ.get("ARETE_RUN_ID", ""), cwd, add_dirs)
    name = WRITE_PROFILE if writing else READ_PROFILE
    granted = dict.fromkeys(both(cwd), "write" if writing else "read")
    if writing:
        granted |= {path: "write" for d in add_dirs for path in both(d)}
    elif temp_dir is not None:
        granted |= dict.fromkeys(both(temp_dir), "write")
    metadata = {path: "read" for root in [cwd, *add_dirs] for path in both(root / ".git")}
    result = util.sh(["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd=cwd,
                     timeout=10, env=agent_env(), inherit=False)
    if result.returncode == 0:
        metadata |= {path: "read" for p in result.stdout.splitlines() for path in both(p)}
    authority = sandbox.authority_roots() if authority is None else authority
    protected = [*hidden, *authority]
    denied = {grant_path(p) for root in authority for p in both(root)}
    opened = {p: rule for p, rule in (granted | metadata).items()
              if not any(grant_path(p).is_relative_to(root) for root in denied)}
    table = {"extends": PROFILE_BASE[name], "filesystem": {**opened,
             **kept_out([cwd, *add_dirs], protected, floor),
             **executables(allowed_tools, [*protected, *floor.closed, *floor.files])},
             "network": {"enabled": False}}
    return [*setting("default_permissions", name), "-c", f"permissions.{name}={toml_value(table)}"]


def owner_hooks_off() -> dict[str, dict]:
    path = home() / "hooks.json"
    try:
        data = util.read_json(path, {})
    except OSError as e:
        raise ValueError("codex cannot read this login's hooks to exclude them; the call is withheld") from e
    events = data.get("hooks") if isinstance(data, dict) else None
    off: dict[str, dict] = {}
    for event, groups in (events if isinstance(events, dict) else {}).items():
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", str(event)).lower()
        for g, group in enumerate(groups if isinstance(groups, list) else []):
            for h, _ in enumerate((group or {}).get("hooks") or [] if isinstance(group, dict) else []):
                off[f"{path}:{snake}:{g}:{h}"] = {"enabled": False}
    return off


def guard_hook(judge: bool, scratch: tuple[str, ...] = (), opened=()) -> list[str]:
    hook = shlex.join(paths.cli_command("codex-guard", *(["--judge"] if judge else []),
                                        *(arg for path in opened for arg in ("--open", str(path))), *scratch))
    entry = [{"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": guard.HOOK_TIMEOUT_SECONDS}]}]
    off = owner_hooks_off()
    return [*setting("hooks.PreToolUse", entry), *(setting("hooks.state", off) if off else [])]


def mcp_settings(mcp_config: str, allowed_tools: list[str] | None = None) -> list[str]:
    servers = (json.loads(mcp_config).get("mcpServers") or {}) if mcp_config else {}
    out: list[str] = []
    attempt = verification_scratch.PRODUCING.get()
    browser_wanted = bool(servers) and attempt is not None and verification_scratch.activated(attempt)
    if browser_wanted:
        try:
            expected = {"playwright": browser.browser_server(attempt)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise ValueError(f"browser confinement unavailable: {error}") from error
        if servers != expected:
            raise ValueError("under trusted scratch the only MCP server allowed is the engine's confined browser")
    for name, server in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            continue
        table = ({"url": server["url"]} if server.get("url") else
                 {"command": server.get("command", ""), "args": list(server.get("args") or [])})
        if browser_wanted:
            table["cwd"] = server["cwd"]
        elif "command" in table:
            confined = sandbox.protected_command([table["command"], *table["args"]])
            table["command"], table["args"] = confined[0], confined[1:]
        prefix = f"mcp__{name}"
        rules = set(allowed_tools or [])
        whole = prefix in rules or f"{prefix}__*" in rules
        tools = sorted(r.removeprefix(f"{prefix}__") for r in rules if r.startswith(f"{prefix}__") and "*" not in r)
        table["enabled"] = whole or bool(tools)
        table["default_tools_approval_mode"] = "approve" if whole else "prompt"
        if not whole:
            table["enabled_tools"] = tools
            table["tools"] = {tool: {"approval_mode": "approve"} for tool in tools}
        if server.get("env"):
            table["env_vars"] = list(server["env"])
        out += ["-c", f"mcp_servers.{name}={toml_value(table)}"]
    return out


def repo_instructions(cwd: Path) -> str:
    folders = []
    for folder in [cwd, *cwd.parents]:
        folders.append(folder)
        if (folder / ".git").exists():
            break
    else:
        folders = [cwd]
    root = folders[-1].resolve()
    found = []
    for folder in reversed(folders):
        for name in ("AGENTS.override.md", "AGENTS.md"):
            path = folder / name
            if not path.resolve().is_relative_to(root) or path.resolve().is_relative_to(home().resolve()):
                continue
            if path.is_file():
                found.append(f"Instructions from {path}:\n{path.read_text(errors='replace')}")
                break
    return "\n\n".join(found)


def command(
    cwd: Path,
    *,
    model: str = "",
    effort: str = "",
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    resume: Session = NO_SESSION,
    system: str = "",
    persist: bool = True,
    add_dirs: list[Path] | None = None,
    mcp_config: str = "",
    instructions: list[str] | None = None,
    judge: bool = False,
    passing: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    hidden: list[Path | str] = (),
    scratch: tuple[str, ...] = (),
    floor: guard.Floor | None = None,
    temp_dir: Path | None = None,
    authority: tuple[Path, ...] | None = None,
) -> list[str]:
    try:
        if any((home() / name).exists() for name in ("AGENTS.override.md", "AGENTS.md")):
            raise ValueError(OWNER_INSTRUCTIONS_REFUSAL)
    except OSError as e:
        raise ValueError(OWNER_INSTRUCTIONS_REFUSAL) from e
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
    floor = floor or guard.floor(os.environ.get("ARETE_RUN_ID", ""), cwd, add_dirs)
    servers = (json.loads(mcp_config).get("mcpServers") or {}) if mcp_config else {}
    resume = resume if resume.runner == "codex" else NO_SESSION
    cmd = [binary(), "exec", *(["resume", resume.id] if resume.id else []), "--json", *ALWAYS]
    if not resume.id:
        cmd += ["-C", str(cwd)]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += setting("model_reasoning_effort", effort)
    if not persist and not resume.id:
        cmd += ["--ephemeral"]
    for feature in FEATURES_OFF:
        cmd += ["--disable", feature]
    for key, value in SETTINGS.items():
        cmd += setting(key, value)
    cmd += profile(cwd, add_dirs, writes(tools, allowed_tools), list(hidden), allowed_tools, floor=floor,
                   temp_dir=temp_dir, authority=authority)
    shell_names = set(command_names(passing or [], extra_env)) - {
        k for server in servers.values() for k in (server.get("env") or {})}
    if temp_dir is not None:
        shell_names.update(TEMP_NAMES)
    cmd += setting("shell_environment_policy", {"inherit": "all", "ignore_default_excludes": True,
                                                "include_only": sorted(shell_names)})
    cmd += setting("project_doc_max_bytes", 0)
    if instructions is None or "repo" in instructions:
        system = "\n\n".join(filter(None, [system, repo_instructions(cwd)]))
    if system:
        cmd += setting("developer_instructions", system)
    cmd += guard_hook(judge, scratch, floor.open)
    cmd += mcp_settings(mcp_config, allowed_tools)
    return [*cmd, "-"]


def authentication(passing=None, extra=None, remote=None) -> accounts.Auth:
    if remote or paths.ISOLATED:
        return accounts.Auth()
    env = agent_env(passing, extra)
    folder = Path(env.get("CODEX_HOME") or Path(env.get("HOME", str(paths.HOME))) / ".codex")
    data = accounts.read(folder / "auth.json")
    try:
        provider = "" if data else login(env)[2]
    except (OSError, subprocess.SubprocessError):
        provider = ""
    return accounts.codex(data, str(folder), provider)


def run(*args, auth: accounts.Auth | None = None, **kw) -> Result:
    auth = auth or authentication(kw.get("passing"), kw.get("extra_env"), kw.get("remote"))
    with sandbox.WorkerLease() as activation:
        result = _run(*args, auth=auth, activation=activation, **kw)
    result.auth_mode = auth.mode
    if auth.mode not in ("subscription", "api_key"):
        result.usage_limit = None
    return result


def launch(cmd, cwd, prompt, env, timeout, on_line, *, hidden=(), scratch=(), add_dirs=None, mcp_config=""):
    attempt = verification_scratch.PRODUCING.get()
    protected = {Path(p).resolve() for p in hidden}
    confined = (attempt is None or not scratch or (sandbox.AUTHORITY.resolve() in protected
                     and paths.WORKTREES_DIR.resolve() in protected
                     and verification_scratch.isolation(attempt, "codex", False, add_dirs, mcp_config)))
    return child.stream(cmd, str(cwd), prompt, env, timeout, on_line, actor_confined=confined)


def remove_temp_dir(folder: Path) -> None:
    try:
        shutil.rmtree(folder)
    except OSError as e:
        LOG.warning("Could not remove Codex temporary directory %s: %s", folder, e)


def protected_temp_dir(folder: Path, roots: tuple[Path | str, ...]) -> bool:
    try:
        locations = [Path(path) for path in both(folder)]
        protected = {Path(path) for root in roots for path in both(root)}
        if any(location.is_relative_to(root) for location in locations for root in protected):
            return True
        identities = set()
        for root in protected:
            try:
                info = root.stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            identities.add((info.st_dev, info.st_ino))
        for ancestor in {p for location in locations for p in (location, *location.parents)}:
            info = ancestor.stat()
            if (info.st_dev, info.st_ino) in identities:
                return True
    except OSError:
        return True
    return False


def _run(
    prompt: str,
    cwd: Path | str,
    *,
    model: str = "",
    effort: str = "",
    tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    permission_mode: str = "dontAsk",
    json_schema: dict | None = None,
    resume: Session = NO_SESSION,
    system: str = "",
    max_budget_usd: float | None = None,
    timeout: float = 1800,
    persist: bool = True,
    add_dirs: list[Path] | None = None,
    mcp_config: str = "",
    remote=None,
    settings: dict | None = None,
    instructions: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    passing: list[str] | None = None,
    judge: bool = False,
    hidden: list[Path | str] = (),
    on_line: Callable[[str], None] = lambda line: None,
    auth: accounts.Auth | None = None,
    scratch: tuple[str, ...] = (),
    floor: guard.Floor | None = None,
    api_key: str = "",
    activation: sandbox.WorkerLease,
) -> Result:
    auth = auth if auth is not None else accounts.Auth()
    resume = resume if resume.runner == "codex" else NO_SESSION
    if remote:
        return Result(ok=False, session=resume, error=RUNS_LOCALLY, runner="codex", model=model)
    if instructions and "user" in instructions:
        return Result(ok=False, session=resume, error="codex loads no user instruction files; use instructions = "
                                                      "[\"repo\"] or []", runner="codex", model=model)
    cwd = Path(cwd).absolute()
    if blocked := isolation_problem(cwd, instructions):
        return Result(ok=False, session=resume, error=blocked, runner="codex", model=model)
    servers = (json.loads(mcp_config).get("mcpServers") or {}) if mcp_config else {}
    mcp_env = {}
    for server in servers.values():
        for key, value in (server.get("env") or {}).items():
            if key in mcp_env and mcp_env[key] != str(value):
                return Result(ok=False, session=resume, error="codex MCP servers require conflicting environment values",
                              runner="codex", model=model)
            mcp_env[key] = str(value)
    if set(mcp_env) & {*NEVER, *LOGIN_VARS, *environment.BASE, "OPENAI_API_KEY"}:
        return Result(ok=False, session=resume, error="codex MCP environment contains a reserved login variable",
                      runner="codex", model=model)
    env = {**agent_env(passing, extra_env, api_key=api_key), **mcp_env}
    temp_dir = None
    try:
        try:
            activation.acquire()
            authority = sandbox.worker_authority(activation)
            if not writes(tools, allowed_tools) and verification_scratch.PRODUCING.get() is None:
                temp_dir = Path(tempfile.mkdtemp(prefix="arete-codex-tmp-"))
                if protected_temp_dir(temp_dir, (cwd, *(add_dirs or []), *authority)):
                    remove_temp_dir(temp_dir)
                    temp_dir = None
            cmd = command(cwd, model=model, effort=effort, tools=tools, allowed_tools=allowed_tools, resume=resume,
                          system=system, persist=persist, add_dirs=add_dirs, mcp_config=mcp_config,
                          instructions=instructions, judge=judge, passing=passing, extra_env=extra_env, hidden=hidden,
                          scratch=scratch, floor=floor, temp_dir=temp_dir, authority=authority)
        except (ValueError, OSError) as e:
            return Result(ok=False, session=resume, error=str(e), runner="codex", model=model)
        if temp_dir is not None:
            env.update(dict.fromkeys(TEMP_NAMES, str(temp_dir)))
        if json_schema:
            prompt = f"{prompt}\n\n{SCHEMA_RULE.format(schema=json.dumps(json_schema))}"
        provider = KEY_PROVIDER if auth.mode == "api_key" else CHATGPT_PROVIDER
        before = thread_usage(resume.id)
        on_line(json.dumps({"arete": "command", "argv": cmd, "usage_before": before}) + "\n")
        session = resume

        def streamed(line: str) -> None:
            nonlocal session
            for event in events(line):
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    session = Session(str(event["thread_id"]), 0.0, "codex")
            on_line(line)

        try:
            proc = launch(activation.command(cmd), cwd, prompt, env, timeout, streamed, hidden=hidden, scratch=scratch,
                          add_dirs=add_dirs, mcp_config=mcp_config)
        except subprocess.TimeoutExpired:
            return Result(ok=False, session=session, error=f"timed out after {timeout}s", runner="codex", model=model,
                          provider=provider)
        except FileNotFoundError as e:
            return Result(ok=False, session=resume, error=f"codex not found: {e}", runner="codex", model=model)
        return parse(proc, resume, model, json_schema, provider, before)
    finally:
        if temp_dir is not None:
            remove_temp_dir(temp_dir)


def isolation_problem(cwd: Path, instructions: list[str] | None) -> str:
    for parent in [cwd, *cwd.parents]:
        if (parent / ".codex").resolve() in {home().resolve(), (paths.HOME / ".codex").resolve()}:
            continue
        if any((parent / ".codex" / n).exists() for n in ("config.toml", "hooks.json")):
            return "codex refuses project .codex config or hooks that can override the step's isolation"
    return ""



def thread_usage(thread: str) -> dict[str, int]:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", thread):
        return {}
    for rollout in sorted((home() / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl")):
        totals: dict = {}
        for line in rollout.read_text(errors="replace").splitlines():
            if '"token_count"' not in line:
                continue
            try:
                info = json.loads(line)["payload"]["info"] or {}
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            totals = info.get("total_token_usage") or totals
        return {k: int(v or 0) for k, v in totals.items()}
    return {}


def events(stdout: str) -> list[dict]:
    found = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found


def parse(proc: subprocess.CompletedProcess, resume: Session, model: str, json_schema: dict | None,
          provider: str, before: dict[str, int] | None = None) -> Result:
    thread, texts, failures, diagnostics, usage, turns = "", [], [], [], {}, 0
    for event in events(proc.stdout):
        kind = event.get("type")
        if kind == "thread.started":
            thread = str(event.get("thread_id") or "")
        elif kind == "item.completed" and isinstance(item := event.get("item"), dict):
            if item.get("type") == "agent_message":
                texts.append(str(item.get("text") or ""))
            turns += item.get("type") != "reasoning"
        elif kind == "turn.completed":
            usage = {k: max(0, int(v or 0) - (before or {}).get(k, 0)) for k, v in (event.get("usage") or {}).items()}
        elif kind in ("turn.failed", "error"):
            details = accounts.mapping(event.get("error")) if kind == "turn.failed" else event
            message = details.get("message")
            fallback = "the turn failed" if kind == "turn.failed" else "codex reported an error"
            failures.append(message if isinstance(message, str) and message else fallback)
            diagnostics.append(event)
    text = texts[-1] if texts else ""
    ok = proc.returncode == 0 and not failures and bool(thread)
    error = "" if ok else util.tail("\n".join(failures) or proc.stderr.strip() or f"exit {proc.returncode}", 1500)
    session = Session(thread, 0.0, "codex") if thread else resume
    limit = None
    if not ok:
        errors = "\n".join([*failures, proc.stderr])
        failure_text = [proc.stderr]
        for event in diagnostics:
            detail = event.get("error")
            failure_text.append(detail)
            failure_text.extend(provider_failure_fields(event))
            failure_text.extend(provider_failure_fields(accounts.mapping(detail)))
        if provider == KEY_PROVIDER:
            limit = (credit_limit([*diagnostics, proc.stderr]) or overload_limit(errors, model)
                     or unreachable_limit(failure_text, model))
        else:
            limit = usage_limit(errors, model, failure_text=failure_text)
    return Result(
        ok=ok,
        text=text,
        session=session,
        structured=json_from_text(text) if json_schema and text else None,
        turns=turns,
        error=error,
        connection_failed=not ok and util.is_network_error(error),
        usage_limit=limit,
        raw={"usage": usage, "thread_id": thread},
        tokens_in=usage.get("input_tokens", 0),
        tokens_out=usage.get("output_tokens", 0),
        cache_read=usage.get("cached_input_tokens", 0),
        cache_write=usage.get("cache_write_input_tokens", 0),
        model=model,
        provider=provider,
        runner="codex",
    )


def usage_limit(text: str, model: str = "", *, failure_text: list[str | int | None] | None = None) -> UsageLimit | None:
    if not USAGE_LIMIT_TEXT.search(text):
        return overload_limit(text, model) or unreachable_limit(text if failure_text is None else failure_text, model)
    soonest = util.now() + timedelta(seconds=claude.LIMIT_SLACK_SECONDS)
    resets = reset_time(text)
    until = (max(soonest, resets + timedelta(seconds=claude.LIMIT_SLACK_SECONDS)) if resets
             else util.now() + timedelta(seconds=claude.LIMIT_RECHECK_SECONDS))
    return UsageLimit(util.iso(until), LIMIT_SCOPE, "")


def reset_time(text: str) -> datetime | None:
    if found := TRY_IN.search(text):
        seconds = sum(int(amount) * UNIT_SECONDS[unit.lower()] for amount, unit in DURATIONS.findall(found.group(1)))
        if seconds:
            return util.now() + timedelta(seconds=seconds)
    if not (found := TRY_AT.search(text)):
        return None
    words = ORDINAL.sub(r"\1", found.group(1)).strip()
    now = datetime.now().astimezone()
    for form in RESET_FORMATS:
        try:
            read = datetime.strptime(words, form)
        except ValueError:
            continue
        if "%Y" not in form:
            read = read.replace(year=now.year) if any(m in form for m in ("%b", "%B")) else read.replace(year=now.year, month=now.month,
                                                                                 day=now.day)
        at = read.replace(tzinfo=now.tzinfo)
        if at < now and "%Y" not in form:
            at = at.replace(year=now.year + 1) if any(m in form for m in ("%b", "%B")) else at + timedelta(days=1)
        return at
    return None


def login(env: dict | None = None) -> tuple[bool, str, str]:
    if paths.ISOLATED:
        return False, paths.ISOLATED_LOGINS, ""
    res = util.sh([binary(), "login", "status"], timeout=10, env=env if env is not None else agent_env(), inherit=False)
    said = f"{res.stdout}\n{res.stderr}"
    if "ChatGPT" in said and res.returncode == 0:
        return True, "logged in with ChatGPT", CHATGPT_PROVIDER
    if "API key" in said and res.returncode == 0:
        return True, "logged in with an API key, so codex steps are metered", KEY_PROVIDER
    if "Not logged in" in said:
        return False, "codex CLI is not logged in; run `codex login`", ""
    return False, f"codex login status failed: exit {res.returncode}", ""


def git_write(command: str | list[str], rules: list[str], depth: int = 0) -> str:
    if depth > 12:
        return "nested shell command"
    if isinstance(command, list):
        command = shlex.join(command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_BREAKS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    try:
        for word in lexer:
            if word and all(c in SHELL_BREAKS for c in word):
                segments.append([])
            else:
                segments[-1].append(word)
    except ValueError:
        return "unparsed shell command"
    for words in segments:
        while words and (LEADING_WORD.fullmatch(words[0]) or Path(words[0]).name in WRAPPER_WORDS):
            words = words[1:]
        if not words:
            continue
        if Path(words[0]).name in SHELL_WORDS and (script := inline_shell(words[1:])) is not None:
            if found := git_write(script, rules, depth + 1):
                return found
        if denied := denied_git(words, rules):
            return denied
    return ""


def inline_shell(args: list[str], *, skip_values: bool = False) -> str | None:
    i = 0
    while i < len(args):
        option = args[i]
        if skip_values and option in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if option == "--" or not option.startswith("-"):
            break
        if option.startswith("--command="):
            return option.partition("=")[2]
        if option == "--command" or (not option.startswith("--") and "c" in option[1:]):
            return args[i + 1] if i + 1 < len(args) else None
        i += 1
    return None


def command_words(words: list[str]) -> list[str]:
    while words:
        head, *words = words
        tool = Path(head).name
        if tool not in WRAPPER_WORDS and tool != "timeout":
            return [head, *words]
        if tool == "command" and words[:1] in (["-v"], ["-V"]):
            return []
        takes_value = COMMAND_OPTIONS.get(tool, ())
        while words and words[0].startswith("-") and words[0] != "-":
            option, *words = words
            if option == "--":
                break
            if tool == "env" and option in ("-S", "--split-string") and words:
                words = shlex.split(words[0]) + words[1:]
                break
            if tool == "env" and option.startswith(("-S", "--split-string=")):
                words = shlex.split(option[2:] if option.startswith("-S") else option.partition("=")[2]) + words
                break
            if option in takes_value:
                words = words[1:]
        if tool == "timeout":
            words = words[1:]
        if tool == "env":
            while words and ASSIGNMENT.match(words[0]):
                words = words[1:]
    return []


def sed_search(args: list[str], literal: bool = False) -> bool:
    while args and args[0] in ("-n", "-E", "-r", "--quiet", "--silent", "--regexp-extended"):
        args = args[1:]
    while (literal and len(args) > 2 and args[0] in ("-e", "--expression", "-ne") and args[2] in ("-e", "--expression")
           and (SED_SEARCH.fullmatch(args[1]) or SED_LITERAL_SEARCH.fullmatch(args[1]))):
        args = args[2:]
    if args[:1] in (["-e"], ["--expression"], ["--"]) or literal and args[:1] == ["-ne"]:
        args = args[1:]
    return bool(args and all(not arg.startswith("-") for arg in args[1:])
                and (SED_SEARCH.fullmatch(args[0]) or literal and SED_LITERAL_SEARCH.fullmatch(args[0])))


def denied_keychain(words: list[str], depth: int, shell: str, *, literal: bool = False) -> str:
    if not words:
        return ""
    if depth > 12:
        return "nested shell command"
    tool, *args = words
    program = Path(tool).name.casefold()
    if program == "security":
        while args and args[0] in ("-q", "-v"):
            args = args[1:]
        if args and args[0] in ("verify-cert", "find-certificate"):
            return ""
        return guard.reach("security keychain items")
    if program in SHELL_WORDS and (script := inline_shell(args, skip_values=True)) is not None:
        return keychain_refusal(script, depth + 1, shell=tool)
    if tool == "eval":
        return keychain_refusal(" ".join(args), depth + 1, shell=shell)
    if program == "find":
        for index, arg in enumerate(args[:-1]):
            if arg in ("-exec", "-execdir", "-ok", "-okdir"):
                if reason := denied_keychain(command_words(args[index + 1:]), depth + 1, shell):
                    return reason
    if program in SCRIPT_TOOLS and not (program == "sed" and sed_search(args, literal)):
        if literal and program in ("awk", "gawk") and args[:1] == ["-e"]:
            args = args[1:]
        if literal and program in ("awk", "gawk") and args and AWK_SEARCH.fullmatch(args[0]) and all(
                not arg.startswith("-") for arg in args[1:]):
            return ""
        if any(KEYCHAIN_COMMAND.search(word) for word in words):
            return guard.reach("security keychain items")
    return ""


def ansi_c_escape(match: re.Match[bytes]) -> bytes:
    code = match[1]
    head = code[0]
    if head in ANSI_C_SIMPLE:
        return bytes([ANSI_C_SIMPLE[head]])
    if head in b"01234567":
        return bytes([int(code, 8) & 0xff])
    if len(code) == 1:
        return match[0]
    if head == ord("c"):
        return bytes([127 if code[1] == ord("?") else code[1] & 31])
    number = int(code[1:], 16)
    return bytes([number]) if head == ord("x") else chr(number).encode()


def path_uses_system_sh(search_path: str) -> bool:
    for directory in search_path.split(os.pathsep):
        if not os.path.isabs(directory):
            return False
        if executable := shutil.which("sh", path=directory):
            return executable == "/bin/sh"
    return False


def preserves_shell_lookup(words: list[str]) -> bool:
    if words[0] == "printf":
        return len(words) > 1 and DISPLAY_FORMAT.fullmatch(words[1]) is not None
    if words[0] == "set":
        return all(re.fullmatch(r"[-+][eufx]+", flag) for flag in words[1:]) or words[1:] == ["-o", "pipefail"]
    return words[0] in (":", "cd", "echo", "true", "false", "pwd")


class KeychainShell:
    def __init__(self, source: str, *, shell: str = "bash"):
        self.source = source
        self.shell = shell if shell == "/bin/sh" and sys.platform == "darwin" else Path(shell).name.casefold()
        self.pos = 0
        self.refusal = ""

    def char(self) -> str:
        return self.source[self.pos:self.pos + 1]

    def starts(self, text: str) -> bool:
        return self.source.startswith(text, self.pos)

    def escaped(self, special: str | None = None) -> str:
        self.pos += 1
        ch = self.char()
        if not ch:
            raise ValueError("trailing escape")
        if special is not None and ch not in special:
            return "\\"
        self.pos += 1
        return "" if ch == "\n" else ch

    def expansion(self, depth: int) -> str:
        start = self.pos
        if self.char() == "`":
            if not (match := BACKTICK.match(self.source, self.pos)):
                raise ValueError("unclosed backtick")
            self.pos = match.end()
            script = re.sub(r"\\([$`\\])", r"\1", match[1])
            self.refusal = self.refusal or keychain_refusal(script, depth + 1, shell=self.shell)
        elif not self.scan_arithmetic(depth + 1, "$(("):
            self.pos += 2
            self.commands(depth + 1, closing=True)
        return self.source[start:self.pos]

    def scan_arithmetic(self, depth: int, opening: str) -> bool:
        if not self.starts(opening):
            return False
        if depth > 12:
            raise ValueError("nested shell command")
        start, refusal = self.pos, self.refusal
        self.pos += len(opening)
        parens = 0
        while ch := self.char():
            if ch == "`" or self.starts("$("):
                self.expansion(depth)
            elif self.starts("$'"):
                self.ansi_quoted()
            elif ch in "'\"":
                self.quoted(depth, False)
            elif ch == "\\":
                self.escaped()
            elif ch == ")" and not parens:
                if self.starts("))"):
                    self.pos += 2
                    return True
                self.pos, self.refusal = start, refusal
                return False
            else:
                if ch == "(":
                    parens += 1
                elif ch == ")":
                    parens -= 1
                self.pos += 1
        raise ValueError("unclosed arithmetic expansion")

    def skip_blanks(self) -> None:
        while self.char() in (" ", "\t") or self.starts("\\\n"):
            self.pos += 2 if self.char() == "\\" else 1

    def take_literal_word(self, literal: str) -> bool:
        start = self.pos
        for ch in literal:
            if self.char() != ch:
                break
            self.pos += 1
            while self.starts("\\\n"):
                self.escaped()
        else:
            if not (tail := self.char()) or tail in SHELL_WORD_BREAKS:
                return True
        self.pos = start
        return False

    def compound_timing_prefix(self) -> bool:
        start = self.pos
        self.skip_blanks()
        if self.take_literal_word("-p"):
            self.skip_blanks()
        body = self.pos
        if self.char() == "(" or any(self.take_literal_word(word) for word in (*SHELL_PREFIX, "for", "time")):
            self.pos = body
            return True
        self.pos = start
        return False

    def scan_function_header(self) -> bool:
        start = self.pos
        self.skip_blanks()
        if name := FUNCTION_NAME.match(self.source, self.pos):
            self.pos = name.end()
            self.skip_blanks()
            if self.pos != name.end() and self.take_literal_word("{"):
                return True
        self.pos = start
        return False

    def scan_arithmetic_for(self, depth: int) -> bool:
        self.skip_blanks()
        if not self.starts("(("):
            return False
        if not self.scan_arithmetic(depth, "(("):
            raise ValueError("unclosed arithmetic for header")
        return True

    def ansi_quoted(self) -> str:
        if not (match := ANSI_C_QUOTE.match(self.source, self.pos)):
            raise ValueError("unclosed ANSI-C quote")
        self.pos = match.end()
        value = ANSI_C_ESCAPE.sub(ansi_c_escape, match[1].encode(errors="surrogateescape"))
        return value.partition(b"\0")[0].decode(errors="surrogateescape")

    def quoted(self, depth: int, delimiter: bool) -> str:
        quote = self.char()
        self.pos += 1
        start = self.pos
        if quote == "'":
            end = self.source.find(quote, start)
            if end < 0:
                raise ValueError("unclosed quote")
            self.pos = end + 1
            return self.source[start:end]
        value = []
        while (ch := self.char()) and ch != quote:
            if ch == "\\":
                value.append(self.escaped('$`"\\\n'))
            elif not delimiter and (ch == "`" or self.starts("$(")):
                value.append(self.expansion(depth))
            else:
                value.append(ch)
                self.pos += 1
        if not ch:
            raise ValueError("unclosed quote")
        self.pos += 1
        return "".join(value)

    def word(self, depth: int, *, delimiter: bool = False) -> str:
        value = []
        start = self.pos
        while ch := self.char():
            if not delimiter and (ch == "`" or self.starts("$(") or self.starts("<(") or self.starts(">(")):
                value.append(self.expansion(depth))
            elif self.starts("$'"):
                value.append(self.ansi_quoted())
            elif ch in "'\"":
                value.append(self.quoted(depth, delimiter))
            elif ch == "\\":
                value.append(self.escaped())
            elif ch in SHELL_WORD_BREAKS:
                break
            else:
                value.append(ch)
                self.pos += 1
        if self.pos == start:
            raise ValueError("expected shell word")
        return "".join(value)

    def line(self) -> str:
        end = self.source.find("\n", self.pos)
        if end < 0:
            end = len(self.source)
        text = self.source[self.pos:end]
        self.pos = min(end + 1, len(self.source))
        return text

    def heredoc(self, delimiter: str, strip_tabs: bool, expand: bool) -> str:
        lines = []
        while self.char():
            line = self.line()
            while expand and (len(line) - len(line.rstrip("\\"))) % 2 and self.char():
                line = line[:-1] + self.line()
            if strip_tabs:
                line = line.lstrip("\t")
            if line == delimiter:
                return "\n".join(lines)
            lines.append(line)
        raise ValueError("unclosed heredoc")

    def inspect_heredocs(self, docs: list[tuple[str, bool, bool, list[list[str]]]], depth: int) -> None:
        for delimiter, strip_tabs, expand, pipeline in docs:
            body = self.heredoc(delimiter, strip_tabs, expand)
            if expand:
                scanner = KeychainShell(body, shell=self.shell)
                while match := HEREDOC_EXPANSION.search(body, scanner.pos):
                    scanner.pos = match.start()
                    if scanner.char() == "\\":
                        scanner.pos = match.end()
                    else:
                        scanner.expansion(depth)
                self.refusal = self.refusal or scanner.refusal
            for words in pipeline or [[]]:
                program = Path(words[0]).name.casefold() if words else ""
                if program in SHELL_WORDS or program in ("", ".", "source", "eval"):
                    shell = words[0] if program in SHELL_WORDS else self.shell
                    self.refusal = self.refusal or keychain_refusal(body, depth + 1, shell=shell)
                elif program in SCRIPT_TOOLS and not (program == "sed" and sed_search(words[1:])):
                    if KEYCHAIN_COMMAND.search(body):
                        self.refusal = self.refusal or guard.reach("security keychain items")

    def commands(self, depth: int, *, closing: bool = False, initial_path: str = "") -> None:
        if depth > 12:
            raise ValueError("nested shell command")
        words: list[str] = []
        pipeline: list[list[str]] = []
        docs: list[tuple[str, bool, bool, list[list[str]]]] = []
        needs_consumer = False
        literal = True
        arithmetic = self.shell in ("bash", "zsh", "/bin/sh")

        def finish() -> None:
            nonlocal needs_consumer, initial_path
            if words:
                command = command_words(words)
                if command:
                    if initial_path and words[0] == "sh" and path_uses_system_sh(initial_path):
                        command[0] = "/bin/sh"
                    pipeline.append(command)
                if not preserves_shell_lookup(words):
                    initial_path = ""
                self.refusal = self.refusal or denied_keychain(command, depth, self.shell,
                                                               literal=literal and command == words)
                needs_consumer = False

        while ch := self.char():
            if ch in " \t" or self.starts("\\\n"):
                self.skip_blanks()
            elif ch == "#":
                end = self.source.find("\n", self.pos)
                self.pos = end if end >= 0 else len(self.source)
            elif not (self.starts("<(") or self.starts(">(")) and (redirect := REDIRECT.match(self.source, self.pos)):
                initial_path = ""
                self.pos = redirect.end()
                self.skip_blanks()
                operation = redirect[0].lstrip("0123456789")
                heredoc = operation in ("<<", "<<-")
                start = self.pos
                target = self.word(depth, delimiter=heredoc)
                if heredoc:
                    raw = self.source[start:self.pos].replace("\\\n", "")
                    docs.append((target, operation == "<<-", raw == target, pipeline))
            elif not words and arithmetic and self.scan_arithmetic(depth + 1, "(("):
                initial_path = ""
                pipeline.append([])
                needs_consumer = False
            elif ch in ";|&\n()":
                finish()
                words, literal = [], True
                boolean = self.starts("||") or self.starts("&&")
                self.pos += 2 if boolean or self.starts("|&") else 1
                if ch == "|" and not boolean:
                    needs_consumer = True
                elif ch == "(":
                    initial_path = ""
                    self.commands(depth + 1, closing=True)
                    pipeline.append([])
                    needs_consumer = False
                elif ch == ")" and closing:
                    if docs:
                        raise ValueError("unclosed heredoc")
                    return
                else:
                    if ch == "\n":
                        if docs and needs_consumer:
                            pipeline.append([])
                        self.inspect_heredocs(docs, depth)
                        docs.clear()
                    pipeline = []
                    needs_consumer = False
            else:
                start = self.pos
                word = self.word(depth)
                raw = self.source[start:self.pos].replace("\\\n", "")
                if not words and self.shell == "bash" and (
                        raw == "time" and self.compound_timing_prefix() or
                        raw == "function" and self.scan_function_header()):
                    initial_path = ""
                    continue
                if not words and arithmetic and raw == "for" and self.scan_arithmetic_for(depth + 1):
                    initial_path = ""
                    pipeline.append([])
                    needs_consumer = False
                elif words or (raw not in SHELL_PREFIX and not ASSIGNMENT.match(raw)):
                    literal = literal and LITERAL_SHELL_WORD.fullmatch(raw) is not None
                    if not literal:
                        initial_path = ""
                    words.append(word)
                else:
                    initial_path = ""
        if closing or docs:
            raise ValueError("unclosed shell group or heredoc")
        finish()


def keychain_refusal(command: str, depth: int = 0, *, shell: str = "bash", path: str | None = None) -> str:
    scanner = KeychainShell(command, shell=shell)
    search_path = os.environ.get("PATH", "") if path is None else path
    try:
        scanner.commands(depth, initial_path=search_path if depth == 0 and sys.platform == "darwin" else "")
    except ValueError:
        return scanner.refusal or "unparsed shell command"
    return scanner.refusal


def denied_git(words: list[str], rules: list[str]) -> str:
    tool, rest = Path(words[0]).name, words[1:]
    if tool == "gh":
        return "gh"
    if tool == "git":
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in GIT_VALUE_OPTIONS else rest[1:]
        if rest and not reads_git(rest[0], rest[1:]):
            return f"git {rest[0]}"
    for rule in rules:
        want = rule.removeprefix("Bash(").removesuffix(")").rstrip("*").split()
        if want and want[0] == tool and rest[:len(want) - 1] == want[1:]:
            return " ".join([tool, *rest[:len(want) - 1]])
    return ""


def reads_git(sub: str, args: list[str]) -> bool:
    if sub == "branch":
        return all(a in BRANCH_LISTING for a in args)
    return sub in GIT_READS or (bool(args) and args[0] in GIT_READ_FORMS.get(sub, ()))


def guard_reason(payload: dict, judge: bool, rules: list[str], opened=()) -> str:
    tool = str(payload.get("tool_name") or "").rsplit(".", 1)[-1]
    if tool.endswith(uploads.NAME):
        return uploads.CODEX_REFUSAL
    args = payload.get("tool_input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"command": args}
    if isinstance(args, list):
        args = {"command": args}
    command = args.get("command") or args.get("cmd") or args.get("input") or args.get("patch") or ""
    text = shlex.join(command) if isinstance(command, list) else str(command)
    tool = "Bash" if tool in SHELL_TOOLS else tool
    cwd = str(payload.get("cwd") or args.get("cwd") or args.get("workdir") or os.getcwd())
    payload = {**payload, "tool_name": tool, "tool_input": {**args, "command": text}, "cwd": cwd}
    floor = guard.floor(os.environ.get("ARETE_RUN_ID", ""), cwd, opened)
    if tool == "apply_patch":
        return next(filter(None, (guard.decide({"tool_name": "Write", "cwd": cwd,
                                               "tool_input": {"file_path": p.strip()}}, judge, floor)
                                  for p in PATCH_PATHS.findall(text))), "")
    if tool != "Bash" or not text.strip():
        return ""
    if reason := keychain_refusal(text):
        return reason
    if reason := guard.decide(payload, False, floor):
        return reason
    if re.search(r"(?:auth\.json|(?:\$CODEX_HOME|\$\{CODEX_HOME\}|~[/\\]\.codex))", text):
        return "Arete keeps Codex login files out of agent tools"
    if found := git_write(text, rules):
        return KEPT_OUT_OF_GIT.format(found)
    return guard.decide(payload, True, floor) if judge else ""


def guard_main(judge: bool, rules: list[str], opened=()) -> int:
    try:
        reason = guard_reason(json.load(sys.stdin), judge, rules, opened)
    except Exception:
        return 0
    return write_guard_decision(reason)


def write_guard_decision(reason):
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0
