from arete.store import runs as run_store
from arete.store import db as foundation

rid = record.start("pipeline")
original = foundation.connect_db
sqlite_connect = sqlite3.connect
def quick_connect(*a, **kw):
    kw["timeout"] = .2
    return sqlite_connect(*a, **kw)
def short_wait(*a, **kw):
    with mock.patch.object(sqlite3, "connect", quick_connect):
        return original(*a, **kw)
foundation.connect_db = short_wait
lock = sqlite3.connect(paths.DB_FILE)
def locked(*a, **kw):
    lock.execute("BEGIN EXCLUSIVE")
    return result()
claude.run = locked
try:
    attempt(rid)
    raise AssertionError("write should time out")
except sqlite3.OperationalError as e:
    assert "locked" in str(e)
finally:
    lock.rollback()
    lock.close()
assert rows(rid) == [] and run_store.run_get(rid)["cost_usd"] == 0
assert marks(rid)[0]["cost_usd"] == 2.5 and "cost is not in steps" in marks(rid)[0]["error"]
