import os
import sys
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("ARETE_CONFIG_DIR", HOME / ".config" / "arete"))
STATE_DIR = Path(os.environ.get("ARETE_STATE_DIR", HOME / ".local" / "share" / "arete"))
ON_MAC = sys.platform == "darwin"
LOG_DIR = Path(os.environ.get("ARETE_LOG_DIR", HOME / "Library" / "Logs" / "arete" if ON_MAC else STATE_DIR / "logs"))

CONFIG_FILE = CONFIG_DIR / "config.toml"
PI_KEYS_FILE = CONFIG_DIR / "pi.env"
EVENTS_FILE = STATE_DIR / "events.jsonl"
DB_FILE = STATE_DIR / "arete.db"
RUNS_DIR = STATE_DIR / "runs"
WORKTREES_DIR = Path(os.environ.get("ARETE_WORKTREES_DIR", HOME / "Library" / "Developer" / "arete" / "worktrees"
                                    if ON_MAC else STATE_DIR / "worktrees"))
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"
SERVER_FILE = STATE_DIR / "server.json"
TLS_CERT_FILE = STATE_DIR / "dashboard.crt"
TLS_KEY_FILE = STATE_DIR / "dashboard.key"
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
