from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import accounts, claude, config, devices, environment, paths, util
from .agent import (
    NO_SESSION,
    READ_ONLY_TOOLS,
    READ_TOOLS,
    Result,
    Session,
    json_from_text,
    usage_totals,
)

TOOLS_OF = {"Read": ("read",), "Glob": ("find", "ls"), "Grep": ("grep",), "LS": ("ls",), "Edit": ("edit",),
            "MultiEdit": ("edit",), "Write": ("write",), "Bash": ("bash",)}
WRITING = {"edit", "write", "bash"}
SESSIONS_DIR = paths.STATE_DIR / "pi-sessions"
PI_HOME = paths.HOME / ".pi"
AGENT_DIR = paths.STATE_DIR / "pi-agent"
KEYS_FILE = paths.PI_KEYS_FILE
GUARD_FILE = paths.PACKAGE_DIR / "pi_guard.mjs"
GUARDED_TOOLS = frozenset({"read", "edit", "write", "grep", "find", "ls"})
SHARED_SETTINGS = ("models.json", "settings.json")
SCHEMA_RULE = ("No tool takes this answer, so write it as the text of your last reply. Reply with only one JSON value "
               "that matches this JSON schema, in a ```json fence, and nothing after it:\n\n{schema}")
RESTATE = ("An agent finished a task but did not write its answer as the JSON it was asked for. Below is how its run "
           "ended: its last replies, the tool calls it tried, and its last thoughts when it ended on a thought. Write "
           "the answer it gave as that JSON, using only what it said.")
NO_ANSWER = "A tool call is not an answer. If the ending holds no answer, reply with only the words: no answer."
ENDING_REPLIES = 3
ENDING_CHARS = 12000
RESTATE_TIMEOUT = 600
WRITABLE_ALWAYS = ("/private/tmp", "/private/var/folders", "/dev")
NO_CONFINEMENT_REASON = "pi keeps file writes inside the work folder only with the macOS sandbox on this Mac"
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def binary() -> str:
    installed = sorted((paths.HOME / ".nvm" / "versions" / "node").glob("v*/bin/pi"), key=lambda p: p.parent.parent.name)
    return shutil.which("pi") or (str(installed[-1]) if installed else "pi")


def is_pattern_rule(rule: str) -> bool:
    return "(" in rule


def rules_for_throwaway_sandbox_without_git_credentials(disallowed_tools: list[str] | None) -> list[str]:
    return [t for t in disallowed_tools or [] if not is_pattern_rule(t)]


def tool_names(tools: list[str] | None, allowed_tools: list[str] | None, disallowed_tools: list[str] | None) -> list[str]:
    granted = tools if tools is not None else [*(allowed_tools or []), *READ_TOOLS]
    allowed = set(allowed_tools or []) | set(READ_TOOLS)
    denied = {t.split("(", 1)[0] for t in disallowed_tools or []}
    names: list[str] = []
    for tool in granted:
        if tool in denied or is_pattern_rule(tool):
            continue
        if tool not in allowed and tool not in READ_ONLY_TOOLS:
            continue
        names += [n for n in TOOLS_OF.get(tool, ()) if n not in names]
    return names


def agent_dir() -> Path:
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    for name in SHARED_SETTINGS:
        link, source = AGENT_DIR / name, PI_HOME / "agent" / name
        if source.exists() and not link.is_symlink():
            link.unlink(missing_ok=True)
            try:
                link.symlink_to(source)
            except FileExistsError:
                pass
    return AGENT_DIR


def default_provider() -> str:
    settings = util.read_json(PI_HOME / "agent" / "settings.json", {})
    provider = settings.get("defaultProvider") if isinstance(settings, dict) else ""
    return provider if isinstance(provider, str) else ""


def provider_names(provider: str, providers: dict[str, tuple[str, ...]]) -> list[str]:
    return [re.sub(r"\W", "_", provider).upper() + "_*", *providers.get(provider, ())]


