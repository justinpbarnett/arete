from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import devices, paths, util
from .agent import NO_SESSION, Result, Session, UsageLimit, json_from_text

PARENT_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "ANTHROPIC_API_KEY")
PATH_RULE_OF = {"Read": "Read", "Glob": "Read", "Grep": "Read", "LS": "Read", "Edit": "Edit", "MultiEdit": "Edit",
                "Write": "Edit", "NotebookEdit": "Edit"}
GIT_WRITES_TO_ANY_PATH = "Bash(git * --output*)"
USAGE_LIMIT_TEXT = re.compile(r"(?:hit|reached) your [^.\n]{0,60}limit|usage limit reached", re.IGNORECASE)
LEGACY_LIMIT_RESET = re.compile(r"usage limit reached\|(\d{10,13})", re.IGNORECASE)
LIMIT_SCOPES = {"seven_day_opus": "opus", "seven_day_sonnet": "sonnet"}
EVERY_MODEL = "claude"
LIMIT_SLACK_SECONDS = 60
LIMIT_RECHECK_SECONDS = 1800
MILLISECOND_STAMPS = 1e12


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
    settings: dict | None = None,
    extra_env: dict[str, str] | None = None,
) -> Result:
    add_dirs = [Path(d).absolute() for d in add_dirs or []]
    cmd = ["claude" if remote else binary(), "-p", "--output-format", "json", "--permission-mode", permission_mode]
    if model:
        cmd += ["--model", model]
    if tools is not None:
        cmd += ["--tools", ",".join(tools)]
    if allowed_tools:
        roots = [remote_root(cwd)] if remote else local_roots([cwd, *add_dirs])
        cmd += ["--allowedTools", *scoped_to(roots, allowed_tools)]
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
    if settings:
        cmd += ["--settings", json.dumps(settings)]

    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    env["ARETE_AGENT"] = "1"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env.update(extra_env or {})
    if remote:
        cmd = devices.wrap(remote, ["env", "-u", "ANTHROPIC_API_KEY", *cmd], cwd,
                           {"ARETE_AGENT": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    try:
        proc = subprocess.run(
            cmd, cwd=None if remote else str(cwd), input=prompt, capture_output=True, text=True, timeout=timeout,
            errors="replace", env=env, check=False,
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
        error = util.tail(proc.stderr or out or f"exit {proc.returncode}", 1500)
        return Result(ok=False, session=resume, error=error,
                      usage_limit=usage_limit({}, error) if proc.returncode else None)

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
        usage_limit=usage_limit(data, data.get("result", "") or "") if is_error else None,
        raw=data,
        tokens_in=sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                         "cache_read_input_tokens")),
        tokens_out=int(usage.get("output_tokens") or 0),
        model=next(iter(data.get("modelUsage") or {}), model),
        provider="anthropic",
    )


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
    return text if text == "~" or text.startswith("~/") else f"~/{text}"


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
            if entry.get("type") == "assistant" and (usage := (entry.get("message") or {}).get("usage")):
                return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                             "cache_creation_input_tokens"))
    return 0
