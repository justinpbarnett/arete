from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, jev, paths, record, sandbox, scratch, util
from .agent import Result

FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS")
MATCHER = "|".join(("Bash", *FILE_TOOLS))
HOOK_TIMEOUT_SECONDS = 60
PLACEHOLDER = re.compile(
    r"(?<![$\w{])(?:export\s+)?[A-Za-z_]\w*=(['\"]?)arete-placeholder[\w.-]*\1(?=\s|[;|&]|$)\s*;?\s*")
EFFECT = "What does running this shell command do, given the working directory it runs in?"
EFFECTS = {
    "irreversible": ("harm git cannot undo: throws away uncommitted changes or untracked work (git reset --hard, git "
                     "checkout or restore over edited files, git clean, git stash drop), deletes or overwrites files "
                     "outside the working directory, drops or rewrites a database, force pushes or deletes remote "
                     "branches or tags, publishes a package or release, sends messages, mail or requests that change "
                     "something on a service on another machine, changes system settings, users or permissions, or "
                     "kills processes it did not start"),
    "reversible": ("changes files inside the working directory that git tracks or that a build makes again, or "
                   "temporary files, installs dependencies, builds, formats or runs tests, commits, pushes a branch "
                   "without force, opens a pull request, sends requests to a local server (localhost, 127.0.0.1 or "
                   "[::1]) without destroying files or untracked work or dropping or rewriting a database, or stops "
                   "a background job it started (kill %1)"),
    "read_only": "only reads, lists, searches or prints, and changes nothing",
}
SECRETS = ("Does this command expose the value of a credential, API key, token, password or private key: print it, copy "
           "it, read it from a secret file, a keychain or an environment variable, or send it somewhere?")


LOGIN_FOLDERS = tuple(dict.fromkeys([
    *(paths.HOME / p for p in (".pi", ".claude", ".codex", ".gemini", ".ssh", ".aws", ".docker", ".kube",
                               ".gnupg", ".password-store", ".config/gh", ".config/gcloud", ".config/opencode",
                               ".local/share/opencode", ".config/arete")),
    *(Path(os.environ[p]) for p in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR")
      if os.environ.get(p)),
]))
LOGIN_FILES = tuple(paths.HOME / p for p in (
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".claude/.credentials.json", ".codex/auth.json",
    ".gemini/oauth_creds.json", ".aws/credentials", ".config/gh/hosts.yml",
    ".config/gcloud/application_default_credentials.json", ".config/opencode/auth.json",
    ".local/share/opencode/auth.json"))


@dataclass(frozen=True)
class Floor:
    closed: tuple[Path, ...]
    files: tuple[Path, ...]
    open: tuple[Path, ...]

    def denies(self, path: Path | str) -> bool:
        roots = tuple(p for root in self.closed for p in (root.absolute(), root.resolve()))
        opened = tuple(p for root in self.open for p in (root.absolute(), root.resolve()))
        files = {p for file in self.files for p in (file.absolute(), file.resolve())}
        for candidate in (Path(path).absolute(), Path(path).resolve()):
            if candidate in files:
                return True
            closed_depth = max((len(p.parts) for p in roots if candidate.is_relative_to(p)), default=-1)
            open_depth = max((len(p.parts) for p in opened if candidate.is_relative_to(p)), default=-1)
            if closed_depth >= 0 and closed_depth >= open_depth:
                return True
        return False


def run_folders(run_id: str) -> tuple[Path, ...]:
    from . import sessions

    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        return ()
    return (paths.RUNS_DIR / run_id, sessions.SESSIONS_DIR / run_id)


def floor(run_id: str, cwd: Path | str, add_dirs) -> Floor:
    from . import codex

    closed = (paths.CONFIG_DIR, paths.STATE_DIR, paths.WORKTREES_DIR, paths.LOG_DIR, paths.CLIENT_DIR,
              *LOGIN_FOLDERS, codex.home(), Path(os.environ.get("CLAUDE_CONFIG_DIR") or paths.HOME / ".claude"))
    return Floor(tuple(dict.fromkeys(Path(p).absolute() for p in closed)),
                 (*LOGIN_FILES, paths.PI_KEYS_FILE),
                 tuple(dict.fromkeys(Path(p).absolute() for p in (*run_folders(run_id), cwd, *add_dirs))))


