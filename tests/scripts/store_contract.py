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
    assert before[-1][2] == "loops", before[-1]
    db.migrate()
    assert ledger(db) == before
    run = record.start("pipeline", run_id="store-contract")
    record.settle(run, "done")
    assert state.run_get(run)["status"] == "done"
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
    for name in names:
        data = subprocess.run(["git", "show", f"{revision}:arete/migrations/{name}"],
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
    assert full[:2] == original and [r[0] for r in full[2:]] == [3, 4], full
    db.migrate()
    assert ledger(db) == full
    old_location = historical_chain(root, "origin/main", names)
    other = paths.STATE_DIR / "origin-main" / "arete.db"
    other.parent.mkdir()
    with mock.patch.object(paths, "DB_FILE", other):
        with mock.patch.object(migrations, "FOLDER", old_location):
            db.migrate()
        with db.db() as conn:
            conn.execute("UPDATE schema_migrations SET area='work' WHERE n=4")
        previous = ledger(db)
        db.migrate()
        assert ledger(db) == previous
        assert previous[-1][2] == "work"
    return {"historical_rows": len(original), "hashes_unchanged": True, "relocated_rows_added": 0,
            "applied_once": [3, 4], "origin_main_rows_added": 0, "historical_work_preserved": True}


def main():
    case = sys.argv[1]
    assert case in ("ownership", "unknown-area", "upgrade"), case
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
        result.update({"ownership": ownership, "unknown-area": unknown_area, "upgrade": upgrade}[case](root))
        print("BEHAVIOR_RESULT=" + json.dumps(result))


if __name__ == "__main__":
    main()
