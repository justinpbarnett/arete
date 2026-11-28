from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
from collections.abc import Callable
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from . import config, configkinds, events, paths, principals, refusals, retention, util
from .store import config as config_store
from .store import db as foundation

ABSENT = "absent"
BRANCH = "live"
SOURCES = frozenset(("dashboard", "hand", "agent", "revert", "merge", "engine", "draft"))
FENCE_ENV = "ARETE_CONFIG_FENCE"
FENCE_FILE = ".arete-fence"
ACTIVATE_C_ENV = "ARETE_ACTIVATE_C"
INSTANCE_LOCK = "instance-mutation"
REPO_LOCK = "config-repo"
GITIGNORE_LINES = (
    "pi.env",
    ".env",
    ".env.*",
    "*.env",
    "*.secret",
    "hooks/",
    "secrets/",
    "verification-authority/",
    "*.tmp",
    "*.arete-tmp",
)
GITIGNORE_TEXT = "\n".join(GITIGNORE_LINES) + "\n"
TRAILER_RE = re.compile(r"^(Arete-[A-Za-z-]+):\s*(.+?)\s*$")
BACKUP_PREFIX = "config.toml.bak-"

_ACTIVE_FENCE: ContextVar[Fence | None] = ContextVar("config_instance_fence", default=None)
_ACTIVE_REPO_LOCK: ContextVar[bool] = ContextVar("config_repo_lock_held", default=False)
_INVALID_HAND_LOGGED: dict[tuple[str, str], str] = {}
_LOG_GUARD = threading.Lock()


class ConfigRepoError(refusals.Invalid, RuntimeError):
    pass


class StaleBase(refusals.Conflict, RuntimeError):
    code = "stale_base"

    def __init__(self, path: str, base: str, current: str):
        super().__init__(f"stale_base: {path} moved (expected {base}, live is {current})")
        self.path = path
        self.base = base
        self.current = current


class HandEditInvalid(refusals.Conflict, RuntimeError):
    code = "hand_edit_invalid"

    def __init__(self, path: str, detail: str):
        super().__init__(f"hand_edit_invalid: {path}: {detail}")
        self.path = path
        self.detail = detail


@dataclass(frozen=True)
class Fence:
    token: str
    pid: int


def is_repo(root: Path | None = None) -> bool:
    base = (root or paths.CONFIG_DIR)
    git_dir = base / ".git"
    return git_dir.is_dir() and not git_dir.is_symlink()


def _root() -> Path:
    return paths.CONFIG_DIR.resolve()


def _git_env(root: Path, *, actor: str = "", fence_token: str = "", use_hooks: bool = False,
             activate_c: bool = False) -> dict[str, str]:
    from . import environment

    env = {
        k: v for k, v in environment.build(()).items()
        if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG_"))
    }
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    if actor:
        env.update({
            "GIT_AUTHOR_NAME": actor,
            "GIT_AUTHOR_EMAIL": actor,
            "GIT_COMMITTER_NAME": actor,
            "GIT_COMMITTER_EMAIL": actor,
        })
    if fence_token:
        env[FENCE_ENV] = fence_token
    elif FENCE_ENV in env:
        env.pop(FENCE_ENV, None)
    if activate_c:
        env[ACTIVATE_C_ENV] = "1"
    else:
        env.pop(ACTIVATE_C_ENV, None)
    if not use_hooks:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
        env["GIT_CONFIG_VALUE_0"] = os.devnull
    return util.foreground_maintenance(env)


