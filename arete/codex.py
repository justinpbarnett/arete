from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from . import accounts, claude, config, environment, guard, paths, util
from .agent import NO_SESSION, Result, Session, UsageLimit, credit_limit, json_from_text
from .pi import SCHEMA_RULE

EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
LIMIT_SCOPE = "codex"
LOGIN_VARS = ("CODEX_HOME",)
NEVER = (*claude.PARENT_SESSION_VARS, "CODEX_API_KEY", "OPENAI_API_KEY")
AGENT_SETS = {"ARETE_AGENT": "1"}
CHATGPT_PROVIDER = "chatgpt"
KEY_PROVIDER = "openai"
READ_PROFILE, WRITE_PROFILE = "read_only", "workspace_write"
PROFILE_BASE = {READ_PROFILE: ":read-only", WRITE_PROFILE: ":workspace"}
WRITING_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
READ_BASH = re.compile(r"git -C \S+ (?:status|diff|log|show)\*")
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
         "only declared MCP servers/tools are enabled and preapproved; environment values stay out of argv")
LEADING_WORD = re.compile(r"\w+=.*|-.*|\d+")


def binary() -> str:
    return shutil.which("codex") or "codex"


def home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or paths.HOME / ".codex")


def agent_env(passing: list[str] | None = None, extra: dict[str, str] | None = None, source=None) -> dict[str, str]:
    passing = [] if passing is None else passing
    return {k: v for k, v in environment.build([*LOGIN_VARS, *passing], {**AGENT_SETS, **(extra or {})},
                                               never=NEVER, source=source).items() if k not in NEVER}


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


def kept_out(roots: list[Path], hidden: list[Path | str]) -> dict[str, str]:
    denied = {f"{root.rstrip('/')}/**/{name}": "deny" for r in [Path('/'), *roots, paths.HOME] for root in both(r)
              for name in (*guard.SECRET_NAMES, *sorted(guard.SECRET_DIRS))}
    denied |= {f"{p}/**": "deny" for p in denied if Path(p).name in guard.SECRET_DIRS}
    denied |= {path: "deny" for p in hidden for path in both(p)}
    denied |= {str(paths.HOME / d): "deny" for d in sorted(guard.SECRET_DIRS)}
    denied |= {p: "deny" for d in (home(), paths.CONFIG_DIR, paths.HOME / ".config" / "arete",
                                   paths.HOME / ".claude", paths.HOME / ".pi") for p in both(d)}
    if gh := shutil.which("gh"):
        denied |= {p: "deny" for p in both(gh)}
    return denied


def executables(allowed_tools: list[str] | None, hidden: list[Path | str]) -> dict[str, str]:
    granted = {}
    blocked = [home().resolve(), (paths.HOME / ".claude").resolve(), (paths.HOME / ".pi").resolve()]
    hidden_paths = {Path(p).resolve() for p in hidden}
    if gh := shutil.which("gh"):
        hidden_paths.add(Path(gh).resolve())
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
        resolved = path.resolve()
        if (not path.is_file() or not os.access(path, os.X_OK) or resolved in hidden_paths or
                any(resolved.is_relative_to(root) for root in blocked) or
                any(guard.secret_path(p) or p.name in ("auth.json", "config.toml") for p in (path, resolved))):
            continue
        granted |= {p: "read" for p in both(path)}
    return granted



def profile(cwd: Path, add_dirs: list[Path], writing: bool, hidden: list[Path | str],
            allowed_tools: list[str] | None = None) -> list[str]:
    name = WRITE_PROFILE if writing else READ_PROFILE
    granted = {path: "write" for d in add_dirs for path in both(d)} if writing else {}
    metadata = {path: "read" for root in [cwd, *add_dirs] for path in both(root / ".git")}
    result = util.sh(["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd=cwd,
                     timeout=10, env=agent_env(), inherit=False)
    if result.returncode == 0:
        metadata |= {path: "read" for p in result.stdout.splitlines() for path in both(p)}
    table = {"extends": PROFILE_BASE[name], "filesystem": {**granted, **metadata,
             **kept_out([cwd, *add_dirs], hidden), **executables(allowed_tools, hidden)},
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


def guard_hook(judge: bool, scratch: tuple[str, ...] = ()) -> list[str]:
    entrypoint = f"import sys; sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); from arete.cli import main; raise SystemExit(main())"
    hook = shlex.join([sys.executable, "-I", "-c", entrypoint, "codex-guard", *(["--judge"] if judge else []), *scratch])
    entry = [{"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": guard.HOOK_TIMEOUT_SECONDS}]}]
    off = owner_hooks_off()
    return [*setting("hooks.PreToolUse", entry), *(setting("hooks.state", off) if off else [])]


