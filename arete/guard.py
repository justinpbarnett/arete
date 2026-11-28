from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, jev, local_paths, paths, record, sandbox, scratch, util
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


GLOB_CHARS = re.compile(r"[*?\[]")
ANSI_C_QUOTE = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
ANSI_C_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                 "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
FLAG_PATH = re.compile(r"^-[A-Za-z0-9]+([/~$].*)$")
SHORT_FLAG_DOT = re.compile(r"^-[A-Za-z](\.[^/].*)$")
FAIL_CLOSED_REASON = "Arete could not verify the agent's file or command permissions."


def _safe_spellings(path: Path | str) -> tuple[Path, ...] | None:
    if local_paths.has_null(path):
        return None
    try:
        raw = Path(path)
        return (Path(os.path.normpath(raw.absolute())), raw.resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def _is_subpath(child: tuple[str, ...], root: tuple[str, ...]) -> bool:
    return len(child) >= len(root) and child[:len(root)] == root


def _glob_matches_target(gparts: tuple[str, ...], target: tuple[str, ...]) -> bool:
    for i, part in enumerate(gparts):
        if part == "**":
            return i <= len(target) and all(fnmatch.fnmatchcase(target[j], gparts[j]) for j in range(i))
        if i >= len(target):
            return True
        if not fnmatch.fnmatchcase(target[i], part):
            return False
    return len(gparts) == len(target)


def _decode_ansi_c_body(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] != "\\" or i + 1 >= len(body):
            out.append(body[i])
            i += 1
            continue
        ch = body[i + 1]
        if ch in ANSI_C_SIMPLE:
            out.append(ANSI_C_SIMPLE[ch])
            i += 2
        elif ch == "x":
            digits = re.match(r"[0-9A-Fa-f]{1,2}", body[i + 2:])
            if digits:
                out.append(chr(int(digits.group(0), 16)))
                i += 2 + len(digits.group(0))
            else:
                out.append("\\x")
                i += 2
        elif ch in ("u", "U"):
            width = 4 if ch == "u" else 8
            digits = re.match(rf"[0-9A-Fa-f]{{1,{width}}}", body[i + 2:])
            if digits:
                code = int(digits.group(0), 16)
                out.append(chr(code) if code <= 0x10FFFF else "")
                i += 2 + len(digits.group(0))
            else:
                out.append("\\" + ch)
                i += 2
        elif ch in "01234567":
            digits = re.match(r"[0-7]{1,3}", body[i + 1:])
            out.append(chr(int(digits.group(0), 8) & 0xFF))
            i += 1 + len(digits.group(0))
        else:
            out.append("\\" + ch)
            i += 2
    return "".join(out)


def _decode_ansi_c(text: str) -> str:
    if "$'" not in text:
        return text
    return ANSI_C_QUOTE.sub(lambda m: _decode_ansi_c_body(m.group(1)), text)


def _command_parts(command: str) -> list[str]:
    texts = [command]
    decoded = _decode_ansi_c(command)
    if decoded != command:
        texts.append(decoded)
    parts: list[str] = []
    seen: set[str] = set()

    def emit(token: str) -> None:
        cleaned = token.strip()
        if not cleaned:
            return
        variants = [cleaned]
        stripped = cleaned.strip("'\"")
        if stripped and stripped != cleaned:
            variants.append(stripped)
        for item in variants:
            if item not in seen:
                seen.add(item)
                parts.append(item)
            for pattern in (FLAG_PATH, SHORT_FLAG_DOT):
                if m := pattern.match(item):
                    suffix = m.group(1)
                    if suffix not in seen:
                        seen.add(suffix)
                        parts.append(suffix)

    for text in texts:
        try:
            words = shlex.split(text)
        except ValueError:
            words = text.split()
        for word in words:
            for chunk in re.split(r"[=<>;|&()`]", word):
                emit(chunk)
            if any(c in word for c in " \t\r\n'\"{}[],"):
                for chunk in re.split(r"[\s=<>;|&()`'\"{}\[\],]+", word):
                    emit(chunk)
                try:
                    for sub in shlex.split(word):
                        for chunk in re.split(r"[=<>;|&()`]", sub):
                            emit(chunk)
                except ValueError:
                    pass
    return parts


def _secret_glob_match(pattern_path: Path) -> bool:
    if local_paths.has_null(pattern_path):
        return True
    text = str(pattern_path)
    prefix = GLOB_CHARS.split(text, maxsplit=1)[0]
    parent = Path(prefix) if prefix.endswith("/") else Path(prefix).parent
    if not str(parent) or not parent.is_dir():
        return False
    rel_glob = text[len(str(parent).rstrip("/")):].lstrip("/")
    if not rel_glob or "/" in rel_glob:
        return False
    leaf_folded = rel_glob.casefold()
    try:
        entries = os.listdir(parent)
    except OSError:
        return False
    for entry in entries:
        if entry.startswith(".") and not rel_glob.startswith("."):
            continue
        if fnmatch.fnmatchcase(entry.casefold(), leaf_folded):
            candidate = parent / entry
            if sandbox.secret_path(candidate):
                return True
            try:
                if sandbox.secret_path(candidate.resolve()):
                    return True
            except (OSError, RuntimeError, ValueError):
                return True
    return False


@dataclass(frozen=True)
class Floor:
    closed: tuple[Path, ...]
    files: tuple[Path, ...]
    open: tuple[Path, ...]

    def _folded_sets(self) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...], set[tuple[str, ...]]]:
        roots = tuple(local_paths.folded_parts(p) for root in self.closed for p in (_safe_spellings(root) or ()))
        opened = tuple(local_paths.folded_parts(p) for root in self.open for p in (_safe_spellings(root) or ()))
        files = {local_paths.folded_parts(p) for file in self.files for p in (_safe_spellings(file) or ())}
        return roots, opened, files

    def denies(self, path: Path | str) -> bool:
        candidates = _safe_spellings(path)
        if candidates is None:
            return True
        roots, opened, files = self._folded_sets()
        for candidate in candidates:
            cparts = local_paths.folded_parts(candidate)
            if cparts in files:
                return True
            closed_depth = max((len(p) for p in roots if _is_subpath(cparts, p)), default=-1)
            open_depth = max((len(p) for p in opened if _is_subpath(cparts, p)), default=-1)
            if closed_depth >= 0 and closed_depth >= open_depth:
                return True
        return False

    def denies_glob(self, pattern: Path | str) -> bool:
        candidates = _safe_spellings(pattern)
        if candidates is None:
            return True
        roots, opened, files = self._folded_sets()
        for candidate in candidates:
            gparts = local_paths.folded_parts(candidate)
            for target in (*roots, *files):
                if any(_is_subpath(gparts, o) and len(o) > len(target) for o in opened):
                    continue
                if _glob_matches_target(gparts, target):
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
    own = tuple(local_paths.folded_parts(p) for folder in run_folders(run_id) for p in (_safe_spellings(folder) or ()))
    worktrees = tuple(local_paths.folded_parts(p) for p in (_safe_spellings(paths.WORKTREES_DIR) or ()))
    closed_roots = tuple(local_paths.folded_parts(p) for root in boundary.closed for p in (_safe_spellings(root) or ()))
    for target in (cwd, *add_dirs):
        candidates = _safe_spellings(target)
        if candidates is None:
            return reach(str(target))
        for candidate in candidates:
            cparts = local_paths.folded_parts(candidate)
            for closed in closed_roots:
                if not _is_subpath(cparts, closed):
                    continue
                allowed = any(_is_subpath(cparts, folder) for folder in own)
                allowed = allowed or cparts[:-1] in worktrees
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
    if local_paths.has_null(command) or local_paths.has_null(cwd):
        return command
    for part in _command_parts(command):
        if local_paths.has_null(part):
            return part
        path = paths.expand(part)
        full = path if path.is_absolute() else Path(cwd) / path
        if GLOB_CHARS.search(part):
            if _secret_glob_match(full):
                return part
            continue
        try:
            exists = full.exists() or full.is_symlink()
        except (OSError, ValueError):
            return part
        if not exists:
            continue
        if sandbox.secret_path(path) or sandbox.secret_path(full):
            return part
        try:
            if sandbox.secret_path(full.resolve()):
                return part
        except (OSError, RuntimeError, ValueError):
            return part
    return ""