def _git(args: list[str], cwd: Path, *, actor: str = "", fence_token: str = "", use_hooks: bool = False,
         activate_c: bool = False, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "--no-pager", "--no-replace-objects"]
    if use_hooks:
        cmd.extend(["-c", f"core.hooksPath={cwd / '.git' / 'hooks'}"])
    else:
        cmd.extend(["-c", "core.hooksPath=/dev/null"])
    cmd.extend(["-c", "commit.gpgsign=false", *args])
    done = subprocess.run(
        cmd,
        cwd=cwd,
        env=_git_env(cwd, actor=actor, fence_token=fence_token, use_hooks=use_hooks, activate_c=activate_c),
        capture_output=True,
        text=text,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    if check and done.returncode != 0:
        err = (done.stderr if text else done.stderr.decode(errors="replace")).strip()
        out = (done.stdout if text else done.stdout.decode(errors="replace")).strip()
        raise ConfigRepoError(err or out or f"git {args[0]} failed ({done.returncode})")
    return done


def branch_name(root: Path | None = None) -> str:
    base = root or _root()
    if not is_repo(base):
        return BRANCH
    if _git(["rev-parse", "--verify", f"refs/heads/{BRANCH}"], base, check=False).returncode == 0:
        return BRANCH
    head = _git(["symbolic-ref", "--short", "HEAD"], base, check=False).stdout.strip()
    return head or BRANCH


def live_sha(root: Path | None = None) -> str:
    base = root or _root()
    if not is_repo(base):
        return ""
    ref = branch_name(base)
    res = _git(["rev-parse", "--verify", f"refs/heads/{ref}"], base, check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def _git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _normalize_rel(path: str | Path) -> str:
    raw = str(path).replace("\\", "/").strip()
    if not raw or raw.startswith("/") or "\0" in raw:
        raise ConfigRepoError(f"invalid config path: {path!r}")
    parts = [p for p in raw.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or parts[0] == ".git":
        raise ConfigRepoError(f"invalid config path: {path!r}")
    rel = "/".join(parts)
    if _is_secret_rel(rel):
        raise ConfigRepoError(f"refused secret config path: {rel}")
    return rel


def _is_secret_rel(rel: str) -> bool:
    parts = rel.split("/")
    for i, part in enumerate(parts):
        lower = part.lower()
        if lower in ("pi.env", ".env", "verification-authority") or lower.startswith(".env.") or lower.endswith(".env"):
            return True
        if lower.endswith((".secret", ".secrets")):
            return True
        if i == 0 and lower in ("hooks", "secrets") and len(parts) > 1:
            return True
    return False


def _forbidden_secret_entries(root: Path) -> list[str]:
    if not root.exists():
        return []
    found: list[str] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name == ".git":
            continue
        lower = name.lower()
        if lower == "verification-authority":
            found.append(name)
            continue
        if lower == "hooks" and entry.is_dir():
            files = [p for p in sorted(entry.rglob("*")) if p.is_file() or p.is_symlink()]
            if files:
                found.append(files[0].relative_to(root).as_posix())
            continue
        if lower == "secrets" and entry.is_dir():
            files = [p for p in sorted(entry.rglob("*")) if p.is_file() or p.is_symlink()]
            if files:
                found.append(files[0].relative_to(root).as_posix())
            continue
        if lower in ("pi.env", ".env") or lower.startswith(".env.") or lower.endswith((".env", ".secret", ".secrets")):
            found.append(name)
    for here, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "hooks", "secrets", "verification-authority"))
        for fname in sorted(files):
            lower = fname.lower()
            if lower in ("pi.env", ".env") or lower.startswith(".env.") or lower.endswith((".env", ".secret")):
                rel = (Path(here) / fname).relative_to(root).as_posix()
                if rel not in found:
                    found.append(rel)
    return found


def _outside_config_paths(root: Path) -> list[str]:
    cfg_file = root / "config.toml"
    if not cfg_file.is_file():
        return []
    try:
        data = tomllib.loads(cfg_file.read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    outside: list[str] = []
    serve = data.get("serve")
    if isinstance(serve, dict) and "loop_dirs" in serve and isinstance(serve["loop_dirs"], list):
        for raw in serve["loop_dirs"]:
            if isinstance(raw, str) and raw.strip():
                resolved = paths.expand(raw).resolve()
                if not resolved.is_relative_to(root):
                    outside.append(f"[serve] loop_dirs entry {raw!r} ({resolved})")
    repos = data.get("repo")
    if isinstance(repos, list):
        for repo in repos:
            if not isinstance(repo, dict):
                continue
            raw_layer = repo.get("layer")
            if isinstance(raw_layer, str) and raw_layer.strip():
                resolved = paths.expand(raw_layer).resolve()
                if not resolved.is_relative_to(root):
                    outside.append(f"repo {repo.get('name', '?')!r} layer {raw_layer!r} ({resolved})")
    return outside


def classify_path(rel: str) -> tuple[str, str]:
    rel = rel.strip("/")
    if rel == "layers/defaults/config.toml":
        return "step_settings", configkinds.DEFAULTS_LAYER_ADDRESS
    classified = configkinds.classify(rel)
    if classified is not None:
        return classified
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == "layers":
        repo = parts[1]
        if len(parts) == 3 and parts[2] == "config.toml":
            return "step_settings", repo
        if len(parts) == 3 and parts[2] == "expertise.md":
            return "expertise", repo
        if len(parts) == 4 and parts[2] == "evals" and parts[3].endswith(".toml"):
            return "eval", f"{repo}/{parts[3].removesuffix('.toml')}"
    if len(parts) >= 3 and parts[0] == "loops":
        loop_name = parts[1]
        rest = "/".join(parts[2:])
        if rest == "loop.toml":
            return "loop", loop_name
        return "loop", f"{loop_name}/{rest}"
    if rel == "config.toml":
        return "step_settings", "defaults"
    return "file", rel


def origin_of(rel: str) -> str:
    parts = rel.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "layers":
        return f"layer:{parts[1]}"
    if len(parts) >= 3 and parts[0] == "loops":
        return f"loop:{parts[1]}"
    return "config"


def level_of(rel: str) -> str:
    parts = rel.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "layers":
        return f"layer:{parts[1]}"
    return "config"


def validate_actor(actor: str) -> str:
    if not isinstance(actor, str):
        raise ConfigRepoError("config commit actor must be a string")
    actor = actor.strip()
    if not actor or actor == "owner" or "@" in actor or "\n" in actor or "\r" in actor:
        raise ConfigRepoError(f"invalid config commit actor {actor!r}: must be a principal/run/hand id without '@'")
    return actor


def _validate_content(
    rel: str,
    raw: bytes,
    root: Path,
    *,
    staged_targets: dict[str, object] | None = None,
    extra_adws: set[str] | None = None,
) -> None:
    classified = configkinds.classify(rel)
    if classified is not None and classified[0] == "library":
        raise ConfigRepoError(f"{rel}: config kind 'library' is read only")
    parsed: dict | None = None
    if rel.endswith(".toml"):
        try:
            text = raw.decode("utf-8")
            parsed = tomllib.loads(text)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise ConfigRepoError(f"{rel} is not valid TOML: {e}") from e
    if rel == "config.toml":
        with tempfile.TemporaryDirectory(prefix="arete-cfg-check-") as tmp:
            draft = Path(tmp) / "config.toml"
            draft.write_bytes(raw)
            try:
                config.load(draft, strict=True, base_dir=root)
            except Exception as e:
                raise ConfigRepoError(f"config.toml failed validation: {e}") from e
        return
    if rel == retention.FILENAME:
        try:
            retention.parse_bytes(raw)
        except Exception as e:
            raise ConfigRepoError(f"{retention.FILENAME} failed validation: {e}") from e
        return
    if classified is not None and isinstance(parsed, dict):
        kind, item_name = classified
        if kind == "device":
            from . import configschema, devices

            try:
                configschema.validate("device", parsed, where=rel)
                devices.parse_device(item_name, parsed)
            except (ValueError, devices.DeviceError) as e:
                raise ConfigRepoError(f"{rel} failed validation: {e}") from e
        elif kind == "target":
            from . import outbox_config

            try:
                outbox_config.validate_target(item_name, parsed, where=rel)
            except ValueError as e:
                raise ConfigRepoError(str(e)) from e
        elif kind == "rule":
            from . import outbox_config

            try:
                targets = outbox_config.load_targets(root)
                if staged_targets:
                    targets.update(staged_targets)
                outbox_config.validate_rule(item_name, parsed, targets=targets, where=rel)
            except ValueError as e:
                raise ConfigRepoError(str(e)) from e
        elif kind == "adw":
            from . import adw

            if errors := adw.validate_adw(parsed, where=rel):
                raise ConfigRepoError(f"{rel} failed validation: {'; '.join(errors)}")
            if parsed.get("name") != item_name:
                raise ConfigRepoError(
                    f"{rel} failed validation: name {parsed.get('name')!r} does not match {item_name!r}"
                )
        elif kind == "routes":
            from . import adw

            if errors := adw.validate_routes(parsed, config_dir=root, extra_adws=extra_adws, where=rel):
                raise ConfigRepoError(f"{rel} failed validation: {'; '.join(errors)}")


def _validate_batch(files: dict[str, bytes | None], root: Path) -> None:
    from . import outbox_config

    staged_targets: dict[str, outbox_config.OutboxTarget] = {}
    extra_adws: set[str] = {
        classified[1]
        for rel, raw in files.items()
        if raw is not None and (classified := configkinds.classify(rel)) is not None and classified[0] == "adw"
    }
    deferred_rules: list[tuple[str, bytes]] = []
    deferred_routes: list[tuple[str, bytes]] = []
    for rel in sorted(files):
        raw = files[rel]
        if raw is None:
            continue
        classified = configkinds.classify(rel)
        if classified is not None and classified[0] == "rule":
            deferred_rules.append((rel, raw))
            continue
        if classified is not None and classified[0] == "routes":
            deferred_routes.append((rel, raw))
            continue
        _validate_content(rel, raw, root, extra_adws=extra_adws)
        if classified is not None and classified[0] == "target":
            parsed = tomllib.loads(raw.decode("utf-8"))
            staged_targets[classified[1]] = outbox_config._target_from_data(
                classified[1], parsed, level="config", raw=raw, where=rel
            )
    for rel, raw in deferred_rules:
        _validate_content(rel, raw, root, staged_targets=staged_targets)
    for rel, raw in deferred_routes:
        _validate_content(rel, raw, root, extra_adws=extra_adws)


def _write_atomic_bytes(target: Path, data: bytes, mode: int | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600 if target.name == "config.toml" else 0o644
    tmp = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.arete-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        try:
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            util.flush_fd(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
        util.flush_folder(target.parent)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


@contextmanager
def instance_fence():
    if foundation.in_write():
        raise ConfigRepoError("cannot acquire instance fence inside a SQLite write transaction")
    if _ACTIVE_FENCE.get() is not None:
        raise RuntimeError("instance fence is already held in this context; pass the existing fence instead of reacquiring")
    with util.lock(INSTANCE_LOCK, wait=True):
        fence = Fence(token=secrets.token_hex(16), pid=os.getpid())
        tok = _ACTIVE_FENCE.set(fence)
        try:
            yield fence
        finally:
            _ACTIVE_FENCE.reset(tok)


def current_fence() -> Fence | None:
    return _ACTIVE_FENCE.get()


@contextmanager
def maybe_instance_fence():
    if (existing := _ACTIVE_FENCE.get()) is not None:
        yield existing
        return
    with instance_fence() as owned:
        yield owned


def _verify_fence(fence: Fence | None) -> Fence | None:
    if fence is None:
        return None
    active = _ACTIVE_FENCE.get()
    if active is None or active != fence:
        raise RuntimeError("unverified instance fence passed to configrepo")
    return fence


@contextmanager
def writer_lock(fence: Fence | None = None):
    if foundation.in_write():
        raise ConfigRepoError("cannot enter configrepo writer inside a SQLite write transaction")
    verified = _verify_fence(fence)
    if verified is not None:
        if _ACTIVE_REPO_LOCK.get():
            yield verified
            return
        with util.lock(REPO_LOCK, wait=True):
            tok = _ACTIVE_REPO_LOCK.set(True)
            try:
                yield verified
            finally:
                _ACTIVE_REPO_LOCK.reset(tok)
    else:
        with instance_fence() as owned, util.lock(REPO_LOCK, wait=True):
            tok = _ACTIVE_REPO_LOCK.set(True)
            try:
                yield owned
            finally:
                _ACTIVE_REPO_LOCK.reset(tok)


@contextmanager
def _active_hook_fence(root: Path, fence: Fence):
    marker = root / ".git" / FENCE_FILE
    _write_atomic_bytes(marker, fence.token.encode("ascii"), 0o600)
    try:
        yield fence.token
    finally:
        with suppress(FileNotFoundError):
            os.unlink(marker)


def _install_hook(root: Path) -> None:
    hook = root / ".git" / "hooks" / "pre-commit"
    package_root = str(paths.PACKAGE_DIR.parent)
    script = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} -I -B -c "
        f"{shlex.quote(f'import sys; sys.path.insert(0, {package_root!r}); from arete import configrepo; raise SystemExit(configrepo.verify_pre_commit_hook())')}\n"
    )
    _write_atomic_bytes(hook, script.encode("utf-8"), 0o755)


def verify_pre_commit_hook() -> int:
    from . import configcheck

    root = Path(os.getcwd()).resolve()
    done = _git(["diff", "--cached", "--name-status", "-z", "--diff-filter=ACM"], root, check=False, text=False)
    if done.returncode != 0:
        print("configrepo pre-commit hook: failed to inspect staged files", file=sys.stderr)
        return 1
    parts = done.stdout.split(b"\0")
    staged: list[str] = []
    i = 0
    while i + 1 < len(parts):
        status = parts[i].decode(errors="replace")
        rel = parts[i + 1].decode(errors="replace")
        i += 2
        if not status or not rel:
            continue
        staged.append(rel)
    staged_blobs: dict[str, bytes] = {}
    for rel in staged:
        if _is_secret_rel(rel):
            print(f"configrepo pre-commit hook: refused secret file {rel}", file=sys.stderr)
            return 1
        blob = _git(["cat-file", "blob", f":{rel}"], root, check=False, text=False)
        if blob.returncode != 0:
            print(f"configrepo pre-commit hook: could not read staged {rel}", file=sys.stderr)
            return 1
        staged_blobs[rel] = blob.stdout
    ok, max_gate, problems = configcheck.check_staged_files(staged_blobs, root)
    if not ok:
        print("; ".join(problems) or "configrepo pre-commit hook: configcheck failed", file=sys.stderr)
        return 1
    if max_gate == "C" and os.environ.get(ACTIVATE_C_ENV) != "1":
        print(
            "configrepo pre-commit hook: refused gate C change (requires ARETE_ACTIVATE_C=1)",
            file=sys.stderr,
        )
        return 1
    try:
        _validate_batch(staged_blobs, root)
    except ConfigRepoError as e:
        print(str(e), file=sys.stderr)
        return 1
    branch = _git(["symbolic-ref", "--short", "HEAD"], root, check=False).stdout.strip()
    if branch == BRANCH:
        token = os.environ.get(FENCE_ENV, "")
        marker = root / ".git" / FENCE_FILE
        expected = ""
        try:
            expected = marker.read_text().strip()
        except OSError:
            expected = ""
        if (expected or token) and token != expected:
            print(
                "Refused unfenced commit on branch live. Leave edits in the working tree for arete "
                "(or commit through arete/configrepo) so instance and config-repo locks are held.",
                file=sys.stderr,
            )
            return 1
    return 0


def _set_enabled_toml(text: str, enabled: bool) -> str:
    val = "true" if enabled else "false"
    lines = text.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if re.match(r"\s*\[", line)]
    end = headers[0] if headers else len(lines)
    pat = re.compile(r"(\s*enabled\s*=\s*)[^#\r\n]*?(\s*(?:#[^\r\n]*)?)(\r?\n?)\Z")
    for i in range(end):
        if m := pat.match(lines[i]):
            lines[i] = f"{m.group(1)}{val}{m.group(2)}{m.group(3) or chr(10)}"
            return "".join(lines)
    insert_at = max((i for i in range(end) if lines[i].strip()), default=-1) + 1
    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    lines.insert(insert_at, f"enabled = {val}\n")
    return "".join(lines)


def _format_commit_message(note: str, actor: str, source: str, run_id: str = "", idem: str = "",
                           draft_id: str = "") -> str:
    subject = (note or "config update").splitlines()[0].strip() or "config update"
    trailers = [f"Arete-Actor: {actor}", f"Arete-Source: {source}"]
    if draft_id:
        trailers.append(f"Arete-Draft: {draft_id}")
    if run_id:
        trailers.append(f"Arete-Run: {run_id}")
    if idem:
        trailers.append(f"Arete-Idempotency-Key: {idem}")
    return subject + "\n\n" + "\n".join(trailers) + "\n"


def _parse_commit_meta(root: Path, sha: str) -> dict:
    raw = _git(["show", "-s", "--format=%s%x00%cI%x00%an%x00%B", sha], root).stdout
    parts = raw.split("\0", 3)
    subject = parts[0].strip() if len(parts) > 0 else ""
    committed_at = parts[1].strip() if len(parts) > 1 else ""
    author_name = parts[2].strip() if len(parts) > 2 else "hand"
    body = parts[3] if len(parts) > 3 else ""
    trailers: dict[str, str] = {}
    for line in body.splitlines():
        if m := TRAILER_RE.match(line.strip()):
            trailers[m.group(1)] = m.group(2)
    at = util.iso(util.parse_iso(committed_at)) if committed_at else util.iso()
    actor = trailers.get("Arete-Actor") or author_name or "hand"
    source = trailers.get("Arete-Source") or "hand"
    if source not in SOURCES:
        source = "hand"
    return {
        "note": subject,
        "at": at,
        "author": actor,
        "source": source,
        "draft_id": trailers.get("Arete-Draft", ""),
        "run_id": trailers.get("Arete-Run", ""),
        "idem": trailers.get("Arete-Idempotency-Key", ""),
    }


def _find_idem_commit(root: Path, actor: str, idem: str) -> str:
    if not idem or not live_sha(root):
        return ""
    ref = branch_name(root)
    done = _git(
        ["log", "--first-parent", f"--grep=^Arete-Idempotency-Key: {re.escape(idem)}$",
         "--format=%H", ref],
        root,
        check=False,
    )
    if done.returncode != 0:
        return ""
    for sha in done.stdout.splitlines():
        sha = sha.strip()
        if not sha:
            continue
        meta = _parse_commit_meta(root, sha)
        if meta["author"] == actor and meta["idem"] == idem:
            return sha
    return ""


def _live_blob(root: Path, rel: str) -> str:
    ref = branch_name(root)
    if not live_sha(root):
        return ABSENT
    done = _git(["rev-parse", "--verify", f"refs/heads/{ref}:{rel}"], root, check=False)
    return done.stdout.strip() if done.returncode == 0 else ABSENT


def _live_bytes(root: Path, rel: str) -> bytes | None:
    ref = branch_name(root)
    if not live_sha(root):
        return None
    done = _git(["cat-file", "blob", f"refs/heads/{ref}:{rel}"], root, check=False, text=False)
    return done.stdout if done.returncode == 0 else None


def live_bytes(path: str | Path, root: Path | None = None) -> bytes | None:
    return _live_bytes(root or _root(), _normalize_rel(path))


def commit_bytes(commit_sha: str, path: str | Path, root: Path | None = None) -> bytes | None:
    base = root or _root()
    if not is_repo(base) or not commit_sha:
        return None
    rel = _normalize_rel(path)
    done = _git(["cat-file", "blob", f"{commit_sha}:{rel}"], base, check=False, text=False)
    return done.stdout if done.returncode == 0 else None


def resolve_ref(ref: str, root: Path | None = None) -> str:
    base = root or _root()
    if not is_repo(base) or not ref:
        return ""
    done = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], base, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def tree_sha(ref: str, root: Path | None = None) -> str:
    base = root or _root()
    if not is_repo(base) or not ref:
        return ""
    done = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}"], base, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def parents_of(commit_sha: str, root: Path | None = None) -> list[str]:
    base = root or _root()
    if not is_repo(base) or not commit_sha:
        return []
    done = _git(["show", "-s", "--format=%P", commit_sha], base, check=False)
    return done.stdout.strip().split() if done.returncode == 0 else []