def agent_env(model: str, passing: list[str] | None = None, providers: dict[str, tuple[str, ...]] | None = None,
              source=None) -> dict[str, str]:
    if passing is None or providers is None:
        cfg = config.load()
        passing = environment.passed(cfg) if passing is None else passing
        providers = cfg.environment.providers if providers is None else providers
    keys = util.read_env_file(KEYS_FILE)
    provider = model.split("/", 1)[0] if "/" in model else default_provider()
    names = [*passing, *(provider_names(provider, providers) if provider else keys)]
    return environment.build(names, {"ARETE_AGENT": "1", "PI_CODING_AGENT_DIR": str(AGENT_DIR)},
                             never=PARENT_SESSION_VARS, source={**(os.environ if source is None else source), **keys})


def guard_env(auth_paths: list[Path] | None = None) -> dict[str, str]:
    roots = [PI_HOME, AGENT_DIR, paths.CONFIG_DIR,
             *(paths.HOME / p for p in (".claude", ".codex", ".gemini", ".ssh", ".aws", ".docker", ".kube",
                                       ".gnupg", ".password-store", ".config/gh", ".config/gcloud",
                                       ".config/opencode", ".local/share/opencode")),
             *(Path(os.environ[p]) for p in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR")
               if os.environ.get(p)),
             Path("/proc"), Path("/dev")]
    auth = [PI_HOME / "agent" / "auth.json", AGENT_DIR / "auth.json", *(auth_paths or [])]
    files = [KEYS_FILE, paths.CONFIG_FILE,
             *(paths.HOME / p for p in (".netrc", ".npmrc", ".pypirc", ".git-credentials")),
             PI_HOME / "agent" / "models.json",
             PI_HOME / "agent" / "settings.json", paths.HOME / ".claude" / ".credentials.json",
             paths.HOME / ".codex" / "auth.json", paths.HOME / ".gemini" / "oauth_creds.json",
             paths.HOME / ".aws" / "credentials", paths.HOME / ".config" / "gh" / "hosts.yml",
             paths.HOME / ".config" / "gcloud" / "application_default_credentials.json",
             paths.HOME / ".config" / "opencode" / "auth.json",
             paths.HOME / ".local" / "share" / "opencode" / "auth.json"]
    for path in auth:
        files.extend((path, path.resolve(), Path(str(path) + ".lock"), Path(str(path.resolve()) + ".lock")))
    return {"ARETE_PI_GUARD_PATHS": json.dumps({"roots": list(map(str, roots)), "files": list(map(str, files)),
                                              "write_roots": [str(paths.PACKAGE_DIR)],
                                              "write_files": [str(GUARD_FILE)]})}


def can_confine(remote: bool) -> bool:
    return not remote and sys.platform == "darwin" and bool(shutil.which("sandbox-exec"))


def write_confinement(cwd: Path, add_dirs: list[Path]) -> list[str]:
    roots = [cwd, *add_dirs, AGENT_DIR, SESSIONS_DIR]
    allow = " ".join(f'(subpath (param "W{i}"))' for i in range(len(roots)))
    allow += " " + " ".join(f'(subpath "{p}")' for p in WRITABLE_ALWAYS)
    profile = f'(version 1)(allow default)(deny file-write* (subpath "/"))(allow file-write* {allow})'
    params = [arg for i, root in enumerate(roots) for arg in ("-D", f"W{i}={Path(root).resolve()}")]
    return ["sandbox-exec", *params, "-p", profile]


def sessions_dir(remote: devices.Device | None) -> str:
    return f"{devices.home(remote)}/.arete/pi-sessions" if remote else str(SESSIONS_DIR)