def main(judge: bool = False, opened=()) -> int:
    try:
        payload = json.load(sys.stdin)
        boundary = floor(os.environ.get("ARETE_RUN_ID", ""), payload.get("cwd") or os.getcwd(), opened)
        reason = decide(payload, judge, boundary)
    except Exception:
        reason = FAIL_CLOSED_REASON
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


def _denies_path_or_glob(boundary, path: Path) -> bool:
    if boundary.denies(path):
        return True
    if GLOB_CHARS.search(str(path)) and hasattr(boundary, "denies_glob"):
        return boundary.denies_glob(path)
    return False


def _secret_target(target: str, path: Path) -> bool:
    if sandbox.secret_path(target) or sandbox.secret_path(path):
        return True
    try:
        return sandbox.secret_path(path.resolve())
    except (OSError, RuntimeError, ValueError):
        return True


def decide(payload: dict, judge: bool = False, boundary: Floor | None = None) -> str:
    from . import codex

    tool = payload.get("tool_name", "")
    args = payload.get("tool_input") or {}
    cwd = str(payload.get("cwd") or os.getcwd())
    if local_paths.has_null(cwd):
        return reach(cwd)
    boundary = boundary or floor(os.environ.get("ARETE_RUN_ID", ""), cwd, [])
    if tool in FILE_TOOLS:
        target = str(args.get("file_path") or args.get("notebook_path") or args.get("path") or "")
        pattern = str(args.get("pattern") or "") if tool == "Glob" else ""
        if local_paths.has_null(target) or local_paths.has_null(pattern):
            return reach(target or pattern)
        if tool == "Glob" and pattern:
            expanded_pattern = paths.expand(pattern)
            base = paths.expand(target) if target else Path(cwd)
            base = base if base.is_absolute() else Path(cwd) / base
            full_pattern = expanded_pattern if expanded_pattern.is_absolute() else base / expanded_pattern
            prefix = GLOB_CHARS.split(str(full_pattern), maxsplit=1)[0]
            prefix_path = Path(prefix if prefix.endswith("/") or prefix == str(full_pattern) else str(Path(prefix).parent))
            if _denies_path_or_glob(boundary, prefix_path) or _denies_path_or_glob(boundary, full_pattern):
                return reach(pattern)
            if sandbox.secret_path(expanded_pattern) or sandbox.secret_path(full_pattern):
                return kept_out(tool, pattern)
        if not target and tool in ("Glob", "Grep", "LS"):
            target = cwd
        path = paths.expand(target)
        path = path if path.is_absolute() else Path(cwd) / path
        if target and _denies_path_or_glob(boundary, path):
            return reach(target)
        return kept_out(tool, target) if target and _secret_target(target, path) else ""
    command = str(args.get("command") or "").strip() if tool == "Bash" else ""
    if not command:
        return ""
    if local_paths.has_null(command):
        return reach("command with null byte")
    if reason := codex.keychain_refusal(command):
        return reason
    for part in _command_parts(command):
        if local_paths.has_null(part):
            return reach(part)
        path = paths.expand(part)
        path = path if path.is_absolute() else Path(cwd) / path
        if _denies_path_or_glob(boundary, path):
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
