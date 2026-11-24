from __future__ import annotations

import ctypes
import fcntl
import os
import pwd
import re
import shutil
import sys
from fnmatch import fnmatch
from pathlib import Path

from . import paths

SESSIONS_DIR = paths.STATE_DIR / "pi-sessions"
AGENT_DIR = paths.STATE_DIR / "pi-agent"
LOGIN_DIR = paths.STATE_DIR / "pi-login"
KEY_DIR = paths.STATE_DIR / "pi-key"
TEMPORARY_ROOTS = ("/private/tmp", "/private/var/folders")
WRITABLE_ALWAYS = (*TEMPORARY_ROOTS, "/dev")
WRITABLE_DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/dtracehelper", "/dev/tty")
NO_CONFINEMENT_REASON = "pi keeps file writes inside the work folder only with the macOS sandbox on this Mac"
SECRET_NAMES = (".env", ".env.*", "*.env", ".envrc", ".netrc", ".pgpass", ".npmrc", ".pypirc", "id_rsa*", "id_dsa*",
                "id_ecdsa*", "id_ed25519*", "*.pem", "*.p12", "*.pfx", "*.key", "credentials", "credentials.json",
                "*.secret", "*.secrets", "secrets.toml", "secrets.json")
SAFE_NAMES = ("*.example", "*.sample", "*.template", "*.dist")
SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".password-store"})
try:
    AUTHORITY = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config/arete/verification-authority"
except KeyError:
    AUTHORITY = paths.NOWHERE


class ConfinementUnavailable(PermissionError):
    pass


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
    return lib.sandbox_check(os.getpid(), None, 0) == 1


def secret_store_roots() -> tuple[Path, ...]:
    from . import config

    try:
        directory = config.secret_settings().store_dir
    except (OSError, ValueError, TypeError, KeyError):
        return ()
    return (directory,) if directory else ()


def authority_roots() -> tuple[Path, ...]:
    return (AUTHORITY, paths.ZTE_AUTHORITY, AUTHORITY.parent / "zte-authority", paths.ZTE_STOP,
            paths.CLIENT_DIR.with_name("arete") / "client", paths.DEFAULT_TOKEN_CLIENT_DIR,
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
                paths.ZTE_AUTHORITY, AUTHORITY.parent / "zte-authority"))):
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


def claude_confinement(command: list[str], cwd: Path | str, opened: tuple[Path, ...] = ()) -> list[str]:
    if sys.platform == "darwin" and not can_confine(False):
        raise ConfinementUnavailable("Claude on macOS requires sandbox-exec for secret confinement")
    if sys.platform != "linux":
        return command
    from . import config, secretstore

    try:
        settings = config.secret_settings()
        store = settings.store_dir
        key = secretstore.local_key_path()
        protect_fallback = settings.store is None or key.exists()
        work = Path(cwd).resolve()
        roots = tuple(dict.fromkeys((work, *(Path(p).resolve() for p in opened))))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        raise ConfinementUnavailable("Claude could not determine Linux secret confinement paths") from error
    if not protect_fallback and store is None:
        return command
    launcher = shutil.which("bwrap", path=os.defpath)
    if not launcher:
        raise ConfinementUnavailable("Claude on Linux requires bubblewrap for secret confinement")
    if store is not None and work.is_relative_to(store):
        raise ConfinementUnavailable("Claude cannot start inside the secret store")
    masks = linux_fingerprint_mounts(key, roots) if protect_fallback else []
    if store is not None:
        try:
            with secretstore.file_parent(store / "mount", store, create=True, private=True):
                pass
        except OSError as error:
            raise ConfinementUnavailable("Claude could not prepare the Linux secret store mount") from error
        masks += ["--tmpfs", str(store), "--remount-ro", str(store)]
    return [launcher, "--unshare-user", "--disable-userns", "--unshare-pid", "--unshare-ipc", "--cap-drop", "ALL",
            "--die-with-parent", "--new-session", "--bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
            *masks, "--chdir", str(work), "--", *command]


def linux_fingerprint_mounts(key: Path, opened: tuple[Path, ...]) -> list[str]:
    state = key.parent
    if state == Path("/") or any(p == state or p.is_relative_to(key) for p in opened):
        raise ConfinementUnavailable("Claude cannot open the instance state directory as a workspace")
    try:
        with paths.directory_fd(state, create=True):
            pass
    except OSError as error:
        raise ConfinementUnavailable("Claude could not prepare the Linux fingerprint key mask") from error
    mounts = ["--tmpfs", str(state)]
    inside = tuple(p for p in opened if p.is_relative_to(state))
    for root in inside:
        if not any(parent != root and root.is_relative_to(parent) for parent in inside):
            mounts += ["--bind", str(root), str(root)]
    return [*mounts, "--remount-ro", str(state)]


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
