import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[2]


def ledger(db):
    with db.reader() as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY n")]


def schema(db):
    with db.reader() as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM sqlite_master ORDER BY name")]


def ownership(root):
    from arete import record, state
    from arete.store import db, migrations
    from arete.store import runs as run_store

    db.migrate()
    owners = migrations.owners()
    counts = Counter(table for tables in owners.values() for table in tables)
    with db.reader() as conn:
        actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    actual.discard("sqlite_sequence")
    assert set(counts) == actual, (set(counts) - actual, actual - set(counts))
    assert all(n == 1 for n in counts.values()), counts
    assert migrations.area_owner("foundation") == "db"
    assert migrations.area_owner("work", "m0004_work_event_routing.py") == "loops"
    before = ledger(db)
    assert all(migrations.area_owner(row[2], row[1]) for row in before), before
    assert next(row for row in before if row[0] == 4)[2] == "loops", before
    db.migrate()
    assert ledger(db) == before
    run = record.start("pipeline", run_id="store-contract")
    record.settle(run, "done")
    assert run_store.run_get(run)["status"] == "done"
    moved = """backfill_legacy_cost cost_drift runs run_facets paid_by_run merged_runs runs_since
               runs_reading_pr_comments open_runs run_lock loop_lock run_get save_report report steps step_tries
               steps_since add_feed insert_feed feed loop_spent_since runs_marked_running ticket_runs newest_whole_run
               ticket_runs_marked_running ab_placements ab_held ticket_runs_since later_run interrupted_tickets
               last_pipeline_starts running_run pending_label issue_running pin_record unpin_record record_pins
               last_step_id""".split()
    assert all(getattr(state, name) is getattr(run_store, name) for name in moved)
    assert all(getattr(run_store, name).__module__ == "arete.store.runs" for name in moved)
    tree = ast.parse(Path(record.__file__).read_text())
    sql = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "PRAGMA ", "REPLACE ", "WITH ")
    assert not [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.lstrip().startswith(sql)]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany", "executescript")]

    with state.db() as conn, db.reader() as joined:
        assert conn is joined
    return {"tables": len(actual), "complete": True, "unique": True, "foundation": "db",
            "historical_work": "loops", "ledger_unchanged": True, "record_compatibility": True}


def unknown_area(root):
    from arete.store import db, migrations

    db.migrate()
    before, before_schema = ledger(db), schema(db)
    store = root / "store"
    store.mkdir()
    for path in migrations.STORE_FOLDER.glob("*.py"):
        shutil.copyfile(path, store / path.name)
    (store / "helper.py").write_text("raise RuntimeError('helper implementation imported')\n")
    refusals = {}
    for area in ("outbox", "helper", "work"):
        folder = root / area
        shutil.copytree(migrations.FOLDER, folder)
        name = f"m{db.LATEST + 1:04d}_{area}_injected.py"
        marker = root / f"{area}-executed"
        (folder / name).write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                                   "PHASE = 'expand'\nSQL = 'CREATE TABLE injected (id INTEGER);'\n")
        (folder / "latest.txt").write_text(name + "\n")
        with mock.patch.object(migrations, "FOLDER", folder), mock.patch.object(migrations, "STORE_FOLDER", store):
            try:
                db.migrate()
            except migrations.Refused as error:
                text = str(error)
                assert name in text and area in text, text
                refusals[area] = text
            else:
                raise AssertionError(f"{area} accepted")
        assert not marker.exists()
        assert ledger(db) == before and schema(db) == before_schema
    assert migrations.area_owner("work", "m0004_work_event_routing.py") == "loops"
    return {"refusals": refusals, "executed": False, "schema_unchanged": True, "ledger_unchanged": True,
            "historical_work": "loops"}


def historical_chain(root, revision, names):
    folder = root / revision.replace("/", "-")
    folder.mkdir()
    location = "arete/store/migrations" if revision == "origin/main" else "arete/migrations"
    for name in names:
        data = subprocess.run(["git", "show", f"{revision}:{location}/{name}"],
                              cwd=SOURCE, capture_output=True, check=True).stdout
        (folder / name).write_bytes(data)
    (folder / "latest.txt").write_text(names[-1] + "\n")
    return folder