def parent_sha(commit_sha: str, root: Path | None = None, which: int = 1) -> str:
    parents = parents_of(commit_sha, root)
    return parents[which - 1] if 0 < which <= len(parents) else ""


def blob_at(commit_sha: str, path: str | Path, root: Path | None = None) -> str:
    base = root or _root()
    if not is_repo(base) or not commit_sha:
        return ABSENT
    rel = _normalize_rel(path)
    done = _git(["rev-parse", "--verify", "--quiet", f"{commit_sha}:{rel}"], base, check=False)
    return done.stdout.strip() if done.returncode == 0 else ABSENT


def commit_changed_paths(commit_sha: str, root: Path | None = None, *, base: str = "") -> list[str]:
    repo_root = root or _root()
    if not is_repo(repo_root) or not commit_sha:
        return []
    parent = base or parent_sha(commit_sha, repo_root)
    args = (
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parent, commit_sha]
        if parent
        else ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-z", commit_sha]
    )
    done = _git(args, repo_root, check=False, text=False)
    if done.returncode != 0:
        return []
    return sorted(p.decode("utf-8", errors="replace") for p in done.stdout.split(b"\0") if p)


def commit_diff(base_sha: str, head_sha: str, root: Path | None = None) -> str:
    repo_root = root or _root()
    if not is_repo(repo_root) or not base_sha or not head_sha:
        return ""
    done = _git(["diff", "--no-color", base_sha, head_sha], repo_root, check=False)
    return done.stdout if done.returncode == 0 else ""