def command(names: list[str], *, model: str = "", effort: str = "", resume: Session = NO_SESSION, system: str = "",
            persist: bool = True, add_dirs: list[Path] | None = None,
            remote: devices.Device | None = None) -> list[str]:
    cmd = ["pi" if remote else binary(), "-p", "--mode", "json", "--no-context-files", "--no-skills", "--no-extensions",
           "--no-prompt-templates", "--no-themes", "--no-approve", "--no-mcp"]
    if not remote:
        cmd += ["--extension", str(GUARD_FILE)]
    cmd += ["--tools", ",".join(names)] if names else ["--no-tools"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--thinking", effort]
    if persist or resume.id:
        cmd += ["--session-dir", sessions_dir(remote)]
        if resume.id:
            cmd += ["--session", resume.id]
    else:
        cmd += ["--no-session"]
    if system:
        cmd += ["--append-system-prompt", system]
    if add_dirs:
        cmd += ["--append-system-prompt", "You may also read and change files under: " + ", ".join(map(str, add_dirs))]
    return cmd


def authentication(model: str, passing=None, providers=None, remote=None) -> accounts.Auth:
    if remote:
        return accounts.Auth("api_key")
    env = agent_env(model, passing, providers)
    provider = model.split("/", 1)[0] if "/" in model else default_provider()
    folder = Path(env["PI_CODING_AGENT_DIR"])
    credential = accounts.read(folder / "auth.json").get(provider)
    return accounts.pi(provider, credential, str(folder))


def run(*args, auth: accounts.Auth | None = None, **kw) -> Result:
    auth = auth or authentication(kw.get("model", ""), kw.get("passing"), kw.get("providers"), kw.get("remote"))
    started = time.monotonic()
    result = _run(*args, auth=auth, **kw)
    result.call_seconds = time.monotonic() - started
    if result.calls:
        result.calls[0].call_seconds = result.call_seconds - sum(call.call_seconds for call in result.calls[1:])
    result.auth_mode = auth.mode
    if not result.ok and auth.mode == "subscription":
        if auth.family == "claude":
            result.usage_limit = claude.usage_limit({}, result.error)
        elif auth.family == "codex":
            from . import codex
            result.usage_limit = codex.usage_limit(result.error)
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
    remote: devices.Device | None = None,
    instructions: list[str] | None = None,
    price: Callable[[Result], float] = lambda reply: reply.cost_usd,
    on_line: Callable[[str], None] = lambda line: None,
    passing: list[str] | None = None,
    providers: dict[str, tuple[str, ...]] | None = None,
    auth: accounts.Auth | None = None,
) -> Result:
    auth = auth if auth is not None else accounts.Auth("api_key")
    if mcp_servers(mcp_config):
        return Result(ok=False, error="pi takes MCP servers only from its own mcp.json, so run this step on claude",
                      session=resume, runner="pi", model=model)
    if instructions:
        return Result(ok=False, error="pi runs with --no-context-files and loads no instruction files, so run this "
                                      "step on claude or set instructions = []", session=resume, runner="pi", model=model)
    cwd = cwd if remote else Path(cwd).absolute()
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
    sandboxed = bool(remote and remote.exe_sandbox)
    if sandboxed:
        disallowed_tools = rules_for_throwaway_sandbox_without_git_credentials(disallowed_tools)
    names = tool_names(tools, allowed_tools, disallowed_tools)
    if not remote:
        names = [name for name in names if name in GUARDED_TOOLS]
    if (persist or resume.id) and not remote:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = command(names, model=model, effort=effort, resume=resume, system=system, persist=persist, add_dirs=add_dirs,
                  remote=remote)
    if json_schema:
        prompt = f"{prompt}\n\n{SCHEMA_RULE.format(schema=json.dumps(json_schema))}"
    if (writing := sorted(WRITING & set(names))) and not sandboxed:
        if not can_confine(bool(remote)):
            return Result(ok=False, error=f"{NO_CONFINEMENT_REASON}, so pi cannot {', '.join(writing)} here",
                          session=resume, runner="pi", model=model)
        cmd = write_confinement(cwd, add_dirs) + cmd

    cmd = [*cmd, "--", prompt]
    if remote:
        env = {**{k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}, "ARETE_AGENT": "1"}
        cmd = devices.wrap(remote, cmd, cwd, {"ARETE_AGENT": "1"}, mkdirs=(sessions_dir(remote),))
    else:
        agent_dir()
        env = {**agent_env(model, passing, providers), **guard_env()}
    try:
        proc, cut_short_by = stream(cmd, None if remote else str(cwd), env, timeout, max_budget_usd,
                                    lambda line: reply_cost(line, model, price, auth.mode), on_line)
    except FileNotFoundError as e:
        return Result(ok=False, session=resume, error=f"pi not found: {e}", runner="pi", model=model)
    if cut_short_by:
        return cut_short(proc, resume, model, cut_short_by)
    result = parse(proc, resume, model, json_schema)
    result.auth_mode = auth.mode
    if json_schema and result.ok and result.structured is None:
        restate(result, ending(proc.stdout), json_schema, model, cwd, remote, min(timeout, RESTATE_TIMEOUT), on_line,
                passing, providers, price,
                max(0, max_budget_usd - (0 if auth.mode == "subscription" else price(result)))
                if max_budget_usd is not None else None)
    return result