def upgrade(root):
    from arete import paths
    from arete.store import db, migrations

    chain = migrations.load()
    names = [m.name for m in chain]
    historical = historical_chain(root, "9c8019c", names[:2])
    relocated = root / "relocated"
    relocated.mkdir()
    for name in names[:2]:
        shutil.copyfile(migrations.FOLDER / name, relocated / name)
        assert (historical / name).read_bytes() == (relocated / name).read_bytes(), name
    (relocated / "latest.txt").write_text(names[1] + "\n")
    with mock.patch.object(migrations, "FOLDER", historical):
        db.migrate()
    original = ledger(db)
    assert len(original) == 2
    with mock.patch.object(migrations, "FOLDER", relocated):
        db.migrate()
    assert ledger(db) == original
    db.migrate()
    full = ledger(db)
    suffix = [m.n for m in chain[2:]]
    assert full[:2] == original and [r[0] for r in full[2:]] == suffix, full
    db.migrate()
    assert ledger(db) == full
    available = subprocess.run(["git", "ls-tree", "--name-only", "origin/main:arete/store/migrations"],
                               cwd=SOURCE, capture_output=True, check=True, text=True).stdout.splitlines()
    old_names = sorted(name for name in available if name.startswith("m") and name.endswith(".py"))
    old_location = historical_chain(root, "origin/main", old_names)
    for name in old_names:
        assert (old_location / name).read_bytes() == (migrations.FOLDER / name).read_bytes(), name
    other = paths.STATE_DIR / "origin-main" / "arete.db"
    other.parent.mkdir()
    with mock.patch.object(paths, "DB_FILE", other):
        with mock.patch.object(migrations, "FOLDER", old_location):
            db.migrate()
        with db.owning_transaction(lambda: db.connect_db(other)) as conn:
            conn.execute("UPDATE schema_migrations SET area='work' WHERE n=4")
        previous = ledger(db)
        db.migrate()
        upgraded = ledger(db)
        assert upgraded[:len(previous)] == previous
        added = [m.n for m in chain if m.name not in old_names]
        assert [row[0] for row in upgraded[len(previous):]] == added
        assert next(row for row in previous if row[0] == 4)[2] == "work"
        db.migrate()
        assert ledger(db) == upgraded
    return {"historical_rows": len(original), "hashes_unchanged": True, "relocated_rows_added": 0,
            "applied_once": suffix, "expected_suffix": suffix, "origin_main_rows_added": len(added),
            "budget_applied_once": added, "historical_work_preserved": True}



def refuses(kind, action):
    try:
        action()
    except kind:
        return
    raise AssertionError(f"expected {kind.__name__}")


