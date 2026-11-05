import os
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("ARETE_CONFIG_DIR", HOME / ".config" / "arete"))
STATE_DIR = Path(os.environ.get("ARETE_STATE_DIR", HOME / ".local" / "share" / "arete"))
LOG_DIR = Path(os.environ.get("ARETE_LOG_DIR", HOME / "Library" / "Logs" / "arete"))

CONFIG_FILE = CONFIG_DIR / "config.toml"
EVENTS_FILE = STATE_DIR / "events.jsonl"
DB_FILE = STATE_DIR / "arete.db"
RUNS_DIR = STATE_DIR / "runs"
WORKTREES_DIR = STATE_DIR / "worktrees"
HEARTBEAT_FILE = STATE_DIR / "heartbeat.json"
LOCKS_DIR = STATE_DIR / "locks"

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def short_path(p: str | Path) -> str:
    s, home = str(p), str(HOME)
    return "~" + s[len(home):] if s.startswith(home) else s


def ensure_dirs() -> None:
    for d in (STATE_DIR, RUNS_DIR, WORKTREES_DIR, LOCKS_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