def ending(stdout: str) -> str:
    end = next((e for e in reversed(events(stdout)) if e.get("type") == "agent_end"), {})
    replies = [m for m in end.get("messages", []) if m.get("role") == "assistant"][-ENDING_REPLIES:]
    said = []
    for reply in replies:
        for block in reply.get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                said.append(block["text"].strip())
            elif block.get("type") == "toolCall":
                said.append(f"tool call {block.get('name')}: {json.dumps(block.get('arguments'))}")
    last_blocks = replies[-1].get("content", []) if replies else []
    if not any(b.get("type") == "text" and b.get("text", "").strip() for b in last_blocks):
        said += [f"its last thoughts, which it never wrote as a reply:\n{b['thinking'].strip()}" for b in last_blocks
                 if b.get("type") == "thinking" and b.get("thinking", "").strip()]
    return util.tail("\n\n".join(said), ENDING_CHARS)


def restate(result: Result, ending_text: str, json_schema: dict, model: str, cwd: Path | str,
            remote: devices.Device | None, timeout: float, on_line: Callable[[str], None],
            passing: list[str] | None = None, providers: dict[str, tuple[str, ...]] | None = None,
            price: Callable[[Result], float] = lambda reply: reply.cost_usd,
            max_budget_usd: float | None = None) -> None:
    if not ending_text:
        return
    auth = authentication(model, passing, providers, remote)
    if auth.mode != "subscription" and max_budget_usd is not None and max_budget_usd <= 0:
        return
    prompt = (f"{RESTATE}\n\n<ending>\n{ending_text}\n</ending>\n\n"
              f"{SCHEMA_RULE.format(schema=json.dumps(json_schema))}\n\n{NO_ANSWER}")
    again = run(prompt, cwd, model=model, tools=[], timeout=timeout, persist=False, remote=remote, on_line=on_line,
                passing=passing, providers=providers, auth=auth, price=price, max_budget_usd=max_budget_usd)
    result.calls = [replace(result, calls=[]), again]
    result.aggregate_paid_usd = result.paid_usd + again.paid_usd
    result.auth_modes = tuple(dict.fromkeys([*result.modes, *again.modes]))
    result.cost_usd += again.cost_usd
    result.tokens_in += again.tokens_in
    result.tokens_out += again.tokens_out
    result.cache_read += again.cache_read
    result.cache_write += again.cache_write
    result.structured = json_from_text(again.text) if again.ok else None


def mcp_servers(mcp_config: str) -> dict:
    try:
        return json.loads(mcp_config).get("mcpServers") or {} if mcp_config else {}
    except (json.JSONDecodeError, AttributeError):
        return {"unreadable": mcp_config}