def record_readers(root):
    from datetime import timedelta

    from arete import agent, codebase, record, util
    from arete.store import db, runs as run_store
    from tests.harness import age_run

    db.migrate()
    rid = record.start("pipeline", run_id="readers", repo="demo", issue="7", branch="topic", summary="seed")
    row = run_store.run_get(rid)
    assert (row["engine_sha"], row["schema_version"], row["pid"]) == (codebase.STAMP, db.LATEST, os.getpid())
    assert (row["repo"], row["issue"], row["branch"], row["summary"]) == ("demo", 7, "topic", "seed")
    result = agent.Result(ok=True, runner="pi", provider="openai", model="test", cost_usd=1.25,
                          tokens_in=10, tokens_out=4, error="e" * 600, api_seconds=1.237, auth_mode="api_key")
    record.steps(rid, "build", [(result, 2.347), (result, 3)], effort="high", tier="build")
    record.step(rid, "build", result, 4, try_no=9, auth_mode="subscription")
    record.label_step(rid, "build", 2, ab="choice", ab_side="a", atom="build")
    record.hold_ab(rid, "review", "choice", "b", {"model": "test"})
    rows = run_store.steps(rid)
    assert [r["try"] for r in rows] == [1, 2, 9]
    assert (rows[0]["seconds"], rows[0]["api_seconds"], len(rows[0]["error"])) == (2.35, 1.24, 500)
    assert (rows[1]["ab"], rows[1]["ab_side"], rows[1]["atom"]) == ("choice", "a", "build")
    assert run_store.ab_held(rid) == {"review": ("choice", "b", {"model": "test"})}
    assert {r["atom"] for r in run_store.ab_placements("")} == {"build", "review"}
    row = run_store.run_get(rid)
    assert (row["cost_usd"], row["tokens_in"], row["tokens_out"]) == (3.75, 30, 12)
    for runner, mode, provider in (("claude", "subscription", ""), ("pi", "local", ""),
                                   ("replay", "api_key", ""), ("pi", "", "fixture-local")):
        record.step(rid, "classification", agent.Result(ok=True, runner=runner, auth_mode=mode,
                                                       provider=provider, cost_usd=10), 0)
    record.correct(rid, -.25, "api repricing", "api_key")
    record.correct(rid, -1, "subscription repricing")
    record.correct(rid, -2, "local repricing", "local")
    assert run_store.paid_by_run([rid], local_providers=("fixture-local",)) == {rid: 2.25}
    assert not run_store.cost_drift()
    assert run_store.step_tries(rid, "build") == 9
    assert run_store.last_step_id(rid) == run_store.steps(rid)[-1]["id"]
    assert len(run_store.steps_since("")) == len(run_store.steps(rid))
    run_store.save_report(rid, {"summary": "readable"})
    assert run_store.report(rid) == {"summary": "readable"}
    for value in ("broken", "[]", "null"):
        with db.db() as c:
            c.execute("UPDATE reports SET report = ? WHERE run_id = ?", (value, rid))
        assert run_store.report(rid) is None
    assert run_store.report("missing") is None
    run_store.save_report(rid, {"summary": "restored"})
    run_store.add_feed(rid, "note", "a" * 5000 + "tail")
    first, = run_store.feed(rid)
    assert first["text"] == util.both_ends("a" * 5000 + "tail", 4000) and first["ts"]
    run_store.insert_feed(rid, "note", "second")
    assert [r["text"] for r in run_store.feed(rid, first["id"])] == ["second"]
    assert run_store.pin_record(rid) and not run_store.pin_record(rid)
    assert run_store.record_pins() == {rid}
    assert run_store.unpin_record(rid) and not run_store.unpin_record(rid)
    record.note(rid, summary="changed")
    assert not record.move(rid, "done", ()) and not record.move(rid, "done", ("failed",))
    assert record.ask_stop(rid)["stop_asked"]
    assert record.ask_stop("missing") is None
    assert record.move(rid, "pr", ("running",), pr_url="https://example/pr")
    assert run_store.open_runs("demo", "7")[0]["id"] == rid
    assert record.move(rid, "running", ("pr",))
    record.settle(rid, "done", "finished")
    stop_stamp = run_store.run_get(rid)["stop_asked"]
    assert record.ask_stop(rid)["stop_asked"] == stop_stamp
    boundary = util.iso(util.now() - timedelta(days=1))
    upper = util.iso()
    age_run(rid, started=boundary, finished=boundary)
    second = record.start("pipeline", run_id="second", repo="demo", issue="7")
    record.settle(second, "done")
    age_run(second, started=boundary, finished=upper)
    single = record.start("pipeline", run_id="single", repo="demo", issue="7", only_step="build")
    age_run(single, started=boundary)
    assert [r["id"] for r in record.runs_since(boundary, upper)] == [rid]
    assert [r["id"] for r in run_store.runs_since(boundary)] == [rid, second, single]
    assert [r["id"] for r in run_store.ticket_runs("demo", "7", whole=True)] == [second, rid]
    assert run_store.newest_whole_run("demo", "7")["id"] == second
    assert run_store.later_run("demo", "7", rid)["id"] == second
    assert [r["id"] for r in run_store.ticket_runs_marked_running("demo", "7")] == [single]
    fallback = record.start("pipeline", run_id="query-fallback", started=boundary)
    assert record.move(fallback, "done", ("running",))
    assert {r["id"] for r in record.runs_since(boundary, upper)} == {rid, fallback}
    legacy = record.start("pipeline", run_id="legacy", started="2000-01-01T00:00:00Z", cost_usd=7,
                          tokens_in=20, tokens_out=5)
    assert [r["id"] for r in run_store.cost_drift()] == [legacy]
    assert run_store.backfill_legacy_cost({legacy: 5}) == 2
    assert [r["cost_usd"] for r in run_store.steps(legacy)] == [5, 2]
    assert not run_store.cost_drift() and run_store.backfill_legacy_cost() == 0
    refuses(ValueError, lambda: run_store.backfill_legacy_cost({legacy: float("nan")}))
    refuses(ValueError, lambda: run_store.backfill_legacy_cost({"missing": 1}))
    return {"stamps": True, "steps": True, "paid": 2.25, "reports": True, "feed": True, "pins": True,
            "finished_ids": [rid], "history_ids": [rid, second, single], "whole_ids": [second, rid],
            "corrections": True, "backfill": True}


