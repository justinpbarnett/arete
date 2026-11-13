import errno
import os
import re
import stat
import sys
from pathlib import Path

HOME = Path.home()
ON_MAC = sys.platform == "darwin"
SCRATCH_PREFIX = ".arete-scratch-"
SCRATCH_ENV = "ARETE_VERIFY_SCRATCH"
SCRATCH_TOKEN = re.compile(r"[0-9a-f]{16}")
NOWHERE = Path("/var/empty/arete-isolated")
CREDENTIAL_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|^access$|^refresh$", re.IGNORECASE)
ISOLATED_LOGINS = "scratch config and state never use this machine's logins, keys or price lookups"
ISOLATED_REFUSAL = (f"arete: refusing to start: with {SCRATCH_ENV}, or ARETE_CONFIG_DIR or ARETE_STATE_DIR in a scratch "
                    f"folder, the config and state folders must both sit, without links, in the one scratch folder "
                    f"Arete made (unset ones go to its config and state folders)")


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


def read_confined(p: Path, limit: int | None = None) -> bytes:
    if not ISOLATED:
        with open(p, "rb") as f:
            return f.read(-1 if limit is None else limit)
    root = scratch_root(p)
    parts = Path(os.path.abspath(p)).parts
    if root is None or len(parts) <= len(root.parts) or ".." in Path(p).parts:
        raise FileNotFoundError(errno.ENOENT, "not a file inside a scratch folder", str(p))
    fds = []
    try:
        fds.append(os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY))
        for part in parts[1:-1]:
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        fds.append(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1]))
        st = os.fstat(fds[-1])
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            raise FileNotFoundError(errno.ENOENT, "not a plain file of this user", str(p))
        with os.fdopen(fds.pop(), "rb") as f:
            return f.read(-1 if limit is None else limit)
    except OSError as e:
        raise FileNotFoundError(errno.ENOENT, "not a file inside a scratch folder", str(p)) from e
    finally:
        for fd in fds:
            os.close(fd)


def plain_or_missing(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return True
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.getuid()


def safe_db(p: Path) -> Path:
    if ISOLATED and not all(confined(f) is not None and plain_or_missing(f)
                            for f in (p, *(Path(f"{p}{end}") for end in ("-wal", "-shm", "-journal")))):
        raise PermissionError(errno.EPERM, "refused a state database that is linked or leaves its scratch folder", str(p))
    return p


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


def drop_credentials() -> None:
    for name in [n for n in os.environ if CREDENTIAL_NAME.search(n)]:
        del os.environ[name]


CONFIG_DIR = Path(os.environ.get("ARETE_CONFIG_DIR", HOME / ".config" / "arete"))
STATE_DIR = Path(os.environ.get("ARETE_STATE_DIR", HOME / ".local" / "share" / "arete"))
GUARD_HOOK = sys.argv[1:2] in (["guard"], ["codex-guard"])
ISOLATED = not GUARD_HOOK and (bool(os.environ.get(SCRATCH_ENV)) or inside_scratch(CONFIG_DIR)
                               or inside_scratch(STATE_DIR))
if ISOLATED:
    CONFIG_DIR, STATE_DIR = isolated_dirs()
    drop_credentials()
SCRATCH_CONFIG = confined(CONFIG_DIR) is not None
SCRATCH_STATE = confined(STATE_DIR) is not None
LOG_DIR = Path(os.environ.get("ARETE_LOG_DIR", HOME / "Library" / "Logs" / "arete" if ON_MAC and not ISOLATED
                              else STATE_DIR / "logs"))

CONFIG_FILE = CONFIG_DIR / "config.toml"
PI_KEYS_FILE = (CONFIG_DIR if SCRATCH_CONFIG or not ISOLATED else NOWHERE) / "pi.env"
EVENTS_FILE = STATE_DIR / "events.jsonl"
DB_FILE = STATE_DIR / "arete.db"
RUNS_DIR = STATE_DIR / "runs"
WORKTREES_DIR = Path(os.environ.get("ARETE_WORKTREES_DIR", HOME / "Library" / "Developer" / "arete" / "worktrees"
                                    if ON_MAC and not ISOLATED else STATE_DIR / "worktrees"))
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"
SERVER_FILE = STATE_DIR / "server.json"
LOOP_MARKS_FILE = STATE_DIR / "loops.json"
LOCKS_DIR = STATE_DIR / "locks"

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
ADWS_DIR = PACKAGE_DIR.parent / "adws"
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def state_and_config() -> dict:
    return {"state": str(STATE_DIR.resolve()), "config": str(CONFIG_FILE.resolve())}


def short_path(p: str | Path) -> str:
    s, home = str(p), str(HOME)
    if s == home:
        return "~"
    below_home = home.rstrip(os.sep) + os.sep
    return "~/" + s[len(below_home):] if s.startswith(below_home) else s


def scratch_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id / "scratch"


def record_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id / "record"


def draft_dir(run_id: str) -> Path:
    return STATE_DIR / "drafts" / run_id


def ensure_dirs() -> None:
    for d in (STATE_DIR, RUNS_DIR, WORKTREES_DIR, LOCKS_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
