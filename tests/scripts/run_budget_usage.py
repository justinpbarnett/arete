import contextlib
import json
import shutil
import signal
import sqlite3
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


COUNTERS = ("active_seconds", "verify_seconds", "verify_open_seconds", "repair_calls", "measured_at")
METADATA = {"version", "parents", "left_out", "policy", "specification",
            "wait_minutes_left_out", "earlier_wait_minutes_left_out"}


def expand(root):
    from arete import migrations, state

    original = migrations.FOLDER
    folder = root / "expand-migrations"
    folder.mkdir()
    for file in original.glob("m*.py"):
        if int(file.name[1:5]) <= 5:
            shutil.copyfile(file, folder / file.name)
    (folder / "latest.txt").write_text("m0005_runs_budget_usage.py\n")
    migrations.FOLDER = folder
    state.NEEDED = 5
    state._CHECKED.clear()
    state.migrate()
    return original


def full(folder):
    from arete import migrations, state

    migrations.FOLDER = folder
    state.NEEDED = 6
    state._CHECKED.clear()


def old_run(rid, seconds=200, status="failed", **fields):
    from arete import paths, record, state, util

    finish = util.now() - timedelta(minutes=5)
    record.start("pipeline", run_id=rid, repo="fixture", issue=fields.pop("issue", rid), **fields)
    with state.tx() as conn:
        conn.execute("UPDATE runs SET active_seconds=NULL, verify_seconds=NULL, verify_open_seconds=NULL, "
                     "repair_calls=NULL, measured_at=NULL, status=?, started=?, finished=?, schema_version=5 "
                     "WHERE id=?", (status, util.iso(finish - timedelta(seconds=seconds)),
                                    util.iso(finish) if status != "running" else None, rid))
    (paths.RUNS_DIR / rid).mkdir(parents=True, exist_ok=True)
    return state.run_get(rid)


def saved(rid, **numbers):
    from arete import paths, util

    util.write_json(paths.RUNS_DIR / rid / "budget.json", {"version": 1, "parents": [], **numbers})


def row_values(rid):
    from arete import state

    row = state.run_get(rid)
    return [row[k] for k in COUNTERS]


def budget_after(rid):
    from arete import config, paths, pipeline, record, runlimits, state, tracker

    parent = state.run_get(rid)
    repo = config.Repo(name="fixture", path=paths.STATE_DIR, tracker="local")
    own = record.start("pipeline", repo=repo.name, issue=parent["issue"])
    run = pipeline.Run(own, repo, paths.RUNS_DIR / own, tid=parent["issue"], tracker=tracker.LocalTracker(repo))
    budget = runlimits.Budget(run, runlimits.Policy(), rid)
    record.settle(own, "done")
    return {"totals": [budget.prior_seconds, budget.verify_seconds, budget.prior_repairs],
            "charged": budget.charged, "run": own}


def ledger():
    from arete import state

    with state.reader() as conn:
        return [r[0] for r in conn.execute("SELECT n FROM schema_migrations ORDER BY n")]