def record_failures(root):
    import sqlite3

    from arete import agent, record
    from arete.store import db, runs as run_store

    db.migrate()
    first = record.start("pipeline", run_id="collision")
    with mock.patch.object(record, "new_id", side_effect=[first, "retried"]):
        assert record.start("pipeline") == "retried"
    refuses(sqlite3.IntegrityError, lambda: record.start("pipeline", run_id=first))
    before = run_store.run_get(first)
    with db.db() as c:
        c.execute("CREATE TRIGGER fail_second BEFORE INSERT ON steps WHEN NEW.cost_usd = 2 "
                  "BEGIN SELECT RAISE(ABORT, 'second step'); END")
    calls = [(agent.Result(ok=True, cost_usd=n, tokens_in=n, tokens_out=n), 0) for n in (1, 2)]
    refuses(sqlite3.IntegrityError, lambda: record.steps(first, "build", calls))
    assert run_store.steps(first) == [] and run_store.run_get(first) == before
    with db.db() as c:
        c.execute("DROP TRIGGER fail_second")
        c.execute("CREATE TRIGGER fail_end BEFORE INSERT ON feed WHEN NEW.kind = 'end' "
                  "BEGIN SELECT RAISE(ABORT, 'end feed'); END")
    with mock.patch.object(record, "mark") as mark:
        refuses(sqlite3.IntegrityError, lambda: record.settle(first, "done", "end"))
        assert run_store.run_get(first) == before and not run_store.feed(first)
        assert not mark.called
        with db.db() as c:
            c.execute("DROP TRIGGER fail_end")
        settled = record.settle(first, "done", "first end", summary="first result")
        assert settled["finished"] and settled["status"] == "done"
        assert mark.call_count == 1
        assert record.settle(first, "failed", "second end") is None
        assert record.settle("missing", "failed", "missing end") is None
        assert mark.call_count == 1 and run_store.run_get(first) == settled
        assert [r["text"] for r in run_store.feed(first)] == ["first end"]
    committed = record.start("pipeline", run_id="timeline-failure")
    with mock.patch.object(record, "mark", side_effect=RuntimeError("timeline")):
        refuses(RuntimeError, lambda: record.settle(committed, "done", "committed"))
    assert run_store.run_get(committed)["status"] == "done" and len(run_store.feed(committed)) == 1
    snapshot = run_store.run_get(first), run_store.steps(first)
    for args in ((float("inf"), "bad", "api_key"), (1, "", "api_key"), (1, "bad", "invalid")):
        refuses(ValueError, lambda: record.correct(first, *args))
    refuses(ValueError, lambda: record.correct("missing", 1, "bad"))
    refuses(ValueError, lambda: record.note(first, status="running"))
    assert (run_store.run_get(first), run_store.steps(first)) == snapshot
    crashed = record.start("pipeline", run_id="crashed")
    error = RuntimeError("crash sentinel")
    try:
        with record.failed_on_crash(crashed):
            raise error
    except RuntimeError as raised:
        assert raised is error
    assert run_store.run_get(crashed)["status"] == "failed"
    assert "crash sentinel" in run_store.feed(crashed)[0]["text"]
    return {"collision_retry": True, "explicit_collision": True, "batch_rollback": True, "end_rollback": True,
            "repeated_settle": True, "missing_settle": True, "committed_timeline_failure": True,
            "invalid_unchanged": True, "crash_reraised": True}

