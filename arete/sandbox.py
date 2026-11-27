from __future__ import annotations

import ctypes
import fcntl
import os
import pwd
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, closing, suppress
from fnmatch import fnmatch
from pathlib import Path

from . import paths

SESSIONS_DIR = paths.STATE_DIR / "pi-sessions"
AGENT_DIR = paths.STATE_DIR / "pi-agent"
LOGIN_DIR = paths.STATE_DIR / "pi-login"
KEY_DIR = paths.STATE_DIR / "pi-key"
LINUX_SHM = Path("/dev/shm")
TEMPORARY_ROOTS = ("/private/tmp", "/private/var/folders")
WRITABLE_ALWAYS = (*TEMPORARY_ROOTS, "/dev")
WRITABLE_DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/dtracehelper", "/dev/tty")
NO_CONFINEMENT_REASON = "pi keeps file writes inside the work folder only with the macOS sandbox on this Mac"
SECRET_NAMES = (".env", ".env.*", "*.env", ".envrc", ".netrc", ".pgpass", ".npmrc", ".pypirc", "id_rsa*", "id_dsa*",
                "id_ecdsa*", "id_ed25519*", "*.pem", "*.p12", "*.pfx", "*.key", "credentials", "credentials.json",
                "*.secret", "*.secrets", "secrets.toml", "secrets.json")
SAFE_NAMES = ("*.example", "*.sample", "*.template", "*.dist")
SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".password-store"})
AUTHORITY_NAME = "verification-authority"
AUTHORITY_MIGRATION_RECEIPT = f"{AUTHORITY_NAME}.migrated"
try:
    ACCOUNT_CONFIG_DIR = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/arete"
except KeyError:
    ACCOUNT_CONFIG_DIR = paths.NOWHERE
LEGACY_AUTHORITY = ACCOUNT_CONFIG_DIR / AUTHORITY_NAME
AUTHORITY = (paths.NOWHERE if paths.ISOLATED or (paths.GUARD_HOOK and paths.inside_scratch(paths.STATE_DIR))
             else paths.STATE_DIR / AUTHORITY_NAME)


class ConfinementUnavailable(PermissionError):
    pass


class WorkerLease(ExitStack):
    active = False

    def acquire(self) -> None:
        if sys.platform == "linux":
            from . import secretstore

            self.enter_context(secretstore.credential_activation())
            self.active = True
            self.callback(setattr, self, "active", False)

    def command(self, argv, descriptors=()):
        if not self.active:
            return argv
        entry = (f"import os, sys; env = os.environ.copy(); sys.path.insert(0, {str(paths.PACKAGE_DIR.parent)!r}); "
                 "from arete.sandbox import linux_worker_relay; sys.exit(linux_worker_relay(env, *sys.argv[1:]))")
        return [sys.executable, "-I", "-c", entry, str(os.getpid()), str(paths.STATE_DIR.resolve()),
                ",".join(map(str, descriptors)), *argv]


def worker_authority(lease: WorkerLease) -> tuple[Path, ...]:
    roots = authority_roots()
    if lease.active:
        with suppress(OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error):
            store, active = linux_store_policy(has_local=paths.FINGERPRINT_KEY_FILE.exists(), had_local=False)
            if active and store in roots:
                lease.close()
    return roots


def drain_worker_children(children: Path) -> None:
    while True:
        try:
            if os.waitpid(-1, os.WNOHANG)[0]:
                continue
        except ChildProcessError:
            return
        try:
            live = children.read_text().split()
        except OSError:
            os.waitpid(-1, 0)
            continue
        for pid in live:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(int(pid), signal.SIGKILL)
        time.sleep(0.02)


def monitor_linux_worker(peer, descriptors, command, env) -> None:
    from .check_process import PR_SET_CHILD_SUBREAPER

    if ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise ConfinementUnavailable("could not supervise the Linux worker's descendants")
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    children.read_text()
    if select.select([peer], [], [], 0)[0]:
        return
    proc = subprocess.Popen(command, env=env, pass_fds=descriptors, start_new_session=True)
    try:
        for fd in (0, 1, 2, *descriptors):
            os.close(fd)
        while proc.poll() is None and not select.select([peer], [], [], 0.05)[0]:
            pass
    finally:
        with suppress(PermissionError):
            proc.kill()
        code = proc.wait()
        drain_worker_children(children)
    with suppress(BrokenPipeError):
        peer.sendall(str(code if code >= 0 else 128 - code).encode())