def migrations(root):
    from arete import paths, record, run_lines, state, util

    folder = expand(root)
    at = util.iso()
    a = old_run("A", issue="A")
    saved("A", run_seconds=42, run_seconds_at=at, verification_seconds=7,
          verification_open_seconds=2, verification_active_since=at, repair_calls=1)
    b = old_run("B", seconds=300)
    until = util.parse_iso(b["finished"])
    run_lines.waited("B", "limit", util.iso(until - timedelta(seconds=200)),
                     util.iso(until - timedelta(seconds=100)))
    saved("B", verification_seconds=20,
          verification_active_since=util.iso(until - timedelta(seconds=30)), repair_calls=2)
    old_run("C")
    for n in (1, 2):
        run_lines.write("C", "step_start", step="implement", call="review_fix", atom="review_repair", **{"try": n})
    with state.tx() as conn:
        conn.execute("INSERT INTO steps (run_id, step, seconds) VALUES ('C', 'verify', 10)")
        conn.execute("INSERT INTO steps (run_id, step, seconds) VALUES ('C', 'grade', 5)")
    old_run("D", issue=a["issue"])
    saved("D", run_seconds=5, run_seconds_at=at, parents=["A"])
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        old_run("E", status="running", pid=sleeper.pid)
        before = {rid: budget_after(rid) for rid in "ABCD"}
        full(folder)
        assert state.migrate()
        blocked = ledger()
        assert blocked[-1] == 6 and row_values("E") == [None] * 5
        rows = {rid: row_values(rid) for rid in "ABCDE"}
        assert rows["A"] == [42, 7, 2, 1, at]
        assert rows["B"] == [200, 50, 0, 2, ""]
        assert rows["C"] == [200, 15, 0, 2, ""]
        assert rows["D"] == [5, 0, 0, 0, at]
        after = {rid: budget_after(rid) for rid in "ABCD"}
        for rid in before:
            assert all(abs(x - y) <= 1 for x, y in zip(before[rid]["totals"], after[rid]["totals"]))
        assert after["B"]["charged"]["B"]["estimate"]
        assert after["C"]["charged"]["C"]["estimate"]
        sleeper.kill()
        sleeper.wait()
        assert any(r["id"] == "E" for r in record.reap(tickets=True))
        assert not state.migrate()
        assert row_values("E")[-1] == ""
        frozen = {rid: row_values(rid) for rid in "ABCDE"}
        assert not state.migrate()
        assert frozen == {rid: row_values(rid) for rid in frozen}
        for stamp in (None, "", at):
            with state.tx() as conn:
                conn.execute("UPDATE runs SET measured_at=? WHERE id='C'", (stamp,))
        for stamp in ("2026-01-01T00:00:00+00:00", "2026-1-1T00:00:00Z"):
            try:
                with state.tx() as conn:
                    conn.execute("UPDATE runs SET measured_at=? WHERE id='C'", (stamp,))
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError(stamp)
        with state.tx() as conn:
            conn.execute("UPDATE runs SET active_seconds=NULL WHERE id='C'")
        try:
            budget_after("C")
        except ValueError as e:
            refusal = str(e)
            assert "C" in refusal
        else:
            raise AssertionError("NULL counter accepted")
        return {"before": before, "after": after, "backfill": rows, "frozen": frozen,
                "blocked_ledger": blocked, "ledger": ledger(), "null_refusal": refusal,
                "D_budget": json.loads((paths.RUNS_DIR / "D/budget.json").read_text())}
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait()


BAD_BUDGETS = {
    "json": "{broken", "object": "[]", "negative": {"run_seconds": -1},
    "infinity": {"verification_seconds": float("inf")},
    "nan": {"verification_open_seconds": float("nan")},
    "text": {"run_seconds": "forty"}, "repair-fraction": {"repair_calls": 1.5},
    "repair-bool": {"repair_calls": True}, "repair-negative": {"repair_calls": -1},
    "timestamp": {"run_seconds": 1, "run_seconds_at": "2026-01-01T00:00:00+00:00"},
}


def migration_error(root, case):
    from arete import migrations, paths, runlimits, state, util

    folder = expand(root)
    old_run("Good")
    old_run("Bad")
    bad = BAD_BUDGETS[case]
    (paths.RUNS_DIR / "Bad/budget.json").write_text(bad if isinstance(bad, str) else json.dumps(bad))
    full(folder)
    try:
        state.migrate()
    except migrations.Refused as e:
        refusal = str(e)
        assert "Bad" in refusal
    else:
        raise AssertionError("malformed budget accepted")
    state.NEEDED = 5
    state._CHECKED.clear()
    assert ledger()[-1] == 5
    assert row_values("Good") == row_values("Bad") == [None] * 5
    refusals = []
    for field in COUNTERS[:-1]:
        for bad in (-1, "unknown"):
            try:
                with state.tx() as conn:
                    conn.execute(f"UPDATE runs SET {field}=? WHERE id='Good'", (bad,))
            except sqlite3.IntegrityError:
                refusals.append([field, bad])
            else:
                raise AssertionError([field, bad])
    with state.tx() as conn:
        conn.execute("UPDATE runs SET active_seconds=?, measured_at=? WHERE id='Good'", (float("nan"), util.iso()))
    assert row_values("Good")[0] is None
    try:
        runlimits.active_seconds("Good", state.run_get("Good"))
    except ValueError as e:
        nan_refusal = str(e)
        assert "Good" in nan_refusal
    else:
        raise AssertionError("NaN silently reset")
    return {"case": case, "refusal": refusal, "ledger": ledger(), "checks": refusals,
            "nan_is_null": True, "nan_refusal": nan_refusal}


