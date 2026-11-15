import os
import sys
import tempfile
from pathlib import Path

base = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(base))
with tempfile.TemporaryDirectory(prefix="arete-schema-v0-") as directory:
    root = Path(directory)
    for name in ("home", "config", "state", "logs", "worktrees", "tmp"):
        (root / name).mkdir()
    os.environ.update(HOME=str(root / "home"), TMPDIR=str(root / "tmp"),
                      ARETE_CONFIG_DIR=str(root / "config"), ARETE_STATE_DIR=str(root / "state"),
                      ARETE_LOG_DIR=str(root / "logs"), ARETE_WORKTREES_DIR=str(root / "worktrees"))
    os.environ.pop("ARETE_VERIFY_SCRATCH", None)
    from arete import state

    assert Path(state.__file__).resolve() == base / "arete/state.py", state.__file__
    assert hasattr(state, "SCHEMA") and not hasattr(state, "migrate"), "requires the pre-ledger base checkout"
    with state.db() as conn:
        statements = [r[0].rstrip(";") + ";" for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(statements) + "\n")
