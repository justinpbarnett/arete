from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from . import devices, paths, util
from .agent import (
    NO_SESSION,
    READ_ONLY_TOOLS,
    READ_TOOLS,
    Result,
    Session,
    json_from_text,
)

TOOLS_OF = {"Read": ("read",), "Glob": ("find", "ls"), "Grep": ("grep",), "LS": ("ls",), "Edit": ("edit",),
            "MultiEdit": ("edit",), "Write": ("write",), "Bash": ("bash",)}
WRITING = {"edit", "write", "bash"}
SESSIONS_DIR = paths.STATE_DIR / "pi-sessions"
PI_HOME = paths.HOME / ".pi"
AGENT_DIR = paths.STATE_DIR / "pi-agent"
KEYS_FILE = paths.CONFIG_DIR / "pi.env"
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


def binary() -> str:
    installed = sorted((paths.HOME / ".nvm" / "versions" / "node").glob("v*/bin/pi"), key=lambda p: p.parent.parent.name)
    return shutil.which("pi") or (str(installed[-1]) if installed else "pi")


def tool_names(tools: list[str] | None, allowed_tools: list[str] | None, disallowed_tools: list[str] | None) -> list[str]:
    granted = tools if tools is not None else [*(allowed_tools or []), *READ_TOOLS]
    allowed = set(allowed_tools or []) | set(READ_TOOLS)
    denied = {t.split("(", 1)[0] for t in disallowed_tools or []}
    names: list[str] = []
    for tool in granted:
        if tool in denied or "(" in tool:
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


def can_confine(remote: bool) -> bool:
    return not remote and sys.platform == "darwin" and bool(shutil.which("sandbox-exec"))


def write_confinement(cwd: Path, add_dirs: list[Path]) -> list[str]:
    roots = [cwd, *add_dirs, AGENT_DIR, SESSIONS_DIR]
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
    price: Callable[[Result], float] = lambda reply: reply.cost_usd,
) -> Result:
    if mcp_servers(mcp_config):
        return Result(ok=False, error="pi takes MCP servers only from its own mcp.json, so run this step on claude",
                      session=resume, runner="pi", model=model)
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
    if writing := sorted(WRITING & set(names)):
        if not can_confine(bool(remote)):
            return Result(ok=False, error=f"{NO_CONFINEMENT_REASON}, so pi cannot {', '.join(writing)} here",
                          session=resume, runner="pi", model=model)
        cmd = write_confinement(cwd, add_dirs) + cmd

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["ARETE_AGENT"] = "1"
    if not remote:
        env |= util.read_env_file(KEYS_FILE)
        env["PI_CODING_AGENT_DIR"] = str(agent_dir())
    cmd = [*cmd, "--", prompt]
    if remote:
        cmd = devices.wrap(remote, cmd, cwd, {"ARETE_AGENT": "1"}, mkdirs=(sessions_dir,))
    try:
        proc, cut_short_by = stream(cmd, None if remote else str(cwd), env, timeout, max_budget_usd,
                                    lambda line: reply_cost(line, model, price))
    except FileNotFoundError as e:
        return Result(ok=False, session=resume, error=f"pi not found: {e}", runner="pi", model=model)
    if cut_short_by:
        return cut_short(proc, resume, model, cut_short_by)
    result = parse(proc, resume, model, json_schema)
    if json_schema and result.ok and result.structured is None:
        restate(result, ending(proc.stdout), json_schema, model, cwd, remote, min(timeout, RESTATE_TIMEOUT))
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
            remote: devices.Device | None, timeout: float) -> None:
    if not ending_text:
        return
    prompt = (f"{RESTATE}\n\n<ending>\n{ending_text}\n</ending>\n\n"
              f"{SCHEMA_RULE.format(schema=json.dumps(json_schema))}\n\n{NO_ANSWER}")
    again = run(prompt, cwd, model=model, tools=[], timeout=timeout, persist=False, remote=remote)
    result.cost_usd += again.cost_usd
    result.tokens_in += again.tokens_in
    result.tokens_out += again.tokens_out
    result.structured = json_from_text(again.text) if again.ok else None


def mcp_servers(mcp_config: str) -> dict:
    try:
        return json.loads(mcp_config).get("mcpServers") or {} if mcp_config else {}
    except (json.JSONDecodeError, AttributeError):
        return {"unreadable": mcp_config}


def stream(cmd: list[str], cwd: str | None, env: dict, timeout: float, max_budget_usd: float | None,
           cost_of: Callable[[str], float]) -> tuple[subprocess.CompletedProcess, str]:
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


def reply_cost(line: str, model: str, price: Callable[[Result], float]) -> float:
    try:
        message = json.loads(line).get("message") or {}
    except json.JSONDecodeError:
        return 0.0
    if message.get("role") != "assistant":
        return 0.0
    provider = message.get("provider", "")
    return price(Result(ok=True, runner="pi", provider=provider,
                        model=f"{provider}/{message.get('model')}" if provider else model,
                        **usage_totals([message.get("usage") or {}])))


def usage_totals(usages: list[dict]) -> dict:
    return {"cost_usd": sum(float((u.get("cost") or {}).get("total") or 0) for u in usages),
            "tokens_in": sum(int(u.get("input") or 0) + int(u.get("cacheRead") or 0) + int(u.get("cacheWrite") or 0)
                             for u in usages),
            "tokens_out": sum(int(u.get("output") or 0) for u in usages)}


def cut_short(proc: subprocess.CompletedProcess, resume: Session, model: str, reason: str) -> Result:
    stream = events(proc.stdout)
    replies = [e["message"] for e in stream
               if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant"]
    totals = usage_totals([m.get("usage") or {} for m in replies])
    return Result(ok=False, session=session_after(stream, resume, totals["cost_usd"]), error=reason,
                  turns=len(replies), runner="pi", model=model, **totals)


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
        model=f"{last.get('provider')}/{last.get('model')}" if last.get("provider") else model,
        provider=last.get("provider", ""),
        runner="pi",
    )
