import json
import os
import re
import sys
import tomllib
from functools import lru_cache
from pathlib import Path

from . import local_paths
from .local_paths import SCRATCH_ENV as SCRATCH_ENV
from .local_paths import SCRATCH_PREFIX as SCRATCH_PREFIX
from .local_paths import SCRATCH_SUFFIX as SCRATCH_SUFFIX
from .local_paths import confined as confined
from .local_paths import directory_fd as directory_fd
from .local_paths import inside_scratch as inside_scratch
from .local_paths import read_scratch_file as read_scratch_file
from .local_paths import scratch_root as scratch_root

HOME = Path.home()
ON_MAC = sys.platform == "darwin"
NOWHERE = Path("/var/empty/arete-isolated")
CREDENTIAL_NAME = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL|^access$|^refresh$", re.IGNORECASE)
ISOLATED_LOGINS = "scratch config and state never use this machine's logins, keys or price lookups"


def read_confined(p: Path, limit: int | None = None) -> bytes:
    return local_paths.read_confined(p, limit, isolated=ISOLATED)


def drop_credentials() -> None:
    for name in [n for n in os.environ if CREDENTIAL_NAME.search(n)]:
        del os.environ[name]


CLIENT_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "arete-client"
DEFAULT_TOKEN_CLIENT_DIR = HOME / ".local/state/arete/client"
GUARD_HOOK = sys.argv[1:2] in (["guard"], ["codex-guard"])
CONFIG_DIR, STATE_DIR, ISOLATED = local_paths.local_dirs(HOME, guard_hook=GUARD_HOOK)
if ISOLATED:
    drop_credentials()
SCRATCH_CONFIG = confined(CONFIG_DIR) is not None
SCRATCH_STATE = confined(STATE_DIR) is not None
TOKEN_CLIENT_DIR = (STATE_DIR if SCRATCH_STATE or STATE_DIR.parent.name == local_paths.VERIFY_SCRATCH_ROOT.name
                    else CLIENT_DIR.with_name("arete")) / "client"
LOG_DIR = local_paths.log_dir(HOME, STATE_DIR, ISOLATED)

CONFIG_FILE = CONFIG_DIR / "config.toml"
PI_KEYS_FILE = (CONFIG_DIR if SCRATCH_CONFIG or not ISOLATED else NOWHERE) / "pi.env"
EVENTS_FILE = STATE_DIR / "events.jsonl"
DB_FILE = STATE_DIR / "arete.db"


def backups_dir() -> Path:
    return DB_FILE.parent / "backups"


RUNS_DIR = STATE_DIR / "runs"
WORKTREES_DIR = Path(os.environ.get("ARETE_WORKTREES_DIR", HOME / "Library" / "Developer" / "arete" / "worktrees"
                                    if ON_MAC and not ISOLATED else STATE_DIR / "worktrees"))
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"
SERVE_BEAT_FILE = STATE_DIR / local_paths.SERVE_BEAT_NAME
SERVER_FILE = STATE_DIR / "server.json"
DASHBOARD_PORT = 8796
TLS_CERT_FILE = STATE_DIR / "dashboard.crt"
TLS_KEY_FILE = STATE_DIR / "dashboard.key"
PUSH_KEY_FILE = STATE_DIR / "webpush.key"
FINGERPRINT_KEY_FILE = STATE_DIR / "fingerprint.key"
LOCKS_DIR = STATE_DIR / "locks"
CHECK_LOCKS_DIR = Path("/tmp").resolve() / f"arete-check-slots-{os.getuid()}"
ZTE_AUTHORITY = CONFIG_DIR / "zte-authority"
ZTE_STOP = STATE_DIR / "zte-paused.json"
ZTE_REHEARSAL_INSTALL = STATE_DIR / "zte-managed"
ZTE_LIVE_INSTALL = HOME / "Library/Developer/arete/zte-managed"

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
DEFAULTS_DIR = PACKAGE_DIR / "defaults"
CONFIG_SCHEMAS_DIR = PACKAGE_DIR / "config_schemas"
ADWS_DIR = PACKAGE_DIR.parent / "adws"
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"


PER_CALL_HOOKS = ("hook", "guard", "codex-guard", "background")


def cli_command(*args: str, flags: tuple[str, ...] = ()) -> list[str]:
    pinned = "" if args and args[0] in (*PER_CALL_HOOKS, "status", "serve") else "from arete import codebase; codebase.load(); "
    entrypoint = (f"import sys; sys.path.insert(0, {str(PACKAGE_DIR.parent)!r}); {pinned}"
                  "from arete.cli import main; raise SystemExit(main())")
    return [sys.executable, "-I", *flags, "-c", entrypoint, *args]


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


@lru_cache(maxsize=16)
def parse_toml_bytes(raw: bytes) -> dict:
    return tomllib.loads(raw.decode())


def dashboard_ports() -> frozenset[int]:
    found = {DASHBOARD_PORT}
    try:
        data = parse_toml_bytes(read_confined(CONFIG_FILE))
        found.add(int(data.get("dashboard", {}).get("port", DASHBOARD_PORT)))
    except (OSError, ValueError, TypeError):
        pass
    try:
        record = json.loads(SERVER_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        record = None
    if isinstance(record, dict) and isinstance(record.get("port"), int):
        found.add(record["port"])
    return frozenset(found)


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
    return RUNS_DIR / run_id / "draft"


def ensure_dirs() -> None:
    for d in (STATE_DIR, RUNS_DIR, WORKTREES_DIR, LOCKS_DIR, LOG_DIR):
        if ISOLATED:
            root = scratch_root(STATE_DIR)
            if root is None:
                raise PermissionError("isolated state directory unavailable")
            with directory_fd(root) as parent, directory_fd(d.absolute().relative_to(root), parent, create=True):
                pass
        else:
            d.mkdir(parents=True, exist_ok=True)