def _commit_ref_locked(
    root: Path,
    ref: str,
    changes: dict[str, bytes | None],
    actor: str,
    source: str,
    *,
    run_id: str = "",
    idem: str = "",
    note: str = "",
    draft_id: str = "",
    allow_empty: bool = False,
) -> str:
    if not ref.startswith(("refs/arete/arms/", "refs/drafts/")):
        raise ConfigRepoError(f"refused non-arm config ref {ref!r}")
    parent = live_sha(root)
    if not parent:
        raise ConfigRepoError("config repo has no live commit")
    if not changes and not allow_empty:
        raise ConfigRepoError("configrepo.commit requires at least one file")
    with tempfile.TemporaryDirectory(prefix="arete-ref-commit-") as tmp:
        idx_file = Path(tmp) / "index"
        env = _git_env(root, actor=actor)
        env["GIT_INDEX_FILE"] = str(idx_file)
        subprocess.run(
            ["git", "--no-pager", "read-tree", parent],
            cwd=root, env=env, capture_output=True, check=True, timeout=30,
        )
        for rel in sorted(changes):
            data = changes[rel]
            if data is None:
                subprocess.run(
                    ["git", "--no-pager", "update-index", "--force-remove", "--", rel],
                    cwd=root, env=env, capture_output=True, check=True, timeout=30,
                )
            else:
                hashed = subprocess.run(
                    ["git", "--no-pager", "hash-object", "-w", "--stdin"],
                    cwd=root, env=env, input=data, capture_output=True, check=True, timeout=30,
                ).stdout.decode("ascii").strip()
                subprocess.run(
                    ["git", "--no-pager", "update-index", "--add", "--cacheinfo", f"100644,{hashed},{rel}"],
                    cwd=root, env=env, capture_output=True, check=True, timeout=30,
                )
        tree = subprocess.run(
            ["git", "--no-pager", "write-tree"],
            cwd=root, env=env, capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        msg = _format_commit_message(
            note or f"arm commit on {ref}",
            actor, source, run_id, idem, draft_id=draft_id,
        )
        commit_sha = subprocess.run(
            ["git", "--no-pager", "commit-tree", tree, "-p", parent],
            cwd=root, env=env, input=msg, capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        _git(["update-ref", ref, commit_sha], root, actor=actor)
    return commit_sha


def commit_draft_ref(draft_id: str, path: str | Path, data: bytes, actor: str,
                     root: Path | None = None) -> tuple[str, str]:
    """Commit draft bytes over live onto refs/drafts/<draft_id> without touching live or working tree."""
    base = root or _root()
    if not is_repo(base):
        init()
    rel = _normalize_rel(path)
    actor = validate_actor(actor)
    parent = live_sha(base)
    if not parent:
        raise ConfigRepoError("config repo has no live commit")
    ref_name = f"refs/drafts/{draft_id}"
    with tempfile.TemporaryDirectory(prefix="arete-draft-ref-") as tmp:
        idx_file = Path(tmp) / "index"
        env = _git_env(base, actor=actor)
        env["GIT_INDEX_FILE"] = str(idx_file)
        subprocess.run(
            ["git", "--no-pager", "read-tree", parent],
            cwd=base, env=env, capture_output=True, check=True, timeout=30,
        )
        hashed = subprocess.run(
            ["git", "--no-pager", "hash-object", "-w", "--stdin"],
            cwd=base, env=env, input=data, capture_output=True, check=True, timeout=30,
        ).stdout.decode("ascii").strip()
        mode = "100644"
        subprocess.run(
            ["git", "--no-pager", "update-index", "--add", "--cacheinfo", f"{mode},{hashed},{rel}"],
            cwd=base, env=env, capture_output=True, check=True, timeout=30,
        )
        tree_sha = subprocess.run(
            ["git", "--no-pager", "write-tree"],
            cwd=base, env=env, capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        msg = _format_commit_message(f"try draft {draft_id}: {rel}", actor, "draft", draft_id=draft_id)
        commit_sha = subprocess.run(
            ["git", "--no-pager", "commit-tree", tree_sha, "-p", parent],
            cwd=base, env=env, input=msg, capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        _git(["update-ref", ref_name, commit_sha], base, actor=actor)
    return ref_name, commit_sha


def _migrate_retention_locked(root: Path, fence: Fence) -> str:
    live_cfg = _live_bytes(root, "config.toml")
    if live_cfg is None or (b"retention" not in live_cfg and b"keep_days" not in live_cfg):
        return ""
    try:
        text = live_cfg.decode("utf-8")
        data = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ""
    if not retention.has_legacy_settings(data):
        return ""
    cfg_path = root / "config.toml"
    if cfg_path.is_file() and _git_blob_sha(cfg_path.read_bytes()) != _git_blob_sha(live_cfg):
        return ""
    moved = retention.legacy_values(data)
    new_cfg = retention.strip_legacy_config_toml(text).encode("utf-8")
    changes: dict[str, bytes] = {"config.toml": new_cfg}
    if moved:
        changes[retention.FILENAME] = retention.merge_toml(_live_bytes(root, retention.FILENAME), moved)
    for rel in sorted(changes):
        _validate_content(rel, changes[rel], root)
    return _commit_files_locked(
        root,
        changes,
        "engine",
        "engine",
        note="move retention settings from config.toml to retention.toml",
        fence=fence,
        modes={"config.toml": 0o600},
        write_files=True,
        activate_c=True,
    )


def migrate_retention(*, fence: Fence | None = None) -> str:
    root = _root()
    if not is_repo(root):
        return ""
    with writer_lock(fence) as owned:
        if paths.DB_FILE.exists() and config_store.available():
            _reconcile_locked(root, owned)
        return _migrate_retention_locked(root, owned)


def read(path: str | Path) -> tuple[str, str]:
    rel = _normalize_rel(path)
    root = _root()
    target = root / rel
    if not target.is_file():
        return "", ABSENT
    data = target.read_bytes()
    return data.decode("utf-8"), _git_blob_sha(data)


def init(*, fence: Fence | None = None) -> None:
    root = _root()
    if is_repo(root):
        with writer_lock(fence) as owned:
            if paths.DB_FILE.exists() and config_store.available():
                _reconcile_locked(root, owned)
            _migrate_retention_locked(root, owned)
        return
    if bad := _forbidden_secret_entries(root):
        raise ConfigRepoError(f"cannot initialize config repo while secret files remain: {', '.join(bad)}")
    if outside := _outside_config_paths(root):
        raise ConfigRepoError(f"cannot initialize config repo with paths outside {root}: {', '.join(outside)}")
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with writer_lock(fence) as owned:
        if is_repo(root):
            return
        _write_atomic_bytes(root / ".gitignore", GITIGNORE_TEXT.encode("utf-8"), 0o644)
        _git(["init", "-q", "-b", BRANCH], root)
        _git(["config", "core.fsync", "committed,reference,index"], root)
        _git(["config", "core.fsyncMethod", "fsync"], root)
        _git(["config", "commit.gpgsign", "false"], root)

        # Commit .gitignore first so branch `live` exists before backup/subtree imports.
        _git(["add", "--", ".gitignore"], root)
        _git(["commit", "-q", "-m", _format_commit_message("init config repo", "hand", "hand"), "--", ".gitignore"],
             root, actor="hand")

        # Migrate config.toml.bak-* oldest-first by mtime, then current config.toml.
        backups = sorted(
            (p for p in root.iterdir() if p.is_file() and p.name.startswith(BACKUP_PREFIX)),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        )
        cfg_file = root / "config.toml"
        current_cfg = cfg_file.read_bytes() if cfg_file.is_file() else None
        if backups:
            for bak in backups:
                bak_bytes = bak.read_bytes()
                _write_atomic_bytes(cfg_file, bak_bytes, 0o600)
                _git(["add", "--", "config.toml"], root)
                if _git(["diff", "--cached", "--quiet", "--", "config.toml"], root, check=False).returncode != 0:
                    _git(["commit", "-q", "-m", _format_commit_message(bak.name, "hand", "hand"), "--", "config.toml"],
                         root, actor="hand")
            if current_cfg is not None:
                _write_atomic_bytes(cfg_file, current_cfg, 0o600)
                _git(["add", "--", "config.toml"], root)
                if _git(["diff", "--cached", "--quiet", "--", "config.toml"], root, check=False).returncode != 0:
                    _git(["commit", "-q", "-m", _format_commit_message("config.toml", "hand", "hand"), "--", "config.toml"],
                         root, actor="hand")
            for bak in backups:
                os.unlink(bak)
        elif current_cfg is not None:
            _git(["add", "--", "config.toml"], root)
            if _git(["diff", "--cached", "--quiet", "--", "config.toml"], root, check=False).returncode != 0:
                _git(["commit", "-q", "-m", _format_commit_message("config.toml", "hand", "hand"), "--", "config.toml"],
                     root, actor="hand")

        # Fold layers/.git so its full history lives under layers/ on branch live.
        layers_dir = root / "layers"
        if (layers_dir / ".git").exists():
            if _git(["status", "--porcelain"], layers_dir, check=False).stdout.strip():
                _git(["add", "-A"], layers_dir)
                _git(["commit", "-q", "-m", _format_commit_message("import uncommitted layer edits", "hand", "hand")],
                     layers_dir, actor="hand")
            has_commits = _git(["rev-parse", "--verify", "HEAD"], layers_dir, check=False).returncode == 0
            if has_commits:
                with tempfile.TemporaryDirectory(prefix="arete-layers-fold-") as tmp:
                    staged_layers = Path(tmp) / "layers"
                    shutil.move(str(layers_dir), str(staged_layers))
                    _fold_layers_history(root, staged_layers)
            else:
                shutil.rmtree(layers_dir / ".git", ignore_errors=True)

        # Migrate loop-switches.json into loops/<name>/loop.toml.
        switches_file = root / "loop-switches.json"
        if switches_file.is_file():
            try:
                switched = json.loads(switches_file.read_text())
            except (OSError, ValueError):
                switched = {}
            if isinstance(switched, dict):
                for loop_name, enabled in sorted(switched.items()):
                    if not isinstance(loop_name, str) or not loop_name.strip():
                        continue
                    try:
                        clean_loop = _normalize_rel(loop_name)
                    except ConfigRepoError:
                        continue
                    enabled_bool = bool(enabled)
                    if "/" in clean_loop:
                        repo_name, _, sub = clean_loop.partition("/")
                        if "/" in sub:
                            continue
                        layer_loop = root / "layers" / repo_name / "loops" / sub / "loop.toml"
                        if layer_loop.resolve().is_relative_to(root) and layer_loop.is_file():
                            updated = _set_enabled_toml(layer_loop.read_text(), enabled_bool)
                            _write_atomic_bytes(layer_loop, updated.encode("utf-8"), 0o644)
                            continue
                    target_toml = root / "loops" / clean_loop / "loop.toml"
                    if not target_toml.resolve().is_relative_to(root):
                        continue
                    if target_toml.is_file():
                        updated = _set_enabled_toml(target_toml.read_text(), enabled_bool)
                    else:
                        updated = f"enabled = {'true' if enabled_bool else 'false'}\n"
                    _write_atomic_bytes(target_toml, updated.encode("utf-8"), 0o644)
            os.unlink(switches_file)

        # Commit any remaining untracked/modified non-ignored files in CONFIG_DIR.
        remaining = _status_paths(root)
        if remaining:
            _git(["add", "--", *remaining], root)
            if _git(["diff", "--cached", "--quiet", "--", *remaining], root, check=False).returncode != 0:
                _git(["commit", "-q", "-m", _format_commit_message("import existing config", "hand", "hand"),
                      "--", *remaining], root, actor="hand")

        _install_hook(root)
        if paths.DB_FILE.exists() and config_store.available():
            _reconcile_locked(root, owned)
        _migrate_retention_locked(root, owned)


def _fold_layers_history(root: Path, staged_layers: Path) -> None:
    _git(["fetch", "-q", str(staged_layers), "HEAD"], root)
    tip = _git(["rev-parse", "FETCH_HEAD"], root).stdout.strip()
    commits = [s.strip() for s in _git(["rev-list", "--reverse", "--topo-order", tip], root).stdout.splitlines() if s.strip()]
    mapped: dict[str, str] = {}
    for old_sha in commits:
        tree_sha = _git(["rev-parse", f"{old_sha}^{{tree}}"], root).stdout.strip()
        wrapped = subprocess.run(
            ["git", "--no-pager", "mktree"],
            cwd=root,
            env=_git_env(root),
            input=f"040000 tree {tree_sha}\tlayers\n",
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.strip()
        parents_raw = _git(["show", "-s", "--format=%P", old_sha], root).stdout.strip().split()
        meta_raw = _git(["show", "-s", "--format=%aI%x00%cI%x00%B", old_sha], root).stdout
        a_date, c_date, msg = meta_raw.split("\0", 2)
        cmd = ["git", "--no-pager", "commit-tree", wrapped]
        for p in parents_raw:
            if p in mapped:
                cmd.extend(["-p", mapped[p]])
        env = _git_env(root, actor="hand")
        env["GIT_AUTHOR_DATE"] = a_date.strip()
        env["GIT_COMMITTER_DATE"] = c_date.strip()
        new_sha = subprocess.run(cmd, cwd=root, env=env, input=msg, capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        mapped[old_sha] = new_sha
    folded_tip = mapped[tip]
    _git(["merge", "-q", "--allow-unrelated-histories", "--no-ff",
          "-m", _format_commit_message("fold layers repository", "hand", "merge"), folded_tip],
         root, actor="hand")


def ensure_repo(*, fence: Fence | None = None) -> None:
    if not is_repo():
        init(fence=fence)


def _status_paths(root: Path) -> list[str]:
    done = _git(["status", "--porcelain", "-uall", "-z"], root, check=False, text=False)
    if done.returncode != 0:
        return []
    out: list[str] = []
    entries = done.stdout.split(b"\0")
    i = 0
    while i < len(entries):
        raw = entries[i]
        i += 1
        if len(raw) < 4:
            continue
        code = raw[:2].decode(errors="replace")
        rel = raw[3:].decode(errors="replace")
        if code.startswith(("R", "C")) and i < len(entries):
            i += 1
        if not rel or rel == ".gitignore" or _is_secret_rel(rel) or code.startswith("D") or code.endswith("D"):
            continue
        if (root / rel).is_file():
            out.append(rel)
    return sorted(dict.fromkeys(out))


_PUBLICATION_VALIDATORS: dict[str, Callable[[dict[str, bytes | None], Path], None]] = {}


def register_publication_validator(name: str, fn: Callable[[dict[str, bytes | None], Path], None]) -> None:
    _PUBLICATION_VALIDATORS[name] = fn


def unregister_publication_validator(name: str) -> None:
    _PUBLICATION_VALIDATORS.pop(name, None)


def live_tree_files(root: Path | None = None) -> dict[str, bytes]:
    target = _root() if root is None else root
    if not is_repo(target) or not live_sha(target):
        return {}
    ref = branch_name(target)
    done = _git(["ls-tree", "-r", "-z", "--name-only", ref], target, check=False, text=False)
    if done.returncode != 0:
        raise ConfigRepoError(f"cannot list live config tree at {ref}")
    out: dict[str, bytes] = {}
    for entry in done.stdout.split(b"\0"):
        if not entry:
            continue
        rel = entry.decode("utf-8", errors="replace")
        if rel == ".gitignore" or _is_secret_rel(rel):
            continue
        blob = _git(["cat-file", "blob", f"{ref}:{rel}"], target, check=False, text=False)
        if blob.returncode != 0:
            raise ConfigRepoError(f"cannot read live config blob {ref}:{rel}")
        out[rel] = blob.stdout
    return out


def _commit_files_locked(root: Path, changes: dict[str, bytes | None], actor: str, source: str, *,
                         run_id: str = "", idem: str = "", note: str = "", draft_id: str = "",
                         activate_c: bool = False, fence: Fence,
                         modes: dict[str, int] | None = None, write_files: bool = True,
                         merge_parent: str = "") -> str:
    from . import lines as lines_mod

    lines_mod.validate_config_line_references(changes, root)
    for validator in tuple(_PUBLICATION_VALIDATORS.values()):
        validator(changes, root)
    paths_sorted = sorted(changes)
    old_blobs = {rel: _live_blob(root, rel) for rel in paths_sorted}
    merge_sha = ""
    if merge_parent:
        merge_sha = resolve_ref(merge_parent, root)
        if not merge_sha:
            raise ConfigRepoError(f"invalid merge parent commit {merge_parent!r}")
    previous_working: dict[str, bytes | None] = {}
    if write_files:
        for rel in paths_sorted:
            target = root / rel
            previous_working[rel] = target.read_bytes() if target.is_file() else None
            data = changes[rel]
            if data is None:
                with suppress(FileNotFoundError):
                    os.unlink(target)
            else:
                _write_atomic_bytes(target, data, (modes or {}).get(rel))
    to_add = [rel for rel in paths_sorted if changes[rel] is not None]
    to_del = [rel for rel in paths_sorted if changes[rel] is None]
    if to_add:
        _git(["add", "--", *to_add], root)
    if to_del:
        _git(["rm", "-q", "-f", "--ignore-unmatch", "--", *to_del], root)
    if not merge_sha and _git(["diff", "--cached", "--quiet", "--", *paths_sorted], root, check=False).returncode == 0:
        return live_sha(root)
    msg = _format_commit_message(
        note or f"config: update {', '.join(paths_sorted)}",
        actor, source, run_id, idem, draft_id=draft_id,
    )
    try:
        with _active_hook_fence(root, fence) as token:
            if merge_sha:
                hook = root / ".git" / "hooks" / "pre-commit"
                if hook.is_file():
                    hook_env = _git_env(root, actor=actor, fence_token=token, use_hooks=True, activate_c=activate_c)
                    hook_done = subprocess.run(
                        [str(hook)],
                        cwd=root,
                        env=hook_env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        stdin=subprocess.DEVNULL,
                    )
                    if hook_done.returncode != 0:
                        raise ConfigRepoError(hook_done.stderr.strip() or hook_done.stdout.strip() or "pre-commit hook failed")
                parent = live_sha(root)
                tree = _git(["write-tree"], root, actor=actor).stdout.strip()
                commit_env = _git_env(root, actor=actor, fence_token=token, activate_c=activate_c)
                commit_done = subprocess.run(
                    ["git", "--no-pager", "commit-tree", tree, "-p", parent, "-p", merge_sha],
                    cwd=root,
                    env=commit_env,
                    input=msg,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if commit_done.returncode != 0:
                    raise ConfigRepoError(commit_done.stderr.strip() or "git commit-tree failed")
                new_sha = commit_done.stdout.strip()
                _git(["update-ref", f"refs/heads/{branch_name(root)}", new_sha], root, actor=actor)
            else:
                _git(
                    ["commit", "-q", "-m", msg, "--", *paths_sorted],
                    root, actor=actor, fence_token=token, use_hooks=True, activate_c=activate_c,
                )
    except ConfigRepoError:
        for rel in paths_sorted:
            live_data = _live_bytes(root, rel)
            target = root / rel
            if live_data is not None:
                _write_atomic_bytes(target, live_data, (modes or {}).get(rel))
            else:
                with suppress(FileNotFoundError):
                    os.unlink(target)
        _git(["reset", "-q", "HEAD", "--", *paths_sorted], root, check=False)
        raise
    sha = live_sha(root)
    _index_commit(root, sha, paths_sorted, changes, old_blobs)
    return sha


def _index_commit(root: Path, sha: str, paths_sorted: list[str], changes: dict[str, bytes | None],
                  old_blobs: dict[str, str]) -> None:
    if not paths.DB_FILE.exists() or not config_store.available():
        return
    from . import drafts

    meta = _parse_commit_meta(root, sha)
    with foundation.tx() as conn, principals.acting(meta["author"]):
        for rel in paths_sorted:
            if rel == ".gitignore":
                continue
            raw = changes[rel]
            kind, name = classify_path(rel)
            origin = origin_of(rel)
            if raw is None:
                config_store.record_deleted(
                    conn,
                    kind=kind,
                    name=name,
                    origin=origin,
                    path=rel,
                    commit_sha=sha,
                    author=meta["author"],
                    source=meta["source"],
                    note=meta["note"],
                    at=meta["at"],
                )
                events.emit(
                    f"config.{kind}.changed",
                    f"config:{kind}/{name}",
                    cls="durable",
                    data={
                        "path": rel,
                        "commit": sha,
                        "old_blob": old_blobs.get(rel, ABSENT),
                        "new_blob": ABSENT,
                        "kind": kind,
                        "name": name,
                        "origin": origin,
                        "source": meta["source"],
                    },
                    connection=conn,
                )
                continue
            digest = hashlib.sha256(raw).hexdigest()
            content_version = f"{kind}/{name}@{digest[:12]}"
            new_blob = _git_blob_sha(raw)
            ver = config_store.upsert_live(
                conn,
                kind=kind,
                name=name,
                origin=origin,
                path=rel,
                sha256=digest,
                version=content_version,
                commit_sha=sha,
                author=meta["author"],
                source=meta["source"],
                note=meta["note"],
                at=meta["at"],
                fields="{}",
            )
            events.emit(
                f"config.{kind}.changed",
                f"config:{kind}/{name}",
                cls="durable",
                ver=ver,
                data={
                    "path": rel,
                    "commit": sha,
                    "old_blob": old_blobs.get(rel, ABSENT),
                    "new_blob": new_blob,
                    "kind": kind,
                    "name": name,
                    "origin": origin,
                    "version": ver,
                    "content_version": content_version,
                    "source": meta["source"],
                },
                connection=conn,
            )
        if meta.get("draft_id"):
            drafts.finalize_activated_draft(conn, meta["draft_id"], sha)


def commit(changes: dict[str, str | bytes | None], base: dict[str, str], actor: str, source: str,
           run_id: str = "", idem: str = "", *, note: str = "", draft_id: str = "",
           activate_c: bool = False, fence: Fence | None = None,
           modes: dict[str, int] | None = None, write_files: bool = True,
           ref: str = "", allow_empty: bool = False, merge_parent: str = "") -> str:
    actor = validate_actor(actor)
    if source not in SOURCES:
        raise ConfigRepoError(f"invalid config commit source {source!r}")
    if not changes and not (ref and allow_empty):
        raise ConfigRepoError("configrepo.commit requires at least one file")
    normalized: dict[str, bytes | None] = {}
    norm_base: dict[str, str] = {}
    norm_modes: dict[str, int] = {}
    for raw_path, content in changes.items():
        rel = _normalize_rel(raw_path)
        data = None if content is None else (content.encode("utf-8") if isinstance(content, str) else bytes(content))
        normalized[rel] = data
        if not ref:
            if raw_path in base:
                norm_base[rel] = base[raw_path]
            elif rel in base:
                norm_base[rel] = base[rel]
            else:
                raise ConfigRepoError(f"missing base blob for {rel}")
        if modes and raw_path in modes:
            norm_modes[rel] = modes[raw_path]
        elif modes and rel in modes:
            norm_modes[rel] = modes[rel]
    root = _root()
    if not is_repo(root):
        init(fence=fence)
    with writer_lock(fence) as owned:
        if paths.DB_FILE.exists() and config_store.available():
            _reconcile_locked(root, owned)
        if ref:
            if normalized:
                _validate_batch(normalized, root)
            return _commit_ref_locked(
                root,
                ref,
                normalized,
                actor,
                source,
                run_id=run_id,
                idem=idem,
                note=note,
                draft_id=draft_id,
                allow_empty=allow_empty,
            )
        if idem and (prior := _find_idem_commit(root, actor, idem)):
            return prior
        # Check each path independently for uncommitted hand edits and stale base.
        for rel in sorted(normalized):
            live_blob = _live_blob(root, rel)
            if write_files:
                target = root / rel
                wt_bytes = target.read_bytes() if target.is_file() else None
                wt_blob = _git_blob_sha(wt_bytes) if wt_bytes is not None else ABSENT
                if wt_blob != live_blob:
                    if wt_bytes is None:
                        raise HandEditInvalid(rel, "working tree file was removed")
                    try:
                        _validate_content(rel, wt_bytes, root)
                    except ConfigRepoError as e:
                        raise HandEditInvalid(rel, str(e)) from e
                    from . import configcheck

                    ok_h, gate_h, probs_h = configcheck.check_staged_files({rel: wt_bytes}, root)
                    if not ok_h:
                        raise HandEditInvalid(rel, "; ".join(probs_h) or "configcheck failed")
                    if gate_h == "C":
                        raise HandEditInvalid(rel, "working tree has uncommitted gate C hand edit")
                    _commit_files_locked(
                        root,
                        {rel: wt_bytes},
                        "hand",
                        "hand",
                        note=f"hand edit: {rel}",
                        fence=owned,
                        write_files=False,
                    )
                    live_blob = _live_blob(root, rel)
            if norm_base[rel] != live_blob:
                raise StaleBase(rel, norm_base[rel], live_blob)
        # Validate every new file BEFORE touching the working tree.
        _validate_batch(normalized, root)
        return _commit_files_locked(
            root,
            normalized,
            actor,
            source,
            run_id=run_id,
            idem=idem,
            note=note,
            draft_id=draft_id,
            activate_c=activate_c,
            fence=owned,
            modes=norm_modes,
            write_files=write_files,
            merge_parent=merge_parent,
        )


def _commit_diff_entries(root: Path, sha: str) -> list[tuple[str, str]]:
    done = _git(["diff-tree", "--no-commit-id", "--name-status", "-r", "--root", "-m", "--first-parent", "-z", sha],
                root, text=False)
    parts = done.stdout.split(b"\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(parts):
        status = parts[i].decode(errors="replace")
        rel = parts[i + 1].decode(errors="replace")
        i += 2
        if not status or not rel:
            continue
        if status.startswith(("R", "C")) and i < len(parts):
            rel = parts[i].decode(errors="replace")
            i += 1
        entries.append((status[0], rel))
    return entries


def _walk_history_rows(root: Path) -> list[dict]:
    if not live_sha(root):
        return []
    ref = branch_name(root)
    commits = [s.strip() for s in _git(["log", "--first-parent", "--reverse", "--format=%H", ref], root).stdout.splitlines() if s.strip()]
    rows: dict[tuple[str, str, str], dict] = {}
    max_n: dict[tuple[str, str], int] = {}
    live_key: dict[tuple[str, str], tuple[str, str, str]] = {}
    item_ver: dict[tuple[str, str], int] = {}
    item_live_sha: dict[tuple[str, str], str] = {}
    for sha in commits:
        meta = _parse_commit_meta(root, sha)
        for status, rel in _commit_diff_entries(root, sha):
            if rel == ".gitignore" or _is_secret_rel(rel):
                continue
            origin = origin_of(rel)
            op = (origin, rel)
            if status == "D":
                if prev := live_key.pop(op, None):
                    rows[prev]["live"] = 0
                kind, name = classify_path(rel)
                item_live_sha.pop((kind, name), None)
                del_key = (origin, rel, ABSENT)
                if del_key in rows:
                    rows[del_key].update(
                        kind=kind,
                        name=name,
                        version=f"{kind}/{name}@absent",
                        commit_sha=sha,
                        author=meta["author"],
                        source=meta["source"],
                        note=meta["note"],
                        at=meta["at"],
                        live=0,
                        fields="{}",
                        ver=0,
                    )
                else:
                    n = max_n.get(op, 0) + 1
                    max_n[op] = n
                    rows[del_key] = {
                        "kind": kind,
                        "name": name,
                        "origin": origin,
                        "path": rel,
                        "sha256": ABSENT,
                        "version": f"{kind}/{name}@absent",
                        "n": n,
                        "commit_sha": sha,
                        "author": meta["author"],
                        "source": meta["source"],
                        "note": meta["note"],
                        "at": meta["at"],
                        "live": 0,
                        "fields": "{}",
                        "ver": 0,
                    }
                continue
            blob = _git(["cat-file", "blob", f"{sha}:{rel}"], root, check=False, text=False)
            if blob.returncode != 0:
                continue
            raw = blob.stdout
            digest = hashlib.sha256(raw).hexdigest()
            kind, name = classify_path(rel)
            content_version = f"{kind}/{name}@{digest[:12]}"
            if prev := live_key.get(op):
                rows[prev]["live"] = 0
            kn = (kind, name)
            ver = item_ver.get(kn, 0)
            if origin != "engine" and item_live_sha.get(kn) != digest:
                ver += 1
                item_ver[kn] = ver
                item_live_sha[kn] = digest
            key = (origin, rel, digest)
            if key in rows:
                row = rows[key]
                row.update(
                    kind=kind,
                    name=name,
                    version=content_version,
                    commit_sha=sha,
                    author=meta["author"],
                    source=meta["source"],
                    note=meta["note"],
                    at=meta["at"],
                    live=1,
                    fields="{}",
                    ver=ver if origin != "engine" else 0,
                )
            else:
                n = max_n.get(op, 0) + 1
                max_n[op] = n
                rows[key] = {
                    "kind": kind,
                    "name": name,
                    "origin": origin,
                    "path": rel,
                    "sha256": digest,
                    "version": content_version,
                    "n": n,
                    "commit_sha": sha,
                    "author": meta["author"],
                    "source": meta["source"],
                    "note": meta["note"],
                    "at": meta["at"],
                    "live": 1,
                    "fields": "{}",
                    "ver": ver if origin != "engine" else 0,
                }
            live_key[op] = key
    return sorted(rows.values(), key=lambda r: (r["origin"], r["path"], r["n"], r["sha256"]))


def _rebuild_index_locked(root: Path) -> list[dict]:
    built = _walk_history_rows(root)
    if paths.DB_FILE.exists() and config_store.available():
        with foundation.tx() as conn:
            config_store.replace_all(conn, built)
    return built


def rebuild_index(*, fence: Fence | None = None) -> list[dict]:
    root = _root()
    if not is_repo(root):
        return []
    with writer_lock(fence):
        return _rebuild_index_locked(root)


def _reconcile_locked(root: Path, fence: Fence) -> int:
    if not is_repo(root) or not paths.DB_FILE.exists() or not config_store.available():
        return 0
    if not live_sha(root):
        return 0
    with foundation.reader() as conn:
        if config_store.needs_ver_rebuild(conn):
            _rebuild_index_locked(root)
            return 1
        seen = config_store.indexed_commits(conn)
    ref = branch_name(root)
    commits = [s.strip() for s in _git(["log", "--first-parent", "--reverse", "--format=%H", ref], root).stdout.splitlines() if s.strip()]
    last_indexed = max((i for i, sha in enumerate(commits) if sha in seen), default=-1)
    missing = commits[last_indexed + 1:]
    if not missing:
        return 0
    reconciled = 0
    with foundation.tx() as conn:
        for sha in missing:
            meta = _parse_commit_meta(root, sha)
            parent_done = _git(["rev-parse", "--verify", f"{sha}^1"], root, check=False)
            parent = parent_done.stdout.strip() if parent_done.returncode == 0 else ""
            entries = _commit_diff_entries(root, sha)
            valid_entries = [(st, rel) for st, rel in entries if rel != ".gitignore" and not _is_secret_rel(rel)]
            if not valid_entries:
                continue
            with principals.acting(meta["author"]):
                for status, rel in valid_entries:
                    origin = origin_of(rel)
                    if status == "D":
                        kind, name = classify_path(rel)
                        config_store.record_deleted(
                            conn,
                            kind=kind,
                            name=name,
                            origin=origin,
                            path=rel,
                            commit_sha=sha,
                            author=meta["author"],
                            source=meta["source"],
                            note=meta["note"],
                            at=meta["at"],
                        )
                        continue
                    blob = _git(["cat-file", "blob", f"{sha}:{rel}"], root, check=False, text=False)
                    if blob.returncode != 0:
                        continue
                    raw = blob.stdout
                    kind, name = classify_path(rel)
                    digest = hashlib.sha256(raw).hexdigest()
                    content_version = f"{kind}/{name}@{digest[:12]}"
                    old_blob = ABSENT
                    if parent:
                        prev = _git(["rev-parse", "--verify", f"{parent}:{rel}"], root, check=False)
                        if prev.returncode == 0:
                            old_blob = prev.stdout.strip()
                    ver = config_store.upsert_live(
                        conn,
                        kind=kind,
                        name=name,
                        origin=origin,
                        path=rel,
                        sha256=digest,
                        version=content_version,
                        commit_sha=sha,
                        author=meta["author"],
                        source=meta["source"],
                        note=meta["note"],
                        at=meta["at"],
                        fields="{}",
                    )
                    events.emit(
                        f"config.{kind}.changed",
                        f"config:{kind}/{name}",
                        cls="durable",
                        ver=ver,
                        data={
                            "path": rel,
                            "commit": sha,
                            "old_blob": old_blob,
                            "new_blob": _git_blob_sha(raw),
                            "kind": kind,
                            "name": name,
                            "origin": origin,
                            "version": ver,
                            "content_version": content_version,
                            "source": meta["source"],
                        },
                        connection=conn,
                    )
                if meta.get("draft_id"):
                    from . import drafts

                    drafts.finalize_activated_draft(conn, meta["draft_id"], sha)
            reconciled += 1
    return reconciled


def reconcile(*, fence: Fence | None = None) -> int:
    root = _root()
    if not is_repo(root):
        return 0
    with writer_lock(fence) as owned:
        return _reconcile_locked(root, owned)


def _log_invalid_hand_once(root: Path, rel: str, blob_sha: str, detail: str) -> None:
    key = (str(root), rel)
    with _LOG_GUARD:
        if _INVALID_HAND_LOGGED.get(key) != blob_sha:
            _INVALID_HAND_LOGGED[key] = blob_sha
            print(f"config hand edit not committed ({rel}): {util.first_line(detail, 200)}",
                  file=sys.stderr, flush=True)


def commit_hand_edits(*, fence: Fence | None = None) -> list[str]:
    from . import configcheck, drafts, inbox

    root = _root()
    if not is_repo(root):
        return []
    committed: list[str] = []
    has_drafts = paths.DB_FILE.exists() and drafts.available()
    with writer_lock(fence) as owned:
        if paths.DB_FILE.exists() and config_store.available():
            _reconcile_locked(root, owned)
        dirty_list = _status_paths(root)
        if has_drafts:
            dirty_set = set(dirty_list)
            stale_hands = [
                d for d in drafts.list_drafts(state=("draft", "valid", "invalid"), author="hand", limit=100)
                if d["path"] and d["path"] not in dirty_set
            ]
            if stale_hands:
                with foundation.tx() as c, principals.acting("hand"):
                    for prior in stale_hands:
                        if prior["state"] == "valid":
                            drafts.move(c, prior["numeric_id"], "valid", "discarded",
                                        reason="hand edit reverted to live", sync_inbox=False)
                        elif prior["state"] in ("draft", "invalid"):
                            drafts.move(c, prior["numeric_id"], prior["state"], "discarded",
                                        reason="hand edit reverted to live", sync_inbox=False)
                    inbox.sync(c, "proposals")
                    inbox.sync(c, "config")
        for rel in dirty_list:
            target = root / rel
            if not target.is_file():
                continue
            raw = target.read_bytes()
            blob_sha = _git_blob_sha(raw)
            live_blob = _live_blob(root, rel)
            if blob_sha == live_blob:
                continue
            key = (str(root), rel)
            classified = configkinds.classify(rel)
            if has_drafts and classified is not None:
                kind, item_name = classified
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    _log_invalid_hand_once(root, rel, blob_sha, str(error))
                    continue
                # Skip if an identical open hand draft already exists for this path and base.
                existing_open = [
                    d for d in drafts.list_drafts(state=("draft", "valid", "invalid"), author="hand", limit=50)
                    if d["path"] == rel
                ]
                same = next(
                    (d for d in existing_open if d["body"] == text and d["base_version"] == live_blob),
                    None,
                )
                if same is not None:
                    if same["state"] == "invalid":
                        probs = [
                            p for st in (same.get("check_report") or {}).get("stages", ())
                            for p in st.get("problems", ())
                        ]
                        _log_invalid_hand_once(root, rel, blob_sha, "; ".join(probs) or "invalid hand edit")
                    continue
                old_bytes = _live_bytes(root, rel)
                report = configcheck.check(
                    kind, item_name, rel, text, author="hand", config_dir=root, old_bytes=old_bytes
                )
                diff_text = drafts._unified_diff(rel, old_bytes, text)
                with foundation.tx() as c, principals.acting("hand"):
                    for prior in existing_open:
                        if prior["state"] == "valid":
                            drafts.move(c, prior["numeric_id"], "valid", "superseded",
                                        reason="replaced by newer hand edit", sync_inbox=False)
                        elif prior["state"] in ("draft", "invalid"):
                            drafts.move(c, prior["numeric_id"], prior["state"], "discarded",
                                        reason="replaced by newer hand edit", sync_inbox=False)
                    draft_num = drafts.add(
                        c,
                        kind=kind,
                        name=item_name,
                        origin=origin_of(rel),
                        path=rel,
                        base_version=live_blob,
                        body=text,
                        diff=diff_text,
                        author="hand",
                        why=f"hand edit: {rel}",
                        gate=report["gate"],
                        check_report=report,
                        validator=str(report.get("validator") or ""),
                        state="draft",
                        evidence=[],
                        sync_inbox=False,
                    )
                    if report["ok"]:
                        drafts.move(c, draft_num, "draft", "valid", sync_inbox=False)
                    else:
                        drafts.move(c, draft_num, "draft", "invalid", sync_inbox=False)
                    inbox.sync(c, "proposals")
                    inbox.sync(c, "config")
                if not report["ok"]:
                    probs = [p for st in report["stages"] for p in st.get("problems", ())]
                    _log_invalid_hand_once(root, rel, blob_sha, "; ".join(probs) or "configcheck failed")
                    continue
                with _LOG_GUARD:
                    _INVALID_HAND_LOGGED.pop(key, None)
                if report["gate"] in ("A", "B"):
                    sha = _commit_files_locked(
                        root,
                        {rel: raw},
                        "hand",
                        "hand",
                        note=f"hand edit: {rel}",
                        draft_id=drafts.format_id(draft_num),
                        fence=owned,
                        write_files=False,
                    )
                    committed.append(sha)
                # Gate C hand draft stays valid with one proposal item and live unchanged.
                continue

            try:
                _validate_content(rel, raw, root)
            except ConfigRepoError as e:
                _log_invalid_hand_once(root, rel, blob_sha, str(e))
                continue
            with _LOG_GUARD:
                _INVALID_HAND_LOGGED.pop(key, None)
            sha = _commit_files_locked(
                root,
                {rel: raw},
                "hand",
                "hand",
                note=f"hand edit: {rel}",
                fence=owned,
                write_files=False,
            )
            committed.append(sha)
    return committed


def version_history(kind: str, name: str, path: str) -> list[dict]:
    """Return newest-first version entries for (kind, name) from first-parent git history."""
    root = _root()
    if not is_repo(root) or not live_sha(root):
        return []
    ref = branch_name(root)
    done = _git(["log", "--first-parent", "--reverse", "--format=%H", ref, "--", path], root, check=False)
    if done.returncode != 0:
        return []
    commits = [s.strip() for s in done.stdout.splitlines() if s.strip()]
    origin = origin_of(path)
    entries: list[dict] = []
    seen_n: dict[str, int] = {}
    ver = 0
    prev_sha = ""
    for sha in commits:
        blob = _git(["cat-file", "blob", f"{sha}:{path}"], root, check=False, text=False)
        if blob.returncode != 0:
            continue
        raw = blob.stdout
        digest = hashlib.sha256(raw).hexdigest()
        if digest != prev_sha:
            ver += 1
            prev_sha = digest
        if digest not in seen_n:
            seen_n[digest] = len(seen_n) + 1
        meta = _parse_commit_meta(root, sha)
        entries.append({
            "version": ver,
            "content_version": f"{kind}/{name}@{digest[:12]}",
            "commit_sha": sha,
            "sha256": digest,
            "origin": origin,
            "path": path,
            "n": seen_n[digest],
            "author": meta["author"],
            "source": meta["source"],
            "note": meta["note"],
            "at": meta["at"],
            "live": False,
        })
    if entries:
        entries[-1]["live"] = True
    entries.reverse()
    return entries