def run_retention(root):
    import sqlite3
    from datetime import timedelta

    from arete import agent, paths, record, state, util
    from arete.store import db, runs as run_store
    from tests.harness import age_run

    db.migrate()

    def made(rid, days, status="done", kind="pipeline"):
        record.start(kind, run_id=rid, repo="demo", issue=rid,
                     **({"loop_kind": "command"} if kind == "loop" else {}))
        if status != "running":
            record.settle(rid, status, rid)
        run_store.add_feed(rid, "note", rid)
        stamp = util.iso(util.now() - timedelta(days=days))
        age_run(rid, started=stamp, finished=stamp if status != "running" else None)
        return rid

    def proposal(rid, status, evidence=False):
        state.add_proposal("other" if evidence else rid,
                           {"repo": "demo", "layer": "repo", "file": "f", "change": "c", "why": "w",
                            "evidence": [rid] if evidence else [], "confidence": .9}, status)

    def inventory():
        with db.reader() as c:
            return {table: [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("runs", "feed", "run_purges")}

    for status in ("done", "skipped"):
        made("old-" + status, 30, status, "loop")
        made("recent-" + status, 1, status, "loop")
    paid = made("paid", 120, kind="loop")
    record.step(paid, "paid", agent.Result(ok=True, cost_usd=1, auth_mode="api_key"), 0)
    made("pipeline", 30)
    record.step("old-done", "history", agent.Result(ok=True, runner="replay"), 0)
    run_store.save_report("old-done", {"summary": "free history survives"})
    for rid in ("expired", "fallback", "pinned", "ask-open", "ask-answered", "ask-taken",
                "proposal-hand", "proposal-evidence", "proposal-merged", "proposal-rejected",
                "proposal-skipped", "proposal-filed", "recent-ticket", "finish-recent"):
        made(rid, 120)
    record.note("fallback", finished=None)
    record.note("finish-recent", finished=util.iso(util.now() - timedelta(days=1)))
    for status in ("running", "pr", "needs-info"):
        made("active-" + status, 120, status)
    record.start("pipeline", run_id="recent-sibling", repo="demo", issue="recent-ticket")
    run_store.pin_record("pinned")
    for suffix in ("open", "answered", "taken"):
        rid = "ask-" + suffix
        state.ask_add(suffix, rid, "demo", rid, "build", "call", "why", "tried", [], {})
        if suffix != "open":
            state.ask_close(suffix, "answered", [])
        if suffix == "taken":
            state.asks_taken([suffix], "consumer")
    proposal("proposal-hand", "hand")
    proposal("proposal-evidence", "hand", evidence=True)
    for status in ("merged", "rejected", "skipped", "filed"):
        proposal("proposal-" + status, status)
    record.step("expired", "history", agent.Result(ok=True, runner="replay"), 0)
    run_store.save_report("expired", {"summary": "history survives"})
    with db.db() as c:
        c.execute("INSERT INTO events (loop, ts) VALUES ('old', ?)",
                  (util.iso(util.now() - timedelta(days=100)),))
        c.execute("CREATE TRIGGER later_owner_failure BEFORE DELETE ON events "
                  "BEGIN SELECT RAISE(ABORT, 'later owner'); END")
    before = inventory()
    refuses(sqlite3.IntegrityError, state.prune)
    assert inventory() == before
    with db.db() as c:
        c.execute("DROP TRIGGER later_owner_failure")
    state.prune()
    all_ids = {r["id"] for r in run_store.runs(firings=True, limit=200)}
    deleted = {"old-done", "old-skipped"}
    assert all_ids == {r[0] for r in before["runs"]} - deleted
    with db.reader() as c:
        assert not list(c.execute("SELECT * FROM feed WHERE run_id IN ('old-done', 'old-skipped')"))
    assert run_store.steps("old-done") and run_store.report("old-done") == {"summary": "free history survives"}
    queued, _ = run_store.purge_inventory()
    expired = {"expired", "fallback", "ask-taken", "proposal-merged", "proposal-rejected",
               "proposal-skipped", "proposal-filed", "paid"}
    assert queued == deleted | expired, queued
    protected = {"pinned", "ask-open", "ask-answered", "proposal-hand", "proposal-evidence", "recent-ticket",
                 "active-running", "active-pr", "active-needs-info", "finish-recent"}
    assert all(run_store.run_get(r)["files_purged"] is None for r in protected)
    assert all(run_store.run_get(r)["files_purged"] for r in expired)
    raw_kept = state.runs_cited_by_kept_proposals(all_ids)
    assert {"proposal-merged", "proposal-hand", "proposal-evidence"} <= raw_kept
    assert not raw_kept & {"proposal-rejected", "proposal-skipped", "proposal-filed"}
    assert run_store.steps("expired") and run_store.report("expired") == {"summary": "history survives"}
    raw = paths.record_dir("proposal-merged") / ("agent" + record.RECORD_SUFFIX)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"raw evidence")
    assert "proposal-merged" in record.kept(record.stored())
    retry_folder = paths.RUNS_DIR / "expired"
    retry_folder.mkdir(exist_ok=True)
    (retry_folder / "payload").write_text("retained until delete succeeds")
    with mock.patch.object(record.shutil, "rmtree", side_effect=OSError("retry sentinel")):
        failed = record.purge_folders()
    assert failed.errors and retry_folder.exists()
    assert "expired" in run_store.purge_inventory()[0]
    unsafe = paths.RUNS_DIR / "unsafe"
    outside = root / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("safe")
    unsafe.symlink_to(outside, target_is_directory=True)
    with db.db() as c:
        c.execute("INSERT INTO run_purges VALUES ('unsafe', 'lines', ?)", (util.iso(),))
    purged = record.purge_folders()
    assert any("unsafe" in e for e in purged.errors)
    assert (outside / "sentinel").read_text() == "safe"
    assert not retry_folder.exists()
    assert run_store.purge_inventory()[0] == {"unsafe"}
    assert run_store.steps("expired") and run_store.report("expired")
    made("custom-free", 6, kind="loop")
    made("custom-folder", 11)
    made("custom-kept", 4)
    state.prune(days=5, lines_days=10)
    assert run_store.run_get("custom-free") is None
    assert run_store.run_get("custom-folder")["files_purged"]
    assert run_store.run_get("custom-kept")["files_purged"] is None
    return {"deleted_ids": sorted(deleted), "retained_ids": sorted(all_ids), "folder_ids": sorted(expired),
            "protected_ids": sorted(protected), "rollback": True, "history_retained": True,
            "merged_raw_kept": True, "custom_windows": True, "deletion_retry": True, "unsafe_refused": True}

