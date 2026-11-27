import errno
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

SCRATCH_PREFIX = ".arete-scratch-"
SCRATCH_SUFFIX = ".noindex"
SCRATCH_ENV = "ARETE_VERIFY_SCRATCH"
SCRATCH_TOKEN = re.compile(r"[0-9a-f]{16}(?:" + re.escape(SCRATCH_SUFFIX) + ")?")
SEARCH_ONLY = getattr(os, "O_SEARCH", 0)
SERVE_BEAT_NAME = "serve.json"
ISOLATED_REFUSAL = (f"arete: refusing to start: with {SCRATCH_ENV}, or ARETE_CONFIG_DIR or ARETE_STATE_DIR in a scratch "
                    f"folder, the config and state folders must both sit, without links, in the one scratch folder "
                    f"selected for isolation (unset ones go to its config and state folders)")


def inside_scratch(d: Path) -> bool:
    return any(part.startswith(SCRATCH_PREFIX) for part in (*d.absolute().parts, *Path(os.path.realpath(d)).parts))


def valid_scratch(root: str) -> os.stat_result | None:
    try:
        name = os.path.basename(root)
        if (not os.path.isabs(root) or os.path.abspath(root) != root or not name.startswith(SCRATCH_PREFIX)
                or not SCRATCH_TOKEN.fullmatch(name.removeprefix(SCRATCH_PREFIX))):
            return None
        st = os.lstat(root)
    except (OSError, TypeError, ValueError):
        return None
    if (not stat.S_ISDIR(st.st_mode) or os.path.realpath(root) != root or st.st_uid != os.getuid()
            or st.st_mode & 0o077):
        return None
    return st


def scratch_root(p: Path) -> Path | None:
    full = Path(os.path.abspath(p))
    for d in (full, *full.parents):
        if d.name.startswith(SCRATCH_PREFIX):
            return d if valid_scratch(str(d)) else None
    return None


def confined(p: Path) -> Path | None:
    root = scratch_root(p)
    if root is None:
        return None
    here = root
    for part in Path(os.path.abspath(p)).relative_to(root).parts:
        here = here / part
        try:
            if stat.S_ISLNK(os.lstat(here).st_mode):
                return None
        except FileNotFoundError:
            break
        except OSError:
            return None
    real = Path(os.path.realpath(p))
    return real if real == root or root in real.parents else None


def open_directory(part: str, parent: int, above_target: bool) -> int:
    flags = os.O_DIRECTORY | os.O_NOFOLLOW
    if above_target and SEARCH_ONLY:
        try:
            return os.open(part, SEARCH_ONLY | flags, dir_fd=parent)
        except PermissionError:
            pass
    return os.open(part, os.O_RDONLY | flags, dir_fd=parent)


@contextmanager
def directory_fd(p: Path, parent: int | None = None, create: bool = False, *, durable: bool = False):
    parts = p.parts
    if ".." in parts or (parent is not None and p.is_absolute()):
        raise PermissionError("directory traversal refused")
    fd = os.dup(parent) if parent is not None else os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        walked = parts if parent is not None else p.absolute().parts[1:]
        for index, part in enumerate(walked):
            created = False
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
            # A retry can encounter a directory created before its parent was synced.
            if durable and (created or os.access(".", os.W_OK, dir_fd=fd)):
                readable = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    os.fsync(readable)
                finally:
                    os.close(readable)
            next_fd = open_directory(part, fd, index < len(walked) - 1)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def read_confined(p: Path, limit: int | None = None, *, isolated: bool) -> bytes:
    if not isolated:
        with open(p, "rb") as f:
            return f.read(-1 if limit is None else limit)
    try:
        data = read_scratch_file(p, limit)
    except OSError as error:
        raise FileNotFoundError(errno.ENOENT, "not a file inside a scratch folder", str(p)) from error
    if data is None:
        raise FileNotFoundError(errno.ENOENT, "not a file inside a scratch folder", str(p))
    return data


def read_scratch_file(p: Path, limit: int | None = None) -> bytes | None:
    root = scratch_root(p)
    if root is None or p.absolute() == root or ".." in p.parts:
        raise PermissionError("not a file inside a scratch folder")
    expected = os.lstat(root)
    with directory_fd(root) as root_fd, ExitStack() as stack:
        current = os.fstat(root_fd)
        if ((expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino)
                or current.st_uid != os.getuid() or current.st_mode & 0o077):
            raise PermissionError("scratch root replaced or unsafe")
        try:
            parent = stack.enter_context(directory_fd(p.absolute().parent.relative_to(root), root_fd))
            source = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(source, "rb") as f:
            st = os.fstat(source)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
                raise PermissionError("not a plain file of this user")
            return f.read(-1 if limit is None else limit)


def isolated_dirs() -> tuple[Path, Path]:
    given = {name: os.environ.get(f"ARETE_{name.upper()}_DIR", "") for name in ("config", "state")}
    roots = {str(r) if (r := scratch_root(Path(v))) else "" for v in given.values() if v}
    if asked := os.environ.get(SCRATCH_ENV, ""):
        roots.add(asked)
    root = roots.pop() if len(roots) == 1 else ""
    config, state = (Path(given[name] or os.path.join(root, name)) for name in ("config", "state"))
    if not root or valid_scratch(root) is None or confined(config) is None or confined(state) is None:
        raise SystemExit(ISOLATED_REFUSAL)
    return config, state


def local_dirs(home: Path, *, guard_hook: bool = False) -> tuple[Path, Path, bool]:
    config = Path(os.environ.get("ARETE_CONFIG_DIR", home / ".config" / "arete"))
    state = Path(os.environ.get("ARETE_STATE_DIR", home / ".local" / "share" / "arete"))
    isolated = not guard_hook and (bool(os.environ.get(SCRATCH_ENV)) or inside_scratch(config) or inside_scratch(state))
    if isolated:
        config, state = isolated_dirs()
    return config, state, isolated


def log_dir(home: Path, state: Path, isolated: bool) -> Path:
    default = home / "Library" / "Logs" / "arete" if sys.platform == "darwin" and not isolated else state / "logs"
    return Path(os.environ.get("ARETE_LOG_DIR", default))
