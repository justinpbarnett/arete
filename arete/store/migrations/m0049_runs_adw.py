import json
import sqlite3

from arete import paths

PHASE = "backfill"


def _read_lines(run_id: str) -> list[dict]:
    try:
        data = (paths.RUNS_DIR / run_id / "run.jsonl").read_bytes()
    except OSError:
        return []
    found: list[dict] = []
    for piece in data.split(b"\n")[:-1]:
        try:
            item = json.loads(piece)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def _path_in_lines(lines: list[dict]) -> str:
    for event in ("execution", "task"):
        for line in lines:
            if line.get("event") == event:
                path = str(line.get("path") or "").strip()
                if path:
                    return path
                break
    return ""


def _route_continues(lines: list[dict]) -> str:
    for line in lines:
        if line.get("event") == "route":
            return str(line.get("continues") or "").strip()
    return ""


def _orchestrated_modes(conn) -> dict[str, str]:
    modes: dict[str, str] = {}
    try:
        rows = conn.execute("SELECT root, current_run, data FROM orchestrations").fetchall()
    except sqlite3.OperationalError:
        return modes
    for root, current_run, raw in rows:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        mode = str(data.get("mode") or "orchestrate") if isinstance(data, dict) else "orchestrate"
        for run_id in (root, current_run):
            if run_id:
                modes[str(run_id)] = mode
    return modes


def apply(conn):
    conn.execute("ALTER TABLE runs ADD COLUMN adw TEXT")
    conn.execute("ALTER TABLE runs ADD COLUMN adw_version TEXT")
    conn.execute("ALTER TABLE runs ADD COLUMN adw_rule TEXT")

    rows = conn.execute("SELECT id, continues FROM runs WHERE kind = 'pipeline' ORDER BY seq").fetchall()
    if not rows:
        return

    continues_by_id = {str(run_id): str(cont or "") for run_id, cont in rows}
    orchestrated = _orchestrated_modes(conn)
    lines_cache: dict[str, list[dict]] = {}

    def get_lines(run_id: str) -> list[dict]:
        if run_id not in lines_cache:
            lines_cache[run_id] = _read_lines(run_id)
        return lines_cache[run_id]

    updates: list[tuple[str, str]] = []
    for run_id, _ in rows:
        run_id = str(run_id)
        if run_id in orchestrated:
            updates.append((orchestrated[run_id], run_id))
            continue
        cur = run_id
        seen: set[str] = set()
        found = ""
        while cur and cur not in seen:
            seen.add(cur)
            if cur in orchestrated:
                found = orchestrated[cur]
                break
            lines = get_lines(cur)
            if path := _path_in_lines(lines):
                found = path
                break
            cur = continues_by_id.get(cur) or _route_continues(lines)
        if found and found != "classic":
            updates.append((found, run_id))

    if updates:
        conn.executemany("UPDATE runs SET adw = ? WHERE id = ?", updates)
