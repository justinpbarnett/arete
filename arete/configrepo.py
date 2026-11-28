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
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from . import config, configkinds, events, paths, principals, refusals, util
from .store import config as config_store
from .store import db as foundation

ABSENT = "absent"
BRANCH = "live"
SOURCES = frozenset(("dashboard", "hand", "agent", "revert", "merge", "engine"))
FENCE_ENV = "ARETE_CONFIG_FENCE"
FENCE_FILE = ".arete-fence"
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


def _git_env(root: Path, *, actor: str = "", fence_token: str = "", use_hooks: bool = False) -> dict[str, str]:
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
    if not use_hooks:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
        env["GIT_CONFIG_VALUE_0"] = os.devnull
    return util.foreground_maintenance(env)


def _git(args: list[str], cwd: Path, *, actor: str = "", fence_token: str = "", use_hooks: bool = False,
         check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "--no-pager", "--no-replace-objects"]
    if use_hooks:
        cmd.extend(["-c", f"core.hooksPath={cwd / '.git' / 'hooks'}"])
    else:
        cmd.extend(["-c", "core.hooksPath=/dev/null"])
    cmd.extend(["-c", "commit.gpgsign=false", *args])
    done = subprocess.run(
        cmd,
        cwd=cwd,
        env=_git_env(cwd, actor=actor, fence_token=fence_token, use_hooks=use_hooks),
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


def _validate_content(rel: str, raw: bytes, root: Path) -> None:
    if rel.endswith(".toml"):
        try:
            text = raw.decode("utf-8")
            tomllib.loads(text)
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
    if _ACTIVE_FENCE.get() is not None:
        raise RuntimeError("instance fence is already held in this context; pass the existing fence instead of reacquiring")
    with util.lock(INSTANCE_LOCK, wait=True):
        fence = Fence(token=secrets.token_hex(16), pid=os.getpid())
        tok = _ACTIVE_FENCE.set(fence)
        try:
            yield fence
        finally:
            _ACTIVE_FENCE.reset(tok)


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
    for rel in staged:
        if _is_secret_rel(rel):
            print(f"configrepo pre-commit hook: refused secret file {rel}", file=sys.stderr)
            return 1
        blob = _git(["cat-file", "blob", f":{rel}"], root, check=False, text=False)
        if blob.returncode != 0:
            print(f"configrepo pre-commit hook: could not read staged {rel}", file=sys.stderr)
            return 1
        try:
            _validate_content(rel, blob.stdout, root)
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
        if not token or not expected or token != expected:
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


def _format_commit_message(note: str, actor: str, source: str, run_id: str = "", idem: str = "") -> str:
    subject = (note or "config update").splitlines()[0].strip() or "config update"
    trailers = [f"Arete-Actor: {actor}", f"Arete-Source: {source}"]
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
        if paths.DB_FILE.exists() and config_store.available():
            with writer_lock(fence) as owned:
                _reconcile_locked(root, owned)
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
                    enabled_bool = bool(enabled)
                    if "/" in loop_name:
                        repo_name, _, sub = loop_name.partition("/")
                        layer_loop = root / "layers" / repo_name / "loops" / sub / "loop.toml"
                        if layer_loop.is_file():
                            updated = _set_enabled_toml(layer_loop.read_text(), enabled_bool)
                            _write_atomic_bytes(layer_loop, updated.encode("utf-8"), 0o644)
                            continue
                    target_toml = root / "loops" / loop_name / "loop.toml"
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


def _commit_files_locked(root: Path, changes: dict[str, bytes], actor: str, source: str, *,
                         run_id: str = "", idem: str = "", note: str = "", fence: Fence,
                         modes: dict[str, int] | None = None, write_files: bool = True) -> str:
    paths_sorted = sorted(changes)
    old_blobs = {rel: _live_blob(root, rel) for rel in paths_sorted}
    previous_working: dict[str, bytes | None] = {}
    if write_files:
        for rel in paths_sorted:
            target = root / rel
            previous_working[rel] = target.read_bytes() if target.is_file() else None
            _write_atomic_bytes(target, changes[rel], (modes or {}).get(rel))
    _git(["add", "--", *paths_sorted], root)
    if _git(["diff", "--cached", "--quiet", "--", *paths_sorted], root, check=False).returncode == 0:
        return live_sha(root)
    msg = _format_commit_message(note or f"config: update {', '.join(paths_sorted)}", actor, source, run_id, idem)
    try:
        with _active_hook_fence(root, fence) as token:
            _git(["commit", "-q", "-m", msg, "--", *paths_sorted], root, actor=actor, fence_token=token, use_hooks=True)
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


def _index_commit(root: Path, sha: str, paths_sorted: list[str], changes: dict[str, bytes],
                  old_blobs: dict[str, str]) -> None:
    if not paths.DB_FILE.exists() or not config_store.available():
        return
    meta = _parse_commit_meta(root, sha)
    with foundation.tx() as conn, principals.acting(meta["author"]):
        for rel in paths_sorted:
            if rel == ".gitignore":
                continue
            raw = changes[rel]
            kind, name = classify_path(rel)
            origin = origin_of(rel)
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


def commit(changes: dict[str, str | bytes], base: dict[str, str], actor: str, source: str,
           run_id: str = "", idem: str = "", *, note: str = "", fence: Fence | None = None,
           modes: dict[str, int] | None = None) -> str:
    actor = validate_actor(actor)
    if source not in SOURCES:
        raise ConfigRepoError(f"invalid config commit source {source!r}")
    if not changes:
        raise ConfigRepoError("configrepo.commit requires at least one file")
    normalized: dict[str, bytes] = {}
    norm_base: dict[str, str] = {}
    norm_modes: dict[str, int] = {}
    for raw_path, content in changes.items():
        rel = _normalize_rel(raw_path)
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        normalized[rel] = data
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
        if idem and (prior := _find_idem_commit(root, actor, idem)):
            return prior
        # Check each path independently for uncommitted hand edits and stale base.
        for rel in sorted(normalized):
            live_blob = _live_blob(root, rel)
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
        for rel in sorted(normalized):
            _validate_content(rel, normalized[rel], root)
        return _commit_files_locked(
            root,
            normalized,
            actor,
            source,
            run_id=run_id,
            idem=idem,
            note=note,
            fence=owned,
            modes=norm_modes,
            write_files=True,
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
                        config_store.mark_deleted(conn, origin, rel)
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
            reconciled += 1
    return reconciled


def reconcile(*, fence: Fence | None = None) -> int:
    root = _root()
    if not is_repo(root):
        return 0
    with writer_lock(fence) as owned:
        return _reconcile_locked(root, owned)


def commit_hand_edits(*, fence: Fence | None = None) -> list[str]:
    root = _root()
    if not is_repo(root):
        return []
    committed: list[str] = []
    with writer_lock(fence) as owned:
        if paths.DB_FILE.exists() and config_store.available():
            _reconcile_locked(root, owned)
        for rel in _status_paths(root):
            target = root / rel
            if not target.is_file():
                continue
            raw = target.read_bytes()
            blob_sha = _git_blob_sha(raw)
            live_blob = _live_blob(root, rel)
            if blob_sha == live_blob:
                continue
            key = (str(root), rel)
            try:
                _validate_content(rel, raw, root)
            except ConfigRepoError as e:
                with _LOG_GUARD:
                    if _INVALID_HAND_LOGGED.get(key) != blob_sha:
                        _INVALID_HAND_LOGGED[key] = blob_sha
                        print(f"config hand edit not committed ({rel}): {util.first_line(str(e), 200)}",
                              file=sys.stderr, flush=True)
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
