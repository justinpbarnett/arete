import json
import re

from arete import paths

PHASE = "backfill"

_INTERRUPTED_RE = re.compile(r"restarting after run (\S+) was interrupted")


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


def _interrupted_source(run_id: str) -> str:
    try:
        text = (paths.RUNS_DIR / run_id / "run.log").read_text(errors="replace")
    except OSError:
        return ""
    match = _INTERRUPTED_RE.search(text)
    return match.group(1) if match else ""


def apply(conn):
    conn.execute("ALTER TABLE runs ADD COLUMN continues TEXT")
    conn.execute("ALTER TABLE runs ADD COLUMN chain TEXT")
    conn.execute("ALTER TABLE runs ADD COLUMN restart TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE runs ADD COLUMN started_by TEXT")
    conn.execute("ALTER TABLE runs ADD COLUMN payer TEXT")
    conn.execute("ALTER TABLE runs ADD COLUMN seq INTEGER")
    conn.execute("""
        CREATE TRIGGER runs_seq_fill AFTER INSERT ON runs WHEN NEW.seq IS NULL
        BEGIN
            UPDATE runs SET seq = (SELECT COALESCE(MAX(seq), 0) + 1 FROM runs) WHERE id = NEW.id;
        END
    """)
    conn.execute("UPDATE runs SET seq = oid")
    conn.execute("CREATE UNIQUE INDEX runs_seq ON runs (seq)")
    conn.execute("CREATE INDEX runs_ticket_seq ON runs (repo, issue, seq)")
    conn.execute("CREATE INDEX runs_chain ON runs (chain, seq)")

    owner_row = conn.execute(
        "SELECT id FROM principals WHERE kind = 'human' AND status = 'active' AND role = 'owner' "
        "ORDER BY created, oid LIMIT 1"
    ).fetchone()
    owner_id = owner_row[0] if owner_row else None

    run_ids = [row[0] for row in conn.execute("SELECT id FROM runs ORDER BY seq")]
    answered_pairs = {
        (row[0], row[1])
        for row in conn.execute("SELECT run, taken_by FROM asks WHERE COALESCE(taken_by, '') != ''")
    }

    continues_map: dict[str, str | None] = {}
    restart_map: dict[str, str] = {}
    for run_id in run_ids:
        lines = _read_lines(run_id)
        route = next((line for line in lines if line.get("event") == "route"), {})
        repair = next((line for line in lines if line.get("event") == "repair"), {})
        interrupted_from = _interrupted_source(run_id)
        continues = (
            str(route.get("continues") or "")
            or str(repair.get("source") or "")
            or interrupted_from
            or None
        )
        if route.get("only"):
            restart = "step"
        elif continues and (continues, run_id) in answered_pairs:
            restart = "answer"
        elif interrupted_from:
            restart = "interrupted"
        elif continues:
            restart = "resume"
        else:
            restart = ""
        continues_map[run_id] = continues
        restart_map[run_id] = restart

    updates = []
    for run_id in run_ids:
        cur = run_id
        seen = {cur}
        while (nxt := continues_map.get(cur)) and nxt not in seen:
            seen.add(nxt)
            cur = nxt
        updates.append((continues_map[run_id], cur, restart_map[run_id], owner_id, run_id))

    conn.executemany(
        "UPDATE runs SET continues = ?, chain = ?, restart = ?, payer = ? WHERE id = ?",
        updates,
    )
