from __future__ import annotations

import json
import os
import shlex
import sys
from contextlib import suppress
from pathlib import Path

from . import guard, paths, scratch
from .agent import Result

NAME = "browser_file_upload"
TOOL = f"mcp__playwright__{NAME}"
OUTSIDE = "outside this run's evidence and browser folders"
SECRET = "a secret file"
MISSING = "not an existing file"
FAILED = ("Arete's upload guard could not check this call, so it refused it. Nothing was uploaded. Record the check "
          "as blocked.")
WITHHELD = (f"`{NAME}` is not available on this route. Do not call it; record a file chooser check as not passed "
            "with `cause` `environment`.")
CODEX_REFUSAL = ("Arete does not let the codex verifier upload files in the browser. Nothing was uploaded. Record the "
                 "file chooser check as blocked.")


def folders(run_id: str) -> tuple[Path, ...]:
    if not run_id or run_id in (".", "..") or Path(run_id).name != run_id:
        return ()
    run_dir = str(paths.RUNS_DIR / run_id)
    return (scratch.evidence_path(run_dir), scratch.browser_path(run_dir))


def refused(path: object, roots: tuple[Path, ...]) -> str:
    if not isinstance(path, str) or not path:
        raise TypeError("upload path is not a string")
    if not os.path.isabs(path):
        return OUTSIDE
    if guard.secret_path(path):
        return SECRET
    try:
        resolved = Path(os.path.realpath(path, strict=True))
    except OSError:
        return MISSING
    if not resolved.is_file():
        return MISSING
    inside = any(resolved != root and resolved.is_relative_to(root)
                 for root in (Path(os.path.realpath(r)) for r in roots))
    if not inside:
        return OUTSIDE
    return SECRET if guard.secret_path(resolved) else ""


def decide(payload: dict) -> str:
    args = payload.get("tool_input")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise TypeError("tool_input is not an object")
    listed = args.get("paths")
    if listed is None or listed == []:
        return ""
    if not isinstance(listed, list):
        raise TypeError("paths is not a list")
    roots = folders(os.environ.get("ARETE_RUN_ID", ""))
    for path in listed:
        rule = refused(path, roots)
        if rule:
            where = (f"Put upload fixtures in {roots[0]} (copy a worktree file there first) and pass their absolute "
                     "paths." if roots else "This call carries no run, so no folder is open for uploads.")
            return f"Arete's guard refused this upload: {path} is {rule}. Nothing was uploaded. {where}"
    return ""


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
    except Exception:
        reason = FAILED
    if reason:
        with suppress(Exception):
            guard.note(Result(ok=False, error=reason, runner="guard"))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


def with_hook(settings: dict | None) -> dict:
    settings = json.loads(json.dumps(settings or {}))
    command = shlex.join([sys.executable, "-I", "-m", "arete.cli", "upload-guard"])
    group = {"matcher": TOOL, "hooks": [{"type": "command", "command": command,
                                         "timeout": guard.HOOK_TIMEOUT_SECONDS}]}
    hooks = settings.setdefault("hooks", {})
    hooks["PreToolUse"] = [*hooks.get("PreToolUse", []), group]
    return settings


def granted(rules: list[str] | None) -> bool:
    return TOOL in (rules or [])


def without(rules: list[str] | None) -> list[str]:
    return [r for r in rules or [] if r != TOOL]