def launch_refusal(run_id: str, cwd: Path | str, add_dirs) -> str:
    boundary = floor(run_id, cwd, add_dirs)
    own = run_folders(run_id)
    for target in (cwd, *add_dirs):
        for candidate in (Path(target).absolute(), Path(target).resolve()):
            for root in boundary.closed:
                for closed in (root.absolute(), root.resolve()):
                    if not candidate.is_relative_to(closed):
                        continue
                    allowed = any(candidate.is_relative_to(p) for folder in own
                                  for p in (folder.absolute(), folder.resolve()))
                    allowed = allowed or candidate.parent in (paths.WORKTREES_DIR.absolute(),
                                                               paths.WORKTREES_DIR.resolve())
                    if not allowed:
                        return reach(str(target))
    return ""


def reach(target: str) -> str:
    return f"Arete keeps {target} outside the agent's reach: its worktree and its run folder."


def settings(judge: bool = False, opened=()) -> dict:
    command = shlex.join(paths.cli_command("guard", *(["--judge"] if judge else []),
                                           *(arg for path in opened for arg in ("--open", str(path)))))
    return {"hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [
        {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]}]}}


def wanted(tools: list[str] | None) -> bool:
    return tools is None or any(t == "Bash" or t in FILE_TOOLS for t in tools)


def secret_named(command: str, cwd: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for part in (p for w in words for p in re.split(r"[=<>;|&()`]", w) if p):
        path = Path(os.path.expanduser(part))
        if sandbox.secret_path(path) and (path if path.is_absolute() else Path(cwd) / path).exists():
            return part
    return ""


def main(judge: bool = False, opened=()) -> int:
    try:
        payload = json.load(sys.stdin)
        boundary = floor(os.environ.get("ARETE_RUN_ID", ""), payload.get("cwd") or os.getcwd(), opened)
        reason = decide(payload, judge, boundary)
    except Exception:
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


def decide(payload: dict, judge: bool = False, boundary: Floor | None = None) -> str:
    tool = payload.get("tool_name", "")
    args = payload.get("tool_input") or {}
    cwd = str(payload.get("cwd") or os.getcwd())
    boundary = boundary or floor(os.environ.get("ARETE_RUN_ID", ""), cwd, [])
    if tool in FILE_TOOLS:
        target = str(args.get("file_path") or args.get("notebook_path") or args.get("path") or "")
        if tool == "Glob" and not target:
            pattern = str(args.get("pattern") or "")
            if Path(pattern).is_absolute():
                prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
                target = prefix if prefix.endswith("/") or prefix == pattern else str(Path(prefix).parent)
        if not target and tool in ("Glob", "Grep", "LS"):
            target = cwd
        path = Path(paths.expand(target))
        path = path if path.is_absolute() else Path(cwd) / path
        if target and boundary.denies(path):
            return reach(target)
        return kept_out(tool, target) if target and sandbox.secret_path(target) else ""
    command = str(args.get("command") or "").strip() if tool == "Bash" else ""
    if not command:
        return ""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    keychain_reads = {"find-generic-password", "find-internet-password", "dump-keychain", "export"}
    if any(Path(word).name == "security" and any(w in keychain_reads for w in words[i + 1:])
           for i, word in enumerate(words)):
        return reach("security keychain items")
    for part in (p for word in words for p in re.split(r"[=<>;|&()`]", word) if p):
        path = Path(paths.expand(part))
        path = path if path.is_absolute() else Path(cwd) / path
        if boundary.denies(path):
            return reach(part)
    if secret := secret_named(command, cwd):
        return kept_out(tool, secret)
    return judged(command, cwd, config.load()) if judge else ""


def kept_out(tool: str, target: str) -> str:
    note(Result(ok=False, error=f"kept {tool} out of {target}", runner="guard"))
    return (f"Arete's guard keeps agents out of secret files, and {target} looks like one. Leave it alone and say in "
            f"your summary what it needs.")


def judged(command: str, cwd: str, cfg: config.Config) -> str:
    if not jev.ready(cfg, [cfg.repo_for_work_dir(cwd)]):
        return ""
    shown = PLACEHOLDER.sub("", command).strip() or command
    context = scratch.context(command, cwd)
    reply = jev.ask(f"{context}Working directory: {cwd}\n\nCommand:\n{shown}",
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
