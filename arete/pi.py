from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import devices, paths, util
from .agent import NO_SESSION, Result, Session, json_from_text

TOOLS_OF = {"Read": ("read",), "Glob": ("find", "ls"), "Grep": ("grep",), "LS": ("ls",), "Edit": ("edit",),
            "MultiEdit": ("edit",), "Write": ("write",), "Bash": ("bash",)}
READ_ONLY = ("read", "grep", "find", "ls")
WRITING = {"edit", "write", "bash"}
SESSIONS_DIR = paths.STATE_DIR / "pi-sessions"
PI_HOME = paths.HOME / ".pi"
SCHEMA_RULE = ("Reply with only one JSON value that matches this JSON schema, in a ```json fence, and nothing after "
               "it:\n\n{schema}")
WRITABLE_ALWAYS = ("/private/tmp", "/private/var/folders", "/dev")


def binary() -> str:
    installed = sorted((paths.HOME / ".nvm" / "versions" / "node").glob("v*/bin/pi"), key=lambda p: p.parent.parent.name)
    return shutil.which("pi") or (str(installed[-1]) if installed else "pi")


def tool_names(tools: list[str] | None, allowed_tools: list[str] | None, disallowed_tools: list[str] | None) -> list[str]:
    granted = tools if tools is not None else [*(allowed_tools or []), "Read", "Glob", "Grep"]
    allowed = set(allowed_tools or []) | {"Read", "Glob", "Grep"}
    denied = {t for t in disallowed_tools or [] if "(" not in t}
    names: list[str] = []
    for tool in granted:
        if tool in denied or "(" in tool:
            continue
        if tool not in allowed and tool not in ("Read", "Glob", "Grep", "LS"):
            continue
        names += [n for n in TOOLS_OF.get(tool, ()) if n not in names]
    return names


def write_confinement(cwd: Path, add_dirs: list[Path]) -> list[str]:
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        return []
    roots = [cwd, *add_dirs, PI_HOME, SESSIONS_DIR]
    allow = " ".join(f'(subpath (param "W{i}"))' for i in range(len(roots)))
    allow += " " + " ".join(f'(subpath "{p}")' for p in WRITABLE_ALWAYS)
    profile = f'(version 1)(allow default)(deny file-write* (subpath "/"))(allow file-write* {allow})'
    params = [arg for i, root in enumerate(roots) for arg in ("-D", f"W{i}={Path(root).resolve()}")]
    return ["sandbox-exec", *params, "-p", profile]


def run(
    prompt: str,
    cwd: Path | str,
    *,
    model: str = "",
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
) -> Result:
    cwd = cwd if remote else Path(cwd).absolute()
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
    names = tool_names(tools, allowed_tools, disallowed_tools)
    sessions_dir = f"{devices.home(remote)}/.arete/pi-sessions" if remote else str(SESSIONS_DIR)
    cmd = ["pi" if remote else binary(), "-p", "--mode", "json", "--no-context-files", "--no-skills", "--no-extensions",
           "--no-prompt-templates", "--no-themes", "--no-approve", "--no-mcp"]
    cmd += ["--tools", ",".join(names)] if names else ["--no-tools"]
    if model:
        cmd += ["--model", model]
    if persist or resume.id:
        if not remote:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        cmd += ["--session-dir", sessions_dir]
        if resume.id:
            cmd += ["--session", resume.id]
    else:
        cmd += ["--no-session"]
    if system:
        cmd += ["--append-system-prompt", system]
    if add_dirs:
        cmd += ["--append-system-prompt", "You may also read and change files under: " + ", ".join(map(str, add_dirs))]
    if json_schema:
        prompt = f"{prompt}\n\n{SCHEMA_RULE.format(schema=json.dumps(json_schema))}"
    if WRITING & set(names) and not remote:
        cmd = write_confinement(cwd, add_dirs) + cmd

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["ARETE_AGENT"] = "1"
    cmd = [*cmd, "--", prompt]
    if remote:
        cmd = devices.wrap(remote, cmd, cwd, {"ARETE_AGENT": "1"}, mkdirs=(sessions_dir,))
    try:
        proc = subprocess.run(cmd, cwd=None if remote else str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        return Result(ok=False, session=resume, error=f"timed out after {timeout}s", runner="pi", model=model)
    except FileNotFoundError as e:
        return Result(ok=False, session=resume, error=f"pi not found: {e}", runner="pi", model=model)
    return parse(proc, resume, model, json_schema)


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
    session_id = next((e.get("id", "") for e in stream if e.get("type") == "session"), "")
    end = next((e for e in reversed(stream) if e.get("type") == "agent_end"), {})
    replies = [m for m in end.get("messages", []) if m.get("role") == "assistant"]
    if not replies:
        error = util.tail(proc.stderr or proc.stdout or f"exit {proc.returncode}", 1500)
        return Result(ok=False, session=resume, error=error, runner="pi", model=model,
                      connection_failed=util.is_network_error(error))
    last = replies[-1]
    text = "".join(c.get("text", "") for c in last.get("content", []) if c.get("type") == "text")
    usages = [m.get("usage") or {} for m in replies]
    cost = sum(float((u.get("cost") or {}).get("total") or 0) for u in usages)
    failed = last.get("stopReason") == "error" or proc.returncode != 0
    error = (last.get("errorMessage") or util.first_line(proc.stderr, 1500) or "pi stopped on an error") if failed else ""
    session = Session(session_id or resume.id, resume.spent_usd + cost, "pi") if session_id or resume.id else resume
    return Result(
        ok=not failed,
        text=text,
        session=session,
        structured=json_from_text(text) if json_schema and not failed else None,
        cost_usd=cost,
        turns=len(replies),
        error=error,
        connection_failed=failed and util.is_network_error(error),
        raw={"stopReason": last.get("stopReason"), "usage": usages[-1]},
        tokens_in=sum(int(u.get("input") or 0) + int(u.get("cacheRead") or 0) + int(u.get("cacheWrite") or 0)
                      for u in usages),
        tokens_out=sum(int(u.get("output") or 0) for u in usages),
        model=f"{last.get('provider')}/{last.get('model')}" if last.get("provider") else model,
        provider=last.get("provider", ""),
        runner="pi",
    )
