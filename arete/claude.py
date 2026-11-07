from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import util

PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_API_KEY")
FILE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
GIT_WRITES_TO_ANY_PATH = "Bash(git * --output*)"


@dataclass
class Result:
    ok: bool
    text: str = ""
    session_id: str = ""
    structured: dict | list | None = None
    cost_usd: float = 0.0
    turns: int = 0
    error: str = ""
    connection_failed: bool = False
    raw: dict = field(default_factory=dict)


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
    resume: str = "",
    system: str = "",
    max_budget_usd: float | None = None,
    timeout: float = 1800,
    persist: bool = True,
    add_dirs: list[Path] | None = None,
    mcp_config: str = "",
) -> Result:
    cmd = [binary(), "-p", "--output-format", "json", "--permission-mode", permission_mode]
    if model:
        cmd += ["--model", model]
    if tools is not None:
        cmd += ["--tools", ",".join(tools)]
    if allowed_tools:
        cmd += ["--allowedTools", *within(allowed_tools, [Path(cwd), *(add_dirs or [])])]
    cmd += ["--disallowedTools", GIT_WRITES_TO_ANY_PATH, *(disallowed_tools or [])]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if resume:
        cmd += ["--resume", resume]
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
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), input=prompt, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        return Result(ok=False, error=f"timed out after {timeout}s")
    except FileNotFoundError as e:
        return Result(ok=False, error=f"claude not found: {e}")

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
        return Result(ok=False, error=util.tail(proc.stderr or out or f"exit {proc.returncode}", 1500))

    structured = data.get("structured_output")
    if structured is None and json_schema:
        structured = _json_from_text(data.get("result", ""))
    is_error = bool(data.get("is_error")) or data.get("subtype", "success") != "success"
    return Result(
        ok=not is_error and proc.returncode == 0,
        text=data.get("result", "") or "",
        session_id=data.get("session_id", ""),
        structured=structured,
        cost_usd=float(data.get("total_cost_usd") or 0),
        turns=int(data.get("num_turns") or 0),
        error=(data.get("result", "") if is_error else "") or (proc.stderr.strip()[:1500] if proc.returncode else ""),
        connection_failed=data.get("api_error") == "connection_failed",
        raw=data,
    )


def within(rules: list[str], folders: list[Path]) -> list[str]:
    roots = dict.fromkeys(p for f in folders for p in (Path(f).absolute(), Path(f).resolve()))
    return [scoped for rule in rules for scoped in ([f"{rule}(/{r}/**)" for r in roots] if rule in FILE_TOOLS else [rule])]


def _json_from_text(text: str):
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for chunk in reversed(parts):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith(("{", "[")):
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def auth_status() -> dict:
    res = util.sh([binary(), "auth", "status"], timeout=30)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": util.first_line(res.stderr or res.stdout)}