def main():
    case = sys.argv[1]
    assert case in ("ownership", "unknown-area", "upgrade", "record-readers", "record-failures", "run-retention"), case
    with tempfile.TemporaryDirectory(prefix="arete-store-contract-") as directory:
        root = Path(directory).resolve()
        env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR")
               if name in os.environ}
        for name, part in (("ARETE_CONFIG_DIR", "config"), ("ARETE_STATE_DIR", "state"),
                           ("ARETE_LOG_DIR", "logs"), ("ARETE_WORKTREES_DIR", "worktrees"),
                           ("TMPDIR", "tmp"), ("HOME", "home")):
            folder = root / part
            folder.mkdir()
            env[name] = str(folder)
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        os.environ.clear()
        os.environ.update(env)
        sys.path.insert(0, str(SOURCE))
        from arete.store import runs as run_store
        from arete import paths, replay

        paths.CONFIG_FILE.write_text("\n".join(replay.sample(root / "sample")) + "\n")
        result = {"case": case, "isolated_paths": {name: os.environ[name] for name in (
            "ARETE_CONFIG_DIR", "ARETE_STATE_DIR", "ARETE_LOG_DIR", "ARETE_WORKTREES_DIR", "TMPDIR", "HOME")}}
        result.update({"ownership": ownership, "unknown-area": unknown_area, "upgrade": upgrade,
                       "record-readers": record_readers, "record-failures": record_failures,
                       "run-retention": run_retention}[case](root))
        print("BEHAVIOR_RESULT=" + json.dumps(result))


if __name__ == "__main__":
    main()