def stream(cmd: list[str], cwd: str | None, env: dict, timeout: float, max_budget_usd: float | None,
           cost_of: Callable[[str], float],
           on_line: Callable[[str], None]) -> tuple[subprocess.CompletedProcess, str]:
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", env=env)
    stderr: list[str] = []
    reader = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
    reader.start()
    out_of_time = threading.Event()
    timer = threading.Timer(timeout, lambda: (out_of_time.set(), proc.kill()))
    timer.start()
    lines, spent, over_budget = [], 0.0, ""
    try:
        for line in proc.stdout:
            lines.append(line)
            on_line(line)
            if max_budget_usd and '"message_end"' in line:
                spent += cost_of(line)
                if spent > max_budget_usd:
                    over_budget = f"stopped at its ${max_budget_usd:g} budget after ${spent:.2f}"
                    proc.kill()
                    break
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    reader.join(5)
    done = subprocess.CompletedProcess(cmd, proc.returncode, "".join(lines), "".join(stderr))
    return done, over_budget or (f"timed out after {timeout:g}s" if out_of_time.is_set() else "")


def reply_cost(line: str, model: str, price: Callable[[Result], float], auth_mode: str = "") -> float:
    if auth_mode == "subscription":
        return 0.0
    try:
        message = json.loads(line).get("message") or {}
    except json.JSONDecodeError:
        return 0.0
    if message.get("role") != "assistant":
        return 0.0
    provider = message.get("provider", "")
    return price(Result(ok=True, runner="pi", provider=provider, auth_mode=auth_mode,
                        model=f"{provider}/{message.get('model')}" if provider else model,
                        **usage_totals([message.get("usage") or {}])))


def cut_short(proc: subprocess.CompletedProcess, resume: Session, model: str, reason: str) -> Result:
    stream = events(proc.stdout)
    replies = [e["message"] for e in stream
               if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"]
    totals = usage_totals([m.get("usage") or {} for m in replies])
    return Result(ok=False, session=session_after(stream, resume, totals["cost_usd"]), error=reason,
                  turns=len(replies), runner="pi", model=model, first_context_tokens=first_context_tokens(proc.stdout),
                  **totals)


def first_context_tokens(stream: str) -> int:
    first = next((e["message"] for e in events(stream)
                  if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"), None)
    return usage_totals([first.get("usage") or {}])["tokens_in"] if first else 0


def session_after(stream: list[dict], resume: Session, cost_usd: float) -> Session:
    session_id = next((e.get("id", "") for e in stream if e.get("type") == "session"), "") or resume.id
    return Session(session_id, resume.spent_usd + cost_usd, "pi") if session_id else resume


def events(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def parse(proc: subprocess.CompletedProcess, resume: Session, model: str, json_schema: dict | None) -> Result:
    stream = events(proc.stdout)
    end = next((e for e in reversed(stream) if e.get("type") == "agent_end"), {})
    replies = [m for m in end.get("messages", []) if m.get("role") == "assistant"]
    if not replies:
        error = util.tail(proc.stderr or proc.stdout or f"exit {proc.returncode}", 1500)
        return Result(ok=False, session=resume, error=error, runner="pi", model=model,
                      connection_failed=util.is_network_error(error))
    last = replies[-1]
    text = "".join(c.get("text", "") for c in last.get("content", []) if c.get("type") == "text")
    usages = [m.get("usage") or {} for m in replies]
    totals = usage_totals(usages)
    failed = last.get("stopReason") == "error" or proc.returncode != 0
    error = (last.get("errorMessage") or util.first_line(proc.stderr, 1500) or "pi stopped on an error") if failed else ""
    return Result(
        ok=not failed,
        text=text,
        session=session_after(stream, resume, totals["cost_usd"]),
        structured=json_from_text(text) if json_schema and not failed else None,
        turns=len(replies),
        error=error,
        connection_failed=failed and util.is_network_error(error),
        raw={"stopReason": last.get("stopReason"), "usage": usages[-1]},
        **totals,
        first_context_tokens=first_context_tokens(proc.stdout),
        model=f"{last.get('provider')}/{last.get('model')}" if last.get("provider") else model,
        provider=last.get("provider", ""),
        runner="pi",
    )
