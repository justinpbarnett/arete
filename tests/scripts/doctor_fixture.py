import hashlib
import json
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from arete import agent, alerts, auditor, backup, cli, gh, paths, record, state, util  # noqa: E402
import urllib.request  # noqa: E402

NOTIFIED = paths.STATE_DIR / "notified.jsonl"


def ago(**delta):
    return util.iso(util.now() - timedelta(**delta))


def set_row(run_id, **fields):
    with state.db() as c:
        c.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), run_id))


def age_folder(path, **delta):
    stamp = (util.now() - timedelta(**delta)).timestamp()
    os.utime(path, (stamp, stamp))


def valid_run(run_id, kind="pipeline", cost=0.5):
    record.start(kind, run_id=run_id)
    folder = paths.RUNS_DIR / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run.log").write_text("log\n")
    record.step(run_id, "build", agent.Result(ok=True, cost_usd=cost), 1)
    set_row(run_id, status="done", started=ago(hours=3), log=str(folder / "run.log"))
    age_folder(folder, hours=3)


def clean():
    paths.ensure_dirs()
    valid_run("run-a")
    valid_run("run-b", cost=1.0)
    backup.snapshot("hand")


def faults():
    clean()
    record.start("pipeline", run_id="cost-run")
    (paths.RUNS_DIR / "cost-run").mkdir()
    record.step("cost-run", "build", agent.Result(ok=True, cost_usd=1.25), 1)
    set_row("cost-run", cost_usd=9.99, status="done", started=ago(hours=3))
    age_folder(paths.RUNS_DIR / "cost-run", hours=3)
    record.step("ghost-run", "build", agent.Result(ok=True, cost_usd=0.1), 1)
    for _ in range(2):
        record.step("chat", "chat", agent.Result(ok=True, cost_usd=0.1), 1)
    for name in ("stray-folder", "chat", "eva-x-prompts"):
        (paths.RUNS_DIR / name).mkdir()
        age_folder(paths.RUNS_DIR / name, hours=1)
    record.start("pipeline", run_id="old-pipeline")
    set_row("old-pipeline", started=ago(hours=1), status="done")
    record.start("session", run_id="sess-gone")
    set_row("sess-gone", started=ago(hours=1), status="done", log=str(paths.RUNS_DIR / "sess-gone" / "session.log"))
    record.start("pipeline", run_id="young-row")
    (paths.RUNS_DIR / "young-folder").mkdir()
    age_folder(paths.RUNS_DIR / "young-folder", minutes=1)
    chat_id = state.chat_add("")
    state.chat_claim(chat_id, "hi", "t")
    with state.db() as c:
        c.execute("UPDATE chats SET updated = ? WHERE id = ?", (ago(minutes=30), chat_id))
    valid_run("done-run")
    item = state.enqueue("loop", "q", "g", "trigger", [1])
    state.take_queue_item(item, "done-run")
    approval = state.add_approval("loop", "run-a", {"kind": "x"}, "summary")
    state.claim_approval(approval, util.iso(util.now() + timedelta(hours=1)))
    with state.db() as c:
        c.execute("UPDATE approvals SET decided = ? WHERE id = ?", (ago(hours=2), approval))


def repair():
    with state.db() as c:
        c.execute("DELETE FROM steps WHERE run_id IN ('ghost-run', 'chat')")
        c.execute("DELETE FROM runs WHERE id IN ('sess-gone', 'young-row')")
        c.execute("UPDATE runs SET cost_usd = 1.25 WHERE id = 'cost-run'")
        c.execute("UPDATE chats SET busy = 0")
        c.execute("DELETE FROM queue")
        c.execute("UPDATE approvals SET status = 'done'")
    shutil.rmtree(paths.RUNS_DIR / "stray-folder")
    shutil.rmtree(paths.RUNS_DIR / "young-folder")
    (paths.RUNS_DIR / "old-pipeline").mkdir()
    age_folder(paths.RUNS_DIR / "old-pipeline", hours=1)


def fingerprint():
    stat = paths.DB_FILE.stat()
    print(json.dumps({"sha256": hashlib.sha256(paths.DB_FILE.read_bytes()).hexdigest(), "mtime_ns": stat.st_mtime_ns}))


def heartbeat(*keys, severity="fail"):
    checks = [{"key": k, "ok": False, "detail": "x", "severity": severity} for k in keys]
    util.write_json(paths.HEARTBEAT_FILE, {"ts": util.iso(), "ok": False, "checks": checks, "actions": []})


def offline_cli(argv):
    def forbidden(*a, **k):
        raise AssertionError("network or gh used")

    urllib.request.urlopen = forbidden
    gh.gh = forbidden
    alerts.notify = lambda title, message: NOTIFIED.open("a").write(json.dumps([title, message]) + "\n")
    return cli.main(argv)


def inspect():
    notified = [json.loads(x) for x in NOTIFIED.read_text().splitlines()] if NOTIFIED.exists() else []
    print(json.dumps({"alerts": sorted(a["key"] for a in state.alerts()), "notified": notified,
                      "latest": auditor.latest_findings()}))


def stale_backup(hours=4):
    stamp = (util.now() - timedelta(hours=hours)).timestamp()
    os.utime(backup.newest_snapshot(), (stamp, stamp))


def corrupt_store():
    import sqlite3

    conn = sqlite3.connect(paths.DB_FILE)
    root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'steps_run'").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with open(paths.DB_FILE, "r+b") as f:
        f.seek((root - 1) * page_size)
        f.write(bytes(page_size))


ACTIONS = {
    "clean": clean, "faults": faults, "repair": repair, "fingerprint": fingerprint, "inspect": inspect,
    "heartbeat-cost": lambda: heartbeat("costs:drift"), "heartbeat-backup": lambda: heartbeat("backup:db"),
    "heartbeat-both": lambda: heartbeat("costs:drift", "backup:db"),
    "heartbeat-passing": lambda: util.write_json(paths.HEARTBEAT_FILE, {"checks": [
        {"key": "costs:drift", "ok": True, "detail": "", "severity": "fail"}]}),
    "heartbeat-warn": lambda: heartbeat("costs:drift", severity="warn"),
    "heartbeat-malformed": lambda: paths.HEARTBEAT_FILE.write_text("{not json"),
    "stale-backup": stale_backup, "fresh-backup": lambda: backup.snapshot("hand"),
    "bad-backup-record": lambda: backup.update_last(quick_check="not ok"),
    "no-backup": lambda: [p.unlink() for p in backup.snapshots()],
    "corrupt-store": corrupt_store,
}

if __name__ == "__main__":
    if sys.argv[1] == "cli":
        sys.exit(offline_cli(sys.argv[2:]))
    ACTIONS[sys.argv[1]]()