def linux_worker_relay(env, parent, state, descriptors, *command):
    from . import secretstore

    descriptors = tuple(map(int, descriptors.split(","))) if descriptors else ()
    relay, peer = socket.socketpair()

    def cancel(*_):
        with suppress(OSError):
            relay.shutdown(socket.SHUT_WR)

    with relay, peer, secretstore.credential_activation(state=Path(state)):
        signal.signal(signal.SIGTERM, cancel)
        set_parent_death_signal = 1
        if ctypes.CDLL(None).prctl(set_parent_death_signal, signal.SIGTERM, 0, 0, 0) or os.getppid() != int(parent):
            return 125
        launcher = os.fork()
        if launcher == 0:
            try:
                relay.close()
                os.setsid()
                if os.fork() == 0:
                    monitor_linux_worker(peer, descriptors, command, env)
            except OSError as error:
                with suppress(OSError):
                    os.write(2, f"Linux worker supervision failed: {error}\n".encode())
            finally:
                os._exit(0)
        peer.close()
        os.waitpid(launcher, 0)
        for fd in (0, 1, 2, *descriptors):
            os.close(fd)
        with relay.makefile("rb") as receipt:
            return int(receipt.read() or b"125")


def secret_path(p: Path | str) -> bool:
    p = Path(p)
    if SECRET_DIRS & set(p.parts[:-1]):
        return True
    name = p.name
    return any(fnmatch(name, n) for n in SECRET_NAMES) and not any(fnmatch(name, n) for n in SAFE_NAMES)


def absolute(path: Path) -> Path:
    try:
        return Path(path) if Path(path).is_absolute() else Path.cwd() / path
    except OSError as e:
        raise ConfinementUnavailable(f"{path} cannot be located: {e.strerror}") from e


def canonical(path: Path) -> Path:
    try:
        current, missing = absolute(path).resolve(), []
    except (OSError, RuntimeError) as e:
        raise ConfinementUnavailable(f"{path} cannot be resolved") from e
    while True:
        try:
            fd = os.open(current, getattr(os, "O_SEARCH", os.O_RDONLY) | os.O_DIRECTORY)
        except (FileNotFoundError, NotADirectoryError):
            if current.parent == current:
                raise ConfinementUnavailable(f"{path} cannot be located") from None
            missing.append(current.name)
            current = current.parent
            continue
        except OSError as e:
            raise ConfinementUnavailable(f"{path} cannot be checked: {e.strerror}") from e
        try:
            getpath = getattr(fcntl, "F_GETPATH", None)
            name = fcntl.fcntl(fd, getpath, bytes(1024)).split(b"\0", 1)[0] if getpath else os.fsencode(current)
        except OSError as e:
            raise ConfinementUnavailable(f"{path} cannot be checked: {e.strerror}") from e
        finally:
            os.close(fd)
        return Path(os.fsdecode(name), *reversed(missing))


def can_confine(remote: bool) -> bool:
    return not remote and sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK)


def sandboxed() -> bool:
    lib = ctypes.CDLL(None)
    lib.sandbox_check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    return lib.sandbox_check(os.getpid(), None, 0) != 0


def secret_store_roots() -> tuple[Path, ...]:
    from . import config

    try:
        directory = config.secret_settings().store_dir
    except (OSError, ValueError, TypeError, KeyError):
        return ()
    return (directory,) if directory else ()


def authority_roots() -> tuple[Path, ...]:
    migration = () if AUTHORITY == paths.NOWHERE else (AUTHORITY.with_name(AUTHORITY_MIGRATION_RECEIPT),)
    return (AUTHORITY, LEGACY_AUTHORITY, *migration, paths.ZTE_AUTHORITY, ACCOUNT_CONFIG_DIR / "zte-authority",
            paths.ZTE_STOP, paths.CLIENT_DIR.with_name("arete") / "client", paths.DEFAULT_TOKEN_CLIENT_DIR,
            paths.FINGERPRINT_KEY_FILE, *secret_store_roots(),
            *(root / "current.json" for root in (paths.ZTE_REHEARSAL_INSTALL, paths.ZTE_LIVE_INSTALL)))


