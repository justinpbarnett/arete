from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import devices, util
from .agent import NO_SESSION, Result, Session, json_from_text

PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_API_KEY")
FILE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
GIT_WRITES_TO_ANY_PATH = "Bash(git * --output*)"


def binary() -> str:
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


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
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
    cmd = ["claude" if remote else binary(), "-p", "--output-format", "json", "--permission-mode", permission_mode]
    if model:
        cmd += ["--model", model]
    if tools is not None:
        cmd += ["--tools", ",".join(tools)]
    if allowed_tools:
        cmd += ["--allowedTools", *(allowed_tools if remote else scoped_to([cwd, *add_dirs], allowed_tools))]
    cmd += ["--disallowedTools", GIT_WRITES_TO_ANY_PATH, *(disallowed_tools or [])]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if resume.id:
        cmd += ["--resume", resume.id]
    if system:
        cmd += ["--append-system-prompt", system]
    if max_budget_usd:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if not persist:
        cmd += ["--no-session-persistence"]
    if add_dirs:
        cmd += ["--add-dir", *map(str, add_dirs)]
    if mcp_config:
        cmd += ["--strict-mcp-config", "--mcp-config", mcp_config]

    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    env["ARETE_AGENT"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if remote:
        cmd = devices.wrap(remote, cmd, cwd, {"ARETE_AGENT": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    try:
        proc = subprocess.run(
            cmd, cwd=None if remote else str(cwd), input=prompt, capture_output=True, text=True, timeout=timeout,
            env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(ok=False, session=resume, error=f"timed out after {timeout}s")
    except FileNotFoundError as e:
        return Result(ok=False, session=resume, error=f"claude not found: {e}")

    out = proc.stdout.strip()
    data: dict = {}
    if out:
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if not data:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                pass
    if not data:
        return Result(ok=False, session=resume, error=util.tail(proc.stderr or out or f"exit {proc.returncode}", 1500))

    structured = data.get("structured_output")
    if structured is None and json_schema:
        structured = json_from_text(data.get("result", ""))
    is_error = bool(data.get("is_error")) or data.get("subtype", "success") != "success"
    session = (Session(data["session_id"], float(data.get("total_cost_usd") or 0), "claude") if data.get("session_id")
               else resume)
    usage = data.get("usage") or {}
    return Result(
        ok=not is_error and proc.returncode == 0,
        text=data.get("result", "") or "",
        session=session,
        structured=structured,
        cost_usd=max(0.0, session.spent_usd - resume.spent_usd),
        turns=int(data.get("num_turns") or 0),
        error=(data.get("result", "") if is_error else "") or (proc.stderr.strip()[:1500] if proc.returncode else ""),
        connection_failed=data.get("api_error") == "connection_failed",
        raw=data,
        tokens_in=sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens")),
        tokens_out=int(usage.get("output_tokens") or 0),
        model=next(iter(data.get("modelUsage") or {}), model),
        provider="anthropic",
    )


def scoped_to(folders: list[Path | str], rules: list[str]) -> list[str]:
    roots = dict.fromkeys(p for f in folders for p in (Path(f).absolute(), Path(f).resolve()))
    scoped = []
    for rule in rules:
        if rule in FILE_TOOLS:
            scoped += [f"{rule}(/{root}/**)" for root in roots]
        else:
            scoped.append(rule)
    return scoped


def auth_status() -> dict:
    res = util.sh([binary(), "auth", "status"], timeout=30)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": util.first_line(res.stderr or res.stdout)}
