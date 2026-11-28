import math
import os
import tomllib
from datetime import timedelta

from arete import paths, util

PHASE = "expand"


def _one_component(run_id: str) -> bool:
    return bool(run_id) and run_id not in (".", "..") and "/" not in run_id and os.sep not in run_id


def _record_days() -> float:
    ret_file = paths.CONFIG_DIR / "retention.toml"
    if ret_file.is_file():
        try:
            data = tomllib.loads(paths.read_confined(ret_file).decode("utf-8"))
            val = (data.get("records") or {}).get("days")
            if type(val) in (int, float) and math.isfinite(val) and val > 0:
                return float(val)
        except (OSError, ValueError, tomllib.TOMLDecodeError, AttributeError):
            pass
    if paths.CONFIG_FILE.is_file():
        try:
            data = tomllib.loads(paths.read_confined(paths.CONFIG_FILE).decode("utf-8"))
            val = (data.get("retention") or {}).get("days")
            if type(val) in (int, float) and math.isfinite(val) and val > 0:
                return float(val)
        except (OSError, ValueError, tomllib.TOMLDecodeError, AttributeError):
            pass
    return 30.0


def apply(conn):
    conn.execute("ALTER TABLE runs ADD COLUMN records_purged TEXT")
    now = util.iso()
    cutoff = util.iso(util.now() - timedelta(days=_record_days()))
    rows = conn.execute(
        "SELECT id FROM runs WHERE records_purged IS NULL AND COALESCE(finished, started) < ?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        run_id = row[0]
        rec_dir = paths.RUNS_DIR / run_id / "record" if _one_component(run_id) else None
        if rec_dir is None or rec_dir.is_symlink() or not rec_dir.is_dir():
            conn.execute("UPDATE runs SET records_purged = ? WHERE id = ?", (now, run_id))