def already_protects_authority() -> bool:
    lib = ctypes.CDLL(None)
    lib.sandbox_check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)

    def denied(path: Path, *operations: bytes) -> bool:
        return all(lib.sandbox_check(os.getpid(), op, 1, ctypes.c_char_p(os.fsencode(path))) == 1
                   for op in operations)

    def absent(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    for path in authority_roots():
        fingerprint = path == paths.FINGERPRINT_KEY_FILE
        if denied(real_path(path) if fingerprint else path, b"file-read-data", b"file-write-data", b"file-write-create"):
            continue
        if not fingerprint and absent(path) and (path != paths.ZTE_STOP or all(absent(root) for root in (
                paths.ZTE_AUTHORITY, ACCOUNT_CONFIG_DIR / "zte-authority"))):
            continue
        return False
    return all(denied(root, b"file-write-data", b"file-write-create") or absent(root)
               for root in (paths.ZTE_REHEARSAL_INSTALL, paths.ZTE_LIVE_INSTALL))


def protected_command(command, cwd=None, keeps=(), *, _deploy=False, _restore_paths=()):
    if not can_confine(False) or (sandboxed() and already_protects_authority()):
        return command
    words = ["/bin/sh", "-c", command] if isinstance(command, str) else list(command)
    return write_confinement(Path(cwd or Path.cwd()), [], restrict_writes=False,
                             keeps=tuple(keeps), _deploy=_deploy, _restore_paths=_restore_paths) + words


def linux_git_metadata(source: Path) -> Path | None:
    source = Path(os.path.abspath(source))
    marker = source / ".git"
    if source.resolve() != source or marker.is_symlink():
        return None
    if marker.is_dir():
        return marker
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--is-bare-repository"],
            cwd=source, env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    fields = os.fsdecode(probe.stdout).removesuffix("\n").split("\n")
    if probe.returncode or len(fields) != 3:
        return None
    git_dir, common = (Path(p) for p in fields[:2])
    if not common.is_absolute() or common.resolve() != common:
        return None
    if not marker.exists() and git_dir == common == source and fields[2] == "true":
        return common
    if marker.is_file() and git_dir.parent == common / "worktrees" and git_dir.resolve() == git_dir:
        from . import workspace

        backlink = git_dir / "gitdir"
        if (backlink.is_file() and not backlink.is_symlink() and backlink.stat().st_size <= 4096
                and workspace.registered_root_checkout(source)):
            return common
    return None


def linux_repository_roots(checkouts: tuple[Path, ...]) -> set[Path]:
    repositories = set()
    for checkout in checkouts:
        for parent in (checkout, *checkout.parents):
            if parent in (LINUX_SHM, paths.WORKTREES_DIR.resolve(), parent.parent):
                break
            if (parent / ".git").exists():
                repositories.add(parent)
                break
    return repositories


def linux_checkout_metadata(checkouts: tuple[Path, ...], repositories: set[Path], session_checkout) -> set[Path]:
    from . import config
    from .store import db, workspaces

    base = paths.WORKTREES_DIR.resolve()
    requested: dict[str, list[Path]] = {}
    for root in checkouts:
        if base in root.parents:
            requested.setdefault(root.relative_to(base).parts[0], []).append(root)
    selected = set()
    if session_checkout:
        worktree, source = session_checkout
        expected = base / worktree.name
        if worktree.parent.resolve() == base and expected in repositories and checkouts[0].is_relative_to(expected):
            selected.add((expected, source))
    if requested:
        with suppress(db.NoDatabase), db.reader() as conn:
            if db.migration_applied(db.WORKSPACE_SNAPSHOT_SCHEMA, conn):
                for name, roots in requested.items():
                    if row := workspaces.open_for(name):
                        for checkout in workspaces.owner_of(row).checkouts:
                            worktree = checkout.wt.resolve()
                            if worktree.is_relative_to(base / name) and any(
                                    worktree.is_relative_to(root) or root.is_relative_to(worktree) for root in roots):
                                selected.add((worktree, checkout.src))
    # A configured linked checkout can also be selected directly.
    linked = {repo for repo in repositories if (repo / ".git").is_file()}
    if linked:
        configured = {Path(os.path.abspath(repo.path / part)) for repo in config.load().repos
                      for part in ["", *(p.dir for p in repo.parts)]}
        selected.update((repo, repo) for repo in linked & configured)
    metadata = set()
    for worktree, source in selected:
        common = linux_git_metadata(worktree)
        if common is not None and common == linux_git_metadata(source):
            metadata.add(common)
    return metadata


def linux_claude_mounts(checkouts: tuple[Path, ...], profile: Path | None, *, run_id="", temporary=None,
                       store=None, managed_profile=False, session_checkout=None):
    from . import guard

    state = paths.STATE_DIR.resolve()
    config_root = paths.CONFIG_DIR.resolve()
    worktrees = paths.WORKTREES_DIR.resolve()
    run_dirs = guard.run_folders(run_id)
    protected = (state, config_root, store) if store else (state, config_root)
    repositories = linux_repository_roots(checkouts)
    writable = set(checkouts) | linux_checkout_metadata(checkouts, repositories, session_checkout)
    writable.update(p.resolve() for p in run_dirs)
    if profile is not None:
        writable.add(profile)
    if temporary is not None:
        writable.add(temporary)
    resumed_profile = (managed_profile and profile and profile.name == "claude-openrouter"
                       and profile.parent.parent == paths.RUNS_DIR.resolve())
    for root in writable:
        if root in (LINUX_SHM, LINUX_SHM.parent) or any(p.is_relative_to(root) for p in protected):
            raise ConfinementUnavailable("Claude writable directories must be separate from its protected state")
        if root.is_relative_to(config_root) or (store and root.is_relative_to(store)):
            raise ConfinementUnavailable("Claude cannot write to its configuration or secret store")
        if root.is_relative_to(state) and not (
                (worktrees != state and worktrees in root.parents) or (root == profile and resumed_profile)
                or any(root.is_relative_to(p.absolute()) for p in run_dirs)):
            raise ConfinementUnavailable("Claude cannot write to instance coordination state")
    for path in (*run_dirs, *((profile,) if profile is not None else ())):
        with paths.directory_fd(path.absolute(), create=True):
            pass
    mounts = ["--ro-bind", str(state), str(state)] if state.is_dir() else []
    for repo in sorted(repositories - writable):
        if LINUX_SHM in repo.parents and not any(repo.is_relative_to(p) or p.is_relative_to(repo) for p in protected):
            mounts += ["--ro-bind", str(repo), str(repo)]
    if temporary is not None:
        sockets = temporary.parent
        if (sockets / "judge").is_socket() and any(sockets.is_relative_to(root) for root in writable):
            raise ConfinementUnavailable("Claude's judge socket must be outside writable directories")
        if LINUX_SHM in temporary.parents:
            for name in ("hook", "judge"):
                address = sockets / name
                if address.is_socket():
                    mounts += ["--ro-bind", str(address), str(address)]
    for root in sorted(writable):
        if writable.isdisjoint(root.parents):
            mounts += ["--bind", str(root), str(root)]
    return mounts


def claude_confinement(command: list[str], cwd: Path | str, opened: tuple[Path, ...] = (), *,
                       profile: Path | None = None, mount_lease: ExitStack | None = None,
                       activation_lease: WorkerLease | None = None, run_id: str = "",
                       temporary: Path | None = None, managed_profile: bool = False,
                       session_checkout: tuple[Path, Path] | None = None) -> list[str]:
    if sys.platform == "darwin" and not can_confine(False):
        raise ConfinementUnavailable("Claude on macOS requires sandbox-exec for secret confinement")
    if sys.platform != "linux":
        return command
    from . import secretstore

    launcher = shutil.which("bwrap", path=os.defpath)
    if not launcher:
        raise ConfinementUnavailable("Claude on Linux requires bubblewrap for secret confinement")
    try:
        key = secretstore.local_key_path()
        info = linux_key_info(key)
        had_local = info is not None
        if activation_lease is not None:
            activation_lease.acquire()
            info = linux_key_info(key)
        if info is not None and mount_lease is not None:
            had_local = True
            info = mount_lease.enter_context(secretstore.local_key_mount_lease(key))
        store, activated = linux_store_policy(has_local=info is not None, had_local=had_local)
        if activated and activation_lease is not None:
            activation_lease.close()
        work = Path(cwd).resolve()
        checkouts = (work, *(Path(root).resolve() for root in opened))
        profile = (work / profile).resolve() if profile is not None else None
        targets = checkouts + ((profile,) if profile is not None else ())
        if store is not None and any(root.is_relative_to(store) for root in targets):
            raise ConfinementUnavailable("Claude cannot start inside the secret store")
        masks = linux_fingerprint_mounts(key, info, targets) if info is not None else []
        restored = linux_claude_mounts(checkouts, profile, run_id=run_id, temporary=temporary,
                                      store=store, managed_profile=managed_profile, session_checkout=session_checkout)
    except ConfinementUnavailable:
        raise
    except secretstore.FingerprintKeyMissing as error:
        raise ConfinementUnavailable(str(error)) from error
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        raise ConfinementUnavailable("Claude could not determine Linux secret confinement paths") from error
    if store is not None:
        try:
            with secretstore.file_parent(store / "mount", store, create=True, private=True):
                pass
        except OSError as error:
            raise ConfinementUnavailable("Claude could not prepare the Linux secret store mount") from error
        masks += ["--tmpfs", str(store), "--remount-ro", str(store)]
    return [launcher, "--unshare-user", "--disable-userns", "--unshare-pid", "--unshare-ipc", "--cap-drop", "ALL",
            "--die-with-parent", "--new-session", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
            *restored, *masks, "--chdir", str(work), "--", *command]


def linux_key_info(key: Path) -> os.stat_result | None:
    with suppress(FileNotFoundError):
        return key.lstat()
    return None


def linux_fingerprint_mounts(key: Path, info: os.stat_result, opened: tuple[Path, ...]) -> list[str]:
    from . import secretstore

    if any(Path(root).resolve().is_relative_to(key) for root in opened):
        raise ConfinementUnavailable("Claude cannot open the instance fingerprint key as a workspace")
    if not secretstore.private_file(info) or info.st_size != 32:
        raise ConfinementUnavailable("Claude requires a private regular fingerprint key file for confinement")
    return ["--ro-bind", os.devnull, str(key)]


def linux_store_policy(*, has_local: bool, had_local: bool) -> tuple[Path | None, bool]:
    from . import config, secretstore
    from .store import credentials, db

    meta = {}
    required = had_local
    with suppress(db.NoDatabase), closing(db.read_connection()) as c, db.read_snapshot(c):
        meta = db.metadata(c)
        required = required or secretstore.KEY_LOCAL in meta or (
            not has_local and bool(meta) and db.migration_applied(db.SECRET_REGISTRY_SCHEMA, c)
            and credentials.has_fingerprints(c))
    settings = config.secret_settings()
    activated = secretstore.KEY_REF in meta
    if activated and settings.store == "pve_store":
        secretstore.referenced_key(meta, settings)
    elif activated or (required and not has_local):
        raise secretstore.FingerprintKeyMissing("fingerprint_key_missing")
    return settings.store_dir, activated


def write_confinement(cwd: Path, add_dirs: list[Path], auth_path: Path | None = None,
                      *, hidden: tuple[Path, ...] = (), restrict_writes: bool = True,
                      agent_roots: bool = True, peers: Path | None = None, secrets_denied: bool = False,
                      keeps: tuple[str, ...] = (), unix_confined: bool = False, temp_writes: bool = True,
                      state: tuple[Path, tuple[Path, ...]] | None = None,
                      mach: tuple[tuple[str, ...], str] | None = None, tcp_port: int | None = None,
                      denied_ports: tuple[int, ...] = (), listen_port: int | None = None,
                      private_temporary: bool = False, read_roots: tuple[Path, ...] | None = None,
                      _deploy: bool = False, _restore_paths: tuple[Path, ...] = ()) -> list[str]:
    hidden = tuple(dict.fromkeys((*hidden, *authority_roots())))
    roots = [cwd, *add_dirs, *([AGENT_DIR, LOGIN_DIR, KEY_DIR, SESSIONS_DIR] if agent_roots else [])]
    allow = " ".join(f'(subpath (param "W{i}"))' for i in range(len(roots)))
    if temp_writes and not private_temporary:
        allow += " " + " ".join(f'(subpath "{p}")' for p in WRITABLE_ALWAYS)
    else:
        allow += " " + " ".join(f'(literal "{p}")' for p in WRITABLE_DEVICES)
    profile = '(version 1)(allow default)'
    if restrict_writes:
        profile += f'(deny file-write* (subpath "/"))(allow file-write* {allow})'
    if auth_path is not None:
        profile += '(allow file-write* (literal (param "AUTH")) (literal (param "LOCK")))'
    params = [arg for i, root in enumerate(roots) for arg in ("-D", f"W{i}={real_path(root)}")]
    if read_roots is not None:
        readable = tuple(dict.fromkeys(path for root in (*roots, *read_roots)
                                       for path in (absolute(root), real_path(root))))
        params += [arg for i, root in enumerate(readable) for arg in ("-D", f"RD{i}={root}")]
        reads = " ".join(f'(subpath (param "RD{i}"))' for i in range(len(readable)))
        # dyld opens the root directory while locating its shared cache.
        profile += f'(deny file-read* (subpath "/"))(allow file-read* (literal "/") {reads})'
        for i, path in enumerate(sorted({p for root in readable for p in root.parents})):
            params += ["-D", f"RA{i}={path}"]
            profile += f'(allow file-read* (literal (param "RA{i}")))'
    if auth_path is not None:
        params += ["-D", f"AUTH={real_path(auth_path)}", "-D", f"LOCK={real_path(auth_path)}.lock"]
    for i, path in enumerate(hidden):
        params += ["-D", f"H{i}={real_path(path)}"]
        profile += f'(deny file-read* file-write* file-link (subpath (param "H{i}")))'
    if peers:
        params += ["-D", f"PEERS={real_path(peers)}", "-D", f"WORK={real_path(cwd)}"]
        profile += ('(deny file-read* file-write* (require-all (subpath (param "PEERS")) '
                    '(require-not (literal (param "PEERS"))) (require-not (subpath (param "WORK")))))'
                    '(deny file-read-data file-write* (literal (param "PEERS")))')
    if state is not None:
        profile += state_denials(params, *state)
    if private_temporary:
        for i, folder in enumerate(TEMPORARY_ROOTS):
            profile += state_denials(params, Path(folder), (*roots, *(read_roots or ())), prefix=f"TMP{i}_")
    if secrets_denied:
        def pattern(names, descendants=False):
            return "(^|/)(" + "|".join(re.escape(n).replace(r"\*", "[^/]*") for n in names) + (
                ")($|/)" if descendants else ")$")

        params += ["-D", "NAMES=" + pattern(SECRET_NAMES), "-D", "SAFE=" + pattern(SAFE_NAMES),
                   "-D", "DIRS=" + pattern(SECRET_DIRS, True)]
        profile += ('(deny file-read* file-write* (require-all (regex (param "NAMES")) '
                    '(require-not (regex (param "SAFE")))))(deny file-read* file-write* (regex (param "DIRS")))')
    engine = real_path(paths.PACKAGE_DIR)
    if not _deploy:
        params += ["-D", f"ENGINE={engine}"]
        engine_filter = '(subpath (param "ENGINE"))'
        if _restore_paths:
            exceptions = ""
            for i, path in enumerate(_restore_paths):
                params += ["-D", f"RESTORE{i}={path}"]
                exceptions += f'(require-not (literal (param "RESTORE{i}")))'
            engine_filter = f"(require-all {engine_filter} {exceptions})"
        profile += f"(deny file-write* {engine_filter})"
    params += ["-D", f"RUNTIME={real_path(sys.prefix)}"]
    profile += '(deny file-write* (subpath (param "RUNTIME")))'
    for i, root in enumerate((paths.ZTE_REHEARSAL_INSTALL, paths.ZTE_LIVE_INSTALL)):
        params += ["-D", f"INSTALL{i}={real_path(root)}"]
        profile += f'(deny file-write* file-link (subpath (param "INSTALL{i}")))'
    ancestors = {*engine.parents, *(p for root in (*hidden, Path(sys.prefix)) for p in real_path(root).parents)}
    for i, path in enumerate(sorted(ancestors)):
        params += ["-D", f"A{i}={path}"]
        profile += f'(deny file-write* (literal (param "A{i}")))'
    if "launch" not in keeps:
        profile += '(deny lsopen)(deny appleevent-send)(deny job-creation)'
    if mach is not None:
        names, pattern = mach
        params += ["-D", f"MACHRE={pattern}"]
        profile += ("(deny mach-lookup)(allow mach-lookup " + "".join(f'(global-name "{n}")' for n in names)
                    + '(global-name-regex (param "MACHRE")))')
    if tcp_port is not None:
        params += ["-D", f"UNIX={real_path(cwd)}"]
        profile += (f'(deny network-outbound)(allow network-outbound (remote tcp "localhost:{int(tcp_port)}") '
                    '(subpath (param "UNIX")))')
    if listen_port is not None:
        profile += ('(deny network-bind network-inbound (local ip "*:*"))'
                    f'(allow network-bind network-inbound (local ip "localhost:{int(listen_port)}"))')
    if unix_confined:
        params += ["-D", f"UNIX={real_path(cwd)}"]
        profile += ('(deny network-outbound (subpath "/"))(allow network-outbound (subpath (param "UNIX")) '
                    '(subpath "/private/tmp") (subpath "/private/var/folders"))')
    folders, sockets = command_sockets(keeps)
    for i, folder in enumerate(folders):
        params += ["-D", f"SF{i}={folder}"]
        profile += f'(deny network-outbound (subpath (param "SF{i}")))'
    for i, sock in enumerate(sockets):
        params += ["-D", f"SL{i}={sock}"]
        profile += f'(deny network-outbound (literal (param "SL{i}")))'
    for port in sorted({*paths.dashboard_ports(), *denied_ports}):
        profile += f'(deny network-outbound (remote tcp "*:{int(port)}"))'
    return ["/usr/bin/sandbox-exec", *params, "-p", profile]


def real_path(path) -> Path:
    try:
        return canonical(Path(path))
    except ConfinementUnavailable:
        return Path(path).resolve()


def state_denials(params: list[str], folder: Path, opened: tuple[Path, ...], *, prefix: str = "") -> str:
    folder = canonical(folder)
    kept = [canonical(Path(p)) for p in opened]
    between = sorted({folder, *(q for p in kept for q in p.parents if q.is_relative_to(folder))})
    params += ["-D", f"{prefix}STATE={folder}"]
    exempt = ""
    for i, path in enumerate(kept):
        params += ["-D", f"{prefix}SO{i}={path}"]
        exempt += f'(require-not (subpath (param "{prefix}SO{i}")))'
    listing = ""
    for i, path in enumerate(between):
        params += ["-D", f"{prefix}SB{i}={path}"]
        exempt += f'(require-not (literal (param "{prefix}SB{i}")))'
        listing += f'(literal (param "{prefix}SB{i}"))'
    return (f'(deny file-read* file-write* file-link (require-all (subpath (param "{prefix}STATE")) {exempt}))'
            f"(deny file-read-data file-write* {listing})")


def command_sockets(keeps: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    uid = os.getuid()
    folders = [f"/private/tmp/tmux-{uid}"]
    if tmpdir := os.environ.get("TMUX_TMPDIR"):
        folders.append(str(Path(tmpdir).resolve() / f"tmux-{uid}"))
    folders += [str(paths.HOME / p) for p in (".docker/run", ".colima", ".orbstack/run", ".rd",
                                              "Library/Containers/com.docker.docker/Data")]
    sockets = []
    if tmux := os.environ.get("TMUX", "").split(",")[0]:
        sockets.append(tmux)
    sockets += ["/private/var/run/docker.sock", str(Path("/var/run/docker.sock").resolve())]
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        sockets.append(host.removeprefix("unix://"))
    agent = os.environ.get("SSH_AUTH_SOCK", "")
    if agent and "ssh-agent" not in keeps and os.path.isabs(agent):
        sockets += [agent, str(Path(agent).resolve())]
    return list(dict.fromkeys(folders)), list(dict.fromkeys(sockets))
