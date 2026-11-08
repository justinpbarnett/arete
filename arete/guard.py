from __future__ import annotations

import json
import os
import re
import shlex
import sys
from fnmatch import fnmatch
from pathlib import Path

from . import config, jev, record, util
from .agent import Result

FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit")
MATCHER = "|".join(("Bash", *FILE_TOOLS))
HOOK_TIMEOUT_SECONDS = 60
SECRET_NAMES = (".env", ".env.*", "*.env", ".envrc", ".netrc", ".pgpass", ".npmrc", ".pypirc", "id_rsa*", "id_dsa*",
                "id_ecdsa*", "id_ed25519*", "*.pem", "*.p12", "*.pfx", "*.key", "credentials", "credentials.json",
                "*.secret", "*.secrets", "secrets.toml", "secrets.json")
SAFE_NAMES = ("*.example", "*.sample", "*.template", "*.dist")
SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".password-store"})
EFFECT = "What does running this shell command do, given the working directory it runs in?"
EFFECTS = {
    "irreversible": ("harm git cannot undo: throws away uncommitted changes or untracked work (git reset --hard, git "
                     "checkout or restore over edited files, git clean, git stash drop), deletes or overwrites files "
                     "outside the working directory, drops or rewrites a database, force pushes or deletes remote "
                     "branches or tags, publishes a package or release, sends messages, mail or requests that change "
                     "something on an outside service, changes system settings, users or permissions, or kills "
                     "processes it did not start"),
    "reversible": ("changes files inside the working directory that git tracks or that a build makes again, or "
                   "temporary files, installs dependencies, builds, formats or runs tests, commits, pushes a branch "
                   "without force, opens a pull request, or stops a background job it started (kill %1)"),
    "read_only": "only reads, lists, searches or prints, and changes nothing",
}
SECRETS = ("Does this command expose the value of a credential, API key, token, password or private key: print it, copy "
           "it, read it from a secret file, a keychain or an environment variable, or send it somewhere?")


def settings() -> dict:
    command = shlex.join([sys.executable, "-I", "-m", "arete.cli", "guard"])
    return {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [
        {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]}]}}


def wanted(tools: list[str] | None) -> bool:
    return tools is None or any(t == "Bash" or t in FILE_TOOLS for t in tools)


def secret_path(p: Path | str) -> bool:
    p = Path(p)
    if SECRET_DIRS & set(p.parts[:-1]):
        return True
    name = p.name
    return any(fnmatch(name, n) for n in SECRET_NAMES) and not any(fnmatch(name, n) for n in SAFE_NAMES)


def secret_named(command: str, cwd: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for part in (p for w in words for p in re.split(r"[=<>;|&()`]", w) if p):
        path = Path(os.path.expanduser(part))
        if secret_path(path) and (path if path.is_absolute() else Path(cwd) / path).exists():
            return part
    return ""


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
    except Exception:
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


def decide(payload: dict) -> str:
    tool = payload.get("tool_name", "")
    args = payload.get("tool_input") or {}
    cwd = str(payload.get("cwd") or os.getcwd())
    if tool in FILE_TOOLS:
        target = str(args.get("file_path") or args.get("notebook_path") or "")
        return kept_out(tool, target) if target and secret_path(target) else ""
    command = str(args.get("command") or "").strip() if tool == "Bash" else ""
    if not command:
        return ""
    if secret := secret_named(command, cwd):
        return kept_out(tool, secret)
    return judged(command, cwd, config.load())


def kept_out(tool: str, target: str) -> str:
    note(Result(ok=False, error=f"kept {tool} out of {target}", runner="guard"))
    return (f"Arete's guard keeps agents out of secret files, and {target} looks like one. Leave it alone and say in "
            f"your summary what it needs.")


def judged(command: str, cwd: str, cfg: config.Config) -> str:
    if not jev.ready(cfg, [cfg.repo_for_work_dir(cwd)]):
        return ""
    reply = jev.ask(f"Working directory: {cwd}\n\nCommand:\n{command}",
                    {"effect": jev.pick(EFFECT, EFFECTS), "secrets": jev.yes_no(SECRETS)}, cfg, note=False)
    effect, secrets = reply.answers.get("effect"), reply.answers.get("secrets")
    irreversible = effect.probabilities.get("irreversible", 0.0) if effect else 0.0
    if irreversible >= cfg.jev.irreversible_hold:
        reason, way = f"JEV reads it as a step git cannot undo (p {irreversible:.2f})", "Find a way git can undo"
    elif secrets and secrets.yes >= cfg.jev.secrets_hold:
        reason, way = f"JEV reads it as touching credentials (p {secrets.yes:.2f})", "Work without the credentials"
    else:
        return ""
    note(jev.result([reply], error=f"{reason}: {util.first_line(command, 200)}"))
    return f"Arete's guard blocked this command: {reason}. {way}, or leave the step for a person and say so in your summary."


def note(res: Result) -> None:
    if run_id := os.environ.get("ARETE_RUN_ID", ""):
        record.step(run_id, "guard", res, 0)
