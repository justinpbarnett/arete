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
BASELINE = "56706fe2c07d36fd9d7c00d227f9f68caffeda29"
BEFORE_BUDGET = "0090e09dc89ccca0b96250ce6a2e4e64cb84999b"
AGGREGATE_BASELINE = "5d0900bbb326cf9a896575959e32877c0e969903"


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
               runs_reading_pr_comments open_runs run_lock run_get save_report report steps step_tries
               steps_since add_feed insert_feed feed loop_spent_since runs_marked_running ticket_runs newest_whole_run
               ticket_runs_marked_running ab_placements ab_held ticket_runs_since later_run interrupted_tickets
               last_pipeline_starts running_run pending_label issue_running pin_record unpin_record record_pins
               last_step_id""".split()
    assert all(getattr(state, name) is getattr(run_store, name) for name in moved)
    assert all(getattr(run_store, name).__module__ == "arete.store.runs" for name in moved)
    from arete.store import loops as loops_owner
    names = """loop_state loop_states loop_marks set_loop_state finding_seen record_finding mark_emitted
             set_finding_url findings add_event enqueue pending queue_item take_queue_item
             drop_queue_item release_claimed_items finish_claim end_claim release_settled_claims
             dead_items requeue_item loop_lock""".split()
    assert all(getattr(state, name) is getattr(loops_owner, name) for name in names)
    assert all(getattr(loops_owner, name).__module__ == "arete.store.loops" for name in names)
    from arete.store import sources as sources_owner
    names = """source_seen claim_seen forget_seen cursor set_cursor adopt_source source_owner set_source_owner
             cursors pass_over tickets_passed_over take_passed_over drop_passed_over""".split()
    assert all(getattr(state, name) is getattr(sources_owner, name) for name in names)
    assert all(getattr(sources_owner, name).__module__ == "arete.store.sources" for name in names)
    from arete.store import incidents as incidents_owner
    names = """write_incident record_incident add_incident_note open_incidents incidents_to_decide
             known_fingerprints update_incident woken_incidents closed_alerted add_repair
             set_repair incident_repairs parsed incidents blocked_incidents incident alert_get
             alert_set alert_clear alerts""".split()
    assert all(getattr(state, name) is getattr(incidents_owner, name) for name in names)
    assert all(getattr(incidents_owner, name).__module__ == "arete.store.incidents" for name in names)

    from importlib import import_module

    remaining = {
        "approvals": ("DECIDABLE APPROVAL_NOTES add_approval approvals set_approval_note approval approval_of "
            "claim_approval renew_claim lapsed_claims settle_approval add_proposal RELEASING_PROPOSALS "
            "runs_cited_by_kept_proposals proposals proposal move_proposal forget_proposal "
            "set_proposal_status_by_pr add_mined mined_row mined_urls").split(),
        "chats": ("CHAT_USAGE chat_add chat_get chats chat_messages chat_claim chat_say CHAT_SESSION_COLUMNS "
            "chat_link_session chat_sessions chat_of_session chat_sessions_untold chat_tell_session "
            "chat_summarize chat_drop_draft chat_newest_draft busy_chats").split(),
        "tasks": ("task_add task_get tasks task_children task_set_state task_comment task_comments "
            "tasks_replied").split(),
        "triage": ("TRIAGE_COLUMNS TRIAGE_LISTS APPROVAL_COLUMNS APPROVAL_LISTS triage_rows triage_replace "
            "triage_put triage_drop triage_approve triage_approvals triage_approval triage_prune").split(),
        "devices": ("REFRESH_SCOPE hold save_hold holds clear_holds refresh_links linked_scopes "
            "refresh_continues record_refresh add_vm note_vm_answered forget_vm forget_gone_vm "
            "note_delete_failed vms_of_run vms_left_over probe_get probe_set probes").split(),
        "insights": ("USAGE_COLUMNS add_verdict settle_verdicts set_verdict_sha verdicts_since EVAL_LINEAGE "
            "add_eval BASELINE_SOURCE task_history eval_rows eval_history evals_since FEEDBACK_COLUMNS "
            "feedback_keys add_feedback feedback_due close_feedback feedback_waiting open_feedback "
            "take_feedback settle_feedback feedback_between RESEARCH_COLUMNS research_rows research_get "
            "research_set research_keep add_usage OPEN_CAPTURE EDITABLE_CAPTURE capture_draft "
            "capture_move capture_begin_checks capture_written capture_linked capture_note "
            "capture_filed capture_filed_row capture_filing capture_check_done capture_inspected "
            "capture_end_checks capture_checking capture_evidence capture_stage capture_drop_evidence "
            "capture_save capture_discard usage_since").split(),
        "asks": ("add_message take_messages ask_row ask_add ask_get asks_of open_asks asks_due ask_close "
            "asks_retire asks_taken ask_remind asks_asked_before").split(),
        "push": ("push_add push_list push_drop push_succeeded").split(),
        "_rows": ("lists_read lists_written insert_sql").split(),
    }
    implementations = {}
    for owner, names in remaining.items():
        module = import_module('arete.store.' + owner)
        for name in names:
            value = getattr(module, name)
            assert getattr(state, name) is value, (owner, name)
            if callable(value):
                assert value.__module__ == 'arete.store.' + owner, (owner, name)
        implementations[owner] = names
    assert state.mined_until is run_store.mined_until
    assert run_store.mined_until.__module__ == 'arete.store.runs'
    assert '_rows' not in owners and migrations.area_owner('_rows') is None
    state_tree = ast.parse(Path(state.__file__).read_text())
    assert [n.name for n in state_tree.body if isinstance(n, ast.FunctionDef)] == ['prune']

    reader_results = owner_readers(root)
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
            "historical_work": "loops", "ledger_unchanged": True, "record_compatibility": True,
            "owners": owners, "ledger": before,
            "aggregate_compatibility": True, "implementations": implementations,
            "private_helper_excluded": True, "loop_lock_owner": "loops", "readers": reader_results}


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
    for area in ("outbox", "helper", "_rows", "work"):
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
    private_refusals = {"_rows": refusals.pop("_rows")}
    return {"refusals": refusals, "private_refusals": private_refusals,
            "executed": False, "schema_unchanged": True, "ledger_unchanged": True,
            "historical_work": "loops"}


def historical_chain(root, revision, names):
    folder = root / revision.replace("/", "-")
    folder.mkdir()
    location = "arete/migrations" if revision == "9c8019c" else "arete/store/migrations"
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
    available = subprocess.run(["git", "ls-tree", "--name-only", f"{BEFORE_BUDGET}:arete/store/migrations"],
                               cwd=SOURCE, capture_output=True, check=True, text=True).stdout.splitlines()
    old_names = sorted(name for name in available if name.startswith("m") and name.endswith(".py"))
    old_location = historical_chain(root, BEFORE_BUDGET, old_names)
    for name in old_names:
        assert (old_location / name).read_bytes() == (migrations.FOLDER / name).read_bytes(), name
    other = paths.STATE_DIR / "before-budget" / "arete.db"
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
    from datetime import timedelta

    from arete import agent, paths, record, state, store, util
    from arete.store import db, incidents, runs as run_store
    from tests.harness import age_run

    db.migrate()
    instant = util.now()
    clock = mock.patch.object(util, "now", return_value=instant)
    clock.start()

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
                    for table in ("runs", "feed", "run_purges", "queue", "events", "incidents",
                                  "incident_notes", "incident_aliases", "incident_repairs")}

    for status in ("done", "skipped"):
        made("old-" + status, 30, status, "loop")
        made("recent-" + status, 1, status, "loop")
    paid = made("paid", 120, kind="loop")
    record.step(paid, "paid", agent.Result(ok=True, cost_usd=1, auth_mode="api_key"), 0)
    made("paid-30", 30, kind="loop")
    record.step("paid-30", "paid", agent.Result(ok=True, cost_usd=1, auth_mode="api_key"), 0)
    made("pipeline", 30)
    made("free-equal", 14, kind="loop")
    made("folder-equal", 90)
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
    seed_retention()
    baseline = baseline_database(root, "retention-baseline")
    with mock.patch.object(paths, "DB_FILE", baseline):
        original_function("prune")()
        expected = inventory()
    before = inventory()

    def fail_incidents(c, *args):
        assert not c.execute("SELECT 1 FROM runs WHERE id = 'old-done'").fetchone()
        assert not c.execute("SELECT 1 FROM queue WHERE loop = 'dead-expired'").fetchone()
        assert not c.execute("SELECT 1 FROM events WHERE loop = 'event-old'").fetchone()
        assert c.execute("SELECT 1 FROM run_purges").fetchone()
        raise RuntimeError("incidents prune sentinel")

    with mock.patch.object(incidents, "prune", side_effect=fail_incidents):
        refuses(RuntimeError, store.prune)
    after_failure = inventory()
    assert after_failure == before
    store.prune()
    assert inventory() == expected
    retention = retained_inventory()
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
    protected.add("folder-equal")
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
    store.prune(days=5, keep_days=5, lines_days=10)
    assert_custom_retention()
    custom_retention = snapshot_tables("queue", "events", "incidents", "incident_notes")
    assert run_store.run_get("custom-free") is None
    assert run_store.run_get("custom-folder")["files_purged"]
    assert run_store.run_get("custom-kept")["files_purged"] is None
    clock.stop()
    return {"retention": retention, "custom_retention": custom_retention, "baseline_compatible": True, "rollback_tables": sorted(before),
            "rollback_before": before, "rollback_after": after_failure, "rollback_failure": "incidents prune sentinel",
            "deleted_ids": sorted(deleted), "retained_ids": sorted(all_ids), "folder_ids": sorted(expired),
            "protected_ids": sorted(protected), "rollback": True, "history_retained": True,
            "merged_raw_kept": True, "custom_windows": True, "deletion_retry": True, "unsafe_refused": True}


def snapshot_tables(*tables):
    from arete.store import db

    with db.reader() as c:
        return {table: [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}


def baseline_database(root, name):
    import sqlite3

    from arete import paths
    from arete.store import db

    target = root / (name + ".db")
    with db.reader() as c, sqlite3.connect(target) as other:
        c.backup(other)
    assert target != paths.DB_FILE
    return target


def original_function(name):
    from datetime import timedelta

    from arete import util
    from arete.sighting import Sighting
    from arete.store import db, incidents, runs

    source = subprocess.run(["git", "show", f"{BASELINE}:arete/state.py"], cwd=SOURCE,
                            text=True, capture_output=True, check=True).stdout
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {"db": db.db, "run_store": runs, "util": util, "timedelta": timedelta, "Sighting": Sighting,
                 "SEEN_AGAIN": incidents.SEEN_AGAIN, "CLOSED_AT": incidents.CLOSED_AT,
                 "write_incident": incidents.write_incident}
    if name == "commit_scan":
        write = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "write_incident")
        exec(compile(ast.Module(body=[write], type_ignores=[]), f"<{BASELINE}>", "exec"), namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), f"<{BASELINE}>", "exec"), namespace)
    return namespace[name]


def owner_readers(root):
    from arete import board, config, control, loops, paths, util
    from arete.sighting import Sighting
    from arete.store import db, incidents, loops as loop_store, sources

    folder = paths.CONFIG_DIR / "loops" / "owner-reader"
    folder.mkdir(parents=True)
    (folder / "loop.toml").write_text('kind = "command"\n[run]\ncommand = "true"\n'
                                    '[[source]]\ntype = "command"\ncommand = "true"\n')
    cfg = config.load()
    loop = loops.load(folder, "private", "", cfg)
    loop_store.set_loop_state(loop.name, data={"remember": 7}, mark_at=util.iso(), mark_ok=1, mark_detail="healthy")
    item = loop_store.enqueue(loop.name, "default", "", "event", [{"key": "reader"}])
    loop_store.record_finding(loop.name, "finding", "text", "title", "", "store-contract")
    sources.set_cursor(loop.name + "#0", "position", True)
    incident_id, _ = incidents.record_incident(Sighting("reader", "unexpected", "reader", "", "sig", 1, "e", None))
    incidents.update_incident(incident_id, phase="blocked", waiting="owner")
    incidents.alert_set("reader", 7, "demo")
    with db.db() as c:
        loop_store.add_event(loop.name, {"key": "reader"}, connect=db.db)
        sources.claim_seen("reader", ["one"], connect=db.db)
        assert sources.source_seen("reader", connect=db.reader) == {"one"}
        assert loop_store.loop_states(connect=db.reader)[loop.name]["data"] == {"remember": 7}
        assert incidents.incident(incident_id, connect=db.reader)["notes_total"] == 1
        with db.db() as joined:
            assert c is joined
    with db.one_read(), mock.patch.object(loops, "load_all", return_value=[loop]), \
            mock.patch.object(control, "find_loop", return_value=loop):
        detail = control.loop_detail(loop.name, connect=db.reader)
        checks, alerts = board.checks(), board.alerts()
        blocked = board.checks_and_alerts()["incidents"]["rows"]
    assert detail["state"] == {"remember": 7}
    assert [r["id"] for r in detail["waiting"]] == [item]
    assert [r["fingerprint"] for r in detail["findings"]] == ["finding"]
    assert [r["value"] for r in detail["cursors"]] == ["position"]
    assert checks["loops"] == [{"name": loop.name, "ts": loop_store.loop_marks()[loop.name]["ts"],
                               "ok": True, "detail": "healthy"}]
    assert [(r["key"], r["issue"], r["repo"]) for r in alerts["alerts"]] == [("reader", 7, "demo")]
    assert [r["id"] for r in blocked] == [incident_id]
    return {"state": detail["state"], "waiting": [item], "finding": "finding", "cursor": "position",
            "marks": checks["loops"], "blocked": blocked, "alerts": alerts["alerts"]}


def seed_retention():
    from datetime import timedelta

    from arete import util
    from arete.sighting import Sighting
    from arete.store import db, incidents, loops

    def ago(days):
        return util.iso(util.now() - timedelta(days=days))

    for age in (30, 1, 500):
        for claimed in (False, True):
            name = f"live-{age}-{'claimed' if claimed else 'waiting'}"
            item = loops.enqueue(name, "default", "", "event", [{"key": name}])
            if claimed:
                assert loops.take_queue_item(item, "claimed-run")
            with db.db() as c:
                c.execute("UPDATE queue SET enqueued = ? WHERE id = ?", (ago(age), item))
    for name, age in (("dead-expired", 57), ("dead-recent", 1), ("dead-equal", 56)):
        item = loops.enqueue(name, "default", "", "event", [{"key": name}])
        with db.db() as c:
            c.execute("UPDATE queue SET enqueued = ?, dead_at = ?, run_id = 'claim' WHERE id = ?",
                      (ago(500), ago(age), item))
    for name, age in (("event-old", 70), ("event-recent", 10), ("event-equal", 56)):
        event = loops.add_event(name, {"key": name})
        with db.db() as c:
            c.execute("UPDATE events SET ts = ? WHERE id = ?", (ago(age), event))
    for name, seen, closed in (("inactive", 70, None), ("dismissed", 40, 40),
                               ("open", 1, None), ("recent-close", 70, 1),
                               ("empty-close", 40, ""), ("null-close", 40, None),
                               ("inactive-equal", 56, None), ("closed-equal", 40, 30),
                               ("custom-close", 10, 10)):
        incident_id, _ = incidents.record_incident(Sighting(name, "unexpected", "fixture", "", name, 1, "evidence", None))
        is_closed = name not in ("inactive", "open", "inactive-equal")
        with db.db() as c:
            c.execute("UPDATE incidents SET first_seen = ?, last_seen = ?, state = ?, closed = ? WHERE id = ?",
                      (ago(seen + 1), ago(seen), "dismissed" if is_closed else "open",
                       ago(closed) if isinstance(closed, int) else closed, incident_id))
            c.execute("UPDATE incident_notes SET ts = ?, note = ? WHERE incident = ?",
                      (ago(20), name + " ordinary old", incident_id))
        incidents.add_repair(incident_id, "demo")
        with db.db() as c:
            incidents.write_alias(c, name + "-alias", incident_id, name + " alias")
        if name == "open":
            for note, age in (("ordinary recent", 1), ("seen again: old", 20),
                              ("seen again: recent", 1), ("seen again: equal", 14)):
                incidents.add_incident_note(incident_id, note)
                with db.db() as c:
                    c.execute("UPDATE incident_notes SET ts = ? WHERE id = "
                              "(SELECT MAX(id) FROM incident_notes)", (ago(age),))
    with db.db() as c:
        incidents.write_alias(c, "orphan", 9999, "orphan")
        c.execute("INSERT INTO incident_repairs (incident, repo, started) VALUES (9999, 'demo', ?)", (ago(1),))


def retained_inventory():
    from arete.store import incidents

    found = snapshot_tables("queue", "events", "incidents", "incident_notes", "incident_aliases", "incident_repairs")
    assert sorted(r["loop"] for r in found["queue"]) == sorted(
        [f"live-{age}-{claim}" for age in (30, 1, 500) for claim in ("claimed", "waiting")] +
        ["dead-recent", "dead-equal"])
    assert [r["loop"] for r in found["events"]] == ["event-recent", "event-equal"]
    rows = {r["fingerprint"]: r for r in found["incidents"]}
    assert set(rows) == {"dismissed", "open", "recent-close", "empty-close", "null-close",
                         "inactive-equal", "closed-equal", "custom-close"}
    for name in ("dismissed", "empty-close", "null-close"):
        assert rows[name]["evidence"] == ""
        assert not incidents.incident(rows[name]["id"])["notes"]
    for name in ("recent-close", "inactive-equal", "closed-equal", "custom-close"):
        assert rows[name]["evidence"] == "evidence"
        assert incidents.incident(rows[name]["id"])["notes"]
    notes = [r["note"] for r in incidents.incident(rows["open"]["id"])["notes"]]
    assert notes == ["open ordinary old", "open alias", "ordinary recent", "seen again: recent", "seen again: equal"]
    ids = {r["id"] for r in rows.values()}
    assert all(r["incident"] in ids for table in ("incident_notes", "incident_aliases", "incident_repairs")
               for r in found[table])
    return found


def assert_custom_retention():
    from arete.store import incidents

    rows = {r["fingerprint"]: r for r in incidents.incidents()}
    assert rows["custom-close"]["evidence"] == ""
    assert not incidents.incident(rows["custom-close"]["id"])["notes"]
    assert "inactive-equal" not in rows
    assert "recent-close" in rows and rows["recent-close"]["evidence"] == "evidence"


def scan_atomicity(root):
    from arete import oncall, paths, util
    from arete.sighting import Sighting
    from arete.store import db, incidents, sources

    db.migrate()
    instant = util.now()
    with mock.patch.object(util, "now", return_value=instant):
        def sighting(fp, count=1):
            return Sighting(fp, "unexpected", "fixture", "demo", "sig", count, "evidence", None)
        original, _ = incidents.record_incident(sighting("original"))
        sources.set_cursor("existing", "old", True, started=util.iso())
        tables = ("incidents", "incident_notes", "incident_aliases", "cursors")
        before = snapshot_tables(*tables)
        rows = [sighting("alias", 2), sighting("new"), sighting("original", 3), sighting("last")]
        aliases = [("alias", original, "alias first"), ("second-alias", original, "alias second")]
        marks = {"alias": ({"decision": "wake"}, "must not mark existing"),
                 "new": ({"state": "dismissed", "reason": "marked"}, "new marked")}
        cursors = [("existing", None, False, "x" * 600), ("later", "new", True, "later detail")]
        baseline = baseline_database(root, "scan-baseline")
        with mock.patch.object(paths, "DB_FILE", baseline):
            expected_flags = original_function("commit_scan")(rows, cursors, aliases, marks)
            expected = snapshot_tables(*tables)
        write = sources.write_scan_cursor

        def fails(c, source, *args):
            if source == "later":
                assert c.execute("SELECT value FROM cursors WHERE source = 'existing'").fetchone()[0] is None
                assert c.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 3
                raise RuntimeError("later cursor sentinel")
            write(c, source, *args)

        with mock.patch.object(sources, "write_scan_cursor", side_effect=fails):
            refuses(RuntimeError, lambda: oncall.commit_scan(rows, cursors, aliases, marks))
        after_failure = snapshot_tables(*tables)
        assert after_failure == before
        flags = oncall.commit_scan(rows, cursors, aliases, marks)
        after = snapshot_tables(*tables)
        assert after == expected and flags == expected_flags == [False, True, False, True]
        notes = [r["note"] for r in after["incident_notes"]]
        assert notes == ["opened: unexpected, 1 from fixture", "alias first", "alias second",
                         "seen again: 2 more from fixture, 3 in all", "opened: unexpected, 1 from fixture",
                         "new marked", "seen again: 3 more from fixture, 6 in all",
                         "opened: unexpected, 1 from fixture"]
        assert incidents.incident(original)["decision"] == ""
        new = next(r for r in after["incidents"] if r["fingerprint"] == "new")
        assert new["state"] == "dismissed" and new["closed"] == "" and new["reason"] == "marked"
        cursor = sources.cursor("existing")
        assert cursor["value"] is None and cursor["last_ok"] is None and cursor["ok"] == 0
        assert cursor["detail"] == "x" * 500
        assert sources.cursor("later")["last_ok"] is None
        assert len(after["incidents"]) == 3
    return {"opened": flags, "notes": notes, "baseline_compatible": True, "rollback": before == after_failure,
            "failure": "later cursor sentinel", "before": before, "after_failure": after_failure,
            "committed": after, "cursor_detail_length": len(cursor["detail"]), "new_only_marks": True}


def server_prune(root):
    from dataclasses import replace

    from arete import backup, record, serve, store, tool_events
    from arete.store import db

    db.migrate()
    record.start("pipeline", run_id="backup-history")
    record.settle("backup-history", "done")
    server = serve.Server()
    server.cfg = replace(server.cfg, retention=replace(server.cfg.retention, lines_days=17),
                         oncall=replace(server.cfg.oncall, keep_days=9))
    messages, phases, backups = [], [], []
    server.say = messages.append
    real_prune = store.prune

    def checked_prune(**kwargs):
        latest = backup.newest_snapshot()
        assert latest.exists() and backup.inspect(latest)["quick_check"] == "ok"
        assert backup.inspect(latest)["counts"]["runs"] == 1
        backups.append(str(latest))
        phases.append("store")
        real_prune(**kwargs)

    def records(*args):
        phases.append("records")
        return prune_records(*args)

    def rotate(*args):
        phases.append("rotate")

    prune_records = record.prune
    purge_folders = record.purge_folders

    def folders():
        phases.append("folders")
        return purge_folders()

    try:
        with mock.patch.object(server, "collect_db") as collect, \
                mock.patch.object(server, "copy_off_machine", side_effect=lambda made: phases.append("copy")), \
                mock.patch.object(store, "prune", side_effect=checked_prune) as pruned, \
                mock.patch.object(record, "purge_folders", side_effect=folders), \
                mock.patch.object(record, "prune", side_effect=records), \
                mock.patch.object(tool_events, "rotate_events", side_effect=rotate):
            server.prune()
            pruned.assert_called_once_with(lines_days=17, keep_days=9)
            collect.assert_called_once()
        assert phases == ["copy", "store", "folders", "records", "rotate"], phases
        successful = list(phases)
        phases.clear()
        with mock.patch.object(server, "collect_db") as collect, \
                mock.patch.object(backup, "snapshot", side_effect=OSError("snapshot sentinel")), \
                mock.patch.object(store, "prune") as pruned, \
                mock.patch.object(record, "purge_folders") as purge, \
                mock.patch.object(record, "prune") as records_mock, \
                mock.patch.object(tool_events, "rotate_events") as rotation:
            server.prune()
            assert not any(p.called for p in (pruned, purge, records_mock, rotation))
            collect.assert_called_once()
        assert any("backup failed, so nothing was pruned: OSError: snapshot sentinel" in m for m in messages)
        continuations = {}
        for target, name, message in ((store, "prune", "database sentinel"), (record, "prune", "record sentinel")):
            phases.clear()
            with mock.patch.object(server, "collect_db") as collect, \
                    mock.patch.object(server, "copy_off_machine"), \
                    mock.patch.object(record, "purge_folders", side_effect=folders), \
                    mock.patch.object(record, "prune", side_effect=records), \
                    mock.patch.object(tool_events, "rotate_events", side_effect=rotate), \
                    mock.patch.object(target, name, side_effect=OSError(message)):
                server.prune()
                collect.assert_called_once()
            expected = ["folders", "records", "rotate"] if target is store else ["folders", "rotate"]
            assert phases == expected, phases
            continuations[message] = list(phases)
        assert any("state prune failed: OSError: database sentinel" in m for m in messages)
        assert any("record prune failed: OSError: record sentinel" in m for m in messages)
    finally:
        server.pool.shutdown()
        for pool in server.job_pools.values():
            pool.shutdown()
    return {"backup_checked": True, "backups": backups, "phases": successful,
            "prune_calls": [{"lines_days": 17, "keep_days": 9}], "snapshot_failure_skipped": True,
            "continuations": continuations, "diagnostics": messages, "health_requested": True}


def original_join(name):
    from arete.store import approvals, db, runs

    source = subprocess.run(["git", "show", f"{AGGREGATE_BASELINE}:arete/state.py"], cwd=SOURCE,
                            text=True, capture_output=True, check=True).stdout
    namespace = {"reader": db.reader, "json": json, "run_store": runs, "DECIDABLE": approvals.DECIDABLE}
    helpers = {"approvals": "approval_of", "open_asks": "ask_row"}
    wanted = {name, helpers.get(name)}
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert len(nodes) == (2 if name in helpers else 1)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), f"<{AGGREGATE_BASELINE}>", "exec"), namespace)
    return namespace[name]


def aggregate_joins(root):
    from datetime import UTC, datetime, timedelta

    from arete import record, util
    from arete.store import approvals, asks, db, devices, insights

    db.migrate()
    instant = datetime(2026, 11, 1, 12, tzinfo=UTC)
    quiet = util.iso(instant - timedelta(minutes=10))
    old = util.iso(instant - timedelta(minutes=11))
    recent = util.iso(instant - timedelta(minutes=9))

    def run(name, issue, status="done", repo="demo", kind="pipeline", **fields):
        record.start(kind, run_id=name, issue=issue, repo=repo, status=status, **fields)

    with mock.patch.object(util, "now", return_value=instant):
        run("approval-run", "approval", repo="approval-repo")
        pending = approvals.add_approval("loop", "approval-run", {"nested": [1, "two"]}, "pending")
        failed = approvals.add_approval("loop", "missing", {"missing": True}, "failed", "failure")
        settled = approvals.add_approval("loop", "approval-run", {}, "settled")
        assert approvals.settle_approval(settled, "skipped", "", judged=False, came_from=("pending",))
        approvals.set_approval_note(failed, "review", json.dumps({"ok": False}))
        run("vm-live", "vm", "running")
        run("vm-done", "vm", "done")
        for name, owner, stamp in (("probe-old", None, old), ("probe-equal", None, quiet),
                                   ("probe-new", None, recent), ("live-old", "vm-live", old),
                                   ("done-new", "vm-done", recent), ("missing-new", "missing", recent)):
            devices.add_vm(name, owner, {"kind": "exe", "name": name}, vmid=7)
            with db.db() as c:
                c.execute("UPDATE vms SET created = ? WHERE name = ?", (stamp, name))

        for name, issue in (("ask-whole", "superseded"), ("ask-step", "single-step"),
                            ("ask-unrelated", "unrelated"), ("ask-closed", "closed")):
            run(name, issue, "needs-info")
            asks.ask_add(name, name, "demo", issue, "build", "call", "why", "tried",
                         [{"kind": "text", "question": name}], {"resume": name})
        asks.ask_close("ask-closed", "answered", [{"answer": "done"}])
        asks.ask_add("ask-missing", "missing", "demo", "missing", "build", "call", "why", "tried", [], {})
        run("later-whole", "superseded")
        run("later-step", "single-step", only_step="build")
        run("later-other-issue", "elsewhere")
        run("later-other-repo", "unrelated", repo="other")
        run("later-other-kind", "unrelated", kind="eval")

        feedback_fixture = []

        def feedback(issue, stamp, *, at_once=0, status="new", run_status="pr", repo="demo",
                     run_repo=None, urls=None, run_issue=None, owner=True):
            url = "https://example.test/pull/" + issue
            if owner:
                run("feedback-" + issue, run_issue or issue, run_status, repo=run_repo or repo,
                    pr_url=urls if urls is not None else url)
            row = {k: "" for k in insights.FEEDBACK_COLUMNS}
            row.update(pr_url=url, kind="comment", gh_id=len(feedback_fixture) + 1, repo=repo, issue=issue,
                       body="feedback", line=None, at_once=at_once, created=stamp, pr_state="OPEN", status=status)
            feedback_fixture.append(row)
            return row

        feedback("quiet", old)
        feedback("equal", quiet)
        feedback("noisy", recent)
        feedback("immediate", recent, at_once=1)
        grouped = feedback("grouped", old)
        feedback_fixture.append({**grouped, "gh_id": 999, "created": recent})
        feedback("failed-run", old, run_status="failed")
        feedback("stopped-run", old, run_status="stopped")
        feedback("done-run", old, run_status="done")
        feedback("running-run", old, run_status="running")
        feedback("failed-row", old, status="failed")
        feedback("taken-row", old, status="taken")
        feedback("other-repo", old, repo="other")
        feedback("wrong-run-repo", old, run_repo="other")
        feedback("wrong-run-issue", old, run_issue="different")
        feedback("missing-run", old, owner=False)
        feedback("prefix", old, urls="https://example.test/pull/prefix0")
        feedback("suffix", old, urls="xhttps://example.test/pull/suffix")
        feedback("multi", old, urls="https://example.test/pull/other https://example.test/pull/multi")
        insights.add_feedback(feedback_fixture)

        calls = (("approvals", approvals, approvals.approvals, ()),
                 ("vms_left_over", devices, devices.vms_left_over, (quiet,)),
                 ("open_asks", asks, asks.open_asks, ()),
                 ("feedback_due", insights, insights.feedback_due, ("demo", quiet)))
        inventories, expected, evidence = {}, {}, {}
        for name, owner, function, args in calls:
            expected[name] = original_join(name)(*args, **({} if name == "feedback_due" else {"connect": db.reader}))
            statements = []
            with db.one_read(), db.reader() as c:
                c.set_trace_callback(statements.append)
                try:
                    if name == "feedback_due":
                        with mock.patch.object(owner, "reader", wraps=db.reader) as reader:
                            inventories[name] = function(*args)
                            reader.assert_called_once_with()
                    else:
                        inventories[name] = function(*args, connect=db.reader)
                finally:
                    c.set_trace_callback(None)
            selects = [sql for sql in statements if sql.lstrip().startswith("SELECT ")]
            assert len(selects) == 1, (name, statements)
            assert inventories[name] == expected[name], name
            evidence[name] = {"reader": True, "selects": selects, "listing_selects": len(selects)}

        assert [r["id"] for r in inventories["approvals"]] == [failed, pending]
        assert [r["run_repo"] for r in inventories["approvals"]] == ["", "approval-repo"]
        assert [r["name"] for r in inventories["vms_left_over"]] == ["probe-old", "done-new", "missing-new"]
        assert [r["id"] for r in inventories["open_asks"]] == ["ask-step", "ask-unrelated", "ask-missing"]
        assert inventories["feedback_due"] == ["failed-run", "multi", "quiet", "stopped-run", "equal", "immediate"]
        transactions = approval_transactions(pending)
    return {"baseline": AGGREGATE_BASELINE, "baseline_compatible": True, "inventories": inventories,
            "baseline_inventories": expected, "queries": evidence, "approval_transactions": transactions,
            "feedback_fixture": feedback_fixture, "vm_cutoff": quiet}


def approval_transactions(approval_id):
    from arete.store import approvals, insights

    for kind, outcome in (("push", "waiting"), ("proof", "failed"), ("other", "waiting"),
                          ("push", "approved"), ("proof", "replaced")):
        insights.add_verdict("approval-run", kind, kind + outcome, "about", {}, "", outcome)
    token = approvals.claim_approval(approval_id, "2026-11-01T13:00:00Z")
    assert token
    before = snapshot_tables("approvals", "verdicts")
    write = insights.judge_approval

    def fails(c, *args):
        assert c.execute("SELECT status FROM approvals WHERE id = ?", (approval_id,)).fetchone()[0] == "done"
        write(c, *args)
        raise RuntimeError("insights settlement sentinel")

    def settle(**kwargs):
        return approvals.settle_approval(approval_id, "done", "landed", **kwargs)

    with mock.patch.object(insights, "judge_approval", side_effect=fails):
        refuses(RuntimeError, lambda: settle(judged=True, token=token))
    after_failure = snapshot_tables("approvals", "verdicts")
    assert after_failure == before
    with mock.patch.object(insights, "judge_approval", wraps=write) as judged:
        assert not settle(judged=True, token="wrong")
        assert not settle(judged=True, token=token, until="2026-11-01T14:00:00Z")
        assert not settle(judged=True, came_from=("pending",))
        judged.assert_not_called()
    after_rejected = snapshot_tables("approvals", "verdicts")
    assert after_rejected == before
    unjudged = approvals.add_approval("loop", "approval-run", {}, "unjudged")
    with mock.patch.object(insights, "judge_approval", wraps=write) as judged:
        assert approvals.settle_approval(unjudged, "skipped", "ignored", judged=False, came_from=("pending",))
        judged.assert_not_called()
    after_unjudged = snapshot_tables("verdicts")
    assert after_unjudged["verdicts"] == before["verdicts"]
    assert settle(judged=True, token=token, until="2026-11-01T13:00:00Z")
    committed = snapshot_tables("approvals", "verdicts")
    assert approvals.approval(approval_id)["status"] == "done"
    assert len(committed["verdicts"]) == len(before["verdicts"]) == 5
    for previous, current in zip(before["verdicts"], committed["verdicts"]):
        if previous["kind"] in ("push", "proof") and previous["outcome"] in ("waiting", "failed", "approved"):
            assert current == {**previous, "outcome": "landed", "outcome_at": "2026-11-01T12:00:00Z",
                               "approval": approval_id}
        else:
            assert current == previous
    return {"rollback": True, "before": before, "after_failure": after_failure,
            "after_rejected": after_rejected, "after_unjudged": after_unjudged,
            "rejected_unchanged": True, "unjudged_unchanged": True, "settled": True, "committed": committed}


def main():
    case = sys.argv[1]
    assert case in ("ownership", "unknown-area", "upgrade", "record-readers", "record-failures", "run-retention",
                    "scan-atomicity", "server-prune", "aggregate-joins"), case
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
        from arete import paths, replay

        paths.CONFIG_FILE.write_text("\n".join(replay.sample(root / "sample")) + "\n")
        result = {"case": case, "isolated_paths": {name: os.environ[name] for name in (
            "ARETE_CONFIG_DIR", "ARETE_STATE_DIR", "ARETE_LOG_DIR", "ARETE_WORKTREES_DIR", "TMPDIR", "HOME")}}
        result.update({"ownership": ownership, "unknown-area": unknown_area, "upgrade": upgrade,
                       "record-readers": record_readers, "record-failures": record_failures,
                       "run-retention": run_retention, "scan-atomicity": scan_atomicity,
                       "server-prune": server_prune, "aggregate-joins": aggregate_joins}[case](root))
        print("BEHAVIOR_RESULT=" + json.dumps(result))


if __name__ == "__main__":
    main()