def checkpoints(root):
    from arete import config, paths, pipeline, record, runlimits, state, tracker, util

    folder = expand(root)
    repo = config.Repo(name="fixture", path=root, tracker="local")
    rid = record.start("pipeline", repo=repo.name, issue="probe")
    assert row_values(rid) == [0, 0, 0, 0, state.run_get(rid)["started"]]
    early = record.start("pipeline", repo=repo.name, issue="before-budget")
    early_zero = row_values(early)
    record.interrupt(state.run_get(early))
    assert row_values(early) == early_zero
    other = record.start("eval", repo=repo.name)
    assert row_values(other) == [None] * 5
    run = pipeline.Run(rid, repo, paths.RUNS_DIR / rid, tid="probe", tracker=tracker.LocalTracker(repo))
    clock = [100.0]
    sql = []
    original_tx = state.tx
    @contextlib.contextmanager
    def traced():
        with original_tx() as conn:
            conn.set_trace_callback(sql.append)
            yield conn
    with patch.object(runlimits, "time", SimpleNamespace(monotonic=lambda: clock[0])):
        budget = runlimits.Budget(run, runlimits.Policy(), "")
        clock[0] += 10
        with patch.object(state, "tx", side_effect=sqlite3.OperationalError("fixture write failed")):
            budget.checkpoint()
        assert row_values(rid)[0] == 0
        assert (run.dir / "run.log").read_text().count("budget checkpoint failed:") == 1
        with patch.object(state, "tx", traced):
            budget.checkpoint()
        assert row_values(rid)[0] == 10
        updates = list(dict.fromkeys(s for s in sql if s.startswith("UPDATE")))
        assert len(updates) == 1 and updates[0].startswith("UPDATE runs SET active_seconds=")
        assert not any("INSERT" in s for s in sql)
        compatibility = json.loads((run.dir / "budget.json").read_text())
        assert compatibility["run_seconds"] == row_values(rid)[0]
        entered, release, second_ready = threading.Event(), threading.Event(), threading.Event()
        @contextlib.contextmanager
        def held():
            entered.set()
            assert release.wait(5)
            with original_tx() as conn:
                yield conn
        failures = []
        def checkpoint(second=False):
            try:
                if second:
                    second_ready.set()
                budget.checkpoint()
            except BaseException as e:
                failures.append(str(e))
        with patch.object(state, "tx", held):
            first = threading.Thread(target=checkpoint)
            first.start()
            assert entered.wait(5)
            clock[0] += 10
            second = threading.Thread(target=checkpoint, args=(True,))
            second.start()
            assert second_ready.wait(5)
            release.set()
            first.join(5)
            second.join(5)
        assert not failures and not first.is_alive() and not second.is_alive()
        assert row_values(rid)[0] == 20
        with budget.idling("limit"):
            clock[0] += 20
            with budget.idling("slot"):
                clock[0] += 20
                budget.checkpoint()
                assert row_values(rid)[0] == 20
        with budget.verifying(), budget.idling("limit"):
            clock[0] += 5
            budget.checkpoint()
            assert row_values(rid)[0] == 20 and row_values(rid)[2] == 5
        with budget.idling("ci"):
            clock[0] += 5
            budget.checkpoint()
            assert row_values(rid)[0] == 25
        budget.charge("review_fix1", "review_repair")
        resumed = runlimits.Budget(run, runlimits.Policy(), "")
        assert resumed.run_seconds() == 25 and resumed.repair_calls == 1
        assert resumed.current_verification_seconds == resumed.verify_seconds == 5
        handler = signal.getsignal(signal.SIGALRM)
        timer = signal.getitimer(signal.ITIMER_REAL)
        for exceptional in (False, True):
            observed = threading.Event()
            original = resumed.checkpoint
            def observing():
                original()
                observed.set()
            with patch.object(runlimits, "CHECKPOINT_SECONDS", .01), patch.object(resumed, "checkpoint", observing):
                try:
                    with resumed.watching():
                        assert observed.wait(5)
                        if exceptional:
                            raise RuntimeError("fixture exit")
                except RuntimeError:
                    assert exceptional
            assert signal.getsignal(signal.SIGALRM) == handler
            assert signal.getitimer(signal.ITIMER_REAL) == timer
            assert not any(t.name == f"budget-{rid}" for t in threading.enumerate())
        full(folder)
        assert not state.migrate()
        resumed.checkpoint()
        compact = json.loads((run.dir / "budget.json").read_text())
        assert set(compact) == METADATA
        frozen = row_values(rid)
        with patch.object(util, "now", return_value=util.now() + timedelta(seconds=600)):
            record.settle(rid, "interrupted")
        assert row_values(rid) == frozen
    return {"row": frozen, "startup_zero": early_zero, "non_ticket": row_values(other),
            "compatibility": compatibility, "compact": compact, "sql": sql,
            "failure_log": (run.dir / "run.log").read_text(), "ordered": True,
            "settlement_unchanged": True, "signals_restored": True, "ledger": ledger()}


def main(root, action):
    from arete import replay

    root = Path(root)
    replay.sample(root / "sample")
    if action == "migrations":
        result = migrations(root)
    elif action.startswith("migration-error-"):
        result = migration_error(root, action.removeprefix("migration-error-"))
    else:
        assert action == "checkpoint-errors"
        result = checkpoints(root)
    print("BEHAVIOR_RESULT=" + json.dumps(result))
