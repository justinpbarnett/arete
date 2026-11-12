from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import background, config, devices, environment, paths, util
from .agent import (
    NO_SESSION,
    Result,
    Session,
    UsageLimit,
    claude_tokens,
    json_from_text,
)

PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_API_KEY")
LOGIN_VARS = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")
AGENT_SETS = {"ARETE_AGENT": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
PATH_RULE_OF = {"Read": "Read", "Glob": "Read", "Grep": "Read", "LS": "Read", "Edit": "Edit", "MultiEdit": "Edit",
                "Write": "Edit", "NotebookEdit": "Edit"}
GIT_WRITES_TO_ANY_PATH = "Bash(git * --output*)"
NO_MCP_SERVERS = json.dumps({"mcpServers": {}})
USAGE_LIMIT_TEXT = re.compile(r"(?:hit|reached) your [^.\n]{0,60}limit|usage limit reached", re.IGNORECASE)
LEGACY_LIMIT_RESET = re.compile(r"usage limit reached\|(\d{10,13})", re.IGNORECASE)
LIMIT_SCOPES = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
EVERY_MODEL = "claude"
LIMIT_SLACK_SECONDS = 60
LIMIT_RECHECK_SECONDS = 1800
MILLISECOND_STAMPS = 1e12
STOP_GRACE_SECONDS = 30
DRAIN_SECONDS = 10
EFFORTS = ("low", "medium", "high", "xhigh", "max")
SETTING_SOURCES = {"user": ("user",), "repo": ("project", "local")}


def binary() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def agent_env(passing: list[str] | None = None, extra: dict[str, str] | None = None, source=None) -> dict[str, str]:
    passing = environment.passed(config.load()) if passing is None else passing
    return environment.build([*LOGIN_VARS, *passing], {**AGENT_SETS, **(extra or {})}, never=PARENT_SESSION_VARS,
                             source=source)


def command(
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
    fresh_session: str = "",
    system: str = "",
    max_budget_usd: float | None = None,
    persist: bool = True,
    add_dirs: list[Path] | None = None,
    mcp_config: str = "",
    remote: devices.Device | None = None,
    settings: dict | None = None,
    instructions: list[str] | None = None,
) -> list[str]:
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
    cmd = ["claude" if remote else binary(), "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", permission_mode]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    tool_names = tool_set(tools, allowed_tools or [])
    cmd += ["--tools", ",".join(tool_names)]
    if allowed_tools:
        roots = [remote_root(cwd)] if remote else local_roots([cwd, *add_dirs])
        cmd += ["--allowedTools", *scoped_to(roots, allowed_tools)]
    cmd += ["--disallowedTools", GIT_WRITES_TO_ANY_PATH, *(disallowed_tools or [])]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if resume.id:
        cmd += ["--resume", resume.id]
    if fresh_session:
        cmd += ["--session-id", fresh_session]
    if "Bash" in tool_names:
        system = "\n\n".join(filter(None, [system, background.NOTE]))
        if not remote:
            settings = background.with_stop_hook(settings)
    if system:
        cmd += ["--append-system-prompt", system]
    if max_budget_usd:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if not persist:
        cmd += ["--no-session-persistence"]
    if add_dirs:
        cmd += ["--add-dir", *map(str, add_dirs)]
    cmd += ["--strict-mcp-config", "--mcp-config", mcp_config or NO_MCP_SERVERS]
    if instructions is not None and set(instructions) != set(SETTING_SOURCES):
        cmd += ["--setting-sources", ",".join(s for kind, sources in SETTING_SOURCES.items() if kind in instructions
                                              for s in sources)]
    if settings:
        cmd += ["--settings", json.dumps(settings)]
    return cmd


def run(
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
    settings: dict | None = None,
    instructions: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    passing: list[str] | None = None,
    on_line: Callable[[str], None] = lambda line: None,
) -> Result:
    fresh_session = "" if resume.id or not persist else str(uuid.uuid4())
    cmd = command(cwd, model=model, effort=effort, tools=tools, allowed_tools=allowed_tools,
                  disallowed_tools=disallowed_tools, permission_mode=permission_mode, json_schema=json_schema,
                  resume=resume, fresh_session=fresh_session, system=system, max_budget_usd=max_budget_usd,
                  persist=persist, add_dirs=add_dirs, mcp_config=mcp_config, remote=remote, settings=settings,
                  instructions=instructions)
    if remote:
        env = {**{k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}, **AGENT_SETS, **(extra_env or {})}
        cmd = devices.wrap(remote, ["env", "-u", "ANTHROPIC_API_KEY", *cmd], cwd, dict(AGENT_SETS))
    else:
        env = agent_env(passing, extra_env)
    try:
        proc = run_streaming(cmd, None if remote else str(cwd), prompt, env, timeout, on_line)
    except subprocess.TimeoutExpired:
        session = Session(fresh_session, 0.0, "claude") if fresh_session else resume
        return Result(ok=False, session=session, error=f"timed out after {timeout}s")
    except FileNotFoundError as e:
        return Result(ok=False, session=resume, error=f"claude not found: {e}")
    return parse(proc, resume, model, json_schema)


def parse(proc: subprocess.CompletedProcess, resume: Session, model: str, json_schema: dict | None) -> Result:
    out = proc.stdout.strip()
    if not (data := result_event(out)):
        error = util.tail(proc.stderr or out or f"exit {proc.returncode}", 1500)
        return Result(ok=False, session=resume, error=error,
                      usage_limit=usage_limit({}, error) if proc.returncode else None)

    structured = data.get("structured_output")
    if structured is None and json_schema:
        structured = json_from_text(data.get("result", ""))
    is_error = bool(data.get("is_error")) or data.get("subtype", "success") != "success"
    session = (Session(data["session_id"], float(data.get("total_cost_usd") or 0), "claude") if data.get("session_id")
               else resume)
    return Result(
        ok=not is_error and proc.returncode == 0,
        text=data.get("result", "") or "",
        session=session,
        structured=structured,
        cost_usd=max(0.0, session.spent_usd - resume.spent_usd),
        turns=int(data.get("num_turns") or 0),
        error=(failure_reason(data) if is_error else "") or (proc.stderr.strip()[:1500] if proc.returncode else ""),
        connection_failed=data.get("api_error") == "connection_failed",
        usage_limit=usage_limit(data, data.get("result", "") or "") if is_error else None,
        raw=data,
        **claude_tokens(data.get("usage") or {}),
        api_seconds=round(float(data.get("duration_api_ms") or 0) / 1000, 2),
        first_context_tokens=first_context_tokens(out),
        model=next(iter(data.get("modelUsage") or {}), model),
        provider="anthropic",
    )


def result_event(out: str) -> dict:
    for line in reversed(out.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return {}


def first_context_tokens(stream: str) -> int:
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
            return claude_tokens((event.get("message") or {}).get("usage") or {})["tokens_in"]
    return 0


def failure_reason(data: dict) -> str:
    if data.get("result"):
        return data["result"]
    subtype = "" if data.get("subtype", "success") == "success" else data["subtype"]
    return ": ".join(filter(None, [subtype, "; ".join(map(str, data.get("errors") or []))]))


def run_streaming(cmd: list[str], cwd: str | None, prompt: str, env: dict[str, str], timeout: float,
                  on_line: Callable[[str], None]) -> subprocess.CompletedProcess:
    child = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors="replace", env=env)
    out: list[str] = []
    err: list[str] = []
    helpers = [threading.Thread(target=feed, args=(child.stdin, prompt), daemon=True),
               threading.Thread(target=collect, args=(child.stdout, out, on_line), daemon=True),
               threading.Thread(target=collect, args=(child.stderr, err, lambda line: None), daemon=True)]
    for helper in helpers:
        helper.start()
    try:
        child.wait(timeout=timeout)
    except BaseException:
        stop(child)
        raise
    finally:
        for helper in helpers:
            helper.join(DRAIN_SECONDS)
    return subprocess.CompletedProcess(cmd, child.returncode, "".join(out), "".join(err))


def feed(stdin, prompt: str) -> None:
    with contextlib.suppress(OSError, ValueError), stdin:
        stdin.write(prompt)


def collect(stream, lines: list[str], on_line: Callable[[str], None]) -> None:
    with stream:
        for line in stream:
            lines.append(line)
            on_line(line)


def stop(child: subprocess.Popen) -> None:
    child.terminate()
    try:
        child.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        util.kill_tree(child.pid)
        child.wait()


def tool_set(tools: list[str] | None, rules: list[str]) -> list[str]:
    names = [rule.split("(", 1)[0] for rule in (rules if tools is None else tools)]
    built_in = [name for name in names if not name.startswith("mcp__")]
    if "Bash" in built_in:
        built_in.append("TaskStop")
    if any(rule.startswith("mcp__") for rule in rules):
        built_in.append("ToolSearch")
    return list(dict.fromkeys(built_in))


def usage_limit(data: dict, text: str) -> UsageLimit | None:
    if data.get("api_error") != "usage_limit_reached" and not USAGE_LIMIT_TEXT.search(text):
        return None
    info = (data.get("api_error_params") or {}).get("rate_limit_info") or {}
    legacy = LEGACY_LIMIT_RESET.search(text)
    resets = float(info.get("resetsAt") or (legacy.group(1) if legacy else 0))
    soonest = util.now() + timedelta(seconds=LIMIT_SLACK_SECONDS)
    if resets:
        stamp = resets / 1000 if resets > MILLISECOND_STAMPS else resets
        until = max(soonest, datetime.fromtimestamp(stamp, UTC) + timedelta(seconds=LIMIT_SLACK_SECONDS))
    else:
        until = util.now() + timedelta(seconds=LIMIT_RECHECK_SECONDS)
    kind = str(info.get("rateLimitType") or "")
    return UsageLimit(util.iso(until), LIMIT_SCOPES.get(kind, EVERY_MODEL), kind.replace("_", " "))


def local_roots(folders: list[Path | str]) -> list[str]:
    return list(dict.fromkeys(f"/{p}" for f in folders for p in (Path(f).absolute(), Path(f).resolve())))


def remote_root(folder: Path | str) -> str:
    text = str(folder).rstrip("/") or "/"
    if text.startswith("/"):
        return f"/{text}"
    if text == "~" or text.startswith("~/"):
        return text
    return "." if devices.HOME_DIR.fullmatch(text.partition("/")[0]) else f"~/{text}"


def scoped_to(roots: list[str], rules: list[str]) -> list[str]:
    scoped = []
    for rule in rules:
        family = PATH_RULE_OF.get(rule)
        scoped += [f"{family}({root}/**)" for root in roots] if family else [rule]
    return list(dict.fromkeys(scoped))


def context_tokens(session_id: str) -> int:
    for f in paths.CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        for line in reversed(f.read_text(errors="replace").splitlines()):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant" or not (usage := (entry.get("message") or {}).get("usage")):
                continue
            if tokens := sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                               "cache_creation_input_tokens")):
                return tokens
    return 0