def mcp_settings(mcp_config: str, allowed_tools: list[str] | None = None) -> list[str]:
    servers = (json.loads(mcp_config).get("mcpServers") or {}) if mcp_config else {}
    out: list[str] = []
    for name, server in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            continue
        table = ({"url": server["url"]} if server.get("url") else
                 {"command": server.get("command", ""), "args": list(server.get("args") or [])})
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
) -> list[str]:
    try:
        if any((home() / name).exists() for name in ("AGENTS.override.md", "AGENTS.md")):
            raise ValueError(OWNER_INSTRUCTIONS_REFUSAL)
    except OSError as e:
        raise ValueError(OWNER_INSTRUCTIONS_REFUSAL) from e
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
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
    cmd += profile(cwd, add_dirs, writes(tools, allowed_tools), list(hidden), allowed_tools)
    cmd += setting("shell_environment_policy", {"inherit": "all", "ignore_default_excludes": True,
                                                "include_only": sorted(set(command_names(passing or [], extra_env)) -
                                                    {k for server in servers.values() for k in (server.get("env") or {})})})
    cmd += setting("project_doc_max_bytes", 0)
    if instructions is None or "repo" in instructions:
        system = "\n\n".join(filter(None, [system, repo_instructions(cwd)]))
    if system:
        cmd += setting("developer_instructions", system)
    cmd += guard_hook(judge, scratch)
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
    result = _run(*args, auth=auth, **kw)
    result.auth_mode = auth.mode
    if auth.mode not in ("subscription", "api_key"):
        result.usage_limit = None
    return result


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
    env = {**agent_env(passing, extra_env), **mcp_env}
    try:
        cmd = command(cwd, model=model, effort=effort, tools=tools, allowed_tools=allowed_tools, resume=resume,
                      system=system, persist=persist, add_dirs=add_dirs, mcp_config=mcp_config, instructions=instructions,
                      judge=judge, passing=passing, extra_env=extra_env, hidden=hidden, scratch=scratch)
    except ValueError as e:
        return Result(ok=False, session=resume, error=str(e), runner="codex", model=model)
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
        proc = claude.run_streaming(cmd, str(cwd), prompt, env, timeout, streamed)
    except subprocess.TimeoutExpired:
        return Result(ok=False, session=session, error=f"timed out after {timeout}s", runner="codex", model=model,
                      provider=provider)
    except FileNotFoundError as e:
        return Result(ok=False, session=resume, error=f"codex not found: {e}", runner="codex", model=model)
    return parse(proc, resume, model, json_schema, provider, before)


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
        elif kind == "turn.failed":
            failures.append(str((event.get("error") or {}).get("message") or "the turn failed"))
            diagnostics.append(event)
        elif kind == "error":
            failures.append(str(event.get("message") or "codex reported an error"))
            diagnostics.append(event)
    text = texts[-1] if texts else ""
    ok = proc.returncode == 0 and not failures and bool(thread)
    error = "" if ok else util.tail("\n".join(failures) or proc.stderr.strip() or f"exit {proc.returncode}", 1500)
    session = Session(thread, 0.0, "codex") if thread else resume
    return Result(
        ok=ok,
        text=text,
        session=session,
        structured=json_from_text(text) if json_schema and text else None,
        turns=turns,
        error=error,
        connection_failed=not ok and util.is_network_error(error),
        usage_limit=None if ok else (credit_limit([*diagnostics, proc.stderr]) if provider == KEY_PROVIDER else
                                    usage_limit("\n".join([*failures, proc.stderr]))),
        raw={"usage": usage, "thread_id": thread},
        tokens_in=usage.get("input_tokens", 0),
        tokens_out=usage.get("output_tokens", 0),
        cache_read=usage.get("cached_input_tokens", 0),
        cache_write=usage.get("cache_write_input_tokens", 0),
        model=model,
        provider=provider,
        runner="codex",
    )


def usage_limit(text: str) -> UsageLimit | None:
    if not USAGE_LIMIT_TEXT.search(text):
        return None
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


def login_provider() -> str:
    return KEY_PROVIDER if authentication().mode == "api_key" else CHATGPT_PROVIDER


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
        if Path(words[0]).name in SHELL_WORDS:
            for i, option in enumerate(words[1:], 1):
                if option == "--" or not option.startswith("-"):
                    break
                if option.startswith("--command="):
                    if found := git_write(option.partition("=")[2], rules, depth + 1):
                        return found
                    break
                if option == "--command" or (not option.startswith("--") and "c" in option[1:]):
                    if i + 1 < len(words) and (found := git_write(words[i + 1], rules, depth + 1)):
                        return found
                    break
        if denied := denied_git(words, rules):
            return denied
    return ""


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


def guard_reason(payload: dict, judge: bool, rules: list[str]) -> str:
    tool = str(payload.get("tool_name") or "").rsplit(".", 1)[-1]
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
    if tool == "apply_patch":
        return next(filter(None, (guard.decide({"tool_name": "Write", "cwd": cwd,
                                               "tool_input": {"file_path": p.strip()}})
                                  for p in PATCH_PATHS.findall(text))), "")
    if tool != "Bash" or not text.strip():
        return ""
    if re.search(r"(?:auth\.json|(?:\$CODEX_HOME|\$\{CODEX_HOME\}|~[/\\]\.codex))", text):
        return "Arete keeps Codex login files out of agent tools"
    if found := git_write(text, rules):
        return KEPT_OUT_OF_GIT.format(found)
    if judge:
        return guard.decide(payload)
    secret = guard.secret_named(text, str(payload.get("cwd") or os.getcwd()))
    return guard.kept_out(tool, secret) if secret else ""


def guard_main(judge: bool, rules: list[str]) -> int:
    try:
        reason = guard_reason(json.load(sys.stdin), judge, rules)
    except Exception:
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0
